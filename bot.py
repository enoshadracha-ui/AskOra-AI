import os
import asyncio
import threading
import logging
import hashlib
import hmac
import json
import time

from datetime import datetime, timezone
from io import BytesIO
from urllib.parse import urlencode, parse_qsl

from flask import Flask, jsonify, render_template, request

import psycopg2
from psycopg2.extras import RealDictCursor

from groq import Groq

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ============================================================
# SETTINGS
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]
RENDER_EXTERNAL_URL = os.environ["RENDER_EXTERNAL_URL"]

PORT = int(os.environ.get("PORT", "10000"))

ADMIN_ID = 7721346673

BOT_USERNAME = "askora_official_bot"
BOT_LINK = f"https://t.me/{BOT_USERNAME}"

TEXT_MODEL = "openai/gpt-oss-120b"
VOICE_MODEL = "whisper-large-v3-turbo"

groq_client = Groq(
    api_key=GROQ_API_KEY
)

app = Flask(__name__)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# AI INSTRUCTION
# ============================================================

SYSTEM_INSTRUCTION = """
You are AskOra, a smart, friendly and concise AI assistant.

Answer the user's question directly.

Keep normal answers SHORT.

Rules:
- Simple questions: 1 to 3 sentences.
- Normal questions: usually 40 to 100 words.
- Use short paragraphs.
- Use short bullet points when useful.
- Avoid unnecessary introductions.
- Avoid repeating the user's question.
- Avoid unnecessary conclusions.
- Do not write essays unless the user specifically asks for detail.
- Keep explanations easy to understand.
- Use Markdown formatting when helpful.

If the user specifically asks for a detailed explanation,
you may provide a longer answer.
"""


# ============================================================
# DATABASE CONNECTION
# ============================================================

def get_connection():
    return psycopg2.connect(DATABASE_URL)


# ============================================================
# DATABASE HELPERS
# ============================================================

