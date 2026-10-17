"""Process the transactional email outbox. Does not move money.

Running without --apply only inspects counts. Sending also requires an explicit
message-ID scope or --all-pending. Multiple workers claim different rows.
Retries reuse Resend keys only within a 23-hour window;
after that a potentially delivered email needs review, not a blind resend.
"""
import argparse
import asyncio
import json
from uuid import UUID, uuid4

from .database import PostgresStateStore
from .integrations import EmailIntegration


async def deliver_pending(backend, api_key: str, *, limit: int = 20,
                          message_ids=None, sender_factory=EmailIntegration):
    if not api_key or not 1 <= limit <= 100:
        raise ValueError("Email API key and limit 1..100 are required")
    # None is the explicitly selected full queue. An empty/invalid scope must
    # never fall back to it, including when called outside the CLI.
    selected = None
    if message_ids is not None:
        if isinstance(message_ids, (str, bytes)) or not 1 <= len(message_ids) <= 100:
            raise ValueError("Expected 1..100 message IDs")
        selected = list(dict.fromkeys(UUID(str(value)) for value in message_ids))
    result = {"sent": 0, "retry": 0, "review": 0}
    async with await backend.connect() as conn:
        for _ in range(limit):
            token = str(uuid4())
            async with conn.transaction():
                cursor = await conn.execute(
                    "select id,recipient,sender,subject,body,attempts,"
                    "(first_attempt_at is not null and first_attempt_at < now()-interval '23 hours') "
                    "from toolbako_runtime.email_outbox "
                    "where ((status='pending' and available_at<=now()) or (status='sending' and lease_until<now())) "
                    + ("and id=any(%s::uuid[]) " if selected is not None else "") +
                    "order by created_at for update skip locked limit 1",
                    (selected,) if selected is not None else (),
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


def main(argv=None):
    parser = argparse.ArgumentParser(description="Inspect notification counts by default; sending requires --apply and a scope")
    parser.add_argument("--limit", type=int, default=20)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Inspect queue counts only (the default)")
    mode.add_argument("--apply", action="store_true", help="Enable delivery for the explicitly selected scope")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--message-id", action="append", help="Approved outbox UUID; repeat for up to 100 messages")
    scope.add_argument("--all-pending", action="store_true", help="Process the entire eligible queue, including old mail")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")
    if args.apply != bool(args.message_id or args.all_pending):
        parser.error("Sending requires --apply and either --message-id or --all-pending")
    selected = None
    if args.message_id:
        try:
            if len(args.message_id) > 100:
                raise ValueError
            selected = [UUID(value) for value in args.message_id]
        except (ValueError, AttributeError):
            # argparse's built-in type error would echo the input (possibly a
            # mistakenly pasted credential). Keep the error independent of it.
            parser.error("Expected 1..100 valid message UUIDs")
    from .config import settings
    try:
        backend = PostgresStateStore(settings.database_url, production=settings.is_deployed)
        if not args.apply:
            from .operations import inspect_mail
            result = asyncio.run(inspect_mail(backend))
            counts = result['counts']
            print(json.dumps({'dry_run': True, 'counts': counts, 'notice': result['notice']}, ensure_ascii=False))
            return 2 if any(counts[key] for key in ('mail_overdue', 'mail_expired_lease', 'mail_review', 'mail_missing_lease')) else 0
        async def bounded_delivery():
            # End before the next five-minute scheduler run. Cancellation leaves
            # the committed claim for lease recovery with the SAME provider key.
            async with asyncio.timeout(240):
                return await deliver_pending(backend, settings.email_api_key,
                                             limit=args.limit, message_ids=selected)
        result = asyncio.run(bounded_delivery())
    except Exception:
        print("Email worker failed. Check database, migration and email configuration. No credentials were printed.")
        return 1
    print(f"Email outbox: sent={result['sent']} retry={result['retry']} review={result['review']}")
    if result["retry"] or result["review"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
