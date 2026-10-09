"""Read-only DB/Auth preflight. This is NOT approval to launch payments.

Run in the target server's environment; never export sealed secrets locally.
No users, profiles, charges or database rows are created by this command.
"""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urlparse

import httpx

from .config import Settings
from .database import PostgresStateStore


def configuration_checks(settings: Settings) -> dict[str, bool]:
    endpoint = urlparse(settings.supabase_url)
    site = urlparse(settings.site_base_url)
    return {
        "server_environment": settings.environment in {"staging", "production"},
        "demo_disabled": not settings.demo_mode,
        "https_site": site.scheme == "https" and bool(site.hostname)
        and not site.username and not site.password and not site.query and not site.fragment,
        "host_allowed": bool(site.hostname) and site.hostname in settings.allowed_hosts
        and "*" not in settings.allowed_hosts,
        "session_secret": len(settings.session_secret) >= 32
        and settings.session_secret not in {"dev-only-secret", "change-me-in-production"},
        "supabase_endpoint": endpoint.scheme == "https" and bool(endpoint.hostname)
        and not endpoint.username and not endpoint.password and not endpoint.query
        and not endpoint.fragment and endpoint.path in {"", "/"},
        "supabase_keys_present": bool(settings.supabase_anon_key and settings.supabase_service_role_key),
        "database_configured": bool(settings.database_url),
        # Live money movement remains a separately reviewed, code-blocked step.
        "no_live_stripe_key": not settings.stripe_secret_key.startswith(("sk_live_", "rk_live_")),
    }


async def connection_checks(settings: Settings) -> dict[str, bool]:
    results = configuration_checks(settings)
    results.update(database_schema=False, auth_api=False, profile_api=False,
                   badge_api=False, anonymous_profiles_denied=False)
    if not all(configuration_checks(settings).values()):
        return results

    # Apply production TLS requirements in staging too. probe is read-only and
    # checks the required runtime schema version; it never auto-migrates/seeds.
    backend = PostgresStateStore(settings.database_url, production=True)
    results["database_schema"] = await backend.probe()
    anon_headers = {"apikey": settings.supabase_anon_key,
                    "Authorization": f"Bearer {settings.supabase_anon_key}"}
    server_headers = {"apikey": settings.supabase_service_role_key,
                      "Authorization": f"Bearer {settings.supabase_service_role_key}"}
    profile_columns = (
        "id,username,display_name,avatar_url,bio,headline,skills,experience,portfolio,"
        "availability,response_time,pricing_note,x_url,website_url,is_banned,"
        "identity_status,creator_badges(*)"
    )
    badge_columns = ("profile_id,is_founding_member,founding_member_since,"
                     "is_certified_creator,certified_creator_since,updated_at")
    # HEAD checks columns/relationships/privileges without returning profile data.
    checks = [
        ("auth_api", "GET", "/auth/v1/settings", anon_headers, None, {200}),
        ("profile_api", "HEAD", "/rest/v1/profiles", server_headers,
         {"select": profile_columns, "limit": "0"}, {200}),
        ("badge_api", "HEAD", "/rest/v1/creator_badges", server_headers,
         {"select": badge_columns, "limit": "0"}, {200}),
        ("anonymous_profiles_denied", "HEAD", "/rest/v1/profiles", anon_headers,
         {"select": "id", "limit": "0"}, {401, 403}),
    ]
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        async def check(item):
            name, method, path, headers, params, accepted = item
            try:
                response = await client.request(method, settings.supabase_url + path,
                                                headers=headers, params=params)
                return name, response.status_code in accepted
            except httpx.HTTPError:
                return name, False
        results.update(await asyncio.gather(*(check(item) for item in checks)))
    return results


def main() -> int:
    # No provider response, exception text, URL or credential is printed.
    try:
        results = asyncio.run(connection_checks(Settings()))
    except Exception:
        print(json.dumps({"connection_preflight_passed": False,
                          "error": "Connection check failed; inspect server configuration securely."}))
        return 1
    passed = all(results.values())
    print(json.dumps({"connection_preflight_passed": passed, "checks": results,
                      "live_payments_enabled": False,
                      "notice": "Read-only DB/Auth checks only; not launch approval."}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
