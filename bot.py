import os
import re
import csv
import io
import sqlite3

from datetime import datetime, timezone

from telegram import (
    Update,
    InputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)

from telegram.constants import ChatMemberStatus

from telegram.error import TelegramError

from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ==================================================
# SETTINGS
# ==================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

ALLOWED_GROUP_IDS = {
    int(x.strip())
    for x in os.getenv("ALLOWED_GROUP_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

DB_PATH = os.getenv("DB_PATH", "postcode_bot.db")


# ==================================================
# POSTCODE VALIDATION
# ==================================================

# Examples accepted:
#
# BD12 7
# BD7 4
# BD12 7AB
# bd7 4xy
#
# Only the sector is stored:
#
# BD12 7

POSTCODE_RE = re.compile(
    r"^\s*([A-Z]{1,2}\d[A-Z\d]?)\s*(\d)(?:\s*[A-Z]{2})?\s*$",
    re.IGNORECASE,
)


# ==================================================
# DATABASE
# ==================================================

def db():

    conn = sqlite3.connect(DB_PATH)

    conn.row_factory = sqlite3.Row

    return conn


def init_db():

    conn = db()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS postcode_submissions (

            telegram_id INTEGER PRIMARY KEY,

            postcode_sector TEXT NOT NULL,

            first_name TEXT,

            username TEXT,

            is_active INTEGER NOT NULL DEFAULT 1,

            created_at TEXT NOT NULL,

            updated_at TEXT NOT NULL

        )
        """
    )

    # Upgrade older database automatically

    columns = {
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(postcode_submissions)"
        ).fetchall()
    }

    if "is_active" not in columns:

        conn.execute(
            """
            ALTER TABLE postcode_submissions
            ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1
            """
        )

    conn.commit()

    conn.close()


# ==================================================
# BASIC HELPERS
# ==================================================

def is_admin(user_id):

    return user_id in ADMIN_IDS


def normalize_postcode_sector(text):

    text = (text or "").strip().upper()

    match = POSTCODE_RE.match(text)

    if not match:

        return None

    outward = match.group(1).upper()

    sector_number = match.group(2)

    return f"{outward} {sector_number}"


def postcode_district(sector):

    # BD12 7 becomes BD12

    return sector.split()[0]


def customer_record(user_id):

    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM postcode_submissions
        WHERE telegram_id = ?
        """,
        (user_id,),
    ).fetchone()

    conn.close()

    return row


def set_customer_active(user_id, active):

    conn = db()

    conn.execute(
        """
        UPDATE postcode_submissions
        SET is_active = ?
        WHERE telegram_id = ?
        """,
        (
            1 if active else 0,
            user_id,
        ),
    )

    conn.commit()

    conn.close()


# ==================================================
# GROUP MEMBERSHIP CHECK
# ==================================================

async def is_member_of_allowed_group(
    context,
    user_id
):

    # Admins always have access

    if is_admin(user_id):

        return True

    # Fail closed if no customer groups
    # have been configured yet

    if not ALLOWED_GROUP_IDS:

        return False

    for group_id in ALLOWED_GROUP_IDS:

        try:

            member = await context.bot.get_chat_member(
                chat_id=group_id,
                user_id=user_id,
            )

            status = member.status

            if status in {
                ChatMemberStatus.MEMBER,
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.OWNER,
            }:

                return True

            # Restricted users can still technically
            # be members of a group.

            if (
                status == ChatMemberStatus.RESTRICTED
                and getattr(member, "is_member", False)
            ):

                return True

        except TelegramError:

            # If one group cannot be checked,
            # move on to the next configured group.

            continue

    return False


async def verify_customer_access(
    update,
    context
):

    user = update.effective_user

    if not user:

        return False

    if is_admin(user.id):

        return True

    allowed = await is_member_of_allowed_group(
        context,
        user.id
    )

    existing = customer_record(user.id)

    if existing:

        set_customer_active(
            user.id,
            allowed
        )

    if not allowed:

        context.user_data[
            "awaiting_postcode_change"
        ] = False

        message = (
            "🔒 CUSTOMER ACCESS ONLY\n\n"
            "This postcode service is only available "
            "to customers who are members of one of "
            "our customer Telegram groups.\n\n"
            "If you believe you should have access, "
            "please contact an admin."
        )

        if update.callback_query:

            try:

                await update.callback_query.answer(
                    "Customer access required.",
                    show_alert=True,
                )

            except TelegramError:

                pass

            try:

                await update.callback_query.message.reply_text(
                    message
                )

            except TelegramError:

                pass

        elif update.message:

            await update.message.reply_text(
                message
            )

        return False

    return True


# ==================================================
# CUSTOMER BUTTONS
# ==================================================

def existing_customer_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✏️ Change Postcode",
                    callback_data="customer_change_postcode"
                )
            ],
            [
                InlineKeyboardButton(
                    "🗑 Remove My Postcode",
                    callback_data="customer_remove_postcode"
                )
            ],
        ]
    )


