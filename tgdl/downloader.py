"""The download loop: walk a chat, pick files, save them per sender."""

import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from telethon import errors

from .filters import MessageFilter, file_ext, media_kind
from .naming import (default_filename, display_name, ext_group, highest_number,
                     render_folder, sanitize, sender_fields, with_message_suffix)
from .state import DownloadLog

DEFAULT_FOLDER = "{chat}/{sender}"
FOLDER_FIELDS = ("chat", "chat_id", "sender", "sender_id", "username", "day",
                 "type", "year", "month")


@dataclass
class Options:
    out_dir: Path = Path("downloads")
    folder: str = DEFAULT_FOLDER
    filter: MessageFilter = field(default_factory=MessageFilter)
    since: object = None      # aware datetime, inclusive
    until: object = None      # aware datetime, exclusive
    ids: list = None          # only these message ids
    limit: int = None         # stop after this many matching files
    parallel: int = 3
    dry_run: bool = False
    retries: int = 3
    type_names: dict = field(default_factory=dict)  # kind -> {type} folder name
    # "original": keep the sender's file name. "numbers": 1.jpg, 2.jpg, ... with
    # a separate sequence per folder and file group (images, Word, PDF, ...).
    naming: str = "original"


@dataclass
class Stats:
    scanned: int = 0          # messages looked at
    matched: int = 0
    queued: int = 0           # new files picked for download (or dry run)
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    bytes: int = 0
    failures: list = field(default_factory=list)


def stripped_thumbnail(msg):
    """Tiny JPEG preview that Telegram sends inside the message, or None.

    It costs no download; Telethon only has to add the JPEG headers.
    """
    media = getattr(msg, "photo", None) or getattr(msg, "document", None)
    sizes = getattr(media, "sizes", None) or getattr(media, "thumbs", None) or []
    for size in sizes:
        if type(size).__name__ == "PhotoStrippedSize":
            from telethon.utils import stripped_photo_to_jpg
            return stripped_photo_to_jpg(size.bytes)
    return None


def human_size(num):
    if num is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024


