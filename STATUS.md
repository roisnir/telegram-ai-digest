# Project Status

**Branch:** `telegraph-publishing`
**Last updated:** 2026-05-15

## What the project does

Fetches the last 12 hours of messages from one or more Telegram channels (via Telethon, user account), summarises them into a structured Hebrew news digest using Claude AI, publishes the result as a Telegraph page, and sends the page URL as a formatted Telegram message to a private channel — where it opens as Instant View.

## Current state: working end-to-end

The full pipeline is implemented and functional:

| Step | Implementation |
|---|---|
| Fetch messages | `fetch_messages()` — Telethon, per-channel, configurable date range |
| Summarise | `create_digest()` — Claude `claude-haiku-4-5-20251001`, structured JSON output |
| Publish | `publish_to_telegraph()` — Telegraph Python library, Hebrew RTL content |
| Notify | `format_telegram_message()` + `client.send_message()` — HTML-formatted, Instant View link |

## Key files

| File | Purpose |
|---|---|
| `digest.py` | Main script (~445 lines) |
| `Dockerfile` | Production image (`python:3.12-slim`) |
| `requirements.txt` | `telethon`, `anthropic`, `telegraph`, `pytz`, `aiohttp` |
| `conftest.py` | Pytest env-var fixtures |
| `test_digest.py` | Unit tests (58 tests, all passing) |
| `.claude/CLAUDE.md` | Auto-run pytest after every code change |

## Configuration (`.env`)

```
API_ID=
API_HASH=
PHONE_NUMBER=
CHANNEL_USERNAMES=channel_one,channel_two
TARGET_CHANNEL=-1001234567890
CLAUDE_API_KEY=
TELEGRAPH_TOKEN=          # see First run below
```

## First run / one-time setup

1. **Telegram auth** — run `python digest.py` locally once; Telethon prompts for a verification code and saves `session.session`. All subsequent runs (including Docker) are non-interactive.
2. **Telegraph token** — on the first run without `TELEGRAPH_TOKEN` set, a Telegraph account is created and the token is logged: `Add to .env: TELEGRAPH_TOKEN=<token>`. Paste it into `.env`. The token is also saved to `telegraph_token.txt` as a fallback.

## Docker deployment

```bash
docker build -t telegram-ai-digest .

docker run --rm \
  -v /opt/telegram-news-digest/.env:/app/.env:ro \
  -v /opt/telegram-news-digest/session.session:/app/session.session \
  telegram-ai-digest
```

## Scheduled runs (crontab)

A wrapper script at `/opt/telegram-news-digest/run.sh` keeps the crontab entry short:

```bash
#!/bin/bash
IMAGE=telegram-ai-digest:latest
IMAGE_HASH=$(docker image inspect "$IMAGE" --format '{{.Id}}')

docker run --rm \
  -e DIGEST_IMAGE_HASH="$IMAGE_HASH" \
  -v /opt/telegram-news-digest/.env:/app/.env:ro \
  -v /opt/telegram-news-digest/session.session:/app/session.session \
  "$IMAGE" >> /var/log/digest.log 2>&1
```

Crontab entry (runs at 07:00 and 19:00 server time):

```
0 7,19 * * * /opt/telegram-news-digest/run.sh
```

## Known limitations / not yet done

- No retry logic if Claude returns malformed JSON (currently logs and exits).
- No alerting if the script fails silently inside cron (email-on-failure not configured).
- `session.session` must be pre-generated locally; there is no non-interactive Telegram auth path.
- Telegraph pages are created fresh on every run (no edit/update of existing pages).
