import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import httpx
import pytest

from tools.storage_backup import (BackupError, BUCKETS, FORMAT, HOST, ORIGIN, PROJECT,
    Source, Restic, backup_snapshot, collect, configuration, digest, verify_payload, tree_path)


def fake_source(handler, page_size=1000):
    return Source(httpx.Client(transport=httpx.MockTransport(handler)), "sb_secret_fake",
                  deadline=time.monotonic() + 60, page_size=page_size)


def row(name, body=b"abc", updated="2026-10-25T01:00:00Z"):
    return {"name": name, "id": name, "updated_at": updated,
            "metadata": {"size": len(body), "mimetype": "application/octet-stream", "eTag": "v1"}}


def handler(request):
    if request.method == "GET":
        return httpx.Response(200, content=b"abc")
    body = json.loads(request.content)
    if request.url.path.endswith(BUCKETS[0]):
        if body["prefix"] == "":
            return httpx.Response(200, json=[{"name": "images", "id": None, "metadata": None}])
        return httpx.Response(200, json=[row("sample.webp")])
    return httpx.Response(200, json=[row("test.bin")])


def test_collect_restore_manifest_and_no_source_writes(tmp_path):
    requests = []
    def tracked(req):
        requests.append(req)
        return handler(req)
    manifest = collect(fake_source(tracked), tmp_path / "payload")
    assert len(manifest["files"]) == 2
    assert verify_payload(tmp_path / "payload", manifest) == manifest
    assert all(r.url.host == PROJECT + ".supabase.co" for r in requests)
    assert all(r.method == "GET" or (r.method == "POST" and "/object/list/" in r.url.path) for r in requests)
    assert all(r.headers["apikey"] == "sb_secret_fake" for r in requests)
    assert all("authorization" not in r.headers for r in requests)


def test_listing_paginates_without_missing_objects():
    def pages(req):
        data = json.loads(req.content)
        return httpx.Response(200, json=[row(str(data["offset"]) + ".bin")] if data["offset"] < 3 else [])
    found = fake_source(pages, page_size=1).inventory()
    assert len(found) == 6


@pytest.mark.parametrize("bad", ["../escape", "../", ".", "", "a/b", "a\\b", ".."])
def test_reject_untrusted_paths(bad):
    with pytest.raises(Exception):
        fake_source(lambda req: httpx.Response(200, json=[row(bad)])).inventory()


@pytest.mark.parametrize("rows", [[], [row("a"), row("a")], [dict(row("a"), metadata={"size": 99_000_000})]])
def test_empty_duplicate_or_large_source_fails(rows):
    with pytest.raises(BackupError):
        fake_source(lambda req: httpx.Response(200, json=rows)).inventory()


def test_change_during_download_fails_without_snapshot(tmp_path):
    calls = 0
    def changed(req):
        nonlocal calls
        if req.method == "POST":
            calls += 1
            return httpx.Response(200, json=[row("a", updated=str(calls))])
        return httpx.Response(200, content=b"abc")
    with pytest.raises(BackupError, match="source_changed"):
        collect(fake_source(changed), tmp_path / "payload")


@pytest.mark.parametrize("content", [b"ab", b"abcd"])
def test_short_or_long_download_fails(tmp_path, content):
    def broken(req):
        return httpx.Response(200, content=content) if req.method == "GET" else handler(req)
    with pytest.raises(BackupError, match="download_size"):
        collect(fake_source(broken), tmp_path / "payload")


def test_redirect_never_sends_key_to_other_host(tmp_path):
    seen = []
    def redirect(req):
        seen.append(req.url.host)
        return httpx.Response(302, headers={"location": "https://evil.example/"})
    with pytest.raises(httpx.HTTPStatusError):
        collect(fake_source(redirect), tmp_path / "payload")
    assert seen == [PROJECT + ".supabase.co"]


@pytest.mark.parametrize("tamper", ["content", "extra", "path", "source_duplicate"])
def test_restore_checks_hash_inventory_and_paths(tmp_path, tamper):
    payload = tmp_path / "payload"
    manifest = collect(fake_source(handler), payload)
    if tamper == "content":
        (payload / manifest["files"][0]["file"]).write_bytes(b"bad")
    elif tamper == "extra":
        (payload / "unlisted").write_bytes(b"x")
    else:
        if tamper == "path":
            manifest["files"][0]["file"] = "../outside"
        else:
            manifest["files"][1].update(bucket=manifest["files"][0]["bucket"], key=manifest["files"][0]["key"])
        (payload / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BackupError):
        verify_payload(payload)


def config():
    return {"BACKUP_S3_ENDPOINT": "https://t3.storageapi.dev", "BACKUP_S3_BUCKET": "toolbako-file-backups-test",
        "BACKUP_ENABLED": "true", "BACKUP_SOURCE_KEY": "sb_secret_fake", "BACKUP_PASSWORD": "x" * 48,
        "BACKUP_S3_ACCESS_KEY_ID": "id", "BACKUP_S3_SECRET_ACCESS_KEY": "key"}


