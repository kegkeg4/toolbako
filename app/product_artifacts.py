"""Immutable private file versions for paid instant delivery."""
import asyncio
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException

from .files import clamav_scan, validate_delivery_file
from .object_storage import ObjectStorageError, SupabaseObjectStorage
from .web import clean_visible_text


def artifact_ready(artifact, *, production=False):
    if not isinstance(artifact, dict):
        return False
    record = artifact.get("file", {})
    scope = artifact.get("storage_scope", "")
    if not isinstance(record, dict) or not re.fullmatch(r"product-[a-f0-9]{32}", scope):
        return False
    if (record.get("scan_status") != "passed" or not artifact.get("guide")
            or not re.fullmatch(r"[a-f0-9]{64}", record.get("sha256", ""))
            or record.get("id") != record["sha256"][:24]):
        return False
    if production and (record.get("storage_backend") != "supabase" or record.get("scan_engine") != "clamav"):
        return False
    if record.get("storage_backend") == "supabase":
        return bool(re.fullmatch(r"deliveries/" + re.escape(scope) + r"/[a-f0-9]{32}\.[a-z0-9]+", record.get("object_key", "")))
    return not production and bool(record.get("path"))


def instant_ready(tool, *, production=False):
    return tool.get("fulfillment_type") != "instant" or artifact_ready(tool.get("instant_artifact"), production=production)


def download_scope(order, record):
    """Only the exact purchased version may borrow a product's private scope."""
    artifact = order.get("instant_artifact") or {}
    original = artifact.get("file") or {}
    if record.get("object_key", "").startswith("deliveries/product-"):
        if not artifact_ready(artifact) or any(record.get(k) != original.get(k) for k in ("id", "sha256", "bucket", "object_key")):
            raise ValueError("unmatched purchased artifact")
        return artifact["storage_scope"]
    return order["id"]


async def store_artifact(upload, guide, settings):
    guide = clean_visible_text(guide, 10, 1500, "導入手順", preserve_lines=True)
    content = await upload.read(50_000_001)
    try:
        suffix = validate_delivery_file(upload.filename or "", content)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    scan_engine = "basic"
    if settings.clamav_host:
        try:
            clean, scan_engine = await asyncio.to_thread(clamav_scan, content, settings.clamav_host, settings.clamav_port)
        except OSError:
            raise HTTPException(503, "ファイル安全検査へ接続できません") from None
        if not clean:
            raise HTTPException(422, "安全検査で問題が見つかったため登録できません")
    elif not settings.demo_mode:
        raise HTTPException(503, "ファイル安全検査が未設定です")
    version = uuid4().hex
    scope = "product-" + version
    digest = hashlib.sha256(content).hexdigest()
    record = {"id": digest[:24], "sha256": digest, "name": clean_visible_text(Path(upload.filename).name, 1, 120, "ファイル名"),
              "size": len(content), "scan_status": "passed", "scan_engine": scan_engine}
    if settings.storage_provider == "supabase":
        try:
            record.update(await SupabaseObjectStorage(settings).upload(f"deliveries/{scope}/{uuid4().hex}{suffix}", content))
        except ObjectStorageError:
            raise HTTPException(503, "納品ファイルの保存を確認できません。再送せず運営へお問い合わせください") from None
    elif settings.demo_mode:
        directory = Path(settings.private_storage_path) / scope
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
        path = directory / (digest[:24] + suffix)
        await asyncio.to_thread(path.write_bytes, content)
        path.chmod(0o600)
        record["path"] = str(path)
    else:
        raise HTTPException(503, "納品ファイルの永続保存先が未設定です")
    return {"version": version, "storage_scope": scope, "file": record, "guide": guide, "uploaded_at": datetime.now(timezone.utc)}
