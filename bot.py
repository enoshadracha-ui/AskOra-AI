import os
import logging
from datetime import datetime, timedelta, timezone

from google import genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from flask import Flask, request

# =========================
# SETTINGS
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

MODEL = "gemini-2.5-flash"

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

users = {}
payment_requests = {}


# =========================
# USER HELPERS
# =========================

def get_user(user_id):
    if user_id not in users:
        users[user_id] = {
            "questions_today": 0,
            "last_date": datetime.now(timezone.utc).date(),
            "premium_until": None,
            "history": [],
        }

    user = users[user_id]

    today = datetime.now(timezone.utc).date()

    if user["last_date"] != today:
        user["questions_today"] = 0
        user["last_date"] = today

    return user


def is_premium(user):
    expiry = user.get("premium_until")

    if expiry and expiry > datetime.now(timezone.utc):
        return True

    return False


# =========================
# START
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)

    premium_text = ""

    if is_premium(user):
        premium_text = (
            f"\n\n💎 Premium active until "
            f"{user['premium_until'].strftime('%Y-%m-%d %H:%M UTC')}"
        )

    await update.message.reply_text(
        "🤖 Welcome!\n\n"
        "I'm your AI assistant powered by Gemini.\n\n"
        f"🆓 Free users: {FREE_DAILY_LIMIT} questions/day\n"
        f"💎 Premium: ₦{PREMIUM_PRICE} for {PREMIUM_DAYS} days"
        f"{premium_text}\n\n"
        "Just send me a message to begin.\n\n"
        "Use /premium to upgrade.\n"
        "Use /reset to clear your conversation."
    )


# =========================
# PREMIUM
# =========================

async def premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton(
                "💳 I've Made Payment",
                callback_data="payment_made",
            )
        ]
    ]

    text = (
        "💎 PREMIUM\n\n"
        f"Price: ₦{PREMIUM_PRICE}\n"
        f"Duration: {PREMIUM_DAYS} days\n\n"
        "🏦 Bank Transfer Details\n\n"
        f"Bank: {BANK_NAME}\n"
        f"Account Name: {ACCOUNT_NAME}\n"
        f"Account Number: {ACCOUNT_NUMBER}\n\n"
        f"Transfer exactly ₦{PREMIUM_PRICE}.\n\n"
        "After making the transfer, tap the button below."
    )

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =========================
# PAYMENT BUTTON
# =========================

async def payment_made(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    context.user_data["payment_step"] = "name"

    await query.message.reply_text(
        "🧾 Payment verification\n\n"
        "Please send the name used for the transfer."
    )


# =========================
# RESET
# =========================

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)

    user["history"] = []

    await update.message.reply_text(
        "🔄 Your conversation has been reset."
    )


# =========================
# PAYMENT INFORMATION
# =========================

async def handle_payment_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get("payment_step")

    if not step:
        return False

    text = update.message.text.strip()

    if step == "name":
        context.user_data["payment_name"] = text
        context.user_data["payment_step"] = "amount"

        await update.message.reply_text(
            "💰 Enter the amount you transferred."
        )
        return True

    if step == "amount":
        try:
            amount = int(
                text.replace(",", "")
                .replace("₦", "")
                .strip()
            )
        except ValueError:
            await update.message.reply_text(
                "Please enter the amount as a number.\n"
                "Example: 600"
            )
            return True

        context.user_data["payment_amount"] = amount
        context.user_data["payment_step"] = "reference"

        await update.message.reply_text(
            "🔖 Send your transfer/reference number."
        )
        return True

    if step == "reference":
        user_id = update.effective_user.id

        payment_id = len(payment_requests) + 1

        payment_requests[payment_id] = {
            "user_id": user_id,
            "name": context.user_data.get("payment_name"),
            "amount": context.user_data.get("payment_amount"),
            "reference": text,
            "status": "pending",
        }

        context.user_data.clear()

        keyboard = [
            [
                InlineKeyboardButton(
                    "✅ APPROVE",
                    callback_data=f"approve_{payment_id}",
                ),
                InlineKeyboardButton(
                    "❌ REJECT",
                    callback_data=f"reject_{payment_id}",
                ),
            ]
        ]

        payment = payment_requests[payment_id]

        admin_message = (
            "🔔 NEW PAYMENT REQUEST\n\n"
            f"User: {update.effective_user.full_name}\n"
            f"Telegram ID: {user_id}\n\n"
            f"Name: {payment['name']}\n"
            f"Amount: ₦{payment['amount']}\n"
            f"Reference: {payment['reference']}\n\n"
            f"Payment ID: {payment_id}"
        )

        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=admin_message,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

        await update.message.reply_text(
            "✅ Payment request submitted.\n\n"
            "Your payment will be manually reviewed by the administrator."
        )

        return True

    return False


# =========================
# ADMIN APPROVAL / REJECTION
# =========================

