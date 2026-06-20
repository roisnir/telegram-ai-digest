import asyncio
import json
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from pytz import UTC, timezone

from digest import (
    normalize_digest,
    time_of_day_label,
    format_telegram_message,
    build_html_page,
    build_channel_sources,
    compute_channel_stats,
    compute_coverage,
    _channel_of_link,
    extract_media_info,
    extract_external_links,
    fetch_messages,
    create_digest,
    main,
    LOCAL_TZ,
)


# ---------------------------------------------------------------------------
# Minimal stub classes that mimic Telethon entity types by name
# ---------------------------------------------------------------------------

class MessageEntityUrl:
    def __init__(self, offset, length):
        self.offset = offset
        self.length = length


class MessageEntityTextUrl:
    def __init__(self, offset, length, url):
        self.offset = offset
        self.length = length
        self.url = url

# 07:00 Israel = 04:00 UTC in summer (UTC+3), 05:00 UTC in winter (UTC+2)
# Use aware datetimes in Israel timezone directly to avoid DST ambiguity in tests.
MORNING_IL = datetime(2026, 5, 13, 7, 0, tzinfo=LOCAL_TZ)
EVENING_IL = datetime(2026, 5, 13, 19, 0, tzinfo=LOCAL_TZ)


# ---------------------------------------------------------------------------
# normalize_digest
# ---------------------------------------------------------------------------

class TestNormalizeDigest:
    def test_proper_list_of_dicts_unchanged(self):
        data = {
            "date_range": "...",
            "big_news": [{"headline": "h", "section": "conflict", "links": ["https://t.me/x/1"]}],
            "minor_news": [{"headline": "m", "section": "world", "links": ["https://t.me/x/2"]}],
        }
        result = normalize_digest(data)
        assert result["big_news"][0]["links"] == ["https://t.me/x/1"]
        assert result["minor_news"][0]["links"] == ["https://t.me/x/2"]

    def test_json_string_array_is_parsed(self):
        data = {
            "date_range": "...",
            "big_news": '[{"headline": "h", "section": "conflict", "links": ["https://t.me/x/1"]}]',
            "minor_news": "[]",
        }
        result = normalize_digest(data)
        assert result["big_news"][0]["headline"] == "h"
        assert result["minor_news"] == []

    def test_bare_link_string_promoted_to_links_list(self):
        data = {
            "date_range": "...",
            "big_news": [{"headline": "h", "link": "https://t.me/x/1"}],
            "minor_news": [],
        }
        result = normalize_digest(data)
        assert result["big_news"][0]["links"] == ["https://t.me/x/1"]
        assert "link" not in result["big_news"][0]

    def test_links_as_string_promoted_to_list(self):
        data = {
            "date_range": "...",
            "big_news": [{"headline": "h", "links": "https://t.me/x/1"}],
            "minor_news": [],
        }
        result = normalize_digest(data)
        assert result["big_news"][0]["links"] == ["https://t.me/x/1"]

    def test_non_dict_items_are_filtered_out(self):
        data = {
            "date_range": "...",
            "big_news": [{"headline": "good", "links": []}, "bad string", 42, None],
            "minor_news": [],
        }
        result = normalize_digest(data)
        assert len(result["big_news"]) == 1
        assert result["big_news"][0]["headline"] == "good"

    def test_invalid_json_string_becomes_empty_list(self):
        data = {"date_range": "...", "big_news": "not valid json {{{", "minor_news": []}
        result = normalize_digest(data)
        assert result["big_news"] == []

    def test_missing_keys_default_to_empty_list(self):
        result = normalize_digest({"date_range": "..."})
        assert result["big_news"] == []
        assert result["minor_news"] == []

    def test_mutates_and_returns_same_dict(self):
        data = {"date_range": "...", "big_news": [], "minor_news": []}
        result = normalize_digest(data)
        assert result is data


# ---------------------------------------------------------------------------
# time_of_day_label
# ---------------------------------------------------------------------------

class TestTimeOfDayLabel:
    @pytest.mark.parametrize("hour", [0, 6, 7, 12])
    def test_morning(self, hour):
        assert time_of_day_label(hour) == "בוקר"

    @pytest.mark.parametrize("hour", [13, 19, 22, 23])
    def test_evening(self, hour):
        assert time_of_day_label(hour) == "ערב"


# ---------------------------------------------------------------------------
# format_telegram_message
# ---------------------------------------------------------------------------

