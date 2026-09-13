import os
import asyncio
import threading
import logging
from datetime import datetime, timedelta, timezone

from flask import Flask, request, jsonify
from google import genai

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)


# ============================================================
# CONFIGURATION
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")

ADMIN_ID = 7721346673

PREMIUM_PRICE = 600
PREMIUM_DAYS = 30
FREE_DAILY_LIMIT = 10

MODEL = "gemini-3.6-flash"

PORT = int(os.environ.get("PORT", 10000))

BANK_NAME = "YOUR BANK NAME"
ACCOUNT_NAME = "YOUR ACCOUNT NAME"
ACCOUNT_NUMBER = "YOUR ACCOUNT NUMBER"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# BASIC CHECKS
# ============================================================

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is missing")

if not RENDER_EXTERNAL_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL is missing")


# ============================================================
# GEMINI
# ============================================================

gemini_client = genai.Client(api_key=GEMINI_API_KEY)


# ============================================================
# USER DATA
# ============================================================

users = {}
payment_requests = {}
awaiting_payment_details = set()


def get_user(user_id):
    today = datetime.now(timezone.utc).date()

    if user_id not in users:
        users[user_id] = {
            "questions": 0,
            "date": today,
            "premium_until": None,
            "chat_history": [],
        }

    user = users[user_id]

    if user.get("date") != today:
        user["questions"] = 0
        user["date"] = today

    if "chat_history" not in user:
        user["chat_history"] = []

    return user


def is_premium(user):
    premium_until = user.get("premium_until")

    if not premium_until:
        return False

    return datetime.now(timezone.utc) < premium_until


# ============================================================
# TELEGRAM MESSAGE LENGTH FIX
# ============================================================

def split_message(text, max_length=4000):
    """
    Telegram allows messages of roughly 4096 characters.
    We use 4000 to leave a safe margin.
    """

    if not text:
        return ["Sorry, Gemini returned an empty response."]

    if len(text) <= max_length:
        return [text]

    chunks = []

    while len(text) > max_length:
        split_at = text.rfind("\n", 0, max_length)

        if split_at < 1000:
            split_at = text.rfind(" ", 0, max_length)

        if split_at < 1000:
            split_at = max_length

        chunks.append(text[:split_at].strip())
        text = text[split_at:].strip()

    if text:
        chunks.append(text)

    return chunks


async def send_long_message(update, text):
    """
    Sends a Gemini response safely even if it is longer
    than Telegram's message limit.
    """

    for chunk in split_message(text):
        await update.message.reply_text(chunk)


# ============================================================
# /START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    get_user(user_id)

    message = (
        "Welcome to Askora 🤖\n\n"
        "I'm your AI assistant powered by Gemini.\n\n"
        "Free users: 10 questions/day.\n"
        "Premium: ₦600 for 30 days.\n\n"
        "Just send me a message to begin.\n\n"
        "Use /status to check your usage.\n"
        "Use /premium to upgrade."
    )

    await update.message.reply_text(message)


# ============================================================
# /STATUS
# ============================================================

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)

    if is_premium(user):
        remaining_time = user["premium_until"] - datetime.now(timezone.utc)

        days = remaining_time.days

        if days < 0:
            days = 0

        await update.message.reply_text(
            "📊 Your Askora Status\n\n"
            "⭐ Plan: Premium\n"
            f"📅 Premium remaining: {days} day(s)\n\n"
            "Premium users have unlimited questions."
        )

        return

    used = user["questions"]
    remaining = max(0, FREE_DAILY_LIMIT - used)

    await update.message.reply_text(
        "📊 Your Askora Status\n\n"
        "🆓 Plan: Free\n"
        f"❓ Questions used today: {used}/{FREE_DAILY_LIMIT}\n"
        f"✅ Questions remaining: {remaining}\n\n"
        "Your free allowance resets each day."
    )


# ============================================================
# /RESET
# ============================================================

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)

    user["chat_history"] = []

    await update.message.reply_text(
        "🔄 Conversation context has been reset."
    )


