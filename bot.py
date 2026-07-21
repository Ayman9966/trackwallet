import logging
import os
import sqlite3
import re
from datetime import datetime
from io import BytesIO
from typing import Optional, Tuple, List, Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.error import BadRequest, TelegramError

# ============================================================================
# CONFIGURATION
# ============================================================================

# Enable logging with more detail
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# States for ConversationHandler
SELECTING_ACTION, GET_AMOUNT, GET_DESCRIPTION, DELETE_RECORD = range(4)

# Database Setup (SQLite)
DB_NAME = "finance_bot.db"

# Constants
MAX_DESCRIPTION_LENGTH = 100
MAX_AMOUNT = 999999999.99
MIN_AMOUNT = 0.01


# ============================================================================
# DATABASE FUNCTIONS
# ============================================================================

def init_db() -> None:
    """Initialize the database with required tables and indexes."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Main transactions table
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('Income', 'Expense')),
            amount REAL NOT NULL CHECK(amount > 0),
            description TEXT NOT NULL,
            date TEXT NOT NULL
        )
        """
    )

    # Bot state table for message tracking
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_state (
            user_id INTEGER PRIMARY KEY,
            message_id INTEGER
        )
        """
    )

    # Create indexes for better performance
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_transactions_user_id ON transactions(user_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_transactions_user_type ON transactions(user_id, type)"
    )

    conn.commit()
    conn.close()
    logger.info("Database initialized successfully")


def get_db_connection() -> sqlite3.Connection:
    """Get a database connection with row factory."""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def get_user_data(user_id: int) -> Tuple[float, float, float, List[Tuple]]:
    """
    Get financial data for a user.

    Returns:
        Tuple of (total_income, total_expense, net_balance, recent_transactions)
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT COALESCE(SUM(amount), 0.0) FROM transactions WHERE user_id = ? AND type = 'Income'",
            (user_id,),
        )
        total_income = float(cursor.fetchone()[0])

        cursor.execute(
            "SELECT COALESCE(SUM(amount), 0.0) FROM transactions WHERE user_id = ? AND type = 'Expense'",
            (user_id,),
        )
        total_expense = float(cursor.fetchone()[0])

        net = total_income - total_expense

        cursor.execute(
            """SELECT id, type, amount, description, date 
               FROM transactions 
               WHERE user_id = ? 
               ORDER BY id DESC 
               LIMIT 5""",
            (user_id,),
        )
        rows = cursor.fetchall()

        return total_income, total_expense, net, rows
    except sqlite3.Error as e:
        logger.error(f"Database error in get_user_data for user {user_id}: {e}")
        return 0.0, 0.0, 0.0, []
    finally:
        conn.close()


def add_transaction(user_id: int, tx_type: str, amount: float, description: str) -> bool:
    """Add a new transaction to the database."""
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            """INSERT INTO transactions (user_id, type, amount, description, date) 
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, tx_type, amount, description, date_str),
        )
        conn.commit()
        logger.info(f"Added {tx_type} transaction for user {user_id}: {amount}")
        return True
    except sqlite3.Error as e:
        logger.error(f"Database error adding transaction for user {user_id}: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def delete_transaction_db(user_id: int, tx_id: int) -> Tuple[bool, int]:
    """
    Delete a transaction from the database.

    Returns:
        Tuple of (success, deleted_rows_count)
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "DELETE FROM transactions WHERE id = ? AND user_id = ?",
            (tx_id, user_id),
        )
        deleted_rows = cursor.rowcount
        conn.commit()

        if deleted_rows > 0:
            logger.info(f"Deleted transaction {tx_id} for user {user_id}")

        return True, deleted_rows
    except sqlite3.Error as e:
        logger.error(f"Database error deleting transaction {tx_id} for user {user_id}: {e}")
        conn.rollback()
        return False, 0
    finally:
        conn.close()


