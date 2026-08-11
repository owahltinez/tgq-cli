"""tgq: a read-only Telegram client for the terminal.

Search for channels, inspect their metadata, and export their history.
Read-only is a deliberate boundary, not an unfinished state -- see the Scope
section of README.md.

Every command emits JSONL on stdout and logs to stderr, so stages chain:

    tgq search fnideq --kind megagroup
    tgq messages ceuta_news --since 2026-07-27 > msgs.jsonl
    tgq forwards < msgs.jsonl | jq -s 'group_by(.origin)'
    tgq activity --as-of 2026-08-11 < msgs.jsonl

Analysis that is not Telegram-specific is out of scope by design; pipe rows to
whatever does sentiment, text parsing or network detection downstream.
"""

import asyncio
import json
import logging
import sys
import time
import urllib.error
from datetime import UTC, datetime

import click

from tgq import analyze, records
from tgq import preview as preview_mod
from tgq.client import (
    SEARCH_LIMIT,
    MissingCredentialsError,
    build_client,
    fetch_channels,
    fetch_members,
    fetch_messages,
    search_channels,
)
from tgq.names import normalise_channel

DATE_FORMATS = [
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
]
# Persistent across working directories: the session is a long-lived
# credential, not per-project state.
DEFAULT_SESSION = "~/.config/tgq/tgq.session"

# Below this share of a group's members, the sample is too truncated to
# reason about and the log line is raised to a warning.
COVERAGE_WARN = 0.9


