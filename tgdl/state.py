"""Remembering which messages were already downloaded, so runs can resume."""

from pathlib import Path


class DownloadLog:
    """Append-only ``<message id>\\t<relative path>`` file, one per chat."""

    def __init__(self, path):
        self.path = Path(path)
        self._entries = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                msg_id, sep, rel_path = line.partition("\t")
                if sep and msg_id.isdigit():
                    self._entries[int(msg_id)] = rel_path

    def get(self, msg_id):
        return self._entries.get(msg_id)

    def add(self, msg_id, rel_path):
        rel_path = Path(rel_path).as_posix()
        self._entries[msg_id] = rel_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(f"{msg_id}\t{rel_path}\n")

    def __len__(self):
        return len(self._entries)