class TestFormatTelegramMessage:
    URL = "https://telegra.ph/test"
    DIGEST = {
        "big_news": [
            {"headline": "כותרת ראשית", "links": ["https://t.me/ch/42"], "source": "@ch", "time": "06:45", "section": "conflict"},
        ],
        "minor_news": [],
    }

    def test_morning_label(self):
        assert "עדכון בוקר" in format_telegram_message(self.DIGEST, MORNING_IL, self.URL)

    def test_evening_label(self):
        assert "עדכון ערב" in format_telegram_message(self.DIGEST, EVENING_IL, self.URL)

    def test_local_time_in_title_not_utc(self):
        # 19:00 Israel time — title must show 19:00, not the UTC equivalent
        msg = format_telegram_message(self.DIGEST, EVENING_IL, self.URL)
        assert "19:00" in msg

    def test_morning_time_in_title(self):
        msg = format_telegram_message(self.DIGEST, MORNING_IL, self.URL)
        assert "07:00" in msg

    def test_headline_in_body(self):
        assert "כותרת ראשית" in format_telegram_message(self.DIGEST, MORNING_IL, self.URL)

    def test_source_in_body(self):
        assert "@ch" in format_telegram_message(self.DIGEST, MORNING_IL, self.URL)

    def test_item_time_in_body(self):
        assert "06:45" in format_telegram_message(self.DIGEST, MORNING_IL, self.URL)

    @pytest.mark.parametrize("section,emoji", [
        ("conflict", "⚔️"),
        ("politics", "🏛️"),
        ("world", "🌍"),
        ("deep", "📖"),
    ])
    def test_section_emoji_prefix(self, section, emoji):
        digest = {"big_news": [{"headline": "כותרת", "link": "https://t.me/x/1", "source": "@ch", "time": "07:00", "section": section}]}
        msg = format_telegram_message(digest, MORNING_IL, self.URL)
        assert emoji in msg

    def test_headline_has_separate_source_link(self):
        msg = format_telegram_message(self.DIGEST, MORNING_IL, self.URL)
        assert '<a href="https://t.me/ch/42">מקור</a>' in msg
        assert '>כותרת ראשית<' not in msg  # headline is plain text, not wrapped in anchor

    def test_multiple_links_shown_as_numbered_sources(self):
        digest = {"big_news": [{"headline": "כותרת", "links": ["https://t.me/x/1", "https://t.me/x/2"], "source": "@ch", "time": "07:00", "section": "conflict"}]}
        msg = format_telegram_message(digest, MORNING_IL, self.URL)
        assert "מקור 1" in msg
        assert "מקור 2" in msg

    def test_headline_without_links_still_renders(self):
        digest = {"big_news": [{"headline": "כותרת", "links": [], "source": "@ch", "time": "07:00", "section": "conflict"}]}
        msg = format_telegram_message(digest, MORNING_IL, self.URL)
        assert "⚔️ כותרת" in msg

    def test_source_is_linked_to_channel(self):
        msg = format_telegram_message(self.DIGEST, MORNING_IL, self.URL)
        assert '<a href="https://t.me/ch">@ch</a>' in msg

    def test_headlines_separated_by_blank_lines(self):
        digest = {"big_news": [
            {"headline": "א", "links": ["https://t.me/x/1"], "source": "@ch", "time": "07:00", "section": "conflict"},
            {"headline": "ב", "links": ["https://t.me/x/2"], "source": "@ch", "time": "07:01", "section": "politics"},
        ]}
        msg = format_telegram_message(digest, MORNING_IL, self.URL)
        assert "\n\n" in msg[msg.index("⚔️"):]  # blank line between headlines

    def test_telegraph_url_is_first(self):
        msg = format_telegram_message(self.DIGEST, MORNING_IL, self.URL)
        assert msg.startswith(self.URL)

    def test_no_headlines_still_has_url(self):
        msg = format_telegram_message({"big_news": [], "minor_news": []}, MORNING_IL, self.URL)
        assert self.URL in msg

    def test_items_with_empty_headline_skipped(self):
        digest = {"big_news": [{"headline": "", "links": [], "source": "@ch", "time": "07:00", "section": "conflict"}]}
        msg = format_telegram_message(digest, MORNING_IL, self.URL)
        assert "⚔️" not in msg


# ---------------------------------------------------------------------------
# extract_media_info
# ---------------------------------------------------------------------------

class TestExtractMediaInfo:
    def _msg(self, video=None, photo=None, document=None, file_duration=None):
        msg = Mock()
        msg.video = video
        msg.photo = photo
        msg.document = document
        msg.file = Mock()
        msg.file.duration = file_duration
        return msg

    def test_video_returns_video_type(self):
        msg = self._msg(video=object(), file_duration=90)
        media_type, duration = extract_media_info(msg)
        assert media_type == 'video'

    def test_video_returns_duration_seconds(self):
        msg = self._msg(video=object(), file_duration=90)
        _, duration = extract_media_info(msg)
        assert duration == 90

    def test_photo_returns_photo_type(self):
        msg = self._msg(photo=object())
        media_type, duration = extract_media_info(msg)
        assert media_type == 'photo'
        assert duration is None

    def test_document_returns_document_type(self):
        msg = self._msg(document=object())
        media_type, duration = extract_media_info(msg)
        assert media_type == 'document'
        assert duration is None

    def test_text_only_returns_none_none(self):
        msg = self._msg()
        assert extract_media_info(msg) == (None, None)

    def test_video_without_duration_returns_none_duration(self):
        msg = self._msg(video=object(), file_duration=None)
        media_type, duration = extract_media_info(msg)
        assert media_type == 'video'
        assert duration is None


# ---------------------------------------------------------------------------
# extract_external_links
# ---------------------------------------------------------------------------

class TestExtractExternalLinks:
    def _msg(self, text="", entities=None):
        msg = Mock()
        msg.text = text
        msg.entities = entities
        return msg

    def test_none_entities_returns_empty(self):
        assert extract_external_links(self._msg(entities=None)) == []

    def test_empty_entities_returns_empty(self):
        assert extract_external_links(self._msg(entities=[])) == []

    def test_text_url_entity_extracted(self):
        text = "visit https://example.com today"
        entities = [MessageEntityUrl(offset=6, length=19)]
        links = extract_external_links(self._msg(text=text, entities=entities))
        assert links == ["https://example.com"]

    def test_text_url_entity_with_tme_filtered_out(self):
        text = "see https://t.me/channel/123 for info"
        entities = [MessageEntityUrl(offset=4, length=24)]
        links = extract_external_links(self._msg(text=text, entities=entities))
        assert links == []

    def test_hyperlink_entity_extracted(self):
        entities = [MessageEntityTextUrl(offset=0, length=4, url="https://example.com")]
        links = extract_external_links(self._msg(text="link", entities=entities))
        assert links == ["https://example.com"]

    def test_hyperlink_tme_filtered_out(self):
        entities = [MessageEntityTextUrl(offset=0, length=4, url="https://t.me/ch/1")]
        links = extract_external_links(self._msg(text="link", entities=entities))
        assert links == []

    def test_duplicate_urls_deduplicated(self):
        text = "https://example.com https://example.com"
        entities = [
            MessageEntityUrl(offset=0, length=19),
            MessageEntityUrl(offset=20, length=19),
        ]
        links = extract_external_links(self._msg(text=text, entities=entities))
        assert links == ["https://example.com"]

    def test_order_of_first_appearance_preserved(self):
        entities = [
            MessageEntityTextUrl(offset=0, length=1, url="https://first.com"),
            MessageEntityTextUrl(offset=2, length=1, url="https://second.com"),
        ]
        links = extract_external_links(self._msg(text="a b", entities=entities))
        assert links == ["https://first.com", "https://second.com"]

    def test_non_url_entities_ignored(self):
        class MessageEntityBold:
            def __init__(self, offset, length):
                self.offset = offset
                self.length = length
        entities = [MessageEntityBold(offset=0, length=5)]
        links = extract_external_links(self._msg(text="hello", entities=entities))
        assert links == []

    def test_mirror_permalink_excluded(self):
        entities = [MessageEntityTextUrl(offset=0, length=4, url="https://abualiexpress.com/heb123456")]
        links = extract_external_links(self._msg(text="link", entities=entities))
        assert links == []

    def test_mirror_permalink_with_comments_fragment_excluded(self):
        entities = [MessageEntityTextUrl(offset=0, length=4, url="https://abualiexpress.com/heb123456#comments")]
        links = extract_external_links(self._msg(text="link", entities=entities))
        assert links == []

    def test_mirror_permalink_with_trailing_slash_excluded(self):
        entities = [MessageEntityTextUrl(offset=0, length=4, url="https://abualiexpress.co.il/heb789/")]
        links = extract_external_links(self._msg(text="link", entities=entities))
        assert links == []

    def test_genuine_external_link_kept(self):
        entities = [MessageEntityTextUrl(offset=0, length=4, url="https://go.amitsegal.co.il/abc123")]
        links = extract_external_links(self._msg(text="link", entities=entities))
        assert links == ["https://go.amitsegal.co.il/abc123"]


