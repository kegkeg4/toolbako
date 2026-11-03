"""Durable local Postgres + MockTransport, never real refunds."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.config import Settings
from app.data import DemoStore
from app.database import PostgresStateStore, StorageUnavailable, content_hash, snapshot
from app.finance import FinanceConflict, balance
from app.financial_scope import FinancialScope, FinancialScopeError
from app.integrations import StripeIntegration, StripeOutcomeUnknown
from app.mfa import SupabaseMFA
from app.security import SessionService
from app.payment_reconciliation import propose_payment_recovery, approve_payment_recovery, refund_request_data
from app.refund_resumption import (
    propose_refund_resumption, approve_refund_resumption, refund_resumption_inventory,
    inspect_refund_resumption_live,
)
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
        self.scope = None
        self.wrong_platform = False
        self.wrong_balance_mode = False
        self.partial_race = False

    def __call__(self, request):
        if self.scope is not None and request.method == "GET" and request.url.path in {"/v1/account", "/v1/balance"}:
            self.requests.append(request)
            if request.url.path == "/v1/account":
                return httpx.Response(200, json={"object": "account", "id": "acct_other" if self.wrong_platform else self.scope.platform_account})
            return httpx.Response(200, json={"object": "balance", "livemode": not self.wrong_balance_mode})
        if request.method == "GET" and request.url.path == "/v1/refunds":
            self.requests.append(request)
            records = [deepcopy(x) for x in self.records if x.get("payment_intent") == request.url.params.get("payment_intent")]
            if self.duplicate and records: records.append({**records[0], "id": "re_duplicate"})
            return httpx.Response(200, json={"object": "list", "data": records, "has_more": self.incomplete})
        if request.method == "POST" and request.url.path == "/v1/refunds":
            data = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            payment_id = data["payment_intent"]
            assert data["amount"] == str(self.payments[payment_id]["amount"])
            if self.partial_race:
                self.requests.append(request)
                self.payments[payment_id]["latest_charge"]["amount_refunded"] = 1
                return httpx.Response(400, json={"error": {"message": "synthetic amount exceeds remaining"}})
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


async def seed(backend, dsn, provider, *, recover=True, initial=False):
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
        if initial:
            order.update(refund_status="awaiting_approval")
            provider.timeouts.clear()
        else:
            with pytest.raises(StripeOutcomeUnknown): await request_order_refunds(store, order, stripe)
            order.update(refund_status="review", payment_reconciliation_required=True)
        await backend.save()
    if recover and not initial:
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
        async def refunds(*args, **kwargs):
            calls.append(True)
            raise AssertionError("Party consent must never send a refund")
        async def email(*args): return True
        async def persist(): return None
        monkeypatch.setattr(main.stripe, "refund_payment", refunds)
        monkeypatch.setattr(main, "persist_state", persist)
        monkeypatch.setattr(main, "send_email_safely", email)
        request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
        assert (await main.order_transition(request, order["id"], "cancel_accept", "")).status_code == 303
        assert order["status"] == "cancel_pending" and order["refund_cancel_accepted_by"] == "seller"
        assert order["refund_status"] == "awaiting_approval" and calls == []
        with pytest.raises(HTTPException) as error: await main.order_transition(request, order["id"], "cancel_accept", "")
        assert error.value.status_code == 409 and calls == []
    asyncio.run(run())


@pytest.mark.parametrize("recovery_ready,execution_ready,inspection_available", [
    (False, None, False), (True, None, False),
    (False, False, True), (True, False, True),
    (False, True, True), (True, True, True),
])
@pytest.mark.parametrize("actor", ["admin_one", "admin_two"])
def test_new_template_does_not_show_actions_when_recovery_disabled(
    recovery_ready, execution_ready, inspection_available, actor,
):
    from jinja2 import Environment, FileSystemLoader
    from pathlib import Path
    env = Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"), autoescape=True)
    proposal = {"status": "proposed", "id": "proposal", "proposed_by": "admin_one", "remaining_count": 1, "next_amount": 400, "next_operation": "refund-extra-o-e1"}
    finance = {"sellers": [], "transactions": [], "payouts": [], **{key: 0 for key in ("gross_paid", "completed_gmv", "platform_fee_earned", "processor_fee_total", "available_to_payout", "paid_out", "held", "refund_total", "disputed_total", "blocked_for_payout")}}
    context = dict(finance=finance, finance_controls_ready=False,
        payment_recovery_ready=recovery_ready, payment_recoveries={"items": [], "truncated": False},
        refund_live_inspection_available=inspection_available,
        refund_resumptions={"items": [{"order_id": "o", "tool_name": "<script>private</script>", "proposal": proposal}], "truncated": False},
        user={"id": actor})
    if execution_ready is not None:
        context["refund_execution_ready"] = execution_ready
    html = env.get_template("_admin_finance.html").render(**context)
    # Paid-out refunds have their own independent execution-stop notice.
    # Inspect only this ordinary-refund panel when checking its operation flag.
    _, separator, panel = html.partition('<details id="refund-resumption"')
    assert separator
    panel = panel.split("</details>", 1)[0]
    enabled = recovery_ready if execution_ready is None else execution_ready
    assert "<script>private</script>" not in html
    assert ("/refund-resumption/approve" in panel) is (enabled and actor != proposal["proposed_by"])
    assert ("/refund-resumption/propose" in panel) is enabled
    assert ("/refund-inspection" in panel) is inspection_available
    assert ("返金実行は停止中です。" in panel) is (not enabled)
    assert "結果不明の返金は再送しません。" in panel
    if enabled and actor != proposal["proposed_by"]:
        assert "別の管理者として返金を承認" in panel
    if actor == proposal["proposed_by"]:
        assert "別の管理者による承認を待っています" in panel


@pytest.mark.parametrize("status", ["awaiting_approval", "processing", "pending", "partial", "review", "completed"])
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


def test_first_refund_requires_two_admins_and_persists_before_post(database, provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await seed(backend, dsn, provider, initial=True)
        async with backend.request(store):
            order = store.orders[0]
            assert balance(store, "seller") == 0
            assert refund_resumption_inventory(store)["items"]
            proposal = await propose_refund_resumption(backend, store, stripe, settings, order, "admin_one", at=at)
            assert not any(r.method == "POST" for r in provider.requests)
            for actor, token, when in [("admin_one", proposal["id"], at), ("outsider", proposal["id"], at),
                                       ("admin_two", "stale", at), ("admin_two", proposal["id"], at + timedelta(minutes=31))]:
                with pytest.raises(FinanceConflict):
                    await approve_refund_resumption(backend, store, stripe, settings, order, actor, token, sender(backend), at=when)
            assert not any(r.method == "POST" for r in provider.requests)
        # Re-load from the durable database before the second administrator acts.
        async with backend.request(store):
            assert await approve_refund_resumption(backend, store, stripe, settings, store.orders[0], "admin_two", proposal["id"], sender(backend), at=at) == "partial"
            with pytest.raises(FinanceConflict):
                await approve_refund_resumption(backend, store, stripe, settings, store.orders[0], "admin_two", proposal["id"], sender(backend), at=at)
        assert sum(r.method == "POST" for r in provider.requests) == 1
        first_post = next(r for r in provider.requests if r.method == "POST")
        assert parse_qs(first_post.content.decode())["amount"] == ["1000"]
        assert await resume(backend, store, settings, stripe, at) == "partial"
        assert await resume(backend, store, settings, stripe, at) == "completed"
        assert sum(r.method == "POST" for r in provider.requests) == 3
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["same_party", "missing_requester", "manual_refund", "amount_changed", "dispute", "live", "save_failure"])
def test_first_refund_fails_closed_before_any_send(database, provider, monkeypatch, failure):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await seed(backend, dsn, provider, initial=True)
        original_save = backend.save
        async with backend.request(store):
            order = store.orders[0]
            proposal = await propose_refund_resumption(backend, store, stripe, settings, order, "admin_one", at=at)
            if failure == "same_party": order["cancel_requested_by"] = "seller"
            elif failure == "missing_requester": order.pop("cancel_requested_by")
            elif failure == "manual_refund": provider.records.append({"id": "re_manual", "payment_intent": "pi_main"})
            elif failure == "amount_changed": order["amount"] += 1
            elif failure == "dispute": order["dispute_status"] = "opened"
            elif failure == "live": settings = replace(settings, stripe_secret_key="sk_live_not_real")
            elif failure == "save_failure":
                async def fail(): raise StorageUnavailable("approval commit failed")
                monkeypatch.setattr(backend, "save", fail)
            with pytest.raises((FinanceConflict, StorageUnavailable)):
                await approve_refund_resumption(backend, store, stripe, settings, order, "admin_two", proposal["id"], sender(backend), at=at)
            if failure == "save_failure": monkeypatch.setattr(backend, "save", original_save)
        assert not any(r.method == "POST" for r in provider.requests)
    asyncio.run(run())


@pytest.mark.parametrize("amount", [None, True, False, "1000", 1000.0, 0, -1])
def test_new_refund_api_rejects_non_exact_amount_before_provider_io(amount):
    stripe = StripeIntegration("sk_test_synthetic", "https://example.test", charge_mode="separate")
    with pytest.raises(RuntimeError):
        stripe.refund_data("pi_one", "order", amount=amount)
    with pytest.raises(RuntimeError):
        asyncio.run(stripe.refund_payment("pi_one", "order", amount=amount))


def test_new_refund_api_cannot_omit_amount():
    stripe = StripeIntegration("sk_test_synthetic", "https://example.test", charge_mode="separate")
    with pytest.raises(TypeError):
        stripe.refund_data("pi_one", "order")
    with pytest.raises(TypeError):
        stripe.refund_payment("pi_one", "order")


def legacy_record(backend, store, stripe, provider, at, status):
    async def create():
        async with backend.request(store):
            order = store.orders[0]
            # Hand-build the historic request. The new API cannot generate it.
            legacy = {"payment_intent": "pi_main", "metadata[order_id]": order["id"]}
            key = "refund-" + order["id"]
            remote = {"object": "refund", "id": "re_legacy", "payment_intent": "pi_main", "charge": "ch_main",
                      "amount": 1000, "currency": "jpy", "status": "succeeded", "created": int(at.timestamp()),
                      "metadata": {"order_id": order["id"]}}
            await backend.begin_operation(key, "refunds", legacy)
            await backend.finish_operation(key, status, remote if status == "succeeded" else None)
            provider.records.append(remote)
            provider.payments["pi_main"]["latest_charge"].update(amount_refunded=1000, refunded=True)
            order.update(refund_status="review", payment_reconciliation_required=True)
            await backend.save()
            return key, content_hash(legacy)
    return create()


def test_legacy_unknown_refund_reconciles_using_original_hash_without_post(database, provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await seed(backend, dsn, provider, initial=True)
        key, digest = await legacy_record(backend, store, stripe, provider, at, "unknown")
        async with backend.request(store):
            proposal = await propose_payment_recovery(backend, store, stripe, settings, store.orders[0], "refund", "admin_one", at=at)
            assert proposal["request_hash"] == digest
            await approve_payment_recovery(backend, store, stripe, settings, store.orders[0], "refund", "admin_two", proposal["id"], sender(backend), at=at)
        assert query(dsn, "select request_hash,status from toolbako_runtime.stripe_operations where operation_id=%s", (key,)) == [(digest, "succeeded")]
        assert not any(r.method == "POST" for r in provider.requests)
    asyncio.run(run())


def test_legacy_success_allows_only_next_explicit_amount_refund(database, provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await seed(backend, dsn, provider, initial=True)
        key, digest = await legacy_record(backend, store, stripe, provider, at, "succeeded")
        assert await resume(backend, store, settings, stripe, at) == "partial"
        posts = [r for r in provider.requests if r.method == "POST"]
        assert len(posts) == 1
        assert posts[0].headers["Idempotency-Key"] == "refund-extra-resume_order-e1"
        assert parse_qs(posts[0].content.decode())["amount"] == ["400"]
        assert query(dsn, "select request_hash from toolbako_runtime.stripe_operations where operation_id=%s", (key,)) == [(digest,)]
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["different_amount", "different_payment", "different_order", "different_extra", "different_endpoint", "wrong_amount_hash"])
def test_legacy_unknown_evidence_cannot_expand_to_another_target(database, provider, failure):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await seed(backend, dsn, provider, initial=True)
        key, digest = await legacy_record(backend, store, stripe, provider, at, "unknown")
        async with backend.request(store):
            order = store.orders[0]
            if failure == "different_amount": order["primary_payment_amount"] += 1
            elif failure == "different_payment": order["payment_reference"] = "pi_elsewhere"
            elif failure == "different_order": order["id"] = "other_order"
            elif failure == "different_extra":
                provider.records[0]["metadata"]["extra_id"] = "different_extra"
            elif failure == "different_endpoint":
                await backend._connection.execute("update toolbako_runtime.stripe_operations set endpoint='transfers' where operation_id=%s", (key,))
            else:
                bad = {"payment_intent": "pi_main", "amount": "999", "metadata[order_id]": order["id"]}
                await backend._connection.execute("update toolbako_runtime.stripe_operations set request_hash=%s where operation_id=%s", (content_hash(bad), key))
            with pytest.raises((FinanceConflict, RuntimeError)):
                await propose_payment_recovery(backend, store, stripe, settings, order, "refund", "admin_one", at=at)
        assert not any(r.method == "POST" for r in provider.requests)
        assert query(dsn, "select status from toolbako_runtime.stripe_operations where operation_id=%s", (key,)) == [("unknown",)]
    asyncio.run(run())


def test_external_partial_refund_between_get_and_post_never_changes_approved_amount(database, provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await seed(backend, dsn, provider, initial=True)
        provider.partial_race = True
        with pytest.raises(RuntimeError):
            await resume(backend, store, settings, stripe, at)
        posts = [r for r in provider.requests if r.method == "POST"]
        assert len(posts) == 1 and parse_qs(posts[0].content.decode())["amount"] == ["1000"]
        async with backend.request(store):
            assert store.orders[0]["refund_status"] == "review"
            with pytest.raises(FinanceConflict):
                await propose_refund_resumption(backend, store, stripe, settings, store.orders[0], "admin_one", at=at)
        assert sum(r.method == "POST" for r in provider.requests) == 1
        assert query(dsn, "select status from toolbako_runtime.stripe_operations where operation_id='refund-resume_order'") == [("rejected",)]
    asyncio.run(run())


async def seed_live(backend, dsn, provider, *, bind=True):
    at = datetime.now(timezone.utc)
    scope = FinancialScope("live", "acct_platform", str(uuid4()))
    provider.scope = scope
    settings = Settings(environment="staging", demo_mode=False, database_url=dsn,
                        stripe_secret_key="rk_live_SYNTHETIC", stripe_charge_mode="separate",
                        stripe_connection_mode="live_read_only", stripe_platform_account_id=scope.platform_account,
                        stripe_ledger_id=scope.ledger_id, site_base_url="https://example.test",
                        admin_user_ids=("admin_one", "admin_two"))
    stripe = StripeIntegration(settings.stripe_secret_key, settings.site_base_url,
                               charge_mode="separate", journal=backend, financial_scope=scope)
    store = DemoStore(seed=False)
    sessions = SessionService(store, 3600)
    mfa = SupabaseMFA(settings, sessions)
    request = Request({"type": "http", "method": "GET", "headers": [], "session": {}})
    async with backend.request(store):
        if bind:
            await backend.bind_empty_financial_scope(scope)
        for name in settings.admin_user_ids:
            store.registered_users[name] = {"id": name, "username": name, "email_verified": True}
        sessions.establish(request, store.registered_users["admin_one"])
        store.account_sessions[request.session["sid"]].update(mfa_verified_at=at, mfa_token_expires_at=at + timedelta(hours=1))
        order = make_order(id="live_order", status="cancel_pending", payment_reference="pi_live", amount=1000,
                           cancel_requested_by="buyer", refund_cancel_accepted_by="seller", refund_cancel_accepted_at=at,
                           refund_status="awaiting_approval", pending_extras=[], tool_slug="test")
        store.orders.append(order)
        store.tools.append({"slug": "test", "sales_count": 1})
        metadata = {"order_id": order["id"], "ledger_id": scope.ledger_id}
        provider.payment("pi_live", 1000, metadata)
        payment = provider.payments["pi_live"]
        payment["livemode"] = payment["latest_charge"]["livemode"] = True
        payment["latest_charge"]["metadata"] = dict(metadata)
        await backend.save()
    return store, settings, stripe, mfa, request, scope, at


def test_live_inspection_is_fresh_get_only_and_does_not_persist_or_enable_execution(database, provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, mfa, request, scope, at = await seed_live(backend, dsn, provider)
        async with backend.request(store):
            before = deepcopy(snapshot(store))
            result = await inspect_refund_resumption_live(backend, store, stripe, settings, mfa, request, order_id="live_order", at=at)
            assert result["scope"] == scope.payload() and result["execution_allowed"] is False
            assert result["remaining_amount"] == 1000 and result["remaining_count"] == 1
            assert snapshot(store) == before
            with pytest.raises(FinanceConflict):
                await propose_refund_resumption(backend, store, stripe, settings, store.orders[0], "admin_one", at=at)
            with pytest.raises(RuntimeError):
                await stripe.refund_payment("pi_live", "live_order", amount=1000)
            provider.payments["pi_live"]["latest_charge"]["disputed"] = True
            with pytest.raises(FinanceConflict):
                await inspect_refund_resumption_live(backend, store, stripe, settings, mfa, request, order_id="live_order", at=at)
        assert all(r.method == "GET" and "stripe-account" not in r.headers for r in provider.requests)
        assert query(dsn, "select count(*) from toolbako_runtime.stripe_operations where endpoint='refunds'") == [(0,)]
        assert query(dsn, "select count(*) from toolbako_runtime.email_outbox") == [(0,)]
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["unbound", "wrong_ledger", "wrong_platform", "wrong_balance_mode", "test_key", "different_key", "different_backend",
    "different_store", "no_boundary", "no_session", "stale_mfa", "banned_admin", "deleted_second_admin", "not_admin", "duplicate_order",
    "test_payment", "test_charge", "payment_amount", "charge_amount", "charge_parent", "charge_currency", "payment_metadata", "charge_metadata",
    "payment_ledger", "charge_ledger", "partial_refund", "manual_refund", "unknown_intent", "paid_out_case", "reserved_payout", "incomplete", "missing_consent"])
def test_live_inspection_rejects_unproved_context_or_parent_without_post(database, provider, failure):
    backend, dsn = database
    async def run():
        store, settings, stripe, mfa, request, scope, at = await seed_live(backend, dsn, provider, bind=failure != "unbound")
        if failure == "no_boundary":
            with pytest.raises(FinanceConflict):
                await inspect_refund_resumption_live(backend, store, stripe, settings, mfa, request, order_id="live_order", at=at)
            return
        async with backend.request(store):
            order = store.orders[0]
            payment, charge = provider.payments["pi_live"], provider.payments["pi_live"]["latest_charge"]
            if failure == "wrong_ledger":
                scope = FinancialScope("live", scope.platform_account, str(uuid4()))
                settings = replace(settings, stripe_ledger_id=scope.ledger_id)
                stripe.financial_scope = scope
                mfa.settings = settings
            elif failure == "wrong_platform": provider.wrong_platform = True
            elif failure == "wrong_balance_mode": provider.wrong_balance_mode = True
            elif failure == "test_key":
                settings = replace(settings, stripe_secret_key="sk_test_SYNTHETIC")
                stripe.secret_key = settings.stripe_secret_key
                mfa.settings = settings
            elif failure == "different_key": stripe.secret_key = "rk_live_other"
            elif failure == "different_backend": stripe.journal = PostgresStateStore(dsn)
            elif failure == "different_store": mfa.sessions.store = DemoStore(seed=False)
            elif failure == "no_session": request.session.clear()
            elif failure == "stale_mfa": store.account_sessions[request.session["sid"]]["mfa_verified_at"] = at - timedelta(minutes=11)
            elif failure == "banned_admin": store.registered_users["admin_one"]["is_banned"] = True
            elif failure == "deleted_second_admin": store.registered_users["admin_two"]["deleted_at"] = at
            elif failure == "not_admin":
                settings = replace(settings, admin_user_ids=("outsider", "admin_two"))
                mfa.settings = settings
            elif failure == "duplicate_order": store.orders.append(deepcopy(order))
            elif failure == "test_payment": payment["livemode"] = False
            elif failure == "test_charge": charge["livemode"] = False
            elif failure == "payment_amount": payment["amount"] = 999
            elif failure == "charge_amount": charge["amount_captured"] = 999
            elif failure == "charge_parent": charge["payment_intent"] = "pi_elsewhere"
            elif failure == "charge_currency": charge["currency"] = "usd"
            elif failure == "payment_metadata": payment["metadata"]["order_id"] = "another_order"
            elif failure == "charge_metadata": charge["metadata"]["order_id"] = "another_order"
            elif failure == "payment_ledger": payment["metadata"]["ledger_id"] = str(uuid4())
            elif failure == "charge_ledger": charge["metadata"].pop("ledger_id")
            elif failure == "partial_refund": charge["amount_refunded"] = 1
            elif failure == "manual_refund": provider.records.append({"id": "re_manual", "payment_intent": "pi_live"})
            elif failure == "unknown_intent":
                key, data = refund_request_data(stripe, order, None, 1000, scope=scope)
                await backend.begin_operation(key, "refunds", data)
                await backend.finish_operation(key, "unknown")
            elif failure == "paid_out_case": order["paid_out_refund_case"] = {}
            elif failure == "reserved_payout": store.payouts.append({"finance_v2": True, "status": "paid", "allocations": [{"order_id": order["id"]}]})
            elif failure == "incomplete": provider.incomplete = True
            elif failure == "missing_consent": order.pop("refund_cancel_accepted_by")
            with pytest.raises((FinanceConflict, FinancialScopeError, RuntimeError)):
                await inspect_refund_resumption_live(backend, store, stripe, settings, mfa, request, order_id="live_order", at=at)
        assert all(r.method == "GET" for r in provider.requests)
    asyncio.run(run())


@pytest.mark.parametrize("failure", [None, "legacy_amountless", "wrong_amount_hash", "other_ledger_hash", "refund_amount", "refund_currency",
    "refund_parent", "refund_charge", "refund_order", "refund_extra", "refund_ledger", "refund_test_mode", "refund_pending", "refund_duplicate"])
def test_live_existing_refund_requires_exact_scoped_request_and_fresh_full_result(database, provider, failure):
    backend, dsn = database
    async def run():
        store, settings, stripe, mfa, request, scope, at = await seed_live(backend, dsn, provider)
        async with backend.request(store):
            order = store.orders[0]
            key, data = refund_request_data(stripe, order, None, 1000, scope=scope)
            assert data["amount"] == "1000" and data["metadata[ledger_id]"] == scope.ledger_id
            if failure == "legacy_amountless":
                data.pop("amount")
                data.pop("metadata[ledger_id]")
            elif failure == "wrong_amount_hash": data["amount"] = "999"
            elif failure == "other_ledger_hash": data["metadata[ledger_id]"] = str(uuid4())
            remote = {"object": "refund", "id": "re_live", "payment_intent": "pi_live", "charge": "ch_live",
                      "amount": 1000, "currency": "jpy", "status": "succeeded", "created": int(at.timestamp()),
                      "metadata": {"order_id": order["id"], "ledger_id": scope.ledger_id}}
            await backend.begin_operation(key, "refunds", data)
            await backend.finish_operation(key, "succeeded", deepcopy(remote))
            provider.records.append(remote)
            provider.payments["pi_live"]["latest_charge"].update(amount_refunded=1000, refunded=True)
            if failure == "refund_amount": remote["amount"] = 999
            elif failure == "refund_currency": remote["currency"] = "usd"
            elif failure == "refund_parent": remote["payment_intent"] = "pi_elsewhere"
            elif failure == "refund_charge": remote["charge"] = "ch_elsewhere"
            elif failure == "refund_order": remote["metadata"]["order_id"] = "another_order"
            elif failure == "refund_extra": remote["metadata"]["extra_id"] = "another_extra"
            elif failure == "refund_ledger": remote["metadata"]["ledger_id"] = str(uuid4())
            elif failure == "refund_test_mode": remote["livemode"] = False
            elif failure == "refund_pending": remote["status"] = "pending"
            elif failure == "refund_duplicate": provider.duplicate = True
            before = deepcopy(snapshot(store))
            if failure is None:
                result = await inspect_refund_resumption_live(backend, store, stripe, settings, mfa, request, order_id="live_order", at=at)
                assert result["verified_refund_count"] == 1 and result["remaining_amount"] == 0
                assert result["execution_allowed"] is False
            else:
                with pytest.raises((FinanceConflict, RuntimeError)):
                    await inspect_refund_resumption_live(backend, store, stripe, settings, mfa, request, order_id="live_order", at=at)
            assert snapshot(store) == before
        assert all(r.method == "GET" for r in provider.requests)
    asyncio.run(run())
