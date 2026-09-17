import os
import asyncio
import threading
import logging
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone, timedelta
from io import BytesIO

from flask import Flask, request, jsonify

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
BOT_LINK = "https://t.me/askora_official_bot"

TEXT_MODEL = "openai/gpt-oss-120b"
VOICE_MODEL = "whisper-large-v3-turbo"

groq_client = Groq(api_key=GROQ_API_KEY)

app = Flask(__name__)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# AI SYSTEM INSTRUCTION
# ============================================================

SYSTEM_INSTRUCTION = """
You are AskOra, a smart, friendly and concise AI assistant.

Your job is to answer the user's questions directly and naturally.

IMPORTANT CONVERSATION RULES:

1. Remember and use the recent conversation provided to you.
2. Treat previous user and assistant messages as part of the same conversation.
3. If the user refers to something they said earlier, use the previous messages to understand what they mean.
4. If the user introduces themselves, remember their name during the conversation.
5. If the user asks "and you?", "what about you?", or a similar follow-up, understand that it refers to the previous message.
6. Do not pretend that you forgot previous messages when they are included in the conversation history.
7. Do not restart the conversation unless the user actually starts a new conversation or uses /reset.
8. Respond naturally, like a helpful conversational assistant.

ANSWER STYLE:

- Simple questions: 1 to 3 sentences.
- Normal questions: usually 40 to 100 words.
- Keep answers concise.
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
# DATABASE
# ============================================================

def get_connection():
    return psycopg2.connect(DATABASE_URL)


def init_database():
    conn = get_connection()
    cur = conn.cursor()

    try:
        # ----------------------------------------------------
        # USERS TABLE
        # ----------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY
            )
        """)

        # Existing-column compatibility
        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'users'
        """)

        existing_columns = {
            row[0]
            for row in cur.fetchall()
        }

        # Find an old Telegram ID column if one exists
        possible_old_id_columns = [
            "user_id",
            "telegram_user_id",
            "telegramid",
            "telegram_user",
            "chat_id",
        ]

        old_id_column = None

        for column in possible_old_id_columns:
            if column in existing_columns:
                old_id_column = column
                break

        if "telegram_id" not in existing_columns:
            cur.execute("""
                ALTER TABLE users
                ADD COLUMN telegram_id BIGINT
            """)

            if old_id_column:
                cur.execute(
                    f"""
                    UPDATE users
                    SET telegram_id = "{old_id_column}"
                    WHERE telegram_id IS NULL
                    """
                )

        # Add missing profile columns
        user_columns = {
            "username": "TEXT",
            "first_name": "TEXT",
            "last_name": "TEXT",
            "first_seen": "TIMESTAMPTZ",
            "last_seen": "TIMESTAMPTZ",
        }

        for column, data_type in user_columns.items():
            if column not in existing_columns:
                cur.execute(
                    f"""
                    ALTER TABLE users
                    ADD COLUMN {column} {data_type}
                    """
                )

        # Repair NULL timestamps
        cur.execute("""
            UPDATE users
            SET first_seen = COALESCE(
                first_seen,
                NOW()
            )
            WHERE first_seen IS NULL
        """)

        cur.execute("""
            UPDATE users
            SET last_seen = COALESCE(
                last_seen,
                NOW()
            )
            WHERE last_seen IS NULL
        """)

        # ----------------------------------------------------
        # CONVERSATIONS TABLE
        # ----------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT,
                role TEXT,
                content TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        cur.execute("""
            ALTER TABLE conversations
            ADD COLUMN IF NOT EXISTS telegram_id BIGINT
        """)

        cur.execute("""
            ALTER TABLE conversations
            ADD COLUMN IF NOT EXISTS role TEXT
        """)

        cur.execute("""
            ALTER TABLE conversations
            ADD COLUMN IF NOT EXISTS content TEXT
        """)

        cur.execute("""
            ALTER TABLE conversations
            ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ
        """)

        cur.execute("""
            UPDATE conversations
            SET created_at = NOW()
            WHERE created_at IS NULL
        """)

        # ----------------------------------------------------
        # USAGE EVENTS
        # ----------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS usage_events (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT,
                event_type TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        cur.execute("""
            ALTER TABLE usage_events
            ADD COLUMN IF NOT EXISTS telegram_id BIGINT
        """)

        cur.execute("""
            ALTER TABLE usage_events
            ADD COLUMN IF NOT EXISTS event_type TEXT
        """)

        cur.execute("""
            ALTER TABLE usage_events
            ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ
        """)

        cur.execute("""
            UPDATE usage_events
            SET created_at = NOW()
            WHERE created_at IS NULL
        """)

        # ----------------------------------------------------
        # INDEXES
        # ----------------------------------------------------

        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
            users_telegram_id_unique
            ON users (telegram_id)
            WHERE telegram_id IS NOT NULL
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS
            conversations_telegram_id_idx
            ON conversations (telegram_id)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS
            conversations_created_at_idx
            ON conversations (created_at)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS
            usage_events_telegram_id_idx
            ON usage_events (telegram_id)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS
            usage_events_created_at_idx
            ON usage_events (created_at)
        """)

        conn.commit()

        logger.info("Database initialized successfully.")

    except Exception:
        conn.rollback()
        logger.exception("Database initialization failed.")
        raise

    finally:
        cur.close()
        conn.close()


