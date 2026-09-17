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

from flask import Flask, request, jsonify, render_template

import psycopg2
from psycopg2.extras import RealDictCursor

from groq import Groq

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
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
RENDER_EXTERNAL_URL = os.environ["RENDER_EXTERNAL_URL"].rstrip("/")

PORT = int(os.environ.get("PORT", "10000"))

ADMIN_ID = 7721346673

BOT_USERNAME = "askora_official_bot"
BOT_LINK = f"https://t.me/{BOT_USERNAME}"

TEXT_MODEL = "openai/gpt-oss-120b"
VOICE_MODEL = "whisper-large-v3-turbo"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("askora")


# ============================================================
# CLIENTS
# ============================================================

groq_client = Groq(api_key=GROQ_API_KEY)

flask_app = Flask(__name__)


# ============================================================
# AI INSTRUCTIONS
# ============================================================

SYSTEM_INSTRUCTION = """
You are AskOra, a smart, friendly and concise AI assistant.

Your job is to answer the user's questions directly and naturally.

CONVERSATION RULES:
- Remember the recent conversation provided to you.
- Treat previous user and assistant messages as part of the same conversation.
- Use previous messages when answering follow-up questions.
- If the user introduces themselves, remember their name during this conversation.
- If the user asks "and you?", understand it based on the previous message.
- Do not act as if every message is a brand-new conversation.
- Do not forget previous messages unless the user uses reset or starts a genuinely new conversation.
- Be conversational and natural.

ANSWER STYLE:
- Simple questions: 1 to 3 sentences.
- Normal questions: usually 40 to 100 words.
- Keep answers concise.
- Use short paragraphs.
- Use bullets when useful.
- Avoid unnecessary introductions.
- Do not repeat the user's question.
- Do not write essays unless the user asks for detail.
- Keep explanations easy to understand.
- Use Markdown when helpful.

If the user specifically asks for a detailed explanation,
you may provide a longer answer.
"""


# ============================================================
# DATABASE
# ============================================================

def get_db():
    return psycopg2.connect(
        DATABASE_URL,
        sslmode="require",
    )


def init_database():
    """
    Creates the AskOra database structure.

    Also repairs common columns from older versions.
    """

    conn = get_db()

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

            cur.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'users'
            """)

            columns = {
                row[0]
                for row in cur.fetchall()
            }

            # Telegram ID
            if "telegram_id" not in columns:

                old_id_column = None

                possible_old_columns = [
                    "user_id",
                    "telegram_user_id",
                    "telegramid",
                    "telegram_user",
                    "chat_id",
                ]

                for old_column in possible_old_columns:
                    if old_column in columns:
                        old_id_column = old_column
                        break

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

            cur.execute("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS first_seen TIMESTAMPTZ
            """)

            cur.execute("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ
            """)

            # Repair timestamps
            cur.execute("""
                UPDATE users
                SET first_seen = NOW()
                WHERE first_seen IS NULL
            """)

            cur.execute("""
                UPDATE users
                SET last_seen = NOW()
                WHERE last_seen IS NULL
            """)

            # ------------------------------------------------
            # MESSAGES
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            cur.execute("""
                ALTER TABLE messages
                ADD COLUMN IF NOT EXISTS telegram_id BIGINT
            """)

            cur.execute("""
                ALTER TABLE messages
                ADD COLUMN IF NOT EXISTS role TEXT
            """)

            cur.execute("""
                ALTER TABLE messages
                ADD COLUMN IF NOT EXISTS content TEXT
            """)

            cur.execute("""
                ALTER TABLE messages
                ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ
            """)

            cur.execute("""
                UPDATE messages
                SET created_at = NOW()
                WHERE created_at IS NULL
            """)

            # ------------------------------------------------
            # USAGE EVENTS
            # ------------------------------------------------

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

            # ------------------------------------------------
            # INDEXES
            # ------------------------------------------------

            cur.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_messages_telegram_id
                ON messages(telegram_id)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_messages_created_at
                ON messages(created_at)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_usage_telegram_id
                ON usage_events(telegram_id)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_usage_created_at
                ON usage_events(created_at)
            """)

            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                idx_users_telegram_id
                ON users(telegram_id)
                WHERE telegram_id IS NOT NULL
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

