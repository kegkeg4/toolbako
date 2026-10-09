from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any
from uuid import uuid4

from .config import settings
from .payout_policy import RESERVED_PAYOUT_STATUSES, payout_fee, scheduled_payout_date

CATEGORIES = ["業務効率化", "文章・ライティング", "画像・デザイン", "開発者ツール", "マーケティング", "学習・教育", "エンタメ・ネタ", "データ分析", "その他"]
AI_OPTIONS = ["Claude", "ChatGPT", "Gemini", "Codex", "Copilot", "その他"]
PRICE_LABELS = {"free": "無料", "paid": "買い切り", "consultation": "相談して決める"}
DIST_LABELS = {"webapp": "Webアプリ", "download": "ダウンロード", "github": "GitHub", "prompt": "プロンプト・テキスト"}
TRANSFER_ASSET_LABELS = {
    "source_code": "ソースコード一式",
    "deployment_docs": "環境構築・運用手順",
    "domain": "ドメイン",
    "brand_assets": "名称・ロゴなどのブランド資産",
    "accounts": "移管可能な外部サービスアカウント",
    "customer_data": "顧客関連データ（適法性確認後）",
}
TRANSFER_STATUS_LABELS = {
    "nda_pending": "NDA確認待ち",
    "reviewing": "内容確認中",
    "negotiating": "条件交渉中",
    "declined": "見送り",
    "closed": "終了",
}

now = datetime.now(timezone.utc)


def platform_fee_for(amount: int, *, custom: bool = False) -> int:
    """Return the integer yen platform fee used by every sales ledger row."""
    rate = settings.custom_fee_rate if custom else settings.platform_fee_rate
    return round(max(0, amount) * rate)
SEED_TOOLS: list[dict[str, Any]] = [
    {"slug":"minutes-magic","name":"議事録マジック","tagline":"会議のメモを、次の行動が見える議事録へ。","description_md":"## 会議後の10分を取り戻す\n\nラフなメモを貼るだけで、決定事項・担当者・期限を整理します。購入後、会社用フォーマットへの調整もDMで相談できます。","category":"業務効率化","price_type":"paid","price":980,"distribution":"webapp","ai_used":["Claude"],"tags":["議事録","仕事術","要約"],"author_name":"mugi","author_username":"mugi","author_id":"seller-mugi","like_count":126,"view_count":1480,"sales_count":38,"rating":4.9,"support_days":14,"created_at":now-timedelta(hours=7),"accent":"coral"},
    {"slug":"pixel-recipe","name":"Pixel Recipe","tagline":"スクショから、デザインのレシピを抽出。","description_md":"## 見た目を言葉にする\n\n画像を入れると、配色・余白・タイポグラフィの特徴を説明します。買い切りでアップデートも受け取れます。","category":"画像・デザイン","price_type":"paid","price":1500,"distribution":"webapp","ai_used":["Gemini"],"tags":["デザイン","配色","UI"],"author_name":"sora","author_username":"sora","author_id":"seller-sora","like_count":94,"view_count":920,"sales_count":21,"rating":4.8,"support_days":7,"created_at":now-timedelta(days=1),"accent":"blue"},
    {"slug":"commit-senpai","name":"コミット先輩","tagline":"あなたの差分を、やさしく厳しくレビュー。","description_md":"## push前の相棒\n\nGit差分からバグ候補と改善点を見つけ、コミット文も提案します。ソースコードと導入手順をお渡しします。","category":"開発者ツール","price_type":"paid","price":2500,"distribution":"download","ai_used":["Codex"],"tags":["Git","レビュー","開発"],"author_name":"nullpo","author_username":"nullpo","author_id":"seller-nullpo","like_count":211,"view_count":2300,"sales_count":67,"rating":5.0,"support_days":30,"created_at":now-timedelta(days=2),"accent":"purple"},
    {"slug":"oyatsu-gacha","name":"おやつガチャAI","tagline":"今日の15時に、ちいさな運命を。","description_md":"## 迷う時間もおやつです\n\n気分と天気から、コンビニで買えるおやつを選びます。まずは無料で遊べます。","category":"エンタメ・ネタ","price_type":"free","price":0,"distribution":"webapp","ai_used":["ChatGPT"],"tags":["おやつ","ガチャ","生活"],"author_name":"haru","author_username":"haru","author_id":"seller-haru","like_count":67,"view_count":810,"sales_count":0,"rating":4.7,"support_days":0,"created_at":now-timedelta(days=3),"accent":"yellow"},
    {"slug":"copy-pocket","name":"コピーポケット Pro","tagline":"あなたの商品専用に、売れるコピー生成AIを調整。","description_md":"## あなたの商品に合わせて調整\n\nECやSNS向けのコピー生成ツールを、ブランドのトーンや商品データに合わせて納品します。内容と価格はDMで相談しましょう。","category":"マーケティング","price_type":"consultation","price":0,"distribution":"webapp","ai_used":["Claude","ChatGPT"],"tags":["コピー","SNS","EC"],"author_name":"nami","author_username":"nami","author_id":"seller-nami","like_count":52,"view_count":640,"sales_count":12,"rating":4.9,"support_days":30,"created_at":now-timedelta(days=4),"accent":"green"},
    {"slug":"study-map","name":"まなびマップ","tagline":"知りたいことから、自分だけの学習ルートを。","description_md":"目標と使える時間を入力すると、無理のない学習計画を作ります。プロンプト一式をすぐダウンロードできます。","category":"学習・教育","price_type":"paid","price":500,"distribution":"prompt","ai_used":["Gemini"],"tags":["学習","計画"],"author_name":"yui","author_username":"yui","author_id":"seller-yui","like_count":43,"view_count":502,"sales_count":44,"rating":4.6,"support_days":7,"created_at":now-timedelta(days=5),"accent":"pink"},
]


