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
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

DB_PATH = os.getenv("DB_PATH", "postcode_bot.db")


# --------------------------------------------------
# POSTCODE VALIDATION
# --------------------------------------------------

# Accepts:
# BD12 7
# BD7 4
# BD12 7AB
# bd7 4xy
#
# Only stores the postcode sector:
# BD12 7

POSTCODE_RE = re.compile(
    r"^\s*([A-Z]{1,2}\d[A-Z\d]?)\s*(\d)(?:\s*[A-Z]{2})?\s*$",
    re.IGNORECASE,
)


# --------------------------------------------------
# DATABASE
# --------------------------------------------------

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
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    conn.commit()
    conn.close()


# --------------------------------------------------
# HELPERS
# --------------------------------------------------

def normalize_postcode_sector(text):
    text = (text or "").strip().upper()

    match = POSTCODE_RE.match(text)

    if not match:
        return None

    outward = match.group(1).upper()
    sector_digit = match.group(2)

    return f"{outward} {sector_digit}"


def postcode_district(sector):
    # BD12 7 becomes BD12
    # BD7 4 becomes BD7
    return sector.split()[0]


def is_admin(user_id):
    return user_id in ADMIN_IDS


# --------------------------------------------------
# ADMIN BUTTONS
# --------------------------------------------------

def admin_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📊 Postcode Counts",
                    callback_data="admin_counts"
                ),
                InlineKeyboardButton(
                    "👥 Total Customers",
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
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔄 Refresh Dashboard",
                    callback_data="admin_menu"
                ),
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


# --------------------------------------------------
# DATABASE REPORTS
# --------------------------------------------------

