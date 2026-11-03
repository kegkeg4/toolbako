"""Offline config/PEM tests only; no credentials or connection are retrieved."""
from dataclasses import asdict
import fnmatch
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import pickle
import shutil
from types import SimpleNamespace

import pytest
from psycopg.conninfo import conninfo_to_dict

from app import live_database_config as live


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_CA = "700723581420dd1ac98fd7e9ac529f0ef210eadcaf87fc868a3ad7d114c2f3b7"


def test_bundled_public_ca_has_exact_pinned_bytes_and_is_parsable():
    certificate = ROOT / "app/certs/supabase-prod-ca-2021.crt"
    assert certificate.is_file() and not certificate.is_symlink()
    assert hashlib.sha256(certificate.read_bytes()).hexdigest() == EXPECTED_CA
    assert Path(live._verified_ca_path()) == certificate


@pytest.mark.parametrize("password", [
    "SYNTHETIC_ONLY", " space before and after ", "single'quote", 'double"quote',
    r"slash\backslash\\", "x;y#z?x&y=1%40:@/", "$(never-run) `echo never` $env:NOT_READ",
    "日付と記号パスワード", "line\nbreak\r\ntab\tend", "   ",
    "comma,host=attacker.invalid password=changed sslmode=disable",
])
def test_password_is_encoded_by_libpq_and_round_trips_without_option_injection(password):
    config = live.build_live_database_config(password, environment={})
    parsed = conninfo_to_dict(config.for_connection(environment={}))
    assert parsed == {
        "host": "aws-0-ap-northeast-1.pooler.supabase.com", "port": "5432", "dbname": "postgres",
        "user": "postgres.xijdnqnsuhuciqyygnkg", "password": password,
        "sslmode": "verify-full", "sslrootcert": str(ROOT / "app/certs/supabase-prod-ca-2021.crt"),
    }
    assert "hostaddr" not in parsed and "service" not in parsed


@pytest.mark.parametrize("password", [None, "", 123, b"bytes", "NUL\x00SECRET", {"password": "SECRET"}])
def test_invalid_password_fails_with_fixed_error(password):
    with pytest.raises(live.LiveDatabaseConfigError) as error:
        live.build_live_database_config(password, environment={})
    assert str(error.value) == "live_database_configuration_invalid"


@pytest.mark.parametrize("name", ["PGPASSWORD", "PGHOST", "PGPORT", "PGUSER", "PGDATABASE", "PGSERVICE",
                                 "PGSERVICEFILE", "PGPASSFILE", "PGOPTIONS", "PGSSLMODE", "PGSSLROOTCERT",
                                 "PGHOSTADDR", "PGTARGETSESSIONATTRS", "PGANY_FUTURE_OPTION", "pgpassword"])
@pytest.mark.parametrize("value", ["", "SYNTHETIC_AMBIENT_SECRET"])
def test_any_existing_libpq_setting_is_rejected_even_if_empty_or_lowercase(name, value):
    environment = {"PATH": "not-used", name: value}
    before = dict(environment)
    with pytest.raises(live.LiveDatabaseConfigError):
        live.build_live_database_config("SYNTHETIC_ONLY", environment=environment)
    assert environment == before


def test_ambient_pg_is_rejected_without_reading_value_or_modifying_environment(monkeypatch):
    monkeypatch.setenv("PGPASSWORD", "SYNTHETIC_EXISTING_VALUE")
    before = dict(os.environ)
    with pytest.raises(live.LiveDatabaseConfigError):
        live.build_live_database_config("SYNTHETIC_NEW_VALUE")
    assert dict(os.environ) == before
    clean_child = {k: v for k, v in os.environ.items() if not k.upper().startswith("PG")}
    config = live.build_live_database_config("SYNTHETIC_NEW_VALUE", environment=clean_child)
    with pytest.raises(live.LiveDatabaseConfigError):
        config.for_connection()
    assert conninfo_to_dict(config.for_connection(environment=clean_child))["password"] == "SYNTHETIC_NEW_VALUE"
    assert dict(os.environ) == before


@pytest.mark.parametrize("environment", ["PGPASSWORD=SECRET", ["PATH"], 1, {1: "SECRET"}])
def test_environment_must_be_explicit_mapping_with_string_keys(environment):
    with pytest.raises(live.LiveDatabaseConfigError):
        live.build_live_database_config("SYNTHETIC_ONLY", environment=environment)


def test_secret_is_redacted_in_normal_string_repr_debug_logging_and_serialization(capsys, caplog):
    secret = "UNIQUE_SYNTHETIC_PASSWORD_9aff10"
    config = live.build_live_database_config(secret, environment={})
    print(config)
    print(repr(config))
    print(f"{config=}")
    with caplog.at_level(logging.WARNING):
        logging.getLogger(__name__).warning("configuration %s / %r", config, config)
    for serializer in (json.dumps, pickle.dumps, asdict, vars):
        with pytest.raises((TypeError, ValueError)) as error:
            serializer(config)
        assert secret not in str(error.value) and secret not in repr(error.value)
    output = capsys.readouterr()
    assert secret not in output.out + output.err + caplog.text
    assert "<redacted>" in output.out and "<redacted>" in caplog.text


