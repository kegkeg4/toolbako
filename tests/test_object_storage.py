import asyncio
import io
import json
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import HTTPException, UploadFile
from starlette.requests import Request
from starlette.datastructures import Headers
from PIL import Image

from app.config import Settings
from app.data import DemoStore
from app.object_storage import ObjectStorageError, SupabaseObjectStorage, storage_configured, storage_ready
from app.production import readiness_summary
from test_database import database, query
from test_finance import order as make_order


def configured(**changes):
    return Settings(storage_provider="supabase", supabase_url="https://project.supabase.co",
        supabase_service_role_key="sb_secret_fixture", supabase_private_bucket="toolbako-deliveries",
        supabase_image_bucket="toolbako-images", **changes)


class Provider:
    def __init__(self):
        self.requests = []
        self.mode_failure = None
        self.url_failure = None

    def __call__(self, request):
        self.requests.append(request)
        assert request.headers["apikey"] == "sb_secret_fixture" and "authorization" not in request.headers
        assert request.url.host == "project.supabase.co"
        if self.mode_failure == "timeout": raise httpx.ReadTimeout("private provider error", request=request)
        if self.mode_failure == "redirect": return httpx.Response(302, headers={"location": "https://evil.example"})
        if self.mode_failure == "error": return httpx.Response(403, json={"message": "private provider error"})
        if "/bucket/" in request.url.path:
            name = request.url.path.rsplit("/", 1)[-1]
            image = name == "toolbako-images"
            data = {"id": name, "name": name, "public": image, "file_size_limit": 5_000_000 if image else 50_000_000,
                    "allowed_mime_types": ["image/webp" if image else "application/octet-stream"]}
            if self.mode_failure == "public_private": data["public"] = not image
            elif self.mode_failure == "wrong_limit": data["file_size_limit"] -= 1
            elif self.mode_failure == "wrong_mime": data["allowed_mime_types"] = ["text/html"]
            elif self.mode_failure == "wrong_id": data["id"] = "other-bucket"
            return httpx.Response(200, json=data)
        if "/object/sign/" in request.url.path:
            assert request.method == "POST" and json.loads(request.content) == {"expiresIn": 60}
            url = request.url.path.replace("/storage/v1", "") + "?token=test.private.signature"
            if self.url_failure == "host": url = "https://evil.example" + request.url.path + "?token=test.private.signature"
            elif self.url_failure == "path": url = "/object/sign/toolbako-deliveries/deliveries/other/file.txt?token=test"
            elif self.url_failure == "query": url += "&redirect=https://evil.example"
            elif self.url_failure == "duplicate": url += "&token=another"
            elif self.url_failure == "fragment": url += "#private"
            elif self.url_failure == "empty": url = url.split("?")[0] + "?token="
            elif self.url_failure == "wrong_type": url = {"unsafe": True}
            return httpx.Response(200, json={"signedURL": url})
        assert request.method == "POST" and request.headers["x-upsert"] == "false"
        key = request.url.path.split("/object/", 1)[1]
        return httpx.Response(200, json={"Key": "wrong" if self.mode_failure == "wrong_key" else key, "Id": "object-id"})


@pytest.fixture
def provider(monkeypatch):
    result = Provider()
    original = httpx.AsyncClient
    result.client_class = original
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(result), **kwargs))
    return result


def webp():
    output = io.BytesIO()
    Image.new("RGB", (16, 16), "blue").save(output, "WEBP")
    return output.getvalue()


def test_separate_private_delivery_and_public_webp_bucket(provider):
    async def run():
        storage = SupabaseObjectStorage(configured())
        image = await storage.upload("images/random.webp", webp(), image=True)
        delivery = await storage.upload("deliveries/order/file.txt", b"private")
        assert image["bucket"] == "toolbako-images" and delivery["bucket"] == "toolbako-deliveries"
        assert storage.image_url(image["object_key"]).startswith("https://project.supabase.co/storage/v1/object/public/toolbako-images/")
        signed = await storage.signed_delivery_url({**delivery, "name": "日本語の手順書.txt"}, "order")
        parsed = urlparse(signed)
        assert parse_qs(parsed.query)["download"] == ["日本語の手順書.txt"]
        assert "signature" in parsed.query and "signature" not in str(delivery)
        assert await storage_ready(configured())
    asyncio.run(run())


