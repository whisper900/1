import pytest

from tgdl.filters import (MessageFilter, file_ext, media_kind, normalize_exts,
                          parse_size, sender_matches)
from tests.fakes import FakeMsg, user


def test_media_kind():
    assert media_kind(FakeMsg(1, kind="photo")) == "photo"
    assert media_kind(FakeMsg(1, has_media=False)) is None
    assert media_kind(FakeMsg(1, kind="photo", web_preview=object())) is None


def test_media_kind_prefers_specific_kinds():
    msg = FakeMsg(1, kind="video_note")
    msg.video = True  # round videos are also videos in Telegram
    assert media_kind(msg) == "video_note"


def test_file_ext_prefers_file_name():
    assert file_ext(FakeMsg(1, name="Report.PDF", ext=".bin")) == ".pdf"
    assert file_ext(FakeMsg(1, name=None, ext=".jpg")) == ".jpg"
    assert file_ext(FakeMsg(1, has_media=False)) == ""


@pytest.mark.parametrize("text, expected", [
    ("2048", 2048), ("1K", 1024), ("10MB", 10 * 1024 ** 2),
    ("1.5 gb", int(1.5 * 1024 ** 3)), ("3MiB", 3 * 1024 ** 2),
])
def test_parse_size(text, expected):
    assert parse_size(text) == expected


def test_parse_size_invalid():
    with pytest.raises(ValueError):
        parse_size("lots")


def test_sender_matches():
    ahmed = user(10, "Ahmed", "Ali", username="ahmed_a")
    assert sender_matches(ahmed, [])
    assert sender_matches(ahmed, ["ahmed ali"])
    assert sender_matches(ahmed, ["@Ahmed_A"])
    assert sender_matches(ahmed, ["10"])
    assert not sender_matches(ahmed, ["Ahmed"])
    assert not sender_matches(None, ["Ahmed"])


def test_message_filter():
    sender = user(1, "A")
    pdf = FakeMsg(1, name="a.pdf", content=b"x" * 100)
    flt = MessageFilter(exts=normalize_exts(["PDF", ".zip"]))
    assert flt.matches(pdf, "document", sender)
    assert not flt.matches(FakeMsg(2, name="a.doc"), "document", sender)
    assert not MessageFilter(kinds=frozenset({"photo"})).matches(pdf, "document", sender)
    assert not MessageFilter(min_size=101).matches(pdf, "document", sender)
    assert not MessageFilter(max_size=99).matches(pdf, "document", sender)
    assert MessageFilter(min_size=100, max_size=100).matches(pdf, "document", sender)
    assert not MessageFilter().matches(pdf, "sticker", sender)
