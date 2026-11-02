"""Read-only, server-authenticated preparation for live Express onboarding.

Nothing here creates an account/link, changes a mapping, or bypasses the live
POST guard. A returned plan is NOT a transferable authorization token. A future
executor must re-run this preparation in the SAME locked database request,
commit ``begin_operation(plan.operation_id, 'accounts', dict(parameters))``
before its first POST, and use exactly that idempotency key. It must never use
the legacy ``create_express_account`` rejected-operation retry loop.

Pending/unknown intents are GET-only: no match is not permission to POST again.
After a match, mapping + sanitized receipt + audit must be committed atomically
with the existing journal's ``reconcile_operation``. A succeeded receipt whose
mapping is missing needs an explicit atomic repair, not another create request.
Account links need a separate durable intent and validated short-lived handoff;
neither their URL nor company/bank/identity details belong in this plan.
"""
from dataclasses import dataclass
import re
from uuid import UUID

from .config import Settings
from .connect_status import account_status
from .data import DemoStore
from .database import PostgresStateStore, content_hash
from .financial_scope import FinancialScope, configured_scope
from .integrations import StripeIntegration
from .mfa import SupabaseMFA
from .security import SessionService


class OnboardingPreparationError(RuntimeError):
    def __init__(self):
        super().__init__("販売者の本番受取登録を安全に確認できません。処理を停止しました")


def _require(value):
    if not value:
        raise OnboardingPreparationError()


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def _account_id(value):
    return isinstance(value, str) and re.fullmatch(r"acct_[A-Za-z0-9]+", value) is not None


def _operation_id(scope, seller_id):
    return "live-connect-account-v1-" + content_hash({"scope": scope.payload(), "seller_id": seller_id})


def _metadata(scope, seller_id, operation_id):
    return {"user_id": seller_id, "ledger_id": scope.ledger_id,
            "platform_account_id": scope.platform_account,
            "onboarding_operation_id": operation_id}


def _parameters(scope, seller_id, operation_id):
    # No pre-filled personal, company, bank or terms-acceptance information.
    result = {"type": "express", "country": "JP",
              "capabilities[card_payments][requested]": "true",
              "capabilities[transfers][requested]": "true",
              "settings[payouts][schedule][interval]": "manual"}
    result.update({f"metadata[{key}]": value for key, value in _metadata(scope, seller_id, operation_id).items()})
    return result


@dataclass(frozen=True)
class OnboardingPlan:
    action: str
    scope: FinancialScope
    seller_id: str
    operation_id: str
    request_hash: str
    account_id: str = ""
    # Only create_intent_needed includes parameters. Never a key/URL/PII.
    create_parameters: tuple[tuple[str, str], ...] = ()
    account_status: tuple[tuple[str, bool], ...] = ()
    capabilities_active: bool = False


async def _context(backend, store, stripe, settings, mfa, request, seller_id):
    _require(isinstance(settings, Settings) and isinstance(store, DemoStore)
             and isinstance(backend, PostgresStateStore)
             and backend._store is store and backend._connection is not None
             and isinstance(stripe, StripeIntegration) and stripe.journal is backend
             and stripe.secret_key == settings.stripe_secret_key
             and stripe.charge_mode == settings.stripe_charge_mode == "separate"
             and not settings.demo_mode and not settings.stripe_connect_accounts_v2
             and isinstance(mfa, SupabaseMFA) and mfa.settings is settings
             and isinstance(mfa.sessions, SessionService) and mfa.sessions.store is store
             and _uuid(seller_id))
    user = mfa.sessions.current(request)
    _require(isinstance(user, dict) and user.get("id") == seller_id
             and mfa.recent(request))
    registered = [u for u in store.registered_users.values()
                  if isinstance(u, dict) and u.get("id") == seller_id]
    _require(len(registered) == 1 and not registered[0].get("is_banned"))
    scope = configured_scope(settings)
    _require(isinstance(scope, FinancialScope) and scope.mode == "live"
             and stripe.financial_scope == scope)
    # Binding comes from the private journal, not an input payload or session.
    await backend.assert_financial_scope(scope)
    await stripe.verify_financial_scope()
    return scope


def _mapping(store, scope, seller_id):
    _require(isinstance(store.connected_accounts, dict))
    seen = set()
    for owner, value in store.connected_accounts.items():
        _require(_uuid(owner) and isinstance(value, dict)
                 and _account_id(value.get("account_id"))
                 and value.get("scope") == scope.payload()
                 and value.get("onboarding_operation_id") == _operation_id(scope, owner)
                 and sum(isinstance(u, dict) and u.get("id") == owner
                         for u in store.registered_users.values()) == 1
                 and value["account_id"] not in seen)
        seen.add(value["account_id"])
    return store.connected_accounts.get(seller_id)


