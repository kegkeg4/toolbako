"""Durable, serialized repository for the existing marketplace model.

This is a compatibility bridge, not the final normalized financial ledger.
Each dynamic request reloads committed state under a database-wide lock. The
lock stays on a dedicated connection across payment reservation checkpoints.
Only direct Postgres / session pooling is supported. Never use transaction
pooling: its session locks could be detached from the owning request.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
from uuid import uuid4
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .persistence import STATE_FIELDS, decode_value, encode_value

SCHEMA_VERSION = 2
LOCK_ID = 7365419001298
logger = logging.getLogger("toolbako.database")

DATABASE_FAILURE_CODES = frozenset({
    "unavailable", "configuration_invalid", "authentication_failed",
    "password_not_supplied", "pooler_target_not_found", "pooler_unavailable",
    "unsupported_startup_parameter", "database_not_found", "connections_exhausted",
    "database_not_ready", "connection_timeout", "dns_resolution_failed",
    "tls_connection_failed", "network_connection_failed", "connection_failed",
    "schema_missing", "schema_access_denied", "schema_version_mismatch",
    "query_timeout", "schema_probe_failed",
})


def database_failure_code(exc: Exception, *, phase: str) -> str:
    """Classify in memory; export only a fixed code, never a provider message.

    libpq connection errors can lack SQLSTATE. Known message signatures are a
    fallback diagnostic, not proof that a particular credential is incorrect.
    """
    try:
        state = getattr(exc, "sqlstate", None)
        codes = {
            "28000": "authentication_failed", "28P01": "authentication_failed",
            "3D000": "database_not_found", "53300": "connections_exhausted",
            "57P03": "database_not_ready", "42P01": "schema_missing",
            "3F000": "schema_missing", "42501": "schema_access_denied",
            "57014": "query_timeout",
        }
        if state in codes:
            return codes[state]
        if phase == "connect":
            message = str(exc).lower()
            signatures = (
                ("pooler_target_not_found", ("tenant or user not found",)),
                ("pooler_unavailable", ("circuit breaker open",)),
                ("unsupported_startup_parameter", ("unsupported startup parameter",)),
                ("password_not_supplied", ("no password supplied",)),
                ("authentication_failed", ("password authentication failed", "sasl authentication failed")),
                ("dns_resolution_failed", ("could not translate host name", "failed to resolve host", "name or service not known", "nodename nor servname provided")),
                ("connection_timeout", ("connection timed out", "timeout expired", "connection timeout")),
                ("tls_connection_failed", ("certificate verify failed", "ssl error", "server does not support ssl")),
                ("network_connection_failed", ("connection refused", "network is unreachable", "no route to host")),
            )
            for code, matches in signatures:
                if any(signature in message for signature in matches):
                    return code
    except Exception:
        pass
    return "connection_failed" if phase == "connect" else "schema_probe_failed"


class StorageUnavailable(Exception):
    """Deliberately contains no connection URL, provider payload or credential."""

    def __init__(self, message="Storage unavailable", *, diagnostic_code="unavailable"):
        super().__init__(message)
        self.diagnostic_code = diagnostic_code if isinstance(diagnostic_code, str) and diagnostic_code in DATABASE_FAILURE_CODES else "unavailable"


class OperationConflict(Exception):
    pass


def snapshot(store) -> dict:
    with store._lock:
        return encode_value({field: getattr(store, field) for field in STATE_FIELDS})


def restore(store, payload: dict) -> None:
    if set(payload) != set(STATE_FIELDS):
        raise StorageUnavailable("Database state needs a compatible migration")
    state = decode_value(payload)
    with store._lock:
        for field in STATE_FIELDS:
            setattr(store, field, state[field])


def content_hash(value) -> str:
    data = json.dumps(encode_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


class PostgresStateStore:
    def __init__(self, dsn: str, *, production: bool = False):
        self.dsn = dsn
        self.production = production
        self._local_lock = asyncio.Lock()
        self._connection = None
        self._store = None
        self._checkpoint = None
        self._revision = None
        self._audit_hashes = {}
        self._emails = []
        self._last_finance_hash = None

    async def connect(self):
        try:
            from psycopg import AsyncConnection
            from psycopg.conninfo import conninfo_to_dict
            config = conninfo_to_dict(self.dsn)
            # The well-known Supabase transaction pooler endpoint is unsafe for
            # session-level locking. Custom poolers also MUST use session mode.
            if not self.dsn or config.get("port") == "6543":
                raise ValueError("unsupported database connection")
            if self.production and config.get("sslmode") not in {"require", "verify-ca", "verify-full"}:
                raise ValueError("TLS is required")
        except Exception:
            raise StorageUnavailable("Database connection unavailable or unsafe", diagnostic_code="configuration_invalid") from None
        try:
            return await AsyncConnection.connect(
                self.dsn, autocommit=True, connect_timeout=5,
                prepare_threshold=None, options="-c statement_timeout=10000",
            )
        except Exception as exc:
            raise StorageUnavailable("Database connection unavailable or unsafe", diagnostic_code=database_failure_code(exc, phase="connect")) from None

    async def _check_schema(self, conn):
        cursor = await conn.execute("select version from toolbako_runtime.schema_version where singleton")
        row = await cursor.fetchone()
        if not row or row[0] != SCHEMA_VERSION:
            raise StorageUnavailable("Database migration required", diagnostic_code="schema_version_mismatch")

    async def probe(self) -> bool:
        try:
            async with await self.connect() as conn:
                await self._check_schema(conn)
            return True
        except Exception as exc:
            code = exc.diagnostic_code if isinstance(exc, StorageUnavailable) else database_failure_code(exc, phase="schema")
            # Never log the exception, traceback, SQLSTATE detail, URL or secret.
            code = code if isinstance(code, str) and code in DATABASE_FAILURE_CODES else "unavailable"
            logger.warning("database_probe_failed reason=%s", code)
            return False

    @asynccontextmanager
    async def request(self, store):
        acquired = False
        conn = None
        try:
            await asyncio.wait_for(self._local_lock.acquire(), timeout=5)
            acquired = True
            conn = await self.connect()
            cursor = await conn.execute("select pg_try_advisory_lock(%s)", (LOCK_ID,))
            if not (await cursor.fetchone())[0]:
                raise StorageUnavailable("Database is busy; retry later")
            # The advisory lock must be acquired in an earlier statement. Do
            # not put it in this SELECT: PostgreSQL may evaluate its reads
            # before the lock expression. Schema and state, however, can be
            # read together, saving a network round trip on every navigation.
            cursor = await conn.execute(
                "select v.version, s.revision, s.payload "
                "from toolbako_runtime.schema_version v "
                "left join toolbako_runtime.marketplace_state s on s.singleton "
                "where v.singleton"
            )
            row = await cursor.fetchone()
            if not row or row[0] != SCHEMA_VERSION:
                raise StorageUnavailable("Database migration required", diagnostic_code="schema_version_mismatch")
            if row[1] is None:
                # Never silently copy a local/demo instance into production.
                from .data import DemoStore
                payload = snapshot(DemoStore(seed=False))
                await conn.execute(
                    "insert into toolbako_runtime.marketplace_state(singleton, payload) values (true, %s::jsonb)",
                    (json.dumps(payload),),
                )
                row = (SCHEMA_VERSION, 0, payload)
            self._revision, self._checkpoint = row[1:]
            restore(store, self._checkpoint)
            # JSON encodes sets as arrays. Another Python worker may have a
            # different iteration order; compare with this worker's encoding
            # of the freshly loaded state, not the other worker's array order.
            self._checkpoint = snapshot(store)
            self._audit_hashes = {event["id"]: content_hash(event) for event in store.audit_logs}
            self._connection, self._store = conn, store
            self._emails = []
        except BaseException as exc:
            if conn:
                await conn.close()
            if acquired:
                self._local_lock.release()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise StorageUnavailable("Database request could not start") from None
        try:
            yield
        finally:
            # A failed request must not leave dirty objects available to another
            # request. A pre-provider checkpoint remains durable intentionally.
            restore(store, self._checkpoint)
            self._connection = self._store = None
            self._emails = []
            try:
                await conn.close()  # Releases the session advisory lock.
            finally:
                self._local_lock.release()

    async def _save(self, conn, *, payload=None):
        from .finance import sync_finance
        payload = snapshot(self._store) if payload is None else payload
        finance_hash = content_hash((payload["orders"], payload["payouts"]))
        # Ordinary navigation/session refresh must not issue per-sale SQL for
        # the whole ledger. The cache is accepted ONLY after a DB commit.
        if finance_hash != self._last_finance_hash:
            await sync_finance(conn, self._store)
        for email in self._emails:
            await conn.execute(
                "insert into toolbako_runtime.email_outbox(id,recipient,sender,subject,body) "
                "values(%s,%s,%s,%s,%s) on conflict(id) do nothing", email,
            )
        for event in self._store.audit_logs:
            digest = content_hash(event)
            if event["id"] in self._audit_hashes:
                if self._audit_hashes[event["id"]] != digest:
                    raise OperationConflict("An existing audit event was modified")
                continue
            cursor = await conn.execute(
                "insert into toolbako_runtime.audit_events "
                "(id, actor_id, action, target, request_id, occurred_at, content_hash) "
                "values (%s,%s,%s,%s,%s,%s,%s) on conflict (id) do nothing returning id",
                (event["id"], event.get("user_id"), event["action"], event["target"],
                 event.get("request_id", ""), event["created_at"], digest),
            )
            if not await cursor.fetchone():
                cursor = await conn.execute("select content_hash from toolbako_runtime.audit_events where id=%s", (event["id"],))
                if (await cursor.fetchone())[0] != digest:
                    raise OperationConflict("Audit event ID conflict")
        cursor = await conn.execute(
            "update toolbako_runtime.marketplace_state set payload=%s::jsonb, revision=revision+1, updated_at=now() "
            "where singleton and revision=%s returning revision",
            (json.dumps(payload), self._revision),
        )
        row = await cursor.fetchone()
        if not row:
            raise OperationConflict("Concurrent state update detected")
        return row[0], payload

    def _accept_commit(self, committed):
        self._revision, self._checkpoint = committed
        self._last_finance_hash = content_hash((self._checkpoint["orders"], self._checkpoint["payouts"]))
        self._audit_hashes = {event["id"]: content_hash(event) for event in self._store.audit_logs}
        self._emails = []

    def enqueue_email(self, recipient: str, sender: str, subject: str, body: str):
        if self._connection is None:
            raise StorageUnavailable("A database request boundary is required")
        self._emails.append((str(uuid4()), recipient, sender, subject, body))

    async def save(self):
        if self._connection is None:
            raise StorageUnavailable("A database request boundary is required")
        try:
            payload = snapshot(self._store)
            finance_hash = content_hash((payload["orders"], payload["payouts"]))
            if (payload == self._checkpoint and not self._emails
                    and finance_hash == self._last_finance_hash):
                # This is NOT a GET/write route shortcut or a stale page cache.
                # Each request still loads fresh committed state under the
                # database lock. Expiry, auth, audit and financial mutations
                # anywhere in STATE_FIELDS and an outbox-only change must save.
                # The first save on each worker still verifies the subledger.
                return
            async with self._connection.transaction():
                committed = await self._save(self._connection, payload=payload)
            self._accept_commit(committed)
        except Exception:
            raise StorageUnavailable("Database commit failed") from None

    async def begin_operation(self, operation_id: str, endpoint: str, data: dict):
        """Commit both the business reservation and Stripe intent BEFORE I/O.

        Completed attempts are replayed; unknown/pending attempts are held for
        reconciliation, not automatically retried after Stripe's key TTL.
        """
        from .integrations import StripeOutcomeUnknown
        conn = self._connection
        if conn is None:
            raise StorageUnavailable("A database request boundary is required")
        digest = content_hash(data)
        cursor = await conn.execute(
            "select endpoint,request_hash,status,response from toolbako_runtime.stripe_operations where operation_id=%s",
            (operation_id,),
        )
        row = await cursor.fetchone()
        if row:
            if row[0] != endpoint or row[1] != digest:
                raise OperationConflict("Stripe idempotency key was reused with changed parameters")
            if row[2] == "succeeded":
                return row[3]
            if row[2] == "rejected":
                raise RuntimeError("This Stripe operation was rejected; contact support")
            raise StripeOutcomeUnknown(operation_id)
        async with conn.transaction():
            await conn.execute(
                "insert into toolbako_runtime.stripe_operations(operation_id,endpoint,request_hash,status) values(%s,%s,%s,'pending')",
                (operation_id, endpoint, digest),
            )
            committed = await self._save(conn)
        self._accept_commit(committed)
        return None

    async def operation_result(self, operation_id: str):
        if self._connection is None:
            raise StorageUnavailable("A database request boundary is required")
        cursor = await self._connection.execute(
            "select status,response from toolbako_runtime.stripe_operations where operation_id=%s", (operation_id,),
        )
        row = await cursor.fetchone()
        return {"status": row[0], "response": row[1]} if row else None

    async def operation_details(self, operation_id: str):
        if self._connection is None:
            raise StorageUnavailable("A database request boundary is required")
        row = await (await self._connection.execute(
            "select endpoint,request_hash,status,created_at from toolbako_runtime.stripe_operations where operation_id=%s",
            (operation_id,),
        )).fetchone()
        return dict(zip(("endpoint", "request_hash", "status", "created_at"), row)) if row else None

    async def reconcile_operation(self, operation_id: str, *, endpoint: str, request_hash: str, response: dict):
        """Commit verified recovery, business state and audit in one transaction."""
        if self._connection is None or not isinstance(response.get("id"), str):
            raise OperationConflict("Invalid reconciliation boundary")
        try:
            async with self._connection.transaction():
                row = await (await self._connection.execute(
                    "update toolbako_runtime.stripe_operations set status='succeeded',response=%s::jsonb,updated_at=now() "
                    "where operation_id=%s and endpoint=%s and request_hash=%s and status in ('pending','unknown') returning operation_id",
                    (json.dumps(response), operation_id, endpoint, request_hash),
                )).fetchone()
                if not row:
                    raise OperationConflict("Operation changed during reconciliation")
                committed = await self._save(self._connection)
            self._accept_commit(committed)
        except Exception:
            raise StorageUnavailable("Reconciliation was not committed") from None

    async def finish_operation(self, operation_id: str, status: str, response: dict | None = None):
        if status not in {"succeeded", "unknown", "rejected"} or (status == "succeeded") != (response is not None):
            raise ValueError("Invalid operation outcome")
        cursor = await self._connection.execute(
            "update toolbako_runtime.stripe_operations set status=%s,response=%s::jsonb,updated_at=now() "
            "where operation_id=%s and status='pending' returning operation_id",
            (status, json.dumps(response) if response is not None else None, operation_id),
        )
        if not await cursor.fetchone():
            raise OperationConflict("Stripe operation is not pending")


class DatabaseBoundaryMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, backend, store):
        super().__init__(app)
        self.backend, self.store = backend, store

    async def dispatch(self, request, call_next):
        if request.url.path.startswith("/static/") or request.url.path in {"/healthz", "/readyz", "/deploymentz"}:
            return await call_next(request)
        if self.backend is None:
            return self.unavailable()
        try:
            async with self.backend.request(self.store):
                response = await call_next(request)
                if response.status_code < 500:
                    # GET also revokes expired sessions / performs due lifecycle
                    # transitions. Commit before returning response headers.
                    await self.backend.save()
                return response
        except Exception:
            logger.warning("Database request failed; no success response was sent")
            return self.unavailable()

    @staticmethod
    def unavailable():
        return JSONResponse(
            {"error": "storage_unavailable", "message": "保存先を確認中です。操作を繰り返さず、時間をおいて再読み込みしてください。"},
            status_code=503, headers={"Retry-After": "30", "Cache-Control": "no-store"},
        )


async def migrate(backend):
    async with await backend.connect() as conn:
        async with conn.transaction():
            await conn.execute("select pg_advisory_xact_lock(%s)", (LOCK_ID,))
            await conn.execute(Path(__file__).with_name("runtime_schema.sql").read_text())


def main():
    parser = argparse.ArgumentParser(description="Toolbako private runtime database (never imports demo data)")
    parser.add_argument("command", choices=("migrate", "check"))
    args = parser.parse_args()
    from .config import settings
    backend = PostgresStateStore(settings.database_url, production=settings.is_production)
    try:
        if args.command == "migrate":
            asyncio.run(migrate(backend))
            print("Runtime schema applied. No demo data was imported. Live payments remain blocked.")
        else:
            ok = asyncio.run(backend.probe())
            print("Runtime database: ready" if ok else "Runtime database: unavailable or migration missing")
            raise SystemExit(0 if ok else 1)
    except Exception:
        # psycopg/libpq exceptions may contain connection details. Do not print.
        print("Database operation failed. Check connection, TLS, session pooling and migration permissions.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
