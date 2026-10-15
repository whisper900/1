"""Download preferences and the tabbed settings window."""

import re
from datetime import date as Date, datetime, time, timedelta
from pathlib import Path

from PySide6.QtCore import QDate, QStandardPaths, Qt
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea,
    QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from .. import __version__
from ..downloader import Options
from ..filters import KINDS, MessageFilter, normalize_exts
from .theme import set_kind

# kind, label, ticked by default
TYPE_CHOICES = [
    ("photo", "صور", True),
    ("video", "فيديو", True),
    ("document", "ملفات ومستندات", True),
    ("audio", "موسيقى وصوتيات", True),
    ("voice", "رسائل صوتية", True),
    ("video_note", "فيديو دائري", True),
    ("gif", "صور متحركة GIF", True),
    ("sticker", "ملصقات", False),
]
# Sub-folder names used when "one folder per file type" is ticked.
TYPE_FOLDERS = {
    "photo": "صور", "video": "فيديو", "document": "ملفات", "audio": "صوتيات",
    "voice": "رسائل صوتية", "video_note": "فيديو دائري", "gif": "GIF",
    "sticker": "ملصقات",
}
NAMING_CHOICES = [
    ("original", "الاسم الأصلي للملف"),
    ("numbers", "أرقام متسلسلة 1، 2، 3… (ترقيم منفصل لكل نوع)"),
]

TAB_FILES, TAB_SAVING, TAB_ACCOUNT, TAB_ABOUT = range(4)


def default_out_dir():
    desktop = QStandardPaths.writableLocation(QStandardPaths.DesktopLocation)
    return str(Path(desktop or Path.home()) / "تنزيلات تليكرام")


def split_list(text):
    """Split on commas (Latin or Arabic)."""
    return [part.strip() for part in re.split(r"[,،]", text or "") if part.strip()]


def prefs(config):
    """The saved download preferences, with defaults filled in."""
    kinds = config.get("kinds")
    if not isinstance(kinds, list):
        kinds = [kind for kind, _, on in TYPE_CHOICES if on]
    return {
        "kinds": [k for k in kinds if k in KINDS],
        "exts": config.get("exts", ""),
        "senders": config.get("senders", ""),
        "since_on": bool(config.get("since_on", False)),
        "since": config.get("since") or (Date.today() - timedelta(days=30)).isoformat(),
        "until_on": bool(config.get("until_on", False)),
        "until": config.get("until") or Date.today().isoformat(),
        "max_on": bool(config.get("max_on", False)),
        "max_mb": int(config.get("max_mb", 200)),
        "out_dir": config.get("out_dir") or default_out_dir(),
        "chat_folder": bool(config.get("chat_folder", True)),
        "type_folder": bool(config.get("type_folder", False)),
        "naming": config.get("naming", "original"),
        "auto_delete": bool(config.get("auto_delete", False)),
        "delete_after": int(config.get("delete_after", 15)),
    }


def folder_template(p):
    template = "{sender}"
    if p["chat_folder"]:
        template = "{chat}/" + template
    if p["type_folder"]:
        template += "/{type}"
    return template


def build_options(config, preview=False, everything=False):
    """Options for a download from the saved preferences; ValueError explains a problem.

    ``everything`` keeps where and how files are saved but drops the filters:
    used to list a chat's files for the picker, and to download hand-picked ones.
    """
    p = prefs(config)
    if everything:
        p.update(kinds=list(KINDS), exts="", senders="", since_on=False, until_on=False,
                 max_on=False)
    if not p["kinds"]:
        raise ValueError("اختر نوعاً واحداً من الملفات على الأقل (الإعدادات ← الملفات).")
    since = until = None
    if p["since_on"]:
        since = datetime.combine(Date.fromisoformat(p["since"]), time.min).astimezone()
    if p["until_on"]:
        until = (datetime.combine(Date.fromisoformat(p["until"]), time.min)
                 + timedelta(days=1)).astimezone()
    if since and until and until <= since:
        raise ValueError("تاريخ البداية بعد تاريخ النهاية (الإعدادات ← الملفات).")
    return Options(
        out_dir=Path(p["out_dir"]),
        folder=folder_template(p),
        filter=MessageFilter(
            kinds=frozenset(p["kinds"]),
            exts=normalize_exts(re.split(r"[,،\s]+", p["exts"])),
            senders=split_list(p["senders"]),
            max_size=p["max_mb"] * 1024 * 1024 if p["max_on"] else None,
        ),
        since=since,
        until=until,
        parallel=3,
        dry_run=preview,
        type_names=TYPE_FOLDERS,
        naming=p["naming"],
    )


