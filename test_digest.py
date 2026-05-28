import json
import pytest
from datetime import datetime
from unittest.mock import Mock
from pytz import UTC, timezone

from digest import (
    normalize_digest,
    time_of_day_label,
    format_telegram_message,
    build_telegraph_content,
    _meta_text,
    _deep_item_node,
    _section_nodes,
    extract_media_info,
    extract_external_links,
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
# _meta_text
# ---------------------------------------------------------------------------

class TestMetaText:
    def test_source_and_time(self):
        assert _meta_text({"source": "@ch", "time": "07:00"}) == "@ch | 07:00"

    def test_source_only(self):
        assert _meta_text({"source": "@ch"}) == "@ch"

    def test_time_only(self):
        assert _meta_text({"time": "07:00"}) == "07:00"

    def test_empty_dict(self):
        assert _meta_text({}) == ""

    def test_empty_string_values_excluded(self):
        assert _meta_text({"source": "", "time": ""}) == ""


# ---------------------------------------------------------------------------
# _deep_item_node
# ---------------------------------------------------------------------------

class TestDeepItemNode:
    def test_tag_is_p(self):
        node = _deep_item_node({"headline": "כתבה", "links": ["https://t.me/x/1"], "source": "@ch", "time": "06:00"})
        assert node["tag"] == "p"

    def test_headline_in_children(self):
        node = _deep_item_node({"headline": "כתבה", "links": ["https://t.me/x/1"], "source": "@ch", "time": "06:00"})
        assert any("כתבה" in str(c) for c in node["children"])

    def test_link_produces_anchor(self):
        node = _deep_item_node({"headline": "כתבה", "links": ["https://t.me/x/1"], "source": "@ch", "time": "06:00"})
        assert any(isinstance(c, dict) and c.get("tag") == "a" for c in node["children"])

    def test_multiple_links_produce_multiple_anchors(self):
        node = _deep_item_node({"headline": "כתבה", "links": ["https://t.me/x/1", "https://t.me/x/2"], "source": "@ch", "time": "06:00"})
        anchors = [c for c in node["children"] if isinstance(c, dict) and c.get("tag") == "a"]
        assert len(anchors) == 2

    def test_without_links_no_anchor(self):
        node = _deep_item_node({"headline": "כתבה", "links": [], "source": "@ch", "time": "06:00"})
        assert not any(isinstance(c, dict) and c.get("tag") == "a" for c in node["children"])


# ---------------------------------------------------------------------------
# _section_nodes
# ---------------------------------------------------------------------------

class TestSectionNodes:
    def _big(self, **kw):
        return {"headline": "כותרת", "summary": "סיכום", "links": ["https://t.me/x/1"],
                "source": "@ch", "time": "06:00", **kw}

    def _minor(self, **kw):
        return {"headline": "כותרת קטנה", "links": ["https://t.me/x/2"],
                "source": "@ch", "time": "05:00", **kw}

    def test_empty_inputs_returns_empty(self):
        assert _section_nodes([], []) == []

    def test_big_item_produces_h4(self):
        nodes = _section_nodes([self._big()], [])
        assert any(n["tag"] == "h4" and n["children"] == ["כותרת"] for n in nodes)

    def test_big_item_produces_summary_paragraph(self):
        nodes = _section_nodes([self._big()], [])
        p_texts = [n["children"][0] for n in nodes if n["tag"] == "p" and isinstance(n["children"][0], str)]
        assert "סיכום" in p_texts

    def test_big_item_no_summary_skips_paragraph(self):
        nodes = _section_nodes([self._big(summary="")], [])
        p_texts = [n["children"][0] for n in nodes if n["tag"] == "p" and isinstance(n["children"][0], str)]
        assert "" not in p_texts

    def test_minor_items_produce_subheading(self):
        nodes = _section_nodes([], [self._minor()])
        h4_texts = [n["children"][0] for n in nodes if n["tag"] == "h4"]
        assert "עוד עדכונים" in h4_texts

    def test_minor_items_produce_ul(self):
        nodes = _section_nodes([], [self._minor()])
        assert any(n["tag"] == "ul" for n in nodes)

    def test_no_minor_no_subheading(self):
        nodes = _section_nodes([self._big()], [])
        h4_texts = [n["children"][0] for n in nodes if n["tag"] == "h4"]
        assert "עוד עדכונים" not in h4_texts

    def test_deep_returns_only_p_tags(self):
        nodes = _section_nodes([self._big()], [self._minor()], is_deep=True)
        assert all(n["tag"] == "p" for n in nodes)


# ---------------------------------------------------------------------------
# build_telegraph_content
# ---------------------------------------------------------------------------

class TestBuildTelegraphContent:
    DIGEST = {
        "date_range": "...",
        "big_news": [
            {"headline": "לחימה", "summary": "סיכום", "links": ["https://t.me/x/1"],
             "section": "conflict", "source": "@ch", "time": "06:00"},
            {"headline": "ניתוח", "summary": "", "links": ["https://t.me/x/3"],
             "section": "deep", "source": "@ch", "time": "04:00"},
        ],
        "minor_news": [
            {"headline": "פוליטיקה", "links": ["https://t.me/x/2"],
             "section": "politics", "source": "@ch", "time": "05:00"},
        ],
    }

    def _h3_texts(self, content):
        return [n["children"][0] for n in content if n["tag"] == "h3"]

    def test_conflict_section_present(self):
        assert "עדכוני לחימה והסכסוך" in self._h3_texts(build_telegraph_content(self.DIGEST))

    def test_politics_section_present(self):
        assert "פוליטיקה ישראלית" in self._h3_texts(build_telegraph_content(self.DIGEST))

    def test_deep_section_present(self):
        assert "לקריאה נוספת" in self._h3_texts(build_telegraph_content(self.DIGEST))

    def test_empty_section_omitted(self):
        assert "כותרות נוספות" not in self._h3_texts(build_telegraph_content(self.DIGEST))

    def test_empty_digest_returns_empty(self):
        assert build_telegraph_content({"big_news": [], "minor_news": []}) == []


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
