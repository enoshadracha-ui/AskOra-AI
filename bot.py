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
BOT_LINK = "https://t.me/askora_official_bot"

TEXT_MODEL = "openai/gpt-oss-120b"
VOICE_MODEL = "whisper-large-v3-turbo"

groq_client = Groq(api_key=GROQ_API_KEY)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("askora")


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


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
# DATABASE
# ============================================================

def get_connection():
    return psycopg2.connect(DATABASE_URL)


def init_database():
    """
    Creates the current AskOra database structure and repairs
    common columns from older versions of the bot.
    """

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            # ------------------------------------------------
            # USERS
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY
                )
            """)

            # Current Telegram ID column
            cur.execute("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS telegram_id BIGINT
            """)

            # Profile columns
            cur.execute("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS username TEXT
            """)

            cur.execute("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS first_name TEXT
            """)

            cur.execute("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS last_name TEXT
            """)

            # Timestamp columns
            cur.execute("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS first_seen TIMESTAMPTZ
            """)

            cur.execute("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ
            """)

            cur.execute("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ
            """)

            # ------------------------------------------------
            # REPAIR OLD TELEGRAM ID COLUMNS
            # ------------------------------------------------

            possible_old_columns = [
                "user_id",
                "telegram_user_id",
                "telegramid",
                "telegram_user",
                "chat_id",
            ]

            cur.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'users'
            """)

            existing_columns = {
                row[0]
                for row in cur.fetchall()
            }

            for old_column in possible_old_columns:

                if old_column in existing_columns:

                    cur.execute(
                        f"""
                        UPDATE users
                        SET telegram_id = {old_column}
                        WHERE telegram_id IS NULL
                          AND {old_column} IS NOT NULL
                        """
                    )

            # ------------------------------------------------
            # REPAIR TIMESTAMPS
            # ------------------------------------------------

            cur.execute("""
                UPDATE users
                SET created_at = COALESCE(first_seen, NOW())
                WHERE created_at IS NULL
            """)

            cur.execute("""
                UPDATE users
                SET first_seen = COALESCE(created_at, NOW())
                WHERE first_seen IS NULL
            """)

            cur.execute("""
                UPDATE users
                SET last_seen = COALESCE(created_at, NOW())
                WHERE last_seen IS NULL
            """)

            # ------------------------------------------------
            # UNIQUE TELEGRAM ID INDEX
            # ------------------------------------------------

            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                users_telegram_id_unique
                ON users (telegram_id)
                WHERE telegram_id IS NOT NULL
            """)

            # ------------------------------------------------
            # USAGE EVENTS
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS usage_events (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    event_type TEXT,
                    created_at TIMESTAMPTZ
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

            # ------------------------------------------------
            # CONVERSATIONS
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    role TEXT,
                    content TEXT,
                    created_at TIMESTAMPTZ
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

        conn.commit()

        logger.info("Database initialized successfully.")

    except Exception:
        conn.rollback()
        logger.exception("Database initialization failed.")
        raise

    finally:
        conn.close()


# ============================================================
# USER DATABASE FUNCTIONS
# ============================================================

def record_user(user):
    """
    Creates or updates the Telegram user.
    """

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            telegram_id = user.id
            username = user.username
            first_name = user.first_name
            last_name = user.last_name

            now = datetime.now(timezone.utc)

            cur.execute("""
                SELECT id
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
                        created_at,
                        last_seen
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                    telegram_id,
                    username,
                    first_name,
                    last_name,
                    now,
                    now,
                    now,
                ))

        conn.commit()

    except Exception:
        conn.rollback()
        logger.exception("Could not record user.")

    finally:
        conn.close()


def record_usage(telegram_id, event_type):
    """
    Records text/voice usage.
    """

    conn = get_connection()

    try:
        with conn.cursor() as cur:

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
        logger.exception("Could not record usage event.")

    finally:
        conn.close()


def save_message(telegram_id, role, content):
    """
    Saves a conversation message.
    """

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
                VALUES (%s, %s, %s, NOW())
            """, (
                telegram_id,
                role,
                content,
            ))

        conn.commit()

    except Exception:
        conn.rollback()
        logger.exception("Could not save message.")

    finally:
        conn.close()


def get_history(telegram_id, limit=20):
    """
    Returns the latest conversation messages.
    """

    conn = get_connection()

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:

            cur.execute("""
                SELECT role, content
                FROM conversations
                WHERE telegram_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT %s
            """, (
                telegram_id,
                limit,
            ))

            rows = cur.fetchall()

            rows.reverse()

            return [
                {
                    "role": row["role"],
                    "content": row["content"],
                }
                for row in rows
            ]

    finally:
        conn.close()


def clear_history(telegram_id):
    """
    Deletes a user's conversation history.
    """

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
        logger.exception("Could not clear history.")

    finally:
        conn.close()


# ============================================================
# ADMIN STATISTICS
# ============================================================

def get_statistics():
    """
    Returns only the statistics needed by the admin dashboard.

    No recent users are returned.
    """

    conn = get_connection()

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:

            # -----------------------------------------------
            # TOTAL USERS
            # -----------------------------------------------

            cur.execute("""
                SELECT COUNT(*) AS total_users
                FROM users
                WHERE telegram_id IS NOT NULL
            """)

            total_users = cur.fetchone()["total_users"]

            # -----------------------------------------------
            # NEW USERS TODAY
            # -----------------------------------------------

            cur.execute("""
                SELECT COUNT(*) AS new_today
                FROM users
                WHERE created_at >= CURRENT_DATE
            """)

            new_today = cur.fetchone()["new_today"]

            # -----------------------------------------------
            # NEW USERS THIS WEEK
            # -----------------------------------------------

            cur.execute("""
                SELECT COUNT(*) AS new_week
                FROM users
                WHERE created_at >= CURRENT_DATE - INTERVAL '6 days'
            """)

            new_week = cur.fetchone()["new_week"]

            # -----------------------------------------------
            # ACTIVE USERS TODAY
            #
            # A user is active if they made a text or voice
            # request today.
            # -----------------------------------------------

            cur.execute("""
                SELECT COUNT(DISTINCT telegram_id) AS active_today
                FROM usage_events
                WHERE created_at >= CURRENT_DATE
                  AND event_type IN ('text', 'voice')
            """)

            active_today = cur.fetchone()["active_today"]

            # -----------------------------------------------
            # TOTAL USER MESSAGES
            # -----------------------------------------------

            cur.execute("""
                SELECT COUNT(*) AS total_messages
                FROM conversations
                WHERE role = 'user'
            """)

            total_messages = cur.fetchone()["total_messages"]

            # -----------------------------------------------
            # USER MESSAGES TODAY
            # -----------------------------------------------

            cur.execute("""
                SELECT COUNT(*) AS messages_today
                FROM conversations
                WHERE role = 'user'
                  AND created_at >= CURRENT_DATE
            """)

            messages_today = cur.fetchone()["messages_today"]

            # -----------------------------------------------
            # TOTAL TEXT REQUESTS
            # -----------------------------------------------

            cur.execute("""
                SELECT COUNT(*) AS text_requests
                FROM usage_events
                WHERE event_type = 'text'
            """)

            text_requests = cur.fetchone()["text_requests"]

            # -----------------------------------------------
            # TOTAL VOICE REQUESTS
            # -----------------------------------------------

            cur.execute("""
                SELECT COUNT(*) AS voice_requests
                FROM usage_events
                WHERE event_type = 'voice'
            """)

            voice_requests = cur.fetchone()["voice_requests"]

            # -----------------------------------------------
            # TOTAL EVENTS
            # -----------------------------------------------

            cur.execute("""
                SELECT COUNT(*) AS total_events
                FROM usage_events
            """)

            total_events = cur.fetchone()["total_events"]

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
        conn.close()


# ============================================================
# AI
# ============================================================

def generate_answer(messages):
    """
    Sends conversation to Groq.
    """

    response = groq_client.chat.completions.create(
        model=TEXT_MODEL,
        messages=messages,
        temperature=0.5,
        max_tokens=450,
    )

    return response.choices[0].message.content


def transcribe_audio(audio_bytes):
    """
    Transcribes Telegram voice audio using Groq Whisper.
    """

    audio_file = BytesIO(audio_bytes)
    audio_file.name = "voice.ogg"

    result = groq_client.audio.transcriptions.create(
        file=audio_file,
        model=VOICE_MODEL,
    )

    return result.text


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.effective_user:
        return

    record_user(update.effective_user)

    await update.message.reply_text(
        "🤖 Hey! I'm AskOra.\n\n"
        "Ask me anything, or send me a voice note.\n\n"
        "Ask. Get answers."
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.effective_user:
        return

    telegram_id = update.effective_user.id

    record_user(update.effective_user)

    clear_history(telegram_id)

    await update.message.reply_text(
        "🔄 Conversation cleared.\n\n"
        "Fresh start. Ask me anything!"
    )


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.effective_user:
        return

    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text(
            "❌ You are not authorized."
        )
        return

    try:
        stats = get_statistics()

        text = (
            "🛠️ ASKORA ADMIN DASHBOARD\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"

            "📊 OVERVIEW\n"
            f"👥 Total users: {stats['total_users']}\n"
            f"🆕 New users today: {stats['new_today']}\n"
            f"📅 New users this week: {stats['new_week']}\n"
            f"🟢 Active users today: {stats['active_today']}\n\n"

            "💬 AI USAGE\n"
            f"💬 Total messages: {stats['total_messages']}\n"
            f"📨 Messages today: {stats['messages_today']}\n"
            f"⌨️ Text requests: {stats['text_requests']}\n"
            f"🎤 Voice requests: {stats['voice_requests']}\n"
            f"📈 Total events: {stats['total_events']}"
        )

        await update.message.reply_text(text)

    except Exception:
        logger.exception("Admin dashboard error.")

        await update.message.reply_text(
            "⚠️ Could not load the admin dashboard."
        )


# ============================================================
# TELEGRAM TEXT CHAT
# ============================================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.effective_user or not update.message:
        return

    user = update.effective_user
    telegram_id = user.id
    message = update.message.text.strip()

    if not message:
        return

    record_user(user)
    record_usage(telegram_id, "text")
    save_message(telegram_id, "user", message)

    # Telegram typing indicator
    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action="typing",
        )
    except Exception:
        pass

    history = get_history(
        telegram_id,
        limit=20,
    )

    messages = [
        {
            "role": "system",
            "content": SYSTEM_INSTRUCTION,
        }
    ]

    messages.extend(history)

    try:

        answer = await asyncio.to_thread(
            generate_answer,
            messages,
        )

        save_message(
            telegram_id,
            "assistant",
            answer,
        )

        await update.message.reply_text(
            answer,
            parse_mode=None,
        )

    except Exception:
        logger.exception("Text AI error.")

        await update.message.reply_text(
            "⚠️ Sorry, something went wrong while generating your answer."
        )


# ============================================================
# TELEGRAM VOICE
# ============================================================

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.effective_user or not update.message:
        return

    user = update.effective_user
    telegram_id = user.id

    record_user(user)
    record_usage(telegram_id, "voice")

    try:

        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action="typing",
        )

        voice = update.message.voice

        telegram_file = await context.bot.get_file(
            voice.file_id
        )

        audio_bytes = await telegram_file.download_as_bytearray()

        transcript = await asyncio.to_thread(
            transcribe_audio,
            bytes(audio_bytes),
        )

        if not transcript.strip():
            await update.message.reply_text(
                "🎤 I couldn't hear anything clearly. Please try again."
            )
            return

        save_message(
            telegram_id,
            "user",
            transcript,
        )

        history = get_history(
            telegram_id,
            limit=20,
        )

        messages = [
            {
                "role": "system",
                "content": SYSTEM_INSTRUCTION,
            }
        ]

        messages.extend(history)

        answer = await asyncio.to_thread(
            generate_answer,
            messages,
        )

        save_message(
            telegram_id,
            "assistant",
            answer,
        )

        await update.message.reply_text(
            answer,
            parse_mode=None,
        )

    except Exception:
        logger.exception("Voice AI error.")

        await update.message.reply_text(
            "⚠️ Sorry, I couldn't process that voice note."
        )


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
# PERSISTENT ASYNCIO LOOP
# ============================================================

