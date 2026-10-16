"""Bound request bodies before parsers or database transactions can run."""
from tempfile import SpooledTemporaryFile

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse


WEBHOOK_BODY_LIMIT = 1_000_000
MEMORY_BODY_LIMIT = 1_000_000
REPLAY_CHUNK_BYTES = 65_536


class RequestBodyLimitMiddleware:
    def __init__(self, app, *, max_bytes):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        limit = self.max_bytes
        if scope['path'].startswith('/webhooks/'):
            limit = min(limit, WEBHOOK_BODY_LIMIT)

        async def reject(status, error):
            response = JSONResponse(
                {'error': error, 'request_id': scope.get('state', {}).get('request_id', '')},
                status_code=status,
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
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                chunk = message.get('body', b'')
                size += len(chunk)
                if size > limit:
                    return await reject(413, 'request_too_large')
                if declared is not None and size > declared:
                    return await reject(400, 'content_length_mismatch')
                if chunk:
                    await run_in_threadpool(body.write, chunk)
                if not message.get('more_body', False):
                    break
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
