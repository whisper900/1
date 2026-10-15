"""Wiring of the desktop window and the Telegram worker, without a network."""

import asyncio
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

import os  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402
from telethon import errors  # noqa: E402

from tgdl.downloader import Stats  # noqa: E402
from tgdl.filters import KINDS  # noqa: E402
from tgdl.gui import window as window_mod  # noqa: E402
from tgdl.gui.config import Config  # noqa: E402
from tgdl.gui.worker import LoginCancelled, TelegramWorker  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def delete_windows(app, no_dialogs):
    """Destroy every window a test made while QApplication still exists.

    Depends on no_dialogs so closing a busy window cannot open a real
    (blocking) question box.
    """
    yield
    from PySide6.QtCore import QCoreApplication, QEvent
    for widget in QApplication.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


@pytest.fixture(autouse=True)
def first_download(monkeypatch):
    """Answer "which period?" before a first download (default: everything)."""
    answer = {"choice": "all", "asked": 0}

    class Dialog:
        def __init__(self, name, parent=None):
            pass

        def exec(self):
            answer["asked"] += 1
            self.choice = answer["choice"]
            return 1

        def setParent(self, parent):
            pass
    monkeypatch.setattr(window_mod, "FirstDownloadDialog", Dialog)
    return answer


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
    ask = Signal(str, dict)
    qr = Signal(str)
    notice = Signal(str)
    chats_partial = Signal(list)
    flood_wait = Signal(int, str)
    auto_event = Signal(str, dict)
    auto_done = Signal(int, str, int)
    job_done = Signal(str, object)
    job_failed = Signal(str, object)
    event = Signal(str, dict)

    def __init__(self):
        super().__init__()
        self.submitted = []
        self.downloader = None
        self.calls = []
        self.me_id = None

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

    def download(self, chat_id, options, name=None):
        self.calls.append(("download", chat_id, options))
        return self._noop()

    def shutdown(self):
        pass

    def configure_auto(self, enabled, options=None, scope="private", chat_ids=()):
        self.auto = (enabled, options, scope, set(chat_ids)) if enabled else None


def make_window(tmp_path):
    config = Config(tmp_path / "settings.json")
    config.update(api_id=1, api_hash="0" * 32, out_dir=str(tmp_path / "out"))
    worker = FakeWorker()
    win = window_mod.MainWindow(config=config, worker=worker, autostart=False)
    win.chat_cache_path = tmp_path / "chats.json"
    win.cleanup_store = tmp_path / "cleanup.json"
    win.orders_path = tmp_path / "orders.json"
    win.marks_path = tmp_path / "marks.json"
    return win, worker


def test_login_then_choose_chat_then_download(app, tmp_path, no_dialogs):
    win, worker = make_window(tmp_path)
    assert not win.start_btn.isEnabled()

    win.login()
    assert worker.submitted == ["login"]
    worker.job_done.emit("login", "Ahmed Ali")
    assert "Ahmed Ali" in win.account_label.text()
    assert worker.submitted[-1] == "chats"

    worker.job_done.emit("chats", [(-100123, "العائلة", "group", ""), (42, "Sara", "user", "")])
    assert win.chat_list.count() == 2
    win.chat_list.setCurrentRow(0)
    assert win.start_btn.isEnabled()

    win.config.update(kinds=["photo", "document", "audio", "voice", "video_note", "gif"],
                      type_folder=True, exts="pdf، docx", senders="Ahmed Ali, @sara",
                      max_on=True, max_mb=200, naming="numbers")
    win._refresh_summary()
    assert win.pages.currentIndex() == 1                    # the chat page is shown
    win.start(preview=False)

    _, chat_id, opts = worker.calls[-1]
    assert chat_id == -100123
    assert opts.folder == "{chat}/{sender}/{type}"
    assert "video" not in opts.filter.kinds and "photo" in opts.filter.kinds
    assert opts.filter.exts == {".pdf", ".docx"}
    assert opts.filter.senders == ["Ahmed Ali", "@sara"]
    assert opts.filter.max_size == 200 * 1024 * 1024
    assert opts.type_names["photo"] == "صور" and not opts.dry_run
    assert opts.naming == "numbers"
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
    worker.job_done.emit("chats", [(1, "Work", "group", "")])
    win.chat_list.setCurrentRow(0)

    win.config["kinds"] = []
    win.start(preview=True)
    assert any("اختر نوعاً واحداً" in text for kind, text in no_dialogs if kind == "warning")
    assert not any(c[0] == "download" for c in worker.calls)

    win.config["kinds"] = ["document"]
    win.start(preview=True)
    assert worker.calls[-1][2].dry_run
    worker.job_done.emit("preview", Stats(matched=5, queued=5, bytes=5 * 1024 ** 2))
    assert any("سيتم تنزيل 5 ملف" in text for _, text in no_dialogs)
    win.close()


def test_empty_page_until_a_chat_is_chosen(app, tmp_path):
    win, worker = make_window(tmp_path)
    assert win.pages.currentIndex() == 0 and not win.empty_login_btn.isHidden()
    assert "سجّل الدخول" in win.empty_text.text()
    win.login()
    worker.job_done.emit("login", "Ahmed")
    worker.job_done.emit("chats", [(1, "Work", "group", "")])
    assert win.pages.currentIndex() == 0 and "اختر محادثة" in win.empty_title.text()
    win.chat_list.setCurrentRow(0)
    assert win.pages.currentIndex() == 1 and win.chat_name.text() == "Work"
    win.close()


def test_settings_dialog_saves_everything(app, tmp_path, no_dialogs):
    from tgdl.gui.settings import SettingsDialog, build_options, summary
    config = Config(tmp_path / "settings.json")
    dialog = SettingsDialog(config, require_api=True)
    dialog.type_buttons["sticker"].setChecked(True)
    dialog.type_buttons["video"].setChecked(False)
    dialog.ext_edit.setText("pdf")
    dialog.chat_folder_check.setChecked(False)
    dialog.naming_combo.setCurrentIndex(dialog.naming_combo.findData("numbers"))
    dialog.out_edit.setText(str(tmp_path / "out"))
    assert "1.jpg" in dialog.example_label.text()

    dialog._save()                                   # API is required here
    assert any("API ID" in text for _, text in no_dialogs)
    assert dialog.tabs.currentIndex() == 4                 # the account tab
    dialog.api_id.setText("12345")
    dialog.api_hash.setText("ab" * 16)
    dialog._save()

    saved = Config(tmp_path / "settings.json")       # read back from disk
    assert saved["api_id"] == 12345 and saved.has_api
    opts = build_options(saved)
    assert "sticker" in opts.filter.kinds and "video" not in opts.filter.kinds
    assert opts.filter.exts == {".pdf"} and opts.folder == "{sender}"
    assert opts.naming == "numbers" and opts.out_dir == tmp_path / "out"
    lines = dict(summary(saved))
    assert "pdf" in lines["التصفية"] and lines["التسمية"].startswith("أرقام")


