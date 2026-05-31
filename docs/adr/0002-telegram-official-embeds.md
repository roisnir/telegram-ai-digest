# ADR-0002: Telegram official post embeds for source rendering

**Status:** Accepted
**Date:** 2026-05-30

## Context

ADR-0001 replaced Telegraph with self-contained HTML to get collapsible source bubbles. Those bubbles are hand-built: we capture the original message text and media metadata at fetch time and render a Telegram-styled `<div>` with a 📹/🖼 label and an "open in Telegram" link.

This has limits. Video and image content cannot actually be played or viewed inline — we only show a label. The bubble is an approximation of the real message. The goal behind the feature (let the reader consume the original content without leaving the page) is not met for media.

There was also a redundancy: each big-news item carried a separate "קישור למקור" link alongside the bubbles, and that link was frequently just a self-referential channel comment link (e.g. `abualiexpress.com/heb<id>#comments`), not a real external source.

## Decision

Render each Source Message using Telegram's official post embed widget (`telegram-widget.js`, `data-telegram-post="<channel>/<id>"`) inside the collapsible source thread, instead of the hand-built bubble.

To avoid loading dozens of iframes on page load, embeds are lazy-loaded: the widget script is injected into a story's `<details>` the first time it is expanded.

When a story merges several Source Messages, all embeds are stacked as one expandable thread under a single button.

The separate "קישור למקור" link is removed. The embed is the source. A link is surfaced only when the message contains a genuine external article, framed as optional "further reading" (Further Reading Link), with Comment Links excluded.

## Consequences

- **Gained:** native inline rendering of the original message — text, video playback, images, link previews — exactly as in Telegram.
- **Gained:** less hand-built rendering code (no bubble meta, media labels, or per-message timestamp rendering for display).
- **Lost:** self-contained / offline rendering. The page now depends on telegram.org at view time (JS + one iframe per post). With no network, or if a post is deleted, the source will not render.
- **Requires JavaScript** for the lazy-load-on-expand behaviour. Custom HTML allows this; Telegraph did not.
- `source_map` still captures original text, media type, and video duration at fetch time, because the Claude input depends on them (`[VIDEO]`/`[IMAGE]` markers keep media-only messages from being dropped). Embeds are display-only.
- **Relationship to Instant View (#3 backlog):** this partially delivers the "original content inline" goal, but via embedded iframes on the hosted page rather than Telegram-native Instant View. The IV-template work remains separate.

## Alternatives considered

- **Keep hand-built bubbles (ADR-0001):** self-contained and offline, but cannot play video or show images inline — only labels.
- **Telegram Instant View template (#3):** native inline experience, but requires building and maintaining an IV template per domain and does not help the in-browser hosted page.