# ============================================================
# /PREMIUM
# ============================================================

async def premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    keyboard = [
        [
            InlineKeyboardButton(
                "💳 Pay ₦600",
                callback_data="payment_done",
            )
        ]
    ]

    await update.message.reply_text(
        "⭐ Askora Premium\n\n"
        "Price: ₦600\n"
        "Duration: 30 days\n"
        "Unlimited questions.\n\n"
        "Make a bank transfer using the details "
        "provided after you continue.\n\n"
        "After payment, submit your payment details "
        "for admin verification.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ============================================================
# PAYMENT START
# ============================================================

async def payment_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    awaiting_payment_details.add(user_id)

    await query.message.reply_text(
        "💳 Premium Payment\n\n"
        f"Amount: ₦{PREMIUM_PRICE}\n\n"
        f"Bank: {BANK_NAME}\n"
        f"Account Name: {ACCOUNT_NAME}\n"
        f"Account Number: {ACCOUNT_NUMBER}\n\n"
        "After making the transfer, send me:\n"
        "1. Your name\n"
        "2. Amount paid\n"
        "3. Transaction/reference ID\n\n"
        "An admin will verify your payment."
    )


# ============================================================
# ADMIN PAYMENT APPROVAL
# ============================================================

async def approve_payment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.message.reply_text("Unauthorized.")
        return

    try:
        user_id = int(query.data.split(":")[1])
    except Exception:
        await query.message.reply_text("Invalid payment request.")
        return

    user = get_user(user_id)

    user["premium_until"] = (
        datetime.now(timezone.utc)
        + timedelta(days=PREMIUM_DAYS)
    )

    awaiting_payment_details.discard(user_id)

    await query.message.reply_text(
        f"✅ Payment approved for user {user_id}."
    )

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "🎉 Premium Activated!\n\n"
                "Your ₦600 payment has been approved.\n"
                "You now have Askora Premium for 30 days.\n\n"
                "Enjoy unlimited questions! 🚀"
            ),
        )
    except Exception as e:
        logger.exception("Could not notify premium user: %s", e)


# ============================================================
# ADMIN PAYMENT REJECTION
# ============================================================

async def reject_payment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.message.reply_text("Unauthorized.")
        return

    try:
        user_id = int(query.data.split(":")[1])
    except Exception:
        await query.message.reply_text("Invalid payment request.")
        return

    awaiting_payment_details.discard(user_id)

    await query.message.reply_text(
        f"❌ Payment rejected for user {user_id}."
    )

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "❌ Your Premium payment could not be verified.\n\n"
                "Please contact support or submit the correct "
                "payment information."
            ),
        )
    except Exception as e:
        logger.exception("Could not notify user: %s", e)


# ============================================================
# PAYMENT DETAILS
# ============================================================

