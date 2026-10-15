"""Look of the app: a light, card-based theme with Telegram's blue as accent."""

import hashlib

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPixmap

ACCENT = "#2AABEE"
ACCENT_HOVER = "#1E9AD9"
ACCENT_SOFT = "#E7F5FD"
BACKGROUND = "#F2F4F8"
SURFACE = "#FFFFFF"
BORDER = "#E3E7EE"
TEXT = "#1B2430"
MUTED = "#6B7684"
DANGER = "#E5484D"

# Telegram's avatar colours.
AVATAR_COLORS = ["#FF516A", "#FF9F45", "#9D7CF2", "#4FC26B", "#2BC0B4", "#3AA3E8", "#F25CA2"]

STYLESHEET = f"""
QMainWindow, QDialog {{ background: {BACKGROUND}; }}
QWidget {{ color: {TEXT}; }}
QToolTip {{ background: {TEXT}; color: white; border: none; padding: 6px 8px; border-radius: 6px; }}

QFrame#header {{ background: {SURFACE}; border: none; border-bottom: 1px solid {BORDER}; }}
QFrame#card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 14px; }}
QLabel#appTitle {{ font-size: 13pt; font-weight: 600; }}
QLabel#version, QLabel[muted="true"] {{ color: {MUTED}; }}
QLabel#chatTitle {{ font-size: 15pt; font-weight: 600; }}
QLabel#sectionTitle {{ font-size: 11pt; font-weight: 600; }}
QLabel#emptyTitle {{ font-size: 14pt; font-weight: 600; }}
QLabel#badge {{ background: {ACCENT_SOFT}; color: {ACCENT_HOVER}; border-radius: 9px;
               padding: 1px 8px; font-weight: 600; }}
QLabel#summaryKey {{ color: {MUTED}; }}
QLabel#status {{ color: {MUTED}; }}

QPushButton {{ background: {SURFACE}; border: 1px solid #D3D9E2; border-radius: 9px;
              padding: 7px 16px; }}
QPushButton:hover {{ background: #F5F8FB; border-color: #C2CAD5; }}
QPushButton:pressed {{ background: #EAEFF4; }}
QPushButton:disabled {{ color: #A7AFBA; background: #F7F8FA; border-color: #E7EAEE; }}
QPushButton[kind="primary"] {{ background: {ACCENT}; border: 1px solid {ACCENT}; color: white;
                              font-weight: 600; padding: 9px 22px; }}
QPushButton[kind="primary"]:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton[kind="primary"]:disabled {{ background: #A8DBF6; border-color: #A8DBF6; color: #F3FAFE; }}
QPushButton[kind="danger"] {{ color: {DANGER}; border-color: #F4C2C3; }}
QPushButton[kind="danger"]:hover {{ background: #FDEEEE; }}
QPushButton[kind="danger"]:disabled {{ color: #A7AFBA; border-color: #E7EAEE; background: #F7F8FA; }}
QPushButton[kind="ghost"] {{ background: transparent; border: 1px solid transparent; color: {ACCENT_HOVER};
                            padding: 6px 10px; }}
QPushButton[kind="ghost"]:hover {{ background: {ACCENT_SOFT}; }}
QPushButton[kind="ghost"]:disabled {{ color: #A7AFBA; background: transparent; }}
QPushButton[chip="true"] {{ border-radius: 15px; padding: 6px 14px; }}
QPushButton[chip="true"]:checked {{ background: {ACCENT_SOFT}; border-color: {ACCENT}; color: {ACCENT_HOVER}; }}

QLineEdit {{ background: {SURFACE}; border: 1px solid #D3D9E2; border-radius: 9px; padding: 6px 10px;
             selection-background-color: {ACCENT}; }}
QLineEdit:focus {{ border-color: {ACCENT}; }}
QLineEdit:disabled {{ background: #F7F8FA; color: #A7AFBA; }}
QComboBox, QDateEdit, QSpinBox {{ min-height: 24px; }}
QLineEdit#search {{ background: {BACKGROUND}; border-color: transparent; border-radius: 17px; padding: 7px 14px; }}
QLineEdit#search:focus {{ background: {SURFACE}; border-color: {ACCENT}; }}

QListWidget#chats {{ background: transparent; border: none; outline: 0; }}
QTreeWidget, QPlainTextEdit {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 10px; }}
QTreeWidget::item {{ padding: 5px 2px; }}
QHeaderView::section {{ background: {BACKGROUND}; border: none; border-bottom: 1px solid {BORDER};
                       padding: 6px; font-weight: 600; }}

QProgressBar {{ background: #E8EDF3; border: none; border-radius: 6px; min-height: 12px; max-height: 12px;
               text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 6px; }}

QTabWidget::pane {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 12px; top: -1px; }}
QTabBar::tab {{ background: transparent; border: none; border-bottom: 2px solid transparent;
               padding: 9px 18px; color: {MUTED}; }}
QTabBar::tab:selected {{ color: {ACCENT_HOVER}; border-bottom-color: {ACCENT}; font-weight: 600; }}
QTabBar::tab:hover {{ color: {TEXT}; }}

QSplitter::handle {{ background: transparent; }}
QScrollArea#page, QWidget#pageBody {{ background: {SURFACE}; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #CDD3DC; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #B5BDC8; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
"""


def apply_theme(app):
    app.setStyle("Fusion")    # draws the same on every Windows version
    palette = QPalette()
    for role, color in (
            (QPalette.Window, BACKGROUND), (QPalette.Base, SURFACE),
            (QPalette.AlternateBase, "#F7F9FB"), (QPalette.Text, TEXT),
            (QPalette.WindowText, TEXT), (QPalette.ButtonText, TEXT),
            (QPalette.Button, SURFACE), (QPalette.Highlight, ACCENT),
            (QPalette.HighlightedText, "#FFFFFF"), (QPalette.ToolTipBase, TEXT),
            (QPalette.ToolTipText, "#FFFFFF"), (QPalette.PlaceholderText, "#9AA3AF")):
        palette.setColor(role, QColor(color))
    app.setPalette(palette)
    font = QFont("Segoe UI", 10)
    font.setStyleHint(QFont.SansSerif)
    app.setFont(font)
    app.setStyleSheet(STYLESHEET)


def set_kind(button, kind):
    """Give a button the "primary", "danger" or "ghost" look."""
    button.setProperty("kind", kind)
    return button


def avatar_color(key):
    digest = hashlib.md5(str(key).encode()).digest()
    return AVATAR_COLORS[digest[0] % len(AVATAR_COLORS)]


def initials(name):
    words = [w for w in str(name or "").replace("📌", "").split() if w[:1].isalnum()]
    if not words:
        return "؟"
    letters = words[0][0] + (words[1][0] if len(words) > 1 else "")
    return letters.upper()


def paint_avatar(painter, rect, name, key, saved=False):
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(ACCENT if saved else avatar_color(key)))
    painter.drawEllipse(QRectF(rect))
    font = QFont(painter.font())
    font.setBold(True)
    font.setPixelSize(max(10, int(rect.height() * (0.42 if saved else 0.36))))
    painter.setFont(font)
    painter.setPen(QColor("white"))
    painter.drawText(QRectF(rect), Qt.AlignCenter, "★" if saved else initials(name))
    painter.restore()


def avatar_pixmap(name, key, size=40, saved=False, ratio=2.0):
    pixmap = QPixmap(int(size * ratio), int(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    paint_avatar(painter, QRectF(0, 0, size, size).toRect(), name, key, saved)
    painter.end()
    return pixmap