def get_counts():

    conn = db()

    rows = conn.execute(
        """
        SELECT postcode_sector, COUNT(*) AS customer_count
        FROM postcode_submissions
        GROUP BY postcode_sector
        ORDER BY customer_count DESC, postcode_sector ASC
        """
    ).fetchall()

    total = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM postcode_submissions
        """
    ).fetchone()["total"]

    conn.close()

    return rows, total


def make_counts_text():

    rows, total = get_counts()

    if not rows:
        return (
            "📊 POSTCODE COUNTS\n\n"
            "No postcode areas have been submitted yet."
        )

    lines = [
        "📊 POSTCODE COUNTS",
        "",
        f"👥 Total customers: {total}",
        "",
    ]

    for row in rows:
        lines.append(
            f"📍 {row['postcode_sector']} — "
            f"{row['customer_count']}"
        )

    return "\n".join(lines)


def make_busiest_text():

    rows, total = get_counts()

    if not rows:
        return (
            "🔥 BUSIEST AREAS\n\n"
            "No postcode areas have been submitted yet."
        )

    lines = [
        "🔥 BUSIEST AREAS",
        "",
        f"👥 Total customers: {total}",
        "",
        "Top postcode sectors:",
        "",
    ]

    for number, row in enumerate(rows[:10], start=1):

        count = row["customer_count"]

        customer_word = (
            "customer"
            if count == 1
            else "customers"
        )

        lines.append(
            f"{number}. {row['postcode_sector']} "
            f"— {count} {customer_word}"
        )

    return "\n".join(lines)


def make_collection_plan_text():

    rows, total = get_counts()

    if not rows:
        return (
            "🗺 COLLECTION PLANNING\n\n"
            "No postcode areas have been submitted yet."
        )

    grouped = {}

    for row in rows:

        sector = row["postcode_sector"]
        count = row["customer_count"]

        district = postcode_district(sector)

        if district not in grouped:

            grouped[district] = {
                "total": 0,
                "sectors": [],
            }

        grouped[district]["total"] += count

        grouped[district]["sectors"].append(
            (sector, count)
        )


    ordered = sorted(
        grouped.items(),
        key=lambda item: (
            -item[1]["total"],
            item[0]
        ),
    )


    lines = [
        "🗺 COLLECTION PLANNING",
        "",
        f"👥 Total customers: {total}",
        "",
        "Postcode sectors are grouped together "
        "by postcode district.",
        "",
    ]


    for district, info in ordered:

        district_total = info["total"]

        customer_word = (
            "customer"
            if district_total == 1
            else "customers"
        )

        lines.append(
            f"📌 {district} — "
            f"{district_total} {customer_word}"
        )


        sectors = sorted(
            info["sectors"],
            key=lambda item: (
                -item[1],
                item[0]
            ),
        )


        for sector, count in sectors:

            lines.append(
                f"   • {sector} — {count}"
            )

        lines.append("")


    return "\n".join(lines).strip()


# --------------------------------------------------
# START
# --------------------------------------------------

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    text = (
        "📍 Please send your postcode.\n\n"

        "You can send either your full postcode "
        "or just the first part and number.\n\n"

        "Examples:\n"
        "BD12 7\n"
        "BD7 4\n"
        "BD12 7AB\n\n"

        "🔒 For privacy, the bot only stores "
        "the postcode sector.\n\n"

        "For example:\n"
        "BD12 7"
    )


    if user and is_admin(user.id):

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

    else:

        await update.message.reply_text(text)


# --------------------------------------------------
# HELP
# --------------------------------------------------

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    text = (
        "📍 Simply send your postcode "
        "as a normal message.\n\n"

        "Examples:\n"
        "BD12 7\n"
        "BD7 4\n"
        "BD12 7AB\n\n"

        "If you submit a new postcode later, "
        "your previous area will automatically "
        "be updated.\n\n"

        "Use /remove if you want your saved "
        "postcode area deleted."
    )


    if (
        update.effective_user
        and is_admin(update.effective_user.id)
    ):

        text += (
            "\n\n🔐 Use /admin to open "
            "the Admin Dashboard."
        )


    await update.message.reply_text(text)


# --------------------------------------------------
# CUSTOMER REMOVE POSTCODE
# --------------------------------------------------

async def remove(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    conn = db()

    cursor = conn.execute(
        """
        DELETE FROM postcode_submissions
        WHERE telegram_id = ?
        """,
        (user.id,),
    )

    conn.commit()

    removed = cursor.rowcount

    conn.close()


    if removed:

        await update.message.reply_text(
            "✅ Your postcode area has been removed."
        )

    else:

        await update.message.reply_text(
            "ℹ️ You do not currently have "
            "a postcode area saved."
        )


# --------------------------------------------------
# CUSTOMER POSTCODE SUBMISSION
# --------------------------------------------------

async def handle_postcode(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

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


    now = datetime.now(
        timezone.utc
    ).isoformat()


    conn = db()


    existing = conn.execute(
        """
        SELECT postcode_sector
        FROM postcode_submissions
        WHERE telegram_id = ?
        """,
        (user.id,),
    ).fetchone()


    # Customer already registered
    if existing:

        old_sector = existing["postcode_sector"]


        # Same postcode again
        if old_sector == sector:

            conn.close()

            await update.message.reply_text(
                f"✅ You're already registered "
                f"in area {sector}."
            )

            return


        # Update postcode
        conn.execute(
            """
            UPDATE postcode_submissions

            SET postcode_sector = ?,
                first_name = ?,
                username = ?,
                updated_at = ?

            WHERE telegram_id = ?
            """,
            (
                sector,
                user.first_name,
                user.username,
                now,
                user.id,
            ),
        )


        conn.commit()
        conn.close()


        await update.message.reply_text(
            f"✅ Thank you.\n\n"
            f"Your collection area has been updated "
            f"from {old_sector} to {sector}."
        )

        return


    # New customer
    conn.execute(
        """
        INSERT INTO postcode_submissions

        (
            telegram_id,
            postcode_sector,
            first_name,
            username,
            created_at,
            updated_at
        )

        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            user.id,
            sector,
            user.first_name,
            user.username,
            now,
            now,
        ),
    )


    conn.commit()
    conn.close()


    await update.message.reply_text(
        f"✅ Thank you.\n\n"
        f"Your collection area "
        f"{sector} has been registered."
    )


# --------------------------------------------------
# ADMIN DASHBOARD
# --------------------------------------------------

async def admin_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user


    if not user or not is_admin(user.id):
        return


    rows, total = get_counts()


    await update.message.reply_text(
        "🔐 ADMIN DASHBOARD\n\n"

        f"👥 Registered customers: {total}\n\n"

        "Choose an option below:",
        reply_markup=admin_keyboard(),
    )


# --------------------------------------------------
# ADMIN COMMANDS
# --------------------------------------------------

