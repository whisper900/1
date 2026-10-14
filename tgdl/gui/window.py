"""Main window of the desktop app (Arabic, right-to-left)."""

import asyncio
import io
import re
import sys
import time as clock
from datetime import datetime, time, timedelta
from pathlib import Path

from PySide6.QtCore import QDate, QStandardPaths, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QIntValidator, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QProgressDialog, QPushButton, QSpinBox, QSplitter, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget)
from telethon import errors as tg_errors

from .. import __version__
from ..downloader import Options
from ..filters import MessageFilter, normalize_exts
from . import updater
from .config import (Config, chat_cache_age, clear_chat_cache, load_chat_cache,
                     save_chat_cache, session_path)
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

# kind, label, checked by default
TYPE_CHOICES = [
    ("photo", "صور", True),
    ("video", "فيديو", True),
    ("document", "ملفات ومستندات", True),
    ("audio", "موسيقى وملفات صوتية", True),
    ("voice", "رسائل صوتية", True),
    ("video_note", "فيديو دائري", True),
    ("gif", "صور متحركة (GIF)", True),
    ("sticker", "ملصقات", False),
]
# Sub-folder names used when "one folder per file type" is ticked.
TYPE_FOLDERS = {
    "photo": "صور", "video": "فيديو", "document": "ملفات", "audio": "صوتيات",
    "voice": "رسائل صوتية", "video_note": "فيديو دائري", "gif": "GIF",
    "sticker": "ملصقات",
}
CHAT_KINDS = {"user": "شخص", "group": "مجموعة", "channel": "قناة", "saved": "محفوظة"}

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


def default_out_dir():
    desktop = QStandardPaths.writableLocation(QStandardPaths.DesktopLocation)
    return str(Path(desktop or Path.home()) / "تنزيلات تليكرام")


def size_ar(num):
    """File size with Arabic units (Latin units get scrambled in RTL text)."""
    if num is None:
        return "حجم غير معروف"
    for unit in ("بايت", "كيلوبايت", "ميغابايت"):
        if num < 1024:
            return f"{num:.0f} {unit}" if unit == "بايت" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.2f} غيغابايت"


def split_list(text):
    """Split on commas (Latin or Arabic)."""
    return [part.strip() for part in re.split(r"[,،]", text) if part.strip()]


class SettingsDialog(QDialog):
    """Asks for the API ID / API Hash from my.telegram.org."""

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("الإعدادات")
        self.setMinimumWidth(520)

        help_label = QLabel(
            "<b>يحتاج البرنامج إلى رقمين مجانيين من تليكرام (مرة واحدة فقط):</b>"
            "<ol>"
            "<li>افتح <a href='https://my.telegram.org'>my.telegram.org</a> "
            "وسجّل الدخول برقم هاتفك (يصلك الكود في تطبيق تليكرام).</li>"
            "<li>اضغط <b>API development tools</b>.</li>"
            "<li>املأ App title و Short name بأي اسم، ثم اضغط Create application.</li>"
            "<li>انسخ <b>App api_id</b> و <b>App api_hash</b> والصقهما هنا.</li>"
            "</ol>"
            "لا تشارك هذين الرقمين مع أحد.")
        help_label.setWordWrap(True)
        help_label.setOpenExternalLinks(True)
        help_label.setTextFormat(Qt.RichText)

        self.api_id = QLineEdit(str(config.get("api_id", "")))
        self.api_id.setValidator(QIntValidator(1, 2_000_000_000, self))
        self.api_id.setPlaceholderText("مثال: 1234567")
        self.api_id.setLayoutDirection(Qt.LeftToRight)
        self.api_hash = QLineEdit(config.get("api_hash", ""))
        self.api_hash.setPlaceholderText("32 حرفاً ورقماً")
        self.api_hash.setLayoutDirection(Qt.LeftToRight)

        form = QFormLayout()
        form.addRow("API ID:", self.api_id)
        form.addRow("API Hash:", self.api_hash)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("حفظ")
        buttons.button(QDialogButtonBox.Cancel).setText("إلغاء")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(help_label)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _save(self):
        api_id = self.api_id.text().strip()
        api_hash = self.api_hash.text().strip()
        if not api_id.isdigit():
            QMessageBox.warning(self, "الإعدادات", "API ID يجب أن يكون أرقاماً فقط.")
            return
        if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
            QMessageBox.warning(self, "الإعدادات",
                                "API Hash يجب أن يكون 32 خانة (أرقام وحروف a-f).")
            return
        self.config["api_id"] = int(api_id)
        self.config["api_hash"] = api_hash
        self.config.save()
        self.accept()


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
        ok = buttons.addButton("تأكيد", QDialogButtonBox.AcceptRole)
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


