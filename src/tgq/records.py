"""Flatten Telethon objects into JSON-serialisable rows.

Nothing here imports Telethon. Inputs are duck-typed, which keeps the module
testable without a session and reusable over cached responses.
"""

import logging
from datetime import datetime

log = logging.getLogger(__name__)

# Rows come straight off the platform interface, so they are primary sources in
# the provenance taxonomy. Derived rows must restate their own kind.
SOURCE_KIND = "primary"


def _iso(value):
    """Serialise a datetime as ISO-8601, passing None through unchanged."""
    return value.isoformat() if isinstance(value, datetime) else None


def _channel_url(channel):
    """Public t.me URL where a username exists, private /c/ form otherwise."""
    if getattr(channel, "username", None):
        return f"https://t.me/{channel.username}"
    return f"https://t.me/c/{channel.id}"


def _kind(channel):
    """Classify the channel type.

    The two behave differently for attribution: broadcast posts have no
    per-message author, megagroup messages do.
    """
    if getattr(channel, "broadcast", False):
        return "broadcast"
    if getattr(channel, "megagroup", False):
        return "megagroup"
    return "unknown"


def _reaction_key(reaction):
    """Key for one reaction variant, or None if the variant carries no identity.

    ReactionEmoji exposes `emoticon`; ReactionCustomEmoji exposes `document_id`
    and no emoticon at all. Keying only on the former undercounts silently,
    which is worse than failing, so custom emoji get an explicit namespace.
    """
    emoticon = getattr(reaction, "emoticon", None)
    if emoticon:
        return emoticon

    document_id = getattr(reaction, "document_id", None)
    if document_id is not None:
        return f"custom:{document_id}"

    # ReactionPaid (Telegram Stars) carries no fields at all, so it can only be
    # recognised by type name. Matched on the name rather than isinstance to
    # keep this module free of a Telethon import and testable without a session.
    if type(reaction).__name__ == "ReactionPaid":
        return "paid"

    return None


def _reactions(reactions):
    """Flatten Telegram's reaction results into {key: count}, or None."""
    counts = {}
    for item in getattr(reactions, "results", None) or []:
        key = _reaction_key(getattr(item, "reaction", None))
        if key is None:
            # ReactionEmpty and any variant added later. Logged rather than
            # dropped quietly, so a coverage gap is visible in the run output.
            log.warning("unrecognised reaction variant, skipped: %r", item)
            continue
        counts[key] = item.count
    return counts or None


def _forward(fwd):
    """Origin fields for a forwarded message; all None when not a forward.

    Origin channel id is absent when the sender forwarded with the origin
    hidden, in which case from_name is the only identifying signal Telegram
    returns.
    """
    from_id = getattr(fwd, "from_id", None)
    return {
        # from_id is a Peer: PeerChannel carries channel_id, PeerUser carries
        # user_id. Reading only channel_id discards user-originated forwards.
        "fwd_from_channel_id": getattr(from_id, "channel_id", None),
        "fwd_from_user_id": getattr(from_id, "user_id", None),
        # MTProto identifies the origin numerically; the username slot exists
        # so both backends emit one key set. See preview._forward.
        "fwd_from_username": None,
        "fwd_from_name": getattr(fwd, "from_name", None),
        "fwd_from_msg_id": getattr(fwd, "channel_post", None),
        "fwd_from_date": _iso(getattr(fwd, "date", None)),
    }


def _has_media(media):
    """True for media the poster attached, False for an auto-generated preview.

    MessageMediaWebPage is synthesised by Telegram from a URL in the message
    text, so counting it overstates attachment and disagrees with the web
    preview backend, which does not render it as media. Matched on type name to
    keep this module free of a Telethon import.
    """
    if media is None:
        return False

    return type(media).__name__ != "MessageMediaWebPage"


def message_record(message, channel, *, retrieved_at):
    """One message plus its channel context, as a provenance-stamped flat row.

    views and forwards stay None when the platform omits them: a real zero and
    an absent field mean different things for reach estimation.
    """
    replies = getattr(message, "replies", None)
    return {
        "channel_id": channel.id,
        "channel_username": getattr(channel, "username", None),
        "message_id": message.id,
        "date": _iso(message.date),
        "text": message.message,
        "views": message.views,
        "forwards": message.forwards,
        "replies_count": getattr(replies, "replies", None),
        "reactions": _reactions(getattr(message, "reactions", None)),
        **_forward(getattr(message, "fwd_from", None)),
        "has_media": _has_media(getattr(message, "media", None)),
        "post_author": getattr(message, "post_author", None),
        "edit_date": _iso(getattr(message, "edit_date", None)),
        "grouped_id": getattr(message, "grouped_id", None),
        "source_url": f"{_channel_url(channel)}/{message.id}",
        "retrieved_at": _iso(retrieved_at),
        "source_kind": SOURCE_KIND,
    }


def member_record(user, channel, *, retrieved_at):
    """One group member, with the channel it was observed in.

    Account flags and last-seen are the substance here: batch-registered
    accounts cluster in user_id space, since ids are broadly sequential over
    time, and a cohort that all went quiet at once shows up in last_seen.
    """
    status = getattr(user, "status", None)
    username = getattr(user, "username", None)
    return {
        "channel_id": channel.id,
        "channel_username": getattr(channel, "username", None),
        "user_id": user.id,
        "username": username,
        "first_name": getattr(user, "first_name", None),
        "last_name": getattr(user, "last_name", None),
        "is_bot": bool(getattr(user, "bot", False)),
        "is_deleted": bool(getattr(user, "deleted", False)),
        "is_premium": bool(getattr(user, "premium", False)),
        "is_verified": bool(getattr(user, "verified", False)),
        "is_scam": bool(getattr(user, "scam", False)),
        "is_fake": bool(getattr(user, "fake", False)),
        "lang_code": getattr(user, "lang_code", None),
        # Offline statuses carry a timestamp; Recently/LastWeek/LastMonth are
        # privacy buckets with no time, so the kind is reported alongside.
        "last_seen": _iso(getattr(status, "was_online", None)),
        "status_kind": type(status).__name__ if status is not None else None,
        "source_url": f"https://t.me/{username}" if username else None,
        "retrieved_at": _iso(retrieved_at),
        "source_kind": SOURCE_KIND,
    }


def channel_record(channel, *, retrieved_at):
    """Channel metadata as a provenance-stamped flat row."""
    return {
        "channel_id": channel.id,
        "username": getattr(channel, "username", None),
        "title": getattr(channel, "title", None),
        "subscribers": getattr(channel, "participants_count", None),
        "verified": bool(getattr(channel, "verified", False)),
        "kind": _kind(channel),
        "created_at": _iso(getattr(channel, "date", None)),
        "source_url": _channel_url(channel),
        "retrieved_at": _iso(retrieved_at),
        "source_kind": SOURCE_KIND,
    }
