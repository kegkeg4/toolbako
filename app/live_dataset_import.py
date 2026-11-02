"""Transactional synthetic import rehearsal into a newly created local DB only.

This is deliberately not a deployment/migration CLI. It cannot accept an
existing target, does not read source databases, and never creates a financial
scope binding. The caller owns the truth of its synthetic-data declaration.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
from uuid import UUID, uuid4, uuid5
from weakref import WeakSet

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from .live_dataset import (
    ARCHIVE_FIELDS, MANUAL_FIELDS, NORMALIZED_EMPTY_TABLES,
    SUPPORTED_RUNTIME_SCHEMA_VERSION, DatasetValidationError, DryRunResult,
    empty_state, prepare_candidate, value_hash,
)
from .persistence import decode_value, encode_value


TABLES = tuple(sorted(("schema_version", "marketplace_state", "audit_events", *NORMALIZED_EMPTY_TABLES)))
ACTION = "local.synthetic_dataset_import"
_OWNED_TARGETS = WeakSet()
_CONFIG_KEYS = {"host", "port", "dbname", "user", "password", "sslmode", "hostaddr", "options", "passfile", "gssencmode", "connect_timeout", "application_name"}


class LocalImportError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(f"local_dataset_import:{code}")


def _require(value, code):
    if not value:
        raise LocalImportError(code)


def local_config(admin_dsn):
    """Reject fallback hosts, libpq service/options and inherited routing."""
    try:
        options = conninfo_to_dict(admin_dsn)
    except (psycopg.Error, TypeError, ValueError):
        raise LocalImportError("unsafe_connection") from None
    _require(not any(key.upper().startswith("PG") for key in os.environ), "inherited_libpq_options")
    _require(not set(options) - {"host", "port", "dbname", "user", "password", "sslmode"}, "unsafe_connection")
    _require(options.get("host") in {"127.0.0.1", "::1"} and options.get("dbname") == "postgres", "unsafe_connection")
    _require(type(options.get("user")) is str and bool(options["user"]), "unsafe_connection")
    port = options.get("port", "5432")
    _require(type(port) is str and port.isascii() and port.isdigit() and 1 <= int(port) <= 65535, "unsafe_connection")
    _require(options.get("sslmode", "disable") in {"disable", "require", "verify-full"}, "unsafe_connection")
    return {**options, "port": port, "hostaddr": options["host"], "options": "",
            "password": options.get("password", ""), "passfile": os.devnull,
            "sslmode": options.get("sslmode", "disable"), "gssencmode": "disable",
            "connect_timeout": "5", "application_name": "toolbako-local-import-rehearsal"}


def _connect(config, database="postgres", *, autocommit=False):
    _require(not any(key.upper().startswith("PG") for key in os.environ), "inherited_libpq_options")
    _require(type(config) is dict and set(config) == _CONFIG_KEYS and config.get("host") in {"127.0.0.1", "::1"}
             and config.get("hostaddr") == config["host"] and config.get("dbname") == "postgres"
             and config.get("options") == "" and config.get("passfile") == os.devnull, "unsafe_connection")
    _require(type(database) is str and (database == "postgres" or re.fullmatch(r"toolbako_import_[a-f0-9]{32}", database) is not None), "target_not_owned")
    try:
        return psycopg.connect(make_conninfo(**(config | {"dbname": database})), autocommit=autocommit)
    except psycopg.Error:
        raise LocalImportError("local_connection_failed") from None


def _server_details(conn):
    return conn.execute("select host(inet_server_addr()),inet_server_port(),current_database()").fetchone()


def _server(conn, config, database, expected=None):
    # A loopback-published Docker service has a different backend IP/port.
    # Keep the *client* endpoint numeric-loopback-only and pin the backend
    # identity observed before CREATE across initialization/import/cleanup.
    _require(config.get("host") in {"127.0.0.1", "::1"}
             and conn.info.hostaddr == config["host"]
             and conn.info.port == int(config["port"])
             and conn.info.dbname == database, "client_identity_mismatch")
    row = _server_details(conn)
    _require(type(row) is tuple and len(row) == 3 and row[2] == database
             and type(row[0]) is str and type(row[1]) is int and 1 <= row[1] <= 65535, "server_identity_mismatch")
    try:
        address = ipaddress.ip_address(row[0])
    except ValueError:
        raise LocalImportError("server_identity_mismatch") from None
    _require((address.is_loopback or address.is_private)
             and not address.is_unspecified and not address.is_multicast, "server_identity_mismatch")
    identity = (str(address), row[1])
    _require(expected is None or identity == expected, "server_identity_mismatch")
    return identity


def _schema_signature(conn):
    """Capture columns, ACL/RLS policies, keys, indexes and trigger functions."""
    tables = conn.execute("""select c.relname,c.relrowsecurity,c.relforcerowsecurity,
        coalesce(c.relacl,acldefault('r',c.relowner))::text
        from pg_class c join pg_namespace n on n.oid=c.relnamespace
        where n.nspname='toolbako_runtime' and c.relkind in ('r','p') order by c.relname""").fetchall()
    _require(tuple(row[0] for row in tables) == TABLES and all(row[1] for row in tables), "schema_mismatch")
    parts = [tables]
    for query in (
        """select c.relname,a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,
            coalesce(pg_get_expr(d.adbin,d.adrelid),'') from pg_attribute a
            join pg_class c on c.oid=a.attrelid join pg_namespace n on n.oid=c.relnamespace
            left join pg_attrdef d on d.adrelid=a.attrelid and d.adnum=a.attnum
            where n.nspname='toolbako_runtime' and c.relkind in ('r','p') and a.attnum>0 and not a.attisdropped order by c.relname,a.attnum""",
        """select c.relname,k.conname,pg_get_constraintdef(k.oid) from pg_constraint k
            join pg_class c on c.oid=k.conrelid join pg_namespace n on n.oid=c.relnamespace
            where n.nspname='toolbako_runtime' order by c.relname,k.conname""",
        "select indexname,indexdef from pg_indexes where schemaname='toolbako_runtime' order by indexname",
        """select c.relname,p.polname,p.polpermissive,p.polcmd,p.polroles::text,
            pg_get_expr(p.polqual,p.polrelid),pg_get_expr(p.polwithcheck,p.polrelid)
            from pg_policy p join pg_class c on c.oid=p.polrelid
            join pg_namespace n on n.oid=c.relnamespace
            where n.nspname='toolbako_runtime' order by c.relname,p.polname""",
        """select c.relname,t.tgname,t.tgenabled,pg_get_triggerdef(t.oid)
            from pg_trigger t join pg_class c on c.oid=t.tgrelid join pg_namespace n on n.oid=c.relnamespace
            where n.nspname='toolbako_runtime' and not t.tgisinternal order by c.relname,t.tgname""",
        """select p.proname,pg_get_functiondef(p.oid) from pg_proc p join pg_namespace n on n.oid=p.pronamespace
            where n.nspname='toolbako_runtime' order by p.proname,p.oid""",
        "select coalesce(nspacl,acldefault('n',nspowner))::text from pg_namespace where nspname='toolbako_runtime'",
    ):
        parts.append(conn.execute(query).fetchall())
    return value_hash(parts)


@dataclass(eq=False, frozen=True)
class LocalTarget:
    database_name: str
    fingerprint: str
    schema_signature: str
    _config: dict = field(repr=False)
    _nonce: str = field(repr=False)
    _oid: int = field(repr=False)
    _server_identity: tuple = field(repr=False)
    _active: bool = field(default=True, repr=False)


def _target_identity(conn, target):
    _require(type(target) is LocalTarget and target._active and target in _OWNED_TARGETS, "target_not_owned")
    _require(re.fullmatch(r"toolbako_import_[a-f0-9]{32}", target.database_name) is not None, "target_not_owned")
    _server(conn, target._config, target.database_name, target._server_identity)
    row = conn.execute("select oid::bigint,shobj_description(oid,'pg_database') from pg_database where datname=current_database()").fetchone()
    _require(row == (target._oid, "toolbako-local-import:" + target._nonce), "target_not_owned")
    expected = value_hash((target._config["host"], int(target._config["port"]), target._server_identity, target.database_name, target._oid, target._nonce))
    _require(expected == target.fingerprint, "target_not_owned")


@contextmanager
def new_local_target(admin_dsn):
    """Create/schema/drop ONLY a random DB allocated by this context.

    Uses the checked-in schema, not application migrate/config or seed paths.
    No existing DB can be supplied as the target. Cleanup verifies ownership.
    """
    config = local_config(admin_dsn)
    name = "toolbako_import_" + uuid4().hex
    nonce = uuid4().hex
    target = None
    created = False
    oid = None
    server_identity = None
    try:
        with _connect(config, autocommit=True) as admin:
            server_identity = _server(admin, config, "postgres")
            admin.execute(sql.SQL("create database {} template template0").format(sql.Identifier(name)))
            created = True
            oid = admin.execute("select oid::bigint from pg_database where datname=%s", (name,)).fetchone()[0]
            admin.execute(sql.SQL("comment on database {} is {}").format(sql.Identifier(name), sql.Literal("toolbako-local-import:" + nonce)))
        with _connect(config, name) as conn:
            _server(conn, config, name, server_identity)
            conn.execute(Path(__file__).with_name("runtime_schema.sql").read_text(encoding="utf-8"))
            signature = _schema_signature(conn)
        fingerprint = value_hash((config["host"], int(config["port"]), server_identity, name, oid, nonce))
        target = LocalTarget(name, fingerprint, signature, config, nonce, oid, server_identity)
        _OWNED_TARGETS.add(target)
        yield target
    except psycopg.Error:
        raise LocalImportError("local_database_operation_failed") from None
    finally:
        if created:
            # OID is captured immediately after CREATE. Never use a wildcard or
            # reuse a database whose identity has changed since allocation.
            with _connect(config, autocommit=True) as admin:
                _server(admin, config, "postgres", server_identity)
                row = admin.execute("select oid::bigint from pg_database where datname=%s", (name,)).fetchone()
                _require(row == (oid,), "cleanup_target_changed")
                admin.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(name)))
            if target is not None:
                _OWNED_TARGETS.discard(target)
                object.__setattr__(target, "_active", False)


def _audit_hash(event):
    # Same representation as PostgresStateStore._save/content_hash, without
    # importing application config, starting a store, or calling save/bind.
    body = json.dumps(encode_value(event), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def _event(manifest, proof, at):
    return {"id": str(uuid5(UUID(manifest["manifest_id"]), ACTION)), "user_id": None,
            "action": ACTION, "target": json.dumps(proof, sort_keys=True, separators=(",", ":")),
            "request_id": manifest["manifest_id"], "created_at": at.astimezone(timezone.utc)}


def _write_snapshot(conn, payload):
    conn.execute("""insert into toolbako_runtime.marketplace_state(singleton,revision,payload)
        values(true,1,%s::jsonb) on conflict(singleton) do update
        set revision=1,payload=excluded.payload,updated_at=now()
        where toolbako_runtime.marketplace_state.revision=0""", (json.dumps(encode_value(payload)),))


def _write_audit(conn, event):
    conn.execute("""insert into toolbako_runtime.audit_events(id,actor_id,action,target,request_id,occurred_at,content_hash)
        values(%s,%s,%s,%s,%s,%s,%s)""", (event["id"], None, ACTION, event["target"], event["request_id"], event["created_at"], _audit_hash(event)))


def _read_snapshot(conn):
    rows = conn.execute("select revision,payload from toolbako_runtime.marketplace_state order by singleton").fetchall()
    _require(len(rows) <= 1, "target_nonempty")
    return (rows[0][0], decode_value(rows[0][1])) if rows else None


def _audit_rows(conn):
    return conn.execute("select id::text,actor_id,action,target,request_id,occurred_at,content_hash from toolbako_runtime.audit_events order by id").fetchall()


def _verify_persisted(conn, expected, event):
    actual = _read_snapshot(conn)
    _require(actual is not None and actual[0] == 1 and value_hash(actual[1]) == value_hash(expected), "snapshot_verification_failed")
    _require(_audit_rows(conn) == [(event["id"], None, ACTION, event["target"], event["request_id"], event["created_at"], _audit_hash(event))], "audit_verification_failed")


def import_local_rehearsal(target, source_state, manifest, prepared, *, synthetic_data=False):
    """Revalidate preparation and atomically import into an owned disposable DB.

    Identical replay is a read-only no-op only while target remains byte/type
    equivalent. Different manifest, tampered data or later activity is rejected.
    Explicit declaration alone does not establish that arbitrary data is fake;
    use only artificial fixtures. No actual source DB is connected or verified.
    """
    _require(synthetic_data is True, "synthetic_declaration_required")
    _require(type(target) is LocalTarget and target._active and target in _OWNED_TARGETS, "target_not_owned")
    _require(re.fullmatch(r"toolbako_import_[a-f0-9]{32}", target.database_name) is not None, "target_not_owned")
    _require(type(prepared) is DryRunResult, "preparation_required")
    manifest = deepcopy(manifest)
    try:
        verified = prepare_candidate(deepcopy(source_state), manifest)
        _require(prepared.report == verified.report and value_hash(prepared.candidate) == value_hash(verified.candidate), "preparation_mismatch")
        _require(manifest["target"]["database_fingerprint"] == target.fingerprint, "target_fingerprint_mismatch")
        _require(manifest["source"]["database_fingerprint"] != target.fingerprint, "source_target_collision")
        _require(not any(verified.candidate[field] for field in ARCHIVE_FIELDS | MANUAL_FIELDS), "candidate_not_empty_financially")
    except DatasetValidationError:
        raise LocalImportError("candidate_invalid") from None
    except (TypeError, ValueError, KeyError):
        raise LocalImportError("preparation_mismatch") from None
    proof = {key: verified.report[key] for key in ("manifest_sha256", "source_sha256", "candidate_sha256")}
    proof["target_database_fingerprint"] = target.fingerprint
    replay = False
    payload = deepcopy(verified.candidate)
    try:
        with _connect(target._config, target.database_name) as conn:
            conn.execute("set transaction isolation level serializable")
            conn.execute("set local lock_timeout='5s'")
            conn.execute("set local statement_timeout='15s'")
            conn.execute(sql.SQL("lock table {} in access exclusive mode").format(sql.SQL(",").join(sql.Identifier("toolbako_runtime", name) for name in TABLES)))
            _target_identity(conn, target)
            _require(_schema_signature(conn) == target.schema_signature, "schema_mismatch")
            _require(conn.execute("select singleton,version from toolbako_runtime.schema_version").fetchall() == [(True, SUPPORTED_RUNTIME_SCHEMA_VERSION)], "schema_version_mismatch")
            for table in NORMALIZED_EMPTY_TABLES:
                count = conn.execute(sql.SQL("select count(*) from {}").format(sql.Identifier("toolbako_runtime", table))).fetchone()[0]
                _require(count == 0, "target_financial_or_outbox_nonempty")
            state = _read_snapshot(conn)
            audit = _audit_rows(conn)
            if audit:
                _require(len(audit) == 1 and state is not None, "target_nonempty")
                event = _event(manifest, proof, audit[0][5])
                payload["audit_logs"] = [event]
                _verify_persisted(conn, payload, event)
                replay = True
            else:
                _require(state is None or (state[0] == 0 and value_hash(state[1]) == value_hash(empty_state())), "target_nonempty")
                event = _event(manifest, proof, datetime.now(timezone.utc))
                payload["audit_logs"] = [event]
                _write_snapshot(conn, payload)
                _write_audit(conn, event)
                _verify_persisted(conn, payload, event)
    except psycopg.Error:
        raise LocalImportError("transaction_failed") from None
    except (DatasetValidationError, TypeError, ValueError, KeyError):
        raise LocalImportError("transaction_invalid_state") from None
    return {"status": "already_imported" if replay else "synthetic_imported",
            "manifest_sha256": proof["manifest_sha256"], "candidate_sha256": proof["candidate_sha256"],
            "stored_payload_sha256": value_hash(payload), "target_database_fingerprint": target.fingerprint,
            "snapshot_revision": 1, "audit_events": 1, "synthetic_only": True,
            "binding_created": False, "live_activated": False, "production_migration_complete": False}
