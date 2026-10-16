"""Explicit account-erasure worker. Default is a read-only count-only dry run.

Activation requires an approved retention-policy revision in server settings.
No scheduler is installed by this command. Never log provider bodies or IDs.
"""
import argparse
import asyncio
import json
from datetime import datetime, timezone
from uuid import UUID

import httpx

from .account_lifecycle import deletion_hold
from .data import DemoStore
from .database import PostgresStateStore, restore
from .object_storage import storage_origin
from .supabase_api import supabase_headers


class ErasureUnavailable(RuntimeError):
    pass


class AuthErasure:
    def __init__(self, settings):
        self.origin = storage_origin(settings)
        self.key = settings.supabase_service_role_key
        if not self.origin or not self.key or self.key.startswith("sb_publishable_"):
            raise ErasureUnavailable("Account erasure is not configured")

    async def erase(self, user_id):
        # A stored UUID cannot choose another path, host or query string.
        if str(UUID(user_id)) != user_id:
            raise ErasureUnavailable("Invalid account reference")
        url = f"{self.origin}/auth/v1/admin/users/{user_id}"
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                async def exists():
                    response = await client.get(url, headers=supabase_headers(self.key))
                    if response.status_code == 404 and response.json().get("code") == "user_not_found":
                        return False
                    if response.status_code != 200 or response.json().get("id") != user_id:
                        raise ErasureUnavailable("Auth deletion requires review")
                    return True
                if await exists():
                    response = await client.delete(url, headers=supabase_headers(self.key), params=None)
                    if response.status_code not in {200, 204}:
                        raise ErasureUnavailable("Auth deletion requires review")
                # A successful DELETE response alone is insufficient after a
                # timeout or crash. Confirm absence before local completion.
                if await exists():
                    raise ErasureUnavailable("Auth deletion is unconfirmed")
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise ErasureUnavailable("Auth deletion is unconfirmed") from None


def counts(store, at):
    due = [d for d in store.account_deletions if d.get("status") in {"scheduled", "erasing"}
           and d.get("delete_after") and d["delete_after"] <= at]
    held = sum(bool(deletion_hold(store, d["user_id"])) for d in due)
    return {"due": len(due), "held": held, "eligible": len(due)-held,
            "provider_review": sum(d.get("status") == "erasing" for d in due)}


async def inspect(backend, *, at=None):
    at = at or datetime.now(timezone.utc)
    store = DemoStore(seed=False)
    async with await backend.connect() as conn:
        async with conn.transaction():
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            await backend._check_schema(conn)
            row = await (await conn.execute("select payload from toolbako_runtime.marketplace_state where singleton")).fetchone()
            if row:
                restore(store, row[0])
            return counts(store, at)


async def run_once(backend, settings, *, provider=None, at=None, limit=10):
    if not settings.account_erasure_policy_revision.strip() or settings.demo_mode or not 1 <= limit <= 100:
        raise ErasureUnavailable("An approved retention policy and durable non-demo repository are required")
    provider = provider or AuthErasure(settings)
    store = DemoStore(seed=False)
    at = at or datetime.now(timezone.utc)
    completed = held = review = 0
    async with backend.request(store):
        due = sorted((d for d in store.account_deletions if d.get("status") in {"scheduled", "erasing"}
                     and d.get("delete_after") and d["delete_after"] <= at), key=lambda d: d["delete_after"])[:limit]
        for deletion in due:
            reason = deletion_hold(store, deletion["user_id"])
            if reason:
                deletion["hold_reason"] = reason
                held += 1
                continue
            # Persist access revocation before the irreversible remote step.
            deletion.update(status="erasing", retention_policy=settings.account_erasure_policy_revision)
            store.account_sessions = {sid: s for sid, s in store.account_sessions.items()
                                      if s.get("user", {}).get("id") != deletion["user_id"]}
            store.audit(None, "account.erasure_started", deletion["id"])
            await backend.save()
            try:
                await provider.erase(deletion["user_id"])
            except (ErasureUnavailable, ValueError):
                deletion["hold_reason"] = "provider_review"
                review += 1
                await backend.save()
                continue
            if store.execute_due_account_deletions(at, only_user_id=deletion["user_id"]) != 1:
                raise ErasureUnavailable("Local completion requires review")
            store.audit(None, "account.erasure_completed", deletion["id"])
            await backend.save()
            completed += 1
        await backend.save()
    return {"completed": completed, "held": held, "provider_review": review}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Requires an approved retention-policy revision; erases due accounts")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)
    from .config import settings
    backend = PostgresStateStore(settings.database_url, production=settings.is_deployed)
    try:
        result = asyncio.run(run_once(backend, settings, limit=args.limit) if args.apply else inspect(backend))
        print(json.dumps({"dry_run": not args.apply, "counts": result}))
        return 2 if result.get("held") or result.get("provider_review") else 0
    except Exception:
        print(json.dumps({"error": "Account lifecycle check failed; no provider details or personal data were printed."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
