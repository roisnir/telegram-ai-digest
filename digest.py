import os
import re
import html
import asyncio
import json
import logging
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import anthropic
from telethon import TelegramClient
from telethon.sessions import StringSession
from pytz import UTC, timezone

LOCAL_TZ = timezone('Asia/Jerusalem')

MAX_OUTPUT_TOKENS = 64000
OUTPUT_TOKEN_WARN_THRESHOLD = 48000  # 75% of the model's 64K output ceiling

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


def load_env_from_file(env_file: str = '.env') -> None:
    env_path = Path(env_file)
    if env_path.exists():
        with env_path.open() as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    key, value = line.split('=', 1)
                    os.environ[key.strip()] = value.strip()
    else:
        logging.warning(f".env file not found at {env_path.absolute()}. Using system environment variables.")


load_env_from_file()


def get_env_variable(var_name: str) -> str:
    value = os.getenv(var_name)
    if value is None:
        raise ValueError(f"Environment variable '{var_name}' is not set.")
    return value


try:
    API_ID = int(get_env_variable('API_ID'))
    API_HASH = get_env_variable('API_HASH')
    PHONE_NUMBER = get_env_variable('PHONE_NUMBER')
    CHANNEL_USERNAMES = [c.strip() for c in get_env_variable('CHANNEL_USERNAMES').split(',')]
    CLAUDE_API_KEY = get_env_variable('CLAUDE_API_KEY')
    TARGET_CHANNEL = get_env_variable('TARGET_CHANNEL')
    HTML_OUTPUT_DIR = get_env_variable('HTML_OUTPUT_DIR')
    PUBLIC_BASE_URL = get_env_variable('PUBLIC_BASE_URL').rstrip('/')
    BOT_TOKEN = os.getenv('BOT_TOKEN')  # optional: send digest via bot instead of user account
except ValueError as e:
    logging.error(f"Environment variable error: {str(e)}")
    raise