def get_table_columns(cur, table_name):
    """
    Return the existing columns for a table.

    This lets AskOra work with older database schemas instead
    of assuming the database is brand new.
    """

    cur.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
    """, (table_name,))

    return {
        row[0]
        for row in cur.fetchall()
    }


def find_old_telegram_id_column(columns):
    """
    Look for common column names used by older AskOra versions.

    We deliberately do NOT automatically use a generic `id`
    column because that is often an internal SERIAL primary key.
    """

    candidates = [
        "user_id",
        "telegram_user_id",
        "telegramid",
        "telegram_user",
        "chat_id",
    ]

    for candidate in candidates:
        if candidate in columns:
            return candidate

    return None


# ============================================================
# DATABASE INITIALIZATION / MIGRATION
# ============================================================

def init_database():

    conn = get_connection()

    try:

        with conn.cursor() as cur:

            # =================================================
            # USERS TABLE
            # =================================================

            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id BIGINT
                )
            """)

            user_columns = get_table_columns(
                cur,
                "users",
            )

            # -------------------------------------------------
            # Add telegram_id if old table doesn't have it
            # -------------------------------------------------

            if "telegram_id" not in user_columns:

                logger.info(
                    "Migrating old users table: adding telegram_id."
                )

                cur.execute("""
                    ALTER TABLE users
                    ADD COLUMN telegram_id BIGINT
                """)

                user_columns.add("telegram_id")

            # -------------------------------------------------
            # Detect an older Telegram ID column
            # -------------------------------------------------

            old_id_column = find_old_telegram_id_column(
                user_columns - {"telegram_id"}
            )

            if old_id_column:

                logger.info(
                    "Found old Telegram ID column: %s",
                    old_id_column,
                )

                # Copy old Telegram IDs into the new column.
                #
                # Identifier is selected only from a fixed,
                # internally-detected list above.
                cur.execute(
                    f"""
                    UPDATE users
                    SET telegram_id = "{old_id_column}"
                    WHERE telegram_id IS NULL
                      AND "{old_id_column}" IS NOT NULL
                    """
                )

            # -------------------------------------------------
            # Add user profile columns
            # -------------------------------------------------

            required_user_columns = {
                "username": "TEXT",
                "first_name": "TEXT",
                "last_name": "TEXT",
                "first_seen": "TIMESTAMPTZ",
                "created_at": "TIMESTAMPTZ",
                "last_seen": "TIMESTAMPTZ",
            }

            for column_name, column_type in required_user_columns.items():

                if column_name not in user_columns:

                    logger.info(
                        "Adding users.%s",
                        column_name,
                    )

                    cur.execute(
                        f"""
                        ALTER TABLE users
                        ADD COLUMN "{column_name}" {column_type}
                        """
                    )

            # -------------------------------------------------
            # Repair missing timestamps
            # -------------------------------------------------

            cur.execute("""
                UPDATE users
                SET first_seen = COALESCE(
                    first_seen,
                    created_at,
                    NOW()
                )
                WHERE first_seen IS NULL
            """)

            cur.execute("""
                UPDATE users
                SET created_at = COALESCE(
                    created_at,
                    first_seen,
                    NOW()
                )
                WHERE created_at IS NULL
            """)

            cur.execute("""
                UPDATE users
                SET last_seen = COALESCE(
                    last_seen,
                    created_at,
                    NOW()
                )
                WHERE last_seen IS NULL
            """)

            # -------------------------------------------------
            # Defaults
            # -------------------------------------------------

            cur.execute("""
                ALTER TABLE users
                ALTER COLUMN first_seen
                SET DEFAULT NOW()
            """)

            cur.execute("""
                ALTER TABLE users
                ALTER COLUMN created_at
                SET DEFAULT NOW()
            """)

            cur.execute("""
                ALTER TABLE users
                ALTER COLUMN last_seen
                SET DEFAULT NOW()
            """)

            # -------------------------------------------------
            # Create unique index for Telegram IDs
            #
            # NULL values are allowed, so old rows that cannot
            # be mapped still remain in the database.
            # -------------------------------------------------

            # Telegram IDs should be unique. If an old migration
            # somehow copied duplicate IDs, keep one record per
            # Telegram ID before creating the unique index.
            cur.execute("""
                DELETE FROM users a
                USING users b
                WHERE a.telegram_id IS NOT NULL
                  AND a.telegram_id = b.telegram_id
                  AND a.ctid > b.ctid
            """)

            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                users_telegram_id_unique_idx
                ON users (telegram_id)
            """)

            # =================================================
            # USAGE EVENTS
            # =================================================

            cur.execute("""
                CREATE TABLE IF NOT EXISTS usage_events (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    event_type TEXT,
                    created_at TIMESTAMPTZ
                )
            """)

            usage_columns = get_table_columns(
                cur,
                "usage_events",
            )

            if "telegram_id" not in usage_columns:

                cur.execute("""
                    ALTER TABLE usage_events
                    ADD COLUMN telegram_id BIGINT
                """)

            if "event_type" not in usage_columns:

                cur.execute("""
                    ALTER TABLE usage_events
                    ADD COLUMN event_type TEXT
                """)

            if "created_at" not in usage_columns:

                cur.execute("""
                    ALTER TABLE usage_events
                    ADD COLUMN created_at TIMESTAMPTZ
                """)

            cur.execute("""
                UPDATE usage_events
                SET created_at = NOW()
                WHERE created_at IS NULL
            """)

            cur.execute("""
                ALTER TABLE usage_events
                ALTER COLUMN created_at
                SET DEFAULT NOW()
            """)

            # =================================================
            # CONVERSATIONS
            # =================================================

            cur.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    role TEXT,
                    content TEXT,
                    created_at TIMESTAMPTZ
                )
            """)

            conversation_columns = get_table_columns(
                cur,
                "conversations",
            )

            if "telegram_id" not in conversation_columns:

                cur.execute("""
                    ALTER TABLE conversations
                    ADD COLUMN telegram_id BIGINT
                """)

            if "role" not in conversation_columns:

                cur.execute("""
                    ALTER TABLE conversations
                    ADD COLUMN role TEXT
                """)

            if "content" not in conversation_columns:

                cur.execute("""
                    ALTER TABLE conversations
                    ADD COLUMN content TEXT
                """)

            if "created_at" not in conversation_columns:

                cur.execute("""
                    ALTER TABLE conversations
                    ADD COLUMN created_at TIMESTAMPTZ
                """)

            cur.execute("""
                UPDATE conversations
                SET created_at = NOW()
                WHERE created_at IS NULL
            """)

            cur.execute("""
                ALTER TABLE conversations
                ALTER COLUMN created_at
                SET DEFAULT NOW()
            """)

        conn.commit()

        logger.info(
            "AskOra database initialized and migrated successfully."
        )

    except Exception:

        conn.rollback()

        logger.exception(
            "Database initialization failed."
        )

        raise

    finally:

        conn.close()


