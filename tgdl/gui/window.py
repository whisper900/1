"""Main window of the desktop app (Arabic, right-to-left)."""

import asyncio
import io
import sys
import time as clock
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QProgressDialog, QPushButton, QSizePolicy, QSplitter, QStackedWidget,
    QVBoxLayout, QWidget)
from telethon import errors as tg_errors

from .. import __version__
from . import updater
from .config import (Config, chat_cache_age, clear_chat_cache, load_chat_cache,
                     save_chat_cache, session_path)
from .settings import (TAB_ACCOUNT, TAB_FILES, SettingsDialog, build_options, default_out_dir,
                       prefs, summary)
from .picker import SelectFilesDialog, size_ar
from .theme import avatar_pixmap, set_kind
from .widgets import (CHAT_ID_ROLE, CHAT_KIND_ROLE, CHAT_KINDS, CHAT_MARK_ROLE,
                      CHAT_TITLE_ROLE, Card, ChatDelegate, chat_subtitle)
from .worker import RESEND, LoginCancelled, TelegramWorker, friendly_error

APP_TITLE = "منزّل ملفات تليكرام"
# A saved chat list younger than this is shown without asking Telegram again
# (every full refresh counts against Telegram's rate limit).
CHAT_REFRESH_AFTER = 10 * 60
# What Telegram is rate-limiting, by request name.
WAIT_REASONS = {
    "GetDialogsRequest": "أثناء تحديث قائمة المحادثات",
    "GetHistoryRequest": "أثناء قراءة رسائل المحادثة",
    "SearchRequest": "أثناء قراءة رسائل المحادثة",
    "GetMessagesRequest": "أثناء قراءة رسائل المحادثة",
    "GetChannelMessagesRequest": "أثناء قراءة رسائل المحادثة",
    "GetFileRequest": "أثناء تنزيل الملفات",
}

ASK_TEXT = {
    "phone": ("تسجيل الدخول",
              "اكتب رقم هاتف حساب تليكرام كاملاً مع رمز الدولة، ويبدأ بعلامة +\n"
              "مثال: +<رمز الدولة><الرقم بدون الصفر الأول>"),
    "password": ("التحقق بخطوتين",
                 "حسابك محمي بكلمة مرور التحقق بخطوتين.\nاكتب كلمة المرور:"),
    "email": ("بريد الدخول مطلوب",
              "يطلب تليكرام إضافة بريد إلكتروني لتسجيل الدخول قبل أن يرسل الكود.\n"
              "اكتب بريدك الإلكتروني، وسيرسل تليكرام إليه كود تأكيد:"),
}
CODE_TITLES = {"code": "كود الدخول", "email_code": "كود تأكيد البريد"}


def _dispose(widget):
    """Free a finished dialog now rather than when its parent window dies."""
    if isinstance(widget, QWidget):
        widget.setParent(None)
        widget.deleteLater()


class CodeDialog(QDialog):
    """Asks for a login code and says where Telegram sent it."""

    def __init__(self, title, info, error="", resend=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        self.result_value = None

        info_label = QLabel(info)
        info_label.setWordWrap(True)
        info_label.setTextFormat(Qt.RichText)
        info_label.setOpenExternalLinks(True)
        self.code_edit = QLineEdit(placeholderText="الكود")
        self.code_edit.setLayoutDirection(Qt.LeftToRight)
        self.code_edit.setAlignment(Qt.AlignCenter)
        font = self.code_edit.font()
        font.setPointSize(font.pointSize() + 4)
        self.code_edit.setFont(font)

        layout = QVBoxLayout(self)
        if error:
            error_label = QLabel(f"⚠ {error}")
            error_label.setWordWrap(True)
            error_label.setStyleSheet("color: #c0392b; font-weight: bold;")
            layout.addWidget(error_label)
        layout.addWidget(info_label)
        layout.addWidget(self.code_edit)

        buttons = QDialogButtonBox()
        ok = set_kind(buttons.addButton("تأكيد", QDialogButtonBox.AcceptRole), "primary")
        ok.setDefault(True)
        buttons.addButton("إلغاء", QDialogButtonBox.RejectRole)
        if resend:
            hint = QLabel(f"لم يصلك الكود؟ يمكنك طلبه مرة أخرى {resend}.")
            hint.setStyleSheet("color: gray;")
            layout.addWidget(hint)
            again = buttons.addButton(f"أرسل الكود {resend}", QDialogButtonBox.ActionRole)
            again.clicked.connect(self._resend)
        buttons.accepted.connect(self._accept_code)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept_code(self):
        if self.code_edit.text().strip():
            self.result_value = self.code_edit.text().strip()
            self.accept()

    def _resend(self):
        self.result_value = RESEND
        self.accept()


class LoginMethodDialog(QDialog):
    """QR code (no SMS needed) or phone number + code."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("تسجيل الدخول")
        self.setMinimumWidth(440)
        self.method = None
        text = QLabel("اختر طريقة تسجيل الدخول إلى حسابك في تليكرام:")
        qr_btn = set_kind(QPushButton("بمسح رمز QR من هاتفك (موصى به، لا يحتاج كوداً)"), "primary")
        qr_btn.setDefault(True)
        qr_btn.clicked.connect(lambda: self._choose("qr"))
        phone_btn = QPushButton("برقم الهاتف وكود الدخول")
        phone_btn.clicked.connect(lambda: self._choose("phone"))
        cancel = QPushButton("إلغاء")
        cancel.clicked.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(text)
        layout.addWidget(qr_btn)
        layout.addWidget(phone_btn)
        layout.addWidget(cancel, alignment=Qt.AlignLeft)

    def _choose(self, method):
        self.method = method
        self.accept()


def qr_pixmap(url, scale=7):
    import segno
    buffer = io.BytesIO()
    segno.make(url, error="m", micro=False).save(buffer, kind="png", scale=scale, border=3)
    pixmap = QPixmap()
    pixmap.loadFromData(buffer.getvalue(), "PNG")
    return pixmap


class QrDialog(QDialog):
    """Shows the login QR code until the phone scans it (or the user cancels)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("تسجيل الدخول برمز QR")
        self.setMinimumWidth(400)
        self.image = QLabel(alignment=Qt.AlignCenter)
        self.image.setMinimumSize(300, 300)
        self.image.setStyleSheet("background: white;")
        steps = QLabel(
            "<ol>"
            "<li>افتح تليكرام على هاتفك.</li>"
            "<li>اذهب إلى <b>الإعدادات</b> ← <b>الأجهزة</b> ← "
            "<b>ربط جهاز سطح المكتب</b> (Link Desktop Device).</li>"
            "<li>وجّه كاميرا الهاتف نحو هذا الرمز.</li>"
            "</ol>"
            "يتجدد الرمز تلقائياً كل نصف دقيقة تقريباً، فلا مشكلة إن انتظرت.")
        steps.setWordWrap(True)
        steps.setTextFormat(Qt.RichText)
        buttons = QDialogButtonBox()
        buttons.addButton("إلغاء", QDialogButtonBox.RejectRole)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.image)
        layout.addWidget(steps)
        layout.addWidget(buttons)

    def show_url(self, url):
        self.image.setPixmap(qr_pixmap(url))


