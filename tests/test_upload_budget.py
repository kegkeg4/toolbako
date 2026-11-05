"""Combined product uploads fit the body budget; per-file guards stay bounded."""
import asyncio
from contextlib import ExitStack
import json
from tempfile import TemporaryFile

from fastapi import FastAPI, File, UploadFile
import httpx
import pytest

from app.config import Settings
from app.files import MAX_DELIVERY_BYTES, validate_delivery_file
from app.product_disclosures import DISCLOSURE_FIELDS
from app.request_limits import RequestBodyLimitMiddleware, WEBHOOK_BODY_LIMIT
import app.request_limits as limits


BODY_BUDGET = 57_500_000
THUMBNAIL_LIMIT = 5_000_000


def large_form_fields():
    # Four-byte UTF-8 exercises the server's character limits conservatively.
    fields = {name: "𠮷" * size for name, size in {
        "name": 60, "tagline": 100, "description_md": 5000,
        "purchase_notes": 2000, "artifact_guide": 1500,
        "tech_stack": 300, "exclusive_summary": 1000,
    }.items()}
    fields.update(tags=",".join(["𠮷" * 24] * 5),
                  demo_url="https://example.invalid/" + "a" * 2000,
                  faq_question=["𠮷" * 120] * 5, faq_answer=["𠮷" * 500] * 5)
    for key, (_label, maximum) in DISCLOSURE_FIELDS.items():
        fields["disclosure_" + key] = "𠮷" * maximum
    fields.update({key: str(value) for key, value in {
        "price": 1000000, "capacity": 50, "support_days": 365,
        "estimated_delivery_days": 120, "handover_days": 180,
        "monthly_revenue": 100000000, "monthly_profit": 100000000,
        "monthly_cost": 100000000, "weekly_ops_hours": 168,
        "exclusive_price_min": 100000000, "prohibited_agreement": "yes",
        "listing_rights_agreement": "yes", "transfer_rights_agreement": "yes",
        "fulfillment_type": "instant", "price_type": "paid", "distribution": "download",
    }.items()})
    return fields


async def call_asgi(app, *, path, chunks=(), headers=()):
    iterator = iter(chunks)
    output = []

    async def receive():
        try:
            return {"type": "http.request", "body": next(iterator), "more_body": True}
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        output.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "scheme": "https", "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(name.lower(), value) for name, value in headers], "client": ("127.0.0.1", 1234),
             "server": ("toolbako.example", 443)}
    await app(scope, receive, send)
    status = next(m["status"] for m in output if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in output if m["type"] == "http.response.body")
    return status, json.loads(body)


def test_both_maximum_files_and_large_metadata_reach_multipart_parser_once(monkeypatch):
    assert Settings().max_request_bytes == BODY_BUDGET
    spools = []
    original = limits.SpooledTemporaryFile

    def spool(**kwargs):
        result = original(**kwargs)
        spools.append(result)
        return result

    monkeypatch.setattr(limits, "SpooledTemporaryFile", spool)
    app = FastAPI()
    parsed = []

    @app.post("/tools/new")
    async def upload(artifact_file: UploadFile = File(...), thumbnail: UploadFile = File(...)):
        sizes = []
        for upload_file in (artifact_file, thumbnail):
            assert upload_file.file._rolled
            size = 0
            while chunk := await upload_file.read(65_536):
                size += len(chunk)
            sizes.append(size)
        parsed.append(sizes)
        return {"sizes": sizes}

    # Files and raw request are disk-backed. No provider or storage handler runs.
    with ExitStack() as stack:
        artifact = stack.enter_context(TemporaryFile(mode="w+b"))
        thumbnail = stack.enter_context(TemporaryFile(mode="w+b"))
        artifact.truncate(MAX_DELIVERY_BYTES)
        thumbnail.truncate(THUMBNAIL_LIMIT)
        request = httpx.Request("POST", "https://toolbako.example/tools/new",
            data=large_form_fields(), files={
                "artifact_file": ("artifact.txt", artifact, "text/plain"),
                "thumbnail": ("image.png", thumbnail, "image/png"),
            })
        declared = int(request.headers["content-length"])
        assert declared == 55_085_159
        assert 52_500_000 < declared < BODY_BUDGET
        status, result = asyncio.run(call_asgi(
            RequestBodyLimitMiddleware(app, max_bytes=Settings().max_request_bytes),
            path="/tools/new", chunks=request.stream, headers=request.headers.raw))
    assert status == 200 and result["sizes"] == [50_000_000, 5_000_000]
    assert parsed == [[50_000_000, 5_000_000]]
    assert len(spools) == 1 and spools[0]._rolled and spools[0].closed