def _float_env(var_name: str, default: float) -> float:
    raw = os.getenv(var_name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        logging.warning(f"Invalid {var_name}={raw!r} — falling back to {default}")
        return default


# Optional operator alerting. Unset ALERT_CHAT_ID => the feature is entirely off.
# Accepts a numeric Telegram user id, an '@username', or the literal 'me'
# (the sending account's own Saved Messages).
# NOTE: a Telegram bot cannot open a conversation with a user. If BOT_TOKEN is
# set the alert is sent by the bot, which only works once the operator has
# messaged that bot at least once (or if ALERT_CHAT_ID is a group/channel the
# bot is in). Otherwise use the user session (leave BOT_TOKEN unset) or 'me'.
ALERT_CHAT_ID = os.getenv('ALERT_CHAT_ID')
# Percentage of source messages that must appear in the digest before a run is
# considered healthy. Real runs land at 80-95%; the 2026-08-28 incident was 39%.
COVERAGE_ALERT_THRESHOLD = _float_env('COVERAGE_ALERT_THRESHOLD', 60.0)


# ---------------------------------------------------------------------------
# Digest helpers (pure functions — no I/O, fully testable)
# ---------------------------------------------------------------------------

class DigestParseError(Exception):
    """The model's tool output could not be parsed into the expected digest shape.

    Raised instead of silently dropping a section: an unparseable ``big_news`` /
    ``minor_news`` field means the digest is incomplete, and an incomplete digest
    must never be published.
    """


def _truncated(value: Any, limit: int = 500) -> str:
    """Render a possibly huge offending value for a log line / exception message."""
    text = value if isinstance(value, str) else repr(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… ({len(text)} chars total)"


def normalize_digest(data: dict[str, Any]) -> dict[str, Any]:
    """
    Ensures big_news/minor_news are lists of dicts.
    The SDK may return array fields as a JSON string when the model doesn't
    strictly follow the tool schema.
    Also normalises each item's `links` field: a bare string becomes a one-element list.

    Raises DigestParseError if a required array field cannot be read as a list.
    """
    for key in ("big_news", "minor_news"):
        val = data.get(key, [])
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except json.JSONDecodeError as e:
                snippet = _truncated(val)
                logging.error("Could not parse '%s' as JSON (%s): %s", key, e, snippet)
                raise DigestParseError(f"Could not parse '{key}' as JSON: {snippet}") from e
        if not isinstance(val, list):
            snippet = _truncated(val)
            logging.error("Field '%s' is not a list (got %s): %s", key, type(val).__name__, snippet)
            raise DigestParseError(
                f"Field '{key}' is not a list (got {type(val).__name__}): {snippet}"
            )
        normalized = []
        for item in val:
            if not isinstance(item, dict):
                continue
            links = item.get("links", item.get("link", []))
            if isinstance(links, str):
                links = [links] if links else []
            item["links"] = [l for l in links if isinstance(l, str) and l]
            item.pop("link", None)
            normalized.append(item)
        data[key] = normalized
    return data


def time_of_day_label(hour: int) -> str:
    return "בוקר" if hour < 13 else "ערב"


def _local_end_date(end_date: datetime) -> datetime:
    return end_date.astimezone(LOCAL_TZ)


SECTION_EMOJI: dict[str, str] = {
    "conflict": "⚔️",
    "politics": "🏛️",
    "world": "🌍",
    "deep": "📖",
}


def format_telegram_message(digest: dict[str, Any], end_date: datetime, page_url: str) -> str:
    local = _local_end_date(end_date)
    time_of_day = time_of_day_label(local.hour)
    time_str = local.strftime('%H:%M')
    date_str = local.strftime('%d.%m.%Y')

    lines = []
    for item in digest.get("big_news", []):
        headline = item.get("headline", "").strip()
        if not headline:
            continue
        link = item.get("link", "").strip()
        section = item.get("section", "")
        source = item.get("source", "").strip()
        time = item.get("time", "").strip()
        emoji = SECTION_EMOJI.get(section, "•")
        links = item.get("links", [])
        channel_handle = source.lstrip("@")
        linked_source = f'<a href="https://t.me/{channel_handle}">{source}</a>' if channel_handle else ""
        source_links_html = " | ".join(
            f'<a href="{l}">{"מקור" if len(links) == 1 else f"מקור {i+1}"}</a>'
            for i, l in enumerate(links)
        )
        meta_parts = [p for p in (linked_source, source_links_html, time) if p]
        meta = " | ".join(meta_parts)
        line = f"{emoji} {headline}"
        if meta:
            line += f"\n  ({meta})"
        lines.append(line)

    title = f"📰 עדכון {time_of_day} לשעה {time_str} | {date_str}"
    if lines:
        return f"{page_url}\n\n{title}\n\n" + "\n\n".join(lines)
    return f"{page_url}\n\n{title}"


# ---------------------------------------------------------------------------
# Claude API
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are creating a structured Hebrew daily news update from Telegram channel messages.

Classify every story into one of four sections:
- "conflict": Middle East conflicts, Gaza war, Lebanon, Iran, military operations, hostages
- "politics": Israeli domestic politics, government, Knesset, legal system, parties
- "world": global news, international events, economy, tech, anything else
- "deep": long articles, analyses, or investigative pieces — do NOT summarize; preserve original headline and link only

Within each section, classify as:
- "big_news": significant stories — headline + 2-3 sentence summary. MAXIMUM 3 items per section. Any item beyond 3 per section MUST go to minor_news.
- "minor_news": ALL remaining items — headline only. EVERY input message must appear in the output, at minimum as a minor_news item. Do not drop any message.

Rules:
- Write ALL text in Hebrew.
- HEADLINES: Copy the headline exactly as it appears in the source message. Do NOT paraphrase, shorten, or change any words. If the message has no clear title, write a minimal factual one.
- Summaries max 40 words.
- Preserve ALL original t.me message links. For a single message use one link. For merged messages include every original link in the links array.
- Merge multiple messages about the same story into one item; include all their links.
- "deep" items: preserve original headline exactly; do not copy the article body.
- Every item must include "source" (@channel handle) and "time" (HH:MM from message timestamp)."""

DIGEST_TOOL: dict[str, Any] = {
    "name": "publish_digest",
    "description": "Output the structured Hebrew daily news update",
    # Grammar-constrained decoding: the API compiles input_schema into a grammar
    # and constrains token sampling to schema-valid output. This makes it
    # structurally impossible for big_news/minor_news to arrive as a JSON string
    # instead of an array — the failure that silently gutted the digests on
    # 2026-08-28 and 2026-09-11. Requires additionalProperties: false on every
    # object below; see test_strict_schema_compliance.
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "date_range": {
                "type": "string",
                "description": "The digest window, e.g. '2026-08-28 07:00 - 2026-08-28 19:00 Israel'",
            },
            "big_news": {
                "type": "array",
                "description": (
                    "Significant stories: headline plus a 2-3 sentence Hebrew summary. "
                    "At most 3 per section."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "headline": {"type": "string"},
                        "summary": {"type": "string"},
                        "links": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                        "section": {"type": "string", "enum": ["conflict", "politics", "world", "deep"]},
                        "source": {"type": "string"},
                        "time": {"type": "string"},
                    },
                    "required": ["headline", "summary", "links", "section", "source", "time"],
                    "additionalProperties": False,
                },
            },
            "minor_news": {
                "type": "array",
                "description": (
                    "Every remaining input message, headline only. No message may be omitted."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "headline": {"type": "string"},
                        "links": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                        "section": {"type": "string", "enum": ["conflict", "politics", "world", "deep"]},
                        "source": {"type": "string"},
                        "time": {"type": "string"},
                    },
                    "required": ["headline", "links", "section", "source", "time"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["date_range", "big_news", "minor_news"],
        "additionalProperties": False,
    },
    "cache_control": {"type": "ephemeral"},
}


async def create_digest(
    messages_by_channel: dict[str, list[str]],
    start_date: datetime,
    end_date: datetime,
) -> dict[str, Any] | None:
    total = sum(len(v) for v in messages_by_channel.values())
    if total == 0:
        return None

    local_start = start_date.astimezone(LOCAL_TZ)
    local_end = end_date.astimezone(LOCAL_TZ)
    date_str = f"{local_start.strftime('%Y-%m-%d %H:%M')} - {local_end.strftime('%Y-%m-%d %H:%M')} Israel"
    combined = ""
    for channel, msgs in messages_by_channel.items():
        combined += f"\n\n### Channel: @{channel}\n" + "\n".join(msgs)

    anthropic_client = anthropic.AsyncAnthropic(
        api_key=CLAUDE_API_KEY,
        timeout=anthropic.Timeout(300.0, connect=10.0),
    )
    async with anthropic_client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=MAX_OUTPUT_TOKENS,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        tools=[DIGEST_TOOL],
        tool_choice={"type": "tool", "name": "publish_digest"},
        messages=[{
            "role": "user",
            "content": f"Date range: {date_str}\nTotal messages: {total}\n\nMessages:\n{combined}",
        }],
    ) as stream:
        response = await stream.get_final_message()

    if response.stop_reason == "max_tokens":
        logging.error(
            "Claude response truncated at max_tokens (%s output tokens) — digest unusable",
            response.usage.output_tokens,
        )
        return None

    output_tokens = response.usage.output_tokens
    near_limit = output_tokens >= OUTPUT_TOKEN_WARN_THRESHOLD
    if near_limit:
        logging.warning(
            "Claude output %s tokens — within %s of the %s ceiling; digest may need splitting soon",
            output_tokens, MAX_OUTPUT_TOKENS - output_tokens, MAX_OUTPUT_TOKENS,
        )

    for block in response.content:
        if block.type == "tool_use":
            try:
                digest = normalize_digest(dict(block.input))
            except DigestParseError as e:
                logging.error("Unusable digest from Claude — refusing to publish: %s", e)
                return None
            digest["_diagnostics"] = {
                "output_tokens": output_tokens,
                "max_output_tokens": MAX_OUTPUT_TOKENS,
                "near_limit": near_limit,
            }
            return digest

    logging.error("Claude did not return a tool_use block")
    return None


# ---------------------------------------------------------------------------
# HTML page builder
# ---------------------------------------------------------------------------

_HTML_CSS = """
* { box-sizing: border-box; }
body { font-family: Arial, 'Helvetica Neue', sans-serif; direction: rtl; margin: 0; padding: 0; background: #f5f5f5; color: #222; line-height: 1.6; }
header { background: #1a1a2e; color: white; padding: 1rem 2rem; }
header h1 { margin: 0; font-size: 1.5rem; }
.date-range { margin: 0.25rem 0 0; opacity: 0.8; font-size: 0.9rem; }
main { max-width: 800px; margin: 0 auto; padding: 1rem 1.5rem; }
section { margin-bottom: 2rem; }
section h2 { border-bottom: 2px solid #1a1a2e; padding-bottom: 0.4rem; font-size: 1.3rem; }
article { background: white; border-radius: 8px; padding: 1rem 1.25rem; margin-bottom: 1rem; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
article h4 { margin: 0 0 0.3rem; font-size: 1.05rem; }
.meta { color: #666; font-size: 0.85rem; margin: 0 0 0.5rem; }
details { margin-top: 0.5rem; }
summary { cursor: pointer; color: #555; font-size: 0.85rem; padding: 0.2rem 0.5rem; background: #f0f0f0; border-radius: 4px; display: inline-block; }
summary:hover { background: #e0e0e0; }
.further-reading { margin: 0.4rem 0 0; }
.further-reading a { color: #1a1a2e; font-weight: 600; text-decoration: none; }
.further-reading a:hover { text-decoration: underline; }
ul.minor-news { list-style: none; padding: 0; margin: 0; }
ul.minor-news li { margin-bottom: 0.4rem; padding: 0.5rem 0.75rem; background: white; border-radius: 6px; }
ul.minor-news li:hover { background: #fafafa; }
ul.minor-news li > details > summary { cursor: pointer; font-size: 0.95rem; color: #222; background: none; padding: 0; padding-inline-start: 1.1rem; display: block; position: relative; list-style: none; }
ul.minor-news li > details > summary::-webkit-details-marker { display: none; }
ul.minor-news li > details > summary::after { content: "▾"; position: absolute; inset-inline-start: 0; top: 0; display: inline-block; transform: rotate(90deg); transition: transform 0.15s; }
ul.minor-news li > details[open] > summary::after { transform: rotate(0deg); }
.channel-stats, .diagnostics { max-width: 800px; margin: 1rem auto; padding: 0 1.5rem; }
.channel-stats h2 { font-size: 1.1rem; border-bottom: 1px solid #ccc; padding-bottom: 0.3rem; }
.channel-stats ul { list-style: none; padding: 0; margin: 0.5rem 0; }
.channel-stats li { padding: 0.2rem 0; font-size: 0.95rem; }
.stats-total { font-weight: 600; margin: 0.3rem 0 0; }
.diagnostics { color: #888; font-size: 0.85rem; border-top: 1px solid #ddd; padding-top: 1rem; margin-top: 2rem; }
.diagnostics h2 { font-size: 1rem; border: none; color: #888; margin: 0 0 0.4rem; }
.diagnostics ul.coverage-per-channel { list-style: none; padding: 0; margin: 0.3rem 0; }
.diagnostics li { padding: 0.15rem 0; }
.diagnostics a { color: #777; }
.diagnostics-warning { color: #b00020; font-weight: 600; background: #fff3f3; padding: 0.5rem 0.75rem; border-radius: 6px; margin: 0 0 0.6rem; }
.diagnostics details.uncovered-real > summary { color: #b00020; background: #fff3f3; font-weight: 600; }
.diagnostics details.uncovered-ads > summary { color: #999; background: #f4f4f4; }
""".strip()


def _esc(s) -> str:
    return html.escape(str(s)) if s is not None else ""


def _tg_post_id(link: str) -> str:
    parts = link.split("/")
    if len(parts) >= 5 and "t.me" in parts[2]:
        return f"{parts[3]}/{parts[4]}"
    return ""


def _embed_placeholder(link: str) -> str:
    post_id = _tg_post_id(link)
    return f'<div class="tg-embed" data-telegram-post="{_esc(post_id)}"></div>'


def _thread_details(links: list[str], label: str) -> str:
    placeholders = "".join(_embed_placeholder(link) for link in links)
    return f'<details><summary>{label}</summary>{placeholders}</details>\n'


_LAZY_LOAD_JS = """(function(){
  document.addEventListener('toggle',function(e){
    var d=e.target;
    if(!d.open||d.dataset.embedded)return;
    d.dataset.embedded='1';
    d.querySelectorAll('.tg-embed').forEach(function(ph){
      var sc=document.createElement('script');
      sc.async=true;
      sc.src='https://telegram.org/js/telegram-widget.js?23';
      sc.setAttribute('data-telegram-post',ph.getAttribute('data-telegram-post'));
      sc.setAttribute('data-width','100%');
      ph.parentNode.replaceChild(sc,ph);
    });
  },true);
})();"""


def _further_reading_url(item: dict, source_map: dict) -> str | None:
    for link in item.get("links", []):
        ext = source_map.get(link, {}).get("external_links", [])
        if ext:
            return ext[0]
    return None


def _ordered_links(links: list[str], source_map: dict) -> list[str]:
    """Dedup exact links and order them earliest→latest by message timestamp,
    regardless of channel. Links missing a timestamp keep their relative order."""
    unique = list(dict.fromkeys(links))
    return sorted(unique, key=lambda link: source_map.get(link, {}).get("ts", float('inf')))


def _big_item_html(item: dict, source_map: dict, further_reading_url: str | None = None) -> str:
    headline = _esc(item.get("headline", ""))
    summary = _esc(item.get("summary", ""))
    source = _esc(item.get("source", ""))
    time = item.get("time", "")
    links = _ordered_links(item.get("links", []), source_map)

    meta_parts = [p for p in (source, _esc(time)) if p]
    meta_html = f'<p class="meta"><em>{" | ".join(meta_parts)}</em></p>' if meta_parts else ""
    summary_html = f'<p>{summary}</p>' if summary else ""
    fr_html = f'<p class="further-reading"><a href="{_esc(further_reading_url)}">להמשך קריאה ←</a></p>' if further_reading_url else ""

    label = "מקור" if len(links) == 1 else f"מקורות ({len(links)})"
    thread_html = _thread_details(links, label) if links else ""

    return f'<article>\n<h4>{headline}</h4>\n{meta_html}{summary_html}{fr_html}{thread_html}</article>\n'


def _minor_item_html(item: dict, source_map: dict) -> str:
    headline = _esc(item.get("headline", ""))
    links = _ordered_links(item.get("links", []), source_map)
    placeholders = "".join(_embed_placeholder(link) for link in links)
    return f'<li><details><summary>{headline}</summary>{placeholders}</details></li>\n'


_SECTION_ORDER_HTML: list[tuple[str, str]] = [
    ("conflict", "עדכוני לחימה והסכסוך"),
    ("politics", "פוליטיקה ישראלית"),
    ("world", "כותרות נוספות"),
    ("deep", "לקריאה נוספת"),
]


def _channel_of_link(link: str) -> str:
    """Return the channel username for a t.me message link, or '' if unparseable.

    Links have the shape ``https://t.me/{channel_username}/{message_id}``.
    """
    parts = link.split("/")
    if len(parts) >= 5 and "t.me" in parts[2]:
        return parts[3]
    return ""


def compute_channel_stats(source_map: dict) -> dict[str, Any]:
    """Count how many digested messages each source channel contributed.

    Derived purely from ``source_map`` keys so it behaves identically in
    ``--fixture`` mode. Returns ``{"per_channel": {username: count}, "total": int}``
    with channels ordered by descending count then name for stable rendering.
    """
    counts: dict[str, int] = {}
    for link in source_map:
        channel = _channel_of_link(link)
        if not channel:
            continue
        counts[channel] = counts.get(channel, 0) + 1
    ordered = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    return {"per_channel": ordered, "total": sum(counts.values())}


def _digest_referenced_links(digest: dict[str, Any]) -> set[str]:
    """Every t.me link referenced by any big_news or minor_news item.

    Handles both the normalised ``links`` list and a legacy single ``link``.
    """
    referenced: set[str] = set()
    for key in ("big_news", "minor_news"):
        for item in digest.get(key, []):
            if not isinstance(item, dict):
                continue
            links = item.get("links", [])
            if isinstance(links, str):
                links = [links]
            for link in links:
                if isinstance(link, str) and link:
                    referenced.add(link)
            single = item.get("link")
            if isinstance(single, str) and single:
                referenced.add(single)
    return referenced


# Sponsored-content disclosure markers used by the source channels (see CONTEXT.md,
# "Ad Message"). Derived from the published archive in /opt/nginx/digest: across all
# historical pages every occurrence of the degree-sign sigil "°תוכן…" sat in the
# uncovered list, and none ever appeared in a real story — @abualiexpress prefixes
# its paid posts with it ("°תוכן שיווקי", "°תוכן פוליטי במימון…").
# Tune this list rather than the code below. Keep it narrow: wrongly hiding a real
# story as an ad is far worse than showing an ad as missing.
_AD_MARKER_PATTERNS: list[str] = [
    r'°\s*תוכן',            # channel disclosure sigil, any "°תוכן …" variant
    r'תוכן\s+שיווקי',
    r'תוכן\s+ממומן',
    r'תוכן\s+פרסומי',
    r'פוסט\s+ממומן',
    r'הודעה\s+פרסומית',
    r'#\s*פרסומת',
    r'הפרסום\s+בטלגרם',     # channel selling its own ad slots
    r'לפרסום\s+בערוץ',
]

_AD_MARKER_RE = re.compile("|".join(_AD_MARKER_PATTERNS))

# A disclosure only counts as such when it opens the message: "בחסות"-style wording
# deep inside a news item is not a sponsorship notice.
_AD_MARKER_HEAD_CHARS = 120


def _is_ad_marker(text: str | None) -> bool:
    """True when a Source Message opens with a sponsored-content disclosure."""
    head = (text or "").replace("*", "").replace("_", "").strip()[:_AD_MARKER_HEAD_CHARS]
    return bool(head) and bool(_AD_MARKER_RE.search(head))


def classify_ad_messages(source_map: dict) -> set[str]:
    """Return the links of Source Messages that are Ad Messages (see CONTEXT.md).

    Two deliberately conservative rules:

    1. the message opens with a sponsorship disclosure marker
       (``_AD_MARKER_PATTERNS``); or
    2. it is the message posted immediately after such a marker in the same
       channel — the channels usually post the marker and the ad body as two
       consecutive messages, so a marker-only match under-counts by ~half.

    Rule 2 never chains: only the single message right after a marker is taken,
    so a real story two posts later is never swallowed. Ordering is by ``ts``
    within each channel, so it behaves the same for fixtures and live data.
    """
    ads: set[str] = set()
    by_channel: dict[str, list[str]] = {}
    for link in source_map:
        by_channel.setdefault(_channel_of_link(link), []).append(link)
    for links in by_channel.values():
        ordered = sorted(links, key=lambda l: source_map.get(l, {}).get("ts", float('inf')))
        after_marker = False
        for link in ordered:
            is_marker = _is_ad_marker(source_map.get(link, {}).get("text"))
            if is_marker or after_marker:
                ads.add(link)
            after_marker = is_marker
    return ads


def compute_coverage(digest: dict[str, Any], source_map: dict) -> dict[str, Any]:
    """Verify every source message appears in at least one story.

    Returns overall and per-channel covered/total counts plus the ordered list
    of uncovered source links, split into Ad Messages (correctly skipped) and
    real ones (genuinely dropped stories). Computed from ``source_map`` keys and
    the digest dict only, so it works the same with fixtures and live data.

    Keys: ``covered``/``total`` are the raw figures, unchanged, so pages stay
    comparable over time. ``real_covered``/``real_total`` exclude Ad Messages
    and are the actionable number; ``uncovered_ads``/``uncovered_real`` split
    ``uncovered`` (which still holds every uncovered link, in order).
    """
    referenced = _digest_referenced_links(digest)
    ad_links = classify_ad_messages(source_map)
    source_links = list(source_map.keys())

    per_channel: dict[str, dict[str, int]] = {}
    uncovered: list[str] = []
    for link in source_links:
        channel = _channel_of_link(link)
        bucket = per_channel.setdefault(channel, {"covered": 0, "total": 0})
        bucket["total"] += 1
        if link in referenced:
            bucket["covered"] += 1
        else:
            uncovered.append(link)

    uncovered = _ordered_links(uncovered, source_map)
    ordered_channels = dict(
        sorted(per_channel.items(), key=lambda kv: (-kv[1]["total"], kv[0]))
    )
    real_links = [link for link in source_links if link not in ad_links]
    return {
        "covered": sum(b["covered"] for b in per_channel.values()),
        "total": len(source_links),
        "per_channel": ordered_channels,
        "uncovered": uncovered,
        "uncovered_ads": [link for link in uncovered if link in ad_links],
        "uncovered_real": [link for link in uncovered if link not in ad_links],
        "ads": len(ad_links),
        "real_covered": sum(1 for link in real_links if link in referenced),
        "real_total": len(real_links),
    }


def _channel_stats_html(source_map: dict) -> str:
    stats = compute_channel_stats(source_map)
    if stats["total"] == 0:
        return ""
    items = "".join(
        f'<li>@{_esc(ch)} — {count} הודעות</li>\n'
        for ch, count in stats["per_channel"].items()
    )
    return (
        f'<section class="channel-stats">\n'
        f'<h2>מקורות העדכון</h2>\n'
        f'<ul>\n{items}</ul>\n'
        f'<p class="stats-total">סה"כ {stats["total"]} הודעות נכללו בעדכון.</p>\n'
        f'</section>\n'
    )


def _coverage_html(digest: dict[str, Any], source_map: dict) -> str:
    cov = compute_coverage(digest, source_map)
    diag = digest.get("_diagnostics", {})
    warning_html = ""
    if diag.get("near_limit"):
        warning_html = (
            f'<p class="diagnostics-warning">⚠️ הפלט קרוב למגבלת המודל '
            f'({diag.get("output_tokens")} מתוך {diag.get("max_output_tokens")} טוקנים). '
            f'ייתכן שחלק מההודעות לא נכללו — שקול לצמצם את טווח הזמן.</p>\n'
        )
    if cov["total"] == 0:
        if warning_html:
            return (
                f'<section class="diagnostics">\n'
                f'<h2>בדיקת כיסוי</h2>\n'
                f'{warning_html}'
                f'</section>\n'
            )
        return ""

    per_channel_items = "".join(
        f'<li>@{_esc(ch)} — סוקרו {b["covered"]} מתוך {b["total"]} הודעות</li>\n'
        for ch, b in cov["per_channel"].items()
    )

    def _uncovered_details(links: list[str], summary: str, css_class: str, open_by_default: bool) -> str:
        rows = ""
        for link in links:
            info = source_map.get(link, {})
            time = info.get("time", "")
            snippet = (info.get("text") or "").strip().replace("\n", " ")
            if len(snippet) > 80:
                snippet = snippet[:80] + "…"
            label_parts = [p for p in (_esc(time), _esc(snippet)) if p]
            label = " — ".join(label_parts) if label_parts else _esc(link)
            rows += f'<li><a href="{_esc(link)}">{label}</a></li>\n'
        attrs = f' class="{css_class}"' + (" open" if open_by_default else "")
        return (
            f'<details{attrs}>\n'
            f'<summary>{summary}</summary>\n'
            f'<ul>\n{rows}</ul>\n'
            f'</details>\n'
        )

    real_uncovered = cov["uncovered_real"]
    ad_uncovered = cov["uncovered_ads"]

    if real_uncovered:
        warning_html += (
            f'<p class="diagnostics-warning">⚠️ {len(real_uncovered)} הודעות תוכן לא סוקרו בעדכון — '
            f'ייתכן שידיעות אמיתיות נשמטו.</p>\n'
        )

    uncovered_html = ""
    if real_uncovered:
        uncovered_html += _uncovered_details(
            real_uncovered,
            f'{len(real_uncovered)} הודעות תוכן שלא סוקרו',
            "uncovered-real",
            True,
        )
    elif not ad_uncovered:
        uncovered_html += '<p>כל ההודעות סוקרו בעדכון. ✓</p>\n'
    else:
        uncovered_html += '<p>כל הודעות התוכן סוקרו בעדכון. ✓</p>\n'

    if ad_uncovered:
        uncovered_html += _uncovered_details(
            ad_uncovered,
            f'{len(ad_uncovered)} הודעות שיווקיות שדולגו (כצפוי)',
            "uncovered-ads",
            False,
        )

    real_line = ""
    if cov["ads"] and cov["real_total"]:
        real_line = (
            f'<p class="meta">כיסוי תוכן: סוקרו {cov["real_covered"]} מתוך {cov["real_total"]} '
            f'הודעות, ללא {cov["ads"]} הודעות שיווקיות.</p>\n'
        )

    return (
        f'<section class="diagnostics">\n'
        f'<h2>בדיקת כיסוי</h2>\n'
        f'{warning_html}'
        f'<p class="meta">סוקרו {cov["covered"]} מתוך {cov["total"]} הודעות.</p>\n'
        f'{real_line}'
        f'<ul class="coverage-per-channel">\n{per_channel_items}</ul>\n'
        f'{uncovered_html}'
        f'</section>\n'
    )


def build_html_page(digest: dict[str, Any], source_map: dict, end_date: datetime) -> str:
    date_range = _esc(digest.get("date_range", ""))
    sections_html = ""
    for section_key, label in _SECTION_ORDER_HTML:
        big = [i for i in digest.get("big_news", []) if i.get("section") == section_key]
        minor = [i for i in digest.get("minor_news", []) if i.get("section") == section_key]
        if not big and not minor:
            continue
        inner = "".join(_big_item_html(item, source_map, _further_reading_url(item, source_map)) for item in big)
        if minor:
            minor_li = "".join(_minor_item_html(item, source_map) for item in minor)
            inner += f'<ul class="minor-news">\n{minor_li}</ul>\n'
        sections_html += f'<section>\n<h2>{_esc(label)}</h2>\n{inner}</section>\n'

    return (
        f'<!DOCTYPE html>\n'
        f'<html lang="he" dir="rtl">\n'
        f'<head>\n'
        f'<meta charset="UTF-8">\n'
        f'<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f'<title>דיג\'סט יומי — {date_range}</title>\n'
        f'<style>\n{_HTML_CSS}\n</style>\n'
        f'</head>\n'
        f'<body>\n'
        f'<header>\n<h1>דיג\'סט יומי</h1>\n<p class="date-range">{date_range}</p>\n</header>\n'
        f'{_channel_stats_html(source_map)}'
        f'<main>\n{sections_html}</main>\n'
        f'{_coverage_html(digest, source_map)}'
        f'<script>\n{_LAZY_LOAD_JS}\n</script>\n'
        f'</body>\n'
        f'</html>'
    )


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def _format_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "?:??"
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def extract_media_info(message) -> tuple[str | None, int | None]:
    if getattr(message, 'video', None) is not None:
        file_obj = getattr(message, 'file', None)
        duration = getattr(file_obj, 'duration', None) if file_obj is not None else None
        return ('video', duration)
    if getattr(message, 'photo', None) is not None:
        return ('photo', None)
    if getattr(message, 'document', None) is not None:
        return ('document', None)
    return (None, None)


# Matches channel mirror-permalink pattern: abualiexpress.<tld>/heb<id>
# Used to exclude Comment Links from Further Reading (see CONTEXT.md)
_MIRROR_LINK_RE = re.compile(r'//[^/]*abualiexpress\.[^/]+/heb\d+', re.IGNORECASE)


def extract_external_links(message) -> list[str]:
    entities = getattr(message, 'entities', None)
    if not entities:
        return []
    text = getattr(message, 'text', '') or ''
    seen: set[str] = set()
    links: list[str] = []
    for entity in entities:
        entity_type = type(entity).__name__
        url: str | None = None
        if entity_type == 'MessageEntityTextUrl':
            url = getattr(entity, 'url', None)
        elif entity_type == 'MessageEntityUrl':
            offset = getattr(entity, 'offset', 0)
            length = getattr(entity, 'length', 0)
            url = text[offset:offset + length]
        if url and 't.me' not in url and not _MIRROR_LINK_RE.search(url) and url not in seen:
            seen.add(url)
            links.append(url)
    return links


def build_channel_sources(messages, channel_username: str) -> tuple[list[str], dict]:
    """Build (message_strings, source_map) from telethon messages (newest-first).

    A Telegram album (several photos/videos in one post) arrives as multiple
    messages that share a ``grouped_id``; visually they are a single post and any
    member's embed renders the whole album. Collapse each ``grouped_id`` to one
    source so an album is counted and embedded once, not once per media item.
    """
    posts: list[dict] = []
    group_idx: dict[int, int] = {}
    for message in messages:
        media_type, video_duration = extract_media_info(message)
        text = message.text or ""
        grouped_id = getattr(message, 'grouped_id', None)
        if grouped_id is not None and grouped_id in group_idx:
            post = posts[group_idx[grouped_id]]
            # Embed the album anchor (lowest id) — it renders the whole album.
            post["id"] = min(post["id"], message.id)
            # The caption may sit on a different album member than the anchor.
            if text and not post["text"]:
                post["text"] = text
            if media_type == 'video' and post["media_type"] != 'video':
                post["media_type"] = 'video'
                post["video_duration"] = video_duration
            elif media_type and post["media_type"] is None:
                post["media_type"] = media_type
            for url in extract_external_links(message):
                if url not in post["external_links"]:
                    post["external_links"].append(url)
            continue
        if not text and media_type is None:
            continue
        post = {
            "id": message.id,
            "text": text,
            "media_type": media_type,
            "video_duration": video_duration,
            "external_links": extract_external_links(message),
            "time": message.date.astimezone(LOCAL_TZ).strftime('%H:%M'),
            "ts": message.date.timestamp(),
        }
        posts.append(post)
        if grouped_id is not None:
            group_idx[grouped_id] = len(posts) - 1

    message_strings: list[str] = []
    source_map: dict = {}
    for post in reversed(posts):  # oldest-first for the Claude prompt
        link = f"https://t.me/{channel_username}/{post['id']}"
        if post["media_type"] == 'video':
            content = post["text"] or "[סרטון]"
            msg_str = f"[{post['time']}] [VIDEO: {_format_duration(post['video_duration'])}] {content}\nLink: {link}"
        elif post["media_type"] == 'photo':
            content = post["text"] or "[תמונה]"
            msg_str = f"[{post['time']}] [IMAGE] {content}\nLink: {link}"
        else:
            msg_str = f"[{post['time']}] {post['text']}\nLink: {link}"
        message_strings.append(msg_str)
        source_map[link] = {
            "text": post["text"],
            "media_type": post["media_type"],
            "video_duration": post["video_duration"],
            "external_links": post["external_links"],
            "time": post["time"],
            "ts": post["ts"],
        }
    return message_strings, source_map


async def fetch_messages(client, channel_username: str, start_date: datetime, end_date: datetime) -> tuple[list[str], dict]:
    try:
        channel = await client.get_entity(channel_username)
        logging.info(f"Fetching from: {channel.title} (@{channel_username})")
        raw: list = []
        async for message in client.iter_messages(channel, offset_date=end_date, limit=None):
            if message.date < start_date:
                break
            if not (start_date <= message.date <= end_date):
                continue
            raw.append(message)
        message_strings, source_map = build_channel_sources(raw, channel_username)
        logging.info(f"Fetched {len(message_strings)} messages from @{channel_username}")
        return message_strings, source_map
    except Exception as e:
        logging.error(f"Error fetching from @{channel_username}: {e}")
        return [], {}


# ---------------------------------------------------------------------------
# Operator alerts
# ---------------------------------------------------------------------------

_ALERT_ERROR_MAXLEN = 400


def _coerce_chat_id(raw: str) -> int | str:
    """Coerce a configured chat id to what Telethon expects.

    Same convention as TARGET_CHANNEL: a bare (optionally negative) number is
    passed as an int; anything else — '@username' or the literal 'me' — is
    passed through as a string for Telethon to resolve.
    """
    raw = (raw or "").strip()
    return int(raw) if raw.lstrip('-').isdigit() else raw


def format_window(start_date: datetime | None, end_date: datetime | None) -> str:
    """Human-readable local-time description of the window being processed."""
    if end_date is None:
        return "unknown window"
    end_str = end_date.astimezone(LOCAL_TZ).strftime('%Y-%m-%d %H:%M')
    if start_date is None:
        return f"up to {end_str} Israel"
    start_str = start_date.astimezone(LOCAL_TZ).strftime('%Y-%m-%d %H:%M')
    return f"{start_str} -> {end_str} Israel"


def check_digest_health(
    digest: dict[str, Any],
    source_map: dict,
    threshold: float | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Return (issues, coverage) for a digest that is about to be published.

    ``issues`` is empty for a healthy digest. Two independent signals:
    poor coverage of the source messages, and an empty ``big_news`` section
    despite there being source messages at all (the shape of the 2026-08-28
    incident, where the model returned big_news as an unparseable string and
    normalize_digest substituted an empty list).
    """
    if threshold is None:
        threshold = COVERAGE_ALERT_THRESHOLD
    cov = compute_coverage(digest, source_map)
    issues: list[str] = []
    total = cov["total"]
    if total:
        pct = 100.0 * cov["covered"] / total
        if pct < threshold:
            issues.append(f"coverage {pct:.0f}% is below the {threshold:.0f}% threshold")
        if not digest.get("big_news"):
            issues.append(f"no big_news stories despite {total} source messages")
    return issues, cov


def format_failure_alert(error: BaseException | str, window: str, stage: str = "") -> str:
    """Short, phone-skimmable alert for a run that produced no digest."""
    if isinstance(error, BaseException):
        detail = f"{type(error).__name__}: {error}"
    else:
        detail = str(error)
    if len(detail) > _ALERT_ERROR_MAXLEN:
        detail = detail[:_ALERT_ERROR_MAXLEN] + "…"
    lines = ["🚨 Digest run FAILED — nothing was published.", f"Window: {window}"]
    if stage:
        lines.append(f"Stage: {stage}")
    lines.append(f"Error: {detail}")
    return "\n".join(lines)


def format_health_alert(
    issues: list[str],
    coverage: dict[str, Any],
    window: str,
    page_url: str | None = None,
) -> str:
    """Short, phone-skimmable alert for a digest that published but looks wrong."""
    total = coverage.get("total", 0)
    covered = coverage.get("covered", 0)
    pct = (100.0 * covered / total) if total else 0.0
    lines = [
        "⚠️ Digest published but looks wrong.",
        f"Window: {window}",
        f"Coverage: {covered}/{total} ({pct:.0f}%)",
    ]
    lines += [f"• {issue}" for issue in issues]
    if page_url:
        lines.append(f"Page: {page_url}")
    return "\n".join(lines)


async def send_alert(text: str, client=None) -> bool:
    """Best-effort operator alert. Never raises.

    Off entirely when ALERT_CHAT_ID is unset. Prefers the bot when BOT_TOKEN is
    set (see the note next to ALERT_CHAT_ID about bots not being able to DM
    first), then a user session already connected by the caller, and finally a
    freshly started user session.

    Sent as plain text on purpose: the digest message uses parse_mode='html',
    but alert bodies embed arbitrary exception text that would break HTML
    parsing and silently drop the alert.
    """
    if not ALERT_CHAT_ID:
        return False
    bot = None
    own_client = None
    try:
        target = _coerce_chat_id(ALERT_CHAT_ID)
        if BOT_TOKEN:
            bot = await TelegramClient(StringSession(), API_ID, API_HASH).start(bot_token=BOT_TOKEN)
            sender = bot
        elif client is not None:
            sender = client
        else:
            own_client = TelegramClient('session', API_ID, API_HASH)
            await own_client.start(phone=PHONE_NUMBER)
            sender = own_client
        await sender.send_message(target, text)
        logging.info(f"Operator alert sent to {target}")
        return True
    except Exception as e:
        logging.error(
            f"Failed to send operator alert ({type(e).__name__}: {e}). "
            f"Alert text was: {text}"
        )
        return False
    finally:
        for c in (bot, own_client):
            if c is None:
                continue
            try:
                await c.disconnect()
            except Exception as e:
                logging.warning(f"Failed to disconnect alert client: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run_digest(ctx: dict[str, Any]) -> None:
    """The digest pipeline. ``ctx`` collects state main() needs when alerting."""
    parser = argparse.ArgumentParser(description='Generate daily Telegram news update as HTML page.')
    parser.add_argument('--startdate', type=str, help='Start datetime YYYY-MM-DD or YYYY-MM-DD HH:MM (UTC)')
    parser.add_argument('--enddate', type=str, help='End datetime YYYY-MM-DD or YYYY-MM-DD HH:MM (UTC)')
    parser.add_argument('--dry-run', action='store_true', help='Generate HTML only, skip sending Telegram message')
    parser.add_argument('--fixture', type=str, help='Load digest from JSON fixture (skips Telegram fetch and Claude API)')
    parser.add_argument('--output', type=str, help='Write HTML to this exact path instead of HTML_OUTPUT_DIR')
    args = parser.parse_args()

    def parse_dt(s: str) -> datetime:
        for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d'):
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
        raise ValueError(f"Unrecognized date format: {s}")

    if args.fixture:
        with open(args.fixture) as f:
            fixture_data = json.load(f)
        digest = normalize_digest(fixture_data['digest'])
        source_map = fixture_data.get('source_map', {})
        end_date = datetime.now(UTC)
        ctx['window'] = format_window(None, end_date)
        logging.info(f"Loaded fixture: {args.fixture}")
    else:
        if args.startdate and args.enddate:
            start_date = parse_dt(args.startdate)
            end_date = parse_dt(args.enddate)
            if len(args.enddate) == 10:
                end_date = end_date.replace(hour=23, minute=59, second=59)
        else:
            end_date = datetime.now(UTC)
            start_date = end_date - timedelta(hours=12)

        ctx['window'] = format_window(start_date, end_date)
        logging.info(f"Update period: {start_date} -> {end_date}")

        client = TelegramClient('session', API_ID, API_HASH)
        ctx['client'] = client
        await client.start(phone=PHONE_NUMBER)
        logging.info("Connected to Telegram")

        messages_by_channel: dict[str, list[str]] = {}
        source_map: dict = {}
        for username in CHANNEL_USERNAMES:
            msgs, channel_source_map = await fetch_messages(client, username, start_date, end_date)
            if msgs:
                messages_by_channel[username] = msgs
            source_map.update(channel_source_map)

        if not messages_by_channel:
            # A quiet news window is not a failure — exit 0 so cron stays green.
            # The operator still gets no digest though, so say so — that is
            # exactly the "no digests arrived and nobody noticed" mode.
            logging.warning("No messages fetched from any channel — nothing to publish.")
            await send_alert(
                f"⚠️ No digest published.\nWindow: {ctx['window']}\n"
                "No messages were fetched from any channel — quiet window, or a broken fetch.",
                client,
            )
            await client.disconnect()
            return

        logging.info("Generating update via Claude...")
        digest = await create_digest(messages_by_channel, start_date, end_date)

        if not digest:
            # Covers every generation failure: unparseable tool output,
            # max_tokens truncation, and a missing tool_use block. Nothing is
            # published, and the process exits non-zero so cron/run.sh notice.
            logging.error("Failed to generate update — nothing published.")
            await send_alert(
                format_failure_alert(
                    "create_digest() returned no digest — output truncated at max_tokens, "
                    "or no tool_use block came back. See the run log.",
                    ctx['window'],
                    stage="claude",
                ),
                client,
            )
            await client.disconnect()
            raise SystemExit(1)

    html_content = build_html_page(digest, source_map, end_date)

    if args.output:
        html_path = Path(args.output)
        html_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        local = end_date.astimezone(LOCAL_TZ)
        filename = f"digest-{local.strftime('%Y-%m-%d-%H%M')}.html"
        html_path = Path(HTML_OUTPUT_DIR) / filename
        html_path.parent.mkdir(parents=True, exist_ok=True)

    html_path.write_text(html_content, encoding='utf-8')
    logging.info(f"HTML digest saved: {html_path}")
    page_url = f"{PUBLIC_BASE_URL}/{html_path.name}" if not args.output else str(html_path)

    if not args.dry_run and not args.fixture:
        target = int(TARGET_CHANNEL) if TARGET_CHANNEL.lstrip('-').isdigit() else TARGET_CHANNEL
        message = format_telegram_message(digest, end_date, page_url)
        if BOT_TOKEN:
            bot = await TelegramClient(StringSession(), API_ID, API_HASH).start(bot_token=BOT_TOKEN)
            await bot.send_message(target, message, parse_mode='html')
            await bot.disconnect()
        else:
            await client.send_message(target, message, parse_mode='html')

    issues, coverage = check_digest_health(digest, source_map)
    if issues:
        logging.warning(f"Digest published but looks wrong: {'; '.join(issues)}")
        await send_alert(
            format_health_alert(issues, coverage, ctx.get('window', 'unknown window'), page_url),
            ctx.get('client'),
        )

    if not args.fixture:
        await client.disconnect()
        ctx['client'] = None


async def main() -> None:
    """Run the digest, alerting the operator on failure. Re-raises after alerting."""
    ctx: dict[str, Any] = {}
    try:
        await run_digest(ctx)
    except Exception as e:
        logging.error(f"Digest run failed: {type(e).__name__}: {e}")
        await send_alert(
            format_failure_alert(e, ctx.get('window', 'unknown window')),
            ctx.get('client'),
        )
        raise


if __name__ == "__main__":
    asyncio.run(main())
