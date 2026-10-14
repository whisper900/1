from datetime import datetime
from pathlib import Path

import pytest

from tgdl.naming import (MAX_NAME_BYTES, default_filename, render_folder,
                         sanitize, sender_fields, with_message_suffix)
from tests.fakes import user


@pytest.mark.parametrize("raw, expected", [
    ("Ahmed Ali", "Ahmed Ali"),
    ("a/b\\c:d*e?f", "a_b_c_d_e_f"),
    ("  spaced   out  ", "spaced out"),
    ("trailing dots...", "trailing dots"),
    ("..", "unknown"),
    ("", "unknown"),
    (None, "unknown"),
    ("CON", "_CON"),
    ("nul.txt", "_nul.txt"),
    ("محمد أحمد", "محمد أحمد"),
])
def test_sanitize(raw, expected):
    assert sanitize(raw) == expected


def test_sanitize_truncates_by_bytes_and_keeps_extension():
    name = "ملف" * 100 + ".pdf"
    out = sanitize(name)
    assert out.endswith(".pdf")
    assert len(out.encode("utf-8")) <= MAX_NAME_BYTES
    out.encode("utf-8").decode("utf-8")  # no broken multi-byte characters


def test_sender_fields_fallbacks():
    assert sender_fields(user(1, "Sara", "K"))["sender"] == "Sara K"
    assert sender_fields(user(2, "", username="bob"))["sender"] == "@bob"
    assert sender_fields(user(3, ""))["sender"] == "user_3"
    assert sender_fields(None)["sender"] == "unknown"


def test_render_folder_cannot_escape_output_dir():
    fields = {"chat": "../../etc", "sender": "a/../../b"}
    path = render_folder("{chat}/{sender}", fields)
    assert ".." not in path.parts
    assert len(path.parts) == 2


def test_render_folder_unknown_placeholder():
    with pytest.raises(ValueError, match="unknown placeholder"):
        render_folder("{nope}", {"chat": "x"})


def test_render_folder_literal_text():
    assert render_folder("files/{sender} - {type}", {"sender": "Ali", "type": "photo"}) \
        == Path("files", "Ali - photo")


def test_filenames():
    date = datetime(2025, 3, 4, 5, 6, 7)
    assert default_filename("photo", 42, date, ".jpg") == "photo_2025-03-04_05-06-07_42.jpg"
    assert with_message_suffix("report.pdf", 42) == "report (msg 42).pdf"
