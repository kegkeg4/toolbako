"""Interrupted synthetic uploads: no external services or customer files."""
import asyncio
import functools
import http.client
import json
import socket
import threading
import time
from contextlib import closing
from tempfile import SpooledTemporaryFile

import pytest
import uvicorn

from app.config import Settings
from app.middleware import ProductionGuardMiddleware
from app.request_limits import RequestBodyLimitMiddleware


def scope(path='/upload', version='1.1'):
    return {'type': 'http', 'path': path, 'method': 'POST', 'headers': [],
            'http_version': version, 'state': {'request_id': 'synthetic-request'}}


def chunk(body=b'x', more=True):
    return {'type': 'http.request', 'body': body, 'more_body': more}


def track_spools(monkeypatch):
    handles = []
    def temporary(**kwargs):
        handle = SpooledTemporaryFile(**kwargs)
        handles.append(handle)
        return handle
    monkeypatch.setattr('app.request_limits.SpooledTemporaryFile', temporary)
    return handles


async def forbidden(*args):
    raise AssertionError('An incomplete upload reached the database/handler')


def test_stalled_partial_upload_closes_disk_spool(monkeypatch):
    handles = track_spools(monkeypatch)
    output = []
    reads = 0
    cancelled = False
    async def receive():
        nonlocal reads, cancelled
        reads += 1
        if reads == 1:
            return chunk(b'x' * 1_000_001)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled = True
    async def send(message):
        output.append(message)
    middleware = RequestBodyLimitMiddleware(forbidden, max_bytes=2_000_000, idle_timeout=.05)
    asyncio.run(asyncio.wait_for(middleware(scope(), receive, send), 3))
    assert output[0]['status'] == 408 and cancelled
    assert dict(output[0]['headers'])[b'connection'] == b'close'
    assert dict(output[0]['headers'])[b'cache-control'] == b'no-store'
    assert json.loads(output[1]['body']) == {
        'error': 'request_body_timeout', 'request_id': 'synthetic-request'}
    assert len(handles) == 1 and handles[0]._rolled and handles[0].closed


@pytest.mark.parametrize('path,frames,expected_reads', [
    # Every chunk arrives within the idle limit, but the total budget expires.
    ('/upload', [(20, b'x'), (40, b'x'), (61, b'x')], 3),
    # Empty frames do not extend the idle deadline.
    ('/upload', [(10, b''), (20, b''), (31, b'')], 3),
    # Webhooks have the stricter 30-second total limit.
    ('/webhooks/stripe', [(20, b'x'), (31, b'x')], 2),
])
def test_progress_cannot_extend_absolute_or_idle_deadlines(monkeypatch, path, frames, expected_reads):
    now = [0]
    monkeypatch.setattr('app.request_limits.monotonic', lambda: now[0])
    output, received = [], []
    async def receive():
        at, body = frames[len(received)]
        now[0] = at
        received.append(body)
        return chunk(body)
    async def send(message):
        output.append(message)
    app = RequestBodyLimitMiddleware(forbidden, max_bytes=100, idle_timeout=30, total_timeout=60)
    asyncio.run(app(scope(path), receive, send))
    assert output[0]['status'] == 408
    assert len(received) == expected_reads


def test_finished_body_does_not_timeout_the_business_handler(monkeypatch):
    now = [0]
    monkeypatch.setattr('app.request_limits.monotonic', lambda: now[0])
    output = []
    async def receive():
        return chunk(b'complete', False)
    async def handler(scope, receive, send):
        assert (await receive())['body'] == b'complete'
        now[0] = 60  # Business work exceeds the body budget after spooling.
        await send({'status': 200})
    async def send(message):
        output.append(message)
    app = RequestBodyLimitMiddleware(handler, max_bytes=100, idle_timeout=.03, total_timeout=.03)
    asyncio.run(app(scope(), receive, send))
    assert output == [{'status': 200}]


def test_expired_after_spooling_never_enters_handler(monkeypatch):
    now = [0]
    monkeypatch.setattr('app.request_limits.monotonic', lambda: now[0])
    async def spool(operation, *args):
        operation(*args)
        now[0] = 61
    monkeypatch.setattr('app.request_limits.run_in_threadpool', spool)
    output = []
    async def receive():
        return chunk(b'complete', False)
    async def send(message):
        output.append(message)
    app = RequestBodyLimitMiddleware(forbidden, max_bytes=100, total_timeout=60)
    asyncio.run(app(scope(), receive, send))
    assert output[0]['status'] == 408