# ---------------------------------------------------------------------------
# build_html_page
# ---------------------------------------------------------------------------

class TestBuildHtmlPage:
    END_DATE = datetime(2026, 5, 13, 7, 0, tzinfo=LOCAL_TZ)

    DIGEST = {
        "date_range": "2026-05-13",
        "big_news": [
            {
                "headline": "כותרת גדולה",
                "summary": "סיכום חשוב",
                "links": ["https://t.me/ch/100"],
                "section": "conflict",
                "source": "@ch",
                "time": "06:00",
            }
        ],
        "minor_news": [
            {
                "headline": "כותרת קטנה",
                "links": ["https://t.me/ch/200"],
                "section": "politics",
                "source": "@ch",
                "time": "05:00",
            }
        ],
    }

    SOURCE_MAP = {
        "https://t.me/ch/100": {
            "text": "טקסט מקורי",
            "media_type": None,
            "video_duration": None,
            "external_links": ["https://ynet.co.il/article"],
        },
        "https://t.me/ch/200": {
            "text": "טקסט קטן",
            "media_type": "photo",
            "video_duration": None,
            "external_links": [],
        },
    }

    def _build(self, digest=None, source_map=None):
        return build_html_page(
            digest if digest is not None else self.DIGEST,
            source_map if source_map is not None else self.SOURCE_MAP,
            self.END_DATE,
        )

    def test_returns_doctype_html(self):
        assert self._build().startswith("<!DOCTYPE html>")

    def test_lang_he_dir_rtl(self):
        page = self._build()
        assert 'lang="he"' in page
        assert 'dir="rtl"' in page

    def test_no_external_css_links(self):
        assert "<link" not in self._build()

    def test_style_tag_present(self):
        assert "<style>" in self._build()

    def test_conflict_section_present(self):
        assert "עדכוני לחימה והסכסוך" in self._build()

    def test_politics_section_present(self):
        assert "פוליטיקה ישראלית" in self._build()

    def test_world_section_omitted_when_empty(self):
        assert "כותרות נוספות" not in self._build()

    def test_big_news_headline_as_h4(self):
        assert "<h4>כותרת גדולה</h4>" in self._build()

    def test_big_news_summary_paragraph(self):
        assert "סיכום חשוב" in self._build()

    def test_embed_has_details_element(self):
        assert "<details>" in self._build()

    def test_embed_has_data_telegram_post(self):
        assert 'data-telegram-post="ch/100"' in self._build()

    def test_embed_minor_has_data_telegram_post(self):
        assert 'data-telegram-post="ch/200"' in self._build()

    def test_lazy_load_js_inlined(self):
        page = self._build()
        assert "telegram-widget.js?23" in page
        assert "<script>" in page

    def test_no_kishor_lemakhor_in_big_news(self):
        assert "קישור למקור" not in self._build()

    def test_no_source_bubble_markup(self):
        page = self._build()
        assert "source-bubble" not in page
        assert "פתח בטלגרם" not in page

    def test_minor_news_uses_ul(self):
        assert "<ul" in self._build()

    def test_minor_news_li_is_details(self):
        assert "<li><details>" in self._build()

    def test_multiple_links_produce_multiple_embeds(self):
        digest = {
            "date_range": "2026-05-13",
            "big_news": [
                {
                    "headline": "כותרת",
                    "summary": "סיכום",
                    "links": ["https://t.me/ch/100", "https://t.me/ch2/200"],
                    "section": "conflict",
                    "source": "@ch",
                    "time": "06:00",
                }
            ],
            "minor_news": [],
        }
        page = build_html_page(digest, {}, self.END_DATE)
        assert 'data-telegram-post="ch/100"' in page
        assert 'data-telegram-post="ch2/200"' in page
        assert page.count("<details>") == 1

    def test_multiple_links_unified_into_single_button(self):
        digest = {
            "date_range": "2026-05-13",
            "big_news": [
                {
                    "headline": "כותרת",
                    "summary": "סיכום",
                    "links": ["https://t.me/ch/100", "https://t.me/ch2/200"],
                    "section": "conflict",
                    "source": "@ch",
                    "time": "06:00",
                }
            ],
            "minor_news": [],
        }
        page = build_html_page(digest, {}, self.END_DATE)
        assert "מקורות (2)" in page

    def test_empty_digest_renders_without_sections(self):
        digest = {"date_range": "2026-05-13", "big_news": [], "minor_news": []}
        page = build_html_page(digest, {}, self.END_DATE)
        assert "<!DOCTYPE html>" in page
        assert "עדכוני לחימה" not in page

    def test_further_reading_link_present_when_external_link_in_source_map(self):
        page = self._build()
        assert "להמשך קריאה ←" in page
        assert "https://ynet.co.il/article" in page

    def test_further_reading_link_absent_when_no_external_link(self):
        source_map_no_ext = {
            "https://t.me/ch/100": {
                "text": "טקסט",
                "media_type": None,
                "video_duration": None,
                "external_links": [],
            },
        }
        digest = {
            "date_range": "2026-05-13",
            "big_news": [
                {"headline": "כותרת", "summary": "סיכום", "links": ["https://t.me/ch/100"],
                 "section": "conflict", "source": "@ch", "time": "06:00"},
            ],
            "minor_news": [],
        }
        page = build_html_page(digest, source_map_no_ext, self.END_DATE)
        assert "להמשך קריאה ←" not in page

    def test_minor_news_never_shows_further_reading_link(self):
        source_map_with_ext = {
            "https://t.me/ch/200": {
                "text": "טקסט",
                "media_type": None,
                "video_duration": None,
                "external_links": ["https://external.com/article"],
            },
        }
        digest = {
            "date_range": "2026-05-13",
            "big_news": [],
            "minor_news": [
                {"headline": "כותרת קטנה", "links": ["https://t.me/ch/200"],
                 "section": "politics", "source": "@ch", "time": "05:00"},
            ],
        }
        page = build_html_page(digest, source_map_with_ext, self.END_DATE)
        assert "להמשך קריאה ←" not in page

    def test_sections_in_order(self):
        digest = {
            "date_range": "2026-05-13",
            "big_news": [
                {"headline": "a", "summary": "s", "links": ["https://t.me/ch/1"], "section": "world", "source": "@ch", "time": "06:00"},
                {"headline": "b", "summary": "s", "links": ["https://t.me/ch/2"], "section": "conflict", "source": "@ch", "time": "06:00"},
            ],
            "minor_news": [],
        }
        page = build_html_page(digest, {}, self.END_DATE)
        assert page.index("עדכוני לחימה") < page.index("כותרות נוספות")


