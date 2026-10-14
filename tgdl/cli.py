"""Command line entry point: ``python -m tgdl --help``."""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .downloader import (DEFAULT_FOLDER, FOLDER_FIELDS, Downloader,
                         Options, human_size)
from .filters import DEFAULT_KINDS, KINDS, MessageFilter, normalize_exts, parse_size
from .naming import display_name, render_folder

EPILOG = """\
examples:
  python -m tgdl --list-chats
  python -m tgdl --chat "Family" --types photo,video
  python -m tgdl --chat @some_channel --ext pdf,docx --since 2025-01-01
  python -m tgdl --chat -1001234567890 --from "Ahmed Ali" --dry-run
  python -m tgdl --chat me --folder "{sender}/{type}"

folder placeholders: {chat} {chat_id} {sender} {sender_id} {username}
                     {type} {year} {month}
"""


def load_dotenv(path=".env"):
    """Minimal ``KEY=VALUE`` loader; real environment variables win."""
    path = Path(path)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def csv_list(text):
    return [part.strip() for part in text.split(",") if part.strip()]


def parse_kinds(text):
    values = {v.lower() for v in csv_list(text)}
    if "all" in values:
        return frozenset(KINDS)
    unknown = values - set(KINDS)
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown type(s): {', '.join(sorted(unknown))}; "
            f"choose from: all, {', '.join(KINDS)}")
    return frozenset(values)


def parse_ids(text):
    try:
        return [int(v) for v in csv_list(text)]
    except ValueError:
        raise argparse.ArgumentTypeError("--ids expects numbers, e.g. 120,121,130")


def parse_date(text):
    try:
        # Interpreted as local midnight.
        return datetime.strptime(text, "%Y-%m-%d").astimezone()
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid date {text!r}, use YYYY-MM-DD")


