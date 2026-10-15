"""Put images into one PDF, one image per A4 page, without altering them.

JPEG files are embedded byte for byte (no decoding, no re-compression).
Simple PNG files (8-bit grey or RGB, not interlaced, no transparency) are
embedded byte for byte as well. Other images can be handed to a ``decode``
callback that returns raw RGB pixels, which are stored losslessly.

Each image is shown the way phones and Windows show it: a landscape image
gets a landscape page, a portrait image a portrait page, and an EXIF
orientation flag is honoured by the page's drawing matrix. The pixels
themselves are never rotated, cropped or changed, and the source files are
only read.
"""

import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

A4 = (595.28, 841.89)          # points
MARGIN = 14.17                 # 5 mm

JPEG_EXTS = {".jpg", ".jpeg", ".jfif", ".jpe"}
PNG_EXTS = {".png"}
IMAGE_EXTS = JPEG_EXTS | PNG_EXTS | {".webp", ".bmp", ".gif", ".tif", ".tiff"}


@dataclass
class MergeResult:
    pages: int = 0
    skipped: list = field(default_factory=list)     # (path, reason)


@dataclass
class _Image:
    width: int
    height: int
    colorspace: bytes
    bits: int
    filter: bytes
    data: bytes
    params: bytes = b""
    decode: bytes = b""
    orientation: int = 1


# -- reading image headers -------------------------------------------------------

_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _exif_orientation(app1):
    """Orientation tag (1-8) from an APP1 Exif segment, or 1."""
    if not app1.startswith(b"Exif\0\0") or len(app1) < 14:
        return 1
    tiff = app1[6:]
    order = {b"II": "<", b"MM": ">"}.get(tiff[:2])
    if not order:
        return 1
    try:
        (ifd,) = struct.unpack(order + "I", tiff[4:8])
        (count,) = struct.unpack(order + "H", tiff[ifd:ifd + 2])
        for i in range(count):
            entry = tiff[ifd + 2 + 12 * i: ifd + 14 + 12 * i]
            tag, kind, _, value = struct.unpack(order + "HHI4s", entry)
            if tag == 0x0112:
                (orientation,) = struct.unpack(order + "H", value[:2])
                return orientation if 1 <= orientation <= 8 else 1
    except struct.error:
        pass
    return 1


def read_jpeg(data):
    """Size, colour components and EXIF orientation of a JPEG, or None."""
    if data[:2] != b"\xff\xd8":
        return None
    pos, orientation, adobe = 2, 1, False
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker == 0xFF:            # fill byte
            pos += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        (length,) = struct.unpack(">H", data[pos + 2:pos + 4])
        segment = data[pos + 4:pos + 2 + length]
        if marker == 0xE1:
            orientation = _exif_orientation(segment) if orientation == 1 else orientation
        elif marker == 0xEE and segment.startswith(b"Adobe"):
            adobe = True
        elif marker in _SOF:
            bits, height, width, components = struct.unpack(">BHHB", segment[:6])
            if not (width and height) or components not in (1, 3, 4):
                return None
            return width, height, components, orientation, adobe
        elif marker == 0xDA:          # image data started without a frame header
            return None
        pos += 2 + length
    return None


def _jpeg(data):
    info = read_jpeg(data)
    if info is None:
        return None
    width, height, components, orientation, adobe = info
    colorspace = {1: b"/DeviceGray", 3: b"/DeviceRGB", 4: b"/DeviceCMYK"}[components]
    # Adobe CMYK JPEGs store inverted values.
    decode = b"/Decode [1 0 1 0 1 0 1 0]" if components == 4 and adobe else b""
    return _Image(width, height, colorspace, 8, b"/DCTDecode", data, decode=decode,
                  orientation=orientation)


def _png(data):
    """Pass-through for PNGs a PDF can hold as they are; None otherwise."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos, idat, header = 8, [], None
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body[:13])
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"tRNS":
            return None                      # transparency: needs decoding
        elif kind == b"IEND":
            break
        pos += 12 + length
    if not header or not idat:
        return None
    width, height, bits, color, _, _, interlace = header
    if bits != 8 or interlace or color not in (0, 2):
        return None
    colors = 1 if color == 0 else 3
    params = b"/DecodeParms << /Predictor 15 /Colors %d /BitsPerComponent 8 /Columns %d >>" % (
        colors, width)
    return _Image(width, height, b"/DeviceGray" if colors == 1 else b"/DeviceRGB", 8,
                  b"/FlateDecode", b"".join(idat), params=params)


def _raw(width, height, rgb):
    return _Image(width, height, b"/DeviceRGB", 8, b"/FlateDecode", zlib.compress(rgb, 6))


def load_image(path, decode=None):
    """An image ready for the PDF, or a reason (str) why it can't be used."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as e:
        return f"تعذّرت قراءة الملف: {e}"
    image = _jpeg(data) or _png(data)
    if image is None and decode is not None:
        pixels = decode(path)
        if pixels:
            image = _raw(*pixels)
    return image or "صيغة صورة غير مدعومة"


