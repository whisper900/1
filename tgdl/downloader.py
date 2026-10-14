"""The download loop: walk a chat, pick files, save them per sender."""

import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from telethon import errors

from .filters import MessageFilter, file_ext, media_kind
from .naming import (default_filename, display_name, render_folder, sanitize,
                     sender_fields, with_message_suffix)
from .state import DownloadLog

DEFAULT_FOLDER = "{chat}/{sender}"
FOLDER_FIELDS = ("chat", "chat_id", "sender", "sender_id", "username",
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


@dataclass
class Stats:
    matched: int = 0
    queued: int = 0           # new files picked for download (or dry run)
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    bytes: int = 0
    failures: list = field(default_factory=list)


def human_size(num):
    if num is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024


class Downloader:
    def __init__(self, client, chat, options, chat_name=None, log=print):
        self.client = client
        self.chat = chat
        self.opts = options
        self.chat_name = chat_name or display_name(chat) or str(chat.id)
        self.log = log
        self.stats = Stats()
        self.out_dir = Path(options.out_dir)
        self.state = DownloadLog(self.out_dir / ".tgdl" / f"{chat.id}.tsv")
        self._reserved = set()
        self._me = None
        self._sleep = asyncio.sleep
        self._show_progress = (options.parallel == 1 and sys.stdout.isatty()
                               and log is print)

    async def run(self):
        sem = asyncio.Semaphore(max(1, self.opts.parallel))
        tasks = set()
        try:
            async for msg in self._messages():
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
            "type": kind,
            "year": f"{msg.date:%Y}" if msg.date else "nodate",
            "month": f"{msg.date:%m}" if msg.date else "nodate",
            **sender_fields(sender),
        }
        folder = self.out_dir / render_folder(self.opts.folder, fields)
        if msg.file.name:
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
                        msg, file=str(part),
                        progress_callback=self._progress(rel) if self._show_progress else None)
                    if not written:
                        raise RuntimeError("message has no downloadable media")
                    os.replace(written, target)
                    break
                except errors.FloodWaitError as e:
                    self.log(f"[{msg.id}] Telegram asked to wait {e.seconds}s, waiting...")
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
                    await self._sleep(delay)
        except Exception as e:  # noqa: BLE001 - one bad file must not stop the run
            self.stats.failed += 1
            self.stats.failures.append((msg.id, str(rel), f"{type(e).__name__}: {e}"))
            self._clear_progress()
            self.log(f"[{msg.id}] FAILED {rel}: {type(e).__name__}: {e}")
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

    def _progress(self, rel):
        def callback(current, total):
            pct = f"{current * 100 // total:3d}%" if total else "   ?"
            sys.stdout.write(f"\r  {pct} {human_size(current)} / {human_size(total)}  {rel}"[:120].ljust(120))
            sys.stdout.flush()
        return callback

    def _clear_progress(self):
        if self._show_progress:
            sys.stdout.write("\r" + " " * 120 + "\r")