KIND_LABELS = {
    "photo": "صورة", "video": "فيديو", "document": "ملف", "audio": "صوت",
    "voice": "رسالة صوتية", "video_note": "فيديو دائري", "gif": "GIF", "sticker": "ملصق",
}
SORT_ROLE = Qt.UserRole
ID_ROLE = Qt.UserRole + 1
SEARCH_ROLE = Qt.UserRole + 2


class _FileRow(QTreeWidgetItem):
    """Sorts by the raw value (bytes, timestamp) rather than the shown text."""

    def __lt__(self, other):
        column = self.treeWidget().sortColumn() if self.treeWidget() else 0
        mine, theirs = self.data(column, SORT_ROLE), other.data(column, SORT_ROLE)
        if mine is not None and theirs is not None:
            return mine < theirs
        return self.text(column) < other.text(column)


class SelectFilesDialog(QDialog):
    """Lists the files a download would fetch; the user ticks the ones wanted."""

    COLUMNS = ["الملف", "المرسل", "النوع", "الحجم", "التاريخ"]

    def __init__(self, items, already=0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("اختر الملفات المطلوب تنزيلها")
        self.resize(860, 580)

        intro = f"وُجد {len(items)} ملف جديد في هذه المحادثة حسب اختياراتك. ضع علامة ✔ على ما تريد تنزيله."
        if already:
            intro += f"\n({already} ملف منزّل مسبقاً لا يظهر هنا.)"
        intro_label = QLabel(intro)
        intro_label.setWordWrap(True)

        self.search = QLineEdit(placeholderText="ابحث باسم الملف أو المرسل أو النوع...")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(self.COLUMNS)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        for i, item in enumerate(items):
            name = item.get("file_name") or Path(item["path"]).name
            kind = KIND_LABELS.get(item.get("kind"), item.get("kind") or "")
            date = item.get("date")
            row = _FileRow([
                f"\u2068{name}\u2069",
                f"\u2068{item.get('sender') or ''}\u2069",
                kind,
                size_ar(item.get("size")),
                f"{date:%Y/%m/%d %H:%M}" if date else "",
            ])
            row.setFlags(row.flags() | Qt.ItemIsUserCheckable)
            row.setCheckState(0, Qt.Unchecked)
            row.setData(0, ID_ROLE, item["msg_id"])
            row.setData(0, SEARCH_ROLE, f"{name} {item.get('sender') or ''} {kind}".casefold())
            row.setData(0, SORT_ROLE, name.casefold())
            row.setData(1, SORT_ROLE, (item.get("sender") or "").casefold())
            row.setData(2, SORT_ROLE, kind)
            row.setData(3, SORT_ROLE, item.get("size") or 0)
            row.setData(4, SORT_ROLE, date.timestamp() if date else 0.0)
            self.tree.addTopLevelItem(row)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(4, Qt.AscendingOrder)       # oldest first, like the chat
        self.tree.header().resizeSection(0, 330)
        self.tree.itemChanged.connect(self._update_summary)
        self.tree.itemDoubleClicked.connect(self._toggle)

        select_all = QPushButton("تحديد الكل")
        select_all.clicked.connect(lambda: self._set_visible(Qt.Checked))
        select_none = QPushButton("إلغاء التحديد")
        select_none.clicked.connect(lambda: self._set_visible(Qt.Unchecked))
        self.summary = QLabel()
        tools = QHBoxLayout()
        tools.addWidget(select_all)
        tools.addWidget(select_none)
        tools.addStretch(1)
        tools.addWidget(self.summary)

        buttons = QDialogButtonBox()
        self.ok_btn = buttons.addButton("تنزيل المحدد", QDialogButtonBox.AcceptRole)
        self.ok_btn.setDefault(True)
        buttons.addButton("إلغاء", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro_label)
        layout.addWidget(self.search)
        layout.addWidget(self.tree, 1)
        layout.addLayout(tools)
        layout.addWidget(buttons)
        self._update_summary()

    def _rows(self):
        return [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]

    def _filter(self, text):
        needle = text.strip().casefold()
        for row in self._rows():
            row.setHidden(bool(needle) and needle not in row.data(0, SEARCH_ROLE))

    def _set_visible(self, state):
        self.tree.blockSignals(True)         # one summary update, not one per row
        for row in self._rows():
            if not row.isHidden():
                row.setCheckState(0, state)
        self.tree.blockSignals(False)
        self._update_summary()

    def _toggle(self, row, _column):
        checked = row.checkState(0) == Qt.Checked
        row.setCheckState(0, Qt.Unchecked if checked else Qt.Checked)

    def _update_summary(self, *_):
        count = total = 0
        for row in self._rows():
            if row.checkState(0) == Qt.Checked:
                count += 1
                total += row.data(3, SORT_ROLE) or 0
        self.summary.setText(f"المحدد: {count} ملف ({size_ar(total)})")
        self.ok_btn.setText(f"تنزيل المحدد ({count})")
        self.ok_btn.setEnabled(count > 0)

    def selected_ids(self):
        return sorted(row.data(0, ID_ROLE) for row in self._rows()
                      if row.checkState(0) == Qt.Checked)


class LoginMethodDialog(QDialog):
    """QR code (no SMS needed) or phone number + code."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("تسجيل الدخول")
        self.setMinimumWidth(440)
        self.method = None
        text = QLabel("اختر طريقة تسجيل الدخول إلى حسابك في تليكرام:")
        qr_btn = QPushButton("بمسح رمز QR من هاتفك (موصى به، لا يحتاج كوداً)")
        qr_btn.setDefault(True)
        qr_btn.setStyleSheet("font-weight: bold; padding: 10px;")
        qr_btn.clicked.connect(lambda: self._choose("qr"))
        phone_btn = QPushButton("برقم الهاتف وكود الدخول")
        phone_btn.setStyleSheet("padding: 8px;")
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
        self.resize(1000, 720)
        self._build_ui()
        self._load_preferences()
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
        # Account bar
        self.account_label = QLabel("غير مسجّل الدخول")
        self.account_label.setStyleSheet("font-weight: bold;")
        self.login_btn = QPushButton("تسجيل الدخول")
        self.login_btn.clicked.connect(self.login)
        self.logout_btn = QPushButton("تسجيل الخروج")
        self.logout_btn.clicked.connect(self.logout)
        self.update_btn = QPushButton("التحقق من التحديثات")
        self.update_btn.clicked.connect(lambda: self.check_updates(manual=True))
        self.settings_btn = QPushButton("الإعدادات")
        self.settings_btn.clicked.connect(self.open_settings)
        account_bar = QHBoxLayout()
        account_bar.addWidget(self.account_label, 1)
        account_bar.addWidget(self.login_btn)
        account_bar.addWidget(self.logout_btn)
        account_bar.addWidget(self.update_btn)
        account_bar.addWidget(self.settings_btn)

        # 1. Chats
        self.chat_search = QLineEdit(placeholderText="ابحث عن محادثة...")
        self.chat_search.setClearButtonEnabled(True)
        self.chat_search.textChanged.connect(self._filter_chats)
        self.chat_list = QListWidget()
        self.chat_list.currentItemChanged.connect(lambda *_: self._update_state())
        self.refresh_btn = QPushButton("تحديث القائمة")
        self.refresh_btn.clicked.connect(self.load_chats)
        chats_box = QGroupBox("١. اختر المحادثة")
        self.chats_box = chats_box
        self.chats_status = QLabel()
        self.chats_status.setStyleSheet("color: gray;")
        self.chats_status.setWordWrap(True)
        chats_layout = QVBoxLayout(chats_box)
        chats_layout.addWidget(self.chat_search)
        chats_layout.addWidget(self.chat_list, 1)
        chats_layout.addWidget(self.chats_status)
        chats_layout.addWidget(self.refresh_btn)

        # 2. What to download
        self.type_checks = {}
        types_grid = QGridLayout()
        for i, (kind, label, checked) in enumerate(TYPE_CHOICES):
            box = QCheckBox(label, checked=checked)
            self.type_checks[kind] = box
            types_grid.addWidget(box, i // 2, i % 2)

        self.ext_edit = QLineEdit(placeholderText="اتركه فارغاً للكل، أو مثلاً: pdf, docx, zip")
        self.senders_edit = QLineEdit(
            placeholderText="اتركه فارغاً للكل، أو اكتب الأسماء مفصولة بفاصلة")
        self.since_check = QCheckBox("من تاريخ")
        self.since_date = QDateEdit(QDate.currentDate().addMonths(-1), calendarPopup=True)
        self.until_check = QCheckBox("إلى تاريخ")
        self.until_date = QDateEdit(QDate.currentDate(), calendarPopup=True)
        for date in (self.since_date, self.until_date):
            date.setDisplayFormat("yyyy/MM/dd")
        self.since_check.toggled.connect(self.since_date.setEnabled)
        self.until_check.toggled.connect(self.until_date.setEnabled)
        self.since_date.setEnabled(False)
        self.until_date.setEnabled(False)
        dates = QHBoxLayout()
        dates.addWidget(self.since_check)
        dates.addWidget(self.since_date)
        dates.addSpacing(16)
        dates.addWidget(self.until_check)
        dates.addWidget(self.until_date)
        dates.addStretch(1)

        self.max_check = QCheckBox("تجاهل الملفات الأكبر من")
        self.max_size = QSpinBox(minimum=1, maximum=4000, value=200, suffix=" ميغابايت")
        self.max_size.setEnabled(False)
        self.max_check.toggled.connect(self.max_size.setEnabled)
        max_row = QHBoxLayout()
        max_row.addWidget(self.max_check)
        max_row.addWidget(self.max_size)
        max_row.addStretch(1)

        what_box = QGroupBox("٢. ماذا تريد أن تنزّل؟")
        what = QFormLayout(what_box)
        what.addRow("أنواع الملفات:", types_grid)
        what.addRow("امتدادات محددة:", self.ext_edit)
        what.addRow("من مرسلين محددين:", self.senders_edit)
        what.addRow("الفترة:", dates)
        what.addRow("الحجم:", max_row)

        # 3. Where
        self.out_edit = QLineEdit()
        self.out_edit.setLayoutDirection(Qt.LeftToRight)  # paths read left to right
        self.out_edit.textChanged.connect(self._update_example)
        browse = QPushButton("اختيار...")
        browse.clicked.connect(self._browse_out_dir)
        out_row = QHBoxLayout()
        out_row.addWidget(self.out_edit, 1)
        out_row.addWidget(browse)
        self.chat_folder_check = QCheckBox("مجلد باسم المحادثة", checked=True)
        self.type_folder_check = QCheckBox("مجلد لكل نوع ملف داخل مجلد المرسل")
        self.naming_combo = QComboBox()
        self.naming_combo.addItem("الاسم الأصلي للملف", "original")
        self.naming_combo.addItem("أرقام متسلسلة: 1، 2، 3 … (ترقيم منفصل لكل نوع)", "numbers")
        self.naming_combo.setToolTip(
            "الصور تُرقَّم 1، 2، 3… وملفات الوورد 1، 2، 3… وملفات PDF 1، 2، 3… وهكذا\n"
            "لكل نوع داخل مجلد كل مرسل، حسب تاريخ الإرسال (الأقدم = 1).\n"
            "يتغير اسم الملف فقط، ولا يُعدَّل محتواه إطلاقاً.")
        self.naming_combo.currentIndexChanged.connect(self._update_example)
        naming_row = QHBoxLayout()
        naming_row.addWidget(QLabel("تسمية الملفات:"))
        naming_row.addWidget(self.naming_combo, 1)
        for box in (self.chat_folder_check, self.type_folder_check):
            box.toggled.connect(self._update_example)
        self.example_label = QLabel()
        self.example_label.setStyleSheet("color: gray;")
        self.example_label.setWordWrap(True)
        where_box = QGroupBox("٣. أين تُحفظ الملفات؟")
        where = QVBoxLayout(where_box)
        where.addLayout(out_row)
        where.addWidget(self.chat_folder_check)
        where.addWidget(self.type_folder_check)
        where.addLayout(naming_row)
        where.addWidget(self.example_label)

        options = QWidget()
        options_layout = QVBoxLayout(options)
        options_layout.setContentsMargins(0, 0, 0, 0)
        options_layout.addWidget(what_box)
        options_layout.addWidget(where_box)
        options_layout.addStretch(1)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(chats_box)
        splitter.addWidget(options)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)

        # Actions and progress
        self.pick_btn = QPushButton("اختيار ملفات محددة...")
        self.pick_btn.setToolTip("يعرض قائمة الملفات في المحادثة لتختار منها ما تريد تنزيله فقط")
        self.pick_btn.clicked.connect(self.pick_files)
        self.preview_btn = QPushButton("معاينة (بدون تنزيل)")
        self.preview_btn.clicked.connect(lambda: self.start(preview=True))
        self.start_btn = QPushButton("ابدأ التنزيل")
        self.start_btn.setDefault(True)
        self.start_btn.setStyleSheet("font-weight: bold; padding: 6px 18px;")
        self.start_btn.clicked.connect(lambda: self.start(preview=False))
        self.stop_btn = QPushButton("إيقاف")
        self.stop_btn.clicked.connect(self.stop)
        self.open_btn = QPushButton("فتح مجلد التنزيلات")
        self.open_btn.clicked.connect(self.open_out_dir)
        actions = QHBoxLayout()
        actions.addWidget(self.start_btn)
        actions.addWidget(self.pick_btn)
        actions.addWidget(self.preview_btn)
        actions.addWidget(self.stop_btn)
        actions.addStretch(1)
        actions.addWidget(self.open_btn)

        self.current_label = QLabel(" ")
        self.progress = QProgressBar(textVisible=True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.stats_label = QLabel(" ")
        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.setMaximumBlockCount(5000)
        option = self.log_view.document().defaultTextOption()
        option.setTextDirection(Qt.RightToLeft)
        option.setAlignment(Qt.AlignRight)
        self.log_view.document().setDefaultTextOption(option)
        self.log_view.setMinimumHeight(120)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(account_bar)
        layout.addWidget(splitter, 3)
        layout.addLayout(actions)
        layout.addWidget(self.current_label)
        layout.addWidget(self.progress)
        layout.addWidget(self.stats_label)
        layout.addWidget(self.log_view, 2)
        self.setCentralWidget(central)

    # -- preferences --------------------------------------------------------

    def _load_preferences(self):
        cfg = self.config
        self.out_edit.setText(cfg.get("out_dir") or default_out_dir())
        kinds = cfg.get("kinds")
        if isinstance(kinds, list):
            for kind, box in self.type_checks.items():
                box.setChecked(kind in kinds)
        self.chat_folder_check.setChecked(cfg.get("chat_folder", True))
        self.type_folder_check.setChecked(cfg.get("type_folder", False))
        index = self.naming_combo.findData(cfg.get("naming", "original"))
        self.naming_combo.setCurrentIndex(max(index, 0))
        self._update_example()

    def _save_preferences(self):
        self.config.update(
            out_dir=self.out_edit.text().strip(),
            kinds=[k for k, box in self.type_checks.items() if box.isChecked()],
            chat_folder=self.chat_folder_check.isChecked(),
            type_folder=self.type_folder_check.isChecked(),
            naming=self.naming_combo.currentData(),
        )
        try:
            self.config.save()
        except OSError:
            pass

    def _folder_template(self):
        template = "{sender}"
        if self.chat_folder_check.isChecked():
            template = "{chat}/" + template
        if self.type_folder_check.isChecked():
            template += "/{type}"
        return template

    def _update_example(self):
        parts = [self.out_edit.text().strip() or "..."]
        if self.chat_folder_check.isChecked():
            parts.append("اسم المحادثة")
        parts.append("اسم المرسل")
        if self.type_folder_check.isChecked():
            parts.append("صور")
        numbered = self.naming_combo.currentData() == "numbers"
        parts.append("\u20681.jpg\u2069" if numbered else "الملف")
        self.example_label.setText("مثال: " + "  ›  ".join(parts))

    # -- state --------------------------------------------------------------

    def _update_state(self):
        busy = self.job is not None
        downloading = self.job in ("download", "preview", "scan")
        has_chat = self.chat_list.currentItem() is not None
        self.login_btn.setVisible(not self.logged_in)
        self.login_btn.setEnabled(not busy)
        self.logout_btn.setVisible(self.logged_in)
        self.logout_btn.setEnabled(not busy)
        self.settings_btn.setEnabled(not busy)
        self.refresh_btn.setEnabled(self.logged_in and self.chats_future is None
                                    and self.job not in ("login", "logout"))
        self.chat_list.setEnabled(self.logged_in and not downloading)
        ready = self.logged_in and has_chat and not busy
        self.start_btn.setEnabled(ready)
        self.preview_btn.setEnabled(ready)
        self.pick_btn.setEnabled(ready)
        self.stop_btn.setEnabled(downloading)

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
            if not self.open_settings():
                return
        self.login()

    def open_settings(self):
        dialog = SettingsDialog(self.config, self)
        if dialog.exec() == QDialog.Accepted:
            if self.logged_in:
                self.log("تم حفظ الإعدادات.")
            return True
        return False

    def login(self):
        if not self.config.has_api:
            if not self.open_settings():
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
            self.worker.answer(dialog.method)
            return
        if what in CODE_TITLES:
            dialog = CodeDialog(CODE_TITLES[what], details.get("info", ""), error,
                                details.get("resend"), self)
            accepted = dialog.exec() == QDialog.Accepted
            self.worker.answer(dialog.result_value if accepted else None)
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

    def _cancel_login(self):
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
        if box.clickedButton() is now:
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
            item = QListWidgetItem(f"{pin}{title}    ({label})")
            item.setData(Qt.UserRole, chat_id)
            item.setData(Qt.UserRole + 1, title)
            self.chat_list.addItem(item)
            if chat_id == selected:
                self.chat_list.setCurrentItem(item)
        self._filter_chats(self.chat_search.text())
        self.chats_box.setTitle(f"١. اختر المحادثة ({len(chats)})")
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

    def _collect_options(self, preview):
        kinds = frozenset(k for k, box in self.type_checks.items() if box.isChecked())
        if not kinds:
            QMessageBox.warning(self, APP_TITLE, "اختر نوعاً واحداً من الملفات على الأقل.")
            return None
        out_dir = self.out_edit.text().strip()
        if not out_dir:
            QMessageBox.warning(self, APP_TITLE, "اختر مجلد الحفظ.")
            return None

        since = until = None
        if self.since_check.isChecked():
            since = datetime.combine(self.since_date.date().toPython(), time.min).astimezone()
        if self.until_check.isChecked():
            until = (datetime.combine(self.until_date.date().toPython(), time.min)
                     + timedelta(days=1)).astimezone()
        if since and until and until <= since:
            QMessageBox.warning(self, APP_TITLE, "تاريخ البداية بعد تاريخ النهاية.")
            return None

        exts = normalize_exts(re.split(r"[,،\s]+", self.ext_edit.text()))
        return Options(
            out_dir=Path(out_dir),
            folder=self._folder_template(),
            filter=MessageFilter(
                kinds=kinds,
                exts=exts,
                senders=split_list(self.senders_edit.text()),
                max_size=(self.max_size.value() * 1024 * 1024
                          if self.max_check.isChecked() else None),
            ),
            since=since,
            until=until,
            parallel=3,
            dry_run=preview,
            type_names=TYPE_FOLDERS,
            naming=self.naming_combo.currentData(),
        )

    def _current_chat(self):
        item = self.chat_list.currentItem()
        chat_id = item.data(Qt.UserRole) if item else None
        return (chat_id, item.data(Qt.UserRole + 1)) if chat_id is not None else None

    def _begin(self, job, message, coro_factory, preview):
        self._save_preferences()
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
        options = self._collect_options(preview)
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
        options = self._collect_options(preview=True)
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
        dialog = SelectFilesDialog(items, stats.skipped, self)
        if dialog.exec() == QDialog.Accepted and dialog.selected_ids():
            self.start(ids=dialog.selected_ids(), chat=self.pick_chat)

    def stop(self):
        if self.job_future is not None:
            self.job_future.cancel()

    def open_out_dir(self):
        path = Path(self.out_edit.text().strip() or default_out_dir())
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _browse_out_dir(self):
        path = QFileDialog.getExistingDirectory(self, "اختر مجلد الحفظ", self.out_edit.text())
        if path:
            self.out_edit.setText(path)

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
        sender_index = 1 if self.chat_folder_check.isChecked() else 0
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
            self.account_label.setText(f"الحساب: {result}")
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
            self.chats_box.setTitle("١. اختر المحادثة")
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
                self.open_settings()
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
        if box.clickedButton() is open_btn:
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
        self._save_preferences()
        self.worker.shutdown()
        event.accept()
