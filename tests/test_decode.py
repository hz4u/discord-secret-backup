import io
import zipfile

import pytest

pytest.importorskip("pytestqt")

from PySide6.QtGui import QColor, QImage, QPageSize, QPainter, QPdfWriter  # noqa: E402

from secret.ui import decode  # noqa: E402
from secret.ui.viewer import list_zip, viewer_kind, zip_name  # noqa: E402


def heic_bytes(size=(64, 48), color=(200, 30, 30)) -> bytes:
    import pillow_heif
    from PIL import Image

    pillow_heif.register_heif_opener()
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="HEIF")
    return buf.getvalue()


def pdf_file(path, pages=2):
    w = QPdfWriter(str(path))
    w.setPageSize(QPageSize(QPageSize.A4))
    p = QPainter(w)
    for i in range(pages):
        if i:
            w.newPage()
        p.drawText(200, 200, f"page {i + 1}")
    p.end()
    return path


def test_heic_from_disk_and_memory(qapp, tmp_path):
    data = heic_bytes()
    (tmp_path / "아이폰.HEIC").write_bytes(data)
    img = decode.read_image(tmp_path / "아이폰.HEIC", "아이폰.HEIC")
    assert (img.width(), img.height()) == (64, 48)
    assert img.pixelColor(10, 10).red() > 150
    small = decode.read_image(data, "a.heic", size=32)
    assert max(small.width(), small.height()) == 32


def test_pdf_first_page_on_white_paper(qapp, tmp_path):
    path = pdf_file(tmp_path / "문서.pdf")
    img = decode.read_image(path, path.name, size=200)
    assert max(img.width(), img.height()) == 200 and img.height() > img.width()
    assert img.pixelColor(5, 5).lightness() > 220
    assert not decode.read_image(path.read_bytes(), "x.pdf", size=100).isNull()


def test_regular_image_is_scaled_and_bad_data_raises(qapp, tmp_path):
    img = QImage(400, 100, QImage.Format_RGB32)
    img.fill(QColor("#00FF00"))
    img.save(str(tmp_path / "a.png"))
    assert decode.read_image(tmp_path / "a.png", "a.png", size=100).width() == 100
    with pytest.raises(ValueError):
        decode.read_image(b"not an image", "b.heic")
    with pytest.raises(ValueError):
        decode.read_image(b"%PDF-broken", "c.pdf")


def test_what_gets_thumbnails_and_viewer_kinds():
    assert all(decode.can_thumb(n) for n in ("a.jpg", "b.HEIC", "c.pdf", "d.tif"))
    assert not decode.can_thumb("e.docx")
    kinds = {n: viewer_kind(n) for n in ("a.heic", "b.pdf", "c.mp3", "d.flac", "e.zip", "f.json", "g.log", "h.7z", "i.hwp")}
    assert kinds == {"a.heic": "image", "b.pdf": "pdf", "c.mp3": "audio", "d.flac": "audio", "e.zip": "archive",
                     "f.json": "text", "g.log": "text", "h.7z": None, "i.hwp": None}


def test_zip_listing_reads_old_korean_names():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("폴더/안의파일.txt", "hi")
        z.writestr("abcd.txt", "x")
    old = "옛날.txt".encode("cp949")
    assert len(old) == len(b"abcd.txt")
    infos = list_zip(buf.getvalue().replace(b"abcd.txt", old))
    assert sorted(zip_name(i) for i in infos) == ["옛날.txt", "폴더/안의파일.txt"]
