"""Read-only synthetic Stripe plus disposable real PostgreSQL reservations."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from app import live_payout_requests as requests
from app.database import StorageUnavailable
from app.finance import FinanceConflict, balance
from app.mfa import SupabaseMFA
from test_database import database, query
from test_live_payout_execution import live


async def clear_reservation(e):
    async with e.backend.request(e.store):
        e.store.payouts[0]["status"] = "cancelled"
        await e.backend.save()


async def submit(e, **kwargs):
    return await requests.request_live_payout(e.backend, e.store, e.stripe, e.settings,
        e.seller, kwargs.pop("amount", 850), kwargs.pop("key", "A" * 32),
        mfa=e.mfa, request=kwargs.pop("request", e.one), **kwargs)


async def hold(e):
    return await requests.hold_live_seller_payouts(e.backend, e.store, e.stripe, e.settings,
        e.seller, e.seller, mfa=e.mfa, request=e.one)


async def propose(e, **kwargs):
    return await requests.propose_live_seller_release(e.backend, e.store, e.stripe, e.settings,
        e.seller, e.seller, mfa=e.mfa, request=e.one, **kwargs)


async def approve(e, proposal, **kwargs):
    return await requests.approve_live_seller_release(e.backend, e.store, e.stripe, e.settings,
        e.seller, kwargs.pop("actor", e.buyer), proposal["id"], mfa=e.mfa,
        request=kwargs.pop("request", e.two), **kwargs)


def test_request_is_atomic_idempotent_and_never_posts(live):
    e = live
    async def run():
        await clear_reservation(e)
        async with e.backend.request(e.store):
            first = await submit(e)
            assert first["scope"] == e.scope.payload()
            assert first["account_id"] == "acct_seller"
            assert first["status"] == "requested"
            assert first["net_amount"] + first["fee"] == first["amount"] == 850
            assert await submit(e) is first
            with pytest.raises(FinanceConflict): await submit(e, amount=851)
            assert balance(e.store, e.seller) == 0
        assert query(e.dsn, "select count(*) from toolbako_runtime.finance_payouts where status='requested'") == [(1,)]
        assert query(e.dsn, "select count(*) from toolbako_runtime.audit_events where action='payout.requested'") == [(1,)]
        assert not e.posts
        assert query(e.dsn, "select count(*) from toolbako_runtime.email_outbox") == [(0,)]
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["off", "setup", "no_mfa", "other_actor", "get", "bad_key", "bool_amount", "low_amount", "over_balance", "paused", "foreign_scope", "wrong_owner", "disabled", "manual", "capability", "source_ledger", "charge_ledger", "source_mode", "refunded", "disputed", "pending_other", "paid_out_case", "mfa_after_get"])
def test_request_rejects_unsafe_context_without_reservation(live, fault):
    e = live
    async def run():
        await clear_reservation(e)
        if fault in {"off", "setup"}:
            e.settings = replace(e.settings, **({"stripe_live_payouts_enabled":False} if fault == "off" else {"stripe_connection_mode":"live_setup_only"}))
            e.mfa = SupabaseMFA(e.settings, e.mfa.sessions)
        async with e.backend.request(e.store):
            kwargs = {}
            mapping = e.store.connected_accounts[e.seller]
            if fault == "no_mfa": e.store.account_sessions[e.one.session["sid"]]["mfa_verified_at"] = None
            if fault == "other_actor": kwargs["request"] = e.two
            if fault == "get": e.one.scope["method"] = "GET"
            if fault == "bad_key": kwargs["key"] = "bad"
            if fault == "bool_amount": kwargs["amount"] = True
            if fault == "low_amount": kwargs["amount"] = 1
            if fault == "over_balance": kwargs["amount"] = 851
            if fault == "paused": mapping["payouts_paused"] = True
            if fault == "foreign_scope": mapping["scope"]["ledger_id"] = str(uuid4())
            if fault == "wrong_owner": e.account["metadata"]["user_id"] = str(uuid4())
            if fault == "disabled": e.account["payouts_enabled"] = False
            if fault == "manual": e.account["settings"]["payouts"]["schedule"]["interval"] = "daily"
            if fault == "capability": e.account["capabilities"]["transfers"] = "inactive"
            if fault == "source_ledger": e.payment["metadata"].pop("ledger_id")
            if fault == "charge_ledger": e.payment["latest_charge"]["metadata"].pop("ledger_id")
            if fault == "source_mode": e.payment["livemode"] = False
            if fault == "refunded": e.payment["latest_charge"]["amount_refunded"] = 1000
            if fault == "disputed": e.payment["latest_charge"]["disputed"] = True
            if fault == "paid_out_case": e.store.orders[0]["paid_out_refund_case"] = {"buyer_refund":{"state":"reserved"}}
            if fault == "pending_other": await e.backend.begin_operation("other-unknown", "refunds", {"synthetic":True})
            if fault == "mfa_after_get":
                def expire(req):
                    if req.url.path == "/v1/payment_intents/pi_finance":
                        e.store.account_sessions[e.one.session["sid"]]["mfa_verified_at"] = None
                e.get_hook = expire
            before = len(e.store.payouts)
            with pytest.raises((FinanceConflict, RuntimeError, ValueError)):
                await submit(e, **kwargs)
            assert len(e.store.payouts) == before
            assert not e.posts
        assert query(e.dsn, "select count(*) from toolbako_runtime.finance_payouts where status='requested'") == [(0,)]
    asyncio.run(run())


def test_request_save_failure_does_not_commit_reservation(live, monkeypatch):
    e = live
    async def run():
        await clear_reservation(e)
        async with e.backend.request(e.store):
            async def fail(*args): raise StorageUnavailable("synthetic")
            monkeypatch.setattr(e.backend, "save", fail)
            with pytest.raises(StorageUnavailable): await submit(e)
        assert query(e.dsn, "select count(*) from toolbako_runtime.finance_payouts where status='requested'") == [(0,)]
        assert not e.posts
    asyncio.run(run())


def test_hold_needs_no_stripe_io_and_release_requires_two_current_admins(live):
    e = live
    async def run():
        async with e.backend.request(e.store):
            calls = len(e.calls)
            await hold(e)
            assert len(e.calls) == calls
            assert e.store.payouts[0]["status"] == "held"
            proposal = await propose(e)
            with pytest.raises(FinanceConflict): await approve(e, proposal, actor=e.seller, request=e.one)
            await approve(e, proposal)
            assert e.store.connected_accounts[e.seller]["payouts_paused"] is False
            assert e.store.payouts[0]["status"] == "requested"
            with pytest.raises(FinanceConflict): await approve(e, proposal)
        assert not e.posts
        assert query(e.dsn, "select count(*) from toolbako_runtime.stripe_operations where endpoint=%s", (requests.RELEASE_ENDPOINT,)) == [(1,)]
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["expired", "future", "same_actor", "no_mfa", "proposer_banned", "changed_payout", "rehold", "wrong_scope", "forged_proof", "missing_receipt", "pending_other", "refunded", "disputed", "unknown_hold", "provider_disabled"])
def test_release_cannot_clear_changed_or_unverified_hold(live, fault):
    e = live
    async def run():
        async with e.backend.request(e.store):
            await hold(e)
            proposal = await propose(e)
            kwargs = {}
            if fault == "expired": kwargs["at"] = proposal["proposed_at"] + timedelta(minutes=31)
            if fault == "future": kwargs["at"] = proposal["proposed_at"] - timedelta(minutes=1)
            if fault == "same_actor": kwargs.update(actor=e.seller, request=e.one)
            if fault == "no_mfa": e.store.account_sessions[e.two.session["sid"]]["mfa_verified_at"] = None
            if fault == "proposer_banned": e.store.registered_users["seller"]["is_banned"] = True
            if fault == "changed_payout": e.store.payouts[0]["scheduled_for"] += timedelta(days=1)
            if fault == "rehold": await hold(e)
            if fault == "wrong_scope": proposal["scope"]["ledger_id"] = str(uuid4())
            if fault == "forged_proof": proposal["id"] = str(uuid4())
            if fault == "missing_receipt":
                await e.backend._connection.execute("delete from toolbako_runtime.stripe_operations where operation_id=%s", ("live-payout-release-review-" + proposal["id"],))
            if fault == "pending_other": await e.backend.begin_operation("pending-other", "payouts", {"synthetic":True})
            if fault == "refunded": e.store.orders[0]["refund_status"] = "pending"
            if fault == "disputed": e.store.orders[0]["dispute_status"] = "open"
            if fault == "unknown_hold": e.store.payouts[0]["hold_reason"] = "result unknown"
            if fault == "provider_disabled": e.account["payouts_enabled"] = False
            with pytest.raises((FinanceConflict, RuntimeError)):
                await approve(e, proposal, **kwargs)
            assert e.store.connected_accounts[e.seller]["payouts_paused"] is True
            assert not e.posts
    asyncio.run(run())


def test_release_preserves_other_hold_and_inflight_bank_status(live):
    e = live
    async def run():
        async with e.backend.request(e.store):
            # Other hold already existed before review: account pause may be
            # released, but an unrelated result-unknown hold is never resumed.
            p = e.store.payouts[0]
            p.update(status="review", hold_reason="result unknown")
            await hold(e)
            await approve(e, await propose(e))
            assert p["status"] == "review" and p["hold_reason"] == "result unknown"
        assert not e.posts
    asyncio.run(run())


@pytest.mark.parametrize("intent", [None, "bank", "transfer", "reversal"])
def test_cancel_only_untouched_reservations(live, intent):
    e = live
    async def run():
        async with e.backend.request(e.store):
            p = e.store.payouts[0]
            p["scope"] = e.scope.payload()
            if intent:
                key = "bank-payout-" + p["id"] if intent == "bank" else ("allocation-" if intent == "transfer" else "reverse-allocation-") + p["allocations"][0]["id"]
                await e.backend.begin_operation(key, "payouts" if intent == "bank" else "transfers", {"synthetic":True})
                with pytest.raises(FinanceConflict):
                    await requests.cancel_unstarted_live_payout(e.backend, e.store, e.stripe, e.settings, p, e.seller, mfa=e.mfa, request=e.one)
                assert p["status"] == "requested"
            else:
                await requests.cancel_unstarted_live_payout(e.backend, e.store, e.stripe, e.settings, p, e.seller, mfa=e.mfa, request=e.one)
                assert p["status"] == "cancelled" and balance(e.store, e.seller) == 850
        assert not e.posts
    asyncio.run(run())
