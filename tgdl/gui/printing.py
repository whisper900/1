"""Printing a customer's files in order, without changing them.

Images and PDFs are drawn by the program into one print job, each on a
page that follows its own orientation (a landscape photo prints on a
landscape page). Files are only read. Word, Excel and other documents are
handed to the program Windows uses for them ("printto"), so they print
exactly as that program shows them.
"""

import sys
from dataclasses import dataclass, field

from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QImageReader, QPageLayout, QPainter

from .orders import group_of

PDF_DPI = 300


@dataclass
class PrintResult:
    pages: int = 0
    sent: list = field(default_factory=list)      # handed to their own program
    failed: list = field(default_factory=list)    # (path, reason)


def split(paths):
    """(drawn by us, sent to their program) keeping the original order."""
    ours, others = [], []
    for path in paths:
        (ours if group_of(path) in ("images", "pdf") else others).append(path)
    return ours, others


def read_image(path):
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)     # shown as phones show it (EXIF); file untouched
    image = reader.read()
    return None if image.isNull() else image


def pdf_pages(path, dpi=PDF_DPI):
    """Each page of a PDF as a QImage."""
    import pypdfium2
    with pypdfium2.PdfDocument(str(path)) as doc:
        for page in doc:
            bitmap = page.render(scale=dpi / 72, rev_byteorder=True)
            fmt = QImage.Format_RGBA8888 if bitmap.mode == "RGBA" else QImage.Format_RGB888
            image = QImage(bytes(bitmap.buffer), bitmap.width, bitmap.height, bitmap.stride, fmt)
            yield image.copy()        # own the pixels before pdfium frees them


def _images(path):
    if group_of(path) == "pdf":
        yield from pdf_pages(path)
    else:
        image = read_image(path)
        if image is None:
            raise ValueError("تعذّرت قراءة الصورة")
        yield image


def _fit(image, width, height):
    scale = min(width / image.width(), height / image.height())
    w, h = image.width() * scale, image.height() * scale
    return QRectF((width - w) / 2, (height - h) / 2, w, h)


def print_files(printer, paths, progress=None, cancelled=lambda: False):
    """Print ``paths`` on ``printer`` (already set up by the print dialog)."""
    result = PrintResult()
    ours, others = split(paths)
    painter = None
    try:
        for index, path in enumerate(ours):
            if cancelled():
                break
            try:
                for image in _images(path):
                    landscape = image.width() > image.height()
                    printer.setPageOrientation(
                        QPageLayout.Landscape if landscape else QPageLayout.Portrait)
                    if painter is None:
                        painter = QPainter()
                        if not painter.begin(printer):
                            raise RuntimeError("تعذّر بدء الطباعة على هذه الطابعة")
                    else:
                        printer.newPage()
                    area = printer.pageLayout().paintRectPixels(printer.resolution())
                    painter.setViewport(0, 0, area.width(), area.height())
                    painter.setWindow(0, 0, area.width(), area.height())
                    painter.drawImage(_fit(image, area.width(), area.height()), image)
                    result.pages += 1
            except (ValueError, OSError, ImportError) as e:
                result.failed.append((str(path), str(e)))
            if progress:
                progress(index + 1, len(ours) + len(others))
    finally:
        if painter is not None:
            painter.end()

    for index, path in enumerate(others):
        if cancelled():
            break
        if shell_print(path, printer.printerName()):
            result.sent.append(str(path))
        else:
            result.failed.append((str(path), "لا يوجد برنامج على الجهاز يطبع هذا النوع"))
        if progress:
            progress(len(ours) + index + 1, len(ours) + len(others))
    return result


def shell_print(path, printer_name):
    """Ask Windows to print ``path`` with the program it opens such files with."""
    if sys.platform != "win32":
        return False
    import ctypes
    shell = ctypes.windll.shell32
    if printer_name and shell.ShellExecuteW(None, "printto", str(path), f'"{printer_name}"',
                                            None, 0) > 32:
        return True
    return shell.ShellExecuteW(None, "print", str(path), None, None, 0) > 32
