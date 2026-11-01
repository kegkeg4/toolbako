"""Read-only verification of a staged live key; never activate payments.

Uses STRIPE_LIVE_PENDING_SECRET_KEY and STRIPE_LIVE_PENDING_ACCOUNT_ID only.
No fallback to the application's active key, no DB connection, no Stripe POST.
Run inside the approved runtime, without pasting credentials into commands.
"""
import asyncio
import json
import os
import re

import httpx


class LiveConnectionUnverified(RuntimeError):
    def __init__(self):
        super().__init__("live_connection_unverified")


async def inspect_connection(key, expected_account, *, transport=None):
    # Restricted keys only. Prefix validation is not proof of live mode;
    # the authenticated balance response must also explicitly say true.
    if (not isinstance(key, str) or not re.fullmatch(r"rk_live_[A-Za-z0-9]+", key)
            or not isinstance(expected_account, str)
            or not re.fullmatch(r"acct_[A-Za-z0-9]+", expected_account)):
        raise LiveConnectionUnverified()
    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=20, follow_redirects=False,
            headers={"Authorization": f"Bearer {key}", "Stripe-Version": "2026-02-25.clover"},
        ) as client:
            account_response = await client.get("https://api.stripe.com/v1/account")
            account_response.raise_for_status()
            account = account_response.json()
            if (not isinstance(account, dict) or account.get("object") != "account"
                    or account.get("id") != expected_account):
                raise LiveConnectionUnverified()
            balance_response = await client.get("https://api.stripe.com/v1/balance")
            balance_response.raise_for_status()
            balance = balance_response.json()
            if (not isinstance(balance, dict) or balance.get("object") != "balance"
                    or balance.get("livemode") is not True):
                raise LiveConnectionUnverified()
        capabilities = account.get("capabilities") or {}
        if not isinstance(capabilities, dict):
            raise LiveConnectionUnverified()
        # Never serialize account/balance objects, personal details or amounts.
        return {
            "connection_verified": True, "platform_matches": True, "mode": "live",
            "charges_enabled": account.get("charges_enabled") is True,
            "payouts_enabled": account.get("payouts_enabled") is True,
            "card_payments_active": capabilities.get("card_payments") == "active",
            "transfers_active": capabilities.get("transfers") == "active",
            "read_only": True, "payments_activated": False,
            "write_permissions_verified": False,
            "ledger_binding_verified": False, "webhooks_verified": False,
        }
    except Exception:
        # Do not leak response bodies, request headers, URLs or credentials
        # through exception chaining or the command-line traceback.
        raise LiveConnectionUnverified() from None


def main():
    try:
        result = asyncio.run(inspect_connection(
            os.environ.get("STRIPE_LIVE_PENDING_SECRET_KEY", ""),
            os.environ.get("STRIPE_LIVE_PENDING_ACCOUNT_ID", ""),
        ))
    except Exception:
        print(json.dumps({"connection_verified": False, "read_only": True,
                          "payments_activated": False, "reason": "live_connection_unverified"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
