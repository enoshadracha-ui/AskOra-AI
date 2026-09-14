import os
import asyncio
import threading
import logging

from flask import Flask, request, jsonify
from google import genai
from google.genai import types

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")

MODEL = "gemini-3.6-flash"
PORT = int(os.environ.get("PORT", 10000))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is missing")

if not RENDER_EXTERNAL_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL is missing")

client = genai.Client(api_key=GEMINI_API_KEY)

SYSTEM_INSTRUCTION = """
You are Askora, a helpful AI assistant.

Answer questions simply, clearly, and directly.

Keep normal answers short, usually 2 to 5 sentences.

Use simple language that is easy to understand.

If the user sends a photo, carefully read the image and answer the question shown in it.

If the user sends a photo with a caption, follow the caption and focus on exactly what the user asks about.

If the user sends a voice note, understand the spoken request and answer it directly.

For school assignments, show the useful answer clearly and explain the important steps when needed.

Do not add unnecessary sections, tables, long examples, summaries, or extra explanations.

For simple questions, give a simple answer.

If the user asks for more detail, then explain in more detail.
"""

users = {}


def get_user(user_id):
    if user_id not in users:
        users[user_id] = {
            "chat_history": []
        }

    return users[user_id]


def split_message(text, max_length=4000):
    return [
        text[i:i + max_length]
        for i in range(0, len(text), max_length)
    ]


async def send_long_message(update, text):
    for part in split_message(text):
        await update.message.reply_text(part)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = (
        "Welcome to Askora 🤖\n\n"
        "Your AI assistant powered by Gemini.\n\n"
        "💬 Send a question\n"
        "📸 Send a photo of an assignment or question\n"
        "🎤 Send a voice note\n\n"
        "You can also send a photo with a caption like "
        "\"Answer question 3\".\n\n"
        "Askora is completely free with unlimited questions.\n\n"
        "Just send me anything to begin."
    )

    await update.message.reply_text(message)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)

    user["chat_history"] = []

    await update.message.reply_text(
        "Your conversation has been reset. 🔄"
    )


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    user = get_user(user_id)

    question = update.message.text.strip()

    if not question:
        return

    await update.message.chat.send_action("typing")

    try:
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=MODEL,
            contents=question,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION
            )
        )

        answer = response.text

        if not answer:
            answer = "Sorry, I couldn't generate a response."

        user["chat_history"].append({
            "user": question,
            "assistant": answer
        })

        if len(user["chat_history"]) > 50:
            user["chat_history"] = user["chat_history"][-50:]

        await send_long_message(update, answer)

    except Exception:
        logging.exception("Gemini text error")

        await update.message.reply_text(
            "Sorry, something went wrong while processing your question. "
            "Please try again."
        )


async def photo_chat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message or not update.message.photo:
        return

    user_id = update.effective_user.id
    user = get_user(user_id)

    await update.message.chat.send_action("typing")

    try:
        photo = update.message.photo[-1]

        telegram_file = await context.bot.get_file(
            photo.file_id
        )

        image_bytes = await telegram_file.download_as_bytearray()

        caption = (
            update.message.caption.strip()
            if update.message.caption
            else ""
        )

        if not caption:
            caption = (
                "Read this image carefully. "
                "If it contains a question or assignment, "
                "answer it directly."
            )

        image_part = types.Part.from_bytes(
            data=bytes(image_bytes),
            mime_type="image/jpeg"
        )

        response = await asyncio.to_thread(
            client.models.generate_content,
            model=MODEL,
            contents=[
                image_part,
                caption
            ],
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION
            )
        )

        answer = response.text

        if not answer:
            answer = "Sorry, I couldn't understand the image."

        user["chat_history"].append({
            "user": "[Photo] " + caption,
            "assistant": answer
        })

        if len(user["chat_history"]) > 50:
            user["chat_history"] = user["chat_history"][-50:]

        await send_long_message(update, answer)

    except Exception:
        logging.exception("Gemini image error")

        await update.message.reply_text(
            "Sorry, I couldn't process that photo. "
            "Please try sending it again with a clear image."
        )


async def voice_chat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message or not update.message.voice:
        return

    user_id = update.effective_user.id
    user = get_user(user_id)

    await update.message.chat.send_action("typing")

    try:
        voice = update.message.voice

        telegram_file = await context.bot.get_file(
            voice.file_id
        )

        audio_bytes = await telegram_file.download_as_bytearray()

        audio_part = types.Part.from_bytes(
            data=bytes(audio_bytes),
            mime_type="audio/ogg"
        )

        response = await asyncio.to_thread(
            client.models.generate_content,
            model=MODEL,
            contents=[
                audio_part,
                (
                    "Listen to this voice note carefully. "
                    "Understand what the user is asking and "
                    "answer the request directly."
                )
            ],
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION
            )
        )

        answer = response.text

        if not answer:
            answer = "Sorry, I couldn't understand the voice note."

        user["chat_history"].append({
            "user": "[Voice note]",
            "assistant": answer
        })

        if len(user["chat_history"]) > 50:
            user["chat_history"] = user["chat_history"][-50:]

        await send_long_message(update, answer)

    except Exception:
        logging.exception("Gemini audio error")

        await update.message.reply_text(
            "Sorry, I couldn't process that voice note. "
            "Please try sending it again."
        )


application = (
    Application.builder()
    .token(TELEGRAM_BOT_TOKEN)
    .build()
)


application.add_handler(
    CommandHandler("start", start)
)

application.add_handler(
    CommandHandler("reset", reset)
)

application.add_handler(
    MessageHandler(
        filters.PHOTO,
        photo_chat
    )
)

application.add_handler(
    MessageHandler(
        filters.VOICE,
        voice_chat
    )
)

application.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        chat
    )
)


event_loop = asyncio.new_event_loop()


def run_event_loop():
    asyncio.set_event_loop(event_loop)
    event_loop.run_forever()


loop_thread = threading.Thread(
    target=run_event_loop,
    daemon=True
)

loop_thread.start()


def initialize_bot():

    future = asyncio.run_coroutine_threadsafe(
        application.initialize(),
        event_loop
    )

    future.result()

    webhook_url = (
        RENDER_EXTERNAL_URL.rstrip("/")
        + "/webhook"
    )

    future = asyncio.run_coroutine_threadsafe(
        application.bot.set_webhook(url=webhook_url),
        event_loop
    )

    future.result()

    logging.info(
        f"Webhook set to {webhook_url}"
    )


initialize_bot()


flask_app = Flask(__name__)


@flask_app.route("/", methods=["GET"])
def home():

    return jsonify({
        "status": "online",
        "bot": "Askora",
        "mode": "free",
        "limit": "unlimited",
        "features": [
            "text",
            "photo",
            "voice"
        ]
    })


@flask_app.route("/webhook", methods=["POST"])
def webhook():

    try:

        data = request.get_json(force=True)

        update = Update.de_json(
            data,
            application.bot
        )

        asyncio.run_coroutine_threadsafe(
            application.process_update(update),
            event_loop
        )

        return jsonify({
            "ok": True
        })

    except Exception as e:

        logging.exception("Webhook error")

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


if __name__ == "__main__":

    logging.basicConfig(
        level=logging.INFO
    )

    flask_app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False
    )
