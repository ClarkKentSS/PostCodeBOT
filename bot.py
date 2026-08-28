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

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS collection_rounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            district TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'planned',
            customer_count INTEGER NOT NULL DEFAULT 0,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS collection_round_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id INTEGER NOT NULL,
            telegram_id INTEGER NOT NULL,
            postcode_sector TEXT NOT NULL,
            first_name TEXT,
            username TEXT,
            FOREIGN KEY (round_id)
                REFERENCES collection_rounds(id)
                ON DELETE CASCADE
        )
        """
    )

    conn.commit()
    conn.close()


# ==================================================
# BASIC HELPERS
# ==================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def pretty_date(value):
    if not value:
        return "â"

    try:
        dt = datetime.fromisoformat(value)
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return value


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
        (1 if active else 0, user_id),
    )

    conn.commit()
    conn.close()


# ==================================================
# GROUP MEMBERSHIP
# ==================================================

async def is_member_of_allowed_group(context, user_id):
    if is_admin(user_id):
        return True

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

            if (
                status == ChatMemberStatus.RESTRICTED
                and getattr(member, "is_member", False)
            ):
                return True

        except TelegramError:
            continue

    return False


async def verify_customer_access(update, context):
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
            "ð CUSTOMER ACCESS ONLY\n\n"
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
                    "âï¸ Change Postcode",
                    callback_data="customer_change_postcode"
                )
            ],
            [
                InlineKeyboardButton(
                    "ð Remove My Postcode",
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
                    "â Yes, Remove It",
                    callback_data="customer_confirm_remove"
                ),
                InlineKeyboardButton(
                    "â Cancel",
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
                    "â Cancel Change",
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
                    "ð Postcode Counts",
                    callback_data="admin_counts"
                ),
                InlineKeyboardButton(
                    "ð¥ Customer Summary",
                    callback_data="admin_total"
                ),
            ],
            [
                InlineKeyboardButton(
                    "ð¥ Busiest Areas",
                    callback_data="admin_busiest"
                ),
                InlineKeyboardButton(
                    "ðº Collection Planning",
                    callback_data="admin_plan"
                ),
            ],
            [
                InlineKeyboardButton(
                    "ð Collection Rounds",
                    callback_data="rounds_menu"
                )
            ],
            [
                InlineKeyboardButton(
                    "ð Export CSV",
                    callback_data="admin_export"
                )
            ],
            [
                InlineKeyboardButton(
                    "ð Refresh Customer Access",
                    callback_data="admin_refresh_access"
                )
            ],
            [
                InlineKeyboardButton(
                    "â»ï¸ Refresh Dashboard",
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
                    "â¬ï¸ Back to Admin Menu",
                    callback_data="admin_menu"
                )
            ]
        ]
    )


# ==================================================
# ADMIN REPORTS
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

    conn.close()

    return rows, active, inactive, active + inactive


def make_counts_text():
    rows, active, inactive, total = get_counts()

    if not rows:
        return (
            "ð POSTCODE COUNTS\n\n"
            "No active postcode submissions yet."
        )

    lines = [
        "ð ACTIVE POSTCODE COUNTS",
        "",
        f"ð¥ Active customers: {active}",
        "",
    ]

    for row in rows:
        lines.append(
            f"ð {row['postcode_sector']} â "
            f"{row['customer_count']}"
        )

    return "\n".join(lines)


def make_customer_summary():
    rows, active, inactive, total = get_counts()

    return (
        "ð¥ CUSTOMER SUMMARY\n\n"
        f"â Active customers: {active}\n"
        f"â Inactive customers: {inactive}\n"
        f"ð¥ Total stored customers: {total}\n\n"
        "Inactive customers are excluded from "
        "postcode counts and collection planning."
    )


def make_busiest_text():
    rows, active, inactive, total = get_counts()

    if not rows:
        return (
            "ð¥ BUSIEST AREAS\n\n"
            "No active postcode areas yet."
        )

    lines = [
        "ð¥ BUSIEST AREAS",
        "",
        f"ð¥ Active customers: {active}",
        "",
        "Top postcode sectors:",
        "",
    ]

    for number, row in enumerate(rows[:10], start=1):
        count = row["customer_count"]
        word = "customer" if count == 1 else "customers"

        lines.append(
            f"{number}. {row['postcode_sector']} "
            f"â {count} {word}"
        )

    return "\n".join(lines)


def make_collection_plan_text():
    rows, active, inactive, total = get_counts()

    if not rows:
        return (
            "ðº COLLECTION PLANNING\n\n"
            "No active postcode areas yet."
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
        )
    )

    lines = [
        "ðº COLLECTION PLANNING",
        "",
        f"ð¥ Active customers: {active}",
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
            f"ð {district} â "
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
                f"   â¢ {sector} â {count}"
            )

        lines.append("")

    return "\n".join(lines).strip()


# ==================================================
# COLLECTION ROUNDS - DATA
# ==================================================

def get_districts():
    conn = db()

    rows = conn.execute(
        """
        SELECT
            postcode_sector,
            COUNT(*) AS customer_count
        FROM postcode_submissions
        WHERE is_active = 1
        GROUP BY postcode_sector
        """
    ).fetchall()

    conn.close()

    grouped = {}

    for row in rows:
        district = postcode_district(
            row["postcode_sector"]
        )

        grouped[district] = (
            grouped.get(district, 0)
            + row["customer_count"]
        )

    return sorted(
        grouped.items(),
        key=lambda item: (
            -item[1],
            item[0]
        )
    )


def get_district_customers(district):
    conn = db()

    rows = conn.execute(
        """
        SELECT
            telegram_id,
            postcode_sector,
            first_name,
            username
        FROM postcode_submissions
        WHERE
            is_active = 1
            AND postcode_sector LIKE ?
        ORDER BY
            postcode_sector ASC,
            first_name ASC
        """,
        (f"{district} %",),
    ).fetchall()

    conn.close()
    return rows


def get_active_round(district=None):
    conn = db()

    if district:
        row = conn.execute(
            """
            SELECT *
            FROM collection_rounds
            WHERE
                status = 'planned'
                AND district = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (district,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT *
            FROM collection_rounds
            WHERE status = 'planned'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    conn.close()
    return row


def get_round(round_id):
    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM collection_rounds
        WHERE id = ?
        """,
        (round_id,),
    ).fetchone()

    conn.close()
    return row


def get_round_members(round_id):
    conn = db()

    rows = conn.execute(
        """
        SELECT *
        FROM collection_round_members
        WHERE round_id = ?
        ORDER BY
            postcode_sector ASC,
            first_name ASC
        """,
        (round_id,),
    ).fetchall()

    conn.close()
    return rows


def create_round(district, admin_id):
    existing = get_active_round(district)

    if existing:
        return existing["id"], False

    customers = get_district_customers(district)

    if not customers:
        return None, False

    conn = db()

    cursor = conn.execute(
        """
        INSERT INTO collection_rounds (
            district,
            status,
            customer_count,
            created_by,
            created_at
        )
        VALUES (?, 'planned', ?, ?, ?)
        """,
        (
            district,
            len(customers),
            admin_id,
            now_iso(),
        )
    )

    round_id = cursor.lastrowid

    for customer in customers:
        conn.execute(
            """
            INSERT INTO collection_round_members (
                round_id,
                telegram_id,
                postcode_sector,
                first_name,
                username
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                round_id,
                customer["telegram_id"],
                customer["postcode_sector"],
                customer["first_name"],
                customer["username"],
            )
        )

    conn.commit()
    conn.close()

    return round_id, True


