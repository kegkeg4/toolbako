"""Route admission and UI wiring; financial evidence is tested by each domain."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request

from app import main


def wire(monkeypatch, *, mode="live_read_only", refunds=False, payouts=False, actor=True, recent=True):
    cfg = SimpleNamespace(demo_mode=False, is_deployed=True, stripe_charge_mode="separate",
        stripe_connection_mode=mode, admin_user_ids=("one", "two"),
        stripe_live_refunds_enabled=refunds, stripe_live_payouts_enabled=payouts)
    monkeypatch.setattr(main, "settings", cfg)
    monkeypatch.setattr(main, "database_store", object())
    monkeypatch.setattr(main, "stripe", SimpleNamespace(financial_scope=SimpleNamespace(mode="live")))
    monkeypatch.setattr(main, "current_user", lambda r: {"id": "two"} if actor else None)
    monkeypatch.setattr(main, "is_admin", lambda u: bool(u))
    monkeypatch.setattr(main, "mfa", SimpleNamespace(recent=lambda r: recent))
    monkeypatch.setattr(main, "sandbox_payouts_ready", lambda s: False)
    monkeypatch.setattr(main, "store", SimpleNamespace(orders=[{"id": "order"}],
        payouts=[{"id": "payout", "finance_v2": True}]))
    return Request({"type": "http", "method": "POST", "path": "/admin", "headers": [], "session": {}})


@pytest.mark.parametrize("mode,refunds,payouts,allowed", [
    ("live_read_only", True, True, (False,False)),
    ("live_setup_only", True, True, (False,False)),
    ("live_financial", False,False,(False,False)),
    ("live_financial", True,False,(True,False)),
    ("live_financial", False,True,(False,True)),
    ("live_financial", "true", 1,(False,False)),
])
def test_operation_flags_do_not_open_other_financial_lanes(monkeypatch,mode,refunds,payouts,allowed):
    wire(monkeypatch,mode=mode,refunds=refunds,payouts=payouts)
    assert main.live_finance_route_ready("read")
    assert (main.live_finance_route_ready("refunds"),main.live_finance_route_ready("payouts")) == allowed
    assert not main.live_finance_route_ready("checkout")


@pytest.mark.parametrize("actor,recent,enabled,expected",[(False,True,True,403),(True,True,False,409),(True,False,True,303)])
def test_live_payout_admission_runs_before_domain(monkeypatch,actor,recent,enabled,expected):
    req=wire(monkeypatch,mode="live_financial",payouts=enabled,actor=actor,recent=recent)
    async def forbidden(*a,**kw): raise AssertionError("executor must not run")
    monkeypatch.setattr(main,"propose_live_payout",forbidden)
    async def run():
        if expected ==303:
            result=await main.admin_execute_live_payout(req,"payout","propose","")
            assert result.status_code==303 and "%2Fadmin%3Fsection%3Dfinance" in result.headers['location']
        else:
            with pytest.raises(HTTPException) as error: await main.admin_execute_live_payout(req,"payout","propose","")
            assert error.value.status_code==expected
    asyncio.run(run())


@pytest.mark.parametrize("lane,action",[(lane,action) for lane in ("payout","refund","payout_recovery","payment_recovery") for action in ("propose","approve")])
def test_routes_forward_server_mfa_and_request_to_domains(monkeypatch,lane,action):
    req=wire(monkeypatch,mode="live_financial",refunds=True,payouts=True)
    calls=[]
    async def record(*args,**kwargs): calls.append((args,kwargs))
    names={"payout":("propose_live_payout","approve_live_payout"),"refund":("propose_refund_resumption","approve_refund_resumption"),
        "payout_recovery":("propose_recovery","approve_recovery"),"payment_recovery":("propose_payment_recovery","approve_payment_recovery")}
    monkeypatch.setattr(main,names[lane][action=="approve"],record)
    async def run():
        if lane=="payout": return await main.admin_execute_live_payout(req,"payout",action,"proposal")
        if lane=="refund": return await main.admin_resume_refund(req,"order",action,"proposal")
        if lane=="payout_recovery": return await main.admin_reconcile_payout(req,"payout",action,"proposal")
        return await main.admin_reconcile_payment(req,"order","refund",action,"proposal","")
    result=asyncio.run(run())
    assert result.status_code==303 and result.headers['location'].startswith('/admin?section=finance')
    assert len(calls)==1 and calls[0][1]['mfa'] is main.mfa and calls[0][1]['request'] is req


@pytest.mark.parametrize("action",["propose","approve"])
def test_live_payout_provider_error_never_exposes_body(monkeypatch,action):
    req=wire(monkeypatch,mode="live_financial",payouts=True)
    async def fail(*a,**kw): raise RuntimeError("SENSITIVE_PROVIDER_BODY")
    monkeypatch.setattr(main,"propose_live_payout" if action=="propose" else "approve_live_payout",fail)
    async def run():
        with pytest.raises(HTTPException) as error: await main.admin_execute_live_payout(req,"payout",action,"proposal")
        assert error.value.status_code==409 and 'SENSITIVE' not in error.value.detail
    asyncio.run(run())


def test_finance_template_keeps_unsupported_live_actions_closed_and_shows_exact_next_amount():
    env=Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1]/"templates"),autoescape=True)
    finance={"sellers":[],"transactions":[],"payouts":[{"id":"payout","seller_id":"seller","finance_v2":True,"status":"requested",
        "created_at":datetime.now(timezone.utc),"amount":1000,"fee":160,"net_amount":840,
        "live_payout_review":{"id":"proposal","status":"proposed","proposed_by":"one","endpoint":"transfers","amount":400}}],
        **{k:0 for k in ("gross_paid","completed_gmv","platform_fee_earned","processor_fee_total","available_to_payout","paid_out","held","refund_total","disputed_total","blocked_for_payout")}}
    html=env.get_template("_admin_finance.html").render(finance=finance,finance_controls_ready=False,payment_recovery_ready=False,
        live_recovery_ready=True,live_payout_execution_ready=True,refund_execution_ready=False,payout_labels={},
        payment_recoveries={"items":[],"truncated":False},user={"id":"two"})
    assert '/execute-live/approve' in html and '400円で1件実行' in html
    assert '/cancel"' not in html and '/recover-failure/approve' not in html and '/refund-resumption/approve' not in html
    assert '本番の送金です' in html
