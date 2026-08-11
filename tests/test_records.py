"""Normalisation is pure: duck-typed Telethon objects, never a client."""

from datetime import UTC, datetime
from types import SimpleNamespace

from bs4 import BeautifulSoup

from tgq.preview import _record
from tgq.records import channel_record, member_record, message_record

RETRIEVED = datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC)


def _channel(**kw):
    base = {
        "id": 100,
        "username": "ceuta_news",
        "title": "Ceuta News",
        "participants_count": 4200,
        "verified": False,
        "broadcast": True,
        "megagroup": False,
        "date": datetime(2024, 1, 5, tzinfo=UTC),
    }
    return SimpleNamespace(**{**base, **kw})


def _message(**kw):
    base = {
        "id": 77,
        "date": datetime(2026, 7, 30, 6, 15, tzinfo=UTC),
        "message": "crossing at tarajal now",
        "views": 1500,
        "forwards": 42,
        "replies": None,
        "reactions": None,
        "fwd_from": None,
        "media": None,
        "post_author": None,
        "edit_date": None,
        "grouped_id": None,
        "entities": None,
    }
    return SimpleNamespace(**{**base, **kw})


# --- provenance -----------------------------------------------------------


def test_message_record_carries_provenance_triple():
    rec = message_record(_message(), _channel(), retrieved_at=RETRIEVED)

    assert rec["source_url"] == "https://t.me/ceuta_news/77"
    assert rec["retrieved_at"] == "2026-08-11T12:00:00+00:00"
    assert rec["source_kind"] == "primary"


def test_source_url_falls_back_to_private_channel_form():
    rec = message_record(
        _message(), _channel(username=None), retrieved_at=RETRIEVED
    )

    assert rec["source_url"] == "https://t.me/c/100/77"


def test_dates_are_serialised_as_utc_iso8601():
    rec = message_record(_message(), _channel(), retrieved_at=RETRIEVED)

    assert rec["date"] == "2026-07-30T06:15:00+00:00"


# --- reach fields ---------------------------------------------------------


def test_reach_fields_are_preserved():
    rec = message_record(_message(), _channel(), retrieved_at=RETRIEVED)

    assert rec["views"] == 1500
    assert rec["forwards"] == 42


def test_replies_count_read_from_nested_object():
    msg = _message(replies=SimpleNamespace(replies=9))

    rec = message_record(msg, _channel(), retrieved_at=RETRIEVED)

    assert rec["replies_count"] == 9


def test_reactions_flattened_to_emoji_counts():
    reactions = SimpleNamespace(
        results=[
            SimpleNamespace(reaction=SimpleNamespace(emoticon="👍"), count=12),
            SimpleNamespace(reaction=SimpleNamespace(emoticon="😢"), count=3),
        ]
    )
    msg = _message(reactions=reactions)

    rec = message_record(msg, _channel(), retrieved_at=RETRIEVED)

    assert rec["reactions"] == {"👍": 12, "😢": 3}


def test_missing_reach_fields_become_none_not_zero():
    # A real zero and "not returned" mean different things for reach estimation.
    msg = _message(views=None, forwards=None)

    rec = message_record(msg, _channel(), retrieved_at=RETRIEVED)

    assert rec["views"] is None
    assert rec["forwards"] is None


# --- forward provenance ---------------------------------------------------


def test_forward_fields_absent_when_not_a_forward():
    rec = message_record(_message(), _channel(), retrieved_at=RETRIEVED)

    assert rec["fwd_from_channel_id"] is None
    assert rec["fwd_from_date"] is None


def test_forward_fields_extracted_from_fwd_from():
    fwd = SimpleNamespace(
        from_id=SimpleNamespace(channel_id=555),
        from_name=None,
        date=datetime(2026, 7, 30, 6, 10, tzinfo=UTC),
        channel_post=31,
    )
    msg = _message(fwd_from=fwd)

    rec = message_record(msg, _channel(), retrieved_at=RETRIEVED)

    assert rec["fwd_from_channel_id"] == 555
    assert rec["fwd_from_date"] == "2026-07-30T06:10:00+00:00"
    assert rec["fwd_from_msg_id"] == 31


