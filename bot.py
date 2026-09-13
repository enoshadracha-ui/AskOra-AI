import os
import asyncio
import threading
import logging
from datetime import datetime, timedelta, timezone

from flask import Flask, request
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

# =========================
# CONFIGURATION
# =========================

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

ADMIN_ID = 7721346673

PREMIUM_PRICE = 600
PREMIUM_DAYS = 30
FREE_DAILY_LIMIT = 10

BANK_NAME = os.environ.get("BANK_NAME", "YOUR BANK")
ACCOUNT_NAME = os.environ.get("ACCOUNT_NAME", "YOUR ACCOUNT NAME")
ACCOUNT_NUMBER = os.environ.get("ACCOUNT_NUMBER", "YOUR ACCOUNT NUMBER")

MODEL = "gemini-3.6-flash"

# =========================
# LOGGING
# =========================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# =========================
# DATA
# =========================

users = {}
payment_requests = {}

# =========================
# GEMINI
# =========================

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# =========================
# USER DATA HELPERS
# =========================

def get_user(user_id):
    if user_id not in users:
        users[user_id] = {
            "questions": 0,
            "date": datetime.now(timezone.utc).date(),
            "premium_until": None,
        }

    user = users[user_id]

    today = datetime.now(timezone.utc).date()

    if user.get("date") != today:
        user["date"] = today
        user["questions"] = 0

    return user


def is_premium(user):
    premium_until = user.get("premium_until")

    if not premium_until:
        return False

    return premium_until > datetime.now(timezone.utc)


# =========================
# START
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    get_user(user_id)

    await update.message.reply_text(
        "Welcome to Askora 🤖\n\n"
        "I'm your AI assistant powered by Gemini.\n\n"
        "🆓 Free users: 10 questions/day\n"
        "⭐ Premium: ₦600 for 30 days\n\n"
        "Just send me a message to begin.\n\n"
        "Use /status to check your usage.\n"
        "Use /premium to upgrade."
    )


# =========================
# STATUS
# =========================

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    user = get_user(user_id)

    questions = user.get("questions", 0)

    if is_premium(user):
        plan = "⭐ Premium"
        premium_until = user["premium_until"].strftime("%d %b %Y")
        usage = "Unlimited"
        remaining = "Unlimited"

        await update.message.reply_text(
            "📊 Your Askora Status\n\n"
            f"Plan: {plan}\n"
            f"Premium until: {premium_until}\n"
            f"Questions today: {questions}\n"
            f"Remaining: {remaining}"
        )

    else:
        plan = "🆓 Free"
        remaining = max(0, FREE_DAILY_LIMIT - questions)

        await update.message.reply_text(
            "📊 Your Askora Status\n\n"
            f"Plan: {plan}\n"
            f"Questions today: {questions}/{FREE_DAILY_LIMIT}\n"
            f"Remaining today: {remaining}\n"
            "Premium: Not active\n\n"
            "Use /premium to upgrade."
        )


# =========================
# RESET CHAT
# =========================

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    user = get_user(user_id)

    # Reset conversation-related data only.
    # The daily question counter is NOT reset.
    user["chat_history"] = []

    await update.message.reply_text(
        "🔄 Chat reset successfully.\n\n"
        "Your next question will start a fresh conversation.\n"
        "Your daily question count has NOT been reset."
    )


# =========================
# PREMIUM
# =========================

async def premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton(
                "💳 I've Made Payment",
                callback_data="payment_done",
            )
        ]
    ]

    await update.message.reply_text(
        "⭐ Askora Premium\n\n"
        "Price: ₦600\n"
        "Duration: 30 days\n\n"
        "🏦 Bank: " + BANK_NAME + "\n"
        "👤 Account Name: " + ACCOUNT_NAME + "\n"
        "💳 Account Number: " + ACCOUNT_NUMBER + "\n\n"
        "Transfer exactly ₦600, then press the button below.\n\n"
        "Your payment will be manually verified by the admin.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =========================
# PAYMENT BUTTON
# =========================

async def payment_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    payment_requests[user_id] = {
        "step": "name",
        "name": None,
        "amount": None,
        "reference": None,
    }

    await query.message.reply_text(
        "💳 Payment verification\n\n"
        "Please send the name used for the bank transfer."
    )


# =========================
# PAYMENT ADMIN APPROVAL
# =========================

async def approve_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.message.reply_text("❌ You are not authorized.")
        return

    try:
        user_id = int(query.data.split(":")[1])
    except Exception:
        await query.message.reply_text("❌ Invalid payment request.")
        return

    user = get_user(user_id)

    now = datetime.now(timezone.utc)

    current_until = user.get("premium_until")

    if current_until and current_until > now:
        start_date = current_until
    else:
        start_date = now

    user["premium_until"] = start_date + timedelta(days=PREMIUM_DAYS)

    payment_requests.pop(user_id, None)

    await query.message.reply_text(
        f"✅ Payment approved.\n\n"
        f"User ID: {user_id}\n"
        f"Premium until: "
        f"{user['premium_until'].strftime('%d %b %Y')}"
    )

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "🎉 Premium activated!\n\n"
                f"Your Askora Premium is active until "
                f"{user['premium_until'].strftime('%d %b %Y')}."
            ),
        )
    except Exception as error:
        logger.exception("Could not notify user: %s", error)