# ---------------------------------------------------------------------------
# build_channel_sources — album (grouped_id) de-duplication
# ---------------------------------------------------------------------------

def _tg_msg(msg_id, text="", grouped_id=None, photo=False, video=False,
            file_duration=None, entities=None, dt=None):
    """Minimal Telethon-message stub for build_channel_sources."""
    m = Mock()
    m.id = msg_id
    m.text = text
    m.grouped_id = grouped_id
    m.photo = object() if photo else None
    m.video = object() if video else None
    m.document = None
    m.file = Mock()
    m.file.duration = file_duration
    m.entities = entities
    m.date = dt or datetime(2026, 5, 13, 6, 0, tzinfo=LOCAL_TZ)
    return m


class TestBuildChannelSourcesAlbums:
    # iter_messages yields newest-first; an album's caption commonly sits on the
    # oldest (lowest-id) member, encountered last.
    def _album(self):
        return [
            _tg_msg(102, text="", grouped_id=555, photo=True,
                    dt=datetime(2026, 5, 13, 6, 0, 2, tzinfo=LOCAL_TZ)),
            _tg_msg(101, text="", grouped_id=555, photo=True,
                    dt=datetime(2026, 5, 13, 6, 0, 1, tzinfo=LOCAL_TZ)),
            _tg_msg(100, text="כותרת האלבום", grouped_id=555, photo=True,
                    dt=datetime(2026, 5, 13, 6, 0, 0, tzinfo=LOCAL_TZ)),
        ]

    def test_album_collapses_to_single_source(self):
        strings, source_map = build_channel_sources(self._album(), "ch")
        assert len(source_map) == 1
        assert len(strings) == 1

    def test_album_caption_folded_from_any_member(self):
        _, source_map = build_channel_sources(self._album(), "ch")
        (entry,) = source_map.values()
        assert entry["text"] == "כותרת האלבום"

    def test_album_representative_is_anchor_lowest_id(self):
        # Embedding the album anchor (lowest id) reliably renders the whole album.
        _, source_map = build_channel_sources(self._album(), "ch")
        (link,) = source_map.keys()
        assert link == "https://t.me/ch/100"

    def test_distinct_posts_not_collapsed(self):
        msgs = [
            _tg_msg(200, text="סיפור א", grouped_id=None),
            _tg_msg(199, text="סיפור ב", grouped_id=None),
        ]
        _, source_map = build_channel_sources(msgs, "ch")
        assert len(source_map) == 2

    def test_two_separate_albums_kept_separate(self):
        msgs = [
            _tg_msg(300, text="א", grouped_id=1, photo=True),
            _tg_msg(301, text="", grouped_id=1, photo=True),
            _tg_msg(302, text="ב", grouped_id=2, photo=True),
            _tg_msg(303, text="", grouped_id=2, photo=True),
        ]
        _, source_map = build_channel_sources(msgs, "ch")
        assert len(source_map) == 2

    def test_album_with_video_member_marks_video(self):
        msgs = [
            _tg_msg(400, text="כותרת", grouped_id=9, photo=True),
            _tg_msg(401, text="", grouped_id=9, video=True, file_duration=42),
        ]
        strings, source_map = build_channel_sources(msgs, "ch")
        (entry,) = source_map.values()
        assert entry["media_type"] == "video"
        assert "[VIDEO:" in strings[0]

    def test_text_only_messages_unaffected(self):
        msgs = [_tg_msg(500, text="טקסט בלבד")]
        _, source_map = build_channel_sources(msgs, "ch")
        assert len(source_map) == 1

    def test_empty_service_message_skipped(self):
        msgs = [_tg_msg(600, text="")]  # no text, no media
        strings, source_map = build_channel_sources(msgs, "ch")
        assert strings == []
        assert source_map == {}

    def test_source_map_entry_has_sortable_ts(self):
        _, source_map = build_channel_sources([_tg_msg(700, text="x")], "ch")
        (entry,) = source_map.values()
        assert isinstance(entry["ts"], (int, float))

    def test_message_strings_oldest_first(self):
        msgs = [
            _tg_msg(800, text="חדש", dt=datetime(2026, 5, 13, 8, 0, tzinfo=LOCAL_TZ)),
            _tg_msg(799, text="ישן", dt=datetime(2026, 5, 13, 5, 0, tzinfo=LOCAL_TZ)),
        ]
        strings, _ = build_channel_sources(msgs, "ch")
        assert strings[0].index("ישן") >= 0
        assert "ישן" in strings[0] and "חדש" in strings[1]


