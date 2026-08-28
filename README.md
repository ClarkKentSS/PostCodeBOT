# Postcode Collection Bot

A simple Telegram bot that lets customers submit their postcode area/sector.

Examples:
- BD12 7
- BD7 4
- BD12 7AB

If a full postcode is entered, the bot only stores the postcode sector, for example:
BD12 7

## Features

- One postcode area per Telegram user
- Customers can update their postcode
- Duplicate protection
- Admin postcode-area counts
- Total customer count
- CSV export
- Customer self-removal

## Admin Commands

/counts
Shows the number of customers in each postcode sector.

/areas
Same as /counts.

/total
Shows the total number of registered customers.

/export
Exports postcode-sector counts as a CSV file.

## Customer Commands

/start
Starts the bot.

/help
Shows instructions.

/remove
Deletes the customer's saved postcode area.

## Environment Variables

The bot needs these environment variables:

BOT_TOKEN
Your Telegram bot token from BotFather.

ADMIN_IDS
Your Telegram user ID.

Example:
ADMIN_IDS=123456789

You can add multiple admins separated by commas:
ADMIN_IDS=123456789,987654321

DB_PATH
Optional. Defaults to:
postcode_bot.db

## Railway Setup

1. Create a new GitHub repository.
2. Upload the files from this folder.
3. Connect the repository to Railway.
4. Add the following Railway variables:
   - BOT_TOKEN
   - ADMIN_IDS
5. Railway should detect Python automatically.
6. Set the start command to:

python bot.py

## Local Setup

Install dependencies:

pip install -r requirements.txt

Then set your environment variables and run:

python bot.py

## Database

The bot uses SQLite.

By default the database file is:

postcode_bot.db

For Railway, use a persistent volume if you want the postcode data to survive redeployments.

A recommended DB_PATH when using a Railway volume is:

/data/postcode_bot.db

## Privacy

The bot only stores:
- Telegram user ID
- First name
- Telegram username, if available
- Postcode sector such as BD12 7
- Submission/update timestamps

It does not need to store a full postcode.
