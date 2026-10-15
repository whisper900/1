"""The "pick files" window: every new file of a chat, with filters and ticks."""

from collections import Counter
from datetime import date as Date, timedelta
from pathlib import Path

from PySide6.QtCore import QDate, QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QPushButton, QSpinBox, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout)

from ..filters import KINDS
from .theme import set_kind
from .widgets import Card

KIND_LABELS = {
    "photo": "صورة", "video": "فيديو", "document": "ملف", "audio": "صوت",
    "voice": "رسالة صوتية", "video_note": "فيديو دائري", "gif": "GIF", "sticker": "ملصق",
}
KIND_CHIPS = {
    "photo": "صور", "video": "فيديو", "document": "ملفات", "audio": "صوتيات",
    "voice": "رسائل صوتية", "video_note": "فيديو دائري", "gif": "GIF", "sticker": "ملصقات",
}
SORT_ROLE = Qt.UserRole
ID_ROLE = Qt.UserRole + 1
SEARCH_ROLE = Qt.UserRole + 2
KIND_ROLE = Qt.UserRole + 3
SENDER_ROLE = Qt.UserRole + 4
EXT_ROLE = Qt.UserRole + 5
DAY_ROLE = Qt.UserRole + 6
NO_EXT = "(بدون امتداد)"


def size_ar(num):
    """File size with Arabic units (Latin units get scrambled in RTL text)."""
    if num is None:
        return "حجم غير معروف"
    for unit in ("بايت", "كيلوبايت", "ميغابايت"):
        if num < 1024:
            return f"{num:.0f} {unit}" if unit == "بايت" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.2f} غيغابايت"


def item_ext(item):
    name = item.get("file_name") or Path(item["path"]).name
    return Path(name).suffix.lower()


def item_day(item):
    """Local calendar day of the message, as an ordinal (0 if unknown)."""
    date = item.get("date")
    return date.astimezone().date().toordinal() if date else 0


class _FileRow(QTreeWidgetItem):
    """Sorts by the raw value (bytes, timestamp) rather than the shown text."""

    def __lt__(self, other):
        column = self.treeWidget().sortColumn() if self.treeWidget() else 0
        mine, theirs = self.data(column, SORT_ROLE), other.data(column, SORT_ROLE)
        if mine is not None and theirs is not None:
            return mine < theirs
        return self.text(column) < other.text(column)


