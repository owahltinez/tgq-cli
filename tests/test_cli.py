"""The stdin filters are exercised end-to-end; they need no network."""

import email.message
import json
import logging
import urllib.error
from importlib.metadata import version
from pathlib import Path

import pytest
from click.testing import CliRunner

from tgq.cli import DEFAULT_SESSION, _report_coverage, cli
from tgq.client import (
    MissingCredentialsError,
    build_client,
    prepare_session_path,
)


def _lines(output):
    return [json.loads(line) for line in output.splitlines() if line.strip()]


@pytest.fixture
def runner():
    return CliRunner()


MESSAGES = [
    {
        "channel_id": 100,
        "channel_username": "relay_a",
        "message_id": 1,
        "date": "2026-07-30T06:15:00+00:00",
        "fwd_from_channel_id": 555,
        "fwd_from_msg_id": 31,
        "fwd_from_date": "2026-07-30T06:10:00+00:00",
        "fwd_from_name": None,
    },
    {
        "channel_id": 200,
        "channel_username": "relay_b",
        "message_id": 2,
        "date": "2026-08-09T00:00:00+00:00",
        "fwd_from_channel_id": None,
        "fwd_from_msg_id": None,
        "fwd_from_date": None,
        "fwd_from_name": None,
    },
]


def _stdin(rows):
    return "\n".join(json.dumps(r) for r in rows) + "\n"


# --- forwards -------------------------------------------------------------


def test_forwards_emits_one_edge_per_forward(runner):
    result = runner.invoke(cli, ["forwards"], input=_stdin(MESSAGES))

    assert result.exit_code == 0
    (edge,) = _lines(result.output)
    assert edge["origin"] == "555"
    assert edge["relay"] == "100"
    assert edge["latency_seconds"] == 300.0


def test_forwards_on_empty_input_emits_nothing(runner):
    result = runner.invoke(cli, ["forwards"], input="")

    assert result.exit_code == 0
    assert _lines(result.output) == []


# --- activity -------------------------------------------------------------


def test_activity_reports_days_silent_against_as_of(runner):
    result = runner.invoke(
        cli,
        ["activity", "--as-of", "2026-08-11T00:00:00+00:00"],
        input=_stdin(MESSAGES),
    )

    assert result.exit_code == 0
    rows = _lines(result.output)
    assert [r["channel_id"] for r in rows] == [100, 200]
    # 30 Jul 06:15 -> 11 Aug 00:00 is 11d 17h45m; partials are kept.
    assert rows[0]["days_silent"] == 11.74
    assert rows[1]["days_silent"] == 2.0


def test_activity_defaults_as_of_to_now(runner):
    result = runner.invoke(cli, ["activity"], input=_stdin(MESSAGES))

    assert result.exit_code == 0
    assert all(r["days_silent"] > 0 for r in _lines(result.output))


# --- argument handling ----------------------------------------------------


def test_version_reports_the_installed_distribution(runner):
    """A bug report is unactionable without the version that produced it."""
    result = runner.invoke(cli, ["--version"])

    assert result.exit_code == 0
    assert version("tgq-cli") in result.output


def test_channels_requires_a_target(runner):
    result = runner.invoke(cli, ["channels"])

    assert result.exit_code != 0
    assert "at least one channel name" in result.output


def test_missing_credentials_is_a_clean_error(runner, monkeypatch):
    monkeypatch.delenv("TG_API_ID", raising=False)
    monkeypatch.delenv("TG_API_HASH", raising=False)

    result = runner.invoke(cli, ["channels", "ceuta_news"])

    assert result.exit_code != 0
    assert "TG_API_ID" in result.output


def test_build_client_raises_without_credentials(monkeypatch):
    monkeypatch.delenv("TG_API_ID", raising=False)
    monkeypatch.delenv("TG_API_HASH", raising=False)

    with pytest.raises(MissingCredentialsError):
        build_client("unused.session")


# --- preview: empty result must not look like "no posts in window" -------


def test_preview_warns_when_first_page_is_empty(runner, monkeypatch):
    """A public GROUP renders no messages at t.me/s/; a channel does.

    Silently emitting nothing would read as "no matching posts" when the real
    cause is that the target is not previewable at all.
    """
    monkeypatch.setattr(
        "tgq.preview.fetch_page",
        lambda channel, before=None: "<section></section>",
    )

    result = runner.invoke(cli, ["preview", "somegroup"])

    assert result.exit_code == 0
    assert "no previewable posts" in result.output.lower()
    assert "group" in result.output.lower()


# --- session persistence --------------------------------------------------


def test_default_session_lives_under_the_home_directory():
    assert Path(DEFAULT_SESSION).expanduser().is_relative_to(Path.home())


def test_prepare_session_path_creates_parent_owner_only(tmp_path):
    """The session authenticates as the account, so 0700 not the umask."""
    target = tmp_path / "nested" / "tgq.session"

    resolved = prepare_session_path(target)

    assert resolved.parent.is_dir()
    assert oct(resolved.parent.stat().st_mode)[-3:] == "700"


def test_prepare_session_path_leaves_an_existing_directory_alone(tmp_path):
    # Must never chmod a directory it did not create -- a bare filename would
    # otherwise re-permission the working directory.
    existing = tmp_path / "already"
    existing.mkdir(mode=0o755)
    before = oct(existing.stat().st_mode)[-3:]

    prepare_session_path(existing / "tgq.session")

    assert oct(existing.stat().st_mode)[-3:] == before