# ---------------------------------------------------------------------------
# build_html_page — chronological embed ordering (issue: sort by time)
# ---------------------------------------------------------------------------

class TestEmbedChronologicalOrder:
    END_DATE = datetime(2026, 5, 13, 7, 0, tzinfo=LOCAL_TZ)

    def test_embeds_sorted_earliest_first_across_authors(self):
        # Links listed late->early; later author irrelevant. Expect chronological.
        digest = {
            "date_range": "2026-05-13",
            "big_news": [{
                "headline": "כותרת", "summary": "סיכום",
                "links": ["https://t.me/chB/200", "https://t.me/chA/100"],
                "section": "conflict", "source": "@x", "time": "06:00",
            }],
            "minor_news": [],
        }
        source_map = {
            "https://t.me/chB/200": {"external_links": [], "ts": 2000.0},
            "https://t.me/chA/100": {"external_links": [], "ts": 1000.0},  # earlier
        }
        page = build_html_page(digest, source_map, self.END_DATE)
        assert page.index('data-telegram-post="chA/100"') < page.index('data-telegram-post="chB/200"')

    def test_minor_embeds_also_sorted(self):
        digest = {
            "date_range": "2026-05-13",
            "big_news": [],
            "minor_news": [{
                "headline": "כותרת", "section": "politics", "source": "@x", "time": "05:00",
                "links": ["https://t.me/ch/9", "https://t.me/ch/3"],
            }],
        }
        source_map = {
            "https://t.me/ch/9": {"external_links": [], "ts": 900.0},
            "https://t.me/ch/3": {"external_links": [], "ts": 300.0},  # earlier
        }
        page = build_html_page(digest, source_map, self.END_DATE)
        assert page.index('data-telegram-post="ch/3"') < page.index('data-telegram-post="ch/9"')

    def test_duplicate_links_collapse_to_single_embed(self):
        digest = {
            "date_range": "2026-05-13",
            "big_news": [{
                "headline": "כותרת", "summary": "סיכום",
                "links": ["https://t.me/ch/5", "https://t.me/ch/5"],
                "section": "conflict", "source": "@x", "time": "06:00",
            }],
            "minor_news": [],
        }
        page = build_html_page(digest, {}, self.END_DATE)
        assert page.count('data-telegram-post="ch/5"') == 1
        assert "מקור" in page and "מקורות" not in page  # single source label


# ---------------------------------------------------------------------------
# Channel-of-link parsing helper
# ---------------------------------------------------------------------------

class TestChannelOfLink:
    def test_standard_link(self):
        assert _channel_of_link("https://t.me/abualiexpress/12500") == "abualiexpress"

    def test_unparseable_returns_empty(self):
        assert _channel_of_link("https://example.com/foo") == ""

    def test_garbage_returns_empty(self):
        assert _channel_of_link("not-a-link") == ""


# ---------------------------------------------------------------------------
# Per-channel message stats (top block)
# ---------------------------------------------------------------------------

class TestComputeChannelStats:
    def test_multi_channel_counting(self):
        source_map = {
            "https://t.me/alpha/1": {},
            "https://t.me/alpha/2": {},
            "https://t.me/beta/9": {},
        }
        stats = compute_channel_stats(source_map)
        assert stats["per_channel"] == {"alpha": 2, "beta": 1}
        assert stats["total"] == 3

    def test_total_equals_sum_of_per_channel(self):
        source_map = {
            "https://t.me/alpha/1": {},
            "https://t.me/beta/2": {},
            "https://t.me/beta/3": {},
            "https://t.me/gamma/4": {},
        }
        stats = compute_channel_stats(source_map)
        assert stats["total"] == sum(stats["per_channel"].values())

    def test_ordered_by_descending_count_then_name(self):
        source_map = {
            "https://t.me/zeta/1": {},
            "https://t.me/alpha/1": {},
            "https://t.me/alpha/2": {},
        }
        # alpha (2) before zeta (1)
        assert list(compute_channel_stats(source_map)["per_channel"]) == ["alpha", "zeta"]

    def test_ties_broken_alphabetically(self):
        source_map = {
            "https://t.me/zeta/1": {},
            "https://t.me/alpha/1": {},
        }
        assert list(compute_channel_stats(source_map)["per_channel"]) == ["alpha", "zeta"]

    def test_empty_source_map(self):
        stats = compute_channel_stats({})
        assert stats == {"per_channel": {}, "total": 0}

    def test_unparseable_link_skipped(self):
        source_map = {
            "https://t.me/alpha/1": {},
            "https://example.com/foo": {},
        }
        stats = compute_channel_stats(source_map)
        assert stats["per_channel"] == {"alpha": 1}
        assert stats["total"] == 1


# ---------------------------------------------------------------------------
# Coverage diagnostics (bottom block)
# ---------------------------------------------------------------------------

