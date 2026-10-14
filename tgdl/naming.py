"""Turning Telegram names into safe folder and file names."""

import re
import string
from pathlib import Path

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
# Most filesystems cap a single name at 255 bytes; leave room for suffixes
# such as " (msg 123456)" and ".part".
MAX_NAME_BYTES = 180


def sanitize(name, fallback="unknown"):
    """Make a single path component safe on Windows, macOS and Linux."""
    name = _INVALID_CHARS.sub("_", str(name or ""))
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    if not name:
        return fallback
    if name.split(".")[0].upper() in _WINDOWS_RESERVED:
        name = "_" + name
    return _truncate_bytes(name, MAX_NAME_BYTES)


def _truncate_bytes(name, limit):
    """Truncate to ``limit`` UTF-8 bytes, keeping the extension if possible."""
    if len(name.encode("utf-8")) <= limit:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot or len(ext) > 10:
        stem, ext = name, ""
    else:
        ext = "." + ext
    budget = limit - len(ext.encode("utf-8"))
    stem = stem.encode("utf-8")[:budget].decode("utf-8", "ignore").rstrip(". ")
    return stem + ext


def display_name(entity):
    """Human readable name of a user, group or channel (empty if unknown)."""
    if entity is None:
        return ""
    title = getattr(entity, "title", None)
    if title:
        return title
    first = getattr(entity, "first_name", None) or ""
    last = getattr(entity, "last_name", None) or ""
    return f"{first} {last}".strip()


def sender_fields(sender):
    """Values available to the ``--folder`` template for a message sender."""
    sender_id = getattr(sender, "id", None)
    username = getattr(sender, "username", None) or ""
    name = display_name(sender)
    if not name:
        name = f"@{username}" if username else (
            f"user_{sender_id}" if sender_id else "unknown")
    return {
        "sender": name,
        "sender_id": str(sender_id or "unknown"),
        "username": username or str(sender_id or "unknown"),
    }


def render_folder(template, fields):
    """Fill ``template`` (e.g. ``"{chat}/{sender}"``) with sanitized values.

    Every ``/`` in the template becomes a sub-folder; values themselves can
    never introduce extra folders or escape the output directory.
    """
    parts = []
    for raw_part in template.replace("\\", "/").split("/"):
        if not raw_part.strip():
            continue
        values = {
            key: sanitize(fields.get(key, ""))
            for _, key, _, _ in string.Formatter().parse(raw_part) if key
        }
        missing = [key for key in values if key not in fields]
        if missing:
            raise ValueError(
                f"unknown placeholder {{{missing[0]}}} in folder template; "
                f"available: {', '.join('{%s}' % k for k in sorted(fields))}")
        part = sanitize(raw_part.format(**values))
        if part in (".", ".."):
            part = "_"
        parts.append(part)
    return Path(*parts) if parts else Path(".")


def default_filename(kind, msg_id, date, ext):
    """File name for media without one (photos, voice notes, ...)."""
    stamp = date.strftime("%Y-%m-%d_%H-%M-%S") if date else "nodate"
    return f"{kind}_{stamp}_{msg_id}{ext or ''}"


def with_message_suffix(filename, msg_id):
    """``report.pdf`` -> ``report (msg 42).pdf`` to avoid name clashes."""
    path = Path(filename)
    return f"{path.stem} (msg {msg_id}){path.suffix}"