class MainWindow(QMainWindow):
    def __init__(self, config=None, worker=None, autostart=True):
        super().__init__()
        self.config = config if config is not None else Config()
        self.worker = worker if worker is not None else TelegramWorker()
        self.logged_in = False
        self.job = None            # name of the running job
        self.job_future = None
        self.preview = False
        self.mode = None             # "download", "preview" or "scan"
        self.pick_chat = None
        self.qr_dialog = None
        self.pending_update = None
        self.chats_future = None     # chat list refresh runs beside other jobs
        self.showing_saved_chats = False
        self.chat_cache_path = None  # None = the app's settings folder
        self.wait_until = 0.0        # end of Telegram's current rate-limit wait
        self.wait_label = None
        self.wait_text = ""
        self.wait_timer = QTimer(self, interval=1000)
        self.wait_timer.timeout.connect(self._tick_wait)
        self.updater = updater.Updater()

        self.setWindowTitle(f"{APP_TITLE} — الإصدار {__version__}")
        self.resize(1100, 740)
        self.setMinimumSize(860, 600)
        self._build_ui()
        self._refresh_summary()
        self._update_state()

        self.worker.ask.connect(self._on_ask)
        self.worker.job_done.connect(self._on_job_done)
        self.worker.job_failed.connect(self._on_job_failed)
        self.worker.event.connect(self._on_download_event)
        self.worker.notice.connect(self.log)
        self.worker.qr.connect(self._on_qr)
        self.worker.chats_partial.connect(self._on_chats_partial)
        self.worker.flood_wait.connect(self._on_flood_wait)
        self.updater.checked.connect(self._on_update_checked)
        self.updater.check_failed.connect(self._on_update_check_failed)

        self.stats_timer = QTimer(self, interval=300)
        self.stats_timer.timeout.connect(self._refresh_stats)

        if autostart:
            QTimer.singleShot(0, self._first_start)

    # -- layout -------------------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        body = QSplitter(Qt.Horizontal)
        body.setChildrenCollapsible(False)
        body.setHandleWidth(14)
        body.addWidget(self._build_sidebar())
        body.addWidget(self._build_main())
        body.setStretchFactor(0, 0)
        body.setStretchFactor(1, 1)
        body.setSizes([330, 770])
        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(14, 14, 14, 14)
        holder_layout.addWidget(body)
        root.addWidget(holder, 1)
        self.setCentralWidget(central)

    def _build_header(self):
        header = QFrame(objectName="header")
        row = QHBoxLayout(header)
        row.setContentsMargins(18, 10, 18, 10)
        row.setSpacing(10)
        logo = QLabel()
        icon = self.windowIcon().pixmap(30, 30)
        if not icon.isNull():
            logo.setPixmap(icon)
        title = QLabel(APP_TITLE, objectName="appTitle")
        version = QLabel(f"الإصدار {__version__}", objectName="version")
        row.addWidget(logo)
        row.addWidget(title)
        row.addWidget(version)
        row.addStretch(1)

        self.account_avatar = QLabel()
        self.account_label = QLabel("غير مسجّل الدخول")
        self.account_label.setStyleSheet("font-weight: 600;")
        self.login_btn = set_kind(QPushButton("تسجيل الدخول"), "primary")
        self.login_btn.clicked.connect(self.login)
        self.logout_btn = set_kind(QPushButton("خروج"), "ghost")
        self.logout_btn.setToolTip("تسجيل الخروج من الحساب")
        self.logout_btn.clicked.connect(self.logout)
        self.update_btn = set_kind(QPushButton("التحديثات"), "ghost")
        self.update_btn.setToolTip("التحقق من وجود إصدار أحدث")
        self.update_btn.clicked.connect(lambda: self.check_updates(manual=True))
        self.settings_btn = QPushButton("⚙  الإعدادات")
        self.settings_btn.clicked.connect(lambda: self.open_settings())
        for widget in (self.account_avatar, self.account_label, self.login_btn,
                       self.logout_btn, self.update_btn, self.settings_btn):
            row.addWidget(widget)
        return header

    def _build_sidebar(self):
        card = Card(margins=(10, 12, 10, 10), spacing=8)
        card.setMinimumWidth(270)
        title_row = QHBoxLayout()
        title_row.setContentsMargins(6, 0, 6, 0)
        self.chats_title = QLabel("المحادثات", objectName="sectionTitle")
        self.chats_count = QLabel(objectName="badge")
        self.chats_count.hide()
        self.refresh_btn = set_kind(QPushButton("⟳ تحديث"), "ghost")
        self.refresh_btn.setToolTip("تحديث قائمة المحادثات من تليكرام")
        self.refresh_btn.clicked.connect(self.load_chats)
        title_row.addWidget(self.chats_title)
        title_row.addWidget(self.chats_count)
        title_row.addStretch(1)
        title_row.addWidget(self.refresh_btn)

        self.chat_search = QLineEdit(placeholderText="🔍  ابحث عن محادثة", objectName="search")
        self.chat_search.setClearButtonEnabled(True)
        self.chat_search.textChanged.connect(self._filter_chats)
        self.chat_list = QListWidget(objectName="chats")
        self.chat_list.setItemDelegate(ChatDelegate(self.chat_list))
        self.chat_list.setMouseTracking(True)
        self.chat_list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.chat_list.currentItemChanged.connect(lambda *_: self._on_chat_selected())
        self.chats_status = QLabel(objectName="status")
        self.chats_status.setWordWrap(True)

        card.body.addLayout(title_row)
        card.body.addWidget(self.chat_search)
        card.body.addWidget(self.chat_list, 1)
        card.body.addWidget(self.chats_status)
        return card

    def _build_main(self):
        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_empty_page())
        self.pages.addWidget(self._build_chat_page())
        return self.pages

    def _build_empty_page(self):
        card = Card(margins=(30, 30, 30, 30))
        card.body.addStretch(1)
        icon = QLabel(alignment=Qt.AlignCenter)
        pixmap = self.windowIcon().pixmap(96, 96)
        if not pixmap.isNull():
            icon.setPixmap(pixmap)
        self.empty_title = QLabel(alignment=Qt.AlignCenter, objectName="emptyTitle")
        self.empty_text = QLabel(alignment=Qt.AlignCenter)
        self.empty_text.setProperty("muted", True)
        self.empty_text.setWordWrap(True)
        self.empty_login_btn = set_kind(QPushButton("تسجيل الدخول"), "primary")
        self.empty_login_btn.clicked.connect(self.login)
        for widget in (icon, self.empty_title, self.empty_text):
            card.body.addWidget(widget)
        card.body.addWidget(self.empty_login_btn, alignment=Qt.AlignCenter)
        card.body.addStretch(2)
        return card

    def _build_chat_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        # Selected chat and what will happen
        head = Card(margins=(18, 16, 18, 16), spacing=14)
        top = QHBoxLayout()
        top.setSpacing(12)
        self.chat_avatar = QLabel()
        names = QVBoxLayout()
        names.setSpacing(0)
        self.chat_name = QLabel(objectName="chatTitle")
        self.chat_kind = QLabel()
        self.chat_kind.setProperty("muted", True)
        names.addWidget(self.chat_name)
        names.addWidget(self.chat_kind)
        top.addWidget(self.chat_avatar)
        top.addLayout(names, 1)
        head.body.addLayout(top)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color: #E3E7EE;")
        head.body.addWidget(line)

        settings_row = QHBoxLayout()
        settings_row.addWidget(QLabel("إعدادات التنزيل", objectName="sectionTitle"))
        settings_row.addStretch(1)
        edit = set_kind(QPushButton("تعديل"), "ghost")
        edit.clicked.connect(lambda: self.open_settings(TAB_FILES))
        settings_row.addWidget(edit)
        head.body.addLayout(settings_row)
        self.summary_grid = QGridLayout()
        self.summary_grid.setHorizontalSpacing(14)
        self.summary_grid.setVerticalSpacing(6)
        self.summary_grid.setColumnStretch(1, 1)
        head.body.addLayout(self.summary_grid)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.start_btn = set_kind(QPushButton("⬇  تنزيل كل الملفات"), "primary")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(lambda: self.start(preview=False))
        self.pick_btn = QPushButton("☑  اختيار ملفات محددة")
        self.pick_btn.setToolTip("يعرض قائمة الملفات في المحادثة لتختار منها ما تريد تنزيله فقط")
        self.pick_btn.clicked.connect(self.pick_files)
        self.preview_btn = set_kind(QPushButton("معاينة"), "ghost")
        self.preview_btn.setToolTip("يحسب عدد الملفات وحجمها بدون تنزيل")
        self.preview_btn.clicked.connect(lambda: self.start(preview=True))
        actions.addWidget(self.start_btn)
        actions.addWidget(self.pick_btn)
        actions.addWidget(self.preview_btn)
        actions.addStretch(1)
        head.body.addLayout(actions)
        layout.addWidget(head)

        # Progress
        progress_card = Card(margins=(18, 14, 18, 14), spacing=8)
        progress_top = QHBoxLayout()
        progress_top.addWidget(QLabel("التقدّم", objectName="sectionTitle"))
        progress_top.addStretch(1)
        self.open_btn = set_kind(QPushButton("📂 فتح مجلد التنزيلات"), "ghost")
        self.open_btn.clicked.connect(self.open_out_dir)
        self.stop_btn = set_kind(QPushButton("■  إيقاف"), "danger")
        self.stop_btn.clicked.connect(self.stop)
        progress_top.addWidget(self.open_btn)
        progress_top.addWidget(self.stop_btn)
        self.current_label = QLabel(" ")
        self.current_label.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.stats_label = QLabel(" ", objectName="status")
        self.stats_label.setWordWrap(True)
        progress_card.body.addLayout(progress_top)
        progress_card.body.addWidget(self.current_label)
        progress_card.body.addWidget(self.progress)
        progress_card.body.addWidget(self.stats_label)

        self.log_toggle = set_kind(QPushButton("عرض السجل ▾"), "ghost")
        self.log_toggle.setCheckable(True)
        self.log_toggle.toggled.connect(self._toggle_log)
        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.setMaximumBlockCount(5000)
        option = self.log_view.document().defaultTextOption()
        option.setTextDirection(Qt.RightToLeft)
        option.setAlignment(Qt.AlignRight)
        self.log_view.document().setDefaultTextOption(option)
        self.log_view.setMinimumHeight(110)
        self.log_view.hide()
        progress_card.body.addWidget(self.log_toggle, alignment=Qt.AlignRight)
        progress_card.body.addWidget(self.log_view, 1)
        layout.addWidget(progress_card)
        layout.addStretch(1)
        self.chat_page_layout = layout
        return page

    def _toggle_log(self, shown):
        self.log_view.setVisible(shown)
        self.log_toggle.setText("إخفاء السجل ▴" if shown else "عرض السجل ▾")
        # Let the progress card take the free space only while the log is open.
        self.chat_page_layout.setStretch(1, 1 if shown else 0)
        self.chat_page_layout.setStretch(2, 0 if shown else 1)

    # -- preferences --------------------------------------------------------

    def _refresh_summary(self):
        while self.summary_grid.count():
            widget = self.summary_grid.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        for row, (key, value) in enumerate(summary(self.config)):
            key_label = QLabel(key, objectName="summaryKey")
            value_label = QLabel(value)
            value_label.setWordWrap(True)
            value_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            self.summary_grid.addWidget(key_label, row, 0, Qt.AlignTop)
            self.summary_grid.addWidget(value_label, row, 1)

    # -- state --------------------------------------------------------------

    def _on_chat_selected(self):
        chat = self._current_chat()
        if chat is not None:
            item = self.chat_list.currentItem()
            title, kind = item.data(CHAT_TITLE_ROLE), item.data(CHAT_KIND_ROLE)
            self.chat_name.setText(title)
            self.chat_kind.setText(chat_subtitle(kind, item.data(CHAT_MARK_ROLE)))
            self.chat_avatar.setPixmap(avatar_pixmap(title, chat[0], 56, saved=kind == "saved"))
        self._update_state()

    def _update_state(self):
        busy = self.job is not None
        downloading = self.job in ("download", "preview", "scan")
        has_chat = self._current_chat() is not None
        self.login_btn.setVisible(False)     # the welcome page has the login button
        self.login_btn.setEnabled(not busy)
        self.logout_btn.setVisible(self.logged_in)
        self.logout_btn.setEnabled(not busy)
        self.account_avatar.setVisible(self.logged_in)
        self.settings_btn.setEnabled(not downloading)
        self.refresh_btn.setEnabled(self.logged_in and self.chats_future is None
                                    and self.job not in ("login", "logout"))
        self.chat_list.setEnabled(self.logged_in and not downloading)
        ready = self.logged_in and has_chat and not busy
        self.start_btn.setEnabled(ready)
        self.preview_btn.setEnabled(ready)
        self.pick_btn.setEnabled(ready)
        self.stop_btn.setEnabled(downloading)

        if self.logged_in and (has_chat or downloading):
            self.pages.setCurrentIndex(1)
        else:
            self.pages.setCurrentIndex(0)
            if self.logged_in:
                self.empty_title.setText("اختر محادثة")
                self.empty_text.setText("اختر محادثة من القائمة لتنزيل ملفاتها، "
                                        "وكل ملف يُحفظ في مجلد باسم مرسله.")
            elif self.job == "login":
                self.empty_title.setText("جارٍ الاتصال بتليكرام...")
                self.empty_text.setText("")
            else:
                self.empty_title.setText("أهلاً بك 👋")
                self.empty_text.setText("سجّل الدخول إلى حسابك في تليكرام للبدء.")
            self.empty_login_btn.setVisible(not self.logged_in and self.job != "login")

    def _run(self, name, coro):
        self.job = name
        self.job_future = self.worker.submit(name, coro)
        self._update_state()

    def _job_finished(self):
        self.job = None
        self.job_future = None
        self._update_state()
        if self.pending_update is not None:
            release, self.pending_update = self.pending_update, None
            QTimer.singleShot(0, lambda: self._offer_update(release))

    def log(self, text):
        self.log_view.appendPlainText(text)

    # -- login --------------------------------------------------------------

    def _first_start(self):
        if updater.can_self_update():
            updater.cleanup_old(sys.executable)
            QTimer.singleShot(2000, self.check_updates)
        if not self.config.has_api:
            QMessageBox.information(
                self, APP_TITLE,
                "أهلاً بك! قبل الاستخدام الأول تحتاج إلى إدخال رقمي API من موقع تليكرام.\n"
                "ستظهر لك الخطوات في النافذة التالية.")
            if not self.open_settings(TAB_ACCOUNT, require_api=True):
                return
        self.login()

    def open_settings(self, tab=TAB_FILES, require_api=False):
        dialog = SettingsDialog(self.config, self, tab=tab, require_api=require_api,
                                on_check_updates=lambda: self.check_updates(manual=True))
        accepted = dialog.exec() == QDialog.Accepted
        _dispose(dialog)
        if accepted:
            self._refresh_summary()
        return accepted

    def login(self):
        if not self.config.has_api:
            if not self.open_settings(TAB_ACCOUNT, require_api=True):
                return
        self.account_label.setText("جارٍ الاتصال بتليكرام...")
        self._run("login", self.worker.login(
            self.config["api_id"], self.config["api_hash"], session_path()))

    def logout(self):
        answer = QMessageBox.question(
            self, "تسجيل الخروج",
            "هل تريد تسجيل الخروج من الحساب؟\nستحتاج إلى تسجيل الدخول مرة أخرى.")
        if answer == QMessageBox.Yes:
            self._run("logout", self.worker.logout())

    def _on_ask(self, what, details):
        error = details.get("error", "")
        if what == "method":
            dialog = LoginMethodDialog(self)
            dialog.exec()
            method = dialog.method
            _dispose(dialog)
            self.worker.answer(method)
            return
        if what in CODE_TITLES:
            dialog = CodeDialog(CODE_TITLES[what], details.get("info", ""), error,
                                details.get("resend"), self)
            accepted = dialog.exec() == QDialog.Accepted
            value = dialog.result_value if accepted else None
            _dispose(dialog)
            self.worker.answer(value)
            return
        title, text = ASK_TEXT[what]
        if error:
            text = f"⚠ {error}\n\n{text}"
        mode = QLineEdit.Password if what == "password" else QLineEdit.Normal
        value, ok = QInputDialog.getText(self, title, text, mode)
        self.worker.answer(value if ok else None)

    def _on_qr(self, url):
        if url:
            if self.qr_dialog is None:
                self.qr_dialog = QrDialog(self)
                self.qr_dialog.rejected.connect(self._cancel_login)
                self.qr_dialog.open()
            self.qr_dialog.show_url(url)
        elif self.qr_dialog is not None:
            dialog, self.qr_dialog = self.qr_dialog, None
            dialog.rejected.disconnect(self._cancel_login)
            dialog.accept()
            dialog.deleteLater()

    def _cancel_login(self):
        if self.qr_dialog is not None:
            self.qr_dialog.deleteLater()
        self.qr_dialog = None
        if self.job == "login" and self.job_future is not None:
            self.job_future.cancel()

    # -- updates --------------------------------------------------------------

    def check_updates(self, manual=False):
        if not updater.can_self_update():
            if manual:
                QMessageBox.information(self, "التحديثات",
                                        "التحديث التلقائي يعمل في نسخة البرنامج (exe) فقط.")
            return
        if manual:
            self.update_btn.setEnabled(False)
        self.updater.check(manual)

    def _on_update_checked(self, release, manual):
        self.update_btn.setEnabled(True)
        if release is None:
            if manual:
                QMessageBox.information(self, "التحديثات",
                                        f"لديك أحدث إصدار ({__version__}).")
            return
        if self.job is not None:
            self.pending_update = release   # ask when the current task ends
            return
        self._offer_update(release)

    def _on_update_check_failed(self, message, manual):
        self.update_btn.setEnabled(True)
        if manual:
            QMessageBox.warning(self, "التحديثات", message)

    def _offer_update(self, release):
        text = f"يتوفر إصدار جديد من البرنامج: {release.version}\n(الإصدار الحالي: {__version__})"
        if release.notes:
            text += f"\n\nالجديد في هذا الإصدار:\n{release.notes}"
        text += "\n\nهل تريد التحديث الآن؟ سيُعاد تشغيل البرنامج تلقائياً."
        box = QMessageBox(QMessageBox.Question, "تحديث جديد", text, parent=self)
        now = box.addButton("تحديث الآن", QMessageBox.AcceptRole)
        box.addButton("لاحقاً", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        _dispose(box)
        if clicked is now:
            self._download_update(release)

    def _download_update(self, release):
        dialog = QProgressDialog("جارٍ تنزيل التحديث...", "إلغاء", 0, 100, self)
        dialog.setWindowTitle("تحديث البرنامج")
        dialog.setWindowModality(Qt.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.canceled.connect(self.updater.cancel)

        def progress(done, total):
            dialog.setValue(int(done * 100 / total) if total else 0)

        def finished(path):
            disconnect()
            dialog.close()
            try:
                updater.install(path, sys.executable)
                updater.restart(sys.executable)
            except OSError as e:
                QMessageBox.warning(self, "تحديث البرنامج",
                                    f"تعذّر تثبيت التحديث: {e}\nحاول مرة أخرى لاحقاً.")
                return
            self.close()

        def failed(message):
            disconnect()
            dialog.close()
            if not dialog.wasCanceled():
                QMessageBox.warning(self, "تحديث البرنامج", message)

        def disconnect():
            self.updater.progress.disconnect(progress)
            self.updater.downloaded.disconnect(finished)
            self.updater.download_failed.disconnect(failed)

        self.updater.progress.connect(progress)
        self.updater.downloaded.connect(finished)
        self.updater.download_failed.connect(failed)
        dialog.show()
        self.updater.download(release)

    def load_chats(self):
        """Refresh the chat list in the background; downloads stay usable."""
        if self.chats_future is not None:
            return
        if self.chat_list.count() == 0:
            placeholder = QListWidgetItem("جارٍ تحميل المحادثات...")
            placeholder.setFlags(Qt.NoItemFlags)
            self.chat_list.addItem(placeholder)
            self.showing_saved_chats = False
        self.chats_status.setText("جارٍ تحديث قائمة المحادثات من تليكرام...")
        self.chats_future = self.worker.submit("chats", self.worker.load_chats())
        self._update_state()

    def _show_chats(self, chats):
        """Replace the list, keeping the selected chat selected."""
        current = self.chat_list.currentItem()
        selected = current.data(Qt.UserRole) if current else None
        self.chat_list.clear()
        for chat_id, title, kind, mark in chats:
            label = CHAT_KINDS.get(kind, kind)
            if mark == "archived":
                label += "، مؤرشفة"
            pin = "📌 " if mark == "pinned" else ""
            # The delegate draws the row; this text is what search matches.
            item = QListWidgetItem(f"{pin}{title}    ({label})")
            item.setData(CHAT_ID_ROLE, chat_id)
            item.setData(CHAT_TITLE_ROLE, title)
            item.setData(CHAT_KIND_ROLE, kind)
            item.setData(CHAT_MARK_ROLE, mark)
            self.chat_list.addItem(item)
            if chat_id == selected:
                self.chat_list.setCurrentItem(item)
        self._filter_chats(self.chat_search.text())
        self.chats_count.setText(str(len(chats)))
        self.chats_count.setVisible(bool(chats))
        self._update_state()

    def _on_chats_partial(self, chats):
        # A saved list is more complete than the first pages of a refresh.
        if not self.showing_saved_chats:
            self._show_chats(chats)
            self.chats_status.setText(f"تم تحميل {len(chats)} محادثة حتى الآن...")

    def _on_flood_wait(self, seconds, request=""):
        reason = WAIT_REASONS.get(request, "")
        self.log(f"⏳ طلب تليكرام الانتظار {seconds} ثانية {reason}. هذا طبيعي، "
                 "وسيكمل البرنامج تلقائياً.".replace("  ", " "))
        chats = request == "GetDialogsRequest" or (self.chats_future is not None
                                                   and self.job is None)
        self.wait_label = self.chats_status if chats else self.current_label
        self.wait_text = reason
        self.wait_until = clock.monotonic() + seconds
        self._tick_wait()
        self.wait_timer.start()

    def _tick_wait(self):
        left = round(self.wait_until - clock.monotonic())
        if left <= 0 or self.wait_label is None:
            self.wait_timer.stop()
            if self.wait_label is not None and self.wait_label.text().startswith("⏳"):
                self.wait_label.setText("")
            return
        reason = f" {self.wait_text}" if self.wait_text else ""
        self.wait_label.setText(f"⏳ تليكرام طلب الانتظار{reason}: باقي {left} ثانية، "
                                "ثم يكمل البرنامج تلقائياً.")

    def _chats_loaded(self, chats):
        self.chats_future = None
        self.showing_saved_chats = False
        self._show_chats(chats)
        self.chats_status.setText("")
        if self.worker.me_id is not None:
            save_chat_cache(self.worker.me_id, chats, self.chat_cache_path)

    def _chats_failed(self, exc):
        self.chats_future = None
        if self.showing_saved_chats:
            self.chats_status.setText("تعذّر تحديث القائمة؛ تُعرض القائمة المحفوظة.")
        else:
            self.chat_list.clear()
            self.chats_status.setText("")
        self._update_state()
        if not isinstance(exc, asyncio.CancelledError):
            QMessageBox.warning(self, APP_TITLE, friendly_error(exc))

    def _filter_chats(self, text):
        needle = text.strip().casefold()
        for i in range(self.chat_list.count()):
            item = self.chat_list.item(i)
            item.setHidden(bool(needle) and needle not in item.text().casefold())

    # -- downloading --------------------------------------------------------

    def _collect_options(self, preview, everything=False):
        try:
            return build_options(self.config, preview, everything=everything)
        except ValueError as e:
            QMessageBox.warning(self, APP_TITLE, str(e))
            return None

    def _current_chat(self):
        item = self.chat_list.currentItem()
        chat_id = item.data(Qt.UserRole) if item else None
        return (chat_id, item.data(Qt.UserRole + 1)) if chat_id is not None else None

    def _begin(self, job, message, coro_factory, preview):
        self.preview = preview
        self.mode = job
        self.log_view.clear()
        self.progress.setValue(0)
        self.current_label.setText(" ")
        self.log(message)
        self.worker.downloader = None
        self._run(job, coro_factory())
        self.stats_timer.start()

    def start(self, preview=False, ids=None, chat=None):
        chat = chat or self._current_chat()
        if chat is None:
            return
        # Hand-picked files are downloaded as picked, whatever the filters say.
        options = self._collect_options(preview, everything=bool(ids))
        if options is None:
            return
        options.ids = ids
        chat_id, name = chat
        if ids:
            message = f"بدء تنزيل {len(ids)} ملف محدد من: {name}"
        else:
            message = f"بدء {'معاينة' if preview else 'تنزيل'} الملفات من: {name}"
        self._begin("preview" if preview else "download", message,
                    lambda: self.worker.download(chat_id, options, name), preview)

    def pick_files(self):
        """Find the matching files, then let the user tick the ones to download."""
        chat = self._current_chat()
        if chat is None:
            return
        # List every file; the picker's own filters narrow it down.
        options = self._collect_options(preview=True, everything=True)
        if options is None:
            return
        self.pick_chat = chat
        self._begin("scan", f"جارٍ البحث عن الملفات في: {chat[1]}",
                    lambda: self.worker.scan(chat[0], options, chat[1]), True)

    def _show_picker(self, result):
        self.stats_timer.stop()
        self._refresh_stats()
        items, stats = result
        if not items:
            msg = "لا توجد ملفات جديدة مطابقة للاختيارات في هذه المحادثة."
            if stats.skipped:
                msg += f"\n{stats.skipped} ملف منزّل مسبقاً."
            self.current_label.setText(msg.replace("\n", " "))
            QMessageBox.information(self, "اختيار الملفات", msg)
            return
        self.current_label.setText(f"وُجد {len(items)} ملف. اختر ما تريد تنزيله.")
        dialog = SelectFilesDialog(items, stats.skipped, self, kinds=prefs(self.config)["kinds"])
        accepted = dialog.exec() == QDialog.Accepted
        ids = dialog.selected_ids() if accepted else []
        _dispose(dialog)
        if ids:
            self.start(ids=ids, chat=self.pick_chat)

    def stop(self):
        if self.job_future is not None:
            self.job_future.cancel()

    def open_out_dir(self):
        path = Path(prefs(self.config)["out_dir"] or default_out_dir())
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _refresh_stats(self):
        dl = self.worker.downloader
        if dl is None:
            return
        s = dl.stats
        if self.mode == "scan":
            text = f"تم فحص {s.scanned} رسالة — وُجد {s.queued} ملف جديد"
        elif self.preview:
            text = (f"تم فحص {s.scanned} رسالة — سيتم تنزيل {s.queued} ملف "
                    f"({size_ar(s.bytes)}) — موجود مسبقاً {s.skipped}")
        else:
            text = (f"تم فحص {s.scanned} رسالة — تم تنزيل {s.downloaded} ملف "
                    f"({size_ar(s.bytes)}) — موجود مسبقاً {s.skipped} — فشل {s.failed}")
        self.stats_label.setText(text)

    def _describe(self, rel):
        """``chat/sender/[type/]file`` -> "file — sender" for the log."""
        parts = Path(rel).parts
        sender_index = 1 if prefs(self.config)["chat_folder"] else 0
        sender = parts[sender_index] if len(parts) > sender_index + 1 else ""
        # Unicode isolates keep "محاضرة 5.mp4" from being reordered by the
        # surrounding right-to-left text.
        name = f"\u2068{parts[-1]}\u2069"
        return f"{name} — \u2068{sender}\u2069" if sender else name

    def _on_download_event(self, name, data):
        if name == "progress":
            total = data["total"] or 0
            pct = int(data["current"] * 100 / total) if total else 0
            self.progress.setValue(pct)
            self.current_label.setText(f"جارٍ تنزيل: {self._describe(data['path'])}")
        elif name == "planned":
            self.log(f"• {self._describe(data['path'])} — {size_ar(data['size'])}")
        elif name == "saved":
            self.log(f"✔ {self._describe(data['path'])} — {size_ar(data['size'])}")
        elif name == "failed":
            self.log(f"✖ فشل: {self._describe(data['path'])} — {friendly_error(data['error'])}")
        elif name == "wait":
            self.log(f"⏳ طلب تليكرام الانتظار {data['seconds']} ثانية، سيكمل البرنامج تلقائياً...")
        elif name == "retry":
            self.log(f"↻ مشكلة في الاتصال، إعادة المحاولة بعد {data['delay']} ثانية...")

    # -- job results ----------------------------------------------------------

    def _on_job_done(self, name, result):
        if name == "chats":
            self._chats_loaded(result)
            return
        self._job_finished()
        if name == "login":
            self.logged_in = True
            self.account_label.setText(result)
            self.account_avatar.setPixmap(
                avatar_pixmap(result, self.worker.me_id or result, 30))
            saved = load_chat_cache(self.worker.me_id, self.chat_cache_path)
            age = chat_cache_age(self.worker.me_id, self.chat_cache_path)
            if saved:
                self._show_chats(saved)
                self.showing_saved_chats = True
            self._update_state()
            if saved and age is not None and age < CHAT_REFRESH_AFTER:
                minutes = max(1, round(age / 60))
                self.chats_status.setText(f"القائمة محدّثة قبل {minutes} دقيقة. "
                                          "اضغط «تحديث القائمة» لتحديثها الآن.")
            else:
                self.load_chats()
        elif name in ("download", "preview"):
            self._finish_download(result)
        elif name == "scan":
            self._show_picker(result)
        elif name == "logout":
            self.logged_in = False
            clear_chat_cache(self.chat_cache_path)
            self.chats_count.hide()
            self.chat_list.clear()
            self.account_label.setText("تم تسجيل الخروج")
            self._update_state()

    def _on_job_failed(self, name, exc):
        if name == "chats":
            self._chats_failed(exc)
            return
        self._job_finished()
        if isinstance(exc, (tg_errors.AuthKeyUnregisteredError,
                            tg_errors.SessionRevokedError)):
            # The session was ended from another device: log in again.
            self.logged_in = False
            self.account_label.setText("انتهت جلسة الدخول، سجّل الدخول من جديد")
            self._update_state()
        if name in ("download", "preview", "scan"):
            self.stats_timer.stop()
            self._refresh_stats()
            if isinstance(exc, asyncio.CancelledError):
                self.current_label.setText("تم الإيقاف. يمكنك الضغط على «ابدأ التنزيل» "
                                           "لاحقاً وسيكمل من حيث توقف.")
            else:
                self.current_label.setText(" ")
                QMessageBox.warning(self, APP_TITLE, friendly_error(exc))
            return
        if name == "login":
            self.logged_in = False
            self._on_qr("")
            if isinstance(exc, (LoginCancelled, asyncio.CancelledError)):
                self.account_label.setText("غير مسجّل الدخول")
                return
            self.account_label.setText("لم يتم تسجيل الدخول")
            QMessageBox.warning(self, APP_TITLE, friendly_error(exc))
            if "ApiIdInvalid" in type(exc).__name__:
                self.open_settings(TAB_ACCOUNT, require_api=True)
            return
        QMessageBox.warning(self, APP_TITLE, friendly_error(exc))

    def _finish_download(self, stats):
        self.stats_timer.stop()
        self._refresh_stats()
        self.progress.setValue(100 if stats.matched else 0)
        if self.preview:
            if stats.queued:
                msg = f"سيتم تنزيل {stats.queued} ملف بحجم {size_ar(stats.bytes)} تقريباً."
            else:
                msg = "لا توجد ملفات جديدة مطابقة للاختيارات."
            if stats.skipped:
                msg += f"\n{stats.skipped} ملف موجود مسبقاً ولن يُنزَّل مرة أخرى."
            self.current_label.setText(msg.replace("\n", " "))
            QMessageBox.information(self, "المعاينة", msg)
            return
        self.current_label.setText("اكتمل التنزيل.")
        msg = f"تم تنزيل {stats.downloaded} ملف ({size_ar(stats.bytes)})."
        if stats.skipped:
            msg += f"\n{stats.skipped} ملف كان موجوداً مسبقاً."
        if stats.failed:
            msg += (f"\nفشل {stats.failed} ملف (التفاصيل في السجل). "
                    "اضغط «ابدأ التنزيل» مرة أخرى لإعادة المحاولة.")
        if not stats.matched:
            msg = "لم يتم العثور على ملفات مطابقة للاختيارات في هذه المحادثة."
        box = QMessageBox(QMessageBox.Information, "اكتمل", msg, parent=self)
        open_btn = box.addButton("فتح المجلد", QMessageBox.AcceptRole)
        box.addButton("إغلاق", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        _dispose(box)
        if clicked is open_btn:
            self.open_out_dir()

    # -- closing --------------------------------------------------------------

    def closeEvent(self, event):
        if self.job in ("download", "preview", "scan"):
            answer = QMessageBox.question(self, APP_TITLE,
                                          "التنزيل ما زال يعمل. هل تريد إيقافه والخروج؟")
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.stop()
        self.worker.shutdown()
        event.accept()