event_loop = None


def telegram_worker():
    global event_loop

    event_loop = asyncio.new_event_loop()

    asyncio.set_event_loop(event_loop)

    async def runner():

        await telegram_app.initialize()

        await telegram_app.start()

        await telegram_app.bot.set_webhook(
            url=f"{RENDER_EXTERNAL_URL.rstrip('/')}/webhook"
        )

        logger.info(
            "Telegram webhook configured."
        )

        logger.info(
            "AskOra Telegram application started."
        )

        await asyncio.Event().wait()

    try:
        event_loop.run_until_complete(
            runner()
        )

    except Exception:
        logger.exception(
            "Telegram worker crashed."
        )


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

@app.route(
    "/webhook",
    methods=["POST"],
)
def webhook():

    if not event_loop:
        return jsonify(
            {
                "ok": False,
                "error": "Telegram event loop not ready",
            }
        ), 503

    try:

        data = request.get_json(
            force=True
        )

        update = Update.de_json(
            data,
            telegram_app.bot,
        )

        asyncio.run_coroutine_threadsafe(
            telegram_app.process_update(update),
            event_loop,
        )

        return jsonify(
            {
                "ok": True
            }
        )

    except Exception:
        logger.exception(
            "Webhook processing error."
        )

        return jsonify(
            {
                "ok": False
            }
        ), 500


