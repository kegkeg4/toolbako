from dataclasses import dataclass
import os

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    environment: str = os.getenv("APP_ENV", "development").lower()
    supabase_url: str = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_anon_key: str = os.getenv("SUPABASE_ANON_KEY", "")
    supabase_service_role_key: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    site_base_url: str = os.getenv("SITE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    session_secret: str = os.getenv("SESSION_SECRET", "dev-only-secret")
    admin_user_ids: tuple[str, ...] = tuple(x.strip() for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip())
    # Demo access must always fail closed in production, even after a bad env rollout.
    demo_mode: bool = os.getenv("APP_ENV", "development").lower() != "production" and os.getenv("DEMO_MODE", "true").lower() in {"1", "true", "yes"}
    allowed_hosts: tuple[str, ...] = tuple(x.strip() for x in os.getenv("ALLOWED_HOSTS", "127.0.0.1,localhost,testserver").split(",") if x.strip())
    session_max_age: int = int(os.getenv("SESSION_MAX_AGE", "604800"))
    asset_version: str = os.getenv("ASSET_VERSION", "20261009-release30")
    # Delivery files support 50 MB; leave room for multipart metadata.
    max_request_bytes: int = int(os.getenv("MAX_REQUEST_BYTES", "52500000"))
    rate_limit_per_minute: int = int(os.getenv("RATE_LIMIT_PER_MINUTE", "120"))
    auth_rate_limit_per_minute: int = int(os.getenv("AUTH_RATE_LIMIT_PER_MINUTE", "10"))
    redis_url: str = os.getenv("REDIS_URL", "").strip()
    # Production always enforces step-up; this flag also enables it in staging.
    privileged_mfa_required: bool = os.getenv("PRIVILEGED_MFA_REQUIRED", "false").lower() in {"1", "true", "yes"}
    trust_proxy_headers: bool = os.getenv("TRUST_PROXY_HEADERS", "false").lower() in {"1", "true", "yes"}
    trusted_proxy_cidrs: tuple[str, ...] = tuple(x.strip() for x in os.getenv("TRUSTED_PROXY_CIDRS", "").split(",") if x.strip())
    stripe_secret_key: str = os.getenv("STRIPE_SECRET_KEY", "")
    stripe_webhook_secret: str = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    stripe_connect_webhook_secret: str = os.getenv("STRIPE_CONNECT_WEBHOOK_SECRET", "")
    stripe_connect_client_id: str = os.getenv("STRIPE_CONNECT_CLIENT_ID", "")
    # Keep destination charges as the backwards-compatible default. Set this to
    # ``separate`` once the production payout worker is enabled so ONE can hold
    # funds until acceptance and then create an explicit Connect transfer.
    stripe_charge_mode: str = os.getenv("STRIPE_CHARGE_MODE", "destination").lower()
    stripe_api_version: str = os.getenv("STRIPE_API_VERSION", "2026-02-25.clover")
    stripe_connect_accounts_v2: bool = os.getenv("STRIPE_CONNECT_ACCOUNTS_V2", "false").lower() in {"1", "true", "yes"}
    email_api_key: str = os.getenv("EMAIL_API_KEY", "")
    email_from: str = os.getenv("EMAIL_FROM", "")
    support_email: str = os.getenv("SUPPORT_EMAIL", "")
    sentry_dsn: str = os.getenv("SENTRY_DSN", "")
    # Only the operating entity is decided. Keep unverified legal contact details
    # empty so production readiness fails closed instead of publishing stale data.
    legal_business_name: str = os.getenv("LEGAL_BUSINESS_NAME", "合同会社ONE")
    legal_representative: str = os.getenv("LEGAL_REPRESENTATIVE", "")
    legal_address: str = os.getenv("LEGAL_ADDRESS", "")
    legal_phone: str = os.getenv("LEGAL_PHONE", "")
    legal_website: str = os.getenv("LEGAL_WEBSITE", "").rstrip("/")
    legal_invoice_number: str = os.getenv("LEGAL_INVOICE_NUMBER", "")
    # Platform take rate. Keep these in configuration so checkout, seller views,
    # finance admin and Connect application fees cannot drift apart.
    platform_fee_rate: float = float(os.getenv("PLATFORM_FEE_RATE", "0.15"))
    custom_fee_rate: float = float(os.getenv("CUSTOM_FEE_RATE", "0.18"))
    payout_schedule_label: str = os.getenv("PAYOUT_SCHEDULE_LABEL", "申請した週の翌週木曜日（金融機関・Stripeの処理状況により前後）")
    # Demo payout rules mirror the familiar Coconala flow. Production payouts
    # are ultimately governed by Stripe Connect and the published terms.
    payout_minimum: int = int(os.getenv("PAYOUT_MINIMUM", "161"))
    payout_fee: int = int(os.getenv("PAYOUT_FEE", "160"))
    payout_fee_free_threshold: int = int(os.getenv("PAYOUT_FEE_FREE_THRESHOLD", "3000"))
    payout_auto_days: int = int(os.getenv("PAYOUT_AUTO_DAYS", "120"))
    payout_weekday_label: str = os.getenv("PAYOUT_WEEKDAY_LABEL", "申請した週の翌週木曜日")
    state_db_path: str = os.getenv("STATE_DB_PATH", "")
    # Dedicated Postgres database or Supabase *session* pooler, never port 6543.
    # Runtime storage is private and separate from the public PostgREST schema.
    database_url: str = os.getenv("DATABASE_URL", "").strip()
    private_storage_path: str = os.getenv("PRIVATE_STORAGE_PATH", "private_uploads")
    clamav_host: str = os.getenv("CLAMAV_HOST", "")
    clamav_port: int = int(os.getenv("CLAMAV_PORT", "3310"))

    def __post_init__(self) -> None:
        # Fail closed even when Settings is constructed directly in a test,
        # management command or future dependency-injection container.
        if (self.environment == "production" or self.database_url) and self.demo_mode:
            object.__setattr__(self, "demo_mode", False)
        if self.stripe_charge_mode not in {"destination", "separate"}:
            object.__setattr__(self, "stripe_charge_mode", "destination")
        if not 0 <= self.platform_fee_rate <= 1:
            object.__setattr__(self, "platform_fee_rate", 0.15)
        if not 0 <= self.custom_fee_rate <= 1:
            object.__setattr__(self, "custom_fee_rate", 0.18)

    @property
    def supabase_ready(self) -> bool:
        return bool(self.supabase_url and self.supabase_anon_key)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_deployed(self) -> bool:
        """Internet-facing staging needs the same request protections as live."""
        return self.environment in {"staging", "production"}

    @property
    def stripe_ready(self) -> bool:
        return bool(self.stripe_secret_key and self.stripe_webhook_secret and self.stripe_connect_webhook_secret and self.stripe_connect_client_id)

    @property
    def email_ready(self) -> bool:
        return bool(self.email_api_key and self.email_from)


settings = Settings()