def test_config_does_not_forward_other_credentials():
    value = config() | {"RESTIC_PASSWORD_COMMAND": "evil", "AWS_PROFILE": "another", "DATABASE_URL": "secret"}
    child = configuration(value)
    assert child["RESTIC_REPOSITORY"].endswith("/toolbako-storage-v1")
    assert not set(child) & {"BACKUP_SOURCE_KEY", "DATABASE_URL", "RESTIC_PASSWORD_COMMAND", "AWS_PROFILE"}


def test_local_verification_failure_never_sends_alert(monkeypatch, tmp_path):
    import tools.storage_backup as module
    monkeypatch.setattr("sys.argv", ["backup", "verify-local", "--payload", str(tmp_path)])
    monkeypatch.setenv("BACKUP_ALERTS_ENABLED", "true")
    monkeypatch.setattr(module, "notify_failure", lambda *args: pytest.fail("Local verification sent a notification"))
    assert module.main() == 1


@pytest.mark.parametrize("field,value", [("BACKUP_S3_ENDPOINT", "http://t3.storageapi.dev"),
    ("BACKUP_S3_ENDPOINT", "https://t3.storageapi.dev@evil.test"), ("BACKUP_S3_BUCKET", "another-site"),
    ("BACKUP_PASSWORD", "short"), ("BACKUP_ENABLED", "false"), ("BACKUP_SOURCE_KEY", "sb_publishable_fake")])
def test_invalid_destination_fails(field, value):
    with pytest.raises(BackupError):
        configuration(config() | {field: value})


def test_no_retention_after_failed_restore(tmp_path):
    manifest = collect(fake_source(handler), tmp_path / "payload")
    calls = []
    class FailingRestic:
        def latest_manifest(self): return None
        def run(self, *args, **kw):
            calls.append(args[0])
            if args[0] == "stats": return {"total_size": 0}
            if args[0] == "backup": return b'{"message_type":"summary","snapshot_id":"abcd1234"}\n'
            raise BackupError("repository_restore")
    with pytest.raises(BackupError):
        backup_snapshot(FailingRestic(), tmp_path / "payload", manifest, tmp_path / "restore", retention=True)
    assert calls == ["stats", "backup", "restore"]


def test_source_mass_deletion_stops_before_backup(tmp_path):
    manifest = collect(fake_source(handler), tmp_path / "payload")
    class Existing:
        def latest_manifest(self):
            return {"files": [{"bucket": BUCKETS[0]}] * 10}
    with pytest.raises(BackupError, match="unexpected_source_shrink"):
        backup_snapshot(Existing(), tmp_path / "payload", manifest, tmp_path / "restore", retention=True)


def test_restic_real_encryption_roundtrip_and_deleted_source_recovery(tmp_path):
    binary = os.getenv("TOOLBAKO_TEST_RESTIC")
    if not binary:
        pytest.skip("Official restic binary required for real encrypted restore drill")
    password = tmp_path / "password"
    password.write_text("synthetic-test-password-not-a-production-key", encoding="utf-8")
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME") if key in os.environ}
    env.update(RESTIC_REPOSITORY=str(tmp_path / "repository"), RESTIC_PASSWORD_FILE=str(password),
               RESTIC_CACHE_DIR=str(tmp_path / "cache"))
    restic = Restic(binary, env, time.monotonic() + 180, local_test=True)
    restic.run("init", "--repository-version", "2", "--quiet")
    payload = tmp_path / "payload"
    manifest = collect(fake_source(handler), payload)
    result = backup_snapshot(restic, payload, manifest, tmp_path / "restore", retention=True)
    assert result["objects"] == 2 and result["restored_and_hashed"]
    assert restic.latest_manifest() == manifest
    # Simulated source loss only in this disposable local fixture.
    shutil.rmtree(payload)
    restic.run("restore", result["snapshot_id"] + ":" + tree_path(payload.resolve()),
               "--target", str(tmp_path / "after-loss"), "--verify", "--quiet")
    recovered = list((tmp_path / "after-loss").rglob("manifest.json"))
    assert len(recovered) == 1
    assert verify_payload(recovered[0].parent, manifest) == manifest
    restic.run("check", "--read-data", "--quiet")
    wrong = tmp_path / "wrong-password"
    wrong.write_text("a-wrong-synthetic-password", encoding="utf-8")
    with pytest.raises(BackupError):
        Restic(binary, env | {"RESTIC_PASSWORD_FILE": str(wrong)}, time.monotonic() + 60,
               local_test=True).run("snapshots", "--json")
    # No open handles remain after restore; TemporaryDirectory can be cleaned on Windows.
    (recovered[0].parent / manifest["files"][0]["file"]).unlink()
