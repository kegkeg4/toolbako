"""Fresh, request-local anonymous views; never a financial/session cache."""
from copy import deepcopy
from dataclasses import dataclass, field

from .data import DemoStore


CATALOG_PATHS = frozenset({"/", "/tools"})
PUBLIC_READ_PATHS = CATALOG_PATHS | frozenset({
    "/login", "/support", "/about", "/terms", "/privacy", "/tokushoho", "/match",
})


def anonymous_read(request):
    # Any credential/cookie, even malformed/empty, uses the original boundary.
    # Explicit routes only: GET alone does not prove a handler is read-only.
    method_allowed = request.method == "GET" or (request.method == "HEAD" and request.url.path == "/login")
    return (method_allowed and request.url.path in PUBLIC_READ_PATHS
            and "cookie" not in request.headers and "authorization" not in request.headers)


@dataclass
class PublicView:
    tools: list
    registered_users: dict
    audit_logs: list = field(default_factory=list, repr=False)
    claim_policy: object = field(default=None, repr=False)
    _claim_registry_cache: object = field(default=None, init=False, repr=False, compare=False)

    # Reuse the exact publication/filter/sort rules without a mutable DemoStore.
    list_tools = DemoStore.list_tools
    get_public = DemoStore.get_public

    def claim_registry(self, key, build):
        """Reuse validated evidence only inside this fresh database read view.

        Keep an independent snapshot: changing a review in place, changing the
        policy/scope/reviewers, or creating another request invalidates reuse.
        Account flags and the returned badge dict are never cached here.
        """
        cached = self._claim_registry_cache
        if cached is not None and cached[0] == key and cached[1] == self.audit_logs:
            return cached[2]
        evidence = deepcopy(self.audit_logs)
        registry = build(evidence)
        self._claim_registry_cache = (key, evidence, registry)
        return registry

    def badges_for(self, username):
        account = self.registered_users.get(username) or {}
        if self.claim_policy:
            return self.claim_policy.badges(account, self)
        return account