async def _inspect_account(stripe, scope, seller_id, operation_id, account_id):
    _require(_account_id(account_id) and account_id != scope.platform_account)
    remote = await stripe.retrieve_account(account_id)
    expected = _metadata(scope, seller_id, operation_id)
    _require(isinstance(remote, dict) and isinstance(remote.get("metadata"), dict)
             and all(remote["metadata"].get(k) == v for k, v in expected.items())
             and remote.get("country") == "JP" and remote.get("type") == "express"
             and remote.get("default_currency") == "jpy"
             and ("livemode" not in remote or remote["livemode"] is True))
    status = account_status(remote, account_id)
    payout_settings = remote.get("settings", {}).get("payouts", {}) if isinstance(remote.get("settings"), dict) else {}
    _require(isinstance(payout_settings, dict) and isinstance(payout_settings.get("schedule"), dict)
             and payout_settings["schedule"].get("interval") == "manual")
    # v1 Account has no required livemode field. Check the connected balance
    # using this exact platform credential and Stripe-Account header instead.
    balance = await stripe.retrieve_balance(account_id)
    _require(isinstance(balance, dict) and balance.get("object") == "balance"
             and balance.get("livemode") is True)
    capabilities = remote.get("capabilities")
    _require(isinstance(capabilities, dict))
    active = True
    for name in ("card_payments", "transfers"):
        capability = await stripe._get(f"accounts/{account_id}/capabilities/{name}")
        _require(isinstance(capability, dict) and capability.get("object") == "capability"
                 and capability.get("id") == name and capability.get("account") == account_id
                 and capability.get("requested") is True
                 and capability.get("status") in {"active", "inactive", "pending"}
                 and capabilities.get(name) == capability["status"])
        active = active and capability["status"] == "active"
    return tuple(sorted(status.items())), active


async def prepare_live_seller_onboarding(backend, store, stripe, settings, mfa, request, *, seller_id):
    """Inspect a real server session + bound journal + authenticated provider GETs.

    The caller must already hold ``backend.request(store)``. This function never
    calls begin/finish/reconcile/save and never creates or updates provider data.
    Returned actions describe the NEXT integration step, not completed onboarding
    or permission to start accepting payments. Normal session expiry may revoke a
    stale session through SessionService.current; raw identity/MFA is not reset.
    """
    scope = await _context(backend, store, stripe, settings, mfa, request, seller_id)
    mapping = _mapping(store, scope, seller_id)
    operation_id = _operation_id(scope, seller_id)
    parameters = _parameters(scope, seller_id, operation_id)
    digest = content_hash(parameters)
    details = await backend.operation_details(operation_id)
    result = await backend.operation_result(operation_id)
    plan = {"scope": scope, "seller_id": seller_id, "operation_id": operation_id, "request_hash": digest}
    if details is None:
        _require(result is None and mapping is None)
        phase = "new"
    else:
        _require(isinstance(details, dict) and details.get("endpoint") == "accounts"
                 and details.get("request_hash") == digest
                 and details.get("status") in {"pending", "unknown", "succeeded", "rejected"}
                 and isinstance(result, dict) and result.get("status") == details["status"])
        phase = details["status"]
        if phase == "rejected":
            _require(mapping is None)
            return OnboardingPlan(action="operator_review_required", **plan)
    # Complete bounded traversal; malformed/incomplete pagination propagates as
    # a failure. Zero matches never authorize a replay of an uncertain POST.
    accounts = await stripe.list_financial_objects("accounts")
    relevant = []
    for remote in accounts:
        _require(remote.get("object") == "account" and _account_id(remote.get("id")))
        metadata = remote.get("metadata")
        _require(isinstance(metadata, dict))
        if (metadata.get("user_id") == seller_id
                or metadata.get("onboarding_operation_id") == operation_id):
            relevant.append(remote)
    _require(len(relevant) <= 1)
    if phase == "new":
        # Orphan provider accounts are not silently adopted or duplicated.
        _require(not relevant)
        return OnboardingPlan(action="create_intent_needed", create_parameters=tuple(sorted(parameters.items())), **plan)
    if phase in {"pending", "unknown"} and not relevant:
        _require(mapping is None)
        return OnboardingPlan(action="outcome_unknown", **plan)
    _require(len(relevant) == 1)
    account_id = relevant[0]["id"]
    if phase == "succeeded":
        receipt = result.get("response")
        _require(isinstance(receipt, dict) and receipt.get("object") == "account"
                 and receipt.get("id") == account_id)
    if mapping is not None:
        _require(phase == "succeeded" and mapping["account_id"] == account_id)
    status, active = await _inspect_account(stripe, scope, seller_id, operation_id, account_id)
    if phase in {"pending", "unknown"}:
        action = "reconcile_intent_needed"
    elif mapping is None:
        action = "reconcile_mapping_needed"
    else:
        action = "account_ready" if active and all(value for _, value in status) else "onboarding_link_needed"
    return OnboardingPlan(action=action, account_id=account_id, account_status=status,
                          capabilities_active=active, **plan)
