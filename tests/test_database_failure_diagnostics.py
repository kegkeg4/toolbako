import asyncio
import logging

import psycopg
import pytest

from app.database import (
    DATABASE_FAILURE_CODES, PostgresStateStore, StorageUnavailable,
    database_failure_code,
)


@pytest.mark.parametrize("state, code", [
    ("28000", "authentication_failed"), ("28P01", "authentication_failed"),
    ("3D000", "database_not_found"), ("53300", "connections_exhausted"),
    ("57P03", "database_not_ready"), ("42P01", "schema_missing"),
    ("3F000", "schema_missing"), ("42501", "schema_access_denied"),
    ("57014", "query_timeout"),
])
def test_database_sqlstate_diagnostics_are_fixed_codes(state, code):
    error = psycopg.errors.lookup(state)("private-password private-provider-payload")
    assert database_failure_code(error, phase="schema") == code
    assert code in DATABASE_FAILURE_CODES


@pytest.mark.parametrize("message, code", [
    ("Tenant or user not found", "pooler_target_not_found"),
    ("Circuit breaker open", "pooler_unavailable"),
    ("unsupported startup parameter: options", "unsupported_startup_parameter"),
    ("fe_sendauth: no password supplied", "password_not_supplied"),
    ("password authentication failed", "authentication_failed"),
    ("SASL authentication failed", "authentication_failed"),
    ("could not translate host name", "dns_resolution_failed"),
    ("failed to resolve host", "dns_resolution_failed"),
    ("name or service not known", "dns_resolution_failed"),
    ("nodename nor servname provided", "dns_resolution_failed"),
    ("connection timed out", "connection_timeout"),
    ("timeout expired", "connection_timeout"),
    ("connection timeout", "connection_timeout"),
    ("certificate verify failed", "tls_connection_failed"),
    ("SSL error", "tls_connection_failed"),
    ("server does not support SSL", "tls_connection_failed"),
    ("connection refused", "network_connection_failed"),
    ("network is unreachable", "network_connection_failed"),
    ("no route to host", "network_connection_failed"),
    ("unknown private provider payload", "connection_failed"),
])
def test_database_connection_errors_are_classified_without_exporting_message(message, code):
    error = psycopg.OperationalError(message + " private-password")
    assert database_failure_code(error, phase="connect") == code
    assert database_failure_code(error, phase="schema") == "schema_probe_failed"


def test_connection_failure_never_logs_or_returns_provider_details(monkeypatch, caplog):
    async def connect(*args, **kwargs):
        raise psycopg.OperationalError("password authentication failed for private-user private-password")
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    backend = PostgresStateStore("postgresql://private-user:private-password@localhost/test?sslmode=require", production=True)
    with pytest.raises(StorageUnavailable) as error:
        asyncio.run(backend.connect())
    assert error.value.diagnostic_code == "authentication_failed"
    assert "private" not in str(error.value)
    with caplog.at_level(logging.WARNING, logger="toolbako.database"):
        assert asyncio.run(backend.probe()) is False
    assert "database_probe_failed reason=authentication_failed" in caplog.text
    assert "private" not in caplog.text and "postgresql" not in caplog.text


def test_database_configuration_rejection_remains_before_network(monkeypatch, caplog):
    async def forbidden(*args, **kwargs):
        pytest.fail("Unsafe DB configuration must never connect")
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", forbidden)
    backend = PostgresStateStore("postgresql://private-user:private-password@localhost:6543/test?sslmode=require", production=True)
    with caplog.at_level(logging.WARNING, logger="toolbako.database"):
        assert asyncio.run(backend.probe()) is False
    assert "reason=configuration_invalid" in caplog.text and "private" not in caplog.text


@pytest.mark.parametrize("failure", ["permission", "missing_schema", "wrong_version", "unknown"])
def test_schema_probe_classification_is_readonly_and_redacted(monkeypatch, caplog, failure):
    class Cursor:
        async def fetchone(self):
            return (999,)
    class Connection:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def execute(self, statement):
            assert statement == "select version from toolbako_runtime.schema_version where singleton"
            if failure == "permission": raise psycopg.errors.InsufficientPrivilege("private-password")
            if failure == "missing_schema": raise psycopg.errors.UndefinedTable("private-password")
            if failure == "unknown": raise RuntimeError("private-password")
            return Cursor()
    async def connect(): return Connection()
    backend = PostgresStateStore("private-dsn")
    monkeypatch.setattr(backend, "connect", connect)
    with caplog.at_level(logging.WARNING, logger="toolbako.database"):
        assert asyncio.run(backend.probe()) is False
    codes = {"permission": "schema_access_denied", "missing_schema": "schema_missing", "wrong_version": "schema_version_mismatch", "unknown": "schema_probe_failed"}
    assert f"reason={codes[failure]}" in caplog.text
    assert "private" not in caplog.text


@pytest.mark.parametrize("code", ["private-password", None, {}, [], True])
def test_unknown_diagnostic_code_cannot_be_exported(code):
    assert StorageUnavailable(diagnostic_code=code).diagnostic_code == "unavailable"


def test_unprintable_provider_error_fails_closed():
    class Unprintable(Exception):
        def __str__(self): raise RuntimeError("private-password")
    assert database_failure_code(Unprintable(), phase="connect") == "connection_failed"
