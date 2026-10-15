"""Deleting downloaded files a set time after they were saved.

The schedule is a small JSON file next to the settings. While the app runs
it deletes due files itself; when it closes with files still pending it
starts a background copy of itself (``--cleanup``) that waits, deletes them
and exits. That helper steps aside as soon as the app is running again.
"""

import json
import os
import sys
import time
from pathlib import Path

from .config import app_dir

OWNER_FRESH = 60        # seconds a heartbeat file counts as "still running"
MAX_NAP = 30            # the helper re-checks at least this often


def store_path():
    return app_dir() / "cleanup.json"


def _app_flag(store):
    return Path(store).with_name("cleanup.app")


def _helper_flag(store):
    return Path(store).with_name("cleanup.helper")


def load(store):
    try:
        data = json.loads(Path(store).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict) and "path" in e and "at" in e]


def _save(store, entries):
    store = Path(store)
    tmp = store.with_suffix(".tmp")
    try:
        if entries:
            tmp.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, store)
        elif store.exists():
            store.unlink()
    except OSError:
        pass


def schedule(store, files, delay, root=None, now=None):
    """Delete ``files`` ``delay`` seconds from now (``root`` bounds folder pruning)."""
    when = (now if now is not None else time.time()) + delay
    entries = {e["path"]: e for e in load(store)}
    for path in files:
        entries[str(path)] = {"path": str(path), "at": when,
                              "root": str(root) if root else None}
    _save(store, sorted(entries.values(), key=lambda e: e["at"]))


def pending(store):
    return sorted(load(store), key=lambda e: e["at"])


def next_due(store):
    entries = pending(store)
    return entries[0]["at"] if entries else None


def _prune_empty_dirs(path, root):
    """Remove folders left empty by a deletion, up to (not including) ``root``."""
    if not root:
        return
    root = Path(root).resolve()
    folder = Path(path).resolve().parent
    while folder != root and root in folder.parents:
        try:
            folder.rmdir()           # only succeeds when empty
        except OSError:
            return
        folder = folder.parent


def clean_due(store, now=None):
    """Delete every due file. Returns how many were deleted.

    A file that is open (Windows refuses to delete it) stays scheduled and is
    retried next time; a file already gone is simply forgotten.
    """
    now = now if now is not None else time.time()
    keep, deleted = [], 0
    for entry in load(store):
        if entry["at"] > now:
            keep.append(entry)
            continue
        try:
            os.remove(entry["path"])
            deleted += 1
            _prune_empty_dirs(entry["path"], entry.get("root"))
        except FileNotFoundError:
            pass
        except OSError:
            keep.append(entry)
    _save(store, keep)
    return deleted


def _touch(flag, text=""):
    try:
        Path(flag).write_text(text, encoding="utf-8")
    except OSError:
        pass


def _fresh(flag, now):
    try:
        return now - Path(flag).stat().st_mtime < OWNER_FRESH
    except OSError:
        return False


def app_heartbeat(store):
    """Called regularly by the running app: tells a helper to step aside."""
    _touch(_app_flag(store), str(os.getpid()))


def app_closed(store):
    try:
        _app_flag(store).unlink()
    except OSError:
        pass


def run_helper(store, sleep=time.sleep, clock=time.time):
    """Background mode: wait for due files, delete them, exit when none are left."""
    helper = _helper_flag(store)
    if _fresh(helper, clock()):
        return 0                       # another helper is already on it
    try:
        while True:
            if _fresh(_app_flag(store), clock()):
                return 0               # the app is open again and takes over
            _touch(helper, str(os.getpid()))
            clean_due(store, clock())
            upcoming = next_due(store)
            if upcoming is None:
                return 0
            sleep(min(MAX_NAP, max(1.0, upcoming - clock())))
    finally:
        try:
            helper.unlink()
        except OSError:
            pass


def helper_command():
    """How to start the background helper for this installation."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--cleanup"]
    return [sys.executable, "-m", "tgdl.gui.app", "--cleanup"]
