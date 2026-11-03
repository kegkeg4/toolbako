"""Receiver-version compatibility with synthetic signed events and GET-only I/O.

The memory boundary models rollback; these cases do not claim provider delivery
or real PostgreSQL durability. Outbound Stripe requests must remain on Clover.
"""
from copy import deepcopy
from dataclasses import replace
import time

import pytest

from app.config import Settings
from app.database import snapshot
from app.integrations import StripeIntegration
from app.live_setup_plan import (
    CONNECT_EVENTS, PLATFORM_EVENTS, STRIPE_API_VERSION, SetupPlanError,
    build_live_setup_plan, inspect_endpoint_configuration,
)
from app.live_webhooks import build_live_webhook_handler
from test_live_webhooks import (
    CONNECT_PATH, CONNECT_SECRET, PLATFORM_PATH, SECRET, SCOPE,
    MemoryBoundary, ProviderResponses, account_current, build_app,
    checkout_evidence, configuration, deliver, event, mock_memory_bank_validation, payout_evidence,
    scoped_refund_evidence, seed,
)
from test_payment_source_proof import source


CLOVER = "2026-02-25.clover"
DAHLIA = "2026-06-24.dahlia"
VERSIONS = (CLOVER, DAHLIA)


class CloverResponses(ProviderResponses):
    def respond(self, request):
        assert request.headers.get("stripe-version") == CLOVER
        return super().respond(request)


def environment(monkeypatch, version=DAHLIA):
    # Journal proof has its own real-PostgreSQL integration suite. This explicit
    # mock accepts only the artificial boundary, never an actual StateStore.
    mock_memory_bank_validation(monkeypatch)
    responses = CloverResponses(monkeypatch)
    store = seed()
    settings = replace(configuration(), stripe_webhook_api_version=version)
    backend = MemoryBoundary(store)
    provider = StripeIntegration(settings.stripe_secret_key, settings.site_base_url,
        charge_mode="separate", api_version=settings.stripe_api_version,
        journal=backend, financial_scope=SCOPE)
    handler = build_live_webhook_handler(settings, store, backend, provider)
    assert handler is not None
    return build_app(handler, settings), store, backend, provider, responses, handler


def notification_for(fixture, kind, version):
    _, store, backend, _, responses, _ = fixture
    if kind == "account.updated":
        notification = event()
        account_current(responses, True)
    elif kind.startswith("checkout.session."):
        expired = kind == "checkout.session.expired"
        notification = checkout_evidence(responses, kind=kind,
            status="expired" if expired else "complete", paid="unpaid" if expired else "paid")
        # Dahlia changed the hosted Checkout label; our receiver must not rely
        # on that display field when proving the session/payment relationship.
        label = "hosted_page" if version == DAHLIA else "hosted"
        notification["data"]["object"]["ui_mode"] = label
        responses.objects[("checkout/sessions/cs_live_fixture", None)]["ui_mode"] = "hosted"
    elif kind in {"refund.updated", "charge.refunded"}:
        notification = scoped_refund_evidence(fixture, kind)
    elif kind.startswith("payout."):
        notification = payout_evidence(fixture)
        notification["type"] = kind
        status = {"payout.created": "pending", "payout.updated": "in_transit",
            "payout.paid": "paid", "payout.failed": "failed", "payout.canceled": "canceled"}[kind]
        notification["data"]["object"]["status"] = status
        responses.objects[("payouts/po_fixture", "acct_seller")]["status"] = status
    else:
        assert kind == "charge.dispute.created"
        store.orders[0].update(payment_status="paid", payment_reference="pi_source")
        backend.committed = snapshot(store)
        payment = source("live")
        payment["latest_charge"]["disputed"] = True
        responses.objects[("payment_intents/pi_source", None)] = payment
        obj = {"id": "dp_fixture", "object": "dispute", "livemode": True,
            "currency": "jpy", "amount": 1000, "payment_intent": "pi_source",
            "charge": "ch_source", "metadata": {}}
        responses.objects[("disputes/dp_fixture", None)] = deepcopy(obj)
        notification = event(kind, obj, identifier="evt_dispute")
    notification["api_version"] = version
    responses.event(notification)
    return notification