def summary(config):
    """Short Arabic lines describing the preferences, for the main window."""
    p = prefs(config)
    labels = [label for kind, label, _ in TYPE_CHOICES if kind in p["kinds"]]
    if len(labels) == len(TYPE_CHOICES):
        types = "كل الأنواع"
    elif len(labels) == len(TYPE_CHOICES) - 1 and "sticker" not in p["kinds"]:
        types = "كل الأنواع عدا الملصقات"
    else:
        types = "، ".join(labels) or "لا شيء"
    filters = []
    if p["exts"].strip():
        filters.append("الامتدادات: " + p["exts"].strip())
    if p["senders"].strip():
        filters.append("من: " + p["senders"].strip())
    if p["since_on"]:
        filters.append("من تاريخ " + p["since"].replace("-", "/"))
    if p["until_on"]:
        filters.append("إلى تاريخ " + p["until"].replace("-", "/"))
    if p["max_on"]:
        filters.append(f"أصغر من {p['max_mb']} ميغابايت")
    parts = ["⁨" + p["out_dir"] + "⁩"]
    if p["chat_folder"]:
        parts.append("المحادثة")
    parts.append("المرسل")
    if p["type_folder"]:
        parts.append("النوع")
    naming = dict(NAMING_CHOICES)[p["naming"]].split(" (")[0]
    lines = [
        ("الأنواع", types),
        ("التصفية", " · ".join(filters) or "بدون"),
        ("الحفظ في", "  ›  ".join(parts)),
        ("التسمية", naming),
    ]
    if p["auto_delete"]:
        lines.append(("حذف تلقائي", f"بعد {p['delete_after']} دقيقة من تنزيل كل ملف"))
    return lines


