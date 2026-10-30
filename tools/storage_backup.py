"""Scoped Storage -> encrypted restic backup. No source writes or business workers.

The scheduler runs this once and exits. Provider errors, file names, credentials
and signed links are never printed. Every snapshot is restored and hashed before
retention is allowed. Production restore is deliberately not implemented here.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import quote, urlparse

import httpx

from app.object_storage import safe_object_key
from app.supabase_api import supabase_headers

PROJECT = "xnjzgfaasbemffgorvnj"
ORIGIN = f"https://{PROJECT}.supabase.co"
BUCKETS = ("toolbako-images", "toolbako-deliveries")
TAG = "toolbako-storage-v1"
HOST = "toolbako-storage-backup"
FORMAT = "toolbako-storage-manifest-v1"
MAX_SOURCE_BYTES = 1_000_000_000
MAX_OBJECTS = 20_000
MAX_REPOSITORY_BYTES = 45_000_000_000


class BackupError(RuntimeError):
    """Only a fixed, non-sensitive stage identifier may leave this module."""


def tree_path(path):
    """restic represents Windows drive C: as /C inside its snapshot tree."""
    value = str(path).replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", value):
        value = "/" + value[0] + value[2:]
    return value.rstrip("/")


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class Item:
    bucket: str
    key: str
    size: int
    version: str
    mime: str


class Source:
    def __init__(self, client, key, *, deadline, page_size=1000):
        if not key.startswith("sb_secret_"):
            raise BackupError("source_configuration")
        self.client, self.headers = client, supabase_headers(key)
        self.deadline, self.page_size = deadline, page_size

    def check_time(self):
        if time.monotonic() >= self.deadline:
            raise BackupError("deadline")

    def inventory(self):
        found, seen, folders = [], set(), set()
        total = 0
        for bucket in BUCKETS:
            pending = [""]
            while pending:
                prefix = pending.pop()
                if (bucket, prefix) in folders or len(folders) >= MAX_OBJECTS:
                    raise BackupError("inventory_folder")
                folders.add((bucket, prefix))
                offset = 0
                while True:
                    self.check_time()
                    response = self.client.post(ORIGIN + "/storage/v1/object/list/" + bucket,
                        headers=self.headers, json={"prefix": prefix, "limit": self.page_size,
                        "offset": offset, "sortBy": {"column": "name", "order": "asc"}})
                    response.raise_for_status()
                    rows = response.json()
                    if not isinstance(rows, list) or len(rows) > self.page_size:
                        raise BackupError("inventory_response")
                    for row in rows:
                        name = row.get("name")
                        if not isinstance(name, str) or "/" in name:
                            raise BackupError("inventory_name")
                        key = safe_object_key(prefix + name)
                        if len(key.split("/")) > 12 or (bucket, key) in seen:
                            raise BackupError("inventory_duplicate")
                        seen.add((bucket, key))
                        if row.get("id") is None and row.get("metadata") is None:
                            pending.append(key + "/")
                            continue
                        metadata = row.get("metadata")
                        if not isinstance(metadata, dict) or not row.get("id") or not row.get("updated_at"):
                            raise BackupError("inventory_metadata")
                        size = metadata.get("size")
                        limit = 5_000_000 if bucket == BUCKETS[0] else 50_000_000
                        if type(size) is not int or not 0 <= size <= limit:
                            raise BackupError("object_size")
                        total += size
                        if total > MAX_SOURCE_BYTES or len(found) >= MAX_OBJECTS:
                            raise BackupError("source_capacity")
                        version = json.dumps([row["id"], row["updated_at"], metadata.get("eTag"), size])
                        found.append(Item(bucket, key, size, version, str(metadata.get("mimetype", ""))))
                    if len(rows) < self.page_size:
                        break
                    offset += self.page_size
        if not found:
            raise BackupError("empty_source")
        return sorted(found, key=lambda item: (item.bucket, item.key))

    def download(self, item, target):
        self.check_time()
        path = ORIGIN + "/storage/v1/object/authenticated/" + item.bucket + "/" + quote(item.key, safe="/")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        size = 0
        h = hashlib.sha256()
        with self.client.stream("GET", path, headers=self.headers) as response:
            response.raise_for_status()
            with target.open("xb") as out:
                for chunk in response.iter_bytes(1024 * 1024):
                    self.check_time()
                    size += len(chunk)
                    if size > item.size:
                        raise BackupError("download_size")
                    out.write(chunk)
                    h.update(chunk)
        if size != item.size:
            raise BackupError("download_size")
        return h.hexdigest()


def collect(source, payload):
    before = source.inventory()
    payload.mkdir(mode=0o700)
    files = []
    # Names supplied by the source never become local filesystem names. This also
    # avoids Windows device names, case collisions, links and traversal on restore.
    for index, item in enumerate(before):
        local = f"objects/{index:08d}.bin"
        checksum = source.download(item, payload / local)
        files.append({"bucket": item.bucket, "key": item.key, "size": item.size,
                      "mime": item.mime, "sha256": checksum, "file": local})
    if source.inventory() != before:
        raise BackupError("source_changed")
    manifest = {"format": FORMAT, "project": PROJECT,
                "created_at": datetime.now(timezone.utc).isoformat(), "files": files}
    (payload / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return manifest


def verify_payload(payload, expected=None):
    manifest_path = payload / "manifest.json"
    if manifest_path.is_symlink() or manifest_path.stat().st_size > 15_000_000:
        raise BackupError("restore_manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("format") != FORMAT or manifest.get("project") != PROJECT
            or not isinstance(manifest.get("files"), list) or not 0 < len(manifest["files"]) <= MAX_OBJECTS
            or (expected is not None and manifest != expected)):
        raise BackupError("restore_manifest")
    permitted, source_keys, total = {"manifest.json"}, set(), 0
    for record in manifest["files"]:
        local = record["file"]
        if (not re.fullmatch(r"objects/[0-9]{8}\.bin", local) or local in permitted
                or record["bucket"] not in BUCKETS):
            raise BackupError("restore_path")
        key = safe_object_key(record["key"])
        identity = (record["bucket"], key)
        if identity in source_keys:
            raise BackupError("restore_duplicate")
        source_keys.add(identity)
        permitted.add(local)
        path = payload / local
        if path.is_symlink() or not path.is_file() or type(record["size"]) is not int or record["size"] < 0:
            raise BackupError("restore_file")
        if path.stat().st_size != record["size"] or digest(path) != record["sha256"]:
            raise BackupError("restore_hash")
        total += record["size"]
    actual = set()
    for path in payload.rglob("*"):
        if path.is_symlink():
            raise BackupError("restore_link")
        if path.is_file():
            actual.add(path.relative_to(payload).as_posix())
    if actual != permitted or total > MAX_SOURCE_BYTES:
        raise BackupError("restore_inventory")
    return manifest


class Restic:
    def __init__(self, binary, env, deadline, *, local_test=False):
        self.binary, self.env, self.deadline = str(binary), env, deadline
        self.options = [] if local_test else ["-o", "s3.bucket-lookup=dns", "-o", "s3.connections=2"]

    def run(self, *args, json_result=False):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BackupError("deadline")
        result = subprocess.run([self.binary, *self.options, *args], env=self.env,
            capture_output=True, timeout=min(remaining, 600))
        # No provider output is passed to logs, including when password is wrong.
        if result.returncode:
            raise BackupError("repository_" + args[0])
        return json.loads(result.stdout) if json_result else result.stdout

    def latest_manifest(self):
        snapshots = self.run("snapshots", "--host", HOST, "--tag", TAG, "--json", json_result=True)
        if not snapshots:
            return None
        last = max(snapshots, key=lambda value: value["time"])
        paths = last.get("paths", [])
        if len(paths) != 1:
            raise BackupError("repository_scope")
        value = self.run("dump", last["id"], tree_path(paths[0]) + "/manifest.json")
        manifest = json.loads(value)
        if manifest.get("format") != FORMAT or manifest.get("project") != PROJECT:
            raise BackupError("repository_scope")
        return manifest


def backup_snapshot(restic, payload, manifest, restore_root, *, retention=False):
    previous = restic.latest_manifest()
    if previous:
        for bucket in BUCKETS:
            old = sum(item["bucket"] == bucket for item in previous["files"])
            new = sum(item["bucket"] == bucket for item in manifest["files"])
            if old and new * 2 < old:
                raise BackupError("unexpected_source_shrink")
    stats = restic.run("stats", "--mode", "raw-data", "--json", json_result=True)
    total_bytes = sum(item["size"] for item in manifest["files"])
    if stats["total_size"] + total_bytes > MAX_REPOSITORY_BYTES:
        raise BackupError("repository_capacity")
    output = restic.run("backup", str(payload.resolve()), "--host", HOST, "--tag", TAG,
                        "--group-by", "host,tags", "--read-concurrency", "1", "--json", "--quiet")
    summaries = [json.loads(line) for line in output.splitlines() if line]
    summaries = [row for row in summaries if row.get("message_type") == "summary"]
    if len(summaries) != 1 or not re.fullmatch(r"[a-f0-9]{8,64}", summaries[0].get("snapshot_id", "")):
        raise BackupError("snapshot_confirmation")
    snapshot_id = summaries[0]["snapshot_id"]
    # Restore only our payload subtree; never restore ancestor directories/ACLs.
    restic.run("restore", snapshot_id + ":" + tree_path(payload.resolve()),
               "--target", str(restore_root), "--verify", "--quiet")
    manifests = list(restore_root.rglob("manifest.json"))
    if len(manifests) != 1:
        raise BackupError("restore_inventory")
    verify_payload(manifests[0].parent, manifest)
    if retention:
        restic.run("forget", "--host", HOST, "--tag", TAG, "--group-by", "host,tags",
                   "--keep-within", "30d", "--keep-last", "7", "--prune", "--quiet")
    return {"status": "verified", "snapshot_id": snapshot_id, "objects": len(manifest["files"]),
            "bytes": total_bytes, "restored_and_hashed": True, "retention_applied": retention}


def configuration(env):
    endpoint = env.get("BACKUP_S3_ENDPOINT", "")
    parsed = urlparse(endpoint)
    bucket = env.get("BACKUP_S3_BUCKET", "")
    if (parsed.scheme != "https" or not re.fullmatch(r"[a-z0-9-]+\.storageapi\.dev", parsed.hostname or "")
            or parsed.netloc != parsed.hostname or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or not re.fullmatch(r"toolbako-file-backups-[a-z0-9-]+", bucket)
            or env.get("BACKUP_ENABLED") != "true"
            or not env.get("BACKUP_SOURCE_KEY", "").startswith("sb_secret_")
            or len(env.get("BACKUP_PASSWORD", "")) < 40
            or not env.get("BACKUP_S3_ACCESS_KEY_ID") or not env.get("BACKUP_S3_SECRET_ACCESS_KEY")):
        raise BackupError("configuration")
    # Do not inherit alternative repository/password commands, proxy credentials,
    # cloud config files, or Supabase keys into the restic child process.
    child = {key: env[key] for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME") if key in env}
    child.update(RESTIC_REPOSITORY=f"s3:{endpoint.rstrip('/')}/{bucket}/{TAG}",
                 AWS_ACCESS_KEY_ID=env["BACKUP_S3_ACCESS_KEY_ID"],
                 AWS_SECRET_ACCESS_KEY=env["BACKUP_S3_SECRET_ACCESS_KEY"],
                 AWS_DEFAULT_REGION=env.get("BACKUP_S3_REGION", "auto"),
                 RESTIC_COMPRESSION="auto", GOMAXPROCS="1", GOMEMLIMIT="300MiB")
    return child


def notify_failure(env, stage):
    if env.get("BACKUP_ALERTS_ENABLED") != "true":
        return False
    key = env.get("BACKUP_EMAIL_API_KEY", "")
    if not key:
        return False
    try:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        response = httpx.post("https://api.resend.com/emails", timeout=15, follow_redirects=False,
            headers={"Authorization": "Bearer " + key, "Idempotency-Key": f"storage-backup-failed-{day}"},
            json={"from": "ツールバコ <info@onellc-ai.com>", "to": ["info@onellc-ai.com"],
                  "subject": "【ツールバコ】ファイルバックアップの確認が必要です",
                  "text": "ファイルバックアップまたは復元確認が完了しませんでした。\n"
                          "元ファイルの変更・削除は行っていません。Railwayのバックアップサービスを確認してください。\n"
                          f"処理段階: {stage}\n確認日（UTC）: {day}\n個人情報・ファイル名・秘密値は含みません。"})
        return 200 <= response.status_code < 300
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("initialize", "run", "verify-local"))
    parser.add_argument("--payload", type=Path, help="An already restored local payload; no network")
    args = parser.parse_args()
    env = os.environ.copy()
    stage = "configuration"
    try:
        if args.command == "verify-local":
            if args.payload is None:
                raise BackupError("restore_path")
            value = verify_payload(args.payload)
            print(json.dumps({"status": "verified", "objects": len(value["files"])}))
            return 0
        os.umask(0o077)
        child = configuration(env)
        deadline = time.monotonic() + 900
        with tempfile.TemporaryDirectory(prefix="toolbako-file-backup-") as directory:
            root = Path(directory)
            password = root / "repository-password"
            password.write_text(env["BACKUP_PASSWORD"], encoding="utf-8")
            child.update(RESTIC_PASSWORD_FILE=str(password), RESTIC_CACHE_DIR=str(root / "cache"))
            restic = Restic("restic", child, deadline)
            if args.command == "initialize":
                stage = "initialize"
                restic.run("init", "--repository-version", "2", "--quiet")
                result = {"status": "initialized"}
            else:
                stage = "source"
                with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
                    source = Source(client, env["BACKUP_SOURCE_KEY"], deadline=deadline)
                    manifest = collect(source, root / "payload")
                stage = "backup_and_restore"
                result = backup_snapshot(restic, root / "payload", manifest, root / "restore",
                                         retention=env.get("BACKUP_RETENTION_ENABLED") == "true")
        print(json.dumps({"event": "storage_backup", **result}, separators=(",", ":")))
        return 0
    except Exception as error:
        if isinstance(error, BackupError) and re.fullmatch(r"[a-z_]+", str(error)):
            stage = str(error)
        alerted = False if args.command == "verify-local" else notify_failure(env, stage)
        print(json.dumps({"event": "storage_backup", "status": "failed", "stage": stage,
                          "operator_alert_accepted": alerted}, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
