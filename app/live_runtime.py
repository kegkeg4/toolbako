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

from .live_database_config import build_live_database_config


class RuntimeLaunchError(RuntimeError):
    def __init__(self):
        super().__init__("runtime_launch_configuration_unverified")


def _require(value):
    if not value:
        raise RuntimeLaunchError()


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
             and all(type(k) is str and type(v) is str for k, v in environment.items()))
    env = environment.copy()
    selection = env.get("LIVE_RUNTIME_CONFIG", "off")
    _require(selection in {"off", "isolated_v1"})
    if selection == "isolated_v1":
        mode = env.get("STRIPE_CONNECTION_MODE")
        flag = env.get("STRIPE_LIVE_SELLER_SETUP_ENABLED", "false")
        _require((mode, flag) in {("live_read_only", "false"), ("live_setup_only", "true")}
                 and env.get("APP_ENV") in {"staging", "production"}
                 and env.get("DEMO_MODE", "false") == "false"
                 and env.get("STRIPE_CHARGE_MODE") == "separate"
                 and env.get("STRIPE_PLATFORM_ACCOUNT_ID") == "acct_1Ty4WnIogL39waUY"
                 and env.get("SUPABASE_URL", "").rstrip("/") == "https://xnjzgfaasbemffgorvnj.supabase.co"
                 and env.get("STORAGE_PROVIDER") == "supabase"
                 and env.get("SUPABASE_PRIVATE_BUCKET") and env.get("SUPABASE_IMAGE_BUCKET"))
        try:
            ledger = env["STRIPE_LEDGER_ID"]
            _require(str(UUID(ledger)) == ledger)
        except (KeyError, ValueError, TypeError):
            raise RuntimeLaunchError() from None
        # Only this known legacy source password is removed. Unknown libpq
        # controls must not silently become part of the target connection.
        _require(not any(k.upper().startswith("PG") and k != "PGPASSWORD" for k in env))
        env.pop("PGPASSWORD", None)
        password = env.pop("TOOLBAKO_LIVE_MIGRATION_PASSWORD", "")
        key = env.pop("STRIPE_LIVE_PENDING_SECRET_KEY", "")
        _require(re.fullmatch(r"rk_live_[A-Za-z0-9]+", key) is not None)
        try:
            env["DATABASE_URL"] = build_live_database_config(password, environment=env).for_connection(environment=env)
        except Exception:
            raise RuntimeLaunchError() from None
        env["STRIPE_SECRET_KEY"] = key
        # The transition variable names must not remain as an alternate code
        # path inside the running child. Active values are still secret env.
        for pending, active in (("STRIPE_LIVE_PENDING_WEBHOOK_SECRET", "STRIPE_WEBHOOK_SECRET"),
                                ("STRIPE_LIVE_PENDING_CONNECT_WEBHOOK_SECRET", "STRIPE_CONNECT_WEBHOOK_SECRET")):
            value = env.pop(pending, "")
            if value:
                _require(re.fullmatch(r"whsec_[A-Za-z0-9]+", value) is not None)
            # Never inherit a test endpoint signature after live key selection.
            env[active] = value
        if mode == "live_setup_only":
            _require(env["STRIPE_WEBHOOK_SECRET"] and env["STRIPE_CONNECT_WEBHOOK_SECRET"]
                     and env["STRIPE_WEBHOOK_SECRET"] != env["STRIPE_CONNECT_WEBHOOK_SECRET"]
                     and env.get("STRIPE_API_VERSION", "2026-02-25.clover") == "2026-02-25.clover"
                     and env.get("STRIPE_CONNECT_ACCOUNTS_V2", "false") == "false")
    if action == "preflight":
        argv = [sys.executable, "-m", "app.deployment_check"]
    else:
        port = env.get("PORT", "8000")
        _require(port.isascii() and port.isdecimal() and 1 <= int(port) <= 65535)
        argv = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", port,
                "--no-access-log"]
    return RuntimeLaunch(argv, env)


def main():
    try:
        _require(len(sys.argv) == 2)
        prepare_runtime_launch(sys.argv[1], dict(os.environ)).execute()
    except Exception:
        # No exception chaining or argument/credential echo.
        print("runtime_launch_configuration_unverified", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
