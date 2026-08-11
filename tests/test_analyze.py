"""Analysis filters are pure and operate on the JSONL records, not on Telegram.

Both are stdin filters in the CLI, so they must work on plain dicts.
"""

from tgq.analyze import channel_activity, forward_edges


def _rec(**kw):
    base = {
        "channel_id": 100,
        "channel_username": "relay_a",
        "message_id": 77,
        "date": "2026-07-30T06:15:00+00:00",
        "fwd_from_channel_id": None,
        "fwd_from_name": None,
        "fwd_from_msg_id": None,
        "fwd_from_date": None,
    }
    return {**base, **kw}


# --- forward edges --------------------------------------------------------


def test_non_forwards_produce_no_edges():
    assert list(forward_edges([_rec()])) == []


def test_forward_edge_carries_origin_relay_and_latency():
    rec = _rec(
        fwd_from_channel_id=555,
        fwd_from_msg_id=31,
        fwd_from_date="2026-07-30T06:10:00+00:00",
    )

    (edge,) = list(forward_edges([rec]))

    assert edge["origin"] == "555"
    assert edge["relay"] == "100"
    assert edge["origin_msg_id"] == 31
    assert edge["relay_msg_id"] == 77
    assert edge["latency_seconds"] == 300.0


def test_latency_is_none_when_origin_date_missing():
    rec = _rec(fwd_from_channel_id=555, fwd_from_date=None)

    (edge,) = list(forward_edges([rec]))

    assert edge["latency_seconds"] is None


def test_hidden_origin_falls_back_to_name_as_node_id():
    # Origin channel hidden: the name is the only stable node key available.
    rec = _rec(
        fwd_from_channel_id=None,
        fwd_from_name="Anon Relay",
        fwd_from_date="2026-07-30T06:10:00+00:00",
    )

    (edge,) = list(forward_edges([rec]))

    assert edge["origin"] == "name:Anon Relay"
    assert edge["origin_hidden"] is True


def test_negative_latency_is_preserved_not_clamped():
    # Clock skew and edited forwards can invert this; silently clamping would
    # hide a data-quality problem that matters for cascade analysis.
    rec = _rec(
        fwd_from_channel_id=555,
        date="2026-07-30T06:10:00+00:00",
        fwd_from_date="2026-07-30T06:15:00+00:00",
    )

    (edge,) = list(forward_edges([rec]))

    assert edge["latency_seconds"] == -300.0


# --- channel activity / dormancy -----------------------------------------


def test_activity_aggregates_first_last_and_count():
    recs = [
        _rec(message_id=1, date="2026-07-28T10:00:00+00:00"),
        _rec(message_id=2, date="2026-07-30T06:15:00+00:00"),
        _rec(message_id=3, date="2026-07-29T09:00:00+00:00"),
    ]

    (row,) = list(channel_activity(recs, as_of="2026-08-11T00:00:00+00:00"))

    assert row["channel_id"] == 100
    assert row["first_post_at"] == "2026-07-28T10:00:00+00:00"
    assert row["last_post_at"] == "2026-07-30T06:15:00+00:00"
    assert row["n_messages"] == 3


def test_days_silent_measured_against_as_of():
    recs = [_rec(date="2026-08-01T00:00:00+00:00")]

    (row,) = list(channel_activity(recs, as_of="2026-08-11T00:00:00+00:00"))

    assert row["days_silent"] == 10.0


def test_channels_are_reported_separately():
    recs = [
        _rec(channel_id=100, channel_username="a"),
        _rec(channel_id=200, channel_username="b"),
    ]

    rows = list(channel_activity(recs, as_of="2026-08-11T00:00:00+00:00"))

    assert {r["channel_id"] for r in rows} == {100, 200}


def test_rows_sorted_by_days_silent_descending():
    # Most-dormant first: that is what the operator is scanning for.
    recs = [
        _rec(channel_id=100, date="2026-08-09T00:00:00+00:00"),
        _rec(channel_id=200, date="2026-07-31T00:00:00+00:00"),
    ]

    rows = list(channel_activity(recs, as_of="2026-08-11T00:00:00+00:00"))

    assert [r["channel_id"] for r in rows] == [200, 100]


