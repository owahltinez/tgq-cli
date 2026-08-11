"""Channel-name normalisation, shared by every command and both backends.

Accepting a name verbatim corrupted output rather than failing: "@x" became the
graph node "@@x" alongside "@x", splitting one channel in two, and a pasted URL
was percent-encoded into the request path.
"""

import re

# Public channel links, in the spellings people actually paste.
_PREFIXES = (
    "https://t.me/",
    "http://t.me/",
    "https://telegram.me/",
    "http://telegram.me/",
    "t.me/",
    "telegram.me/",
)

# The /s/ segment is the web-preview view, not part of the channel name.
_PREVIEW_SEGMENT = "s/"

_VALID = re.compile(r"^[A-Za-z0-9_]{4,32}$")


def normalise_channel(name):
    """Return the bare channel username, or raise ValueError explaining why not.

    Rejects rather than guesses for links that cannot be resolved by username:
    a private /c/<id>/ link would otherwise normalise to the literal "c"
    and collect a different channel; an invite link names no channel.
    """
    text = (name or "").strip()
    from_url = False
    for prefix in _PREFIXES:
        if text.lower().startswith(prefix):
            text = text[len(prefix) :]
            from_url = True
            break

    if text.lower().startswith(_PREVIEW_SEGMENT):
        text = text[len(_PREVIEW_SEGMENT) :]

    text = text.lstrip("@").strip("/")

    if text.startswith("+") or text.lower().startswith("joinchat"):
        raise ValueError(f"invite links identify no channel username: {name!r}")

    # A deep link carries /<message_id> after the channel.
    head = text.split("/", 1)[0]

    if head == "c" and from_url:
        raise ValueError(
            f"not a public channel: {name!r} is a private /c/ link, which has "
            "no resolvable username"
        )
    if not head:
        raise ValueError(f"empty channel name: {name!r}")
    if not _VALID.match(head):
        raise ValueError(f"not a valid channel username: {head!r}")

    return head
