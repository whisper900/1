"""Wiring of the desktop window and the Telegram worker, without a network."""

import asyncio
import time

import pytest

pytest.importorskip("PySide6")

import os  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402
from telethon import errors  # noqa: E402

from tgdl.downloader import Stats  # noqa: E402
from tgdl.gui import window as window_mod  # noqa: E402
from tgdl.gui.config import Config  # noqa: E402
from tgdl.gui.worker import LoginCancelled, TelegramWorker  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_dialogs(monkeypatch):
    """Message boxes would block the test; record them instead."""
    shown = []
    for name in ("information", "warning", "question"):
        monkeypatch.setattr(QMessageBox, name,
                            lambda *a, _n=name: shown.append((_n, a[2])) or QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: shown.append(("box", self.text())))
    return shown


class FakeWorker(QObject):
    ask = Signal(str, str)
    job_done = Signal(str, object)
    job_failed = Signal(str, object)
    event = Signal(str, dict)

    def __init__(self):
        super().__init__()
        self.submitted = []
        self.downloader = None
        self.calls = []

    def submit(self, name, coro):
        coro.close()
        self.submitted.append(name)

        class Future:
            cancelled = False

            def cancel(self):
                Future.cancelled = True
        self.future = Future()
        return self.future

    async def _noop(self):
        pass

    def login(self, *args):
        self.calls.append(("login", args))
        return self._noop()

    def load_chats(self):
        return self._noop()

    def download(self, chat_id, options):
        self.calls.append(("download", chat_id, options))
        return self._noop()

    def shutdown(self):
        pass


def make_window(tmp_path):
    config = Config(tmp_path / "settings.json")
    config.update(api_id=1, api_hash="0" * 32)
    worker = FakeWorker()
    win = window_mod.MainWindow(config=config, worker=worker, autostart=False)
    win.out_edit.setText(str(tmp_path / "out"))
    return win, worker


def test_login_then_choose_chat_then_download(app, tmp_path, no_dialogs):
    win, worker = make_window(tmp_path)
    assert not win.start_btn.isEnabled()

    win.login()
    assert worker.submitted == ["login"]
    worker.job_done.emit("login", "Ahmed Ali")
    assert "Ahmed Ali" in win.account_label.text()
    assert worker.submitted[-1] == "chats"

    worker.job_done.emit("chats", [(-100123, "العائلة", "group"), (42, "Sara", "user")])
    assert win.chat_list.count() == 2
    win.chat_list.setCurrentRow(0)
    assert win.start_btn.isEnabled()

    win.type_checks["video"].setChecked(False)
    win.type_folder_check.setChecked(True)
    win.ext_edit.setText("pdf، docx")
    win.senders_edit.setText("Ahmed Ali, @sara")
    win.max_check.setChecked(True)
    win.start(preview=False)

    _, chat_id, opts = worker.calls[-1]
    assert chat_id == -100123
    assert opts.folder == "{chat}/{sender}/{type}"
    assert "video" not in opts.filter.kinds and "photo" in opts.filter.kinds
    assert opts.filter.exts == {".pdf", ".docx"}
    assert opts.filter.senders == ["Ahmed Ali", "@sara"]
    assert opts.filter.max_size == 200 * 1024 * 1024
    assert opts.type_names["photo"] == "صور" and not opts.dry_run
    assert win.stop_btn.isEnabled() and not win.start_btn.isEnabled()

    worker.event.emit("progress", {"path": "a.pdf", "current": 50, "total": 100})
    worker.event.emit("saved", {"path": "العائلة/Ahmed/a.pdf", "size": 2048})
    assert win.progress.value() == 50
    assert "a.pdf" in win.log_view.toPlainText()

    win.stop()
    assert worker.future.cancelled
    worker.job_failed.emit("download", asyncio.CancelledError())
    assert "تم الإيقاف" in win.current_label.text()
    assert win.start_btn.isEnabled()

    win.start(preview=False)
    worker.job_done.emit("download", Stats(matched=3, downloaded=2, skipped=1, bytes=4096))
    assert any("تم تنزيل 2 ملف" in text for kind, text in no_dialogs if kind == "box")
    win.close()


def test_preview_and_validation(app, tmp_path, no_dialogs):
    win, worker = make_window(tmp_path)
    win.logged_in = True
    worker.job_done.emit("chats", [(1, "Work", "group")])
    win.chat_list.setCurrentRow(0)

    for box in win.type_checks.values():
        box.setChecked(False)
    win.start(preview=True)
    assert ("warning", "اختر نوعاً واحداً من الملفات على الأقل.") in no_dialogs
    assert not any(c[0] == "download" for c in worker.calls)

    win.type_checks["document"].setChecked(True)
    win.start(preview=True)
    assert worker.calls[-1][2].dry_run
    worker.job_done.emit("preview", Stats(matched=5, queued=5, bytes=5 * 1024 ** 2))
    assert any("سيتم تنزيل 5 ملف" in text for _, text in no_dialogs)
    win.close()


def test_preferences_are_remembered(app, tmp_path):
    win, _ = make_window(tmp_path)
    win.type_checks["sticker"].setChecked(True)
    win.chat_folder_check.setChecked(False)
    win.close()
    again = window_mod.MainWindow(config=Config(tmp_path / "settings.json"),
                                  worker=FakeWorker(), autostart=False)
    assert again.type_checks["sticker"].isChecked()
    assert not again.chat_folder_check.isChecked()
    assert again.out_edit.text() == str(tmp_path / "out")
    again.close()


# -- the real worker's thread bridge and login flow ---------------------------

class LoginClient:
    """Pretends to be Telethon: wrong code once, then asks for 2FA."""

    def __init__(self):
        self.signed_in_with = []

    async def send_code_request(self, phone):
        if phone == "+1":
            raise errors.PhoneNumberInvalidError(request=None)
        return type("Sent", (), {"phone_code_hash": "h"})()

    async def sign_in(self, phone=None, code=None, password=None, phone_code_hash=None):
        if code == "00000":
            raise errors.PhoneCodeInvalidError(request=None)
        if code is not None:
            raise errors.SessionPasswordNeededError(request=None)
        self.signed_in_with.append(password)


def wait_for(app, future, timeout=5):
    deadline = time.monotonic() + timeout
    while not future.done() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    return future.result(0)


def test_worker_login_asks_ui_and_handles_errors(app):
    worker = TelegramWorker()
    worker.client = LoginClient()
    replies = iter(["+1", "+15550001", "00000", "12345", "secret"])
    asked = []

    def on_ask(what, error):
        asked.append((what, error))
        worker.answer(next(replies))
    worker.ask.connect(on_ask)
    try:
        fut = worker.submit("login", worker._interactive_login())
        wait_for(app, fut)
        assert [w for w, _ in asked] == ["phone", "phone", "code", "code", "password"]
        assert "رقم الهاتف غير صحيح" in asked[1][1]
        assert "الكود غير صحيح" in asked[3][1]
        assert worker.client.signed_in_with == ["secret"]
    finally:
        worker.shutdown()


def test_worker_login_cancel(app):
    worker = TelegramWorker()
    worker.client = LoginClient()
    worker.ask.connect(lambda what, error: worker.answer(None))
    try:
        fut = worker.submit("login", worker._interactive_login())
        with pytest.raises(LoginCancelled):
            wait_for(app, fut)
    finally:
        worker.shutdown()
