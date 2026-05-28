# ADR-0001: Custom hosted HTML instead of Telegraph

**Status:** Accepted  
**Date:** 2026-05-29

## Context

The digest was published to Telegraph (telegra.ph) because it renders as Telegram Instant View — inline in the Telegram app with no browser jump. Telegraph is free and requires zero hosting infrastructure.

The requirement to show original Telegram message text in a collapsible/expandable widget cannot be met by Telegraph: it has no `<details>`/`<summary>` support, no JavaScript, and no custom CSS. Always-expanded blockquotes for every item would make the page unreadably long.

## Decision

Replace Telegraph with a self-contained HTML file served from the VPS that already runs the digest cron job. The HTML page uses native `<details>`/`<summary>` for collapsible source bubbles and inline CSS for a Telegram-style bubble aesthetic.

Configuration added to `.env`:
- `HTML_OUTPUT_DIR` — filesystem path where HTML files are written
- `PUBLIC_BASE_URL` — base URL served by the web server on the same VPS

## Consequences

- **Gained:** collapsible source message bubbles for every item (big_news and minor_news); full control over layout and styling.
- **Lost:** Telegram Instant View — the URL now opens in the phone browser instead of inline in the app.
- **Future work:** Build a Telegram Instant View template for the custom domain so IV is restored. (Tracked as a GitHub issue.)
