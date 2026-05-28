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
The expandable UI element in the HTML digest that shows the Source Message in a Telegram-style rounded bubble (gray background, channel name, timestamp). Uses `<details>/<summary>` for native collapse. Appears in both big_news and minor_news items.

### Media Type
Whether a Telegram message carries text only, a video (with duration), an image, or a document. Affects how the Source Bubble is labelled (📹 + duration for video, 🖼 for image) and whether the full content can be shown inline.

### External Link
A URL inside the Telegram message body that points outside Telegram (e.g. to a news article). Extracted from `message.entities` at fetch time. Used as the primary "קישור למקור" in the digest; the t.me permalink is used as fallback when no external link exists.