def test_settings_dialog_rejects_no_types_and_bad_dates(app, tmp_path, no_dialogs):
    from PySide6.QtCore import QDate
    from tgdl.gui.settings import SettingsDialog
    config = Config(tmp_path / "settings.json")
    dialog = SettingsDialog(config)
    for button in dialog.type_buttons.values():
        button.setChecked(False)
    dialog._save()
    assert any("نوعاً واحداً" in text for _, text in no_dialogs)
    dialog.type_buttons["photo"].setChecked(True)
    dialog.since_check.setChecked(True)
    dialog.until_check.setChecked(True)
    dialog.since_date.setDate(QDate(2025, 5, 1))
    dialog.until_date.setDate(QDate(2025, 4, 1))
    dialog._save()
    assert any("تاريخ البداية" in text for _, text in no_dialogs)
    assert not (tmp_path / "settings.json").exists()


def test_build_options_dates():
    from datetime import timedelta
    from tgdl.gui.settings import build_options
    config = {"since_on": True, "since": "2025-03-01", "until_on": True, "until": "2025-03-31"}
    opts = build_options(config)
    assert opts.since.date().isoformat() == "2025-03-01"
    assert opts.until - opts.since == timedelta(days=31)       # until is inclusive
    with pytest.raises(ValueError):
        build_options({"since_on": True, "since": "2025-04-01",
                       "until_on": True, "until": "2025-03-01"})


# -- the real worker's thread bridge and login flow ---------------------------

from telethon.tl import functions, types  # noqa: E402

from tgdl.gui.worker import (RESEND, LoginBlocked, normalize_phone,  # noqa: E402
                             describe_sent_code)


def sent(kind, next_type=None, hash_="h"):
    return types.auth.SentCode(type=kind, phone_code_hash=hash_, next_type=next_type)


class LoginClient:
    """Pretends to be Telethon. ``codes`` is what successive SendCode returns."""

    api_id, api_hash = 1, "x"

    def __init__(self, first, resend=None, verified=None):
        self.first, self.resend, self.verified = first, resend, verified
        self.requests = []
        self.signed_in_with = []

    async def __call__(self, request):
        self.requests.append(type(request).__name__)
        if isinstance(request, functions.auth.SendCodeRequest):
            self.phone = request.phone_number
            return self.first
        if isinstance(request, functions.auth.ResendCodeRequest):
            return self.resend
        if isinstance(request, functions.account.SendVerifyEmailCodeRequest):
            if "@" not in request.email:
                raise errors.EmailInvalidError(request=None)
            return None
        if isinstance(request, functions.account.VerifyEmailRequest):
            return types.account.EmailVerifiedLogin(email="a@b.c", sent_code=self.verified)
        raise AssertionError(request)

    async def sign_in(self, phone=None, code=None, password=None, phone_code_hash=None):
        if code == "00000":
            raise errors.PhoneCodeInvalidError(request=None)
        if code == "2fa":
            raise errors.SessionPasswordNeededError(request=None)
        self.signed_in_with.append(code or password)


def wait_for(app, future, timeout=5):
    deadline = time.monotonic() + timeout
    while not future.done() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    return future.result(0)


def login(app, client, replies):
    """Run the interactive login, answering each question from ``replies``."""
    worker = TelegramWorker()
    worker.client = client
    replies = iter(replies)
    asked = []

    def on_ask(what, details):
        asked.append((what, details))
        worker.answer(next(replies))
    worker.ask.connect(on_ask)
    try:
        wait_for(app, worker.submit("login", worker._interactive_login()))
    finally:
        worker.shutdown()
    return asked


def test_code_in_telegram_app_resend_by_sms_and_2fa(app):
    client = LoginClient(
        first=sent(types.auth.SentCodeTypeApp(5), next_type=types.auth.CodeTypeSms()),
        resend=sent(types.auth.SentCodeTypeSms(5)))
    asked = login(app, client, ["0770", "٠٠١ ٥٥٥ ٠١٢٣٤٥٦", RESEND, "٠٠٠٠٠", "2fa", "secret"])

    assert [w for w, _ in asked] == ["phone", "phone", "code", "code", "code", "password"]
    assert "رمز الدولة" in asked[1][1]["error"]
    assert client.phone == "15550123456"              # Arabic digits and 00 handled
    assert "Telegram" in asked[2][1]["info"] and asked[2][1]["resend"] == "برسالة SMS"
    assert "SMS" in asked[3][1]["info"]                # after the resend
    assert "غير صحيح" in asked[4][1]["error"]
    assert client.signed_in_with == ["secret"]
    assert client.requests == ["SendCodeRequest", "ResendCodeRequest"]


def test_code_sent_by_email_says_so(app):
    client = LoginClient(first=sent(types.auth.SentCodeTypeEmailCode(
        email_pattern="a***@gmail.com", length=6)))
    asked = login(app, client, ["+15550123456", "123456"])
    info = asked[1][1]["info"]
    assert "بريدك الإلكتروني" in info and "a***@gmail.com" in info
    assert asked[1][1]["resend"] is None
    assert client.signed_in_with == ["123456"]


def test_login_email_setup_required(app):
    client = LoginClient(
        first=sent(types.auth.SentCodeTypeSetUpEmailRequired()),
        verified=sent(types.auth.SentCodeTypeEmailCode(email_pattern="m***@x.com", length=6)))
    asked = login(app, client, ["+15550123456", "not-an-email", "me@x.com", "4321", "999999"])
    assert [w for w, _ in asked] == ["phone", "email", "email", "email_code", "code"]
    assert "غير صحيح" in asked[2][1]["error"]
    assert client.signed_in_with == ["999999"]


