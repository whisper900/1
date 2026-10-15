"""Self-update of the Windows exe from the repository's GitHub releases.

Each CI build publishes a release ``v1.0.<build>`` with the exe attached.
The app compares that with its own version, downloads the new exe next to
itself, checks its size and SHA-256, swaps it in and restarts.

A running exe cannot be overwritten on Windows, but it can be renamed, so
the old file becomes ``TelegramDownloader.exe.old`` and is deleted on the
next start.
"""

import hashlib
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from .. import __version__

REPO = "whisper900/1"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET_NAME = "TelegramDownloader.exe"
USER_AGENT = "TelegramDownloader-updater"


class UpdateError(Exception):
    """Message is shown to the user (Arabic)."""


@dataclass
class Release:
    version: str
    url: str
    size: int
    sha256: str = None
    notes: str = ""


def parse_version(text):
    parts = str(text).strip().lstrip("vV").split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def is_newer(latest, current):
    new, old = parse_version(latest), parse_version(current)
    return new is not None and old is not None and new > old


def parse_release(data):
    for asset in data.get("assets") or []:
        if asset.get("name") == ASSET_NAME:
            digest = asset.get("digest") or ""
            return Release(
                version=str(data.get("tag_name", "")).lstrip("vV"),
                url=asset["browser_download_url"],
                size=int(asset.get("size") or 0),
                sha256=digest[7:].lower() if digest.startswith("sha256:") else None,
                notes=(data.get("body") or "").strip(),
            )
    return None


def _request(url, accept="application/octet-stream"):
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})


def fetch_latest(urlopen=urllib.request.urlopen, timeout=15):
    try:
        with urlopen(_request(LATEST_URL, "application/vnd.github+json"),
                     timeout=timeout) as response:
            return parse_release(json.load(response))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError(
                "لا يمكن الوصول إلى صفحة التحديثات. يجب أن يكون مستودع البرنامج على "
                "GitHub عاماً (Public) حتى يعمل التحديث التلقائي.") from e
        raise UpdateError(f"تعذّر التحقق من التحديثات (خطأ {e.code}).") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise UpdateError("تعذّر الاتصال بالإنترنت للتحقق من التحديثات.") from e


def download(release, dest, progress=None, cancelled=lambda: False,
             urlopen=urllib.request.urlopen):
    """Download ``release`` to ``dest`` and verify it; raise UpdateError."""
    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    done = 0
    try:
        with urlopen(_request(release.url), timeout=30) as response, part.open("wb") as fh:
            while True:
                if cancelled():
                    raise UpdateError("تم إلغاء التحديث.")
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, release.size)
        if release.size and done != release.size:
            raise UpdateError("اكتمل التنزيل ناقصاً. حاول مرة أخرى.")
        if release.sha256 and digest.hexdigest() != release.sha256:
            raise UpdateError("الملف المنزّل تالف (لا يطابق البصمة). حاول مرة أخرى.")
        os.replace(part, dest)
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError("انقطع تنزيل التحديث. تأكد من الإنترنت وحاول مرة أخرى.") from e
    finally:
        if part.exists():
            part.unlink()
    return dest


def old_path(exe):
    exe = Path(exe)
    return exe.with_name(exe.name + ".old")


def install(new_file, exe):
    """Swap ``new_file`` in place of the (possibly running) ``exe``."""
    exe, old = Path(exe), old_path(exe)
    if old.exists():
        old.unlink()
    os.replace(exe, old)
    try:
        os.replace(new_file, exe)
    except OSError:
        os.replace(old, exe)
        raise


def cleanup_old(exe):
    try:
        old_path(exe).unlink()
    except OSError:
        pass


def launch_detached(args):
    """Start ``args`` as an independent process that outlives this one."""
    # A onefile exe passes its unpack folder to children through these
    # variables; the new process must unpack its own files instead.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("_PYI_") and k != "_MEIPASS2"}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(a) for a in args], env=env, close_fds=True, creationflags=flags)


def restart(exe):
    """Start ``exe`` as a fresh, independent process."""
    launch_detached([exe])


def can_self_update():
    return getattr(sys, "frozen", False) and sys.platform == "win32"


class Updater(QObject):
    checked = Signal(object, bool)        # Release or None, manual check
    check_failed = Signal(str, bool)      # message, manual check
    progress = Signal(int, int)
    downloaded = Signal(str)
    download_failed = Signal(str)

    def __init__(self, current=__version__):
        super().__init__()
        self.current = current
        self._cancel = threading.Event()

    def check(self, manual=False):
        def run():
            try:
                release = fetch_latest()
            except UpdateError as e:
                self.check_failed.emit(str(e), manual)
                return
            newer = release if release and is_newer(release.version, self.current) else None
            self.checked.emit(newer, manual)
        threading.Thread(target=run, name="update-check", daemon=True).start()

    def download(self, release, exe=None):
        exe = Path(exe or sys.executable)
        self._cancel.clear()

        def run():
            try:
                path = download(release, exe.with_name(exe.name + ".new"),
                                progress=self.progress.emit, cancelled=self._cancel.is_set)
            except UpdateError as e:
                self.download_failed.emit(str(e))
                return
            self.downloaded.emit(str(path))
        threading.Thread(target=run, name="update-download", daemon=True).start()

    def cancel(self):
        self._cancel.set()