# ============================================================
# MINI APP AUTHENTICATION
# ============================================================

def validate_telegram_init_data(init_data):
    """
    Validates Telegram WebApp initData using the bot token.
    """

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

        # Prevent very old init data
        auth_date = parsed.get("auth_date")

        if auth_date:

            try:
                auth_timestamp = int(
                    auth_date
                )

                if (
                    time.time()
                    - auth_timestamp
                    > 86400
                ):
                    return None

            except ValueError:
                return None

        user_json = parsed.get("user")

        if not user_json:
            return None

        return json.loads(user_json)

    except Exception:
        logger.exception(
            "Telegram initData validation failed."
        )
        return None


def get_webapp_user():
    init_data = request.headers.get(
        "X-Telegram-Init-Data",
        "",
    )

    return validate_telegram_init_data(
        init_data
    )


# ============================================================
# MINI APP ROUTES
# ============================================================

@app.route("/")
def home():

    return render_template(
        "index.html"
    )


@app.route("/health")
def health():

    return jsonify(
        {
            "status": "ok",
            "service": "AskOra",
        }
    )


# ============================================================
# MINI APP HISTORY
# ============================================================

@app.route(
    "/api/history",
    methods=["GET"],
)
def api_history():

    user_data = get_webapp_user()

    if not user_data:
        return jsonify(
            {
                "error": "Unauthorized"
            }
        ), 401

    telegram_id = int(
        user_data["id"]
    )

    record_user_data = type(
        "TelegramUser",
        (),
        {
            "id": telegram_id,
            "username": user_data.get("username"),
            "first_name": user_data.get("first_name"),
            "last_name": user_data.get("last_name"),
        },
    )()

    record_user(record_user_data)

    history = get_history(
        telegram_id,
        limit=50,
    )

    return jsonify(
        {
            "history": history
        }
    )


