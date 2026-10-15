import hashlib
import re

import pytest

Image = pytest.importorskip("PIL.Image")
pdfium = pytest.importorskip("pypdfium2")

from tgdl.pdfmerge import merge_images, read_jpeg  # noqa: E402


def make_jpeg(path, size=(400, 300), orientation=1, marker_at=(0, 0)):
    """Grey JPEG with a red block at ``marker_at`` (stored pixel corner)."""
    img = Image.new("RGB", size, (200, 200, 200))
    x, y = marker_at
    for i in range(size[0] // 5):
        for j in range(size[1] // 5):
            img.putpixel((x + i if x == 0 else size[0] - 1 - i,
                          y + j if y == 0 else size[1] - 1 - j), (255, 0, 0))
    exif = Image.Exif()
    if orientation != 1:
        exif[0x0112] = orientation
    img.save(path, "JPEG", quality=95, exif=exif.tobytes())
    return path


def red_corner(img):
    """Which quadrant (left/right, top/bottom) of ``img`` is mostly red."""
    img = img.convert("RGB")
    w, h = img.size
    best = None
    for name, box in {"tl": (0, 0, w // 2, h // 2), "tr": (w // 2, 0, w, h // 2),
                      "bl": (0, h // 2, w // 2, h), "br": (w // 2, h // 2, w, h)}.items():
        raw = img.crop(box).resize((20, 20)).tobytes()
        red = sum(1 for i in range(0, len(raw), 3) if raw[i] > 180 and raw[i + 1] < 90)
        if best is None or red > best[0]:
            best = (red, name)
    return best[1]


def rendered_page(pdf, index):
    doc = pdfium.PdfDocument(str(pdf))
    page = doc[index]
    width, height = page.get_size()
    img = page.render(scale=0.5).to_pil()
    return (width, height), img


def test_jpeg_bytes_go_into_the_pdf_unchanged(tmp_path):
    src = make_jpeg(tmp_path / "1.jpg")
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    result = merge_images([src], tmp_path / "out.pdf")
    assert result.pages == 1 and not result.skipped
    pdf = (tmp_path / "out.pdf").read_bytes()
    stream = re.search(rb"/DCTDecode.*?stream\n(.*?)\nendstream", pdf, re.S).group(1)
    assert stream == src.read_bytes()                       # byte for byte
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before   # source untouched


def test_landscape_stays_landscape_portrait_stays_portrait(tmp_path):
    wide = make_jpeg(tmp_path / "wide.jpg", size=(400, 300))
    tall = make_jpeg(tmp_path / "tall.jpg", size=(300, 400))
    merge_images([wide, tall], tmp_path / "out.pdf")
    (w1, h1), _ = rendered_page(tmp_path / "out.pdf", 0)
    (w2, h2), _ = rendered_page(tmp_path / "out.pdf", 1)
    assert w1 > h1 and w2 < h2                               # page follows the photo


@pytest.mark.parametrize("orientation", range(1, 9))
def test_pdf_shows_the_photo_as_a_phone_does(tmp_path, orientation):
    from PIL import ImageOps
    src = make_jpeg(tmp_path / "p.jpg", orientation=orientation)
    expected = red_corner(ImageOps.exif_transpose(Image.open(src)))
    merge_images([src], tmp_path / "out.pdf")
    _, page = rendered_page(tmp_path / "out.pdf", 0)
    assert red_corner(page) == expected
    shown = ImageOps.exif_transpose(Image.open(src)).size
    (w, h), _ = rendered_page(tmp_path / "out.pdf", 0)
    assert (w > h) == (shown[0] > shown[1])


def test_order_is_kept_and_unsupported_files_are_reported(tmp_path):
    a = make_jpeg(tmp_path / "a.jpg", marker_at=(0, 0))
    b = make_jpeg(tmp_path / "b.jpg", marker_at=(1, 1))
    bad = tmp_path / "c.heic"
    bad.write_bytes(b"not an image")
    result = merge_images([b, bad, a], tmp_path / "out.pdf")
    assert result.pages == 2 and result.skipped[0][0] == str(bad)
    assert red_corner(rendered_page(tmp_path / "out.pdf", 0)[1]) == "br"   # b first
    assert red_corner(rendered_page(tmp_path / "out.pdf", 1)[1]) == "tl"


def test_png_passthrough_and_decoder_fallback(tmp_path):
    plain = tmp_path / "plain.png"
    Image.new("RGB", (50, 30), (0, 0, 255)).save(plain)
    alpha = tmp_path / "alpha.png"
    Image.new("RGBA", (30, 50), (0, 255, 0, 128)).save(alpha)

    def decode(path):
        img = Image.open(path).convert("RGB")
        return img.width, img.height, img.tobytes()
    result = merge_images([plain, alpha], tmp_path / "out.pdf", decode=decode)
    assert result.pages == 2
    pdf = (tmp_path / "out.pdf").read_bytes()
    assert b"/Predictor 15" in pdf                            # plain PNG kept as is
    _, page = rendered_page(tmp_path / "out.pdf", 0)
    r, g, b = page.convert("RGB").getpixel((page.width // 2, page.height // 2))
    assert b > 200 and r < 50
    assert merge_images([alpha], tmp_path / "x.pdf").skipped     # no decoder: reported


def test_cmyk_and_grey_jpegs(tmp_path):
    grey = tmp_path / "g.jpg"
    Image.new("L", (40, 40), 128).save(grey)
    cmyk = tmp_path / "c.jpg"
    Image.new("CMYK", (40, 40), (0, 0, 0, 0)).save(cmyk)
    assert read_jpeg(grey.read_bytes())[2] == 1
    assert read_jpeg(cmyk.read_bytes())[2] == 4
    result = merge_images([grey, cmyk], tmp_path / "out.pdf")
    assert result.pages == 2
    doc = pdfium.PdfDocument(str(tmp_path / "out.pdf"))
    assert len(doc) == 2


def test_nothing_mergeable_writes_no_file(tmp_path):
    bad = tmp_path / "x.png"
    bad.write_bytes(b"junk")
    result = merge_images([bad], tmp_path / "out.pdf")
    assert result.pages == 0 and not (tmp_path / "out.pdf").exists()
    assert not list(tmp_path.glob("*.part"))
