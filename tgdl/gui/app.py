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
    if "--cleanup" in argv:
        # Background helper: delete scheduled files, no window, then exit.
        from .cleanup import run_helper, store_path
        return run_helper(store_path())
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
        _check_pdf_tools(Path(tmp))
        window = MainWindow(config=Config(Path(tmp) / "settings.json"), autostart=False)
        window.cleanup_store = Path(tmp) / "cleanup.json"
        window.orders_path = Path(tmp) / "orders.json"
        window.show()
        app.processEvents()
        if screenshot:
            window.grab().save(screenshot)
        window.close()
    print("selftest ok")
    return 0


def _check_pdf_tools(folder):
    """Merge a photo into a PDF and read it back: proves the bundle has what
    merging, page counting and printing need (fails the build otherwise)."""
    from PySide6.QtGui import QImage

    from ..pdfmerge import merge_images
    from .orders import count_pages
    from .printing import pdf_pages

    photo = folder / "photo.jpg"
    image = QImage(60, 40, QImage.Format_RGB888)
    image.fill(0x2AABEE)
    if not image.save(str(photo), "JPEG"):
        raise SystemExit("selftest: JPEG support missing")
    merge_images([photo], folder / "check.pdf")
    if count_pages(folder / "check.pdf") != 1:
        raise SystemExit("selftest: PDF page counting failed")
    if next(pdf_pages(folder / "check.pdf", dpi=30)).isNull():
        raise SystemExit("selftest: PDF rendering failed")


if __name__ == "__main__":
    raise SystemExit(main())