class Downloader:
    """Download the files of one chat.

    ``log`` receives human readable lines (used by the command line).
    ``on_event(name, **data)``, if given, receives the same information in a
    structured form for graphical front ends: ``planned``, ``saved``,
    ``failed``, ``progress``, ``wait`` and ``retry``.
    """

    def __init__(self, client, chat, options, chat_name=None, log=print,
                 on_event=None):
        self.client = client
        self.chat = chat
        self.opts = options
        self.chat_name = chat_name or display_name(chat) or str(chat.id)
        self.log = log
        self.on_event = on_event
        self.stats = Stats()
        self.out_dir = Path(options.out_dir)
        self.state = DownloadLog(self.out_dir / ".tgdl" / f"{chat.id}.tsv")
        self._reserved = set()
        self._counters = {}       # (folder, ext group) -> last number given
        self._me = None
        self._sleep = asyncio.sleep
        self._show_progress = (options.parallel == 1 and sys.stdout.isatty()
                               and log is print)

    async def run(self):
        sem = asyncio.Semaphore(max(1, self.opts.parallel))
        tasks = set()
        try:
            async for msg in self._messages():
                self.stats.scanned += 1
                kind = media_kind(msg)
                if kind is None:
                    continue
                sender = await self._sender_of(msg)
                if not self.opts.filter.matches(msg, kind, sender):
                    continue
                self.stats.matched += 1

                target = self._plan(msg, kind, sender)
                if target is None:
                    continue
                self.stats.queued += 1
                rel = target.relative_to(self.out_dir)
                if self.opts.dry_run:
                    self.log(f"[{msg.id}] would download {rel} "
                             f"({human_size(msg.file.size)})")
                    self._emit("planned", msg_id=msg.id, path=rel, size=msg.file.size,
                               kind=kind, date=msg.date, file_name=msg.file.name,
                               sender=sender_fields(sender)["sender"],
                               thumb=stripped_thumbnail(msg))
                    self.stats.bytes += msg.file.size or 0
                    if self.opts.limit and self.stats.queued >= self.opts.limit:
                        break
                    continue

                await sem.acquire()
                task = asyncio.create_task(self._download(msg, target))
                task.add_done_callback(lambda _: sem.release())
                tasks.add(task)
                task.add_done_callback(tasks.discard)
                if self.opts.limit and self.stats.queued >= self.opts.limit:
                    break
        except BaseException:
            # Ctrl+C or a fatal error: stop the downloads still running
            # (their partial files are removed) instead of waiting for them.
            for task in tasks:
                task.cancel()
            raise
        finally:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        return self.stats

    async def _sender_of(self, msg):
        sender = msg.sender or await msg.get_sender()
        if sender is None and getattr(msg, "out", False):
            # Telegram omits the sender of our own messages in private chats.
            if self._me is None:
                self._me = await self.client.get_me()
            sender = self._me
        return sender

    async def _messages(self):
        """Matching-date messages, oldest first."""
        if self.opts.ids:
            found = await self.client.get_messages(self.chat, ids=self.opts.ids)
            for msg in sorted(filter(None, found), key=lambda m: m.id):
                yield msg
            return

        async for msg in self.client.iter_messages(
                self.chat, reverse=True, offset_date=self.opts.since):
            if self.opts.since and msg.date < self.opts.since:
                continue
            if self.opts.until and msg.date >= self.opts.until:
                break
            yield msg

    def _plan(self, msg, kind, sender):
        """Pick the target path, or return ``None`` if already downloaded."""
        known = self.state.get(msg.id)
        if known and (self.out_dir / known).exists():
            self.stats.skipped += 1
            return None

        fields = {
            "chat": self.chat_name,
            "chat_id": str(self.chat.id),
            "type": self.opts.type_names.get(kind, kind),
            "year": f"{msg.date:%Y}" if msg.date else "nodate",
            "month": f"{msg.date:%m}" if msg.date else "nodate",
            # Calendar day the file was sent, in this computer's time zone.
            "day": f"{msg.date.astimezone():%Y-%m-%d}" if msg.date else "nodate",
            **sender_fields(sender),
        }
        folder = self.out_dir / render_folder(self.opts.folder, fields)
        if self.opts.naming == "numbers":
            name = self._next_number(folder, file_ext(msg))
        elif msg.file.name:
            name = sanitize(msg.file.name)
        else:
            name = default_filename(kind, msg.id, msg.date, file_ext(msg))

        size = msg.file.size
        for candidate in (folder / name, folder / with_message_suffix(name, msg.id)):
            if candidate in self._reserved:
                continue
            if candidate.exists():
                if size and candidate.stat().st_size == size:
                    # Same name and size: downloaded before (e.g. state lost).
                    if not self.opts.dry_run:
                        self.state.add(msg.id, candidate.relative_to(self.out_dir))
                    self.stats.skipped += 1
                    return None
                if candidate.name == name:
                    continue  # a different file owns this name
            self._reserved.add(candidate)
            return candidate
        raise AssertionError("unreachable: suffixed name is unique per message")

    def _next_number(self, folder, ext):
        """``"7.docx"``: one past the highest number already used in this group.

        Existing files are counted, so a later run continues the sequence.
        Numbers follow message order (oldest first) because they are handed
        out while planning, before the parallel downloads start.
        """
        key = (folder, ext_group(ext))
        if key not in self._counters:
            self._counters[key] = highest_number(folder, key[1])
        self._counters[key] += 1
        return f"{self._counters[key]}{ext}"

    async def _download(self, msg, target):
        rel = target.relative_to(self.out_dir)
        part = target.with_name(target.name + ".part")
        target.parent.mkdir(parents=True, exist_ok=True)
        attempt = 0
        started = time.monotonic()
        try:
            while True:
                try:
                    if part.exists():
                        part.unlink()
                    written = await self.client.download_media(
                        msg, file=str(part), progress_callback=self._progress(msg.id, rel))
                    if not written:
                        raise RuntimeError("message has no downloadable media")
                    os.replace(written, target)
                    break
                except errors.FloodWaitError as e:
                    self.log(f"[{msg.id}] Telegram asked to wait {e.seconds}s, waiting...")
                    self._emit("wait", msg_id=msg.id, seconds=e.seconds)
                    await self._sleep(e.seconds + 1)
                except errors.FileReferenceExpiredError:
                    attempt += 1
                    if attempt >= self.opts.retries:
                        raise
                    msg = await self.client.get_messages(self.chat, ids=msg.id) or msg
                except (errors.RPCError, ConnectionError, asyncio.TimeoutError) as e:
                    attempt += 1
                    if attempt >= self.opts.retries:
                        raise
                    delay = 2 ** attempt
                    self.log(f"[{msg.id}] {type(e).__name__}: {e}; retrying in {delay}s")
                    self._emit("retry", msg_id=msg.id, error=e, delay=delay)
                    await self._sleep(delay)
        except Exception as e:  # noqa: BLE001 - one bad file must not stop the run
            self.stats.failed += 1
            self.stats.failures.append((msg.id, str(rel), f"{type(e).__name__}: {e}"))
            self._clear_progress()
            self.log(f"[{msg.id}] FAILED {rel}: {type(e).__name__}: {e}")
            self._emit("failed", msg_id=msg.id, path=rel, error=e)
            return
        finally:
            self._reserved.discard(target)
            if part.exists():
                part.unlink()

        size = target.stat().st_size
        self.state.add(msg.id, rel)
        self.stats.downloaded += 1
        self.stats.bytes += size
        elapsed = max(time.monotonic() - started, 1e-6)
        self._clear_progress()
        self.log(f"[{msg.id}] saved {rel} ({human_size(size)}, "
                 f"{human_size(size / elapsed)}/s)")
        self._emit("saved", msg_id=msg.id, path=rel, size=size, speed=size / elapsed)

    def _emit(self, name, **data):
        if self.on_event is not None:
            self.on_event(name, **data)

    def _progress(self, msg_id, rel):
        if not (self._show_progress or self.on_event):
            return None

        def callback(current, total):
            self._emit("progress", msg_id=msg_id, path=rel, current=current, total=total)
            if self._show_progress:
                pct = f"{current * 100 // total:3d}%" if total else "   ?"
                line = f"\r  {pct} {human_size(current)} / {human_size(total)}  {rel}"
                sys.stdout.write(line[:120].ljust(120))
                sys.stdout.flush()
        return callback

    def _clear_progress(self):
        if self._show_progress:
            sys.stdout.write("\r" + " " * 120 + "\r")
