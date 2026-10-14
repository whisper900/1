"""Deciding which messages carry a file we want."""

import re
from dataclasses import dataclass, field
from pathlib import PurePath

from .naming import display_name

# Checked in this order: a video sticker is also a video, a GIF is also a
# video, a round video note is also a video, ...
KINDS = ("sticker", "gif", "video_note", "voice", "video", "audio",
         "photo", "document")
DEFAULT_KINDS = frozenset(KINDS) - {"sticker"}

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)i?b?\s*$", re.IGNORECASE)
_SIZE_UNITS = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}
# mimetypes picks rarely used extensions for some common types.
_FRIENDLY_EXT = {".oga": ".ogg", ".jpe": ".jpg"}


def media_kind(msg):
    """Return the kind of downloadable file in ``msg``, or ``None``.

    Link previews, polls, locations, contacts and service messages are not
    considered files.
    """
    if msg.media is None or getattr(msg, "web_preview", None) is not None:
        return None
    for kind in KINDS:
        if getattr(msg, kind, None):
            return kind
    return None


def file_ext(msg):
    """Lower-case extension (``".pdf"``) of the file in ``msg``, or ``""``."""
    file = msg.file
    if file is None:
        return ""
    if file.name:
        suffix = PurePath(file.name).suffix
        if suffix:
            return suffix.lower()
    ext = (file.ext or "").lower()
    return _FRIENDLY_EXT.get(ext, ext)


def parse_size(text):
    """``"500K"``, ``"10MB"``, ``"1.5 GB"`` or ``"2048"`` (bytes) -> bytes."""
    match = _SIZE_RE.match(str(text))
    if not match:
        raise ValueError(f"invalid size: {text!r} (examples: 500K, 10MB, 2GB)")
    number, unit = match.groups()
    return int(float(number) * _SIZE_UNITS[unit.lower()])


def normalize_exts(values):
    return {"." + v.strip().lower().lstrip(".") for v in values if v.strip()}


def sender_matches(sender, wanted):
    """True if ``sender`` matches any of the ``--from`` values.

    A value can be a numeric id, ``@username``, a bare username or the
    display name (case-insensitive).
    """
    if not wanted:
        return True
    sender_id = str(getattr(sender, "id", "") or "")
    username = (getattr(sender, "username", None) or "").lower()
    name = display_name(sender).lower()
    for value in wanted:
        value = value.strip().lower()
        if not value:
            continue
        if value == sender_id or value == name:
            return True
        if username and value.lstrip("@") == username:
            return True
    return False


@dataclass
class MessageFilter:
    kinds: frozenset = DEFAULT_KINDS
    exts: set = field(default_factory=set)
    senders: list = field(default_factory=list)
    min_size: int = None
    max_size: int = None

    def matches(self, msg, kind, sender):
        if kind not in self.kinds:
            return False
        if self.exts and file_ext(msg) not in self.exts:
            return False
        size = msg.file.size if msg.file else None
        if self.min_size is not None and (size or 0) < self.min_size:
            return False
        if self.max_size is not None and size is not None and size > self.max_size:
            return False
        return sender_matches(sender, self.senders)