# =========================
# PAYMENT REJECTION
# =========================

async def reject_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.message.reply_text("❌ You are not authorized.")
        return

    try:
        user_id = int(query.data.split(":")[1])
    except Exception:
        await query.message.reply_text("❌ Invalid payment request.")
        return

    payment_requests.pop(user_id, None)

    await query.message.reply_text(
        f"❌ Payment rejected.\n\nUser ID: {user_id}"
    )

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "❌ Your payment could not be verified.\n\n"
                "Please check your payment details and contact support."
            ),
        )
    except Exception as error:
        logger.exception("Could not notify user: %s", error)


# =========================
# PAYMENT INFORMATION
# =========================

async def handle_payment_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in payment_requests:
        return False

    request_data = payment_requests[user_id]

    text = update.message.text.strip()

    if request_data["step"] == "name":

        request_data["name"] = text
        request_data["step"] = "amount"

        await update.message.reply_text(
            "How much did you transfer?\n\n"
            "Please enter the amount in naira."
        )

        return True

    if request_data["step"] == "amount":

        try:
            amount = float(text.replace(",", "").replace("₦", "").strip())
        except ValueError:

            await update.message.reply_text(
                "❌ Please enter a valid amount.\n\n"
                "Example: 600"
            )

            return True

        request_data["amount"] = amount
        request_data["step"] = "reference"

        await update.message.reply_text(
            "Please send your bank transfer reference/transaction ID."
        )

        return True

    if request_data["step"] == "reference":

        request_data["reference"] = text

        name = request_data["name"]
        amount = request_data["amount"]
        reference = request_data["reference"]

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

        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "💰 New Premium Payment\n\n"
                f"User ID: {user_id}\n"
                f"Transfer Name: {name}\n"
                f"Amount: ₦{amount}\n"
                f"Reference: {reference}"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

        payment_requests.pop(user_id, None)

        await update.message.reply_text(
            "✅ Payment details submitted.\n\n"
            "Your payment is now waiting for admin verification."
        )

        return True

    return False


# =========================
# GEMINI CHAT
# =========================

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id

    user = get_user(user_id)

    # Check if user is currently submitting payment details
    handled = await handle_payment_message(update, context)

    if handled:
        return

    # Premium users have unlimited questions
    if not is_premium(user):

        if user["questions"] >= FREE_DAILY_LIMIT:

            await update.message.reply_text(
                "🛑 You have reached your 10-question daily limit.\n\n"
                "Your limit resets automatically each day.\n\n"
                "Use /premium to get Premium for ₦600 / 30 days."
            )

            return

    prompt = update.message.text

    try:

        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=MODEL,
            contents=prompt,
        )

        answer = response.text

        if not answer:
            answer = "⚠️ Gemini returned an empty response."

        # Count the question only after Gemini successfully responds
        if not is_premium(user):
            user["questions"] += 1

        await update.message.reply_text(answer)

    except Exception as error:

        logger.exception("Gemini error: %s", error)

        await update.message.reply_text(
            "⚠️ Sorry, something went wrong while generating the response."
        )


# =========================
# TELEGRAM APPLICATION
# =========================

application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

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
    CallbackQueryHandler(payment_done, pattern="^payment_done$")
)

application.add_handler(
    CallbackQueryHandler(approve_payment, pattern="^approve:")
)

application.add_handler(
    CallbackQueryHandler(reject_payment, pattern="^reject:")
)

application.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        chat,
    )
)

# =========================
# PERSISTENT ASYNCIO LOOP
# =========================

event_loop = asyncio.new_event_loop()


def run_event_loop():
    asyncio.set_event_loop(event_loop)
    event_loop.run_forever()


loop_thread = threading.Thread(
    target=run_event_loop,
    daemon=True,
)

loop_thread.start()


async def initialize_application():

    await application.initialize()

    await application.start()

    render_url = os.environ.get("RENDER_EXTERNAL_URL")

    if render_url:

        webhook_url = render_url.rstrip("/") + "/webhook"

        await application.bot.set_webhook(
            url=webhook_url
        )

        logger.info(
            "Webhook set to %s",
            webhook_url
        )


startup_future = asyncio.run_coroutine_threadsafe(
    initialize_application(),
    event_loop,
)

startup_future.result()

# =========================
# FLASK WEB SERVER
# =========================

flask_app = Flask(__name__)


@flask_app.route("/", methods=["GET"])
def home():

    return "🤖 Askora Bot is running!", 200


@flask_app.route("/webhook", methods=["POST"])
def webhook():

    try:

        data = request.get_json(force=True)

        update = Update.de_json(
            data,
            application.bot,
        )

        future = asyncio.run_coroutine_threadsafe(
            application.process_update(update),
            event_loop,
        )

        future.result(timeout=30)

        return "OK", 200

    except Exception as error:

        logger.exception(
            "Webhook error: %s",
            error,
        )

        return "Webhook error", 500


# =========================
# START SERVER
# =========================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            10000,
        )
    )

    flask_app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
    )
