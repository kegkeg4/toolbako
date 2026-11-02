"""Live snapshot notifications, committed through the existing bound dataset.

No routes are installed by importing this module. The factory stays disabled
for the active test configuration. The dispatcher belongs inside the existing
host/security/body-limit middleware and outside DatabaseBoundaryMiddleware;
it owns the same backend's request/commit boundary for these two paths only.
No Stripe writes or emails are performed here.
"""
from copy import deepcopy
import asyncio
import logging
import re

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse

from .connect_status import account_status
from .financial_scope import configured_scope, key_mode, FinancialScopeError
from .finance import FinanceConflict
from .live_setup_plan import CONNECT_EVENTS, PLATFORM_EVENTS, STRIPE_API_VERSION
from .payment_source_proof import payment_source_proof
from .request_limits import WEBHOOK_BODY_LIMIT
from .stripe_bank_proof import bank_payout_proof
from .webhooks import StripeWebhookService


LIVE_PATHS = {
    "/webhooks/stripe/live": "platform",
    "/webhooks/stripe/connect/live": "connect",
}
logger = logging.getLogger(__name__)


def _require(condition, message="live notification does not match", status=400):
    if not condition:
        raise HTTPException(status, message)


def _reference(value, prefix):
    expression = r"cs_live_[A-Za-z0-9]+" if prefix == "cs" else prefix + r"_[A-Za-z0-9]+"
    return isinstance(value, str) and re.fullmatch(expression, value) is not None


def _amount(value, expected):
    return type(value) is int and type(expected) is int and expected > 0 and value == expected


def _metadata_matches(value, order, extra):
    return (isinstance(value, dict) and value.get("order_id") == order["id"]
            and (value.get("extra_id") == extra["id"] if extra else "extra_id" not in value))


async def _no_mail(*args):
    raise RuntimeError("live notification email delivery is disabled")


