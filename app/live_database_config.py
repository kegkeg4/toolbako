"""Build, but never apply, the designated live database connection settings.

The public CA ships with the app. No network/DB access, environment mutation,
credential lookup, CLI, logging or fallback endpoint exists here. A caller must
use the exact environment of its future process; inherited PG* options are not
silently removed. Existing source-web credentials must remain unchanged.

The returned object renders as redacted text. ``for_connection()`` is the only
intentional plaintext boundary and must be passed directly to the connection
consumer or sealed configuration transport, never printed or serialized.
This helper is not evidence that migration/binding/cutover has been authorized.
"""
from collections.abc import Mapping
import hashlib
import os
from pathlib import Path
import ssl

from psycopg.conninfo import conninfo_to_dict, make_conninfo


TARGET_PROJECT = "xijdnqnsuhuciqyygnkg"
TARGET_HOST = "aws-0-ap-northeast-1.pooler.supabase.com"
TARGET_USER = "postgres." + TARGET_PROJECT
RUNTIME_ROLE = "toolbako_web_live_v1"
RUNTIME_USER = RUNTIME_ROLE + "." + TARGET_PROJECT
CA_FILENAME = "supabase-prod-ca-2021.crt"
CA_SHA256 = "700723581420dd1ac98fd7e9ac529f0ef210eadcaf87fc868a3ad7d114c2f3b7"


class LiveDatabaseConfigError(RuntimeError):
    def __init__(self):
        # Do not interpolate filenames, environments, inputs or libpq messages.
        super().__init__("live_database_configuration_invalid")


def _require(ok):
    if not ok:
        raise LiveDatabaseConfigError()


def _check_environment(environment):
    current = os.environ if environment is None else environment
    _require(isinstance(current, Mapping))
    try:
        # Case-insensitive checking also covers Windows environment semantics.
        valid = all(isinstance(key, str) and not key.upper().startswith("PG") for key in current)
    except Exception:
        raise LiveDatabaseConfigError() from None
    _require(valid)


def _verified_ca_path():
    try:
        path = Path(__file__).resolve().parent / "certs" / CA_FILENAME
        _require(path.is_absolute() and path.is_file() and not path.is_symlink()
                 and path.stat().st_size <= 16384)
        certificate = path.read_bytes()
        _require(hashlib.sha256(certificate).hexdigest() == CA_SHA256)
        # Parse the pinned public PEM locally; this does not make a connection.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=certificate.decode("ascii"))
        return str(path)
    except LiveDatabaseConfigError:
        raise
    except Exception:
        raise LiveDatabaseConfigError() from None


class LiveDatabaseConfig:
    """Redacted holder; not a dictionary/dataclass suitable for accidental JSON."""
    __slots__ = ("__conninfo", "__ca_path")

    def __init__(self, conninfo, ca_path, *, _token=None):
        _require(_token is _BUILD_TOKEN)
        self.__conninfo, self.__ca_path = conninfo, ca_path

    def __repr__(self):
        return "LiveDatabaseConfig(<redacted>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("LiveDatabaseConfig cannot be serialized")

    def for_connection(self, *, environment=None):
        """Explicit secret boundary, revalidating CA and process environment.

        If environment is provided, it must be the exact future subprocess
        environment. This method does not clear PG* from the current process.
        """
        _check_environment(environment)
        _require(_verified_ca_path() == self.__ca_path)
        return self.__conninfo


_BUILD_TOKEN = object()


def _build_database_config(password, *, environment, user):
    """Generate exact session-pooler conninfo; no connection or writes.

    Local public-CA and environment validation are the only reads. A clean
    environment may be supplied for a planned subprocess without altering the
    running web process. The caller supplies the password through a secret-safe
    path; this function never retrieves it from existing variables or files.
    """
    _check_environment(environment)
    _require(type(password) is str and bool(password) and "\x00" not in password)
    ca_path = _verified_ca_path()
    expected = {
        "host": TARGET_HOST, "port": "5432", "dbname": "postgres", "user": user,
        "password": password, "sslmode": "verify-full", "sslrootcert": ca_path,
    }
    try:
        conninfo = make_conninfo(**expected)
        _require(conninfo_to_dict(conninfo) == expected)
        return LiveDatabaseConfig(conninfo, ca_path, _token=_BUILD_TOKEN)
    except LiveDatabaseConfigError:
        raise
    except Exception:
        raise LiveDatabaseConfigError() from None


def build_live_database_config(password, *, environment=None):
    """Temporary migration-admin path, retained only for explicit transition."""
    return _build_database_config(password, environment=environment, user=TARGET_USER)


def build_runtime_database_config(password, *, environment=None):
    """Fixed non-owner runtime role; never falls back to the migration admin."""
    return _build_database_config(password, environment=environment, user=RUNTIME_USER)