def test_payment_required_is_explained(app):
    client = LoginClient(first=types.auth.SentCodePaymentRequired(
        store_product="p", phone_code_hash="h", support_email_address="",
        support_email_subject="", premium_days=0, currency="USD", amount=1))
    with pytest.raises(LoginBlocked, match="تطبيق تليكرام الرسمي"):
        login(app, client, ["+15550123456"])


def test_worker_login_cancel(app):
    client = LoginClient(first=sent(types.auth.SentCodeTypeApp(5)))
    with pytest.raises(LoginCancelled):
        login(app, client, [None])


@pytest.mark.parametrize("raw, expected", [
    ("+1 (555) 012-3456", "15550123456"),
    ("00964 770 123 4567", "9647701234567"),
    ("+٩٦٤٧٧٠١٢٣٤٥٦٧", "9647701234567"),
    ("07701234567", None),      # no country code: could reach a stranger
    ("7701234567", None),
    ("+12", None),
])
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


def test_code_dialog_resend_and_submit(app):
    dialog = window_mod.CodeDialog("t", describe_sent_code(sent(types.auth.SentCodeTypeApp(5))),
                                   error="x", resend="برسالة SMS")
    dialog._resend()
    assert dialog.result_value == RESEND
    dialog = window_mod.CodeDialog("t", "info")
    dialog.code_edit.setText(" 12345 ")
    dialog._accept_code()
    assert dialog.result_value == "12345"


# -- QR login -------------------------------------------------------------------

import datetime  # noqa: E402


class FakeQR:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)   # exceptions to raise from wait(), in order
        self.n = 0
        self.expires = datetime.datetime.now(datetime.timezone.utc)

    @property
    def url(self):
        return f"tg://login?token=t{self.n}"

    async def wait(self, timeout=None):
        if self.outcomes:
            raise self.outcomes.pop(0)

    async def recreate(self):
        self.n += 1


class QrClient(LoginClient):
    def __init__(self, qr, authorized=False):
        super().__init__(first=None)
        self._qr = qr
        self.authorized = authorized

    async def qr_login(self):
        return self._qr

    def is_connected(self):
        return True

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        return type("Me", (), {"first_name": "Sara", "last_name": None, "id": 7,
                               "phone": None, "username": None, "title": None})()


def run_login(app, client, replies, method=None):
    worker = TelegramWorker()
    worker.client = client
    worker._client_key = (1, "x", "s")
    replies = iter(replies)
    asked, urls = [], []
    worker.ask.connect(lambda what, d: (asked.append(what), worker.answer(next(replies))))
    worker.qr.connect(urls.append)
    try:
        name = wait_for(app, worker.submit("login", worker.login(1, "x", "s", method)))
    finally:
        worker.shutdown()
    return name, asked, urls


def test_qr_login_refreshes_expired_codes(app):
    client = QrClient(FakeQR([asyncio.TimeoutError(), asyncio.TimeoutError()]))
    name, asked, urls = run_login(app, client, ["qr"])
    assert asked == ["method"]
    assert urls == ["tg://login?token=t0", "tg://login?token=t1", "tg://login?token=t2", ""]
    assert name == "Sara"


def test_qr_login_with_two_step_password(app):
    client = QrClient(FakeQR([errors.SessionPasswordNeededError(request=None)]))
    name, asked, urls = run_login(app, client, ["qr", "secret"])
    assert asked == ["method", "password"]
    assert urls[-1] == ""                       # QR window closed before the password
    assert client.signed_in_with == ["secret"]


def test_existing_session_skips_login_questions(app):
    name, asked, _ = run_login(app, QrClient(FakeQR([]), authorized=True), [])
    assert asked == [] and name == "Sara"


def test_qr_dialog_in_window(app, tmp_path):
    win, worker = make_window(tmp_path)
    win.login()
    worker.qr.emit("tg://login?token=abc")
    assert win.qr_dialog is not None
    assert not win.qr_dialog.image.pixmap().isNull()
    win.qr_dialog.reject()                      # user presses cancel
    assert worker.future.cancelled
    worker.job_failed.emit("login", asyncio.CancelledError())
    assert "غير مسجّل" in win.account_label.text()
    win.login()
    worker.qr.emit("tg://login?token=new")
    worker.qr.emit("")                          # scanned: window closes by itself
    assert win.qr_dialog is None
    win.close()


# -- chat list: saved copy, background refresh, rate-limit notices -------------

from tgdl.gui.config import load_chat_cache, save_chat_cache  # noqa: E402

def age_cache(path, seconds=3600):
    import json
    import time as clock
    data = json.loads(path.read_text(encoding="utf-8"))
    data["saved_at"] = clock.time() - seconds
    path.write_text(json.dumps(data), encoding="utf-8")


CHATS = [(-1001, "العائلة", "group", "pinned"), (42, "Sara", "user", ""),
         (7, "Work", "group", "archived")]


def test_saved_chats_show_at_once_and_refresh_in_background(app, tmp_path):
    win, worker = make_window(tmp_path)
    worker.me_id = 99
    save_chat_cache(99, CHATS[:2], win.chat_cache_path)
    age_cache(win.chat_cache_path)

    win.login()
    worker.job_done.emit("login", "Ahmed")
    assert win.chat_list.count() == 2                       # instantly, from disk
    assert worker.submitted[-1] == "chats"
    assert "جارٍ تحديث" in win.chats_status.text()

    win.chat_list.setCurrentRow(1)                          # pick Sara
    assert win.start_btn.isEnabled()                        # usable during refresh
    worker.chats_partial.emit(CHATS[:1])                    # first page: ignored
    assert win.chat_list.count() == 2

    worker.job_done.emit("chats", CHATS)
    assert win.chat_list.count() == 3
    assert win.chat_list.currentItem().data(Qt.UserRole) == 42   # still selected
    assert win.chats_count.text() == "3"
    assert load_chat_cache(99, win.chat_cache_path) == CHATS
    assert load_chat_cache(100, win.chat_cache_path) == []  # other account
    win.close()