class LiveStripeWebhookService(StripeWebhookService):
    """Reuse existing state transitions after stronger live provider evidence."""

    def __init__(self, settings, store, *, backend, provider, kind, paid_out_refund_reconciler=None):
        super().__init__(settings, store, _no_mail, journal=backend, provider=provider)
        self.kind = kind
        self.paid_out_refund_reconciler = paid_out_refund_reconciler

    def validate_event(self, payload, signature):
        event = super().validate_event(payload, signature)
        expected = CONNECT_EVENTS if self.kind == "connect" else PLATFORM_EVENTS
        _require(event.get("object") == "event" and _reference(event.get("id"), "evt")
                 and event.get("livemode") is True
                 and event.get("api_version") == STRIPE_API_VERSION
                 and type(event.get("created")) is int and event["created"] > 0
                 and event["type"] in expected)
        _require(("account" in event) == (self.kind == "connect"), "event endpoint scope does not match")
        if self.kind == "connect":
            _require(_reference(event["account"], "acct"))
        return event

    def _resolve(self, event):
        state = super()._resolve(event)
        if self.kind == "connect":
            mappings = [(seller, account) for seller, account in self.store.connected_accounts.items()
                        if account.get("account_id") == event["account"]]
            _require(len(mappings) == 1, "related live account unavailable", 409)
            seller, mapping = mappings[0]
            _require(any(user.get("id") == seller for user in self.store.registered_users.values()),
                     "related live account unavailable", 409)
            state["mapped_seller"] = seller
            state["mapped_account"] = mapping
            if event["type"].startswith("payout."):
                _require(state["payout"] is not None and state["payout"].get("seller_id") == seller,
                         "related live payout unavailable", 409)
        else:
            _require(state["order"] is not None, "related live order unavailable", 409)
            order, extra = state["order"], state["extra"]
            _require(order.get("billing_type", "one_time") == "one_time")
            _require(not state["metadata"].get("extra_id") or extra is not None)
            _require(sum(item.get("id") == order["id"] for item in self.store.orders) == 1)
            amount = extra["amount"] if extra else order.get("primary_payment_amount", order.get("amount"))
            _require(type(amount) is int and amount > 0)
            state["expected_amount"] = amount
        return state

    async def _get_current(self, path, *, account=None):
        try:
            return await self.provider._get(path, account=account)
        except RuntimeError:
            raise HTTPException(503, "provider reconciliation unavailable") from None

    def _validate_payout(self, obj, state, event):
        payout = state["payout"]
        try:
            bank_payout_proof(obj, expected_mode="live", account_id=payout.get("account_id"),
                response_account_id=event["account"], amount=payout.get("net_amount"), currency="jpy",
                metadata={"payout_id": payout["id"]}, payout_id=payout.get("provider_payout_id") or None)
        except FinanceConflict:
            raise HTTPException(400, "live notification does not match") from None

    async def _source_payment(self, state, payment_id, *, dispute=False):
        _require(_reference(payment_id, "pi"))
        try:
            payment = await self.provider.retrieve_payment(payment_id)
        except RuntimeError:
            raise HTTPException(503, "provider reconciliation unavailable") from None
        order, extra = state["order"], state["extra"]
        _require(isinstance(payment, dict) and _metadata_matches(payment.get("metadata"), order, extra))
        charge = payment.get("latest_charge")
        _require(isinstance(charge, dict) and type(charge.get("disputed")) is bool
                 and charge.get("transfer_group") == f"order-{order['id']}")
        # The common source proof forbids disputed sources for financial writes.
        # A dispute notification must still prove that same source, while making
        # no write permission claim. Only this already-checked bool is relaxed.
        proof_input = payment
        if dispute and charge["disputed"]:
            proof_input = deepcopy(payment)
            proof_input["latest_charge"]["disputed"] = False
        try:
            proof = payment_source_proof(proof_input, payment_id=payment_id,
                amount=state["expected_amount"], order_id=order["id"], expected_mode="live", allow_refunded=True)
        except FinanceConflict:
            raise HTTPException(409, "payment source reconciliation required") from None
        previous = (extra or order).get("payment_reference")
        _require(not previous or previous == payment_id)
        state["source_proof"] = proof
        return proof

    async def _refresh_provider_state(self, event, state):
        kind, original = event["type"], state["obj"]
        if kind == "account.updated":
            _require(original.get("object") == "account" and original.get("id") == event["account"])
            await super()._refresh_provider_state(event, state)
            try:
                account_status(state["obj"], event["account"])
            except RuntimeError:
                raise HTTPException(503, "connected account reconciliation unavailable") from None
            return
        if kind.startswith("payout."):
            self._validate_payout(original, state, event)
            expected = {"payout.paid": "paid", "payout.failed": "failed", "payout.canceled": "canceled"}.get(kind)
            _require(not expected or original["status"] == expected)
            state["obj"] = await self._get_current("payouts/" + original["id"], account=event["account"])
            self._validate_payout(state["obj"], state, event)
            return
        order, extra = state["order"], state["extra"]
        if kind in {"refund.updated", "charge.refunded"}:
            paid_allocation = any(p.get("finance_v2") and (p.get("status") == "paid" or p.get("paid_at"))
                and any(a.get("order_id") == order["id"] for a in p.get("allocations", []))
                for p in self.store.payouts)
            special = ("paid_out_case_id" in state["metadata"] or "ledger_id" in state["metadata"]
                       or "paid_out_refund_case" in order or paid_allocation)
            if special:
                _require(self.paid_out_refund_reconciler is not None,
                         "paid-out refund reconciliation unavailable", 503)
                result = await self.paid_out_refund_reconciler(event, self.store, self.journal,
                    self.provider, self.provider.financial_scope)
                _require(result is True, "paid-out refund reconciliation unavailable", 503)
                state["paid_out_refund_handled"] = True
                return
        if kind.startswith("checkout.session."):
            _require(original.get("object") == "checkout.session" and _reference(original.get("id"), "cs"))
            current = await self._get_current("checkout/sessions/" + original["id"])
            _require(current.get("id") == original["id"] and current.get("object") == "checkout.session"
                     and current.get("livemode") is True and current.get("mode") == "payment"
                     and current.get("currency") == "jpy" and _amount(current.get("amount_total"), state["expected_amount"])
                     and _metadata_matches(current.get("metadata"), order, extra))
            status, payment_status = current.get("status"), current.get("payment_status")
            _require((status, payment_status) in {("complete", "paid"), ("complete", "unpaid"), ("expired", "unpaid")})
            state["obj"] = current
            state["live_checkout_outcome"] = "paid" if payment_status == "paid" else "expired" if status == "expired" else "pending"
            if payment_status == "paid":
                proof = await self._source_payment(state, current.get("payment_intent"))
                part = extra or order
                already_recorded = part.get("payment_reference") == proof["id"] and part.get("status" if extra else "payment_status") == "paid"
                _require(already_recorded or (proof["amount_refunded"] == 0
                         and not order.get("refund_status") and not order.get("dispute_status")
                         and order.get("status") not in {"cancelled", "cancel_pending"}),
                         "payment source reconciliation required", 409)
            return
        paths = {"refund.updated": ("refunds", "refund", "re"),
                 "charge.refunded": ("charges", "charge", "ch"),
                 "charge.dispute.created": ("disputes", "dispute", "dp")}
        path, object_type, prefix = paths[kind]
        _require(original.get("object") == object_type and _reference(original.get("id"), prefix))
        current = await self._get_current(path + "/" + original["id"])
        _require(current.get("id") == original["id"] and current.get("object") == object_type
                 and current.get("currency") == "jpy")
        if kind in {"refund.updated", "charge.refunded"}:
            metadata = current.get("metadata") or {}
            _require(isinstance(metadata, dict) and not {"paid_out_case_id", "ledger_id"} & set(metadata),
                     "paid-out refund reconciliation required", 409)
        proof = await self._source_payment(state, current.get("payment_intent"), dispute=kind == "charge.dispute.created")
        _require(current.get("id") == proof["charge"] if kind == "charge.refunded" else current.get("charge") == proof["charge"])
        if kind == "charge.refunded":
            _require(current.get("livemode") is True and _amount(current.get("amount"), state["expected_amount"])
                     and type(current.get("amount_refunded")) is int
                     and 0 < current["amount_refunded"] == proof["amount_refunded"] <= state["expected_amount"])
        elif kind == "refund.updated":
            _require(_amount(current.get("amount"), state["expected_amount"])
                     and _metadata_matches(current.get("metadata"), order, extra)
                     and (current.get("status") != "succeeded" or proof["amount_refunded"] == state["expected_amount"]))
        else:
            _require(current.get("livemode") is True and type(current.get("amount")) is int
                     and 0 < current["amount"] <= state["expected_amount"])
        state["obj"] = current

    def _validate(self, event_id, event, state, request_id):
        if state.get("paid_out_refund_handled"):
            return
        check = dict(state)
        if event["type"].startswith("payout."):
            # Event snapshot may be older than the freshly retrieved payout.
            check["event_type"] = "payout.updated"
        elif "live_checkout_outcome" in state:
            session = state["obj"]
            target = state["extra"] or state["order"]
            expected_id = state.get("recovered_checkout_session_id") or target.get("checkout_session_id")
            _require(session["id"] == expected_id)
            if state["live_checkout_outcome"] == "pending":
                return
            check["paid_checkout_event"] = state["live_checkout_outcome"] == "paid"
            check["event_type"] = "checkout.session.completed" if check["paid_checkout_event"] else "checkout.session.expired"
        super()._validate(event_id, event, check, request_id)

    def _apply(self, event, state):
        if state.get("paid_out_refund_handled"):
            return []
        applied = dict(state)
        if "live_checkout_outcome" in state:
            if state["live_checkout_outcome"] == "pending":
                self.store.audit(None, "webhook.payment_pending", event["id"])
                return []
            applied["paid_checkout_event"] = state["live_checkout_outcome"] == "paid"
            applied["event_type"] = "checkout.session.completed" if applied["paid_checkout_event"] else "checkout.session.expired"
        messages = super()._apply(event, applied)
        if messages:
            self.store.audit(None, "webhook.email_suppressed", f"{event['id']}:{len(messages)}")
        return []


