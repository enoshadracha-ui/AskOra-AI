import os
import asyncio
import threading
import logging

from flask import Flask, request, jsonify
from google import genai

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# CONFIG
# =========================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")

MODEL = "gemini-3.6-flash"
PORT = int(os.environ.get("PORT", 10000))

# =========================
# CHECK ENVIRONMENT
# =========================

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is missing")

if not RENDER_EXTERNAL_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL is missing")

# =========================
# GEMINI
# =========================

client = genai.Client(api_key=GEMINI_API_KEY)

# =========================
# USER DATA
# =========================

users = {}


def get_user(user_id):
    if user_id not in users:
        users[user_id] = {
            "chat_history": []
        }

    return users[user_id]


# =========================
# MESSAGE SPLITTING
# =========================

def split_message(text, max_length=4000):
    return [
        text[i:i + max_length]
        for i in range(0, len(text), max_length)
    ]


async def send_long_message(update, text):
    for part in split_message(text):
        await update.message.reply_text(part)


# =========================
# START
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = (
        "Welcome to Askora 🤖\n\n"
        "I'm your AI assistant powered by Gemini.\n\n"
        "Ask me anything and I'll do my best to help.\n\n"
        "You can use Askora completely free and without a daily question limit.\n\n"
        "Just send me a message to begin."
    )

    await update.message.reply_text(message)


# =========================
# RESET
# =========================

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)

    user["chat_history"] = []

    await update.message.reply_text(
        "Your conversation has been reset. 🔄"
    )


# =========================
# CHAT
# =========================

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    user = get_user(user_id)

    question = update.message.text.strip()

    if not question:
        return

    # Show typing indicator
    await update.message.chat.send_action("typing")

    try:
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=MODEL,
            contents=question
        )

        answer = response.text

        if not answer:
            answer = "Sorry, I couldn't generate a response."

        # Save local history
        user["chat_history"].append({
            "user": question,
            "assistant": answer
        })

        # Keep history from growing forever
        if len(user["chat_history"]) > 50:
            user["chat_history"] = user["chat_history"][-50:]

        await send_long_message(update, answer)

    except Exception as e:
        logging.exception("Gemini error")

        await update.message.reply_text(
            "Sorry, something went wrong while processing your question. "
            "Please try again."
        )


# =========================
# TELEGRAM APPLICATION
# =========================

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
        filters.TEXT & ~filters.COMMAND,
        chat
    )
)


# =========================
# PERSISTENT EVENT LOOP
# =========================

event_loop = asyncio.new_event_loop()


def run_event_loop():
    asyncio.set_event_loop(event_loop)
    event_loop.run_forever()


loop_thread = threading.Thread(
    target=run_event_loop,
    daemon=True
)

loop_thread.start()


# =========================
# INITIALIZE TELEGRAM
# =========================

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


# =========================
# FLASK
# =========================

flask_app = Flask(__name__)


@flask_app.route("/", methods=["GET"])
def home():

    return jsonify({
        "status": "online",
        "bot": "Askora",
        "mode": "free",
        "limit": "unlimited"
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


# =========================
# START SERVER
# =========================

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

Then do this

1. Replace everything in "bot.py".
2. Save.
3. Commit/push the change to GitHub.
4. Render should automatically start a new deployment.
5. Wait until Render says Live.
6. Open Telegram.
7. Send:
   "/start"
8. Then test:
   "2 + 2"

Your Render environment should now only need:

TELEGRAM_BOT_TOKEN
GEMINI_API_KEY
RENDER_EXTERNAL_URL

And yes — delete "BANK_NAME", "ACCOUNT_NAME", and "ACCOUNT_NUMBER" from Render.
