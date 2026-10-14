"""Tiny stand-ins for Telethon objects so the logic can be tested offline."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace


def user(uid, first, last=None, username=None):
    return SimpleNamespace(id=uid, first_name=first, last_name=last,
                           username=username)


def day(n):
    return datetime(2025, 1, n, 12, 0, tzinfo=timezone.utc)


@dataclass
class FakeFile:
    name: str = None
    ext: str = ""
    size: int = None


@dataclass
class FakeMsg:
    id: int
    sender: object = None
    kind: str = "document"
    name: str = None
    ext: str = ""
    content: bytes = b"data"
    date: datetime = field(default_factory=lambda: day(1))
    out: bool = False
    has_media: bool = True
    web_preview: object = None

    def __post_init__(self):
        self.media = object() if self.has_media else None
        self.file = (FakeFile(self.name, self.ext, len(self.content))
                     if self.has_media else None)
        for kind in ("sticker", "gif", "video_note", "voice", "video",
                     "audio", "photo", "document"):
            setattr(self, kind, self.has_media and kind == self.kind)

    async def get_sender(self):
        return self.sender


class FakeClient:
    def __init__(self, messages, me=None, fail=None):
        self.messages = messages
        self.me = me or user(999, "Me")
        # msg_id -> list of exceptions to raise on successive attempts
        self.fail = fail or {}
        self.download_calls = []

    async def iter_messages(self, chat, reverse=False, offset_date=None):
        assert reverse, "downloader should walk oldest first"
        for msg in sorted(self.messages, key=lambda m: m.id):
            if offset_date and msg.date <= offset_date:
                continue
            yield msg

    async def get_messages(self, chat, ids=None):
        by_id = {m.id: m for m in self.messages}
        if isinstance(ids, list):
            return [by_id.get(i) for i in ids]
        return by_id.get(ids)

    async def get_me(self):
        return self.me

    async def download_media(self, msg, file=None, progress_callback=None):
        self.download_calls.append(msg.id)
        errors = self.fail.get(msg.id)
        if errors:
            raise errors.pop(0)
        with open(file, "wb") as fh:
            fh.write(msg.content)
        return file