def confirm_remove_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Yes, Remove It",
                    callback_data="customer_confirm_remove"
                ),
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="customer_cancel_remove"
                ),
            ]
        ]
    )


def cancel_change_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "❌ Cancel Change",
                    callback_data="customer_cancel_change"
                )
            ]
        ]
    )


# ==================================================
# ADMIN BUTTONS
# ==================================================

def admin_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📊 Postcode Counts",
                    callback_data="admin_counts"
                ),
                InlineKeyboardButton(
                    "👥 Customer Summary",
                    callback_data="admin_total"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔥 Busiest Areas",
                    callback_data="admin_busiest"
                ),
                InlineKeyboardButton(
                    "🗺 Collection Planning",
                    callback_data="admin_plan"
                ),
            ],
            [
                InlineKeyboardButton(
                    "📄 Export CSV",
                    callback_data="admin_export"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔄 Refresh Customer Access",
                    callback_data="admin_refresh_access"
                )
            ],
            [
                InlineKeyboardButton(
                    "♻️ Refresh Dashboard",
                    callback_data="admin_menu"
                )
            ],
        ]
    )


def back_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⬅️ Back to Admin Menu",
                    callback_data="admin_menu"
                )
            ]
        ]
    )


# ==================================================
# ADMIN DATABASE REPORTS
# ==================================================

def get_counts():

    conn = db()

    rows = conn.execute(
        """
        SELECT
            postcode_sector,
            COUNT(*) AS customer_count

        FROM postcode_submissions

        WHERE is_active = 1

        GROUP BY postcode_sector

        ORDER BY
            customer_count DESC,
            postcode_sector ASC
        """
    ).fetchall()

    active = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM postcode_submissions
        WHERE is_active = 1
        """
    ).fetchone()["total"]

    inactive = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM postcode_submissions
        WHERE is_active = 0
        """
    ).fetchone()["total"]

    all_customers = active + inactive

    conn.close()

    return rows, active, inactive, all_customers


def make_counts_text():

    rows, active, inactive, total = get_counts()

    if not rows:

        return (
            "📊 POSTCODE COUNTS\n\n"
            "No active postcode submissions yet."
        )

    lines = [
        "📊 ACTIVE POSTCODE COUNTS",
        "",
        f"👥 Active customers: {active}",
        "",
    ]

    for row in rows:

        lines.append(
            f"📍 {row['postcode_sector']} — "
            f"{row['customer_count']}"
        )

    return "\n".join(lines)


def make_customer_summary():

    rows, active, inactive, total = get_counts()

    return (
        "👥 CUSTOMER SUMMARY\n\n"

        f"✅ Active customers: {active}\n"
        f"⛔ Inactive customers: {inactive}\n"
        f"👥 Total stored customers: {total}\n\n"

        "Inactive customers are excluded from "
        "postcode counts and collection planning."
    )