def complete_round(round_id):
    conn = db()

    conn.execute(
        """
        UPDATE collection_rounds
        SET
            status = 'completed',
            completed_at = ?
        WHERE
            id = ?
            AND status = 'planned'
        """,
        (
            now_iso(),
            round_id,
        )
    )

    conn.commit()
    conn.close()


def get_round_history(limit=15):
    conn = db()

    rows = conn.execute(
        """
        SELECT *
        FROM collection_rounds
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    conn.close()
    return rows


def round_customer_name(member):
    name = member["first_name"] or "Customer"
    username = member["username"]

    if username:
        return f"{name} (@{username})"

    return name


def make_district_details(district):
    customers = get_district_customers(district)

    if not customers:
        return (
            f"ð {district} COLLECTION AREA\n\n"
            "No active customers in this district."
        )

    grouped = {}

    for customer in customers:
        sector = customer["postcode_sector"]

        grouped.setdefault(
            sector,
            []
        ).append(customer)

    lines = [
        f"ð {district} COLLECTION AREA",
        "",
        f"ð¥ Active customers: {len(customers)}",
        "",
    ]

    for sector in sorted(grouped):
        members = grouped[sector]

        lines.append(
            f"ð {sector} â {len(members)}"
        )

        for member in members:
            lines.append(
                f"   â¢ {round_customer_name(member)}"
            )

        lines.append("")

    active_round = get_active_round(district)

    if active_round:
        lines.append(
            f"ð¡ Planned round already exists: "
            f"Round #{active_round['id']}"
        )

    return "\n".join(lines).strip()


def make_round_details(round_id):
    round_row = get_round(round_id)

    if not round_row:
        return "â Collection round not found."

    members = get_round_members(round_id)

    status_text = (
        "ð¡ PLANNED"
        if round_row["status"] == "planned"
        else "â COMPLETED"
    )

    lines = [
        f"ð COLLECTION ROUND #{round_row['id']}",
        "",
        f"ð District: {round_row['district']}",
        f"ð Status: {status_text}",
        f"ð¥ Customers: {round_row['customer_count']}",
        f"ð Planned: {pretty_date(round_row['created_at'])}",
    ]

    if round_row["completed_at"]:
        lines.append(
            f"â Completed: "
            f"{pretty_date(round_row['completed_at'])}"
        )

    lines.extend(
        [
            "",
            "Customers in this round:",
            "",
        ]
    )

    for member in members:
        lines.append(
            f"ð {member['postcode_sector']} â "
            f"{round_customer_name(member)}"
        )

    return "\n".join(lines)


# ==================================================
# COLLECTION ROUND BUTTONS
# ==================================================

def rounds_menu_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "â Plan New Round",
                    callback_data="rounds_choose_district"
                )
            ],
            [
                InlineKeyboardButton(
                    "ð¡ Planned Rounds",
                    callback_data="rounds_active"
                ),
                InlineKeyboardButton(
                    "ð Round History",
                    callback_data="rounds_history"
                ),
            ],
            [
                InlineKeyboardButton(
                    "â¬ï¸ Back to Admin Menu",
                    callback_data="admin_menu"
                )
            ],
        ]
    )


def districts_keyboard():
    districts = get_districts()

    rows = []

    for district, count in districts[:30]:
        rows.append(
            [
                InlineKeyboardButton(
                    f"ð {district} â {count}",
                    callback_data=f"round_district:{district}"
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "â¬ï¸ Back",
                callback_data="rounds_menu"
            )
        ]
    )

    return InlineKeyboardMarkup(rows)


def district_actions_keyboard(district):
    active_round = get_active_round(district)

    rows = []

    if active_round:
        rows.append(
            [
                InlineKeyboardButton(
                    f"ð¡ Open Round #{active_round['id']}",
                    callback_data=f"round_view:{active_round['id']}"
                )
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    "â Plan This Round",
                    callback_data=f"round_create:{district}"
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "â¬ï¸ Choose Another Area",
                callback_data="rounds_choose_district"
            )
        ]
    )

    return InlineKeyboardMarkup(rows)


def round_view_keyboard(round_id, status):
    rows = []

    if status == "planned":
        rows.append(
            [
                InlineKeyboardButton(
                    "â Mark Round Completed",
                    callback_data=f"round_complete_confirm:{round_id}"
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "â¬ï¸ Collection Rounds",
                callback_data="rounds_menu"
            )
        ]
    )

    return InlineKeyboardMarkup(rows)


def round_complete_confirm_keyboard(round_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "â Yes, Complete Round",
                    callback_data=f"round_complete:{round_id}"
                ),
                InlineKeyboardButton(
                    "â Cancel",
                    callback_data=f"round_view:{round_id}"
                ),
            ]
        ]
    )


def history_keyboard():
    rounds = get_round_history(15)

    rows = []

    for row in rounds:
        icon = (
            "ð¡"
            if row["status"] == "planned"
            else "â"
        )

        rows.append(
            [
                InlineKeyboardButton(
                    f"{icon} #{row['id']} {row['district']} â "
                    f"{row['customer_count']}",
                    callback_data=f"round_view:{row['id']}"
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "â¬ï¸ Back",
                callback_data="rounds_menu"
            )
        ]
    )

    return InlineKeyboardMarkup(rows)


def planned_rounds_keyboard():
    conn = db()

    rounds = conn.execute(
        """
        SELECT *
        FROM collection_rounds
        WHERE status = 'planned'
        ORDER BY id DESC
        LIMIT 20
        """
    ).fetchall()

    conn.close()

    rows = []

    for row in rounds:
        rows.append(
            [
                InlineKeyboardButton(
                    f"ð¡ #{row['id']} {row['district']} â "
                    f"{row['customer_count']} customers",
                    callback_data=f"round_view:{row['id']}"
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "â¬ï¸ Back",
                callback_data="rounds_menu"
            )
        ]
    )

    return InlineKeyboardMarkup(rows)


# ==================================================
# START / HELP
# ==================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not user:
        return

    if is_admin(user.id):
        text = (
            "ð POSTCODE COLLECTION BOT\n\n"
            "Customers simply send their postcode "
            "and the bot stores only their postcode sector.\n\n"
            "Use the Admin Dashboard below to manage "
            "customers and collection rounds."
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "ð Admin Dashboard",
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
            "â YOU'RE ALREADY REGISTERED\n\n"
            f"ð Your postcode area:\n"
            f"{existing['postcode_sector']}\n\n"
            "Only one postcode can be registered "
            "to each Telegram account.\n\n"
            "If you have moved or need to correct it, "
            "use the Change Postcode button below.",
            reply_markup=existing_customer_keyboard(),
        )

        return

    await update.message.reply_text(
        "ð PLEASE SEND YOUR POSTCODE\n\n"
        "You can send either your full postcode "
        "or just the postcode sector.\n\n"
        "Examples:\n"
        "BD12 7\n"
        "BD7 4\n"
        "BD12 7AB\n\n"
        "ð For privacy, only the postcode sector "
        "is stored.\n\n"
        "For example:\n"
        "BD12 7AB â BD12 7"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not user:
        return

    if is_admin(user.id):
        await update.message.reply_text(
            "ð ADMIN HELP\n\n"
            "/admin - Open admin dashboard\n"
            "/groupid - Show the current group's ID\n"
            "/counts - Active postcode counts\n"
            "/total - Customer summary\n"
            "/busiest - Busiest postcode areas\n"
            "/plan - Collection planning\n"
            "/rounds - Collection rounds\n"
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
        "ð POSTCODE HELP\n\n"
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
# GROUP ID
# ==================================================

async def group_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    if not user or not is_admin(user.id):
        return

    if not chat:
        return

    if chat.type == "private":
        await update.message.reply_text(
            "â¹ï¸ To get a customer Group ID:\n\n"
            "1. Add this bot to the customer group.\n"
            "2. Make the bot an admin.\n"
            "3. Send /groupid inside that group.\n\n"
            "I will then show you the Group ID."
        )
        return

    await update.message.reply_text(
        "ð GROUP ID\n\n"
        f"`{chat.id}`\n\n"
        "Add this number to your Railway "
        "ALLOWED_GROUP_IDS variable.",
        parse_mode="Markdown"
    )


# ==================================================
# CUSTOMER POSTCODE SUBMISSION
# ==================================================

async def handle_postcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not user:
        return

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
            "â I couldn't recognise that postcode.\n\n"
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

    if existing and not changing:
        await update.message.reply_text(
            "â You already have a postcode registered.\n\n"
            f"ð Current area:\n"
            f"{existing['postcode_sector']}\n\n"
            "To prevent duplicate or accidental entries, "
            "you need to press Change Postcode first.",
            reply_markup=existing_customer_keyboard(),
        )
        return

    now = now_iso()
    conn = db()

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
                f"â Your postcode is already {sector}.",
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
            "â POSTCODE UPDATED\n\n"
            f"{old_sector} â {sector}\n\n"
            "Your new collection area "
            "has been saved.",
            reply_markup=existing_customer_keyboard(),
        )

        return

    conn.execute(
        """
        INSERT INTO postcode_submissions (
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
        "â THANK YOU\n\n"
        f"ð Your collection area:\n"
        f"{sector}\n\n"
        "Your postcode area has been registered.\n\n"
        "Only one postcode can be registered "
        "to your Telegram account.",
        reply_markup=existing_customer_keyboard(),
    )


# ==================================================
# CUSTOMER CALLBACKS
# ==================================================

async def customer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    if action == "customer_change_postcode":
        if not existing:
            await query.edit_message_text(
                "ð Please send your postcode."
            )
            return

        context.user_data[
            "awaiting_postcode_change"
        ] = True

        await query.edit_message_text(
            "âï¸ CHANGE POSTCODE\n\n"
            f"Current postcode area:\n"
            f"{existing['postcode_sector']}\n\n"
            "Please send your new postcode now.\n\n"
            "Example:\n"
            "BD12 7",
            reply_markup=cancel_change_keyboard(),
        )

        return

    if action == "customer_cancel_change":
        context.user_data[
            "awaiting_postcode_change"
        ] = False

        if existing:
            await query.edit_message_text(
                "â No changes made.\n\n"
                f"ð Your postcode area:\n"
                f"{existing['postcode_sector']}",
                reply_markup=existing_customer_keyboard(),
            )
        else:
            await query.edit_message_text(
                "â Change cancelled."
            )

        return

    if action == "customer_remove_postcode":
        if not existing:
            await query.edit_message_text(
                "â¹ï¸ You do not currently have "
                "a postcode saved."
            )
            return

        await query.edit_message_text(
            "ð REMOVE POSTCODE\n\n"
            f"Are you sure you want to remove "
            f"{existing['postcode_sector']}?",
            reply_markup=confirm_remove_keyboard(),
        )
        return

    if action == "customer_cancel_remove":
        if existing:
            await query.edit_message_text(
                "â No changes made.\n\n"
                f"ð Your postcode area:\n"
                f"{existing['postcode_sector']}",
                reply_markup=existing_customer_keyboard(),
            )
        return

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
            "â Your postcode area has been removed.\n\n"
            "If you want to register again later, "
            "send /start."
        )

        return


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            "â¹ï¸ You do not currently "
            "have a postcode saved."
        )
        return

    await update.message.reply_text(
        "ð REMOVE POSTCODE\n\n"
        f"Are you sure you want to remove "
        f"{existing['postcode_sector']}?",
        reply_markup=confirm_remove_keyboard(),
    )


