import os
import asyncio
import threading
import logging
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from io import BytesIO

import psycopg2
from flask import Flask, request, jsonify
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


# =========================================================
# SETTINGS
# =========================================================

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
RENDER_EXTERNAL_URL = os.environ["RENDER_EXTERNAL_URL"]
DATABASE_URL = os.environ["DATABASE_URL"]

PORT = int(os.environ.get("PORT", 10000))

TEXT_MODEL = "openai/gpt-oss-120b"
VOICE_MODEL = "whisper-large-v3-turbo"

ADMIN_ID = 7721346673

BOT_USERNAME = "askora_official_bot"
BOT_LINK = f"https://t.me/{BOT_USERNAME}"

groq_client = Groq(api_key=GROQ_API_KEY)


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# =========================================================
# FLASK
# =========================================================

app = Flask(__name__)


# =========================================================
# SYSTEM INSTRUCTION
# =========================================================

SYSTEM_INSTRUCTION = """
You are AskOra, a helpful AI assistant.

Give short, clear and direct answers.

Use simple language.

For school questions, give a clear answer without unnecessary details.

Usually answer in 2 to 5 sentences.

Do not use tables unless the user specifically asks.

Do not add unnecessary headings or sections.

Only give a longer explanation when the user asks for one.

Be accurate, natural and helpful.
"""


# =========================================================
# DATABASE
# =========================================================

def get_connection():
    return psycopg2.connect(DATABASE_URL)


def init_database():
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            first_seen TIMESTAMPTZ NOT NULL,
            last_seen TIMESTAMPTZ NOT NULL
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_events (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            event_type TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL
        )
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_usage_events_user
        ON usage_events(user_id)
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_usage_events_created
        ON usage_events(created_at)
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_conversations_user
        ON conversations(user_id, created_at)
        """
    )

    connection.commit()

    cursor.close()
    connection.close()

    logger.info("Database initialized.")


# =========================================================
# USER TRACKING
# =========================================================

def record_user(user_id):
    now = datetime.now(timezone.utc)

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute(
        """
        INSERT INTO users (
            user_id,
            first_seen,
            last_seen
        )
        VALUES (%s, %s, %s)

        ON CONFLICT (user_id)
        DO UPDATE SET
            last_seen = EXCLUDED.last_seen
        """,
        (
            user_id,
            now,
            now,
        ),
    )

    connection.commit()

    cursor.close()
    connection.close()


# =========================================================
# USAGE TRACKING
# =========================================================

def record_usage(user_id, event_type):
    now = datetime.now(timezone.utc)

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute(
        """
        INSERT INTO usage_events (
            user_id,
            event_type,
            created_at
        )
        VALUES (%s, %s, %s)
        """,
        (
            user_id,
            event_type,
            now,
        ),
    )

    connection.commit()

    cursor.close()
    connection.close()


# =========================================================
# CONVERSATION STORAGE
# =========================================================

def save_message(user_id, role, content):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute(
        """
        INSERT INTO conversations (
            user_id,
            role,
            content,
            created_at
        )
        VALUES (%s, %s, %s, %s)
        """,
        (
            user_id,
            role,
            content,
            datetime.now(timezone.utc),
        ),
    )

    connection.commit()

    cursor.close()
    connection.close()


def get_history(user_id, limit=10):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT role, content
        FROM conversations
        WHERE user_id = %s
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (
            user_id,
            limit,
        ),
    )

    rows = cursor.fetchall()

    cursor.close()
    connection.close()

    rows.reverse()

    return [
        {
            "role": row[0],
            "content": row[1],
        }
        for row in rows
    ]


# =========================================================
# STATISTICS
# =========================================================

def get_statistics():
    connection = get_connection()
    cursor = connection.cursor()

    now = datetime.now(timezone.utc)

    today_start = now.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    week_start = today_start - timedelta(days=6)

    month_start = today_start - timedelta(days=29)

    # -------------------------
    # USERS
    # -------------------------

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM users
        """
    )

    total_users = cursor.fetchone()[0]

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM users
        WHERE first_seen >= %s
        """,
        (today_start,),
    )

    new_users_today = cursor.fetchone()[0]

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM users
        WHERE first_seen >= %s
        """,
        (week_start,),
    )

    new_users_week = cursor.fetchone()[0]

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM users
        WHERE first_seen >= %s
        """,
        (month_start,),
    )

    new_users_month = cursor.fetchone()[0]

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM users
        WHERE last_seen >= %s
        """,
        (today_start,),
    )

    active_today = cursor.fetchone()[0]

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM users
        WHERE last_seen >= %s
        """,
        (week_start,),
    )

    active_week = cursor.fetchone()[0]

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM users
        WHERE last_seen >= %s
        """,
        (month_start,),
    )

    active_month = cursor.fetchone()[0]

    # -------------------------
    # MESSAGES
    # -------------------------

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM usage_events
        WHERE event_type = 'text'
        AND created_at >= %s
        """,
        (today_start,),
    )

    messages_today = cursor.fetchone()[0]

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM usage_events
        WHERE event_type = 'text'
        AND created_at >= %s
        """,
        (week_start,),
    )

    messages_week = cursor.fetchone()[0]

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM usage_events
        WHERE event_type = 'text'
        """
    )

    total_messages = cursor.fetchone()[0]

    # -------------------------
    # VOICE
    # -------------------------

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM usage_events
        WHERE event_type = 'voice'
        AND created_at >= %s
        """,
        (today_start,),
    )

    voice_today = cursor.fetchone()[0]

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM usage_events
        WHERE event_type = 'voice'
        AND created_at >= %s
        """,
        (week_start,),
    )

    voice_week = cursor.fetchone()[0]

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM usage_events
        WHERE event_type = 'voice'
        """
    )

    total_voice = cursor.fetchone()[0]

    # -------------------------
    # AVERAGE
    # -------------------------

    if total_users > 0:
        average_messages = round(
            total_messages / total_users,
            1,
        )
    else:
        average_messages = 0

    cursor.close()
    connection.close()

    return {
        "total_users": total_users,
        "new_users_today": new_users_today,
        "new_users_week": new_users_week,
        "new_users_month": new_users_month,
        "active_today": active_today,
        "active_week": active_week,
        "active_month": active_month,
        "messages_today": messages_today,
        "messages_week": messages_week,
        "total_messages": total_messages,
        "voice_today": voice_today,
        "voice_week": voice_week,
        "total_voice": total_voice,
        "average_messages": average_messages,
    }


