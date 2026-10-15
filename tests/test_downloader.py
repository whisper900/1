import asyncio
from types import SimpleNamespace

from telethon import errors

from tgdl.downloader import Downloader, Options
from tgdl.filters import MessageFilter
from tests.fakes import FakeClient, FakeMsg, day, user

CHAT = SimpleNamespace(id=555, title="Family")
AHMED = user(1, "Ahmed", "Ali")
SARA = user(2, "Sara")


def run(client, tmp_path, **opts):
    logs = []
    dl = Downloader(client, CHAT, Options(out_dir=tmp_path, parallel=2, **opts),
                    log=logs.append)

    async def no_sleep(_):
        pass
    dl._sleep = no_sleep
    stats = asyncio.run(dl.run())
    return stats, logs


def files(tmp_path):
    return sorted(p.relative_to(tmp_path).as_posix()
                  for p in tmp_path.rglob("*")
                  if p.is_file() and ".tgdl" not in p.parts)


def test_files_go_to_one_folder_per_sender(tmp_path):
    client = FakeClient([
        FakeMsg(1, AHMED, name="cv.pdf"),
        FakeMsg(2, SARA, kind="photo", ext=".jpg", date=day(2)),
        FakeMsg(3, AHMED, has_media=False),                    # plain text
        FakeMsg(4, AHMED, kind="photo", web_preview=object()),  # link preview
        FakeMsg(5, SARA, kind="sticker", ext=".webp"),          # off by default
    ])
    stats, _ = run(client, tmp_path)
    assert files(tmp_path) == [
        "Family/Ahmed Ali/cv.pdf",
        "Family/Sara/photo_2025-01-02_12-00-00_2.jpg",
    ]
    assert (stats.downloaded, stats.failed, stats.matched) == (2, 0, 2)


def test_second_run_resumes_without_redownloading(tmp_path):
    msgs = [FakeMsg(1, AHMED, name="a.pdf"), FakeMsg(2, SARA, name="b.pdf")]
    run(FakeClient(msgs), tmp_path)
    client = FakeClient(msgs + [FakeMsg(3, SARA, name="c.pdf")])
    stats, _ = run(client, tmp_path)
    assert client.download_calls == [3]
    assert (stats.downloaded, stats.skipped) == (1, 2)


def test_same_name_different_files_are_both_kept(tmp_path):
    client = FakeClient([
        FakeMsg(1, AHMED, name="report.pdf", content=b"v1"),
        FakeMsg(2, AHMED, name="report.pdf", content=b"version 2"),
    ])
    run(client, tmp_path)
    assert files(tmp_path) == [
        "Family/Ahmed Ali/report (msg 2).pdf",
        "Family/Ahmed Ali/report.pdf",
    ]
    assert (tmp_path / "Family/Ahmed Ali/report (msg 2).pdf").read_bytes() == b"version 2"


def test_existing_identical_file_is_not_downloaded_again(tmp_path):
    folder = tmp_path / "Family" / "Ahmed Ali"
    folder.mkdir(parents=True)
    (folder / "a.pdf").write_bytes(b"data")
    client = FakeClient([FakeMsg(1, AHMED, name="a.pdf", content=b"data")])
    stats, _ = run(client, tmp_path)
    assert client.download_calls == []
    assert stats.skipped == 1


def test_filters_dates_and_senders(tmp_path):
    client = FakeClient([
        FakeMsg(1, AHMED, name="old.pdf", date=day(1)),
        FakeMsg(2, AHMED, name="in.pdf", date=day(5)),
        FakeMsg(3, SARA, name="sara.pdf", date=day(5)),
        FakeMsg(4, AHMED, name="in.zip", date=day(6)),
        FakeMsg(5, AHMED, name="late.pdf", date=day(20)),
    ])
    run(client, tmp_path, since=day(3), until=day(10),
        filter=MessageFilter(exts={".pdf"}, senders=["Ahmed Ali"]))
    assert files(tmp_path) == ["Family/Ahmed Ali/in.pdf"]


def test_ids_limit_and_custom_folder(tmp_path):
    client = FakeClient([FakeMsg(i, SARA, name=f"{i}.pdf") for i in range(1, 6)])
    run(client, tmp_path, ids=[4, 2, 99, 5], limit=2, folder="{sender}/{type}")
    assert files(tmp_path) == ["Sara/document/2.pdf", "Sara/document/4.pdf"]


