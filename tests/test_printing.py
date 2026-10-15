import pytest

pytest.importorskip("PySide6")
Image = pytest.importorskip("PIL.Image")
pdfium = pytest.importorskip("pypdfium2")

import os  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtPrintSupport import QPrinter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tgdl.gui.printing import print_files, split  # noqa: E402
from tgdl.pdfmerge import merge_images  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def pdf_printer(path):
    printer = QPrinter(QPrinter.HighResolution)
    printer.setOutputFormat(QPrinter.PdfFormat)
    printer.setOutputFileName(str(path))
    return printer


def test_order_is_printed_in_one_job_with_each_page_oriented_like_its_file(app, tmp_path):
    wide, tall = tmp_path / "1.jpg", tmp_path / "2.jpg"
    Image.new("RGB", (400, 300), (255, 0, 0)).save(wide)
    Image.new("RGB", (300, 400), (0, 0, 255)).save(tall)
    doc_images = [tmp_path / f"p{i}.jpg" for i in range(2)]
    for i, path in enumerate(doc_images):
        Image.new("RGB", (300, 400), (0, 200, 0)).save(path)
    merge_images(doc_images, tmp_path / "doc.pdf")
    before = {p: p.read_bytes() for p in (wide, tall, tmp_path / "doc.pdf")}

    out = tmp_path / "printed.pdf"
    result = print_files(pdf_printer(out), [wide, tmp_path / "doc.pdf", tall])
    assert result.pages == 4 and not result.failed

    doc = pdfium.PdfDocument(str(out))
    sizes = [doc[i].get_size() for i in range(len(doc))]
    assert [w > h for w, h in sizes] == [True, False, False, False]   # landscape photo first
    colours = []
    for i in range(len(doc)):
        img = doc[i].render(scale=0.2).to_pil().convert("RGB")
        colours.append(img.getpixel((img.width // 2, img.height // 2)))
    assert colours[0][0] > 200 and colours[1][1] > 150 and colours[3][2] > 200   # in order
    assert all(p.read_bytes() == data for p, data in before.items())          # files untouched


def test_office_files_go_to_their_own_program(app, tmp_path, monkeypatch):
    from tgdl.gui import printing
    sent = []
    monkeypatch.setattr(printing, "shell_print", lambda path, name: sent.append(path) or True)
    photo, doc, other = tmp_path / "a.jpg", tmp_path / "b.docx", tmp_path / "c.xyz"
    Image.new("RGB", (30, 20)).save(photo)
    doc.write_bytes(b"x")
    other.write_bytes(b"y")
    assert split([doc, photo, other]) == ([photo], [doc, other])
    result = print_files(pdf_printer(tmp_path / "o.pdf"), [doc, photo, other])
    assert result.pages == 1 and sent == [doc, other]


def test_unreadable_image_is_reported(app, tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    result = print_files(pdf_printer(tmp_path / "o.pdf"), [bad])
    assert result.pages == 0 and result.failed[0][0] == str(bad)