def _emit(row):
    """Write one JSONL row, flushing so the tool behaves inside a pipe."""
    json.dump(row, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _read_jsonl(stream):
    """Parse JSONL from a stream, skipping blank lines.

    A decode error names its line: a bare traceback gives no way to find the
    bad record in a million-row file.
    """
    for number, line in enumerate(stream, 1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            yield json.loads(stripped)
        except json.JSONDecodeError as exc:
            msg = f"malformed JSON on line {number}: {exc}"
            raise click.ClickException(msg) from exc


def _utc(value):
    """Attach UTC to a naive CLI datetime, so it compares to message dates."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _normalised(name):
    """Normalise a channel name, reporting a rejection as a clean CLI error."""
    try:
        return normalise_channel(name)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from exc


def _run(coro):
    """Drive an async command, turning credential errors into CLI errors."""
    try:
        asyncio.run(coro)
    except (MissingCredentialsError, LookupError, PermissionError) as exc:
        raise click.ClickException(str(exc)) from exc


session_option = click.option(
    "--session",
    default=DEFAULT_SESSION,
    show_default=True,
    help="Telethon session file. First run prompts for phone and login code.",
)


@click.group()
# Read from installed metadata rather than a second literal in this file: two
# copies of a version drift, and the one in pyproject.toml is what ships.
@click.version_option(package_name="tgq-cli", prog_name="tgq")
@click.option("-v", "--verbose", is_flag=True, help="Debug logging on stderr.")
def cli(verbose):
    """Read public Telegram channels from the terminal, as JSONL."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


# --- network commands -----------------------------------------------------


@cli.command()
@click.argument("names", nargs=-1)
@click.option(
    "--from-file", type=click.File("r"), help="One channel name per line."
)
@session_option
def channels(names, from_file, session):
    """Resolve channel names to metadata rows.

    Emits subscribers, verification flag, kind and creation date -- the fields a
    public-figure or frame-eligibility decision is made from.
    """
    targets = list(names)
    if from_file:
        targets += [line.strip() for line in from_file if line.strip()]
    if not targets:
        raise click.UsageError("give at least one channel name, or --from-file")

    targets = [_normalised(t) for t in targets]

    _run(_collect_channels(targets, session))


async def _collect_channels(targets, session):
    client = build_client(session)
    resolved = 0
    async with client:
        async for entity in fetch_channels(client, targets):
            _emit(
                records.channel_record(entity, retrieved_at=datetime.now(UTC))
            )
            resolved += 1

    logging.getLogger(__name__).info(
        "resolved %d of %d channels", resolved, len(targets)
    )
    if resolved == 0:
        msg = f"resolved none of the {len(targets)} requested channels"
        raise click.ClickException(msg)


@cli.command()
@click.argument("query")
@click.option(
    "--kind",
    type=click.Choice(["all", "broadcast", "megagroup"]),
    default="all",
    show_default=True,
    help="Restrict to broadcast channels or to groups.",
)
@click.option(
    "--limit",
    type=int,
    default=SEARCH_LIMIT,
    show_default=True,
    help="Requested result cap. Telegram caps far lower server-side.",
)
@session_option
def search(query, kind, limit, session):
    """Find public channels and groups matching QUERY.

    Emits the same rows as `channels`, so results pipe straight into
    `messages` or `preview`.

    Telegram's global search matches username and title only, and returns
    single-digit result counts in practice. An empty result is NOT evidence
    that no such channel exists -- discovery on Telegram is genuinely weak,
    and a sampling frame built from this must say so.
    """
    _run(_collect_search(query, session, kind=kind, limit=limit))


async def _collect_search(query, session, *, kind, limit):
    client = build_client(session)
    found = 0
    async with client:
        async for entity in search_channels(
            client, query, limit=limit, kind=kind
        ):
            _emit(
                records.channel_record(entity, retrieved_at=datetime.now(UTC))
            )
            found += 1

    logging.getLogger(__name__).info("%r: %d results", query, found)


@cli.command()
@click.argument("channel")
@click.option(
    "--since",
    type=click.DateTime(DATE_FORMATS),
    help="Inclusive lower bound (UTC).",
)
@click.option(
    "--until",
    type=click.DateTime(DATE_FORMATS),
    help="Start paging back from here (UTC).",
)
@click.option("--limit", type=int, help="Stop after this many messages.")
@session_option
def messages(channel, since, until, limit, session):
    """Fetch message history for one channel, newest first.

    Includes views, forwards, reactions and forward origin, which are the inputs
    for reach estimation and cascade analysis.
    """
    _run(
        _collect_messages(
            _normalised(channel),
            session,
            since=_utc(since),
            until=_utc(until),
            limit=limit,
        )
    )


async def _collect_messages(channel, session, *, since, until, limit):
    client = build_client(session)
    async with client:
        count = 0
        async for entity, message in fetch_messages(
            client, channel, since=since, until=until, limit=limit
        ):
            _emit(
                records.message_record(
                    message, entity, retrieved_at=datetime.now(UTC)
                )
            )
            count += 1
        logging.getLogger(__name__).info("%s: %d messages", channel, count)


@cli.command()
@click.argument("channel")
@click.option("--limit", type=int, help="Stop after this many members.")
@session_option
def members(channel, limit, session):
    """List the members of a public group (megagroup).

    Emits account flags, last-seen and user ids -- the inputs for
    co-membership and account-cohort analysis. Batch-registered accounts
    cluster in user_id space, since ids are broadly sequential over time.

    Coverage is reported on stderr and MUST be read before using the output.
    Broadcast channels expose no participants at all, and admins of groups with
    100+ members can hide the list -- the server then returns almost nothing
    while still reporting the true total, so a hidden group is
    indistinguishable from a tiny one unless you read the warning.
    """
    _run(_collect_members(_normalised(channel), session, limit=limit))


async def _collect_members(channel, session, *, limit):
    client = build_client(session)
    stats = {}
    try:
        async with client:
            async for user, group in fetch_members(
                client, channel, limit=limit, stats=stats
            ):
                _emit(
                    records.member_record(
                        user, group, retrieved_at=datetime.now(UTC)
                    )
                )
    finally:
        _report_coverage(channel, stats, limit=limit)


def _report_coverage(channel, stats, *, limit=None):
    """Log retrieved-versus-total, naming which cause applies.

    Three causes look identical in the row count and must not: the caller's own
    --limit, an admin-hidden member list, and genuine incompleteness. Warning
    about Telegram when the operator did the truncating is worse than silence,
    because it invites a conclusion about the platform from your own flag.
    """
    retrieved, total = stats.get("retrieved"), stats.get("total")
    if retrieved is None:
        return

    log = logging.getLogger(__name__)
    if not total:
        log.info(
            "%s: %d members retrieved, group total unknown", channel, retrieved
        )
        return

    share = retrieved / total
    stem = "%s: %d of %d members retrieved (%.1f%%)"
    args = (channel, retrieved, total, share * 100)

    if limit is not None and retrieved >= limit:
        log.info(stem + " -- stopped at your --limit", *args)
    elif stats.get("hidden"):
        log.warning(
            stem + " -- the admin has HIDDEN this group's member list, so "
            "enumeration is blocked server-side and this is not a sample of "
            "the membership",
            *args,
        )
    elif share < COVERAGE_WARN:
        log.warning(
            stem + " -- incomplete; treat as a sample of unknown bias rather "
            "than a member list",
            *args,
        )
    else:
        log.info(stem + " coverage", *args)


# --- no-account backend ---------------------------------------------------


@cli.command()
@click.argument("channel")
@click.option(
    "--since",
    type=click.DateTime(DATE_FORMATS),
    help="Stop paging once posts are older than this (UTC).",
)
@click.option("--limit", type=int, help="Stop after this many messages.")
@click.option(
    "--pages",
    type=int,
    default=50,
    show_default=True,
    help="Page cap, as a backstop against walking a channel's whole history.",
)
@click.option(
    "--delay",
    type=float,
    default=1.0,
    show_default=True,
    help="Seconds between page requests.",
)
def preview(channel, since, limit, pages, delay):
    """Collect from t.me/s/CHANNEL with no account or credentials.

    Lower fidelity than `messages`: no reactions, forward counts or numeric
    channel id, and those fields are emitted as null rather than substituted.
    Text, timestamps, view counts and forward origin are all present, so the
    same downstream filters apply.
    """
    channel = _normalised(channel)
    since = _utc(since)
    emitted, before = 0, None
    seen = set()

    for page_number in range(pages):
        try:
            html = preview_mod.fetch_page(channel, before=before)
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            # Rows already emitted stay on stdout; the non-zero exit is what
            # tells the caller the collection is incomplete.
            msg = f"fetching {channel} at cursor {before} failed: {exc}"
            raise click.ClickException(msg) from exc

        page = preview_mod.parse_page(
            html, channel=channel, retrieved_at=datetime.now(UTC)
        )
        if page_number == 0 and page.subscribers is not None:
            logging.getLogger(__name__).info(
                "%s: %d subscribers", channel, page.subscribers
            )

        if not page.messages:
            # An empty FIRST page is not an empty window. Public groups
            # render no history at t.me/s/, so silence here would read as
            # "no matching posts" when the target is not previewable.
            if page_number == 0:
                click.echo(
                    f"warning: {channel} returned no previewable posts. "
                    "t.me/s/ serves broadcast channels only -- if this is a "
                    "public group, or the name is wrong, use `tgq messages` "
                    "with a session instead.",
                    err=True,
                )
            break

        # Pages arrive oldest-first within the page; reverse so output is
        # newest-first and matches the `messages` command.
        fresh = 0
        for row in reversed(page.messages):
            if since is not None and _older_than(row, since):
                return

            # Deduplicate by id rather than trusting the cursor. A stuck or
            # non-decreasing cursor otherwise re-emits a whole page, and
            # duplicated rows are silently wrong rather than visibly broken.
            if row["message_id"] in seen:
                continue

            seen.add(row["message_id"])
            fresh += 1
            _emit(row)
            emitted += 1
            if limit is not None and emitted >= limit:
                return

        if fresh == 0:
            logging.getLogger(__name__).warning(
                "page at cursor %s contained no new messages; stopping", before
            )
            break

        if page.next_before is None:
            break

        before = page.next_before
        time.sleep(delay)


def _older_than(row, since):
    """True when a row predates the requested window."""
    stamp = row.get("date")
    if stamp is None:
        return False

    try:
        parsed = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        logging.getLogger(__name__).warning("unparseable row date: %r", stamp)
        return False

    # Rows are UTC end to end; a naive stamp must not raise mid-collection.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)

    return parsed < since


# --- stdin filters (no network) -------------------------------------------


@cli.command()
def forwards():
    """Emit origin -> relay forward edges from message JSONL on stdin.

    Edge latency is the gap between origin post and relay; implausibly tight
    latency across many relays is the strongest coordination signal available.
    """
    for edge in analyze.forward_edges(_read_jsonl(sys.stdin)):
        _emit(edge)


@cli.command()
@click.option(
    "--as-of",
    type=click.DateTime(DATE_FORMATS),
    help="Timestamp to measure silence against (UTC). Defaults to now.",
)
def activity(as_of):
    """Per-channel activity span and days_silent, from message JSONL on stdin.

    Most-dormant first, which surfaces channels that stopped posting after an
    event. What counts as dormant is left to the caller.
    """
    reference = (_utc(as_of) or datetime.now(UTC)).isoformat()
    for row in analyze.channel_activity(
        _read_jsonl(sys.stdin), as_of=reference
    ):
        _emit(row)