# ==================================================
# ADMIN COMMANDS
# ==================================================

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not user or not is_admin(user.id):
        return

    rows, active, inactive, total = get_counts()

    await update.message.reply_text(
        "ð ADMIN DASHBOARD\n\n"
        f"â Active customers: {active}\n"
        f"â Inactive customers: {inactive}\n"
        f"ð¥ Total stored: {total}\n\n"
        "Choose an option below:",
        reply_markup=admin_keyboard()
    )


async def counts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(
        update.effective_user.id
    ):
        return

    await update.message.reply_text(
        make_counts_text(),
        reply_markup=back_keyboard()
    )


async def total_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(
        update.effective_user.id
    ):
        return

    await update.message.reply_text(
        make_customer_summary(),
        reply_markup=back_keyboard()
    )


async def busiest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(
        update.effective_user.id
    ):
        return

    await update.message.reply_text(
        make_busiest_text(),
        reply_markup=back_keyboard()
    )


async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(
        update.effective_user.id
    ):
        return

    await update.message.reply_text(
        make_collection_plan_text(),
        reply_markup=back_keyboard()
    )


async def rounds_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(
        update.effective_user.id
    ):
        return

    await update.message.reply_text(
        "ð COLLECTION ROUNDS\n\n"
        "Plan a collection area, save the customer "
        "list for that round, then mark it completed "
        "when the collection is finished.",
        reply_markup=rounds_menu_keyboard()
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
            "ð Active postcode collection counts\n\n"
            f"ð¥ Active customers: {active}"
        )
    )


