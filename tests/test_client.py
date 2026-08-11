"""Resolution and failure handling in the network layer.

Exercised with a fake client: the behaviour the README advertises -- skip a name
that will not resolve, but stop on a rate limit -- was previously asserted
nowhere, and the blanket handler did the opposite of what was documented.
"""

import asyncio

import pytest
from telethon.errors import ChatAdminRequiredError, FloodWaitError

from tgq.client import (
    fetch_channels,
    fetch_members,
    fetch_messages,
    search_channels,
)


class _Entity:
    def __init__(self, name):
        self.id = hash(name) % 10_000
        self.username = name


class _FakeClient:
    """Resolves names, except those mapped to an exception in `failures`."""

    def __init__(self, failures=None):
        self.failures = failures or {}
        self.calls = []

    async def get_entity(self, name):
        self.calls.append(name)
        if name in self.failures:
            raise self.failures[name]
        return _Entity(name)


def _drain(agen):
    async def run():
        return [x async for x in agen]

    return asyncio.run(run())


def _flood(seconds):
    return FloodWaitError(request=None, capture=seconds)


# --- fetch_channels -------------------------------------------------------


def test_unresolvable_name_is_skipped_and_the_rest_continue():
    client = _FakeClient({"bad": ValueError("no such entity")})

    got = _drain(fetch_channels(client, ["good1", "bad", "good2"]))

    assert [e.username for e in got] == ["good1", "good2"]
    assert client.calls == ["good1", "bad", "good2"]


def test_rate_limit_aborts_instead_of_hammering_the_remaining_names():
    """A blanket except logged FloodWait as 'could not resolve' and kept going,
    issuing a request for every remaining name and worsening the limit."""
    client = _FakeClient({"b": _flood(86400)})

    with pytest.raises(FloodWaitError):
        _drain(fetch_channels(client, ["a", "b", "c", "d"]))

    assert client.calls == ["a", "b"]


# --- fetch_messages -------------------------------------------------------


def test_unresolvable_channel_raises_a_useful_error():
    client = _FakeClient({"nope": ValueError("no such entity")})

    with pytest.raises(LookupError, match="nope"):
        _drain(fetch_messages(client, "nope"))


# --- search_channels ------------------------------------------------------


class _Chat:
    def __init__(self, name, *, broadcast=True, megagroup=False, count=10):
        self.id = abs(hash(name)) % 10_000
        self.username = name
        self.title = name.title()
        self.broadcast = broadcast
        self.megagroup = megagroup
        self.participants_count = count
        self.verified = False
        self.date = None


class _SearchResult:
    def __init__(self, chats, users):
        self.chats = chats
        self.users = users


class _SearchClient:
    """Stands in for the MTProto request object protocol."""

    def __init__(self, result):
        self.result = result
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        return self.result


def test_search_returns_chats_and_drops_users():
    """contacts.Search returns users too; a channel search must not emit them."""
    client = _SearchClient(
        _SearchResult(chats=[_Chat("a"), _Chat("b")], users=[object()])
    )

    got = _drain(search_channels(client, "q", limit=50))

    assert [c.username for c in got] == ["a", "b"]


def test_search_can_filter_to_broadcast_channels_only():
    client = _SearchClient(
        _SearchResult(
            chats=[
                _Chat("chan", broadcast=True),
                _Chat("grp", broadcast=False, megagroup=True),
            ],
            users=[],
        )
    )

    got = _drain(search_channels(client, "q", limit=50, kind="broadcast"))

    assert [c.username for c in got] == ["chan"]


def test_search_can_filter_to_megagroups_only():
    client = _SearchClient(
        _SearchResult(
            chats=[
                _Chat("chan", broadcast=True),
                _Chat("grp", broadcast=False, megagroup=True),
            ],
            users=[],
        )
    )

    got = _drain(search_channels(client, "q", limit=50, kind="megagroup"))

    assert [c.username for c in got] == ["grp"]


def test_search_passes_the_query_and_limit_through():
    client = _SearchClient(_SearchResult(chats=[], users=[]))

    _drain(search_channels(client, "fnideq", limit=25))

    assert client.requests[0].q == "fnideq"
    assert client.requests[0].limit == 25


# --- fetch_members --------------------------------------------------------


class _MemberClient:
    """iter_participants plus the full-channel count, as Telethon exposes them."""

    def __init__(
        self, users, total, *, entity_error=None, iter_error=None, hidden=False
    ):
        self.users = users
        self.total = total
        self.entity_error = entity_error
        self.iter_error = iter_error
        self.hidden = hidden

    async def get_entity(self, name):
        if self.entity_error:
            raise self.entity_error
        return _Chat(name, broadcast=False, megagroup=True, count=self.total)

    async def __call__(self, request):
        full = type(
            "F",
            (),
            {
                "participants_count": self.total,
                "participants_hidden": self.hidden,
            },
        )()
        return type("R", (), {"full_chat": full})()

    def iter_participants(self, entity, limit=None):
        users, error = self.users, self.iter_error

        class _Agen:
            def __aiter__(self):
                self._i = 0
                return self

            async def __anext__(self):
                if error:
                    raise error
                if self._i >= len(users):
                    raise StopAsyncIteration
                self._i += 1
                return users[self._i - 1]

        return _Agen()


def test_members_yields_users_with_the_group_entity():
    client = _MemberClient([_Entity("a"), _Entity("b")], total=2)

    got = _drain(fetch_members(client, "grp"))

    assert [u.username for u, _ in got] == ["a", "b"]
    assert all(getattr(chat, "megagroup", False) for _, chat in got)


def test_members_reports_coverage_against_the_true_total():
    """A 0.03% list and a 100% list are indistinguishable without this."""
    client = _MemberClient([_Entity("a")], total=1000)
    stats = {}

    _drain(fetch_members(client, "grp", stats=stats))

    assert stats["retrieved"] == 1
    assert stats["total"] == 1000


def test_broadcast_channel_admin_error_becomes_a_clear_failure():
    client = _MemberClient(
        [], total=0, iter_error=ChatAdminRequiredError(request=None)
    )

    with pytest.raises(PermissionError, match="megagroup"):
        _drain(fetch_members(client, "chan"))


def test_members_records_the_participants_hidden_flag():
    """Coverage of 0% means different things when the admin hid the list.

    Admins of 100+ member groups can call channels.toggleParticipantsHidden;
    the server then returns an empty participants array while count still
    reports the true total.
    """
    client = _MemberClient([_Entity("a")], total=14679, hidden=True)
    stats = {}

    _drain(fetch_members(client, "grp", stats=stats))

    assert stats["hidden"] is True
    assert stats["total"] == 14679
