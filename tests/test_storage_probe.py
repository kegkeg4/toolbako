import asyncio
import json
import sys

import httpx
import pytest

from app import storage_probe
from app.object_storage import ObjectStorageError, SupabaseObjectStorage
from test_object_storage import Provider, configured


class CanaryProvider(Provider):
    def __init__(self):
        super().__init__()
        self.objects = {}
        self.deletes = []
        self.failure = None

    def __call__(self, request):
        path = request.url.path.removeprefix("/storage/v1/")
        if path.startswith("bucket/") or (path.startswith("object/sign/") and request.method == "POST"):
            return super().__call__(request)
        self.requests.append(request)
        if request.method == "DELETE":
            assert request.headers["apikey"] == "sb_secret_fixture"
            prefixes = json.loads(request.content)["prefixes"]
            assert len(prefixes) == 1 and "release-probe-" in prefixes[0]
            key = path.removeprefix("object/") + "/" + prefixes[0]
            self.deletes.append(key)
            if self.failure == "cleanup": return httpx.Response(503)
            body = self.objects.pop(key, None)
            return httpx.Response(200, json=[{"name": prefixes[0]}] if body else [])
        if request.method == "POST":
            assert request.headers["apikey"] == "sb_secret_fixture"
            key = path.removeprefix("object/")
            self.objects[key] = request.content
            if self.failure == "upload_unknown": raise httpx.ReadTimeout("secret provider detail", request=request)
            return httpx.Response(200, json={"Key": key})
        assert request.method == "GET"
        if path.startswith("object/public/"):
            key = path.removeprefix("object/public/")
        elif path.startswith("object/sign/"):
            assert "token=test.private.signature" in str(request.url)
            key = path.removeprefix("object/sign/")
        else:
            assert request.headers["apikey"] == "anon-fixture"
            return httpx.Response(200 if self.failure == "anonymous_allowed" else 403)
        return httpx.Response(200, content=self.objects[key])


@pytest.fixture
def canary(monkeypatch):
    provider = CanaryProvider()
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(provider), **kwargs))
    return provider


def test_default_probe_only_reads_bucket_metadata(canary):
    checks = asyncio.run(storage_probe.probe(configured(supabase_anon_key="anon-fixture")))
    assert all(checks.values()) and len(canary.requests) == 2
    assert all(request.method == "GET" for request in canary.requests)
    assert not canary.objects and not canary.deletes


def test_canary_checks_public_signed_and_unauthorized_download_then_removes_own_objects(canary):
    checks = asyncio.run(storage_probe.probe(configured(supabase_anon_key="anon-fixture"), write_canary=True))
    assert all(checks.values())
    assert {"public_image_read", "signed_private_read", "anonymous_private_denied", "canary_cleanup"} <= checks.keys()
    assert len(canary.deletes) == 2 and not canary.objects
    assert "test.private.signature" not in json.dumps(checks) and "sb_secret" not in json.dumps(checks)


def test_unconfirmed_upload_is_not_retried_and_own_canary_is_cleaned(canary):
    canary.failure = "upload_unknown"
    checks = asyncio.run(storage_probe.probe(configured(supabase_anon_key="anon-fixture"), write_canary=True))
    assert not checks["canary_io"] and checks["canary_cleanup"]
    assert sum(request.method == "POST" for request in canary.requests) == 1
    assert len(canary.deletes) == 2 and not canary.objects


@pytest.mark.parametrize("failure,key", [("cleanup", "canary_cleanup"), ("anonymous_allowed", "anonymous_private_denied")])
def test_failed_security_or_cleanup_check_does_not_pass(canary, failure, key):
    canary.failure = failure
    checks = asyncio.run(storage_probe.probe(configured(supabase_anon_key="anon-fixture"), write_canary=True))
    assert checks[key] is False and not all(checks.values())


def test_incorrect_bucket_permissions_never_upload_or_delete(canary):
    canary.mode_failure = "public_private"
    checks = asyncio.run(storage_probe.probe(configured(supabase_anon_key="anon-fixture"), write_canary=True))
    assert not all(checks.values()) and not canary.deletes
    assert all(request.method == "GET" for request in canary.requests)


@pytest.mark.parametrize("key,image", [("deliveries/customer/file.txt", False), ("images/customer.webp", True),
    ("images/release-probe-" + "a"*32 + ".webp", False), ("deliveries/release-probe-" + "a"*32 + "/other.txt", False),
    ("images/release-probe-../../customer.webp", True)])
def test_cleanup_refuses_any_noncanary_customer_path(canary, key, image):
    with pytest.raises(ObjectStorageError):
        asyncio.run(SupabaseObjectStorage(configured()).remove_probe(key, image=image))
    assert not canary.requests


def test_cli_failure_contains_no_provider_details_or_signed_urls(monkeypatch, capsys):
    async def failed(*args, **kwargs): raise RuntimeError("sb_secret_fixture token=test.private.signature")
    monkeypatch.setattr(storage_probe, "probe", failed)
    monkeypatch.setattr(sys, "argv", ["storage_probe"])
    assert storage_probe.main() == 1
    result = capsys.readouterr().out
    assert "sb_secret_fixture" not in result and "test.private.signature" not in result
    assert json.loads(result)["storage_probe_passed"] is False
