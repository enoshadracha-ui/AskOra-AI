import os
import asyncio
import threading
import logging
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlencode, parse_qsl
from io import BytesIO

from flask import Flask, jsonify, render_template, request
import psycopg2
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

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
RENDER_EXTERNAL_URL = os.environ["RENDER_EXTERNAL_URL"]
DATABASE_URL = os.environ["DATABASE_URL"]

PORT = int(os.environ.get("PORT", "10000"))

TEXT_MODEL = "openai/gpt-oss-120b"
VOICE_MODEL = "whisper-large-v3-turbo"

ADMIN_ID = 7721346673

BOT_USERNAME = "askora_official_bot"
BOT_LINK = f"https://t.me/{BOT_USERNAME}"

groq_client = Groq(api_key=GROQ_API_KEY)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

app = Flask(__name__)

# =========================
# AI INSTRUCTION
# =========================

SYSTEM_INSTRUCTION = """
You are AskOra, a simple and friendly AI assistant.

Give clear, useful and accurate answers.

Keep answers reasonably short and easy to understand.

Do not unnecessarily repeat the user's question.

Use simple formatting when helpful.
"""

# =========================
# DATABASE
# =========================


def get_connection():
    return psycopg2.connect(DATABASE_URL)


def init_database():
    connection = get_connection()

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                last_seen TIMESTAMPTZ DEFAULT NOW()
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_events (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                event_type TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
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
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
            """
        )

        connection.commit()

    finally:
        connection.close()


def record_user(user_id, username=None, first_name=None, last_name=None):
    connection = get_connection()

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            INSERT INTO users (
                user_id,
                username,
                first_name,
                last_name
            )
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id)
            DO UPDATE SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                last_name = EXCLUDED.last_name,
                last_seen = NOW()
            """,
            (
                user_id,
                username,
                first_name,
                last_name,
            ),
        )

        connection.commit()

    finally:
        connection.close()


def record_usage(user_id, event_type):
    connection = get_connection()

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            INSERT INTO usage_events (
                user_id,
                event_type
            )
            VALUES (%s, %s)
            """,
            (
                user_id,
                event_type,
            ),
        )

        connection.commit()

    finally:
        connection.close()


def save_message(user_id, role, content):
    connection = get_connection()

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            INSERT INTO conversations (
                user_id,
                role,
                content
            )
            VALUES (%s, %s, %s)
            """,
            (
                user_id,
                role,
                content,
            ),
        )

        connection.commit()

    finally:
        connection.close()


def get_history(user_id, limit=20):
    connection = get_connection()

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            SELECT role, content
            FROM conversations
            WHERE user_id = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (
                user_id,
                limit,
            ),
        )

        rows = cursor.fetchall()

        rows.reverse()

        return [
            {
                "role": row[0],
                "content": row[1],
            }
            for row in rows
        ]

    finally:
        connection.close()