def test_forward_from_name_kept_when_origin_is_hidden():
    # Channels can forward with the origin hidden; the name is all we get.
    fwd = SimpleNamespace(
        from_id=None,
        from_name="Anon Relay",
        date=datetime(2026, 7, 30, 6, 10, tzinfo=UTC),
        channel_post=None,
    )
    msg = _message(fwd_from=fwd)

    rec = message_record(msg, _channel(), retrieved_at=RETRIEVED)

    assert rec["fwd_from_channel_id"] is None
    assert rec["fwd_from_name"] == "Anon Relay"


# --- channel records ------------------------------------------------------


def test_channel_record_fields():
    rec = channel_record(_channel(), retrieved_at=RETRIEVED)

    assert rec["channel_id"] == 100
    assert rec["username"] == "ceuta_news"
    assert rec["title"] == "Ceuta News"
    assert rec["subscribers"] == 4200
    assert rec["verified"] is False
    assert rec["kind"] == "broadcast"
    assert rec["created_at"] == "2024-01-05T00:00:00+00:00"
    assert rec["source_url"] == "https://t.me/ceuta_news"


def test_channel_kind_distinguishes_megagroup():
    rec = channel_record(
        _channel(broadcast=False, megagroup=True), retrieved_at=RETRIEVED
    )

    assert rec["kind"] == "megagroup"


# --- reaction variants beyond plain emoji --------------------------------


def test_custom_emoji_reactions_are_kept_not_dropped():
    """ReactionCustomEmoji carries document_id, not emoticon.

    Regression: keying only on .emoticon silently discarded these, producing
    undercounted reaction totals with no error raised.
    """
    reactions = SimpleNamespace(
        results=[
            SimpleNamespace(reaction=SimpleNamespace(emoticon="👍"), count=12),
            SimpleNamespace(
                reaction=SimpleNamespace(document_id=5789123), count=7
            ),
        ]
    )
    msg = _message(reactions=reactions)

    rec = message_record(msg, _channel(), retrieved_at=RETRIEVED)

    assert rec["reactions"] == {"👍": 12, "custom:5789123": 7}


def test_unrecognised_reaction_variant_does_not_crash():
    # ReactionEmpty and future variants carry neither attribute.
    reactions = SimpleNamespace(
        results=[SimpleNamespace(reaction=SimpleNamespace(), count=3)]
    )
    msg = _message(reactions=reactions)

    rec = message_record(msg, _channel(), retrieved_at=RETRIEVED)

    assert rec["reactions"] is None


# --- forwards originating from a user, not a channel ---------------------


def test_forward_from_user_keeps_the_user_id():
    """PeerUser carries user_id; reading only channel_id lost the origin."""
    fwd = SimpleNamespace(
        from_id=SimpleNamespace(user_id=90210),
        from_name=None,
        date=datetime(2026, 7, 30, 6, 10, tzinfo=UTC),
        channel_post=None,
    )
    msg = _message(fwd_from=fwd)

    rec = message_record(msg, _channel(), retrieved_at=RETRIEVED)

    assert rec["fwd_from_channel_id"] is None
    assert rec["fwd_from_user_id"] == 90210


def test_paid_star_reactions_are_counted():
    """ReactionPaid (Telegram Stars) has no emoticon and no document_id.

    Regression: it is a real reaction with a real count -- 47 of 786 observed
    live -- and identity-by-attribute dropped all of them.
    """

    class ReactionPaid:  # name matters: the variant carries no fields at all
        pass

    reactions = SimpleNamespace(
        results=[
            SimpleNamespace(reaction=SimpleNamespace(emoticon="👍"), count=12),
            SimpleNamespace(reaction=ReactionPaid(), count=8),
        ]
    )
    msg = _message(reactions=reactions)

    rec = message_record(msg, _channel(), retrieved_at=RETRIEVED)

    assert rec["reactions"] == {"👍": 12, "paid": 8}


