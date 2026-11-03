"""Offline, secret-safe transition configuration and preflight gates."""
import asyncio
from dataclasses import replace
import os
import pickle
from types import SimpleNamespace

import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from app import deployment_check as checks
from app import live_database_config as config
from app.live_runtime import RuntimeLaunchError, prepare_runtime_launch
from test_live_connection_stage import Backend, stage
from test_live_runtime import fixture_env


@pytest.mark.parametrize("password", ["SYNTHETIC", " spaces '", 'double"quote', "\\\\slash", "a\nb\rc\t", "記号:@/#?=%$,host=x", "$(never) `never`"])
def test_runtime_password_round_trip_exact_target_and_redaction(password):
    value = config.build_runtime_database_config(password, environment={})
    parsed = conninfo_to_dict(value.for_connection(environment={}))
    assert parsed["user"] == "toolbako_web_live_v1.xijdnqnsuhuciqyygnkg"
    assert parsed["password"] == password
    assert parsed["host"] == config.TARGET_HOST and parsed["port"] == "5432"
    assert parsed["sslmode"] == "verify-full" and parsed["sslrootcert"] == config._verified_ca_path()
    assert repr(value) == str(value) == "LiveDatabaseConfig(<redacted>)"
    with pytest.raises(TypeError):
        pickle.dumps(value)


def runtime_env(**changes):
    env = fixture_env(LIVE_DATABASE_ROLE="runtime_v1", TOOLBAKO_LIVE_RUNTIME_PASSWORD="synthetic-runtime")
    env.pop("TOOLBAKO_LIVE_MIGRATION_PASSWORD")
    return {**env, **changes}


