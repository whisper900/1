"""Runs Telethon on a background asyncio loop and talks to the UI via signals.

The UI thread never blocks on the network: every operation is a coroutine
submitted to the worker loop, and its outcome comes back as a Qt signal.
"""

import asyncio
import threading
import time

from PySide6.QtCore import QObject, Signal
from telethon import TelegramClient, errors

from ..downloader import Downloader
from ..naming import display_name


class LoginCancelled(Exception):
    pass


class TelegramWorker(QObject):
    # Emitted from the worker thread; Qt delivers them on the UI thread.
    ask = Signal(str, str)              # what to ask (phone/code/password), error text
    job_done = Signal(str, object)      # job name, result
    job_failed = Signal(str, object)    # job name, exception
    event = Signal(str, dict)           # downloader event name, data

    def __init__(self):
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever,
                                        name="telegram", daemon=True)
        self._thread.start()
        self.client = None
        self._client_key = None
        self._answer = None
        self.chats = {}           # dialog id -> (entity, name)
        self.downloader = None    # the running Downloader, for live stats
        self._last_progress = 0.0

    # -- plumbing ---------------------------------------------------------

    def submit(self, name, coro):
        """Run ``coro`` on the worker loop; report through job_done/failed."""
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def finished(f):
            if f.cancelled():
                self.job_failed.emit(name, asyncio.CancelledError())
            elif f.exception() is not None:
                self.job_failed.emit(name, f.exception())
            else:
                self.job_done.emit(name, f.result())
        future.add_done_callback(finished)
        return future

    def answer(self, value):
        """Called by the UI with the user's reply to ``ask`` (None = cancel)."""
        def resolve():
            if self._answer is not None and not self._answer.done():
                self._answer.set_result(value)
        self.loop.call_soon_threadsafe(resolve)

    async def _ask(self, what, error=""):
        self._answer = self.loop.create_future()
        self.ask.emit(what, error)
        value = await self._answer
        if not value or not str(value).strip():
            raise LoginCancelled()
        return str(value).strip()

    def shutdown(self, timeout=5):
        async def close():
            if self.client is not None:
                await self.client.disconnect()
        try:
            asyncio.run_coroutine_threadsafe(close(), self.loop).result(timeout)
        except Exception:  # noqa: BLE001 - best effort on exit
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout)
        if not self._thread.is_alive():
            self.loop.close()

    # -- jobs (coroutines run on the worker loop) -------------------------

    async def login(self, api_id, api_hash, session):
        key = (int(api_id), api_hash, str(session))
        if self.client is not None and self._client_key != key:
            await self.client.disconnect()
            self.client = None
        if self.client is None:
            self.client = TelegramClient(str(session), int(api_id), api_hash,
                                         flood_sleep_threshold=600)
            self._client_key = key
        if not self.client.is_connected():
            await self.client.connect()
        if not await self.client.is_user_authorized():
            await self._interactive_login()
        me = await self.client.get_me()
        return display_name(me) or (f"+{me.phone}" if me.phone else str(me.id))

    async def _interactive_login(self):
        client = self.client
        error = ""
        while True:
            phone = await self._ask("phone", error)
            try:
                sent = await client.send_code_request(phone)
                break
            except (errors.PhoneNumberInvalidError, errors.PhoneNumberBannedError,
                    errors.PhoneNumberUnoccupiedError, TypeError, ValueError) as e:
                error = friendly_error(e)

        error = ""
        while True:
            code = await self._ask("code", error)
            try:
                await client.sign_in(phone=phone, code=code.replace(" ", ""),
                                     phone_code_hash=sent.phone_code_hash)
                return
            except errors.SessionPasswordNeededError:
                break
            except (errors.PhoneCodeInvalidError, errors.PhoneCodeEmptyError):
                error = "الكود غير صحيح، حاول مرة أخرى."
            except errors.PhoneCodeExpiredError:
                sent = await client.send_code_request(phone)
                error = "انتهت صلاحية الكود، أرسلنا لك كوداً جديداً."

        error = ""
        while True:
            password = await self._ask("password", error)
            try:
                await client.sign_in(password=password)
                return
            except errors.PasswordHashInvalidError:
                error = "كلمة المرور غير صحيحة، حاول مرة أخرى."

    async def load_chats(self):
        chats = []
        self.chats.clear()
        async for dialog in self.client.iter_dialogs():
            entity = dialog.entity
            if getattr(entity, "is_self", False):
                name, kind = "الرسائل المحفوظة", "saved"
            else:
                name = dialog.name or str(dialog.id)
                kind = ("user" if dialog.is_user else
                        "channel" if dialog.is_channel and not dialog.is_group else
                        "group")
            self.chats[dialog.id] = (entity, name)
            chats.append((dialog.id, name, kind))
        return chats

    async def download(self, chat_id, options):
        entity, name = self.chats[chat_id]
        self.downloader = Downloader(self.client, entity, options, chat_name=name,
                                     log=lambda _line: None, on_event=self._on_event)
        return await self.downloader.run()

    async def logout(self):
        if self.client is not None:
            if not self.client.is_connected():
                await self.client.connect()
            await self.client.log_out()   # also deletes the session file
            self.client = None
            self._client_key = None
        self.chats.clear()

    def _on_event(self, name, **data):
        if name == "progress":
            now = time.monotonic()
            done = data["current"] >= (data["total"] or 1)
            if now - self._last_progress < 0.15 and not done:
                return  # Telethon reports every chunk; keep the UI calm
            self._last_progress = now
        self.event.emit(name, data)


def friendly_error(exc):
    """Arabic explanation for the errors a user can actually do something about."""
    if isinstance(exc, LoginCancelled):
        return "تم إلغاء تسجيل الدخول."
    if isinstance(exc, errors.ApiIdInvalidError):
        return "رقم API ID أو API Hash غير صحيح. افتح الإعدادات وتأكد منهما."
    if isinstance(exc, errors.PhoneNumberInvalidError):
        return "رقم الهاتف غير صحيح. اكتبه كاملاً مع رمز الدولة، ويبدأ بعلامة +"
    if isinstance(exc, errors.PhoneNumberBannedError):
        return "هذا الرقم محظور من تليكرام."
    if isinstance(exc, errors.PhoneNumberUnoccupiedError):
        return "لا يوجد حساب تليكرام بهذا الرقم."
    if isinstance(exc, errors.FloodWaitError):
        minutes = max(1, round(exc.seconds / 60))
        return f"طلب تليكرام الانتظار حوالي {minutes} دقيقة قبل المحاولة مرة أخرى."
    if isinstance(exc, (errors.AuthKeyUnregisteredError, errors.SessionRevokedError,
                        errors.AuthKeyDuplicatedError)):
        return "انتهت جلسة الدخول. سجّل الدخول من جديد."
    if isinstance(exc, (ConnectionError, OSError, asyncio.TimeoutError)):
        return "تعذّر الاتصال بتليكرام. تأكد من اتصال الإنترنت ثم حاول مرة أخرى."
    if isinstance(exc, (TypeError, ValueError)) and "phone" in str(exc).lower():
        return "رقم الهاتف غير صحيح. اكتبه كاملاً مع رمز الدولة، ويبدأ بعلامة +"
    return f"حدث خطأ: {type(exc).__name__}: {exc}"