class SelectFilesDialog(QDialog):
    """Lists the chat's new files; filters narrow the list, ticks pick the downloads."""

    COLUMNS = ["الملف", "المرسل", "النوع", "الحجم", "التاريخ"]

    def __init__(self, items, already=0, parent=None, kinds=None):
        super().__init__(parent)
        self.setWindowTitle("اختر الملفات المطلوب تنزيلها")
        self.resize(1000, 700)
        self.total = len(items)

        intro = (f"في هذه المحادثة {len(items)} ملف جديد. استخدم الفلاتر لعرض ما تريده، "
                 "ثم ضع علامة ✔ على الملفات المطلوبة.")
        if already:
            intro += f" ({already} ملف منزّل مسبقاً لا يظهر هنا.)"
        intro_label = QLabel(intro)
        intro_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)
        layout.addWidget(intro_label)
        layout.addWidget(self._build_filters(items, kinds))
        layout.addWidget(self._build_tree(items), 1)

        select_all = QPushButton("تحديد الظاهر")
        select_all.setToolTip("يضع علامة على كل الملفات الظاهرة بعد الفلترة")
        select_all.clicked.connect(lambda: self._set_visible(Qt.Checked))
        select_none = QPushButton("إلغاء تحديد الظاهر")
        select_none.clicked.connect(lambda: self._set_visible(Qt.Unchecked))
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        tools = QHBoxLayout()
        tools.addWidget(select_all)
        tools.addWidget(select_none)
        tools.addSpacing(12)
        tools.addWidget(self.summary, 1)
        layout.addLayout(tools)

        buttons = QDialogButtonBox()
        self.ok_btn = set_kind(buttons.addButton("تنزيل المحدد", QDialogButtonBox.AcceptRole),
                               "primary")
        self.ok_btn.setDefault(True)
        buttons.addButton("إلغاء", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._apply_filters()

    # -- building -------------------------------------------------------------------

    def _build_filters(self, items, kinds):
        card = Card(margins=(14, 12, 14, 12), spacing=8)
        top = QHBoxLayout()
        title = QLabel("الفلترة", objectName="sectionTitle")
        reset = set_kind(QPushButton("إعادة ضبط الفلاتر"), "ghost")
        reset.clicked.connect(self.reset_filters)
        top.addWidget(title)
        top.addStretch(1)
        top.addWidget(reset)
        card.body.addLayout(top)

        # File types present in this chat, with their counts.
        kind_counts = Counter(item.get("kind") for item in items)
        wanted = set(kinds or KINDS) & set(kind_counts)
        if not wanted:
            wanted = set(kind_counts)
        self.kind_chips = {}
        chips = QHBoxLayout()
        chips.setSpacing(6)
        chips.addWidget(QLabel("النوع:"))
        for kind in KIND_CHIPS:                 # images first, stickers last
            if kind not in kind_counts:
                continue
            chip = QPushButton(f"{KIND_CHIPS[kind]} ({kind_counts[kind]})", checkable=True)
            chip.setProperty("chip", True)
            chip.setChecked(kind in wanted)
            chip.toggled.connect(self._apply_filters)
            self.kind_chips[kind] = chip
            chips.addWidget(chip)
        chips.addStretch(1)
        card.body.addLayout(chips)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        self.sender_combo = self._choices(
            "كل المرسلين", Counter(item.get("sender") or "" for item in items))
        self.ext_combo = self._choices(
            "كل الامتدادات", Counter(item_ext(item) for item in items),
            label=lambda ext: ext.lstrip(".") or NO_EXT)
        grid.addWidget(QLabel("المرسل:"), 0, 0)
        grid.addWidget(self.sender_combo, 0, 1)
        grid.addWidget(QLabel("الامتداد:"), 0, 2)
        grid.addWidget(self.ext_combo, 0, 3)

        days = [item_day(item) for item in items if item_day(item)] or [Date.today().toordinal()]
        first, last = (QDate(day.year, day.month, day.day)
                       for day in map(Date.fromordinal, (min(days), max(days))))
        self.since_check = QCheckBox("من تاريخ")
        self.since_date = QDateEdit(first, calendarPopup=True)
        self.until_check = QCheckBox("إلى تاريخ")
        self.until_date = QDateEdit(last, calendarPopup=True)
        dates = QHBoxLayout()
        for check, edit in ((self.since_check, self.since_date), (self.until_check, self.until_date)):
            edit.setDisplayFormat("yyyy/MM/dd")
            edit.setEnabled(False)
            check.toggled.connect(edit.setEnabled)
            check.toggled.connect(self._apply_filters)
            edit.dateChanged.connect(self._apply_filters)
            dates.addWidget(check)
            dates.addWidget(edit)
            dates.addSpacing(10)
        dates.addStretch(1)
        for label, days in (("الكل", None), ("اليوم", 0), ("أمس", 1), ("آخر 7 أيام", 6)):
            quick = set_kind(QPushButton(label), "ghost")
            quick.clicked.connect(lambda _=False, d=days, l=label: self.quick_range(d, l))
            dates.addWidget(quick)
        grid.addWidget(QLabel("الفترة:"), 1, 0)
        grid.addLayout(dates, 1, 1, 1, 3)

        self.max_check = QCheckBox("أصغر من")
        self.max_size = QSpinBox(minimum=1, maximum=4000, value=100, suffix=" ميغابايت")
        self.max_size.setEnabled(False)
        self.max_check.toggled.connect(self.max_size.setEnabled)
        self.max_check.toggled.connect(self._apply_filters)
        self.max_size.valueChanged.connect(self._apply_filters)
        size_row = QHBoxLayout()
        size_row.addWidget(self.max_check)
        size_row.addWidget(self.max_size)
        size_row.addStretch(1)
        grid.addWidget(QLabel("الحجم:"), 2, 0)
        grid.addLayout(size_row, 2, 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        card.body.addLayout(grid)

        self.search = QLineEdit(placeholderText="🔍  ابحث باسم الملف أو المرسل...",
                                objectName="search")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filters)
        card.body.addWidget(self.search)
        return card

    def _choices(self, everything, counts, label=lambda value: value or "—"):
        combo = QComboBox()
        combo.addItem(f"{everything} ({sum(counts.values())})", None)
        for value, count in counts.most_common():
            combo.addItem(f"⁨{label(value)}⁩ ({count})", value)
        combo.currentIndexChanged.connect(self._apply_filters)
        return combo

    def _build_tree(self, items):
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(self.COLUMNS)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setIconSize(QSize(40, 40))
        for item in items:
            kind = item.get("kind")
            kind_label = KIND_LABELS.get(kind, kind or "")
            date = item.get("date")
            name = item.get("file_name") or (
                f"{kind_label} {date:%Y/%m/%d}" if date else Path(item["path"]).name)
            sender = item.get("sender") or ""
            row = _FileRow([
                f"⁨{name}⁩",
                f"⁨{sender}⁩",
                kind_label,
                size_ar(item.get("size")),
                f"{date:%Y/%m/%d %H:%M}" if date else "",
            ])
            row.setFlags(row.flags() | Qt.ItemIsUserCheckable)
            row.setCheckState(0, Qt.Unchecked)
            if item.get("thumb"):
                pixmap = QPixmap()
                if pixmap.loadFromData(item["thumb"]):
                    row.setIcon(0, QIcon(pixmap))
            row.setData(0, ID_ROLE, item["msg_id"])
            row.setData(0, SEARCH_ROLE, f"{name} {sender}".casefold())
            row.setData(0, KIND_ROLE, kind)
            row.setData(0, SENDER_ROLE, sender)
            row.setData(0, EXT_ROLE, item_ext(item))
            row.setData(0, DAY_ROLE, item_day(item))
            row.setData(0, SORT_ROLE, name.casefold())
            row.setData(1, SORT_ROLE, sender.casefold())
            row.setData(2, SORT_ROLE, kind_label)
            row.setData(3, SORT_ROLE, item.get("size") or 0)
            row.setData(4, SORT_ROLE, date.timestamp() if date else 0.0)
            self.tree.addTopLevelItem(row)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(4, Qt.AscendingOrder)       # oldest first, like the chat
        # Fit the columns once; a live ResizeToContents mode keeps measuring
        # rows and has crashed while Qt tears the dialog down.
        header = self.tree.header()
        for column in range(1, len(self.COLUMNS)):
            self.tree.resizeColumnToContents(column)
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.itemChanged.connect(self._update_summary)
        self.tree.itemDoubleClicked.connect(self._toggle)
        return self.tree

    # -- behaviour ------------------------------------------------------------------

    def _rows(self):
        return [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]

    def _apply_filters(self, *_):
        kinds = {kind for kind, chip in self.kind_chips.items() if chip.isChecked()}
        sender, ext = self.sender_combo.currentData(), self.ext_combo.currentData()
        since = until = None
        if self.since_check.isChecked():
            since = self.since_date.date().toPython().toordinal()
        if self.until_check.isChecked():
            until = self.until_date.date().toPython().toordinal()
        limit = self.max_size.value() * 1024 * 1024 if self.max_check.isChecked() else None
        needle = self.search.text().strip().casefold()
        for row in self._rows():
            day = row.data(0, DAY_ROLE)
            show = (row.data(0, KIND_ROLE) in kinds
                    and (sender is None or row.data(0, SENDER_ROLE) == sender)
                    and (ext is None or row.data(0, EXT_ROLE) == ext)
                    and (since is None or day >= since)
                    and (until is None or day <= until)
                    and (limit is None or (row.data(3, SORT_ROLE) or 0) <= limit)
                    and (not needle or needle in row.data(0, SEARCH_ROLE)))
            row.setHidden(not show)
        self._update_summary()

    def quick_range(self, days_back, label=""):
        """«اليوم» / «أمس» / «آخر 7 أيام» / «الكل»."""
        widgets = (self.since_check, self.until_check, self.since_date, self.until_date)
        for widget in widgets:
            widget.blockSignals(True)
        if days_back is None:
            self.since_check.setChecked(False)
            self.until_check.setChecked(False)
        else:
            today = Date.today()
            start = today - timedelta(days=days_back)
            end = start if days_back == 1 else today
            self.since_date.setDate(QDate(start.year, start.month, start.day))
            self.until_date.setDate(QDate(end.year, end.month, end.day))
            self.since_check.setChecked(True)
            self.until_check.setChecked(True)
        self.since_date.setEnabled(self.since_check.isChecked())
        self.until_date.setEnabled(self.until_check.isChecked())
        for widget in widgets:
            widget.blockSignals(False)
        self._apply_filters()

    def reset_filters(self):
        widgets = [*self.kind_chips.values(), self.sender_combo, self.ext_combo,
                   self.since_check, self.until_check, self.max_check, self.search]
        for widget in widgets:
            widget.blockSignals(True)
        for chip in self.kind_chips.values():
            chip.setChecked(True)
        self.sender_combo.setCurrentIndex(0)
        self.ext_combo.setCurrentIndex(0)
        for check, edit in ((self.since_check, self.since_date),
                            (self.until_check, self.until_date),
                            (self.max_check, self.max_size)):
            check.setChecked(False)
            edit.setEnabled(False)
        self.search.clear()
        for widget in widgets:
            widget.blockSignals(False)
        self._apply_filters()

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
        shown = count = hidden_ticked = total = 0
        for row in self._rows():
            shown += not row.isHidden()
            if row.checkState(0) == Qt.Checked:
                count += 1
                total += row.data(3, SORT_ROLE) or 0
                hidden_ticked += row.isHidden()
        text = f"يظهر {shown} من {self.total} — المحدد: {count} ملف ({size_ar(total)})"
        if hidden_ticked:
            text += f"، منها {hidden_ticked} غير ظاهر بسبب الفلترة"
        self.summary.setText(text)
        self.ok_btn.setText(f"تنزيل المحدد ({count})")
        self.ok_btn.setEnabled(count > 0)

    def selected_ids(self):
        """Every ticked file, including ticked files the filters now hide."""
        return sorted(row.data(0, ID_ROLE) for row in self._rows()
                      if row.checkState(0) == Qt.Checked)