def test_first_load_fills_progressively_and_shows_waits(app, tmp_path):
    win, worker = make_window(tmp_path)
    win.login()
    worker.job_done.emit("login", "Ahmed")
    worker.chats_partial.emit(CHATS[:2])
    assert win.chat_list.count() == 2
    assert "2" in win.chats_status.text()
    worker.flood_wait.emit(35, "GetDialogsRequest")
    assert "35" in win.chats_status.text() and "35" in win.log_view.toPlainText()
    assert "قائمة المحادثات" in win.chats_status.text()
    worker.job_done.emit("chats", CHATS)
    assert win.chat_list.count() == 3 and win.chats_status.text() == ""
    win.close()


def test_flood_wait_log_line_becomes_signal():
    import logging
    from tgdl.gui.worker import _FloodWaitLog
    seen = []
    handler = _FloodWaitLog(lambda seconds, request: seen.append((seconds, request)))
    record = logging.LogRecord("telethon.client.users", logging.INFO, "", 0,
                               "Sleeping%s for %ds (%s) on %s flood wait",
                               ("", 35, "0:00:35", "GetDialogsRequest"), None)
    handler.emit(record)
    other = logging.LogRecord("x", logging.INFO, "", 0, "connected", (), None)
    handler.emit(other)
    assert seen == [(35, "GetDialogsRequest")]


def test_download_from_saved_list_resolves_chat(app, tmp_path):
    from types import SimpleNamespace
    from tgdl.downloader import Options

    class EntityClient:
        async def get_entity(self, chat_id):
            return SimpleNamespace(id=5, title="القناة")

        async def iter_messages(self, *a, **kw):
            return
            yield

    worker = TelegramWorker()
    worker.client = EntityClient()
    try:
        stats = wait_for(app, worker.submit("download", worker.download(
            -1005, Options(out_dir=tmp_path), "القناة")))
        assert stats.scanned == 0 and worker.chats[-1005][1] == "القناة"
    finally:
        worker.shutdown()


# -- Telegram's chat order and picking individual files -------------------------

def test_chats_come_in_telegram_order(app):
    from types import SimpleNamespace as NS

    def dialog(did, name, pinned=False):
        return NS(id=did, name=name, pinned=pinned, entity=NS(id=did), is_user=True,
                  is_channel=False, is_group=False)

    class DialogClient:
        async def iter_dialogs(self, folder=None):
            folders = {0: [dialog(1, "Pinned", True), dialog(2, "Recent"), dialog(3, "Older")],
                       1: [dialog(4, "Archived")]}
            for d in folders[folder]:
                yield d

    worker = TelegramWorker()
    worker.client = DialogClient()
    try:
        chats = wait_for(app, worker.submit("chats", worker.load_chats()))
    finally:
        worker.shutdown()
    assert chats == [(1, "Pinned", "user", "pinned"), (2, "Recent", "user", ""),
                     (3, "Older", "user", ""), (4, "Archived", "user", "archived")]


def test_chat_marks_shown_and_old_cache_format_read(app, tmp_path):
    import json
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"account": 1, "chats": [[5, "Old", "user"]]}))
    assert load_chat_cache(1, path) == [(5, "Old", "user", "")]

    win, worker = make_window(tmp_path)
    win.logged_in = True
    worker.job_done.emit("chats", CHATS)
    texts = [win.chat_list.item(i).text() for i in range(win.chat_list.count())]
    assert texts[0].startswith("📌 العائلة")
    assert "مؤرشفة" in texts[2] and texts[2].startswith("Work")
    win.close()


def picked(n, kind="document", name=None, sender="Ahmed", size=1000, ext=".pdf"):
    import datetime as dt
    return {"msg_id": n, "path": Path(f"c/{sender}/{n}{ext}"), "size": size, "kind": kind,
            "date": dt.datetime(2025, 1, n, 12, tzinfo=dt.timezone.utc),
            "file_name": name, "sender": sender}


def visible_ids(dialog):
    from tgdl.gui.picker import ID_ROLE
    return [r.data(0, ID_ROLE) for r in dialog._rows() if not r.isHidden()]


def test_select_files_dialog(app):
    from tgdl.gui.picker import ID_ROLE
    items = [picked(3, name="c.pdf"), picked(1, name="a.pdf", sender="Sara"),
             picked(2, kind="photo", size=5000, ext=".jpg")]
    dialog = window_mod.SelectFilesDialog(items, already=4)
    rows = dialog._rows()
    assert [r.data(0, ID_ROLE) for r in rows] == [1, 2, 3]   # oldest first
    assert not dialog.ok_btn.isEnabled()

    dialog.search.setText("sara")
    dialog._set_visible(Qt.Checked)                  # ticks the visible rows only
    assert dialog.selected_ids() == [1]
    dialog.search.setText("")
    dialog._toggle(rows[1], 0)                        # double-click the photo
    assert dialog.selected_ids() == [1, 2]
    assert "2" in dialog.ok_btn.text() and dialog.ok_btn.isEnabled()
    dialog._set_visible(Qt.Unchecked)
    assert dialog.selected_ids() == []


def test_picker_filters(app):
    items = [
        picked(1, name="a.pdf", sender="Sara"),
        picked(2, kind="photo", size=5 * 1024 ** 2, ext=".jpg"),
        picked(3, name="report.docx", ext=".docx"),
        picked(4, kind="video", size=300 * 1024 ** 2, ext=".mp4", sender="Sara"),
        picked(5, kind="sticker", ext=".webp"),
        picked(6, name="b.pdf"),
    ]
    # Types start from the settings: stickers off.
    dialog = window_mod.SelectFilesDialog(items, kinds=["photo", "video", "document"])
    assert set(dialog.kind_chips) == {"photo", "video", "document", "sticker"}
    assert "(3)" in dialog.kind_chips["document"].text()
    assert visible_ids(dialog) == [1, 2, 3, 4, 6]

    dialog.kind_chips["document"].setChecked(False)
    assert visible_ids(dialog) == [2, 4]
    dialog.kind_chips["document"].setChecked(True)

    dialog.sender_combo.setCurrentIndex(dialog.sender_combo.findData("Sara"))
    assert visible_ids(dialog) == [1, 4]
    dialog.sender_combo.setCurrentIndex(0)

    dialog.ext_combo.setCurrentIndex(dialog.ext_combo.findData(".pdf"))
    assert visible_ids(dialog) == [1, 6]
    dialog._set_visible(Qt.Checked)                  # tick the two PDFs
    dialog.ext_combo.setCurrentIndex(0)

    dialog.max_check.setChecked(True)
    dialog.max_size.setValue(100)                    # MB: hides the 300 MB video
    assert 4 not in visible_ids(dialog)
    dialog.max_check.setChecked(False)

    from PySide6.QtCore import QDate
    dialog.since_check.setChecked(True)
    dialog.since_date.setDate(QDate(2025, 1, 3))
    dialog.until_check.setChecked(True)
    dialog.until_date.setDate(QDate(2025, 1, 4))
    assert visible_ids(dialog) == [3, 4]
    # Ticked files stay ticked while hidden, and the summary says so.
    assert dialog.selected_ids() == [1, 6]
    assert "غير ظاهر" in dialog.summary.text()

    dialog.reset_filters()
    assert visible_ids(dialog) == [1, 2, 3, 4, 5, 6]   # reset shows every type
    assert not dialog.since_date.isEnabled()


