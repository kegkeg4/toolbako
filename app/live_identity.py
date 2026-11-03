"""Read-only eligibility policy for claims reused from shared Auth.

This does not verify identity documents, grant credentials, revoke profile
flags, alter MFA, or write a migration. Raw profile/session claims are not a
trust root. Only independently reviewed evidence recorded by an authorized
server-side workflow in a trusted audit source can populate review_registry.
An action name, digest, or caller-provided JSON alone proves no provenance.

Live trade eligibility and badge provenance are separate results; callers must
preserve original profile fields. A missing/invalid live review holds live
eligibility without deleting a legitimate historic credential. None is the
legacy test context, never a fallback for an invalid live configuration.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
from types import MappingProxyType
from uuid import UUID
from weakref import WeakSet

from .financial_scope import FinancialScope, configured_scope


REVIEW_ACTION = "live.claims.reviewed"
CLAIMS = ("identity", "founding", "certified")
BADGE_FIELDS = {
    "founding": ("is_founding_member", "founding_member_since"),
    "certified": ("is_certified_creator", "certified_creator_since"),
}
_REVIEW_KEYS = frozenset({"version", "scope", "subject_user_id", "claim",
                         "claim_sha256", "evidence_sha256", "source_record_sha256",
                         "source", "decision"})
_SOURCES = frozenset({"reviewed_import", "live_provider", "operator_review"})
_REGISTRIES = WeakSet()


class ClaimEvidenceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__("live_claims:" + code)


def _require(value, code):
    if not value:
        raise ClaimEvidenceError(code)


def _uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _hash(value):
    return type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _at(value):
    if type(value) is str:
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ClaimEvidenceError("invalid_time") from None
    _require(type(value) is datetime and value.tzinfo is not None
             and value.utcoffset() is not None, "invalid_time")
    return value.astimezone(timezone.utc)


def claim_digest(account, claim):
    """Fingerprint the exact positive claim reviewed, excluding private data.

    This fingerprint is not approval. It is useful only when matched to an
    independently reviewed audit record for this subject and live scope.
    """
    _require(type(account) is dict and _uuid(account.get("id")), "invalid_subject")
    _require(claim in CLAIMS, "invalid_claim")
    values = {"subject_user_id": account["id"], "claim": claim}
    if claim == "identity":
        _require(account.get("is_verified") is True and account.get("identity_status") == "verified", "claim_not_asserted")
        values.update(is_verified=True, identity_status="verified")
    else:
        flag, since = BADGE_FIELDS[claim]
        _require(account.get(flag) is True, "claim_not_asserted")
        values.update(enabled=True, since=_at(account.get(since)).isoformat())
    encoded = json.dumps(values, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class _Review:
    record_id: str
    at: datetime
    decision: str
    claim_sha256: str
    evidence_sha256: str
    source_record_sha256: str
    source: str


@dataclass(eq=False, frozen=True)
class ReviewRegistry:
    scope: FinancialScope
    _reviews: object = field(repr=False)


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate_review_field")
        result[key] = value
    return result


def review_registry(trusted_audit_events, scope, reviewer_ids, *, now):
    """Validate a trusted audit projection, not untrusted profile metadata.

    The caller must obtain these events from its authorized private audit
    source. reviewer_ids must come from verified operator governance, never
    request/JWT user_metadata. Event production requires actual evidence
    review; this function cannot establish that review merely from hashes.

    Other action names and valid different scopes are ignored. Malformed
    relevant events reject the whole registry, so a damaged later hold cannot
    silently expose an older approval. Equal-time conflicts also fail closed.
    """
    _require(type(scope) is FinancialScope and scope.mode == "live", "live_scope_required")
    checked_at = _at(now)
    _require(type(trusted_audit_events) in (list, tuple), "invalid_audit_source")
    _require(type(reviewer_ids) in (set, frozenset, list, tuple)
             and all(_uuid(value) for value in reviewer_ids), "invalid_reviewers")
    reviewers = frozenset(reviewer_ids)
    reviews, seen_ids = {}, {}
    for event in trusted_audit_events:
        _require(type(event) is dict, "invalid_audit_source")
        if event.get("action") != REVIEW_ACTION:
            continue
        _require(_uuid(event.get("id")) and _uuid(event.get("user_id"))
                 and event["user_id"] in reviewers, "untrusted_reviewer")
        try:
            payload = json.loads(event["target"], object_pairs_hook=_unique_json)
        except ClaimEvidenceError:
            raise
        except (KeyError, TypeError, ValueError):
            raise ClaimEvidenceError("invalid_review_record") from None
        _require(type(payload) is dict and set(payload) == _REVIEW_KEYS
                 and type(payload["version"]) is int and payload["version"] == 1, "invalid_review_record")
        scope_data = payload["scope"]
        _require(type(scope_data) is dict and set(scope_data) == {"mode", "platform_account", "ledger_id"}, "invalid_review_scope")
        try:
            record_scope = FinancialScope(**scope_data)
        except (ValueError, TypeError, RuntimeError):
            raise ClaimEvidenceError("invalid_review_scope") from None
        if record_scope != scope:
            continue
        _require(_uuid(payload["subject_user_id"]) and type(payload["claim"]) is str and payload["claim"] in CLAIMS
                 and type(payload["decision"]) is str and payload["decision"] in {"approve", "hold"}
                 and type(payload["source"]) is str and payload["source"] in _SOURCES, "invalid_review_record")
        _require(all(_hash(payload[key]) for key in ("claim_sha256", "evidence_sha256", "source_record_sha256")), "invalid_review_record")
        at = _at(event.get("created_at"))
        _require(at <= checked_at, "future_review_record")
        record = _Review(event["id"], at, payload["decision"], payload["claim_sha256"],
                         payload["evidence_sha256"], payload["source_record_sha256"], payload["source"])
        key = (payload["subject_user_id"], payload["claim"])
        duplicate = seen_ids.get(event["id"])
        _require(duplicate is None or duplicate == (key, record), "conflicting_review_id")
        seen_ids[event["id"]] = (key, record)
        previous = reviews.get(key)
        if previous is not None and previous.at == at:
            _require(previous == record, "ambiguous_review_order")
        if previous is None or previous.at < at:
            reviews[key] = record
    registry = ReviewRegistry(scope, MappingProxyType(reviews))
    _REGISTRIES.add(registry)
    return registry


@dataclass(frozen=True)
class ClaimDecision:
    eligible: bool
    reason: str


@dataclass(frozen=True)
class ClaimEligibility:
    live_required: bool
    identity: ClaimDecision
    founding: ClaimDecision
    certified: ClaimDecision

    @property
    def live_identity_eligible(self):
        return self.identity.eligible


def evaluate_claims(account, scope, registry=None):
    """Return eligibility without overwriting/revoking account/session claims.

    Pass the authoritative runtime account, not a profile or session. Registry
    data is independent of the account: embedded self-asserted proofs are never
    read. In the legacy None/test context this preserves existing truthiness.
    """
    account = account if type(account) is dict else {}
    if scope is None or (type(scope) is FinancialScope and scope.mode == "test"):
        return ClaimEligibility(False,
            ClaimDecision(bool(account.get("is_verified") or account.get("identity_status") == "verified"), "legacy_scope"),
            ClaimDecision(bool(account.get("is_founding_member")), "legacy_scope"),
            ClaimDecision(bool(account.get("is_certified_creator")), "legacy_scope"))

    def denied(reason):
        decision = ClaimDecision(False, reason)
        return ClaimEligibility(True, decision, decision, decision)

    if type(scope) is not FinancialScope or scope.mode != "live":
        return denied("invalid_live_scope")
    if not _uuid(account.get("id")):
        return denied("invalid_subject")
    if account.get("is_banned"):
        return denied("account_restricted")
    if type(registry) is not ReviewRegistry or registry not in _REGISTRIES or registry.scope != scope:
        return denied("provenance_unavailable")

    decisions = []
    for claim in CLAIMS:
        try:
            digest = claim_digest(account, claim)
        except ClaimEvidenceError:
            decisions.append(ClaimDecision(False, "claim_not_asserted"))
            continue
        record = registry._reviews.get((account["id"], claim))
        if record is None:
            decisions.append(ClaimDecision(False, "provenance_missing"))
        elif record.decision != "approve":
            decisions.append(ClaimDecision(False, "review_hold"))
        elif record.claim_sha256 != digest:
            decisions.append(ClaimDecision(False, "claim_changed"))
        else:
            decisions.append(ClaimDecision(True, "reviewed_live_evidence"))
    return ClaimEligibility(True, *decisions)


def annotate_live_eligibility(user, account, scope, registry=None):
    """Ephemeral annotation only. Never replace original verification or MFA."""
    result = dict(user)
    if type(account) is not dict or user.get("id") != account.get("id"):
        account = {}
    eligibility = evaluate_claims(account, scope, registry)
    result["live_claims_required"] = eligibility.live_required
    result["live_identity_eligible"] = eligibility.identity.eligible
    result["live_identity_reason"] = eligibility.identity.reason
    result["live_founding_eligible"] = eligibility.founding.eligible
    result["live_certified_eligible"] = eligibility.certified.eligible
    return result


class LiveClaimPolicy:
    """Integration with private runtime audit history; no evidence creation."""
    def __init__(self, settings_provider):
        self.settings_provider = settings_provider

    def scope(self):
        # Invalid configured scope must raise, not become a legacy fallback.
        return configured_scope(self.settings_provider())

    def requires_live(self):
        scope = self.scope()
        return scope is not None and scope.mode == "live"

    def registry(self, store, scope):
        if scope is None or scope.mode != "live":
            return None
        reviewers = self.settings_provider().admin_user_ids
        def build(events):
            try:
                return review_registry(events, scope, reviewers, now=datetime.now(timezone.utc))
            except ClaimEvidenceError:
                return None
        from .public_read import PublicView
        if isinstance(store, PublicView):
            # Only fresh, owner-scoped/anonymous projections reuse parsing.
            # The shared mutable store and financial callers remain uncached.
            reviewer_key = tuple(reviewers) if type(reviewers) in (tuple, list, set, frozenset) else None
            return store.claim_registry((self, scope, reviewer_key, review_registry), build)
        return build(getattr(store, "audit_logs", []))

    def eligibility(self, store, user_id):
        account = next((row for row in store.registered_users.values() if row.get("id") == user_id), {})
        scope = self.scope()
        return evaluate_claims(account, scope, self.registry(store, scope))

    def annotate(self, user, store):
        account = store.registered_users.get(user.get("username", ""))
        scope = self.scope()
        return annotate_live_eligibility(user, account, scope, self.registry(store, scope))

    @staticmethod
    def raw_badges(account):
        return {field: bool(account.get(field)) if field.startswith("is_") else account.get(field)
                for pair in BADGE_FIELDS.values() for field in pair}

    def badges(self, account, store):
        # Only the view is filtered. Grant/revoke writers must use raw_badges
        # so a pending provenance review cannot revoke another real badge.
        result = self.raw_badges(account)
        scope = self.scope()
        if scope is not None and scope.mode == "live":
            eligibility = evaluate_claims(account, scope, self.registry(store, scope))
            for claim, (flag, since) in BADGE_FIELDS.items():
                if not getattr(eligibility, claim).eligible:
                    result[flag], result[since] = False, None
        return result

    def cache_profile(self, account, session_user, fields):
        # Shared Auth values remain in the profile/session but cannot replace
        # independently reviewed live runtime claims during a login refresh.
        protected = {"is_verified", "identity_status", *(field for pair in BADGE_FIELDS.values() for field in pair)}
        live = self.requires_live()
        account.update({key: session_user.get(key) for key in fields if not live or key not in protected})