# =========================================================
# INVITE BUTTON
# =========================================================

def invite_button():
    share_url = (
        "https://t.me/share/url?"
        + urlencode(
            {
                "url": BOT_LINK,
                "text": "Try AskOra 🤖 — a free AI assistant on Telegram!",
            }
        )
    )

    keyboard = [
        [
            InlineKeyboardButton(
                "✨ Invite a Friend",
                url=share_url,
            )
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# SEND LONG MESSAGE
# =========================================================

async def send_long_message(
    message,
    text,
):
    max_length = 4000

    chunks = [
        text[i:i + max_length]
        for i in range(
            0,
            len(text),
            max_length,
        )
    ]

    for index, chunk in enumerate(chunks):
        if index == len(chunks) - 1:
            await message.reply_text(
                chunk,
                reply_markup=invite_button(),
            )
        else:
            await message.reply_text(chunk)


# =========================================================
# START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id

    try:
        record_user(user_id)

        await update.message.reply_text(
            "👋 Welcome to AskOra!\n\n"
            "🤖 Your simple AI assistant.\n"
            "Ask me anything — by text or voice. 🎤",
            reply_markup=invite_button(),
        )

    except Exception:
        logger.exception("Start command failed")

        await update.message.reply_text(
            "👋 Welcome to AskOra!\n\n"
            "Ask me anything — by text or voice. 🎤"
        )


# =========================================================
# ADMIN
# =========================================================

async def admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id

    if user_id != ADMIN_ID:
        await update.message.reply_text(
            "⛔ You are not authorized to use this command."
        )
        return

    try:
        stats = get_statistics()

        text = (
            "📊 ASKORA ADMIN DASHBOARD\n\n"

            "👥 USERS\n"
            f"• Total: {stats['total_users']}\n"
            f"• New today: {stats['new_users_today']}\n"
            f"• New this week: {stats['new_users_week']}\n"
            f"• New this month: {stats['new_users_month']}\n\n"

            "🟢 ACTIVE USERS\n"
            f"• Today: {stats['active_today']}\n"
            f"• This week: {stats['active_week']}\n"
            f"• This month: {stats['active_month']}\n\n"

            "💬 TEXT USAGE\n"
            f"• Today: {stats['messages_today']}\n"
            f"• This week: {stats['messages_week']}\n"
            f"• Total: {stats['total_messages']}\n\n"

            "🎤 VOICE USAGE\n"
            f"• Today: {stats['voice_today']}\n"
            f"• This week: {stats['voice_week']}\n"
            f"• Total: {stats['total_voice']}\n\n"

            f"📈 Average messages/user: "
            f"{stats['average_messages']}"
        )

        await update.message.reply_text(text)

    except Exception:
        logger.exception("Admin statistics failed")

        await update.message.reply_text(
            "⚠️ I couldn't load the statistics."
        )


# =========================================================
# TEXT CHAT
# =========================================================

async def chat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id
    message = update.message.text.strip()

    if not message:
        return

    try:
        record_user(user_id)
        record_usage(user_id, "text")

        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action="typing",
        )

        history = get_history(
            user_id,
            limit=10,
        )

        messages = [
            {
                "role": "system",
                "content": SYSTEM_INSTRUCTION,
            }
        ]

        messages.extend(history)

        messages.append(
            {
                "role": "user",
                "content": message,
            }
        )

        response = groq_client.chat.completions.create(
            model=TEXT_MODEL,
            messages=messages,
            temperature=0.7,
            max_tokens=1000,
        )

        answer = response.choices[0].message.content.strip()

        save_message(
            user_id,
            "user",
            message,
        )

        save_message(
            user_id,
            "assistant",
            answer,
        )

        await send_long_message(
            update.message,
            answer,
        )

    except Exception:
        logger.exception("Text chat failed")

        await update.message.reply_text(
            "⚠️ Sorry, something went wrong. Please try again."
        )


# =========================================================
# VOICE CHAT
# =========================================================

async def voice_chat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id

    try:
        record_user(user_id)
        record_usage(user_id, "voice")

        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action="typing",
        )

        voice = update.message.voice

        telegram_file = await context.bot.get_file(
            voice.file_id
        )

        audio_bytes = await telegram_file.download_as_bytearray()

        audio_file = (
            "voice.ogg",
            BytesIO(bytes(audio_bytes)),
        )

        transcription = groq_client.audio.transcriptions.create(
            model=VOICE_MODEL,
            file=audio_file,
        )

        transcribed_text = transcription.text.strip()

        if not transcribed_text:
            await update.message.reply_text(
                "⚠️ I couldn't understand that voice note."
            )
            return

        history = get_history(
            user_id,
            limit=10,
        )

        messages = [
            {
                "role": "system",
                "content": SYSTEM_INSTRUCTION,
            }
        ]

        messages.extend(history)

        messages.append(
            {
                "role": "user",
                "content": transcribed_text,
            }
        )

        response = groq_client.chat.completions.create(
            model=TEXT_MODEL,
            messages=messages,
            temperature=0.7,
            max_tokens=1000,
        )

        answer = response.choices[0].message.content.strip()

        save_message(
            user_id,
            "user",
            transcribed_text,
        )

        save_message(
            user_id,
            "assistant",
            answer,
        )

        await send_long_message(
            update.message,
            answer,
        )

    except Exception:
        logger.exception("Voice chat failed")

        await update.message.reply_text(
            "⚠️ Sorry, I couldn't process that voice note."
        )