async def payment_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if query.from_user.id != ADMIN_ID:
        await query.answer(
            "You are not authorized.",
            show_alert=True,
        )
        return

    await query.answer()

    data = query.data

    if data.startswith("approve_"):
        payment_id = int(data.split("_")[1])
        action = "approve"

    elif data.startswith("reject_"):
        payment_id = int(data.split("_")[1])
        action = "reject"

    else:
        return

    payment = payment_requests.get(payment_id)

    if not payment:
        await query.message.reply_text(
            "❌ Payment request not found."
        )
        return

    if payment["status"] != "pending":
        await query.message.reply_text(
            f"⚠️ This payment has already been {payment['status']}."
        )
        return

    user_id = payment["user_id"]

    if action == "approve":
        user = get_user(user_id)

        user["premium_until"] = (
            datetime.now(timezone.utc)
            + timedelta(days=PREMIUM_DAYS)
        )

        payment["status"] = "approved"

        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "🎉 PAYMENT APPROVED!\n\n"
                "💎 Premium is now active.\n"
                f"⏳ Duration: {PREMIUM_DAYS} days\n"
                f"💰 Amount: ₦{payment['amount']}\n\n"
                "Premium expires:\n"
                f"{user['premium_until'].strftime('%Y-%m-%d %H:%M UTC')}"
            ),
        )

        await query.message.edit_text(
            query.message.text + "\n\n✅ APPROVED"
        )

    else:
        payment["status"] = "rejected"

        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "❌ PAYMENT REJECTED\n\n"
                "Your payment could not be verified.\n"
                "Please contact the administrator if you believe this was a mistake."
            ),
        )

        await query.message.edit_text(
            query.message.text + "\n\n❌ REJECTED"
        )


# =========================
# GEMINI AI CHAT
# =========================

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)

    if await handle_payment_info(update, context):
        return

    if not is_premium(user):

        if user["questions_today"] >= FREE_DAILY_LIMIT:
            await update.message.reply_text(
                "🆓 You have reached your daily free limit.\n\n"
                f"Free limit: {FREE_DAILY_LIMIT} questions/day\n"
                f"Premium: ₦{PREMIUM_PRICE} for {PREMIUM_DAYS} days\n\n"
                "Use /premium to upgrade."
            )
            return

        user["questions_today"] += 1

    message = update.message.text

    user["history"].append(
        {
            "role": "user",
            "content": message,
        }
    )

    conversation = []

    for item in user["history"][-10:]:
        conversation.append(
            f"{item['role'].upper()}: {item['content']}"
        )

    prompt = (
        "You are a helpful AI assistant. "
        "Answer clearly, accurately and naturally.\n\n"
        "Conversation:\n"
        + "\n".join(conversation)
    )

    try:
        response = gemini_client.models.generate_content(
            model=MODEL,
            contents=prompt,
        )

        answer = response.text

        user["history"].append(
            {
                "role": "assistant",
                "content": answer,
            }
        )

        await update.message.reply_text(answer)

    except Exception as error:
        logging.error("Gemini error: %s", error)

        await update.message.reply_text(
            "⚠️ Sorry, something went wrong while generating the response."
        )


# =========================
# PAYMENT SUPPORT
# =========================

async def paysupport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💳 Payment Support\n\n"
        "For Premium payment issues, please contact the administrator."
    )


# =========================
# TELEGRAM APPLICATION
# =========================

application = Application.builder().token(
    TELEGRAM_BOT_TOKEN
).build()

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("premium", premium))
application.add_handler(CommandHandler("reset", reset))
application.add_handler(CommandHandler("paysupport", paysupport))

application.add_handler(
    CallbackQueryHandler(
        payment_decision,
        pattern=r"^(approve|reject)_\d+$",
    )
)

application.add_handler(
    CallbackQueryHandler(
        payment_made,
        pattern=r"^payment_made$",
    )
)

application.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        chat,
    )
)


# =========================
# FLASK WEBHOOK SERVER
# =========================

flask_app = Flask(__name__)


@flask_app.route("/", methods=["GET"])
def home():
    return "🤖 Bot is running!", 200


@flask_app.route("/webhook", methods=["POST"])
async def webhook():
    data = request.get_json(force=True)

    update = Update.de_json(
        data,
        application.bot,
    )

    await application.process_update(update)

    return "OK", 200


# =========================
# START WEBHOOK
# =========================

async def setup():
    await application.initialize()
    await application.start()

    port = int(os.environ.get("PORT", 10000))

    render_url = os.environ.get("RENDER_EXTERNAL_URL")

    if render_url:
        webhook_url = f"{render_url}/webhook"

        await application.bot.set_webhook(
            url=webhook_url
        )

        logging.info(
            "Webhook set to %s",
            webhook_url,
        )

    return port


if __name__ == "__main__":
    import asyncio

    port = asyncio.run(setup())

    flask_app.run(
        host="0.0.0.0",
        port=port,
    )
