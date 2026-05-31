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
from pytz import UTC, timezone

LOCAL_TZ = timezone('Asia/Jerusalem')

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
except ValueError as e:
    logging.error(f"Environment variable error: {str(e)}")
    raise

client = TelegramClient('session', API_ID, API_HASH)


# ---------------------------------------------------------------------------
# Digest helpers (pure functions — no I/O, fully testable)
# ---------------------------------------------------------------------------

def normalize_digest(data: dict[str, Any]) -> dict[str, Any]:
    """
    Ensures big_news/minor_news are lists of dicts.
    The SDK may return array fields as a JSON string when the model doesn't
    strictly follow the tool schema.
    Also normalises each item's `links` field: a bare string becomes a one-element list.
    """
    for key in ("big_news", "minor_news"):
        val = data.get(key, [])
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except json.JSONDecodeError:
                logging.warning(f"Could not parse '{key}' as JSON, using empty list")
                val = []
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


def _meta_text(item: dict[str, Any]) -> str:
    parts = [p for p in (item.get("source", ""), item.get("time", "")) if p]
    return " | ".join(parts)


def _link_nodes(links: list[str], label: str = "קישור") -> list:
    nodes = []
    for i, link in enumerate(links):
        text = label if len(links) == 1 else f"{label} {i + 1}"
        if nodes:
            nodes.append(" | ")
        nodes.append({"tag": "a", "attrs": {"href": link}, "children": [text]})
    return nodes


def _deep_item_node(item: dict[str, Any]) -> dict:
    meta = _meta_text(item)
    headline = item.get("headline", "")
    links = item.get("links", [])
    label = f"כתבה מ-{meta}: " if meta else ""
    children: list = [label + headline]
    if links:
        children.append(" — ")
        children.extend(_link_nodes(links, "לקריאה"))
    return {"tag": "p", "children": children}


def _section_nodes(items_big: list[dict], items_minor: list[dict], is_deep: bool = False) -> list[dict]:
    if is_deep:
        return [_deep_item_node(i) for i in items_big + items_minor]

    nodes: list[dict] = []
    for item in items_big:
        nodes.append({"tag": "h4", "children": [item.get("headline", "")]})
        meta = _meta_text(item)
        if meta:
            nodes.append({"tag": "p", "children": [{"tag": "i", "children": [meta]}]})
        if item.get("summary"):
            nodes.append({"tag": "p", "children": [item["summary"]]})
        links = item.get("links", [])
        if links:
            nodes.append({"tag": "p", "children": _link_nodes(links, "קישור למקור")})

    if items_minor:
        nodes.append({"tag": "h4", "children": ["עוד עדכונים"]})
        li_nodes = []
        for item in items_minor:
            meta = _meta_text(item)
            children: list = [item.get("headline", "")]
            if meta:
                children.append(f" ({meta})")
            links = item.get("links", [])
            if links:
                children.append(" — ")
                children.extend(_link_nodes(links, "קישור"))
            li_nodes.append({"tag": "li", "children": children})
        nodes.append({"tag": "ul", "children": li_nodes})

    return nodes


SECTION_HEADINGS: list[tuple[str, str, bool]] = [
    ("עדכוני לחימה והסכסוך", "conflict", False),
    ("פוליטיקה ישראלית", "politics", False),
    ("כותרות נוספות", "world", False),
    ("לקריאה נוספת", "deep", True),
]

SECTION_EMOJI: dict[str, str] = {
    "conflict": "⚔️",
    "politics": "🏛️",
    "world": "🌍",
    "deep": "📖",
}