def test_build_client_restricts_the_session_file_to_the_owner(
    tmp_path, monkeypatch
):
    """The session file IS the credential; sqlite creates it at the umask.

    The 0700 parent only protects the default path. A `--session ./tgq.session`
    in a shared or synced directory leaves an account-takeover credential
    world-readable unless the file itself is restricted.
    """
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "x")
    target = tmp_path / "existing" / "tgq.session"
    target.parent.mkdir(mode=0o755)

    build_client(str(target))

    assert oct(target.stat().st_mode)[-3:] == "600"


def test_prepare_session_path_expands_tilde():
    resolved = prepare_session_path("~/.config/tgq/tgq.session")

    assert "~" not in str(resolved)
    assert resolved.is_relative_to(Path.home())


# --- preview pagination safety -------------------------------------------


def _page_html(before, ids):
    posts = "".join(
        f'<div class="tgme_widget_message" data-post="c/{i}">'
        f'<div class="tgme_widget_message_bubble">'
        f'<a class="tgme_widget_message_date">'
        f'<time datetime="2026-08-0{1 + (i % 8)}T00:00:00+00:00"></time></a>'
        f"</div></div>"
        for i in ids
    )
    more = (
        f'<a class="tme_messages_more" data-before="{before}">m</a>'
        if before is not None
        else ""
    )
    return f'<section class="tgme_channel_history">{posts}{more}</section>'


def test_preview_stops_when_the_cursor_does_not_advance(runner, monkeypatch):
    """A stuck cursor re-emitted the same page until --pages was exhausted.

    Regression: silent duplication of collected rows, exit 0.
    """
    monkeypatch.setattr(
        "tgq.preview.fetch_page",
        lambda channel, before=None: _page_html(438, [438]),
    )

    result = runner.invoke(
        cli, ["preview", "chan1", "--pages", "5", "--delay", "0"]
    )

    assert result.exit_code == 0
    assert len(_lines(result.output)) == 1


def test_preview_rejects_an_unresolvable_channel_name(runner):
    result = runner.invoke(cli, ["preview", "https://t.me/c/123/4"])

    assert result.exit_code != 0
    assert "not a public channel" in result.output


# --- argument validation --------------------------------------------------


def test_activity_rejects_an_unparseable_as_of(runner):
    """Silently accepting it emitted every row with days_silent null, exit 0."""
    result = runner.invoke(
        cli, ["activity", "--as-of", "not-a-date"], input=_stdin(MESSAGES)
    )

    assert result.exit_code != 0


def test_malformed_jsonl_names_the_offending_line(runner):
    result = runner.invoke(cli, ["forwards"], input='{"ok":1}\nnot json\n')

    assert result.exit_code != 0
    assert "line 2" in result.output


# --- failure signalling ---------------------------------------------------


class _NullClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_channels_exits_nonzero_when_nothing_resolves(runner, monkeypatch):
    """Exiting 0 with empty stdout is indistinguishable from 'no channels'."""

    async def _none(client, names):
        for _ in names:
            if False:
                yield None

    monkeypatch.setattr("tgq.cli.build_client", lambda s: _NullClient())
    monkeypatch.setattr("tgq.cli.fetch_channels", _none)

    result = runner.invoke(cli, ["channels", "chan1", "chan2"])

    assert result.exit_code != 0
    assert "resolved" in result.output.lower()


def test_preview_reports_an_http_error_cleanly(runner, monkeypatch):
    """A raw HTTPError traceback gave no indication of what to do."""

    def _boom(channel, before=None):
        raise urllib.error.HTTPError(
            "https://t.me/s/x",
            429,
            "Too Many Requests",
            email.message.Message(),
            None,
        )

    monkeypatch.setattr("tgq.preview.fetch_page", _boom)

    result = runner.invoke(cli, ["preview", "chan1"])

    assert result.exit_code != 0
    assert "429" in result.output
    assert "Traceback" not in result.output


# --- member coverage reporting -------------------------------------------


def _coverage(caplog, stats, limit=None):
    """Return (level, message) pairs emitted by the coverage reporter."""
    with caplog.at_level(logging.INFO, logger="tgq.cli"):
        _report_coverage("grp", stats, limit=limit)
    return [(r.levelname, r.getMessage()) for r in caplog.records]


def test_coverage_does_not_blame_telegram_for_your_own_limit(caplog):
    """Warning about truncation when --limit caused it invites a false
    conclusion about the platform from the operator's own flag."""
    ((level, message),) = _coverage(
        caplog, {"retrieved": 3, "total": 67}, limit=3
    )

    assert level == "INFO"
    assert "--limit" in message


def test_coverage_names_a_hidden_member_list(caplog):
    ((level, message),) = _coverage(
        caplog, {"retrieved": 5, "total": 14679, "hidden": True}
    )

    assert level == "WARNING"
    assert "HIDDEN" in message


def test_coverage_warns_on_genuine_incompleteness(caplog):
    ((level, message),) = _coverage(
        caplog, {"retrieved": 5, "total": 100, "hidden": False}
    )

    assert level == "WARNING"
    assert "unknown bias" in message


def test_full_coverage_is_not_a_warning(caplog):
    ((level, _),) = _coverage(
        caplog, {"retrieved": 67, "total": 67, "hidden": False}
    )

    assert level == "INFO"
