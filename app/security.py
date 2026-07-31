from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from starlette.requests import Request


PASSWORD_SCRYPT_N = 2**14
PASSWORD_SCRYPT_R = 8
PASSWORD_SCRYPT_P = 1
_DUMMY_SALT = bytes.fromhex("746f6f6c62616b6f2d64756d6d792121")
_DUMMY_HASH = hashlib.scrypt(
    b"toolbako-invalid-password",
    salt=_DUMMY_SALT,
    n=PASSWORD_SCRYPT_N,
    r=PASSWORD_SCRYPT_R,
    p=PASSWORD_SCRYPT_P,
).hex()


def hash_password(password: str) -> tuple[str, str]:
    """Return an unpredictable salt and scrypt password verifier."""
    salt = secrets.token_bytes(16)
    password_hash = hashlib.scrypt(
        password.encode(), salt=salt, n=PASSWORD_SCRYPT_N, r=PASSWORD_SCRYPT_R, p=PASSWORD_SCRYPT_P
    ).hex()
    return salt.hex(), password_hash


def verify_password(password: str, salt_hex: str | None, expected_hash: str | None) -> bool:
    """Verify in constant work even when an account does not exist."""
    valid_record = bool(salt_hex and expected_hash)
    try:
        salt = bytes.fromhex(salt_hex or "") if valid_record else _DUMMY_SALT
        if len(salt) != 16:
            raise ValueError("invalid salt")
    except ValueError:
        salt = _DUMMY_SALT
        valid_record = False
    expected = expected_hash if valid_record else _DUMMY_HASH
    candidate = hashlib.scrypt(
        password.encode(), salt=salt, n=PASSWORD_SCRYPT_N, r=PASSWORD_SCRYPT_R, p=PASSWORD_SCRYPT_P
    ).hex()
    return bool(valid_record and hmac.compare_digest(candidate, expected or _DUMMY_HASH))


class SessionService:
    """Keep signed-cookie contents opaque while storing revocable sessions server-side."""

    def __init__(self, store: Any, max_age_seconds: int):
        self.store = store
        self.max_age_seconds = max_age_seconds

    def establish(self, request: Request, user: dict[str, Any]) -> dict[str, Any]:
        sid = secrets.token_urlsafe(32)
        now_utc = datetime.now(timezone.utc)
        with self.store._lock:
            self.store.account_sessions[sid] = {
                "user": dict(user),
                "created_at": now_utc,
                "expires_at": now_utc + timedelta(seconds=self.max_age_seconds),
            }
        request.session.clear()
        request.session["sid"] = sid
        return user

    def update(self, request: Request, user: dict[str, Any]) -> dict[str, Any]:
        sid = request.session.get("sid")
        with self.store._lock:
            if sid in self.store.account_sessions:
                self.store.account_sessions[sid]["user"] = dict(user)
                return user
        return self.establish(request, user)

    def revoke_user(self, user_id: str, keep_sid: str | None = None) -> int:
        with self.store._lock:
            targets = [
                sid
                for sid, record in self.store.account_sessions.items()
                if record.get("user", {}).get("id") == user_id and sid != keep_sid
            ]
            for sid in targets:
                self.store.account_sessions.pop(sid, None)
        return len(targets)

    def current(self, request: Request) -> dict[str, Any] | None:
        now_utc = datetime.now(timezone.utc)
        self.store.execute_due_account_deletions(now_utc)
        with self.store._lock:
            expired = [
                sid
                for sid, value in self.store.account_sessions.items()
                if value.get("expires_at") and value["expires_at"] <= now_utc
            ]
            for sid in expired:
                self.store.account_sessions.pop(sid, None)
            sid = request.session.get("sid")
            record = self.store.account_sessions.get(sid) if sid else None
            user = dict(record["user"]) if record else None
            if user:
                account = self.store.registered_users.get(user.get("username", ""))
                if not account or account.get("is_banned"):
                    self.store.account_sessions.pop(sid, None)
                    request.session.clear()
                    user = None
        # Old cookies used to contain the complete user object. They are not
        # promoted into a new server-side session because that would bypass
        # revocation, bans and password resets.
        if request.session.get("user"):
            request.session.clear()
        if user:
            user.setdefault("is_verified", False)
            user.setdefault("identity_status", "verified" if user.get("is_verified") else "not_started")
            user.setdefault("email_verified", True)
            user.setdefault("phone_verified", False)
        return user