def send(client, notification):
    connected = notification["type"] in CONNECT_EVENTS
    return deliver(client, notification,
        path=CONNECT_PATH if connected else PLATFORM_PATH,
        secret=CONNECT_SECRET if connected else SECRET)


def test_default_receiver_version_does_not_change_outbound_version():
    assert Settings.__dataclass_fields__["stripe_webhook_api_version"].default == CLOVER
    assert Settings.__dataclass_fields__["stripe_api_version"].default == CLOVER
    assert STRIPE_API_VERSION == CLOVER
    packet = build_live_setup_plan("https://toolbako.example", SCOPE.platform_account)
    assert all(item["api_version"] == CLOVER for item in packet["endpoints"].values())


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("kind", PLATFORM_EVENTS + CONNECT_EVENTS)
def test_all_twelve_signed_snapshot_types_keep_clover_outbound(monkeypatch, version, kind):
    fixture = environment(monkeypatch, version)
    client, store, backend, provider, responses, _ = fixture
    notification = notification_for(fixture, kind, version)
    response = send(client, notification)
    assert response.status_code == 200, response.text
    assert response.json() == {"received": True}
    assert notification["id"] in store.processed_webhook_events
    assert backend.saved == 1 and provider.api_version == CLOVER
    assert responses.calls
    if kind == "account.updated":
        assert store.connected_accounts["seller"]["payouts_enabled"] is True
    elif kind in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        assert store.orders[0]["payment_status"] == "paid"
        assert store.orders[0]["payment_reference"] == "pi_source"
    elif kind == "checkout.session.expired":
        assert store.orders[0]["payment_status"] == "expired"
    elif kind in {"refund.updated", "charge.refunded"}:
        assert store.orders[0]["refund_status"] == "completed"
    elif kind == "charge.dispute.created":
        assert store.orders[0]["dispute_status"] == "provider_dispute"
        assert store.orders[0]["payment_status"] == "paid"
    else:
        expected = {"payout.paid": "paid", "payout.failed": "bank_failed",
                    "payout.canceled": "bank_failed"}.get(kind, "bank_pending")
        assert store.payouts[0]["status"] == expected
        assert ("payouts/po_fixture", "acct_seller") in responses.calls


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("kind", ("platform", "connect"))
def test_endpoint_readback_requires_selected_receiver_version_exactly(version, kind):
    packet = build_live_setup_plan("https://toolbako.example", SCOPE.platform_account,
        webhook_api_version=version)
    expected = packet["endpoints"][kind]
    endpoint = {"id": "we_synthetic", "object": "webhook_endpoint", "livemode": True,
        "status": "enabled", "url": expected["url"], "api_version": version,
        "enabled_events": list(expected["enabled_events"])}
    def inspect():
        return inspect_endpoint_configuration(packet, kind,
            observed_platform_account=SCOPE.platform_account,
            observed_scope=expected["scope"], endpoint=endpoint)
    result = inspect()
    assert result["configuration_matches"]
    assert not any(result[name] for name in (
        "delivery_verified", "ledger_isolation_verified", "payments_activated"))
    endpoint["api_version"] = CLOVER if version == DAHLIA else DAHLIA
    assert inspect()["issues"] == ["api_version_mismatch"]
    assert packet["payments_activated"] is False


@pytest.mark.parametrize("version", [None, "", True, [], {}, "2026-03-25.dahlia",
    "2026-06-24.dahlia;preview", "2026-10-28.endive", "2026-02-25.clover "])
def test_plan_rejects_unreviewed_or_malformed_receiver_versions(version):
    with pytest.raises(SetupPlanError, match="^invalid_live_setup_plan_input$"):
        build_live_setup_plan("https://toolbako.example", SCOPE.platform_account,
            webhook_api_version=version)