def test_server_cancellation_is_not_converted_into_timeout(monkeypatch):
    handles = track_spools(monkeypatch)
    output = []
    async def receive():
        raise asyncio.CancelledError
    async def send(message):
        output.append(message)
    app = RequestBodyLimitMiddleware(forbidden, max_bytes=100)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app(scope(), receive, send))
    assert not output and handles[0].closed


def test_http2_timeout_has_no_connection_specific_header():
    output = []
    async def receive():
        await asyncio.Event().wait()
    async def send(message):
        output.append(message)
    app = RequestBodyLimitMiddleware(forbidden, max_bytes=100, idle_timeout=.01)
    asyncio.run(app(scope(version='2'), receive, send))
    assert output[0]['status'] == 408
    assert b'connection' not in dict(output[0]['headers'])


@pytest.mark.parametrize('value', [0, -1, float('inf'), float('nan')])
def test_time_limits_cannot_be_disabled(value):
    for option in ['idle_timeout', 'total_timeout']:
        with pytest.raises(ValueError):
            RequestBodyLimitMiddleware(forbidden, max_bytes=100, **{option: value})


@pytest.fixture
def local_server(monkeypatch):
    # Real HTTP/1 sockets exercise Uvicorn + BaseHTTPMiddleware cancellation.
    # Short test budgets avoid a deliberate slow request to any deployed site.
    monkeypatch.setattr('app.middleware.RequestBodyLimitMiddleware', functools.partial(
        RequestBodyLimitMiddleware, idle_timeout=.15, total_timeout=.4))
    calls = []
    async def handler(scope, receive, send):
        body = bytearray()
        while True:
            message = await receive()
            body.extend(message.get('body', b''))
            if not message.get('more_body'):
                break
        calls.append(bytes(body))
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'application/json')]})
        await send({'type': 'http.response.body', 'body': b'{"ok":true}'})
    app = ProductionGuardMiddleware(handler, Settings(environment='development', max_request_bytes=2_000_000))
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, lifespan='off', ws='none', access_log=False, log_level='error'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started
        yield port, calls
    finally:
        server.should_exit = True
        thread.join(5)
        listener.close()
        assert not thread.is_alive(), 'The temporary local HTTP server did not stop'


def test_real_http_stalled_upload_closes_connection_and_next_request_succeeds(local_server):
    port, calls = local_server
    with socket.create_connection(('127.0.0.1', port), timeout=3) as connection:
        connection.sendall(b'POST /upload HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\n\r\npartial')
        response = http.client.HTTPResponse(connection)
        response.begin()
        assert response.status == 408
        assert response.getheader('Connection') == 'close'
        assert response.getheader('X-Content-Type-Options') == 'nosniff'
        assert json.loads(response.read())['error'] == 'request_body_timeout'
        assert connection.recv(1) == b''
    assert not calls
    with closing(http.client.HTTPConnection('127.0.0.1', port, timeout=3)) as client:
        client.request('GET', '/healthz')
        response = client.getresponse()
        assert response.status == 200 and response.read() == b'{"ok":true}'
    assert calls == [b'']


def test_real_http_chunked_upload_does_not_reset_total_timeout(local_server):
    port, calls = local_server
    with socket.create_connection(('127.0.0.1', port), timeout=3) as connection:
        connection.sendall(b'POST /upload HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n')
        # Stop writing before the server closes at .4s. Sending again after
        # its 408 can reset the socket on Windows and discard the response.
        for _ in range(6):
            try:
                connection.sendall(b'1\r\nx\r\n')
            except OSError:
                break
            time.sleep(.07)
        response = http.client.HTTPResponse(connection)
        response.begin()
        assert response.status == 408
        assert json.loads(response.read())['error'] == 'request_body_timeout'
    assert not calls


def test_real_http_upload_disconnect_does_not_reach_handler(local_server, caplog):
    port, calls = local_server
    with socket.create_connection(('127.0.0.1', port), timeout=3) as connection:
        connection.sendall(b'POST /upload HTTP/1.1\r\nHost: localhost\r\nContent-Length: 10\r\n\r\nshort')
    with closing(http.client.HTTPConnection('127.0.0.1', port, timeout=3)) as client:
        client.request('GET', '/healthz')
        response = client.getresponse()
        assert response.status == 200 and response.read()
    assert calls == [b'']
    assert not [record for record in caplog.records if record.levelno >= 40]