# ============================================================
# USER RECORDING
# ============================================================

def record_user(
    telegram_id,
    username=None,
    first_name=None,
    last_name=None,
):

    conn = get_connection()

    try:

        with conn.cursor() as cur:

            # ------------------------------------------------
            # Check whether this Telegram user already exists.
            #
            # We don't rely on telegram_id being the primary key;
            # the migration supports older AskOra databases.
            # ------------------------------------------------

            cur.execute("""
                SELECT telegram_id
                FROM users
                WHERE telegram_id = %s
                LIMIT 1
            """, (telegram_id,))

            existing = cur.fetchone()

            if existing:

                cur.execute("""
                    UPDATE users
                    SET
                        username = %s,
                        first_name = %s,
                        last_name = %s,
                        last_seen = NOW()
                    WHERE telegram_id = %s
                """, (
                    username,
                    first_name,
                    last_name,
                    telegram_id,
                ))

            else:

                cur.execute("""
                    INSERT INTO users (
                        telegram_id,
                        username,
                        first_name,
                        last_name,
                        first_seen,
                        created_at,
                        last_seen
                    )
                    VALUES (
                        %s,
                        %s,
                        %s,
                        %s,
                        NOW(),
                        NOW(),
                        NOW()
                    )
                """, (
                    telegram_id,
                    username,
                    first_name,
                    last_name,
                ))

        conn.commit()

    except Exception:

        conn.rollback()

        logger.exception(
            "record_user failed."
        )

        raise

    finally:

        conn.close()


# ============================================================
# USAGE
# ============================================================

def record_usage(
    telegram_id,
    event_type,
):

    conn = get_connection()

    try:

        with conn.cursor() as cur:

            cur.execute("""
                INSERT INTO usage_events (
                    telegram_id,
                    event_type,
                    created_at
                )
                VALUES (
                    %s,
                    %s,
                    NOW()
                )
            """, (
                telegram_id,
                event_type,
            ))

        conn.commit()

    except Exception:

        conn.rollback()

        logger.exception(
            "record_usage failed."
        )

        raise

    finally:

        conn.close()


# ============================================================
# CONVERSATION
# ============================================================

def save_message(
    telegram_id,
    role,
    content,
):

    conn = get_connection()

    try:

        with conn.cursor() as cur:

            cur.execute("""
                INSERT INTO conversations (
                    telegram_id,
                    role,
                    content,
                    created_at
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    NOW()
                )
            """, (
                telegram_id,
                role,
                content,
            ))

        conn.commit()

    except Exception:

        conn.rollback()

        logger.exception(
            "save_message failed."
        )

        raise

    finally:

        conn.close()


def get_history(telegram_id):

    conn = get_connection()

    try:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute("""
                SELECT
                    role,
                    content
                FROM conversations
                WHERE telegram_id = %s
                ORDER BY created_at ASC, id ASC
            """, (telegram_id,))

            return cur.fetchall()

    finally:

        conn.close()


def clear_history(telegram_id):

    conn = get_connection()

    try:

        with conn.cursor() as cur:

            cur.execute("""
                DELETE FROM conversations
                WHERE telegram_id = %s
            """, (telegram_id,))

        conn.commit()

    except Exception:

        conn.rollback()

        logger.exception(
            "clear_history failed."
        )

        raise

    finally:

        conn.close()


# ============================================================
# ADMIN STATISTICS
# ============================================================