def get_all_transactions(user_id: int) -> List[sqlite3.Row]:
    """Get all transactions for a user for export."""
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            """SELECT id, type, amount, description, date 
               FROM transactions 
               WHERE user_id = ? 
               ORDER BY id ASC""",
            (user_id,),
        )
        return cursor.fetchall()
    except sqlite3.Error as e:
        logger.error(f"Database error getting transactions for user {user_id}: {e}")
        return []
    finally:
        conn.close()


# ============================================================================
# UI BUILDERS
# ============================================================================

def build_home_keyboard() -> InlineKeyboardMarkup:
    """Build the main menu keyboard."""
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


def build_back_keyboard() -> InlineKeyboardMarkup:
    """Build a keyboard with just a back button."""
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="btn_back")]]
    return InlineKeyboardMarkup(keyboard)


def generate_status_text(user_id: int) -> str:
    """Generate the status overview text."""
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
            # Escape markdown in description
            safe_desc = desc.replace("*", "\*").replace("_", "\_").replace("`", "\`")
            text += f"#{tx_id} | {emoji} {sign}{amount:.2f} | {safe_desc} _({date})_\n"

    return text


# ============================================================================
# MESSAGE MANAGEMENT
# ============================================================================

async def save_or_update_main_message(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> Optional[int]:
    """
    Save or update the main bot message.

    Returns:
        The message ID, or None if failed.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT message_id FROM bot_state WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
    except sqlite3.Error as e:
        logger.error(f"Database error getting bot state for user {user_id}: {e}")
        row = None
    finally:
        conn.close()

    if row:
        msg_id = row[0]
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="MarkdownV2",
            )
            return msg_id
        except BadRequest as e:
            # Message is not modified - this is okay
            if "message is not modified" in str(e).lower():
                return msg_id
            logger.warning(f"Failed to edit message {msg_id}: {e}")
        except TelegramError as e:
            logger.warning(f"Telegram error editing message {msg_id}: {e}")

    # Send new message if edit failed or no existing message
    try:
        new_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode="MarkdownV2",
        )

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "REPLACE INTO bot_state (user_id, message_id) VALUES (?, ?)",
                (user_id, new_msg.message_id),
            )
            conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Database error saving bot state for user {user_id}: {e}")
        finally:
            conn.close()

        return new_msg.message_id
    except TelegramError as e:
        logger.error(f"Failed to send message to user {user_id}: {e}")
        return None


async def safe_delete_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Safely delete a user's message."""
    if update.message:
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
            )
        except BadRequest as e:
            if "message to delete not found" not in str(e).lower():
                logger.warning(f"Could not delete message: {e}")
        except TelegramError as e:
            logger.warning(f"Telegram error deleting message: {e}")


# ============================================================================
# COMMAND HANDLERS
# ============================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /start command."""
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
        # Clean up old message if exists
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT message_id FROM bot_state WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            if row:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=row[0])
                except (BadRequest, TelegramError) as e:
                    logger.debug(f"Could not delete old message: {e}")
        except sqlite3.Error as e:
            logger.error(f"Database error in start for user {user_id}: {e}")
        finally:
            conn.close()

        await save_or_update_main_message(context, chat_id, user_id, text, reply_markup)

    return SELECTING_ACTION


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    help_text = (
        "📖 **Finance Bot Help**\n\n"
        "**Commands:**\n"
        "/start - Open the main menu\n"
        "/help - Show this help message\n"
        "/cancel - Cancel current operation\n\n"
        "**Features:**\n"
        "• 💰 Add expenses with descriptions\n"
        "• 💵 Add income with descriptions\n"
        "• 🗑 Delete transactions by ID\n"
        "• 📥 Export all data as CSV\n\n"
        "**Tips:**\n"
        "• Use the inline buttons to navigate\n"
        "• Your data is stored locally\n"
        "• Transaction IDs are shown in the list"
    )

    try:
        await update.message.reply_text(help_text, parse_mode="MarkdownV2")
    except TelegramError as e:
        logger.error(f"Error sending help message: {e}")


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /cancel command to reset conversation."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # Clean up user data
    context.user_data.clear()

    await safe_delete_user_message(update, context)

    text = "❌ Operation cancelled. Returning to main menu..."
    reply_markup = build_home_keyboard()

    await save_or_update_main_message(context, chat_id, user_id, text, reply_markup)

    return SELECTING_ACTION


