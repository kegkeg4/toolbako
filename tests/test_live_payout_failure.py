"""Synthetic failed bank payout + isolated localhost PostgreSQL ledger."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest

from app import live_payout_execution as execution
from app import live_payout_failure as recovery
from app.database import StorageUnavailable, content_hash
from app.finance import FinanceConflict, balance
from app.integrations import StripeOutcomeUnknown
from app.live_setup_plan import STRIPE_API_VERSION
from app.mfa import SupabaseMFA
from test_database import database, query
from test_live_payout_execution import live, transfer, propose as propose_payout, approve as approve_payout


@pytest.fixture
def failed(live, monkeypatch):
    e = live
    async def initialize():
        await transfer(e)
        async with e.backend.request(e.store):
            await approve_payout(e, await propose_payout(e))
            e.store.payouts[0].update(scope=e.scope.payload(), status="bank_failed")
            await e.backend.save()
    asyncio.run(initialize())
    e.initial_posts = list(e.posts)
    e.posts.clear()
    e.bank_status = "failed"
    e.payouts[0].update(status="failed", balance_transaction="txn_debit", failure_balance_transaction="txn_return")
    now = int(datetime.now(timezone.utc).timestamp())
    e.returned = {"id":"txn_return", "object":"balance_transaction", "source":"po_live", "type":"payout_failure",
                  "status":"available", "currency":"jpy", "amount":690, "net":690, "fee":0, "created":now, "available_on":now}
    e.reversals = []
    e.recovery_fault = None
    e.recovery_before_post = None
    old_client = httpx.AsyncClient(timeout=20)
    old_transport = old_client._transport
    client_type = type(old_client)
    async def provider(req):
        path = req.url.path
        if path == "/v1/transfers/tr_live" and req.method == "GET":
            e.calls.append(req)
            return httpx.Response(200, json=e.transfers[0])
        if path == "/v1/balance_transactions/txn_return" and req.method == "GET":
            e.calls.append(req)
            assert req.headers["stripe-account"] == "acct_seller"
            return httpx.Response(200, json=e.returned)
        if path == "/v1/transfers/tr_live/reversals":
            e.calls.append(req)
            assert "stripe-account" not in req.headers and req.headers["stripe-version"] == STRIPE_API_VERSION
            if req.method == "GET":
                return httpx.Response(200, json={"object":"list", "data":e.reversals, "has_more":False})
            assert req.method == "POST"
            data = {k:v[0] for k,v in parse_qs(req.content.decode()).items()}
            key = req.headers["idempotency-key"]
            assert query(e.dsn, "select endpoint,request_hash,status from toolbako_runtime.stripe_operations where operation_id=%s", (key,)) == [
                (path[4:], content_hash(data), "pending")]
            saved = query(e.dsn, "select payload from toolbako_runtime.marketplace_state where singleton")[0][0]["payouts"][0]
            approved = saved[recovery.APPROVALS][key]
            assert approved["status"] == "executing" and approved["proposed_by"] == e.seller and approved["approved_by"] == e.buyer
            e.posts.append(req)
            if e.recovery_before_post: e.recovery_before_post(req)
            if e.recovery_fault == "timeout_before": raise httpx.ReadTimeout("synthetic", request=req)
            if e.recovery_fault == "rejected": return httpx.Response(403, text="PRIVATE")
            remote = {"id":"trr_recovered", "object":"transfer_reversal", "amount":int(data["amount"]), "currency":"jpy",
                      "transfer":"tr_live", "created":int(datetime.now(timezone.utc).timestamp()),
                      "metadata":{k[9:-1]:v for k,v in data.items() if k.startswith("metadata[")}}
            e.reversals.append(remote)
            e.transfers[0].update(reversed=True, amount_reversed=remote["amount"])
            e.available -= remote["amount"]
            if e.recovery_fault == "timeout_after": raise httpx.ReadTimeout("synthetic", request=req)
            if e.recovery_fault == "wrong_response": return httpx.Response(200, json={**remote,"amount":1})
            return httpx.Response(200, json=remote)
        return await old_transport.handle_async_request(req)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_type(transport=httpx.MockTransport(provider), **kw))
    return e


async def propose(e, **kwargs):
    return await recovery.propose_live_failed_payout(e.backend,e.store,e.stripe,e.settings,e.store.payouts[0],e.seller,
                                                   mfa=e.mfa,request=e.one,**kwargs)


async def approve(e, proposal, **kwargs):
    return await recovery.approve_live_failed_payout(e.backend,e.store,e.stripe,e.settings,e.store.payouts[0],
        kwargs.pop("actor",e.buyer),proposal["id"],mfa=e.mfa,request=kwargs.pop("request",e.two),**kwargs)


def test_live_failure_returns_only_verified_funds_and_separate_review_releases(failed):
    e = failed
    async def run():
        async with e.backend.request(e.store):
            first = await propose(e)
            assert first["action"] == "reverse" and first["amount"] == 690
            assert not e.posts and balance(e.store,e.seller) == 0
            with pytest.raises(FinanceConflict): await approve(e,first,actor=e.seller,request=e.one)
            assert await approve(e,first) == "reverse"
            assert e.store.payouts[0]["status"] == "bank_failed" and balance(e.store,e.seller) == 0
            with pytest.raises(FinanceConflict): await approve(e,first)
        async with e.backend.request(e.store):
            release = await propose(e)
            assert release["action"] == "release"
            assert await approve(e,release) == "release"
            assert balance(e.store,e.seller) == 850
            assert e.store.payouts[0]["status"] == "cancelled"
        assert len(e.posts) == 1 and e.posts[0].url.path.endswith("/reversals")
        assert query(e.dsn,"select count(*) from toolbako_runtime.finance_entries where kind='withdrawal_released'") == [(1,)]
        assert query(e.dsn,"select count(*) from toolbako_runtime.email_outbox") == [(0,)]
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["bank_paid","historically_paid","bank_mode","bank_ledger","return_pending","return_source","return_amount","return_net","return_fee","return_future","return_old","return_mode","return_missing","transfer_mode","transfer_owner","transfer_source","transfer_ledger","partial_reversal","outside_reversal","no_balance","pending_refund","scope","no_mfa","off"])
def test_unverified_bank_return_or_original_transfer_never_creates_reversal(failed,fault):
    e = failed
    async def run():
        if fault == "off":
            e.settings = replace(e.settings,stripe_live_payouts_enabled=False)
            e.mfa = SupabaseMFA(e.settings,e.mfa.sessions)
        async with e.backend.request(e.store):
            p = e.store.payouts[0]
            if fault == "bank_paid": e.bank_status = "paid"
            if fault == "historically_paid": p["paid_at"] = e.at
            if fault == "bank_mode": e.payouts[0]["livemode"] = False
            if fault == "bank_ledger": e.payouts[0]["metadata"]["ledger_id"] = str(uuid4())
            if fault == "return_pending": e.returned["status"] = "pending"
            if fault == "return_source": e.returned["source"] = "po_other"
            if fault == "return_amount": e.returned["amount"] = 689
            if fault == "return_net": e.returned["net"] = 689
            if fault == "return_fee": e.returned["fee"] = 1
            if fault == "return_future": e.returned["available_on"] += 10000
            if fault == "return_old": e.returned["created"] -= 10000
            if fault == "return_mode": e.returned["livemode"] = False
            if fault == "return_missing": e.payouts[0].pop("failure_balance_transaction")
            if fault == "transfer_mode": e.transfers[0]["livemode"] = False
            if fault == "transfer_owner": e.transfers[0]["destination"] = "acct_other"
            if fault == "transfer_source": e.transfers[0]["source_transaction"] = "ch_other"
            if fault == "transfer_ledger": e.transfers[0]["metadata"]["ledger_id"] = str(uuid4())
            if fault == "partial_reversal": e.transfers[0]["amount_reversed"] = 1
            if fault == "outside_reversal": e.reversals.append({"id":"trr_outside"})
            if fault == "no_balance": e.available = 0
            if fault == "pending_refund": p_order = e.store.orders[0]; p_order["primary_refund_status"] = "pending"
            if fault == "scope": p["scope"]["ledger_id"] = str(uuid4())
            if fault == "no_mfa": e.store.account_sessions[e.one.session["sid"]]["mfa_verified_at"] = None
            with pytest.raises((FinanceConflict,RuntimeError)):
                await propose(e)
            assert not e.posts
        assert query(e.dsn,"select count(*) from toolbako_runtime.stripe_operations where endpoint like 'transfers/%%/reversals'") == [(0,)]
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["same_admin","expired","banned","changed_amount","forged_review","missing_receipt","expired_after_get"])
def test_changed_or_forged_approval_cannot_send(failed,fault):
    e = failed
    async def run():
        async with e.backend.request(e.store):
            proposal = await propose(e)
            kwargs = {}
            if fault == "same_admin": kwargs.update(actor=e.seller,request=e.one)
            if fault == "expired": kwargs["at"] = proposal["proposed_at"]+timedelta(minutes=31)
            if fault == "banned": e.store.registered_users["seller"]["is_banned"] = True
            if fault == "changed_amount": proposal["amount"] = 1
            if fault == "forged_review": proposal["id"] = str(uuid4())
            if fault == "missing_receipt":
                await e.backend._connection.execute("delete from toolbako_runtime.stripe_operations where operation_id=%s",("live-failed-payout-review-"+proposal["id"],))
            if fault == "expired_after_get":
                def expire(req):
                    if req.url.path == "/v1/payment_intents/pi_finance":
                        e.store.account_sessions[e.two.session["sid"]]["mfa_verified_at"] = None
                e.get_hook = expire
            with pytest.raises((FinanceConflict,RuntimeError)):
                await approve(e,proposal,**kwargs)
            assert not e.posts
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["timeout_before","timeout_after","wrong_response","rejected"])
def test_unknown_never_reposts_and_only_unique_full_return_can_reconcile(failed,fault):
    e = failed
    async def run():
        async with e.backend.request(e.store):
            proposal = await propose(e)
            e.recovery_fault = fault
            with pytest.raises((StripeOutcomeUnknown,FinanceConflict)):
                await approve(e,proposal)
            assert len(e.posts) == 1 and e.store.payouts[0]["status"] == "bank_failed"
        e.recovery_fault = None
        async with e.backend.request(e.store):
            if fault in {"timeout_before","rejected"}:
                with pytest.raises(FinanceConflict): await propose(e)
            else:
                e.settings = replace(e.settings,stripe_live_payouts_enabled=False)
                e.mfa = SupabaseMFA(e.settings,e.mfa.sessions)
                reconcile = await propose(e)
                assert reconcile["action"] == "reconcile"
                assert await approve(e,reconcile) == "reconcile"
                assert await approve(e,await propose(e)) == "release"
            assert len(e.posts) == 1
    asyncio.run(run())


def test_failed_preprovider_checkpoint_sends_nothing(failed,monkeypatch):
    e=failed
    async def run():
        async with e.backend.request(e.store):
            proposal=await propose(e)
            async def fail(*args,**kwargs): raise StorageUnavailable("synthetic")
            monkeypatch.setattr(e.backend,"begin_operation",fail)
            with pytest.raises(StorageUnavailable): await approve(e,proposal)
            assert not e.posts
    asyncio.run(run())


def test_cancellation_cannot_bypass_durable_live_release(failed):
    e=failed
    async def run():
        async with e.backend.request(e.store):
            p=e.store.payouts[0]
            p.update(status="cancelled",failed_payout_recovery={"status":"released","action":"release"})
            with pytest.raises((FinanceConflict,StorageUnavailable)): await e.backend.save()
        assert query(e.dsn,"select status from toolbako_runtime.finance_payouts where id=%s",(e.payout_id,)) == [("bank_failed",)]
        assert not e.posts
    asyncio.run(run())