@pytest.mark.parametrize("changes", [{"storage_provider": "other"}, {"supabase_url": "http://project.supabase.co"},
    {"supabase_url": "https://project.supabase.co.evil.example"}, {"supabase_url": "https://user:secret@project.supabase.co"},
    {"supabase_url": "https://project.supabase.co:bad"}, {"supabase_url": "https://project.supabase.co/path"},
    {"supabase_url": "https://project.supabase.co?secret=x"}, {"supabase_private_bucket": "../other"},
    {"supabase_private_bucket": "toolbako-images"}, {"supabase_service_role_key": "sb_publishable_wrong"}])
def test_invalid_configuration_never_sends_credentials(changes, monkeypatch):
    def forbidden(**kwargs): pytest.fail("Invalid configuration must not make requests")
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    settings = replace(configured(), **changes)
    assert not storage_configured(settings) and not asyncio.run(storage_ready(settings))
    with pytest.raises(ObjectStorageError): SupabaseObjectStorage(settings)


@pytest.mark.parametrize("key", ["../other", "/deliveries/o/file", "deliveries//file", "deliveries/o/../file", "deliveries/o/%2e%2e/file", "deliveries/o/file?token=x", "deliveries/o/file\\name", "deliveries/o/file\nname", "images/x.webp"])
def test_invalid_delivery_keys_are_rejected_without_upload(provider, key):
    with pytest.raises(ObjectStorageError): asyncio.run(SupabaseObjectStorage(configured()).upload(key, b"private"))
    assert not provider.requests


@pytest.mark.parametrize("failure", ["timeout", "redirect", "error", "public_private", "wrong_limit", "wrong_mime", "wrong_id", "wrong_key"])
def test_provider_mismatch_fails_closed_without_raw_details(provider, failure):
    provider.mode_failure = failure
    with pytest.raises(ObjectStorageError) as error:
        asyncio.run(SupabaseObjectStorage(configured()).upload("deliveries/o/file.txt", b"private"))
    assert "private provider error" not in str(error.value) and "sb_secret" not in str(error.value)
    assert len(provider.requests) <= 2  # No retries of an uncertain upload.


@pytest.mark.parametrize("failure", ["host", "path", "query", "duplicate", "fragment", "empty", "wrong_type"])
def test_signed_url_must_match_original_origin_object_and_token(provider, failure):
    provider.url_failure = failure
    record = {"storage_backend": "supabase", "bucket": "toolbako-deliveries", "object_key": "deliveries/o/file.txt", "name": "guide.txt"}
    with pytest.raises(ObjectStorageError) as error:
        asyncio.run(SupabaseObjectStorage(configured()).signed_delivery_url(record, "o"))
    assert "signature" not in str(error.value) and "evil.example" not in str(error.value)


def test_image_edit_uses_new_url_and_does_not_depend_on_container_disk(provider, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "settings", configured(demo_mode=False))
    async def run():
        urls = []
        for _ in range(2):
            upload = UploadFile(io.BytesIO(webp()), filename="same.webp", headers=Headers({"content-type": "image/webp"}))
            urls.append(await main.save_thumbnail(upload, "same-slug"))
        assert urls[0] != urls[1]
        assert all("/object/public/toolbako-images/images/" in url for url in urls)
    asyncio.run(run())


def test_nondemo_thumbnail_refuses_ephemeral_local_storage(monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "settings", Settings(demo_mode=False))
    upload = UploadFile(io.BytesIO(webp()), filename="image.webp", headers=Headers({"content-type": "image/webp"}))
    with pytest.raises(HTTPException) as error: asyncio.run(main.save_thumbnail(upload, "slug"))
    assert error.value.status_code == 503