# ============================================================================
# CALLBACK HANDLERS
# ============================================================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle inline button callbacks."""
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if data == "btn_back":
        # Clean up any pending user data
        context.user_data.pop("tx_type", None)
        context.user_data.pop("amount", None)

        text = generate_status_text(user_id)
        await save_or_update_main_message(
            context, chat_id, user_id, text, build_home_keyboard()
        )
        return SELECTING_ACTION

    elif data == "btn_expense":
        context.user_data["tx_type"] = "Expense"
        text = "💸 **Add Expense**\n\nPlease enter the amount:"
        await save_or_update_main_message(
            context, chat_id, user_id, text, build_back_keyboard()
        )
        return GET_AMOUNT

    elif data == "btn_income":
        context.user_data["tx_type"] = "Income"
        text = "💵 **Add Income**\n\nPlease enter the amount:"
        await save_or_update_main_message(
            context, chat_id, user_id, text, build_back_keyboard()
        )
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
        await save_or_update_main_message(
            context, chat_id, user_id, text, build_back_keyboard()
        )
        return DELETE_RECORD

    elif data == "btn_export":
        return await handle_export(update, context)

    return SELECTING_ACTION


async def handle_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle CSV export."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    rows = get_all_transactions(user_id)

    if not rows:
        text = "❌ No transactions to export."
        await save_or_update_main_message(
            context, chat_id, user_id, text, build_home_keyboard()
        )
        return SELECTING_ACTION

    try:
        # Build CSV content
        csv_content = "ID,Type,Amount,Description,Date\n"
        for row in rows:
            # Escape commas and quotes in description
            desc = str(row[3]).replace('"', '""')
            if ',' in desc or '"' in desc or '\n' in desc:
                desc = f'"{desc}"'
            csv_content += f"{row[0]},{row[1]},{row[2]},{desc},{row[4]}\n"

        file_bytes = BytesIO(csv_content.encode("utf-8"))
        file_bytes.name = f"transactions_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

        await context.bot.send_document(
            chat_id=chat_id,
            document=file_bytes,
            caption="📂 Here is your complete transaction export.",
        )

        logger.info(f"Exported {len(rows)} transactions for user {user_id}")

    except TelegramError as e:
        logger.error(f"Error sending export for user {user_id}: {e}")
        text = "❌ Error generating export. Please try again."
        await save_or_update_main_message(
            context, chat_id, user_id, text, build_home_keyboard()
        )
        return SELECTING_ACTION

    text = generate_status_text(user_id)
    await save_or_update_main_message(
        context, chat_id, user_id, text, build_home_keyboard()
    )
    return SELECTING_ACTION


# ============================================================================
# MESSAGE HANDLERS
# ============================================================================

async def receive_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle amount input from user."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    await safe_delete_user_message(update, context)

    try:
        amount_text = update.message.text.strip().replace(",", ".")
        amount = float(amount_text)

        if amount <= 0:
            raise ValueError("Amount must be positive")
        if amount > MAX_AMOUNT:
            raise ValueError(f"Amount too large (max: {MAX_AMOUNT})")

        # Round to 2 decimal places
        amount = round(amount, 2)

        context.user_data["amount"] = amount
        text = "📝 Now, enter a short description:"
        await save_or_update_main_message(
            context, chat_id, user_id, text, build_back_keyboard()
        )
        return GET_DESCRIPTION

    except ValueError as e:
        error_msg = str(e) if "too large" in str(e) or "positive" in str(e) else "Invalid amount"
        text = f"⚠️ {error_msg}. Please enter a valid number greater than 0:"
        await save_or_update_main_message(
            context, chat_id, user_id, text, build_back_keyboard()
        )
        return GET_AMOUNT