@pytest.mark.parametrize("action", ["web", "preflight"])
def test_runtime_launch_needs_no_admin_secret_and_drops_transition_name(action, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(os, "execve", lambda *args: calls.append(args))
    env = runtime_env()
    original = env.copy()
    prepare_runtime_launch(action, env).execute()
    _, argv, child = calls[0]
    parsed = conninfo_to_dict(child["DATABASE_URL"])
    assert parsed["user"] == config.RUNTIME_USER and parsed["password"] == "synthetic-runtime"
    assert child["LIVE_DATABASE_ROLE"] == "runtime_v1"
    assert not any(name in child for name in ("PGPASSWORD", "TOOLBAKO_LIVE_MIGRATION_PASSWORD", "TOOLBAKO_LIVE_RUNTIME_PASSWORD"))
    assert "synthetic-runtime" not in str(argv) and env == original
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("changes", [
    {"LIVE_DATABASE_ROLE": "other"}, {"LIVE_DATABASE_ROLE": "RUNTIME_V1"},
    {"TOOLBAKO_LIVE_MIGRATION_PASSWORD": "admin"}, {"TOOLBAKO_LIVE_MIGRATION_PASSWORD": ""},
    {"TOOLBAKO_LIVE_RUNTIME_PASSWORD": ""}, {"TOOLBAKO_LIVE_RUNTIME_PASSWORD": "x\x00secret"},
    {"PGOPTIONS": "-c role=postgres"}, {"pgpassword": "synthetic"},
    {"STRIPE_LIVE_CHECKOUT_ENABLED": "true"}, {"STRIPE_LIVE_CHECKOUT_ENABLED": "1"},
])
def test_runtime_transition_rejects_conflicting_or_unsafe_inputs(changes):
    with pytest.raises(RuntimeLaunchError, match="^runtime_launch_configuration_unverified$"):
        prepare_runtime_launch("web", runtime_env(**changes))


def test_admin_path_rejects_unused_runtime_secret():
    with pytest.raises(RuntimeLaunchError):
        prepare_runtime_launch("preflight", fixture_env(TOOLBAKO_LIVE_RUNTIME_PASSWORD="synthetic"))


@pytest.mark.parametrize("flag", ["refunds", "payouts", "checkout"])
def test_live_financial_writes_never_use_migration_admin(flag, monkeypatch):
    monkeypatch.setattr(os, "environ", {})
    env = fixture_env(STRIPE_CONNECTION_MODE="live_financial", STRIPE_LIVE_PENDING_WEBHOOK_SECRET="whsec_platform",
        STRIPE_LIVE_PENDING_CONNECT_WEBHOOK_SECRET="whsec_connect", **{"STRIPE_LIVE_" + flag.upper() + "_ENABLED": "true"})
    with pytest.raises(RuntimeLaunchError):
        prepare_runtime_launch("preflight", env)
    settings = stage(stripe_connection_mode="live_financial", stripe_webhook_secret="whsec_platform",
        stripe_connect_webhook_secret="whsec_connect", **{"stripe_live_" + flag + "_enabled": True})
    assert checks._live_configuration_verified(settings) is False


@pytest.mark.parametrize("flag", ["false", "true"])
def test_checkout_flag_only_admissible_in_live_financial(flag):
    prepare_runtime_launch("preflight", runtime_env(STRIPE_CONNECTION_MODE="live_financial", STRIPE_LIVE_CHECKOUT_ENABLED=flag,
        STRIPE_LIVE_PENDING_WEBHOOK_SECRET="whsec_platform", STRIPE_LIVE_PENDING_CONNECT_WEBHOOK_SECRET="whsec_connect"))


def role_stage():
    value = config.build_runtime_database_config("SYNTHETIC", environment={}).for_connection(environment={})
    settings = stage()
    # Preserve the test's independence while parent owns Settings integration.
    return SimpleNamespace(**{**vars(settings), "live_database_role": "runtime_v1", "database_url": value})


def test_config_requires_exact_runtime_role_and_pinned_ca(monkeypatch):
    monkeypatch.setattr(os, "environ", {})
    settings = role_stage()
    assert checks._live_configuration_verified(settings) is True
    assert checks._live_database_config(settings)["user"] == config.RUNTIME_USER
    settings.database_url = settings.database_url.replace(config.RUNTIME_USER, config.TARGET_USER)
    assert checks._live_configuration_verified(settings) is False


@pytest.mark.parametrize("mutation", ["ca", "port", "host", "role", "mode", "mixed_pg"])
def test_runtime_preflight_config_does_not_adopt_admin_or_other_target(monkeypatch, mutation):
    monkeypatch.setattr(os, "environ", {})
    settings = role_stage()
    if mutation == "ca": settings.database_url = make_conninfo(**{**conninfo_to_dict(settings.database_url), "sslrootcert": "/untrusted/cert.crt"})
    if mutation == "port": settings.database_url = settings.database_url.replace("5432", "6543")
    if mutation == "host": settings.database_url = settings.database_url.replace(config.TARGET_HOST, "other.invalid")
    if mutation == "role": settings.live_database_role = "migration_admin"
    if mutation == "mode": settings.live_database_role = "unknown"
    if mutation == "mixed_pg": monkeypatch.setenv("pgservice", "override")
    assert checks._live_configuration_verified(settings) is False


def test_missing_or_bad_effective_role_cannot_pass_preflight(monkeypatch):
    monkeypatch.setattr(os, "environ", {})
    settings = role_stage()
    backend = Backend(dsn=settings.database_url)
    backend.conn.info.user = config.RUNTIME_USER
    calls = []
    async def refused(conn, *, mail_queue_since):
        assert mail_queue_since == 0
        calls.append(conn)
        raise RuntimeError("SYNTHETIC_SECRET_NOT_TO_ECHO")
    monkeypatch.setattr("app.live_runtime_role.verify_runtime_role", refused)
    result = asyncio.run(checks._live_database_checks(settings, backend))
    assert calls and result == {"database_schema": False, "database_target_verified": True, "financial_scope_bound": False}


def test_runtime_preflight_checks_role_then_existing_binding_without_writes(monkeypatch):
    monkeypatch.setattr(os, "environ", {})
    settings = role_stage()
    backend = Backend(dsn=settings.database_url)
    backend.conn.info.user = config.RUNTIME_USER
    calls = []
    async def accepted(conn, *, mail_queue_since):
        assert mail_queue_since == 0
        assert conn.calls == [("set default_transaction_read_only = on", None)]
        calls.append(conn)
        return True
    monkeypatch.setattr("app.live_runtime_role.verify_runtime_role", accepted)
    assert all(asyncio.run(checks._live_database_checks(settings, backend)).values())
    assert len(calls) == 1


def test_runtime_preflight_forwards_explicit_mail_cutoff(monkeypatch):
    monkeypatch.setattr(os, "environ", {})
    settings = role_stage()
    settings.live_mail_policy_since = 1793700000
    backend = Backend(dsn=settings.database_url)
    backend.conn.info.user = config.RUNTIME_USER
    calls = []
    async def accepted(conn, *, mail_queue_since):
        calls.append((conn, mail_queue_since))
        return True
    monkeypatch.setattr("app.live_runtime_role.verify_runtime_role", accepted)
    assert all(asyncio.run(checks._live_database_checks(settings, backend)).values())
    assert calls == [(backend.conn, settings.live_mail_policy_since)]


@pytest.mark.parametrize("value", [True, False, None, -1, 4102444801, 1.0, "1793700000", "0"])
def test_runtime_preflight_rejects_invalid_mail_cutoff_before_connect(monkeypatch, value):
    monkeypatch.setattr(os, "environ", {})
    settings = role_stage()
    settings.live_mail_policy_since = value
    backend = Backend(dsn=settings.database_url)
    backend.conn.info.user = config.RUNTIME_USER
    async def never(*args, **kwargs):
        pytest.fail("Malformed mail policy cutoff must not open a DB connection")
    monkeypatch.setattr(backend, "connect", never)
    assert asyncio.run(checks._live_database_checks(settings, backend)) == {
        "database_schema": False, "database_target_verified": False, "financial_scope_bound": False}


def test_positive_mail_contract_needs_runtime_role_proof_even_during_setup(monkeypatch):
    monkeypatch.setattr(os, "environ", {})
    settings = stage()
    settings = SimpleNamespace(**{**vars(settings), "live_mail_policy_since": 1793700000})
    backend = Backend(dsn=settings.database_url)
    async def never(*args, **kwargs):
        pytest.fail("Migration admin cannot attest the configured Web/mail role contract")
    monkeypatch.setattr(backend, "connect", never)
    assert asyncio.run(checks._live_database_checks(settings, backend)) == {
        "database_schema": False, "database_target_verified": False, "financial_scope_bound": False}
