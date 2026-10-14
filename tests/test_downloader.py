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
