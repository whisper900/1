import zipfile

import pytest

from tgdl.gui.orders import OrderBook, count_pages, group_of, summarize


def office(path, tag, value):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("docProps/app.xml", f"<Properties><{tag}>{value}</{tag}></Properties>")
    return path


def test_groups_and_page_counts(tmp_path):
    assert group_of("a.JPG") == "images" and group_of("b.docx") == "word"
    assert group_of("c.zip") == "other"
    assert count_pages(tmp_path / "x.jpg") == 1
    assert count_pages(office(tmp_path / "r.docx", "Pages", 12)) == 12
    assert count_pages(office(tmp_path / "s.pptx", "Slides", 7)) == 7
    assert count_pages(office(tmp_path / "e.docx", "Words", 3)) is None
    (tmp_path / "broken.docx").write_bytes(b"not a zip")
    assert count_pages(tmp_path / "broken.docx") is None


def test_pdf_page_count(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    pytest.importorskip("pypdfium2")
    from tgdl.pdfmerge import merge_images
    imgs = []
    for i in range(3):
        imgs.append(tmp_path / f"{i}.jpg")
        Image.new("RGB", (20, 20)).save(imgs[-1])
    merge_images(imgs, tmp_path / "doc.pdf")
    assert count_pages(tmp_path / "doc.pdf") == 3


def test_summary_and_price(tmp_path):
    files = []
    for i in range(3):
        files.append(tmp_path / f"{i}.jpg")
        files[-1].write_bytes(b"x")
    files.append(office(tmp_path / "r.docx", "Pages", 5))
    files.append(tmp_path / "a.zip")
    files[-1].write_bytes(b"z")
    s = summarize(files, price_per_page=250)
    assert s.counts == {"images": 3, "word": 1, "other": 1}
    assert s.pages == 8 and s.price == 2000 and s.unknown == [str(tmp_path / "a.zip")]
    assert s.describe() == "3 صورة · 1 وورد · 1 ملف آخر"


def test_order_book(tmp_path):
    book = OrderBook(tmp_path / "orders.json")
    a, b = tmp_path / "a.jpg", tmp_path / "b.jpg"
    a.write_bytes(b"1")
    b.write_bytes(b"2")
    book.add(-100, "أحمد", b, "photo", msg_id=20)
    book.add(-100, "أحمد", a, "photo", msg_id=10)
    book.add(-100, "أحمد", a, "photo", msg_id=10)          # no duplicates
    assert book.files(-100) == [a, b]                     # message order
    assert book.counts() == {-100: 2}
    b.unlink()                                            # deleted files drop out
    assert book.files(-100) == [a] and book.counts() == {-100: 1}
    book.clear(-100)
    assert book.files(-100) == [] and book.counts() == {}