# ============================================================
# MINI APP CHAT
# ============================================================

@app.route(
    "/api/chat",
    methods=["POST"],
)
def api_chat():

    user_data = get_webapp_user()

    if not user_data:
        return jsonify(
            {
                "error": "Unauthorized"
            }
        ), 401

    body = request.get_json(
        silent=True
    ) or {}

    message = str(
        body.get("message", "")
    ).strip()

    if not message:
        return jsonify(
            {
                "error": "Message is required"
            }
        ), 400

    telegram_id = int(
        user_data["id"]
    )

    record_user_data = type(
        "TelegramUser",
        (),
        {
            "id": telegram_id,
            "username": user_data.get("username"),
            "first_name": user_data.get("first_name"),
            "last_name": user_data.get("last_name"),
        },
    )()

    record_user(record_user_data)

    record_usage(
        telegram_id,
        "text",
    )

    save_message(
        telegram_id,
        "user",
        message,
    )

    history = get_history(
        telegram_id,
        limit=20,
    )

    messages = [
        {
            "role": "system",
            "content": SYSTEM_INSTRUCTION,
        }
    ]

    messages.extend(history)

    try:

        answer = generate_answer(
            messages
        )

        save_message(
            telegram_id,
            "assistant",
            answer,
        )

        return jsonify(
            {
                "answer": answer
            }
        )

    except Exception:
        logger.exception(
            "Mini App chat error."
        )

        return jsonify(
            {
                "error": "AI generation failed"
            }
        ), 500


