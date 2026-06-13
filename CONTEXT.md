# Context: Telegram AI Digest

## Glossary

### Update (עדכון)
One complete run of the digest pipeline: fetches messages from the past N hours, classifies them, and publishes a structured HTML page. Called "digest" in code and "עדכון" in Hebrew output.

### Source Message (הודעה מקורית)
The raw Telegram message as returned by Telethon — text, media type, duration (for video), and the t.me permalink. This is the ground truth. The LLM never echoes it back; the fetch layer keeps a `link → source_message` map and the renderer uses it directly.

### Big News (חדשות גדולות)
A significant story: AI-generated headline + 2–3 sentence Hebrew summary. Max 3 per section. Displayed with full treatment: heading, meta, summary, and an expandable source message bubble.

### Minor News (עדכונים קטנים)
A smaller update: headline only (no AI summary). All input messages not classified as big_news become minor_news. Displayed as a compact list, each item expandable to show the source message bubble.

### Section
One of four topical buckets every item is classified into:
- `conflict` — Gaza war, Lebanon, Iran, military ops, hostages
- `politics` — Israeli domestic politics, Knesset, legal system, parties
- `world` — global news, economy, tech, anything else
- `deep` — long articles / analyses; headline preserved verbatim, no summary

### Source Bubble (בועת מקור)
The expandable view of a Source Message in the HTML digest, collapsed by default. Renders the original message inline using Telegram's official post embed (telegram-widget.js), so text, video, and images appear as they do in Telegram. Appears in both big_news and minor_news items. When an item merges several Source Messages, they are shown together as one expandable thread.

### Media Type
Whether a Telegram message carries text only, a video (with duration), an image, or a document. Captured at fetch time so media-only messages are not dropped from the Claude input (`[VIDEO: M:SS]` / `[IMAGE]` markers). In the rendered page the media itself is shown by the Telegram embed, so no separate media label is needed.

### Further Reading Link (להמשך קריאה)
A link to a genuinely external article referenced by a Source Message — e.g. a full op-ed column hosted on another site. Extracted from `message.entities` at fetch time. Shown in a digest item only when one exists, framed as optional "further reading" (להמשך קריאה →), never as the item's source — the source is always the Source Message itself (see Source Bubble).

Explicitly NOT a Further Reading Link: a Comment Link (below).

### Comment Link
A self-referential URL that some channels append to every message, pointing back to the same post on the channel's own web mirror — usually its comments section (e.g. `abualiexpress.com/heb<id>#comments`, or the bare `abualiexpress.com/heb<id>` mirror permalink). It is the same content as the Telegram message, not an external source, so it must be excluded from Further Reading Links. Detection is by the channel's mirror-permalink pattern (host + `/heb<id>` path), not by the `#comments` fragment — matching on `#comments` alone is too fuzzy (a real external article could carry that anchor too).
