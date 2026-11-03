"""Held-lock, read-only export boundary for the designated OLD runtime DB.

This does not stop workers, enable maintenance, authorize a migration, import
main, initialize a snapshot, bind a scope or write rows. HTTP maintenance and
the external mail/manual/schema/provider writer inventory must be independently
reviewed FIRST and remain stopped throughout the context. The supplied evidence
hash records that precondition; this code cannot prove it from a caller claim.

The common advisory lock excludes cooperating writers only. Independent READ
COMMITTED observations fingerprint every row of all nine tables, including
journal/outbox writes that do not change the marketplace revision. They detect
observed drift, not unobserved change-and-revert or external Auth/Storage/Stripe
activity. Keep the context alive through any separately approved target commit;
an export result must not be treated as a durable permission to migrate later.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import math
import os
import re
import time

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.pq import TransactionStatus

from .database import LOCK_ID, SCHEMA_VERSION
from .live_database_config import _verified_ca_path
from .live_dataset import value_hash
from .live_dataset_import import TABLES, _schema_signature
from .persistence import STATE_FIELDS, decode_value


SOURCE_PROJECT = "xnjzgfaasbemffgorvnj"
SOURCE_ENDPOINTS = frozenset({
    (f"db.{SOURCE_PROJECT}.supabase.co", "postgres"),
    ("aws-0-ap-northeast-1.pooler.supabase.com", f"postgres.{SOURCE_PROJECT}"),
})
_HASH = re.compile(r"[a-f0-9]{64}")
_TOKEN = object()


class SourceDrainError(RuntimeError):
    def __init__(self, code):
        allowed = {"configuration_invalid", "precondition_required", "identity_mismatch",
                   "schema_mismatch", "source_uninitialized", "unsettled_work", "lock_busy",
                   "lock_lost", "source_changed", "manifest_stale", "boundary_inactive",
                   "export_failed", "export_mutated", "read_failed", "cleanup_failed"}
        self.code = code if code in allowed else "read_failed"
        super().__init__("live_source_drain:" + self.code)


def _require(value, code):
    if not value:
        raise SourceDrainError(code)


def _is_hash(value):
    return type(value) is str and _HASH.fullmatch(value) is not None


def _source_config(dsn, password):
    try:
        config = conninfo_to_dict(dsn)
        ca = _verified_ca_path()
        _require(set(config) <= {"host", "port", "dbname", "user", "sslmode", "sslrootcert"}
                 and (config.get("host"), config.get("user")) in SOURCE_ENDPOINTS
                 and config.get("port") == "5432" and config.get("dbname") == "postgres"
                 and config.get("sslmode") == "verify-full" and config.get("sslrootcert") == ca
                 and type(password) is str and bool(password) and "\x00" not in password
                 and not any(key.upper().startswith("PG") for key in os.environ), "configuration_invalid")
        return config | {"password": password, "passfile": os.devnull, "options": "",
                         "connect_timeout": "5", "gssencmode": "disable",
                         "application_name": "toolbako-source-read-only-drain"}
    except SourceDrainError:
        raise
    except Exception:
        raise SourceDrainError("configuration_invalid") from None


def _connect(config):
    return psycopg.connect(make_conninfo(**config), autocommit=True)


def _identity(conn, config):
    _require((conn.info.host, conn.info.user) in SOURCE_ENDPOINTS
             and conn.info.host == config["host"] and conn.info.user == config["user"]
             and conn.info.port == 5432 and conn.info.dbname == "postgres"
             and conn.pgconn.ssl_in_use is True, "identity_mismatch")
    row = conn.execute("""select current_database(),
        (select oid::bigint from pg_database where datname=current_database()),
        session_user,current_user,pg_backend_pid()""").fetchone()
    _require(row and row[0] == "postgres" and type(row[1]) is int
             and row[2] == row[3] == "postgres" and type(row[4]) is int, "identity_mismatch")
    return value_hash((SOURCE_PROJECT, conn.info.host, conn.info.user, 5432, row[0], row[1])), row[4]


def _readonly(conn):
    _require(not conn.closed and conn.autocommit
             and conn.info.transaction_status == TransactionStatus.IDLE, "read_failed")
    modes = conn.execute("""select current_setting('default_transaction_read_only'),
        current_setting('transaction_read_only'),current_setting('transaction_isolation'),
        current_setting('row_security')""").fetchone()
    _require(modes == ("on", "on", "read committed", "off"), "read_failed")


def _owns_lock(conn):
    row = conn.execute("""select exists(select 1 from pg_locks
        where locktype='advisory' and pid=pg_backend_pid() and granted
        and mode='ExclusiveLock' and classid=%s::oid and objid=%s::oid and objsubid=1
        and database=(select oid from pg_database where datname=current_database()))""",
        (LOCK_ID >> 32, LOCK_ID & 0xffffffff)).fetchone()
    _require(row == (True,), "lock_lost")


def _acquire(conn, timeout):
    deadline = time.monotonic() + timeout
    while True:
        if conn.execute("select pg_try_advisory_lock(%s)", (LOCK_ID,)).fetchone() == (True,):
            return
        remaining = deadline - time.monotonic()
        _require(remaining > 0, "lock_busy")
        time.sleep(min(0.05, remaining))


def _all_rows(conn):
    # One statement => one committed view across all nine subqueries. Separate
    # calls use new READ COMMITTED snapshots; no repeatable-read hiding drift.
    pieces = []
    for table in TABLES:
        pieces.extend([sql.Literal(table), sql.SQL("(select coalesce(jsonb_agg(to_jsonb(t) order by to_jsonb(t)::text),'[]'::jsonb) from {} t)").format(
            sql.Identifier("toolbako_runtime", table))])
    query = sql.SQL("select jsonb_build_object({})").format(sql.SQL(",").join(pieces))
    row = conn.execute(query).fetchone()
    _require(row and type(row[0]) is dict and set(row[0]) == set(TABLES)
             and all(type(value) is list for value in row[0].values()), "read_failed")
    return row[0]


@dataclass(frozen=True)
class SourceObservation:
    database_fingerprint: str
    schema_sha256: str
    revision: int
    snapshot_sha256: str
    table_counts: tuple[tuple[str, int], ...]
    table_sha256: tuple[tuple[str, str], ...]


def _observe(conn, config, expected_identity, expected_schema):
    _readonly(conn)
    _require(_identity(conn, config) == expected_identity, "identity_mismatch")
    _owns_lock(conn)
    _require(_schema_signature(conn) == expected_schema, "schema_mismatch")
    rows = _all_rows(conn)
    _require(rows["schema_version"] == [{"singleton": True, "version": SCHEMA_VERSION}], "schema_mismatch")
    snapshots = rows["marketplace_state"]
    _require(len(snapshots) == 1 and snapshots[0].get("singleton") is True
             and type(snapshots[0].get("revision")) is int and snapshots[0]["revision"] >= 0
             and type(snapshots[0].get("payload")) is dict, "source_uninitialized")
    state = decode_value(snapshots[0]["payload"])
    _require(type(state) is dict and set(state) == set(STATE_FIELDS), "source_uninitialized")
    _require(all(row.get("status") in {"succeeded", "rejected"} for row in rows["stripe_operations"]), "unsettled_work")
    # mail_worker retains historical claim_token on successful sent rows. That
    # alone is not an active lease; every column is still fingerprinted below.
    _require(all(row.get("status") == "sent" and row.get("lease_until") is None
                 and row.get("sent_at") is not None for row in rows["email_outbox"]), "unsettled_work")
    observation = SourceObservation(expected_identity[0], expected_schema, snapshots[0]["revision"], value_hash(state),
                                    tuple((name, len(rows[name])) for name in TABLES),
                                    tuple((name, value_hash(rows[name])) for name in TABLES))
    _require(_schema_signature(conn) == expected_schema, "schema_mismatch")
    _require(_identity(conn, config) == expected_identity, "identity_mismatch")
    _owns_lock(conn)
    return observation, state


class HeldSource:
    """Opaque, active-context-only handle. No raw rows in repr or errors."""
    __slots__ = ("__conn", "__config", "__identity", "__baseline", "__active", "__evidence")

    def __init__(self, conn, config, identity, baseline, evidence, *, _token=None):
        _require(_token is _TOKEN, "boundary_inactive")
        self.__conn, self.__config, self.__identity = conn, config, identity
        self.__baseline, self.__evidence, self.__active = baseline, evidence, True

    def __repr__(self):
        return "HeldSource(<redacted>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("HeldSource cannot be serialized")

    def _close(self):
        self.__active = False

    def _fresh(self):
        _require(self.__active, "boundary_inactive")
        try:
            current, state = _observe(self.__conn, self.__config, self.__identity, self.__baseline.schema_sha256)
            _require(current == self.__baseline, "source_changed")
            return current, state
        except SourceDrainError:
            self.__active = False
            raise
        except Exception:
            self.__active = False
            raise SourceDrainError("read_failed") from None

    def verify(self):
        """Return fixed classifications/counts/hashes only, with a fresh read."""
        return self._fresh()[0]

    def export(self, consumer, *, manifest):
        """Call an in-memory export/validation consumer WHILE the lock is held.

        No files/network/target writer is supplied by this module. The consumer
        is trusted application code, not a request parameter. Its result is an
        explicit data boundary; callers must not log it. Retain this context
        through any separately approved target transaction, then verify again
        before that commit. A later observation failure never authorizes target
        overwrite, source resume or retry of provider work.
        """
        before, state = self._fresh()
        _require(callable(consumer) and type(manifest) is dict
                 and type(manifest.get("source")) is dict
                 and manifest["source"].get("mode") == "test"
                 and manifest["source"].get("database_fingerprint") == before.database_fingerprint
                 and manifest.get("source_revision") == before.revision
                 and type(manifest.get("source_revision")) is int
                 and manifest.get("source_schema_version") == SCHEMA_VERSION
                 and manifest.get("snapshot_sha256") == before.snapshot_sha256, "manifest_stale")
        exported = deepcopy(state)
        try:
            result = consumer(exported)
            _require(value_hash(exported) == before.snapshot_sha256, "export_mutated")
            self._fresh()
            return result
        except SourceDrainError:
            self.__active = False
            raise
        except Exception:
            self.__active = False
            raise SourceDrainError("export_failed") from None


@contextmanager
def held_source_drain(dsn, password, *, expected_fingerprint, expected_schema_sha256,
                      external_writer_evidence_sha256, lock_timeout_seconds=5, observation_interval_seconds=0.1):
    """Source-only read session, with bounded acquire and explicit finally-close.

    expected_fingerprint/schema must come from an independently reviewed source
    inventory. The evidence hash is an external operator precondition, NOT
    authentication or a replacement for user authorization. No source writes,
    schema repair, lock stealing or unfinished-operation cleanup occurs.
    """
    _require(_is_hash(expected_fingerprint) and _is_hash(expected_schema_sha256)
             and _is_hash(external_writer_evidence_sha256), "precondition_required")
    _require(type(lock_timeout_seconds) in (int, float) and math.isfinite(lock_timeout_seconds)
             and 0 < lock_timeout_seconds <= 5
             and type(observation_interval_seconds) in (int, float) and math.isfinite(observation_interval_seconds)
             and 0.01 <= observation_interval_seconds <= 1, "configuration_invalid")
    config = _source_config(dsn, password)
    conn = handle = None
    locked = False
    try:
        conn = _connect(config)
        conn.execute("set default_transaction_read_only=on")
        conn.execute("set default_transaction_isolation='read committed'")
        conn.execute("set row_security=off")
        conn.execute("set statement_timeout='5s'")
        conn.execute("set timezone='UTC'")
        identity = _identity(conn, config)
        _require(identity[0] == expected_fingerprint, "identity_mismatch")
        _acquire(conn, lock_timeout_seconds)
        locked = True
        first, _ = _observe(conn, config, identity, expected_schema_sha256)
        time.sleep(observation_interval_seconds)
        second, _ = _observe(conn, config, identity, expected_schema_sha256)
        _require(first == second, "source_changed")
        handle = HeldSource(conn, config, identity, second, external_writer_evidence_sha256, _token=_TOKEN)
        yield handle
        handle.verify()
    except SourceDrainError:
        raise
    except Exception:
        raise SourceDrainError("read_failed") from None
    finally:
        if handle is not None:
            handle._close()
        if conn is not None:
            try:
                if locked and not conn.closed:
                    _require(conn.execute("select pg_advisory_unlock(%s)", (LOCK_ID,)).fetchone() == (True,), "cleanup_failed")
            except Exception:
                raise SourceDrainError("cleanup_failed") from None
            finally:
                conn.close()