class LiveWebhookHandler:
    def __init__(self, settings, store, backend, provider, *, paid_out_refund_reconciler=None):
        self.store, self.backend = store, backend
        self.services = {kind: LiveStripeWebhookService(settings, store, backend=backend,
            provider=provider, kind=kind, paid_out_refund_reconciler=paid_out_refund_reconciler)
            for kind in ("platform", "connect")}

    async def __call__(self, scope, receive, send):
        phase = "admission"
        try:
            _require(scope.get("method") == "POST", "method not allowed", 405)
            signatures = [value for key, value in scope["headers"] if key.lower() == b"stripe-signature"]
            _require(len(signatures) == 1, "invalid signature")
            service = self.services[LIVE_PATHS[scope["path"]]]
            payload = bytearray()
            async with asyncio.timeout(20):
                async for chunk in Request(scope, receive).stream():
                    _require(len(payload) + len(chunk) <= WEBHOOK_BODY_LIMIT, "webhook payload too large", 413)
                    payload.extend(chunk)
            payload = bytes(payload)
            service.validate_event(payload, signatures[0].decode("latin-1"))
            consumed = False
            async def replay():
                nonlocal consumed
                if consumed:
                    return {"type": "http.disconnect"}
                consumed = True
                return {"type": "http.request", "body": payload, "more_body": False}
            async with asyncio.timeout(55):
                phase = "database"
                async with self.backend.request(self.store):
                    phase = "reconciliation"
                    result = await service.handle(Request(scope, replay))
                    phase = "commit"
                    await self.backend.save()
                    phase = "cleanup"
            response = JSONResponse(result, headers={"Cache-Control": "no-store"})
        except HTTPException as exc:
            if exc.status_code >= 500:
                logger.warning("live_webhook_unavailable phase=%s", phase)
            response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                                    headers={"Cache-Control": "no-store"})
        except Exception:
            # No raw exception, provider payload, request ID, or credential output.
            logger.warning("live_webhook_unavailable phase=%s", phase)
            response = unavailable_response()
        await response(scope, receive, send)


