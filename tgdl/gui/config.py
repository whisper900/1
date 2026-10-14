"""Where the desktop app keeps its settings and login session."""

import json
import os
import sys
import time
from pathlib import Path

APP_NAME = "TelegramDownloader"


def app_dir():
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def session_path():
    # Telethon adds the ".session" extension.
    return app_dir() / "account"


class Config(dict):
    """A dict persisted as JSON next to the session file."""

    def __init__(self, path=None):
        super().__init__()
        self.path = Path(path) if path else app_dir() / "settings.json"
        try:
            self.update(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    @property
    def has_api(self):
        return str(self.get("api_id", "")).isdigit() and bool(self.get("api_hash"))


def _chat_cache_path():
    return app_dir() / "chats.json"


def chat_cache_age(account_id, path=None):
    """Seconds since the saved chat list was refreshed (None if there is none)."""
    path = Path(path) if path else _chat_cache_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("account") == account_id and data.get("saved_at"):
            return max(0.0, time.time() - float(data["saved_at"]))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def load_chat_cache(account_id, path=None):
    """Chat list saved by the last refresh for this account, or ``[]``."""
    path = Path(path) if path else _chat_cache_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict) or data.get("account") != account_id:
        return []
    chats = []
    for chat in data.get("chats", []):
        if len(chat) == 3:          # saved by version 1.0.5
            chat = [*chat, ""]
        if len(chat) == 4:
            chats.append(tuple(chat))
    return chats


def save_chat_cache(account_id, chats, path=None):
    path = Path(path) if path else _chat_cache_path()
    tmp = path.with_suffix(".tmp")
    data = {"account": account_id, "saved_at": time.time(),
            "chats": [list(chat) for chat in chats]}
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def clear_chat_cache(path=None):
    try:
        (Path(path) if path else _chat_cache_path()).unlink()
    except OSError:
        pass
