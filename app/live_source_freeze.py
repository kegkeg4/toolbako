"""Explicit OLD-source SHARE-lock freeze with reviewed retained mail.

This is not the ordinary read-only drain and does not relax it. SHARE relation
locks need a read-write transaction; this module executes no row DML/schema DDL
and ALWAYS rolls that transaction back, including on success. All nine runtime
tables remain SHARE locked while the opaque handle is active. Cooperating app
writers are also excluded with the existing advisory lock.

Review/approval and stopping old processes/provider side effects remain external
preconditions. A review object/evidence hash is an operator attestation, not
authentication or proof that maintenance/workers are stopped. Do not expose this
API as a user-request-controlled endpoint. Keep the context through the separately
approved target commit, then retire old writers before releasing maintenance.

Pending email may ONLY be retained unchanged in the OLD DB, excluded from the new
DB, and never sent/deleted by this operation. Its complete full-row hash and exact
status counts must have been reviewed. Sending/review/lease or pending/unknown
financial operations always stop the freeze. Connection loss/timeout means stop
dependent migration; neither target overwrite nor provider resend is recovery.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import math

from psycopg import sql
from psycopg.errors import LockNotAvailable
from psycopg.pq import TransactionStatus

from . import live_source_drain as drain
from .live_dataset import value_hash
from .live_dataset_import import TABLES, _schema_signature
from .persistence import STATE_FIELDS, decode_value


DISPOSITION = "retain_source_no_send_no_copy_no_delete"
_TOKEN = object()


class SourceFreezeError(RuntimeError):
    def __init__(self, code):
        allowed = {"configuration_invalid", "review_required", "mail_review_mismatch",
                   "identity_mismatch", "schema_mismatch", "source_uninitialized",
                   "unsettled_work", "lock_busy", "lock_lost", "source_changed",
                   "manifest_stale", "boundary_inactive", "export_failed",
                   "export_mutated", "freeze_failed", "cleanup_failed"}
        self.code = code if code in allowed else "freeze_failed"
        super().__init__("live_source_freeze:" + self.code)


def _require(ok, code):
    if not ok:
        raise SourceFreezeError(code)


@dataclass(frozen=True)
class RetainedMailReview:
    source_fingerprint: str
    outbox_sha256: str
    pending_count: int
    sent_count: int
    evidence_sha256: str
    disposition: str = DISPOSITION


def _validate_review(review, fingerprint):
    _require(type(review) is RetainedMailReview
             and review.source_fingerprint == fingerprint
             and drain._is_hash(review.outbox_sha256) and drain._is_hash(review.evidence_sha256)
             and type(review.pending_count) is int and review.pending_count >= 0
             and type(review.sent_count) is int and review.sent_count >= 0
             and review.disposition == DISPOSITION, "review_required")


def _verify_mail(rows, review):
    _require(all(row.get("status") in {"pending", "sent"}
                 and row.get("lease_until") is None
                 and ((row.get("status") == "sent") == (row.get("sent_at") is not None))
                 for row in rows), "unsettled_work")
    # A sent row can retain a historical claim_token. Hash it, do not clear it.
    _require(value_hash(rows) == review.outbox_sha256
             and sum(row["status"] == "pending" for row in rows) == review.pending_count
             and sum(row["status"] == "sent" for row in rows) == review.sent_count,
             "mail_review_mismatch")


def _assert_locks(conn, config, identity):
    _require(not conn.closed and conn.autocommit
             and conn.info.transaction_status == TransactionStatus.INTRANS, "lock_lost")
    _require(drain._identity(conn, config) == identity, "identity_mismatch")
    modes = conn.execute("""select current_setting('transaction_isolation'),
        current_setting('transaction_read_only'),current_setting('row_security')""").fetchone()
    _require(modes == ("read committed", "off", "off"), "configuration_invalid")
    drain._owns_lock(conn)
    locked = conn.execute("""select c.relname from pg_locks l
        join pg_class c on c.oid=l.relation join pg_namespace n on n.oid=c.relnamespace
        where l.locktype='relation' and l.pid=pg_backend_pid() and l.granted
        and l.mode='ShareLock' and n.nspname='toolbako_runtime'
        and l.database=(select oid from pg_database where datname=current_database())
        order by c.relname""").fetchall()
    _require(tuple(row[0] for row in locked) == TABLES, "lock_lost")


def _observe(conn, config, identity, schema, review):
    _assert_locks(conn, config, identity)
    _require(_schema_signature(conn) == schema, "schema_mismatch")
    rows = drain._all_rows(conn)
    _require(rows["schema_version"] == [{"singleton": True, "version": drain.SCHEMA_VERSION}], "schema_mismatch")
    snapshots = rows["marketplace_state"]
    _require(len(snapshots) == 1 and snapshots[0].get("singleton") is True
             and type(snapshots[0].get("revision")) is int and snapshots[0]["revision"] >= 0
             and type(snapshots[0].get("payload")) is dict, "source_uninitialized")
    state = decode_value(snapshots[0]["payload"])
    _require(type(state) is dict and set(state) == set(STATE_FIELDS), "source_uninitialized")
    _require(all(row.get("status") in {"succeeded", "rejected"} for row in rows["stripe_operations"]), "unsettled_work")
    _verify_mail(rows["email_outbox"], review)
    proof = drain.SourceObservation(identity[0], schema, snapshots[0]["revision"], value_hash(state),
                                    tuple((name, len(rows[name])) for name in TABLES),
                                    tuple((name, value_hash(rows[name])) for name in TABLES))
    _require(_schema_signature(conn) == schema, "schema_mismatch")
    _assert_locks(conn, config, identity)
    return proof, state


class FrozenSource:
    __slots__ = ("__conn", "__config", "__identity", "__baseline", "__review", "__active")

    def __init__(self, conn, config, identity, baseline, review, *, _token=None):
        _require(_token is _TOKEN, "boundary_inactive")
        self.__conn, self.__config, self.__identity = conn, config, identity
        self.__baseline, self.__review, self.__active = baseline, review, True

    def __repr__(self):
        return "FrozenSource(<redacted>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("FrozenSource cannot be serialized")

    def _close(self):
        self.__active = False

    def _fresh(self):
        _require(self.__active, "boundary_inactive")
        try:
            proof, state = _observe(self.__conn, self.__config, self.__identity,
                                    self.__baseline.schema_sha256, self.__review)
            _require(proof == self.__baseline, "source_changed")
            return proof, state
        except SourceFreezeError:
            self.__active = False
            raise
        except Exception:
            self.__active = False
            raise SourceFreezeError("freeze_failed") from None

    def verify(self):
        return self._fresh()[0]

    def export(self, consumer, *, manifest):
        """Pass only the marketplace payload to trusted in-memory preparation.

        Outbox rows, connection data and the connection itself are never exposed
        to the consumer. Full candidate filtering remains the reviewed dataset
        adapter's job. Keep this context and separately verify target no-mail
        invariants through target commit; this result is not later authorization.
        """
        proof, state = self._fresh()
        _require(callable(consumer) and type(manifest) is dict
                 and type(manifest.get("source")) is dict
                 and manifest["source"].get("mode") == "test"
                 and manifest["source"].get("database_fingerprint") == proof.database_fingerprint
                 and type(manifest.get("source_revision")) is int
                 and manifest["source_revision"] == proof.revision
                 and manifest.get("source_schema_version") == drain.SCHEMA_VERSION
                 and manifest.get("snapshot_sha256") == proof.snapshot_sha256, "manifest_stale")
        exported = deepcopy(state)
        try:
            result = consumer(exported)
            _require(value_hash(exported) == proof.snapshot_sha256, "export_mutated")
            self._fresh()
            return result
        except SourceFreezeError:
            self.__active = False
            raise
        except Exception:
            self.__active = False
            raise SourceFreezeError("export_failed") from None


@contextmanager
def held_source_freeze(dsn, password, *, expected_fingerprint, expected_schema_sha256,
                       mail_review, external_writer_evidence_sha256, lock_timeout_seconds=1):
    """Hold advisory+all-nine SHARE locks; always rollback then unlock/close.

    No approval is inferred from the review object. Caller must have an approved
    maintenance/relogin window and independently prevent old writers from resuming
    after release. Lock/statement waits are short; an idle transaction expires
    after 30 seconds, so never pause for human authentication inside this context.
    """
    _require(drain._is_hash(expected_fingerprint) and drain._is_hash(expected_schema_sha256)
             and drain._is_hash(external_writer_evidence_sha256), "review_required")
    _validate_review(mail_review, expected_fingerprint)
    _require(type(lock_timeout_seconds) in (int, float) and math.isfinite(lock_timeout_seconds)
             and 0.01 <= lock_timeout_seconds <= 5, "configuration_invalid")
    try:
        config = drain._source_config(dsn, password)
    except Exception:
        raise SourceFreezeError("configuration_invalid") from None
    conn = handle = None
    advisory_locked = False
    try:
        conn = drain._connect(config)
        conn.execute("set default_transaction_read_only=off")
        conn.execute("set default_transaction_isolation='read committed'")
        conn.execute("set statement_timeout='5s'")
        conn.execute("set timezone='UTC'")
        conn.execute("set search_path=pg_catalog")
        identity = drain._identity(conn, config)
        _require(identity[0] == expected_fingerprint, "identity_mismatch")
        drain._acquire(conn, lock_timeout_seconds)
        advisory_locked = True
        with conn.transaction(force_rollback=True):
            conn.execute("set local row_security=off")
            conn.execute("set local idle_in_transaction_session_timeout='30s'")
            conn.execute("select set_config('lock_timeout',%s,true)", (str(max(1, int(lock_timeout_seconds * 1000))) + "ms",))
            conn.execute(sql.SQL("lock table {} in share mode").format(sql.SQL(",").join(
                sql.Identifier("toolbako_runtime", name) for name in TABLES)))
            first, _ = _observe(conn, config, identity, expected_schema_sha256, mail_review)
            second, _ = _observe(conn, config, identity, expected_schema_sha256, mail_review)
            _require(first == second, "source_changed")
            handle = FrozenSource(conn, config, identity, second, mail_review, _token=_TOKEN)
            yield handle
            handle.verify()
    except SourceFreezeError:
        raise
    except drain.SourceDrainError as error:
        raise SourceFreezeError(error.code) from None
    except LockNotAvailable:
        raise SourceFreezeError("lock_busy") from None
    except Exception:
        raise SourceFreezeError("freeze_failed") from None
    finally:
        if handle is not None:
            handle._close()
        if conn is not None:
            try:
                if advisory_locked and not conn.closed:
                    _require(conn.execute("select pg_advisory_unlock(%s)", (drain.LOCK_ID,)).fetchone() == (True,), "cleanup_failed")
            except Exception:
                raise SourceFreezeError("cleanup_failed") from None
            finally:
                try:
                    conn.close()
                except Exception:
                    raise SourceFreezeError("cleanup_failed") from None
