"""Durable, default-off live Express SETUP executor; never financial mutations.

Only the authenticated seller's explicit POST may enter this module, inside the
same locked backend.request(store). An explicit live setup/financial stage and the independent
server flag are required. StripeIntegration._post remains live-blocked.

Intent commit precedes first provider I/O. Unknown account creates recover by
GET only; unknown Account Links cannot be retrieved and require operator review.
Link URLs exist only in the successful call's redacted handoff after DB commit.
There is no automatic new key, collection, payment, refund, transfer or payout.
"""
from dataclasses import dataclass, field
import time
from urllib.parse import urlparse

import httpx

from .database import content_hash, restore
from .deployment_check import _live_configuration_verified, _live_database_config
from .live_seller_onboarding import (
    prepare_live_seller_onboarding, _parameters, _metadata, _account_id,
)
from .live_setup_plan import STRIPE_API_VERSION


class SellerSetupError(RuntimeError):
    def __init__(self):
        super().__init__("販売者の受取登録を確認できません。再送せず、運営へお問い合わせください")


class SellerSetupUncertain(SellerSetupError):
    """A committed intent or provider result may exist; never send again."""


def _require(value):
    if not value:
        raise SellerSetupError()


class AccountLinkHandoff:
    __slots__ = ("__url", "__expires")

    def __init__(self, url, expires):
        self.__url, self.__expires = url, expires

    def __repr__(self):
        return "AccountLinkHandoff(<redacted>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("AccountLinkHandoff cannot be serialized")

    def for_redirect(self):
        _require(time.time() < self.__expires)
        return self.__url


@dataclass(frozen=True)
class SetupResult:
    action: str
    account_id: str = ""
    operation_id: str = ""
    handoff: AccountLinkHandoff | None = field(default=None, repr=False)


def _target_bound_connection(backend, settings):
    """Actual locked connection identity, not merely a configured URL."""
    _require(settings.stripe_connection_mode in {"live_setup_only", "live_financial"}
             and getattr(settings, "stripe_live_seller_setup_enabled", False) is True
             and _live_configuration_verified(settings)
             and backend.dsn == settings.database_url)
    config = _live_database_config(settings)
    conn = backend._connection
    _require(conn is not None and conn.info.host == config["host"]
             and conn.info.user == config["user"] and conn.info.port == 5432
             and conn.info.dbname == "postgres" and conn.pgconn.ssl_in_use is True)


def _actor(mfa, request, seller_id):
    user = mfa.sessions.current(request)
    _require(request.method == "POST" and isinstance(user, dict)
             and user.get("id") == seller_id and mfa.recent(request))


async def _prepare(backend, store, stripe, settings, mfa, request, seller_id):
    # Default-off check must precede all credential use, DB/provider reads.
    _require(settings.stripe_connection_mode in {"live_setup_only", "live_financial"}
             and getattr(settings, "stripe_live_seller_setup_enabled", False) is True)
    _target_bound_connection(backend, settings)
    _actor(mfa, request, seller_id)
    _require(stripe.base_url == settings.site_base_url and not backend._emails
             and stripe.api_version == settings.stripe_api_version == STRIPE_API_VERSION)
    return await prepare_live_seller_onboarding(backend, store, stripe, settings, mfa, request,
                                              seller_id=seller_id)


def _restore_checkpoint(backend, store):
    # A route may turn an exception into a 409/503 response. Never leave an
    # uncommitted mapping/audit for its generic end-of-request save to commit.
    if backend._checkpoint is not None:
        restore(store, backend._checkpoint)


async def _begin(backend, store, seller_id, operation_id, endpoint, data):
    store.audit(seller_id, "connect.live_setup_requested", operation_id)
    try:
        cached = await backend.begin_operation(operation_id, endpoint, data)
        _require(cached is None)
    except Exception:
        _restore_checkpoint(backend, store)
        raise SellerSetupUncertain() from None


async def _held(backend, operation_id, status="unknown"):
    try:
        details = await backend.operation_details(operation_id)
        if details and details["status"] == "pending":
            await backend.finish_operation(operation_id, status)
    except Exception:
        # The prior pending intent remains the safety barrier if this update
        # was not acknowledged. Do not turn that uncertainty into another POST.
        raise SellerSetupUncertain() from None