def test_pick_files_then_download_only_those(app, tmp_path, monkeypatch, no_dialogs):
    win, worker = make_window(tmp_path)
    win.logged_in = True
    worker.job_done.emit("chats", CHATS)
    win.chat_list.setCurrentRow(1)
    scans = []
    worker.scan = lambda chat_id, options, name: scans.append((chat_id, options)) or worker._noop()

    class Picker:
        def __init__(self, items, already, parent, kinds=None):
            self.items = items

        def exec(self):
            return window_mod.QDialog.Accepted

        def selected_ids(self):
            return [self.items[0]["msg_id"]]
    monkeypatch.setattr(window_mod, "SelectFilesDialog", Picker)

    win.config.update(kinds=["photo"], exts="zip")      # settings filters...
    win.pick_files()
    assert worker.submitted[-1] == "scan" and scans[0][1].dry_run
    scan_filter = scans[0][1].filter                       # ...don't limit the list
    assert scan_filter.kinds == frozenset(KINDS) and not scan_filter.exts
    assert win.stop_btn.isEnabled()
    worker.job_done.emit("scan", ([picked(9)], Stats(queued=1)))
    _, chat_id, opts = worker.calls[-1]
    assert worker.submitted[-1] == "download"
    assert chat_id == 42 and opts.ids == [9] and not opts.dry_run
    assert opts.filter.kinds == frozenset(KINDS) and not opts.filter.exts

    win._job_finished()
    win.pick_files()
    worker.job_done.emit("scan", ([], Stats(skipped=3)))
    assert any("لا توجد ملفات جديدة" in text for _, text in no_dialogs)
    win.close()



def test_fresh_saved_list_is_not_refetched(app, tmp_path):
    win, worker = make_window(tmp_path)
    worker.me_id = 99
    save_chat_cache(99, CHATS, win.chat_cache_path)
    win.login()
    worker.job_done.emit("login", "Ahmed")
    assert win.chat_list.count() == 3
    assert worker.submitted == ["login"]                     # no GetDialogs storm
    assert "تحديث القائمة" in win.chats_status.text()
    win.load_chats()                                          # the button still works
    assert worker.submitted[-1] == "chats"
    win.close()


def test_stale_saved_list_is_refreshed(app, tmp_path):
    win, worker = make_window(tmp_path)
    worker.me_id = 99
    save_chat_cache(99, CHATS, win.chat_cache_path)
    age_cache(win.chat_cache_path)
    win.login()
    worker.job_done.emit("login", "Ahmed")
    assert worker.submitted[-1] == "chats"
    win.close()


def test_wait_countdown_during_download(app, tmp_path):
    win, worker = make_window(tmp_path)
    win.logged_in = True
    worker.job_done.emit("chats", CHATS)
    win.chat_list.setCurrentRow(0)
    win.start()
    worker.flood_wait.emit(26, "GetHistoryRequest")
    assert "26" in win.current_label.text() and "قراءة رسائل" in win.current_label.text()
    win.wait_until = 0
    win._tick_wait()
    assert win.current_label.text() == "" and not win.wait_timer.isActive()
    win.close()


def test_build_options_everything_ignores_filters():
    from tgdl.gui.settings import build_options
    opts = build_options({"kinds": [], "exts": "pdf", "senders": "x", "since_on": True,
                          "since": "2025-01-01", "max_on": True, "naming": "numbers"},
                         everything=True)
    assert opts.filter.kinds == frozenset(KINDS)
    assert not opts.filter.exts and not opts.filter.senders and opts.filter.max_size is None
    assert opts.since is None and opts.naming == "numbers"     # saving options kept



# -- auto-delete after download -------------------------------------------------

def test_downloaded_files_are_scheduled_and_deleted(app, tmp_path, monkeypatch, no_dialogs):
    from tgdl.gui import cleanup
    win, worker = make_window(tmp_path)
    win.config.update(auto_delete=True, delete_after=15)
    win._refresh_summary()
    assert any(key == "حذف تلقائي" for key, _ in __import__(
        "tgdl.gui.settings", fromlist=["summary"]).summary(win.config))
    win.logged_in = True
    worker.job_done.emit("chats", CHATS)
    win.chat_list.setCurrentRow(0)
    win.start()

    out = tmp_path / "out"
    target = out / "العائلة" / "Ahmed" / "a.pdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    worker.event.emit("saved", {"path": Path("العائلة/Ahmed/a.pdf"), "size": 1})
    (entry,) = cleanup.pending(win.cleanup_store)
    assert entry["path"] == str(target) and entry["root"] == str(out)
    assert 14 * 60 < entry["at"] - __import__("time").time() <= 15 * 60
    assert not win.cleanup_row.isHidden() and "15 دقيقة" in win.cleanup_label.text()

    win.delete_scheduled_now()                       # question box answers "yes"
    assert not target.exists() and not (out / "العائلة").exists()   # empty folders pruned
    assert win.cleanup_row.isHidden()
    win.close()


def test_closing_with_pending_files_starts_the_helper(app, tmp_path, monkeypatch):
    from tgdl.gui import cleanup, updater
    launched = []
    monkeypatch.setattr(updater, "launch_detached", launched.append)
    win, _ = make_window(tmp_path)
    cleanup.schedule(win.cleanup_store, [tmp_path / "f.pdf"], 600)
    win.close()
    assert launched == [cleanup.helper_command()]

    win2, _ = make_window(tmp_path)
    cleanup.clean_due(win2.cleanup_store, now=float("inf"))
    win2.close()
    assert len(launched) == 1                        # nothing pending: no helper