@pytest.mark.parametrize("selected", VERSIONS)
@pytest.mark.parametrize("mismatch", ["other_supported", None, "missing", "",
    "2026-03-25.dahlia", "2026-10-28.endive", True])
def test_event_version_must_equal_configuration_before_database_or_provider(monkeypatch, selected, mismatch):
    fixture = environment(monkeypatch, selected)
    client, store, backend, _, responses, _ = fixture
    notification = notification_for(fixture, "account.updated", selected)
    if mismatch == "missing":
        notification.pop("api_version")
    else:
        notification["api_version"] = (
            DAHLIA if selected == CLOVER else CLOVER) if mismatch == "other_supported" else mismatch
    response = deliver(client, notification)
    assert response.status_code == 400
    assert backend.opened == 0 and responses.calls == []
    assert not store.processed_webhook_events


@pytest.mark.parametrize("version", [None, "", True, "2026-03-25.dahlia", "2026-10-28.endive"])
def test_factory_rejects_unreviewed_receiver_version(monkeypatch, version):
    _, store, backend, provider, responses, _ = environment(monkeypatch)
    settings = replace(configuration(), stripe_webhook_api_version=version)
    assert build_live_webhook_handler(settings, store, backend, provider) is None
    assert responses.calls == [] and backend.opened == 0


@pytest.mark.parametrize("fault", ["bad_secret", "expired_signature", "wrong_endpoint_scope", "test_mode", "duplicate_header"])
def test_dahlia_does_not_relax_signature_scope_or_mode(monkeypatch, fault):
    fixture = environment(monkeypatch)
    client, store, backend, _, responses, _ = fixture
    notification = notification_for(fixture, "account.updated", DAHLIA)
    options = {}
    if fault == "bad_secret": options["secret"] = SECRET
    elif fault == "expired_signature": options["timestamp"] = int(time.time()) - 301
    elif fault == "wrong_endpoint_scope": options.update(path=PLATFORM_PATH, secret=SECRET)
    elif fault == "test_mode": notification["livemode"] = False
    else: options["extra_headers"] = [("stripe-signature", "duplicate")]
    response = deliver(client, notification, **options)
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"
    assert backend.opened == 0 and responses.calls == []
    assert not store.processed_webhook_events


@pytest.mark.parametrize("fault", ["wrong_binding", "wrong_platform", "test_balance", "fresh_version", "fresh_payload", "commit"])
def test_dahlia_requires_current_provider_scope_and_commit_before_ack(monkeypatch, fault):
    fixture = environment(monkeypatch)
    client, store, backend, _, responses, _ = fixture
    notification = notification_for(fixture, "account.updated", DAHLIA)
    if fault == "wrong_binding":
        backend.binding = replace(SCOPE, ledger_id="22222222-2222-4222-8222-222222222222")
    elif fault == "wrong_platform": responses.account_id = "acct_Other"
    elif fault == "test_balance": responses.live = False
    elif fault == "fresh_version": responses.objects[("events/evt_fixture", "acct_seller")]["api_version"] = CLOVER
    elif fault == "fresh_payload": responses.objects[("events/evt_fixture", "acct_seller")]["data"]["object"]["payouts_enabled"] = False
    else: backend.fail_save = True
    response = deliver(client, notification)
    assert response.status_code == 503 and not response.json().get("received")
    assert not store.processed_webhook_events
    assert store.connected_accounts["seller"]["payouts_enabled"] is False
    assert backend.saved == 0 and "private" not in response.text


def test_dahlia_committed_replay_does_not_repeat_provider_object_or_state_transition(monkeypatch):
    fixture = environment(monkeypatch)
    client, store, backend, _, responses, _ = fixture
    notification = notification_for(fixture, "checkout.session.completed", DAHLIA)
    assert send(client, notification).status_code == 200
    assert send(client, notification).json() == {"received": True, "duplicate": True}
    assert store.tools[0]["sales_count"] == 1
    assert responses.calls.count(("checkout/sessions/cs_live_fixture", None)) == 1
    assert backend.saved == 2
