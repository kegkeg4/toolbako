from __future__ import annotations

from typing import Any
from urllib.parse import urlparse
from pathlib import Path

from .config import Settings


def readiness_checks(settings: Settings, *, runtime_database_ready: bool = False) -> list[dict[str, Any]]:
    """Return non-secret launch checks used by health and the admin console."""
    checks = [
        ("production_mode", "本番モード", settings.is_production and not settings.demo_mode, "あなた", "APP_ENV=production、DEMO_MODE=false"),
        ("https", "独自ドメイン・HTTPS", settings.site_base_url.startswith("https://"), "あなた", "公開ドメインをSITE_BASE_URLへ設定"),
        ("trusted_host", "許可ドメイン制限", urlparse(settings.site_base_url).hostname in settings.allowed_hosts, "あなた", "公開ドメインをALLOWED_HOSTSへ追加"),
        ("session", "安全なセッション鍵", len(settings.session_secret) >= 32 and settings.session_secret not in {"dev-only-secret", "change-me-in-production"}, "あなた", "32文字以上のランダム値"),
        ("authentication", "本番認証基盤", settings.supabase_ready, "あなた", "Supabase AuthのURLとanon key"),
        ("trusted_proxy", "信頼するプロキシの限定", not settings.trust_proxy_headers or bool(settings.trusted_proxy_cidrs), "あなた", "proxy headerを使う場合はTRUSTED_PROXY_CIDRSを設定"),
        ("demo_persistence", "保存先の設定", bool(settings.database_url) if settings.is_production else bool(settings.database_url or (settings.state_db_path and Path(settings.state_db_path).is_absolute())), "接続設定", "本番はDATABASE_URL、デモのみSTATE_DB_PATH。デモデータは本番へ移行しない"),
        ("transaction_database", "本番データ保存の接続", bool(settings.database_url) and runtime_database_ready, "接続・検証", "Postgres互換repository・全更新の排他制御を実装。マイグレーション・TLS・session接続が必要。高負荷対応と金融台帳は別途検証"),
        ("service_role", "サーバー用プロフィール更新権限", bool(settings.supabase_service_role_key), "あなた", "SUPABASE_SERVICE_ROLE_KEY（サーバー内限定）"),
        ("payments", "本番決済・売上分配の検証", False, "実機検証・残実装が必要", "単発JPYの台帳・分配・振込・取消worker、結果不明の分配／振込を別管理者2名で照合する処理をローカル検証。実Stripe試験、再振込・振込後回収、定期課金・保有期限対応は未完了。live書込は停止中"),
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
        ("distributed_rate_limits", "複数台対応アクセス制限", bool(settings.redis_url), "あなた・実装", "RedisまたはエッジWAFで全インスタンス共通の制限"),
        ("privileged_mfa", "重要操作のMFA接続設定", settings.supabase_ready and (settings.is_production or settings.privileged_mfa_required), "接続後に実機検証", "認証アプリ登録・Supabaseのコード検証・10分間の重要操作制限を実装。実アカウントで検証が必要"),
        ("audit", "永続監査ログ", bool(settings.database_url) and runtime_database_ready, "接続・運用設計", "状態保存と同時にPostgres追記専用テーブルへ保存。削除・上書き拒否。保持・バックアップ・復旧訓練は別途必要"),
        ("tests", "主要フロー自動テスト", True, "実装済み", "認証・購入・取引・運営操作"),
    ]
    return [{"key":key,"label":label,"ok":ok,"owner":owner,"requirement":requirement} for key,label,ok,owner,requirement in checks]


def readiness_summary(settings: Settings, *, runtime_database_ready: bool = False) -> dict[str, Any]:
    checks = readiness_checks(settings, runtime_database_ready=runtime_database_ready)
    passed = sum(1 for item in checks if item["ok"])
    return {"ready":passed == len(checks), "passed":passed, "total":len(checks), "score":round(passed/len(checks)*100), "checks":checks,
            "notice":"設定・実装のチェックです。接続試験・負荷試験・決済実機試験の合格率やサービス完成度ではありません。"}
