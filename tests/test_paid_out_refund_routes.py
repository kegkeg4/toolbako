"""HTTP authorization and rendered controls; synthetic data, no provider IO."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape
import pytest
from starlette.requests import Request

from app.config import Settings
from app.data import DemoStore
from app.financial_scope import FinancialScopeError
from app.integrations import StripeIntegration
from app.mfa import SupabaseMFA
from app.paid_out_refund_execution import FIELD
from app.security import SessionService
from app import paid_out_refund_routes as routes


URL = "/admin/orders/order-one/paid-out-refund/"
ADMIN1, ADMIN2 = "admin-one", "admin-two"
LEDGER = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def http_route(monkeypatch):
    settings = Settings(environment="staging", demo_mode=False, stripe_secret_key="rk_test_synthetic",
        stripe_platform_account_id="acct_synthetic", stripe_ledger_id=LEDGER,
        stripe_charge_mode="separate", admin_user_ids=(ADMIN1, ADMIN2))
    store = DemoStore(seed=False)
    at = datetime.now(timezone.utc)
    for who in (ADMIN1, ADMIN2, "member"):
        user = {"id": who, "username": who}
        store.registered_users[who] = user
        store.account_sessions["sid-" + who] = {"user": deepcopy(user), "expires_at": at + timedelta(hours=1),
            "mfa_verified_at": at, "mfa_token_expires_at": at + timedelta(hours=1)}
    store.orders = [{"id": "order-one", "payment_reference": "pi_synthetic", "status": "cancel_pending",
                     "refund_cancel_accepted_by": "seller", "tool_name": "Synthetic tool", "amount": 1000}]
    store.payouts = [{"status": "paid", "allocations": [{"receipt_id": "pi_synthetic"}]}]
    sessions = SessionService(store, 3600)
    state = SimpleNamespace(settings=settings, store=store, backend=object(), stripe=object(),
                            mfa=SupabaseMFA(settings, sessions), calls=[])
    async def capture_open(*args, **kw): state.calls.append(("open", kw))
    async def capture_propose(*args, **kw): state.calls.append(("propose", kw))
    async def capture_approve(*args, **kw): state.calls.append(("approve", args[-1]))
    monkeypatch.setattr(routes, "open_paid_out_refund_case", capture_open)
    monkeypatch.setattr(routes, "propose_paid_out_refund_action", capture_propose)
    monkeypatch.setattr(routes, "approve_paid_out_refund_action", capture_approve)
    app = FastAPI()
    @app.middleware("http")
    async def synthetic_session(request, call_next):
        # Test-only session injection; the real app has signed SessionMiddleware.
        request.scope["session"] = {"sid": "sid-" + request.headers.get("x-synthetic-user", ADMIN1)}
        return await call_next(request)
    app.include_router(routes.create_paid_out_refund_router(
        lambda: (state.backend, state.store, state.stripe, state.settings, state.mfa),
        sessions.current, lambda user: bool(user and user["id"] in state.settings.admin_user_ids)))
    state.client = TestClient(app)
    return state


@pytest.mark.parametrize("who", ["member", "missing-session"])
@pytest.mark.parametrize("action", ["open", "propose", "approve"])
def test_nonadmins_are_denied_before_refund_service(http_route, who, action):
    response = http_route.client.post(URL + action, headers={"x-synthetic-user": who})
    assert response.status_code == 403 and not http_route.calls


@pytest.mark.parametrize("fault", ["expired", "missing", "future", "token_expired"])
def test_missing_or_stale_actual_mfa_returns_to_admin_authentication(http_route, fault):
    record = http_route.store.account_sessions["sid-" + ADMIN1]
    now = datetime.now(timezone.utc)
    if fault == "expired": record["mfa_verified_at"] = now - timedelta(minutes=11)
    if fault == "missing": record.pop("mfa_verified_at")
    if fault == "future": record["mfa_verified_at"] = now + timedelta(minutes=1)
    if fault == "token_expired": record["mfa_token_expires_at"] = now - timedelta(seconds=1)
    response = http_route.client.post(URL + "open", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/security/mfa?next=%2Fadmin%3Fsection%3Dfinance"
    assert not http_route.calls


@pytest.mark.parametrize("fault", ["backend", "no_scope", "one_admin", "demo", "destination"])
def test_unready_configuration_denies_before_refund_service(http_route, fault):
    if fault == "backend": http_route.backend = None
    elif fault == "no_scope": http_route.settings = replace(http_route.settings, stripe_platform_account_id="", stripe_ledger_id="")
    elif fault == "one_admin": http_route.settings = replace(http_route.settings, admin_user_ids=(ADMIN1, ADMIN1))
    elif fault == "demo": http_route.settings = replace(http_route.settings, demo_mode=True)
    else: http_route.settings = replace(http_route.settings, stripe_charge_mode="destination")
    response = http_route.client.post(URL + "open")
    assert response.status_code == 409 and not http_route.calls


def test_configured_but_unbound_ledger_is_rejected_by_actual_service_context(http_route, monkeypatch):
    from app.paid_out_refund_execution import open_paid_out_refund_case
    checked = []
    class UnboundBackend:
        _store = http_route.store
        _connection = object()
        async def assert_financial_scope(self, scope):
            checked.append(scope)
            raise FinancialScopeError()
    backend = UnboundBackend()
    http_route.backend = backend
    from app.financial_scope import configured_scope
    http_route.stripe = StripeIntegration(http_route.settings.stripe_secret_key, "https://synthetic.invalid",
        charge_mode="separate", journal=backend, financial_scope=configured_scope(http_route.settings))
    monkeypatch.setattr(routes, "open_paid_out_refund_case", open_paid_out_refund_case)
    response = http_route.client.post(URL + "open", data={"reserve_id": "reserve", "reserve_amount": 1000, "seller_recovery_amount": 640})
    assert response.status_code == 409 and len(checked) == 1
    assert "接続" not in response.text or "再送せず" in response.text
    assert FIELD not in http_route.store.orders[0] and not http_route.calls


def test_live_execute_proposal_is_rejected_before_service_or_provider(http_route):
    http_route.settings = replace(http_route.settings, stripe_secret_key="rk_live_synthetic")
    response = http_route.client.post(URL + "propose", data={"review_action": "execute"})
    assert response.status_code == 409 and not http_route.calls
    assert "本番の返金実行はまだ開始していません" in response.text


def test_live_reconciliation_proposal_can_reach_the_guarded_service(http_route):
    http_route.settings = replace(http_route.settings, stripe_secret_key="rk_live_synthetic")
    response = http_route.client.post(URL + "propose", data={"review_action": "reconcile"}, follow_redirects=False)
    assert response.status_code == 303
    assert http_route.calls == [("propose", {"action": "reconcile"})]


@pytest.mark.parametrize("action,data", [
    ("open", {"reserve_id": "one-reserve", "reserve_amount": "1000", "seller_recovery_amount": "640"}),
    ("propose", {"review_action": "execute"}), ("approve", {"proposal_id": "proposal-one"}),
])
def test_success_stays_in_admin_finance_with_explicit_saved_feedback(http_route, action, data):
    response = http_route.client.post(URL + action, data=data, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin?section=finance&paid_out_refund_saved=1#paid-out-refund"
    assert http_route.calls[0][0] == action
    if action == "open":
        assert http_route.calls[0][1] == {"reserve_id": "one-reserve", "reserve_amount": 1000, "seller_recovery_amount": 640}


@pytest.mark.parametrize("action,data,status", [
    ("delete", {}, 404), ("propose", {"review_action": "retry"}, 400),
    ("open", {"reserve_amount": "-1"}, 422), ("open", {"reserve_id": "x" * 101}, 422),
])
def test_invalid_actions_and_forms_do_not_call_service(http_route, action, data, status):
    response = http_route.client.post(URL + action, data=data)
    assert response.status_code == status and not http_route.calls


def test_service_rejection_is_sanitized_and_not_a_saved_redirect(http_route, monkeypatch):
    async def reject(*args, **kw): raise RuntimeError("private-sentinel")
    monkeypatch.setattr(routes, "approve_paid_out_refund_action", reject)
    response = http_route.client.post(URL + "approve", data={"proposal_id": "proposal-one"})
    assert response.status_code == 409 and "location" not in response.headers
    assert "private-sentinel" not in response.text and "再送せず" in response.text


def render(case=None, *, ready=True, execute=True, user=ADMIN1, saved=False):
    env = Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"), autoescape=select_autoescape(["html"]))
    req = Request({"type": "http", "method": "GET", "path": "/admin", "headers": [],
                   "query_string": b"paid_out_refund_saved=1" if saved else b""})
    return env.get_template("_admin_paid_out_refunds.html").render(request=req, user={"id": user}, paid_out_refunds={
        "ready": ready, "execute_ready": execute, "truncated": False,
        "items": [{"order_id": "order-one", "tool_name": "<script>private-sentinel</script>", "amount": 1000, "case": case}]})


def a_case(*, status="opened", proposer=ADMIN1, action="execute", consumed=False):
    return {"status": status, "buyer_refund": {"confirmed": 0},
            "proposal": {"id": "proposal-one", "proposed_by": proposer, "action": action},
            "consumed_proposals": ["proposal-one"] if consumed else []}


def test_rendered_success_feedback_distinguishes_saved_record_from_refund_completion():
    html = render(saved=True)
    assert 'role="status"' in html and "返金の確認内容を保存しました" in html
    assert "返金完了は下の状態をご確認ください" in html
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_unready_ui_has_no_money_operation_forms():
    assert "<form" not in render(ready=False, execute=False)


def test_open_form_requires_reserve_and_bounds_recovery_amount():
    html = render()
    assert 'action="' + URL + 'open"' in html
    assert 'name="reserve_id" required maxlength="100"' in html
    assert 'name="reserve_amount" type="number" min="1000" required' in html
    assert 'name="seller_recovery_amount" type="number" min="0" max="1000" required' in html


@pytest.mark.parametrize("user,consumed,visible", [(ADMIN1, False, False), (ADMIN2, False, True), (ADMIN2, True, False)])
def test_approval_form_only_for_distinct_admin_and_unconsumed_proposal(user, consumed, visible):
    html = render(a_case(consumed=consumed), user=user)
    assert (URL + 'approve"' in html) is visible
    assert "別の管理者の追加認証と30分以内の承認" in html if not consumed else True


def test_live_ui_has_no_execute_controls_but_allows_read_reconciliation():
    execute = render(a_case(), execute=False, user=ADMIN2)
    assert 'value="execute"' not in execute and URL + 'approve"' not in execute
    assert "本番の返金実行は停止中" in execute
    reconcile = render(a_case(status="unknown", action="reconcile"), execute=False, user=ADMIN2)
    assert 'value="reconcile"' in reconcile and URL + 'approve"' in reconcile
    assert "再送しません" in reconcile and 'value="execute"' not in reconcile


def test_refunded_case_has_no_resend_or_approval_form():
    html = render(a_case(status="buyer_refunded"), user=ADMIN2)
    assert "購入者への返金完了" in html and "<form" not in html


@pytest.mark.parametrize("execute", [False, True])
def test_webhook_awaiting_review_exposes_only_existing_refund_reconciliation(execute):
    html = render(a_case(status="awaiting_review", consumed=True), execute=execute, user=ADMIN2)
    assert "通知受信済み・返金の照合待ち" in html
    assert URL + 'propose"' in html and 'value="reconcile"' in html
    assert 'value="execute"' not in html and URL + 'approve"' not in html
