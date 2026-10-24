"""Opt-in real local ClamAV tests. No cloud, customer files or actual malware.

Start a loopback-only clamd with official definitions and AlertExceedsMax yes,
then set TEST_CLAMAV_PORT. Tests keep the harmless EICAR sample in memory.
"""
import asyncio
import io
import os
import socket
import zipfile

import httpx
import pytest
from fastapi import HTTPException, UploadFile
from starlette.requests import Request

from app.config import Settings
from app.data import DemoStore
from app.files import MAX_DELIVERY_BYTES, clamav_scan, validate_delivery_file
from test_finance import order as make_order


@pytest.fixture(scope="module")
def scanner_port():
    value = os.getenv("TEST_CLAMAV_PORT")
    if not value:
        pytest.skip("Set TEST_CLAMAV_PORT for a disposable loopback-only ClamAV")
    port = int(value)
    assert 1024 <= port <= 65535
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.sendall(b"zPING\0")
        assert client.recv(32) == b"PONG\0"
    return port


def eicar():
    # The standard harmless test string, never executed or written to disk.
    # https://www.eicar.org/download-anti-malware-testfile/
    return rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


def archived(content, levels=1):
    for depth in range(levels):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("sample.txt" if depth == 0 else "nested.zip", content)
        content = out.getvalue()
    return content


@pytest.mark.parametrize("kind", ["plain", "zip", "nested_zip"])
def test_real_scanner_detects_harmless_eicar(scanner_port, kind):
    content = eicar() if kind == "plain" else archived(eicar(), 2 if kind == "nested_zip" else 1)
    assert clamav_scan(content, "127.0.0.1", scanner_port) == (False, "clamav:malware_detected")


@pytest.mark.parametrize("size", [16, MAX_DELIVERY_BYTES])
def test_real_scanner_accepts_clean_text_through_50mb_boundary(scanner_port, size):
    content = b"a" * size
    assert validate_delivery_file("fixture.txt", content) == ".txt"
    assert clamav_scan(content, "127.0.0.1", scanner_port) == (True, "clamav")


def test_real_scanner_blocks_recursion_limit_instead_of_reporting_clean(scanner_port):
    assert clamav_scan(archived(b"harmless fixture", 25), "127.0.0.1", scanner_port) == (False, "clamav:malware_detected")


@pytest.mark.parametrize("malware", [False, True])
def test_delivery_handler_uses_real_scan_before_any_storage_write(scanner_port, monkeypatch, malware):
    import app.main as main
    store = DemoStore(seed=False)
    store.orders.append(make_order(id="o", status="in_progress", revision_count=0, payment_status="paid"))
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "settings", Settings(
        demo_mode=False, clamav_host="127.0.0.1", clamav_port=scanner_port,
        storage_provider="supabase", supabase_url="https://project.supabase.co",
        supabase_service_role_key="sb_secret_fixture", supabase_private_bucket="toolbako-deliveries",
        supabase_image_bucket="toolbako-images",
    ))
    monkeypatch.setattr(main, "current_user", lambda request: {"id": "seller"})
    writes = []

    async def upload(self, key, content):
        writes.append(content)
        return {"storage_backend": "supabase", "bucket": "toolbako-deliveries", "object_key": key}

    async def no_external_requests(*args, **kwargs):
        pytest.fail("Integration test must not contact a cloud provider")

    monkeypatch.setattr(main.SupabaseObjectStorage, "upload", upload)
    monkeypatch.setattr(httpx.AsyncClient, "request", no_external_requests)
    content = eicar() if malware else b"Harmless delivery fixture."
    request = Request({"type": "http", "method": "POST", "path": "/orders/o/deliver", "headers": []})
    async def run():
        with io.BytesIO(content) as stream:
            return await main.deliver_order(request, "o", "検査用の架空納品です。", UploadFile(stream, filename="sample.txt"))

    if malware:
        with pytest.raises(HTTPException) as failure:
            asyncio.run(run())
        assert failure.value.status_code == 422
        assert not writes and store.orders[0]["status"] == "in_progress"
        assert not store.orders[0].get("delivery")
    else:
        assert asyncio.run(run()).status_code == 303
        assert writes == [content]
        record = store.orders[0]["delivery"]["files"][0]
        assert record["scan_engine"] == "clamav" and record["scan_status"] == "passed"


def test_delivery_handler_refuses_unavailable_local_scanner_without_storage_write(monkeypatch):
    import app.main as main
    store = DemoStore(seed=False)
    store.orders.append(make_order(id="o", status="in_progress", revision_count=0, payment_status="paid"))
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "current_user", lambda request: {"id": "seller"})

    async def forbidden(*args, **kwargs):
        pytest.fail("An unscanned delivery must never reach storage or a cloud provider")

    monkeypatch.setattr(main.SupabaseObjectStorage, "upload", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    # Reserve a local port without listening, so no unrelated service can own it.
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        monkeypatch.setattr(main, "settings", Settings(
            demo_mode=False, clamav_host="127.0.0.1", clamav_port=unavailable.getsockname()[1],
        ))
        request = Request({"type": "http", "method": "POST", "path": "/orders/o/deliver", "headers": []})
        with io.BytesIO(b"Harmless delivery fixture.") as stream:
            with pytest.raises(HTTPException) as failure:
                asyncio.run(main.deliver_order(request, "o", "検査用の架空納品です。", UploadFile(stream, filename="sample.txt")))
        assert failure.value.status_code == 503
    assert store.orders[0]["status"] == "in_progress" and not store.orders[0].get("delivery")