def get_statistics():

    conn = get_connection()

    try:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            # ------------------------------------------------
            # Total users
            # ------------------------------------------------

            cur.execute("""
                SELECT COUNT(*) AS count
                FROM users
                WHERE telegram_id IS NOT NULL
            """)

            total_users = cur.fetchone()["count"]

            # ------------------------------------------------
            # New users today
            # ------------------------------------------------

            cur.execute("""
                SELECT COUNT(*) AS count
                FROM users
                WHERE telegram_id IS NOT NULL
                  AND created_at >= CURRENT_DATE
            """)

            users_today = cur.fetchone()["count"]

            # ------------------------------------------------
            # New users this week
            # ------------------------------------------------

            cur.execute("""
                SELECT COUNT(*) AS count
                FROM users
                WHERE telegram_id IS NOT NULL
                  AND created_at >= NOW() - INTERVAL '7 days'
            """)

            users_week = cur.fetchone()["count"]

            # ------------------------------------------------
            # Active users last 7 days
            # ------------------------------------------------

            cur.execute("""
                SELECT COUNT(*) AS count
                FROM users
                WHERE telegram_id IS NOT NULL
                  AND last_seen >= NOW() - INTERVAL '7 days'
            """)

            active_users = cur.fetchone()["count"]

            # ------------------------------------------------
            # Total messages
            # ------------------------------------------------

            cur.execute("""
                SELECT COUNT(*) AS count
                FROM conversations
                WHERE telegram_id IS NOT NULL
            """)

            total_messages = cur.fetchone()["count"]

            # ------------------------------------------------
            # Messages today
            # ------------------------------------------------

            cur.execute("""
                SELECT COUNT(*) AS count
                FROM conversations
                WHERE created_at >= CURRENT_DATE
            """)

            messages_today = cur.fetchone()["count"]

            # ------------------------------------------------
            # Text requests
            # ------------------------------------------------

            cur.execute("""
                SELECT COUNT(*) AS count
                FROM usage_events
                WHERE event_type IN (
                    'text',
                    'mini_app_text'
                )
            """)

            text_requests = cur.fetchone()["count"]

            # ------------------------------------------------
            # Voice requests
            # ------------------------------------------------

            cur.execute("""
                SELECT COUNT(*) AS count
                FROM usage_events
                WHERE event_type IN (
                    'voice',
                    'mini_app_voice'
                )
            """)

            voice_requests = cur.fetchone()["count"]

            # ------------------------------------------------
            # Total events
            # ------------------------------------------------

            cur.execute("""
                SELECT COUNT(*) AS count
                FROM usage_events
            """)

            total_events = cur.fetchone()["count"]

            # ------------------------------------------------
            # Recent users
            # ------------------------------------------------

            cur.execute("""
                SELECT
                    telegram_id,
                    username,
                    first_name,
                    last_name,
                    created_at,
                    last_seen
                FROM users
                WHERE telegram_id IS NOT NULL
                ORDER BY last_seen DESC NULLS LAST
                LIMIT 15
            """)

            recent_users = cur.fetchall()

            users_list = []

            for user in recent_users:

                first_name = (
                    user["first_name"] or ""
                )

                last_name = (
                    user["last_name"] or ""
                )

                full_name = (
                    f"{first_name} {last_name}"
                ).strip()

                if not full_name:
                    full_name = "Unknown"

                created_at = user["created_at"]
                last_seen = user["last_seen"]

                users_list.append({
                    "telegram_id": user["telegram_id"],
                    "username": user["username"],
                    "name": full_name,
                    "created_at": (
                        created_at.strftime(
                            "%Y-%m-%d %H:%M"
                        )
                        if created_at
                        else "Unknown"
                    ),
                    "last_seen": (
                        last_seen.strftime(
                            "%Y-%m-%d %H:%M"
                        )
                        if last_seen
                        else "Unknown"
                    ),
                })

            return {
                "total_users": total_users,
                "users_today": users_today,
                "users_week": users_week,
                "active_users": active_users,
                "total_messages": total_messages,
                "messages_today": messages_today,
                "text_requests": text_requests,
                "voice_requests": voice_requests,
                "total_events": total_events,
                "recent_users": users_list,
            }

    finally:

        conn.close()


# ============================================================
# AI
# ============================================================

def generate_answer(messages):

    response = groq_client.chat.completions.create(
        model=TEXT_MODEL,
        messages=messages,
        temperature=0.5,
        max_tokens=450,
    )

    return response.choices[0].message.content.strip()


def transcribe_audio(audio_bytes):

    result = groq_client.audio.transcriptions.create(
        file=(
            "voice.webm",
            BytesIO(audio_bytes),
        ),
        model=VOICE_MODEL,
    )

    return result.text.strip()


# ============================================================
# INVITE
# ============================================================

def invite_button():

    share_url = (
        "https://t.me/share/url?"
        + urlencode({
            "url": BOT_LINK,
            "text": (
                "Try AskOra 🤖 — "
                "a free AI assistant on Telegram!"
            ),
        })
    )

    keyboard = [
        [
            InlineKeyboardButton(
                "✨ Invite a Friend",
                url=share_url,
            )
        ]
    ]

    return InlineKeyboardMarkup(
        keyboard
    )


# ============================================================
# TELEGRAM LONG MESSAGE
# ============================================================

