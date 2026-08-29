import asyncio
import json
import logging
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from pytz import UTC, timezone

from digest import (
    DigestParseError,
    normalize_digest,
    time_of_day_label,
    format_telegram_message,
    build_html_page,
    build_channel_sources,
    compute_channel_stats,
    compute_coverage,
    classify_ad_messages,
    _channel_of_link,
    extract_media_info,
    extract_external_links,
    fetch_messages,
    create_digest,
    main,
    DIGEST_TOOL,
    send_alert,
    check_digest_health,
    format_window,
    format_failure_alert,
    format_health_alert,
    _coerce_chat_id,
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

    def test_invalid_json_string_raises_instead_of_dropping_section(self):
        """Regression for the 2026-08-28 incident: an unparseable array field used
        to become [], gutting the digest, which was then published silently."""
        data = {"date_range": "...", "big_news": "not valid json {{{", "minor_news": []}
        with pytest.raises(DigestParseError):
            normalize_digest(data)

    def test_parse_error_names_the_field_and_shows_the_value(self):
        data = {"date_range": "...", "big_news": "not valid json {{{", "minor_news": []}
        with pytest.raises(DigestParseError) as exc:
            normalize_digest(data)
        assert "big_news" in str(exc.value)
        assert "not valid json" in str(exc.value)

    def test_parse_error_truncates_a_huge_offending_value(self):
        data = {"date_range": "...", "big_news": "x" * 5000, "minor_news": []}
        with pytest.raises(DigestParseError) as exc:
            normalize_digest(data)
        message = str(exc.value)
        assert len(message) < 1000
        assert "5000 chars total" in message

    def test_unparseable_minor_news_also_raises(self):
        data = {"date_range": "...", "big_news": [], "minor_news": "{oops"}
        with pytest.raises(DigestParseError) as exc:
            normalize_digest(data)
        assert "minor_news" in str(exc.value)

    def test_non_list_json_string_raises(self):
        """Valid JSON that isn't an array is just as unusable as invalid JSON."""
        data = {"date_range": "...", "big_news": '{"headline": "h"}', "minor_news": []}
        with pytest.raises(DigestParseError) as exc:
            normalize_digest(data)
        assert "big_news" in str(exc.value)

    def test_parse_error_logged_at_error_level(self, caplog):
        data = {"date_range": "...", "big_news": "not valid json {{{", "minor_news": []}
        with caplog.at_level(logging.ERROR):
            with pytest.raises(DigestParseError):
                normalize_digest(data)
        assert any(r.levelno == logging.ERROR and "big_news" in r.getMessage() for r in caplog.records)

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

# ---------------------------------------------------------------------------
# Strict tool use: the schema must stay inside the grammar-constrained subset
# ---------------------------------------------------------------------------

class TestDigestToolStrictSchema:
    """Guards the `strict: True` contract on DIGEST_TOOL.

    Strict tool use compiles input_schema into a sampling grammar, which is what
    makes a stringified `big_news` structurally impossible (the 2026-08-28 and
    2026-09-11 incidents). The API rejects a schema outside the supported
    subset, so a well-meaning schema edit would break every run at request time
    rather than in review. These tests fail instead.
    """

    # Keywords the strict subset does not accept.
    UNSUPPORTED = {
        "maxItems", "minLength", "maxLength", "pattern", "format", "oneOf",
        "allOf", "anyOf", "not", "$ref", "$defs", "multipleOf", "minimum",
        "maximum", "exclusiveMinimum", "exclusiveMaximum", "uniqueItems",
        "patternProperties", "propertyNames", "const", "if", "then", "else",
    }

    @staticmethod
    def _objects(node, path="root"):
        """Yield (path, node) for every object-typed node in the schema."""
        if isinstance(node, dict):
            if node.get("type") == "object":
                yield path, node
            for key, value in node.get("properties", {}).items():
                yield from TestDigestToolStrictSchema._objects(value, f"{path}.{key}")
            if "items" in node:
                yield from TestDigestToolStrictSchema._objects(node["items"], f"{path}[]")

    @staticmethod
    def _keywords(node):
        seen = set()
        if isinstance(node, dict):
            seen |= set(node.keys())
            for value in node.values():
                seen |= TestDigestToolStrictSchema._keywords(value)
        elif isinstance(node, list):
            for value in node:
                seen |= TestDigestToolStrictSchema._keywords(value)
        return seen

    def test_strict_flag_is_set(self):
        assert DIGEST_TOOL.get("strict") is True

    def test_every_object_forbids_additional_properties(self):
        objects = dict(self._objects(DIGEST_TOOL["input_schema"]))
        assert objects, "expected at least the root object"
        offenders = [p for p, n in objects.items() if n.get("additionalProperties") is not False]
        assert not offenders, f"additionalProperties must be False on: {offenders}"

    def test_root_and_both_item_objects_are_covered(self):
        assert set(dict(self._objects(DIGEST_TOOL["input_schema"]))) == {
            "root", "root.big_news[]", "root.minor_news[]",
        }

    def test_every_object_property_is_required(self):
        """Optional properties are capped at 24 across all strict schemas; we use none."""
        for path, node in self._objects(DIGEST_TOOL["input_schema"]):
            assert set(node.get("required", [])) == set(node.get("properties", {})), path

    def test_no_unsupported_json_schema_keywords(self):
        used = self._keywords(DIGEST_TOOL["input_schema"])
        assert not (used & self.UNSUPPORTED), f"unsupported in strict mode: {sorted(used & self.UNSUPPORTED)}"

    def test_min_items_only_uses_supported_values(self):
        """The strict subset allows minItems of 0 or 1 only."""

        def walk(node):
            if isinstance(node, dict):
                if "minItems" in node:
                    assert node["minItems"] in (0, 1), node["minItems"]
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(DIGEST_TOOL["input_schema"])


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

    def test_minor_news_summary_has_chevron_after(self):
        assert "ul.minor-news li > details > summary::after" in self._build()

    def test_minor_news_summary_open_state_rule_present(self):
        assert "ul.minor-news li > details[open] > summary::after" in self._build()

    def test_minor_news_chevron_uses_logical_inline_positioning(self):
        page = self._build()
        style_start = page.index("<style>")
        style_end = page.index("</style>")
        css = page[style_start:style_end]
        assert "inset-inline-start" in css or "padding-inline-start" in css
        for line in css.splitlines():
            if "ul.minor-news li > details > summary" in line:
                assert "left:" not in line
                assert "padding-left" not in line

    def test_minor_news_webkit_marker_suppressed(self):
        assert "ul.minor-news li > details > summary::-webkit-details-marker" in self._build()

    def test_minor_news_markup_unchanged(self):
        page = self._build()
        assert "<li><details><summary>כותרת קטנה</summary>" in page

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

class TestAdMessageClassification:
    """Ad Message detection (CONTEXT.md): disclosure marker + the body after it."""

    MARKER = "**°תוכן שיווקי**"
    POLITICAL_MARKER = "**°תוכן פוליטי במימון המטה הלאומי של פורום הניצחון**"
    AD_BODY = "🌙 חלומות מתוקים מתחילים עם כרית קמומיל לילדים 🌼"

    def test_marker_message_is_ad(self):
        source_map = {"https://t.me/abualiexpress/1": {"text": self.MARKER, "ts": 1.0}}
        assert classify_ad_messages(source_map) == {"https://t.me/abualiexpress/1"}

    def test_political_funding_marker_is_ad(self):
        source_map = {"https://t.me/abualiexpress/1": {"text": self.POLITICAL_MARKER, "ts": 1.0}}
        assert classify_ad_messages(source_map) == {"https://t.me/abualiexpress/1"}

    def test_marker_and_following_body_are_both_ads(self):
        source_map = {
            "https://t.me/abualiexpress/1": {"text": self.MARKER, "ts": 1.0},
            "https://t.me/abualiexpress/2": {"text": self.AD_BODY, "ts": 2.0},
        }
        assert classify_ad_messages(source_map) == {
            "https://t.me/abualiexpress/1",
            "https://t.me/abualiexpress/2",
        }

    def test_adjacency_does_not_chain_to_third_message(self):
        source_map = {
            "https://t.me/abualiexpress/1": {"text": self.MARKER, "ts": 1.0},
            "https://t.me/abualiexpress/2": {"text": self.AD_BODY, "ts": 2.0},
            "https://t.me/abualiexpress/3": {"text": "דובר צה\"ל: תקיפה בג'נין", "ts": 3.0},
        }
        assert "https://t.me/abualiexpress/3" not in classify_ad_messages(source_map)

    def test_adjacency_does_not_cross_channels(self):
        source_map = {
            "https://t.me/abualiexpress/1": {"text": self.MARKER, "ts": 1.0},
            "https://t.me/amitsegal/5": {"text": "ידיעה פוליטית", "ts": 2.0},
        }
        assert classify_ad_messages(source_map) == {"https://t.me/abualiexpress/1"}

    def test_adjacency_uses_timestamp_order_not_dict_order(self):
        source_map = {
            "https://t.me/abualiexpress/2": {"text": self.AD_BODY, "ts": 2.0},
            "https://t.me/abualiexpress/1": {"text": self.MARKER, "ts": 1.0},
        }
        assert "https://t.me/abualiexpress/2" in classify_ad_messages(source_map)

    def test_message_before_marker_is_not_an_ad(self):
        source_map = {
            "https://t.me/abualiexpress/1": {"text": "ידיעה אמיתית", "ts": 1.0},
            "https://t.me/abualiexpress/2": {"text": self.MARKER, "ts": 2.0},
        }
        assert classify_ad_messages(source_map) == {"https://t.me/abualiexpress/2"}

    def test_plain_news_message_is_not_an_ad(self):
        source_map = {
            "https://t.me/amitsegal/1": {"text": "הכלל פשוט: 15 גברים ייכנסו.", "ts": 1.0},
        }
        assert classify_ad_messages(source_map) == set()

    def test_marker_words_deep_in_body_are_ignored(self):
        # A real story that merely mentions marketing far past the opening.
        text = "כתבה ארוכה על שוק הפרסום. " * 10 + "תוכן שיווקי הוא מנוע צמיחה."
        source_map = {"https://t.me/amitsegal/1": {"text": text, "ts": 1.0}}
        assert classify_ad_messages(source_map) == set()

    def test_missing_and_empty_text_is_not_an_ad(self):
        source_map = {
            "https://t.me/alpha/1": {"ts": 1.0},
            "https://t.me/alpha/2": {"text": "", "ts": 2.0},
            "https://t.me/alpha/3": {"text": None, "ts": 3.0},
        }
        assert classify_ad_messages(source_map) == set()

    def test_empty_source_map(self):
        assert classify_ad_messages({}) == set()


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
        assert cov["uncovered_ads"] == []
        assert cov["uncovered_real"] == []
        assert cov["ads"] == 0
        assert cov["real_covered"] == 0
        assert cov["real_total"] == 0

    def test_uncovered_split_into_ads_and_real(self):
        digest = {"big_news": [], "minor_news": []}
        source_map = {
            "https://t.me/abualiexpress/1": {"text": "**°תוכן שיווקי**", "ts": 1.0},
            "https://t.me/abualiexpress/2": {"text": "כרית קמומיל לילדים", "ts": 2.0},
            "https://t.me/abualiexpress/3": {"text": "תקיפה בג'נין", "ts": 3.0},
        }
        cov = compute_coverage(digest, source_map)
        assert cov["uncovered_ads"] == [
            "https://t.me/abualiexpress/1",
            "https://t.me/abualiexpress/2",
        ]
        assert cov["uncovered_real"] == ["https://t.me/abualiexpress/3"]
        # the split partitions uncovered exactly
        assert len(cov["uncovered_ads"]) + len(cov["uncovered_real"]) == len(cov["uncovered"])

    def test_raw_coverage_numbers_unchanged_by_ads(self):
        digest = {"big_news": [], "minor_news": []}
        source_map = {
            "https://t.me/abualiexpress/1": {"text": "**°תוכן שיווקי**", "ts": 1.0},
            "https://t.me/abualiexpress/2": {"text": "כרית קמומיל לילדים", "ts": 2.0},
            "https://t.me/abualiexpress/3": {"text": "תקיפה בג'נין", "ts": 3.0},
        }
        cov = compute_coverage(digest, source_map)
        assert cov["covered"] == 0
        assert cov["total"] == 3
        assert cov["per_channel"]["abualiexpress"] == {"covered": 0, "total": 3}

    def test_real_coverage_excludes_ads(self):
        digest = {
            "big_news": [{"links": ["https://t.me/abualiexpress/3"], "section": "conflict"}],
            "minor_news": [],
        }
        source_map = {
            "https://t.me/abualiexpress/1": {"text": "**°תוכן שיווקי**", "ts": 1.0},
            "https://t.me/abualiexpress/2": {"text": "כרית קמומיל לילדים", "ts": 2.0},
            "https://t.me/abualiexpress/3": {"text": "תקיפה בג'נין", "ts": 3.0},
            "https://t.me/abualiexpress/4": {"text": "ידיעה שנשמטה", "ts": 4.0},
        }
        cov = compute_coverage(digest, source_map)
        assert cov["ads"] == 2
        assert (cov["covered"], cov["total"]) == (1, 4)          # raw: 25%
        assert (cov["real_covered"], cov["real_total"]) == (1, 2)  # adjusted: 50%
        assert cov["uncovered_real"] == ["https://t.me/abualiexpress/4"]

    def test_ad_that_was_covered_counts_as_ad_not_real(self):
        digest = {
            "big_news": [{"links": ["https://t.me/abualiexpress/1"], "section": "world"}],
            "minor_news": [],
        }
        source_map = {
            "https://t.me/abualiexpress/1": {"text": "**°תוכן שיווקי**", "ts": 1.0},
            "https://t.me/abualiexpress/2": {"text": "כרית קמומיל לילדים", "ts": 2.0},
        }
        cov = compute_coverage(digest, source_map)
        assert cov["ads"] == 2
        assert cov["real_total"] == 0
        assert cov["uncovered_real"] == []
        assert cov["uncovered_ads"] == ["https://t.me/abualiexpress/2"]

    def test_all_uncovered_are_ads(self):
        digest = {
            "big_news": [{"links": ["https://t.me/abualiexpress/3"], "section": "conflict"}],
            "minor_news": [],
        }
        source_map = {
            "https://t.me/abualiexpress/1": {"text": "**°תוכן שיווקי**", "ts": 1.0},
            "https://t.me/abualiexpress/2": {"text": "כרית קמומיל לילדים", "ts": 2.0},
            "https://t.me/abualiexpress/3": {"text": "תקיפה בג'נין", "ts": 3.0},
        }
        cov = compute_coverage(digest, source_map)
        assert cov["uncovered_real"] == []
        assert (cov["real_covered"], cov["real_total"]) == (1, 1)


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
        assert "<details" in page
        assert "1 הודעות תוכן שלא סוקרו" in page
        assert 'href="https://t.me/beta/10"' in page
        assert "סוקרו 2 מתוך 3 הודעות" in page

    def test_blocks_omitted_for_empty_source_map(self):
        page = build_html_page(self.DIGEST, {}, self.END_DATE)
        assert 'class="channel-stats"' not in page
        assert 'class="diagnostics"' not in page

    MARKER = "**°תוכן שיווקי**"

    def _source_map_with(self, extras):
        sm = self._source_map(False)
        sm.update(extras)
        return sm

    def test_ad_only_uncovered_shows_no_warning(self):
        sm = self._source_map_with({
            "https://t.me/abualiexpress/1": {"text": self.MARKER, "ts": 3.0, "time": "07:00", "external_links": []},
            "https://t.me/abualiexpress/2": {"text": "כרית קמומיל לילדים", "ts": 4.0, "time": "07:02", "external_links": []},
        })
        page = build_html_page(self.DIGEST, sm, self.END_DATE)
        assert 'class="diagnostics-warning"' not in page
        assert "2 הודעות שיווקיות שדולגו (כצפוי)" in page
        assert "כל הודעות התוכן סוקרו בעדכון. ✓" in page

    def test_ad_group_is_expandable_not_hidden(self):
        sm = self._source_map_with({
            "https://t.me/abualiexpress/1": {"text": self.MARKER, "ts": 3.0, "time": "07:00", "external_links": []},
        })
        page = build_html_page(self.DIGEST, sm, self.END_DATE)
        assert '<details class="uncovered-ads">' in page
        assert 'href="https://t.me/abualiexpress/1"' in page

    def test_real_uncovered_shows_warning(self):
        page = build_html_page(self.DIGEST, self._source_map(True), self.END_DATE)
        assert 'class="diagnostics-warning"' in page
        assert "1 הודעות תוכן לא סוקרו בעדכון" in page
        assert '<details class="uncovered-real" open>' in page

    def test_mixed_uncovered_renders_both_groups(self):
        sm = self._source_map_with({
            "https://t.me/beta/10": {"text": "ידיעה שנשמטה", "ts": 3.0, "time": "07:00", "external_links": []},
            "https://t.me/abualiexpress/1": {"text": self.MARKER, "ts": 4.0, "time": "07:05", "external_links": []},
            "https://t.me/abualiexpress/2": {"text": "כרית קמומיל לילדים", "ts": 5.0, "time": "07:07", "external_links": []},
        })
        page = build_html_page(self.DIGEST, sm, self.END_DATE)
        assert 'class="diagnostics-warning"' in page
        assert "1 הודעות תוכן שלא סוקרו" in page
        assert "2 הודעות שיווקיות שדולגו (כצפוי)" in page
        # the dropped story is listed in the real group, above the ad group
        assert page.index('class="uncovered-real"') < page.index('class="uncovered-ads"')

    def test_raw_and_adjusted_coverage_lines_both_present(self):
        sm = self._source_map_with({
            "https://t.me/abualiexpress/1": {"text": self.MARKER, "ts": 3.0, "time": "07:00", "external_links": []},
            "https://t.me/abualiexpress/2": {"text": "כרית קמומיל לילדים", "ts": 4.0, "time": "07:02", "external_links": []},
        })
        page = build_html_page(self.DIGEST, sm, self.END_DATE)
        assert "סוקרו 2 מתוך 4 הודעות" in page                       # raw, unchanged
        assert "כיסוי תוכן: סוקרו 2 מתוך 2 הודעות, ללא 2 הודעות שיווקיות" in page

    def test_adjusted_line_omitted_when_no_ads(self):
        page = build_html_page(self.DIGEST, self._source_map(False), self.END_DATE)
        assert "כיסוי תוכן:" not in page
        assert "כל ההודעות סוקרו בעדכון. ✓" in page


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


def _anthropic_stub_raw(big_news, minor_news=None, date_range="2026-05-13 10:00 - 2026-05-13 12:00 Israel"):
    """Like _anthropic_stub but passes the tool_use input through verbatim, so a
    test can reproduce a malformed field (e.g. big_news as an unparseable string)."""
    block = Mock()
    block.type = "tool_use"
    block.input = {
        "date_range": date_range,
        "big_news": big_news,
        "minor_news": [] if minor_news is None else minor_news,
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

    def test_unparseable_big_news_returns_none(self):
        """The 2026-08-28 failure mode, at the create_digest boundary."""
        resp = _anthropic_stub_raw(big_news='[{"headline": "כותרת", ')
        p, _ = self._patch_ac(resp)
        with p:
            result = asyncio.run(create_digest({"ch": ["msg"]}, self.START, self.END))
        assert result is None

    def test_valid_json_string_big_news_still_parses(self):
        """The legitimate SDK quirk this defensive branch exists for must still work."""
        resp = _anthropic_stub_raw(big_news=json.dumps([{
            "headline": "כותרת", "summary": "סיכום",
            "links": ["https://t.me/ch/1"],
            "section": "conflict", "source": "@ch", "time": "08:00",
        }]))
        p, _ = self._patch_ac(resp)
        with p:
            result = asyncio.run(create_digest({"ch": ["msg"]}, self.START, self.END))
        assert result is not None
        assert result["big_news"][0]["headline"] == "כותרת"
        assert result["big_news"][0]["links"] == ["https://t.me/ch/1"]

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

    def _setup(self, messages, big_news=None, resp=None):
        mock_tg = _mock_tg_client(messages)
        if resp is None:
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

    def _run_expecting_failure(self, tmp_path, resp):
        """Run the full pipeline with a broken Claude response; return the output path."""
        msg = _tg_msg(100, text="חדשות", dt=self.IN_WINDOW)
        mock_tg, mock_ac = self._setup([msg], resp=resp)
        output = str(tmp_path / "out.html")

        with patch('digest.TelegramClient', return_value=mock_tg), \
             patch('digest.anthropic.AsyncAnthropic', return_value=mock_ac), \
             patch('sys.argv', ['digest.py', '--output', output] + self._DATE_ARGS):
            with pytest.raises(SystemExit) as exc:
                asyncio.run(main())

        assert exc.value.code != 0
        return mock_tg, tmp_path / "out.html"

    def test_unparseable_digest_exits_nonzero_without_publishing(self, tmp_path):
        """The 2026-08-28 incident end-to-end: no HTML page, no Telegram post, exit 1."""
        resp = _anthropic_stub_raw(big_news='[{"headline": "כותרת", ')
        mock_tg, html_path = self._run_expecting_failure(tmp_path, resp)

        assert not html_path.exists()
        mock_tg.send_message.assert_not_called()
        mock_tg.disconnect.assert_awaited()

    def test_max_tokens_truncation_exits_nonzero_without_publishing(self, tmp_path):
        resp = _anthropic_stub(big_news=[{
            "headline": "כותרת", "summary": "סיכום",
            "links": ["https://t.me/ch/100"],
            "section": "conflict", "source": "@ch", "time": "10:00",
        }])
        resp.stop_reason = "max_tokens"
        mock_tg, html_path = self._run_expecting_failure(tmp_path, resp)

        assert not html_path.exists()
        mock_tg.send_message.assert_not_called()

    def test_missing_tool_use_block_exits_nonzero_without_publishing(self, tmp_path):
        resp = Mock()
        resp.content = []
        resp.stop_reason = "end_turn"
        resp.usage = Mock(output_tokens=10)
        mock_tg, html_path = self._run_expecting_failure(tmp_path, resp)

        assert not html_path.exists()
        mock_tg.send_message.assert_not_called()

    def test_no_messages_is_not_a_failure_and_exits_zero(self, tmp_path):
        """A quiet news window is a legitimate empty result, not an error."""
        mock_tg, mock_ac = self._setup([])
        output = str(tmp_path / "out.html")

        with patch('digest.TelegramClient', return_value=mock_tg), \
             patch('digest.anthropic.AsyncAnthropic', return_value=mock_ac), \
             patch('sys.argv', ['digest.py', '--output', output] + self._DATE_ARGS):
            asyncio.run(main())  # must not raise SystemExit

        assert not (tmp_path / "out.html").exists()
        mock_tg.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# Operator alerting — pure helpers
# ---------------------------------------------------------------------------

class TestCoerceChatId:
    def test_numeric_id_becomes_int(self):
        assert _coerce_chat_id("123456789") == 123456789

    def test_negative_channel_id_becomes_int(self):
        assert _coerce_chat_id("-1001234567890") == -1001234567890

    def test_username_passed_through(self):
        assert _coerce_chat_id("@operator") == "@operator"

    def test_me_passed_through(self):
        assert _coerce_chat_id("me") == "me"

    def test_surrounding_whitespace_stripped(self):
        assert _coerce_chat_id("  42  ") == 42
        assert _coerce_chat_id("  me  ") == "me"

    def test_matches_target_channel_convention(self):
        # digest.py coerces TARGET_CHANNEL with the same isdigit()/lstrip('-') rule.
        for raw in ("-1001234567890", "42", "@chan", "me"):
            expected = int(raw) if raw.lstrip('-').isdigit() else raw
            assert _coerce_chat_id(raw) == expected


class TestFormatWindow:
    def test_start_and_end(self):
        w = format_window(datetime(2026, 8, 28, 4, 0, tzinfo=UTC),
                          datetime(2026, 8, 28, 16, 0, tzinfo=UTC))
        assert "2026-08-28 07:00" in w and "2026-08-28 19:00" in w and "Israel" in w

    def test_end_only(self):
        w = format_window(None, datetime(2026, 8, 28, 16, 0, tzinfo=UTC))
        assert w.startswith("up to") and "2026-08-28 19:00" in w

    def test_unknown(self):
        assert format_window(None, None) == "unknown window"


class TestCheckDigestHealth:
    SOURCE_MAP = {f"https://t.me/ch/{i}": {"text": "t", "media_type": None,
                                           "video_duration": None, "external_links": [],
                                           "time": "08:00", "ts": float(i)}
                  for i in range(1, 11)}

    def _digest(self, covered_ids):
        return {"big_news": [{"headline": "h", "section": "conflict",
                              "links": [f"https://t.me/ch/{i}" for i in covered_ids]}],
                "minor_news": []}

    def test_healthy_digest_has_no_issues(self):
        issues, cov = check_digest_health(self._digest(range(1, 10)), self.SOURCE_MAP, threshold=60)
        assert issues == []
        assert (cov["covered"], cov["total"]) == (9, 10)

    def test_low_coverage_flagged(self):
        issues, cov = check_digest_health(self._digest([1, 2, 3]), self.SOURCE_MAP, threshold=60)
        assert len(issues) == 1
        assert "30%" in issues[0] and "60%" in issues[0]

    def test_coverage_exactly_at_threshold_is_healthy(self):
        issues, _ = check_digest_health(self._digest(range(1, 7)), self.SOURCE_MAP, threshold=60)
        assert issues == []

    def test_empty_big_news_flagged_independently(self):
        # 100% coverage from minor_news alone, but no big_news at all.
        digest = {"big_news": [],
                  "minor_news": [{"headline": "m", "section": "world",
                                  "links": list(self.SOURCE_MAP)}]}
        issues, _ = check_digest_health(digest, self.SOURCE_MAP, threshold=60)
        assert len(issues) == 1
        assert "no big_news" in issues[0] and "10 source messages" in issues[0]

    def test_incident_shape_flags_both(self):
        # 2026-08-28: 39% coverage and zero big_news.
        digest = {"big_news": [],
                  "minor_news": [{"headline": "m", "section": "world",
                                  "links": [f"https://t.me/ch/{i}" for i in range(1, 4)]}]}
        issues, _ = check_digest_health(digest, self.SOURCE_MAP, threshold=60)
        assert len(issues) == 2

    def test_empty_source_map_never_alerts(self):
        issues, cov = check_digest_health({"big_news": [], "minor_news": []}, {}, threshold=60)
        assert issues == []
        assert cov["total"] == 0

    def test_threshold_defaults_to_module_constant(self):
        with patch('digest.COVERAGE_ALERT_THRESHOLD', 95.0):
            issues, _ = check_digest_health(self._digest(range(1, 10)), self.SOURCE_MAP)
        assert len(issues) == 1


class TestAlertFormatting:
    def test_failure_alert_has_type_message_and_window(self):
        err = ValueError("Your credit balance is too low")
        text = format_failure_alert(err, "2026-08-26 07:00 -> 19:00 Israel", stage="claude")
        assert "ValueError" in text
        assert "Your credit balance is too low" in text
        assert "2026-08-26 07:00" in text
        assert "claude" in text

    def test_failure_alert_accepts_plain_string(self):
        text = format_failure_alert("no tool_use block", "w")
        assert "no tool_use block" in text

    def test_failure_alert_truncates_long_error(self):
        text = format_failure_alert("x" * 5000, "w")
        assert len(text) < 700
        assert text.endswith("…")

    def test_health_alert_has_numbers_issues_and_url(self):
        cov = {"covered": 21, "total": 54, "per_channel": {}, "uncovered": []}
        text = format_health_alert(["coverage 39% is below the 60% threshold"], cov,
                                   "2026-08-28 07:00 -> 19:00 Israel",
                                   "https://example.com/d.html")
        assert "21/54" in text and "39%" in text
        assert "https://example.com/d.html" in text
        assert "2026-08-28" in text

    def test_health_alert_without_url(self):
        cov = {"covered": 0, "total": 3, "per_channel": {}, "uncovered": []}
        text = format_health_alert(["no big_news stories despite 3 source messages"], cov, "w")
        assert "Page:" not in text

    def test_alerts_stay_phone_sized(self):
        cov = {"covered": 21, "total": 54, "per_channel": {}, "uncovered": []}
        text = format_health_alert(["a" * 60, "b" * 60], cov, "w", "https://example.com/d.html")
        assert len(text) < 500


# ---------------------------------------------------------------------------
# send_alert (mocks Telethon at the network boundary)
# ---------------------------------------------------------------------------

def _bot_factory():
    """Mock for `TelegramClient(StringSession(), ...).start(bot_token=...)`."""
    bot = AsyncMock()
    factory = MagicMock()
    factory.start = AsyncMock(return_value=bot)
    return factory, bot


class TestSendAlert:
    def test_noop_when_alert_chat_id_unset(self):
        client = AsyncMock()
        with patch('digest.ALERT_CHAT_ID', None), patch('digest.BOT_TOKEN', None), \
             patch('digest.TelegramClient') as tg:
            assert asyncio.run(send_alert("boom", client)) is False
        client.send_message.assert_not_called()
        tg.assert_not_called()

    def test_noop_when_alert_chat_id_empty_string(self):
        with patch('digest.ALERT_CHAT_ID', ''), patch('digest.TelegramClient') as tg:
            assert asyncio.run(send_alert("boom")) is False
        tg.assert_not_called()

    def test_sends_via_bot_when_bot_token_set(self):
        factory, bot = _bot_factory()
        client = AsyncMock()
        with patch('digest.ALERT_CHAT_ID', '123456'), patch('digest.BOT_TOKEN', 'tok'), \
             patch('digest.TelegramClient', return_value=factory):
            assert asyncio.run(send_alert("boom", client)) is True
        bot.send_message.assert_awaited_once_with(123456, "boom")
        bot.disconnect.assert_awaited_once()
        client.send_message.assert_not_called()

    def test_sends_via_supplied_user_client_without_bot_token(self):
        client = AsyncMock()
        with patch('digest.ALERT_CHAT_ID', '@operator'), patch('digest.BOT_TOKEN', None), \
             patch('digest.TelegramClient') as tg:
            assert asyncio.run(send_alert("boom", client)) is True
        client.send_message.assert_awaited_once_with("@operator", "boom")
        # The caller still owns the session it passed in.
        client.disconnect.assert_not_called()
        tg.assert_not_called()

    def test_starts_own_user_session_when_no_client_and_no_bot(self):
        own = AsyncMock()
        with patch('digest.ALERT_CHAT_ID', 'me'), patch('digest.BOT_TOKEN', None), \
             patch('digest.TelegramClient', return_value=own):
            assert asyncio.run(send_alert("boom")) is True
        own.start.assert_awaited_once()
        own.send_message.assert_awaited_once_with("me", "boom")
        own.disconnect.assert_awaited_once()

    def test_send_failure_is_logged_and_swallowed(self, caplog):
        client = AsyncMock()
        client.send_message = AsyncMock(side_effect=RuntimeError("peer not found"))
        with patch('digest.ALERT_CHAT_ID', '123'), patch('digest.BOT_TOKEN', None):
            with caplog.at_level('ERROR'):
                assert asyncio.run(send_alert("boom", client)) is False
        assert "Failed to send operator alert" in caplog.text
        assert "peer not found" in caplog.text
        assert "boom" in caplog.text  # the alert we could not deliver is still logged

    def test_bot_start_failure_is_swallowed(self):
        factory = MagicMock()
        factory.start = AsyncMock(side_effect=RuntimeError("bad token"))
        with patch('digest.ALERT_CHAT_ID', '123'), patch('digest.BOT_TOKEN', 'tok'), \
             patch('digest.TelegramClient', return_value=factory):
            assert asyncio.run(send_alert("boom")) is False

    def test_disconnect_failure_is_swallowed(self):
        factory, bot = _bot_factory()
        bot.disconnect = AsyncMock(side_effect=RuntimeError("already closed"))
        with patch('digest.ALERT_CHAT_ID', '123'), patch('digest.BOT_TOKEN', 'tok'), \
             patch('digest.TelegramClient', return_value=factory):
            assert asyncio.run(send_alert("boom")) is True


# ---------------------------------------------------------------------------
# main() alerting (mocks TelegramClient + Anthropic at the network boundary)
# ---------------------------------------------------------------------------

class TestMainPipelineAlerts:
    """Alerting behaviour of the full pipeline.

    Reuses TestMainPipeline's mock setup without re-running its tests. All runs
    use --dry-run, so the only send_message call that can happen is an alert.
    """

    IN_WINDOW  = TestMainPipeline.IN_WINDOW
    _DATE_ARGS = TestMainPipeline._DATE_ARGS
    _setup     = TestMainPipeline._setup

    def _run(self, argv_extra, mock_tg, mock_ac, alert_chat_id='999'):
        with patch('digest.TelegramClient', return_value=mock_tg), \
             patch('digest.anthropic.AsyncAnthropic', return_value=mock_ac), \
             patch('digest.ALERT_CHAT_ID', alert_chat_id), \
             patch('digest.BOT_TOKEN', None), \
             patch('sys.argv', ['digest.py', '--dry-run'] + argv_extra + self._DATE_ARGS):
            asyncio.run(main())

    @staticmethod
    def _link(msg_id):
        # CHANNEL_USERNAMES is 'testchannel' (see conftest.py).
        return f"https://t.me/testchannel/{msg_id}"

    def _big_news(self, msg_ids):
        return [{"headline": "כותרת", "summary": "סיכום",
                 "links": [self._link(i) for i in msg_ids],
                 "section": "conflict", "source": "@testchannel", "time": "10:00"}]

    # -- feature off ------------------------------------------------------
    def test_no_alert_attempted_when_alert_chat_id_unset(self, tmp_path):
        msgs = [_tg_msg(i, text="חדשות", dt=self.IN_WINDOW) for i in (100, 101, 102)]
        mock_tg, mock_ac = self._setup(msgs, big_news=self._big_news([100]))  # 33% coverage
        self._run(['--output', str(tmp_path / "out.html")], mock_tg, mock_ac,
                  alert_chat_id=None)
        mock_tg.send_message.assert_not_called()
        assert (tmp_path / "out.html").exists()

    def test_no_alert_when_run_fails_and_feature_is_off(self, tmp_path):
        mock_tg, mock_ac = self._setup([_tg_msg(100, text="x", dt=self.IN_WINDOW)])
        mock_ac.messages.stream = MagicMock(side_effect=RuntimeError("credit balance too low"))
        with pytest.raises(RuntimeError):
            self._run(['--output', str(tmp_path / "out.html")], mock_tg, mock_ac,
                      alert_chat_id=None)
        mock_tg.send_message.assert_not_called()

    # -- failure triggers -------------------------------------------------
    def test_exception_during_run_alerts_and_reraises(self, tmp_path):
        mock_tg, mock_ac = self._setup([_tg_msg(100, text="x", dt=self.IN_WINDOW)])
        mock_ac.messages.stream = MagicMock(
            side_effect=RuntimeError("Your credit balance is too low"))

        with pytest.raises(RuntimeError):
            self._run(['--output', str(tmp_path / "out.html")], mock_tg, mock_ac)

        mock_tg.send_message.assert_called_once()
        text = mock_tg.send_message.call_args[0][1]
        assert "FAILED" in text
        assert "RuntimeError" in text
        assert "Your credit balance is too low" in text
        assert "2026-05-13 11:00" in text  # window, in Israel local time
        assert not (tmp_path / "out.html").exists()

    def test_create_digest_returning_none_alerts(self, tmp_path):
        mock_tg, mock_ac = self._setup([_tg_msg(100, text="x", dt=self.IN_WINDOW)])
        truncated = Mock()
        truncated.stop_reason = "max_tokens"
        truncated.usage = Mock(output_tokens=64000)
        truncated.content = []
        mock_ac.messages.stream.return_value.__aenter__.return_value.get_final_message = \
            AsyncMock(return_value=truncated)

        with pytest.raises(SystemExit):
            self._run(['--output', str(tmp_path / "out.html")], mock_tg, mock_ac)

        mock_tg.send_message.assert_called_once()
        text = mock_tg.send_message.call_args[0][1]
        assert "FAILED" in text and "max_tokens" in text
        assert not (tmp_path / "out.html").exists()

    def test_no_messages_fetched_alerts(self, tmp_path):
        mock_tg, mock_ac = self._setup([])
        self._run(['--output', str(tmp_path / "out.html")], mock_tg, mock_ac)
        mock_tg.send_message.assert_called_once()
        text = mock_tg.send_message.call_args[0][1]
        assert "No digest published" in text
        assert "2026-05-13 11:00" in text

    # -- coverage triggers ------------------------------------------------
    def test_coverage_below_threshold_alerts(self, tmp_path):
        msgs = [_tg_msg(i, text="חדשות", dt=self.IN_WINDOW) for i in (100, 101, 102)]
        mock_tg, mock_ac = self._setup(msgs, big_news=self._big_news([100]))  # 33%
        output = str(tmp_path / "out.html")

        with patch('digest.COVERAGE_ALERT_THRESHOLD', 60.0):
            self._run(['--output', output], mock_tg, mock_ac)

        mock_tg.send_message.assert_called_once()
        text = mock_tg.send_message.call_args[0][1]
        assert "1/3" in text and "33%" in text
        assert output in text  # page path is included
        assert (tmp_path / "out.html").exists()  # the digest still published

    def test_coverage_above_threshold_does_not_alert(self, tmp_path):
        msgs = [_tg_msg(i, text="חדשות", dt=self.IN_WINDOW) for i in (100, 101, 102)]
        mock_tg, mock_ac = self._setup(msgs, big_news=self._big_news([100, 101, 102]))
        with patch('digest.COVERAGE_ALERT_THRESHOLD', 60.0):
            self._run(['--output', str(tmp_path / "out.html")], mock_tg, mock_ac)
        mock_tg.send_message.assert_not_called()

    def test_empty_big_news_alerts_even_with_full_coverage(self, tmp_path):
        msgs = [_tg_msg(i, text="חדשות", dt=self.IN_WINDOW) for i in (100, 101, 102)]
        mock_tg, mock_ac = self._setup(msgs, big_news=[])
        resp = _anthropic_stub(
            big_news=[],
            minor_news=[{"headline": "m", "section": "world",
                         "links": [self._link(i) for i in (100, 101, 102)]}],
        )
        mock_ac.messages.stream.return_value.__aenter__.return_value.get_final_message = \
            AsyncMock(return_value=resp)

        with patch('digest.COVERAGE_ALERT_THRESHOLD', 60.0):
            self._run(['--output', str(tmp_path / "out.html")], mock_tg, mock_ac)

        mock_tg.send_message.assert_called_once()
        assert "no big_news" in mock_tg.send_message.call_args[0][1]

    def test_unparseable_big_news_string_alerts(self, tmp_path):
        """The 2026-08-28 shape: model returns big_news as an unparseable string.

        Since #32, normalize_digest raises DigestParseError instead of silently
        substituting an empty list, so create_digest returns None and this takes
        the same generic FAILED path as test_create_digest_returning_none_alerts
        (see TestMainPipeline.test_unparseable_digest_exits_nonzero_without_publishing
        for the no-alert/exit-code assertions on this same scenario).
        """
        msgs = [_tg_msg(i, text="חדשות", dt=self.IN_WINDOW) for i in (100, 101, 102)]
        mock_tg, mock_ac = self._setup(msgs)
        block = Mock()
        block.type = "tool_use"
        block.input = {"date_range": "r", "big_news": "[{headline: broken", "minor_news": []}
        resp = Mock(content=[block], stop_reason="tool_use", usage=Mock(output_tokens=10))
        mock_ac.messages.stream.return_value.__aenter__.return_value.get_final_message = \
            AsyncMock(return_value=resp)

        with pytest.raises(SystemExit):
            self._run(['--output', str(tmp_path / "out.html")], mock_tg, mock_ac)

        mock_tg.send_message.assert_called_once()
        text = mock_tg.send_message.call_args[0][1]
        assert "FAILED" in text
        assert not (tmp_path / "out.html").exists()

    # -- robustness -------------------------------------------------------
    def test_alert_send_failure_does_not_break_a_published_run(self, tmp_path, caplog):
        msgs = [_tg_msg(i, text="חדשות", dt=self.IN_WINDOW) for i in (100, 101, 102)]
        mock_tg, mock_ac = self._setup(msgs, big_news=self._big_news([100]))
        mock_tg.send_message = AsyncMock(side_effect=RuntimeError("chat not found"))

        with caplog.at_level('ERROR'):
            self._run(['--output', str(tmp_path / "out.html")], mock_tg, mock_ac)

        assert "Failed to send operator alert" in caplog.text
        assert (tmp_path / "out.html").exists()  # digest still published

    def test_alert_send_failure_does_not_mask_original_error(self, tmp_path, caplog):
        mock_tg, mock_ac = self._setup([_tg_msg(100, text="x", dt=self.IN_WINDOW)])
        mock_ac.messages.stream = MagicMock(side_effect=RuntimeError("original failure"))
        mock_tg.send_message = AsyncMock(side_effect=RuntimeError("chat not found"))

        with caplog.at_level('ERROR'):
            with pytest.raises(RuntimeError, match="original failure"):
                self._run(['--output', str(tmp_path / "out.html")], mock_tg, mock_ac)

        assert "Failed to send operator alert" in caplog.text
