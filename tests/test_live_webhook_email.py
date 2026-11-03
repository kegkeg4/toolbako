"""Future live-event outbox admission; no email or Stripe mutation is sent.

Memory cases verify control flow; the final local-PG cases use the restricted
web role and real atomic commits. They do not claim external delivery.
"""
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone

import psycopg
from psycopg.conninfo import make_conninfo
import pytest

from app.config import Settings
from app.data import DemoStore
from app.database import PostgresStateStore, content_hash, snapshot, restore
from app.integrations import StripeIntegration
from app.live_checkout import checkout_request_data, session_receipt
from app.live_webhooks import build_live_webhook_handler
import test_live_webhooks as evidence
from test_live_webhooks import (
    SCOPE, PLATFORM_PATH, SECRET, MemoryBoundary, ProviderResponses,
    build_app, checkout, checkout_evidence, configuration, deliver, seed,
)
from test_live_runtime_role import (
    admin_config, database as role_database, administrator, run, SCOPE as ROLE_SCOPE,
)


START = 1_800_000_000


def settings_for(**overrides):
    return replace(configuration(), **{
        "email_api_key": "synthetic-never-used",
        "email_from": "sender@example.invalid",
        "live_transaction_emails_enabled": True,
        "live_transaction_emails_since": START,
        **overrides,
    })


def order_store():
    store = seed()
    store.orders[0].update(buyer_email="buyer@example.invalid", seller_email="seller@example.invalid",
                          created_at=datetime.fromtimestamp(100, timezone.utc))
    return store


class MailBoundary(MemoryBoundary):
    def __init__(self, store):
        super().__init__(store)
        self.pending_mail = []
        self.committed_mail = []
        self.enqueue_calls = 0
        self.fail_enqueue = False

    @asynccontextmanager
    async def request(self, store):
        self.pending_mail = []
        try:
            async with super().request(store):
                yield
        finally:
            self.pending_mail = []

    def enqueue_email(self, *message):
        assert self._store is not None
        self.enqueue_calls += 1
        if self.fail_enqueue and self.enqueue_calls == 2:
            raise RuntimeError("synthetic enqueue failure")
        self.pending_mail.append(message)

    async def save(self):
        await super().save()
        self.committed_mail.extend(self.pending_mail)
        self.pending_mail = []


def setup(monkeypatch, settings=None):
    settings = settings or settings_for()
    responses = ProviderResponses(monkeypatch)
    store = order_store()
    backend = MailBoundary(store)
    provider = StripeIntegration(settings.stripe_secret_key, settings.site_base_url,
        charge_mode="separate", api_version=settings.stripe_api_version,
        journal=backend, financial_scope=SCOPE)
    key, data = checkout_request_data(provider, SCOPE, store.orders[0])
    backend.operations[key] = {"endpoint": "checkout/sessions", "request_hash": content_hash(data),
        "status": "succeeded", "created_at": datetime.fromtimestamp(100, timezone.utc),
        "response": session_receipt(checkout())}
    handler = build_live_webhook_handler(settings, store, backend, provider)
    assert handler is not None
    return build_app(handler, settings), store, backend, responses, handler


def notification(responses, *, created=START, identifier="evt_mail", kind="checkout.session.completed"):
    result = checkout_evidence(responses, kind=kind, identifier=identifier)
    result["created"] = created
    responses.event(result)
    return result


def send(client, event):
    return deliver(client, event, path=PLATFORM_PATH, secret=SECRET)


def test_mail_admission_is_default_off_and_activation_time_is_explicit():
    assert Settings.__dataclass_fields__["live_transaction_emails_enabled"].default is False
    assert Settings.__dataclass_fields__["live_transaction_emails_since"].default == 0
    with pytest.raises(ValueError):
        settings_for(live_transaction_emails_since=0)


@pytest.mark.parametrize("kind", ["checkout.session.completed", "checkout.session.async_payment_succeeded"])
@pytest.mark.parametrize("version", ["2026-02-25.clover", "2026-06-24.dahlia"])
def test_new_verified_payment_queues_existing_messages_once(monkeypatch, kind, version):
    client, store, backend, responses, _ = setup(monkeypatch, settings_for(stripe_webhook_api_version=version))
    event = notification(responses, kind=kind)
    event["api_version"] = version
    responses.event(event)
    assert send(client, event).status_code == 200
    assert store.orders[0]["payment_status"] == "paid"
    assert len(store.notifications) == 2
    assert len(backend.committed_mail) == 2
    assert [item[:2] for item in backend.committed_mail] == [
        ("buyer@example.invalid", "sender@example.invalid"),
        ("seller@example.invalid", "sender@example.invalid"),
    ]
    assert all("https://toolbako.example/orders/order_source" in item[3] for item in backend.committed_mail)
    assert sum(item["action"] == "webhook.email_queued" for item in store.audit_logs) == 1
    assert send(client, event).json()["duplicate"] is True
    assert len(backend.committed_mail) == 2
    # A distinct provider Event for the same completed payment is not a new
    # business transition and cannot regenerate its original notifications.
    second = notification(responses, created=START + 1, identifier="evt_mailOther", kind=kind)
    second["api_version"] = version
    responses.event(second)
    assert send(client, second).status_code == 200
    assert len(backend.committed_mail) == 2 and store.tools[0]["sales_count"] == 1