async def _send_setup(backend, stripe, settings, plan, mfa, request, *, operation_id, endpoint, data):
    _require(endpoint in {"accounts", "account_links"})
    _target_bound_connection(backend, settings)
    _actor(mfa, request, plan.seller_id)
    _require(stripe.journal is backend and stripe.secret_key == settings.stripe_secret_key
             and stripe.financial_scope == plan.scope and plan.scope.mode == "live"
             and stripe.api_version == settings.stripe_api_version == STRIPE_API_VERSION)
    canonical = (_parameters(plan.scope, plan.seller_id, plan.operation_id) if endpoint == "accounts"
                 else _link_data(settings, plan.account_id))
    _require(data == canonical and (endpoint != "accounts" or operation_id == plan.operation_id))
    await stripe.verify_financial_scope()
    _actor(mfa, request, plan.seller_id)
    expected = content_hash(data)
    details = await backend.operation_details(operation_id)
    _require(details and details.get("status") == "pending"
             and details.get("endpoint") == endpoint and details.get("request_hash") == expected)
    # No arbitrary origin, path, Stripe-Account header, method, or payload comes
    # from the request. The two callers generate their canonical parameters.
    headers = {"Authorization": f"Bearer {stripe.secret_key}", "Idempotency-Key": operation_id,
               "Stripe-Version": STRIPE_API_VERSION}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.post(f"https://api.stripe.com/v1/{endpoint}", data=data, headers=headers)
        if response.status_code in {400, 401, 403, 404, 422}:
            await _held(backend, operation_id, "rejected")
            return None
        if response.status_code not in {200, 201}:
            raise ValueError("unknown")
        obj = response.json()
        _require(isinstance(obj, dict))
        return obj
    except Exception:
        await _held(backend, operation_id)
        raise SellerSetupUncertain() from None


def _account_receipt(plan):
    return {"id": plan.account_id, "object": "account", "scope": plan.scope.payload(),
            "user_id": plan.seller_id, "onboarding_operation_id": plan.operation_id}


async def _save_mapping(backend, store, plan):
    _require(plan.action in {"reconcile_intent_needed", "reconcile_mapping_needed",
                             "account_ready", "onboarding_link_needed"})
    if plan.action in {"account_ready", "onboarding_link_needed"}:
        receipt = await backend.operation_result(plan.operation_id)
        _require(receipt and receipt.get("status") == "succeeded"
                 and receipt.get("response") == _account_receipt(plan))
        return SetupResult(plan.action, plan.account_id, plan.operation_id)
    receipt = _account_receipt(plan)
    if plan.action == "reconcile_mapping_needed":
        previous = await backend.operation_result(plan.operation_id)
        _require(previous and previous.get("response") == receipt)
    store.connected_accounts[plan.seller_id] = {
        "account_id": plan.account_id, "scope": plan.scope.payload(),
        "onboarding_operation_id": plan.operation_id, **dict(plan.account_status),
        "capabilities_active": plan.capabilities_active,
    }
    store.audit(plan.seller_id, "connect.live_account_mapped", plan.account_id)
    try:
        if plan.action == "reconcile_intent_needed":
            await backend.reconcile_operation(plan.operation_id, endpoint="accounts",
                                              request_hash=plan.request_hash, response=receipt)
        else:
            await backend.save()
    except Exception:
        _restore_checkpoint(backend, store)
        raise SellerSetupUncertain() from None
    return SetupResult("account_mapped", plan.account_id, plan.operation_id)


async def execute_live_seller_account(backend, store, stripe, settings, mfa, request, *, seller_id):
    """Create at most once or recover by GET, with an atomic mapping receipt."""
    plan = await _prepare(backend, store, stripe, settings, mfa, request, seller_id)
    if plan.action == "create_intent_needed":
        data = _parameters(plan.scope, seller_id, plan.operation_id)
        _require(dict(plan.create_parameters) == data)
        _actor(mfa, request, seller_id)
        await _begin(backend, store, seller_id, plan.operation_id, "accounts", data)
        # MFA/session can expire while provider/DB reads were in progress.
        _actor(mfa, request, seller_id)
        obj = await _send_setup(backend, stripe, settings, plan, mfa, request,
                                operation_id=plan.operation_id, endpoint="accounts", data=data)
        if obj is None:
            return SetupResult("operator_review_required", operation_id=plan.operation_id)
        try:
            _require(obj.get("object") == "account" and _account_id(obj.get("id"))
                     and obj.get("id") != plan.scope.platform_account
                     and isinstance(obj.get("metadata"), dict)
                     and all(obj["metadata"].get(k) == v for k, v in _metadata(plan.scope, seller_id, plan.operation_id).items())
                     and ("livemode" not in obj or obj["livemode"] is True))
            checked = await _prepare(backend, store, stripe, settings, mfa, request, seller_id)
            _require(checked.action == "reconcile_intent_needed" and checked.account_id == obj["id"])
        except Exception:
            await _held(backend, plan.operation_id)
            raise SellerSetupUncertain() from None
        return await _save_mapping(backend, store, checked)
    if plan.action in {"outcome_unknown", "operator_review_required"}:
        return SetupResult(plan.action, operation_id=plan.operation_id)
    return await _save_mapping(backend, store, plan)


