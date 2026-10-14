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
    ask = Signal(str, dict)
    qr = Signal(str)
    notice = Signal(str)
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
