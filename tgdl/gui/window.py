"""Main window of the desktop app (Arabic, right-to-left)."""

import asyncio
import re
from datetime import datetime, time, timedelta
from pathlib import Path

from PySide6.QtCore import QDate, QStandardPaths, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QIntValidator
from PySide6.QtWidgets import (
    QCheckBox, QDateEdit, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSpinBox, QSplitter, QVBoxLayout, QWidget)
from telethon import errors as tg_errors

from ..downloader import Options
from ..filters import MessageFilter, normalize_exts
from .config import Config, session_path
from .worker import LoginCancelled, TelegramWorker, friendly_error

APP_TITLE = "منزّل ملفات تليكرام"

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
              "اكتب رقم هاتف حساب تليكرام كاملاً مع رمز الدولة، ويبدأ بعلامة +"),
    "code": ("كود الدخول",
             "أرسل تليكرام كود دخول إلى حسابك (افتح تطبيق تليكرام على هاتفك).\n"
             "اكتب الكود هنا:"),
    "password": ("التحقق بخطوتين",
                 "حسابك محمي بكلمة مرور التحقق بخطوتين.\nاكتب كلمة المرور:"),
}


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


class MainWindow(QMainWindow):
    def __init__(self, config=None, worker=None, autostart=True):
        super().__init__()
        self.config = config if config is not None else Config()
        self.worker = worker if worker is not None else TelegramWorker()
        self.logged_in = False
        self.job = None            # name of the running job
        self.job_future = None
        self.preview = False

        self.setWindowTitle(APP_TITLE)
        self.resize(1000, 720)
        self._build_ui()
        self._load_preferences()
        self._update_state()

        self.worker.ask.connect(self._on_ask)
        self.worker.job_done.connect(self._on_job_done)
        self.worker.job_failed.connect(self._on_job_failed)
        self.worker.event.connect(self._on_download_event)

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
        self.settings_btn = QPushButton("الإعدادات")
        self.settings_btn.clicked.connect(self.open_settings)
        account_bar = QHBoxLayout()
        account_bar.addWidget(self.account_label, 1)
        account_bar.addWidget(self.login_btn)
        account_bar.addWidget(self.logout_btn)
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
        chats_layout = QVBoxLayout(chats_box)
        chats_layout.addWidget(self.chat_search)
        chats_layout.addWidget(self.chat_list, 1)
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
        self._update_example()

    def _save_preferences(self):
        self.config.update(
            out_dir=self.out_edit.text().strip(),
            kinds=[k for k, box in self.type_checks.items() if box.isChecked()],
            chat_folder=self.chat_folder_check.isChecked(),
            type_folder=self.type_folder_check.isChecked(),
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
        parts.append("الملف")
        self.example_label.setText("مثال: " + "  ›  ".join(parts))

    # -- state --------------------------------------------------------------

    def _update_state(self):
        busy = self.job is not None
        downloading = self.job in ("download", "preview")
        has_chat = self.chat_list.currentItem() is not None
        self.login_btn.setVisible(not self.logged_in)
        self.login_btn.setEnabled(not busy)
        self.logout_btn.setVisible(self.logged_in)
        self.logout_btn.setEnabled(not busy)
        self.settings_btn.setEnabled(not busy)
        self.refresh_btn.setEnabled(self.logged_in and not busy)
        self.chat_list.setEnabled(self.logged_in and not downloading)
        ready = self.logged_in and has_chat and not busy
        self.start_btn.setEnabled(ready)
        self.preview_btn.setEnabled(ready)
        self.stop_btn.setEnabled(downloading)

    def _run(self, name, coro):
        self.job = name
        self.job_future = self.worker.submit(name, coro)
        self._update_state()

    def _job_finished(self):
        self.job = None
        self.job_future = None
        self._update_state()

    def log(self, text):
        self.log_view.appendPlainText(text)

    # -- login --------------------------------------------------------------

    def _first_start(self):
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
            "هل تريد تسجيل الخروج من الحساب؟\nستحتاج إلى الكود مرة أخرى للدخول.")
        if answer == QMessageBox.Yes:
            self._run("logout", self.worker.logout())

    def _on_ask(self, what, error):
        title, text = ASK_TEXT[what]
        if error:
            text = f"⚠ {error}\n\n{text}"
        mode = QLineEdit.Password if what == "password" else QLineEdit.Normal
        value, ok = QInputDialog.getText(self, title, text, mode)
        self.worker.answer(value if ok else None)

    def load_chats(self):
        self.chat_list.clear()
        self.chat_list.addItem("جارٍ تحميل المحادثات...")
        self._run("chats", self.worker.load_chats())

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
        )

    def start(self, preview=False):
        item = self.chat_list.currentItem()
        chat_id = item.data(Qt.UserRole) if item else None
        if chat_id is None:
            return
        options = self._collect_options(preview)
        if options is None:
            return
        self._save_preferences()
        self.preview = preview
        self.log_view.clear()
        self.progress.setValue(0)
        self.current_label.setText(" ")
        verb = "معاينة" if preview else "تنزيل"
        self.log(f"بدء {verb} الملفات من: {item.data(Qt.UserRole + 1)}")
        self.worker.downloader = None
        self._run("preview" if preview else "download",
                  self.worker.download(chat_id, options))
        self.stats_timer.start()

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
        if self.preview:
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
        self._job_finished()
        if name == "login":
            self.logged_in = True
            self.account_label.setText(f"الحساب: {result}")
            self._update_state()
            self.load_chats()
        elif name == "chats":
            self.chat_list.clear()
            for chat_id, title, kind in result:
                item = QListWidgetItem(f"{title}    ({CHAT_KINDS.get(kind, kind)})")
                item.setData(Qt.UserRole, chat_id)
                item.setData(Qt.UserRole + 1, title)
                self.chat_list.addItem(item)
            self._filter_chats(self.chat_search.text())
            self._update_state()
        elif name in ("download", "preview"):
            self._finish_download(result)
        elif name == "logout":
            self.logged_in = False
            self.chat_list.clear()
            self.account_label.setText("تم تسجيل الخروج")
            self._update_state()

    def _on_job_failed(self, name, exc):
        self._job_finished()
        if isinstance(exc, (tg_errors.AuthKeyUnregisteredError,
                            tg_errors.SessionRevokedError)):
            # The session was ended from another device: log in again.
            self.logged_in = False
            self.account_label.setText("انتهت جلسة الدخول، سجّل الدخول من جديد")
            self._update_state()
        if name in ("download", "preview"):
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
            if isinstance(exc, LoginCancelled):
                self.account_label.setText("غير مسجّل الدخول")
                return
            self.account_label.setText("لم يتم تسجيل الدخول")
            QMessageBox.warning(self, APP_TITLE, friendly_error(exc))
            if "ApiIdInvalid" in type(exc).__name__:
                self.open_settings()
            return
        if name == "chats":
            self.chat_list.clear()
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
        if self.job in ("download", "preview"):
            answer = QMessageBox.question(self, APP_TITLE,
                                          "التنزيل ما زال يعمل. هل تريد إيقافه والخروج؟")
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.stop()
        self._save_preferences()
        self.worker.shutdown()
        event.accept()