def test_auto_delete_off_schedules_nothing(app, tmp_path):
    from tgdl.gui import cleanup
    win, worker = make_window(tmp_path)
    win.logged_in = True
    worker.job_done.emit("chats", CHATS)
    win.chat_list.setCurrentRow(0)
    win.start()
    worker.event.emit("saved", {"path": Path("a.pdf"), "size": 1})
    assert cleanup.pending(win.cleanup_store) == []
    win.close()


def test_settings_dialog_saves_auto_delete(app, tmp_path):
    from tgdl.gui.settings import SettingsDialog, prefs
    config = Config(tmp_path / "settings.json")
    dialog = SettingsDialog(config)
    assert not dialog.delete_after.isEnabled()
    dialog.delete_check.setChecked(True)
    dialog.delete_after.setValue(30)
    dialog._save()
    p = prefs(Config(tmp_path / "settings.json"))
    assert p["auto_delete"] and p["delete_after"] == 30


def test_cleanup_mode_runs_without_a_window(monkeypatch, tmp_path):
    from tgdl.gui import app as app_mod, cleanup
    calls = []
    monkeypatch.setattr(cleanup, "store_path", lambda: tmp_path / "c.json")
    monkeypatch.setattr(cleanup, "run_helper", lambda store: calls.append(store) or 0)
    assert app_mod.main(["TelegramDownloader.exe", "--cleanup"]) == 0
    assert calls == [tmp_path / "c.json"]


# -- auto-receive ----------------------------------------------------------------

def test_auto_receive_batches_an_album_and_downloads_it(app, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from tests.fakes import FakeClient, FakeMsg, user
    from tgdl.downloader import Options
    from tgdl.gui import worker as worker_mod
    monkeypatch.setattr(worker_mod, "AUTO_SETTLE", 0.05)

    class AutoClient(FakeClient):
        handlers = []

        def add_event_handler(self, handler, event):
            self.handlers.append(handler)

        def remove_event_handler(self, handler):
            self.handlers.remove(handler)

        async def get_entity(self, chat_id):
            return SimpleNamespace(id=5, first_name="أحمد", last_name=None, title=None)

    ahmed = user(5, "أحمد")
    client = AutoClient([FakeMsg(i, ahmed, kind="photo", ext=".jpg") for i in (1, 2, 3)])
    worker = TelegramWorker()
    worker.client = client
    saved, done = [], []
    worker.auto_event.connect(lambda name, data: saved.append((name, data)))
    worker.auto_done.connect(lambda *args: done.append(args))
    try:
        worker.configure_auto(True, Options(out_dir=tmp_path), "private")
        wait_for(app, worker.submit("x", asyncio.sleep(0.01)))
        assert len(client.handlers) == 1

        async def album():
            for i in (1, 2, 3):              # three photos arrive in quick succession
                worker._queue_auto(5, i)
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.3)
        wait_for(app, worker.submit("album", album()))
        app.processEvents()
        assert sorted(client.download_calls) == [1, 2, 3]
        assert [n for n, _ in saved].count("saved") == 3
        assert all(d["chat_id"] == 5 and d["chat_name"] == "أحمد" for _, d in saved)
        assert done == [(5, "أحمد", 3)]                    # one notification per album

        worker.configure_auto(False)
        wait_for(app, worker.submit("x", asyncio.sleep(0.01)))
        assert client.handlers == []
    finally:
        worker.shutdown()


def test_auto_receive_scope(app):
    from types import SimpleNamespace
    worker = TelegramWorker()
    try:
        worker._auto = {"scope": "private", "chat_ids": set(), "options": None}
        person, bot = SimpleNamespace(bot=False), SimpleNamespace(bot=True)
        assert worker._wants(5, True, person)
        assert not worker._wants(5, True, bot)               # bots are not customers
        assert not worker._wants(-100, False, person)        # groups and channels
        worker._auto = {"scope": "selected", "chat_ids": {-100}, "options": None}
        assert worker._wants(-100, False, person) and not worker._wants(5, True, person)
        worker._auto = None
        assert not worker._wants(5, True, person)
    finally:
        worker.shutdown()


# -- orders: card, badges, merge, print, deliver ---------------------------------

def order_window(tmp_path, monkeypatch=None, price=0):
    win, worker = make_window(tmp_path)
    win.config.update(price=price, currency="د.ع")
    win.logged_in = True
    worker.job_done.emit("chats", CHATS)
    win.chat_list.setCurrentRow(1)                 # Sara (42)
    return win, worker


def receive(win, worker, tmp_path, names, chat=(42, "Sara")):
    """Simulate a download of ``names`` for ``chat`` through the saved events."""
    win.chat_list.setCurrentRow(1)
    win.start(chat=chat)
    out = tmp_path / "out"
    for i, name in enumerate(names, 1):
        path = out / "Sara" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name.endswith(".jpg"):
            from PySide6.QtGui import QImage
            img = QImage(40, 30, QImage.Format_RGB888)
            img.fill(Qt.red)
            img.save(str(path), "JPEG")
        else:
            path.write_bytes(b"x")
        worker.event.emit("saved", {"path": Path("Sara") / name, "size": 1, "msg_id": i,
                                    "kind": "photo" if name.endswith(".jpg") else "document"})
    win._job_finished()


def test_order_card_badge_and_price(app, tmp_path):
    win, worker = order_window(tmp_path, price=250)
    assert win.order_card.isHidden()
    receive(win, worker, tmp_path, ["1.jpg", "2.jpg", "3.jpg", "notes.zip"])
    assert not win.order_card.isHidden()
    assert "4 ملف" in win.order_summary.text() and "3 صورة" in win.order_summary.text()
    assert "3 صفحة" in win.order_pages.text() and "750 د.ع" in win.order_pages.text()
    assert "لا يُعرف" in win.order_pages.text()            # the zip has no page count
    item = win.chat_list.item(1)
    assert item.data(window_mod.CHAT_BADGE_ROLE) == 4
    win.close()


