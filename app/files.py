from __future__ import annotations

import io
import socket
import struct
import zipfile
import re
from pathlib import Path
from PIL import Image


MAX_DELIVERY_BYTES = 50_000_000
ALLOWED_SUFFIXES = {".zip",".pdf",".txt",".md",".json",".csv",".py",".js",".ts",".html",".css",".png",".jpg",".jpeg",".webp"}


def encode_thumbnail(content: bytes) -> bytes:
    """Bounded decode/resize; caller runs this CPU work outside the event loop."""
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"} or image.width * image.height > 25_000_000:
                raise ValueError("invalid image")
            image.verify()
        with Image.open(io.BytesIO(content)) as image:
            converted = image.convert("RGB")
            try:
                converted.thumbnail((1200, 900))
                output = io.BytesIO()
                converted.save(output, "WEBP", quality=88, method=6)
                return output.getvalue()
            finally:
                converted.close()
    except Exception:
        raise ValueError("商品画像を読み込めませんでした") from None


def validate_delivery_file(filename: str, content: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES: raise ValueError("対応していないファイル形式です")
    if not content or len(content) > MAX_DELIVERY_BYTES: raise ValueError("納品ファイルは1バイト〜50MBで送信してください")
    signatures = {".pdf":content.startswith(b"%PDF-"),".png":content.startswith(b"\x89PNG\r\n\x1a\n"),".jpg":content.startswith(b"\xff\xd8\xff"),".jpeg":content.startswith(b"\xff\xd8\xff"),".webp":content.startswith(b"RIFF") and content[8:12]==b"WEBP"}
    if suffix in signatures and not signatures[suffix]: raise ValueError("拡張子とファイル内容が一致しません")
    if suffix == ".zip":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                infos = archive.infolist()
                if len(infos) > 500: raise ValueError("ZIP内のファイル数が多すぎます")
                total = 0
                for info in infos:
                    if info.flag_bits & 0x1: raise ValueError("暗号化されたZIPは安全検査できません")
                    normalized_name = info.filename.replace("\\", "/")
                    parts = Path(normalized_name).parts
                    if any(ord(ch) < 32 for ch in normalized_name) or normalized_name.startswith(("/","//")) or ".." in parts or re.match(r"^[A-Za-z]:", normalized_name): raise ValueError("安全でないZIPパスを含んでいます")
                    if ((info.external_attr >> 16) & 0o170000) == 0o120000: raise ValueError("ZIP内のシンボリックリンクは許可されません")
                    total += info.file_size
                    if info.compress_size and info.file_size / info.compress_size > 150: raise ValueError("圧縮率が不自然なZIPです")
                if total > 200_000_000: raise ValueError("ZIP展開後の容量が大きすぎます")
        except zipfile.BadZipFile as exc: raise ValueError("ZIPファイルを読み込めません") from exc
    return suffix


def clamav_scan(content: bytes, host: str, port: int) -> tuple[bool, str]:
    with socket.create_connection((host, port), timeout=20) as client:
        client.sendall(b"zINSTREAM\0")
        for offset in range(0,len(content),65536):
            chunk = content[offset:offset+65536]
            client.sendall(struct.pack(">I",len(chunk)) + chunk)
        client.sendall(struct.pack(">I",0))
        result = bytearray()
        while b"\0" not in result:
            fragment = client.recv(1024)
            if not fragment or len(result) + len(fragment) > 4096:
                raise OSError("Safety scanner returned an incomplete response")
            result.extend(fragment)
    if not result.endswith(b"\0") or result.count(b"\0") != 1:
        raise OSError("Safety scanner returned an invalid response")
    try:
        response = result[:-1].decode("utf-8", "strict")
    except UnicodeError:
        raise OSError("Safety scanner returned an invalid encoding") from None
    if response == "stream: OK":
        return True, "clamav"
    if re.fullmatch(r"stream: [^\r\n\x00]+ FOUND", response):
        return False, "clamav:malware_detected"
    raise OSError("Safety scanner did not confirm a scan result")
