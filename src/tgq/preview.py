"""Public web-preview backend: t.me/s/<channel>, no account required.

Telegram renders public channel history server-side at t.me/s/. That page is
scrapable unauthenticated, which makes it the only path available before an
account exists. Fidelity is lower than MTProto -- no reactions, no forward
counts, no numeric channel id -- so rows carry None where a field is
unavailable rather than a substitute value.

Parsing is separated from fetching so the parser is testable against fixtures.
"""

import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

PREVIEW_URL = "https://t.me/s/{channel}"
PAGE_SIZE = 20
SOURCE_KIND = "primary"

# Telegram abbreviates large counts in the rendered page.
MAGNITUDES = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}

# Media wrappers the preview renders. Matching only the photo wrapper
# undercounted has_media by 7 of 15 rows against the MTProto backend on the
# same window, so every kind is enumerated rather than inferred.
MEDIA_KINDS = (
    "photo",
    "video",
    "document",
    "voice",
    "sticker",
    "roundvideo",
    "poll",
    "location",
    "audio",
)
MEDIA_SELECTOR = ", ".join(f".tgme_widget_message_{k}" for k in MEDIA_KINDS)

# A browser UA is required; the endpoint refuses obvious automation.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


@dataclass
class Page:
    """One rendered preview page, plus the cursor to the previous page."""

    messages: list = field(default_factory=list)
    next_before: int | None = None
    subscribers: int | None = None


def parse_count(raw):
    """Parse a rendered count such as "1.5K" or "903" into an int, or None."""
    if not raw:
        return None

    text = raw.strip().replace(",", "")
    multiplier = MAGNITUDES.get(text[-1:].upper(), 1)
    if multiplier != 1:
        text = text[:-1]

    try:
        return int(float(text) * multiplier)
    except ValueError:
        return None


def _post_id(node):
    """Message id from the data-post="channel/123" attribute."""
    _, _, tail = str(node.get("data-post") or "").partition("/")
    return int(tail) if tail.isdigit() else None


def _text(node):
    """Message text with inline markup flattened and <br> preserved as newlines.

    <br> is substituted before extraction because a separator passed to
    get_text() would also split on inline tags such as <b> and <a>, chopping
    words mid-sentence.
    """
    body = node.select_one(".tgme_widget_message_text")
    if body is None:
        return None

    for break_tag in body.find_all("br"):
        break_tag.replace_with("\n")

    return body.get_text("").strip() or None


def _timestamp(node):
    """ISO-8601 timestamp from the <time datetime=...> element."""
    stamp = node.select_one(".tgme_widget_message_date time")
    return str(stamp.get("datetime")) if stamp else None


def _views(node):
    """Rendered view count, or None when the channel does not expose it."""
    node_views = node.select_one(".tgme_widget_message_views")
    return parse_count(node_views.get_text()) if node_views else None


def _forward(node):
    """Origin of a forwarded post.

    The origin channel and message id come from the attribution link. That link
    is absent when the sender hid the origin, leaving only a display name.
    """
    link = node.select_one(".tgme_widget_message_forwarded_from_name")
    if link is None:
        return {
            "fwd_from_username": None,
            "fwd_from_msg_id": None,
            "fwd_from_name": None,
            "fwd_from_date": None,
        }

    username, msg_id = None, None
    href = str(link.get("href") or "")
    if href:
        parts = urllib.parse.urlparse(href).path.strip("/").split("/")
        if len(parts) == 2 and parts[1].isdigit():
            username, msg_id = parts[0], int(parts[1])

    return {
        "fwd_from_username": username,
        "fwd_from_msg_id": msg_id,
        "fwd_from_name": link.get_text().strip() or None,
        # The preview renders no origin timestamp, so cascade latency is
        # unknowable from this backend. Explicitly null, never omitted:
        # an absent key silently passes numeric comparisons in jq.
        "fwd_from_date": None,
    }


def _record(node, *, channel, retrieved_at):
    """One preview post as a row matching the MTProto record schema.

    Fields the preview cannot supply are None, never zero: reactions, forward
    and reply counts, and the numeric channel id are simply absent here.
    """
    message_id = _post_id(node)
    return {
        "channel_id": None,
        "channel_username": channel,
        "message_id": message_id,
        "date": _timestamp(node),
        "text": _text(node),
        "views": _views(node),
        "forwards": None,
        "replies_count": None,
        "reactions": None,
        "fwd_from_channel_id": None,
        "fwd_from_user_id": None,
        **_forward(node),
        "has_media": node.select_one(MEDIA_SELECTOR) is not None,
        "post_author": None,
        "edit_date": None,
        "grouped_id": None,
        # Interpolating a missing id produced ".../None"; a broken
        # provenance URL is worse than an absent one.
        "source_url": (
            f"https://t.me/{channel}/{message_id}"
            if message_id is not None
            else None
        ),
        "retrieved_at": retrieved_at.isoformat(),
        "source_kind": SOURCE_KIND,
    }


def _subscribers(soup):
    """Subscriber count from the channel header, when present."""
    header = soup.select_one(".tgme_header_counter")
    if header is None:
        return None

    match = re.search(
        r"([\d.,]+[KMB]?)\s+subscriber", header.get_text(), re.IGNORECASE
    )
    return parse_count(match.group(1)) if match else None


def parse_page(html, *, channel, retrieved_at):
    """Parse one t.me/s/ page into records plus the previous-page cursor."""
    soup = BeautifulSoup(html, "html.parser")

    messages = [
        _record(node, channel=channel, retrieved_at=retrieved_at)
        for node in soup.select(".tgme_widget_message[data-post]")
    ]

    more = soup.select_one(".tme_messages_more[data-before]")
    cursor = str(more.get("data-before") or "") if more else ""

    return Page(
        messages=messages,
        next_before=int(cursor) if cursor.isdigit() else None,
        subscribers=_subscribers(soup),
    )


def fetch_page(channel, *, before=None, timeout=30):
    """Retrieve one preview page's HTML."""
    url = PREVIEW_URL.format(channel=urllib.parse.quote(channel))
    if before is not None:
        url = f"{url}?before={before}"

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    log.debug("GET %s", url)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")
