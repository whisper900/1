"""Customer orders: the files downloaded per chat until marked as delivered.

Also counts printable pages (images, PDF, Word, PowerPoint) for pricing.
Files are only ever read here.
"""

import json
import os
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from ..pdfmerge import IMAGE_EXTS

GROUPS = (
    ("images", "صورة", IMAGE_EXTS),
    ("pdf", "PDF", {".pdf"}),
    ("word", "وورد", {".doc", ".docx", ".odt", ".rtf"}),
    ("excel", "إكسل", {".xls", ".xlsx", ".csv", ".ods"}),
    ("powerpoint", "باوربوينت", {".ppt", ".pptx", ".pps", ".ppsx", ".odp"}),
)


def group_of(path):
    ext = Path(path).suffix.lower()
    for key, _, exts in GROUPS:
        if ext in exts:
            return key
    return "other"


def _office_pages(path, tag):
    """Page/slide count Word or PowerPoint saved in docProps/app.xml, or None."""
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("docProps/app.xml").decode("utf-8", "ignore")
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    match = re.search(rf"<{tag}>(\d+)</{tag}>", xml)
    return int(match.group(1)) if match and int(match.group(1)) > 0 else None


def _pdf_pages(path):
    try:
        import pypdfium2
        with pypdfium2.PdfDocument(str(path)) as doc:
            return len(doc)
    except Exception:  # noqa: BLE001 - missing library or broken PDF: unknown
        return None


def count_pages(path):
    """Printed pages for one file, or None when it can't be known."""
    group = group_of(path)
    if group == "images":
        return 1
    if group == "pdf":
        return _pdf_pages(path)
    ext = Path(path).suffix.lower()
    if ext == ".docx":
        return _office_pages(path, "Pages")
    if ext == ".pptx":
        return _office_pages(path, "Slides")
    return None


@dataclass
class OrderSummary:
    files: int = 0
    counts: dict = field(default_factory=dict)    # group -> files
    pages: int = 0
    unknown: list = field(default_factory=list)   # files whose pages are unknown
    price: float = 0.0

    def describe(self):
        """``"80 صورة · 2 PDF (34 صفحة) · 1 وورد"``-style line."""
        labels = dict((key, label) for key, label, _ in GROUPS)
        parts = []
        for key, _, _ in GROUPS:
            if self.counts.get(key):
                parts.append(f"{self.counts[key]} {labels[key]}")
        if self.counts.get("other"):
            parts.append(f"{self.counts['other']} ملف آخر")
        return " · ".join(parts)


def summarize(paths, price_per_page=0.0, page_cache=None):
    summary = OrderSummary()
    cache = page_cache if page_cache is not None else {}
    for path in paths:
        summary.files += 1
        group = group_of(path)
        summary.counts[group] = summary.counts.get(group, 0) + 1
        try:
            stat = os.stat(path)
            key = (str(path), stat.st_size, stat.st_mtime)
        except OSError:
            continue
        if key not in cache:
            cache[key] = count_pages(path)
        pages = cache[key]
        if pages is None:
            summary.unknown.append(str(path))
        else:
            summary.pages += pages
    summary.price = summary.pages * (price_per_page or 0)
    return summary


def within_days(entries, days, now=None):
    """Entries sent today (days=0), since yesterday (1), ... in local time."""
    import datetime as dt
    today = dt.datetime.fromtimestamp(now if now is not None else time.time()).date()
    first = today - dt.timedelta(days=days)
    return [e for e in entries if dt.datetime.fromtimestamp(e["sent"]).date() >= first]


class ChatMarks:
    """The newest message already downloaded from each chat, so "download new
    files" can start right after it instead of at the start of the chat."""

    def __init__(self, path):
        self.path = Path(path)

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, chat_id):
        return int(self._load().get(str(chat_id), 0))

    def advance(self, chat_id, msg_id):
        if not msg_id:
            return
        data = self._load()
        if msg_id > int(data.get(str(chat_id), 0)):
            data[str(chat_id)] = int(msg_id)
            tmp = self.path.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps(data), encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError:
                pass


class OrderBook:
    """``{chat id: {"name", "files": [{"path", "kind", "msg_id", "at"}]}}`` on disk."""

    def __init__(self, path):
        self.path = Path(path)

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data):
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def add(self, chat_id, chat_name, path, kind=None, msg_id=None, sent=None):
        """``sent``: when the customer sent the file (a timestamp)."""
        data = self._load()
        order = data.setdefault(str(chat_id), {"name": chat_name, "files": []})
        order["name"] = chat_name
        if not any(f["path"] == str(path) for f in order["files"]):
            order["files"].append({"path": str(path), "kind": kind, "msg_id": msg_id,
                                   "at": time.time(), "sent": sent})
        self._save(data)

    def entries(self, chat_id):
        """The order's files that still exist, in message order, as dicts with
        ``path`` (Path), ``kind``, ``msg_id`` and ``sent`` (timestamp)."""
        order = self._load().get(str(chat_id), {"files": []})
        result = []
        for entry in sorted(order["files"], key=lambda f: (f.get("msg_id") or 0, f["at"])):
            path = Path(entry["path"])
            if path.exists():
                result.append({**entry, "path": path,
                               "sent": entry.get("sent") or entry["at"]})
        return result

    def files(self, chat_id):
        """Paths of the chat's order that still exist, in message order."""
        return [entry["path"] for entry in self.entries(chat_id)]

    def remove(self, chat_id, paths):
        """Take some files out of an order (the files themselves stay)."""
        data = self._load()
        order = data.get(str(chat_id))
        if order is None:
            return
        drop = {str(p) for p in paths}
        order["files"] = [f for f in order["files"] if f["path"] not in drop]
        if not order["files"]:
            data.pop(str(chat_id))
        self._save(data)

    def last_at(self, chat_id):
        files = self._load().get(str(chat_id), {"files": []})["files"]
        return max((f["at"] for f in files), default=None)

    def counts(self):
        """Chat id -> number of existing files, for every open order."""
        result = {}
        for chat_id, order in self._load().items():
            count = sum(1 for f in order["files"] if Path(f["path"]).exists())
            if count:
                result[int(chat_id)] = count
        return result

    def clear(self, chat_id):
        data = self._load()
        if data.pop(str(chat_id), None) is not None:
            self._save(data)