def test_nondemo_delivery_upload_is_scanned_private_and_persists_only_reference(database, provider, monkeypatch):
    import app.main as main
    backend, dsn = database
    async def run():
        store = DemoStore(seed=False)
        monkeypatch.setattr(main, "store", store)
        monkeypatch.setattr(main, "settings", configured(demo_mode=False, clamav_host="private-scanner"))
        monkeypatch.setattr(main, "current_user", lambda request: {"id": "seller"})
        calls = []
        def scan(content, host, port):
            calls.append(content)
            return True, "clamav"
        monkeypatch.setattr(main, "clamav_scan", scan)
        upload = UploadFile(io.BytesIO(b"private customer fixture"), filename="guide.txt")
        request = Request({"type": "http", "method": "POST", "path": "/orders/o/deliver", "headers": []})
        async with backend.request(store):
            store.orders.append(make_order(id="o", status="in_progress", revision_count=0))
            response = await main.deliver_order(request, "o", "手順書を納品します。", upload)
            await backend.save()
        assert response.status_code == 303 and len(calls) == 1
        record = store.orders[0]["delivery"]["files"][0]
        assert record["scan_status"] == "passed" and record["scan_engine"] == "clamav"
        assert record["bucket"] == "toolbako-deliveries" and record["object_key"].startswith("deliveries/o/")
        assert "path" not in record and "token" not in record
        assert all("private customer fixture" not in str(row) for row in query(dsn, "select payload from toolbako_runtime.marketplace_state"))
    asyncio.run(run())


@pytest.mark.parametrize("failure,status", [("unconfigured", 503), ("malware", 422), ("unavailable", 503), ("unpaid", 409), ("stranger", 403)])
def test_delivery_safety_or_authorization_failure_never_uploads(provider, monkeypatch, failure, status):
    import app.main as main
    store = DemoStore(seed=False)
    store.orders.append(make_order(id="o", status="in_progress", revision_count=0,
                                  payment_status="pending" if failure == "unpaid" else "paid"))
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "settings", configured(demo_mode=False, clamav_host="" if failure == "unconfigured" else "private-scanner"))
    monkeypatch.setattr(main, "current_user", lambda request: {"id": "stranger" if failure == "stranger" else "seller"})
    def scan(*args):
        if failure == "unavailable": raise OSError("private provider detail")
        return failure != "malware", "clamav"
    monkeypatch.setattr(main, "clamav_scan", scan)
    upload = UploadFile(io.BytesIO(b"private fixture"), filename="guide.txt")
    request = Request({"type": "http", "method": "POST", "path": "/orders/o/deliver", "headers": []})
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.deliver_order(request, "o", "手順書を納品します。", upload))
    assert error.value.status_code == status and not provider.requests
    assert store.orders[0]["status"] == "in_progress" and not store.orders[0].get("delivery")


@pytest.mark.parametrize("actor", [None, "stranger", "buyer", "seller"])
def test_signed_download_requires_participant_and_preserves_audit_without_token(database, provider, monkeypatch, actor):
    import app.main as main
    backend, dsn = database
    async def run():
        store = DemoStore(seed=False)
        async with backend.request(store):
            order = make_order(id="o", delivery={"files": [{"id": "file", "name": "guide.txt", "scan_status": "passed",
                "storage_backend": "supabase", "bucket": "toolbako-deliveries", "object_key": "deliveries/o/file.txt"}]})
            store.orders.append(order)
            await backend.save()
        monkeypatch.setattr(main, "store", store)
        monkeypatch.setattr(main, "settings", configured(demo_mode=False))
        monkeypatch.setattr(main, "current_user", lambda request: {"id": actor} if actor else None)
        request = Request({"type": "http", "method": "GET", "path": "/orders/o/files/file", "headers": []})
        async with backend.request(store):
            if actor not in {"buyer", "seller"}:
                with pytest.raises(HTTPException) as error: await main.download_delivery_file(request, "o", "file")
                assert error.value.status_code == 404 and not provider.requests
            else:
                response = await main.download_delivery_file(request, "o", "file")
                assert response.status_code == 303 and response.headers["referrer-policy"] == "no-referrer"
                await backend.save()
        assert all("test.private.signature" not in str(row) for row in query(dsn, "select payload from toolbako_runtime.marketplace_state"))
        assert all("test.private.signature" not in str(row) for row in query(dsn, "select target from toolbako_runtime.audit_events"))
    asyncio.run(run())