# ============================================================
# MINI APP VOICE
# ============================================================

@app.route(
    "/api/voice",
    methods=["POST"],
)
def api_voice():

    user_data = get_webapp_user()

    if not user_data:
        return jsonify(
            {
                "error": "Unauthorized"
            }
        ), 401

    audio = request.files.get(
        "audio"
    )

    if not audio:
        return jsonify(
            {
                "error": "Audio is required"
            }
        ), 400

    telegram_id = int(
        user_data["id"]
    )

    record_user_data = type(
        "TelegramUser",
        (),
        {
            "id": telegram_id,
            "username": user_data.get("username"),
            "first_name": user_data.get("first_name"),
            "last_name": user_data.get("last_name"),
        },
    )()

    record_user(record_user_data)

    try:

        audio_bytes = audio.read()

        transcript = transcribe_audio(
            audio_bytes
        )

        if not transcript.strip():
            return jsonify(
                {
                    "error": "Could not understand audio"
                }
            ), 400

        record_usage(
            telegram_id,
            "voice",
        )

        save_message(
            telegram_id,
            "user",
            transcript,
        )

        history = get_history(
            telegram_id,
            limit=20,
        )

        messages = [
            {
                "role": "system",
                "content": SYSTEM_INSTRUCTION,
            }
        ]

        messages.extend(history)

        answer = generate_answer(
            messages
        )

        save_message(
            telegram_id,
            "assistant",
            answer,
        )

        return jsonify(
            {
                "transcript": transcript,
                "answer": answer,
            }
        )

    except Exception:
        logger.exception(
            "Mini App voice error."
        )

        return jsonify(
            {
                "error": "Voice processing failed"
            }
        ), 500


# ============================================================
# MINI APP RESET
# ============================================================

@app.route(
    "/api/reset",
    methods=["POST"],
)
def api_reset():

    user_data = get_webapp_user()

    if not user_data:
        return jsonify(
            {
                "error": "Unauthorized"
            }
        ), 401

    telegram_id = int(
        user_data["id"]
    )

    clear_history(
        telegram_id
    )

    return jsonify(
        {
            "success": True
        }
    )


# ============================================================
# STARTUP
# ============================================================

def start_services():

    logger.info(
        "Initializing AskOra database..."
    )

    init_database()

    logger.info(
        "Starting Telegram worker..."
    )

    worker = threading.Thread(
        target=telegram_worker,
        daemon=True,
    )

    worker.start()


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    start_services()

    logger.info(
        "AskOra server starting on port %s",
        PORT,
    )

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
    )