@pytest.mark.parametrize("overrides", [
    {"live_transaction_emails_enabled": False},
    {"email_api_key": ""}, {"email_from": ""},
    {"live_transaction_emails_since": START + 1},
])
def test_disabled_unready_or_pre_activation_event_still_commits_without_mail(monkeypatch, overrides):
    client, store, backend, responses, _ = setup(monkeypatch, settings_for(**overrides))
    assert send(client, notification(responses)).status_code == 200
    assert store.orders[0]["payment_status"] == "paid"
    assert "evt_mail" in store.processed_webhook_events
    assert backend.committed_mail == [] and backend.enqueue_calls == 0
    assert any(item["action"] == "webhook.email_suppressed" for item in store.audit_logs)


def test_enabling_does_not_backfill_event_processed_while_disabled(monkeypatch):
    client, store, backend, responses, handler = setup(monkeypatch, settings_for(live_transaction_emails_enabled=False))
    event = notification(responses)
    assert send(client, event).status_code == 200
    for service in handler.services.values():
        service.settings = settings_for()
    assert send(client, event).json()["duplicate"] is True
    assert backend.committed_mail == [] and backend.enqueue_calls == 0


def test_old_first_delivery_cannot_be_promoted_by_a_fresh_signature(monkeypatch):
    client, store, backend, responses, _ = setup(monkeypatch)
    assert send(client, notification(responses, created=START - 1)).status_code == 200
    assert store.orders[0]["payment_status"] == "paid"
    assert backend.committed_mail == []


def test_event_created_must_match_authenticated_provider_snapshot(monkeypatch):
    client, store, backend, responses, _ = setup(monkeypatch)
    event = notification(responses)
    responses.objects[("events/evt_mail", None)]["created"] = START - 1
    assert send(client, event).status_code == 503
    assert not store.processed_webhook_events and backend.committed_mail == []
    assert store.orders[0]["payment_status"] == "pending"


@pytest.mark.parametrize("fault", ["binding", "platform", "mode", "version", "ledger"])
def test_mail_flag_never_bypasses_existing_live_proofs(monkeypatch, fault):
    client, store, backend, responses, _ = setup(monkeypatch)
    event = notification(responses)
    if fault == "binding": backend.binding = replace(SCOPE, ledger_id="22222222-2222-4222-8222-222222222222")
    elif fault == "platform": responses.account_id = "acct_other"
    elif fault == "mode": responses.live = False
    elif fault == "version": event["api_version"] = "2026-06-24.dahlia"
    else: event["data"]["object"]["metadata"]["ledger_id"] = "22222222-2222-4222-8222-222222222222"
    responses.event(event)
    assert send(client, event).status_code in {400, 409, 503}
    assert backend.committed_mail == [] and backend.enqueue_calls == 0
    assert not store.processed_webhook_events


@pytest.mark.parametrize("fault", ["commit", "enqueue"])
def test_failure_cannot_ack_or_commit_partial_mail_and_retry_queues_once(monkeypatch, fault):
    client, store, backend, responses, _ = setup(monkeypatch)
    event = notification(responses)
    backend.fail_save = fault == "commit"
    backend.fail_enqueue = fault == "enqueue"
    assert send(client, event).status_code == 503
    assert not store.processed_webhook_events and store.orders[0]["payment_status"] == "pending"
    assert backend.committed_mail == [] and backend.pending_mail == []
    backend.fail_save = backend.fail_enqueue = False
    assert send(client, event).status_code == 200
    assert len(backend.committed_mail) == 2
    assert send(client, event).json()["duplicate"] is True
    assert len(backend.committed_mail) == 2


@pytest.mark.parametrize("since", [0, True, -1, "1800000000", None])
def test_runtime_malformed_activation_is_suppressed_without_losing_payment(monkeypatch, since):
    settings = settings_for()
    # Exercise defensive admission even for an invalid future DI container;
    # real Settings construction separately rejects this configuration.
    object.__setattr__(settings, "live_transaction_emails_since", since)
    client, store, backend, responses, _ = setup(monkeypatch, settings)
    assert send(client, notification(responses)).status_code == 200
    assert store.orders[0]["payment_status"] == "paid" and backend.committed_mail == []


@pytest.mark.parametrize("flag", [1, "true", None])
def test_runtime_non_boolean_enable_never_queues(monkeypatch, flag):
    settings = settings_for()
    object.__setattr__(settings, "live_transaction_emails_enabled", flag)
    client, _, backend, responses, _ = setup(monkeypatch, settings)
    assert send(client, notification(responses)).status_code == 200
    assert backend.committed_mail == []


def test_absent_recipient_is_not_added_as_an_invalid_outbox_row(monkeypatch):
    client, store, backend, responses, _ = setup(monkeypatch)
    store.orders[0].pop("buyer_email")
    backend.committed = snapshot(store)
    assert send(client, notification(responses)).status_code == 200
    assert len(backend.committed_mail) == 1
    assert backend.committed_mail[0][0] == "seller@example.invalid"