class SettingsDialog(QDialog):
    """All options in one place, in tabs."""

    def __init__(self, config, parent=None, tab=TAB_FILES, require_api=False,
                 on_check_updates=None):
        super().__init__(parent)
        self.config = config
        self.require_api = require_api
        self.setWindowTitle("الإعدادات")
        self.setMinimumSize(700, 640)
        p = prefs(config)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._scroll(self._files_tab(p)), "الملفات")
        self.tabs.addTab(self._scroll(self._saving_tab(p)), "الحفظ والتسمية")
        self.tabs.addTab(self._scroll(self._account_tab()), "حساب تليكرام")
        self.tabs.addTab(self._scroll(self._about_tab(on_check_updates)), "حول البرنامج")
        self.tabs.setCurrentIndex(tab)

        buttons = QDialogButtonBox()
        save = set_kind(buttons.addButton("حفظ", QDialogButtonBox.AcceptRole), "primary")
        save.setDefault(True)
        buttons.addButton("إلغاء", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(14)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(buttons)

    # -- tabs -------------------------------------------------------------------

    @staticmethod
    def _scroll(page):
        """Let a tab scroll instead of squeezing its text when the window is small."""
        area = QScrollArea(objectName="page")
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        page.setObjectName("pageBody")
        area.setWidget(page)
        return area

    @staticmethod
    def _page():
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        return page, layout

    @staticmethod
    def _title(text, hint=None):
        box = QVBoxLayout()
        box.setSpacing(2)
        title = QLabel(text)
        title.setObjectName("sectionTitle")
        box.addWidget(title)
        if hint:
            label = QLabel(hint)
            label.setProperty("muted", True)
            label.setWordWrap(True)
            box.addWidget(label)
        return box

    def _files_tab(self, p):
        page, layout = self._page()
        layout.addLayout(self._title("أنواع الملفات", "اضغط على النوع لتفعيله أو إلغائه."))
        self.type_buttons = {}
        grid = QGridLayout()
        grid.setSpacing(8)
        for i, (kind, label, _) in enumerate(TYPE_CHOICES):
            button = QPushButton(label, checkable=True)
            button.setProperty("chip", True)
            button.setChecked(kind in p["kinds"])
            self.type_buttons[kind] = button
            grid.addWidget(button, i // 4, i % 4)
        layout.addLayout(grid)

        layout.addSpacing(6)
        layout.addLayout(self._title("تصفية إضافية", "كلها اختيارية؛ اتركها فارغة لتنزيل الكل."))
        self.ext_edit = QLineEdit(p["exts"], placeholderText="امتدادات محددة، مثلاً: pdf, docx, zip")
        self.senders_edit = QLineEdit(
            p["senders"], placeholderText="أسماء المرسلين مفصولة بفاصلة، أو @username")
        self.since_check = QCheckBox("من تاريخ", checked=p["since_on"])
        self.since_date = QDateEdit(QDate.fromString(p["since"], Qt.ISODate), calendarPopup=True)
        self.until_check = QCheckBox("إلى تاريخ", checked=p["until_on"])
        self.until_date = QDateEdit(QDate.fromString(p["until"], Qt.ISODate), calendarPopup=True)
        for check, edit in ((self.since_check, self.since_date), (self.until_check, self.until_date)):
            edit.setDisplayFormat("yyyy/MM/dd")
            edit.setEnabled(check.isChecked())
            check.toggled.connect(edit.setEnabled)
        dates = QHBoxLayout()
        for widget in (self.since_check, self.since_date):
            dates.addWidget(widget)
        dates.addSpacing(18)
        for widget in (self.until_check, self.until_date):
            dates.addWidget(widget)
        dates.addStretch(1)
        self.max_check = QCheckBox("تجاهل الملفات الأكبر من", checked=p["max_on"])
        self.max_size = QSpinBox(minimum=1, maximum=4000, suffix=" ميغابايت")
        self.max_size.setValue(p["max_mb"])
        self.max_size.setEnabled(p["max_on"])
        self.max_check.toggled.connect(self.max_size.setEnabled)
        size_row = QHBoxLayout()
        size_row.addWidget(self.max_check)
        size_row.addWidget(self.max_size)
        size_row.addStretch(1)

        form = QFormLayout()
        form.setSpacing(10)
        form.addRow("الامتدادات:", self.ext_edit)
        form.addRow("المرسلون:", self.senders_edit)
        form.addRow("الفترة:", dates)
        form.addRow("الحجم:", size_row)
        layout.addLayout(form)
        layout.addStretch(1)
        return page

    def _saving_tab(self, p):
        page, layout = self._page()
        layout.addLayout(self._title("مجلد الحفظ"))
        self.out_edit = QLineEdit(p["out_dir"])
        self.out_edit.setLayoutDirection(Qt.LeftToRight)   # paths read left to right
        browse = QPushButton("اختيار...")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.out_edit, 1)
        row.addWidget(browse)
        layout.addLayout(row)

        layout.addSpacing(6)
        layout.addLayout(self._title("ترتيب المجلدات", "كل ملف يوضع في مجلد باسم الشخص الذي أرسله."))
        self.chat_folder_check = QCheckBox("مجلد باسم المحادثة", checked=p["chat_folder"])
        self.type_folder_check = QCheckBox("مجلد لكل نوع ملف داخل مجلد المرسل",
                                           checked=p["type_folder"])
        layout.addWidget(self.chat_folder_check)
        layout.addWidget(self.type_folder_check)

        layout.addSpacing(6)
        layout.addLayout(self._title(
            "تسمية الملفات",
            "بالأرقام: الصور 1، 2، 3… والوورد 1، 2، 3… وPDF 1، 2، 3… لكل نوع وحده، حسب تاريخ "
            "الإرسال. يتغيّر الاسم فقط ولا يُعدَّل محتوى الملف."))
        self.naming_combo = QComboBox()
        for value, label in NAMING_CHOICES:
            self.naming_combo.addItem(label, value)
        self.naming_combo.setCurrentIndex(max(0, self.naming_combo.findData(p["naming"])))
        layout.addWidget(self.naming_combo)

        self.example_label = QLabel()
        self.example_label.setProperty("muted", True)
        self.example_label.setWordWrap(True)
        layout.addSpacing(6)
        layout.addWidget(self.example_label)

        layout.addSpacing(6)
        layout.addLayout(self._title(
            "الحذف التلقائي",
            "يحذف كل ملف نهائياً (لا يمر بسلة المحذوفات) بعد المدة المحددة من تنزيله، "
            "حتى لا يبقى على الجهاز. إذا أغلقت البرنامج قبل الموعد يكمل الحذف في الخلفية."))
        self.delete_check = QCheckBox("حذف الملفات المنزّلة تلقائياً بعد", checked=p["auto_delete"])
        self.delete_after = QSpinBox(minimum=1, maximum=7 * 24 * 60, suffix=" دقيقة")
        self.delete_after.setValue(p["delete_after"])
        self.delete_after.setEnabled(p["auto_delete"])
        self.delete_check.toggled.connect(self.delete_after.setEnabled)
        delete_row = QHBoxLayout()
        delete_row.addWidget(self.delete_check)
        delete_row.addWidget(self.delete_after)
        delete_row.addStretch(1)
        layout.addLayout(delete_row)
        for signal in (self.out_edit.textChanged, self.chat_folder_check.toggled,
                       self.type_folder_check.toggled, self.naming_combo.currentIndexChanged):
            signal.connect(self._update_example)
        self._update_example()
        layout.addStretch(1)
        return page

    def _account_tab(self):
        page, layout = self._page()
        layout.addLayout(self._title("رقما API من تليكرام", "يُدخَلان مرة واحدة فقط، ولا تشاركهما مع أحد."))
        steps = QLabel(
            "<ol style='margin:0'>"
            "<li>افتح <a href='https://my.telegram.org'>my.telegram.org</a> وسجّل الدخول برقم "
            "هاتفك (يصلك الكود داخل تطبيق تليكرام).</li>"
            "<li>اضغط <b>API development tools</b>.</li>"
            "<li>املأ App title و Short name بأي اسم، ثم Create application.</li>"
            "<li>انسخ <b>App api_id</b> و <b>App api_hash</b> والصقهما هنا.</li></ol>")
        steps.setWordWrap(True)
        steps.setTextFormat(Qt.RichText)
        steps.setOpenExternalLinks(True)
        layout.addWidget(steps)
        self.api_id = QLineEdit(str(self.config.get("api_id", "")), placeholderText="مثال: 1234567")
        self.api_id.setValidator(QIntValidator(1, 2_000_000_000, self))
        self.api_hash = QLineEdit(self.config.get("api_hash", ""), placeholderText="32 حرفاً ورقماً")
        for edit in (self.api_id, self.api_hash):
            edit.setLayoutDirection(Qt.LeftToRight)
        form = QFormLayout()
        form.setSpacing(10)
        form.addRow("API ID:", self.api_id)
        form.addRow("API Hash:", self.api_hash)
        layout.addLayout(form)
        layout.addStretch(1)
        return page

    def _about_tab(self, on_check_updates):
        page, layout = self._page()
        layout.addLayout(self._title("منزّل ملفات تليكرام", f"الإصدار {__version__}"))
        text = QLabel("ينزّل الملفات من محادثات تليكرام ويرتّبها في مجلد لكل مرسل. "
                      "يتحقق البرنامج من التحديثات عند فتحه ويحدّث نفسه تلقائياً.")
        text.setWordWrap(True)
        layout.addWidget(text)
        if on_check_updates:
            check = QPushButton("التحقق من التحديثات الآن")
            check.clicked.connect(on_check_updates)
            layout.addWidget(check, alignment=Qt.AlignRight)
        layout.addStretch(1)
        return page

    # -- behaviour ----------------------------------------------------------------

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "اختر مجلد الحفظ", self.out_edit.text())
        if path:
            self.out_edit.setText(path)

    def _update_example(self):
        parts = [self.out_edit.text().strip() or "..."]
        if self.chat_folder_check.isChecked():
            parts.append("اسم المحادثة")
        parts.append("اسم المرسل")
        if self.type_folder_check.isChecked():
            parts.append("صور")
        numbered = self.naming_combo.currentData() == "numbers"
        parts.append("⁨1.jpg⁩" if numbered else "الملف")
        self.example_label.setText("مثال: " + "  ›  ".join(parts))

    def _fail(self, tab, message):
        self.tabs.setCurrentIndex(tab)
        QMessageBox.warning(self, "الإعدادات", message)

    def _save(self):
        kinds = [k for k, button in self.type_buttons.items() if button.isChecked()]
        if not kinds:
            return self._fail(TAB_FILES, "اختر نوعاً واحداً من الملفات على الأقل.")
        if (self.since_check.isChecked() and self.until_check.isChecked()
                and self.until_date.date() < self.since_date.date()):
            return self._fail(TAB_FILES, "تاريخ البداية بعد تاريخ النهاية.")
        if not self.out_edit.text().strip():
            return self._fail(TAB_SAVING, "اختر مجلد الحفظ.")
        api_id, api_hash = self.api_id.text().strip(), self.api_hash.text().strip()
        if api_id or api_hash or self.require_api:
            if not api_id.isdigit():
                return self._fail(TAB_ACCOUNT, "API ID يجب أن يكون أرقاماً فقط.")
            if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
                return self._fail(TAB_ACCOUNT, "API Hash يجب أن يكون 32 خانة (أرقام وحروف a-f).")
            self.config.update(api_id=int(api_id), api_hash=api_hash)
        self.config.update(
            kinds=kinds,
            exts=self.ext_edit.text().strip(),
            senders=self.senders_edit.text().strip(),
            since_on=self.since_check.isChecked(),
            since=self.since_date.date().toString(Qt.ISODate),
            until_on=self.until_check.isChecked(),
            until=self.until_date.date().toString(Qt.ISODate),
            max_on=self.max_check.isChecked(),
            max_mb=self.max_size.value(),
            out_dir=self.out_edit.text().strip(),
            chat_folder=self.chat_folder_check.isChecked(),
            type_folder=self.type_folder_check.isChecked(),
            naming=self.naming_combo.currentData(),
            auto_delete=self.delete_check.isChecked(),
            delete_after=self.delete_after.value(),
        )
        try:
            self.config.save()
        except OSError as e:
            QMessageBox.warning(self, "الإعدادات", f"تعذّر حفظ الإعدادات: {e}")
            return
        self.accept()
