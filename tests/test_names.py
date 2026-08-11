"""Channel-name normalisation.

Un-normalised names corrupted output rather than failing: "@x" produced the
graph node "@@x" alongside "@x", splitting one channel into two, and a pasted
t.me URL was percent-encoded into the request path.
"""

import pytest

from tgq.names import normalise_channel


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("ceuta_news", "ceuta_news"),
        ("@ceuta_news", "ceuta_news"),
        ("  @ceuta_news  ", "ceuta_news"),
        ("https://t.me/ceuta_news", "ceuta_news"),
        ("http://t.me/ceuta_news", "ceuta_news"),
        ("t.me/ceuta_news", "ceuta_news"),
        ("https://t.me/s/ceuta_news", "ceuta_news"),
        ("https://telegram.me/ceuta_news", "ceuta_news"),
        ("https://t.me/ceuta_news/", "ceuta_news"),
    ],
)
def test_normalises_every_accepted_spelling(given, expected):
    assert normalise_channel(given) == expected


def test_a_deep_link_to_a_message_keeps_only_the_channel():
    assert normalise_channel("https://t.me/ceuta_news/438") == "ceuta_news"


def test_private_channel_link_is_rejected_rather_than_mangled():
    # t.me/c/<id>/<msg> is not resolvable by username; silently returning "c"
    # would collect the wrong channel entirely.
    with pytest.raises(ValueError, match="not a public channel"):
        normalise_channel("https://t.me/c/1234567890/12")


def test_empty_input_is_rejected():
    with pytest.raises(ValueError, match="empty"):
        normalise_channel("  @  ")


def test_invite_link_is_rejected():
    with pytest.raises(ValueError, match="invite"):
        normalise_channel("https://t.me/+AbCdEf123")
