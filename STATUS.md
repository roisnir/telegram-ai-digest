# Project Status

**Last updated:** 2026-09-16

## What the project does

Fetches the last 12 hours of messages from one or more Telegram channels (via Telethon, user account), summarises them into a structured Hebrew news digest using Claude AI, publishes the result as a self-hosted HTML page (`docs/adr/0001-custom-html-over-telegraph.md`), and sends the page URL as a formatted Telegram message to a private channel — where each source message renders inline via Telegram's official post embeds (`docs/adr/0002-telegram-official-embeds.md`).

## Current state: working end-to-end

The full pipeline is implemented and functional:

| Step | Implementation |
|---|---|
| Fetch messages | `fetch_messages()` — Telethon, per-channel, configurable date range |
| Summarise | `create_digest()` — Claude `claude-sonnet-4-6`, structured JSON output via tool use |
| Normalize | `normalize_digest()` — validates/repairs the model's JSON output shape |
| Publish | `build_html_page()` — self-hosted HTML page, collapsible Telegram embeds for sources |
| Notify | `format_telegram_message()` + `client.send_message()` (or a bot, if `BOT_TOKEN` is set) |
| Health check | `check_digest_health()` / `compute_coverage()` — flags a published digest that looks wrong |
| Alerting | `alerts.py` (`alert_*` functions over `AlertContext`) — operator alert via `ALERT_CHAT_ID` on run failure or an unhealthy digest (#33) |

## Key files

| File | Purpose |
|---|---|
| `digest.py` | Main script (~1190 lines) |
| `tests/` | Fixtures used by `test_digest.py` and the browser QA script |
| `test_digest.py` | Unit/integration tests |
| `conftest.py` | Pytest env-var fixtures |
| `Dockerfile` | Production image (`python:3.12-slim`) |
| `build.sh` | Builds the Docker image; refuses to build from a dirty git tree |
| `scripts/` | Auxiliary scripts (e.g. `browser-qa/` — Puppeteer QA for the rendered HTML) |
| `CONTEXT.md` | Domain glossary |
| `docs/adr/` | Decision records |
| `requirements.txt` | Pinned `anthropic`, `Telethon`, `pytz`, `pytest` — pinned exactly because an unpinned `anthropic` major bump once silently broke production |
| `.claude/CLAUDE.md` | Auto-run pytest after every code change |

## Configuration (`.env`)

```
API_ID=
API_HASH=
PHONE_NUMBER=
CHANNEL_USERNAMES=channel_one,channel_two
TARGET_CHANNEL=-1001234567890
CLAUDE_API_KEY=
HTML_OUTPUT_DIR=
PUBLIC_BASE_URL=
BOT_TOKEN=            # optional: send the digest via a bot instead of the user account
ALERT_CHAT_ID=        # optional: enables operator alerting, see alerts.py
```

## First run / one-time setup

1. **Telegram auth** — run `python digest.py` locally once; Telethon prompts for a verification code and saves `session.session`. All subsequent runs (including Docker) are non-interactive.
2. **HTML hosting** — `HTML_OUTPUT_DIR` must point at a directory served by a web server reachable at `PUBLIC_BASE_URL`; the digest writes an HTML file there on every run and links to it.

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

- `session.session` must be pre-generated locally; there is no non-interactive Telegram auth path.
- HTML pages depend on `telegram.org` at view time (embeds are lazy-loaded iframes); with no network, or if a source post is deleted, the embed will not render.
- No Telegram Instant View — the URL opens in the phone browser rather than inline in the Telegram app (tracked as a GitHub issue; see ADR-0001's "Future work").