def unavailable_response():
    return JSONResponse({"detail": "live notification processing unavailable"}, status_code=503,
                        headers={"Cache-Control": "no-store", "Retry-After": "30"})


def build_live_webhook_handler(settings, store, backend, provider, *, paid_out_refund_reconciler=None):
    """Only the final active live configuration can acquire a real handler."""
    try:
        scope = configured_scope(settings)
        if (scope is None or scope.mode != "live" or key_mode(settings.stripe_secret_key) != "live"
                or settings.demo_mode or not settings.is_deployed or not settings.database_url
                or settings.stripe_charge_mode != "separate" or settings.stripe_api_version != STRIPE_API_VERSION
                or getattr(settings, "stripe_connect_accounts_v2", False)
                or backend is None or provider.journal is not backend or provider.financial_scope != scope
                or provider.secret_key != settings.stripe_secret_key or provider.charge_mode != "separate"
                or provider.api_version != STRIPE_API_VERSION):
            return None
        secrets = [settings.stripe_webhook_secret, settings.stripe_connect_webhook_secret]
        if any(not isinstance(secret, str) or not re.fullmatch(r"whsec_[A-Za-z0-9]+", secret) for secret in secrets) or secrets[0] == secrets[1]:
            return None
    except (AttributeError, RuntimeError, ValueError):
        return None
    return LiveWebhookHandler(settings, store, backend, provider,
                              paid_out_refund_reconciler=paid_out_refund_reconciler)


class LiveWebhookDispatcherMiddleware:
    """Handle two live paths and prohibit live use of the generic test handler."""
    def __init__(self, app, *, handler=None, application_state=None, settings=None):
        self.app, self.handler, self.application_state = app, handler, application_state
        self.settings = settings

    def _legacy_test_allowed(self):
        try:
            return key_mode(self.settings.stripe_secret_key) == "test"
        except FinancialScopeError:
            # Local demo notifications predate provider credentials. Preserve
            # that exact empty-key case, without accepting an unknown deployed
            # configuration or turning a missing key into an unhandled 500.
            return (self.settings.stripe_secret_key == "" and self.settings.demo_mode
                    and not self.settings.is_deployed)

    async def __call__(self, scope, receive, send):
        # The legacy handler lacks the dedicated paid-out refund isolation.
        # After final key cutover it must not become a second live entry point.
        if (scope["type"] == "http" and scope.get("path") == "/webhooks/stripe"
                and scope.get("method") == "POST" and self.settings is not None
                and not self._legacy_test_allowed()):
            return await unavailable_response()(scope, receive, send)
        if scope["type"] != "http" or scope.get("path") not in LIVE_PATHS:
            return await self.app(scope, receive, send)
        handler = (getattr(self.application_state, "live_webhook_handler", None)
                   if self.application_state is not None else self.handler)
        if handler is None:
            return await unavailable_response()(scope, receive, send)
        return await handler(scope, receive, send)
