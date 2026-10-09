from __future__ import annotations

import json
import os
import sqlite3
import stat
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any


STATE_FIELDS = (
    "tools","likes","favorite_folders","favorite_assignments","reports","orders","conversations","transfer_inquiries","transfer_nda_acceptances","reviews","buyer_reviews","notifications","notification_preferences","requests","blocks","payouts",
    "update_followers","creator_follows","reopen_waiters","subscriptions","identity_applications","nda_signatures","nda_records","coupons","coupon_redemptions","security_settings",
    "support_cases","audit_logs","account_deletions","processed_webhook_events","legal_consents","registered_users","connected_accounts","account_sessions",
)


def encode_value(value: Any) -> Any:
    if isinstance(value, datetime): return {"__type__":"datetime","value":value.isoformat()}
    if isinstance(value, set): return {"__type__":"set","value":[encode_value(x) for x in value]}
    if isinstance(value, tuple): return {"__type__":"tuple","value":[encode_value(x) for x in value]}
    if isinstance(value, list): return [encode_value(x) for x in value]
    if isinstance(value, dict): return {str(k):encode_value(v) for k,v in value.items()}
    return value


def decode_value(value: Any) -> Any:
    if isinstance(value, list): return [decode_value(x) for x in value]
    if isinstance(value, dict):
        kind = value.get("__type__")
        if kind == "datetime": return datetime.fromisoformat(value["value"])
        if kind == "set": return set(decode_value(x) for x in value["value"])
        if kind == "tuple": return tuple(decode_value(x) for x in value["value"])
        return {k:decode_value(v) for k,v in value.items()}
    return value


class SQLiteStateStore:
    """Single-instance demo persistence, NOT a production financial repository."""
    def __init__(self, path: str):
        self.path = Path(path).expanduser() if path else None
        self._lock = Lock()
        if self.path:
            # Do not chmod an existing/shared parent (e.g. /tmp or the user's
            # project). Only newly created storage directories are private.
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("SQLite demo storage must be a regular, unlinked file")
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            with self._connect() as db:
                db.execute("pragma journal_mode=WAL")
                db.execute("create table if not exists app_state (id integer primary key check(id=1), payload text not null, updated_at text not null)")

    @property
    def enabled(self) -> bool: return self.path is not None

    def _connect(self):
        if not self.path: raise RuntimeError("state store disabled")
        return sqlite3.connect(self.path, timeout=10)

    def restore(self, store: Any) -> bool:
        if not self.path: return False
        with self._lock, self._connect() as db:
            row = db.execute("select payload from app_state where id=1").fetchone()
        if not row: return False
        state = decode_value(json.loads(row[0]))
        with store._lock:
            for field in STATE_FIELDS:
                if field in state: setattr(store, field, state[field])
        return True

    def save(self, store: Any) -> None:
        if not self.path: return
        with store._lock:
            state = {field:getattr(store,field) for field in STATE_FIELDS}
            payload = json.dumps(encode_value(state), ensure_ascii=False, separators=(",",":"))
        updated_at = datetime.now().astimezone().isoformat()
        with self._lock, self._connect() as db:
            db.execute("begin immediate")
            db.execute("insert into app_state(id,payload,updated_at) values(1,?,?) on conflict(id) do update set payload=excluded.payload,updated_at=excluded.updated_at",(payload,updated_at))
            db.commit()

    def status(self) -> dict[str, Any]:
        return {"enabled":self.enabled,"path":str(self.path) if self.path else None,"exists":bool(self.path and self.path.exists())}