def make_busiest_text():

    rows, active, inactive, total = get_counts()

    if not rows:

        return (
            "🔥 BUSIEST AREAS\n\n"
            "No active postcode areas yet."
        )

    lines = [
        "🔥 BUSIEST AREAS",
        "",
        f"👥 Active customers: {active}",
        "",
        "Top postcode sectors:",
        "",
    ]

    for number, row in enumerate(
        rows[:10],
        start=1
    ):

        count = row["customer_count"]

        word = (
            "customer"
            if count == 1
            else "customers"
        )

        lines.append(
            f"{number}. "
            f"{row['postcode_sector']} "
            f"— {count} {word}"
        )

    return "\n".join(lines)


def make_collection_plan_text():

    rows, active, inactive, total = get_counts()

    if not rows:

        return (
            "🗺 COLLECTION PLANNING\n\n"
            "No active postcode areas yet."
        )

    grouped = {}

    for row in rows:

        sector = row["postcode_sector"]

        count = row["customer_count"]

        district = postcode_district(
            sector
        )

        if district not in grouped:

            grouped[district] = {
                "total": 0,
                "sectors": [],
            }

        grouped[district][
            "total"
        ] += count

        grouped[district][
            "sectors"
        ].append(
            (
                sector,
                count
            )
        )

    ordered = sorted(
        grouped.items(),
        key=lambda item: (
            -item[1]["total"],
            item[0]
        )
    )

    lines = [
        "🗺 COLLECTION PLANNING",
        "",
        f"👥 Active customers: {active}",
        "",
        "Nearby postcode sectors are grouped "
        "by postcode district.",
        "",
    ]

    for district, info in ordered:

        district_total = info["total"]

        word = (
            "customer"
            if district_total == 1
            else "customers"
        )

        lines.append(
            f"📌 {district} — "
            f"{district_total} {word}"
        )

        sectors = sorted(
            info["sectors"],
            key=lambda item: (
                -item[1],
                item[0]
            )
        )

        for sector, count in sectors:

            lines.append(
                f"   • {sector} — {count}"
            )

        lines.append("")

    return "\n".join(lines).strip()


# ==================================================
# START COMMAND
# ==================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user:

        return

    # Admin has separate dashboard access

    if is_admin(user.id):

        existing = customer_record(
            user.id
        )

        text = (
            "📍 POSTCODE COLLECTION BOT\n\n"
            "Customers simply send their postcode "
            "and the bot stores only their postcode sector.\n\n"
            "Examples:\n"
            "BD12 7\n"
            "BD7 4\n"
            "BD12 7AB"
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔐 Admin Dashboard",
                        callback_data="admin_menu"
                    )
                ]
            ]
        )

        await update.message.reply_text(
            text,
            reply_markup=keyboard
        )

        return

    allowed = await verify_customer_access(
        update,
        context
    )

    if not allowed:

        return

    existing = customer_record(
        user.id
    )

    if existing:

        await update.message.reply_text(
            "✅ YOU'RE ALREADY REGISTERED\n\n"

            f"📍 Your postcode area:\n"
            f"{existing['postcode_sector']}\n\n"

            "Only one postcode can be registered "
            "to each Telegram account.\n\n"

            "If you have moved or need to correct it, "
            "use the Change Postcode button below.",

            reply_markup=existing_customer_keyboard(),
        )

        return

    await update.message.reply_text(
        "📍 PLEASE SEND YOUR POSTCODE\n\n"

        "You can send either your full postcode "
        "or just the postcode sector.\n\n"

        "Examples:\n"
        "BD12 7\n"
        "BD7 4\n"
        "BD12 7AB\n\n"

        "🔒 For privacy, only the postcode sector "
        "is stored.\n\n"

        "For example:\n"
        "BD12 7AB → BD12 7"
    )


# ==================================================
# HELP
# ==================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user:

        return

    if is_admin(user.id):

        await update.message.reply_text(
            "🔐 ADMIN HELP\n\n"

            "/admin - Open admin dashboard\n"
            "/groupid - Show the current group's ID\n"
            "/counts - Active postcode counts\n"
            "/total - Customer summary\n"
            "/busiest - Busiest postcode areas\n"
            "/plan - Collection planning\n"
            "/export - Export active postcode counts"
        )

        return

    allowed = await verify_customer_access(
        update,
        context
    )

    if not allowed:

        return

    await update.message.reply_text(
        "📍 POSTCODE HELP\n\n"

        "Simply send your postcode as a normal message.\n\n"

        "Examples:\n"
        "BD12 7\n"
        "BD7 4\n"
        "BD12 7AB\n\n"

        "Only one postcode can be registered "
        "to each Telegram account.\n\n"

        "Use /start to view or change "
        "your registered postcode."
    )


