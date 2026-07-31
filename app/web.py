from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import unquote, urlparse

from fastapi import HTTPException


def slugify(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
    return slug or f"tool-{hashlib.sha1(value.encode()).hexdigest()[:8]}"


def safe_next(value: str | None) -> str:
    if not value:
        return "/"
    decoded = unquote(value)
    if (
        not decoded.startswith("/")
        or decoded.startswith("//")
        or "\\" in decoded
        or any(ord(ch) < 32 for ch in decoded)
    ):
        return "/"
    return value


def safe_http_url(value: str) -> str:
    if not value:
        return ""
    if any(ord(ch) < 32 for ch in value):
        raise HTTPException(422, "URLに使用できない文字が含まれています")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise HTTPException(422, "URLはhttpまたはhttpsで入力してください")
    return value


def clean_visible_text(
    value: str,
    minimum: int,
    maximum: int,
    label: str,
    *,
    preserve_lines: bool = False,
) -> str:
    # NFC preserves intentional Japanese full-width punctuation while still
    # producing stable Unicode text for length checks and storage.
    normalized = unicodedata.normalize("NFC", value)
    normalized = "".join(
        ch for ch in normalized
        if not unicodedata.category(ch).startswith("C") or (preserve_lines and ch in {"\n", "\t"})
    )
    if preserve_lines:
        normalized = "\n".join(line.rstrip() for line in normalized.splitlines()).strip()
    else:
        normalized = re.sub(r"\s+", " ", normalized).strip()
    if not minimum <= len(normalized) <= maximum:
        raise HTTPException(422, f"{label}は{minimum}〜{maximum}文字で入力してください")
    return normalized