class TestComputeCoverage:
    def test_one_unreferenced_link_classified_uncovered(self):
        digest = {
            "big_news": [
                {"links": ["https://t.me/alpha/1"], "section": "conflict"},
            ],
            "minor_news": [
                {"links": ["https://t.me/beta/9"], "section": "world"},
            ],
        }
        source_map = {
            "https://t.me/alpha/1": {"ts": 1.0},
            "https://t.me/beta/9": {"ts": 2.0},
            "https://t.me/beta/10": {"ts": 3.0},  # never referenced
        }
        cov = compute_coverage(digest, source_map)
        assert cov["total"] == 3
        assert cov["covered"] == 2
        assert cov["uncovered"] == ["https://t.me/beta/10"]
        # covered + uncovered == total, no double counting
        assert cov["covered"] + len(cov["uncovered"]) == cov["total"]

    def test_per_channel_covered_total(self):
        digest = {
            "big_news": [{"links": ["https://t.me/alpha/1"], "section": "conflict"}],
            "minor_news": [],
        }
        source_map = {
            "https://t.me/alpha/1": {"ts": 1.0},
            "https://t.me/alpha/2": {"ts": 2.0},  # uncovered
        }
        cov = compute_coverage(digest, source_map)
        assert cov["per_channel"]["alpha"] == {"covered": 1, "total": 2}

    def test_full_coverage(self):
        digest = {
            "big_news": [{"links": ["https://t.me/alpha/1"], "section": "conflict"}],
            "minor_news": [{"links": ["https://t.me/beta/9"], "section": "world"}],
        }
        source_map = {
            "https://t.me/alpha/1": {"ts": 1.0},
            "https://t.me/beta/9": {"ts": 2.0},
        }
        cov = compute_coverage(digest, source_map)
        assert cov["covered"] == cov["total"] == 2
        assert cov["uncovered"] == []

    def test_legacy_single_link_field_counts_as_covered(self):
        digest = {
            "big_news": [{"link": "https://t.me/alpha/1", "section": "conflict"}],
            "minor_news": [],
        }
        source_map = {"https://t.me/alpha/1": {"ts": 1.0}}
        cov = compute_coverage(digest, source_map)
        assert cov["covered"] == 1
        assert cov["uncovered"] == []

    def test_uncovered_ordered_by_timestamp(self):
        digest = {"big_news": [], "minor_news": []}
        source_map = {
            "https://t.me/alpha/9": {"ts": 900.0},
            "https://t.me/alpha/3": {"ts": 300.0},  # earlier
        }
        cov = compute_coverage(digest, source_map)
        assert cov["uncovered"] == ["https://t.me/alpha/3", "https://t.me/alpha/9"]

    def test_empty_source_map(self):
        cov = compute_coverage({"big_news": [], "minor_news": []}, {})
        assert cov["covered"] == 0
        assert cov["total"] == 0
        assert cov["per_channel"] == {}
        assert cov["uncovered"] == []


# ---------------------------------------------------------------------------
# Rendered HTML: stats header + coverage footer
# ---------------------------------------------------------------------------

class TestHtmlStatsAndCoverageBlocks:
    END_DATE = datetime(2026, 5, 13, 7, 0, tzinfo=LOCAL_TZ)

    DIGEST = {
        "date_range": "2026-05-13",
        "big_news": [
            {
                "headline": "כותרת גדולה", "summary": "סיכום",
                "links": ["https://t.me/alpha/1"],
                "section": "conflict", "source": "@alpha", "time": "06:00",
            }
        ],
        "minor_news": [
            {
                "headline": "כותרת קטנה",
                "links": ["https://t.me/beta/9"],
                "section": "world", "source": "@beta", "time": "05:00",
            }
        ],
    }

    def _source_map(self, with_uncovered):
        sm = {
            "https://t.me/alpha/1": {"text": "טקסט אלפא", "ts": 1.0, "time": "06:00", "external_links": []},
            "https://t.me/beta/9": {"text": "טקסט בטא", "ts": 2.0, "time": "05:00", "external_links": []},
        }
        if with_uncovered:
            sm["https://t.me/beta/10"] = {"text": "הודעה לא מסוקרת", "ts": 3.0, "time": "07:00", "external_links": []}
        return sm

    def test_channel_stats_block_present_at_top(self):
        page = build_html_page(self.DIGEST, self._source_map(False), self.END_DATE)
        assert 'class="channel-stats"' in page
        # appears after header, before main
        assert page.index('class="channel-stats"') > page.index("<header>")
        assert page.index('class="channel-stats"') < page.index("<main>")

    def test_channel_stats_per_channel_lines_and_total(self):
        page = build_html_page(self.DIGEST, self._source_map(False), self.END_DATE)
        assert "@alpha — 1 הודעות" in page
        assert "@beta — 1 הודעות" in page
        assert 'class="stats-total"' in page
        assert "2 הודעות" in page  # total

    def test_diagnostics_block_present_at_bottom(self):
        page = build_html_page(self.DIGEST, self._source_map(False), self.END_DATE)
        assert 'class="diagnostics"' in page
        assert page.index('class="diagnostics"') > page.index("</main>")

    def test_all_covered_statement_when_full(self):
        page = build_html_page(self.DIGEST, self._source_map(False), self.END_DATE)
        assert "כל ההודעות סוקרו" in page
        assert "סוקרו 2 מתוך 2 הודעות" in page

    def test_uncovered_details_lists_clickable_link(self):
        page = build_html_page(self.DIGEST, self._source_map(True), self.END_DATE)
        assert "<details>" in page
        assert "1 הודעות שלא סוקרו" in page
        assert 'href="https://t.me/beta/10"' in page
        assert "סוקרו 2 מתוך 3 הודעות" in page

    def test_blocks_omitted_for_empty_source_map(self):
        page = build_html_page(self.DIGEST, {}, self.END_DATE)
        assert 'class="channel-stats"' not in page
        assert 'class="diagnostics"' not in page


# ---------------------------------------------------------------------------
# Integration helpers
# ---------------------------------------------------------------------------

def _async_iter_factory(messages):
    """Returns a side_effect for iter_messages: fresh async generator per call."""
    async def _gen(*args, **kwargs):
        for m in messages:
            yield m
    return lambda *a, **kw: _gen()


def _mock_tg_client(messages):
    """TelegramClient mock where only the two network calls are faked."""
    client = AsyncMock()
    entity = Mock()
    entity.title = "Test Channel"
    client.get_entity = AsyncMock(return_value=entity)
    client.iter_messages = MagicMock(side_effect=_async_iter_factory(messages))
    return client


def _anthropic_stub(big_news=None, minor_news=None, date_range="2026-05-13 10:00 - 2026-05-13 12:00 Israel"):
    """Stub Anthropic response containing a single publish_digest tool_use block."""
    block = Mock()
    block.type = "tool_use"
    block.input = {
        "date_range": date_range,
        "big_news": list(big_news or []),
        "minor_news": list(minor_news or []),
    }
    resp = Mock()
    resp.content = [block]
    resp.stop_reason = "tool_use"
    resp.usage = Mock(output_tokens=1234)
    return resp


# ---------------------------------------------------------------------------
# fetch_messages integration (mocks only Telethon network calls)
# ---------------------------------------------------------------------------

