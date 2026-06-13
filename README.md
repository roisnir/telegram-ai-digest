
# Telegram AI Digest Generator

Python script that fetches messages from Telegram channels, summarizes them with Claude AI, and publishes a self-hosted HTML digest page. The page URL is sent to a private Telegram channel.

## Features

- **Telegram API**: Fetches last 24h of messages from one or more channels via Telethon (user account).
- **Claude AI**: Classifies and summarizes stories into a structured Hebrew digest.
- **HTML page**: Generates a self-contained RTL Hebrew HTML page served from your own VPS.

## Requirements

- Python 3.12+
- Docker (optional, for server deployment)
- A Telegram user account with API credentials
- A web server to serve the generated HTML files (nginx or Python http.server)

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
HTML_OUTPUT_DIR=/var/www/digest          # local path where HTML files are written
PUBLIC_BASE_URL=https://digest.example.com  # public URL prefix (no trailing slash)
```

### 3. Authenticate Telegram (first run only)

Telethon requires interactive phone authentication on first run. Run the script locally once to generate the `session.session` file:

```bash
pip install -r requirements.txt
python digest.py
```

Enter the verification code when prompted. After this, `session.session` is saved and all future runs (including Docker) are non-interactive.

## Web Server Setup

The script writes `digest-YYYY-MM-DD-HHMM.html` files to `HTML_OUTPUT_DIR`. You need a web server to make them publicly accessible.

### Option A — nginx (recommended)

Add a static-file location block to your nginx config:

```nginx
server {
    listen 80;
    server_name digest.example.com;

    location / {
        root /var/www/digest;
        index index.html;
        try_files $uri $uri/ =404;
    }
}
```

Reload nginx after editing:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

### Option B — Python http.server (quick testing)

```bash
cd /var/www/digest
python3 -m http.server 8080
```

Access files at `http://<server-ip>:8080/digest-YYYY-MM-DD-HHMM.html`.

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
├── .env             # your environment variables
└── session.session  # generated during first-run auth above
```

### Run manually (test)

```bash
docker run --rm \
  -v /opt/telegram-news-digest/.env:/app/.env:ro \
  -v /opt/telegram-news-digest/session.session:/app/session.session \
  -v /var/www/digest:/var/www/digest \
  telegram-ai-digest
```

Mount `HTML_OUTPUT_DIR` (here `/var/www/digest`) as a volume so generated HTML files are written to the host and served by your web server.

## Scheduling with crontab

Create a wrapper script at `/opt/telegram-news-digest/run.sh`:

```bash
#!/bin/bash
docker run --rm \
  -v /opt/telegram-news-digest/.env:/app/.env:ro \
  -v /opt/telegram-news-digest/session.session:/app/session.session \
  -v /var/www/digest:/var/www/digest \
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

Run in dry-run mode (generates the HTML file and logs its path, but does **not** send the Telegram message):

```bash
python digest.py --dry-run
```

Use `--dry-run` to inspect the generated HTML in a browser before deploying, or to test layout changes without spamming the target channel.

The script will:
1. Fetch messages from the configured Telegram channels (last 24h).
2. Classify and summarize them into a Hebrew digest using Claude AI.
3. Write an HTML digest file to `HTML_OUTPUT_DIR`.
4. Send the public URL to the target Telegram channel (skipped with `--dry-run`).

## Logging

Logs are printed to stdout with timestamps and log levels. When running via Docker + crontab they are appended to `/var/log/digest.log`.

## License

This project is licensed under the MIT License. See the `LICENSE` file for details.
