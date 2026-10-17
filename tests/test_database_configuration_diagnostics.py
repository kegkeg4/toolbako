import json

import pytest

from app.config import Settings
from app import deployment_check


def configured(dsn):
    return Settings(
        environment="staging", demo_mode=False, database_url=dsn,
        supabase_url="https://project-fixture.supabase.co",
    )


@pytest.mark.parametrize("password", ["", "private-database-password"])
def test_database_diagnostics_report_password_presence_only(monkeypatch, password):
    monkeypatch.setenv("PGPASSWORD", password)
    result = deployment_check.database_configuration_diagnostics(configured(
        "postgresql://postgres.project-fixture@aws-0-ap-northeast-1.pooler.supabase.com:5432/postgres?sslmode=require"
    ))
    assert result == {
        "uri_parsed": True, "tls_configured": True,
        "transaction_pooler_port": False, "uri_password_present": False,
        "environment_password_present": bool(password), "target_matches_supabase": True,
    }
    assert "private-database-password" not in json.dumps(result)
    assert all(type(value) is bool for value in result.values())


@pytest.mark.parametrize("dsn, expected", [
    ("postgresql://postgres:private-password@db.project-fixture.supabase.co:5432/postgres?sslmode=verify-full",
     {"uri_password_present": True, "target_matches_supabase": True, "tls_configured": True}),
    ("postgresql://postgres.project-fixture@aws-0-ap-northeast-1.pooler.supabase.com:6543/postgres?sslmode=require",
     {"transaction_pooler_port": True}),
    ("postgresql://postgres.wrong-project@aws-0-ap-northeast-1.pooler.supabase.com:5432/postgres?sslmode=require",
     {"target_matches_supabase": False}),
    ("postgresql://postgres@db.project-fixture.supabase.co:5432/postgres?sslmode=disable",
     {"tls_configured": False}),
    ("postgresql://postgres@db.project-fixture.supabase.co.evil.example:5432/postgres?sslmode=require",
     {"target_matches_supabase": False}),
    ("host=private-password sslmode=not-a-mode", {"tls_configured": False, "target_matches_supabase": False}),
    ("password='private-password", {"uri_parsed": False}),
    ("", {"uri_parsed": False}),
])
def test_database_diagnostics_do_not_export_connection_parts(monkeypatch, dsn, expected):
    monkeypatch.delenv("PGPASSWORD", raising=False)
    result = deployment_check.database_configuration_diagnostics(configured(dsn))
    for key, value in expected.items():
        assert result[key] is value
    rendered = json.dumps(result)
    assert "private-password" not in rendered
    assert "postgresql" not in rendered and "project-fixture" not in rendered
    assert all(type(value) is bool for value in result.values())


def test_database_diagnostics_do_not_change_preflight_verdict(monkeypatch, capsys):
    monkeypatch.setenv("PGPASSWORD", "private-password")
    monkeypatch.setattr(deployment_check, "Settings", lambda: configured("password='private-password"))
    async def checks(settings):
        return {"database_schema": False}
    monkeypatch.setattr(deployment_check, "connection_checks", checks)
    assert deployment_check.main() == 1
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["connection_preflight_passed"] is False
    assert result["live_payments_enabled"] is False
    assert result["database_configuration"]["uri_parsed"] is False
    assert "private-password" not in output


def test_preflight_exception_has_only_safe_database_observations(monkeypatch, capsys):
    monkeypatch.setenv("PGPASSWORD", "private-password")
    monkeypatch.setattr(deployment_check, "Settings", lambda: configured("host=db.project-fixture.supabase.co sslmode=require"))
    async def checks(settings):
        raise RuntimeError("provider private-password")
    monkeypatch.setattr(deployment_check, "connection_checks", checks)
    assert deployment_check.main() == 1
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["database_configuration"]["environment_password_present"] is True
    assert result["connection_preflight_passed"] is False
    assert "private-password" not in output and "provider" not in output
