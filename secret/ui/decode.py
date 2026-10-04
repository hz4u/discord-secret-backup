import threading
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter
from PySide6.QtPdf import QPdfDocument

from ..i18n import tr

QT_IMAGE_EXTS = {"jpg", "jpeg", "jfif", "png", "gif", "webp", "bmp", "tif", "tiff", "svg", "ico"}
HEIC_EXTS = {"heic", "heif"}
PDF_EXTS = {"pdf"}
THUMB_EXTS = QT_IMAGE_EXTS - {"svg", "ico"} | HEIC_EXTS | PDF_EXTS

_heif_ready = threading.Lock()
_heif_registered = False


def ext_of(name: str) -> str:
    base = name.rpartition("/")[2]
    return base.rpartition(".")[2].lower() if "." in base else ""


def can_thumb(name: str) -> bool:
    return ext_of(name) in THUMB_EXTS


def _device(data: bytes) -> QBuffer:
    buf = QBuffer()
    buf.setData(QByteArray(data))
    buf.open(QIODevice.ReadOnly)
    return buf


def _qt_image(src, size: int | None) -> QImage:
    buf = _device(src) if isinstance(src, bytes) else None
    reader = QImageReader(buf) if buf is not None else QImageReader(str(src))
    reader.setAutoTransform(True)
    full = reader.size()
    if size and full.isValid() and max(full.width(), full.height()) > size:
        reader.setScaledSize(full.scaled(size, size, Qt.KeepAspectRatio))
    img = reader.read()
    if img.isNull():
        raise ValueError(reader.errorString())
    return img


def _heic_image(src, size: int | None) -> QImage:
    global _heif_registered
    import io

    import pillow_heif
    from PIL import Image, ImageOps

    with _heif_ready:
        if not _heif_registered:
            pillow_heif.register_heif_opener()
            _heif_registered = True
    with Image.open(io.BytesIO(src) if isinstance(src, bytes) else src) as im:
        im = ImageOps.exif_transpose(im)
        if size:
            im.thumbnail((size, size))
        im = im.convert("RGBA")
        raw = im.tobytes("raw", "RGBA")
        return QImage(raw, im.width, im.height, im.width * 4, QImage.Format_RGBA8888).copy()


def open_pdf(src) -> tuple[QPdfDocument, QBuffer | None]:
    doc = QPdfDocument()
    buf = None
    if isinstance(src, bytes):
        buf = _device(src)
        doc.load(buf)
    else:
        doc.load(str(src))
    if doc.status() != QPdfDocument.Status.Ready or doc.pageCount() < 1:
        doc.close()
        raise ValueError(tr("PDF를 열 수 없습니다"))
    return doc, buf


def _pdf_image(src, size: int | None) -> QImage:
    doc, _buf = open_pdf(src)
    try:
        page = doc.pagePointSize(0)
        target = size or 1200
        scaled = QSize(round(page.width()), round(page.height())).scaled(target, target, Qt.KeepAspectRatio)
        img = doc.render(0, scaled)
    finally:
        doc.close()
    if img.isNull():
        raise ValueError(tr("PDF를 그릴 수 없습니다"))
    paper = QImage(img.size(), QImage.Format_RGB32)
    paper.fill(QColor("#FFFFFF"))
    p = QPainter(paper)
    p.drawImage(0, 0, img)
    p.end()
    return paper


def read_image(src: Path | bytes, name: str, size: int | None = None) -> QImage:
    ext = ext_of(name)
    try:
        if ext in HEIC_EXTS:
            return _heic_image(src, size)
        if ext in PDF_EXTS:
            return _pdf_image(src, size)
        return _qt_image(src, size)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ValueError(str(exc)) from exc