def test_merge_order_makes_a_pdf_and_keeps_photos(app, tmp_path):
    pdfium = pytest.importorskip("pypdfium2")
    win, worker = order_window(tmp_path)
    receive(win, worker, tmp_path, ["1.jpg", "2.jpg"])
    before = {p: p.read_bytes() for p in (tmp_path / "out" / "Sara").iterdir()}
    win.merge_order()
    pdf = tmp_path / "out" / "Sara" / "Sara - 2 صورة.pdf"
    assert pdf.exists() and len(pdfium.PdfDocument(str(pdf))) == 2
    assert all(p.read_bytes() == data for p, data in before.items())   # photos untouched
    win.merge_order()                                        # never overwrites
    assert (tmp_path / "out" / "Sara" / "Sara - 2 صورة (2).pdf").exists()
    win.close()


def test_print_order_through_the_print_dialog(app, tmp_path, monkeypatch, no_dialogs):
    pytest.importorskip("pypdfium2")

    class Dialog:
        def __init__(self, printer, parent):
            printer.setOutputFormat(window_mod.QPrinter.PdfFormat)
            printer.setOutputFileName(str(tmp_path / "printed.pdf"))

        def setWindowTitle(self, title):
            pass

        def exec(self):
            return window_mod.QDialog.Accepted
    monkeypatch.setattr(window_mod, "QPrintDialog", Dialog)
    win, worker = order_window(tmp_path)
    receive(win, worker, tmp_path, ["1.jpg", "2.jpg"])
    win.print_order()
    assert (tmp_path / "printed.pdf").exists()
    assert any("أُرسلت 2 صفحة" in text for _, text in no_dialogs)
    win.close()


def test_deliver_order_deletes_or_keeps_files(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    role = {"value": QMessageBox.DestructiveRole}
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: next(
        b for b in self.buttons() if self.buttonRole(b) == role["value"]))
    win, worker = order_window(tmp_path)
    receive(win, worker, tmp_path, ["1.jpg"])
    role["value"] = QMessageBox.AcceptRole                     # finish, keep files
    win.deliver_order()
    assert (tmp_path / "out" / "Sara" / "1.jpg").exists() and win.order_card.isHidden()
    receive(win, worker, tmp_path, ["2.jpg"])
    role["value"] = QMessageBox.DestructiveRole                # finish and delete
    win.deliver_order()
    assert not (tmp_path / "out" / "Sara" / "2.jpg").exists()  # this order's file is gone
    assert (tmp_path / "out" / "Sara" / "1.jpg").exists()      # the kept one is not touched
    (tmp_path / "out" / "Sara" / "1.jpg").unlink()
    receive(win, worker, tmp_path, ["3.jpg"])
    win.deliver_order()
    assert not (tmp_path / "out" / "Sara").exists()            # empty folder removed too
    assert (tmp_path / "out").exists()
    assert win.chat_list.item(1).data(window_mod.CHAT_BADGE_ROLE) == 0
    win.close()


def test_auto_receive_toggle_and_incoming_files(app, tmp_path, no_dialogs):
    win, worker = order_window(tmp_path)
    win.auto_btn.setChecked(True)
    assert win.config["auto_receive"] and "يعمل" in win.auto_btn.text()
    enabled, options, scope, chats = worker.auto
    assert enabled and scope == "private" and options.out_dir == tmp_path / "out"

    path = tmp_path / "out" / "Sara" / "9.jpg"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x")
    worker.auto_event.emit("saved", {"path": Path("Sara/9.jpg"), "msg_id": 9, "kind": "photo",
                                     "chat_id": 42, "chat_name": "Sara", "size": 1})
    worker.auto_done.emit(42, "Sara", 1)
    assert win.chat_list.item(1).data(window_mod.CHAT_BADGE_ROLE) == 1
    assert "وصل 1 ملف من Sara" in win.chats_status.text()
    assert not win.order_card.isHidden()                       # Sara is the open chat

    win.config["auto_scope"] = "selected"
    win._show_chat_auto()
    assert not win.chat_auto_check.isHidden()
    win.chat_auto_check.setChecked(True)
    assert win.config["auto_chats"] == [42] and worker.auto[2] == "selected"
    assert worker.auto[3] == {42}
    win.auto_btn.setChecked(False)
    assert worker.auto is None
    win.close()


def test_close_hides_to_tray_while_receiving(app, tmp_path):
    class Tray:
        messages = []

        def showMessage(self, *args):
            self.messages.append(args)

        def hide(self):
            pass
    win, worker = order_window(tmp_path)
    win.tray = Tray()
    win.show()
    win.auto_btn.setChecked(True)
    win.close()
    assert win.isHidden() and Tray.messages                     # still running, told once
    win.quit_app()                                              # tray menu → خروج
    assert win._quitting


def test_qimage_pixels_are_tightly_packed(app, tmp_path):
    from PySide6.QtGui import QImage
    img = QImage(7, 3, QImage.Format_ARGB32)                    # odd width: padded rows
    img.fill(Qt.transparent)
    img.save(str(tmp_path / "a.png"))
    width, height, data = window_mod.qimage_pixels(tmp_path / "a.png")
    assert (width, height) == (7, 3) and len(data) == 7 * 3 * 3
    assert set(data) == {255}                                   # transparent → white paper


def test_picker_quick_ranges_and_thumbnails(app):
    import datetime as dt
    from tgdl.gui.picker import SelectFilesDialog
    now = dt.datetime.now(dt.timezone.utc)
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QImage
    tiny, raw = QImage(8, 8, QImage.Format_RGB888), QByteArray()
    tiny.fill(Qt.blue)
    buffer = QBuffer(raw)
    buffer.open(QIODevice.WriteOnly)
    tiny.save(buffer, "JPEG")
    jpeg = bytes(raw)
    items = [dict(picked(1), date=now, thumb=jpeg), dict(picked(2), date=now - dt.timedelta(days=1)),
             dict(picked(3), date=now - dt.timedelta(days=30))]
    dialog = SelectFilesDialog(items)
    from tgdl.gui.picker import ID_ROLE
    rows = {r.data(0, ID_ROLE): r for r in dialog._rows()}
    assert not rows[1].icon(0).isNull()                       # tiny preview from Telegram
    assert rows[2].icon(0).isNull()                           # none sent: no icon
    dialog.quick_range(0)
    assert visible_ids(dialog) == [1]
    dialog.quick_range(1)
    assert visible_ids(dialog) == [2]
    dialog.quick_range(6)
    assert visible_ids(dialog) == [2, 1]                        # oldest first, like the chat
    dialog.quick_range(None)
    assert visible_ids(dialog) == [3, 2, 1]


