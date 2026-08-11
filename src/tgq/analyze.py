"""Pure filters over collected records.

Both functions are stdin filters in the CLI: they take plain dicts, touch no
network, and emit rows for downstream tools to consume.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

log = logging.getLogger(__name__)

SECONDS_PER_DAY = 86400.0


def _parse(value):
    """Parse an ISO-8601 string as UTC, returning None on anything unusable.

    Naive timestamps are assumed UTC rather than rejected: the tool is UTC end
    to end, and a bare `--as-of 2026-08-11` must still compare against the
    timezone-aware dates that come off the platform.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None

    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _node_key(channel_id, username):
    """Stable graph node key for a channel.

    The MTProto backend supplies a numeric id; the web-preview backend only has
    a username. Numeric wins where both exist so the two never split one channel
    into two nodes, and usernames are prefixed to keep the namespaces distinct.
    """
    if channel_id is not None:
        return str(channel_id)
    if username:
        return f"@{username}"
    return None


def _origin(rec):
    """Node key for a forward's origin, and whether the origin was hidden.

    A hidden origin still forms an edge: the display name is a weak but usable
    node key, and dropping these would silently truncate cascades.
    """
    key = _node_key(
        rec.get("fwd_from_channel_id"), rec.get("fwd_from_username")
    )
    if key is not None:
        return key, False

    # A forward can originate from a user rather than a channel. That origin is
    # known, not hidden, so it gets its own namespace instead of falling through
    # to the display-name branch.
    user_id = rec.get("fwd_from_user_id")
    if user_id is not None:
        return f"user:{user_id}", False

    name = rec.get("fwd_from_name")
    if name:
        return f"name:{name}", True

    return None, False


def _latency(origin_date, relay_date):
    """Seconds between origin post and relay, or None if either is missing.

    Negative values are preserved rather than clamped: they indicate clock skew
    or an edited forward, which is a data-quality signal worth surfacing.
    """
    origin, relay = _parse(origin_date), _parse(relay_date)
    if origin is None or relay is None:
        return None
    return (relay - origin).total_seconds()


def forward_edges(records):
    """Yield one origin -> relay edge per forwarded message."""
    for rec in records:
        origin, hidden = _origin(rec)
        if origin is None:
            continue

        yield {
            "origin": origin,
            "relay": _node_key(
                rec.get("channel_id"), rec.get("channel_username")
            ),
            "origin_msg_id": rec.get("fwd_from_msg_id"),
            "relay_msg_id": rec.get("message_id"),
            "origin_hidden": hidden,
            "latency_seconds": _latency(
                rec.get("fwd_from_date"), rec.get("date")
            ),
        }


@dataclass
class _Span:
    """Running per-channel aggregate.

    A dataclass rather than a dict so timestamps and counts keep distinct types;
    collapsing them into one mapping defeats static checking of the arithmetic
    below.
    """

    channel_id: int | None
    channel_username: str | None
    n_messages: int = 0
    first: datetime | None = None
    last: datetime | None = None

    def observe(self, when):
        """Fold one message into the span; undated rows still count."""
        self.n_messages += 1
        if when is None:
            return
        if self.first is None or when < self.first:
            self.first = when
        if self.last is None or when > self.last:
            self.last = when


def _accumulate(records):
    """Collapse message rows into one _Span per channel."""
    spans: dict[str, _Span] = {}
    for rec in records:
        channel_id = rec.get("channel_id")
        username = rec.get("channel_username")

        # A row with neither identity cannot be attributed to a channel, so it
        # is dropped rather than pooled under a placeholder key.
        key = _node_key(channel_id, username)
        if key is None:
            log.warning(
                "skipping row with no channel identity: %s",
                rec.get("message_id"),
            )
            continue

        if key not in spans:
            spans[key] = _Span(channel_id, username)
        spans[key].observe(_parse(rec.get("date")))

    return spans


def channel_activity(records, *, as_of):
    """Per-channel activity span and silence, most-dormant first.

    days_silent is the gap between the channel's last observed post and as_of,
    which is what surfaces channels that stopped posting after an event. The
    threshold for calling that "dormant" is deliberately left to the caller.
    """
    reference = _parse(as_of)
    rows = []
    for span in _accumulate(records).values():
        silent = None
        if reference is not None and span.last is not None:
            gap = (reference - span.last).total_seconds()
            silent = round(gap / SECONDS_PER_DAY, 3)

        rows.append(
            {
                "channel_id": span.channel_id,
                "channel_username": span.channel_username,
                "n_messages": span.n_messages,
                "first_post_at": span.first.isoformat() if span.first else None,
                "last_post_at": span.last.isoformat() if span.last else None,
                "days_silent": silent,
            }
        )

    # Most-dormant first: that is what an operator scans for.
    return sorted(
        rows,
        key=lambda r: (r["days_silent"] is None, r["days_silent"]),
        reverse=True,
    )
