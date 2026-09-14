import os
import asyncio
import threading
import logging

from flask import Flask, request, jsonify
from groq import Groq

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
RENDER_EXTERNAL_URL = os.environ["RENDER_EXTERNAL_URL"]

PORT = int(os.environ.get("PORT", 10000))

TEXT_MODEL = "openai/gpt-oss-120b"

groq_client = Groq(api_key=GROQ_API_KEY)

SYSTEM_INSTRUCTION = """
You are AskOra, a helpful AI assistant.

Answer questions clearly and accurately.
Use simple language.
For school assignments, explain the answer clearly and directly.
Keep normal answers reasonably concise unless the user asks for more detail.
Do not add unnecessary sections or filler.
Be friendly and helpful.
"""

users = {}


def get_user(user_id):
    if user_id not in users:
        users[user_id] = []

    return users[user_id]


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


async def send_long_message(message, text):
    for part in split_message(text):
        await message.reply_text(part)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to AskOra!\n\n"
        "Ask me anything.\n"
        "You can also send me a voice note.\n\n"
        "Example:\n"
        "What is engineering materials?\n\n"
        "Commands:\n"
        "/start - Start AskOra\n"
        "/reset - Clear your conversation"
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    users[user_id] = []

    await update.message.reply_text(
        "✅ Your conversation has been cleared."
    )


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    question = update.message.text.strip()
    user_id = update.effective_user.id

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
        completion = groq_client.chat.completions.create(
            model=TEXT_MODEL,
            messages=messages,
            temperature=0.7,
            max_tokens=2048,
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

        await send_long_message(update.message, answer)

    except Exception:
        logging.exception("Groq text generation failed")

        await update.message.reply_text(
            "Sorry, something went wrong while processing your question."
        )


async def voice_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.voice:
        return

    await update.message.reply_text("🎤 Processing your voice note...")

    try:
        voice = await update.message.voice.get_file()

        audio_bytes = await voice.download_as_bytearray()

        transcription = groq_client.audio.transcriptions.create(
            file=("voice.ogg", bytes(audio_bytes)),
            model="whisper-large-v3-turbo",
        )

        text = transcription.text.strip()

        if not text:
            await update.message.reply_text(
                "Sorry, I couldn't understand that voice note."
            )
            return

        user_id = update.effective_user.id
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

        completion = groq_client.chat.completions.create(
            model=TEXT_MODEL,
            messages=messages,
            temperature=0.7,
            max_tokens=2048,
        )

        answer = completion.choices[0].message.content

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

        await send_long_message(update.message, answer)

    except Exception:
        logging.exception("Groq voice processing failed")

        await update.message.reply_text(
            "Sorry, I couldn't process that voice note."
        )


app = Flask(__name__)

telegram_application = (
    Application.builder()
    .token(TELEGRAM_BOT_TOKEN)
    .build()
)

telegram_application.add_handler(
    CommandHandler("start", start)
)

telegram_application.add_handler(
    CommandHandler("reset", reset)
)

telegram_application.add_handler(
    MessageHandler(
        filters.VOICE,
        voice_chat
    )
)

telegram_application.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        chat
    )
)


loop = asyncio.new_event_loop()


def run_loop():
    asyncio.set_event_loop(loop)
    loop.run_forever()


loop_thread = threading.Thread(
    target=run_loop,
    daemon=True
)

loop_thread.start()


async def initialize_bot():
    await telegram_application.initialize()

    await telegram_application.bot.set_webhook(
        url=RENDER_EXTERNAL_URL.rstrip("/") + "/webhook"
    )


future = asyncio.run_coroutine_threadsafe(
    initialize_bot(),
    loop
)

future.result()


@app.route("/", methods=["GET"])
def home():
    return jsonify(
        {
            "status": "online",
            "bot": "AskOra",
            "ai": "Groq",
            "features": [
                "text",
                "voice"
            ]
        }
    )


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)

        update = Update.de_json(
            data,
            telegram_application.bot
        )

        asyncio.run_coroutine_threadsafe(
            telegram_application.process_update(update),
            loop
        )

        return jsonify({"ok": True})

    except Exception:
        logging.exception("Webhook error")

        return jsonify({"ok": False}), 500


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False
    )
