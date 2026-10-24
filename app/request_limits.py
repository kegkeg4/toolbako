"""Bound request bodies and stalled uploads before database transactions run."""
import math
from tempfile import SpooledTemporaryFile
from time import monotonic

import anyio

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse


WEBHOOK_BODY_LIMIT = 1_000_000
MEMORY_BODY_LIMIT = 1_000_000
REPLAY_CHUNK_BYTES = 65_536
BODY_IDLE_TIMEOUT_SECONDS = 30
BODY_TIMEOUT_SECONDS = 300
WEBHOOK_BODY_TIMEOUT_SECONDS = 30


class RequestBodyLimitMiddleware:
    def __init__(self, app, *, max_bytes, idle_timeout=BODY_IDLE_TIMEOUT_SECONDS,
                 total_timeout=BODY_TIMEOUT_SECONDS):
        if any(not math.isfinite(value) or value <= 0 for value in (idle_timeout, total_timeout)):
            raise ValueError('Request body time limits must be positive and finite')
        self.app = app
        self.max_bytes = max_bytes
        self.idle_timeout = idle_timeout
        self.total_timeout = total_timeout

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        limit = self.max_bytes
        total_timeout = self.total_timeout
        if scope['path'].startswith('/webhooks/'):
            limit = min(limit, WEBHOOK_BODY_LIMIT)
            total_timeout = min(total_timeout, WEBHOOK_BODY_TIMEOUT_SECONDS)

        async def reject(status, error):
            headers = {'Cache-Control': 'no-store'}
            if status == 408 and scope.get('http_version', '1.1') in {'1.0', '1.1'}:
                # Unread request bytes must not become a subsequent HTTP/1 request.
                headers['Connection'] = 'close'
            response = JSONResponse(
                {'error': error, 'request_id': scope.get('state', {}).get('request_id', '')},
                status_code=status, headers=headers,
            )
            await response(scope, receive, send)

        lengths = [value for key, value in scope['headers'] if key.lower() == b'content-length']
        declared = None
        if lengths:
            value = lengths[0]
            # Reject ambiguous framing and avoid parsing unbounded integers.
            if len(lengths) != 1 or not value or not value.isdigit():
                return await reject(400, 'invalid_content_length')
            normalized = value.lstrip(b'0') or b'0'
            if len(normalized) > len(str(limit)) or int(normalized) > limit:
                return await reject(413, 'request_too_large')
            declared = int(normalized)

        # Keep small forms in memory; larger uploads spill to an automatically
        # removed private temporary file. Do not hold a DB lock during upload.
        with SpooledTemporaryFile(max_size=MEMORY_BODY_LIMIT, mode='w+b') as body:
            size = 0
            started = monotonic()
            deadline = started + total_timeout
            idle_deadline = started + self.idle_timeout
            while True:
                remaining = min(deadline, idle_deadline) - monotonic()
                if remaining <= 0:
                    return await reject(408, 'request_body_timeout')
                try:
                    with anyio.fail_after(remaining):
                        message = await receive()
                except TimeoutError:
                    return await reject(408, 'request_body_timeout')
                if message['type'] == 'http.disconnect':
                    # The server discards this response on a closed connection.
                    # Still complete the ASGI exchange so BaseHTTPMiddleware
                    # does not turn a client disconnect into a server error.
                    return await reject(400, 'request_disconnected')
                received_at = monotonic()
                if received_at >= min(deadline, idle_deadline):
                    return await reject(408, 'request_body_timeout')
                chunk = message.get('body', b'')
                size += len(chunk)
                if size > limit:
                    return await reject(413, 'request_too_large')
                if declared is not None and size > declared:
                    return await reject(400, 'content_length_mismatch')
                if chunk:
                    # Empty progress frames cannot keep an upload alive forever.
                    idle_deadline = received_at + self.idle_timeout
                    await run_in_threadpool(body.write, chunk)
                if not message.get('more_body', False):
                    break
            if monotonic() >= deadline:
                return await reject(408, 'request_body_timeout')
            if declared is not None and size != declared:
                return await reject(400, 'content_length_mismatch')
            await run_in_threadpool(body.seek, 0)
            remaining = size
            replayed = False

            async def replay():
                nonlocal remaining, replayed
                if replayed:
                    return await receive()
                chunk = await run_in_threadpool(body.read, min(remaining, REPLAY_CHUNK_BYTES))
                remaining -= len(chunk)
                replayed = remaining == 0
                return {'type': 'http.request', 'body': chunk, 'more_body': not replayed}

            await self.app(scope, replay, send)
