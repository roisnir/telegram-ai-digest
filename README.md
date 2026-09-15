
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
BOT_TOKEN=<optional_bot_token>          # if set, the digest message is sent by this bot (must be admin of TARGET_CHANNEL); otherwise sent by the user account
HTML_OUTPUT_DIR=/var/www/digest          # local path where HTML files are written
PUBLIC_BASE_URL=https://digest.example.com  # public URL prefix (no trailing slash)
ALERT_CHAT_ID=me                         # optional; operator alerts (see below). Unset = alerts disabled
WHATSAPP_CHANNEL_JID=123@newsletter      # optional; also post the digest to a WhatsApp channel (see below). Unset = disabled
```

### 3. WhatsApp channel (optional)

The digest can go to a WhatsApp channel **in parallel** to Telegram. Telegram is
the authoritative path: if the WhatsApp send fails the run still succeeds, and
the failure is reported through `ALERT_CHAT_ID`. Leave `WHATSAPP_CHANNEL_JID`
unset to turn the whole thing off.

```bash
cd scripts/wa && npm install
# pair the account (QR, first run only), and resolve your channel's JID:
node send.js --jid 'https://whatsapp.com/channel/<code>'   # link: channel → info → Share link
```

Put the printed `…@newsletter` JID in `WHATSAPP_CHANNEL_JID`. The WhatsApp
session lives in `scripts/wa/auth/` — keep it, or you have to re-scan the QR.
WhatsApp drops linked devices more readily than Telegram does; when that
happens the send fails with `logged out`, and re-pairing means deleting that
directory and running the command above again.

### 4. Authenticate Telegram (first run only)

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
./build.sh            # tag = short commit, plus :latest
./build.sh v0.0.4     # explicit version tag, plus :latest
```

`build.sh` refuses to build from a dirty working tree (`git status --porcelain`
must be empty) and bakes the git **branch** and **commit** into the image, so
each run logs which build it came from. The real image `sha256` ID is injected
at runtime by `run.sh` (see below), since an image cannot contain its own ID.

### Prepare the data directory on your server

Copy these files to a persistent directory on your server (e.g. `/opt/telegram-news-digest/`):

```
/opt/telegram-news-digest/
├── .env             # your environment variables
├── session.session  # generated during first-run auth above
└── wa-auth/         # only if using WhatsApp: copy of scripts/wa/auth/ from the machine you paired on
```

### Run manually (test)

```bash
docker run --rm \
  -v /opt/telegram-news-digest/.env:/app/.env:ro \
  -v /opt/telegram-news-digest/session.session:/app/session.session \
  -v /opt/telegram-news-digest/wa-auth:/app/scripts/wa/auth \
  -v /var/www/digest:/var/www/digest \
  telegram-ai-digest
```

Mount `HTML_OUTPUT_DIR` (here `/var/www/digest`) as a volume so generated HTML files are written to the host and served by your web server.

The image ships node and the Baileys deps, but QR pairing is interactive, so pair
on a workstation (step 3) and copy `scripts/wa/auth/` to the server as `wa-auth/`.
Mount it read-write — Baileys refreshes the session keys on every run. Drop that
volume and the `WHATSAPP_CHANNEL_JID` line to run without WhatsApp.

## Scheduling with crontab

Create a wrapper script at `/opt/telegram-news-digest/run.sh`:

```bash
#!/bin/bash
IMAGE=telegram-ai-digest:latest
IMAGE_HASH=$(docker image inspect "$IMAGE" --format '{{.Id}}')

docker run --rm \
  -e DIGEST_IMAGE_HASH="$IMAGE_HASH" \
  -v /opt/telegram-news-digest/.env:/app/.env:ro \
  -v /opt/telegram-news-digest/session.session:/app/session.session \
  -v /opt/telegram-news-digest/wa-auth:/app/scripts/wa/auth \
  -v /var/www/digest:/var/www/digest \
  "$IMAGE" >> /var/log/digest.log 2>&1
```

The container then logs a line like
`Docker image: tag=... hash=sha256:ab12... branch=main commit=9f3c470` at the start of every run.

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

## Operator Alerts

The script runs unattended from cron, so a failed or degraded run is otherwise
invisible until somebody notices that no digest arrived — or reads the coverage
diagnostics at the bottom of the page. Setting `ALERT_CHAT_ID` turns on an
out-of-band Telegram alert to the operator's *private* chat.

**The feature is off by default.** If `ALERT_CHAT_ID` is unset, nothing is sent
and behaviour is identical to before.

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `ALERT_CHAT_ID` | *(unset — feature off)* | Where alerts go. A numeric Telegram user id (`123456789`), an `@username`, or the literal `me` (the sending account's own Saved Messages). |

Numeric values are passed to Telethon as integers and anything else as a string,
the same coercion `TARGET_CHANNEL` uses.

### ⚠️ A bot cannot start a conversation with you

If `BOT_TOKEN` is set, alerts are sent **by the bot**, matching how the digest
message itself is sent. Telegram does not let a bot open a chat with a user:
the bot can only message you once **you have messaged it first** (press *Start*
in the bot's chat). If you skip that, the alert send fails, the failure is
logged, and the alert never reaches you — a particularly unhelpful outcome for
a feature whose whole job is telling you when things break.

Three ways to get this right:

1. **Message the bot once**, then set `ALERT_CHAT_ID` to your numeric user id.
2. Leave `BOT_TOKEN` unset so alerts go over the **user session**, which can
   message anyone (including `me`).
3. Set `ALERT_CHAT_ID=me` — Saved Messages on the sending account. With
   `BOT_TOKEN` set this is the *bot's* own Saved Messages, which you cannot
   read, so `me` is only useful together with the user session.

Verify your setup with `python digest.py --dry-run`: alerts fire in dry-run
mode too, so a misconfiguration shows up before a real incident does.

### What triggers an alert

**Run failed — nothing published:**

- an unhandled exception anywhere in the run (for example
  `anthropic.BadRequestError: Your credit balance is too low`). The alert
  carries the exception type, its message, and the time window; the exception
  is then re-raised so the process still exits non-zero.
- `create_digest()` returned nothing — output truncated at `max_tokens`, no
  `tool_use` block came back, or the model's `big_news`/`minor_news` fields
  couldn't be parsed into a list (`DigestParseError`).
- no messages fetched from any channel (a quiet window looks the same, but you
  still got no digest).

**Digest published but looks wrong** — the alert includes the coverage numbers
and the page URL:

- at least one non-ad source message missing from the digest (Ad Messages are
  expected to be skipped, so they don't count).
- zero `big_news` stories despite there being source messages. This is checked
  independently of coverage, because a model that returns no big stories out of
  a full inbox is itself a sign something went wrong.

Sending an alert can never break a run: the send is wrapped in `try/except`, a
failure to deliver is logged (along with the text that could not be delivered),
and the original error is still raised.

## Logging

Logs are printed to stdout with timestamps and log levels. When running via Docker + crontab they are appended to `/var/log/digest.log`.

## License

This project is licensed under the MIT License. See the `LICENSE` file for details.
