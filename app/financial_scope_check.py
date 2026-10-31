"""Inspect a future payment dataset; optionally bind an EMPTY one explicitly.

No Stripe POST, email, account creation, key issuance, or live activation.
Run inside the approved target runtime; never copy its secrets to a terminal.
"""
import argparse
import asyncio
import json

from .financial_scope import configured_scope, SCOPE_OPERATION_ID, validate_binding
from .integrations import StripeIntegration


async def inspect_scope(backend, stripe, scope):
    """Read-only even for a missing/uninitialized marketplace snapshot."""
    if scope is None:
        return {"configured": False, "live_writes_enabled": False}
    scope.validate_key(stripe.secret_key)
    # Bypass the not-yet-created binding for these two READS only. Never call
    # a mutating provider method from this preparation command.
    account = await stripe._get_raw("account")
    balance = await stripe._get_raw("balance")
    scope.validate_provider(account, balance)
    async with await backend.connect() as conn:
        await conn.execute("set default_transaction_read_only = on")
        row = await (await conn.execute(
            "select endpoint,request_hash,status,response from toolbako_runtime.stripe_operations where operation_id=%s",
            (SCOPE_OPERATION_ID,),
        )).fetchone()
    if row is not None:
        validate_binding(row, scope)
    return {"configured": True, "provider_matches": True, "bound": row is not None,
            "mode": scope.mode, "live_writes_enabled": False}


async def run(settings, *, bind_empty=False):
    from .database import PostgresStateStore
    from .data import DemoStore
    scope = configured_scope(settings)
    backend = PostgresStateStore(settings.database_url, production=settings.is_deployed)
    stripe = StripeIntegration(settings.stripe_secret_key, settings.site_base_url,
                               api_version=settings.stripe_api_version,
                               charge_mode=settings.stripe_charge_mode,
                               journal=backend, financial_scope=scope)
    report = await inspect_scope(backend, stripe, scope)
    if bind_empty:
        if scope is None:
            raise RuntimeError("Financial scope must be configured")
        async with backend.request(DemoStore(seed=False)):
            await backend.bind_empty_financial_scope(scope)
        report["bound"] = True
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind-empty", action="store_true",
                        help="Explicitly bind a reviewed EMPTY dataset; refuses historical test records")
    args = parser.parse_args()
    from .config import settings
    try:
        report = asyncio.run(run(settings, bind_empty=args.bind_empty))
        print(json.dumps(report))
        raise SystemExit(0 if report.get("bound") else 2)
    except Exception:
        print(json.dumps({"verified": False, "live_writes_enabled": False,
                          "reason": "financial_scope_unverified"}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
