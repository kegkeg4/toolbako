"""Strict, non-sensitive projection of an already mapped Connect account.

This proves neither platform/mode binding nor seller identity. Those checks
belong to the scoped provider and the authenticated user's stored mapping.
"""


def account_status(remote, expected_account_id):
    if (not isinstance(remote, dict)
            or not isinstance(expected_account_id, str)
            or not expected_account_id.startswith("acct_")
            or remote.get("object") != "account"
            or remote.get("id") != expected_account_id):
        raise RuntimeError("決済受取設定の口座を確認できませんでした")
    fields = ("charges_enabled", "payouts_enabled", "details_submitted")
    if any(type(remote.get(name)) is not bool for name in fields):
        raise RuntimeError("決済受取設定の状態を確認できませんでした")
    return {name: remote[name] for name in fields}
