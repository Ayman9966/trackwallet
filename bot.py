import logging
import os
import sqlite3
from datetime import datetime
from io import BytesIO
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# States for ConversationHandler
SELECTING_ACTION, GET_AMOUNT, GET_DESCRIPTION, DELETE_RECORD = range(4)

# Database Setup (SQLite)
DB_NAME = "finance_bot.db"


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            type TEXT,
            amount REAL,
            description TEXT,
            date TEXT
        )
    """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_state (
            user_id INTEGER PRIMARY KEY,
            message_id INTEGER
        )
    """
    )
    conn.commit()
    conn.close()


def get_user_data(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT SUM(amount) FROM transactions WHERE user_id = ? AND type = 'Income'",
        (user_id,),
    )
    income_res = cursor.fetchone()[0]
    total_income = income_res if income_res else 0.0

    cursor.execute(
        "SELECT SUM(amount) FROM transactions WHERE user_id = ? AND type = 'Expense'",
        (user_id,),
    )
    expense_res = cursor.fetchone()[0]
    total_expense = expense_res if expense_res else 0.0

    net = total_income - total_expense

    cursor.execute(
        "SELECT id, type, amount, description, date FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT 5",
        (user_id,),
    )
    rows = cursor.fetchall()
    conn.close()

    return total_income, total_expense, net, rows


def build_home_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("💰 Expense", callback_data="btn_expense"),
            InlineKeyboardButton("💵 Income", callback_data="btn_income"),
        ],
        [
            InlineKeyboardButton("🗑 Delete Record", callback_data="btn_delete"),
            InlineKeyboardButton("📥 Export", callback_data="btn_export"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def generate_status_text(user_id: int):
    income, expense, net, transactions = get_user_data(user_id)
    net_icon = "✅" if net >= 0 else "❌"

    text = f"📊 **Balance Overview**\n\n"
    text += f"💵 Income: +{income:.2f}\n"
    text += f"💸 Expenses: -{expense:.2f}\n\n"
    text += f"📈 Net: {net:+.2f} {net_icon}\n\n"
    text += f"📝 **Last 5 Transactions**\n"
    text += f"━━━━━━━━━━━━━━━━━━━\n"

    if not transactions:
        text += "_No transactions yet._"
    else:
        for tx in transactions:
            tx_id, tx_type, amount, desc, date = tx
            sign = "+" if tx_type == "Income" else "-"
            emoji = "💵" if tx_type == "Income" else "💸"
            text += f"#{tx_id} | {emoji} {sign}{amount:.2f} | {desc} _({date})_\n"

    return text


async def save_or_update_main_message(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, text: str, reply_markup
):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT message_id FROM bot_state WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()

    if row:
        msg_id = row[0]
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="Markdown",
            )
            return msg_id
        except Exception:
            pass

    new_msg = await context.bot.send_message(
        chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode="Markdown"
    )

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "REPLACE INTO bot_state (user_id, message_id) VALUES (?, ?)",
        (user_id, new_msg.message_id),
    )
    conn.commit()
    conn.close()
    return new_msg.message_id


