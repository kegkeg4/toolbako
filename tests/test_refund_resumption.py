"""Durable local Postgres + MockTransport, never real refunds."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.config import Settings
from app.data import DemoStore
from app.database import StorageUnavailable
from app.finance import FinanceConflict, balance
from app.integrations import StripeIntegration, StripeOutcomeUnknown
from app.payment_reconciliation import propose_payment_recovery, approve_payment_recovery
from app.refund_resumption import propose_refund_resumption, approve_refund_resumption, refund_resumption_inventory
from app.refunds import request_order_refunds
from test_database import database, query
from test_finance import order as make_order
from test_payment_reconciliation import PaymentProvider, sender


class ResumeProvider(PaymentProvider):
    def __init__(self):
        super().__init__()
        self.timeouts = {"pi_main"}
        self.rejects = set()
        self.invalid = False

    def __call__(self, request):
        if request.method == "GET" and request.url.path == "/v1/refunds":
            self.requests.append(request)
            records = [deepcopy(x) for x in self.records if x.get("payment_intent") == request.url.params.get("payment_intent")]
            if self.duplicate and records: records.append({**records[0], "id": "re_duplicate"})
            return httpx.Response(200, json={"object": "list", "data": records, "has_more": self.incomplete})
        if request.method == "POST" and request.url.path == "/v1/refunds":
            data = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            payment_id = data["payment_intent"]
            if payment_id in self.rejects:
                self.requests.append(request)
                return httpx.Response(400, json={"error": {"message": "private provider error"}})
            try:
                super().__call__(request)
            except httpx.ReadTimeout:
                remote = self.records[-1]
                remote["id"] = "re_" + payment_id[3:]
                if payment_id in self.timeouts: raise
                if self.invalid: remote["amount"] += 1
                return httpx.Response(200, json=remote)
        return super().__call__(request)


@pytest.fixture
def provider(monkeypatch):
    result = ResumeProvider()
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(result), **kwargs))
    return result


async def seed(backend, dsn, provider, *, recover=True):
    at = datetime.now(timezone.utc)
    settings = Settings(environment="staging", database_url=dsn, stripe_secret_key="sk_test_resume",
                        stripe_charge_mode="separate", site_base_url="https://example.test",
                        admin_user_ids=("admin_one", "admin_two"))
    stripe = StripeIntegration(settings.stripe_secret_key, settings.site_base_url, charge_mode="separate", journal=backend)
    store = DemoStore(seed=False)
    async with backend.request(store):
        order = make_order(id="resume_order", status="cancel_pending", amount=1900, platform_fee=285,
                           primary_platform_fee=150, payment_reference="pi_main", tool_slug="test", sales_recorded=True,
                           buyer_email="buyer@example.test", seller_email="seller@example.test", cancel_requested_by="buyer",
                           refund_cancel_accepted_by="seller", refund_cancel_accepted_at=at,
                           pending_extras=[{"id": "e1", "amount": 400, "platform_fee": 60, "note": "extra one", "status": "paid", "payment_reference": "pi_one", "created_at": at},
                                           {"id": "e2", "amount": 500, "platform_fee": 75, "note": "extra two", "status": "paid", "payment_reference": "pi_two", "created_at": at}])
        store.orders.append(order)
        store.tools.append({"slug": "test", "sales_count": 1})
        for extra, part, amount in [(None, order, 1000), *((x, x, x["amount"]) for x in order["pending_extras"])]:
            metadata = {"order_id": order["id"]}
            if extra: metadata["extra_id"] = extra["id"]
            provider.payment(part["payment_reference"], amount, metadata)
        with pytest.raises(StripeOutcomeUnknown): await request_order_refunds(store, order, stripe)
        order.update(refund_status="review", payment_reconciliation_required=True)
        await backend.save()
    if recover:
        async with backend.request(store):
            order = store.orders[0]
            proposal = await propose_payment_recovery(backend, store, stripe, settings, order, "refund", "admin_one", at=at)
            await approve_payment_recovery(backend, store, stripe, settings, order, "refund", "admin_two", proposal["id"], sender(backend), at=at)
    return store, settings, stripe, at


async def resume(backend, store, settings, stripe, at):
    async with backend.request(store):
        order = store.orders[0]
        proposal = await propose_refund_resumption(backend, store, stripe, settings, order, "admin_one", at=at)
        return await approve_refund_resumption(backend, store, stripe, settings, order, "admin_two", proposal["id"], sender(backend), at=at)


def test_one_unsent_refund_per_four_eyes_approval_and_full_completion(database, provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await seed(backend, dsn, provider)
        assert await resume(backend, store, settings, stripe, at) == "partial"
        async with backend.request(store):
            assert store.orders[0]["refund_confirmed_amount"] == 1400 and balance(store, "seller") == 0
            assert store.orders[0]["status"] == "cancel_pending" and store.tools[0]["sales_count"] == 1
        assert await resume(backend, store, settings, stripe, at) == "completed"
        async with backend.request(store):
            assert store.orders[0]["refund_confirmed_amount"] == 1900 and store.orders[0]["status"] == "cancelled"
            assert store.tools[0]["sales_count"] == 0 and not refund_resumption_inventory(store)["items"]
        posts = [r for r in provider.requests if r.method == "POST"]
        assert [r.headers["Idempotency-Key"] for r in posts] == ["refund-resume_order", "refund-extra-resume_order-e1", "refund-extra-resume_order-e2"]
        assert query(dsn, "select sum(amount) from toolbako_runtime.finance_entries where kind='seller_refund'") == [(-1615,)]
        assert query(dsn, "select count(*) from toolbako_runtime.email_outbox") == [(1,)]
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["unknown", "rejected", "no_consent", "live_key", "production", "one_admin", "dispute", "duplicate_pi", "duplicate_extra", "amount", "manual_refund", "manual_partial", "pending_previous", "failed_previous", "duplicate_previous", "wrong_parent", "incomplete"])
def test_unsafe_resumption_does_not_send_another_refund(database, provider, failure):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await seed(backend, dsn, provider, recover=failure != "unknown")
        async with backend.request(store):
            order = store.orders[0]
            if failure == "rejected":
                await backend.begin_operation("refund-extra-resume_order-e1", "refunds", {"payment_intent": "pi_one"})
                await backend.finish_operation("refund-extra-resume_order-e1", "rejected")
            elif failure == "no_consent": order.pop("refund_cancel_accepted_by")
            elif failure == "live_key": settings = replace(settings, stripe_secret_key="sk_live_test")
            elif failure == "production": settings = replace(settings, environment="production")
            elif failure == "one_admin": settings = replace(settings, admin_user_ids=("admin_one",))
            elif failure == "dispute": order["dispute_status"] = "provider_dispute"
            elif failure == "duplicate_pi": order["pending_extras"][1]["payment_reference"] = "pi_one"
            elif failure == "duplicate_extra": order["pending_extras"][1]["id"] = "e1"
            elif failure == "amount": order["amount"] += 1
            elif failure == "manual_refund": provider.records.append({"id": "re_manual", "payment_intent": "pi_one"})
            elif failure == "manual_partial": provider.payments["pi_one"]["latest_charge"]["amount_refunded"] = 1
            elif failure == "pending_previous": provider.records[0]["status"] = "pending"
            elif failure == "failed_previous": provider.records[0]["status"] = "failed"
            elif failure == "duplicate_previous": provider.duplicate = True
            elif failure == "wrong_parent": provider.payments["pi_one"]["livemode"] = True
            elif failure == "incomplete": provider.incomplete = True
            with pytest.raises((FinanceConflict, RuntimeError)):
                await propose_refund_resumption(backend, store, stripe, settings, order, "admin_one", at=at)
            if failure in {"duplicate_pi", "amount"}:
                with pytest.raises(FinanceConflict): balance(store, "seller")
            else:
                assert balance(store, "seller") == 0
        assert sum(r.method == "POST" for r in provider.requests) == 1
    asyncio.run(run())


def test_second_review_rechecks_facts_expiry_identity_and_rejects_same_actor(database, provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await seed(backend, dsn, provider)
        async with backend.request(store):
            order = store.orders[0]
            proposal = await propose_refund_resumption(backend, store, stripe, settings, order, "admin_one", at=at)
            for actor, proposal_id, when in [("admin_one", proposal["id"], at), ("outsider", proposal["id"], at),
                                              ("admin_two", "stale", at), ("admin_two", proposal["id"], at + timedelta(minutes=31))]:
                with pytest.raises(FinanceConflict):
                    await approve_refund_resumption(backend, store, stripe, settings, order, actor, proposal_id, sender(backend), at=when)
            provider.payments["pi_two"]["latest_charge"]["disputed"] = True
            with pytest.raises(FinanceConflict):
                await approve_refund_resumption(backend, store, stripe, settings, order, "admin_two", proposal["id"], sender(backend), at=at)
        assert sum(r.method == "POST" for r in provider.requests) == 1
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["timeout", "rejected", "invalid"])
def test_failed_new_refund_holds_and_cannot_be_automatically_retried(database, provider, failure):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await seed(backend, dsn, provider)
        if failure == "timeout": provider.timeouts.add("pi_one")
        elif failure == "rejected": provider.rejects.add("pi_one")
        else: provider.invalid = True
        with pytest.raises((StripeOutcomeUnknown, FinanceConflict, RuntimeError)):
            await resume(backend, store, settings, stripe, at)
        async with backend.request(store):
            assert store.orders[0]["refund_status"] == "review" and store.orders[0]["payment_reconciliation_required"]
            assert store.orders[0]["refund_resumption"]["status"] == "review"
            with pytest.raises(FinanceConflict):
                await propose_refund_resumption(backend, store, stripe, settings, store.orders[0], "admin_one", at=at)
        assert sum(r.method == "POST" for r in provider.requests) == 2
    asyncio.run(run())


def test_save_failure_after_success_is_restored_from_get_without_reposting(database, provider, monkeypatch):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await seed(backend, dsn, provider)
        original = backend.save
        async def save():
            if store.orders[0].get("refund_resumption", {}).get("status") == "executed":
                raise StorageUnavailable("simulated post-response crash")
            await original()
        monkeypatch.setattr(backend, "save", save)
        with pytest.raises(StorageUnavailable): await resume(backend, store, settings, stripe, at)
        monkeypatch.setattr(backend, "save", original)
        # The next proposal restores the known success, then sends ONLY e2.
        assert await resume(backend, store, settings, stripe, at) == "completed"
        assert sum(r.method == "POST" for r in provider.requests) == 3
        assert query(dsn, "select count(*) from toolbako_runtime.email_outbox") == [(1,)]
    asyncio.run(run())


def test_admin_route_requires_identity_sandbox_mfa_and_known_order(monkeypatch):
    import app.main as main
    async def run():
        request = Request({"type": "http", "method": "POST", "path": "/admin/orders/o/refund-resumption/propose", "headers": [], "session": {}})
        monkeypatch.setattr(main, "current_user", lambda request: None)
        with pytest.raises(HTTPException) as error: await main.admin_resume_refund(request, "o", "propose", "")
        assert error.value.status_code == 403
        monkeypatch.setattr(main, "current_user", lambda request: {"id": "admin_one"})
        monkeypatch.setattr(main, "is_admin", lambda user: True)
        monkeypatch.setattr(main, "database_store", None)
        with pytest.raises(HTTPException) as error: await main.admin_resume_refund(request, "o", "propose", "")
        assert error.value.status_code == 409
        monkeypatch.setattr(main, "database_store", object())
        monkeypatch.setattr(main, "sandbox_payouts_ready", lambda settings: True)
        monkeypatch.setattr(main.mfa, "recent", lambda request: False)
        response = await main.admin_resume_refund(request, "o", "propose", "")
        assert response.status_code == 303 and response.headers["location"].startswith("/security/mfa")
        monkeypatch.setattr(main.mfa, "recent", lambda request: True)
        with pytest.raises(HTTPException) as error: await main.admin_resume_refund(request, "missing", "propose", "")
        assert error.value.status_code == 404
    asyncio.run(run())


def test_timeout_is_reconciled_before_remaining_refund_can_resume(database, provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await seed(backend, dsn, provider)
        provider.timeouts.add("pi_one")
        with pytest.raises(StripeOutcomeUnknown): await resume(backend, store, settings, stripe, at)
        async with backend.request(store):
            order = store.orders[0]
            proposal = await propose_payment_recovery(backend, store, stripe, settings, order, "refund_extra", "admin_one", extra_id="e1", at=at)
            await approve_payment_recovery(backend, store, stripe, settings, order, "refund_extra", "admin_two", proposal["id"], sender(backend), extra_id="e1", at=at)
        assert await resume(backend, store, settings, stripe, at) == "completed"
        assert sum(r.method == "POST" for r in provider.requests) == 3
        assert query(dsn, "select count(*) from toolbako_runtime.email_outbox") == [(1,)]
    asyncio.run(run())


def test_pending_response_never_completes_and_waits_for_full_provider_success(database, provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await seed(backend, dsn, provider)
        provider.refund_status = "pending"
        assert await resume(backend, store, settings, stripe, at) == "partial"
        async with backend.request(store):
            assert store.orders[0]["pending_extras"][0]["refund_status"] == "pending"
            assert store.orders[0]["refund_confirmed_amount"] == 1000
            with pytest.raises(FinanceConflict):
                await propose_refund_resumption(backend, store, stripe, settings, store.orders[0], "admin_one", at=at)
        provider.records[-1]["status"] = "succeeded"
        provider.payments["pi_one"]["latest_charge"].update(amount_refunded=400, refunded=True)
        provider.refund_status = "succeeded"
        assert await resume(backend, store, settings, stripe, at) == "completed"
        assert sum(r.method == "POST" for r in provider.requests) == 3
    asyncio.run(run())


def test_last_success_after_snapshot_crash_restores_state_with_get_only(database, provider, monkeypatch):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await seed(backend, dsn, provider)
        await resume(backend, store, settings, stripe, at)
        original = backend.save
        async def save():
            if store.orders[0].get("refund_resumption", {}).get("status") == "executed":
                raise StorageUnavailable("last response snapshot failed")
            await original()
        monkeypatch.setattr(backend, "save", save)
        with pytest.raises(StorageUnavailable): await resume(backend, store, settings, stripe, at)
        monkeypatch.setattr(backend, "save", original)
        assert sum(r.method == "POST" for r in provider.requests) == 3
        assert await resume(backend, store, settings, stripe, at) == "completed"
        assert sum(r.method == "POST" for r in provider.requests) == 3
        assert query(dsn, "select count(*) from toolbako_runtime.email_outbox") == [(1,)]
    asyncio.run(run())


def test_commit_failure_before_approval_cannot_send_a_refund(database, provider, monkeypatch):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await seed(backend, dsn, provider)
        async with backend.request(store):
            proposal = await propose_refund_resumption(backend, store, stripe, settings, store.orders[0], "admin_one", at=at)
        original = backend.save
        async def fail(): raise StorageUnavailable("approval commit failed")
        monkeypatch.setattr(backend, "save", fail)
        with pytest.raises(StorageUnavailable):
            async with backend.request(store):
                await approve_refund_resumption(backend, store, stripe, settings, store.orders[0], "admin_two", proposal["id"], sender(backend), at=at)
        monkeypatch.setattr(backend, "save", original)
        async with backend.request(store):
            assert store.orders[0]["refund_resumption"]["status"] == "proposed"
        assert sum(r.method == "POST" for r in provider.requests) == 1
    asyncio.run(run())


def test_cancellation_route_records_consent_but_does_not_finish_before_confirmation(monkeypatch):
    import app.main as main
    async def run():
        store = DemoStore(seed=False)
        order = make_order(id="consent_order", status="cancel_pending", cancel_requested_by="buyer")
        store.orders.append(order)
        monkeypatch.setattr(main, "store", store)
        monkeypatch.setattr(main, "settings", Settings(environment="staging", demo_mode=False, site_base_url="https://example.test"))
        monkeypatch.setattr(main, "current_user", lambda request: {"id": "seller"})
        calls = []
        async def refunds(store, order, stripe):
            calls.append(order["refund_cancel_accepted_by"])
            order["refund_status"] = "pending"
        async def email(*args): return True
        monkeypatch.setattr(main, "request_order_refunds", refunds)
        monkeypatch.setattr(main, "send_email_safely", email)
        request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
        assert (await main.order_transition(request, order["id"], "cancel_accept", "")).status_code == 303
        assert order["status"] == "cancel_pending" and order["refund_cancel_accepted_by"] == "seller"
        with pytest.raises(HTTPException) as error: await main.order_transition(request, order["id"], "cancel_accept", "")
        assert error.value.status_code == 409 and calls == ["seller"]
    asyncio.run(run())


def test_new_template_does_not_show_actions_when_recovery_disabled():
    from jinja2 import Environment, FileSystemLoader
    from pathlib import Path
    env = Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"), autoescape=True)
    proposal = {"status": "proposed", "id": "proposal", "proposed_by": "admin_one", "remaining_count": 1, "next_amount": 400, "next_operation": "refund-extra-o-e1"}
    finance = {"sellers": [], "transactions": [], "payouts": [], **{key: 0 for key in ("gross_paid", "completed_gmv", "platform_fee_earned", "processor_fee_total", "available_to_payout", "paid_out", "held", "refund_total", "disputed_total", "blocked_for_payout")}}
    for ready in (False, True):
        html = env.get_template("_admin_finance.html").render(finance=finance, finance_controls_ready=False,
            payment_recovery_ready=ready, payment_recoveries={"items": [], "truncated": False},
            refund_resumptions={"items": [{"order_id": "o", "tool_name": "<script>private</script>", "proposal": proposal}], "truncated": False}, user={"id": "admin_two"})
        assert "<script>private</script>" not in html
        assert ("/refund-resumption/approve" in html) is ready
        assert "新たなテスト返金を1件だけ作成" in html if ready else "本番では使えません" in html


@pytest.mark.parametrize("status", ["processing", "pending", "partial", "review", "completed"])
def test_transaction_shows_refund_facts_without_repeat_cancellation_buttons(status):
    from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader
    from pathlib import Path
    env = Environment(loader=ChoiceLoader([DictLoader({"base.html": "{% block content %}{% endblock %}"}),
                                          FileSystemLoader(Path(__file__).resolve().parents[1] / "templates")]), autoescape=True)
    order = make_order(status="cancel_pending", refund_status=status, refund_confirmed_amount=1000 if status == "completed" else 400,
                       seller_name="Seller", cancel_reason="Reason", cancel_requested_by="buyer", messages=[], delivery=None)
    html = env.get_template("transaction.html").render(order=order, user={"id": "seller"}, room_open=False,
        review_open=False, demo_mode=False, status_labels={"cancel_pending": "キャンセル申請中"})
    assert "/actions/cancel_accept" not in html and "/actions/cancel_reject" not in html
    assert "全額返金を確認しました" in html if status == "completed" else "返金の結果を確認しています" in html
