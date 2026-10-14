"""Entry point of the desktop app."""

import os
import sys
from pathlib import Path

APP_ID = "tgdl.TelegramDownloader"


def resource(name):
    """Path of a bundled file, both from source and inside the PyInstaller exe."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return base / "assets" / name


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    selftest = "--selftest" in argv
    screenshot = None
    if "--screenshot" in argv:
        screenshot = argv[argv.index("--screenshot") + 1]
    if selftest:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # A windowed exe has no console; keep stray prints and log output harmless.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    if sys.platform == "win32":
        # Show our icon (not Python's) in the taskbar.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:  # noqa: BLE001
            pass

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from .config import Config
    from .theme import apply_theme
    from .window import APP_TITLE, MainWindow

    app = QApplication(argv)
    app.setApplicationName(APP_TITLE)
    app.setLayoutDirection(Qt.RightToLeft)
    apply_theme(app)
    icon = resource("icon.png")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))

    if selftest:
        return _selftest(app, MainWindow, Config, screenshot)

    window = MainWindow()
    # Destroy the window (and its dialogs) while Qt is still running, not
    # during Python's shutdown.
    window.setAttribute(Qt.WA_DeleteOnClose)
    window.show()
    return app.exec()


def _selftest(app, MainWindow, Config, screenshot):
    """Start the UI without touching the network or the real settings."""
    import importlib
    import tempfile

    importlib.import_module("telethon")  # make sure the bundle contains it

    with tempfile.TemporaryDirectory() as tmp:
        window = MainWindow(config=Config(Path(tmp) / "settings.json"), autostart=False)
        window.show()
        app.processEvents()
        if screenshot:
            window.grab().save(screenshot)
        window.close()
    print("selftest ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
