"""Explicit connection-only launch using secrets already sealed on this web.

The default preserves existing launches. isolated_v1 creates the child process
environment in memory; no secret is printed, put in argv, written to disk, or
exported to another service. This is NOT migration/binding/activation evidence.
The migration credential still has admin privilege and remains temporary until
replaced with reviewed permanent runtime credentials; do not call it least
privilege. Never import config/main before constructing the new environment.
"""
import os
import re
import sys
from uuid import UUID

from .live_database_config import build_live_database_config, build_runtime_database_config


_FAILURE_REASONS = frozenset({
    "configuration_unverified", "invocation_invalid", "runtime_selection_invalid",
    "operation_flags_invalid", "connection_stage_invalid", "deployment_environment_invalid",
    "stripe_source_configuration_invalid", "auth_storage_configuration_invalid",
    "ledger_invalid", "inherited_database_options", "database_secret_missing",
    "database_secret_invalid", "database_ca_or_config_invalid", "stripe_key_missing",
    "database_role_invalid", "database_credential_conflict",
    "stripe_key_invalid", "webhook_platform_secret_invalid", "webhook_connect_secret_invalid",
    "webhook_secret_pair_missing",
    "webhook_secrets_not_distinct", "webhook_version_unsupported", "web_port_invalid",
    "unexpected_launch_failure",
})


def _safe_reason(reason):
    return reason if type(reason) is str and reason in _FAILURE_REASONS else "configuration_unverified"


class RuntimeLaunchError(RuntimeError):
    def __init__(self, reason="configuration_unverified"):
        super().__init__("runtime_launch_configuration_unverified")
        self.reason = _safe_reason(reason)


def _require(value, reason="configuration_unverified"):
    if not value:
        raise RuntimeLaunchError(reason)


class RuntimeLaunch:
    __slots__ = ("__argv", "__environment")

    def __init__(self, argv, environment):
        self.__argv, self.__environment = argv, environment

    def __repr__(self):
        return "RuntimeLaunch(<redacted>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("RuntimeLaunch cannot be serialized")

    def execute(self):
        os.execve(sys.executable, self.__argv, self.__environment)


