"""Telethon session handling. The only module that touches the network.

Kept deliberately thin: it yields duck-typed Telethon objects and lets
records.py do the flattening, so everything downstream stays testable offline.
"""

import logging
import os
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodWaitError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.contacts import SearchRequest

log = logging.getLogger(__name__)

# Telethon sleeps through FLOOD_WAIT below its threshold and raises above it.
# Raising the threshold lets ordinary throttling resolve itself; anything longer
# is a signal to stop rather than stall a run for an unknown duration.
MAX_FLOOD_WAIT_SECONDS = 900

# Telegram caps global search server-side; asking for more does not help.
SEARCH_LIMIT = 100

# Failures that mean "this one name is unusable" rather than "stop the run".
RESOLUTION_ERRORS = (
    ValueError,
    TypeError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    ChannelPrivateError,
)


class MissingCredentialsError(RuntimeError):
    """Raised when api_id/api_hash are neither passed nor present in the env."""


def prepare_session_path(path):
    """Expand and create the session's parent directory, returning the path.

    The session file authenticates as the account outright, so a directory this
    function creates is owner-only rather than left to the umask. A directory
    that already exists is never re-permissioned: a bare filename resolves its
    parent to the working directory, and chmod-ing that would be destructive.
    """
    resolved = Path(path).expanduser()
    parent = resolved.parent
    if not parent.is_dir():
        parent.mkdir(parents=True, exist_ok=True)
        parent.chmod(0o700)

    return resolved


def _restrict_to_owner(path):
    """Restrict the session file to its owner, or warn if that is impossible.

    sqlite creates the file at the process umask -- 0644 on a typical system.
    The 0700 parent only covers the default location; `--session
    ./tgq.session` in a shared or synced directory would otherwise leave an
    account-takeover credential world-readable. Windows has no POSIX mode, so
    a failure is reported rather than fatal.
    """
    if path is None:
        return

    try:
        Path(path).chmod(0o600)
    except OSError as exc:
        log.warning("could not restrict session file %r: %s", path, exc)


def build_client(session_path, *, api_id=None, api_hash=None):
    """Construct a client, reading credentials from the env by default."""
    api_id = api_id or os.environ.get("TG_API_ID")
    api_hash = api_hash or os.environ.get("TG_API_HASH")
    if not (api_id and api_hash):
        raise MissingCredentialsError(
            "set TG_API_ID and TG_API_HASH (obtain them from my.telegram.org)"
        )

    client = TelegramClient(
        str(prepare_session_path(session_path)),
        int(api_id),
        api_hash,
        flood_sleep_threshold=MAX_FLOOD_WAIT_SECONDS,
    )

    # Telethon appends `.session` to a filename that lacks it, so the mode is
    # applied to the name it actually opened rather than the one we passed.
    _restrict_to_owner(getattr(client.session, "filename", None))

    return client


async def fetch_channels(client, names):
    """Yield the resolved entity for each name.

    A name that will not resolve is logged and skipped: one dead channel in a
    frame of hundreds must not abort the run. A rate limit is NOT skippable and
    propagates -- continuing would issue a request per remaining name and
    deepen the limit, which a blanket handler previously did while reporting it
    as an ordinary resolution failure.
    """
    for name in names:
        try:
            yield await client.get_entity(name)
        except FloodWaitError:
            log.error("rate limited while resolving %r; aborting", name)
            raise
        except RESOLUTION_ERRORS as exc:
            log.warning("could not resolve %r: %s", name, exc)


async def search_channels(client, query, *, limit=SEARCH_LIMIT, kind="all"):
    """Yield public channels and groups matching `query`.

    Telegram's global search matches username and title, and caps results well
    below the requested limit -- single digits is normal. An empty result is
    therefore never evidence that no such channel exists; callers relying on
    this for a sampling frame must say so.

    Users are dropped: this searches channels, and a user row would not fit the
    channel schema.
    """
    result = await client(SearchRequest(q=query, limit=limit))

    for chat in result.chats:
        if kind == "broadcast" and not getattr(chat, "broadcast", False):
            continue
        if kind == "megagroup" and not getattr(chat, "megagroup", False):
            continue
        yield chat


async def fetch_members(client, name, *, limit=None, stats=None):
    """Yield (user, group) for each retrievable participant of a megagroup.

    Two distinct reasons a list comes back short, both recorded in `stats` so
    the caller can tell them apart:

    - Broadcast channels expose no participants to non-admins at all.
    - Admins of groups with 100+ members can hide the list
      (channels.toggleParticipantsHidden). The server then returns an empty
      participants array while `count` still reports the true total, so a
      hidden group looks like a tiny one unless the flag is read.

    Size alone does not restrict enumeration: a large group whose admin has not
    hidden the list enumerates normally.
    """
    entity = await client.get_entity(name)

    total = getattr(entity, "participants_count", None)
    hidden = None
    try:
        full = (await client(GetFullChannelRequest(channel=entity))).full_chat
        total = full.participants_count
        hidden = getattr(full, "participants_hidden", None)
    except RESOLUTION_ERRORS as exc:
        log.debug("no full-channel metadata for %r: %s", name, exc)

    retrieved = 0
    try:
        async for user in client.iter_participants(entity, limit=limit):
            retrieved += 1
            yield user, entity
    except ChatAdminRequiredError as exc:
        msg = (
            f"{name!r} does not expose participants: broadcast channels are "
            "admin-only. Member listing works on megagroups (public groups)."
        )
        raise PermissionError(msg) from exc
    finally:
        if stats is not None:
            stats["retrieved"] = retrieved
            stats["total"] = total
            stats["hidden"] = hidden


async def fetch_messages(client, name, *, since=None, until=None, limit=None):
    """Yield (entity, message) newest first, stopping once older than `since`.

    Walking backwards from `until` and breaking on `since` means we stop paging
    as soon as the window closes, rather than reading history to the beginning.
    """
    try:
        entity = await client.get_entity(name)
    except RESOLUTION_ERRORS as exc:
        msg = f"could not resolve channel {name!r}: {exc}"
        raise LookupError(msg) from exc

    async for message in client.iter_messages(
        entity, offset_date=until, limit=limit
    ):
        if since is not None and message.date < since:
            break
        yield entity, message
