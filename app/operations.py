"""Read-only operational backlog diagnostics; never sends email or money."""
import asyncio
import json
from datetime import datetime, timedelta, timezone


async def operation_summary(backend, settings, *, at=None):
    conn = backend._connection
    if conn is None:
        raise RuntimeError("運用状況の確認にはDB接続が必要です")
    return await connection_summary(conn, settings, at=at)


async def inspect_operations(backend, settings, *, at=None):
    """Independent snapshot: no marketplace initialization or request lock."""
    async with await backend.connect() as conn:
        async with conn.transaction():
            await conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            await backend._check_schema(conn)
            return await connection_summary(conn, settings, at=at)


async def inspect_mail(backend, *, at=None):
    """Mail-only snapshot; a worker needs no marketplace or financial access."""
    async with await backend.connect() as conn:
        async with conn.transaction():
            await conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            await backend._check_schema(conn)
            return {"counts": await mail_counts(conn, at=at),
                    "notice": "Read-only mail counts; no claim or send. Zero does not prove delivery or worker health."}


async def mail_counts(conn, *, at=None):
    at = at or datetime.now(timezone.utc)
    mail = await (await conn.execute(
        "select count(*) filter(where status='pending'),"
        "count(*) filter(where status='pending' and available_at<=%s and created_at<%s),"
        "count(*) filter(where status='sending' and lease_until<%s),"
        "count(*) filter(where status='review'),"
        "count(*) filter(where status='sending'),"
        "count(*) filter(where status='sending' and lease_until is null) from toolbako_runtime.email_outbox",
        (at, at - timedelta(minutes=15), at),
    )).fetchone()
    return dict(zip(("mail_pending", "mail_overdue", "mail_expired_lease", "mail_review", "mail_sending", "mail_missing_lease"), mail))


async def connection_summary(conn, settings, *, at=None):
    at = at or datetime.now(timezone.utc)
    counts = await mail_counts(conn, at=at)
    payments = await (await conn.execute(
        "select count(*) filter(where status in ('pending','unknown')),"
        "count(*) filter(where status='pending' and created_at<%s),"
        "count(*) filter(where status='rejected') from toolbako_runtime.stripe_operations",
        (at - timedelta(minutes=5),),
    )).fetchone()
    payouts = await (await conn.execute(
        "select count(*) filter(where status='review'),count(*) filter(where status='bank_failed') "
        "from toolbako_runtime.finance_payouts"
    )).fetchone()
    counts.update(zip(("payment_unknown", "payment_stale", "payment_rejected", "payout_review", "payout_failed"), (*payments, *payouts)))
    alerts = []
    if not settings.email_ready: alerts.append("取引メールが未接続です。通知workerの配備と送信確認が必要です。")
    if not settings.redis_url: alerts.append("複数台共通のアクセス制限が未接続です。")
    if not settings.clamav_host: alerts.append("納品ファイルのウイルス検査が未接続です。ファイル納品は停止しています。")
    if not settings.sentry_dsn: alerts.append("外部エラー監視と通知先が未接続です。")
    if counts["mail_overdue"] or counts["mail_expired_lease"]: alerts.append("メールの滞留・処理期限超過があります。workerとプロバイダーを確認してください。")
    if counts["mail_review"]: alerts.append("送信結果を運営で確認するメールがあります。無条件に再送しないでください。")
    if counts["mail_missing_lease"]: alerts.append("処理期限が記録されていない送信中メールがあります。送信結果を確認し、無条件に再送しないでください。")
    if counts["payment_unknown"] or counts["payout_review"]: alerts.append("結果不明の決済・振込があります。二者照合が必要です。再送しないでください。")
    if counts["payment_rejected"] or counts["payout_failed"]: alerts.append("拒否・失敗した決済・振込があります。元の要求と資金を確認してください。")
    return {"counts": counts, "alerts": alerts, "checked_at": at,
            "notice": "読取専用の滞留確認です。0件でもworkerの稼働・通知到達・実決済の成功を証明しません。自動再送・自動送金は行いません。"}


def main():
    from .config import settings
    from .database import PostgresStateStore
    async def inspect():
        backend = PostgresStateStore(settings.database_url, production=settings.is_deployed)
        return await inspect_operations(backend, settings)
    try:
        result = asyncio.run(inspect())
        print(json.dumps({"counts": result["counts"], "alerts": result["alerts"], "notice": result["notice"]}, ensure_ascii=False))
        return 2 if result["alerts"] else 0
    except Exception:
        print("運用状況を確認できません。DB接続・設定を確認してください。秘密値は表示していません。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
