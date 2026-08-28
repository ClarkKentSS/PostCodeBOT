import os
import re
import csv
import io
import sqlite3
from datetime import datetime, timezone

from telegram import Update, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
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

# Accepts:
# BD12 7
# BD7 4
# BD12 7AB
# bd7 4xy
#
# Stores only the postcode sector, e.g. BD12 7.
POSTCODE_RE = re.compile(
    r"^\s*([A-Z]{1,2}\d[A-Z\d]?)\s*(\d)(?:\s*[A-Z]{2})?\s*$",
    re.IGNORECASE,
)


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


def normalize_postcode_sector(text: str):
    text = (text or "").strip().upper()
    match = POSTCODE_RE.match(text)
    if not match:
        return None

    outward = match.group(1).upper()
    sector_digit = match.group(2)
    return f"{outward} {sector_digit}"


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📍 Please send your postcode.\n\n"
        "You can send either your full postcode or just the first part and number.\n\n"
        "Examples:\n"
        "BD12 7\n"
        "BD7 4\n"
        "BD12 7AB\n\n"
        "For privacy, the bot only stores the postcode sector, for example BD12 7."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📍 Simply send your postcode as a normal message.\n\n"
        "Examples:\n"
        "BD12 7\n"
        "BD7 4\n"
        "BD12 7AB\n\n"
        "If you submit a new postcode later, your previous area will be updated."
    )

    if update.effective_user and is_admin(update.effective_user.id):
        text += (
            "\n\n🔐 Admin commands:\n"
            "/counts - show customer totals by postcode sector\n"
            "/export - export postcode-sector counts as CSV\n"
            "/total - show total number of registered customers"
        )

    await update.message.reply_text(text)


async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = db()
    cur = conn.execute(
        "DELETE FROM postcode_submissions WHERE telegram_id = ?",
        (user.id,),
    )
    conn.commit()
    removed = cur.rowcount
    conn.close()

    if removed:
        await update.message.reply_text("✅ Your postcode area has been removed.")
    else:
        await update.message.reply_text("ℹ️ You do not currently have a postcode area saved.")


async def handle_postcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    sector = normalize_postcode_sector(update.message.text)

    if not sector:
        await update.message.reply_text(
            "❌ I couldn't recognise that postcode.\n\n"
            "Please try again, for example:\n"
            "BD12 7\n"
            "BD7 4\n"
            "or a full postcode such as BD12 7AB."
        )
        return

    now = datetime.now(timezone.utc).isoformat()

    conn = db()
    existing = conn.execute(
        "SELECT postcode_sector FROM postcode_submissions WHERE telegram_id = ?",
        (user.id,),
    ).fetchone()

    if existing:
        if existing["postcode_sector"] == sector:
            conn.close()
            await update.message.reply_text(
                f"✅ You're already registered in area {sector}."
            )
            return

        conn.execute(
            """
            UPDATE postcode_submissions
            SET postcode_sector = ?, first_name = ?, username = ?, updated_at = ?
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
            f"✅ Thank you. Your collection area has been updated to {sector}."
        )
        return

    conn.execute(
        """
        INSERT INTO postcode_submissions
        (telegram_id, postcode_sector, first_name, username, created_at, updated_at)
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
        f"✅ Thank you. Your collection area {sector} has been registered."
    )


async def counts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return

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
        "SELECT COUNT(*) AS total FROM postcode_submissions"
    ).fetchone()["total"]
    conn.close()

    if not rows:
        await update.message.reply_text("No postcode areas have been submitted yet.")
        return

    lines = [f"📊 POSTCODE AREA COUNTS\n\nTotal customers: {total}\n"]

    for row in rows:
        lines.append(f"{row['postcode_sector']} — {row['customer_count']}")

    # Telegram messages have a maximum length, so split long reports safely.
    message = "\n".join(lines)
    chunk_size = 3900

    for i in range(0, len(message), chunk_size):
        await update.message.reply_text(message[i:i + chunk_size])


async def total(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return

    conn = db()
    count = conn.execute(
        "SELECT COUNT(*) AS total FROM postcode_submissions"
    ).fetchone()["total"]
    conn.close()

    await update.message.reply_text(f"👥 Total registered customers: {count}")


async def export_counts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return

    conn = db()
    rows = conn.execute(
        """
        SELECT postcode_sector, COUNT(*) AS customer_count
        FROM postcode_submissions
        GROUP BY postcode_sector
        ORDER BY customer_count DESC, postcode_sector ASC
        """
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No postcode areas have been submitted yet.")
        return

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["postcode_sector", "customer_count"])

    for row in rows:
        writer.writerow([row["postcode_sector"], row["customer_count"]])

    data = io.BytesIO(output.getvalue().encode("utf-8"))
    data.name = "postcode_area_counts.csv"

    await update.message.reply_document(
        document=InputFile(data, filename="postcode_area_counts.csv"),
        caption="📄 Postcode collection counts",
    )


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing. Add it as an environment variable."
        )

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("remove", remove))
    app.add_handler(CommandHandler("counts", counts))
    app.add_handler(CommandHandler("areas", counts))
    app.add_handler(CommandHandler("total", total))
    app.add_handler(CommandHandler("export", export_counts))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_postcode,
        )
    )

    print("Postcode collection bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