@pytest.mark.parametrize("function", ["make_conninfo", "conninfo_to_dict"])
def test_library_failure_does_not_expose_secret_in_exception_or_prints(monkeypatch, capsys, function):
    secret = "UNIQUE_SYNTHETIC_PASSWORD_ERROR"
    def fail(*args, **kwargs):
        raise RuntimeError(secret)
    monkeypatch.setattr(live, function, fail)
    with pytest.raises(live.LiveDatabaseConfigError) as error:
        live.build_live_database_config(secret, environment={})
    assert str(error.value) == "live_database_configuration_invalid"
    assert error.value.__suppress_context__ is True
    assert error.value.__cause__ is None
    assert secret not in repr(error.value)
    output = capsys.readouterr()
    assert secret not in output.out + output.err


def _relocated_app(tmp_path, monkeypatch):
    target = tmp_path / "image" / "app" / "app"
    (target / "certs").mkdir(parents=True)
    shutil.copyfile(ROOT / "app/live_database_config.py", target / "live_database_config.py")
    shutil.copyfile(ROOT / "app/certs/supabase-prod-ca-2021.crt", target / "certs/supabase-prod-ca-2021.crt")
    monkeypatch.setattr(live, "__file__", str(target / "live_database_config.py"))
    return target


@pytest.mark.parametrize("damage", ["missing", "modified", "oversize", "directory"])
def test_missing_or_changed_ca_fails_closed_without_path_leak(tmp_path, monkeypatch, damage):
    package = _relocated_app(tmp_path, monkeypatch)
    cert = package / "certs" / live.CA_FILENAME
    if damage == "missing": cert.unlink()
    elif damage == "modified": cert.write_bytes(cert.read_bytes() + b"\n")
    elif damage == "oversize": cert.write_bytes(b"x" * 16385)
    elif damage == "directory":
        cert.unlink()
        cert.mkdir()
    with pytest.raises(live.LiveDatabaseConfigError) as error:
        live.build_live_database_config("SYNTHETIC_ONLY", environment={})
    assert str(error.value) == "live_database_configuration_invalid"
    assert str(tmp_path) not in str(error.value)


def test_ca_is_rechecked_at_the_explicit_secret_boundary(tmp_path, monkeypatch):
    package = _relocated_app(tmp_path, monkeypatch)
    config = live.build_live_database_config("SYNTHETIC_ONLY", environment={})
    (package / "certs" / live.CA_FILENAME).write_bytes(b"changed")
    with pytest.raises(live.LiveDatabaseConfigError):
        config.for_connection(environment={})


def test_config_cannot_be_built_from_an_arbitrary_conninfo_claim():
    with pytest.raises(live.LiveDatabaseConfigError):
        live.LiveDatabaseConfig("host=attacker.invalid password=SECRET", "/tmp/ca.pem")


def test_source_package_contains_ca_at_the_relocatable_railpack_runtime_path(tmp_path, monkeypatch):
    # Static packaging contract, not a claim that a new image is deployed.
    deployment = json.loads((ROOT / "railway.json").read_text(encoding="utf-8"))
    assert deployment["build"]["builder"] == "RAILPACK"
    assert deployment["deploy"]["startCommand"] == "python -m app.live_runtime web"
    assert deployment["deploy"]["preDeployCommand"] == ["python -m app.live_runtime preflight"]
    relative_paths = ["app/live_database_config.py", "app/certs/supabase-prod-ca-2021.crt"]
    patterns = [p.strip() for p in (ROOT / ".gitignore").read_text().splitlines()
                if p.strip() and not p.startswith(("#", "!"))]
    for relative in relative_paths:
        assert (ROOT / relative).is_file()
        assert not any(fnmatch.fnmatch(relative, p) or fnmatch.fnmatch(Path(relative).name, p)
                       or (p.endswith("/") and relative.startswith(p)) for p in patterns)
    package = _relocated_app(tmp_path, monkeypatch)
    parsed = conninfo_to_dict(live.build_live_database_config("SYNTHETIC_ONLY", environment={}).for_connection(environment={}))
    assert Path(parsed["sslrootcert"]) == package / "certs" / live.CA_FILENAME
    assert Path(parsed["sslrootcert"]).is_file()
    in_image = PurePosixPath("/") / Path(parsed["sslrootcert"]).relative_to(tmp_path / "image").as_posix()
    assert str(in_image) == "/app/app/certs/supabase-prod-ca-2021.crt"


def test_no_connection_environment_mutation_or_credential_lookup(monkeypatch):
    import psycopg
    before = dict(os.environ)
    def forbidden(*args, **kwargs):
        raise AssertionError("No connection may be opened")
    monkeypatch.setattr(psycopg, "connect", forbidden)
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", forbidden)
    config = live.build_live_database_config("SYNTHETIC_ONLY", environment={})
    assert conninfo_to_dict(config.for_connection(environment={}))["sslmode"] == "verify-full"
    assert dict(os.environ) == before


def test_generated_fields_match_existing_connection_stage_allowlist(monkeypatch):
    from app import deployment_check
    # No production Settings creation or connection: only the existing parser.
    monkeypatch.setattr(os, "environ", {})
    config = live.build_live_database_config("SYNTHETIC_ONLY")
    parsed = deployment_check._live_database_config(SimpleNamespace(database_url=config.for_connection()))
    assert parsed["sslmode"] == "verify-full" and parsed["port"] == "5432"
    assert set(parsed) == {"host", "port", "dbname", "user", "password", "sslmode", "sslrootcert"}