class TestFetchMessagesIntegration:
    START = datetime(2026, 5, 13, 8, 0, tzinfo=UTC)
    END   = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    IN_WINDOW = datetime(2026, 5, 13, 10, 0, tzinfo=UTC)

    def test_single_message_produces_string_and_source_entry(self):
        msg = _tg_msg(100, text="כותרת", dt=self.IN_WINDOW)
        strings, source_map = asyncio.run(
            fetch_messages(_mock_tg_client([msg]), "ch", self.START, self.END)
        )
        assert len(strings) == 1
        assert "https://t.me/ch/100" in source_map
        assert "https://t.me/ch/100" in strings[0]

    def test_message_before_window_is_excluded(self):
        msg = _tg_msg(99, text="ישן", dt=datetime(2026, 5, 13, 6, 0, tzinfo=UTC))
        strings, _ = asyncio.run(
            fetch_messages(_mock_tg_client([msg]), "ch", self.START, self.END)
        )
        assert strings == []

    def test_album_collapses_through_build_channel_sources(self):
        album = [
            _tg_msg(102, text="", grouped_id=555, photo=True, dt=self.IN_WINDOW),
            _tg_msg(101, text="", grouped_id=555, photo=True, dt=self.IN_WINDOW),
            _tg_msg(100, text="כותרת האלבום", grouped_id=555, photo=True, dt=self.IN_WINDOW),
        ]
        strings, source_map = asyncio.run(
            fetch_messages(_mock_tg_client(album), "ch", self.START, self.END)
        )
        assert len(source_map) == 1
        assert len(strings) == 1
        assert "https://t.me/ch/100" in source_map  # anchor = lowest id

    def test_source_map_entry_has_required_fields(self):
        msg = _tg_msg(100, text="טקסט", dt=self.IN_WINDOW)
        _, source_map = asyncio.run(
            fetch_messages(_mock_tg_client([msg]), "ch", self.START, self.END)
        )
        entry = source_map["https://t.me/ch/100"]
        for field in ("text", "media_type", "ts", "time", "external_links"):
            assert field in entry

    def test_iter_messages_called_with_end_date_as_offset(self):
        client = _mock_tg_client([])
        asyncio.run(fetch_messages(client, "ch", self.START, self.END))
        _, kwargs = client.iter_messages.call_args
        assert kwargs.get("offset_date") == self.END

    def test_get_entity_error_returns_empty(self):
        client = AsyncMock()
        client.get_entity = AsyncMock(side_effect=Exception("not found"))
        strings, source_map = asyncio.run(
            fetch_messages(client, "ch", self.START, self.END)
        )
        assert strings == []
        assert source_map == {}

    def test_message_strings_oldest_first(self):
        newer = _tg_msg(200, text="חדש", dt=datetime(2026, 5, 13, 11, 0, tzinfo=UTC))
        older = _tg_msg(100, text="ישן",  dt=datetime(2026, 5, 13, 9,  0, tzinfo=UTC))
        strings, _ = asyncio.run(
            fetch_messages(_mock_tg_client([newer, older]), "ch", self.START, self.END)
        )
        assert len(strings) == 2
        assert "ישן"  in strings[0]
        assert "חדש" in strings[1]


# ---------------------------------------------------------------------------
# create_digest integration (mocks only Anthropic API call)
# ---------------------------------------------------------------------------

class TestCreateDigestIntegration:
    START = datetime(2026, 5, 13, 8,  0, tzinfo=UTC)
    END   = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)

    def _patch_ac(self, response):
        mock_ac = AsyncMock()
        inner = MagicMock()
        inner.get_final_message = AsyncMock(return_value=response)
        stream_cm = MagicMock()
        stream_cm.__aenter__ = AsyncMock(return_value=inner)
        stream_cm.__aexit__ = AsyncMock(return_value=False)
        mock_ac.messages.stream = MagicMock(return_value=stream_cm)
        return patch('digest.anthropic.AsyncAnthropic', return_value=mock_ac), mock_ac

    def test_returns_normalized_digest_on_success(self):
        resp = _anthropic_stub(big_news=[{
            "headline": "כותרת", "summary": "סיכום",
            "links": ["https://t.me/ch/1"],
            "section": "conflict", "source": "@ch", "time": "08:00",
        }])
        p, _ = self._patch_ac(resp)
        with p:
            result = asyncio.run(create_digest(
                {"ch": ["[08:00] כותרת\nLink: https://t.me/ch/1"]}, self.START, self.END
            ))
        assert result is not None
        assert result["big_news"][0]["headline"] == "כותרת"
        assert result["big_news"][0]["links"] == ["https://t.me/ch/1"]

    def test_empty_channel_dict_returns_none_without_api_call(self):
        p, mock_ac = self._patch_ac(_anthropic_stub())
        with p:
            result = asyncio.run(create_digest({}, self.START, self.END))
        assert result is None
        mock_ac.messages.stream.assert_not_called()

    def test_no_tool_use_block_returns_none(self):
        resp = Mock()
        resp.content = []
        resp.stop_reason = "end_turn"
        resp.usage = Mock(output_tokens=10)
        p, _ = self._patch_ac(resp)
        with p:
            result = asyncio.run(create_digest({"ch": ["msg"]}, self.START, self.END))
        assert result is None

    def test_api_called_with_publish_digest_tool(self):
        p, mock_ac = self._patch_ac(_anthropic_stub())
        with p:
            asyncio.run(create_digest({"ch": ["msg"]}, self.START, self.END))
        kwargs = mock_ac.messages.stream.call_args.kwargs
        assert kwargs["tool_choice"] == {"type": "tool", "name": "publish_digest"}

    def test_channel_messages_appear_in_prompt(self):
        p, mock_ac = self._patch_ac(_anthropic_stub())
        with p:
            asyncio.run(create_digest(
                {"mychannel": ["[08:00] חדשות חשובות\nLink: https://t.me/mychannel/5"]},
                self.START, self.END,
            ))
        user_content = mock_ac.messages.stream.call_args.kwargs["messages"][0]["content"]
        assert "@mychannel" in user_content
        assert "חדשות חשובות" in user_content

    def test_normalize_digest_applied_to_response(self):
        """legacy link field promoted to links[] via the real normalize_digest path."""
        resp = _anthropic_stub(big_news=[{
            "headline": "כ", "summary": "ס",
            "link": "https://t.me/ch/1",  # singular — should be promoted
            "section": "conflict", "source": "@ch", "time": "08:00",
        }])
        p, _ = self._patch_ac(resp)
        with p:
            result = asyncio.run(create_digest({"ch": ["msg"]}, self.START, self.END))
        item = result["big_news"][0]
        assert "link" not in item
        assert item["links"] == ["https://t.me/ch/1"]

    def test_truncated_response_returns_none(self):
        resp = _anthropic_stub(big_news=[{
            "headline": "כותרת", "summary": "סיכום",
            "links": ["https://t.me/ch/1"],
            "section": "conflict", "source": "@ch", "time": "08:00",
        }])
        resp.stop_reason = "max_tokens"
        p, _ = self._patch_ac(resp)
        with p:
            result = asyncio.run(create_digest({"ch": ["msg"]}, self.START, self.END))
        assert result is None

    def test_near_limit_diagnostics_true(self):
        resp = _anthropic_stub(big_news=[{
            "headline": "כותרת", "summary": "סיכום",
            "links": ["https://t.me/ch/1"],
            "section": "conflict", "source": "@ch", "time": "08:00",
        }])
        resp.usage = Mock(output_tokens=50000)
        p, _ = self._patch_ac(resp)
        with p:
            result = asyncio.run(create_digest({"ch": ["msg"]}, self.START, self.END))
        assert result["_diagnostics"]["near_limit"] is True

    def test_near_limit_diagnostics_false(self):
        resp = _anthropic_stub(big_news=[{
            "headline": "כותרת", "summary": "סיכום",
            "links": ["https://t.me/ch/1"],
            "section": "conflict", "source": "@ch", "time": "08:00",
        }])
        resp.usage = Mock(output_tokens=1234)
        p, _ = self._patch_ac(resp)
        with p:
            result = asyncio.run(create_digest({"ch": ["msg"]}, self.START, self.END))
        assert result["_diagnostics"]["near_limit"] is False

    def test_html_warning_rendered_when_near_limit(self):
        digest = {
            "date_range": "2026-05-13",
            "big_news": [{
                "headline": "כותרת", "summary": "סיכום",
                "links": ["https://t.me/ch/1"],
                "section": "conflict", "source": "@ch", "time": "08:00",
            }],
            "minor_news": [],
            "_diagnostics": {"output_tokens": 50000, "max_output_tokens": 64000, "near_limit": True},
        }
        source_map = {"https://t.me/ch/1": {"text": "כותרת", "media_type": None,
                                            "video_duration": None, "external_links": [],
                                            "time": "08:00", "ts": 1.0}}
        page = build_html_page(digest, source_map, self.END)
        assert "מגבלת המודל" in page

    def test_html_warning_absent_without_diagnostics(self):
        digest = {
            "date_range": "2026-05-13",
            "big_news": [{
                "headline": "כותרת", "summary": "סיכום",
                "links": ["https://t.me/ch/1"],
                "section": "conflict", "source": "@ch", "time": "08:00",
            }],
            "minor_news": [],
        }
        source_map = {"https://t.me/ch/1": {"text": "כותרת", "media_type": None,
                                            "video_duration": None, "external_links": [],
                                            "time": "08:00", "ts": 1.0}}
        page = build_html_page(digest, source_map, self.END)
        assert "מגבלת המודל" not in page


