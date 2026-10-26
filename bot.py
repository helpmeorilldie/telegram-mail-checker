import os
import re
import sqlite3
import logging
from datetime import datetime, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================= CONFIGURATION =================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "")
CHANNEL_JOIN_URL = os.getenv("CHANNEL_JOIN_URL", "")

DB_NAME = "bot.sqlite3"
STARTER_CREDITS = 15
MAX_BULK = 10

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


# ================= DATABASE =================

def connect_db():
    return sqlite3.connect(DB_NAME)


def init_db():
    with connect_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                credits INTEGER DEFAULT 15,
                unlimited INTEGER DEFAULT 0,
                total_checks INTEGER DEFAULT 0,
                joined_at TEXT
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                result TEXT,
                created_at TEXT
            )
        """)


def register_user(user):
    with connect_db() as db:
        db.execute("""
            INSERT INTO users (
                user_id, username, first_name,
                credits, unlimited, total_checks, joined_at
            )
            VALUES (?, ?, ?, ?, 0, 0, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
        """, (
            user.id,
            user.username or "",
            user.first_name or "",
            STARTER_CREDITS,
            datetime.now(timezone.utc).isoformat(),
        ))


def get_user(user_id):
    with connect_db() as db:
        db.row_factory = sqlite3.Row
        return db.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()


def is_admin(user_id):
    return user_id in ADMIN_IDS


def is_valid_email(email):
    pattern = r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"
    return bool(re.fullmatch(pattern, email))


# ================= CHANNEL VERIFICATION =================

async def check_membership(user_id, context):
    if not REQUIRED_CHANNEL:
        return True

    try:
        member = await context.bot.get_chat_member(
            REQUIRED_CHANNEL,
            user_id
        )

        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )

    except Exception:
        log.exception("Membership verification failed")
        return False


async def require_channel(update, context):
    if await check_membership(
        update.effective_user.id, context
    ):
        return True

    keyboard = []

    if CHANNEL_JOIN_URL:
        keyboard.append([
            InlineKeyboardButton(
                "📢 Join Channel",
                url=CHANNEL_JOIN_URL
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "✅ Check Membership",
            callback_data="membership"
        )
    ])

    await update.effective_message.reply_text(
        "🔒 You must join our channel before using this bot.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return False


# ================= DASHBOARD =================

def dashboard_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📩 Single Check",
                callback_data="single"
            ),
            InlineKeyboardButton(
                "📚 Bulk Check",
                callback_data="bulk"
            ),
        ],
        [
            InlineKeyboardButton(
                "👤 My Profile",
                callback_data="profile"
            ),
            InlineKeyboardButton(
                "📊 Statistics",
                callback_data="stats"
            ),
        ],
        [
            InlineKeyboardButton(
                "📢 Channel",
                callback_data="channel"
            ),
            InlineKeyboardButton(
                "👨‍💻 Developer",
                callback_data="developer"
            ),
        ],
    ])


def dashboard_text(user_id):
    user = get_user(user_id)

    credits = (
        "Unlimited"
        if user["unlimited"]
        else str(user["credits"])
    )

    username = (
        "@" + user["username"]
        if user["username"]
        else user["first_name"]
    )

    return (
        "╔══════════════════════╗\n"
        "     🤖 MAIL CHECKER BOT\n"
        "╚══════════════════════╝\n\n"
        f"👤 User: {username}\n"
        f"🆔 ID: {user_id}\n"
        f"💳 Credits: {credits}\n"
        f"⚡ Unlimited: "
        f"{'ON' if user['unlimited'] else 'OFF'}\n\n"
        "Select an option below."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)

    if not await require_channel(update, context):
        return

    await update.effective_message.reply_text(
        dashboard_text(update.effective_user.id),
        reply_markup=dashboard_keyboard()
    )


# ================= EMAIL CHECKER =================

def verify_email(email):
    """
    Integration point for an authorized verification provider.

    Return:
        GOOD
        BAD
        UNKNOWN

    No Instagram account-association checks are implemented here.
    Email syntax validation does not prove account association.
    """
    return "UNKNOWN"


async def process_emails(update, context):
    mode = context.user_data.get("mode")

    if not mode:
        return

    if not await require_channel(update, context):
        return

    emails = [
        x.strip()
        for x in re.split(
            r"[\s,;]+",
            update.effective_message.text or ""
        )
        if x.strip()
    ]

    if mode == "single" and len(emails) != 1:
        await update.effective_message.reply_text(
            "Send exactly one email address."
        )
        return

    if mode == "bulk" and len(emails) > MAX_BULK:
        await update.effective_message.reply_text(
            f"Maximum {MAX_BULK} emails per request."
        )
        return

    if not emails:
        await update.effective_message.reply_text(
            "No email addresses found."
        )
        return

    invalid = [email for email in emails if not is_valid_email(email)]

    if invalid:
        await update.effective_message.reply_text(
            "❌ Invalid email format:\n" +
            "\n".join(invalid[:10])
        )
        return

    user_id = update.effective_user.id
    user = get_user(user_id)

    if not user["unlimited"] and user["credits"] < len(emails):
        await update.effective_message.reply_text(
            f"❌ Not enough credits.\n"
            f"Required: {len(emails)}\n"
            f"Available: {user['credits']}"
        )
        return

    # Atomically deduct credits before processing.
    with connect_db() as db:
        if not user["unlimited"]:
            result = db.execute("""
                UPDATE users
                SET credits = credits - ?
                WHERE user_id = ?
                  AND unlimited = 0
                  AND credits >= ?
            """, (len(emails), user_id, len(emails)))

            if result.rowcount != 1:
                await update.effective_message.reply_text(
                    "Balance changed. Please try again."
                )
                return

    context.user_data.pop("mode", None)

    output = []

    for email in emails:
        try:
            result = verify_email(email)

            if result not in ("GOOD", "BAD", "UNKNOWN"):
                result = "UNKNOWN"

            output.append(f"{result} | {email}")

            with connect_db() as db:
                db.execute("""
                    INSERT INTO checks (
                        user_id, result, created_at
                    )
                    VALUES (?, ?, ?)
                """, (
                    user_id,
                    result,
                    datetime.now(timezone.utc).isoformat(),
                ))

                db.execute("""
                    UPDATE users
                    SET total_checks = total_checks + 1
                    WHERE user_id = ?
                """, (user_id,))

        except Exception:
            output.append(f"UNKNOWN | {email}")

    user = get_user(user_id)

    remaining = (
        "Unlimited"
        if user["unlimited"]
        else str(user["credits"])
    )

    await update.effective_message.reply_text(
        "📋 RESULTS\n\n"
        + "\n".join(output)
        + f"\n\n💳 Remaining credits: {remaining}"
        + "\n\nUNKNOWN means the association was not verified."
    )


# ================= PROFILE =================

async def profile(update, context):
    user = get_user(update.effective_user.id)

    await update.effective_message.reply_text(
        "👤 YOUR PROFILE\n\n"
        f"User ID: {user['user_id']}\n"
        f"Username: @{user['username'] or 'None'}\n"
        f"Credits: "
        f"{'Unlimited' if user['unlimited'] else user['credits']}\n"
        f"Total checks: {user['total_checks']}\n"
        f"Joined: {user['joined_at'][:10]}"
    )


# ================= STATISTICS =================

async def stats(update, context):
    with connect_db() as db:
        users = db.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]

        checks = db.execute(
            "SELECT COUNT(*) FROM checks"
        ).fetchone()[0]

    await update.effective_message.reply_text(
        "📊 BOT STATISTICS\n\n"
        f"Registered users: {users}\n"
        f"Total checks: {checks}"
    )


# ================= ADMIN COMMANDS =================

async def addcredits(update, context):
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text(
            "⛔ Admin only."
        )
        return

    if len(context.args) != 2:
        await update.effective_message.reply_text(
            "/addcredits USER_ID AMOUNT"
        )
        return

    try:
        user_id, amount = map(int, context.args)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text(
            "Enter a valid user ID and positive amount."
        )
        return

    with connect_db() as db:
        result = db.execute("""
            UPDATE users
            SET credits = credits + ?
            WHERE user_id = ?
        """, (amount, user_id))

    await update.effective_message.reply_text(
        "Credits updated."
        if result.rowcount
        else "User not found."
    )


async def unlimited(update, context):
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text(
            "⛔ Admin only."
        )
        return

    if len(context.args) != 2:
        await update.effective_message.reply_text(
            "/unlimited USER_ID on|off"
        )
        return

    try:
        user_id = int(context.args[0])
        enabled = context.args[1].lower()

        if enabled not in ("on", "off"):
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text(
            "Usage: /unlimited USER_ID on|off"
        )
        return

    with connect_db() as db:
        result = db.execute("""
            UPDATE users
            SET unlimited = ?
            WHERE user_id = ?
        """, (int(enabled == "on"), user_id))

    await update.effective_message.reply_text(
        "Unlimited setting updated."
        if result.rowcount
        else "User not found."
    )


# ================= BUTTON HANDLER =================

async def buttons(update, context):
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    register_user(user)

    if query.data == "membership":
        if await check_membership(user.id, context):
            await query.message.reply_text(
                dashboard_text(user.id),
                reply_markup=dashboard_keyboard()
            )
        else:
            await query.message.reply_text(
                "Membership not detected. Join the channel first."
            )
        return

    if query.data == "channel":
        await query.message.reply_text(
            "Join the required channel to use the bot.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "📢 Join Channel",
                    url=CHANNEL_JOIN_URL
                )
            ]]) if CHANNEL_JOIN_URL else None
        )
        return

    if not await check_membership(user.id, context):
        await query.message.reply_text(
            "🔒 Join the required channel before using this feature."
        )
        return

    if query.data in ("single", "bulk"):
        context.user_data["mode"] = query.data

        prompt = (
            "Send one email address."
            if query.data == "single"
            else "Send up to 10 emails, separated by spaces, commas, or new lines."
        )

        await query.message.reply_text(prompt)

    elif query.data == "profile":
        await profile_from_button(query, user.id)

    elif query.data == "stats":
        with connect_db() as db:
            users = db.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]

            checks = db.execute(
                "SELECT COUNT(*) FROM checks"
            ).fetchone()[0]

        await query.message.reply_text(
            f"📊 STATISTICS\n\nUsers: {users}\nChecks: {checks}"
        )

    elif query.data == "developer":
        await query.message.reply_text(
            "👨‍💻 Developer\n\nContact the bot administrator for support."
        )


async def profile_from_button(query, user_id):
    user = get_user(user_id)

    await query.message.reply_text(
        "👤 YOUR PROFILE\n\n"
        f"ID: {user['user_id']}\n"
        f"Credits: "
        f"{'Unlimited' if user['unlimited'] else user['credits']}\n"
        f"Total checks: {user['total_checks']}"
    )


# ================= MAIN =================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Set TELEGRAM_BOT_TOKEN before starting the bot."
        )

    if not ADMIN_IDS:
        raise RuntimeError(
            "Set ADMIN_IDS to your numeric Telegram user ID."
        )

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("single", single_command))
    app.add_handler(CommandHandler("bulk", bulk_command))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("addcredits", addcredits))
    app.add_handler(CommandHandler("unlimited", unlimited))
    app.add_handler(CallbackQueryHandler(buttons))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            process_emails
        )
    )

    print("Bot is running...")
    app.run_polling()


async def single_command(update, context):
    if not await require_channel(update, context):
        return

    context.user_data["mode"] = "single"

    await update.effective_message.reply_text(
        "📩 Send one email address."
    )


async def bulk_command(update, context):
    if not await require_channel(update, context):
        return

    context.user_data["mode"] = "bulk"

    await update.effective_message.reply_text(
        "📚 Send up to 10 email addresses."
    )


if __name__ == "__main__":
    main()
