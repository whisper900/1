"""Runs Telethon on a background asyncio loop and talks to the UI via signals.

The UI thread never blocks on the network: every operation is a coroutine
submitted to the worker loop, and its outcome comes back as a Qt signal.
"""

import asyncio
import threading
import time

from PySide6.QtCore import QObject, Signal
from telethon import TelegramClient, errors
from telethon.tl import functions, types

from ..downloader import Downloader
from ..naming import display_name


class LoginCancelled(Exception):
    pass


class LoginBlocked(Exception):
    """Telegram will not send a code this program can use; message is Arabic."""


# Returned by the code dialog when the user asks for the code another way.
RESEND = "\x00resend"

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def normalize_digits(text):
    """Arabic-Indic and Persian digits -> ASCII (Telegram only accepts 0-9)."""
    return str(text).translate(_DIGITS)


def normalize_phone(text):
    """``"+1 555-010 0000"`` -> ``"15550100000"``; ``None`` if not international.

    A number without its country code would be read as some other country's
    number, and the code could be sent to a stranger's account, so the
    leading ``+`` (or ``00``) is required.
    """
    text = normalize_digits(text).strip()
    if text.startswith("00"):
        text = "+" + text[2:]
    if not text.startswith("+"):
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits if 7 <= len(digits) <= 15 else None


def describe_sent_code(sent):
    """Arabic explanation of where Telegram sent the login code."""
    kind = sent.type
    length = getattr(kind, "length", None)
    digits = f" (من {length} أرقام)" if length else ""
    if isinstance(kind, types.auth.SentCodeTypeApp):
        return ("أرسل تليكرام الكود <b>داخل تطبيق تليكرام</b>، وليس برسالة SMS.<br>"
                "افتح تليكرام على هاتفك أو حاسوبك، وابحث عن محادثة اسمها "
                "<b>Telegram</b> عليها علامة زرقاء ✔. الكود موجود فيها" + digits + ".")
    if isinstance(kind, types.auth.SentCodeTypeEmailCode):
        return ("أرسل تليكرام الكود إلى <b>بريدك الإلكتروني</b> "
                f"<span dir='ltr'>{kind.email_pattern}</span> وليس إلى تطبيق تليكرام، "
                "لأن حسابك مضبوط على استلام كود الدخول بالبريد.<br>"
                "افحص البريد، وصندوق الرسائل غير المرغوب فيها (Spam) أيضاً.")
    if isinstance(kind, (types.auth.SentCodeTypeSms, types.auth.SentCodeTypeFirebaseSms)):
        return "أرسل تليكرام الكود <b>برسالة SMS</b> إلى رقم هاتفك" + digits + "."
    if isinstance(kind, (types.auth.SentCodeTypeSmsWord, types.auth.SentCodeTypeSmsPhrase)):
        return "أرسل تليكرام <b>رسالة SMS</b> فيها كلمة الدخول. اكتب الكلمة كما هي."
    if isinstance(kind, types.auth.SentCodeTypeCall):
        return "سيتصل بك تليكرام <b>باتصال هاتفي</b> ويقرأ لك الكود" + digits + "."
    if isinstance(kind, types.auth.SentCodeTypeMissedCall):
        return ("ستصلك <b>مكالمة فائتة</b>. الكود هو آخر "
                f"{kind.length} أرقام من رقم المتصل.")
    if isinstance(kind, types.auth.SentCodeTypeFlashCall):
        return "ستصلك <b>مكالمة سريعة</b>، والكود هو آخر أرقام رقم المتصل."
    if isinstance(kind, types.auth.SentCodeTypeFragmentSms):
        return ("الكود أُرسل عبر <b>Fragment</b> (للأرقام المجهولة). افتحه من: "
                f"<a href='{kind.url}'>{kind.url}</a>")
    return "أرسل تليكرام كود الدخول. تحقق من تطبيق تليكرام ومن رسائل SMS."


def describe_next_type(sent):
    """How a resend would deliver the code, or ``None`` if it can't."""
    nxt = getattr(sent, "next_type", None)
    if isinstance(nxt, types.auth.CodeTypeSms):
        return "برسالة SMS"
    if isinstance(nxt, types.auth.CodeTypeCall):
        return "باتصال هاتفي"
    if isinstance(nxt, (types.auth.CodeTypeFlashCall, types.auth.CodeTypeMissedCall)):
        return "بمكالمة فائتة"
    if isinstance(nxt, types.auth.CodeTypeFragmentSms):
        return "عبر Fragment"
    return None


