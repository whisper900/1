"""Small custom widgets: Telegram-style chat rows and white cards."""

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import QFrame, QStyle, QStyledItemDelegate, QVBoxLayout

from .theme import ACCENT, MUTED, TEXT, paint_avatar

BADGE = "#22B25F"

CHAT_ID_ROLE = Qt.UserRole
CHAT_TITLE_ROLE = Qt.UserRole + 1
CHAT_KIND_ROLE = Qt.UserRole + 2
CHAT_MARK_ROLE = Qt.UserRole + 3
CHAT_BADGE_ROLE = Qt.UserRole + 4      # files waiting in this chat's order

CHAT_KINDS = {"user": "شخص", "group": "مجموعة", "channel": "قناة", "saved": "محفوظة"}


def chat_subtitle(kind, mark):
    text = CHAT_KINDS.get(kind, kind or "")
    if mark == "pinned":
        text += " · مثبّتة"
    elif mark == "archived":
        text += " · مؤرشفة"
    return text


class Card(QFrame):
    """A white rounded panel."""

    def __init__(self, parent=None, margins=(16, 14, 16, 14), spacing=10):
        super().__init__(parent)
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(*margins)
        self.body.setSpacing(spacing)


class ChatDelegate(QStyledItemDelegate):
    """Draws a chat like Telegram does: coloured avatar, bold name, grey details."""

    HEIGHT = 58
    AVATAR = 42

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), self.HEIGHT)

    def paint(self, painter, option, index):
        chat_id = index.data(CHAT_ID_ROLE)
        rect = option.rect.adjusted(6, 2, -6, -2)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        if chat_id is None:                      # "loading..." placeholder
            painter.setPen(QColor(MUTED))
            painter.drawText(rect, Qt.AlignCenter, index.data(Qt.DisplayRole) or "")
            painter.restore()
            return

        selected = bool(option.state & QStyle.State_Selected)
        if selected or option.state & QStyle.State_MouseOver:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(ACCENT if selected else "#EEF2F7"))
            painter.drawRoundedRect(rect, 10, 10)

        rtl = option.direction == Qt.RightToLeft
        title = index.data(CHAT_TITLE_ROLE) or ""
        kind = index.data(CHAT_KIND_ROLE)
        mark = index.data(CHAT_MARK_ROLE)
        size, pad = self.AVATAR, 10
        top = rect.center().y() - size // 2 + 1
        avatar = QRect(rect.right() - pad - size if rtl else rect.left() + pad, top, size, size)
        paint_avatar(painter, avatar, title, chat_id, saved=kind == "saved")

        if rtl:
            text = QRect(rect.left() + pad, rect.top(), avatar.left() - rect.left() - 2 * pad,
                         rect.height())
        else:
            text = QRect(avatar.right() + pad, rect.top(), rect.right() - avatar.right() - 2 * pad,
                         rect.height())

        badge = index.data(CHAT_BADGE_ROLE) or 0
        if badge:
            label = str(badge) if badge < 1000 else "999+"
            badge_font = QFont(option.font)
            badge_font.setBold(True)
            badge_font.setPointSizeF(max(7.0, option.font.pointSizeF() - 1))
            metrics = QFontMetrics(badge_font)
            width = max(22, metrics.horizontalAdvance(label) + 12)
            pill = QRect(0, rect.center().y() - 11, width, 22)
            pill.moveLeft(text.left() if rtl else text.right() - width)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("white" if selected else BADGE))
            painter.drawRoundedRect(pill, 11, 11)
            painter.setFont(badge_font)
            painter.setPen(QColor(ACCENT if selected else "white"))
            painter.drawText(pill, Qt.AlignCenter, label)
            if rtl:
                text.setLeft(pill.right() + 8)
            else:
                text.setRight(pill.left() - 8)
        align = (Qt.AlignRight if rtl else Qt.AlignLeft) | Qt.AlignVCenter

        name_font = QFont(option.font)
        name_font.setBold(True)
        painter.setFont(name_font)
        painter.setPen(QColor("white" if selected else TEXT))
        pin = "📌 " if mark == "pinned" else ""
        name = QFontMetrics(name_font).elidedText(pin + title, Qt.ElideRight, text.width())
        painter.drawText(text.adjusted(0, 0, 0, -text.height() // 2 + 2), align | Qt.AlignBottom, name)

        sub_font = QFont(option.font)
        sub_font.setPointSizeF(max(7.0, option.font.pointSizeF() - 1))
        painter.setFont(sub_font)
        painter.setPen(QColor("#DDF1FC" if selected else MUTED))
        painter.drawText(text.adjusted(0, text.height() // 2 + 2, 0, 0), align | Qt.AlignTop,
                         chat_subtitle(kind, mark))
        painter.restore()
