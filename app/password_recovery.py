"""Recovery-only TOTP step-up; never creates an application login or saves tokens.

Only the configured Auth provider is contacted. A browser token is used for
fresh user/factor ownership proof; only a token returned directly by the verify
endpoint is decoded for AAL2 and then checked against a fresh /user response.
"""
from __future__ import annotations

import base64
import json
import re
import time
from uuid import UUID

import httpx


class RecoveryMFAError(Exception):
    def __init__(self, category: str, *, retry: bool = False):
        super().__init__("recovery_mfa_unavailable")
        self.category = category
        self.retry = retry


def valid_recovery_token(token: object) -> bool:
    return isinstance(token, str) and 20 <= len(token) <= 4096 and bool(re.fullmatch(r"[A-Za-z0-9._~+/=-]+", token))


def valid_factor_id(value: object) -> bool:
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def _error_code(payload):
    codes = {payload[field] for field in ("code", "error_code")
             if isinstance(payload, dict) and isinstance(payload.get(field), str)}
    return next(iter(codes)) if len(codes) == 1 else ""


async def _request(client, settings, token, method, path, body=None):
    try:
        response = await client.request(method, f"{settings.supabase_url}/auth/v1/{path}",
            headers={"apikey": settings.supabase_anon_key, "Authorization": f"Bearer {token}"}, json=body)
    except httpx.HTTPError:
        raise RecoveryMFAError("provider_unavailable") from None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if response.status_code == 429:
        raise RecoveryMFAError("rate_limited", retry=True)
    if response.status_code != 200:
        code = _error_code(payload)
        if path.endswith("/verify") and response.status_code in {400, 401, 422} and code in {
            "mfa_verification_failed", "mfa_challenge_expired",
        }:
            raise RecoveryMFAError("code_rejected", retry=True)
        raise RecoveryMFAError("provider_rejected")
    if not isinstance(payload, dict):
        raise RecoveryMFAError("invalid_provider_response")
    return payload


async def _user(client, settings, token):
    payload = await _request(client, settings, token, "GET", "user")
    if not isinstance(payload.get("id"), str) or not payload["id"].strip():
        raise RecoveryMFAError("invalid_identity")
    factors = payload.get("factors", [])
    if not isinstance(factors, list) or len(factors) > 20:
        raise RecoveryMFAError("invalid_factors")
    verified = []
    seen = set()
    for factor in factors:
        if not isinstance(factor, dict):
            raise RecoveryMFAError("invalid_factors")
        if factor.get("factor_type") != "totp" or factor.get("status") != "verified":
            continue
        factor_id = factor.get("id")
        if not valid_factor_id(factor_id) or factor_id in seen:
            raise RecoveryMFAError("invalid_factors")
        seen.add(factor_id)
        name = factor.get("friendly_name")
        name = "".join(c for c in name if ord(c) >= 32 and ord(c) != 127)[:80] if isinstance(name, str) else ""
        verified.append({"id": factor_id, "friendly_name": name or "認証アプリ"})
    return payload["id"], verified


async def recovery_mfa_factors(settings, token):
    if not settings.supabase_ready or not valid_recovery_token(token):
        raise RecoveryMFAError("invalid_input")
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        _, factors = await _user(client, settings, token)
    return factors


async def verify_recovery_mfa(settings, token, factor_id, code):
    if (not settings.supabase_ready or not valid_recovery_token(token)
            or not valid_factor_id(factor_id) or not re.fullmatch(r"[0-9]{6}", code)):
        raise RecoveryMFAError("invalid_input")
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        user_id, factors = await _user(client, settings, token)
        if not any(factor["id"] == factor_id for factor in factors):
            raise RecoveryMFAError("foreign_factor")
        challenge = await _request(client, settings, token, "POST", f"factors/{factor_id}/challenge", {})
        if not valid_factor_id(challenge.get("id")):
            raise RecoveryMFAError("invalid_challenge")
        verified = await _request(client, settings, token, "POST", f"factors/{factor_id}/verify",
                                  {"challenge_id": challenge["id"], "code": code})
        upgraded = verified.get("access_token")
        if not valid_recovery_token(upgraded):
            raise RecoveryMFAError("invalid_assurance")
        try:
            segments = upgraded.split(".")
            if len(segments) != 3:
                raise ValueError()
            claims = json.loads(base64.urlsafe_b64decode(segments[1] + "=" * (-len(segments[1]) % 4)))
            if (not isinstance(claims, dict) or claims.get("aal") != "aal2" or claims.get("sub") != user_id
                    or type(claims.get("exp")) is not int or claims["exp"] <= time.time()):
                raise ValueError()
        except (ValueError, TypeError, UnicodeDecodeError):
            raise RecoveryMFAError("invalid_assurance") from None
        verified_user_id, _ = await _user(client, settings, upgraded)
        if verified_user_id != user_id:
            raise RecoveryMFAError("foreign_identity")
    return upgraded