# -- protection against downloading or printing a chat's whole history ------------

def test_first_download_asks_once_then_continues_after_the_last(app, tmp_path, first_download):
    from tgdl.downloader import Stats
    win, worker = make_window(tmp_path)
    win.logged_in = True
    worker.job_done.emit("chats", CHATS)
    win.chat_list.setCurrentRow(1)                            # Sara (42)

    first_download["choice"] = "today"
    win.start()
    _, chat_id, opts = worker.calls[-1]
    assert first_download["asked"] == 1 and opts.min_id == 0
    assert opts.since == window_mod.local_midnight(0)         # today's files only
    worker.job_done.emit("download", Stats(downloaded=2, matched=2, last_msg_id=950))

    win.start()                                               # next time: no question
    _, _, opts = worker.calls[-1]
    assert first_download["asked"] == 1 and opts.min_id == 950
    assert "بعد آخر تنزيل" in win.log_view.toPlainText()
    worker.job_failed.emit("download", asyncio.CancelledError())

    first_download["choice"] = None                           # cancelling asks nothing else
    win.chat_list.setCurrentRow(2)
    calls = len(worker.calls)
    win.start()
    assert len(worker.calls) == calls
    win.close()


def test_failed_download_does_not_move_the_marker(app, tmp_path):
    from tgdl.downloader import Stats
    win, worker = make_window(tmp_path)
    win.logged_in = True
    worker.job_done.emit("chats", CHATS)
    win.chat_list.setCurrentRow(1)
    win.start()
    worker.job_done.emit("download", Stats(downloaded=1, failed=1, last_msg_id=500))
    assert win._marks().get(42) == 0                          # failed file is retried next time
    win.close()


def test_first_download_pick_opens_the_picker(app, tmp_path, first_download):
    win, worker = make_window(tmp_path)
    win.logged_in = True
    worker.job_done.emit("chats", CHATS)
    win.chat_list.setCurrentRow(1)
    worker.scan = lambda chat_id, options, name: worker._noop()
    first_download["choice"] = "pick"
    win.start()
    assert worker.submitted[-1] == "scan"
    win.close()


def old_order(win, tmp_path, ages_in_days):
    """An order for Sara whose files were sent ``ages_in_days`` ago."""
    import time as clock
    book = win._orders()
    paths = []
    for i, age in enumerate(ages_in_days, 1):
        path = tmp_path / "out" / "Sara" / f"{i}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        book.add(42, "Sara", path, "photo", msg_id=i, sent=clock.time() - age * 86400)
        paths.append(path)
    win._refresh_badges()
    win.chat_list.setCurrentRow(1)
    win._refresh_order()
    return paths


def test_old_files_are_hidden_and_printing_asks_first(app, tmp_path, monkeypatch, no_dialogs):
    win, worker = order_window(tmp_path)
    paths = old_order(win, tmp_path, [1000] * 600 + [0, 0, 1])     # 600 old + 3 new
    assert win.order_range.currentData() == 6                       # last 7 days by default
    assert not win.order_warning.isHidden() and "600 ملف غير معروض" in win.order_warning.text()
    assert "3 من 603 ملف" in win.order_summary.text()
    assert win._order_files() == paths[600:]

    printed = []
    monkeypatch.setattr(window_mod.printing, "print_files",
                        lambda printer, files, *a: printed.append(files) or
                        window_mod.printing.PrintResult(pages=len(files)))

    class Dialog:
        def __init__(self, *a):
            pass

        def setWindowTitle(self, t):
            pass

        def exec(self):
            return window_mod.QDialog.Accepted
    monkeypatch.setattr(window_mod, "QPrintDialog", Dialog)
    win.print_order()
    assert printed[-1] == paths[600:]                               # only the recent files
    assert not any(kind == "question" for kind, _ in no_dialogs)    # small, recent: no question

    win.order_range.setCurrentIndex(win.order_range.findData("all"))
    win._order_range_chosen(0)
    win.print_order()
    question = [text for kind, text in no_dialogs if kind == "question"][-1]
    assert "603 ملف" in question and "600 ملف أُرسل قبل أكثر من أسبوع" in question
    win.close()


def test_delivering_the_shown_files_keeps_the_rest(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: next(
        b for b in self.buttons() if self.buttonRole(b) == QMessageBox.DestructiveRole))
    win, worker = order_window(tmp_path)
    paths = old_order(win, tmp_path, [400, 400, 0])
    win.deliver_order()                                             # shown: today's file
    assert not paths[2].exists() and paths[0].exists() and paths[1].exists()
    assert win._orders().files(42) == paths[:2]                     # old ones still listed
    assert win.chat_list.item(1).data(window_mod.CHAT_BADGE_ROLE) == 2
    win.close()


def test_pick_files_inside_the_order(app, tmp_path, monkeypatch):
    win, worker = order_window(tmp_path)
    paths = old_order(win, tmp_path, [0, 0, 0])

    class Picker:
        def __init__(self, items, already, parent):
            self.ok_btn = window_mod.QPushButton()

        def setWindowTitle(self, t):
            pass

        def exec(self):
            return window_mod.QDialog.Accepted

        def selected_ids(self):
            return [0, 2]

        def setParent(self, p):
            pass
    monkeypatch.setattr(window_mod, "SelectFilesDialog", Picker)
    win.order_range.setCurrentIndex(win.order_range.findData("pick"))
    win._order_range_chosen(0)
    assert win._order_files() == [paths[0], paths[2]]
    assert "2 من 3 ملف" in win.order_summary.text()
    win.close()


def test_auto_received_files_advance_the_marker(app, tmp_path):
    win, worker = order_window(tmp_path)
    win.auto_btn.setChecked(True)
    path = tmp_path / "out" / "Sara" / "1.jpg"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x")
    worker.auto_event.emit("saved", {"path": Path("Sara/1.jpg"), "msg_id": 77, "kind": "photo",
                                     "chat_id": 42, "chat_name": "Sara", "size": 1})
    assert win._marks().get(42) == 77
    win.close()