def size_arg(text):
    try:
        return parse_size(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def build_parser():
    p = argparse.ArgumentParser(
        prog="tgdl",
        description="Log in to your Telegram account and download files from a "
                    "chat into one folder per sender.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)

    what = p.add_argument_group("what to download")
    what.add_argument("--chat", help="chat to download from: title as shown in "
                      "Telegram, @username, t.me link, numeric id (see "
                      "--list-chats) or 'me' for Saved Messages")
    what.add_argument("--types", type=parse_kinds, default=DEFAULT_KINDS,
                      help="comma-separated: all, " + ", ".join(KINDS) +
                      " (default: everything except stickers)")
    what.add_argument("--ext", type=csv_list, default=[],
                      help="only these extensions, e.g. pdf,docx,zip")
    what.add_argument("--from", dest="senders", action="append", default=[],
                      metavar="SENDER", help="only files sent by this person "
                      "(name, @username or id); repeat for several people")
    what.add_argument("--since", type=parse_date, help="from this day (YYYY-MM-DD)")
    what.add_argument("--until", type=parse_date, help="up to and including this day")
    what.add_argument("--ids", type=parse_ids,
                      help="only these message ids, e.g. 120,121")
    what.add_argument("--min-size", type=size_arg, help="e.g. 100K")
    what.add_argument("--max-size", type=size_arg, help="e.g. 500MB")
    what.add_argument("--limit", type=int, help="stop after this many files")

    where = p.add_argument_group("where to save")
    where.add_argument("--out", type=Path, default=Path("downloads"),
                       help="output folder (default: downloads)")
    where.add_argument("--folder", default=DEFAULT_FOLDER,
                       help=f"sub-folder template (default: {DEFAULT_FOLDER})")

    run = p.add_argument_group("running")
    run.add_argument("--list-chats", action="store_true",
                     help="print your chats with their ids and exit")
    run.add_argument("--dry-run", action="store_true",
                     help="show what would be downloaded without downloading")
    run.add_argument("--parallel", type=int, default=3,
                     help="files downloaded at the same time (default: 3)")
    run.add_argument("--logout", action="store_true",
                     help="log out and delete the saved session")

    auth = p.add_argument_group("login (or set TG_API_ID / TG_API_HASH in .env)")
    auth.add_argument("--api-id", default=None)
    auth.add_argument("--api-hash", default=None)
    auth.add_argument("--session", default=None,
                      help="session file name (default: tgdl, env TG_SESSION)")
    return p


async def list_chats(client):
    print(f"{'ID':>16}  {'TYPE':<8} NAME")
    async for dialog in client.iter_dialogs():
        kind = ("user" if dialog.is_user else
                "channel" if dialog.is_channel and not dialog.is_group else
                "group")
        print(f"{dialog.id:>16}  {kind:<8} {dialog.name}")


async def resolve_chat(client, value):
    """Return ``(entity, name)`` for the ``--chat`` value.

    Plain words are matched against your own chat titles only, so that
    "Family" never resolves to some public @family channel by accident.
    """
    value = value.strip()
    if value.lower() in ("me", "saved", "self"):
        return await client.get_me(), "Saved Messages"
    if value.startswith("@") or "t.me/" in value or "telegram.me/" in value:
        entity = await client.get_entity(value)
        return entity, display_name(entity)

    want_id = int(value) if value.lstrip("-").isdigit() else None
    exact, partial = [], []
    async for dialog in client.iter_dialogs():
        if want_id is not None:
            if want_id in (dialog.id, dialog.entity.id):
                return dialog.entity, dialog.name
            continue
        name = (dialog.name or "").casefold()
        if name == value.casefold():
            exact.append(dialog)
        elif value.casefold() in name:
            partial.append(dialog)

    matches = exact or partial
    if len(matches) == 1:
        return matches[0].entity, matches[0].name
    if not matches:
        raise SystemExit(f"No chat matches {value!r}. Run with --list-chats "
                         "to see your chats and their ids.")
    lines = "\n".join(f"  {d.id:>16}  {d.name}" for d in matches[:20])
    raise SystemExit(f"Several chats match {value!r}; pass the id instead:\n{lines}")


def make_client(args):
    try:
        from telethon import TelegramClient
    except ImportError:
        raise SystemExit("Telethon is not installed: pip install -r requirements.txt")

    api_id = args.api_id or os.environ.get("TG_API_ID")
    api_hash = args.api_hash or os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit(
            "Missing API credentials. Create them at https://my.telegram.org "
            "(API development tools) and put TG_API_ID and TG_API_HASH in a "
            ".env file (see .env.example) or pass --api-id/--api-hash.")
    if not str(api_id).isdigit():
        raise SystemExit("TG_API_ID must be a number.")
    session = args.session or os.environ.get("TG_SESSION") or "tgdl"
    # Wait out Telegram's rate limits (up to 10 min) instead of failing.
    return TelegramClient(session, int(api_id), api_hash, flood_sleep_threshold=600)


async def amain(args):
    client = make_client(args)
    if args.logout:
        await client.connect()
        if await client.is_user_authorized():
            await client.log_out()
            print("Logged out; the session file was removed.")
        else:
            await client.disconnect()
            print("Not logged in.")
        return 0

    phone = os.environ.get("TG_PHONE")
    await client.start(**({"phone": phone} if phone else {}))
    try:
        if args.list_chats:
            await list_chats(client)
            return 0

        chat, chat_name = await resolve_chat(client, args.chat)
        options = Options(
            out_dir=args.out,
            folder=args.folder,
            filter=MessageFilter(
                kinds=args.types,
                exts=normalize_exts(args.ext),
                senders=args.senders,
                min_size=args.min_size,
                max_size=args.max_size,
            ),
            since=args.since,
            until=args.until + timedelta(days=1) if args.until else None,
            ids=args.ids,
            limit=args.limit,
            parallel=args.parallel,
            dry_run=args.dry_run,
        )
        print(f"Chat: {chat_name}  ->  {args.out.resolve()}")
        stats = await Downloader(client, chat, options, chat_name=chat_name).run()
    finally:
        await client.disconnect()

    verb = "would download" if args.dry_run else "downloaded"
    count = stats.queued if args.dry_run else stats.downloaded
    print(f"\nDone: {count} {verb} ({human_size(stats.bytes)}), "
          f"{stats.skipped} already present, {stats.failed} failed.")
    for msg_id, rel, error in stats.failures:
        print(f"  message {msg_id}: {rel}: {error}")
    return 1 if stats.failed else 0


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not (args.chat or args.list_chats or args.logout):
        parser.error("pass --chat (or --list-chats to see your chats)")
    if args.parallel < 1:
        parser.error("--parallel must be at least 1")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.since and args.until and args.until < args.since:
        parser.error("--until is before --since")
    try:
        render_folder(args.folder, {key: "x" for key in FOLDER_FIELDS})
    except ValueError as e:
        parser.error(f"--folder: {e}")
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\nStopped. Run the same command again to continue where it left off.")
        return 130