async def send_long_message(
    bot,
    chat_id,
    text,
):

    max_length = 4000

    if len(text) <= max_length:

        await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=invite_button(),
        )

        return

    chunks = []

    while text:

        chunk = text[:max_length]

        text = text[max_length:]

        chunks.append(chunk)

    for index, chunk in enumerate(chunks):

        if index == len(chunks) - 1:

            await bot.send_message(
                chat_id=chat_id,
                text=chunk,
                reply_markup=invite_button(),
            )

        else:

            await bot.send_message(
                chat_id=chat_id,
                text=chunk,
            )


# ============================================================
# /START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    record_user(
        user.id,
        user.username,
        user.first_name,
        user.last_name,
    )

    await update.message.reply_text(
        "👋 Welcome to AskOra!\n\n"
        "🤖 Your simple AI assistant.\n"
        "Ask me anything — by text or voice. 🎤",
        reply_markup=invite_button(),
    )


# ============================================================
# /RESET
# ============================================================

async def reset(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    clear_history(
        user_id
    )

    await update.message.reply_text(
        "🧹 Your AskOra conversation has been cleared."
    )


# ============================================================
# /ADMIN
# ============================================================

async def admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if update.effective_user.id != ADMIN_ID:

        await update.message.reply_text(
            "❌ You are not authorized to access the admin dashboard."
        )

        return

    try:

        stats = get_statistics()

        text = (
            "🛠️ ASKORA ADMIN DASHBOARD\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"

            "📊 OVERVIEW\n"
            f"👥 Total users: {stats['total_users']}\n"
            f"🆕 New today: {stats['users_today']}\n"
            f"📅 New in 7 days: {stats['users_week']}\n"
            f"🟢 Active in 7 days: {stats['active_users']}\n\n"

            "💬 AI USAGE\n"
            f"💬 Total messages: {stats['total_messages']}\n"
            f"📨 Messages today: {stats['messages_today']}\n"
            f"⌨️ Text requests: {stats['text_requests']}\n"
            f"🎤 Voice requests: {stats['voice_requests']}\n"
            f"📈 Total events: {stats['total_events']}\n\n"

            "👤 RECENT USERS\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
        )

        if not stats["recent_users"]:

            text += "\nNo users yet."

        else:

            for user in stats["recent_users"]:

                username = user["username"]

                if username:

                    username_text = (
                        f"@{username}"
                    )

                else:

                    username_text = (
                        "No username"
                    )

                text += (
                    f"\n👤 {user['name']}\n"
                    f"   {username_text}\n"
                    f"   🆔 {user['telegram_id']}\n"
                    f"   🕐 Last seen: {user['last_seen']}\n"
                )

        await update.message.reply_text(
            text
        )

    except Exception:

        logger.exception(
            "Admin dashboard error."
        )

        await update.message.reply_text(
            "⚠️ Unable to load the admin dashboard."
        )


# ============================================================
# TELEGRAM VOICE
# ============================================================

async def handle_voice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    record_user(
        user.id,
        user.username,
        user.first_name,
        user.last_name,
    )

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action="typing",
    )

    try:

        voice = update.message.voice

        file = await context.bot.get_file(
            voice.file_id
        )

        audio_bytes = (
            await file.download_as_bytearray()
        )

        transcription = transcribe_audio(
            bytes(audio_bytes)
        )

        if not transcription:

            await update.message.reply_text(
                "⚠️ I couldn't understand the voice message."
            )

            return

        record_usage(
            user.id,
            "voice",
        )

        save_message(
            user.id,
            "user",
            transcription,
        )

        history = get_history(
            user.id
        )

        messages = [
            {
                "role": "system",
                "content": SYSTEM_INSTRUCTION,
            }
        ]

        messages.extend(
            {
                "role": item["role"],
                "content": item["content"],
            }
            for item in history[-10:]
        )

        answer = generate_answer(
            messages
        )

        save_message(
            user.id,
            "assistant",
            answer,
        )

        await send_long_message(
            context.bot,
            update.effective_chat.id,
            answer,
        )

    except Exception:

        logger.exception(
            "Voice processing error."
        )

        await update.message.reply_text(
            "⚠️ Sorry, I couldn't process your voice message."
        )


# ============================================================
# TELEGRAM TEXT
# ============================================================

