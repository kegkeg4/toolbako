"""Admin read-only inspection: isolated PG plus synthetic provider, no live I/O."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request

from app import main
from app.config import Settings
from app.data import DemoStore
from app.database import snapshot
from app.financial_scope import FinancialScope
from app.middleware import ProductionGuardMiddleware
from test_database import database, query
from test_refund_resumption import provider, seed_live


PATH = "/admin/orders/live_order/refund-inspection"


def request():
    return Request({"type": "http", "method": "POST", "path": PATH,
                    "scheme": "https", "server": ("example.test", 443),
                    "query_string": b"", "headers": [], "session": {}})


def wire(monkeypatch, backend, store, settings, stripe, mfa):
    for name, value in {"database_store": backend, "store": store, "settings": settings,
                        "stripe": stripe, "mfa": mfa, "session_service": mfa.sessions}.items():
        monkeypatch.setattr(main, name, value)


def inspection_result():
    return {"order_id": "<script>private</script>", "observed_at": datetime.now(timezone.utc),
            "verified_refund_count": 1, "remaining_count": 2, "remaining_amount": 1234,
            "fingerprint": "NOT_AN_APPROVAL", "execution_allowed": False}


@pytest.mark.parametrize("gate, expected", [("anonymous", 403), ("non_admin", 403),
                                            ("stale_mfa", 303), ("no_backend", 409)])
def test_route_auth_gates_before_any_provider_inspection(monkeypatch, gate, expected):
    calls = []
    async def inspect(*args, **kwargs):
        calls.append(True)
        raise AssertionError("inspection must not run")
    monkeypatch.setattr(main, "inspect_refund_resumption_live", inspect)
    monkeypatch.setattr(main, "current_user", lambda req: None if gate == "anonymous" else {"id": gate})
    monkeypatch.setattr(main, "is_admin", lambda user: bool(user and user["id"] != "non_admin"))
    monkeypatch.setattr(main.mfa, "recent", lambda req: gate != "stale_mfa")
    monkeypatch.setattr(main, "database_store", None if gate == "no_backend" else object())
    async def run():
        if expected == 303:
            response = await main.admin_inspect_live_refund(request(), "live_order")
            assert response.status_code == 303
            assert response.headers["location"] == "/security/mfa?next=%2Fadmin%3Fsection%3Dfinance"
            assert "no-store" in response.headers["cache-control"]
        else:
            with pytest.raises(HTTPException) as caught:
                await main.admin_inspect_live_refund(request(), "live_order")
            assert caught.value.status_code == expected
            assert "no-store" in caught.value.headers["Cache-Control"]
        assert calls == []
    asyncio.run(run())


@pytest.mark.parametrize("exception", [RuntimeError("SENSITIVE_PROVIDER_BODY"), TimeoutError("SENSITIVE_TIMEOUT")])
def test_route_hides_provider_failures_and_never_creates_approval(monkeypatch, exception):
    monkeypatch.setattr(main, "current_user", lambda req: {"id": "admin"})
    monkeypatch.setattr(main, "is_admin", lambda user: True)
    monkeypatch.setattr(main.mfa, "recent", lambda req: True)
    monkeypatch.setattr(main, "database_store", object())
    async def inspect(*args, **kwargs): raise exception
    monkeypatch.setattr(main, "inspect_refund_resumption_live", inspect)
    async def run():
        with pytest.raises(HTTPException) as caught:
            await main.admin_inspect_live_refund(request(), "live_order")
        assert caught.value.status_code == 409
        assert "返金は実行していません" in caught.value.detail
        assert "SENSITIVE" not in caught.value.detail
    asyncio.run(run())


def test_route_bounds_entire_scope_and_payment_probe(monkeypatch):
    monkeypatch.setattr(main, "current_user", lambda req: {"id": "admin"})
    monkeypatch.setattr(main, "is_admin", lambda user: True)
    monkeypatch.setattr(main.mfa, "recent", lambda req: True)
    monkeypatch.setattr(main, "database_store", object())
    deadlines = []
    class Deadline:
        async def __aenter__(self): raise TimeoutError()
        async def __aexit__(self, *args): return False
    def timeout(seconds):
        deadlines.append(seconds)
        return Deadline()
    monkeypatch.setattr(main.asyncio, "timeout", timeout)
    async def run():
        with pytest.raises(HTTPException) as caught:
            await main.admin_inspect_live_refund(request(), "live_order")
        assert caught.value.status_code == 409 and deadlines == [35]
    asyncio.run(run())


def test_inspection_post_keeps_origin_protection_and_is_not_automatically_gettable(monkeypatch):
    calls = []
    async def inspect(*args, **kwargs):
        calls.append(True)
        return inspection_result()
    monkeypatch.setattr(main, "inspect_refund_resumption_live", inspect)
    app = FastAPI()
    route = next(route for route in main.app.routes if getattr(route, "path", None) == "/admin/orders/{order_id}/refund-inspection")
    assert route.methods == {"POST"}
    app.router.routes.append(route)
    app.add_middleware(ProductionGuardMiddleware, settings=Settings(environment="staging", demo_mode=False, site_base_url="https://example.test"))
    client = TestClient(app, base_url="https://example.test")
    assert client.get(PATH).status_code == 405
    for headers in ({}, {"Origin": "https://evil.invalid"}):
        assert client.post(PATH, headers=headers).status_code == 403
    assert calls == []


@pytest.mark.parametrize("available", [False, True])
def test_finance_template_separates_live_inspection_from_sandbox_approval(available):
    env = Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"), autoescape=True)
    finance = {"sellers": [], "transactions": [], "payouts": [], **{key: 0 for key in (
        "gross_paid", "completed_gmv", "platform_fee_earned", "processor_fee_total", "available_to_payout",
        "paid_out", "held", "refund_total", "disputed_total", "blocked_for_payout")}}
    html = env.get_template("_admin_finance.html").render(finance=finance, finance_controls_ready=False,
        refund_live_inspection_available=available, payment_recovery_ready=False,
        payment_recoveries={"items": [], "truncated": False}, user={"id": "admin_two"},
        refund_resumptions={"items": [{"order_id": "live_order", "tool_name": "<script>private</script>",
                                     "proposal": {"status": "proposed", "proposed_by": "admin_one", "next_amount": 1000}}]})
    assert (PATH in html) is available
    assert "/refund-resumption/approve" not in html and "/refund-resumption/propose" not in html
    assert "<script>private</script>" not in html
    if available:
        assert "本番返金の承認前検査（読取のみ）" in html
        assert "検査結果は承認として保存されず" in html


def test_result_template_is_admin_only_safe_text_without_approval_or_replay_form():
    env = Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"), autoescape=True)
    html = env.get_template("admin_refund_inspection.html").render(inspection=inspection_result(),
        user={"display_name": "運営"}, admin_sections=main.ADMIN_SECTIONS, admin_section="finance", seo_jsonld=[])
    assert 'class="admin-sidebar"' in html and 'class="site-header"' not in html
    assert "&lt;script&gt;private&lt;/script&gt;" in html and "<script>private</script>" not in html
    assert "NOT_AN_APPROVAL" not in html
    assert "¥1,234" in html and "照合時刻（UTC）" in html
    assert "返金は実行していません。承認も記録していません。" in html
    assert "/refund-resumption/approve" not in html and "/refund-inspection" not in html
    assert 'href="/admin?section=finance#refund-resumption"' in html


@pytest.mark.parametrize("mode, backend_present, second_admin, charge_mode, available", [
    ("live", True, True, "separate", True),
    ("test", True, True, "separate", False),
    ("live", False, True, "separate", False),
    ("live", True, False, "separate", False),
    ("live", True, True, "destination", False),
])
def test_admin_finance_button_is_only_configuration_availability_not_a_probe_or_approval(
        monkeypatch, mode, backend_present, second_admin, charge_mode, available):
    scope = FinancialScope(mode, "acct_platform", str(uuid4()))
    settings = Settings(environment="staging", demo_mode=False, stripe_secret_key="rk_" + mode + "_SYNTHETIC",
        stripe_platform_account_id=scope.platform_account, stripe_ledger_id=scope.ledger_id,
        stripe_charge_mode=charge_mode, admin_user_ids=("admin_one", "admin_two") if second_admin else ("admin_one",))
    store = DemoStore(seed=False)
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "database_store", object() if backend_present else None)
    # No provider method exists: just opening finance must not issue live GETs.
    monkeypatch.setattr(main, "stripe", SimpleNamespace(financial_scope=scope))
    monkeypatch.setattr(main, "current_user", lambda req: {"id": "admin_one", "username": "admin_one"})
    async def inventory(*args): return {"items": [], "truncated": False}
    monkeypatch.setattr(main, "payment_recovery_inventory", inventory)
    async def run():
        response = await main.admin(request(), "finance")
        assert response.status_code == 200
        assert response.context["refund_live_inspection_available"] is available
        assert "inspection" not in response.context
        assert store.audit_logs == [] and store.orders == []
    asyncio.run(run())


def test_real_admin_route_live_inspection_renders_then_rejects_changed_source_without_writes(database, provider, monkeypatch):
    backend, dsn = database
    async def run():
        store, settings, stripe, mfa, req, scope, at = await seed_live(backend, dsn, provider)
        req.scope.update(path=PATH, method="POST", scheme="https", server=("example.test", 443), query_string=b"")
        wire(monkeypatch, backend, store, settings, stripe, mfa)
        async with backend.request(store):
            before = deepcopy(snapshot(store))
            page = await main.admin_inspect_live_refund(req, "live_order")
            assert page.status_code == 200 and "¥1,000" in page.body.decode()
            assert "no-store" in page.headers["cache-control"]
            assert page.context["inspection"]["execution_allowed"] is False
            assert snapshot(store) == before
            assert "refund_resumption" not in store.orders[0]
            # Success cannot authorize the existing financial action or a live POST.
            with pytest.raises(HTTPException) as caught:
                await main.admin_resume_refund(req, "live_order", "propose", "")
            assert caught.value.status_code == 409
            with pytest.raises(RuntimeError): await stripe.refund_payment("pi_live", "live_order", amount=1000)
            provider.payments["pi_live"]["latest_charge"]["amount"] = 999
            with pytest.raises(HTTPException) as caught:
                await main.admin_inspect_live_refund(req, "live_order")
            assert caught.value.status_code == 409 and snapshot(store) == before
        assert provider.requests and all(call.method == "GET" for call in provider.requests)
        assert query(dsn, "select count(*) from toolbako_runtime.stripe_operations where endpoint='refunds'") == [(0,)]
        assert query(dsn, "select count(*) from toolbako_runtime.email_outbox") == [(0,)]
        async with backend.request(store):
            assert snapshot(store) == before
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["unbound", "wrong_account", "paid_out_case", "unknown_intent", "stale_mfa", "banned_second_admin"])
def test_admin_route_does_not_hide_real_inspection_rejections(database, provider, monkeypatch, failure):
    backend, dsn = database
    async def run():
        store, settings, stripe, mfa, req, scope, at = await seed_live(backend, dsn, provider, bind=failure != "unbound")
        req.scope.update(path=PATH, method="POST", scheme="https", server=("example.test", 443), query_string=b"")
        wire(monkeypatch, backend, store, settings, stripe, mfa)
        async with backend.request(store):
            if failure == "wrong_account": provider.wrong_platform = True
            if failure == "paid_out_case": store.orders[0]["paid_out_refund_case"] = {}
            if failure == "unknown_intent":
                await backend.begin_operation("refund-live_order", "refunds", {"payment_intent": "pi_live"})
            if failure == "stale_mfa":
                store.account_sessions[req.session["sid"]]["mfa_verified_at"] = at - timedelta(hours=1)
            if failure == "banned_second_admin": store.registered_users["admin_two"]["is_banned"] = True
            before = deepcopy(snapshot(store))
            if failure == "stale_mfa":
                response = await main.admin_inspect_live_refund(req, "live_order")
                assert response.status_code == 303
            else:
                with pytest.raises(HTTPException) as caught:
                    await main.admin_inspect_live_refund(req, "live_order")
                assert caught.value.status_code == 409
            assert snapshot(store) == before
        assert all(call.method == "GET" for call in provider.requests)
        assert query(dsn, "select count(*) from toolbako_runtime.email_outbox") == [(0,)]
    asyncio.run(run())
