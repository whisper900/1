"""Where the desktop app keeps its settings and login session."""

import json
import os
import sys
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
