"""Artificial live-mode evidence only; every provider request is mocked GET."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
import hashlib
import hmac
import json
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.data import DemoStore
from app.database import snapshot, restore
from app.financial_scope import FinancialScope, FinancialScopeError
from app.integrations import StripeIntegration
from app.live_webhooks import build_live_webhook_handler, LiveWebhookDispatcherMiddleware
from app.middleware import ProductionGuardMiddleware
from test_payment_source_proof import source
from test_database import database, query


SCOPE = FinancialScope("live", "acct_LivePlatform", "11111111-1111-4111-8111-111111111111")
PLATFORM_PATH = "/webhooks/stripe/live"
CONNECT_PATH = "/webhooks/stripe/connect/live"
SECRET = "whsec_platformFixture"
CONNECT_SECRET = "whsec_connectFixture"


def configuration():
    return Settings(environment="staging", demo_mode=False, database_url="postgresql://localhost/synthetic",
        stripe_secret_key="rk_live_syntheticOnly", stripe_webhook_secret=SECRET,
        stripe_connect_webhook_secret=CONNECT_SECRET, stripe_platform_account_id=SCOPE.platform_account,
        stripe_ledger_id=SCOPE.ledger_id, stripe_charge_mode="separate", stripe_connect_accounts_v2=False,
        stripe_api_version="2026-02-25.clover", site_base_url="https://toolbako.example", redis_url="")


class MemoryBoundary:
    """Models request rollback/commit, not evidence of real DB durability."""
    def __init__(self, store):
        self.committed = snapshot(store)
        self.binding = SCOPE
        self.opened = self.saved = 0
        self.fail_open = self.fail_save = False
        self._store = None
        self.operations = {}

    @asynccontextmanager
    async def request(self, store):
        self.opened += 1
        if self.fail_open:
            raise RuntimeError("private database failure")
        restore(store, self.committed)
        self._store = store
        try:
            yield
        finally:
            restore(store, self.committed)
            self._store = None

    async def assert_financial_scope(self, expected):
        if self._store is None or expected != self.binding:
            raise FinancialScopeError()

    async def operation_result(self, operation_id):
        return self.operations.get(operation_id)

    async def save(self):
        if self.fail_save:
            raise RuntimeError("private commit failure")
        self.committed = snapshot(self._store)
        self.saved += 1


class ProviderResponses:
    def __init__(self, monkeypatch):
        self.objects = {}
        self.calls = []
        self.account_id = SCOPE.platform_account
        self.live = True
        original = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: original(*a, **kw, transport=httpx.MockTransport(self.respond)))

    def respond(self, request):
        assert request.method == "GET", "No Stripe mutation is authorized by a webhook"
        assert request.url.host == "api.stripe.com"
        account = request.headers.get("stripe-account")
        path = request.url.path.removeprefix("/v1/")
        self.calls.append((path, account))
        if path == "account":
            assert account is None
            return httpx.Response(200, json={"id": self.account_id, "object": "account"})
        if path == "balance":
            assert account is None
            return httpx.Response(200, json={"object": "balance", "livemode": self.live})
        value = self.objects.get((path, account))
        if isinstance(value, int):
            return httpx.Response(value, json={"private": "never-return-provider-errors"})
        return httpx.Response(200 if value is not None else 404, json=value or {})

    def event(self, event):
        self.objects[("events/" + event["id"], event.get("account"))] = deepcopy(event)


def seed():
    store = DemoStore(seed=False)
    store.registered_users["seller"] = {"id": "seller", "email": "seller@example.invalid"}
    store.connected_accounts["seller"] = {"account_id": "acct_seller", "charges_enabled": False,
                                         "payouts_enabled": False, "details_submitted": False}
    store.tools = [{"id": "tool", "slug": "tool", "sales_count": 0}]
    store.orders = [{"id": "order_source", "seller_id": "seller", "buyer_id": "buyer", "tool_name": "Synthetic",
        "tool_slug": "tool", "amount": 1000, "platform_fee": 200, "status": "in_progress",
        "payment_status": "pending", "checkout_session_id": "cs_live_fixture", "billing_type": "one_time"}]
    return store


def event(kind="account.updated", obj=None, *, identifier="evt_fixture"):
    if obj is None:
        obj = {"object": "account", "id": "acct_seller", "charges_enabled": True,
               "payouts_enabled": True, "details_submitted": True}
    result = {"id": identifier, "object": "event", "type": kind, "livemode": True,
              "api_version": "2026-02-25.clover", "created": 100, "data": {"object": deepcopy(obj)}}
    if kind.startswith(("account.", "payout.")):
        result["account"] = "acct_seller"
    return result


def checkout(status="complete", paid="paid"):
    return {"object": "checkout.session", "id": "cs_live_fixture", "livemode": True,
            "currency": "jpy", "amount_total": 1000, "mode": "payment", "status": status,
            "payment_status": paid, "payment_intent": "pi_source", "metadata": {"order_id": "order_source"}}


def build_app(handler, settings):
    app = FastAPI()
    @app.post("/webhooks/stripe")
    async def old_test_route():
        return {"test_route_preserved": True}
    app.add_middleware(LiveWebhookDispatcherMiddleware, handler=handler, settings=settings)
    app.add_middleware(ProductionGuardMiddleware, settings=settings)
    return TestClient(app)


@pytest.fixture
def fixture(monkeypatch):
    responses = ProviderResponses(monkeypatch)
    store = seed()
    settings = configuration()
    backend = MemoryBoundary(store)
    provider = StripeIntegration(settings.stripe_secret_key, settings.site_base_url,
        charge_mode="separate", api_version=settings.stripe_api_version, journal=backend, financial_scope=SCOPE)
    handler = build_live_webhook_handler(settings, store, backend, provider)
    assert handler is not None
    return build_app(handler, settings), store, backend, provider, responses, handler


def deliver(client, payload=None, *, path=CONNECT_PATH, secret=CONNECT_SECRET, raw=None,
            timestamp=None, extra_headers=None):
    raw = json.dumps(payload).encode() if raw is None else raw
    timestamp = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(secret.encode(), str(timestamp).encode() + b"." + raw, hashlib.sha256).hexdigest()
    headers = [("stripe-signature", f"t={timestamp},v1={digest}"), ("content-type", "application/json")]
    headers += extra_headers or []
    return client.post(path, content=raw, headers=headers)


def account_current(responses, enabled=False):
    result = event()["data"]["object"]
    result["payouts_enabled"] = enabled
    responses.objects[("accounts/acct_seller", None)] = result


def checkout_evidence(responses, *, kind="checkout.session.completed", status="complete", paid="paid", identifier="evt_checkout"):
    obj = checkout(status, paid)
    notification = event(kind, obj, identifier=identifier)
    responses.event(notification)
    responses.objects[("checkout/sessions/cs_live_fixture", None)] = obj
    responses.objects[("payment_intents/pi_source", None)] = source("live")
    return notification


@pytest.mark.parametrize("change", ["test_key", "no_scope", "demo", "no_db", "no_backend",
    "destination", "v2", "same_secrets", "missing_secret", "api_version", "wrong_provider_scope"])
def test_factory_never_enables_incomplete_or_active_test_configuration(fixture, change):
    _, store, backend, provider, responses, _ = fixture
    settings = configuration()
    if change == "test_key": settings = replace(settings, stripe_secret_key="rk_test_fixture")
    elif change == "no_scope": settings = replace(settings, stripe_platform_account_id="", stripe_ledger_id="")
    elif change == "demo": settings = replace(settings, demo_mode=True, database_url="")
    elif change == "no_db": settings = replace(settings, database_url="")
    elif change == "no_backend": backend = None
    elif change == "destination": settings = replace(settings, stripe_charge_mode="destination")
    elif change == "v2": settings = replace(settings, stripe_connect_accounts_v2=True)
    elif change == "same_secrets": settings = replace(settings, stripe_connect_webhook_secret=SECRET)
    elif change == "missing_secret": settings = replace(settings, stripe_webhook_secret="")
    elif change == "api_version": settings = replace(settings, stripe_api_version="2026-10-28.endive")
    else: provider.financial_scope = replace(SCOPE, mode="test")
    assert build_live_webhook_handler(settings, store, backend, provider) is None
    client = build_app(None, settings)
    for path in (CONNECT_PATH, PLATFORM_PATH):
        assert deliver(client, event(), path=path).status_code == 503
    legacy = client.post("/webhooks/stripe")
    if change == "test_key":
        assert legacy.json() == {"test_route_preserved": True}
    else:
        assert legacy.status_code == 503
    assert responses.calls == []


def test_legacy_test_route_remains_and_live_key_can_never_use_generic_handler(fixture):
    _, _, backend, _, responses, handler = fixture
    for live_handler in (None, handler):
        client = build_app(live_handler, configuration())
        assert client.post("/webhooks/stripe").status_code == 503
    client = build_app(None, replace(configuration(), stripe_secret_key="rk_test_fixture"))
    assert client.post("/webhooks/stripe").json() == {"test_route_preserved": True}
    assert backend.opened == 0 and responses.calls == []


@pytest.mark.parametrize("environment,demo,key,expected", [
    ("development", True, "", 200),
    ("development", False, "", 503),
    ("staging", True, "", 503),
    ("production", False, "", 503),
    ("development", True, "unrecognized", 503),
    ("staging", False, "unrecognized", 503),
    ("development", True, None, 503),
    ("development", True, "rk_test_fixture", 200),
    ("staging", False, "sk_test_fixture", 200),
    ("development", True, "rk_live_fixture", 503),
    ("staging", False, "sk_live_fixture", 503),
])
def test_legacy_admission_handles_empty_demo_and_unknown_keys_without_500(environment, demo, key, expected):
    settings = Settings(environment=environment, demo_mode=demo, stripe_secret_key=key,
                        database_url="", redis_url="", site_base_url="https://toolbako.example")
    client = build_app(None, settings)
    response = client.post("/webhooks/stripe")
    assert response.status_code == expected
    if expected == 200:
        assert response.json() == {"test_route_preserved": True}


def test_application_state_hook_stays_disabled_until_explicit_handler_is_ready(fixture):
    _, store, backend, _, responses, handler = fixture
    app = FastAPI()
    app.add_middleware(LiveWebhookDispatcherMiddleware, application_state=app.state)
    client = TestClient(app)
    assert deliver(client, event()).status_code == 503
    assert backend.opened == 0 and not responses.calls
    app.state.live_webhook_handler = handler
    notification = event()
    responses.event(notification)
    account_current(responses, True)
    assert deliver(client, notification).status_code == 200
    assert store.connected_accounts["seller"]["payouts_enabled"] is True


@pytest.mark.parametrize("fault", ["bad_secret", "expired_signature", "test_mode", "bool_mode",
    "bad_version", "scope", "unknown_event", "unknown_object", "duplicate_header", "bad_json", "too_large"])
def test_untrusted_notifications_never_open_database(fixture, fault):
    client, _, backend, _, responses, _ = fixture
    notification = event()
    options = {}
    if fault == "bad_secret": options["secret"] = SECRET
    elif fault == "expired_signature": options["timestamp"] = int(time.time()) - 301
    elif fault == "test_mode": notification["livemode"] = False
    elif fault == "bool_mode": notification["livemode"] = 1
    elif fault == "bad_version": notification["api_version"] = None
    elif fault == "scope": options["path"] = PLATFORM_PATH
    elif fault == "unknown_event": notification["type"] = "account.external_account.updated"
    elif fault == "unknown_object": notification["object"] = "v2.core.event"
    elif fault == "duplicate_header": options["extra_headers"] = [("stripe-signature", "duplicate")]
    elif fault == "bad_json": options["raw"] = b"not-json"
    else: options["raw"] = b"x" * 1_000_001
    response = deliver(client, notification, **options)
    assert response.status_code == (413 if fault == "too_large" else 400)
    assert backend.opened == 0 and responses.calls == []
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("fault", ["wrong_binding", "wrong_platform", "test_balance", "event_tampered", "provider_error", "db_open", "commit"])
def test_provider_or_storage_failure_never_acknowledges_or_claims(fixture, fault):
    client, store, backend, _, responses, _ = fixture
    notification = event()
    responses.event(notification)
    account_current(responses, True)
    if fault == "wrong_binding": backend.binding = replace(SCOPE, ledger_id="22222222-2222-4222-8222-222222222222")
    elif fault == "wrong_platform": responses.account_id = "acct_Other"
    elif fault == "test_balance": responses.live = False
    elif fault == "event_tampered": responses.objects[("events/evt_fixture", "acct_seller")]["data"]["object"]["payouts_enabled"] = False
    elif fault == "provider_error": responses.objects[("accounts/acct_seller", None)] = 503
    elif fault == "db_open": backend.fail_open = True
    else: backend.fail_save = True
    response = deliver(client, notification)
    assert response.status_code == 503 and not response.json().get("received")
    assert not store.processed_webhook_events and not store.connected_accounts["seller"]["payouts_enabled"]
    assert "private" not in response.text


def test_account_uses_current_get_not_event_order_and_committed_replay_survives_restart(fixture):
    client, store, backend, provider, responses, _ = fixture
    notification = event()  # Old snapshot enabled; latest GET disables it.
    responses.event(notification)
    account_current(responses, False)
    assert deliver(client, notification).status_code == 200
    assert store.connected_accounts["seller"]["payouts_enabled"] is False
    assert "evt_fixture" in store.processed_webhook_events and backend.saved == 1
    # Restart with an empty store: request reloads committed state, not memory.
    restarted = DemoStore(seed=False)
    handler = build_live_webhook_handler(configuration(), restarted, backend, provider)
    response = deliver(build_app(handler, configuration()), notification)
    assert response.json() == {"received": True, "duplicate": True}
    assert responses.calls.count(("accounts/acct_seller", None)) == 1


@pytest.mark.parametrize("fault", ["unknown_account", "duplicate_mapping", "unknown_seller", "bool_flags"])
def test_account_mapping_and_flags_fail_closed(fixture, fault):
    client, store, backend, _, responses, _ = fixture
    notification = event()
    responses.event(notification)
    account_current(responses)
    if fault == "unknown_account": store.connected_accounts.clear()
    elif fault == "duplicate_mapping": store.connected_accounts["other"] = dict(store.connected_accounts["seller"])
    elif fault == "unknown_seller": store.registered_users.clear()
    else: responses.objects[("accounts/acct_seller", None)]["charges_enabled"] = "true"
    backend.committed = snapshot(store)
    assert deliver(client, notification).status_code in {409, 503}
    assert not store.processed_webhook_events


def test_checkout_commits_receipt_without_sending_email_and_late_expiry_cannot_cancel(fixture):
    client, store, backend, _, responses, _ = fixture
    notification = checkout_evidence(responses)
    assert deliver(client, notification, path=PLATFORM_PATH, secret=SECRET).status_code == 200
    assert store.orders[0]["payment_status"] == "paid" and store.orders[0]["payment_reference"] == "pi_source"
    assert store.tools[0]["sales_count"] == 1
    assert any(row["action"] == "webhook.email_suppressed" for row in store.audit_logs)
    late = event("checkout.session.expired", checkout("expired", "unpaid"), identifier="evt_lateExpiry")
    responses.event(late)  # Current session remains complete/paid.
    assert deliver(client, late, path=PLATFORM_PATH, secret=SECRET).status_code == 200
    assert store.orders[0]["payment_status"] == "paid" and store.orders[0]["status"] == "in_progress"
    assert store.tools[0]["sales_count"] == 1 and backend.saved == 2


@pytest.mark.parametrize("fault", ["bool_amount", "other_session", "test_parent", "wrong_charge", "wrong_order", "refunded_before_receipt"])
def test_checkout_amount_parent_and_mapping_mismatches_are_not_receipts(fixture, fault):
    client, store, _, _, responses, _ = fixture
    notification = checkout_evidence(responses)
    current = responses.objects[("checkout/sessions/cs_live_fixture", None)]
    payment = responses.objects[("payment_intents/pi_source", None)]
    if fault == "bool_amount": current["amount_total"] = True
    elif fault == "other_session": current["id"] = "cs_live_other"
    elif fault == "test_parent": payment["livemode"] = False
    elif fault == "wrong_charge": payment["latest_charge"]["payment_intent"] = "pi_other"
    elif fault == "wrong_order": payment["metadata"]["order_id"] = "other"
    else: payment["latest_charge"]["amount_refunded"] = 1000
    assert deliver(client, notification, path=PLATFORM_PATH, secret=SECRET).status_code == (
        503 if fault == "test_parent" else 409 if fault in {"wrong_charge", "refunded_before_receipt"} else 400)
    assert store.orders[0]["payment_status"] == "pending" and not store.processed_webhook_events


def test_completed_but_unpaid_is_durably_pending_and_async_paid_later_settles(fixture):
    client, store, _, _, responses, _ = fixture
    notification = checkout_evidence(responses, paid="unpaid")
    assert deliver(client, notification, path=PLATFORM_PATH, secret=SECRET).status_code == 200
    assert store.orders[0]["payment_status"] == "pending"
    assert "evt_checkout" in store.processed_webhook_events
    later = checkout_evidence(responses, kind="checkout.session.async_payment_succeeded", identifier="evt_async")
    assert deliver(client, later, path=PLATFORM_PATH, secret=SECRET).status_code == 200
    assert store.orders[0]["payment_status"] == "paid"


def refund_evidence(fixture, *, kind="refund.updated"):
    _, store, backend, _, responses, _ = fixture
    store.orders[0].update(payment_status="paid", payment_reference="pi_source", refund_reference="re_fixture")
    backend.committed = snapshot(store)
    payment = source("live", refunded=1000)
    responses.objects[("payment_intents/pi_source", None)] = payment
    refund = {"id": "re_fixture", "object": "refund", "currency": "jpy", "amount": 1000,
        "status": "succeeded", "charge": "ch_source", "payment_intent": "pi_source", "metadata": {"order_id": "order_source"}}
    obj = payment["latest_charge"] if kind == "charge.refunded" else refund
    responses.objects[("charges/ch_source" if kind == "charge.refunded" else "refunds/re_fixture", None)] = obj
    notification = event(kind, obj, identifier="evt_refund")
    responses.event(notification)
    return notification


@pytest.mark.parametrize("kind", ["refund.updated", "charge.refunded"])
def test_non_paid_out_refund_uses_current_parent_and_updates_full_refund(fixture, kind):
    client, store, _, _, _, _ = fixture
    notification = refund_evidence(fixture, kind=kind)
    assert deliver(client, notification, path=PLATFORM_PATH, secret=SECRET).status_code == 200
    assert store.orders[0]["refund_status"] == "completed"


@pytest.mark.parametrize("marker", ["case_metadata", "empty_case_metadata", "ledger_metadata", "case_record", "paid_allocation", "historically_paid_allocation"])
def test_paid_out_refund_never_falls_through_generic_seller_debt_path(fixture, marker):
    client, store, backend, _, responses, _ = fixture
    notification = refund_evidence(fixture)
    if marker == "case_metadata": notification["data"]["object"]["metadata"]["paid_out_case_id"] = "case1"
    elif marker == "empty_case_metadata": notification["data"]["object"]["metadata"]["paid_out_case_id"] = ""
    elif marker == "ledger_metadata": notification["data"]["object"]["metadata"]["ledger_id"] = SCOPE.ledger_id
    elif marker == "case_record": store.orders[0]["paid_out_refund_case"] = {"id": "case1"}
    else: store.payouts = [{"id": "paid", "status": "paid", "finance_v2": True, "allocations": [{"order_id": "order_source"}]}]
    if marker == "historically_paid_allocation":
        store.payouts[0].update(status="bank_failed", paid_at="historical")
    responses.event(notification)
    backend.committed = snapshot(store)
    assert deliver(client, notification, path=PLATFORM_PATH, secret=SECRET).status_code == 503
    assert "primary_refund_status" not in store.orders[0] and not store.processed_webhook_events


def test_dedicated_paid_out_reconciliation_commits_case_only_and_deduplicates(fixture):
    client, store, backend, _, responses, handler = fixture
    notification = refund_evidence(fixture)
    notification["data"]["object"]["metadata"].update(paid_out_case_id="case1", ledger_id=SCOPE.ledger_id)
    responses.event(notification)
    calls = []
    async def reconcile(event, supplied_store, supplied_backend, provider, scope):
        assert supplied_store is store and supplied_backend is backend and backend._store is store and scope == SCOPE
        calls.append(event["id"])
        store.orders[0]["paid_out_refund_case"] = {"id": "case1", "status": "awaiting_review"}
        return True
    handler.services["platform"].paid_out_refund_reconciler = reconcile
    assert deliver(client, notification, path=PLATFORM_PATH, secret=SECRET).status_code == 200
    assert "primary_refund_status" not in store.orders[0]
    assert store.orders[0]["paid_out_refund_case"]["status"] == "awaiting_review"
    assert deliver(client, notification, path=PLATFORM_PATH, secret=SECRET).json()["duplicate"]
    assert calls == ["evt_refund"]


def test_failed_paid_out_callback_cannot_persist_partial_case_or_event(fixture):
    client, store, _, _, responses, handler = fixture
    notification = refund_evidence(fixture)
    notification["data"]["object"]["metadata"]["paid_out_case_id"] = "case1"
    responses.event(notification)
    async def reconcile(event, supplied_store, *args):
        supplied_store.orders[0]["paid_out_refund_case"] = {"id": "partial"}
        raise RuntimeError("private callback failure")
    handler.services["platform"].paid_out_refund_reconciler = reconcile
    response = deliver(client, notification, path=PLATFORM_PATH, secret=SECRET)
    assert response.status_code == 503 and "private" not in response.text
    assert "paid_out_refund_case" not in store.orders[0] and not store.processed_webhook_events


def test_later_provider_paid_out_metadata_cannot_fall_through_generic_refund(fixture):
    client, store, _, _, responses, _ = fixture
    notification = refund_evidence(fixture)
    responses.objects[("refunds/re_fixture", None)]["metadata"]["paid_out_case_id"] = "case1"
    assert deliver(client, notification, path=PLATFORM_PATH, secret=SECRET).status_code == 409
    assert "primary_refund_status" not in store.orders[0] and not store.processed_webhook_events


def payout_evidence(fixture):
    _, store, backend, _, responses, _ = fixture
    store.payouts = [{"id": "payout1", "seller_id": "seller", "finance_v2": True, "account_id": "acct_seller",
        "provider_payout_id": "po_fixture", "status": "bank_pending", "net_amount": 800}]
    backend.committed = snapshot(store)
    obj = {"object": "payout", "id": "po_fixture", "amount": 800, "currency": "jpy", "livemode": True,
        "status": "paid", "automatic": False, "method": "standard", "type": "bank_account", "metadata": {"payout_id": "payout1"}}
    notification = event("payout.paid", obj, identifier="evt_payout")
    responses.event(notification)
    responses.objects[("payouts/po_fixture", "acct_seller")] = deepcopy(obj)
    return notification


def test_payout_uses_connected_get_and_late_paid_snapshot_cannot_undo_current_failure(fixture):
    client, store, _, _, responses, _ = fixture
    notification = payout_evidence(fixture)
    responses.objects[("payouts/po_fixture", "acct_seller")]["status"] = "failed"
    assert deliver(client, notification).status_code == 200
    assert store.payouts[0]["status"] == "bank_failed"
    assert ("payouts/po_fixture", "acct_seller") in responses.calls


@pytest.mark.parametrize("field,value", [("amount", True), ("amount", 799), ("livemode", False),
    ("automatic", True), ("type", "card"), ("status", "unknown")])
def test_payout_amount_mode_and_type_mismatch_never_marks_paid(fixture, field, value):
    client, store, _, _, responses, _ = fixture
    notification = payout_evidence(fixture)
    responses.objects[("payouts/po_fixture", "acct_seller")][field] = value
    assert deliver(client, notification).status_code == (503 if field == "livemode" else 400)
    assert store.payouts[0]["status"] == "bank_pending" and not store.processed_webhook_events


def test_dispute_binds_live_parent_amount_and_keeps_payment_record(fixture):
    client, store, backend, _, responses, _ = fixture
    store.orders[0].update(payment_status="paid", payment_reference="pi_source")
    backend.committed = snapshot(store)
    payment = source("live")
    payment["latest_charge"]["disputed"] = True
    responses.objects[("payment_intents/pi_source", None)] = payment
    obj = {"id": "dp_fixture", "object": "dispute", "livemode": True, "currency": "jpy", "amount": 1000,
           "payment_intent": "pi_source", "charge": "ch_source", "metadata": {}}
    responses.objects[("disputes/dp_fixture", None)] = obj
    notification = event("charge.dispute.created", obj, identifier="evt_dispute")
    responses.event(notification)
    assert deliver(client, notification, path=PLATFORM_PATH, secret=SECRET).status_code == 200
    assert store.orders[0]["dispute_status"] == "provider_dispute"
    assert store.orders[0]["payment_status"] == "paid"


def test_real_local_postgres_commit_and_restart_deduplicate(database, monkeypatch):
    backend, dsn = database
    responses = ProviderResponses(monkeypatch)
    settings = replace(configuration(), database_url=dsn)
    store = DemoStore(seed=False)
    async def prepare():
        async with backend.request(store):
            await backend.bind_empty_financial_scope(SCOPE)
            store.registered_users["seller"] = {"id": "seller"}
            store.connected_accounts["seller"] = {"account_id": "acct_seller"}
            await backend.save()
    asyncio.run(prepare())
    provider = StripeIntegration(settings.stripe_secret_key, settings.site_base_url,
        api_version=settings.stripe_api_version, charge_mode="separate", journal=backend, financial_scope=SCOPE)
    notification = event()
    responses.event(notification)
    account_current(responses, True)
    client = build_app(build_live_webhook_handler(settings, store, backend, provider), settings)
    assert deliver(client, notification).status_code == 200
    count = query(dsn, "select count(*) from toolbako_runtime.audit_events where action='webhook.processed'")[0][0]
    assert count == 1
    restarted = DemoStore(seed=False)
    client = build_app(build_live_webhook_handler(settings, restarted, backend, provider), settings)
    assert deliver(client, notification).json()["duplicate"]
    assert query(dsn, "select count(*) from toolbako_runtime.audit_events where action='webhook.processed'")[0][0] == 1


def test_main_dispatcher_preserves_guard_order_and_test_paths_stay_unavailable(monkeypatch):
    from app import main
    names = [item.cls.__name__ for item in main.app.user_middleware]
    ordered = ["TrustedHostMiddleware", "ProductionGuardMiddleware", "StripeWebhookGuardMiddleware",
               "SessionMiddleware", "LiveWebhookDispatcherMiddleware"]
    assert [names.index(name) for name in ordered] == sorted(names.index(name) for name in ordered)
    if "DatabaseBoundaryMiddleware" in names:
        assert names.index("LiveWebhookDispatcherMiddleware") < names.index("DatabaseBoundaryMiddleware")
    assert names.index("LiveWebhookDispatcherMiddleware") < names.index("GZipMiddleware")
    monkeypatch.setattr(main.app.state, "live_webhook_handler", None)
    client = TestClient(main.app)
    for path in (PLATFORM_PATH, CONNECT_PATH):
        response = client.post(path, content=b"{}")
        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-request-id"]


def test_main_body_limit_precedes_live_dispatcher(monkeypatch):
    from app import main
    async def never_called(*args):
        pytest.fail("oversized body reached live handler")
    monkeypatch.setattr(main.app.state, "live_webhook_handler", never_called)
    client = TestClient(main.app)
    for path in (PLATFORM_PATH, CONNECT_PATH):
        response = client.post(path, content=b"x" * 1_000_001)
        assert response.status_code == 413
        assert response.headers["x-content-type-options"] == "nosniff"


def test_main_signature_rejection_preserves_outer_guards_and_never_opens_database(fixture, monkeypatch):
    from app import main
    _, _, backend, _, _, handler = fixture
    monkeypatch.setattr(main.app.state, "live_webhook_handler", handler)
    client = TestClient(main.app)
    for path in (PLATFORM_PATH, CONNECT_PATH):
        response = client.post(path, content=b"{}", headers={"Stripe-Signature": "invalid"})
        assert response.status_code == 400
        assert response.headers["x-content-type-options"] == "nosniff"
    assert backend.opened == 0 and backend.saved == 0