# ---------------------------------------------------------------------------
# main() pipeline (mocks TelegramClient + Anthropic at the network boundary)
# ---------------------------------------------------------------------------

class TestMainPipeline:
    # Explicit window passed via --startdate/--enddate so tests are date-independent.
    IN_WINDOW   = datetime(2026, 5, 13, 10, 0, tzinfo=UTC)
    _DATE_ARGS  = ['--startdate', '2026-05-13 08:00', '--enddate', '2026-05-13 12:00']

    def _setup(self, messages, big_news=None):
        mock_tg = _mock_tg_client(messages)
        resp = _anthropic_stub(big_news=big_news or [{
            "headline": "כותרת", "summary": "סיכום",
            "links": ["https://t.me/ch/100"],
            "section": "conflict", "source": "@ch", "time": "10:00",
        }])
        mock_ac = AsyncMock()
        inner = MagicMock()
        inner.get_final_message = AsyncMock(return_value=resp)
        stream_cm = MagicMock()
        stream_cm.__aenter__ = AsyncMock(return_value=inner)
        stream_cm.__aexit__ = AsyncMock(return_value=False)
        mock_ac.messages.stream = MagicMock(return_value=stream_cm)
        return mock_tg, mock_ac

    def test_html_written_with_digest_content(self, tmp_path):
        msg = _tg_msg(100, text="חדשות", dt=self.IN_WINDOW)
        mock_tg, mock_ac = self._setup([msg])
        output = str(tmp_path / "out.html")

        with patch('digest.TelegramClient', return_value=mock_tg), \
             patch('digest.anthropic.AsyncAnthropic', return_value=mock_ac), \
             patch('sys.argv', ['digest.py', '--dry-run', '--output', output] + self._DATE_ARGS):
            asyncio.run(main())

        html = (tmp_path / "out.html").read_text()
        assert "<!DOCTYPE html>" in html
        assert "כותרת" in html

    def test_no_messages_skips_claude_and_does_not_write_html(self, tmp_path):
        mock_tg, mock_ac = self._setup([])  # iter_messages yields nothing
        output = str(tmp_path / "out.html")

        with patch('digest.TelegramClient', return_value=mock_tg), \
             patch('digest.anthropic.AsyncAnthropic', return_value=mock_ac), \
             patch('sys.argv', ['digest.py', '--dry-run', '--output', output] + self._DATE_ARGS):
            asyncio.run(main())

        mock_ac.messages.stream.assert_not_called()
        assert not (tmp_path / "out.html").exists()

    def test_send_message_called_when_not_dry_run(self, tmp_path):
        msg = _tg_msg(100, text="חדשות", dt=self.IN_WINDOW)
        mock_tg, mock_ac = self._setup([msg])
        output = str(tmp_path / "out.html")

        with patch('digest.TelegramClient', return_value=mock_tg), \
             patch('digest.anthropic.AsyncAnthropic', return_value=mock_ac), \
             patch('sys.argv', ['digest.py', '--output', output] + self._DATE_ARGS):
            asyncio.run(main())

        mock_tg.send_message.assert_called_once()