async def chat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    message = (
        update.message.text or ""
    ).strip()

    if not message:
        return

    record_user(
        user.id,
        user.username,
        user.first_name,
        user.last_name,
    )

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action="typing",
    )

    try:

        record_usage(
            user.id,
            "text",
        )

        save_message(
            user.id,
            "user",
            message,
        )

        history = get_history(
            user.id
        )

        messages = [
            {
                "role": "system",
                "content": SYSTEM_INSTRUCTION,
            }
        ]

        messages.extend(
            {
                "role": item["role"],
                "content": item["content"],
            }
            for item in history[-10:]
        )

        answer = generate_answer(
            messages
        )

        save_message(
            user.id,
            "assistant",
            answer,
        )

        await send_long_message(
            context.bot,
            update.effective_chat.id,
            answer,
        )

    except Exception:

        logger.exception(
            "Telegram chat error."
        )

        await update.message.reply_text(
            "⚠️ Sorry, something went wrong. Please try again."
        )


# ============================================================
# MINI APP INIT DATA VALIDATION
# ============================================================

def validate_init_data(init_data):

    if not init_data:
        return None

    try:

        parsed = dict(
            parse_qsl(
                init_data,
                keep_blank_values=True,
            )
        )

        received_hash = parsed.pop(
            "hash",
            None,
        )

        if not received_hash:
            return None

        data_check_string = "\n".join(
            f"{key}={value}"
            for key, value in sorted(
                parsed.items()
            )
        )

        secret_key = hmac.new(
            b"WebAppData",
            TELEGRAM_BOT_TOKEN.encode(),
            hashlib.sha256,
        ).digest()

        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(
            calculated_hash,
            received_hash,
        ):
            return None

        auth_date = parsed.get(
            "auth_date"
        )

        if auth_date:

            if (
                time.time()
                - int(auth_date)
                > 86400
            ):
                return None

        user_data = parsed.get(
            "user"
        )

        if not user_data:
            return None

        return json.loads(
            user_data
        )

    except Exception:

        logger.exception(
            "Mini App validation error."
        )

        return None


def get_mini_app_user():

    init_data = request.headers.get(
        "X-Telegram-Init-Data",
        "",
    )

    return validate_init_data(
        init_data
    )


# ============================================================
# MINI APP API - HISTORY
# ============================================================

@app.get("/api/history")
def api_history():

    user = get_mini_app_user()

    if not user:

        return jsonify({
            "error": "Unauthorized"
        }), 401

    telegram_id = int(
        user["id"]
    )

    try:

        record_user(
            telegram_id,
            user.get("username"),
            user.get("first_name"),
            user.get("last_name"),
        )

        history = get_history(
            telegram_id
        )

        return jsonify({
            "history": [
                {
                    "role": item["role"],
                    "content": item["content"],
                }
                for item in history
            ]
        })

    except Exception:

        logger.exception(
            "Mini App history error."
        )

        return jsonify({
            "error": "Unable to load history."
        }), 500


# ============================================================
# MINI APP API - CHAT
# ============================================================

@app.post("/api/chat")
def api_chat():

    user = get_mini_app_user()

    if not user:

        return jsonify({
            "error": "Unauthorized"
        }), 401

    telegram_id = int(
        user["id"]
    )

    data = request.get_json(
        silent=True
    ) or {}

    message = str(
        data.get("message", "")
    ).strip()

    if not message:

        return jsonify({
            "error": "Message is required."
        }), 400

    try:

        record_user(
            telegram_id,
            user.get("username"),
            user.get("first_name"),
            user.get("last_name"),
        )

        record_usage(
            telegram_id,
            "mini_app_text",
        )

        save_message(
            telegram_id,
            "user",
            message,
        )

        history = get_history(
            telegram_id
        )

        messages = [
            {
                "role": "system",
                "content": SYSTEM_INSTRUCTION,
            }
        ]

        messages.extend(
            {
                "role": item["role"],
                "content": item["content"],
            }
            for item in history[-10:]
        )

        answer = generate_answer(
            messages
        )

        save_message(
            telegram_id,
            "assistant",
            answer,
        )

        return jsonify({
            "answer": answer
        })

    except Exception:

        logger.exception(
            "Mini App chat error."
        )

        return jsonify({
            "error": "Unable to generate answer."
        }), 500


# ============================================================
# MINI APP API - VOICE
# ============================================================