# ============================================================
# USER MANAGEMENT
# ============================================================

def record_user(
    telegram_id,
    username=None,
    first_name=None,
    last_name=None,
):
    conn = get_connection()
    cur = conn.cursor()

    try:
        now = datetime.now(timezone.utc)

        cur.execute("""
            SELECT id
            FROM users
            WHERE telegram_id = %s
        """, (telegram_id,))

        existing = cur.fetchone()

        if existing:
            cur.execute("""
                UPDATE users
                SET
                    username = %s,
                    first_name = %s,
                    last_name = %s,
                    last_seen = %s
                WHERE telegram_id = %s
            """, (
                username,
                first_name,
                last_name,
                now,
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
                    last_seen
                )
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                telegram_id,
                username,
                first_name,
                last_name,
                now,
                now,
            ))

        conn.commit()

    except Exception:
        conn.rollback()
        logger.exception("Could not record user.")

    finally:
        cur.close()
        conn.close()


# ============================================================
# USAGE TRACKING
# ============================================================

def record_usage(telegram_id, event_type):
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            INSERT INTO usage_events (
                telegram_id,
                event_type,
                created_at
            )
            VALUES (%s, %s, NOW())
        """, (
            telegram_id,
            event_type,
        ))

        conn.commit()

    except Exception:
        conn.rollback()
        logger.exception("Could not record usage.")

    finally:
        cur.close()
        conn.close()


# ============================================================
# CONVERSATION STORAGE
# ============================================================

def save_message(
    telegram_id,
    role,
    content,
):
    if not content:
        return

    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            INSERT INTO conversations (
                telegram_id,
                role,
                content,
                created_at
            )
            VALUES (%s, %s, %s, NOW())
        """, (
            telegram_id,
            role,
            content,
        ))

        conn.commit()

    except Exception:
        conn.rollback()
        logger.exception("Could not save conversation message.")

    finally:
        cur.close()
        conn.close()


def get_history(
    telegram_id,
    limit=30,
):
    """
    Get the latest messages in the correct chronological order.

    We first select the newest messages by ID and then reorder
    them from oldest -> newest.

    This is more reliable than depending only on timestamps.
    """

    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        cur.execute("""
            SELECT id, role, content
            FROM (
                SELECT
                    id,
                    role,
                    content
                FROM conversations
                WHERE telegram_id = %s
                ORDER BY id DESC
                LIMIT %s
            ) recent
            ORDER BY id ASC
        """, (
            telegram_id,
            limit,
        ))

        rows = cur.fetchall()

        history = []

        for row in rows:
            role = row["role"]
            content = row["content"]

            if role not in ("user", "assistant"):
                continue

            if not content:
                continue

            history.append({
                "role": role,
                "content": str(content),
            })

        return history

    except Exception:
        logger.exception("Could not get conversation history.")
        return []

    finally:
        cur.close()
        conn.close()


