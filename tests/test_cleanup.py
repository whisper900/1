import os

from tgdl.gui import cleanup


def test_schedule_and_delete_due_files(tmp_path):
    store = tmp_path / "cleanup.json"
    root = tmp_path / "out"
    old = root / "chat" / "Ahmed" / "1.pdf"
    new = root / "chat" / "Sara" / "2.pdf"
    for f in (old, new):
        f.parent.mkdir(parents=True)
        f.write_bytes(b"x")
    cleanup.schedule(store, [old], 60, root=root, now=1000)
    cleanup.schedule(store, [new], 600, root=root, now=1000)
    assert cleanup.next_due(store) == 1060

    assert cleanup.clean_due(store, now=1100) == 1
    assert not old.exists() and new.exists()
    assert not (root / "chat" / "Ahmed").exists()        # emptied folder removed
    assert (root / "chat").exists() and root.exists()    # never above the root
    assert [e["path"] for e in cleanup.pending(store)] == [str(new)]

    assert cleanup.clean_due(store, now=2000) == 1
    assert not store.exists() and root.exists()           # nothing left to track


def test_open_files_are_retried_and_missing_ones_forgotten(tmp_path, monkeypatch):
    store = tmp_path / "cleanup.json"
    busy, gone = tmp_path / "busy.docx", tmp_path / "gone.pdf"
    busy.write_bytes(b"x")
    cleanup.schedule(store, [busy, gone], 0, now=0)

    real_remove = os.remove

    def remove(path):
        if str(path) == str(busy):
            raise PermissionError("file is open in Word")
        real_remove(path)
    monkeypatch.setattr(cleanup.os, "remove", remove)
    assert cleanup.clean_due(store, now=10) == 0
    assert [e["path"] for e in cleanup.pending(store)] == [str(busy)]   # retried later

    monkeypatch.setattr(cleanup.os, "remove", real_remove)
    assert cleanup.clean_due(store, now=20) == 1 and not busy.exists()


def test_rescheduling_a_file_keeps_one_entry(tmp_path):
    store = tmp_path / "cleanup.json"
    cleanup.schedule(store, [tmp_path / "a"], 60, now=0)
    cleanup.schedule(store, [tmp_path / "a"], 60, now=100)
    assert [e["at"] for e in cleanup.pending(store)] == [160]


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def test_helper_waits_deletes_and_exits(tmp_path, monkeypatch):
    store = tmp_path / "cleanup.json"
    f1, f2 = tmp_path / "1.jpg", tmp_path / "2.jpg"
    for f in (f1, f2):
        f.write_bytes(b"x")
    clock = Clock()
    cleanup.schedule(store, [f1], 50, now=clock.t)
    cleanup.schedule(store, [f2], 100, now=clock.t)
    monkeypatch.setattr(cleanup, "_fresh", lambda flag, now: False)
    naps = []

    def sleep(seconds):
        naps.append(seconds)
        clock.sleep(seconds)
    assert cleanup.run_helper(store, sleep=sleep, clock=clock) == 0
    assert not f1.exists() and not f2.exists()
    assert max(naps) <= cleanup.MAX_NAP and clock.t >= 1100
    assert not cleanup._helper_flag(store).exists()


def test_helper_steps_aside_for_the_app_and_other_helpers(tmp_path):
    store = tmp_path / "cleanup.json"
    target = tmp_path / "1.jpg"
    target.write_bytes(b"x")
    cleanup.schedule(store, [target], 0)

    cleanup.app_heartbeat(store)                  # the app is open
    assert cleanup.run_helper(store, sleep=lambda s: None) == 0
    assert target.exists()                        # left for the app to handle
    cleanup.app_closed(store)

    cleanup._touch(cleanup._helper_flag(store), "123")   # another helper is running
    assert cleanup.run_helper(store, sleep=lambda s: None) == 0
    assert target.exists()