def clear_history(user_id):
    connection = get_connection()

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            DELETE FROM conversations
            WHERE user_id = %s
            """,
            (user_id,),
        )

        connection.commit()

    finally:
        connection.close()


def get_statistics():
    connection = get_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM conversations")
        total_messages = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM usage_events")
        total_events = cursor.fetchone()[0]

        return {
            "users": total_users,
            "messages": total_messages,
            "events": total_events,
        }

    finally:
        connection.close()


# =========================
# INVITE BUTTON
# =========================


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


# =========================
# TELEGRAM MESSAGE HELPER
# =========================


async def send_long_message(
    bot,
    chat_id,
    text,
    reply_markup=None,
):
    max_length = 4000

    if len(text) <= max_length:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
        )
        return

    chunks = [
        text[i:i + max_length]
        for i in range(0, len(text), max_length)
    ]

    for index, chunk in enumerate(chunks):
        if index == len(chunks) - 1:
            await bot.send_message(
                chat_id=chat_id,
                text=chunk,
                reply_markup=reply_markup,
            )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=chunk,
            )


# =========================
# AI
# =========================


def generate_answer(messages):
    response = groq_client.chat.completions.create(
        model=TEXT_MODEL,
        messages=messages,
        temperature=0.7,
        max_tokens=1000,
    )

    return response.choices[0].message.content.strip()


def transcribe_audio(audio_bytes, filename="voice.ogg"):
    response = groq_client.audio.transcriptions.create(
        file=(filename, BytesIO(audio_bytes)),
        model=VOICE_MODEL,
    )

    return response.text.strip()


# =========================
# MINI APP AUTHENTICATION
# =========================


def validate_init_data(init_data):
    if not init_data:
        return None

    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))

        received_hash = parsed.pop("hash", None)

        if not received_hash:
            return None

        data_check_string = "\n".join(
            f"{key}={value}"
            for key, value in sorted(parsed.items())
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

        auth_date = parsed.get("auth_date")

        if auth_date:
            if time.time() - int(auth_date) > 86400:
                return None

        user_data = parsed.get("user")

        if not user_data:
            return None

        return json.loads(user_data)

    except Exception as error:
        logger.error("Mini App validation error: %s", error)
        return None


def get_mini_app_user_id():
    init_data = request.headers.get("X-Telegram-Init-Data", "")

    user_data = validate_init_data(init_data)

    if not user_data:
        return None

    return int(user_data["id"])


# =========================
# TELEGRAM COMMANDS
# =========================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        "Ask me anything — by text or voice. 🎤"
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    clear_history(user_id)

    await update.message.reply_text(
        "🧹 Your AskOra conversation has been cleared."
    )


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    stats = get_statistics()

    await update.message.reply_text(
        "📊 AskOra Statistics\n\n"
        f"👥 Users: {stats['users']}\n"
        f"💬 Messages: {stats['messages']}\n"
        f"⚡ Events: {stats['events']}"
    )


# =========================
# TELEGRAM VOICE
# =========================


async def handle_voice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    user_id = user.id

    record_user(
        user.id,
        user.username,
        user.first_name,
        user.last_name,
    )

    await context.bot.send_chat_action(
        chat_id=user_id,
        action="typing",
    )

    try:
        voice = update.message.voice

        telegram_file = await context.bot.get_file(
            voice.file_id
        )

        audio_bytes = bytes(
            await telegram_file.download_as_bytearray()
        )

        text = await asyncio.to_thread(
            transcribe_audio,
            audio_bytes,
            "voice.ogg",
        )

        if not text:
            await update.message.reply_text(
                "⚠️ I couldn't understand that voice note."
            )
            return

        save_message(
            user_id,
            "user",
            text,
        )

        history = get_history(
            user_id,
            limit=20,
        )

        messages = [
            {
                "role": "system",
                "content": SYSTEM_INSTRUCTION,
            }
        ]

        messages.extend(history)

        await context.bot.send_chat_action(
            chat_id=user_id,
            action="typing",
        )

        answer = await asyncio.to_thread(
            generate_answer,
            messages,
        )

        save_message(
            user_id,
            "assistant",
            answer,
        )

        record_usage(
            user_id,
            "voice",
        )

        await send_long_message(
            context.bot,
            user_id,
            answer,
            invite_button(),
        )

    except Exception as error:
        logger.exception(
            "Voice error: %s",
            error,
        )

        await update.message.reply_text(
            "⚠️ Sorry, something went wrong while processing your voice note."
        )


# =========================
# TELEGRAM TEXT CHAT
# =========================


async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    user_id = user.id

    message = update.message.text.strip()

    if not message:
        return

    record_user(
        user.id,
        user.username,
        user.first_name,
        user.last_name,
    )

    await context.bot.send_chat_action(
        chat_id=user_id,
        action="typing",
    )

    try:
        save_message(
            user_id,
            "user",
            message,
        )

        history = get_history(
            user_id,
            limit=20,
        )

        messages = [
            {
                "role": "system",
                "content": SYSTEM_INSTRUCTION,
            }
        ]

        messages.extend(history)

        await context.bot.send_chat_action(
            chat_id=user_id,
            action="typing",
        )

        answer = await asyncio.to_thread(
            generate_answer,
            messages,
        )

        save_message(
            user_id,
            "assistant",
            answer,
        )

        record_usage(
            user_id,
            "text",
        )

        await send_long_message(
            context.bot,
            user_id,
            answer,
            invite_button(),
        )

    except Exception as error:
        logger.exception(
            "Chat error: %s",
            error,
        )

        await update.message.reply_text(
            "⚠️ Sorry, something went wrong while generating the response."
        )


# =========================
# FLASK — MINI APP
# =========================


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify(
        {
            "status": "online",
            "bot": "AskOra",
            "ai": "Groq",
        }
    )


@app.route("/api/history", methods=["GET"])
def api_history():
    user_id = get_mini_app_user_id()

    if not user_id:
        return jsonify(
            {
                "error": "Unauthorized"
            }
        ), 401

    history = get_history(
        user_id,
        limit=100,
    )

    return jsonify(
        {
            "history": history
        }
    )


@app.route("/api/chat", methods=["POST"])
def api_chat():
    user_id = get_mini_app_user_id()

    if not user_id:
        return jsonify(
            {
                "error": "Unauthorized"
            }
        ), 401

    data = request.get_json(silent=True) or {}

    message = str(
        data.get("message", "")
    ).strip()

    if not message:
        return jsonify(
            {
                "error": "Message is required"
            }
        ), 400

    record_user(user_id)

    save_message(
        user_id,
        "user",
        message,
    )

    history = get_history(
        user_id,
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
        answer = generate_answer(messages)

        save_message(
            user_id,
            "assistant",
            answer,
        )

        record_usage(
            user_id,
            "mini_app_text",
        )

        return jsonify(
            {
                "answer": answer
            }
        )

    except Exception as error:
        logger.exception(
            "Mini App chat error: %s",
            error,
        )

        return jsonify(
            {
                "error": "AI response failed"
            }
        ), 500


@app.route("/api/voice", methods=["POST"])
def api_voice():
    user_id = get_mini_app_user_id()

    if not user_id:
        return jsonify(
            {
                "error": "Unauthorized"
            }
        ), 401

    audio = request.files.get("audio")

    if not audio:
        return jsonify(
            {
                "error": "Audio is required"
            }
        ), 400

    try:
        audio_bytes = audio.read()

        text = transcribe_audio(
            audio_bytes,
            audio.filename or "voice.webm",
        )

        if not text:
            return jsonify(
                {
                    "error": "Could not understand the voice"
                }
            ), 400

        record_user(user_id)

        save_message(
            user_id,
            "user",
            text,
        )

        history = get_history(
            user_id,
            limit=20,
        )

        messages = [
            {
                "role": "system",
                "content": SYSTEM_INSTRUCTION,
            }
        ]

        messages.extend(history)

        answer = generate_answer(messages)

        save_message(
            user_id,
            "assistant",
            answer,
        )

        record_usage(
            user_id,
            "mini_app_voice",
        )

        return jsonify(
            {
                "transcript": text,
                "answer": answer,
            }
        )

    except Exception as error:
        logger.exception(
            "Mini App voice error: %s",
            error,
        )

        return jsonify(
            {
                "error": "Voice processing failed"
            }
        ), 500


@app.route("/api/reset", methods=["POST"])
def api_reset():
    user_id = get_mini_app_user_id()

    if not user_id:
        return jsonify(
            {
                "error": "Unauthorized"
            }
        ), 401

    clear_history(user_id)

    return jsonify(
        {
            "success": True
        }
    )


# =========================
# TELEGRAM WEBHOOK
# =========================


async def setup_bot(application):
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
        "Telegram webhook set to %s",
        webhook_url,
    )


def start_async_loop(application):
    global bot_loop

    bot_loop = asyncio.new_event_loop()

    asyncio.set_event_loop(bot_loop)

    bot_loop.run_until_complete(
        setup_bot(application)
    )

    bot_loop.run_forever()


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        update_data = request.get_json(
            force=True
        )

        update = Update.de_json(
            update_data,
            telegram_application.bot,
        )

        future = asyncio.run_coroutine_threadsafe(
            telegram_application.process_update(update),
            bot_loop,
        )

        future.result(timeout=30)

        return "OK", 200

    except Exception as error:
        logger.exception(
            "Webhook error: %s",
            error,
        )

        return "ERROR", 500


# =========================
# START APPLICATION
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


def main():
    logger.info("Initializing database...")

    init_database()

    logger.info("Starting Telegram bot...")

    loop_thread = threading.Thread(
        target=start_async_loop,
        args=(telegram_application,),
        daemon=True,
    )

    loop_thread.start()

    logger.info(
        "AskOra is starting on port %s",
        PORT,
    )

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