@app.post("/api/voice")
def api_voice():

    user = get_mini_app_user()

    if not user:

        return jsonify({
            "error": "Unauthorized"
        }), 401

    telegram_id = int(
        user["id"]
    )

    if "audio" not in request.files:

        return jsonify({
            "error": "Audio file is required."
        }), 400

    try:

        record_user(
            telegram_id,
            user.get("username"),
            user.get("first_name"),
            user.get("last_name"),
        )

        audio_file = request.files["audio"]

        audio_bytes = audio_file.read()

        transcription = transcribe_audio(
            audio_bytes
        )

        if not transcription:

            return jsonify({
                "error": "Could not understand audio."
            }), 400

        record_usage(
            telegram_id,
            "mini_app_voice",
        )

        save_message(
            telegram_id,
            "user",
            transcription,
        )

        history = get_history(
            telegram_id
        )

        messages = [
            {
                "role": "system",
                "content": SYSTEM_INSTRUCTION,
            }
        ]

        messages.extend(
            {
                "role": item["role"],
                "content": item["content"],
            }
            for item in history[-10:]
        )

        answer = generate_answer(
            messages
        )

        save_message(
            telegram_id,
            "assistant",
            answer,
        )

        return jsonify({
            "transcription": transcription,
            "answer": answer,
        })

    except Exception:

        logger.exception(
            "Mini App voice error."
        )

        return jsonify({
            "error": "Unable to process voice."
        }), 500


# ============================================================
# MINI APP API - CLEAR
# ============================================================

@app.post("/api/reset")
def api_reset():

    user = get_mini_app_user()

    if not user:

        return jsonify({
            "error": "Unauthorized"
        }), 401

    telegram_id = int(
        user["id"]
    )

    try:

        clear_history(
            telegram_id
        )

        return jsonify({
            "success": True
        })

    except Exception:

        logger.exception(
            "Mini App reset error."
        )

        return jsonify({
            "error": "Unable to clear conversation."
        }), 500


# ============================================================
# HOME
# ============================================================

@app.get("/")
def home():

    return render_template(
        "index.html"
    )


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():

    return jsonify({
        "status": "online",
        "bot": "AskOra",
        "ai": "Groq",
    })


# ============================================================
# WEBHOOK
# ============================================================

async def process_update(
    update_data
):

    update = Update.de_json(
        update_data,
        telegram_application.bot,
    )

    await telegram_application.process_update(
        update
    )


@app.post("/webhook")
def webhook():

    try:

        update_data = request.get_json(
            force=True
        )

        asyncio.run_coroutine_threadsafe(
            process_update(update_data),
            telegram_loop,
        )

        return jsonify({
            "ok": True
        })

    except Exception:

        logger.exception(
            "Webhook error."
        )

        return jsonify({
            "ok": False
        }), 500


# ============================================================
# TELEGRAM APPLICATION
# ============================================================

telegram_application = (
    Application.builder()
    .token(TELEGRAM_BOT_TOKEN)
    .build()
)


telegram_application.add_handler(
    CommandHandler(
        "start",
        start,
    )
)

telegram_application.add_handler(
    CommandHandler(
        "reset",
        reset,
    )
)

telegram_application.add_handler(
    CommandHandler(
        "admin",
        admin,
    )
)

telegram_application.add_handler(
    MessageHandler(
        filters.VOICE,
        handle_voice,
    )
)

telegram_application.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        chat,
    )
)


# ============================================================
# TELEGRAM LOOP
# ============================================================

telegram_loop = asyncio.new_event_loop()


def telegram_loop_worker():

    asyncio.set_event_loop(
        telegram_loop
    )

    telegram_loop.run_until_complete(
        telegram_application.initialize()
    )

    telegram_loop.run_until_complete(
        telegram_application.start()
    )

    telegram_loop.run_forever()


# ============================================================
# STARTUP
# ============================================================

def startup():

    # Database migration happens before the bot starts.
    init_database()

    thread = threading.Thread(
        target=telegram_loop_worker,
        daemon=True,
    )

    thread.start()

    time.sleep(2)

    webhook_url = (
        RENDER_EXTERNAL_URL.rstrip("/")
        + "/webhook"
    )

    future = asyncio.run_coroutine_threadsafe(
        telegram_application.bot.set_webhook(
            url=webhook_url
        ),
        telegram_loop,
    )

    try:

        future.result(
            timeout=30
        )

        logger.info(
            "Telegram webhook set: %s",
            webhook_url,
        )

    except Exception:

        logger.exception(
            "Could not set Telegram webhook."
        )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    startup()

    logger.info(
        "AskOra is running."
    )

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
    )