def record_user(
    telegram_id,
    username=None,
    first_name=None,
    last_name=None,
):
    conn = get_db()

    try:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT id
                FROM users
                WHERE telegram_id = %s
            """, (telegram_id,))

            existing = cur.fetchone()

            now = datetime.now(timezone.utc)

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

    finally:
        conn.close()


def record_usage(telegram_id, event_type):
    conn = get_db()

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

    finally:
        conn.close()


def save_message(telegram_id, role, content):
    conn = get_db()

    try:
        with conn.cursor() as cur:

            cur.execute("""
                INSERT INTO messages (
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

    finally:
        conn.close()


def get_history(telegram_id, limit=30):
    conn = get_db()

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:

            cur.execute("""
                SELECT role, content
                FROM messages
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
    conn = get_db()

    try:
        with conn.cursor() as cur:

            cur.execute("""
                DELETE FROM messages
                WHERE telegram_id = %s
            """, (telegram_id,))

        conn.commit()

    finally:
        conn.close()


# ============================================================
# AI MESSAGE BUILDER
# ============================================================

def build_ai_messages(telegram_id, current_message):
    history = get_history(
        telegram_id,
        limit=30,
    )

    # If the current user message has already been saved,
    # remove that copy before adding it again.
    if (
        history
        and history[-1]["role"] == "user"
        and history[-1]["content"] == current_message
    ):
        history = history[:-1]

    messages = [
        {
            "role": "system",
            "content": SYSTEM_INSTRUCTION,
        }
    ]

    messages.extend(history)

    messages.append({
        "role": "user",
        "content": current_message,
    })

    return messages


# ============================================================
# AI GENERATION
# ============================================================

def generate_answer(telegram_id, current_message):

    messages = build_ai_messages(
        telegram_id,
        current_message,
    )

    logger.info(
        "Generating answer for user %s using %s. History messages: %s",
        telegram_id,
        TEXT_MODEL,
        len(messages) - 2,
    )

    response = groq_client.chat.completions.create(
        model=TEXT_MODEL,
        messages=messages,
        temperature=0.7,
        max_tokens=1200,
    )

    answer = response.choices[0].message.content

    if not answer:
        raise RuntimeError("Groq returned an empty response.")

    return answer.strip()


# ============================================================
# VOICE TRANSCRIPTION
# ============================================================

def transcribe_audio(audio_bytes):

    audio_file = BytesIO(audio_bytes)

    audio_file.name = "voice.ogg"

    transcription = groq_client.audio.transcriptions.create(
        file=audio_file,
        model=VOICE_MODEL,
    )

    text = transcription.text

    if not text:
        raise RuntimeError(
            "Voice transcription returned no text."
        )

    return text.strip()


# ============================================================
# TELEGRAM HANDLERS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

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
                "🚀 Open AskOra",
                web_app=WebAppInfo(
                    url=RENDER_EXTERNAL_URL
                ),
            )
        ],
        [
            InlineKeyboardButton(
                "✨ Invite a Friend",
                url=(
                    "https://t.me/share/url"
                    f"?url={BOT_LINK}"
                    "&text=Try%20AskOra%20%F0%9F%A4%96"
                    "%20%E2%80%94%20a%20free%20AI"
                    "%20assistant%20on%20Telegram!"
                ),
            )
        ],
    ]

    await update.message.reply_text(
        "🤖 Welcome to AskOra!\n\n"
        "Ask. Get answers.\n\n"
        "I'm your smart AI assistant. "
        "Ask questions, continue conversations, "
        "or send me a voice message.\n\n"
        "✨ Free to use\n"
        "⚡ Fast answers\n"
        "🎤 Voice support\n\n"
        "Tap below to open AskOra.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    clear_history(user_id)

    await update.message.reply_text(
        "🔄 Conversation cleared.\n\n"
        "Start a fresh conversation whenever you're ready."
    )


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text(
            "⛔ You are not authorized to use this command."
        )
        return

    conn = get_db()

    try:
        with conn.cursor() as cur:

            # -----------------------------------------------
            # TOTAL USERS
            # -----------------------------------------------

            cur.execute("""
                SELECT COUNT(*)
                FROM users
                WHERE telegram_id IS NOT NULL
            """)

            total_users = cur.fetchone()[0]

            # -----------------------------------------------
            # NEW USERS TODAY
            # -----------------------------------------------

            cur.execute("""
                SELECT COUNT(*)
                FROM users
                WHERE first_seen >= CURRENT_DATE
            """)

            new_today = cur.fetchone()[0]

            # -----------------------------------------------
            # NEW USERS THIS WEEK
            # -----------------------------------------------

            cur.execute("""
                SELECT COUNT(*)
                FROM users
                WHERE first_seen >= CURRENT_DATE - INTERVAL '6 days'
            """)

            new_week = cur.fetchone()[0]

            # -----------------------------------------------
            # ACTIVE USERS TODAY
            # -----------------------------------------------

            cur.execute("""
                SELECT COUNT(DISTINCT telegram_id)
                FROM usage_events
                WHERE created_at >= CURRENT_DATE
                AND event_type IN ('text', 'voice')
            """)

            active_today = cur.fetchone()[0]

            # -----------------------------------------------
            # TOTAL MESSAGES
            # -----------------------------------------------

            cur.execute("""
                SELECT COUNT(*)
                FROM messages
            """)

            total_messages = cur.fetchone()[0]

            # -----------------------------------------------
            # MESSAGES TODAY
            # -----------------------------------------------

            cur.execute("""
                SELECT COUNT(*)
                FROM messages
                WHERE created_at >= CURRENT_DATE
                AND role = 'user'
            """)

            messages_today = cur.fetchone()[0]

            # -----------------------------------------------
            # TEXT REQUESTS
            # -----------------------------------------------

            cur.execute("""
                SELECT COUNT(*)
                FROM usage_events
                WHERE event_type = 'text'
            """)

            text_requests = cur.fetchone()[0]

            # -----------------------------------------------
            # VOICE REQUESTS
            # -----------------------------------------------

            cur.execute("""
                SELECT COUNT(*)
                FROM usage_events
                WHERE event_type = 'voice'
            """)

            voice_requests = cur.fetchone()[0]

            # -----------------------------------------------
            # TOTAL EVENTS
            # -----------------------------------------------

            cur.execute("""
                SELECT COUNT(*)
                FROM usage_events
            """)

            total_events = cur.fetchone()[0]

    finally:
        conn.close()

    text = (
        "🛠️ ASKORA ADMIN DASHBOARD\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "📊 OVERVIEW\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        f"👥 Total users: {total_users}\n"
        f"🆕 New users today: {new_today}\n"
        f"📅 New users this week: {new_week}\n"
        f"🟢 Active users today: {active_today}\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "💬 AI USAGE\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        f"💬 Total messages: {total_messages}\n"
        f"📨 Messages today: {messages_today}\n"
        f"⌨️ Text requests: {text_requests}\n"
        f"🎤 Voice requests: {voice_requests}\n"
        f"📈 Total events: {total_events}"
    )

    await update.message.reply_text(text)


async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message or not update.message.text:
        return

    user = update.effective_user

    user_id = user.id
    text = update.message.text.strip()

    if not text:
        return

    record_user(
        telegram_id=user_id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )

    record_usage(
        user_id,
        "text",
    )

    # Save user's message first so history is persistent.
    save_message(
        user_id,
        "user",
        text,
    )

    try:

        await context.bot.send_chat_action(
            chat_id=user_id,
            action="typing",
        )

        answer = await asyncio.to_thread(
            generate_answer,
            user_id,
            text,
        )

        save_message(
            user_id,
            "assistant",
            answer,
        )

        await update.message.reply_text(
            answer
        )

    except Exception as error:

        logger.exception(
            "Text AI error: %s",
            error,
        )

        await update.message.reply_text(
            "⚠️ Sorry, something went wrong "
            "while generating the response."
        )


async def handle_voice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message or not update.message.voice:
        return

    user = update.effective_user

    user_id = user.id

    record_user(
        telegram_id=user_id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )

    record_usage(
        user_id,
        "voice",
    )

    try:

        await context.bot.send_chat_action(
            chat_id=user_id,
            action="typing",
        )

        telegram_file = await context.bot.get_file(
            update.message.voice.file_id
        )

        audio_bytes = await telegram_file.download_as_bytearray()

        transcript = await asyncio.to_thread(
            transcribe_audio,
            bytes(audio_bytes),
        )

        if not transcript:
            await update.message.reply_text(
                "🎤 I couldn't understand that voice message."
            )
            return

        # Save the transcription as the user's message.
        save_message(
            user_id,
            "user",
            transcript,
        )

        answer = await asyncio.to_thread(
            generate_answer,
            user_id,
            transcript,
        )

        save_message(
            user_id,
            "assistant",
            answer,
        )

        await update.message.reply_text(
            answer
        )

    except Exception as error:

        logger.exception(
            "Voice AI error: %s",
            error,
        )

        await update.message.reply_text(
            "⚠️ Sorry, I couldn't process that voice message."
        )


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
        handle_text,
    )
)


# ============================================================
# ASYNCIO LOOP
# ============================================================

telegram_loop = asyncio.new_event_loop()


def run_telegram_loop():

    asyncio.set_event_loop(
        telegram_loop
    )

    telegram_loop.run_forever()


telegram_thread = threading.Thread(
    target=run_telegram_loop,
    daemon=True,
)

telegram_thread.start()


async def initialize_telegram():

    try:

        await telegram_application.initialize()

        await telegram_application.start()

        webhook_url = (
            f"{RENDER_EXTERNAL_URL}/webhook"
        )

        await telegram_application.bot.set_webhook(
            url=webhook_url,
            allowed_updates=Update.ALL_TYPES,
        )

        logger.info(
            "Telegram webhook set to %s",
            webhook_url,
        )

    except Exception:

        logger.exception(
            "Telegram initialization failed."
        )


asyncio.run_coroutine_threadsafe(
    initialize_telegram(),
    telegram_loop,
)


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

@flask_app.post("/webhook")
def telegram_webhook():

    try:

        update_data = request.get_json(
            force=True
        )

        update = Update.de_json(
            update_data,
            telegram_application.bot,
        )

        asyncio.run_coroutine_threadsafe(
            telegram_application.process_update(
                update
            ),
            telegram_loop,
        )

        return jsonify({
            "ok": True
        })

    except Exception as error:

        logger.exception(
            "Webhook error: %s",
            error,
        )

        return jsonify({
            "ok": False,
            "error": str(error),
        }), 500


# ============================================================
# MINI APP AUTHENTICATION
# ============================================================

def validate_telegram_init_data(init_data):

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

        auth_date = parsed.get(
            "auth_date"
        )

        if not auth_date:
            return None

        # Don't accept extremely old init data.
        if (
            time.time()
            - int(auth_date)
            > 86400
        ):
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
            "Mini App authentication failed."
        )

        return None


def get_webapp_user():

    init_data = request.headers.get(
        "X-Telegram-Init-Data"
    )

    return validate_telegram_init_data(
        init_data
    )


# ============================================================
# MINI APP ROUTES
# ============================================================

@flask_app.get("/")
def home():

    return render_template(
        "index.html"
    )


@flask_app.get("/api/history")
def api_history():

    user = get_webapp_user()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Unauthorized",
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
        limit=50,
    )

    return jsonify({
        "ok": True,
        "history": history,
    })


@flask_app.post("/api/chat")
def api_chat():

    user = get_webapp_user()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Unauthorized",
        }), 401

    data = request.get_json(
        silent=True
    ) or {}

    message = str(
        data.get("message", "")
    ).strip()

    if not message:
        return jsonify({
            "ok": False,
            "error": "Please enter a message.",
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

    record_usage(
        telegram_id,
        "text",
    )

    save_message(
        telegram_id,
        "user",
        message,
    )

    try:

        answer = generate_answer(
            telegram_id,
            message,
        )

        save_message(
            telegram_id,
            "assistant",
            answer,
        )

        return jsonify({
            "ok": True,
            "answer": answer,
        })

    except Exception as error:

        logger.exception(
            "Mini App chat error: %s",
            error,
        )

        return jsonify({
            "ok": False,
            "error": (
                "Sorry, something went wrong "
                "while generating the response."
            ),
        }), 500


@flask_app.post("/api/voice")
def api_voice():

    user = get_webapp_user()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Unauthorized",
        }), 401

    if "audio" not in request.files:
        return jsonify({
            "ok": False,
            "error": "No audio received.",
        }), 400

    audio_file = request.files["audio"]

    audio_bytes = audio_file.read()

    if not audio_bytes:
        return jsonify({
            "ok": False,
            "error": "Empty audio file.",
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

    record_usage(
        telegram_id,
        "voice",
    )

    try:

        transcript = transcribe_audio(
            audio_bytes
        )

        if not transcript:
            return jsonify({
                "ok": False,
                "error": "I couldn't understand the recording.",
            }), 400

        save_message(
            telegram_id,
            "user",
            transcript,
        )

        answer = generate_answer(
            telegram_id,
            transcript,
        )

        save_message(
            telegram_id,
            "assistant",
            answer,
        )

        return jsonify({
            "ok": True,
            "transcript": transcript,
            "answer": answer,
        })

    except Exception:

        logger.exception(
            "Mini App voice error."
        )

        return jsonify({
            "ok": False,
            "error": (
                "Sorry, I couldn't process "
                "that voice message."
            ),
        }), 500


@flask_app.post("/api/reset")
def api_reset():

    user = get_webapp_user()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Unauthorized",
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
# HEALTH CHECK
# ============================================================

@flask_app.get("/health")
def health():

    return jsonify({
        "ok": True,
        "service": "AskOra",
    })


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    init_database()

    logger.info(
        "AskOra is starting..."
    )

    logger.info(
        "Mini App URL: %s",
        RENDER_EXTERNAL_URL,
    )

    flask_app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True,
    )
