"""Main window of the desktop app (Arabic, right-to-left)."""

import asyncio
import datetime
import io
import sys
import time as clock
from pathlib import Path

import os

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QIcon, QImage, QImageReader, QPainter, QPixmap
from PySide6.QtPrintSupport import QPrintDialog, QPrinter
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QProgressDialog, QPushButton, QSplitter, QStackedWidget,
    QSystemTrayIcon, QToolButton, QVBoxLayout, QWidget)
from telethon import errors as tg_errors

from .. import __version__
from ..pdfmerge import merge_images
from . import cleanup, printing, updater
from .config import (Config, app_dir, chat_cache_age, clear_chat_cache, load_chat_cache,
                     save_chat_cache, session_path)
from .orders import ChatMarks, OrderBook, group_of, summarize, within_days
from .settings import (TAB_ACCOUNT, TAB_FILES, SettingsDialog, build_options,
                       default_out_dir, format_money, prefs, short_summary)
from .picker import SelectFilesDialog, size_ar
from .theme import avatar_pixmap, set_kind
from .widgets import (CHAT_BADGE_ROLE, CHAT_ID_ROLE, CHAT_KIND_ROLE, CHAT_KINDS,
                      CHAT_MARK_ROLE, CHAT_TITLE_ROLE, Card, ChatDelegate, chat_subtitle)
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


def qimage_pixels(path):
    """Decoded RGB pixels for images a PDF can't hold as they are (PNG with
    transparency, WebP...). Shown the way phones show them, on white paper."""
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    image = reader.read()
    if image.isNull():
        return None
    canvas = QImage(image.size(), QImage.Format_RGB888)
    canvas.fill(Qt.white)
    painter = QPainter(canvas)
    painter.drawImage(0, 0, image)
    painter.end()
    width, height, line = canvas.width(), canvas.height(), canvas.bytesPerLine()
    data = bytes(canvas.constBits())
    if line != width * 3:            # drop the padding at the end of each row
        data = b"".join(data[row * line:row * line + width * 3] for row in range(height))
    return width, height, data


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


class FirstDownloadDialog(QDialog):
    """Asked before the first download from a chat, which may hold years of files."""

    def __init__(self, chat_name, parent=None):
        super().__init__(parent)
        self.setWindowTitle("أول تنزيل من هذه المحادثة")
        self.setMinimumWidth(460)
        self.choice = None
        text = QLabel(f"لم تنزّل من «{chat_name}» من قبل، وقد تحتوي المحادثة على ملفات "
                      "قديمة كثيرة.\nمن أي فترة تريد الملفات؟")
        text.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(text)
        for choice, label, kind in (("today", "ملفات اليوم فقط", "primary"),
                                    ("week", "آخر 7 أيام", None),
                                    ("pick", "اختيار الملفات بنفسي...", None),
                                    ("all", "كل ملفات المحادثة منذ البداية", "ghost")):
            button = QPushButton(label)
            if kind:
                set_kind(button, kind)
            button.clicked.connect(lambda _=False, c=choice: self._choose(c))
            layout.addWidget(button)
        cancel = QPushButton("إلغاء")
        cancel.clicked.connect(self.reject)
        layout.addWidget(cancel, alignment=Qt.AlignLeft)

    def _choose(self, choice):
        self.choice = choice
        self.accept()