async def handle_payment_details(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user_id = update.effective_user.id

    if user_id not in awaiting_payment_details:
        return False

    details = update.message.text

    payment_requests[user_id] = {
        "user_id": user_id,
        "details": details,
        "created_at": datetime.now(timezone.utc),
    }

    awaiting_payment_details.discard(user_id)

    keyboard = [
        [
            InlineKeyboardButton(
                "✅ Approve",
                callback_data=f"approve:{user_id}",
            ),
            InlineKeyboardButton(
                "❌ Reject",
                callback_data=f"reject:{user_id}",
            ),
        ]
    ]

    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "💰 NEW PREMIUM PAYMENT\n\n"
                f"User ID: {user_id}\n\n"
                "Payment details:\n"
                f"{details}"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

        await update.message.reply_text(
            "✅ Payment details received.\n\n"
            "Your payment has been sent to the admin for verification."
        )

    except Exception as e:
        logger.exception("Payment notification failed: %s", e)

        await update.message.reply_text(
            "⚠️ Your payment details were received, "
            "but there was a problem notifying the admin."
        )

    return True


# ============================================================
# CHAT WITH GEMINI
# ============================================================

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)

    # --------------------------------------------------------
    # Payment details get priority over normal chat
    # --------------------------------------------------------

    if await handle_payment_details(update, context):
        return

    # --------------------------------------------------------
    # Free user daily limit
    # --------------------------------------------------------

    if not is_premium(user):
        if user["questions"] >= FREE_DAILY_LIMIT:
            await update.message.reply_text(
                "⚠️ You have used all 10 free questions for today.\n\n"
                "Your allowance resets tomorrow.\n\n"
                "Use /premium to get unlimited questions for 30 days."
            )
            return

    prompt = update.message.text

    try:
        logger.info(
            "Generating Gemini response for user %s",
            user_id
        )

        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=MODEL,
            contents=prompt,
        )

        answer = response.text

        if not answer:
            answer = "Sorry, Gemini returned an empty response."

        # Only count the question after successful generation.
        if not is_premium(user):
            user["questions"] += 1

        # Save basic conversation history.
        user["chat_history"].append({
            "user": prompt,
            "assistant": answer,
        })

        # Keep memory from growing forever.
        if len(user["chat_history"]) > 20:
            user["chat_history"] = user["chat_history"][-20:]

        logger.info(
            "Gemini response generated successfully for user %s",
            user_id
        )

        # ----------------------------------------------------
        # IMPORTANT: send long answers in multiple messages
        # ----------------------------------------------------

        await send_long_message(update, answer)

    except Exception as e:
        logger.exception(
            "Gemini/chat error for user %s: %s",
            user_id,
            e
        )

        await update.message.reply_text(
            "⚠️ Sorry, something went wrong while generating the response."
        )


# ============================================================
# TELEGRAM APPLICATION
# ============================================================

application = (
    Application.builder()
    .token(TELEGRAM_BOT_TOKEN)
    .build()
)

application.add_handler(
    CommandHandler("start", start)
)

application.add_handler(
    CommandHandler("status", status)
)

application.add_handler(
    CommandHandler("premium", premium)
)

application.add_handler(
    CommandHandler("reset", reset)
)

application.add_handler(
    CallbackQueryHandler(
        payment_done,
        pattern="^payment_done$"
    )
)

application.add_handler(
    CallbackQueryHandler(
        approve_payment,
        pattern="^approve:"
    )
)

application.add_handler(
    CallbackQueryHandler(
        reject_payment,
        pattern="^reject:"
    )
)

application.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        chat
    )
)


# ============================================================
# PERSISTENT ASYNCIO EVENT LOOP
# ============================================================

event_loop = asyncio.new_event_loop()


def run_event_loop():
    asyncio.set_event_loop(event_loop)
    event_loop.run_forever()


loop_thread = threading.Thread(
    target=run_event_loop,
    daemon=True
)

loop_thread.start()


# ============================================================
# START TELEGRAM APPLICATION
# ============================================================

async def initialize_bot():
    await application.initialize()
    await application.start()

    webhook_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/webhook"

    await application.bot.set_webhook(
        url=webhook_url
    )

    logger.info(
        "Webhook set to %s",
        webhook_url
    )


future = asyncio.run_coroutine_threadsafe(
    initialize_bot(),
    event_loop
)

future.result(timeout=60)


# ============================================================
# FLASK WEB SERVER
# ============================================================

flask_app = Flask(__name__)


@flask_app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "online",
        "service": "Askora Telegram Bot"
    })


@flask_app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)

        update = Update.de_json(
            data,
            application.bot
        )

        future = asyncio.run_coroutine_threadsafe(
            application.process_update(update),
            event_loop
        )

        future.result(timeout=60)

        return jsonify({
            "ok": True
        })

    except Exception as e:
        logger.exception(
            "Webhook processing error: %s",
            e
        )

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 200


# ============================================================
# RUN SERVER
# ============================================================

if __name__ == "__main__":
    logger.info(
        "Askora starting on port %s",
        PORT
    )

    flask_app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
    )
