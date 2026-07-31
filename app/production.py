from __future__ import annotations

from typing import Any
from urllib.parse import urlparse
from pathlib import Path

from .config import Settings


def readiness_checks(settings: Settings) -> list[dict[str, Any]]:
    """Return non-secret launch checks used by health and the admin console."""
    checks = [
        ("production_mode", "本番モード", settings.is_production and not settings.demo_mode, "あなた", "APP_ENV=production、DEMO_MODE=false"),
        ("https", "独自ドメイン・HTTPS", settings.site_base_url.startswith("https://"), "あなた", "公開ドメインをSITE_BASE_URLへ設定"),
        ("trusted_host", "許可ドメイン制限", urlparse(settings.site_base_url).hostname in settings.allowed_hosts, "あなた", "公開ドメインをALLOWED_HOSTSへ追加"),
        ("session", "安全なセッション鍵", len(settings.session_secret) >= 32 and settings.session_secret not in {"dev-only-secret", "change-me-in-production"}, "あなた", "32文字以上のランダム値"),
        ("authentication", "本番認証基盤", settings.supabase_ready, "あなた", "Supabase AuthのURLとanon key"),
        ("trusted_proxy", "信頼するプロキシの限定", not settings.trust_proxy_headers or bool(settings.trusted_proxy_cidrs), "あなた", "proxy headerを使う場合はTRUSTED_PROXY_CIDRSを設定"),
        ("demo_persistence", "検証用データ保存", bool(settings.state_db_path) and Path(settings.state_db_path).is_absolute(), "あなた", "検証環境では永続ボリューム上のSTATE_DB_PATH"),
        ("transaction_database", "本番トランザクションDB", False, "実装が必要", "注文・在庫・Webhook・売上をPostgres等の同一トランザクションで処理するrepository"),
        ("service_role", "サーバー用プロフィール更新権限", bool(settings.supabase_service_role_key), "あなた", "SUPABASE_SERVICE_ROLE_KEY（サーバー内限定）"),
        ("payments", "決済・売上分配", settings.stripe_ready, "あなた", "Stripe ConnectとWebhookの鍵"),
        ("email", "取引メール", settings.email_ready, "あなた", "メール送信APIと送信元ドメイン"),
        ("file_scan", "納品ファイル検査", bool(settings.clamav_host), "あなた", "ClamAV互換スキャナーの接続先"),
        ("private_storage", "非公開ファイル保管", Path(settings.private_storage_path).is_absolute(), "あなた", "永続ボリューム上のPRIVATE_STORAGE_PATH"),
        ("monitoring", "エラー監視", bool(settings.sentry_dsn), "あなた", "監視サービスのDSN"),
        ("admins", "運営管理者", bool(settings.admin_user_ids), "あなた", "ADMIN_USER_IDSに2名以上を推奨"),
        ("support", "問い合わせ窓口", bool(settings.support_email), "あなた", "SUPPORT_EMAIL"),
        ("legal", "販売事業者情報", all((settings.legal_business_name, settings.legal_representative, settings.legal_address, settings.legal_phone)), "あなた・専門家", "事業者名・責任者・住所・電話番号"),
        ("security_headers", "セキュリティヘッダー", True, "実装済み", "CSP・HSTS・クリックジャッキング対策"),
        ("rate_limits", "アクセス制限", settings.rate_limit_per_minute > 0, "実装済み", "過剰アクセスと総当たり対策"),
        ("authorization", "権限分離", True, "実装済み", "購入者・販売者・管理者をサーバー側で検証"),
        ("distributed_rate_limits", "複数台対応アクセス制限", False, "実装が必要", "RedisまたはエッジWAFで全インスタンス共通の制限"),
        ("privileged_mfa", "管理者・販売者MFA", False, "実装が必要", "Supabase AAL2確認と重要操作時の再認証"),
        ("audit", "永続監査ログ", False, "実装が必要", "追記専用Postgres監査ログと保持・検索方針"),
        ("tests", "主要フロー自動テスト", True, "実装済み", "認証・購入・取引・運営操作"),
    ]
    return [{"key":key,"label":label,"ok":ok,"owner":owner,"requirement":requirement} for key,label,ok,owner,requirement in checks]


def readiness_summary(settings: Settings) -> dict[str, Any]:
    checks = readiness_checks(settings)
    passed = sum(1 for item in checks if item["ok"])
    return {"ready":passed == len(checks), "passed":passed, "total":len(checks), "score":round(passed/len(checks)*100), "checks":checks}
