"""Server-side Supabase TOTP enrollment and short-lived step-up authentication.

Provider tokens never enter the signed browser cookie or profile. AAL2 is only
accepted from a successful verify response obtained directly from Supabase.
"""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID
from xml.etree import ElementTree

import httpx
from fastapi import HTTPException


def totp_qr_image(svg: object) -> str:
    """Keep provider SVG in an image context, including its XML declaration."""
    # Supabase's renderer emits a rectangle for every QR module, including
    # white modules. Real enrollment SVGs exceed 100 KB. Allow the full grid
    # while retaining a bounded input and rejecting entity declarations.
    if not isinstance(svg, str) or not svg or len(svg) > 4 * 1024 * 1024:
        return ""
    if "<!DOCTYPE" in svg.upper() or "<!ENTITY" in svg.upper():
        return ""
    try:
        root = ElementTree.fromstring(svg)
    except ElementTree.ParseError:
        return ""
    if root.tag not in {"svg", "{http://www.w3.org/2000/svg}svg"}:
        return ""
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


class SupabaseMFA:
    def __init__(self, settings, sessions):
        self.settings = settings
        self.sessions = sessions

    def record(self, request):
        if not self.sessions.current(request):
            raise HTTPException(401, "再度ログインしてください")
        return self.sessions.store.account_sessions[request.session["sid"]]

    def attach_tokens(self, request, data):
        record = self.record(request)
        record["provider_tokens"] = {
            "access_token": str(data.get("access_token", "")),
            "refresh_token": str(data.get("refresh_token", "")),
        }

    def recent(self, request) -> bool:
        try:
            record = self.record(request)
        except HTTPException:
            return False
        verified_at = record.get("mfa_verified_at")
        expires_at = record.get("mfa_token_expires_at")
        now = datetime.now(timezone.utc)
        return bool(
            isinstance(verified_at, datetime) and isinstance(expires_at, datetime)
            and now - timedelta(minutes=10) <= verified_at <= now < expires_at
        )

    @staticmethod
    def _invalid_access_token(response) -> bool:
        # GoTrue returns 403/bad_jwt for an expired JWT. Other 403s (for
        # example disabled MFA) are not a reason to rotate credentials.
        if response.status_code == 401:
            return True
        if response.status_code != 403:
            return False
        try:
            data = response.json()
        except ValueError:
            return False
        return isinstance(data, dict) and data.get("error_code", data.get("code")) == "bad_jwt"

    @staticmethod
    def _require_login(record):
        for key in ("provider_tokens", "mfa_verified_at", "mfa_token_expires_at"):
            record.pop(key, None)
        raise HTTPException(401, "ログインの有効期限が切れました。再度ログインしてください")

    @staticmethod
    def _check_availability(response):
        if response.status_code == 429:
            raise HTTPException(429, "認証回数の上限です。時間をおいて再度お試しください")
        if response.status_code >= 500:
            raise HTTPException(503, "認証サービスへ接続できませんでした")

    async def _request(self, request, method, path, body=None):
        if not self.settings.supabase_ready:
            raise HTTPException(503, "認証サービスの設定が必要です")
        record = self.record(request)
        tokens = record.get("provider_tokens", {})
        if not tokens.get("access_token"):
            self._require_login(record)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                headers = {"apikey": self.settings.supabase_anon_key,
                           "Authorization": f"Bearer {tokens['access_token']}"}
                response = await client.request(method, f"{self.settings.supabase_url}/auth/v1/{path}", headers=headers, json=body)
                if self._invalid_access_token(response) and tokens.get("refresh_token"):
                    refreshed = await client.post(
                        f"{self.settings.supabase_url}/auth/v1/token?grant_type=refresh_token",
                        headers={"apikey": self.settings.supabase_anon_key},
                        json={"refresh_token": tokens["refresh_token"]},
                    )
                    self._check_availability(refreshed)
                    if refreshed.status_code != 200:
                        self._require_login(record)
                    data = refreshed.json()
                    if (not isinstance(data, dict) or not isinstance(data.get("user"), dict)
                            or data["user"].get("id") != record["user"]["id"]
                            or not all(isinstance(data.get(key), str) and data[key]
                                       for key in ("access_token", "refresh_token"))):
                        self._require_login(record)
                    self.attach_tokens(request, data)
                    headers["Authorization"] = f"Bearer {data['access_token']}"
                    response = await client.request(method, f"{self.settings.supabase_url}/auth/v1/{path}", headers=headers, json=body)
                self._check_availability(response)
                if self._invalid_access_token(response):
                    self._require_login(record)
                if response.status_code >= 400:
                    if path.endswith("/verify"):
                        raise HTTPException(400, "認証を確認できませんでした。コードを確認して再度お試しください")
                    raise HTTPException(403, "認証情報を取得できませんでした。時間をおいて再度お試しください。解決しない場合はお問い合わせください")
                return response.json() if response.content else {}
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise HTTPException(503, "認証サービスへ接続できませんでした") from exc

    async def factors(self, request):
        data = await self._request(request, "GET", "user")
        if data.get("id") != self.record(request)["user"]["id"]:
            self._require_login(self.record(request))
        return [f for f in data.get("factors", []) if f.get("factor_type") == "totp"]

    async def enroll(self, request):
        factors = await self.factors(request)
        if any(f.get("status") == "verified" for f in factors):
            raise HTTPException(409, "登録済みの認証アプリで確認してください")
        # Only incomplete enrollment belonging to this authenticated user can
        # be restarted. Verified factors are never removed by this flow.
        for factor in factors:
            if factor.get("status") == "unverified":
                await self._request(request, "DELETE", f"factors/{UUID(factor['id'])}")
        return await self._request(request, "POST", "factors", {"factor_type": "totp", "friendly_name": "ツールバコ"})

    async def verify(self, request, factor_id: str, code: str):
        if not re.fullmatch(r"[0-9]{6}", code):
            raise HTTPException(422, "認証アプリの6桁の数字を入力してください")
        try:
            factor_id = str(UUID(factor_id))
        except ValueError as exc:
            raise HTTPException(422, "認証情報が不正です") from exc
        if not any(f.get("id") == factor_id for f in await self.factors(request)):
            raise HTTPException(403, "このアカウントの認証方法ではありません")
        challenge = await self._request(request, "POST", f"factors/{factor_id}/challenge", {})
        data = await self._request(request, "POST", f"factors/{factor_id}/verify", {"challenge_id": challenge["id"], "code": code})
        # Decode only the token received over TLS from our configured provider,
        # never a token supplied by a browser. Fail closed on a wrong subject,
        # expired token, or a response that did not actually reach AAL2.
        try:
            payload = data["access_token"].split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            expires_at = datetime.fromtimestamp(claims["exp"], timezone.utc)
            record = self.record(request)
            if claims.get("aal") != "aal2" or claims.get("sub") != record["user"]["id"] or expires_at <= datetime.now(timezone.utc):
                raise ValueError("invalid assurance")
        except (KeyError, IndexError, TypeError, ValueError, OverflowError) as exc:
            raise HTTPException(401, "2段階認証を確認できませんでした") from exc
        self.attach_tokens(request, data)
        record["mfa_verified_at"] = datetime.now(timezone.utc)
        record["mfa_token_expires_at"] = expires_at
        self.sessions.rotate(request)


def is_privileged_path(path: str, method: str) -> bool:
    segments = path.strip("/").split("/")
    if segments[0] in {"admin", "seller", "payouts"} or path == "/account/export":
        return True
    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        return path == "/settings" or (segments[0] in {"account", "security"} and not path.startswith("/security/mfa"))
    return False