# =========================================================
# RESET CONVERSATION
# =========================================================

async def reset(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id

    try:
        connection = get_connection()
        cursor = connection.cursor()

        cursor.execute(
            """
            DELETE FROM conversations
            WHERE user_id = %s
            """,
            (user_id,),
        )

        connection.commit()

        cursor.close()
        connection.close()

        await update.message.reply_text(
            "🔄 Your conversation history has been cleared."
        )

    except Exception:
        logger.exception("Reset failed")

        await update.message.reply_text(
            "⚠️ I couldn't clear your conversation."
        )


# =========================================================
# FLASK ROUTES
# =========================================================

@app.route("/", methods=["GET"])
def home():
    return jsonify(
        {
            "status": "online",
            "bot": "AskOra",
            "ai": "Groq",
        }
    )


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)

        update = Update.de_json(
            data,
            application.bot,
        )

        asyncio.run_coroutine_threadsafe(
            application.process_update(update),
            loop,
        )

        return jsonify(
            {
                "ok": True,
            }
        )

    except Exception:
        logger.exception("Webhook error")

        return jsonify(
            {
                "ok": False,
            }
        ), 500


# =========================================================
# ASYNCIO LOOP
# =========================================================

loop = asyncio.new_event_loop()


def run_loop():
    asyncio.set_event_loop(loop)
    loop.run_forever()


loop_thread = threading.Thread(
    target=run_loop,
    daemon=True,
)

loop_thread.start()


# =========================================================
# TELEGRAM APPLICATION
# =========================================================

application = (
    Application.builder()
    .token(TELEGRAM_BOT_TOKEN)
    .build()
)


application.add_handler(
    CommandHandler(
        "start",
        start,
    )
)

application.add_handler(
    CommandHandler(
        "admin",
        admin,
    )
)

application.add_handler(
    CommandHandler(
        "reset",
        reset,
    )
)

application.add_handler(
    MessageHandler(
        filters.VOICE,
        voice_chat,
    )
)

application.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        chat,
    )
)


# =========================================================
# BOT INITIALIZATION
# =========================================================

async def initialize_bot():
    await application.initialize()

    await application.start()

    webhook_url = (
        RENDER_EXTERNAL_URL.rstrip("/")
        + "/webhook"
    )

    await application.bot.set_webhook(
        url=webhook_url
    )

    logger.info(
        "AskOra webhook set to %s",
        webhook_url,
    )


asyncio.run_coroutine_threadsafe(
    initialize_bot(),
    loop,
)


# =========================================================
# DATABASE INITIALIZATION
# =========================================================

try:
    init_database()
except Exception:
    logger.exception(
        "Database initialization failed"
    )


# =========================================================
# START SERVER
# =========================================================

if __name__ == "__main__":
    logger.info("AskOra is running...")

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
    )