@pytest.mark.parametrize("path", ["/tools/new", "/seller/tools/synthetic/edit"])
def test_declared_request_above_budget_stops_before_reading_body(path):
    async def forbidden(*args):
        pytest.fail("Oversized request reached file parser")

    def forbidden_chunks():
        pytest.fail("Oversized declared request body was consumed")
        yield b""

    status, result = asyncio.run(call_asgi(
        RequestBodyLimitMiddleware(forbidden, max_bytes=Settings().max_request_bytes),
        path=path, chunks=forbidden_chunks(),
        headers=[(b"content-length", str(BODY_BUDGET + 1).encode())]))
    assert status == 413 and result["error"] == "request_too_large"


def test_chunked_request_cannot_pass_new_budget(monkeypatch):
    written = []

    class CountingSpool:
        # Acceptance above already exercises the real disk spool. Rejection
        # needs only byte accounting, avoiding a second large disk write.
        def __init__(self, **kwargs):
            self.closed = False
            written.append(self)
            self.size = 0
        def __enter__(self): return self
        def __exit__(self, *args): self.closed = True
        def write(self, chunk): self.size += len(chunk)

    monkeypatch.setattr(limits, "SpooledTemporaryFile", CountingSpool)
    consumed = 0

    def chunks():
        nonlocal consumed
        block = b"x" * 65_536
        remaining = BODY_BUDGET + 1
        while remaining:
            chunk = block[:min(len(block), remaining)]
            remaining -= len(chunk)
            consumed += len(chunk)
            yield chunk
        pytest.fail("Request read beyond the first overflowing chunk")

    async def forbidden(*args): pytest.fail("Oversized chunked request reached handler")
    status, result = asyncio.run(call_asgi(
        RequestBodyLimitMiddleware(forbidden, max_bytes=Settings().max_request_bytes),
        path="/seller/tools/synthetic/edit", chunks=chunks()))
    assert status == 413 and result["error"] == "request_too_large"
    assert consumed == BODY_BUDGET + 1
    assert written[0].size <= BODY_BUDGET and written[0].closed


@pytest.mark.parametrize("declared", [False, True])
def test_webhook_limit_remains_one_megabyte(declared):
    assert WEBHOOK_BODY_LIMIT == 1_000_000
    async def forbidden(*args): pytest.fail("Oversized webhook reached handler")
    status, result = asyncio.run(call_asgi(
        RequestBodyLimitMiddleware(forbidden, max_bytes=Settings().max_request_bytes),
        path="/webhooks/stripe", chunks=[b"x" * 500_000, b"x" * 500_001],
        headers=[(b"content-length", b"1000001")] if declared else []))
    assert status == 413 and result["error"] == "request_too_large"


def test_delivery_file_size_limit_remains_fifty_decimal_megabytes():
    assert MAX_DELIVERY_BYTES == 50_000_000

    class SizedBytes(bytes):
        def __len__(self): return self.reported_size

    # Boundary-only validator probe: no huge allocation and no claim of scan.
    body = SizedBytes(b"synthetic-text")
    body.reported_size = MAX_DELIVERY_BYTES
    assert validate_delivery_file("synthetic.txt", body) == ".txt"
    body.reported_size += 1
    with pytest.raises(ValueError, match="50MB"):
        validate_delivery_file("synthetic.txt", body)


def test_thumbnail_above_five_decimal_megabytes_stops_before_decoder(monkeypatch):
    from fastapi import HTTPException
    from app import main

    class OversizedBytes(bytes):
        def __len__(self): return THUMBNAIL_LIMIT + 1

    class Upload:
        filename = "synthetic.png"
        content_type = "image/png"
        async def read(self, count):
            assert count == THUMBNAIL_LIMIT + 1
            return OversizedBytes(b"synthetic-only")

    def forbidden(*args): pytest.fail("Oversized image reached decoder or storage")
    monkeypatch.setattr(main, "encode_thumbnail", forbidden)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.save_thumbnail(Upload(), "synthetic"))
    assert caught.value.status_code == 422
    assert caught.value.detail == "商品画像は5MB以下にしてください"