# --- has_media should mean attached media, not link previews --------------


def test_link_preview_is_not_counted_as_media():
    """MessageMediaWebPage is auto-generated from a URL in the text.

    Counting it inflated has_media and disagreed with the preview backend on 5
    of 15 rows for the same window.
    """

    class MessageMediaWebPage:
        pass

    rec = message_record(
        _message(media=MessageMediaWebPage()),
        _channel(),
        retrieved_at=RETRIEVED,
    )

    assert rec["has_media"] is False


def test_attached_photo_is_counted_as_media():
    class MessageMediaPhoto:
        pass

    rec = message_record(
        _message(media=MessageMediaPhoto()), _channel(), retrieved_at=RETRIEVED
    )

    assert rec["has_media"] is True


# --- cross-backend schema parity ------------------------------------------


def test_message_record_key_set_matches_the_preview_backend():
    """The two backends must emit identical keys.

    Regression: preview lacked fwd_from_date and MTProto lacked
    fwd_from_username. Because jq evaluates `null < 5` as true, a missing
    latency made every preview-derived forward pass the documented
    sub-5-second coordination filter -- fabricating findings from absent data.
    """
    node = BeautifulSoup(
        '<div class="tgme_widget_message" data-post="c/1"></div>', "html.parser"
    ).select_one(".tgme_widget_message")

    mtproto = message_record(_message(), _channel(), retrieved_at=RETRIEVED)
    preview = _record(node, channel="c", retrieved_at=RETRIEVED)

    assert set(mtproto) == set(preview)


# --- member records -------------------------------------------------------


def _user(**kw):
    base = {
        "id": 90210,
        "username": "someone",
        "first_name": "Some",
        "last_name": "One",
        "bot": False,
        "deleted": False,
        "premium": False,
        "verified": False,
        "scam": False,
        "fake": False,
        "status": None,
        "lang_code": "en",
    }
    return SimpleNamespace(**{**base, **kw})


def test_member_record_carries_identity_and_channel_context():
    rec = member_record(_user(), _channel(), retrieved_at=RETRIEVED)

    assert rec["user_id"] == 90210
    assert rec["username"] == "someone"
    assert rec["first_name"] == "Some"
    assert rec["last_name"] == "One"
    assert rec["channel_id"] == 100
    assert rec["channel_username"] == "ceuta_news"
    assert rec["source_url"] == "https://t.me/someone"
    assert rec["retrieved_at"] == "2026-08-11T12:00:00+00:00"
    assert rec["source_kind"] == "primary"


def test_member_without_username_has_no_source_url():
    rec = member_record(
        _user(username=None), _channel(), retrieved_at=RETRIEVED
    )

    assert rec["username"] is None
    assert rec["source_url"] is None


def test_account_flags_are_preserved_for_cib_analysis():
    rec = member_record(
        _user(bot=True, premium=True, scam=True, deleted=False),
        _channel(),
        retrieved_at=RETRIEVED,
    )

    assert rec["is_bot"] is True
    assert rec["is_premium"] is True
    assert rec["is_scam"] is True
    assert rec["is_deleted"] is False


def test_last_seen_extracted_when_the_status_carries_a_timestamp():
    class UserStatusOffline:
        was_online = datetime(2026, 8, 1, 9, 30, tzinfo=UTC)

    rec = member_record(
        _user(status=UserStatusOffline()), _channel(), retrieved_at=RETRIEVED
    )

    assert rec["last_seen"] == "2026-08-01T09:30:00+00:00"
    assert rec["status_kind"] == "UserStatusOffline"


def test_privacy_bucketed_status_has_a_kind_but_no_timestamp():
    """UserStatusRecently and friends are buckets, not times."""

    class UserStatusRecently:
        pass

    rec = member_record(
        _user(status=UserStatusRecently()), _channel(), retrieved_at=RETRIEVED
    )

    assert rec["last_seen"] is None
    assert rec["status_kind"] == "UserStatusRecently"
