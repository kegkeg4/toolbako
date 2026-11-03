"""Offline preparation of the designated live mail role, never activation.

No connection, credential lookup, send, environment mutation or CLI exists here.
The returned SQL is a review artifact, not authorization. Its executor must first
verify the designated TLS endpoint, current ledger and separately approved mail
access, then read back effective privileges (including PUBLIC and memberships).

Applying these policies without the corresponding strict Web/mail verifier and
service wiring is unsupported. The Web verifier rejects additional policies
without the explicit matching mail cutoff. Do not bypass that rejection or replace the existing
service_monitor command with an unrestricted --all-pending sender.
"""
from dataclasses import dataclass
import hashlib
import json
import re

from .financial_scope import FinancialScope
from .live_database_config import TARGET_PROJECT, _build_database_config


MAIL_ROLE = "toolbako_mail_live_v1"
MAIL_GROUP = "toolbako_runtime_mail_live"
MAIL_USER = MAIL_ROLE + "." + TARGET_PROJECT
SCOPE = FinancialScope("live", "acct_1Ty4WnIogL39waUY", "667c8593-cee4-4bec-94ed-bbbd95600867")
PERMISSIONS = {
    "schema_version": {"SELECT": "singleton version"},
    # Scope-binding proof only: the RLS below hides all real provider intents.
    "stripe_operations": {"SELECT": "operation_id endpoint request_hash status response"},
    "email_outbox": {
        "SELECT": "id recipient sender subject body status attempts first_attempt_at available_at lease_until claim_token created_at",
        "UPDATE": "status attempts first_attempt_at available_at lease_until claim_token sent_at",
    },
}


class LiveMailPreparationError(RuntimeError):
    def __init__(self):
        super().__init__("live_mail_preparation_invalid")


def _require(ok):
    if not ok:
        raise LiveMailPreparationError()


def build_mail_database_config(password, *, environment=None):
    """Fixed, separate mail identity with the existing pinned public CA.

    The returned holder is redacted and its for_connection() boundary has the
    same PG*/CA checks as the Web configuration. A future worker must receive
    its own sealed password, never the Web or migration-admin password.
    """
    return _build_database_config(password, environment=environment, user=MAIL_USER)


