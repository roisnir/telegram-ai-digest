
# Telegram AI Digest Generator

Python script that fetches messages from Telegram channels, summarizes them with Claude AI, and publishes the result as a Telegraph page (Hebrew, Instant View). The Telegraph URL is sent to a private Telegram channel.

## Features

- **Telegram API**: Fetches last 24h of messages from one or more channels via Telethon (user account).
- **Claude AI**: Classifies and summarizes stories into a structured Hebrew digest.
- **Telegraph**: Publishes the digest as a Telegraph page with Instant View support.

## Requirements

- Python 3.12+
- Docker (optional, for server deployment)
- A Telegram user account with API credentials

## Setup

### 1. Clone the repository

```bash
git clone <repo-url>
cd telegram-ai-digest
```

### 2. Create a `.env` file

```
API_ID=<your_telegram_api_id>
API_HASH=<your_telegram_api_hash>
PHONE_NUMBER=<your_phone_number>
CHANNEL_USERNAMES=channel_one,channel_two
TARGET_CHANNEL=-1001234567890
CLAUDE_API_KEY=<your_claude_api_key>
TELEGRAPH_TOKEN=<your_telegraph_token>   # see step 4
```

### 3. Authenticate Telegram (first run only)

Telethon requires interactive phone authentication on first run. Run the script locally once to generate the `session.session` file:

```bash
pip install -r requirements.txt
python digest.py
```

Enter the verification code when prompted. After this, `session.session` is saved and all future runs (including Docker) are non-interactive.

### 4. Get your Telegraph token (first run only)

On the first run without `TELEGRAPH_TOKEN` set, the script creates a Telegraph account and logs the token:

```
Telegraph account created. Add to .env: TELEGRAPH_TOKEN=<token>
```

Copy that value into your `.env` file. All subsequent runs (including on the server) will use it from there — no file to manage.

## How to Obtain API Tokens

### Telegram API Credentials

1. Go to [my.telegram.org](https://my.telegram.org/) and log in.
2. Navigate to "API development tools" and create a new application.
3. Copy `API_ID` and `API_HASH` into your `.env`.

### Claude AI API Key

1. Sign in to the [Anthropic Console](https://console.anthropic.com/).
2. Generate an API key and set it as `CLAUDE_API_KEY`.

## Docker Deployment

### Build the image

```bash
docker build -t telegram-ai-digest .
```

### Prepare the data directory on your server

Copy these files to a persistent directory on your server (e.g. `/opt/telegram-news-digest/`):

```
/opt/telegram-news-digest/
├── .env             # your environment variables (including TELEGRAPH_TOKEN)
└── session.session  # generated during first-run auth above
```

### Run manually (test)

```bash
docker run --rm \
  -v /opt/telegram-news-digest/.env:/app/.env:ro \
  -v /opt/telegram-news-digest/session.session:/app/session.session \
  telegram-ai-digest
```

## Scheduling with crontab

Create a wrapper script at `/opt/telegram-news-digest/run.sh`:

```bash
#!/bin/bash
docker run --rm \
  -v /opt/telegram-news-digest/.env:/app/.env:ro \
  -v /opt/telegram-news-digest/session.session:/app/session.session \
  telegram-ai-digest >> /var/log/digest.log 2>&1
```

Make it executable:

```bash
chmod +x /opt/telegram-news-digest/run.sh
```

Then add a single short line to your crontab (`crontab -e`):

```cron
0 7,19 * * * /opt/telegram-news-digest/run.sh
```

> **Note:** The times are in the server's local timezone. If your server runs UTC and you want 07:00 and 19:00 Israel time (UTC+3), use `0 4,16 * * *` instead (adjust for DST as needed).

To verify the crontab was saved:

```bash
crontab -l
```

To tail the logs:

```bash
tail -f /var/log/digest.log
```

## Usage

Run manually:

```bash
python digest.py
```

The script will:
1. Fetch messages from the configured Telegram channels (last 24h).
2. Classify and summarize them into a Hebrew digest using Claude AI.
3. Publish the digest to Telegraph and send the URL to the target Telegram channel.

## Logging

Logs are printed to stdout with timestamps and log levels. When running via Docker + crontab they are appended to `/var/log/digest.log`.

## License

This project is licensed under the MIT License. See the `LICENSE` file for details.
