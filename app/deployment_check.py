"""Read-only DB/Auth preflight. This is NOT approval to launch payments.

Run in the target server's environment; never export sealed secrets locally.
No users, profiles, charges or database rows are created by this command.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from urllib.parse import urlparse

import httpx

from .config import Settings
from .database import PostgresStateStore, SCHEMA_VERSION
from .financial_scope import configured_scope, validate_binding, SCOPE_OPERATION_ID
from .supabase_api import supabase_headers

logger = logging.getLogger(__name__)

# Initial connection-only cutover is deliberately limited to the separately
# approved runtime DB. Auth and file storage remain on the original project.
# Expanding these targets needs its own reviewed cutover, not a key swap.
LIVE_DATABASE_PROJECT = "xijdnqnsuhuciqyygnkg"
LIVE_AUTH_STORAGE_URL = "https://xnjzgfaasbemffgorvnj.supabase.co"
LIVE_PLATFORM_ACCOUNT = "acct_1Ty4WnIogL39waUY"
LIVE_CONNECTION_STAGES = {"live_read_only", "live_setup_only", "live_financial"}
LIVE_DATABASE_ENDPOINTS = {
    (f"db.{LIVE_DATABASE_PROJECT}.supabase.co", "postgres"),
    ("aws-0-ap-northeast-1.pooler.supabase.com", f"postgres.{LIVE_DATABASE_PROJECT}"),
}


def _live_database_config(settings: Settings) -> dict:
    """Reject implicit/rerouted targets. Never return this dict in diagnostics."""
    from psycopg.conninfo import conninfo_to_dict
    config = conninfo_to_dict(settings.database_url)
    from .live_database_config import TARGET_HOST, RUNTIME_USER, _verified_ca_path
    role = getattr(settings, "live_database_role", "migration_admin")
    if role not in {"migration_admin", "runtime_v1"}:
        raise ValueError("connection_stage_target_unverified")
    endpoints = {(TARGET_HOST, RUNTIME_USER)} if role == "runtime_v1" else LIVE_DATABASE_ENDPOINTS
    allowed = {"host", "port", "dbname", "user", "password", "sslmode", "sslrootcert"}
    if (set(config) - allowed or any(name.upper().startswith("PG") for name in os.environ)
            or (config.get("host"), config.get("user")) not in endpoints
            or config.get("port") != "5432" or config.get("dbname") != "postgres"
            or config.get("sslmode") != "verify-full" or not config.get("password")
            or (role == "runtime_v1" and config.get("sslrootcert") != _verified_ca_path())):
        raise ValueError("connection_stage_target_unverified")
    return config


def _live_configuration_verified(settings: Settings) -> bool:
    try:
        from .live_setup_plan import STRIPE_API_VERSION, WEBHOOK_API_VERSIONS
        scope = configured_scope(settings)
        flags = (settings.stripe_live_refunds_enabled, settings.stripe_live_payouts_enabled,
                 getattr(settings, "stripe_live_checkout_enabled", False))
        setup_notifications = (
            settings.stripe_api_version == STRIPE_API_VERSION
            and settings.stripe_webhook_api_version in WEBHOOK_API_VERSIONS
            and settings.stripe_connect_accounts_v2 is False
            and all(isinstance(secret, str) and re.fullmatch(r"whsec_[A-Za-z0-9]+", secret)
                    for secret in (settings.stripe_webhook_secret, settings.stripe_connect_webhook_secret))
            and settings.stripe_webhook_secret != settings.stripe_connect_webhook_secret
        )
        return bool(
            settings.stripe_connection_mode in LIVE_CONNECTION_STAGES
            and settings.stripe_api_version == STRIPE_API_VERSION
            and settings.stripe_connect_accounts_v2 is False
            and all(type(value) is bool for value in flags)
            and (not any(flags) or getattr(settings, "live_database_role", "migration_admin") == "runtime_v1")
            and type(settings.stripe_live_seller_setup_enabled) is bool
            and ((settings.stripe_connection_mode == "live_setup_only"
                  and settings.stripe_live_seller_setup_enabled is True and setup_notifications and not any(flags))
                 or (settings.stripe_connection_mode == "live_read_only"
                     and settings.stripe_live_seller_setup_enabled is False and not any(flags)
                     and settings.stripe_webhook_api_version in WEBHOOK_API_VERSIONS)
                 or (settings.stripe_connection_mode == "live_financial" and setup_notifications))
            and re.fullmatch(r"rk_live_[A-Za-z0-9]+", settings.stripe_secret_key)
            and scope is not None and scope.mode == "live"
            and scope.platform_account == LIVE_PLATFORM_ACCOUNT
            and settings.stripe_charge_mode == "separate"
            and settings.supabase_url == LIVE_AUTH_STORAGE_URL
            and settings.storage_provider == "supabase"
            and settings.supabase_private_bucket and settings.supabase_image_bucket
            and _live_database_config(settings)
        )
    except Exception:
        # Parsing can quote credentials. No exception text, DSN, or chaining.
        return False


async def _live_database_checks(settings: Settings, backend) -> dict[str, bool]:
    """One fresh, read-only session proves schema/snapshot and exact binding.

    No request() boundary: it could initialize an absent marketplace snapshot.
    No Stripe call: this is also used by the operational health endpoint.
    """
    result = {"database_schema": False, "database_target_verified": False,
              "financial_scope_bound": False}
    try:
        mail_queue_since = getattr(settings, "live_mail_policy_since", 0)
        if type(mail_queue_since) is not int or not 0 <= mail_queue_since <= 4102444800:
            return result
        if mail_queue_since and getattr(settings, "live_database_role", "migration_admin") != "runtime_v1":
            return result
        if not _live_configuration_verified(settings) or backend.dsn != settings.database_url:
            return result
        config = _live_database_config(settings)
        scope = configured_scope(settings)
        async with asyncio.timeout(7):
            async with await backend.connect() as conn:
                if (conn.info.host != config["host"] or conn.info.user != config["user"]
                        or conn.info.port != 5432 or conn.info.dbname != "postgres"
                        or conn.pgconn.ssl_in_use is not True):
                    return result
                result["database_target_verified"] = True
                await conn.execute("set default_transaction_read_only = on")
                if getattr(settings, "live_database_role", "migration_admin") == "runtime_v1":
                    from .live_runtime_role import verify_runtime_role
                    await verify_runtime_role(conn, mail_queue_since=mail_queue_since)
                row = await (await conn.execute(
                    "select v.version,s.revision,o.endpoint,o.request_hash,o.status,o.response "
                    "from toolbako_runtime.schema_version v "
                    "left join toolbako_runtime.marketplace_state s on s.singleton "
                    "left join toolbako_runtime.stripe_operations o on o.operation_id=%s "
                    "where v.singleton", (SCOPE_OPERATION_ID,),
                )).fetchone()
                if (not row or row[0] != SCHEMA_VERSION or type(row[1]) is not int or row[1] < 0):
                    return result
                result["database_schema"] = True
                validate_binding(row[2:], scope)
                result["financial_scope_bound"] = True
    except Exception:
        # Probe failure is a false verdict, never an automatic bind/retry/write.
        pass
    return result


async def deployment_database_check(settings: Settings, backend) -> bool:
    """Operational DB health, not provider readiness or approval to sell.

    The explicitly selected live connection stage additionally checks the
    current target/binding. Stripe outages and 429s are not liveness failures.
    """
    if backend is None or not all(configuration_checks(settings).values()):
        return False
    if settings.stripe_connection_mode in LIVE_CONNECTION_STAGES:
        return all((await _live_database_checks(settings, backend)).values())
    return bool(await backend.probe())


def _auth_error_category(response: httpx.Response) -> str:
    """Classify known provider errors without logging arbitrary response data."""
    try:
        data = response.json()
    except ValueError:
        return "unclassified"
    if not isinstance(data, dict):
        return "unclassified"
    code = data.get("code")
    if code in ("PGRST301", "PGRST302", "PGRST303"):
        return "jwt_rejected"
    if code == "42501":
        return "permission_denied"
    message = data.get("message")
    if message in ("Invalid API key", "Unregistered API key"):
        return "api_key_rejected"
    if message in ("Forbidden use of secret API key in browser", "Secret API keys cannot be used in a browser"):
        return "browser_key_rejected"
    return "unclassified"


def _jwt_claim_category(message: str) -> str:
    return {"JWT expired": "expired", "JWT not yet valid": "not_yet_valid",
            "JWT issued at future": "issued_at_future", "JWT not in audience": "audience_mismatch",
            "Parsing claims failed": "claims_parse_failed",
            "The JWT 'exp' claim must be a number": "exp_type_invalid",
            "The JWT 'nbf' claim must be a number": "nbf_type_invalid",
            "The JWT 'iat' claim must be a number": "iat_type_invalid",
            "The JWT 'aud' claim must be a string or an array of strings": "aud_type_invalid"}.get(message, "unclassified")


def _response_evidence(response: httpx.Response) -> dict:
    """Allowlisted edge metadata only; never dump headers or response content.

    CF-Ray is a validated request-correlation identifier, not proof that two
    requests reached the same connection, gateway worker or PostgREST instance.
    Cache classifications describe the observed header, not its correctness.
    """
    cache = response.headers.get("cf-cache-status", "")
    ray = response.headers.get("cf-ray", "")
    age = response.headers.get("age", "")
    version = response.extensions.get("http_version", b"")
    proxy = re.fullmatch(r"PostgREST;\s*error=(PGRST30[0-3]|42501)", response.headers.get("proxy-status", ""))
    challenge = re.search(r'(?:^|,\s*)error_description="([^"\r\n]*)"', response.headers.get("www-authenticate", ""))
    request_id = response.headers.get("sb-request-id", "")
    return {
        "http_version": {b"HTTP/1.0": "HTTP/1.0", b"HTTP/1.1": "HTTP/1.1",
                         b"HTTP/2": "HTTP/2", b"HTTP/3": "HTTP/3"}.get(version, "unknown")
                        if isinstance(version, bytes) else "unknown",
        "cache_status": cache if cache in {"HIT", "MISS", "DYNAMIC", "BYPASS", "EXPIRED",
                                           "STALE", "REVALIDATED", "UPDATING"}
                        else ("missing" if not cache else "unknown"),
        "cf_ray": ray if re.fullmatch(r"[0-9a-fA-F]{16,32}-[A-Z]{3}", ray) else None,
        "sb_request_id": request_id if re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", request_id) else None,
        "postgrest_error": proxy.group(1) if proxy else None,
        "jwt_claim_category": _jwt_claim_category(challenge.group(1)) if challenge else "unclassified",
        "age_seconds": int(age) if re.fullmatch(r"[0-9]{1,10}", age) and int(age) <= 2147483647 else None,
        "set_cookie_present": "set-cookie" in response.headers,
        "www_authenticate_present": "www-authenticate" in response.headers,
        "gateway_metadata_present": any(name in response.headers for name in (
            "sb-gateway-mode", "sb-gateway-version", "x-kong-proxy-latency", "x-kong-upstream-latency")),
    }


def _request_evidence(request: httpx.Request, expected_headers: dict) -> dict:
    """Describe actual outgoing auth/browser shape without values or digests."""
    key = request.headers.get("apikey", "")
    user_agent = request.headers.get("user-agent", "")
    expected = httpx.Headers(expected_headers)
    try:
        body_empty = request.content == b""
    except httpx.RequestNotRead:
        body_empty = False
    return {
        "api_key_kind": ("opaque_secret" if key.startswith("sb_secret_") else
                         "opaque_publishable" if key.startswith("sb_publishable_") else
                         "legacy_or_other" if key else "missing"),
        "configured_auth_matches": all(request.headers.get_list(name) == expected.get_list(name)
                                       for name in ("apikey", "authorization")),
        "user_agent_kind": ("python_httpx" if re.fullmatch(r"python-httpx/[0-9]+\.[0-9]+\.[0-9]+", user_agent)
                            else "browser_like" if "Mozilla/" in user_agent else "other" if user_agent else "missing"),
        "origin_present": "origin" in request.headers,
        "fetch_metadata_present": any(name.startswith("sec-fetch-") for name in request.headers),
        "cookie_present": "cookie" in request.headers,
        "body_empty": body_empty,
    }


def _request_comparison(head: httpx.Request, get: httpx.Request) -> dict:
    # Compare entirely in memory. Never emit auth values, URLs, query strings,
    # Cookie values or digests of those values, even if they differ.
    try:
        body_matches = head.content == get.content
    except httpx.RequestNotRead:
        body_matches = False
    return {
        "diagnostic_request_auth_matches_head": all(head.headers.get_list(name) == get.headers.get_list(name)
                                                    for name in ("apikey", "authorization")),
        "diagnostic_request_headers_match_head": sorted(head.headers.multi_items()) == sorted(get.headers.multi_items()),
        "diagnostic_request_url_matches_head": head.url == get.url,
        "diagnostic_request_body_matches_head": body_matches,
    }


async def _diagnose_head_auth(client, url, headers, params, *, head_response):
    """A single zero-row GET explains a failed HEAD; it never changes its verdict.

    Keep credentials on the same configured endpoint, forbid redirects, and
    discard all response content except a small fixed classification vocabulary.
    """
    evidence = {"head_response": _response_evidence(head_response),
                "head_request": _request_evidence(head_response.request, headers)}
    started = time.monotonic()
    try:
        response = await client.get(url, headers=headers, params=params)
        return {**evidence, "diagnostic_get_status": response.status_code,
                "diagnostic_auth_category": (_auth_error_category(response)
                                             if response.status_code in {401, 403} else "not_auth_error"),
                "diagnostic_response": _response_evidence(response),
                "diagnostic_request": _request_evidence(response.request, headers),
                **_request_comparison(head_response.request, response.request),
                "diagnostic_elapsed_ms": round((time.monotonic() - started) * 1000)}
    except httpx.HTTPError as error:
        return {**evidence, "diagnostic_failure": _http_failure_code(error),
                "diagnostic_elapsed_ms": round((time.monotonic() - started) * 1000)}


def _http_failure_code(error: httpx.HTTPError) -> str:
    # Exception strings can contain credentials, query strings or provider data.
    for kind, code in ((httpx.ConnectTimeout, "connect_timeout"),
                       (httpx.ReadTimeout, "read_timeout"),
                       (httpx.WriteTimeout, "write_timeout"),
                       (httpx.PoolTimeout, "pool_timeout"),
                       (httpx.ConnectError, "connect_error"),
                       (httpx.RemoteProtocolError, "remote_protocol_error")):
        if isinstance(error, kind):
            return code
    return "http_error"


def database_configuration_diagnostics(settings: Settings) -> dict[str, bool]:
    """Presence/configuration only; never return a DSN, user, host or password.

    These observations do not replace the connection probe and are not launch
    checks: a password can be supplied by another libpq mechanism. Keep them
    separate from the all-boolean, fail-closed preflight verdict.
    """
    results = {
        "uri_parsed": False,
        "tls_configured": False,
        "transaction_pooler_port": False,
        "uri_password_present": False,
        "environment_password_present": bool(os.getenv("PGPASSWORD", "")),
        "target_matches_supabase": False,
    }
    try:
        from psycopg.conninfo import conninfo_to_dict
        config = conninfo_to_dict(settings.database_url)
        results["uri_parsed"] = bool(settings.database_url)
        results["tls_configured"] = config.get("sslmode") in {"require", "verify-ca", "verify-full"}
        results["transaction_pooler_port"] = config.get("port") == "6543"
        results["uri_password_present"] = bool(config.get("password"))
        api_host = urlparse(settings.supabase_url).hostname or ""
        # Only recognize the hosted project's exact direct/session-pool target.
        suffix = ".supabase.co"
        project = api_host[:-len(suffix)] if api_host.endswith(suffix) else ""
        host = config.get("host", "")
        results["target_matches_supabase"] = bool(project and "." not in project) and (
            host == f"db.{project}.supabase.co" or (
                host.endswith(".pooler.supabase.com")
                and config.get("user") == f"postgres.{project}"
            )
        )
    except Exception:
        # Parsing errors may quote the URI/password. Do not print or attach them.
        pass
    return results


def configuration_checks(settings: Settings) -> dict[str, bool]:
    endpoint = urlparse(settings.supabase_url)
    site = urlparse(settings.site_base_url)
    results = {
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
        # Report presence only, never a value/length/prefix. Separate flags let
        # operators diagnose injection without reading or replacing sealed keys.
        "supabase_public_key_present": bool(settings.supabase_anon_key),
        "supabase_server_key_present": bool(settings.supabase_service_role_key),
        "supabase_key_roles": not settings.supabase_anon_key.startswith("sb_secret_")
        and not settings.supabase_service_role_key.startswith("sb_publishable_"),
        "database_configured": bool(settings.database_url),
        # Live money movement remains a separately reviewed, code-blocked step.
        "no_live_stripe_key": not settings.stripe_secret_key.startswith(("sk_live_", "rk_live_")),
    }
    if settings.stripe_connection_mode in LIVE_CONNECTION_STAGES:
        # Do not mislabel a live key as absent. This static configuration flag
        # alone is never used as connection or scope evidence.
        del results["no_live_stripe_key"]
        results[settings.stripe_connection_mode + "_configuration"] = _live_configuration_verified(settings)
    else:
        results["stripe_connection_mode"] = settings.stripe_connection_mode == "sandbox"
        results["live_seller_setup_disabled"] = settings.stripe_live_seller_setup_enabled is False
        results["live_financial_operations_disabled"] = (
            settings.stripe_live_refunds_enabled is False and settings.stripe_live_payouts_enabled is False)
    return results


async def connection_checks(settings: Settings) -> dict[str, bool]:
    results = configuration_checks(settings)
    results.update(database_schema=False, auth_api=False, profile_api=False,
                   badge_api=False, anonymous_profiles_denied=False)
    live_stage = settings.stripe_connection_mode in LIVE_CONNECTION_STAGES
    if live_stage:
        results.update(database_target_verified=False, financial_scope_bound=False,
                       live_stripe_connection_verified=False)
    if not all(configuration_checks(settings).values()):
        return results

    # Apply production TLS requirements in staging too. probe is read-only and
    # checks the required runtime schema version; it never auto-migrates/seeds.
    backend = PostgresStateStore(settings.database_url, production=True)
    if live_stage:
        results.update(await _live_database_checks(settings, backend))
        if not all(results[name] for name in ("database_schema", "database_target_verified", "financial_scope_bound")):
            return results
        from .stripe_live_connection_check import inspect_connection
        try:
            # Provider evidence is pre-deploy only, never on every health poll.
            # Uses the ACTIVE key: pending-key success cannot prove this runtime.
            async with asyncio.timeout(25):
                proof = await inspect_connection(settings.stripe_secret_key, settings.stripe_platform_account_id)
            results["live_stripe_connection_verified"] = (
                proof.get("connection_verified") is True and proof.get("platform_matches") is True
                and proof.get("mode") == "live" and proof.get("read_only") is True
                and proof.get("payments_activated") is False)
        except Exception:
            pass
        if not results["live_stripe_connection_verified"]:
            return results
    else:
        results["database_schema"] = await backend.probe()
    anon_headers = supabase_headers(settings.supabase_anon_key)
    server_headers = supabase_headers(settings.supabase_service_role_key)
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
            started = time.monotonic()
            status = None
            failure = None
            try:
                response = await client.request(method, settings.supabase_url + path,
                                                headers=headers, params=params)
                status = response.status_code
                if status not in accepted:
                    failure = "unexpected_status"
            except httpx.HTTPError as error:
                failure = _http_failure_code(error)
            if failure:
                elapsed_ms = round((time.monotonic() - started) * 1000)
                diagnostic = {}
                if name in {"profile_api", "badge_api"} and method == "HEAD" and status in {401, 403}:
                    diagnostic = await _diagnose_head_auth(
                        client, settings.supabase_url + path, headers, params, head_response=response)
                # Fixed categories, booleans and validated correlation IDs only. Never log the
                # request, headers, response body, exception text or traceback.
                logger.warning("connection_preflight_failure %s", json.dumps({
                    "check": name, "failure": failure, "status_code": status,
                    "elapsed_ms": elapsed_ms, **diagnostic,
                }))
            return name, failure is None
        results.update(await asyncio.gather(*(check(item) for item in checks)))
    if settings.storage_provider == "supabase":
        from .object_storage import SupabaseObjectStorage, ObjectStorageError
        try:
            results.update(await SupabaseObjectStorage(settings).checks())
        except ObjectStorageError:
            results.update(private_delivery_bucket=False, public_image_bucket=False)
    return results


def main() -> int:
    # No provider response, exception text, URL or credential is printed.
    diagnostics = None
    try:
        settings = Settings()
        diagnostics = database_configuration_diagnostics(settings)
        results = asyncio.run(connection_checks(settings))
    except Exception:
        print(json.dumps({"connection_preflight_passed": False,
                          "error": "Connection check failed; inspect server configuration securely.",
                          "database_configuration": diagnostics}))
        return 1
    passed = all(results.values())
    print(json.dumps({"connection_preflight_passed": passed, "checks": results,
                      "database_configuration": diagnostics,
                      "live_payments_enabled": False,
                      "notice": "Read-only connection checks only; not launch or payment approval."}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
