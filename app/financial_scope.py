"""Identity of one financial dataset, independent of deploy/environment labels.

This does not enable live writes. A bound dataset cannot be relabelled when a
key changes, and binding never imports or erases an existing financial history.
"""
from dataclasses import asdict, dataclass
import re
from uuid import UUID


SCOPE_OPERATION_ID = "toolbako-financial-scope-v1"
SCOPE_ENDPOINT = "internal/financial-scope-v1"


class FinancialScopeError(RuntimeError):
    def __init__(self):
        # Provider responses, identifiers and credentials must not reach logs.
        super().__init__("決済環境と取引台帳の一致を確認できません。処理を停止しました")


def key_mode(key):
    if isinstance(key, str) and key.startswith(("sk_test_", "rk_test_")):
        return "test"
    if isinstance(key, str) and key.startswith(("sk_live_", "rk_live_")):
        return "live"
    raise FinancialScopeError()


@dataclass(frozen=True)
class FinancialScope:
    mode: str
    platform_account: str
    ledger_id: str

    def __post_init__(self):
        try:
            valid = (self.mode in {"test", "live"}
                     and isinstance(self.platform_account, str)
                     and re.fullmatch(r"acct_[A-Za-z0-9]+", self.platform_account)
                     and str(UUID(self.ledger_id)) == self.ledger_id)
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise FinancialScopeError()

    @property
    def livemode(self):
        return self.mode == "live"

    def payload(self):
        return asdict(self)

    def validate_key(self, key):
        if key_mode(key) != self.mode:
            raise FinancialScopeError()

    def validate_provider(self, account, balance):
        if (not isinstance(account, dict) or account.get("object") != "account"
                or account.get("id") != self.platform_account
                or not isinstance(balance, dict) or balance.get("object") != "balance"
                or balance.get("livemode") is not self.livemode):
            raise FinancialScopeError()


def configured_scope(settings):
    account = getattr(settings, "stripe_platform_account_id", "")
    ledger_id = getattr(settings, "stripe_ledger_id", "")
    if not account and not ledger_id:
        return None
    return FinancialScope(key_mode(settings.stripe_secret_key), account, ledger_id)


def validate_binding(row, expected):
    """Row is endpoint/hash/status/response; absence is legacy sandbox only."""
    from .database import content_hash
    if row is None and expected is None:
        return
    if (row is None or expected is None or row[0] != SCOPE_ENDPOINT
            or row[1] != content_hash(expected.payload()) or row[2] != "succeeded"
            or row[3] != expected.payload()):
        raise FinancialScopeError()
