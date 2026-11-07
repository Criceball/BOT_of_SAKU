"""添付ファイルの形式変換。Discordに依存しないので単体でテストできる。"""

import io
import os
import re
import subprocess
import tempfile

IMAGE_FORMATS = {"jpg", "png", "webp", "gif", "bmp"}
AV_FORMATS = {"mp3", "wav", "ogg", "mp4"}

# 指定できる形式名 → 内部の拡張子
FORMAT_ALIASES = {
    "jpeg": "jpg", "jpg": "jpg", "png": "png", "webp": "webp",
    "gif": "gif", "bmp": "bmp", "pdf": "pdf", "txt": "txt",
    "mp3": "mp3", "wav": "wav", "ogg": "ogg", "mp4": "mp4",
}
PIL_FORMAT_NAMES = {"jpg": "JPEG", "png": "PNG", "webp": "WEBP", "gif": "GIF", "bmp": "BMP", "pdf": "PDF"}

# PDFから画像にするときの最大ページ数（Discordの1メッセージの添付上限に合わせる）
MAX_PDF_PAGES = 10
FFMPEG_TIMEOUT = 120

# 「jpegに」の「g」と「に」が続いても拾えるよう、英字の前後だけを見る
FORMAT_PATTERN = re.compile(
    r"(?<![a-z])(" + "|".join(sorted(FORMAT_ALIASES, key=len, reverse=True)) + r")(?![a-z])",
    re.IGNORECASE,
)


class UnsupportedConversion(Exception):
    pass


def normalize(ext: str) -> str:
    """拡張子（例: '.JPEG'）を内部の形式名（例: 'jpg'）にする。未対応なら空文字。"""
    return FORMAT_ALIASES.get(ext.lower().lstrip("."), "")


def detect_target(text: str, src: str) -> str | None:
    """本文から変換先の形式を探す。元と同じ形式は除外し、最後に出てきたものを採用する。"""
    found = [normalize(m) for m in FORMAT_PATTERN.findall(text)]
    found = [f for f in found if f and f != src]
    return found[-1] if found else None


def convert_file(data: bytes, src: str, dst: str) -> list[tuple[str, bytes]]:
    """(ファイル名, バイト列) のリストを返す。PDFのページ画像は複数になる。"""
    if src in IMAGE_FORMATS and dst in IMAGE_FORMATS | {"pdf"}:
        return [(f"converted.{dst}", _image_convert(data, dst))]
    if src == "pdf" and dst in IMAGE_FORMATS:
        return _pdf_to_images(data, dst)
    if src == "pdf" and dst == "txt":
        return [("converted.txt", _pdf_to_text(data))]
    if src in AV_FORMATS | {"gif"} and dst in AV_FORMATS | {"gif"}:
        return [(f"converted.{dst}", _ffmpeg_convert(data, src, dst))]
    raise UnsupportedConversion(f"{src} → {dst} の変換には対応していません")


def _image_convert(data: bytes, dst: str) -> bytes:
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    img.load()
    # 透過を持たない形式は RGB に変換する
    if dst in {"jpg", "bmp", "pdf"}:
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format=PIL_FORMAT_NAMES[dst])
    return buf.getvalue()


def _pdf_to_images(data: bytes, dst: str) -> list[tuple[str, bytes]]:
    import pymupdf

    outputs = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for i, page in enumerate(doc):
            if i >= MAX_PDF_PAGES:
                break
            pix = page.get_pixmap(dpi=150)
            out_format = "jpeg" if dst == "jpg" else dst
            outputs.append((f"page{i + 1}.{dst}", pix.tobytes(out_format)))
    return outputs


def _pdf_to_text(data: bytes) -> bytes:
    import pymupdf

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        text = "\n".join(page.get_text() for page in doc)
    return text.encode("utf-8")


def _ffmpeg_convert(data: bytes, src: str, dst: str) -> bytes:
    # 形式名は上の検証で許可された値だけなので、ファイル名に危険な文字は入らない
    with tempfile.TemporaryDirectory() as tmp:
        src_path = os.path.join(tmp, f"input.{src}")
        dst_path = os.path.join(tmp, f"output.{dst}")
        with open(src_path, "wb") as f:
            f.write(data)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", src_path, dst_path],
            check=True,
            timeout=FFMPEG_TIMEOUT,
        )
        with open(dst_path, "rb") as f:
            return f.read()