def prepare_runtime_launch(action, environment):
    """Prepare one exact preflight/web command; no process or provider I/O."""
    _require(action in {"preflight", "web"} and type(environment) is dict
             and all(type(k) is str and type(v) is str for k, v in environment.items()), "invocation_invalid")
    env = environment.copy()
    selection = env.get("LIVE_RUNTIME_CONFIG", "off")
    _require(selection in {"off", "isolated_v1"}, "runtime_selection_invalid")
    if selection == "isolated_v1":
        from .live_setup_plan import STRIPE_API_VERSION, WEBHOOK_API_VERSIONS
        mode = env.get("STRIPE_CONNECTION_MODE")
        flag = env.get("STRIPE_LIVE_SELLER_SETUP_ENABLED", "false")
        financial_flags = [env.get(name, "false") for name in
                           ("STRIPE_LIVE_REFUNDS_ENABLED", "STRIPE_LIVE_PAYOUTS_ENABLED", "STRIPE_LIVE_CHECKOUT_ENABLED")]
        _require(flag in {"true", "false"} and all(value in {"true", "false"} for value in financial_flags),
                 "operation_flags_invalid")
        _require(((mode, flag) in {("live_read_only", "false"), ("live_setup_only", "true")}
                  and financial_flags == ["false", "false", "false"] or mode == "live_financial"),
                 "connection_stage_invalid")
        _require(env.get("APP_ENV") in {"staging", "production"}
                 and env.get("DEMO_MODE", "false") == "false", "deployment_environment_invalid")
        _require(env.get("STRIPE_API_VERSION", STRIPE_API_VERSION) == STRIPE_API_VERSION
                 and env.get("STRIPE_CONNECT_ACCOUNTS_V2", "false") == "false"
                 and env.get("STRIPE_CHARGE_MODE") == "separate"
                 and env.get("STRIPE_PLATFORM_ACCOUNT_ID") == "acct_1Ty4WnIogL39waUY",
                 "stripe_source_configuration_invalid")
        _require(env.get("SUPABASE_URL", "").rstrip("/") == "https://xnjzgfaasbemffgorvnj.supabase.co"
                 and env.get("STORAGE_PROVIDER") == "supabase"
                 and env.get("SUPABASE_PRIVATE_BUCKET") and env.get("SUPABASE_IMAGE_BUCKET"),
                 "auth_storage_configuration_invalid")
        try:
            ledger = env["STRIPE_LEDGER_ID"]
            _require(str(UUID(ledger)) == ledger, "ledger_invalid")
        except (KeyError, ValueError, TypeError):
            raise RuntimeLaunchError("ledger_invalid") from None
        # Only this known legacy source password is removed. Unknown libpq
        # controls must not silently become part of the target connection.
        _require(not any(k.upper().startswith("PG") and k != "PGPASSWORD" for k in env),
                 "inherited_database_options")
        env.pop("PGPASSWORD", None)
        role = env.get("LIVE_DATABASE_ROLE", "migration_admin")
        _require(role in {"migration_admin", "runtime_v1"}, "database_role_invalid")
        _require(not any(value == "true" for value in financial_flags) or role == "runtime_v1",
                 "database_role_invalid")
        source = "TOOLBAKO_LIVE_RUNTIME_PASSWORD" if role == "runtime_v1" else "TOOLBAKO_LIVE_MIGRATION_PASSWORD"
        other = "TOOLBAKO_LIVE_MIGRATION_PASSWORD" if role == "runtime_v1" else "TOOLBAKO_LIVE_RUNTIME_PASSWORD"
        # Even an empty alternative variable is rejected: no ambiguous fallback.
        _require(other not in env, "database_credential_conflict")
        password = env.pop(source, "")
        env["LIVE_DATABASE_ROLE"] = role
        key = env.pop("STRIPE_LIVE_PENDING_SECRET_KEY", "")
        _require(bool(key), "stripe_key_missing")
        _require(re.fullmatch(r"rk_live_[A-Za-z0-9]+", key) is not None, "stripe_key_invalid")
        _require(bool(password), "database_secret_missing")
        _require("\x00" not in password, "database_secret_invalid")
        try:
            builder = build_runtime_database_config if role == "runtime_v1" else build_live_database_config
            env["DATABASE_URL"] = builder(password, environment=env).for_connection(environment=env)
        except Exception:
            raise RuntimeLaunchError("database_ca_or_config_invalid") from None
        env["STRIPE_SECRET_KEY"] = key
        # The transition variable names must not remain as an alternate code
        # path inside the running child. Active values are still secret env.
        for pending, active, reason in (
            ("STRIPE_LIVE_PENDING_WEBHOOK_SECRET", "STRIPE_WEBHOOK_SECRET", "webhook_platform_secret_invalid"),
            ("STRIPE_LIVE_PENDING_CONNECT_WEBHOOK_SECRET", "STRIPE_CONNECT_WEBHOOK_SECRET", "webhook_connect_secret_invalid"),
        ):
            value = env.pop(pending, "")
            if value:
                _require(re.fullmatch(r"whsec_[A-Za-z0-9]+", value) is not None, reason)
            # Never inherit a test endpoint signature after live key selection.
            env[active] = value
        webhook_version = env.get("STRIPE_WEBHOOK_API_VERSION", STRIPE_API_VERSION)
        _require(webhook_version in WEBHOOK_API_VERSIONS, "webhook_version_unsupported")
        env["STRIPE_WEBHOOK_API_VERSION"] = webhook_version
        if mode in {"live_setup_only", "live_financial"}:
            _require(env["STRIPE_WEBHOOK_SECRET"] and env["STRIPE_CONNECT_WEBHOOK_SECRET"],
                     "webhook_secret_pair_missing")
            _require(env["STRIPE_WEBHOOK_SECRET"] != env["STRIPE_CONNECT_WEBHOOK_SECRET"],
                     "webhook_secrets_not_distinct")
            _require(env.get("STRIPE_API_VERSION", STRIPE_API_VERSION) == STRIPE_API_VERSION
                     and env.get("STRIPE_CONNECT_ACCOUNTS_V2", "false") == "false",
                     "stripe_source_configuration_invalid")
    if action == "preflight":
        argv = [sys.executable, "-m", "app.deployment_check"]
    else:
        port = env.get("PORT", "8000")
        _require(port.isascii() and port.isdecimal() and 1 <= int(port) <= 65535, "web_port_invalid")
        argv = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", port,
                "--no-access-log"]
    return RuntimeLaunch(argv, env)


def main():
    try:
        _require(len(sys.argv) == 2, "invocation_invalid")
        prepare_runtime_launch(sys.argv[1], dict(os.environ)).execute()
    except RuntimeLaunchError as error:
        print("runtime_launch_configuration_unverified", file=sys.stderr)
        print("runtime_launch_reason=" + _safe_reason(error.reason), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        # No exception chaining or argument/credential echo.
        print("runtime_launch_configuration_unverified", file=sys.stderr)
        print("runtime_launch_reason=unexpected_launch_failure", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