async def safe_delete_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id, message_id=update.message.message_id
            )
        except Exception:
            pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if update.message:
        await safe_delete_user_message(update, context)

    text = generate_status_text(user_id)
    reply_markup = build_home_keyboard()

    if update.callback_query:
        await update.callback_query.answer()
        await save_or_update_main_message(context, chat_id, user_id, text, reply_markup)
    else:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT message_id FROM bot_state WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=row[0])
            except Exception:
                pass
        conn.close()

        await save_or_update_main_message(context, chat_id, user_id, text, reply_markup)

    return SELECTING_ACTION


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if data == "btn_expense":
        context.user_data["tx_type"] = "Expense"
        text = "💸 **Add Expense**\n\nPlease enter the amount:"
        await save_or_update_main_message(context, chat_id, user_id, text, None)
        return GET_AMOUNT

    elif data == "btn_income":
        context.user_data["tx_type"] = "Income"
        text = "💵 **Add Income**\n\nPlease enter the amount:"
        await save_or_update_main_message(context, chat_id, user_id, text, None)
        return GET_AMOUNT

    elif data == "btn_delete":
        _, _, _, transactions = get_user_data(user_id)
        if not transactions:
            text = "❌ No records available to delete."
            await save_or_update_main_message(
                context, chat_id, user_id, text, build_home_keyboard()
            )
            return SELECTING_ACTION

        text = "🗑 **Delete Record**\n\nPlease enter the ID number of the transaction you want to delete:"
        await save_or_update_main_message(context, chat_id, user_id, text, None)
        return DELETE_RECORD

    elif data == "btn_export":
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, type, amount, description, date FROM transactions WHERE user_id = ?",
            (user_id,),
        )
        rows = cursor.fetchall()
        conn.close()

        csv_content = "ID,Type,Amount,Description,Date\n"
        for row in rows:
            csv_content += f"{row[0]},{row[1]},{row[2]},{row[3]},{row[4]}\n"

        file_bytes = BytesIO(csv_content.encode("utf-8"))
        file_bytes.name = "transactions_export.csv"

        await context.bot.send_document(
            chat_id=chat_id,
            document=file_bytes,
            caption="📂 Here is your complete transaction export.",
        )

        text = generate_status_text(user_id)
        await save_or_update_main_message(
            context, chat_id, user_id, text, build_home_keyboard()
        )
        return SELECTING_ACTION


async def receive_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    await safe_delete_user_message(update, context)

    try:
        amount = float(update.message.text)
        if amount <= 0:
            raise ValueError
        context.user_data["amount"] = amount
        text = "📝 Now, enter a short description:"
        await save_or_update_main_message(context, chat_id, user_id, text, None)
        return GET_DESCRIPTION
    except ValueError:
        text = "⚠️ Invalid amount. Please enter a valid number greater than 0:"
        await save_or_update_main_message(context, chat_id, user_id, text, None)
        return GET_AMOUNT


async def receive_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    await safe_delete_user_message(update, context)

    description = update.message.text
    tx_type = context.user_data["tx_type"]
    amount = context.user_data["amount"]
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO transactions (user_id, type, amount, description, date) VALUES (?, ?, ?, ?, ?)",
        (user_id, tx_type, amount, description, date_str),
    )
    conn.commit()
    conn.close()

    text = generate_status_text(user_id)
    await save_or_update_main_message(
        context, chat_id, user_id, text, build_home_keyboard()
    )
    return SELECTING_ACTION


async def delete_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    await safe_delete_user_message(update, context)

    try:
        tx_id = int(update.message.text)

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM transactions WHERE id = ? AND user_id = ?", (tx_id, user_id)
        )
        deleted_rows = cursor.rowcount
        conn.commit()
        conn.close()

        if deleted_rows == 0:
            text = "⚠️ Transaction ID not found. Please enter a valid ID to delete, or send /start to cancel:"
            await save_or_update_main_message(context, chat_id, user_id, text, None)
            return DELETE_RECORD

        text = generate_status_text(user_id)
        await save_or_update_main_message(
            context, chat_id, user_id, text, build_home_keyboard()
        )
        return SELECTING_ACTION
    except ValueError:
        text = "⚠️ Invalid ID. Please enter a numerical transaction ID:"
        await save_or_update_main_message(context, chat_id, user_id, text, None)
        return DELETE_RECORD


def main():
    init_db()
    # Securely fetch token from Render environment variables
    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    if not TOKEN:
        raise ValueError("No TELEGRAM_BOT_TOKEN environment variable found!")

    application = Application.builder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(button_handler),
        ],
        states={
            SELECTING_ACTION: [CallbackQueryHandler(button_handler)],
            GET_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_amount)],
            GET_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_description)
            ],
            DELETE_RECORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, delete_transaction)
            ],
        },
        fallbacks=[CommandHandler("start", start)],
    )

    application.add_handler(conv_handler)

    print("Bot is running...")
    application.run_polling()


if __name__ == "__main__":
    main()