# ==================================================
# GROUP ID COMMAND
# ==================================================

async def group_id_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    chat = update.effective_chat

    if not user or not is_admin(user.id):

        return

    if not chat:

        return

    if chat.type == "private":

        await update.message.reply_text(
            "ℹ️ To get a customer Group ID:\n\n"

            "1. Add this bot to the customer group.\n"
            "2. Make the bot an admin.\n"
            "3. Send /groupid inside that group.\n\n"

            "I will then show you the Group ID."
        )

        return

    await update.message.reply_text(
        "🆔 GROUP ID\n\n"

        f"`{chat.id}`\n\n"

        "Add this number to your Railway "
        "ALLOWED_GROUP_IDS variable.",

        parse_mode="Markdown"
    )


# ==================================================
# CUSTOMER POSTCODE SUBMISSION
# ==================================================

async def handle_postcode(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user:

        return

    # Admin messages should not accidentally
    # be recorded as postcode submissions.

    if is_admin(user.id):

        sector = normalize_postcode_sector(
            update.message.text
        )

        if not sector:

            return

    allowed = await verify_customer_access(
        update,
        context
    )

    if not allowed:

        return

    sector = normalize_postcode_sector(
        update.message.text
    )

    if not sector:

        await update.message.reply_text(
            "❌ I couldn't recognise that postcode.\n\n"

            "Please try again.\n\n"

            "Examples:\n"
            "BD12 7\n"
            "BD7 4\n"
            "BD12 7AB"
        )

        return

    existing = customer_record(
        user.id
    )

    changing = context.user_data.get(
        "awaiting_postcode_change",
        False
    )

    # Existing customer cannot simply type
    # another postcode.

    if existing and not changing:

        await update.message.reply_text(
            "✅ You already have a postcode registered.\n\n"

            f"📍 Current area:\n"
            f"{existing['postcode_sector']}\n\n"

            "To prevent duplicate or accidental entries, "
            "you need to press Change Postcode first.",

            reply_markup=existing_customer_keyboard(),
        )

        return

    now = datetime.now(
        timezone.utc
    ).isoformat()

    conn = db()

    # --------------------------------------------------
    # CHANGE EXISTING POSTCODE
    # --------------------------------------------------

    if existing:

        old_sector = existing[
            "postcode_sector"
        ]

        if old_sector == sector:

            context.user_data[
                "awaiting_postcode_change"
            ] = False

            conn.close()

            await update.message.reply_text(
                f"✅ Your postcode is already {sector}.",
                reply_markup=existing_customer_keyboard(),
            )

            return

        conn.execute(
            """
            UPDATE postcode_submissions

            SET
                postcode_sector = ?,
                first_name = ?,
                username = ?,
                is_active = 1,
                updated_at = ?

            WHERE telegram_id = ?
            """,
            (
                sector,
                user.first_name,
                user.username,
                now,
                user.id,
            )
        )

        conn.commit()

        conn.close()

        context.user_data[
            "awaiting_postcode_change"
        ] = False

        await update.message.reply_text(
            "✅ POSTCODE UPDATED\n\n"

            f"{old_sector} → {sector}\n\n"

            "Your new collection area "
            "has been saved.",

            reply_markup=existing_customer_keyboard(),
        )

        return

    # --------------------------------------------------
    # NEW CUSTOMER
    # --------------------------------------------------

    conn.execute(
        """
        INSERT INTO postcode_submissions
        (
            telegram_id,
            postcode_sector,
            first_name,
            username,
            is_active,
            created_at,
            updated_at
        )

        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user.id,
            sector,
            user.first_name,
            user.username,
            1,
            now,
            now,
        )
    )

    conn.commit()

    conn.close()

    await update.message.reply_text(
        "✅ THANK YOU\n\n"

        f"📍 Your collection area:\n"
        f"{sector}\n\n"

        "Your postcode area has been registered.\n\n"

        "Only one postcode can be registered "
        "to your Telegram account.",

        reply_markup=existing_customer_keyboard(),
    )


# ==================================================
# CUSTOMER CALLBACK BUTTONS
# ==================================================

async def customer_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    user = update.effective_user

    if not user:

        return

    allowed = await verify_customer_access(
        update,
        context
    )

    if not allowed:

        return

    await query.answer()

    action = query.data

    existing = customer_record(
        user.id
    )

    # --------------------------------------------------
    # CHANGE POSTCODE
    # --------------------------------------------------

    if action == "customer_change_postcode":

        if not existing:

            await query.edit_message_text(
                "📍 Please send your postcode."
            )

            return

        context.user_data[
            "awaiting_postcode_change"
        ] = True

        await query.edit_message_text(
            "✏️ CHANGE POSTCODE\n\n"

            f"Current postcode area:\n"
            f"{existing['postcode_sector']}\n\n"

            "Please send your new postcode now.\n\n"

            "Example:\n"
            "BD12 7",

            reply_markup=cancel_change_keyboard(),
        )

        return

    # --------------------------------------------------
    # CANCEL CHANGE
    # --------------------------------------------------

    if action == "customer_cancel_change":

        context.user_data[
            "awaiting_postcode_change"
        ] = False

        if existing:

            await query.edit_message_text(
                "✅ No changes made.\n\n"

                f"📍 Your postcode area:\n"
                f"{existing['postcode_sector']}",

                reply_markup=existing_customer_keyboard(),
            )

        else:

            await query.edit_message_text(
                "✅ Change cancelled."
            )

        return

    # --------------------------------------------------
    # ASK TO REMOVE
    # --------------------------------------------------

    if action == "customer_remove_postcode":

        if not existing:

            await query.edit_message_text(
                "ℹ️ You do not currently have "
                "a postcode saved."
            )

            return

        await query.edit_message_text(
            "🗑 REMOVE POSTCODE\n\n"

            f"Are you sure you want to remove "
            f"{existing['postcode_sector']}?",

            reply_markup=confirm_remove_keyboard(),
        )

        return

    # --------------------------------------------------
    # CANCEL REMOVE
    # --------------------------------------------------

    if action == "customer_cancel_remove":

        if existing:

            await query.edit_message_text(
                "✅ No changes made.\n\n"

                f"📍 Your postcode area:\n"
                f"{existing['postcode_sector']}",

                reply_markup=existing_customer_keyboard(),
            )

        return

    # --------------------------------------------------
    # CONFIRM REMOVE
    # --------------------------------------------------

    if action == "customer_confirm_remove":

        conn = db()

        conn.execute(
            """
            DELETE FROM postcode_submissions
            WHERE telegram_id = ?
            """,
            (user.id,)
        )

        conn.commit()

        conn.close()

        context.user_data[
            "awaiting_postcode_change"
        ] = False

        await query.edit_message_text(
            "✅ Your postcode area has been removed.\n\n"

            "If you want to register again later, "
            "send /start."
        )

        return


# ==================================================
# OLD /REMOVE COMMAND
# ==================================================

async def remove_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user:

        return

    allowed = await verify_customer_access(
        update,
        context
    )

    if not allowed:

        return

    existing = customer_record(
        user.id
    )

    if not existing:

        await update.message.reply_text(
            "ℹ️ You do not currently "
            "have a postcode saved."
        )

        return

    await update.message.reply_text(
        "🗑 REMOVE POSTCODE\n\n"

        f"Are you sure you want to remove "
        f"{existing['postcode_sector']}?",

        reply_markup=confirm_remove_keyboard(),
    )


# ==================================================
# ADMIN MENU
# ==================================================

async def admin_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user or not is_admin(user.id):

        return

    rows, active, inactive, total = get_counts()

    await update.message.reply_text(
        "🔐 ADMIN DASHBOARD\n\n"

        f"✅ Active customers: {active}\n"
        f"⛔ Inactive customers: {inactive}\n"
        f"👥 Total stored: {total}\n\n"

        "Choose an option below:",

        reply_markup=admin_keyboard()
    )


# ==================================================
# ADMIN COMMANDS
# ==================================================

async def counts_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(
        update.effective_user.id
    ):

        return

    await update.message.reply_text(
        make_counts_text(),
        reply_markup=back_keyboard()
    )


async def total_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(
        update.effective_user.id
    ):

        return

    await update.message.reply_text(
        make_customer_summary(),
        reply_markup=back_keyboard()
    )


async def busiest_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(
        update.effective_user.id
    ):

        return

    await update.message.reply_text(
        make_busiest_text(),
        reply_markup=back_keyboard()
    )


async def plan_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(
        update.effective_user.id
    ):

        return

    await update.message.reply_text(
        make_collection_plan_text(),
        reply_markup=back_keyboard()
    )


# ==================================================
# CSV EXPORT
# ==================================================

async def send_export(message):

    rows, active, inactive, total = get_counts()

    if not rows:

        await message.reply_text(
            "No active postcode areas "
            "have been submitted yet.",
            reply_markup=back_keyboard()
        )

        return

    output = io.StringIO()

    writer = csv.writer(
        output
    )

    writer.writerow(
        [
            "postcode_sector",
            "active_customer_count"
        ]
    )

    for row in rows:

        writer.writerow(
            [
                row["postcode_sector"],
                row["customer_count"]
            ]
        )

    data = io.BytesIO(
        output.getvalue().encode(
            "utf-8"
        )
    )

    data.name = (
        "active_postcode_counts.csv"
    )

    await message.reply_document(
        document=InputFile(
            data,
            filename="active_postcode_counts.csv"
        ),

        caption=(
            "📄 Active postcode collection counts\n\n"
            f"👥 Active customers: {active}"
        )
    )


async def export_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_admin(
        update.effective_user.id
    ):

        return

    await send_export(
        update.message
    )


# ==================================================
# REFRESH CUSTOMER GROUP ACCESS
# ==================================================

async def refresh_customer_access(
    context
):

    conn = db()

    customers = conn.execute(
        """
        SELECT telegram_id
        FROM postcode_submissions
        """
    ).fetchall()

    conn.close()

    active_count = 0

    inactive_count = 0

    for customer in customers:

        telegram_id = customer[
            "telegram_id"
        ]

        allowed = await is_member_of_allowed_group(
            context,
            telegram_id
        )

        set_customer_active(
            telegram_id,
            allowed
        )

        if allowed:

            active_count += 1

        else:

            inactive_count += 1

    return (
        active_count,
        inactive_count
    )


# ==================================================
# ADMIN CALLBACK BUTTONS
# ==================================================

async def admin_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    user = update.effective_user

    if not user or not is_admin(user.id):

        await query.answer(
            "Admin access only.",
            show_alert=True
        )

        return

    await query.answer()

    action = query.data

    # --------------------------------------------------
    # MAIN MENU
    # --------------------------------------------------

    if action == "admin_menu":

        rows, active, inactive, total = get_counts()

        await query.edit_message_text(
            "🔐 ADMIN DASHBOARD\n\n"

            f"✅ Active customers: {active}\n"
            f"⛔ Inactive customers: {inactive}\n"
            f"👥 Total stored: {total}\n\n"

            "Choose an option below:",

            reply_markup=admin_keyboard()
        )

        return

    # --------------------------------------------------
    # COUNTS
    # --------------------------------------------------

    if action == "admin_counts":

        text = make_counts_text()

        if len(text) <= 3900:

            await query.edit_message_text(
                text,
                reply_markup=back_keyboard()
            )

        else:

            await query.edit_message_text(
                "📊 The postcode list is long, "
                "so I've sent it below."
            )

            for start in range(
                0,
                len(text),
                3900
            ):

                chunk = text[
                    start:start + 3900
                ]

                await query.message.reply_text(
                    chunk
                )

            await query.message.reply_text(
                "Return to the dashboard:",
                reply_markup=back_keyboard()
            )

        return

    # --------------------------------------------------
    # CUSTOMER SUMMARY
    # --------------------------------------------------

    if action == "admin_total":

        await query.edit_message_text(
            make_customer_summary(),
            reply_markup=back_keyboard()
        )

        return

    # --------------------------------------------------
    # BUSIEST
    # --------------------------------------------------

    if action == "admin_busiest":

        await query.edit_message_text(
            make_busiest_text(),
            reply_markup=back_keyboard()
        )

        return

    # --------------------------------------------------
    # PLAN
    # --------------------------------------------------

    if action == "admin_plan":

        text = make_collection_plan_text()

        if len(text) <= 3900:

            await query.edit_message_text(
                text,
                reply_markup=back_keyboard()
            )

        else:

            await query.edit_message_text(
                "🗺 The collection plan is long, "
                "so I've sent it below."
            )

            for start in range(
                0,
                len(text),
                3900
            ):

                chunk = text[
                    start:start + 3900
                ]

                await query.message.reply_text(
                    chunk
                )

            await query.message.reply_text(
                "Return to the dashboard:",
                reply_markup=back_keyboard()
            )

        return

    # --------------------------------------------------
    # EXPORT
    # --------------------------------------------------

    if action == "admin_export":

        await send_export(
            query.message
        )

        return

    # --------------------------------------------------
    # REFRESH GROUP ACCESS
    # --------------------------------------------------

    if action == "admin_refresh_access":

        await query.edit_message_text(
            "🔄 CHECKING CUSTOMER ACCESS...\n\n"

            "I'm checking registered customers "
            "against your approved Telegram groups.\n\n"

            "Please wait."
        )

        active, inactive = (
            await refresh_customer_access(
                context
            )
        )

        await query.edit_message_text(
            "✅ ACCESS CHECK COMPLETE\n\n"

            f"✅ Active customers: {active}\n"
            f"⛔ Inactive customers: {inactive}\n\n"

            "Inactive customers will no longer "
            "be included in collection counts.",

            reply_markup=back_keyboard()
        )

        return


# ==================================================
# TELEGRAM COMMAND MENU
# ==================================================

async def post_init(
    application
):

    await application.bot.set_my_commands(
        [
            BotCommand(
                "start",
                "Postcode registration"
            ),

            BotCommand(
                "help",
                "Help and instructions"
            ),

            BotCommand(
                "remove",
                "Remove your postcode"
            ),

            BotCommand(
                "admin",
                "Admin dashboard"
            ),

            BotCommand(
                "groupid",
                "Show Telegram Group ID"
            ),
        ]
    )


# ==================================================
# START BOT
# ==================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN is missing. "
            "Add it in Railway Variables."
        )

    init_db()

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Customer commands

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        CommandHandler(
            "help",
            help_command
        )
    )

    app.add_handler(
        CommandHandler(
            "remove",
            remove_command
        )
    )

    # Admin commands

    app.add_handler(
        CommandHandler(
            "admin",
            admin_command
        )
    )

    app.add_handler(
        CommandHandler(
            "groupid",
            group_id_command
        )
    )

    app.add_handler(
        CommandHandler(
            "counts",
            counts_command
        )
    )

    app.add_handler(
        CommandHandler(
            "areas",
            counts_command
        )
    )

    app.add_handler(
        CommandHandler(
            "total",
            total_command
        )
    )

    app.add_handler(
        CommandHandler(
            "busiest",
            busiest_command
        )
    )

    app.add_handler(
        CommandHandler(
            "plan",
            plan_command
        )
    )

    app.add_handler(
        CommandHandler(
            "export",
            export_command
        )
    )

    # Customer buttons

    app.add_handler(
        CallbackQueryHandler(
            customer_callback,
            pattern=r"^customer_"
        )
    )

    # Admin buttons

    app.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin_"
        )
    )

    # Postcode messages

    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND
            & filters.ChatType.PRIVATE,
            handle_postcode
        )
    )

    print(
        "Postcode Collection Bot V3 is running..."
    )

    app.run_polling()


if __name__ == "__main__":

    main()