def build_telegraph_content(digest: dict[str, Any]) -> list[dict]:
    content: list[dict] = []
    for heading, key, is_deep in SECTION_HEADINGS:
        big = [i for i in digest.get("big_news", []) if i.get("section") == key]
        minor = [i for i in digest.get("minor_news", []) if i.get("section") == key]
        if not big and not minor:
            continue
        content.append({"tag": "h3", "children": [heading]})
        content.extend(_section_nodes(big, minor, is_deep=is_deep))
    return content


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
    "input_schema": {
        "type": "object",
        "properties": {
            "date_range": {"type": "string"},
            "big_news": {
                "type": "array",
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
                },
            },
            "minor_news": {
                "type": "array",
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
                },
            },
        },
        "required": ["date_range", "big_news", "minor_news"],
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

    anthropic_client = anthropic.AsyncAnthropic(api_key=CLAUDE_API_KEY)
    response = await anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        tools=[DIGEST_TOOL],
        tool_choice={"type": "tool", "name": "publish_digest"},
        messages=[{
            "role": "user",
            "content": f"Date range: {date_str}\nTotal messages: {total}\n\nMessages:\n{combined}",
        }],
    )

    for block in response.content:
        if block.type == "tool_use":
            return normalize_digest(dict(block.input))

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
.source-bubble { background: #fafafa; border: 1px solid #ddd; border-radius: 4px; padding: 0.75rem; margin-top: 0.5rem; font-size: 0.85rem; }
.bubble-meta { margin: 0 0 0.3rem; }
.bubble-text { margin: 0.3rem 0; white-space: pre-wrap; word-wrap: break-word; }
.bubble-media { color: #555; margin: 0.3rem 0; }
.bubble-tme { color: #0088cc; font-size: 0.8rem; }
ul.minor-news { list-style: none; padding: 0; margin: 0; }
ul.minor-news li { margin-bottom: 0.4rem; padding: 0.5rem 0.75rem; background: white; border-radius: 6px; }
ul.minor-news li > details > summary { cursor: pointer; font-size: 0.95rem; color: #222; background: none; padding: 0; display: block; }
""".strip()


def _esc(s) -> str:
    return html.escape(str(s)) if s is not None else ""


def _primary_href(links: list, source_map: dict) -> str:
    for link in links:
        ext = source_map.get(link, {}).get("external_links", [])
        if ext:
            return ext[0]
    return links[0] if links else "#"


def _channel_from_link(link: str) -> str:
    parts = link.split("/")
    if len(parts) >= 4 and "t.me" in parts[2]:
        return f"@{parts[3]}"
    return ""


def _source_bubble_content(link: str, source_map: dict, time: str = "") -> str:
    entry = source_map.get(link, {})
    text = entry.get("text") or ""
    media_type = entry.get("media_type")
    video_duration = entry.get("video_duration")
    channel = _channel_from_link(link)
    time = entry.get("time") or time

    meta_parts = []
    if channel:
        meta_parts.append(f"<strong>{_esc(channel)}</strong>")
    if time:
        meta_parts.append(_esc(time))
    meta_html = f'<p class="bubble-meta">{" | ".join(meta_parts)}</p>' if meta_parts else ""

    text_html = f'<p class="bubble-text">{_esc(text)}</p>' if text else ""

    if media_type == "video":
        media_html = f'<p class="bubble-media">📹 {_esc(_format_duration(video_duration))}</p>'
    elif media_type in ("photo", "document"):
        media_html = '<p class="bubble-media">🖼</p>'
    else:
        media_html = ""

    tme_link = f'<a href="{_esc(link)}" class="bubble-tme">פתח בטלגרם</a>'
    return f'<div class="source-bubble">{meta_html}{text_html}{media_html}{tme_link}</div>'


def _big_item_html(item: dict, source_map: dict) -> str:
    headline = _esc(item.get("headline", ""))
    summary = _esc(item.get("summary", ""))
    source = _esc(item.get("source", ""))
    time = item.get("time", "")
    links = item.get("links", [])

    meta_parts = [p for p in (source, _esc(time)) if p]
    meta_html = f'<p class="meta"><em>{" | ".join(meta_parts)}</em></p>' if meta_parts else ""
    summary_html = f'<p>{summary}</p>' if summary else ""

    primary_href = _primary_href(links, source_map)
    link_html = f'<p><a href="{_esc(primary_href)}">קישור למקור</a></p>' if links else ""

    bubbles_html = ""
    for i, link in enumerate(links):
        label = "מקור" if len(links) == 1 else f"מקור {i + 1}"
        content = _source_bubble_content(link, source_map, time)
        bubbles_html += f'<details><summary>{label}</summary>{content}</details>\n'

    return f'<article>\n<h4>{headline}</h4>\n{meta_html}{summary_html}{link_html}{bubbles_html}</article>\n'


def _minor_item_html(item: dict, source_map: dict) -> str:
    headline = _esc(item.get("headline", ""))
    links = item.get("links", [])
    time = item.get("time", "")
    bubbles_html = "".join(_source_bubble_content(link, source_map, time) for link in links)
    return f'<li><details><summary>{headline}</summary>{bubbles_html}</details></li>\n'


_SECTION_ORDER_HTML: list[tuple[str, str]] = [
    ("conflict", "עדכוני לחימה והסכסוך"),
    ("politics", "פוליטיקה ישראלית"),
    ("world", "כותרות נוספות"),
    ("deep", "לקריאה נוספת"),
]


def build_html_page(digest: dict[str, Any], source_map: dict, end_date: datetime) -> str:
    date_range = _esc(digest.get("date_range", ""))
    sections_html = ""
    for section_key, label in _SECTION_ORDER_HTML:
        big = [i for i in digest.get("big_news", []) if i.get("section") == section_key]
        minor = [i for i in digest.get("minor_news", []) if i.get("section") == section_key]
        if not big and not minor:
            continue
        inner = "".join(_big_item_html(item, source_map) for item in big)
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
        f'<main>\n{sections_html}</main>\n'
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


async def fetch_messages(channel_username: str, start_date: datetime, end_date: datetime) -> tuple[list[str], dict]:
    message_strings: list[str] = []
    source_map: dict = {}
    try:
        channel = await client.get_entity(channel_username)
        logging.info(f"Fetching from: {channel.title} (@{channel_username})")
        async for message in client.iter_messages(channel, offset_date=end_date, limit=None):
            if message.date < start_date:
                break
            if not (start_date <= message.date <= end_date):
                continue
            media_type, video_duration = extract_media_info(message)
            text = message.text or ""
            if not text and media_type is None:
                continue
            link = f"https://t.me/{channel_username}/{message.id}"
            local_time = message.date.astimezone(LOCAL_TZ).strftime('%H:%M')
            if media_type == 'video':
                content = text if text else "[סרטון]"
                msg_str = f"[{local_time}] [VIDEO: {_format_duration(video_duration)}] {content}\nLink: {link}"
            elif media_type == 'photo':
                content = text if text else "[תמונה]"
                msg_str = f"[{local_time}] [IMAGE] {content}\nLink: {link}"
            else:
                msg_str = f"[{local_time}] {text}\nLink: {link}"
            message_strings.append(msg_str)
            source_map[link] = {
                "text": text,
                "media_type": media_type,
                "video_duration": video_duration,
                "external_links": extract_external_links(message),
                "time": local_time,
            }
        message_strings.reverse()
        logging.info(f"Fetched {len(message_strings)} messages from @{channel_username}")
    except Exception as e:
        logging.error(f"Error fetching from @{channel_username}: {e}")
    return message_strings, source_map


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = argparse.ArgumentParser(description='Generate daily Telegram news update as HTML page.')
    parser.add_argument('--startdate', type=str, help='Start datetime YYYY-MM-DD or YYYY-MM-DD HH:MM (UTC)')
    parser.add_argument('--enddate', type=str, help='End datetime YYYY-MM-DD or YYYY-MM-DD HH:MM (UTC)')
    parser.add_argument('--dry-run', action='store_true', help='Generate HTML only, skip sending Telegram message')
    args = parser.parse_args()

    def parse_dt(s: str) -> datetime:
        for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d'):
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
        raise ValueError(f"Unrecognized date format: {s}")

    if args.startdate and args.enddate:
        start_date = parse_dt(args.startdate)
        end_date = parse_dt(args.enddate)
        if len(args.enddate) == 10:
            end_date = end_date.replace(hour=23, minute=59, second=59)
    else:
        end_date = datetime.now(UTC)
        start_date = end_date - timedelta(hours=12)

    logging.info(f"Update period: {start_date} -> {end_date}")

    await client.start(phone=PHONE_NUMBER)
    logging.info("Connected to Telegram")

    messages_by_channel: dict[str, list[str]] = {}
    source_map: dict = {}
    for username in CHANNEL_USERNAMES:
        msgs, channel_source_map = await fetch_messages(username, start_date, end_date)
        if msgs:
            messages_by_channel[username] = msgs
        source_map.update(channel_source_map)

    if not messages_by_channel:
        logging.error("No messages fetched from any channel.")
        await client.disconnect()
        return

    logging.info("Generating update via Claude...")
    digest = await create_digest(messages_by_channel, start_date, end_date)

    if not digest:
        logging.error("Failed to generate update.")
        await client.disconnect()
        return

    html_content = build_html_page(digest, source_map, end_date)
    local = end_date.astimezone(LOCAL_TZ)
    filename = f"digest-{local.strftime('%Y-%m-%d-%H%M')}.html"
    html_path = Path(HTML_OUTPUT_DIR) / filename
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html_content, encoding='utf-8')
    logging.info(f"HTML digest saved: {html_path}")
    page_url = f"{PUBLIC_BASE_URL}/{filename}"

    if not args.dry_run:
        target = int(TARGET_CHANNEL) if TARGET_CHANNEL.lstrip('-').isdigit() else TARGET_CHANNEL
        message = format_telegram_message(digest, end_date, page_url)
        await client.send_message(target, message, parse_mode='html')

    await client.disconnect()


if __name__ == "__main__":
    with client:
        client.loop.run_until_complete(main())