async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(
        update.effective_user.id
    ):
        return

    await send_export(
        update.message
    )


# ==================================================
# REFRESH CUSTOMER ACCESS
# ==================================================

async def refresh_customer_access(context):
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
# ADMIN CALLBACKS
# ==================================================

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    if action == "admin_menu":
        rows, active, inactive, total = get_counts()

        await query.edit_message_text(
            "ð ADMIN DASHBOARD\n\n"
            f"â Active customers: {active}\n"
            f"â Inactive customers: {inactive}\n"
            f"ð¥ Total stored: {total}\n\n"
            "Choose an option below:",
            reply_markup=admin_keyboard()
        )
        return

    if action == "admin_counts":
        text = make_counts_text()

        if len(text) <= 3900:
            await query.edit_message_text(
                text,
                reply_markup=back_keyboard()
            )
        else:
            await query.edit_message_text(
                "ð The postcode list is long, "
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

    if action == "admin_total":
        await query.edit_message_text(
            make_customer_summary(),
            reply_markup=back_keyboard()
        )
        return

    if action == "admin_busiest":
        await query.edit_message_text(
            make_busiest_text(),
            reply_markup=back_keyboard()
        )
        return

    if action == "admin_plan":
        text = make_collection_plan_text()

        if len(text) <= 3900:
            await query.edit_message_text(
                text,
                reply_markup=back_keyboard()
            )
        else:
            await query.edit_message_text(
                "ðº The collection plan is long, "
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

    if action == "admin_export":
        await send_export(
            query.message
        )
        return

    if action == "admin_refresh_access":
        await query.edit_message_text(
            "ð CHECKING CUSTOMER ACCESS...\n\n"
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
            "â ACCESS CHECK COMPLETE\n\n"
            f"â Active customers: {active}\n"
            f"â Inactive customers: {inactive}\n\n"
            "Inactive customers will no longer "
            "be included in collection counts.",
            reply_markup=back_keyboard()
        )

        return


# ==================================================
# COLLECTION ROUND CALLBACKS
# ==================================================

async def rounds_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    if action == "rounds_menu":
        await query.edit_message_text(
            "ð COLLECTION ROUNDS\n\n"
            "Plan a new round, view planned rounds "
            "or look back at previous collection history.",
            reply_markup=rounds_menu_keyboard()
        )
        return

    if action == "rounds_choose_district":
        districts = get_districts()

        if not districts:
            await query.edit_message_text(
                "ð PLAN NEW ROUND\n\n"
                "There are currently no active "
                "postcode districts to collect from.",
                reply_markup=rounds_menu_keyboard()
            )
            return

        await query.edit_message_text(
            "ð PLAN NEW ROUND\n\n"
            "Choose the postcode district you want "
            "to collect from.\n\n"
            "The number beside each area is the "
            "current number of active customers.",
            reply_markup=districts_keyboard()
        )
        return

    if action.startswith("round_district:"):
        district = action.split(":", 1)[1]

        text = make_district_details(
            district
        )

        if len(text) <= 3900:
            await query.edit_message_text(
                text,
                reply_markup=district_actions_keyboard(
                    district
                )
            )
        else:
            await query.edit_message_text(
                f"ð {district} has a long customer list. "
                "I've sent the details below."
            )

            for start in range(
                0,
                len(text),
                3900
            ):
                await query.message.reply_text(
                    text[start:start + 3900]
                )

            await query.message.reply_text(
                "Choose what to do next:",
                reply_markup=district_actions_keyboard(
                    district
                )
            )

        return

    if action.startswith("round_create:"):
        district = action.split(":", 1)[1]

        round_id, created = create_round(
            district,
            user.id
        )

        if not round_id:
            await query.edit_message_text(
                f"â No active customers are currently "
                f"registered in {district}.",
                reply_markup=rounds_menu_keyboard()
            )
            return

        round_row = get_round(
            round_id
        )

        text = make_round_details(
            round_id
        )

        if created:
            heading = (
                "â COLLECTION ROUND PLANNED\n\n"
            )
        else:
            heading = (
                "â¹ï¸ A planned round already exists "
                "for this district.\n\n"
            )

        full_text = heading + text

        if len(full_text) <= 3900:
            await query.edit_message_text(
                full_text,
                reply_markup=round_view_keyboard(
                    round_id,
                    round_row["status"]
                )
            )
        else:
            await query.edit_message_text(
                heading.strip()
            )

            for start in range(
                0,
                len(text),
                3900
            ):
                await query.message.reply_text(
                    text[start:start + 3900]
                )

            await query.message.reply_text(
                "Round options:",
                reply_markup=round_view_keyboard(
                    round_id,
                    round_row["status"]
                )
            )

        return

    if action.startswith("round_view:"):
        round_id = int(
            action.split(":", 1)[1]
        )

        round_row = get_round(
            round_id
        )

        if not round_row:
            await query.edit_message_text(
                "â That collection round "
                "could not be found.",
                reply_markup=rounds_menu_keyboard()
            )
            return

        text = make_round_details(
            round_id
        )

        if len(text) <= 3900:
            await query.edit_message_text(
                text,
                reply_markup=round_view_keyboard(
                    round_id,
                    round_row["status"]
                )
            )
        else:
            await query.edit_message_text(
                f"ð Collection Round #{round_id}\n\n"
                "The customer list is long, so "
                "I've sent it below."
            )

            for start in range(
                0,
                len(text),
                3900
            ):
                await query.message.reply_text(
                    text[start:start + 3900]
                )

            await query.message.reply_text(
                "Round options:",
                reply_markup=round_view_keyboard(
                    round_id,
                    round_row["status"]
                )
            )

        return

    if action.startswith(
        "round_complete_confirm:"
    ):
        round_id = int(
            action.split(":", 1)[1]
        )

        round_row = get_round(
            round_id
        )

        if not round_row:
            await query.edit_message_text(
                "â Round not found.",
                reply_markup=rounds_menu_keyboard()
            )
            return

        await query.edit_message_text(
            "â COMPLETE COLLECTION ROUND?\n\n"
            f"Round #{round_id}\n"
            f"ð {round_row['district']}\n"
            f"ð¥ {round_row['customer_count']} customers\n\n"
            "This will move the round into "
            "your completed history.",
            reply_markup=round_complete_confirm_keyboard(
                round_id
            )
        )
        return

    if action.startswith("round_complete:"):
        round_id = int(
            action.split(":", 1)[1]
        )

        round_row = get_round(
            round_id
        )

        if not round_row:
            await query.edit_message_text(
                "â Round not found.",
                reply_markup=rounds_menu_keyboard()
            )
            return

        if round_row["status"] == "planned":
            complete_round(
                round_id
            )

        round_row = get_round(
            round_id
        )

        await query.edit_message_text(
            "â COLLECTION ROUND COMPLETED\n\n"
            f"Round #{round_id}\n"
            f"ð {round_row['district']}\n"
            f"ð¥ {round_row['customer_count']} customers\n"
            f"â Completed: "
            f"{pretty_date(round_row['completed_at'])}",
            reply_markup=round_view_keyboard(
                round_id,
                round_row["status"]
            )
        )
        return

    if action == "rounds_history":
        history = get_round_history(
            15
        )

        if not history:
            await query.edit_message_text(
                "ð ROUND HISTORY\n\n"
                "No collection rounds have been "
                "created yet.",
                reply_markup=rounds_menu_keyboard()
            )
            return

        await query.edit_message_text(
            "ð ROUND HISTORY\n\n"
            "Showing your latest collection rounds.\n\n"
            "ð¡ = Planned\n"
            "â = Completed",
            reply_markup=history_keyboard()
        )
        return

    if action == "rounds_active":
        active_rounds = []

        conn = db()

        active_rounds = conn.execute(
            """
            SELECT *
            FROM collection_rounds
            WHERE status = 'planned'
            ORDER BY id DESC
            LIMIT 20
            """
        ).fetchall()

        conn.close()

        if not active_rounds:
            await query.edit_message_text(
                "ð¡ PLANNED ROUNDS\n\n"
                "There are currently no planned "
                "collection rounds.",
                reply_markup=rounds_menu_keyboard()
            )
            return

        await query.edit_message_text(
            "ð¡ PLANNED ROUNDS\n\n"
            "Choose a round to view its customers "
            "or mark it completed.",
            reply_markup=planned_rounds_keyboard()
        )
        return


# ==================================================
# COMMAND MENU
# ==================================================

async def post_init(application):
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
                "rounds",
                "Collection rounds"
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

    app.add_handler(
        CommandHandler(
            "admin",
            admin_command
        )
    )

    app.add_handler(
        CommandHandler(
            "rounds",
            rounds_command
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

    app.add_handler(
        CallbackQueryHandler(
            customer_callback,
            pattern=r"^customer_"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            rounds_callback,
            pattern=r"^(rounds_|round_)"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin_"
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND
            & filters.ChatType.PRIVATE,
            handle_postcode
        )
    )

    print(
        "Postcode Collection Bot V4 is running..."
    )

    app.run_polling()


if __name__ == "__main__":
    main()