def clear_history(telegram_id):
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            DELETE FROM conversations
            WHERE telegram_id = %s
        """, (telegram_id,))

        conn.commit()

    except Exception:
        conn.rollback()
        logger.exception("Could not clear conversation.")

    finally:
        cur.close()
        conn.close()


# ============================================================
# AI MESSAGE BUILDER
# ============================================================

def build_ai_messages(telegram_id):
    """
    Builds the exact conversation that will be sent to Groq.

    This is the important conversation-memory fix.
    """

    history = get_history(
        telegram_id,
        limit=30,
    )

    messages = [
        {
            "role": "system",
            "content": SYSTEM_INSTRUCTION,
        }
    ]

    for item in history:
        role = item.get("role")
        content = item.get("content")

        if role in ("user", "assistant") and content:
            messages.append({
                "role": role,
                "content": content,
            })

    return messages


# ============================================================
# AI TEXT GENERATION
# ============================================================

def generate_answer(
    telegram_id,
):
    messages = build_ai_messages(
        telegram_id
    )

    if len(messages) <= 1:
        return "Sorry, I didn't receive your question."

    logger.info(
        "Sending %s messages to Groq for user %s",
        len(messages),
        telegram_id,
    )

    response = groq_client.chat.completions.create(
        model=TEXT_MODEL,
        messages=messages,
        temperature=0.7,
        max_tokens=1000,
    )

    answer = response.choices[0].message.content

    if not answer:
        return "Sorry, I couldn't generate an answer."

    return answer.strip()


# ============================================================
# VOICE TRANSCRIPTION
# ============================================================

def transcribe_audio(audio_bytes):
    audio_file = BytesIO(audio_bytes)
    audio_file.name = "voice.ogg"

    result = groq_client.audio.transcriptions.create(
        model=VOICE_MODEL,
        file=audio_file,
    )

    return result.text.strip()


# ============================================================
# TELEGRAM /START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    record_user(
        telegram_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )

    keyboard = [
        [
            InlineKeyboardButton(
                "✨ Invite a Friend",
                url=(
                    "https://t.me/share/url"
                    "?url=https%3A%2F%2Ft.me%2Faskora_official_bot"
                    "&text=Try%20AskOra%20%F0%9F%A4%96%20%E2%80%94%20a%20free%20AI%20assistant%20on%20Telegram!"
                ),
            )
        ]
    ]

    await update.message.reply_text(
        "🤖 *Welcome to AskOra!*\n\n"
        "Your simple AI assistant.\n\n"
        "Ask me anything and I'll help you get a clear answer.\n\n"
        "✨ Text & voice supported\n"
        "🆓 Free to use\n\n"
        "Just send me a message to begin.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ============================================================
# TELEGRAM /RESET
# ============================================================

async def reset(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    clear_history(user.id)

    await update.message.reply_text(
        "🔄 Conversation cleared.\n\n"
        "We can start fresh!"
    )


# ============================================================
# TELEGRAM /ADMIN
# ============================================================

def get_statistics():
    conn = get_connection()
    cur = conn.cursor()

    try:
        # Total users
        cur.execute("""
            SELECT COUNT(*)
            FROM users
        """)
        total_users = cur.fetchone()[0]

        # New today
        cur.execute("""
            SELECT COUNT(*)
            FROM users
            WHERE first_seen >= CURRENT_DATE
        """)
        new_today = cur.fetchone()[0]

        # New this week
        cur.execute("""
            SELECT COUNT(*)
            FROM users
            WHERE first_seen >= CURRENT_DATE - INTERVAL '6 days'
        """)
        new_week = cur.fetchone()[0]

        # Active users today
        cur.execute("""
            SELECT COUNT(DISTINCT telegram_id)
            FROM usage_events
            WHERE created_at >= CURRENT_DATE
            AND event_type IN ('text', 'voice')
        """)
        active_today = cur.fetchone()[0]

        # Total messages
        cur.execute("""
            SELECT COUNT(*)
            FROM conversations
            WHERE role = 'user'
        """)
        total_messages = cur.fetchone()[0]

        # Messages today
        cur.execute("""
            SELECT COUNT(*)
            FROM conversations
            WHERE role = 'user'
            AND created_at >= CURRENT_DATE
        """)
        messages_today = cur.fetchone()[0]

        # Text requests
        cur.execute("""
            SELECT COUNT(*)
            FROM usage_events
            WHERE event_type = 'text'
        """)
        text_requests = cur.fetchone()[0]

        # Voice requests
        cur.execute("""
            SELECT COUNT(*)
            FROM usage_events
            WHERE event_type = 'voice'
        """)
        voice_requests = cur.fetchone()[0]

        # Total events
        cur.execute("""
            SELECT COUNT(*)
            FROM usage_events
        """)
        total_events = cur.fetchone()[0]

        return {
            "total_users": total_users,
            "new_today": new_today,
            "new_week": new_week,
            "active_today": active_today,
            "total_messages": total_messages,
            "messages_today": messages_today,
            "text_requests": text_requests,
            "voice_requests": voice_requests,
            "total_events": total_events,
        }

    finally:
        cur.close()
        conn.close()


async def admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    if user.id != ADMIN_ID:
        await update.message.reply_text(
            "❌ You are not authorized to use this command."
        )
        return

    try:
        stats = get_statistics()

        text = (
            "🛠️ *ASKORA ADMIN DASHBOARD*\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"

            "📊 *USERS*\n"
            f"👥 Total users: {stats['total_users']}\n"
            f"🆕 New users today: {stats['new_today']}\n"
            f"📅 New users this week: {stats['new_week']}\n"
            f"🟢 Active users today: {stats['active_today']}\n\n"

            "💬 *AI USAGE*\n"
            f"💬 Total messages: {stats['total_messages']}\n"
            f"📨 Messages today: {stats['messages_today']}\n"
            f"⌨️ Text requests: {stats['text_requests']}\n"
            f"🎤 Voice requests: {stats['voice_requests']}\n"
            f"📈 Total events: {stats['total_events']}\n"
        )

        await update.message.reply_text(
            text,
            parse_mode="Markdown",
        )

    except Exception:
        logger.exception("Admin dashboard error.")

        await update.message.reply_text(
            "⚠️ Could not load admin statistics."
        )


# ============================================================
# TELEGRAM TEXT CHAT
# ============================================================

async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    if not update.message or not update.message.text:
        return

    message = update.message.text.strip()

    if not message:
        return

    record_user(
        telegram_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )

    # Save the user's message FIRST.
    save_message(
        telegram_id=user.id,
        role="user",
        content=message,
    )

    record_usage(
        telegram_id=user.id,
        event_type="text",
    )

    # Show Telegram typing indicator
    try:
        await context.bot.send_chat_action(
            chat_id=user.id,
            action="typing",
        )
    except Exception:
        pass

    try:
        # Generate using the complete recent conversation
        answer = await asyncio.to_thread(
            generate_answer,
            user.id,
        )

        # Save assistant response
        save_message(
            telegram_id=user.id,
            role="assistant",
            content=answer,
        )

        await update.message.reply_text(
            answer,
            parse_mode="Markdown",
        )

    except Exception:
        logger.exception("Text AI error.")

        await update.message.reply_text(
            "⚠️ Sorry, something went wrong while generating the response."
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
        telegram_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )

    try:
        await context.bot.send_chat_action(
            chat_id=user.id,
            action="typing",
        )
    except Exception:
        pass

    try:
        voice = update.message.voice

        telegram_file = await context.bot.get_file(
            voice.file_id
        )

        audio_bytes = await telegram_file.download_as_bytearray()

        text = await asyncio.to_thread(
            transcribe_audio,
            bytes(audio_bytes),
        )

        if not text:
            await update.message.reply_text(
                "🎤 I couldn't understand that voice message."
            )
            return

        # Save transcribed voice message as user message
        save_message(
            telegram_id=user.id,
            role="user",
            content=text,
        )

        record_usage(
            telegram_id=user.id,
            event_type="voice",
        )

        # Generate using full conversation
        answer = await asyncio.to_thread(
            generate_answer,
            user.id,
        )

        save_message(
            telegram_id=user.id,
            role="assistant",
            content=answer,
        )

        await update.message.reply_text(
            answer,
            parse_mode="Markdown",
        )

    except Exception:
        logger.exception("Voice AI error.")

        await update.message.reply_text(
            "⚠️ Sorry, something went wrong while processing your voice message."
        )


# ============================================================
# ASYNCIO EVENT LOOP
# ============================================================

loop = asyncio.new_event_loop()


def start_loop():
    asyncio.set_event_loop(loop)

    logger.info(
        "Persistent asyncio event loop started."
    )

    loop.run_forever()


loop_thread = threading.Thread(
    target=start_loop,
    daemon=True,
)

loop_thread.start()


# ============================================================
# TELEGRAM APPLICATION
# ============================================================

telegram_app = (
    Application.builder()
    .token(TELEGRAM_BOT_TOKEN)
    .build()
)


telegram_app.add_handler(
    CommandHandler(
        "start",
        start,
    )
)

telegram_app.add_handler(
    CommandHandler(
        "reset",
        reset,
    )
)

telegram_app.add_handler(
    CommandHandler(
        "admin",
        admin,
    )
)

telegram_app.add_handler(
    MessageHandler(
        filters.VOICE,
        handle_voice,
    )
)

telegram_app.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_text,
    )
)


# ============================================================
# TELEGRAM STARTUP
# ============================================================

async def initialize_telegram():
    await telegram_app.initialize()

    await telegram_app.start()

    webhook_url = (
        RENDER_EXTERNAL_URL.rstrip("/")
        + "/webhook"
    )

    await telegram_app.bot.set_webhook(
        url=webhook_url,
        allowed_updates=Update.ALL_TYPES,
    )

    logger.info(
        "Webhook set to %s",
        webhook_url,
    )


asyncio.run_coroutine_threadsafe(
    initialize_telegram(),
    loop,
)


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

@app.route(
    "/webhook",
    methods=["POST"],
)
def webhook():
    try:
        data = request.get_json(
            force=True
        )

        update = Update.de_json(
            data,
            telegram_app.bot,
        )

        future = asyncio.run_coroutine_threadsafe(
            telegram_app.process_update(update),
            loop,
        )

        future.result(
            timeout=30
        )

        return jsonify({
            "ok": True
        })

    except Exception:
        logger.exception(
            "Webhook processing error."
        )

        return jsonify({
            "ok": False
        }), 500


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/")
def home():
    return "AskOra bot is running."


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot": "AskOra",
    })


# ============================================================
# TELEGRAM MINI APP AUTH
# ============================================================

def validate_telegram_init_data(
    init_data,
):
    if not init_data:
        return None

    try:
        from urllib.parse import parse_qsl

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

        auth_date = int(
            parsed.get(
                "auth_date",
                "0",
            )
        )

        # Reject very old init data
        if time.time() - auth_date > 86400:
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
            "Mini App authentication error."
        )
        return None


def get_webapp_user():
    init_data = request.headers.get(
        "X-Telegram-Init-Data"
    )

    user = validate_telegram_init_data(
        init_data
    )

    return user


# ============================================================
# MINI APP HISTORY
# ============================================================

@app.route(
    "/api/history",
    methods=["GET"],
)
def api_history():
    user = get_webapp_user()

    if not user:
        return jsonify({
            "error": "Unauthorized"
        }), 401

    telegram_id = int(
        user["id"]
    )

    record_user(
        telegram_id=telegram_id,
        username=user.get("username"),
        first_name=user.get("first_name"),
        last_name=user.get("last_name"),
    )

    history = get_history(
        telegram_id,
        limit=30,
    )

    return jsonify({
        "messages": history
    })


# ============================================================
# MINI APP CHAT
# ============================================================

@app.route(
    "/api/chat",
    methods=["POST"],
)
def api_chat():
    user = get_webapp_user()

    if not user:
        return jsonify({
            "error": "Unauthorized"
        }), 401

    data = request.get_json(
        silent=True
    ) or {}

    message = str(
        data.get(
            "message",
            ""
        )
    ).strip()

    if not message:
        return jsonify({
            "error": "Message is required."
        }), 400

    telegram_id = int(
        user["id"]
    )

    record_user(
        telegram_id=telegram_id,
        username=user.get("username"),
        first_name=user.get("first_name"),
        last_name=user.get("last_name"),
    )

    save_message(
        telegram_id=telegram_id,
        role="user",
        content=message,
    )

    record_usage(
        telegram_id=telegram_id,
        event_type="text",
    )

    try:
        answer = generate_answer(
            telegram_id
        )

        save_message(
            telegram_id=telegram_id,
            role="assistant",
            content=answer,
        )

        return jsonify({
            "answer": answer
        })

    except Exception:
        logger.exception(
            "Mini App text AI error."
        )

        return jsonify({
            "error": "Something went wrong while generating the response."
        }), 500


# ============================================================
# MINI APP VOICE
# ============================================================

@app.route(
    "/api/voice",
    methods=["POST"],
)
def api_voice():
    user = get_webapp_user()

    if not user:
        return jsonify({
            "error": "Unauthorized"
        }), 401

    if "audio" not in request.files:
        return jsonify({
            "error": "Audio file is required."
        }), 400

    telegram_id = int(
        user["id"]
    )

    audio = request.files["audio"]

    audio_bytes = audio.read()

    if not audio_bytes:
        return jsonify({
            "error": "Empty audio file."
        }), 400

    record_user(
        telegram_id=telegram_id,
        username=user.get("username"),
        first_name=user.get("first_name"),
        last_name=user.get("last_name"),
    )

    try:
        text = transcribe_audio(
            audio_bytes
        )

        if not text:
            return jsonify({
                "error": "Could not understand the audio."
            }), 400

        save_message(
            telegram_id=telegram_id,
            role="user",
            content=text,
        )

        record_usage(
            telegram_id=telegram_id,
            event_type="voice",
        )

        answer = generate_answer(
            telegram_id
        )

        save_message(
            telegram_id=telegram_id,
            role="assistant",
            content=answer,
        )

        return jsonify({
            "transcription": text,
            "answer": answer,
        })

    except Exception:
        logger.exception(
            "Mini App voice AI error."
        )

        return jsonify({
            "error": "Something went wrong while processing the voice message."
        }), 500


# ============================================================
# MINI APP RESET
# ============================================================

@app.route(
    "/api/reset",
    methods=["POST"],
)
def api_reset():
    user = get_webapp_user()

    if not user:
        return jsonify({
            "error": "Unauthorized"
        }), 401

    telegram_id = int(
        user["id"]
    )

    clear_history(
        telegram_id
    )

    return jsonify({
        "ok": True
    })


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":
    init_database()

    logger.info(
        "Starting AskOra on port %s...",
        PORT,
    )

    app.run(
        host="0.0.0.0",
        port=PORT,
    )