def test_own_messages_in_private_chat_go_to_my_folder(tmp_path):
    client = FakeClient([FakeMsg(1, None, name="mine.pdf", out=True)],
                        me=user(9, "Me", "Myself"))
    run(client, tmp_path)
    assert files(tmp_path) == ["Family/Me Myself/mine.pdf"]


def test_dry_run_writes_nothing(tmp_path):
    client = FakeClient([FakeMsg(1, AHMED, name="a.pdf")])
    stats, logs = run(client, tmp_path, dry_run=True)
    assert client.download_calls == []
    assert not any(tmp_path.iterdir())
    assert stats.matched == 1 and "would download" in logs[0]


def test_failures_are_reported_and_do_not_stop_the_run(tmp_path):
    client = FakeClient(
        [FakeMsg(1, AHMED, name="bad.pdf"), FakeMsg(2, AHMED, name="good.pdf")],
        fail={1: [ConnectionError("x"), ConnectionError("y"), ConnectionError("z")]})
    stats, _ = run(client, tmp_path)
    assert files(tmp_path) == ["Family/Ahmed Ali/good.pdf"]
    assert stats.failed == 1 and stats.failures[0][0] == 1
    assert not list(tmp_path.rglob("*.part"))
    # The failed file is retried on the next run.
    client.fail.clear()
    stats, _ = run(client, tmp_path)
    assert stats.downloaded == 1 and client.download_calls[-1] == 1


def test_flood_wait_and_transient_errors_are_retried(tmp_path):
    client = FakeClient([FakeMsg(1, AHMED, name="a.pdf")], fail={
        1: [errors.FloodWaitError(request=None, capture=5), ConnectionError("blip")],
    })
    stats, logs = run(client, tmp_path)
    assert stats.downloaded == 1
    assert any("wait 5s" in line for line in logs)


def test_limit_counts_new_files_so_runs_can_continue(tmp_path):
    client = FakeClient([FakeMsg(i, SARA, name=f"{i}.pdf") for i in range(1, 6)])
    run(client, tmp_path, limit=2)
    stats, _ = run(client, tmp_path, limit=2)
    assert client.download_calls == [1, 2, 3, 4]
    assert (stats.skipped, stats.downloaded) == (2, 2)


def test_cancel_stops_running_downloads_and_cleans_up(tmp_path):
    started = asyncio.Event()

    class SlowClient(FakeClient):
        async def download_media(self, msg, file=None, progress_callback=None):
            open(file, "wb").close()
            started.set()
            await asyncio.sleep(3600)

    async def main():
        dl = Downloader(SlowClient([FakeMsg(1, AHMED, name="big.mp4")]), CHAT,
                        Options(out_dir=tmp_path), log=lambda _: None)
        task = asyncio.create_task(dl.run())
        await started.wait()
        task.cancel()
        try:
            await asyncio.wait_for(task, 2)
        except asyncio.CancelledError:
            pass
    asyncio.run(main())
    assert files(tmp_path) == []


def test_events_for_graphical_front_ends(tmp_path):
    events = []

    class ProgressClient(FakeClient):
        async def download_media(self, msg, file=None, progress_callback=None):
            progress_callback(2, 4)
            return await super().download_media(msg, file)

    client = ProgressClient(
        [FakeMsg(1, AHMED, name="a.pdf"), FakeMsg(2, AHMED, kind="photo", ext=".jpg")],
        fail={1: [ConnectionError("blip")]})
    dl = Downloader(client, CHAT, Options(out_dir=tmp_path, parallel=1,
                                          type_names={"photo": "صور"},
                                          folder="{sender}/{type}"),
                    log=lambda _: None, on_event=lambda name, **d: events.append((name, d)))

    async def no_sleep(_):
        pass
    dl._sleep = no_sleep
    stats = asyncio.run(dl.run())
    names = [name for name, _ in events]
    assert names.count("saved") == 2 and "retry" in names and "progress" in names
    assert stats.scanned == 2
    assert files(tmp_path) == ["Ahmed Ali/document/a.pdf",
                               "Ahmed Ali/صور/photo_2025-01-01_12-00-00_2.jpg"]