# --- preview-backend records (no numeric channel_id) ---------------------


def _preview_rec(**kw):
    """A row from the t.me/s/ backend: username identity, no numeric id."""
    base = {
        "channel_id": None,
        "channel_username": "relay_a",
        "message_id": 438,
        "date": "2026-07-30T06:15:00+00:00",
        "fwd_from_channel_id": None,
        "fwd_from_username": None,
        "fwd_from_name": None,
        "fwd_from_msg_id": None,
        "fwd_from_date": None,
    }
    return {**base, **kw}


def test_relay_node_key_falls_back_to_username():
    rec = _preview_rec(fwd_from_username="origin_chan", fwd_from_msg_id=10112)

    (edge,) = list(forward_edges([rec]))

    assert edge["relay"] == "@relay_a"


def test_origin_node_key_uses_fwd_username_when_no_numeric_id():
    rec = _preview_rec(fwd_from_username="origin_chan", fwd_from_msg_id=10112)

    (edge,) = list(forward_edges([rec]))

    assert edge["origin"] == "@origin_chan"
    assert edge["origin_hidden"] is False
    assert edge["origin_msg_id"] == 10112


def test_numeric_id_still_wins_over_username():
    rec = _preview_rec(
        channel_id=100, fwd_from_channel_id=555, fwd_from_username="origin_chan"
    )

    (edge,) = list(forward_edges([rec]))

    assert edge["origin"] == "555"
    assert edge["relay"] == "100"


def test_preview_forward_without_origin_link_is_hidden():
    rec = _preview_rec(fwd_from_name="Hidden Origin")

    (edge,) = list(forward_edges([rec]))

    assert edge["origin"] == "name:Hidden Origin"
    assert edge["origin_hidden"] is True


def test_activity_groups_preview_rows_by_username():
    recs = [
        _preview_rec(channel_username="a", date="2026-08-01T00:00:00+00:00"),
        _preview_rec(channel_username="a", date="2026-08-02T00:00:00+00:00"),
        _preview_rec(channel_username="b", date="2026-08-03T00:00:00+00:00"),
    ]

    rows = list(channel_activity(recs, as_of="2026-08-11T00:00:00+00:00"))

    assert len(rows) == 2
    by_name = {r["channel_username"]: r for r in rows}
    assert by_name["a"]["n_messages"] == 2
    assert by_name["b"]["n_messages"] == 1


# --- naive vs aware timestamps -------------------------------------------


def test_bare_date_as_of_is_treated_as_utc():
    """`--as-of 2026-08-11` parses naive; message dates are tz-aware.

    Regression: subtracting the two raised TypeError on a real run.
    """
    recs = [_rec(date="2026-08-01T00:00:00+00:00")]

    (row,) = list(channel_activity(recs, as_of="2026-08-11"))

    assert row["days_silent"] == 10.0


def test_naive_message_dates_also_compare():
    recs = [_rec(date="2026-08-01T00:00:00")]

    (row,) = list(channel_activity(recs, as_of="2026-08-11T00:00:00+00:00"))

    assert row["days_silent"] == 10.0


# --- forwards from users, not channels -----------------------------------


def test_user_origin_forms_an_edge_with_a_distinct_namespace():
    """A forward from a user is a known origin, not a hidden one.

    Without this it fell through to the from_name branch, or vanished entirely
    when the name was absent, truncating cascades.
    """
    rec = _rec(
        fwd_from_user_id=90210, fwd_from_date="2026-07-30T06:10:00+00:00"
    )

    (edge,) = list(forward_edges([rec]))

    assert edge["origin"] == "user:90210"
    assert edge["origin_hidden"] is False


def test_channel_origin_still_wins_over_user_origin():
    rec = _rec(fwd_from_channel_id=555, fwd_from_user_id=90210)

    (edge,) = list(forward_edges([rec]))

    assert edge["origin"] == "555"