async def receive_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle description input and save transaction."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    await safe_delete_user_message(update, context)

    # Validate required data exists
    tx_type = context.user_data.get("tx_type")
    amount = context.user_data.get("amount")

    if not tx_type or amount is None:
        logger.warning(f"Missing user data for user {user_id}")
        text = "⚠️ Session expired. Please start over."
        await save_or_update_main_message(
            context, chat_id, user_id, text, build_home_keyboard()
        )
        return SELECTING_ACTION

    description = update.message.text.strip()

    # Validate description
    if not description:
        text = "⚠️ Description cannot be empty. Please enter a description:"
        await save_or_update_main_message(
            context, chat_id, user_id, text, build_back_keyboard()
        )
        return GET_DESCRIPTION

    if len(description) > MAX_DESCRIPTION_LENGTH:
        text = f"⚠️ Description too long (max {MAX_DESCRIPTION_LENGTH} chars). Please enter a shorter description:"
        await save_or_update_main_message(
            context, chat_id, user_id, text, build_back_keyboard()
        )
        return GET_DESCRIPTION

    # Sanitize description - remove control characters
    description = re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f]', '', description)

    # Save to database
    if add_transaction(user_id, tx_type, amount, description):
        text = generate_status_text(user_id)
    else:
        text = "❌ Error saving transaction. Please try again."

    # Clean up user data
    context.user_data.pop("tx_type", None)
    context.user_data.pop("amount", None)

    await save_or_update_main_message(
        context, chat_id, user_id, text, build_home_keyboard()
    )
    return SELECTING_ACTION


async def delete_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle transaction deletion."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    await safe_delete_user_message(update, context)

    try:
        tx_id = int(update.message.text.strip())

        if tx_id <= 0:
            raise ValueError("ID must be positive")

        success, deleted_rows = delete_transaction_db(user_id, tx_id)

        if not success:
            text = "❌ Database error. Please try again later."
            await save_or_update_main_message(
                context, chat_id, user_id, text, build_home_keyboard()
            )
            return SELECTING_ACTION

        if deleted_rows == 0:
            text = "⚠️ Transaction ID not found. Please enter a valid ID to delete:"
            await save_or_update_main_message(
                context, chat_id, user_id, text, build_back_keyboard()
            )
            return DELETE_RECORD

        text = generate_status_text(user_id)
        await save_or_update_main_message(
            context, chat_id, user_id, text, build_home_keyboard()
        )
        return SELECTING_ACTION

    except ValueError:
        text = "⚠️ Invalid ID. Please enter a numerical transaction ID:"
        await save_or_update_main_message(
            context, chat_id, user_id, text, build_back_keyboard()
        )
        return DELETE_RECORD


# ============================================================================
# ERROR HANDLER
# ============================================================================

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle errors."""
    logger.error(f"Update {update} caused error {context.error}")

    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "❌ An error occurred. Please use /start to restart the bot."
            )
        except TelegramError:
            pass


# ============================================================================
# MAIN
# ============================================================================

def main() -> None:
    """Main function to start the bot."""
    init_db()

    # Get token from environment variable - NEVER hardcode!
    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

    if not TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN environment variable not set!")
        print("ERROR: Please set the TELEGRAM_BOT_TOKEN environment variable.")
        print("Example: export TELEGRAM_BOT_TOKEN='your_bot_token_here'")
        return

    application = Application.builder().token(TOKEN).build()

    # Add error handler
    application.add_error_handler(error_handler)

    # Conversation handler
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
        ],
        states={
            SELECTING_ACTION: [
                CallbackQueryHandler(button_handler),
            ],
            GET_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_amount),
                CallbackQueryHandler(button_handler),
            ],
            GET_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_description),
                CallbackQueryHandler(button_handler),
            ],
            DELETE_RECORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, delete_transaction),
                CallbackQueryHandler(button_handler),
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("cancel", cancel_command),
            CommandHandler("help", help_command),
        ],
        per_message=False,
    )

    application.add_handler(conv_handler)

    # Add standalone command handlers (outside conversation)
    application.add_handler(CommandHandler("help", help_command))

    logger.info("Bot started successfully")
    print("✅ Bot is running...")
    print("Use /start to begin, /help for assistance, /cancel to reset")

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