# -- placing -----------------------------------------------------------------------

def displayed_size(image):
    """Width and height as shown on screen (orientations 5-8 swap them)."""
    if image.orientation >= 5:
        return image.height, image.width
    return image.width, image.height


def page_matrix(image, page=A4, margin=MARGIN):
    """Page size and the ``cm`` matrix that shows the image fitted and centred."""
    shown_w, shown_h = displayed_size(image)
    width, height = (max(page), min(page)) if shown_w > shown_h else (min(page), max(page))
    scale = min((width - 2 * margin) / shown_w, (height - 2 * margin) / shown_h)
    dw, dh = shown_w * scale, shown_h * scale
    x0, y0 = (width - dw) / 2, (height - dh) / 2
    # PDF "cm" matrix (a, b, c, d, e, f) mapping the stored image onto the page
    # so it appears as the EXIF orientation says it should be displayed.
    matrix = {
        1: (dw, 0, 0, dh, x0, y0),
        2: (-dw, 0, 0, dh, x0 + dw, y0),
        3: (-dw, 0, 0, -dh, x0 + dw, y0 + dh),
        4: (dw, 0, 0, -dh, x0, y0 + dh),
        5: (0, -dh, -dw, 0, x0 + dw, y0 + dh),
        6: (0, -dh, dw, 0, x0, y0 + dh),
        7: (0, dh, dw, 0, x0, y0),
        8: (0, dh, -dw, 0, x0 + dw, y0),
    }[image.orientation]
    return (width, height), matrix


# -- writing -----------------------------------------------------------------------

class _Writer:
    def __init__(self, fh):
        self.fh = fh
        self.offsets = {}
        fh.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")

    def put(self, num, body, stream=None):
        self.offsets[num] = self.fh.tell()
        self.fh.write(b"%d 0 obj\n" % num)
        if stream is None:
            self.fh.write(body + b"\nendobj\n")
        else:
            self.fh.write(body + b"\nstream\n")
            self.fh.write(stream)
            self.fh.write(b"\nendstream\nendobj\n")

    def finish(self, root):
        start = self.fh.tell()
        size = max(self.offsets) + 1
        self.fh.write(b"xref\n0 %d\n0000000000 65535 f \n" % size)
        for num in range(1, size):
            self.fh.write(b"%010d 00000 n \n" % self.offsets.get(num, 0))
        self.fh.write(b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
                      % (size, root, start))


def _num(value):
    return (b"%.4f" % value).rstrip(b"0").rstrip(b".") or b"0"


def merge_images(paths, out_path, decode=None, progress=None):
    """Write ``paths`` (in this order) to ``out_path``; returns MergeResult.

    Images are read and written one at a time, so memory use stays small
    however many photos there are.
    """
    result = MergeResult()
    out_path = Path(out_path)
    tmp = out_path.with_name(out_path.name + ".part")
    kids = []
    with tmp.open("wb") as fh:
        writer = _Writer(fh)
        next_num = 3                         # 1 = catalog, 2 = page tree
        for index, path in enumerate(paths):
            image = load_image(path, decode)
            if isinstance(image, str):
                result.skipped.append((str(path), image))
                continue
            (width, height), matrix = page_matrix(image)
            img_num, content_num, page_num = next_num, next_num + 1, next_num + 2
            next_num += 3
            writer.put(img_num, b"<< /Type /XObject /Subtype /Image /Width %d /Height %d "
                       b"/ColorSpace %s /BitsPerComponent %d /Filter %s %s %s /Length %d >>"
                       % (image.width, image.height, image.colorspace, image.bits,
                          image.filter, image.params, image.decode, len(image.data)),
                       image.data)
            content = b"q " + b" ".join(_num(v) for v in matrix) + b" cm /Im0 Do Q"
            writer.put(content_num, b"<< /Length %d >>" % len(content), content)
            writer.put(page_num, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %s %s] "
                       b"/Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>"
                       % (_num(width), _num(height), img_num, content_num))
            kids.append(page_num)
            result.pages += 1
            if progress:
                progress(index + 1, len(paths))
        writer.put(2, b"<< /Type /Pages /Kids [%s] /Count %d >>"
                   % (b" ".join(b"%d 0 R" % k for k in kids), len(kids)))
        writer.put(1, b"<< /Type /Catalog /Pages 2 0 R >>")
        writer.finish(root=1)
    if not result.pages:
        tmp.unlink()
        return result
    tmp.replace(out_path)
    return result
