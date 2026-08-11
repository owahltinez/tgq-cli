"""Preview parsing is pure: it takes HTML text, never a socket."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tgq.preview import parse_count, parse_page

FIXTURE = Path(__file__).parent / "fixtures" / "preview_page.html"
RETRIEVED = datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def page():
    return parse_page(
        FIXTURE.read_text(encoding="utf-8"),
        channel="ceuta_news",
        retrieved_at=RETRIEVED,
    )


# --- counts with magnitude suffixes ---------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("903", 903),
        ("1.5K", 1500),
        ("12.4K", 12400),
        ("9.86M", 9860000),
        ("2B", 2000000000),
        ("", None),
        (None, None),
        ("garbage", None),
    ],
)
def test_parse_count_handles_suffixes(raw, expected):
    assert parse_count(raw) == expected


# --- message extraction ---------------------------------------------------


def test_all_messages_extracted(page):
    assert [r["message_id"] for r in page.messages] == [438, 439, 440]


def test_channel_identity_uses_username_since_preview_has_no_numeric_id(page):
    first = page.messages[0]

    assert first["channel_username"] == "ceuta_news"
    assert first["channel_id"] is None


def test_text_flattened_across_inline_markup_and_breaks(page):
    assert page.messages[0]["text"] == "crossing at tarajal now\nsecond line"


def test_date_and_views_parsed(page):
    first = page.messages[0]

    assert first["date"] == "2026-07-30T06:15:00+00:00"
    assert first["views"] == 1500


def test_absent_views_are_none_not_zero(page):
    assert page.messages[2]["views"] is None


def test_provenance_stamped_per_message(page):
    first = page.messages[0]

    assert first["source_url"] == "https://t.me/ceuta_news/438"
    assert first["retrieved_at"] == "2026-08-11T12:00:00+00:00"
    assert first["source_kind"] == "primary"


# --- forward attribution --------------------------------------------------


def test_non_forward_has_no_origin(page):
    first = page.messages[0]

    assert first["fwd_from_username"] is None
    assert first["fwd_from_msg_id"] is None


def test_forward_origin_channel_and_message_id_extracted(page):
    relayed = page.messages[1]

    assert relayed["fwd_from_username"] == "origin_chan"
    assert relayed["fwd_from_msg_id"] == 10112
    assert relayed["fwd_from_name"] == "Origin Chan"


def test_forward_without_link_keeps_only_the_name(page):
    hidden = page.messages[2]

    assert hidden["fwd_from_username"] is None
    assert hidden["fwd_from_name"] == "Hidden Origin"


# --- page-level metadata --------------------------------------------------


def test_next_before_cursor_extracted(page):
    assert page.next_before == 438


def test_subscribers_parsed_from_header(page):
    assert page.subscribers == 12400


def test_page_without_more_link_has_no_cursor():
    html = "<section class='tgme_channel_history'></section>"

    page = parse_page(html, channel="x", retrieved_at=RETRIEVED)

    assert page.next_before is None
    assert page.messages == []


# --- media detection beyond photos ---------------------------------------


def _one_message(inner):
    return f"""
    <section class="tgme_channel_history">
      <div class="tgme_widget_message" data-post="c/1">
        <div class="tgme_widget_message_bubble">{inner}
          <a class="tgme_widget_message_date">
            <time datetime="2026-08-01T00:00:00+00:00"></time></a>
        </div>
      </div>
    </section>"""


@pytest.mark.parametrize(
    "kind",
    ["photo", "video", "document", "voice", "sticker", "roundvideo", "poll"],
)
def test_media_detected_for_every_wrapper_kind(kind):
    """Matching only .._photo undercounted has_media on live data (7 of 15)."""
    html = _one_message(f'<div class="tgme_widget_message_{kind}"></div>')

    page = parse_page(html, channel="c", retrieved_at=RETRIEVED)

    assert page.messages[0]["has_media"] is True


def test_text_only_message_has_no_media():
    html = _one_message(
        '<div class="tgme_widget_message_text">just words</div>'
    )

    page = parse_page(html, channel="c", retrieved_at=RETRIEVED)

    assert page.messages[0]["has_media"] is False


def test_unparseable_post_id_yields_no_source_url():
    """A broken provenance URL is worse than a null in a provenance-first tool.

    Regression: interpolation produced "https://t.me/c/None".
    """
    html = """
    <section class="tgme_channel_history">
      <div class="tgme_widget_message" data-post="c/notanumber">
        <div class="tgme_widget_message_bubble">
          <a class="tgme_widget_message_date">
            <time datetime="2026-08-01T00:00:00+00:00"></time></a>
        </div>
      </div>
    </section>"""

    page = parse_page(html, channel="c", retrieved_at=RETRIEVED)

    assert page.messages[0]["message_id"] is None
    assert page.messages[0]["source_url"] is None
