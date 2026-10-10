"""Real local Postgres + simulated Stripe; these tests never move real money."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader
from pathlib import Path
from starlette.requests import Request

from app.config import Settings
from app.data import DemoStore
from app.database import StorageUnavailable
from app.finance import FinanceConflict
from app.integrations import StripeIntegration, StripeOutcomeUnknown
from app.payment_reconciliation import approve_payment_recovery, payment_recovery_inventory, propose_payment_recovery
from app.webhooks import StripeWebhookService
from test_database import database, query, webhook_request
from test_finance import order as make_order


class PaymentProvider:
    def __init__(self):
        self.records = []
        self.payments = {}
        self.requests = []
        self.session_status = "complete"
        self.refund_status = "succeeded"
        self.duplicate = False
        self.incomplete = False

    def payment(self, payment_id, amount, metadata, refunded=0):
        charge_id = "ch_" + payment_id[3:]
        self.payments[payment_id] = {"id": payment_id, "object": "payment_intent", "livemode": False,
            "amount": amount, "amount_received": amount, "currency": "jpy", "status": "succeeded", "metadata": metadata,
            "transfer_group": "order-" + metadata["order_id"], "transfer_data": None, "application_fee_amount": None,
            "latest_charge": {"id": charge_id, "object": "charge", "livemode": False, "payment_intent": payment_id,
                "amount": amount, "amount_captured": amount, "currency": "jpy", "paid": True, "captured": True,
                "status": "succeeded", "disputed": False, "transfer_group": "order-" + metadata["order_id"],
                "amount_refunded": refunded, "refunded": refunded == amount}}
        return charge_id

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path.startswith("/v1/payment_intents/"):
            return httpx.Response(200, json=self.payments[path.rsplit("/", 1)[-1]])
        if request.method == "GET" and path in {"/v1/checkout/sessions", "/v1/refunds"}:
            records = deepcopy(self.records)
            if self.duplicate and records:
                records.append({**records[0], "id": records[0]["id"] + "duplicate"})
            return httpx.Response(200, json={"object": "list", "data": records, "has_more": self.incomplete})
        assert request.method == "POST" and path in {"/v1/checkout/sessions", "/v1/refunds"}
        data = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        metadata = {"order_id": data["metadata[order_id]"]}
        if "metadata[extra_id]" in data: metadata["extra_id"] = data["metadata[extra_id]"]
        created = int(datetime.now(timezone.utc).timestamp())
        if path.endswith("/sessions"):
            amount = int(data["line_items[0][price_data][unit_amount]"])
            payment_id = "pi_extra" if "extra_id" in metadata else "pi_main"
            self.payment(payment_id, amount, metadata)
            record = {"id": "cs_test_recover", "object": "checkout.session", "livemode": False,
                "created": created, "currency": "jpy", "amount_total": amount, "metadata": metadata,
                "mode": "payment", "status": self.session_status,
                "payment_status": "paid" if self.session_status == "complete" else "unpaid",
                "payment_intent": payment_id if self.session_status == "complete" else None,
                "success_url": data["success_url"], "cancel_url": data["cancel_url"],
                "customer_email": data.get("customer_email"), "expires_at": created + 3600,
                "url": "https://checkout.stripe.com/c/pay/cs_test_recover" if self.session_status == "open" else None}
        else:
            payment_id = data["payment_intent"]
            payment = self.payments[payment_id]
            if self.refund_status == "succeeded":
                payment["latest_charge"].update(amount_refunded=payment["amount"], refunded=True)
            # Stripe refund objects intentionally have NO livemode field.
            record = {"id": "re_recover", "object": "refund", "created": created,
                "currency": "jpy", "amount": payment["amount"], "metadata": metadata,
                "status": self.refund_status, "payment_intent": payment_id, "charge": payment["latest_charge"]["id"]}
        self.records.append(record)
        raise httpx.ReadTimeout("provider response was lost", request=request)


@pytest.fixture
def provider(monkeypatch):
    provider = PaymentProvider()
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(provider), **kwargs))
    return provider


async def uncertain(backend, dsn, provider, kind):
    at = datetime.now(timezone.utc)
    settings = Settings(environment="staging", database_url=dsn, stripe_secret_key="sk_test_payment",
        stripe_charge_mode="separate", stripe_webhook_secret="whsec_local", site_base_url="https://example.test",
        admin_user_ids=("admin_one", "admin_two"))
    stripe = StripeIntegration(settings.stripe_secret_key, settings.site_base_url, charge_mode="separate", journal=backend)
    store = DemoStore(seed=False)
    async with backend.request(store):
        order = make_order(id="payment_order", tool_slug="test", tool_name="Test", status="in_progress",
            payment_status="pending" if kind == "checkout" else "paid", payment_reference=None if kind == "checkout" else "pi_main",
            buyer_email="buyer@example.test", seller_email="seller@example.test", fulfillment_type="custom", sales_recorded=kind != "checkout")
        store.orders.append(order)
        store.tools.append({"slug": "test", "sales_count": 0 if kind == "checkout" else 1})
        store.connected_accounts["seller"] = {"account_id": "acct_seller"}
        extra = None
        if kind in {"extra", "refund_extra"}:
            extra = {"id": "extra_part", "amount": 450, "note": "Additional", "status": "pending" if kind == "extra" else "paid"}
            order["pending_extras"] = [extra]
            if kind == "refund_extra":
                extra["payment_reference"] = "pi_extra"
                order.update(amount=1450, platform_fee=218, primary_refunded_amount=1000, primary_refund_status="completed", refund_reference="re_primary", refund_status="partial")
        provider.payment("pi_main", 1000, {"order_id": order["id"]})
        if kind == "refund_extra": provider.payment("pi_extra", 450, {"order_id": order["id"], "extra_id": extra["id"]})
        if kind.startswith("refund"): order.update(status="cancel_pending", refund_status="processing")
        with pytest.raises(StripeOutcomeUnknown):
            if kind == "checkout": await stripe.create_checkout(order, "acct_seller")
            elif kind == "extra": await stripe.create_extra_checkout(order, extra, "acct_seller")
            else: await stripe.refund_payment((extra or order)["payment_reference"], order["id"], extra_id=extra["id"] if extra else None)
        order["payment_reconciliation_required"] = True
        if kind.startswith("refund"): order["refund_status"] = "review"
        await backend.save()
    return store, settings, stripe, at


def sender(backend):
    async def queue(to, subject, text):
        backend.enqueue_email(to, "test@example.test", subject, text)
        return True
    return queue


async def recover(backend, store, settings, stripe, kind, at):
    extra_id = "extra_part" if kind in {"extra", "refund_extra"} else ""
    async with backend.request(store):
        order = store.orders[0]
        proposal = await propose_payment_recovery(backend, store, stripe, settings, order, kind, "admin_one", extra_id=extra_id, at=at)
        await approve_payment_recovery(backend, store, stripe, settings, order, kind, "admin_two", proposal["id"], sender(backend), extra_id=extra_id, at=at)


@pytest.mark.parametrize("kind", ["checkout", "extra", "refund", "refund_extra"])
def test_recovery_is_get_only_atomic_and_webhook_afterwards_is_idempotent(database, provider, kind):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await uncertain(backend, dsn, provider, kind)
        await recover(backend, store, settings, stripe, kind, at)
        assert sum(r.method == "POST" for r in provider.requests) == 1
        assert query(dsn, "select status from toolbako_runtime.stripe_operations") == [("succeeded",)]
        assert query(dsn, "select count(*) from toolbako_runtime.audit_events where action like %s", ("payment.reconciliation_%",)) == [(2,)]
        expected_emails = 2 if kind == "checkout" else 1
        assert query(dsn, "select count(*) from toolbako_runtime.email_outbox") == [(expected_emails,)]
        async with backend.request(store):
            order = store.orders[0]
            assert not order["payment_reconciliation_required"]
            if kind.startswith("refund"):
                assert order["status"] == "cancelled" and order["refund_status"] == "completed"
                assert not order["sales_recorded"] and store.tools[0]["sales_count"] == 0
            else:
                assert order["payment_status"] == "paid" and store.tools[0]["sales_count"] == 1
                assert order["amount"] == (1450 if kind == "extra" else 1000)
            notification_count = len(store.notifications)
            event = {"id": "evt_after_recovery", "livemode": False, "type": "refund.updated" if kind.startswith("refund") else "checkout.session.completed", "data": {"object": provider.records[0]}}
            service = StripeWebhookService(settings, store, sender(backend), journal=backend)
            await service.handle(webhook_request(event, settings.stripe_webhook_secret))
            await service.handle(webhook_request({**event, "id": "evt_after_recovery_again"}, settings.stripe_webhook_secret))
            assert len(store.notifications) == notification_count
            await backend.save()
        assert query(dsn, "select count(*) from toolbako_runtime.email_outbox") == [(expected_emails,)]
    asyncio.run(run())


@pytest.mark.parametrize("status", ["open", "expired"])
def test_unpaid_checkout_never_activates_tool(database, provider, status):
    backend, dsn = database
    provider.session_status = status
    async def run():
        store, settings, stripe, at = await uncertain(backend, dsn, provider, "checkout")
        await recover(backend, store, settings, stripe, "checkout", at)
        async with backend.request(store):
            order = store.orders[0]
            assert order["checkout_session_id"] == "cs_test_recover"
            assert order["payment_status"] == ("pending" if status == "open" else "expired")
            assert not order["sales_recorded"] and not store.notifications
            assert order["checkout_url"] == (provider.records[0]["url"] if status == "open" else "")
        assert query(dsn, "select count(*) from toolbako_runtime.email_outbox") == [(0,)]
    asyncio.run(run())


@pytest.mark.parametrize("status", ["pending", "requires_action", "failed", "canceled"])
def test_refund_not_succeeded_never_claims_completion_or_resends(database, provider, status):
    backend, dsn = database
    provider.refund_status = status
    async def run():
        store, settings, stripe, at = await uncertain(backend, dsn, provider, "refund")
        await recover(backend, store, settings, stripe, "refund", at)
        async with backend.request(store):
            assert store.orders[0]["status"] == "cancel_pending"
            assert store.orders[0]["refund_status"] == ("review" if status in {"failed", "canceled"} else "pending")
            assert store.orders[0]["refund_reference"] == "re_recover" and store.orders[0]["sales_recorded"]
        assert sum(r.method == "POST" for r in provider.requests) == 1
        assert query(dsn, "select count(*) from toolbako_runtime.email_outbox") == [(0,)]
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["absent", "duplicate", "metadata", "extra_metadata", "malformed_metadata", "amount", "bool_amount", "currency", "live", "object", "success_url", "cancel_url", "customer", "date", "payment_pending", "pi_amount", "pi_live", "pi_metadata", "pi_transfer", "pi_group", "charge_amount", "charge_live", "charge_parent", "disputed", "already_refunded", "incomplete"])
def test_invalid_checkout_proof_keeps_unknown_and_no_delivery(database, provider, failure):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await uncertain(backend, dsn, provider, "checkout")
        remote, payment = provider.records[0], provider.payments["pi_main"]
        if failure == "absent": provider.records = []
        elif failure == "duplicate": provider.duplicate = True
        elif failure == "metadata": remote["metadata"]["order_id"] = "other"
        elif failure == "extra_metadata": remote["metadata"]["extra_id"] = "other"
        elif failure == "malformed_metadata": remote["metadata"] = []
        elif failure == "amount": remote["amount_total"] += 1
        elif failure == "bool_amount": remote["amount_total"] = True
        elif failure == "currency": remote["currency"] = "usd"
        elif failure == "live": remote["livemode"] = True
        elif failure == "object": remote["object"] = "payment_intent"
        elif failure == "success_url": remote["success_url"] = "https://evil.test"
        elif failure == "cancel_url": remote["cancel_url"] = "https://evil.test"
        elif failure == "customer": remote["customer_email"] = "wrong@example.test"
        elif failure == "date": remote["created"] = 1
        elif failure == "payment_pending": remote["payment_status"] = "unpaid"
        elif failure == "pi_amount": payment["amount_received"] -= 1
        elif failure == "pi_live": payment["livemode"] = True
        elif failure == "pi_metadata": payment["metadata"] = {}
        elif failure == "pi_transfer": payment["transfer_data"] = {"destination": "acct_other"}
        elif failure == "pi_group": payment["transfer_group"] = "order-other"
        elif failure == "charge_amount": payment["latest_charge"]["amount_captured"] -= 1
        elif failure == "charge_live": payment["latest_charge"]["livemode"] = True
        elif failure == "charge_parent": payment["latest_charge"]["payment_intent"] = "pi_other"
        elif failure == "disputed": payment["latest_charge"]["disputed"] = True
        elif failure == "already_refunded": payment["latest_charge"].update(amount_refunded=1000, refunded=True)
        elif failure == "incomplete": provider.incomplete = True
        async with backend.request(store):
            with pytest.raises((FinanceConflict, RuntimeError)):
                await propose_payment_recovery(backend, store, stripe, settings, store.orders[0], "checkout", "admin_one", at=at)
            assert store.orders[0]["payment_status"] == "pending" and store.orders[0]["payment_reconciliation_required"]
        assert query(dsn, "select status from toolbako_runtime.stripe_operations") == [("unknown",)]
        assert sum(r.method == "POST" for r in provider.requests) == 1
    asyncio.run(run())


@pytest.mark.parametrize("url", ["https://evil.test/c/pay/x", "https://checkout.stripe.com@evil.test/c/pay/x", "http://checkout.stripe.com/c/pay/x", "https://checkout.stripe.com.evil.test/c/pay/x", "javascript:alert(1)", None])
def test_recovered_checkout_url_cannot_redirect_to_untrusted_host(database, provider, url):
    backend, dsn = database
    provider.session_status = "open"
    async def run():
        store, settings, stripe, at = await uncertain(backend, dsn, provider, "checkout")
        provider.records[0]["url"] = url
        async with backend.request(store):
            with pytest.raises(FinanceConflict):
                await propose_payment_recovery(backend, store, stripe, settings, store.orders[0], "checkout", "admin_one", at=at)
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["amount", "bool_amount", "charge", "payment", "status", "live_parent", "unconfirmed", "metadata"])
def test_refund_requires_full_amount_and_sandbox_parent_proof(database, provider, failure):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await uncertain(backend, dsn, provider, "refund")
        remote = provider.records[0]
        if failure == "amount": remote["amount"] -= 1
        elif failure == "bool_amount": remote["amount"] = True
        elif failure == "charge": remote["charge"] = "ch_other"
        elif failure == "payment": remote["payment_intent"] = "pi_other"
        elif failure == "status": remote["status"] = "unknown"
        elif failure == "live_parent": provider.payments["pi_main"]["livemode"] = True
        elif failure == "unconfirmed": provider.payments["pi_main"]["latest_charge"]["amount_refunded"] = 0
        elif failure == "metadata": remote["metadata"]["extra_id"] = "other"
        async with backend.request(store):
            with pytest.raises(FinanceConflict):
                await propose_payment_recovery(backend, store, stripe, settings, store.orders[0], "refund", "admin_one", at=at)
            assert store.orders[0]["refund_status"] == "review"
        assert query(dsn, "select status from toolbako_runtime.stripe_operations") == [("unknown",)]
    asyncio.run(run())


def test_distinct_current_admins_fresh_proposal_and_recheck_are_required(database, provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await uncertain(backend, dsn, provider, "checkout")
        async with backend.request(store):
            order = store.orders[0]
            proposal = await propose_payment_recovery(backend, store, stripe, settings, order, "checkout", "admin_one", at=at)
            for actor, proposal_id, instant in (("admin_one", proposal["id"], at), ("outsider", proposal["id"], at), ("admin_two", "stale", at), ("admin_two", proposal["id"], at + timedelta(minutes=31)), ("admin_two", proposal["id"], at - timedelta(seconds=1))):
                with pytest.raises(FinanceConflict):
                    await approve_payment_recovery(backend, store, stripe, settings, order, "checkout", actor, proposal_id, sender(backend), at=instant)
            provider.records[0]["url"] = "changed"
            with pytest.raises(FinanceConflict):
                await approve_payment_recovery(backend, store, stripe, settings, order, "checkout", "admin_two", proposal["id"], sender(backend), at=at)
            assert order["payment_status"] == "pending"
        assert query(dsn, "select status from toolbako_runtime.stripe_operations") == [("unknown",)]
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["live", "production", "one_admin", "key", "journal", "mode", "changed_amount", "changed_name", "changed_base", "cancelled", "subscription", "dispute"])
def test_changed_configuration_or_business_state_cannot_recover(database, provider, failure):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await uncertain(backend, dsn, provider, "checkout")
        if failure == "live": settings = replace(settings, stripe_secret_key="sk_live_fixture"); stripe.secret_key = settings.stripe_secret_key
        elif failure == "production": settings = replace(settings, environment="production")
        elif failure == "one_admin": settings = replace(settings, admin_user_ids=("admin_one", "admin_one"))
        elif failure == "key": stripe.secret_key = "sk_test_different"
        elif failure == "journal": stripe.journal = None
        elif failure == "mode": stripe.charge_mode = "destination"
        elif failure == "changed_base": stripe.base_url = "https://changed.test"
        async with backend.request(store):
            order = store.orders[0]
            if failure == "changed_amount": order["amount"] += 1
            elif failure == "changed_name": order["tool_name"] = "Changed"
            elif failure == "cancelled": order["status"] = "cancelled"
            elif failure == "subscription": order["billing_type"] = "subscription"
            elif failure == "dispute": order["dispute_status"] = "provider_dispute"
            with pytest.raises(FinanceConflict):
                await propose_payment_recovery(backend, store, stripe, settings, order, "checkout", "admin_one", at=at)
        assert sum(r.method == "POST" for r in provider.requests) == 1
    asyncio.run(run())


def test_state_journal_audit_and_outbox_roll_back_together(database, provider, monkeypatch):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await uncertain(backend, dsn, provider, "checkout")
        async with backend.request(store):
            proposal = await propose_payment_recovery(backend, store, stripe, settings, store.orders[0], "checkout", "admin_one", at=at)
        async def fail(conn): raise RuntimeError("simulated database failure")
        monkeypatch.setattr(backend, "_save", fail)
        with pytest.raises(StorageUnavailable):
            async with backend.request(store):
                await approve_payment_recovery(backend, store, stripe, settings, store.orders[0], "checkout", "admin_two", proposal["id"], sender(backend), at=at)
        assert query(dsn, "select status from toolbako_runtime.stripe_operations") == [("unknown",)]
        assert query(dsn, "select count(*) from toolbako_runtime.email_outbox") == [(0,)]
        assert query(dsn, "select count(*) from toolbako_runtime.audit_events where action='payment.reconciliation_approved'") == [(0,)]
        async with backend.request(store):
            assert store.orders[0]["payment_status"] == "pending"
            assert store.orders[0]["payment_reconciliations"]["checkout"]["status"] == "proposed"
            assert store.tools[0]["sales_count"] == 0 and not store.notifications
    asyncio.run(run())


def test_inventory_and_template_show_unknown_without_provider_payloads(database, provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await uncertain(backend, dsn, provider, "checkout")
        async with backend.request(store):
            inventory = await payment_recovery_inventory(backend, store)
            assert len(inventory["items"]) == 1 and not inventory["truncated"]
            assert inventory["items"][0]["kind"] == "checkout"
            assert "response" not in inventory["items"][0]
            env = Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"), autoescape=True)
            html = env.get_template("_admin_finance.html").render(finance={"sellers": [], "transactions": [], "payouts": [], **{k: 0 for k in ("gross_paid", "completed_gmv", "platform_fee_earned", "processor_fee_total", "available_to_payout", "paid_out", "held", "refund_total", "disputed_total", "blocked_for_payout")}}, finance_controls_ready=False, payment_recovery_ready=True, payment_recoveries=inventory, user={"id": "admin_one"})
            assert "購入決済" in html and "/reconcile/checkout/propose" in html
            assert "buyer@example.test" not in html and "sk_test_payment" not in html
    asyncio.run(run())


def test_admin_recovery_route_guards_and_mfa(monkeypatch):
    import app.main as main
    async def run():
        request = Request({"type": "http", "method": "POST", "path": "/admin/orders/o/reconcile/checkout/propose", "headers": [], "session": {}})
        monkeypatch.setattr(main, "current_user", lambda request: None)
        with pytest.raises(HTTPException) as error: await main.admin_reconcile_payment(request, "o", "checkout", "propose", "", "")
        assert error.value.status_code == 403
        monkeypatch.setattr(main, "current_user", lambda request: {"id": "admin_one"})
        monkeypatch.setattr(main, "is_admin", lambda user: True)
        monkeypatch.setattr(main, "database_store", None)
        with pytest.raises(HTTPException) as error: await main.admin_reconcile_payment(request, "o", "checkout", "propose", "", "")
        assert error.value.status_code == 409
        monkeypatch.setattr(main, "database_store", object())
        monkeypatch.setattr(main, "sandbox_payouts_ready", lambda settings: True)
        monkeypatch.setattr(main.mfa, "recent", lambda request: False)
        response = await main.admin_reconcile_payment(request, "o", "checkout", "propose", "", "")
        assert response.status_code == 303 and response.headers["location"].startswith("/security/mfa")
        monkeypatch.setattr(main.mfa, "recent", lambda request: True)
        with pytest.raises(HTTPException) as error: await main.admin_reconcile_payment(request, "missing", "checkout", "propose", "", "")
        assert error.value.status_code == 404
    asyncio.run(run())


def test_other_unknown_parts_keep_hold_and_resolved_operation_cannot_be_reapproved(database, provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await uncertain(backend, dsn, provider, "extra")
        async with backend.request(store):
            await backend.begin_operation("refund-payment_order", "refunds", stripe.refund_data("pi_main", "payment_order"))
        await recover(backend, store, settings, stripe, "extra", at)
        async with backend.request(store):
            order = store.orders[0]
            assert order["payment_reconciliation_required"]
            proposal = order["pending_extras"][0]["payment_reconciliations"]["extra"]
            with pytest.raises(FinanceConflict):
                await approve_payment_recovery(backend, store, stripe, settings, order, "extra", "admin_two", proposal["id"], sender(backend), extra_id="extra_part", at=at)
            assert order["amount"] == 1450
        assert sum(r.method == "POST" for r in provider.requests) == 1
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["wrong_endpoint", "wrong_hash", "no_operation", "pending"])
def test_journal_requires_original_unknown_or_pending_request(database, provider, failure):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await uncertain(backend, dsn, provider, "checkout")
        if failure == "wrong_endpoint": query(dsn, "update toolbako_runtime.stripe_operations set endpoint='refunds' returning operation_id")
        elif failure == "wrong_hash": query(dsn, "update toolbako_runtime.stripe_operations set request_hash=%s returning operation_id", ("0" * 64,))
        elif failure == "no_operation": query(dsn, "delete from toolbako_runtime.stripe_operations returning operation_id")
        else: query(dsn, "update toolbako_runtime.stripe_operations set status='pending' returning operation_id")
        if failure == "pending":
            await recover(backend, store, settings, stripe, "checkout", at)
            assert query(dsn, "select status from toolbako_runtime.stripe_operations") == [("succeeded",)]
        else:
            async with backend.request(store):
                with pytest.raises(FinanceConflict):
                    await propose_payment_recovery(backend, store, stripe, settings, store.orders[0], "checkout", "admin_one", at=at)
        assert sum(r.method == "POST" for r in provider.requests) == 1
    asyncio.run(run())


def test_inventory_is_bounded_and_unmatched_records_have_no_action(database, provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await uncertain(backend, dsn, provider, "checkout")
        query(dsn, "insert into toolbako_runtime.stripe_operations(operation_id,endpoint,request_hash,status) select 'orphan-' || n, 'refunds', %s, 'unknown' from generate_series(1,101) n returning operation_id", ("0" * 64,))
        async with backend.request(store):
            inventory = await payment_recovery_inventory(backend, store)
            assert inventory["truncated"] and len(inventory["items"]) == 100
            assert sum(bool(item.get("order_id")) for item in inventory["items"]) == 1
            assert all("response" not in item and "request_hash" not in item for item in inventory["items"])
    asyncio.run(run())


def test_recovery_preserves_account_email_and_proposal_excludes_sensitive_proof(database, provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await uncertain(backend, dsn, provider, "checkout")
        provider.records[0]["customer_details"] = {"email": "payer-other@example.test", "name": "private name"}
        provider.payments["pi_main"]["client_secret"] = "private-pi-client-secret"
        await recover(backend, store, settings, stripe, "checkout", at)
        async with backend.request(store):
            order = store.orders[0]
            assert order["buyer_email"] == "buyer@example.test"
            proposal = str(order["payment_reconciliations"])
            assert "private" not in proposal and "example.test" not in proposal
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["checkout", "extra", "refund", "refund_extra"])
def test_unknown_handler_holds_all_payment_parts_without_touching_unrelated_orders(monkeypatch, kind):
    import app.main as main
    async def run():
        store = DemoStore(seed=False)
        store.orders = [{"id": "o", "pending_extras": [{"id": "e"}]}, {"id": "other"}]
        monkeypatch.setattr(main, "store", store)
        monkeypatch.setattr(main, "current_user", lambda request: None)
        async def persist(): return None
        async def error(request, exc): return exc
        monkeypatch.setattr(main, "persist_state", persist)
        monkeypatch.setattr(main, "friendly_http_error", error)
        keys = {"checkout": "checkout-o", "extra": "extra-e", "refund": "refund-o", "refund_extra": "refund-extra-o-e"}
        response = await main.stripe_outcome_unknown(Request({"type": "http", "method": "POST", "path": "/", "headers": []}), StripeOutcomeUnknown(keys[kind]))
        assert response.status_code == 502 and store.orders[0]["payment_reconciliation_required"]
        assert "payment_reconciliation_required" not in store.orders[1]
    asyncio.run(run())