class DemoStore:
    def __init__(self, *, seed: bool = True) -> None:
        self._lock = RLock()
        self.tools = deepcopy(SEED_TOOLS)
        option_map = {"minutes-magic":[{"id":"format","name":"社内フォーマットへの調整","price":1500},{"id":"setup","name":"オンライン導入サポート（30分）","price":3000}],"commit-senpai":[{"id":"install","name":"環境構築サポート","price":2000},{"id":"custom","name":"独自レビュー規則の追加","price":5000}],"pixel-recipe":[{"id":"team","name":"チーム利用ライセンス","price":4000}]}
        passport_map = {
            "minutes-magic":{"data_handling":"入力は処理後24時間以内に削除","execution":"クラウド","license":"個人・社内利用可","source_included":False,"commercial_use":True,"skill_level":"かんたん","verified_on":"2026-07-10","security":"運営確認済み","quality_score":94,"update_policy":"1年間の機能アップデート込み","requirements":"最新のChrome / Safari"},
            "pixel-recipe":{"data_handling":"画像は解析後に自動削除","execution":"クラウド","license":"個人利用（チーム利用はオプション）","source_included":False,"commercial_use":True,"skill_level":"かんたん","verified_on":"2026-07-08","security":"運営確認済み","quality_score":91,"update_policy":"継続アップデート","requirements":"PNG / JPG、20MBまで"},
            "commit-senpai":{"data_handling":"コードは端末内で処理・保存なし","execution":"ローカル","license":"1ユーザー・商用開発可","source_included":True,"commercial_use":True,"skill_level":"開発者向け","verified_on":"2026-07-12","security":"ソース確認済み","quality_score":98,"update_policy":"メジャー更新1年間込み","requirements":"macOS / Windows、Git、Python 3.11+"},
            "oyatsu-gacha":{"data_handling":"個人情報を収集しません","execution":"クラウド","license":"無料利用","source_included":False,"commercial_use":False,"skill_level":"かんたん","verified_on":"2026-07-01","security":"基本確認済み","quality_score":86,"update_policy":"不定期更新","requirements":"スマートフォン / PC"},
            "copy-pocket":{"data_handling":"学習用データは案件ごとに個別合意","execution":"クラウド","license":"見積もり時に利用範囲を合意","source_included":False,"commercial_use":True,"skill_level":"伴走サポート","verified_on":"2026-07-09","security":"本人確認済み販売者","quality_score":93,"update_policy":"納品後30日間の調整込み","requirements":"商品情報・ブランド資料"},
            "study-map":{"data_handling":"入力内容は保存しません","execution":"ChatGPT / Gemini上","license":"個人利用・改変可","source_included":True,"commercial_use":False,"skill_level":"かんたん","verified_on":"2026-07-06","security":"内容確認済み","quality_score":88,"update_policy":"6か月間の更新込み","requirements":"ChatGPT または Gemini"},
        }
        version_map = {
            "minutes-magic":[{"version":"2.3.0","date":"2026-07-10","title":"話者別のアクション整理に対応"},{"version":"2.2.0","date":"2026-06-22","title":"社内テンプレートを3種追加"}],
            "pixel-recipe":[{"version":"1.8.1","date":"2026-07-08","title":"日本語フォントの推定精度を改善"}],
            "commit-senpai":[{"version":"3.1.0","date":"2026-07-12","title":"Codexモデル向けレビュー規則を追加"},{"version":"3.0.0","date":"2026-06-18","title":"機密コードを外部送信しないローカルモード"}],
            "oyatsu-gacha":[{"version":"1.2.0","date":"2026-07-01","title":"夏のおやつ候補を追加"}],
            "copy-pocket":[{"version":"2.0","date":"2026-07-09","title":"ブランドトーン診断を追加"}],
            "study-map":[{"version":"1.4","date":"2026-07-06","title":"週次の振り返りテンプレートを追加"}],
        }
        for tool in self.tools:
            tool["options"] = option_map.get(tool["slug"], [])
            tool["passport"] = passport_map[tool["slug"]]
            tool["versions"] = version_map.get(tool["slug"], [])
            tool["status"] = "published"
            tool["capacity"] = 5
            tool["supports_subscription"] = tool["slug"] in {"minutes-magic", "pixel-recipe"}
            tool["subscription_price"] = 680 if tool["slug"] == "minutes-magic" else (980 if tool["slug"] == "pixel-recipe" else 0)
            tool["safety_scan"] = {"status":"passed", "label":"安全チェック済み", "checked_at":"2026-07-14"}
            tool["estimated_delivery_days"] = 7 if tool.get("price_type") == "consultation" else 1
            tool["fulfillment_type"] = "instant" if tool.get("distribution") in {"download", "prompt"} else "custom"
            tool["purchase_notes"] = "購入前に動作環境と利用ライセンスをご確認ください。不明点はメッセージからご相談いただけます。"
            tool["faq"] = [
                {"question":"AIに詳しくなくても使えますか？", "answer":"はい。導入手順を付け、専門用語をできるだけ使わずにご案内します。"},
                {"question":"購入前に相談できますか？", "answer":"商品ページの「作者に質問・相談する」から、用途や環境を無料で確認できます。"},
            ]
            tool["customization_available"] = tool.get("price_type") != "free"
            tool["exclusive_available"] = tool["slug"] == "commit-senpai"
            tool["transfer_review_status"] = "approved" if tool["exclusive_available"] else "not_requested"
            tool["exclusive_price_min"] = 350_000 if tool["slug"] == "commit-senpai" else 0
            tool["transfer_assets"] = ["source_code", "deployment_docs", "brand_assets"] if tool["slug"] == "commit-senpai" else []
            tool["tech_stack"] = "Python 3.11 / Codex API / Git" if tool["slug"] == "commit-senpai" else ""
            tool["monthly_revenue"] = 0
            tool["monthly_profit"] = 0
            tool["monthly_cost"] = 0
            tool["weekly_ops_hours"] = 1 if tool["slug"] == "commit-senpai" else 0
            tool["handover_days"] = 14
            tool["exclusive_summary"] = "ソースコード、導入手順、ブランド資産をまとめて譲渡します。詳細情報は双方のNDA確認後に開示します。" if tool["slug"] == "commit-senpai" else ""
        demo_tool = deepcopy(self.tools[0])
        demo_tool.update({"slug":"workflow-pocket","name":"しごとポケットAI","tagline":"毎日の小さな定型作業を、3分で自動化。","description_md":"## はじめての業務自動化\n\n文章整理、メール下書き、表への転記をひとつにまとめた初心者向けAIツールです。","category":"業務効率化","price":1980,"author_name":"デモクリエイター","author_username":"demo_creator","author_id":"00000000-0000-0000-0000-000000000001","like_count":84,"view_count":1120,"sales_count":26,"rating":4.9,"accent":"purple","supports_subscription":True,"subscription_price":780,"customization_available":True,"exclusive_available":True,"transfer_review_status":"approved","exclusive_price_min":480000,"transfer_assets":["source_code","deployment_docs","brand_assets"],"tech_stack":"Python / FastAPI / OpenAI API","monthly_revenue":68000,"monthly_profit":49000,"monthly_cost":19000,"weekly_ops_hours":2,"handover_days":21,"exclusive_summary":"初心者向け業務自動化ツールのソースコードと運用手順、ブランド資産を一括譲渡します。顧客情報は譲渡対象外です。"})
        self.tools.append(demo_tool)
        self.likes: set[tuple[str, str]] = set()
        self.reports: list[dict[str, Any]] = []
        self.orders: list[dict[str, Any]] = [
            {"id":"demo-sale-001","tool_slug":"custom-request","tool_name":"SNS投稿AI カスタマイズ","buyer_id":"buyer-kado","buyer_name":"KADO商店","seller_id":"00000000-0000-0000-0000-000000000001","seller_name":"デモクリエイター","amount":48000,"platform_fee":4800,"status":"completed","messages":[],"delivery":{"note":"ブランド資料に合わせた投稿生成ツールを納品しました。","created_at":now-timedelta(days=6),"version":1},"revision_count":0,"cancel_reason":None,"reviewed":True,"is_custom":True,"delivery_days":10,"created_at":now-timedelta(days=18),"updated_at":now-timedelta(days=5)},
            {"id":"demo-sale-002","tool_slug":"custom-request","tool_name":"予約電話 要約ツール","buyer_id":"buyer-shokudo","buyer_name":"小さな食堂","seller_id":"00000000-0000-0000-0000-000000000001","seller_name":"デモクリエイター","amount":65000,"platform_fee":6500,"status":"in_progress","messages":[],"delivery":None,"revision_count":0,"cancel_reason":None,"reviewed":False,"is_custom":True,"delivery_days":14,"created_at":now-timedelta(days=2),"updated_at":now-timedelta(hours=4)},
            {"id":"demo-buy-001","tool_slug":"minutes-magic","tool_name":"議事録マジック","buyer_id":"00000000-0000-0000-0000-000000000001","buyer_name":"デモクリエイター","seller_id":"seller-mugi","seller_name":"mugi","amount":980,"platform_fee":98,"status":"completed","messages":[],"delivery":{"note":"Webアプリの利用URLとスタートガイドです。","created_at":now-timedelta(days=11),"version":1},"revision_count":0,"cancel_reason":None,"reviewed":True,"created_at":now-timedelta(days=12),"updated_at":now-timedelta(days=10)},
        ]
        for order in self.orders:
            if order["seller_id"] == "00000000-0000-0000-0000-000000000001": order["seller_username"] = "demo_creator"
            order.setdefault("extras", [])
            order.setdefault("base_amount", order["amount"])
            order.setdefault("primary_payment_amount", order["amount"])
            order.setdefault("payment_status", "paid")
            order.setdefault("sales_recorded", order["status"] != "cancelled")
            order.setdefault("billing_type", "one_time")
            order.setdefault("license_key", self._license_key(order) if order["status"] == "completed" else None)
            order.setdefault("receipt_no", f"TB-{order['created_at'].strftime('%Y%m')}-{order['id'][-8:].upper()}")
            order.setdefault("buyer_reviewed", False)
            if order["status"] == "completed":
                order.setdefault("completed_at", order.get("updated_at", order["created_at"]))
                order.setdefault("room_closed_at", order.get("updated_at", order["created_at"]))
                order.setdefault("review_deadline", order["room_closed_at"] + timedelta(days=10))
        self.conversations: list[dict[str, Any]] = []
        self.transfer_inquiries: list[dict[str, Any]] = []
        self.transfer_nda_acceptances: list[dict[str, Any]] = []
        self.reviews: list[dict[str, Any]] = [
            {"id":"review-demo-001","order_id":"demo-sale-001","tool_slug":"custom-request","tool_name":"SNS投稿AI カスタマイズ","seller_id":"00000000-0000-0000-0000-000000000001","seller_username":"demo_creator","author":"KADO商店","rating":5,"comment":"こちらの曖昧な要望を整理して、現場で迷わず使える形にしてくれました。納品後の質問にも丁寧でした。","created_at":now-timedelta(days=5)},
            {"id":"review-demo-002","order_id":"past-sale-002","tool_slug":"custom-request","tool_name":"問い合わせ分類AI","seller_id":"00000000-0000-0000-0000-000000000001","seller_username":"demo_creator","author":"灯りデザイン","rating":5,"comment":"専門用語を使わず説明してくれたので、AIに詳しくない私でも導入できました。","created_at":now-timedelta(days=28)},
            {"id":"review-demo-003","order_id":"past-sale-003","tool_slug":"custom-request","tool_name":"見積書チェックAI","seller_id":"00000000-0000-0000-0000-000000000001","seller_username":"demo_creator","author":"山川企画","rating":4,"comment":"希望していた機能が揃っていて満足です。今後のアップデートにも期待しています。","created_at":now-timedelta(days=51)},
            {"id":"review-buy-001","order_id":"demo-buy-001","tool_slug":"minutes-magic","tool_name":"議事録マジック","seller_id":"seller-mugi","seller_username":"mugi","author":"デモクリエイター","rating":5,"comment":"設定が簡単で、会議後の整理がかなり楽になりました。","created_at":now-timedelta(days=10)},
        ]
        for review in self.reviews:
            review.setdefault("published", True)
        self.buyer_reviews: list[dict[str, Any]] = []
        self.notifications: list[dict[str, Any]] = []
        self.notification_preferences: dict[str, dict[str, bool]] = {}
        self.favorite_folders: dict[str, list[dict[str, Any]]] = {}
        self.favorite_assignments: dict[str, str] = {}
        self.requests: list[dict[str, Any]] = [
            {"id":"req-001","title":"飲食店の予約電話を要約するAIツール","category":"業務効率化","detail":"録音データから日時・人数・要望を抽出し、スプレッドシートへ記録したいです。","budget_min":30000,"budget_max":80000,"deadline":"2026-07-28","owner_id":"buyer-sample","owner_name":"小さな食堂","status":"open","applications":3,"application_list":[],"created_at":now-timedelta(hours=5)},
            {"id":"req-002","title":"自社商品のSNS投稿文を作る社内ツール","category":"マーケティング","detail":"商品情報からX・Instagram用の投稿案を生成。ブランドトーンを学習させたいです。","budget_min":50000,"budget_max":120000,"deadline":"2026-07-25","owner_id":"buyer-sample2","owner_name":"KADO商店","status":"open","applications":5,"application_list":[],"created_at":now-timedelta(days=1)},
        ]
        self.blocks: set[tuple[str,str]] = set()
        self.reopen_waiters: set[tuple[str,str]] = set()
        self.payouts: list[dict[str, Any]] = []
        self.update_followers: set[tuple[str, str]] = set()
        self.creator_follows: set[tuple[str, str]] = set()
        self.subscriptions: list[dict[str, Any]] = []
        self.identity_applications: dict[str, dict[str, Any]] = {}
        self.nda_signatures: set[str] = set()
        self.nda_records: dict[str, dict[str, Any]] = {}
        self.coupons: list[dict[str, Any]] = []
        self.coupon_redemptions: set[tuple[str,str]] = set()
        self.security_settings: dict[str, dict[str, Any]] = {}
        self.support_cases: list[dict[str, Any]] = []
        self.audit_logs: list[dict[str, Any]] = []
        self.account_deletions: list[dict[str, Any]] = []
        self.processed_webhook_events: set[str] = set()
        self.legal_consents: list[dict[str, Any]] = []
        self.registered_users: dict[str, dict[str, Any]] = {
            "demo_creator": {
                "id": "00000000-0000-0000-0000-000000000001",
                "email": "demo@example.invalid",
                "display_name": "デモクリエイター",
                "headline": "小さな事業のAI導入を、わかりやすく伴走します",
                "bio": "業務にすっと馴染むAIツールを作っています。専門用語をできるだけ使わず、導入後も迷わず使える設計を大切にしています。",
                "skills": ["業務自動化", "プロンプト設計", "Python", "Notion"],
                "experience": [{"title":"AI業務改善支援", "period":"2024年〜", "detail":"小規模事業者向けに、定型作業の自動化とAI活用の導入を支援。"}],
                "portfolio": [{"title":"予約内容の要約・共有ツール", "url":"", "summary":"問い合わせ内容をAIで整理し、担当者へすぐ共有できる仕組みを制作。"}],
                "availability": "受付中",
                "response_time": "24時間以内",
                "pricing_note": "小さな調整は5,000円から。要件が固まっていなくても、まずはメッセージで相談できます。",
                "x_url": "",
                "website_url": "",
                "is_founding_member": True,
                "founding_member_since": now,
                "is_certified_creator": True,
                "certified_creator_since": now,
            }
        }
        self.connected_accounts: dict[str, dict[str, Any]] = {}
        self.account_sessions: dict[str, dict[str, Any]] = {}
        if not seed:
            # Production must start empty, never with fictitious users, sales,
            # reviews or verified-creator credentials. Keep collection types.
            for value in vars(self).values():
                if isinstance(value, (dict, list, set)):
                    value.clear()

    @staticmethod
    def _license_key(order: dict[str, Any]) -> str:
        token = str(order.get("id", "order")).replace("-", "").upper()
        return f"TBX-{token[:4]:0<4}-{token[-4:]:0<4}-AI"

    def list_tools(self, q: str = "", category: str = "", price_type: str = "", ai: str = "", sort: str = "new") -> list[dict[str, Any]]:
        items = [x for x in self.tools if not x.get("is_removed") and x.get("is_published", True) and x.get("status", "published") == "published"]
        if q:
            needle = q.casefold()
            items = [x for x in items if needle in (x["name"]+x["tagline"]+" ".join(x["tags"])).casefold()]
        if category: items = [x for x in items if x["category"] == category]
        if price_type: items = [x for x in items if x["price_type"] == price_type]
        if ai: items = [x for x in items if ai in x["ai_used"]]
        key = (lambda x: x["like_count"]) if sort == "popular" else ((lambda x: x["like_count"]*3+x["view_count"]*.1) if sort == "trending" else lambda x: x["created_at"])
        return sorted(items, key=key, reverse=True)

    def get(self, slug: str) -> dict[str, Any] | None:
        return next((x for x in self.tools if x["slug"] == slug and not x.get("is_removed")), None)

    def get_public(self, slug: str) -> dict[str, Any] | None:
        return next((x for x in self.tools if x["slug"] == slug and not x.get("is_removed") and x.get("is_published", True) and x.get("status", "published") == "published"), None)

    def create(self, values: dict[str, Any]) -> dict[str, Any]:
        passport = {"data_handling":"販売者に確認してください","execution":"未設定","license":"購入前に確認","source_included":False,"commercial_use":False,"skill_level":"未設定","verified_on":"未確認","security":"審査待ち","quality_score":70,"update_policy":"販売者に確認してください","requirements":"未設定"}
        item = {**values, "id": str(uuid4()), "like_count": 0, "view_count": 0, "sales_count": 0, "rating": None, "support_days": 7, "estimated_delivery_days":values.get("estimated_delivery_days", 1), "fulfillment_type":values.get("fulfillment_type", "custom"), "purchase_notes":values.get("purchase_notes", ""), "faq":values.get("faq", []), "customization_available":values.get("customization_available", True), "exclusive_available":values.get("exclusive_available", False), "transfer_review_status":values.get("transfer_review_status", "pending" if values.get("exclusive_available") else "not_requested"), "exclusive_price_min":values.get("exclusive_price_min", 0), "transfer_assets":values.get("transfer_assets", []), "tech_stack":values.get("tech_stack", ""), "monthly_revenue":values.get("monthly_revenue", 0), "monthly_profit":values.get("monthly_profit", 0), "monthly_cost":values.get("monthly_cost", 0), "weekly_ops_hours":values.get("weekly_ops_hours", 0), "handover_days":values.get("handover_days", 14), "exclusive_summary":values.get("exclusive_summary", ""), "created_at": now, "accent": "coral", "is_published": False, "status":"draft", "capacity":5, "supports_subscription":False, "subscription_price":0, "safety_scan":{"status":"pending","label":"安全チェック待ち","checked_at":None}, "options":[], "passport":passport, "versions":[]}
        item["created_at"] = datetime.now(timezone.utc)
        with self._lock: self.tools.insert(0, item)
        return item

    def username_for_id(self, user_id: str) -> str | None:
        for username, account in self.registered_users.items():
            if account.get("id") == user_id:
                return username
        tool = next((item for item in self.tools if item.get("author_id") == user_id), None)
        return tool and tool.get("author_username")

    def user_reference(self, username: str) -> dict[str, str] | None:
        account = self.registered_users.get(username)
        if account:
            return {"id":str(account.get("id", "")), "username":username}
        tool = next((item for item in self.tools if item.get("author_username") == username), None)
        return {"id":str(tool.get("author_id", "")), "username":username} if tool else None

    @staticmethod
    def transfer_is_public(tool: dict[str, Any]) -> bool:
        return bool(tool.get("exclusive_available") and tool.get("transfer_review_status") == "approved" and tool.get("is_published", True) and tool.get("status", "published") == "published" and not tool.get("is_removed"))

    def blocked_between(self, user_id: str, username: str, other_id: str, other_username: str) -> bool:
        return (user_id, other_username) in self.blocks or (other_id, username) in self.blocks

    def toggle_block(self, user: dict[str, Any], target_username: str) -> bool:
        target = self.user_reference(target_username)
        if not target or not target["id"]:
            raise ValueError("user not found")
        key = (user["id"], target_username)
        with self._lock:
            if key in self.blocks:
                self.blocks.remove(key)
                return False
            self.blocks.add(key)
            # A block also removes social signals in both directions. They are not
            # silently restored if the block is later lifted.
            self.creator_follows.discard((user["id"], target_username))
            self.creator_follows.discard((target["id"], user["username"]))
            target_slugs = {item["slug"] for item in self.tools if item.get("author_username") == target_username}
            own_slugs = {item["slug"] for item in self.tools if item.get("author_username") == user["username"]}
            removed_likes = {(uid, slug) for uid, slug in self.likes if (uid == user["id"] and slug in target_slugs) or (uid == target["id"] and slug in own_slugs)}
            self.likes.difference_update(removed_likes)
            for _, slug in removed_likes:
                tool = self.get(slug)
                if tool:
                    tool["like_count"] = max(0, tool.get("like_count", 0) - 1)
            for uid, slug in removed_likes:
                self.favorite_assignments.pop(f"{uid}\u241f{slug}", None)
            self.update_followers = {(uid, slug) for uid, slug in self.update_followers if not ((uid == user["id"] and slug in target_slugs) or (uid == target["id"] and slug in own_slugs))}
            self.reopen_waiters = {(uid, slug) for uid, slug in self.reopen_waiters if not ((uid == user["id"] and slug in target_slugs) or (uid == target["id"] and slug in own_slugs))}
        return True

    def toggle_like(self, user_id: str, slug: str, username: str | None = None) -> tuple[bool, int]:
        item = self.get(slug)
        if not item: return False, 0
        username = username or self.username_for_id(user_id) or ""
        if self.blocked_between(user_id, username, item.get("author_id", ""), item.get("author_username", "")):
            raise ValueError("blocked")
        key = (user_id, slug)
        with self._lock:
            liked = key not in self.likes
            if liked: self.likes.add(key); item["like_count"] += 1
            else:
                self.likes.remove(key); item["like_count"] = max(0, item["like_count"]-1)
                self.favorite_assignments.pop(f"{user_id}\u241f{slug}", None)
        return liked, item["like_count"]

    def favorite_folders_for(self, user_id: str) -> list[dict[str, Any]]:
        return list(self.favorite_folders.get(user_id, []))

    def create_favorite_folder(self, user_id: str, name: str) -> dict[str, Any]:
        with self._lock:
            folders = self.favorite_folders.setdefault(user_id, [])
            normalized = name.casefold()
            if len(folders) >= 20 or any(item["name"].casefold() == normalized for item in folders):
                raise ValueError("folder unavailable")
            folder = {"id":str(uuid4()), "name":name, "created_at":datetime.now(timezone.utc)}
            folders.append(folder)
            return folder

    def assign_favorite_folder(self, user_id: str, slug: str, folder_id: str) -> None:
        with self._lock:
            if (user_id, slug) not in self.likes: raise ValueError("not favorite")
            if folder_id and not any(item["id"] == folder_id for item in self.favorite_folders.get(user_id, [])):
                raise ValueError("folder not found")
            key = f"{user_id}\u241f{slug}"
            if folder_id: self.favorite_assignments[key] = folder_id
            else: self.favorite_assignments.pop(key, None)

    def delete_favorite_folder(self, user_id: str, folder_id: str) -> bool:
        with self._lock:
            folders = self.favorite_folders.get(user_id, [])
            before = len(folders)
            self.favorite_folders[user_id] = [item for item in folders if item["id"] != folder_id]
            if len(self.favorite_folders[user_id]) == before: return False
            self.favorite_assignments = {
                key:value for key,value in self.favorite_assignments.items() if not (key.startswith(f"{user_id}\u241f") and value == folder_id)
            }
            return True

    def coupon_discount(self, user_id: str, tool: dict[str, Any], subtotal: int, coupon: str) -> tuple[str, int, dict[str, Any] | None]:
        coupon_code = coupon.strip().upper()
        if not coupon_code:
            return "", 0, None
        if coupon_code == "WELCOME10":
            if (user_id, coupon_code) in self.coupon_redemptions:
                raise ValueError("coupon unavailable")
            return coupon_code, min(round(subtotal*.1), 1000), None
        coupon_record = next((item for item in self.coupons if item["code"] == coupon_code and item.get("active", True) and item["seller_id"] == tool["author_id"]), None)
        if not coupon_record or (coupon_record.get("expires_at") and coupon_record["expires_at"] < datetime.now(timezone.utc)) or coupon_record.get("used_count", 0) >= coupon_record.get("max_uses", 1) or (user_id, coupon_code) in self.coupon_redemptions:
            raise ValueError("coupon unavailable")
        discount = min(subtotal, round(subtotal * coupon_record["percent"] / 100), coupon_record.get("max_discount", subtotal))
        return coupon_code, discount, coupon_record

    def buy(self, user: dict[str, Any], slug: str, option_ids: list[str] | None = None, coupon: str = "", billing_type: str = "one_time", payment_pending: bool = False, *, return_created: bool = False) -> dict[str, Any] | tuple[dict[str, Any], bool]:
        with self._lock:
            tool = self.get_public(slug)
            if not tool or tool.get("price_type") != "paid": raise ValueError("not purchasable")
            if self.blocked_between(user["id"], user["username"], tool["author_id"], tool["author_username"]): raise ValueError("blocked")
            if billing_type == "subscription" and not tool.get("supports_subscription"): raise ValueError("subscription unavailable")
            existing = next((x for x in self.orders if x["buyer_id"] == user["id"] and x["tool_slug"] == slug and x["status"] != "cancelled" and x.get("payment_status", "paid") not in {"cancelled", "expired"}), None)
            if existing: return (existing, False) if return_created else existing
            active = sum(1 for x in self.orders if x["seller_id"] == tool["author_id"] and x["tool_slug"] == slug and x["status"] in {"in_progress","awaiting_acceptance","cancel_pending"})
            if active >= tool.get("capacity", 5): raise ValueError("capacity reached")
            selected_options = [x for x in tool.get("options",[]) if x["id"] in (option_ids or [])]
            base_amount = tool["subscription_price"] if billing_type == "subscription" else tool["price"]
            subtotal = base_amount + sum(x["price"] for x in selected_options)
            coupon_code, discount, coupon_record = self.coupon_discount(user["id"], tool, subtotal, coupon)
            total = subtotal - discount
            seller_account = self.registered_users.get(tool["author_username"], {})
            order = {"id":str(uuid4()),"tool_slug":slug,"tool_name":tool["name"],"buyer_id":user["id"],"buyer_name":user["display_name"],"buyer_username":user["username"],"buyer_email":user.get("email"),"seller_id":tool["author_id"],"seller_name":tool["author_name"],"seller_username":tool["author_username"],"seller_email":tool.get("author_email") or seller_account.get("email"),"amount":total,"base_amount":base_amount,"primary_payment_amount":total,"discount":discount,"coupon":coupon_code if discount else None,"selected_options":selected_options,"platform_fee":platform_fee_for(total),"platform_fee_rate":settings.platform_fee_rate,"status":"in_progress","payment_status":"pending" if payment_pending else "paid","sales_recorded":not payment_pending,"messages":[],"delivery":None,"revision_count":0,"cancel_reason":None,"reviewed":False,"buyer_reviewed":False,"extras":[],"billing_type":billing_type,"fulfillment_type":tool.get("fulfillment_type", "custom"),"license_key":None,"created_at":datetime.now(timezone.utc),"updated_at":datetime.now(timezone.utc)}
            order["receipt_no"] = f"TB-{order['created_at'].strftime('%Y%m')}-{order['id'][-8:].upper()}"
            self.orders.insert(0, order)
            if not payment_pending: tool["sales_count"] += 1
            if discount:
                if coupon_record:
                    coupon_record["used_count"] = coupon_record.get("used_count", 0) + 1
                self.coupon_redemptions.add((user["id"], coupon_code))
            if billing_type == "subscription":
                self.subscriptions.insert(0,{"id":str(uuid4()),"order_id":order["id"],"buyer_id":user["id"],"seller_id":tool["author_id"],"tool_slug":slug,"tool_name":tool["name"],"amount":total,"status":"pending" if payment_pending else "active","payments":[],"billing_events":[],"next_billing_at":datetime.now(timezone.utc)+timedelta(days=30),"created_at":datetime.now(timezone.utc)})
            if not payment_pending and order["fulfillment_type"] == "instant":
                self.complete_instant_order(order)
        return (order, True) if return_created else order

    def complete_instant_order(self, order: dict[str, Any]) -> None:
        if order.get("fulfillment_type") != "instant" or order.get("status") == "completed":
            return
        completed_at = datetime.now(timezone.utc)
        tool = self.get(order.get("tool_slug", "")) or {}
        distribution = tool.get("distribution", "download")
        note = {
            "webapp":"利用ライセンスを有効化しました。購入ライブラリから利用を開始できます。",
            "download":"配布ファイルと導入ガイドを購入ライブラリへ追加しました。",
            "github":"リポジトリへのアクセス手順を購入ライブラリへ追加しました。",
            "prompt":"プロンプト一式と利用ガイドを購入ライブラリへ追加しました。",
        }.get(distribution, "購入コンテンツをライブラリへ追加しました。")
        order.update({"status":"completed", "delivery":{"note":note,"created_at":completed_at,"version":1}, "license_key":self._license_key(order), "completed_at":completed_at, "room_closed_at":completed_at, "review_deadline":completed_at+timedelta(days=10), "updated_at":completed_at})

    def release_coupon(self, order: dict[str, Any]) -> None:
        code = order.get("coupon")
        if not code:
            return
        with self._lock:
            coupon = next((item for item in self.coupons if item["code"] == code), None)
            if coupon:
                coupon["used_count"] = max(0, coupon.get("used_count", 0) - 1)
            self.coupon_redemptions.discard((order["buyer_id"], code))

    def conversation_for(self, user: dict[str, Any], slug: str, intent: str = "general") -> dict[str, Any]:
        with self._lock:
            if intent not in {"general", "customization", "transfer"}: raise ValueError("invalid intent")
            tool = self.get_public(slug)
            if not tool: raise ValueError("tool not found")
            if self.blocked_between(user["id"], user["username"], tool["author_id"], tool["author_username"]): raise ValueError("blocked")
            existing = next((x for x in self.conversations if x["buyer_id"] == user["id"] and x["tool_slug"] == slug and x.get("conversation_type", "general") == intent), None)
            if existing: return existing
            greetings = {"general":"お問い合わせありがとうございます。どんな使い方を考えていますか？","customization":"カスタマイズのご相談ありがとうございます。現在の業務、実現したいこと、ご予算の目安を教えてください。","transfer":"独占譲渡のご相談ありがとうございます。機密情報は双方のNDA確認後に開示します。まずは利用目的と希望条件を確認させてください。"}
            conversation = {"id":str(uuid4()),"conversation_type":intent,"tool_slug":slug,"tool_name":tool["name"],"buyer_id":user["id"],"buyer_name":user["display_name"],"buyer_username":user["username"],"seller_id":tool["author_id"],"seller_name":tool["author_name"],"seller_username":tool["author_username"],"messages":[{"id":str(uuid4()),"sender_id":tool["author_id"],"sender_name":tool["author_name"],"body":greetings[intent],"created_at":datetime.now(timezone.utc)}],"proposals":[],"updated_at":datetime.now(timezone.utc)}
            self.conversations.insert(0, conversation)
        return conversation

    def create_transfer_inquiry(self, user: dict[str, Any], slug: str, offer_amount: int, intended_use: str, message: str) -> tuple[dict[str, Any], bool]:
        with self._lock:
            tool = self.get_public(slug)
            if not tool or not self.transfer_is_public(tool): raise ValueError("transfer unavailable")
            if tool["author_id"] == user["id"]: raise ValueError("self inquiry")
            if self.blocked_between(user["id"], user["username"], tool["author_id"], tool["author_username"]): raise ValueError("blocked")
            existing = next((item for item in self.transfer_inquiries if item["buyer_id"] == user["id"] and item["tool_slug"] == slug and item["status"] in {"nda_pending","reviewing","negotiating"}), None)
            if existing: return existing, False
            conversation = self.conversation_for(user, slug, "transfer")
            created_at = datetime.now(timezone.utc)
            conversation["messages"].append({"id":str(uuid4()),"sender_id":user["id"],"sender_name":user["display_name"],"body":message[:1000],"created_at":created_at})
            conversation["updated_at"] = created_at
            inquiry = {"id":str(uuid4()),"tool_slug":slug,"tool_name":tool["name"],"buyer_id":user["id"],"buyer_name":user["display_name"],"buyer_username":user["username"],"seller_id":tool["author_id"],"seller_name":tool["author_name"],"seller_username":tool["author_username"],"offer_amount":offer_amount,"intended_use":intended_use[:500],"message":message[:1000],"status":"nda_pending","conversation_id":conversation["id"],"created_at":created_at,"updated_at":created_at}
            self.transfer_inquiries.insert(0, inquiry)
            self.notify(tool["author_id"], f"{tool['name']}に独占譲渡の相談が届きました", f"希望額 ¥{offer_amount:,} · {user['display_name']}", f"/transfer-inquiries/{inquiry['id']}", "messages")
            return inquiry, True

    def transfer_inquiry_for(self, inquiry_id: str, user_id: str) -> dict[str, Any] | None:
        return next((item for item in self.transfer_inquiries if item["id"] == inquiry_id and user_id in {item["buyer_id"], item["seller_id"]}), None)

    def transfer_nda_signer_ids(self, inquiry_id: str) -> set[str]:
        return {item["user_id"] for item in self.transfer_nda_acceptances if item["inquiry_id"] == inquiry_id}

    def sign_transfer_nda(self, inquiry: dict[str, Any], user: dict[str, Any], document_hash: str, ip_hash: str) -> tuple[dict[str, Any], bool]:
        if user["id"] not in {inquiry["buyer_id"], inquiry["seller_id"]}: raise ValueError("not allowed")
        if inquiry.get("status") in {"declined", "closed"}: raise ValueError("case closed")
        other_id = inquiry["seller_id"] if user["id"] == inquiry["buyer_id"] else inquiry["buyer_id"]
        other_username = inquiry["seller_username"] if user["id"] == inquiry["buyer_id"] else inquiry["buyer_username"]
        if self.blocked_between(user["id"], user["username"], other_id, other_username): raise ValueError("blocked")
        with self._lock:
            existing = next((item for item in self.transfer_nda_acceptances if item["inquiry_id"] == inquiry["id"] and item["user_id"] == user["id"]), None)
            if existing: return existing, False
            signed_at = datetime.now(timezone.utc)
            record = {"id":str(uuid4()),"inquiry_id":inquiry["id"],"user_id":user["id"],"signer_name":user["display_name"],"signer_username":user["username"],"role":"buyer" if user["id"] == inquiry["buyer_id"] else "seller","version":"2026-07-16-transfer-v1","document_hash":document_hash[:128],"ip_hash":ip_hash[:128],"signed_at":signed_at}
            self.transfer_nda_acceptances.append(record)
            signer_ids = self.transfer_nda_signer_ids(inquiry["id"])
            if {inquiry["buyer_id"], inquiry["seller_id"]} <= signer_ids and inquiry["status"] == "nda_pending":
                inquiry["status"] = "reviewing"
                inquiry["updated_at"] = signed_at
                self.notify(inquiry["buyer_id"], f"{inquiry['tool_name']}の双方NDAが完了しました", "詳細情報の確認へ進めます", f"/transfer-inquiries/{inquiry['id']}", "messages")
                self.notify(inquiry["seller_id"], f"{inquiry['tool_name']}の双方NDAが完了しました", "詳細情報の確認へ進めます", f"/transfer-inquiries/{inquiry['id']}", "messages")
            else:
                self.notify(other_id, f"{inquiry['tool_name']}のNDA確認が届きました", "あなたの確認後に詳細情報を開示できます", f"/transfer-inquiries/{inquiry['id']}", "messages")
            return record, True

    def update_transfer_status(self, inquiry: dict[str, Any], seller: dict[str, Any], status: str) -> None:
        if inquiry.get("seller_id") != seller["id"] or status not in {"reviewing","negotiating","declined","closed"}: raise ValueError("not allowed")
        transitions = {"nda_pending":{"reviewing","negotiating","declined","closed"},"reviewing":{"negotiating","declined","closed"},"negotiating":{"reviewing","declined","closed"},"declined":set(),"closed":set()}
        if status not in transitions.get(inquiry.get("status"), set()): raise ValueError("invalid transition")
        if status == "negotiating" and not ({inquiry["buyer_id"], inquiry["seller_id"]} <= self.transfer_nda_signer_ids(inquiry["id"])): raise ValueError("nda required")
        with self._lock:
            inquiry["status"] = status
            inquiry["updated_at"] = datetime.now(timezone.utc)
            self.notify(inquiry["buyer_id"], f"{inquiry['tool_name']}の譲渡相談が更新されました", TRANSFER_STATUS_LABELS[status], f"/transfer-inquiries/{inquiry['id']}", "messages")

    def withdraw_transfer_inquiry(self, inquiry: dict[str, Any], buyer: dict[str, Any]) -> None:
        if inquiry.get("buyer_id") != buyer["id"]: raise ValueError("not allowed")
        if inquiry.get("status") in {"declined", "closed"}: raise ValueError("case closed")
        with self._lock:
            inquiry["status"] = "closed"
            inquiry["closed_by"] = "buyer"
            inquiry["updated_at"] = datetime.now(timezone.utc)
            self.notify(inquiry["seller_id"], f"{inquiry['tool_name']}の譲渡相談が取り下げられました", buyer["display_name"], f"/transfer-inquiries/{inquiry['id']}", "messages")

    def add_message(self, conversation_id: str, user: dict[str, Any], body: str) -> dict[str, Any]:
        with self._lock:
            conversation = next((x for x in self.conversations if x["id"] == conversation_id), None)
            if not conversation or user["id"] not in {conversation["buyer_id"], conversation["seller_id"]}: raise ValueError("not allowed")
            other_username = conversation["seller_username"] if user["id"] == conversation["buyer_id"] else conversation["buyer_username"]
            other_id = conversation["seller_id"] if user["id"] == conversation["buyer_id"] else conversation["buyer_id"]
            if (user["id"], other_username) in self.blocks or (other_id, user["username"]) in self.blocks: raise ValueError("blocked")
            message = {"id":str(uuid4()),"sender_id":user["id"],"sender_name":user["display_name"],"body":body[:1000],"created_at":datetime.now(timezone.utc)}
            conversation["messages"].append(message)
            conversation["updated_at"] = message["created_at"]
        return conversation

    def create_proposal(self, conversation_id: str, title: str, detail: str, amount: int, delivery_days: int) -> dict[str, Any]:
        with self._lock:
            conversation = next((x for x in self.conversations if x["id"] == conversation_id), None)
            if not conversation: raise ValueError("not found")
            proposal = {"id":str(uuid4()),"title":title[:80],"detail":detail[:1000],"amount":amount,"delivery_days":delivery_days,"status":"open","created_at":datetime.now(timezone.utc),"expires_at":datetime.now(timezone.utc)+timedelta(days=7),"revision":1}
            conversation["proposals"].append(proposal)
            conversation["updated_at"] = proposal["created_at"]
        return proposal

    def buy_proposal(self, user: dict[str, Any], conversation_id: str, proposal_id: str) -> dict[str, Any]:
        with self._lock:
            conversation = next((x for x in self.conversations if x["id"] == conversation_id and x["buyer_id"] == user["id"]), None)
            if not conversation: raise ValueError("not allowed")
            if self.blocked_between(user["id"], user["username"], conversation["seller_id"], conversation["seller_username"]): raise ValueError("blocked")
            proposal = next((x for x in conversation["proposals"] if x["id"] == proposal_id and x["status"] == "open"), None)
            if not proposal: raise ValueError("proposal not found")
            if proposal.get("expires_at") and proposal["expires_at"] < datetime.now(timezone.utc): proposal["status"] = "expired"; raise ValueError("expired")
            order = {"id":str(uuid4()),"tool_slug":conversation["tool_slug"],"tool_name":proposal["title"],"buyer_id":user["id"],"buyer_name":user["display_name"],"buyer_username":user["username"],"seller_id":conversation["seller_id"],"seller_name":conversation["seller_name"],"seller_username":conversation["seller_username"],"amount":proposal["amount"],"base_amount":proposal["amount"],"primary_payment_amount":proposal["amount"],"platform_fee":platform_fee_for(proposal["amount"], custom=True),"platform_fee_rate":settings.custom_fee_rate,"status":"in_progress","messages":conversation["messages"].copy(),"delivery":None,"revision_count":0,"cancel_reason":None,"reviewed":False,"buyer_reviewed":False,"is_custom":True,"delivery_days":proposal["delivery_days"],"extras":[],"billing_type":"one_time","license_key":None,"created_at":datetime.now(timezone.utc),"updated_at":datetime.now(timezone.utc)}
            order["receipt_no"] = f"TB-{order['created_at'].strftime('%Y%m')}-{order['id'][-8:].upper()}"
            self.orders.insert(0, order)
            proposal["status"] = "purchased"
        return order

    def claim_webhook_event(self, event_id: str) -> bool:
        """Atomically claim an event within one application process."""
        with self._lock:
            if event_id in self.processed_webhook_events:
                return False
            self.processed_webhook_events.add(event_id)
            return True

    def get_order(self, order_id: str, user_id: str) -> dict[str, Any] | None:
        return next((x for x in self.orders if x["id"] == order_id and user_id in {x["buyer_id"],x["seller_id"]}), None)

    def order_message(self, order: dict[str, Any], user: dict[str, Any], body: str) -> None:
        with self._lock:
            order["messages"].append({"id":str(uuid4()),"sender_id":user["id"],"sender_name":user["display_name"],"body":body[:1000],"kind":"message","created_at":datetime.now(timezone.utc)})
            order["updated_at"] = datetime.now(timezone.utc)

    @staticmethod
    def room_is_open(order: dict[str, Any], at: datetime | None = None) -> bool:
        if order.get("status") == "cancelled" or order.get("room_closed_at"):
            return False
        if order.get("status") != "completed":
            return True
        closes_at = order.get("room_closes_at")
        return bool(closes_at and closes_at > (at or datetime.now(timezone.utc)))

    @staticmethod
    def review_is_open(order: dict[str, Any], at: datetime | None = None) -> bool:
        if order.get("status") != "completed":
            return False
        deadline = order.get("review_deadline")
        return not deadline or deadline >= (at or datetime.now(timezone.utc))

    def process_due_order_events(self, at: datetime | None = None) -> int:
        """Apply deterministic room closure and review publication deadlines."""
        at = at or datetime.now(timezone.utc)
        changed = 0
        with self._lock:
            for order in self.orders:
                if order.get("status") == "awaiting_acceptance" and order.get("auto_close_at") and order["auto_close_at"] <= at:
                    order["status"] = "completed"
                    order["completed_at"] = at
                    order["room_closed_at"] = at
                    order["review_deadline"] = at + timedelta(days=10)
                    order["auto_completed"] = True
                    order["license_key"] = self._license_key(order)
                    order["updated_at"] = at
                    changed += 1
                elif order.get("status") == "completed" and not order.get("room_closed_at") and order.get("room_closes_at") and order["room_closes_at"] <= at:
                    order["room_closed_at"] = order["room_closes_at"]
                    order["updated_at"] = at
                    changed += 1
                if order.get("status") == "completed" and order.get("review_deadline") and order["review_deadline"] <= at:
                    for review in self.reviews:
                        if review.get("order_id") == order["id"] and not review.get("published", True):
                            review["published"] = True
                            changed += 1
                    for review in self.buyer_reviews:
                        if review.get("order_id") == order["id"] and not review.get("published", True):
                            review["published"] = True
                            changed += 1
        return changed

    def deliver(self, order: dict[str, Any], note: str) -> None:
        with self._lock:
            if order.get("status") != "in_progress" or not note.strip(): raise ValueError("not deliverable")
            delivered_at = datetime.now(timezone.utc)
            order["delivery"] = {"note":note[:1500],"created_at":delivered_at,"version":order["revision_count"]+1}
            order["auto_close_at"] = delivered_at + timedelta(days=3)
            order["status"] = "awaiting_acceptance"; order["updated_at"] = delivered_at

    def transition(self, order: dict[str, Any], action: str, reason: str = "", actor_id: str | None = None) -> None:
        with self._lock:
            if action == "accept" and order["status"] == "awaiting_acceptance":
                accepted_at = datetime.now(timezone.utc)
                order["status"] = "completed"; order["license_key"] = self._license_key(order)
                order["accepted_at"] = accepted_at; order["completed_at"] = accepted_at
                order["room_closes_at"] = accepted_at + timedelta(hours=24)
                order["review_deadline"] = accepted_at + timedelta(days=10)
            elif action == "revise" and order["status"] == "awaiting_acceptance" and order["revision_count"] < 1:
                order["status"] = "in_progress"; order["revision_count"] += 1; order["delivery"] = None; order.pop("auto_close_at", None)
            elif action == "cancel_request" and order["status"] in {"in_progress","awaiting_acceptance"}:
                order["cancel_previous_status"] = order["status"]; order["status"] = "cancel_pending"; order["cancel_reason"] = reason[:500]; order["cancel_requested_by"] = actor_id
            elif action == "cancel_accept" and order["status"] == "cancel_pending" and actor_id and order.get("cancel_requested_by") != actor_id:
                order["status"] = "cancelled"; order["cancel_requested_by"] = None; order.pop("cancel_previous_status", None)
            elif action == "cancel_reject" and order["status"] == "cancel_pending" and actor_id and order.get("cancel_requested_by") != actor_id:
                order["status"] = order.pop("cancel_previous_status", "in_progress"); order["cancel_reason"] = None; order["cancel_requested_by"] = None
            else: raise ValueError("invalid transition")
            order["updated_at"] = datetime.now(timezone.utc)

    def add_review(self, order: dict[str, Any], user: dict[str, Any], rating: int, comment: str, dimensions: dict[str, int] | None = None) -> dict[str, Any]:
        with self._lock:
            if not self.review_is_open(order) or order.get("reviewed") or user["id"] != order["buyer_id"]: raise ValueError("not reviewable")
            tool = self.get(order["tool_slug"])
            review = {"id":str(uuid4()),"order_id":order["id"],"tool_slug":order["tool_slug"],"tool_name":order["tool_name"],"seller_id":order["seller_id"],"seller_username":tool["author_username"] if tool else order.get("seller_username"),"author":user["display_name"],"rating":rating,"comment":comment[:500],"private_dimensions":dimensions or {},"published":bool(order.get("buyer_reviewed")),"created_at":datetime.now(timezone.utc)}
            self.reviews.insert(0, review)
            order["reviewed"] = True
            if order.get("buyer_reviewed"):
                for item in self.buyer_reviews:
                    if item.get("order_id") == order["id"]: item["published"] = True
        return review

    def add_buyer_review(self, order: dict[str, Any], user: dict[str, Any], rating: int, comment: str, dimensions: dict[str, int] | None = None) -> dict[str, Any]:
        with self._lock:
            if not self.review_is_open(order) or order.get("buyer_reviewed") or user["id"] != order["seller_id"]: raise ValueError("not reviewable")
            review = {"id":str(uuid4()),"order_id":order["id"],"buyer_id":order["buyer_id"],"buyer_username":order.get("buyer_username"),"buyer_name":order["buyer_name"],"author_id":user["id"],"author":user["display_name"],"rating":rating,"comment":comment[:500],"private_dimensions":dimensions or {},"published":bool(order.get("reviewed")),"created_at":datetime.now(timezone.utc)}
            self.buyer_reviews.insert(0, review)
            order["buyer_reviewed"] = True
            if order.get("reviewed"):
                for item in self.reviews:
                    if item.get("order_id") == order["id"]: item["published"] = True
        return review

    def notification_settings_for(self, user_id: str) -> dict[str, bool]:
        defaults = {"messages":True, "transactions":True, "requests":True, "updates":True}
        return {**defaults, **self.notification_preferences.get(user_id, {})}

    def notify(self, user_id: str, title: str, body: str, url: str, category: str = "general") -> None:
        with self._lock:
            if category in {"messages","transactions","requests","updates"} and not self.notification_settings_for(user_id).get(category, True):
                return
            created_at = datetime.now(timezone.utc)
            recent = next((item for item in self.notifications if item["user_id"] == user_id and not item.get("read") and item.get("title") == title and item.get("url") == url and item.get("created_at", created_at) >= created_at-timedelta(minutes=5)), None)
            if recent:
                recent.update({"body":body, "created_at":created_at, "category":category})
                return
            self.notifications.insert(0,{"id":str(uuid4()),"user_id":user_id,"title":title,"body":body,"url":url,"category":category,"read":False,"created_at":created_at})
            user_items = [item for item in self.notifications if item["user_id"] == user_id]
            for stale in user_items[200:]:
                self.notifications.remove(stale)

    def mark_notifications_read(self, user_id: str) -> int:
        with self._lock:
            unread = [item for item in self.notifications if item["user_id"] == user_id and not item["read"]]
            for item in unread:
                item["read"] = True
            return len(unread)

    def mark_notification_read(self, user_id: str, notification_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = next((candidate for candidate in self.notifications if candidate["id"] == notification_id and candidate["user_id"] == user_id), None)
            if item: item["read"] = True
            return item

    def follow_updates(self, user_id: str, slug: str) -> bool:
        tool = self.get(slug)
        username = self.username_for_id(user_id) or ""
        if not tool or self.blocked_between(user_id, username, tool.get("author_id", ""), tool.get("author_username", "")):
            raise ValueError("blocked")
        key = (user_id, slug)
        with self._lock:
            if key in self.update_followers:
                self.update_followers.remove(key)
                return False
            self.update_followers.add(key)
        self.notify(user_id, "更新通知をオンにしました", self.get(slug)["name"], f"/tools/{slug}#updates", "updates")
        return True

    def follow_creator(self, user_id: str, username: str, follower_username: str | None = None) -> bool:
        target = self.user_reference(username)
        follower_username = follower_username or self.username_for_id(user_id) or ""
        if not target or self.blocked_between(user_id, follower_username, target["id"], username):
            raise ValueError("blocked")
        key = (user_id, username)
        with self._lock:
            if key in self.creator_follows:
                self.creator_follows.remove(key); return False
            self.creator_follows.add(key)
        return True

    def toggle_reopen_wait(self, user: dict[str, Any], slug: str) -> bool:
        tool = self.get(slug)
        if not tool or tool.get("author_id") == user["id"]:
            raise ValueError("not waitable")
        if self.blocked_between(user["id"], user["username"], tool.get("author_id", ""), tool.get("author_username", "")):
            raise ValueError("blocked")
        key = (user["id"], slug)
        with self._lock:
            if key in self.reopen_waiters:
                self.reopen_waiters.remove(key)
                return False
            self.reopen_waiters.add(key)
            return True

    def notify_reopened(self, slug: str) -> int:
        tool = self.get(slug)
        if not tool:
            return 0
        with self._lock:
            waiter_ids = [uid for uid, waiting_slug in self.reopen_waiters if waiting_slug == slug]
            self.reopen_waiters = {(uid, waiting_slug) for uid, waiting_slug in self.reopen_waiters if waiting_slug != slug}
        sent = 0
        for user_id in waiter_ids:
            username = self.username_for_id(user_id) or ""
            if self.blocked_between(user_id, username, tool.get("author_id", ""), tool.get("author_username", "")):
                continue
            self.notify(user_id, "販売が再開されました", tool["name"], f"/tools/{slug}", "updates")
            sent += 1
        return sent

    def create_coupon(self, seller: dict[str, Any], code: str, percent: int, max_discount: int, max_uses: int, expires_at: datetime) -> dict[str, Any]:
        normalized = code.strip().upper()
        with self._lock:
            if normalized == "WELCOME10" or any(item["code"] == normalized for item in self.coupons):
                raise ValueError("coupon exists")
            coupon = {"id":str(uuid4()), "seller_id":seller["id"], "seller_username":seller["username"], "code":normalized, "percent":percent, "max_discount":max_discount, "max_uses":max_uses, "used_count":0, "expires_at":expires_at, "active":True, "created_at":datetime.now(timezone.utc)}
            self.coupons.insert(0, coupon)
            return coupon

    def creator_rank(self, username: str, verified: bool = False) -> dict[str, str]:
        sales = sum(x.get("sales_count", 0) for x in self.tools if x.get("author_username") == username)
        score = sales + (20 if verified else 0)
        if score >= 100: return {"name":"プラチナ", "class":"platinum", "next":"最高ランク"}
        if score >= 50: return {"name":"ゴールド", "class":"gold", "next":f"あと{100-score}件でプラチナ"}
        if score >= 20: return {"name":"シルバー", "class":"silver", "next":f"あと{50-score}件でゴールド"}
        if score >= 5: return {"name":"ブロンズ", "class":"bronze", "next":f"あと{20-score}件でシルバー"}
        return {"name":"レギュラー", "class":"regular", "next":f"あと{5-score}件でブロンズ"}

    def set_creator_badge(self, username: str, badge: str, enabled: bool, changed_at: datetime | None = None) -> dict[str, Any]:
        """Grant or revoke an operator-controlled creator credential."""
        fields = {
            "founding": ("is_founding_member", "founding_member_since"),
            "certified": ("is_certified_creator", "certified_creator_since"),
        }
        if badge not in fields:
            raise ValueError("unknown creator badge")
        account = self.registered_users.get(username)
        if not account:
            raise ValueError("user not found")
        flag_field, since_field = fields[badge]
        with self._lock:
            account[flag_field] = bool(enabled)
            account[since_field] = (changed_at or datetime.now(timezone.utc)) if enabled else None
        return account

    def add_extra_payment(self, order: dict[str, Any], amount: int, note: str) -> None:
        with self._lock:
            extra = {"id":str(uuid4()),"amount":amount,"note":note[:200],"created_at":datetime.now(timezone.utc)}
            rate = order.get("platform_fee_rate", settings.custom_fee_rate if order.get("is_custom") else settings.platform_fee_rate)
            order.setdefault("extras", []).append(extra); order["amount"] += amount; order["platform_fee"] += round(amount * rate); order["updated_at"] = extra["created_at"]

    def reserve_extra_payment(self, order: dict[str, Any], amount: int, note: str) -> tuple[dict[str, Any], bool]:
        with self._lock:
            previous = next((item for item in order.get("pending_extras", []) if item.get("status") == "pending" and item["amount"] == amount and item["note"] == note), None)
            if previous:
                return previous, False
            extra = {"id":str(uuid4()),"amount":amount,"note":note,"status":"pending","created_at":datetime.now(timezone.utc)}
            order.setdefault("pending_extras", []).append(extra)
            return extra, True

    def rollback_extra_payment(self, order: dict[str, Any], extra: dict[str, Any]) -> None:
        with self._lock:
            if extra in order.get("pending_extras", []) and extra.get("status") == "pending":
                order["pending_extras"].remove(extra)

    def tool_active_orders(self, slug: str) -> int:
        return sum(1 for x in self.orders if x["tool_slug"] == slug and x["status"] in {"in_progress","awaiting_acceptance","cancel_pending"})

    def audit(self, user_id: str | None, action: str, target: str, request_id: str = "") -> None:
        if not request_id:
            from .request_context import request_id_context
            request_id = request_id_context.get()
        with self._lock:
            self.audit_logs.insert(0,{"id":str(uuid4()),"user_id":user_id,"action":action[:80],"target":target[:160],"request_id":request_id[:64],"created_at":datetime.now(timezone.utc)})
            # Local/demo storage keeps a substantially longer trail. Production
            # migrations use an append-only database table without this cap.
            del self.audit_logs[10_000:]

    def create_support_case(self, user: dict[str, Any], category: str, subject: str, detail: str, order_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            existing = next((x for x in self.support_cases if x.get("order_id") == order_id and order_id and x["status"] in {"open","investigating"}), None)
            if existing: return existing
            item = {"id":str(uuid4()),"user_id":user["id"],"user_name":user["display_name"],"category":category,"subject":subject[:100],"detail":detail[:2000],"order_id":order_id,"status":"open","priority":"high" if category in {"payment","dispute","security"} else "normal","created_at":datetime.now(timezone.utc),"updated_at":datetime.now(timezone.utc)}
            self.support_cases.insert(0,item)
            return item

    def execute_due_account_deletions(self, now: datetime | None = None) -> int:
        """Pseudonymize retained transaction records and erase account data after grace."""
        now = now or datetime.now(timezone.utc)
        completed = 0
        with self._lock:
            due = [item for item in self.account_deletions if item.get("status") == "scheduled" and item.get("delete_after") and item["delete_after"] <= now]
            for deletion in due:
                user_id = deletion["user_id"]
                if any(p["seller_id"] == user_id and p.get("status") not in {"paid", "completed", "cancelled"} for p in self.payouts):
                    # Do not erase the Connect account while funds are reserved
                    # or a bank failure/unknown provider outcome is unresolved.
                    deletion["hold_reason"] = "振込の結果確認が完了するまで退会処理を保留しています"
                    continue
                usernames = [name for name, account in self.registered_users.items() if account.get("id") == user_id]
                for username in usernames:
                    self.registered_users.pop(username, None)
                self.account_sessions = {sid:record for sid,record in self.account_sessions.items() if record.get("user",{}).get("id") != user_id}
                self.identity_applications.pop(user_id, None)
                self.security_settings.pop(user_id, None)
                self.connected_accounts.pop(user_id, None)
                self.nda_signatures.discard(user_id)
                self.nda_records.pop(user_id, None)
                self.notifications = [item for item in self.notifications if item.get("user_id") != user_id]
                self.notification_preferences.pop(user_id, None)
                self.favorite_folders.pop(user_id, None)
                self.favorite_assignments = {key:value for key,value in self.favorite_assignments.items() if not key.startswith(f"{user_id}\u241f")}
                removed_likes = {(uid, slug) for uid, slug in self.likes if uid == user_id}
                self.likes.difference_update(removed_likes)
                for _, slug in removed_likes:
                    tool = self.get(slug)
                    if tool: tool["like_count"] = max(0, tool.get("like_count", 0) - 1)
                self.support_cases = [item for item in self.support_cases if item.get("user_id") != user_id]
                self.update_followers = {item for item in self.update_followers if item[0] != user_id}
                self.reopen_waiters = {item for item in self.reopen_waiters if item[0] != user_id}
                self.creator_follows = {item for item in self.creator_follows if item[0] != user_id and item[1] not in usernames}
                self.blocks = {item for item in self.blocks if item[0] != user_id and item[1] not in usernames}
                self.coupon_redemptions = {item for item in self.coupon_redemptions if item[0] != user_id}
                for coupon in self.coupons:
                    if coupon.get("seller_id") == user_id:
                        coupon.update({"seller_username":"deleted", "active":False})
                for tool in self.tools:
                    if tool.get("author_id") == user_id:
                        tool.update({"author_name":"退会済みユーザー","status":"paused","is_published":False})
                for order in self.orders:
                    if order.get("buyer_id") == user_id:
                        order["buyer_name"] = "退会済みユーザー"; order["buyer_email"] = None; order["buyer_username"] = "deleted"
                    if order.get("seller_id") == user_id:
                        order["seller_name"] = "退会済みユーザー"; order["seller_email"] = None; order["seller_username"] = "deleted"
                    for message in order.get("messages", []):
                        if message.get("sender_id") == user_id: message["sender_name"] = "退会済みユーザー"
                deleted_order_ids = {order["id"] for order in self.orders if user_id in {order.get("buyer_id"),order.get("seller_id")}}
                for review in self.reviews:
                    if review.get("order_id") in deleted_order_ids: review["author"] = "退会済みユーザー"
                for report in self.reports:
                    if report.get("reporter") in usernames: report["reporter"] = "deleted"
                for conversation in self.conversations:
                    if conversation.get("buyer_id") == user_id:
                        conversation["buyer_name"] = "退会済みユーザー"; conversation["buyer_username"] = "deleted"
                    if conversation.get("seller_id") == user_id:
                        conversation["seller_name"] = "退会済みユーザー"; conversation["seller_username"] = "deleted"
                    for message in conversation.get("messages", []):
                        if message.get("sender_id") == user_id: message["sender_name"] = "退会済みユーザー"
                for inquiry in self.transfer_inquiries:
                    if inquiry.get("buyer_id") == user_id:
                        inquiry.update({"buyer_name":"退会済みユーザー","buyer_username":"deleted","intended_use":"","message":""})
                    if inquiry.get("seller_id") == user_id:
                        inquiry.update({"seller_name":"退会済みユーザー","seller_username":"deleted","status":"closed"})
                for acceptance in self.transfer_nda_acceptances:
                    if acceptance.get("user_id") == user_id:
                        acceptance.update({"signer_name":"退会済みユーザー","signer_username":"deleted","ip_hash":""})
                for item in self.requests:
                    if item.get("owner_id") == user_id:
                        item["owner_name"] = "退会済みユーザー"; item["owner_username"] = "deleted"
                    for application in item.get("application_list", []):
                        if application.get("applicant_id") == user_id:
                            application["applicant_name"] = "退会済みユーザー"; application["applicant_username"] = "deleted"; application["message"] = ""
                deletion.update({"status":"completed","completed_at":now,"mode":"pseudonymized_financial_records"})
                completed += 1
        return completed

    def match_tools(self, purpose: str, budget: int, skill: str, data_sensitivity: str) -> list[dict[str, Any]]:
        purpose_categories = {"work":"業務効率化","design":"画像・デザイン","development":"開発者ツール","marketing":"マーケティング","learning":"学習・教育"}
        wanted = purpose_categories.get(purpose)
        ranked = []
        for tool in self.list_tools(sort="popular"):
            score, reasons = 45, []
            if tool["category"] == wanted: score += 28; reasons.append("目的に合うカテゴリ")
            if tool["price_type"] == "free" or tool["price"] <= budget: score += 12; reasons.append("予算内")
            elif tool["price_type"] == "consultation": score += 5
            if skill == "beginner" and tool["passport"]["skill_level"] == "かんたん": score += 9; reasons.append("初めてでも使いやすい")
            if skill == "developer" and tool["passport"]["skill_level"] == "開発者向け": score += 9; reasons.append("開発者向け")
            if data_sensitivity == "high" and tool["passport"]["execution"] == "ローカル": score += 15; reasons.append("データを端末内で処理")
            score += round(tool["passport"]["quality_score"] / 12)
            ranked.append({"tool":tool,"score":min(score,99),"reasons":reasons[:3] or ["評価と実績からおすすめ"]})
        return sorted(ranked,key=lambda x:x["score"],reverse=True)[:4]

    def create_request(self, user: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
        item = {"id":str(uuid4()),"owner_id":user["id"],"owner_name":user["display_name"],"owner_username":user["username"],"status":"open","applications":0,"application_list":[],"created_at":datetime.now(timezone.utc),**values}
        with self._lock:
            self.requests.insert(0,item)
        return item

    def submit_application(self, request_id: str, user: dict[str, Any], message: str, amount: int, delivery_days: int) -> tuple[dict[str, Any], bool]:
        with self._lock:
            item = next((candidate for candidate in self.requests if candidate["id"] == request_id), None)
            if not item or item["status"] != "open" or item["owner_id"] == user["id"]:
                raise ValueError("not applicable")
            owner_username = item.get("owner_username") or self.username_for_id(item["owner_id"]) or ""
            if self.blocked_between(user["id"], user["username"], item["owner_id"], owner_username): raise ValueError("blocked")
            if any(application["applicant_id"] == user["id"] for application in item["application_list"]):
                return item, False
            application = {"id":str(uuid4()),"applicant_id":user["id"],"applicant_name":user["display_name"],"applicant_username":user["username"],"message":message,"amount":amount,"delivery_days":delivery_days,"status":"submitted"}
            item["application_list"].append(application)
            item["applications"] += 1
            return item, True

    def contract_request(self, owner: dict[str, Any], request_id: str, application_id: str, *, payment_pending: bool) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        with self._lock:
            item = next((candidate for candidate in self.requests if candidate["id"] == request_id and candidate["owner_id"] == owner["id"]), None)
            if not item or item["status"] != "open": raise ValueError("not contractable")
            application = next((candidate for candidate in item["application_list"] if candidate["id"] == application_id and candidate["status"] == "submitted"), None)
            if not application: raise ValueError("application unavailable")
            if self.blocked_between(owner["id"], owner["username"], application["applicant_id"], application["applicant_username"]): raise ValueError("blocked")
            created_at = datetime.now(timezone.utc)
            amount = application["amount"]
            seller_account = self.registered_users.get(application["applicant_username"], {})
            order = {"id":str(uuid4()),"tool_slug":"custom-request","tool_name":item["title"],"buyer_id":owner["id"],"buyer_name":owner["display_name"],"buyer_username":owner["username"],"buyer_email":owner.get("email"),"seller_id":application["applicant_id"],"seller_name":application["applicant_name"],"seller_username":application["applicant_username"],"seller_email":seller_account.get("email"),"amount":amount,"base_amount":amount,"primary_payment_amount":amount,"platform_fee":platform_fee_for(amount, custom=True),"platform_fee_rate":settings.custom_fee_rate,"status":"in_progress","payment_status":"pending" if payment_pending else "paid","sales_recorded":False,"messages":[],"delivery":None,"revision_count":0,"cancel_reason":None,"reviewed":False,"buyer_reviewed":False,"is_custom":True,"extras":[],"billing_type":"one_time","license_key":None,"delivery_days":application["delivery_days"],"checkout_cancel_path":f"/requests/{request_id}","created_at":created_at,"updated_at":created_at}
            order["receipt_no"] = f"TB-{created_at.strftime('%Y%m')}-{order['id'][-8:].upper()}"
            application["status"] = "selected"
            item["status"] = "contracted"
            self.orders.insert(0, order)
            return item, application, order

    def rollback_request_contract(self, item: dict[str, Any], application: dict[str, Any], order: dict[str, Any]) -> None:
        with self._lock:
            if order in self.orders and order.get("payment_status") == "pending":
                self.orders.remove(order)
                application["status"] = "submitted"
                item["status"] = "open"

    def available_balance(self, user_id: str) -> int:
        with self._lock:
            completed = [order for order in self.orders if order["seller_id"] == user_id and order["status"] == "completed" and order.get("payment_status", "paid") == "paid" and order.get("refund_status") != "completed" and order.get("dispute_status") != "provider_dispute"]
            paid_out = sum(payout["amount"] for payout in self.payouts if payout["seller_id"] == user_id and payout["status"] in RESERVED_PAYOUT_STATUSES)
            return max(0, sum(order["amount"] - order["platform_fee"] for order in completed) - paid_out)

    def process_expired_payouts(self, user_id: str | None = None, *, at: datetime | None = None) -> list[dict[str, Any]]:
        """Explicit demo job only; GET requests must never trigger fund movement.

        Reserve only aged, still-unallocated earnings, FIFO. No Stripe operation
        is performed. The production ledger/worker is a separate release gate.
        """
        if not settings.demo_mode:
            raise RuntimeError("production payouts require the transactional ledger worker")
        at = at or datetime.now(timezone.utc)
        cutoff = at - timedelta(days=settings.payout_auto_days)
        created: list[dict[str, Any]] = []
        with self._lock:
            seller_ids = {user_id} if user_id else {o["seller_id"] for o in self.orders}
            for seller_id in seller_ids:
                account = self.connected_accounts.get(seller_id, {})
                if account.get("payouts_paused") or not account.get("payouts_enabled") or not account.get("details_submitted"):
                    continue
                def earned_at(order):
                    return order.get("funds_available_at") or order.get("completed_at") or order.get("updated_at") or order["created_at"]
                orders = sorted((o for o in self.orders if o["seller_id"] == seller_id and o["status"] == "completed" and o.get("payment_status", "paid") == "paid" and o.get("refund_status") != "completed" and o.get("dispute_status") != "provider_dispute"), key=earned_at)
                reserved = sum(p["amount"] for p in self.payouts if p["seller_id"] == seller_id and p["status"] in RESERVED_PAYOUT_STATUSES)
                amount = 0
                for order in orders:
                    net = max(0, order["amount"] - order["platform_fee"])
                    allocated = min(reserved, net)
                    reserved -= allocated
                    if earned_at(order) <= cutoff:
                        amount += net - allocated
                if amount < settings.payout_minimum:
                    continue
                payout = self.create_payout(seller_id, amount, at=at)
                payout.update(kind="auto_expired", reason=f"{settings.payout_auto_days}日以上経過した未申請売上（デモ）")
                created.append(payout)
        return created

    def finance_snapshot(self, *, ledger: bool = False) -> dict[str, Any]:
        """Build the operations view from immutable order/payment facts.

        This remains a read model while the demo store is active. The production
        implementation is backed by the financial ledger migration and Stripe
        reconciliation, never by values submitted from a browser.
        """
        with self._lock:
            paid_orders = [order for order in self.orders if order.get("payment_status", "paid") == "paid"]
            valid_orders = [
                order for order in paid_orders
                if order.get("refund_status") != "completed" and order.get("dispute_status") != "provider_dispute"
            ]
            completed_orders = [order for order in valid_orders if order.get("status") == "completed"]
            held_orders = [order for order in valid_orders if order.get("status") not in {"completed", "cancelled"}]
            refund_total = sum(order.get("primary_payment_amount", order.get("amount", 0)) for order in paid_orders if order.get("refund_status") == "completed")
            disputed_total = sum(order.get("primary_payment_amount", order.get("amount", 0)) for order in paid_orders if order.get("refund_status") != "completed" and order.get("dispute_status") == "provider_dispute")
            paid_out_total = sum(payout["amount"] for payout in self.payouts if payout.get("status") in {"processing", "completed", "paid"})

            sellers: dict[str, dict[str, Any]] = {}
            for order in paid_orders:
                seller_id = order["seller_id"]
                seller = sellers.setdefault(seller_id, {
                    "seller_id": seller_id,
                    "seller_name": order.get("seller_name") or order.get("seller_username") or seller_id,
                    "orders": 0,
                    "gross": 0,
                    "platform_fee": 0,
                    "net_earned": 0,
                    "held": 0,
                    "refunded": 0,
                    "disputed": 0,
                    "paid_out": 0,
                    "available": 0,
                })
                seller["orders"] += 1
                amount = order.get("amount", 0)
                fee = order.get("platform_fee", 0)
                if order.get("refund_status") == "completed":
                    seller["refunded"] += order.get("primary_payment_amount", amount)
                elif order.get("dispute_status") == "provider_dispute":
                    seller["disputed"] += order.get("primary_payment_amount", amount)
                elif order.get("status") == "completed":
                    seller["gross"] += amount
                    seller["platform_fee"] += fee
                    seller["net_earned"] += max(0, amount - fee)
                elif order.get("status") != "cancelled":
                    seller["held"] += max(0, amount - fee)

            for payout in self.payouts:
                if payout.get("status") not in {"processing", "completed", "paid"}:
                    continue
                seller = sellers.get(payout["seller_id"])
                if seller:
                    seller["paid_out"] += payout["amount"]

            for seller in sellers.values():
                account = self.connected_accounts.get(seller["seller_id"], {})
                seller["available"] = max(0, seller["net_earned"] - seller["paid_out"])
                seller["connect_status"] = "ready" if account.get("charges_enabled") and account.get("payouts_enabled") else ("reviewing" if account else "not_connected")
                seller["payouts_paused"] = bool(account.get("payouts_paused"))
                seller["payout_eligible"] = seller["available"] if seller["connect_status"] == "ready" and not seller["payouts_paused"] else 0
                seller["blocked_for_payout"] = max(0, seller["available"] - seller["payout_eligible"])

            transactions = []
            for order in sorted(self.orders, key=lambda item: item.get("created_at", now), reverse=True):
                if order.get("refund_status") == "completed":
                    fund_state = "refunded"
                elif order.get("dispute_status") == "provider_dispute":
                    fund_state = "disputed"
                elif order.get("payment_status", "paid") != "paid":
                    fund_state = "payment_pending"
                elif order.get("status") == "completed":
                    seller = sellers.get(order.get("seller_id"), {})
                    fund_state = "available" if seller.get("connect_status") == "ready" and not seller.get("payouts_paused") else "payout_blocked"
                elif order.get("status") == "cancelled":
                    fund_state = "cancelled"
                else:
                    fund_state = "held"
                transactions.append({
                    **order,
                    "seller_net": max(0, order.get("amount", 0) - order.get("platform_fee", 0)),
                    "fund_state": fund_state,
                })

            result = {
                "gross_paid": sum(order.get("amount", 0) for order in paid_orders),
                "completed_gmv": sum(order.get("amount", 0) for order in completed_orders),
                "platform_fee_earned": sum(order.get("platform_fee", 0) for order in completed_orders),
                "processor_fee_total": sum(order.get("processor_fee", 0) for order in completed_orders),
                "seller_payable": sum(max(0, order.get("amount", 0) - order.get("platform_fee", 0)) for order in completed_orders),
                "held": sum(max(0, order.get("amount", 0) - order.get("platform_fee", 0)) for order in held_orders),
                "paid_out": paid_out_total,
                "payout_fee_total": sum(payout.get("fee", 0) for payout in self.payouts if payout.get("status") in {"processing", "completed", "paid"}),
                "refund_total": refund_total,
                "disputed_total": disputed_total,
                "available_to_payout": sum(seller["payout_eligible"] for seller in sellers.values()),
                "blocked_for_payout": sum(seller["blocked_for_payout"] for seller in sellers.values()),
                "sellers": sorted(sellers.values(), key=lambda item: (item["available"], item["gross"]), reverse=True),
                "transactions": transactions,
                "payouts": sorted(self.payouts, key=lambda item: item.get("created_at", now), reverse=True),
            }
            if ledger:
                from .finance import summary
                return summary(self, result)
            return result

    def set_seller_payout_hold(self, seller_id: str, paused: bool) -> dict[str, Any]:
        with self._lock:
            account = self.connected_accounts.setdefault(seller_id, {})
            account["payouts_paused"] = paused
            account["payouts_paused_at"] = datetime.now(timezone.utc) if paused else None
            return account

    def create_payout(self, user_id: str, amount: int, *, at: datetime | None = None) -> dict[str, Any]:
        if not settings.demo_mode:
            raise RuntimeError("production payouts require the transactional ledger worker")
        with self._lock:
            if self.connected_accounts.get(user_id, {}).get("payouts_paused"):
                raise ValueError("payouts paused")
            fee = payout_fee(amount, settings)
            if amount > self.available_balance(user_id): raise ValueError("invalid amount")
            created_at = at or datetime.now(timezone.utc)
            payout = {"id":str(uuid4()),"seller_id":user_id,"amount":amount,"fee":fee,"net_amount":amount-fee,"kind":"requested","status":"processing","created_at":created_at,"scheduled_for":scheduled_payout_date(created_at),"payout_weekday":settings.payout_weekday_label}
            self.payouts.insert(0, payout)
            return payout

store = DemoStore(seed=settings.demo_mode)