def test_refunded_buyer_cannot_request_another_signed_url(provider, monkeypatch):
    import app.main as main
    store = DemoStore(seed=False)
    store.orders.append(make_order(id="o", refund_status="completed", delivery={"files": [{"id": "file", "scan_status": "passed"}]}))
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "settings", configured(demo_mode=False))
    monkeypatch.setattr(main, "current_user", lambda request: {"id": "buyer"})
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.download_delivery_file(Request({"type": "http", "method": "GET", "path": "/", "headers": []}), "o", "file"))
    assert error.value.status_code == 404 and not provider.requests


def test_readiness_requires_verified_buckets_not_absolute_ephemeral_path():
    settings = configured(demo_mode=False)
    assert not next(x for x in readiness_summary(settings)["checks"] if x["key"] == "private_storage")["ok"]
    assert next(x for x in readiness_summary(settings, runtime_storage_ready=True)["checks"] if x["key"] == "private_storage")["ok"]
    local = Settings(demo_mode=False, private_storage_path="/tmp/ephemeral")
    assert not next(x for x in readiness_summary(local)["checks"] if x["key"] == "private_storage")["ok"]


@pytest.mark.parametrize("fragments,clean", [([b"stream: OK\0"], True), ([b"str", b"eam: O", b"K\0"], True),
                                            ([b"stream: Eicar-Test-Signature FOUND\0"], False)])
def test_clamav_reads_complete_framed_reply_and_returns_only_safe_engine(fragments, clean, monkeypatch):
    import app.files as files
    sent = []
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def sendall(self, data): sent.append(data)
        def recv(self, size): return fragments.pop(0) if fragments else b""
    monkeypatch.setattr(files.socket, "create_connection", lambda *args, **kwargs: Socket())
    result = files.clamav_scan(b"content", "private-scanner", 3310)
    assert result[0] is clean and result[1] in {"clamav", "clamav:malware_detected"}
    assert sent[0] == b"zINSTREAM\0" and sent[-1] == b"\0\0\0\0"


@pytest.mark.parametrize("reply", [b"stream: NOTOK\0", b"stream: OK", b"", b"stream: error ERROR\0",
    b"stream: malware FOUND\nstream: OK\0", b"stream: OK\0another\0", b"stream: \xff\0", b"x" * 4097])
def test_clamav_unconfirmed_or_malformed_reply_is_service_failure_not_clean(reply, monkeypatch):
    import app.files as files
    parts = [reply]
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def sendall(self, data): pass
        def recv(self, size): return parts.pop(0) if parts else b""
    monkeypatch.setattr(files.socket, "create_connection", lambda *args, **kwargs: Socket())
    with pytest.raises(OSError): files.clamav_scan(b"content", "private-scanner", 3310)


def test_storage_csp_allows_only_configured_project_and_preserves_no_referrer():
    from app.middleware import ProductionGuardMiddleware
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.responses import RedirectResponse
    app = FastAPI()
    @app.get("/orders/file")
    async def redirect(): return RedirectResponse("https://project.supabase.co/storage/v1/object/sign/file?token=private", 303, headers={"Referrer-Policy": "no-referrer"})
    app.add_middleware(ProductionGuardMiddleware, settings=configured())
    result = TestClient(app).get("/orders/file", follow_redirects=False)
    assert result.headers["referrer-policy"] == "no-referrer"
    assert "img-src 'self' data: https://project.supabase.co;" in result.headers["content-security-policy"]
    assert "https:" not in result.headers["content-security-policy"].split("connect-src", 1)[1].split(";", 1)[0]
    assert "no-store" in result.headers["cache-control"]


def test_storage_preflight_is_read_only_and_requires_real_bucket_metadata(provider, monkeypatch):
    from app import deployment_check
    from test_deployment_check import configured as server_settings
    async def probe(backend): return True
    monkeypatch.setattr(deployment_check.PostgresStateStore, "probe", probe)
    def handle(request):
        if "/storage/v1/" in request.url.path: return provider(request)
        return httpx.Response(401 if request.headers["apikey"] == "anon-fixture" and request.method == "HEAD" else 200)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: provider.client_class(transport=httpx.MockTransport(handle), **kwargs))
    settings = server_settings(storage_provider="supabase", supabase_private_bucket="toolbako-deliveries",
                               supabase_image_bucket="toolbako-images", supabase_service_role_key="sb_secret_fixture")
    assert all(asyncio.run(deployment_check.connection_checks(settings)).values())
    assert all(request.method == "GET" for request in provider.requests)