@pytest.mark.parametrize("kind", ["refund.updated", "charge.refunded"])
def test_new_verified_ordinary_refund_reuses_existing_buyer_message(monkeypatch, kind):
    client, store, backend, responses, handler = setup(monkeypatch)
    fixture = (client, store, backend, None, responses, handler)
    event = evidence.scoped_refund_evidence(fixture, kind)
    event["created"] = START
    responses.event(event)
    assert send(client, event).status_code == 200
    assert store.orders[0]["refund_status"] == "completed"
    assert len(backend.committed_mail) == 1
    assert backend.committed_mail[0][0] == "buyer@example.invalid"
    assert "全額返金を確認しました" in backend.committed_mail[0][2]
    assert send(client, event).json()["duplicate"] is True
    assert len(backend.committed_mail) == 1


def test_unpaid_checkout_does_not_queue_purchase_mail_until_verified_paid(monkeypatch):
    client, store, backend, responses, _ = setup(monkeypatch)
    pending = checkout_evidence(responses, paid="unpaid", identifier="evt_pendingMail")
    pending["created"] = START
    responses.event(pending)
    assert send(client, pending).status_code == 200
    assert store.orders[0]["payment_status"] == "pending" and backend.committed_mail == []
    assert send(client, notification(responses, kind="checkout.session.async_payment_succeeded")).status_code == 200
    assert len(backend.committed_mail) == 2


@pytest.mark.parametrize("fail_after_insert", [False, True])
def test_restricted_web_role_atomically_inserts_without_mail_read_or_update(
        role_database, monkeypatch, fail_after_insert):
    # The imported role fixture creates a fresh localhost DB and temporary
    # NOINHERIT/NOBYPASSRLS web role; it never adopts a production role.
    monkeypatch.setattr(evidence, "SCOPE", ROLE_SCOPE)
    created = int(datetime.now(timezone.utc).timestamp())
    def current_checkout(*args, **kwargs):
        return {**checkout(*args, **kwargs), "created": created, "expires_at": created + 86400}
    monkeypatch.setattr(evidence, "checkout", current_checkout)
    responses = ProviderResponses(monkeypatch)
    dsn = make_conninfo(**role_database[1])
    settings = settings_for(database_url=dsn, stripe_platform_account_id=ROLE_SCOPE.platform_account,
                            stripe_ledger_id=ROLE_SCOPE.ledger_id)
    backend = PostgresStateStore(dsn)
    store = DemoStore(seed=False)
    provider = StripeIntegration(settings.stripe_secret_key, settings.site_base_url,
        charge_mode="separate", api_version=settings.stripe_api_version,
        journal=backend, financial_scope=ROLE_SCOPE)
    async def prepare():
        async with backend.request(store):
            await backend.assert_financial_scope(ROLE_SCOPE)
            restore(store, snapshot(order_store()))
            key, data = checkout_request_data(provider, ROLE_SCOPE, store.orders[0])
            await backend.begin_operation(key, "checkout/sessions", data)
            await backend.finish_operation(key, "succeeded", session_receipt(evidence.checkout()))
            await backend.save()
    run(prepare())
    handler = build_live_webhook_handler(settings, store, backend, provider)
    assert handler is not None
    client = build_app(handler, settings)
    event = notification(responses)
    original_save = backend._save
    async def injected_failure(conn, *, payload=None):
        committed = await original_save(conn, payload=payload)
        if fail_after_insert:
            raise RuntimeError("synthetic after all SQL, before transaction commit")
        return committed
    monkeypatch.setattr(backend, "_save", injected_failure)
    try:
        response = send(client, event)
        assert response.status_code == (503 if fail_after_insert else 200), response.text
        with administrator(role_database) as conn:
            count = conn.execute("select count(*) from toolbako_runtime.email_outbox").fetchone()[0]
            payload = conn.execute("select payload from toolbako_runtime.marketplace_state where singleton").fetchone()[0]
            assert count == (0 if fail_after_insert else 2)
            persisted = DemoStore(seed=False)
            restore(persisted, payload)
            assert (event["id"] in persisted.processed_webhook_events) is not fail_after_insert
            assert payload["orders"][0]["payment_status"] == ("pending" if fail_after_insert else "paid")
        monkeypatch.setattr(backend, "_save", original_save)
        assert send(client, event).status_code == 200
        assert send(client, event).json()["duplicate"] is True
        with administrator(role_database) as conn:
            assert conn.execute("select count(*) from toolbako_runtime.email_outbox").fetchone() == (2,)
        # Actual denied SQL proves that enqueue did not require broadening the
        # web principal into a mail reader, queue updater, or delivery worker.
        with psycopg.connect(dsn, autocommit=True) as conn:
            for statement in (
                "select recipient,body from toolbako_runtime.email_outbox",
                "update toolbako_runtime.email_outbox set status='sent'",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(statement)
    finally:
        run(backend.close())