def local_midnight(days_ago=0):
    day = datetime.date.today() - datetime.timedelta(days=days_ago)
    return datetime.datetime.combine(day, datetime.time.min).astimezone()


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
        self.orders_path = None      # None = the app's settings folder
        self.run_chat = None         # (chat id, name) of the running download
        self.run_new_files = False   # the running download is "download new files"
        self.marks_path = None       # None = the app's settings folder
        self.order_picks = {}        # chat id -> paths picked by hand in the order card
        self.auto_out_dir = None
        self.tray = None
        self._quitting = False
        self._tray_hint_shown = False
        self._page_cache = {}

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
        self.worker.auto_event.connect(self._on_auto_event)
        self.worker.auto_done.connect(self._on_auto_done)
        self.updater.checked.connect(self._on_update_checked)
        self.updater.check_failed.connect(self._on_update_check_failed)

        self.stats_timer = QTimer(self, interval=300)
        self.stats_timer.timeout.connect(self._refresh_stats)

        # Auto-delete: due files are removed every few seconds while the app runs.
        self.cleanup_store = None          # None = the app's settings folder
        self.run_out_dir = None
        self.run_delete_after = None       # seconds, captured when a download starts
        self.cleanup_timer = QTimer(self, interval=15000)
        self.cleanup_timer.timeout.connect(self._cleanup_tick)
        self.cleanup_timer.start()

        if autostart:
            self._setup_tray()
            QTimer.singleShot(0, self._cleanup_tick)
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
        row.addWidget(logo)
        row.addWidget(QLabel(APP_TITLE, objectName="appTitle"))
        row.addWidget(QLabel(f"الإصدار {__version__}", objectName="version"))
        row.addStretch(1)

        self.auto_btn = QPushButton(checkable=True)
        self.auto_btn.setProperty("chip", True)
        self.auto_btn.setToolTip("ينزّل الملفات الجديدة فور وصولها، في مجلد باسم مرسلها")
        self.auto_btn.toggled.connect(self._toggle_auto)
        self.account_avatar = QLabel()
        self.account_label = QLabel("غير مسجّل الدخول")
        self.account_label.setStyleSheet("font-weight: 600;")
        self.account_btn = QToolButton(text="▾", popupMode=QToolButton.InstantPopup)
        self.account_btn.setAutoRaise(True)
        menu = QMenu(self.account_btn)
        self.update_action = menu.addAction("التحقق من التحديثات")
        self.update_action.triggered.connect(lambda: self.check_updates(manual=True))
        self.logout_action = menu.addAction("تسجيل الخروج")
        self.logout_action.triggered.connect(self.logout)
        self.account_btn.setMenu(menu)
        self.login_btn = set_kind(QPushButton("تسجيل الدخول"), "primary")
        self.login_btn.clicked.connect(self.login)
        self.settings_btn = QPushButton("⚙  الإعدادات")
        self.settings_btn.clicked.connect(lambda: self.open_settings())
        for widget in (self.auto_btn, self.account_avatar, self.account_label, self.account_btn,
                       self.login_btn, self.settings_btn):
            row.addWidget(widget)
        self._show_auto_state()
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

        self.cleanup_label = QLabel(objectName="status")
        self.cleanup_label.setWordWrap(True)
        self.delete_now_btn = set_kind(QPushButton("احذفها الآن"), "ghost")
        self.delete_now_btn.clicked.connect(self.delete_scheduled_now)
        cleanup_row = QHBoxLayout()
        cleanup_row.setContentsMargins(6, 0, 6, 0)
        cleanup_row.addWidget(self.cleanup_label, 1)
        cleanup_row.addWidget(self.delete_now_btn)
        self.cleanup_row = QWidget()
        self.cleanup_row.setLayout(cleanup_row)
        self.cleanup_row.hide()

        card.body.addLayout(title_row)
        card.body.addWidget(self.chat_search)
        card.body.addWidget(self.chat_list, 1)
        card.body.addWidget(self.chats_status)
        card.body.addWidget(self.cleanup_row)
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

        # 1. The chat and the two ways to get its files
        head = Card(margins=(18, 16, 18, 14), spacing=12)
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
        self.chat_auto_check = QCheckBox("📥 استقبال تلقائي لهذه المحادثة")
        self.chat_auto_check.toggled.connect(self._on_chat_auto_toggled)
        self.chat_auto_label = QLabel("📥 تُستقبل ملفاتها تلقائياً", objectName="status")
        top.addWidget(self.chat_avatar)
        top.addLayout(names, 1)
        top.addWidget(self.chat_auto_check)
        top.addWidget(self.chat_auto_label)
        head.body.addLayout(top)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.start_btn = set_kind(QPushButton("⬇  تنزيل الملفات الجديدة"), "primary")
        self.start_btn.setDefault(True)
        self.start_btn.setToolTip("ينزّل كل ملف لم يُنزَّل بعد من هذه المحادثة")
        self.start_btn.clicked.connect(lambda: self.start(preview=False))
        self.pick_btn = QPushButton("☑  اختيار ملفات")
        self.pick_btn.setToolTip("يعرض ملفات المحادثة لتختار منها ما تريد فقط")
        self.pick_btn.clicked.connect(self.pick_files)
        actions.addWidget(self.start_btn)
        actions.addWidget(self.pick_btn)
        actions.addStretch(1)
        head.body.addLayout(actions)

        settings_row = QHBoxLayout()
        self.settings_line = QLabel(objectName="status")
        self.settings_line.setWordWrap(True)
        change = set_kind(QPushButton("تغيير"), "ghost")
        change.clicked.connect(lambda: self.open_settings(TAB_FILES))
        settings_row.addWidget(self.settings_line, 1)
        settings_row.addWidget(change, 0, Qt.AlignTop)
        head.body.addLayout(settings_row)
        layout.addWidget(head)

        # 2. Progress of the running download
        self.progress_card = Card(margins=(18, 14, 18, 14), spacing=8)
        progress_top = QHBoxLayout()
        progress_top.addWidget(QLabel("التنزيل", objectName="sectionTitle"))
        progress_top.addStretch(1)
        self.stop_btn = set_kind(QPushButton("■  إيقاف"), "danger")
        self.stop_btn.clicked.connect(self.stop)
        progress_top.addWidget(self.stop_btn)
        self.current_label = QLabel(" ")
        self.current_label.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.stats_label = QLabel(" ", objectName="status")
        self.stats_label.setWordWrap(True)
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
        for widget in (self.current_label, self.progress, self.stats_label):
            self.progress_card.body.addWidget(widget)
        self.progress_card.body.insertLayout(0, progress_top)
        self.progress_card.body.addWidget(self.log_toggle, alignment=Qt.AlignRight)
        self.progress_card.body.addWidget(self.log_view, 1)
        self.progress_card.hide()
        layout.addWidget(self.progress_card)

        # 3. The customer's order: what was received, its price, and what to do with it
        self.order_card = Card(margins=(18, 14, 18, 14), spacing=8)
        order_top = QHBoxLayout()
        self.order_title = QLabel(objectName="sectionTitle")
        self.order_age = QLabel(objectName="status")
        order_top.addWidget(self.order_title)
        order_top.addStretch(1)
        order_top.addWidget(self.order_age)
        range_row = QHBoxLayout()
        range_row.addWidget(QLabel("الملفات المعروضة:"))
        self.order_range = QComboBox()
        for label, value in (("كل الطلب", "all"), ("اليوم", 0), ("آخر يومين", 1),
                             ("آخر 7 أيام", 6), ("اختيار يدوي...", "pick")):
            self.order_range.addItem(label, value)
        self.order_range.activated.connect(self._order_range_chosen)
        range_row.addWidget(self.order_range)
        range_row.addStretch(1)
        self.order_warning = QLabel()
        self.order_warning.setWordWrap(True)
        self.order_warning.setStyleSheet("color: #B54708;")
        self.order_summary = QLabel()
        self.order_summary.setWordWrap(True)
        self.order_pages = QLabel()
        self.order_pages.setWordWrap(True)
        self.order_pages.setStyleSheet("font-weight: 600;")
        order_actions = QHBoxLayout()
        order_actions.setSpacing(8)
        self.print_btn = set_kind(QPushButton("🖨  طباعة الطلب"), "primary")
        self.print_btn.clicked.connect(self.print_order)
        self.merge_btn = QPushButton("🧾  دمج الصور في PDF")
        self.merge_btn.clicked.connect(self.merge_order)
        self.open_btn = set_kind(QPushButton("📂 فتح المجلد"), "ghost")
        self.open_btn.clicked.connect(self.open_order_folder)
        self.deliver_btn = set_kind(QPushButton("✔ تم التسليم"), "ghost")
        self.deliver_btn.setToolTip("ينهي طلب هذا الزبون، مع حذف ملفاته أو إبقائها")
        self.deliver_btn.clicked.connect(self.deliver_order)
        for button in (self.print_btn, self.merge_btn, self.open_btn):
            order_actions.addWidget(button)
        order_actions.addStretch(1)
        order_actions.addWidget(self.deliver_btn)
        self.order_card.body.addLayout(order_top)
        self.order_card.body.addLayout(range_row)
        self.order_card.body.addWidget(self.order_warning)
        self.order_card.body.addWidget(self.order_summary)
        self.order_card.body.addWidget(self.order_pages)
        self.order_card.body.addLayout(order_actions)
        self.order_card.hide()
        layout.addWidget(self.order_card)
        layout.addStretch(1)
        self.chat_page_layout = layout
        return page

    def _toggle_log(self, shown):
        self.log_view.setVisible(shown)
        self.log_toggle.setText("إخفاء السجل ▴" if shown else "عرض السجل ▾")
        # Let the progress card take the free space only while the log is open.
        self.chat_page_layout.setStretch(1, 1 if shown else 0)
        self.chat_page_layout.setStretch(3, 0 if shown else 1)

    # -- preferences --------------------------------------------------------

    def _refresh_summary(self):
        self.settings_line.setText("يُنزَّل: " + short_summary(self.config))

    # -- state --------------------------------------------------------------

    def _on_chat_selected(self):
        chat = self._current_chat()
        if chat is not None:
            item = self.chat_list.currentItem()
            title, kind = item.data(CHAT_TITLE_ROLE), item.data(CHAT_KIND_ROLE)
            self.chat_name.setText(title)
            self.chat_kind.setText(chat_subtitle(kind, item.data(CHAT_MARK_ROLE)))
            self.chat_avatar.setPixmap(avatar_pixmap(title, chat[0], 56, saved=kind == "saved"))
            self.progress_card.setVisible(self.run_chat is not None and self.run_chat[0] == chat[0])
            self._show_chat_auto()
            self._refresh_order()
        self._update_state()

    def _update_state(self):
        busy = self.job is not None
        downloading = self.job in ("download", "preview", "scan")
        has_chat = self._current_chat() is not None
        self.login_btn.setVisible(False)     # the welcome page has the login button
        self.login_btn.setEnabled(not busy)
        self.logout_action.setVisible(self.logged_in)
        self.logout_action.setEnabled(not busy)
        self.account_avatar.setVisible(self.logged_in)
        self.account_btn.setVisible(self.logged_in)
        self.auto_btn.setVisible(self.logged_in)
        self.settings_btn.setEnabled(not downloading)
        self.refresh_btn.setEnabled(self.logged_in and self.chats_future is None
                                    and self.job not in ("login", "logout"))
        self.chat_list.setEnabled(self.logged_in and not downloading)
        ready = self.logged_in and has_chat and not busy
        self.start_btn.setEnabled(ready)
        self.pick_btn.setEnabled(ready)
        self.stop_btn.setEnabled(downloading)
        for button in (self.print_btn, self.merge_btn, self.deliver_btn):
            button.setEnabled(not downloading)

        if self.logged_in and (has_chat or downloading):
            self.pages.setCurrentIndex(1)
        else:
            self.pages.setCurrentIndex(0)
            if self.logged_in:
                self.empty_title.setText("اختر محادثة")
                self.empty_text.setText("اختر زبوناً من القائمة لتنزيل ملفاته، "
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
            self._apply_auto_settings()
            self._show_chat_auto()
            self._refresh_order()
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
            self.update_action.setEnabled(False)
        self.updater.check(manual)

    def _on_update_checked(self, release, manual):
        self.update_action.setEnabled(True)
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
        self.update_action.setEnabled(True)
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
        badges = self._orders().counts()
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
            item.setData(CHAT_BADGE_ROLE, badges.get(chat_id, 0))
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

    def _begin(self, job, message, coro_factory, preview, chat=None):
        p = prefs(self.config)
        self.run_chat = chat
        self.progress_card.show()
        self.run_out_dir = Path(p["out_dir"])
        self.run_delete_after = p["delete_after"] * 60 if p["auto_delete"] else None
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
        elif preview:
            message = f"بدء معاينة الملفات من: {name}"
        else:
            last = self._marks().get(chat_id)
            if last:
                # "New" means what arrived after the last download from this chat.
                options.min_id = last
                message = f"تنزيل الملفات التي وصلت من {name} بعد آخر تنزيل"
            else:
                dialog = FirstDownloadDialog(name, self)
                dialog.exec()
                choice = dialog.choice
                _dispose(dialog)
                if choice is None:
                    return
                if choice == "pick":
                    self.pick_files()
                    return
                if choice in ("today", "week"):
                    start = local_midnight(0 if choice == "today" else 6)
                    options.since = max(options.since, start) if options.since else start
                message = {"today": "تنزيل ملفات اليوم من", "week": "تنزيل ملفات آخر 7 أيام من",
                           "all": "تنزيل كل الملفات من"}[choice] + f": {name}"
        self.run_new_files = not preview and not ids
        self._begin("preview" if preview else "download", message,
                    lambda: self.worker.download(chat_id, options, name), preview, chat)

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
                    lambda: self.worker.scan(chat[0], options, chat[1]), True, chat)

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
            if self.run_chat is not None and self.run_out_dir is not None:
                self._file_saved(self.run_chat[0], self.run_chat[1],
                                 self.run_out_dir / data["path"], data, self.run_delete_after)
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
            self._apply_auto_settings()
            if saved and age is not None and age < CHAT_REFRESH_AFTER:
                minutes = max(1, round(age / 60))
                self.chats_status.setText(f"القائمة محدّثة قبل {minutes} دقيقة. "
                                          "اضغط «تحديث القائمة» لتحديثها الآن.")
            else:
                self.load_chats()
        elif name in ("download", "preview"):
            if (name == "download" and self.run_new_files and not result.failed
                    and self.run_chat is not None):
                # Next "download new files" starts after the newest message seen now.
                self._marks().advance(self.run_chat[0], result.last_msg_id)
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

    # -- orders ---------------------------------------------------------------------

    def _orders(self):
        return OrderBook(self.orders_path or app_dir() / "orders.json")

    def _marks(self):
        return ChatMarks(self.marks_path or app_dir() / "marks.json")

    def _file_saved(self, chat_id, chat_name, path, data, delete_after):
        sent = data.get("date")
        self._orders().add(chat_id, chat_name, path, data.get("kind"), data.get("msg_id"),
                           sent.timestamp() if sent else None)
        if delete_after:
            cleanup.schedule(self._cleanup_store(), [path], delete_after,
                             root=Path(prefs(self.config)["out_dir"]))
            self._show_cleanup_status()
        self._refresh_badges()
        chat = self._current_chat()
        if chat is not None and chat[0] == chat_id:
            self._refresh_order()

    def _refresh_badges(self):
        counts = self._orders().counts()
        for i in range(self.chat_list.count()):
            item = self.chat_list.item(i)
            chat_id = item.data(CHAT_ID_ROLE)
            if chat_id is not None:
                item.setData(CHAT_BADGE_ROLE, counts.get(chat_id, 0))

    def _order_entries(self):
        chat = self._current_chat()
        return self._orders().entries(chat[0]) if chat else []

    def _shown_entries(self):
        """The order's files the card works on, after its date filter."""
        entries = self._order_entries()
        value = self.order_range.currentData()
        if value == "pick":
            chat = self._current_chat()
            picked = self.order_picks.get(chat[0], set()) if chat else set()
            return [e for e in entries if str(e["path"]) in picked]
        if isinstance(value, int):
            return within_days(entries, value)
        return entries

    def _order_files(self):
        return [e["path"] for e in self._shown_entries()]

    def _default_order_range(self, entries):
        """Old files in the order? Then show the last week unless told otherwise."""
        week_ago = clock.time() - 7 * 86400
        index = self.order_range.findData(6 if any(e["sent"] < week_ago for e in entries)
                                          else "all")
        self.order_range.setCurrentIndex(index)

    def _order_range_chosen(self, _index):
        if self.order_range.currentData() == "pick":
            self._pick_order_files()
        self._refresh_order(keep_range=True)

    def _pick_order_files(self):
        chat = self._current_chat()
        entries = self._order_entries()
        if not chat or not entries:
            return
        items = []
        for index, entry in enumerate(entries):
            try:
                size = entry["path"].stat().st_size
            except OSError:
                size = None
            items.append({"msg_id": index, "path": entry["path"], "size": size,
                          "kind": entry.get("kind") or "document",
                          "date": datetime.datetime.fromtimestamp(entry["sent"]).astimezone(),
                          "file_name": entry["path"].name, "sender": entry["path"].parent.name})
        dialog = SelectFilesDialog(items, 0, self)
        dialog.setWindowTitle("اختر ملفات الطلب")
        dialog.ok_btn.setText("اعتماد المحدد")
        accepted = dialog.exec() == QDialog.Accepted
        ids = dialog.selected_ids() if accepted else []
        _dispose(dialog)
        if ids:
            self.order_picks[chat[0]] = {str(entries[i]["path"]) for i in ids}
        elif not self.order_picks.get(chat[0]):
            self.order_range.setCurrentIndex(self.order_range.findData("all"))

    def _refresh_order(self, keep_range=False):
        chat = self._current_chat()
        entries = self._order_entries()
        self.order_card.setVisible(bool(entries))
        if not entries:
            return
        if not keep_range:
            self._default_order_range(entries)
        files = self._order_files()
        oldest = min(e["sent"] for e in entries)
        hidden = len(entries) - len(files)
        if oldest < clock.time() - 7 * 86400:
            when = datetime.datetime.fromtimestamp(oldest).strftime("%Y/%m/%d")
            text = f"⚠ في هذا الطلب ملفات قديمة (أقدمها أُرسل {when})."
            if hidden:
                text += f" {hidden} ملف غير معروض ولن يُطبع."
            self.order_warning.setText(text)
            self.order_warning.show()
        else:
            self.order_warning.hide()
        p = prefs(self.config)
        result = summarize(files, p["price"], self._page_cache)
        self.order_title.setText(f"🧾 طلب {chat[1]}")
        last = self._orders().last_at(chat[0])
        if last:
            minutes = int((clock.time() - last) // 60)
            self.order_age.setText("آخر ملف: الآن" if minutes < 1 else f"آخر ملف قبل {minutes} دقيقة")
        shown = f"{result.files} ملف" if not hidden else f"{result.files} من {len(entries)} ملف"
        self.order_summary.setText(f"{shown}: {result.describe()}" if files else
                                   "لا توجد ملفات في هذه الفترة. اختر «كل الطلب» لرؤيتها.")
        pages = f"المجموع: {result.pages} صفحة"
        if p["price"]:
            pages += f" × {format_money(p['price'])} = {format_money(result.price, p['currency'])}"
        if result.unknown:
            pages += f"  (+ {len(result.unknown)} ملف لا يُعرف عدد صفحاته)"
        self.order_pages.setText(pages)
        busy = self.job in ("download", "preview", "scan")
        self.merge_btn.setEnabled(any(group_of(f) == "images" for f in files) and not busy)
        self.print_btn.setEnabled(bool(files) and not busy)
        self.deliver_btn.setEnabled(bool(files) and not busy)

    def _order_folder(self, files):
        try:
            return Path(os.path.commonpath([str(f.parent) for f in files]))
        except ValueError:            # files on different drives
            return files[0].parent

    def open_order_folder(self):
        files = self._order_files()
        folder = self._order_folder(files) if files else Path(prefs(self.config)["out_dir"])
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def print_order(self):
        entries = self._shown_entries()
        files = [e["path"] for e in entries]
        if not files:
            return
        result = summarize(files, 0, self._page_cache)
        old = [e for e in entries if e["sent"] < clock.time() - 7 * 86400]
        if result.pages >= 100 or old:
            text = f"سيتم طباعة {len(files)} ملف ({result.pages} صفحة)."
            if old:
                text += f"\nمنها {len(old)} ملف أُرسل قبل أكثر من أسبوع."
            text += "\n\nهل تريد المتابعة؟ (يمكنك تحديد الملفات من «الملفات المعروضة»)"
            if QMessageBox.question(self, "تأكيد الطباعة", text) != QMessageBox.Yes:
                return
        printer = QPrinter(QPrinter.HighResolution)
        dialog = QPrintDialog(printer, self)
        dialog.setWindowTitle("طباعة الطلب")
        accepted = dialog.exec() == QDialog.Accepted
        _dispose(dialog)
        if not accepted:
            return
        progress = QProgressDialog("جارٍ إرسال الطلب إلى الطابعة...", "إلغاء", 0, len(files), self)
        progress.setWindowTitle("الطباعة")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)

        def step(done, total):
            progress.setValue(done)
            QApplication.processEvents()
        result = printing.print_files(printer, files, step, progress.wasCanceled)
        progress.close()
        _dispose(progress)
        lines = [f"أُرسلت {result.pages} صفحة إلى الطابعة."]
        if result.sent:
            lines.append(f"و{len(result.sent)} ملف (وورد وغيره) طُبع ببرنامجه على الجهاز.")
        for path, reason in result.failed:
            lines.append(f"✖ {Path(path).name}: {reason}")
        self.log("🖨 " + " ".join(lines))
        QMessageBox.information(self, "الطباعة", "\n".join(lines))

    def merge_order(self):
        chat = self._current_chat()
        images = [f for f in self._order_files() if group_of(f) == "images"]
        if not chat or not images:
            return
        folder = self._order_folder(images)
        out = folder / f"{chat[1]} - {len(images)} صورة.pdf"
        number = 2
        while out.exists():
            out = folder / f"{chat[1]} - {len(images)} صورة ({number}).pdf"
            number += 1
        progress = QProgressDialog("جارٍ دمج الصور في ملف PDF...", None, 0, len(images), self)
        progress.setWindowTitle("دمج الصور")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)

        def step(done, total):
            progress.setValue(done)
            QApplication.processEvents()
        try:
            result = merge_images(images, out, decode=qimage_pixels, progress=step)
        except OSError as e:
            progress.close()
            _dispose(progress)
            QMessageBox.warning(self, "دمج الصور", f"تعذّر إنشاء الملف: {e}")
            return
        progress.close()
        _dispose(progress)
        if not result.pages:
            QMessageBox.warning(self, "دمج الصور", "لا توجد صور يمكن دمجها.")
            return
        p = prefs(self.config)
        if p["auto_delete"]:
            cleanup.schedule(self._cleanup_store(), [out], p["delete_after"] * 60,
                             root=Path(p["out_dir"]))
            self._show_cleanup_status()
        text = f"تم إنشاء ملف PDF فيه {result.pages} صورة بالترتيب:\n{out.name}"
        if result.skipped:
            text += f"\n\nلم تُضَف {len(result.skipped)} صورة لأن صيغتها غير مدعومة."
        self.log(f"🧾 {out.name} ({result.pages} صفحة)")
        box = QMessageBox(QMessageBox.Information, "دمج الصور", text, parent=self)
        open_file = box.addButton("فتح الملف", QMessageBox.AcceptRole)
        open_folder = box.addButton("فتح المجلد", QMessageBox.ActionRole)
        box.addButton("إغلاق", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        _dispose(box)
        if clicked is open_file:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(out)))
        elif clicked is open_folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def deliver_order(self):
        chat = self._current_chat()
        files = self._order_files()
        if not chat or not files:
            return
        box = QMessageBox(QMessageBox.Question, "تم التسليم",
                          f"إنهاء طلب {chat[1]} ({len(files)} ملف)؟", parent=self)
        delete = box.addButton("إنهاء وحذف الملفات", QMessageBox.DestructiveRole)
        keep = box.addButton("إنهاء فقط (إبقاء الملفات)", QMessageBox.AcceptRole)
        box.addButton("إلغاء", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        _dispose(box)
        if clicked not in (delete, keep):
            return
        if clicked is delete:
            root = prefs(self.config)["out_dir"]
            for path in files:
                try:
                    os.remove(path)
                    cleanup._prune_empty_dirs(path, root)
                except OSError:
                    pass
            self.log(f"🗑 حُذفت ملفات طلب {chat[1]} ({len(files)} ملف).")
        self._orders().remove(chat[0], files)
        self.order_picks.pop(chat[0], None)
        self._refresh_badges()
        self._refresh_order()

    # -- auto-receive -------------------------------------------------------------------

    def _show_auto_state(self):
        on = self.auto_btn.isChecked()
        self.auto_btn.setText("📥 الاستقبال التلقائي: يعمل" if on else "📥 الاستقبال التلقائي: متوقف")

    def _toggle_auto(self, on):
        self.config["auto_receive"] = on
        try:
            self.config.save()
        except OSError:
            pass
        self._show_auto_state()
        self._apply_auto_settings()
        self._show_chat_auto()

    def _apply_auto_settings(self):
        """Tell the worker what to receive automatically (or to stop)."""
        p = prefs(self.config)
        on = p["auto_receive"] and self.logged_in
        self.auto_btn.blockSignals(True)
        self.auto_btn.setChecked(p["auto_receive"])
        self.auto_btn.blockSignals(False)
        self._show_auto_state()
        if not on:
            self.worker.configure_auto(False)
            return
        try:
            options = build_options(self.config)
        except ValueError as e:
            QMessageBox.warning(self, APP_TITLE, f"لا يمكن تشغيل الاستقبال التلقائي: {e}")
            self.auto_btn.setChecked(False)
            return
        self.auto_out_dir = options.out_dir
        self.worker.configure_auto(True, options, p["auto_scope"], p["auto_chats"])

    def _show_chat_auto(self):
        chat = self._current_chat()
        p = prefs(self.config)
        item = self.chat_list.currentItem()
        is_person = item is not None and item.data(CHAT_KIND_ROLE) == "user"
        selected_mode = p["auto_scope"] == "selected"
        self.chat_auto_check.setVisible(chat is not None and selected_mode)
        self.chat_auto_label.setVisible(chat is not None and not selected_mode and is_person
                                        and p["auto_receive"])
        if chat is not None:
            self.chat_auto_check.blockSignals(True)
            self.chat_auto_check.setChecked(chat[0] in p["auto_chats"])
            self.chat_auto_check.blockSignals(False)

    def _on_chat_auto_toggled(self, on):
        chat = self._current_chat()
        if chat is None:
            return
        chats = set(prefs(self.config)["auto_chats"])
        (chats.add if on else chats.discard)(chat[0])
        self.config["auto_chats"] = sorted(chats)
        try:
            self.config.save()
        except OSError:
            pass
        self._apply_auto_settings()

    def _on_auto_event(self, name, data):
        if name == "saved" and self.auto_out_dir is not None:
            self._marks().advance(data["chat_id"], data.get("msg_id"))
            p = prefs(self.config)
            delete_after = p["delete_after"] * 60 if p["auto_delete"] else None
            self._file_saved(data["chat_id"], data["chat_name"], self.auto_out_dir / data["path"],
                             data, delete_after)
        elif name == "failed":
            self.log(f"✖ استقبال تلقائي من {data['chat_name']}: {friendly_error(data['error'])}")

    def _on_auto_done(self, chat_id, chat_name, count):
        text = f"📥 وصل {count} ملف من {chat_name}"
        self.log(text)
        self.chats_status.setText(text)
        self._notify("ملفات جديدة", f"وصل {count} ملف من {chat_name}")

    def _notify(self, title, text):
        if prefs(self.config)["auto_sound"]:
            QApplication.beep()
        if self.tray is not None:
            self.tray.showMessage(title, text, QSystemTrayIcon.Information, 8000)

    def _setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self.windowIcon() or QIcon(), self)
        self.tray.setToolTip(APP_TITLE)
        menu = QMenu(self)
        menu.addAction("فتح البرنامج", self._show_from_tray)
        menu.addAction("الاستقبال التلقائي (تشغيل/إيقاف)", self.auto_btn.toggle)
        menu.addSeparator()
        menu.addAction("خروج", self.quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self._show_from_tray()
                                    if reason == QSystemTrayIcon.Trigger else None)
        self.tray.messageClicked.connect(self._show_from_tray)
        self.tray.show()

    def _show_from_tray(self):
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.raise_()
        self.activateWindow()

    def quit_app(self):
        self._quitting = True
        self.close()

    # -- auto-delete ------------------------------------------------------------

    def _cleanup_store(self):
        return self.cleanup_store or cleanup.store_path()

    def _cleanup_tick(self):
        store = self._cleanup_store()
        cleanup.app_heartbeat(store)
        deleted = cleanup.clean_due(store)
        if deleted:
            self.log(f"🗑 حُذف {deleted} ملف تلقائياً بعد انتهاء المدة المحددة.")
        self._show_cleanup_status()

    def _show_cleanup_status(self):
        entries = cleanup.pending(self._cleanup_store())
        if not entries:
            self.cleanup_row.hide()
            return
        minutes = max(1, -(-int(entries[0]["at"] - clock.time()) // 60))   # round up
        self.cleanup_label.setText(
            f"🕒 حذف تلقائي: {len(entries)} ملف سيُحذف، أقربها بعد {minutes} دقيقة.")
        self.cleanup_row.show()

    def delete_scheduled_now(self):
        count = len(cleanup.pending(self._cleanup_store()))
        answer = QMessageBox.question(
            self, "حذف الملفات",
            f"هل تريد حذف {count} ملف منزّل الآن نهائياً بدل انتظار موعدها؟")
        if answer == QMessageBox.Yes:
            deleted = cleanup.clean_due(self._cleanup_store(), now=float("inf"))
            self.log(f"🗑 حُذف {deleted} ملف.")
            self._show_cleanup_status()

    def closeEvent(self, event):
        p = prefs(self.config)
        if (not self._quitting and self.tray is not None and p["keep_running"]
                and self.auto_btn.isChecked()):
            # Keep receiving files in the background, next to the clock.
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.showMessage(APP_TITLE, "البرنامج ما زال يستقبل الملفات في الخلفية. "
                                      "للخروج نهائياً: اضغط على أيقونته بجانب الساعة ← خروج.",
                                      QSystemTrayIcon.Information, 8000)
            return
        if self.job in ("download", "preview", "scan"):
            answer = QMessageBox.question(self, APP_TITLE,
                                          "التنزيل ما زال يعمل. هل تريد إيقافه والخروج؟")
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.stop()
        self.cleanup_timer.stop()
        store = self._cleanup_store()
        cleanup.app_closed(store)
        if cleanup.pending(store):
            # Files are still waiting to be deleted: finish the job in the background.
            try:
                updater.launch_detached(cleanup.helper_command())
            except OSError:
                pass
        if self.tray is not None:
            self.tray.hide()
        self.worker.shutdown()
        event.accept()
