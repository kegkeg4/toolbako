"""Process the transactional email outbox. Does not move money.

Run `python -m app.mail_worker --limit 20` from a scheduler. Multiple workers
claim different rows. Retries reuse Resend keys only within a 23-hour window;
after that a potentially delivered email needs review, not a blind resend.
"""
import argparse
import asyncio
from uuid import uuid4

from .database import PostgresStateStore
from .integrations import EmailIntegration


async def deliver_pending(backend, api_key: str, *, limit: int = 20, sender_factory=EmailIntegration):
    if not api_key or not 1 <= limit <= 100:
        raise ValueError("Email API key and limit 1..100 are required")
    result = {"sent": 0, "retry": 0, "review": 0}
    async with await backend.connect() as conn:
        for _ in range(limit):
            token = str(uuid4())
            async with conn.transaction():
                cursor = await conn.execute(
                    "select id,recipient,sender,subject,body,attempts,"
                    "(first_attempt_at is not null and first_attempt_at < now()-interval '23 hours') "
                    "from toolbako_runtime.email_outbox "
                    "where (status='pending' and available_at<=now()) or (status='sending' and lease_until<now()) "
                    "order by created_at for update skip locked limit 1",
                )
                row = await cursor.fetchone()
                if not row:
                    break
                email_id, recipient, sender, subject, body, attempts, too_old = row
                if attempts >= 10 or too_old:
                    await conn.execute("update toolbako_runtime.email_outbox set status='review',lease_until=null where id=%s", (email_id,))
                    result["review"] += 1
                    continue
                await conn.execute(
                    "update toolbako_runtime.email_outbox set status='sending',attempts=attempts+1,claim_token=%s,"
                    "lease_until=now()+interval '5 minutes',first_attempt_at=coalesce(first_attempt_at,now()) where id=%s",
                    (token, email_id),
                )
            try:
                delivered = await sender_factory(api_key, sender).send(
                    recipient, subject, body, idempotency_key=f"toolbako-mail-{email_id}",
                )
            except Exception:
                delivered = False
            cursor = await conn.execute(
                "update toolbako_runtime.email_outbox set status=%s,lease_until=null,"
                "sent_at=case when %s then now() else null end,available_at=now()+interval '5 minutes' "
                "where id=%s and status='sending' and claim_token=%s returning id",
                ("sent" if delivered else "pending", delivered, email_id, token),
            )
            if await cursor.fetchone():
                result["sent" if delivered else "retry"] += 1
    return result


def main():
    parser = argparse.ArgumentParser(description="Send committed notification emails (not payouts)")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    from .config import settings
    try:
        result = asyncio.run(deliver_pending(
            PostgresStateStore(settings.database_url, production=settings.is_production),
            settings.email_api_key, limit=args.limit,
        ))
    except Exception:
        print("Email worker failed. Check database, migration and email configuration. No credentials were printed.")
        raise SystemExit(1) from None
    print(f"Email outbox: sent={result['sent']} retry={result['retry']} review={result['review']}")
    if result["retry"] or result["review"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