def test_numbered_names_have_one_sequence_per_group_and_sender(tmp_path):
    client = FakeClient([
        FakeMsg(1, AHMED, kind="photo", ext=".jpg", content=b"p1"),
        FakeMsg(2, AHMED, name="تقرير.docx", content=b"d1"),
        FakeMsg(3, AHMED, name="scan.PNG", content=b"p2"),       # still an image
        FakeMsg(4, AHMED, name="old.doc", content=b"d2"),        # still Word
        FakeMsg(5, AHMED, name="a.pdf", content=b"f1"),
        FakeMsg(6, SARA, kind="photo", ext=".jpg", content=b"s1"),  # her own folder
        FakeMsg(7, AHMED, name="b.pdf", content=b"f2"),
        FakeMsg(8, AHMED, name="x.zip", content=b"z1"),
        FakeMsg(9, AHMED, kind="photo", ext=".jpg", content=b"p3"),
    ])
    run(client, tmp_path, naming="numbers")
    assert files(tmp_path) == [
        "Family/Ahmed Ali/1.docx", "Family/Ahmed Ali/1.jpg", "Family/Ahmed Ali/1.pdf",
        "Family/Ahmed Ali/1.zip", "Family/Ahmed Ali/2.doc", "Family/Ahmed Ali/2.pdf",
        "Family/Ahmed Ali/2.png", "Family/Ahmed Ali/3.jpg", "Family/Sara/1.jpg",
    ]
    # Renaming only: the bytes are exactly what Telegram sent.
    assert (tmp_path / "Family/Ahmed Ali/1.docx").read_bytes() == b"d1"
    assert (tmp_path / "Family/Ahmed Ali/3.jpg").read_bytes() == b"p3"


def test_numbering_continues_on_later_runs(tmp_path):
    folder = tmp_path / "Family" / "Ahmed Ali"
    folder.mkdir(parents=True)
    (folder / "4.jpg").write_bytes(b"mine")      # user's own numbered file
    (folder / "notes.txt").write_bytes(b"x")
    msgs = [FakeMsg(1, AHMED, kind="photo", ext=".jpg", content=b"a")]
    run(FakeClient(msgs), tmp_path, naming="numbers")
    msgs.append(FakeMsg(2, AHMED, kind="photo", ext=".jpg", content=b"bb"))
    client = FakeClient(msgs)
    stats, _ = run(client, tmp_path, naming="numbers")
    assert client.download_calls == [2]          # message 1 is not downloaded again
    assert sorted(p.name for p in folder.iterdir()) == ["4.jpg", "5.jpg", "6.jpg", "notes.txt"]
    assert (folder / "6.jpg").read_bytes() == b"bb"


def test_numbered_dry_run_shows_names_without_writing(tmp_path):
    client = FakeClient([FakeMsg(1, AHMED, name="a.pdf"), FakeMsg(2, AHMED, name="b.pdf")])
    _, logs = run(client, tmp_path, naming="numbers", dry_run=True)
    assert "1.pdf" in logs[0] and "2.pdf" in logs[1]
    assert not any(tmp_path.iterdir())


def test_planned_event_describes_the_file(tmp_path):
    events = []
    client = FakeClient([FakeMsg(1, AHMED, name="cv.pdf", date=day(3))])
    dl = Downloader(client, CHAT, Options(out_dir=tmp_path, dry_run=True),
                    log=lambda _: None, on_event=lambda name, **d: events.append((name, d)))
    asyncio.run(dl.run())
    (name, data), = events
    assert name == "planned"
    assert data["file_name"] == "cv.pdf" and data["sender"] == "Ahmed Ali"
    assert data["kind"] == "document" and data["date"] == day(3) and data["msg_id"] == 1


def test_day_folder_and_thumbnail(tmp_path):
    from telethon.tl import types
    events = []
    msg = FakeMsg(1, AHMED, kind="photo", ext=".jpg", date=day(5))
    msg.photo = SimpleNamespace(sizes=[types.PhotoStrippedSize(type="i", bytes=b"\x01\x08\x08" + b"\0" * 10)])
    client = FakeClient([msg])
    dl = Downloader(client, CHAT, Options(out_dir=tmp_path, folder="{day}/{sender}", dry_run=True),
                    log=lambda _: None, on_event=lambda name, **d: events.append(d))
    asyncio.run(dl.run())
    (data,) = events
    local_day = day(5).astimezone().strftime("%Y-%m-%d")
    assert str(data["path"]).startswith(local_day)
    assert data["thumb"][:2] == b"\xff\xd8"               # a JPEG preview
