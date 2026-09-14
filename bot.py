import os
import asyncio
import threading
import logging
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

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


# =========================
# SETTINGS
# =========================

logging.basicConfig(level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
RENDER_EXTERNAL_URL = os.environ["RENDER_EXTERNAL_URL"]
DATABASE_URL = os.environ["DATABASE_URL"]

PORT = int(os.environ.get("PORT", 10000))

TEXT_MODEL = "openai/gpt-oss-120b"

ADMIN_ID = 7721346673

BOT_USERNAME = "askora_official_bot"
BOT_LINK = f"https://t.me/{BOT_USERNAME}"

groq_client = Groq(api_key=GROQ_API_KEY)


# =========================
# AI INSTRUCTION
# =========================

SYSTEM_INSTRUCTION = """
You are AskOra, a helpful AI assistant.

Give short, clear and direct answers.
Use simple language.
For school questions, give a clear answer without unnecessary details.
Usually answer in 2 to 5 sentences.
Do not use tables unless the user specifically asks.
Do not add unnecessary headings or sections.
Only give a longer explanation when the user asks for one.
"""


# =========================
# DATABASE
# =========================

def get_connection():
    return psycopg2.connect(DATABASE_URL)


def init_database():
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            first_seen TIMESTAMPTZ NOT NULL,
            last_seen TIMESTAMPTZ NOT NULL,
            messages INTEGER DEFAULT 0,
            voice_messages INTEGER DEFAULT 0
        )
    """)

    connection.commit()
    cursor.close()
    connection.close()


def record_user(user_id, is_voice=False):
    now = datetime.now(timezone.utc)

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute(
        """
        INSERT INTO users (
            user_id,
            first_seen,
            last_seen,
            messages,
            voice_messages
        )
        VALUES (%s, %s, %s, 1, %s)
        ON CONFLICT (user_id)
        DO UPDATE SET
            last_seen = EXCLUDED.last_seen,
            messages = users.messages + 1,
            voice_messages =
                users.voice_messages + EXCLUDED.voice_messages
        """,
        (
            user_id,
            now,
            now,
            1 if is_voice else 0,
        ),
    )

    connection.commit()
    cursor.close()
    connection.close()


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

    week_start = now - timedelta(days=7)

    month_start = now - timedelta(days=30)

    cursor.execute(
        "SELECT COUNT(*) FROM users"
    )

    total_users = cursor.fetchone()[0]

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

    cursor.execute(
        """
        SELECT COALESCE(SUM(messages), 0)
        FROM users
        """
    )

    total_messages = cursor.fetchone()[0]

    cursor.execute(
        """
        SELECT COALESCE(SUM(voice_messages), 0)
        FROM users
        """
    )

    voice_messages = cursor.fetchone()[0]

    cursor.close()
    connection.close()

    return {
        "total_users": total_users,
        "active_today": active_today,
        "active_week": active_week,
        "active_month": active_month,
        "total_messages": total_messages,
        "voice_messages": voice_messages,
    }


# =========================
# USER CHAT HISTORY
# =========================

users = {}


def get_user(user_id):
    if user_id not in users:
        users[user_id] = []

    return users[user_id]


# =========================
# MESSAGE HELPERS
# =========================

def split_message(text, limit=4000):

    if len(text) <= limit:
        return [text]

    parts = []

    while len(text) > limit:

        cut = text.rfind("\n", 0, limit)

        if cut < 1000:
            cut = text.rfind(" ", 0, limit)

        if cut < 1000:
            cut = limit

        parts.append(text[:cut])

        text = text[cut:].lstrip()

    if text:
        parts.append(text)

    return parts


def invite_button():

    share_url = (
        "https://t.me/share/url?"
        + urlencode(
            {
                "url": BOT_LINK,
                "text": (
                    "Try AskOra 🤖 — "
                    "a free AI assistant on Telegram!"
                ),
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


async def send_long_message(message, text):

    parts = split_message(text)

    for index, part in enumerate(parts):

        if index == len(parts) - 1:

            await message.reply_text(
                part,
                reply_markup=invite_button(),
            )

        else:

            await message.reply_text(part)


# =========================
# START
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    record_user(user_id)

    await update.message.reply_text(
        "👋 Welcome to AskOra!\n\n"
        "🤖 Your simple AI assistant.\n"
        "Ask me anything — by text or voice. 🎤"
    )


# =========================
# ADMIN DASHBOARD
# =========================

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    if user_id != ADMIN_ID:

        await update.message.reply_text(
            "⛔ You are not authorized to use this command."
        )

        return

    try:

        stats = get_statistics()

        await update.message.reply_text(
            "📊 ASKORA ADMIN DASHBOARD\n\n"
            f"👥 Total users: {stats['total_users']}\n"
            f"🟢 Active today: {stats['active_today']}\n"
            f"📅 Active this week: {stats['active_week']}\n"
            f"📆 Active this month: {stats['active_month']}\n\n"
            f"💬 Total messages: {stats['total_messages']}\n"
            f"🎤 Voice messages: {stats['voice_messages']}"
        )

    except Exception:

        logging.exception(
            "Admin statistics failed"
        )

        await update.message.reply_text(
            "⚠️ I couldn't load the statistics."
        )


# =========================
# TEXT CHAT
# =========================

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message or not update.message.text:
        return

    question = update.message.text.strip()

    user_id = update.effective_user.id

    record_user(user_id)

    history = get_user(user_id)

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
            "content": question,
        }
    )

    try:

        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action="typing",
        )

        completion = groq_client.chat.completions.create(
            model=TEXT_MODEL,
            messages=messages,
            temperature=0.5,
            max_tokens=500,
        )

        answer = completion.choices[0].message.content

        if not answer:
            answer = "Sorry, I couldn't generate a response."

        history.append(
            {
                "role": "user",
                "content": question,
            }
        )

        history.append(
            {
                "role": "assistant",
                "content": answer,
            }
        )

        if len(history) > 20:
            del history[:-20]

        await send_long_message(
            update.message,
            answer,
        )

    except Exception:

        logging.exception(
            "Groq text generation failed"
        )

        await update.message.reply_text(
            "Sorry, something went wrong."
        )


# =========================
# VOICE CHAT
# =========================

async def voice_chat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message or not update.message.voice:
        return

    user_id = update.effective_user.id

    record_user(
        user_id,
        is_voice=True,
    )

    await update.message.reply_text(
        "🎤 Processing..."
    )

    try:

        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action="typing",
        )

        voice = await update.message.voice.get_file()

        audio_bytes = await voice.download_as_bytearray()

        transcription = (
            groq_client.audio.transcriptions.create(
                file=(
                    "voice.ogg",
                    bytes(audio_bytes),
                ),
                model="whisper-large-v3-turbo",
            )
        )

        text = transcription.text.strip()

        if not text:

            await update.message.reply_text(
                "I couldn't understand the voice note."
            )

            return

        history = get_user(user_id)

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
                "content": text,
            }
        )

        completion = (
            groq_client.chat.completions.create(
                model=TEXT_MODEL,
                messages=messages,
                temperature=0.5,
                max_tokens=500,
            )
        )

        answer = completion.choices[0].message.content

        if not answer:
            answer = "Sorry, I couldn't generate a response."

        history.append(
            {
                "role": "user",
                "content": text,
            }
        )

        history.append(
            {
                "role": "assistant",
                "content": answer,
            }
        )

        if len(history) > 20:
            del history[:-20]

        await send_long_message(
            update.message,
            answer,
        )

    except Exception:

        logging.exception(
            "Groq voice processing failed"
        )

        await update.message.reply_text(
            "Sorry, I couldn't process that voice note."
        )


# =========================
# DATABASE INITIALIZATION
# =========================

init_database()


# =========================
# FLASK
# =========================

app = Flask(__name__)


# =========================
# TELEGRAM APPLICATION
# =========================

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
        "admin",
        admin,
    )
)


telegram_application.add_handler(
    MessageHandler(
        filters.VOICE,
        voice_chat,
    )
)


telegram_application.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        chat,
    )
)


# =========================
# ASYNCIO LOOP
# =========================

loop = asyncio.new_event_loop()


def run_loop():

    asyncio.set_event_loop(loop)

    loop.run_forever()


loop_thread = threading.Thread(
    target=run_loop,
    daemon=True,
)

loop_thread.start()


# =========================
# INITIALIZE BOT
# =========================

async def initialize_bot():

    await telegram_application.initialize()

    await telegram_application.bot.set_webhook(
        url=RENDER_EXTERNAL_URL.rstrip("/")
        + "/webhook"
    )


future = asyncio.run_coroutine_threadsafe(
    initialize_bot(),
    loop,
)

future.result()


# =========================
# HOME
# =========================

@app.route(
    "/",
    methods=["GET"],
)
def home():

    return jsonify(
        {
            "status": "online",
            "bot": "AskOra",
            "ai": "Groq",
        }
    )


# =========================
# WEBHOOK
# =========================

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
            telegram_application.bot,
        )

        asyncio.run_coroutine_threadsafe(
            telegram_application.process_update(
                update
            ),
            loop,
        )

        return jsonify(
            {
                "ok": True
            }
        )

    except Exception:

        logging.exception(
            "Webhook error"
        )

        return jsonify(
            {
                "ok": False
            }
        ), 500


# =========================
# RUN
# =========================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
    )
