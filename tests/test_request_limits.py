import asyncio
import hashlib
from tempfile import SpooledTemporaryFile

import pytest
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.testclient import TestClient

from app.config import Settings
from app.middleware import ProductionGuardMiddleware
from app.request_limits import RequestBodyLimitMiddleware


def guarded_app(limit=1024):
    app = FastAPI()
    calls = []
    app.add_middleware(ProductionGuardMiddleware, settings=Settings(max_request_bytes=limit))

    @app.post('/upload')
    async def upload(file: UploadFile = File(...)):
        calls.append('upload')
        return {'size': len(await file.read())}

    @app.post('/body')
    @app.post('/webhooks/stripe')
    async def body(request: Request):
        calls.append('body')
        content = await request.body()
        return {'size': len(content), 'digest': hashlib.sha256(content).hexdigest()}

    return app, calls


def test_chunked_body_cannot_bypass_limit():
    app, calls = guarded_app(8)
    response = TestClient(app).post('/body', content=iter([b'1234', b'56789']))
    assert response.status_code == 413
    assert response.json()['error'] == 'request_too_large'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert not calls


def test_webhook_has_smaller_limit_before_handler():
    app, calls = guarded_app(52_500_000)
    response = TestClient(app).post('/webhooks/stripe', content=iter([b'x' * 500_000] * 3))
    assert response.status_code == 413
    assert not calls


def test_chunked_multipart_rejected_before_file_parser():
    app, calls = guarded_app(1024)
    chunks = [b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="test.txt"\r\n\r\n', b'x' * 2048, b'\r\n--boundary--\r\n']
    response = TestClient(app).post('/upload', content=iter(chunks), headers={'content-type': 'multipart/form-data; boundary=boundary'})
    assert response.status_code == 413
    assert not calls


def test_limit_boundary_preserves_raw_webhook_bytes():
    raw = b'{ "example" : "\\u65e5\\u672c", "lines": [1, 2] }\n'
    app, calls = guarded_app(len(raw))
    response = TestClient(app).post('/webhooks/stripe', content=iter([raw[:8], raw[8:]]))
    assert response.status_code == 200
    assert response.json() == {'size': len(raw), 'digest': hashlib.sha256(raw).hexdigest()}
    assert calls == ['body']


def test_valid_multipart_still_reaches_handler():
    app, calls = guarded_app()
    response = TestClient(app).post('/upload', files={'file': ('test.txt', b'safe fixture')})
    assert response.status_code == 200
    assert response.json() == {'size': 12}
    assert calls == ['upload']


def exercise(messages, *, headers=(), limit=8, inner=None):
    output, calls, consumed = [], [], []

    async def receive():
        if len(consumed) >= len(messages):
            raise AssertionError('Middleware read beyond the rejection point')
        message = messages[len(consumed)]
        consumed.append(message)
        return message

    async def send(message):
        output.append(message)

    async def application(scope, receive, send):
        calls.append(True)
        if inner:
            return await inner(scope, receive, send)
        chunks = []
        while True:
            message = await receive()
            chunks.append(message['body'])
            if not message.get('more_body'):
                break
        assert b''.join(chunks) == b''.join(m.get('body', b'') for m in messages)
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok'})

    scope = {'type': 'http', 'path': '/body', 'method': 'POST', 'headers': list(headers)}
    asyncio.run(RequestBodyLimitMiddleware(application, max_bytes=limit)(scope, receive, send))
    return output, calls, consumed


def chunk(body, more=False):
    return {'type': 'http.request', 'body': body, 'more_body': more}


def test_stream_stops_at_limit_without_entering_database_boundary():
    output, calls, consumed = exercise([chunk(b'1234', True), chunk(b'56789', True), chunk(b'must not read')])
    assert output[0]['status'] == 413
    assert len(consumed) == 2
    assert not calls


@pytest.mark.parametrize('headers,body,status', [
    ([(b'content-length', b'9')], b'', 413),
    ([(b'content-length', b'9' * 5000)], b'', 413),
    ([(b'content-length', b'-1')], b'', 400),
    ([(b'content-length', b'1'), (b'content-length', b'1')], b'', 400),
    ([(b'content-length', b'1, 2')], b'', 400),
    ([(b'content-length', b'2')], b'123', 400),
    ([(b'content-length', b'2')], b'1', 400),
])
def test_invalid_declared_lengths_cannot_enter_handler(headers, body, status):
    output, calls, _ = exercise([chunk(body)], headers=headers)
    assert output[0]['status'] == status
    assert not calls


@pytest.mark.parametrize('ending', ['ok', 'overflow', 'disconnect', 'handler_failure'])
def test_disk_spool_is_closed_on_all_exits(monkeypatch, ending):
    handles = []

    def temporary(**kwargs):
        handle = SpooledTemporaryFile(**kwargs)
        handles.append(handle)
        return handle

    monkeypatch.setattr('app.request_limits.SpooledTemporaryFile', temporary)
    messages = [chunk(b'x' * 1_000_001, True)]
    messages.append({'type': 'http.disconnect'} if ending == 'disconnect' else chunk(b'x' * (100 if ending == 'overflow' else 0)))

    async def fail(*args):
        raise RuntimeError('synthetic handler error')

    if ending == 'handler_failure':
        with pytest.raises(RuntimeError, match='synthetic'):
            exercise(messages, limit=1_000_010, inner=fail)
    else:
        output, calls, _ = exercise(messages, limit=1_000_010)
        if ending == 'disconnect':
            assert output[0]['status'] == 400 and not calls
        else:
            assert output[0]['status'] == (413 if ending == 'overflow' else 200)
    assert len(handles) == 1 and handles[0]._rolled and handles[0].closed


def test_nominal_fifty_megabyte_upload_is_replayed_in_bounded_chunks():
    # Synthetic bytes only; proves the delivery allowance still fits while the
    # request spool avoids holding the complete upload in RAM.
    size = 0

    async def consume(scope, receive, send):
        nonlocal size
        while True:
            message = await receive()
            assert len(message['body']) <= 65_536
            size += len(message['body'])
            if not message.get('more_body'):
                break

    messages = [chunk(b'x' * 1_000_000, True)] * 49 + [chunk(b'x' * 1_000_000)]
    exercise(messages, limit=52_500_000, inner=consume)
    assert size == 50_000_000
