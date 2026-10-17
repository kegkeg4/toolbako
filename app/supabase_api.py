"""Headers for Supabase's opaque API keys and legacy JWT API keys."""


def supabase_headers(api_key: str, *, access_token: str | None = None) -> dict[str, str]:
    headers = {"apikey": api_key}
    if access_token is not None:
        # A user's session JWT must not be replaced with a server API key.
        if not access_token or access_token.startswith(("sb_secret_", "sb_publishable_")):
            raise ValueError("A user session token is required")
        headers["Authorization"] = f"Bearer {access_token}"
    elif not api_key.startswith(("sb_secret_", "sb_publishable_")):
        # Legacy keys are JWTs. New opaque keys belong only in apikey.
        headers["Authorization"] = f"Bearer {api_key}"
    return headers