@dataclass(frozen=True)
class PreparedMailAccess:
    """A proposed SQL/config contract; approval_digest is a trace, not a grant.

    queue_since must match the separately reviewed Web notification cutoff.
    Neither an arbitrary hash nor construction of this object proves consent.
    This cannot authorize sending preexisting queued messages or a canary.
    """
    queue_since: int
    approval_digest: str

    def __post_init__(self):
        # Exact integer seconds only; prevent bool, SQL injection and float
        # rounding. This bound is an input contract, not a current-time claim.
        _require(type(self.queue_since) is int and 1 <= self.queue_since <= 4102444800)
        _require(type(self.approval_digest) is str
                 and re.fullmatch(r"[0-9a-f]{64}", self.approval_digest) is not None)

    def policy_contract(self):
        """Four exact policies for a later verifier integration.

        Tuple: PostgreSQL command, permissive, roles, USING SQL, CHECK SQL.
        The verifier must compare parsed/deparsed expressions, not trust the
        policy name/count alone. Missing/extra roles or broader predicates fail.
        The Web verifier may use this exact contract only with its separately
        configured matching mail cutoff; the default remains Web policies only.
        """
        future = f"created_at >= pg_catalog.to_timestamp({self.queue_since}::double precision)"
        binding = ("operation_id = 'toolbako-financial-scope-v1' AND "
                   "endpoint = 'internal/financial-scope-v1'")
        return {
            "schema_version": {"toolbako_mail_live_schema": ("r", True, [MAIL_GROUP], "singleton AND version = 2", None)},
            "stripe_operations": {"toolbako_mail_live_binding": ("r", True, [MAIL_GROUP], binding, None)},
            "email_outbox": {
                "toolbako_mail_live_select": ("r", True, [MAIL_GROUP], future, None),
                "toolbako_mail_live_progress": ("w", True, [MAIL_GROUP],
                    future + " AND status IN ('pending', 'sending')",
                    future + " AND status IN ('pending', 'sending', 'sent', 'review')"),
            },
        }

    def roles_sql(self):
        """One-time NOLOGIN/PASSWORD NULL preparation, never credential setup."""
        return f"""-- MAIL ACCESS IS A SEPARATE APPROVAL; PREPARATION ONLY.
begin;
set local lock_timeout='3s';
set local statement_timeout='10s';
create role {MAIL_GROUP} nologin noinherit nosuperuser nocreatedb nocreaterole noreplication nobypassrls;
create role {MAIL_ROLE} nologin inherit nosuperuser nocreatedb nocreaterole noreplication nobypassrls password null;
grant {MAIL_GROUP} to {MAIL_ROLE};
commit;
"""

    def grants_sql(self):
        """Reviewed ledger/cutoff-specific SQL; it never changes existing mail.

        This has a ledger guard but cannot attest a network endpoint. A future
        operator executor must enforce the exact configured session-pooler/TLS
        identity before running it, and refuse partial replay. Policies use a
        literal cutoff, not caller-controlled PostgreSQL session settings.
        """
        # All interpolations are fixed source constants or validated integers.
        payload = json.dumps(SCOPE.payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        binding_hash = hashlib.sha256(payload.encode()).hexdigest()
        result = f"""-- review sha256: {self.approval_digest}
-- No login/password, message DML, existing-role adoption or PUBLIC changes.
begin;
set local lock_timeout='3s';
set local statement_timeout='10s';
set local row_security=off;
do $mail$ begin
  if current_user <> 'postgres' or session_user <> 'postgres'
     or (select count(*) from toolbako_runtime.schema_version where singleton and version=2) <> 1
     or (select count(*) from toolbako_runtime.marketplace_state where singleton) <> 1
     or (select count(*) from toolbako_runtime.stripe_operations
          where operation_id='toolbako-financial-scope-v1' and endpoint='internal/financial-scope-v1'
            and request_hash='{binding_hash}' and status='succeeded'
            and response='{payload}'::jsonb) <> 1
     or (select count(*) from pg_catalog.pg_roles where rolname in ('{MAIL_ROLE}','{MAIL_GROUP}')
           and not rolcanlogin and not rolsuper and not rolcreatedb and not rolcreaterole
           and not rolreplication and not rolbypassrls) <> 2
  then raise exception 'live_mail_grants_precondition_failed'; end if;
  execute format('grant connect on database %I to {MAIL_GROUP}', current_database());
end $mail$;
grant usage on schema toolbako_runtime to {MAIL_GROUP};
"""
        for table, grants in PERMISSIONS.items():
            entries = ", ".join(f"{verb.lower()}({','.join(columns.split())})" for verb, columns in grants.items())
            result += f"grant {entries} on toolbako_runtime.{table} to {MAIL_GROUP};\n"
        for table, policies in self.policy_contract().items():
            for name, (command, _, _, using, check) in policies.items():
                verb = {"r": "select", "w": "update"}[command]
                result += f"create policy {name} on toolbako_runtime.{table} for {verb} to {MAIL_GROUP}"
                if using is not None:
                    result += f" using ({using})"
                if check is not None:
                    result += f" with check ({check})"
                result += ";\n"
        return result + "commit;\n"

    def manifest(self):
        """Only nonsecret configuration/hashes; never a delivery permission."""
        return {
            "version": 1, "preparation_only": True, "delivery_enabled": False,
            "project": TARGET_PROJECT, "role": MAIL_ROLE, "group": MAIL_GROUP,
            "scope": SCOPE.payload(), "queue_since": self.queue_since,
            "approval_digest": self.approval_digest,
            "roles_sql_sha256": hashlib.sha256(self.roles_sql().encode()).hexdigest(),
            "grants_sql_sha256": hashlib.sha256(self.grants_sql().encode()).hexdigest(),
            "expected_additional_policy_count": 4,
        }