class TelegramWorker(QObject):
    # Emitted from the worker thread; Qt delivers them on the UI thread.
    ask = Signal(str, dict)             # what to ask, {"error", "info", "resend", ...}
    notice = Signal(str)                # a line for the window's log
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

    async def _ask(self, what, error="", **details):
        self._answer = self.loop.create_future()
        self.ask.emit(what, {"error": error, **details})
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
            phone = normalize_phone(await self._ask("phone", error))
            if phone is None:
                error = "اكتب الرقم كاملاً مع رمز الدولة، ويبدأ بعلامة +"
                continue
            try:
                sent = await self._send_code(phone)
                break
            except (errors.PhoneNumberInvalidError, errors.PhoneNumberBannedError,
                    errors.PhoneNumberUnoccupiedError) as e:
                error = friendly_error(e)

        error = ""
        while True:
            if isinstance(sent.type, types.auth.SentCodeTypeSetUpEmailRequired):
                sent = await self._set_up_login_email(phone, sent)
                continue

            self.notice.emit(f"طريقة إرسال الكود: {type(sent.type).__name__}")
            resend = describe_next_type(sent)
            code = await self._ask("code", error, info=describe_sent_code(sent),
                                   resend=resend)
            error = ""
            if code == RESEND:
                try:
                    sent = await self._checked(await client(
                        functions.auth.ResendCodeRequest(phone, sent.phone_code_hash)))
                except errors.SendCodeUnavailableError:
                    error = "لا توجد طريقة أخرى لإرسال الكود الآن. انتظر قليلاً ثم حاول."
                except errors.PhoneCodeExpiredError:
                    sent = await self._send_code(phone)
                continue
            try:
                await client.sign_in(phone=phone, code=normalize_digits(code).replace(" ", ""),
                                     phone_code_hash=sent.phone_code_hash)
                return
            except errors.SessionPasswordNeededError:
                break
            except (errors.PhoneCodeInvalidError, errors.PhoneCodeEmptyError):
                error = "الكود غير صحيح، حاول مرة أخرى."
            except errors.PhoneCodeExpiredError:
                sent = await self._send_code(phone)
                error = "انتهت صلاحية الكود، أرسلنا لك كوداً جديداً."

        error = ""
        while True:
            password = await self._ask("password", error)
            try:
                await client.sign_in(password=password)
                return
            except errors.PasswordHashInvalidError:
                error = "كلمة المرور غير صحيحة، حاول مرة أخرى."

    async def _send_code(self, phone):
        request = functions.auth.SendCodeRequest(
            phone, self.client.api_id, self.client.api_hash, types.CodeSettings())
        for attempt in range(3):
            try:
                return await self._checked(await self.client(request))
            except errors.AuthRestartError:
                if attempt == 2:
                    raise

    @staticmethod
    async def _checked(sent):
        if isinstance(sent, types.auth.SentCodePaymentRequired):
            raise LoginBlocked(
                "تليكرام لا يرسل كود الدخول لهذا الرقم إلى البرامج الخارجية حالياً.\n"
                "سجّل الدخول أولاً في تطبيق تليكرام الرسمي على هاتفك، ثم حاول هنا مرة أخرى؛ "
                "سيصلك الكود عندها داخل التطبيق.")
        if not isinstance(sent, types.auth.SentCode):
            raise LoginBlocked(f"رد غير متوقع من تليكرام: {type(sent).__name__}")
        return sent

    async def _set_up_login_email(self, phone, sent):
        """Telegram wants a login email before it sends any code."""
        purpose = types.EmailVerifyPurposeLoginSetup(phone, sent.phone_code_hash)
        error = ""
        while True:
            email = await self._ask("email", error)
            try:
                await self.client(functions.account.SendVerifyEmailCodeRequest(
                    purpose=purpose, email=email))
                break
            except errors.EmailInvalidError:
                error = "البريد الإلكتروني غير صحيح."
            except errors.RPCError as e:
                error = f"لم يقبل تليكرام هذا البريد ({e.message}). جرّب بريداً آخر."
        error = ""
        while True:
            code = await self._ask("email_code", error, info=(
                f"أرسل تليكرام كوداً إلى <span dir='ltr'>{email}</span>. "
                "افحص البريد وصندوق Spam."))
            try:
                result = await self.client(functions.account.VerifyEmailRequest(
                    purpose=purpose,
                    verification=types.EmailVerificationCode(normalize_digits(code))))
                return await self._checked(getattr(result, "sent_code", result))
            except (errors.CodeInvalidError, errors.EmailVerifyExpiredError,
                    errors.EmailHashExpiredError) as e:
                error = friendly_error(e)

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
    if isinstance(exc, LoginBlocked):
        return str(exc)
    if isinstance(exc, errors.CodeInvalidError):
        return "الكود غير صحيح، حاول مرة أخرى."
    if isinstance(exc, (errors.EmailVerifyExpiredError, errors.EmailHashExpiredError)):
        return "انتهت صلاحية الكود. أغلق النافذة وابدأ تسجيل الدخول من جديد."
    if isinstance(exc, (errors.PhoneNumberFloodError, errors.PhonePasswordFloodError)):
        return "محاولات كثيرة لهذا الرقم. انتظر بضع ساعات ثم حاول مرة أخرى."
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