def _link_data(settings, account_id):
    base = settings.site_base_url
    parsed = urlparse(base)
    _require(parsed.scheme == "https" and parsed.hostname in settings.allowed_hosts
             and parsed.port in {None, 443} and not parsed.username and not parsed.password
             and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
             and not any(ord(c) < 33 or c == "\\" for c in base))
    return {"account": account_id, "type": "account_onboarding",
            "return_url": base.rstrip("/") + "/seller/payments?returned=1",
            "refresh_url": base.rstrip("/") + "/seller/payments?refresh=1"}


def _link_id(plan, generation):
    return "live-connect-link-v1-" + content_hash({"scope": plan.scope.payload(), "seller_id": plan.seller_id,
                                                  "account_id": plan.account_id, "generation": generation})


def _link_receipt(plan, operation_id, created, expires):
    return {"id": operation_id, "object": "account_link", "account_id": plan.account_id,
            "scope": plan.scope.payload(), "created": created, "expires_at": expires}


def _validate_link(obj, now):
    _require(obj.get("object") == "account_link" and type(obj.get("created")) is int
             and type(obj.get("expires_at")) is int and now - 300 <= obj["created"] <= now + 60
             and obj["created"] < obj["expires_at"] and now < obj["expires_at"] <= now + 3600)
    url = obj.get("url")
    _require(isinstance(url, str) and len(url) <= 4096 and not any(ord(c) < 33 or c == "\\" for c in url))
    parsed = urlparse(url)
    _require(parsed.scheme == "https" and parsed.hostname == "connect.stripe.com"
             and parsed.port in {None, 443} and not parsed.username and not parsed.password
             and parsed.path.startswith("/") and parsed.path != "/" and not parsed.fragment)
    return url


async def execute_live_seller_account_link(backend, store, stripe, settings, mfa, request, *, seller_id):
    """Issue one link after durable intent; never recover an unknown URL by POST.

    An acknowledged, expired link may get the next deterministic generation on
    a fresh seller POST. Pending/unknown/rejected links cannot be bypassed by
    requesting another generation. No URL is stored or replayed after restart.
    """
    plan = await _prepare(backend, store, stripe, settings, mfa, request, seller_id)
    _require(plan.action in {"account_ready", "onboarding_link_needed"})
    await _save_mapping(backend, store, plan)  # validates the durable account receipt
    if plan.action == "account_ready":
        return SetupResult("account_ready", plan.account_id, plan.operation_id)
    mapping = store.connected_accounts[seller_id]
    data = _link_data(settings, plan.account_id)
    previous = mapping.get("onboarding_link")
    generation = 1
    if previous is not None:
        _require(isinstance(previous, dict) and set(previous) == {"operation_id", "generation"}
                 and type(previous["generation"]) is int and 1 <= previous["generation"] < 1000)
        generation = previous["generation"]
        operation_id = _link_id(plan, generation)
        _require(previous["operation_id"] == operation_id)
        details = await backend.operation_details(operation_id)
        result = await backend.operation_result(operation_id)
        _require(details and details.get("endpoint") == "account_links"
                 and details.get("request_hash") == content_hash(data) and result
                 and details.get("status") == result.get("status"))
        if details["status"] in {"pending", "unknown"}:
            return SetupResult("link_outcome_unknown", plan.account_id, operation_id)
        if details["status"] == "rejected":
            return SetupResult("operator_review_required", plan.account_id, operation_id)
        receipt = result.get("response")
        _require(details["status"] == "succeeded" and isinstance(receipt, dict)
                 and type(receipt.get("created")) is int and type(receipt.get("expires_at")) is int
                 and receipt["created"] < receipt["expires_at"]
                 and receipt == _link_receipt(plan, operation_id, receipt["created"], receipt["expires_at"]))
        if receipt["expires_at"] > time.time():
            return SetupResult("link_unavailable", plan.account_id, operation_id)
        generation += 1
    operation_id = _link_id(plan, generation)
    _require(await backend.operation_details(operation_id) is None
             and await backend.operation_result(operation_id) is None)
    mapping["onboarding_link"] = {"operation_id": operation_id, "generation": generation}
    _actor(mfa, request, seller_id)
    await _begin(backend, store, seller_id, operation_id, "account_links", data)
    _actor(mfa, request, seller_id)
    obj = await _send_setup(backend, stripe, settings, plan, mfa, request,
                            operation_id=operation_id, endpoint="account_links", data=data)
    if obj is None:
        return SetupResult("operator_review_required", plan.account_id, operation_id)
    try:
        url = _validate_link(obj, time.time())
    except Exception:
        await _held(backend, operation_id)
        raise SellerSetupUncertain() from None
    store.audit(seller_id, "connect.live_link_created", operation_id)
    try:
        await backend.reconcile_operation(operation_id, endpoint="account_links", request_hash=content_hash(data),
                                          response=_link_receipt(plan, operation_id, obj["created"], obj["expires_at"]))
    except Exception:
        _restore_checkpoint(backend, store)
        raise SellerSetupUncertain() from None
    return SetupResult("link_created", plan.account_id, operation_id, AccountLinkHandoff(url, obj["expires_at"]))
