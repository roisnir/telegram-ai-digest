# Project: Telegram Daily Digest → Hosted HTML Page

## What this project does
`digest.py` fetches messages from Telegram channels via Telethon (user account, not bot),
summarizes them with the Claude API, and publishes the result as a self-contained HTML page
hosted on the same VPS that runs the cron job (see `docs/adr/0001-custom-html-over-telegraph.md`
— Telegraph was tried first and rejected). The page URL is sent as a formatted Telegram message
to a private channel; each original source message renders inline via Telegram's official post
embeds (see `docs/adr/0002-telegram-official-embeds.md`).

For domain terms (Update, Source Message, Big/Minor News, Section, Source Bubble, Coverage
Check, ...) see `CONTEXT.md`. For the full decision history see `docs/adr/`.

## Pipeline
Matches `run_digest()` in `digest.py` today:
1. `fetch_messages()` — Telethon, per channel, over the configured date window
2. `create_digest()` — Claude API (`claude-sonnet-4-6`), structured JSON digest via tool use
3. `normalize_digest()` — validates/repairs the model's JSON output shape
4. `build_html_page()` — renders the digest as a self-hosted HTML page, with collapsible
   Telegram embeds for source messages
5. HTML is written under `HTML_OUTPUT_DIR`; the URL is formatted by
   `format_telegram_message()` and sent via `client.send_message()` (or a bot, if `BOT_TOKEN`
   is set)
6. `check_digest_health()` / `compute_coverage()` — post-publish diagnostics (did the digest
   plausibly cover the fetched messages?)
7. `send_alert()` — notifies the operator via `ALERT_CHAT_ID` if the run fails or
   `check_digest_health()` flags the published digest as wrong (#33)

## Working in this repo
Run `pytest` after every change (see `docs/testing-workflow.md` for what each tier covers).
`.claude/CLAUDE.md` auto-runs `python -m pytest test_digest.py -v` after every code change.

## .env variables
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
ALERT_CHAT_ID=        # optional: enables operator alerting, see send_alert()
```

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues (`github.com/roisnir/telegram-ai-digest`). See `docs/agents/issue-tracker.md`.

### Triage labels

Uses the default label vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
