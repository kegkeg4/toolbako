"""Fresh, request-local anonymous views; never a financial/session cache."""
from dataclasses import dataclass

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

    # Reuse the exact publication/filter/sort rules without a mutable DemoStore.
    list_tools = DemoStore.list_tools
    get_public = DemoStore.get_public

    def badges_for(self, username):
        return self.registered_users.get(username) or {}