async def counts(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user or not is_admin(user.id):
        return


    text = make_counts_text()


    await update.message.reply_text(
        text,
        reply_markup=back_keyboard(),
    )


async def total(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user or not is_admin(user.id):
        return


    rows, customer_total = get_counts()


    await update.message.reply_text(
        "👥 TOTAL CUSTOMERS\n\n"
        f"Registered customers: {customer_total}",
        reply_markup=back_keyboard(),
    )


async def busiest(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user or not is_admin(user.id):
        return


    await update.message.reply_text(
        make_busiest_text(),
        reply_markup=back_keyboard(),
    )


async def plan(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user or not is_admin(user.id):
        return


    await update.message.reply_text(
        make_collection_plan_text(),
        reply_markup=back_keyboard(),
    )


# --------------------------------------------------
# CSV EXPORT
# --------------------------------------------------

async def send_export(message):

    rows, total = get_counts()


    if not rows:

        await message.reply_text(
            "No postcode areas have been "
            "submitted yet.",
            reply_markup=back_keyboard(),
        )

        return


    output = io.StringIO()

    writer = csv.writer(output)


    writer.writerow(
        [
            "postcode_sector",
            "customer_count"
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
        output.getvalue().encode("utf-8")
    )

    data.name = "postcode_area_counts.csv"


    await message.reply_document(
        document=InputFile(
            data,
            filename="postcode_area_counts.csv"
        ),

        caption=(
            "📄 Postcode collection counts\n\n"
            f"👥 Total customers: {total}"
        ),
    )


async def export_counts(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user or not is_admin(user.id):
        return


    await send_export(
        update.message
    )


# --------------------------------------------------
# ADMIN BUTTON CALLBACKS
# --------------------------------------------------

async def admin_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    user = update.effective_user


    if not user or not is_admin(user.id):

        await query.answer(
            "Admin access only.",
            show_alert=True,
        )

        return


    await query.answer()


    action = query.data


    # ADMIN MAIN MENU
    if action == "admin_menu":

        rows, total = get_counts()

        await query.edit_message_text(
            "🔐 ADMIN DASHBOARD\n\n"

            f"👥 Registered customers: {total}\n\n"

            "Choose an option below:",

            reply_markup=admin_keyboard(),
        )

        return


    # COUNTS
    if action == "admin_counts":

        await query.edit_message_text(
            make_counts_text(),
            reply_markup=back_keyboard(),
        )

        return


    # TOTAL CUSTOMERS
    if action == "admin_total":

        rows, total = get_counts()

        await query.edit_message_text(
            "👥 TOTAL CUSTOMERS\n\n"

            f"Registered customers: {total}",

            reply_markup=back_keyboard(),
        )

        return


    # BUSIEST AREAS
    if action == "admin_busiest":

        await query.edit_message_text(
            make_busiest_text(),
            reply_markup=back_keyboard(),
        )

        return


    # COLLECTION PLANNING
    if action == "admin_plan":

        await query.edit_message_text(
            make_collection_plan_text(),
            reply_markup=back_keyboard(),
        )

        return


    # EXPORT CSV
    if action == "admin_export":

        await send_export(
            query.message
        )

        return


# --------------------------------------------------
# TELEGRAM COMMAND MENU
# --------------------------------------------------

async def post_init(application):

    await application.bot.set_my_commands(
        [
            BotCommand(
                "start",
                "Submit your postcode"
            ),

            BotCommand(
                "help",
                "Help and instructions"
            ),

            BotCommand(
                "remove",
                "Remove your saved postcode"
            ),

            BotCommand(
                "admin",
                "Open admin dashboard"
            ),
        ]
    )


# --------------------------------------------------
# START BOT
# --------------------------------------------------

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN is missing. "
            "Add it as a Railway environment variable."
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
            remove
        )
    )


    # Admin dashboard
    app.add_handler(
        CommandHandler(
            "admin",
            admin_command
        )
    )


    # Admin commands
    app.add_handler(
        CommandHandler(
            "counts",
            counts
        )
    )

    app.add_handler(
        CommandHandler(
            "areas",
            counts
        )
    )

    app.add_handler(
        CommandHandler(
            "total",
            total
        )
    )

    app.add_handler(
        CommandHandler(
            "busiest",
            busiest
        )
    )

    app.add_handler(
        CommandHandler(
            "plan",
            plan
        )
    )

    app.add_handler(
        CommandHandler(
            "export",
            export_counts
        )
    )


    # Admin buttons
    app.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin_"
        )
    )


    # Customer postcode messages
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_postcode
        )
    )


    print(
        "Postcode Collection Bot is running..."
    )


    app.run_polling()


if __name__ == "__main__":
    main()
