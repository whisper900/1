import argparse
import asyncio
from types import SimpleNamespace

import pytest

from tgdl.cli import build_parser, parse_kinds, resolve_chat


def dialog(did, name):
    return SimpleNamespace(id=did, name=name,
                           entity=SimpleNamespace(id=abs(did) % 10**10, title=name))


class DialogClient:
    def __init__(self, dialogs):
        self.dialogs = dialogs
        self.get_entity_calls = []

    async def iter_dialogs(self):
        for d in self.dialogs:
            yield d

    async def get_entity(self, value):
        self.get_entity_calls.append(value)
        return SimpleNamespace(id=1, title="Public")


CLIENT = DialogClient([
    dialog(-1001111111111, "Family"),
    dialog(-1002222222222, "Family Photos"),
    dialog(333, "Work"),
])


def resolve(value, client=CLIENT):
    return asyncio.run(resolve_chat(client, value))


def test_resolve_by_title_prefers_exact_match():
    assert resolve("family")[1] == "Family"
    assert resolve("photos")[1] == "Family Photos"


def test_resolve_by_id():
    assert resolve("-1002222222222")[1] == "Family Photos"
    assert resolve("333")[1] == "Work"


def test_plain_words_never_hit_public_usernames():
    client = DialogClient(CLIENT.dialogs)
    with pytest.raises(SystemExit, match="No chat matches"):
        resolve("nothere", client)
    assert client.get_entity_calls == []


def test_ambiguous_title_lists_candidates():
    client = DialogClient(CLIENT.dialogs + [dialog(-1003333333333, "Family")])
    with pytest.raises(SystemExit, match="Several chats"):
        resolve("Family", client)


def test_usernames_and_links_use_get_entity():
    client = DialogClient([])
    resolve("@somechannel", client)
    resolve("https://t.me/somechannel", client)
    assert client.get_entity_calls == ["@somechannel", "https://t.me/somechannel"]


def test_parse_kinds():
    assert parse_kinds("photo, video") == {"photo", "video"}
    assert "sticker" in parse_kinds("all")
    with pytest.raises(argparse.ArgumentTypeError):
        parse_kinds("pictures")


def test_parser_defaults():
    args = build_parser().parse_args(["--chat", "x", "--from", "A, B", "--from", "C"])
    assert args.senders == ["A, B", "C"]
    assert args.parallel == 3 and "sticker" not in args.types


def test_bad_folder_template_is_rejected_before_login(capsys):
    from tgdl.cli import main
    with pytest.raises(SystemExit):
        main(["--chat", "x", "--folder", "{nope}"])
    assert "unknown placeholder" in capsys.readouterr().err
