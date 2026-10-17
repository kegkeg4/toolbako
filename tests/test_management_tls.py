"""Exercise management entry points without permitting database or Stripe I/O."""
import sys

import httpx
import psycopg
import pytest

from app import config, database, payout_worker
from app.config import Settings


@pytest.mark.parametrize("command", ["check", "migrate", "payout"])
@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.parametrize("sslmode", [None, "disable", "prefer"])
def test_deployed_management_rejects_plaintext_before_network(
    monkeypatch, capsys, caplog, command, environment, sslmode,
):
    dsn = "postgresql://private-user:private-password@localhost/test"
    if sslmode:
        dsn += f"?sslmode={sslmode}"
    monkeypatch.setattr(config, "settings", Settings(
        environment=environment, database_url=dsn, demo_mode=False,
        stripe_secret_key="rk_test_private", stripe_charge_mode="separate",
    ))

    async def forbidden(*args, **kwargs):
        pytest.fail("Rejected management commands must not reach the network")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    monkeypatch.setattr(sys, "argv", ["worker"] if command == "payout" else ["database", command])
    with pytest.raises(SystemExit) as stopped:
        (payout_worker.main if command == "payout" else database.main)()
    assert stopped.value.code == 1
    output = capsys.readouterr()
    assert not any(secret in output.out + output.err + caplog.text for secret in (
        "private-user", "private-password", "rk_test_private",
    ))
    assert "postgresql" not in output.out + output.err + caplog.text


@pytest.mark.parametrize("command", ["check", "migrate", "payout"])
@pytest.mark.parametrize("sslmode", ["require", "verify-ca", "verify-full"])
def test_staging_management_passes_tls_configuration_to_driver(
    monkeypatch, capsys, caplog, command, sslmode,
):
    dsn = f"postgresql://private-user:private-password@localhost/test?sslmode={sslmode}"
    monkeypatch.setattr(config, "settings", Settings(
        environment="staging", database_url=dsn, demo_mode=False,
        stripe_secret_key="rk_test_private", stripe_charge_mode="separate",
    ))
    attempts = []

    async def unavailable(actual_dsn, **kwargs):
        attempts.append(actual_dsn)
        raise psycopg.OperationalError("private-provider-payload private-password")

    async def forbidden(*args, **kwargs):
        pytest.fail("No Stripe call is permitted in this test")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", unavailable)
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    monkeypatch.setattr(sys, "argv", ["worker"] if command == "payout" else ["database", command])
    with pytest.raises(SystemExit) as stopped:
        (payout_worker.main if command == "payout" else database.main)()
    assert stopped.value.code == 1
    assert attempts == [dsn]
    output = capsys.readouterr()
    assert not any(secret in output.out + output.err + caplog.text for secret in (
        "private-user", "private-password", "private-provider-payload", "rk_test_private",
    ))
