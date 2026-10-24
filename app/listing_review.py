"""Manual listing review evidence, independent of delivery malware scanning."""
import hashlib
import json


REVIEW_CHECKS = {
    "description": "商品説明・画像・デモを確認し、説明と提供内容に矛盾がない",
    "rights": "禁止出品物・販売権限・第三者ライセンスについて、掲載情報と必要な確認を行った",
    "data": "外部サービスへの送信・個人情報の扱い・必要な権限が説明されている（該当なしの場合も確認）",
    "terms": "価格・納品方法・納期・利用条件・サポート範囲が購入前に分かる",
}
REVIEW_LABELS = {"pending": "運営の掲載審査待ち", "passed": "掲載審査承認済み", "failed": "修正・再申請が必要"}
REVIEW_FIELDS = (
    "name", "tagline", "description_md", "thumbnail_url", "category", "tags",
    "distribution", "price_type", "price", "fulfillment_type", "purchase_notes",
    "faq", "demo_url", "ai_used", "support_days", "estimated_delivery_days",
)


def content_fingerprint(tool):
    payload = {key: tool.get(key) for key in REVIEW_FIELDS}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def review_token(tool):
    payload = {"content": content_fingerprint(tool), "review": tool.get("safety_scan", {})}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def reopen_review(tool, at, *, reason):
    previous = tool.get("safety_scan", {})
    tool["safety_scan"] = {
        "status": "pending", "label": REVIEW_LABELS["pending"], "method": "manual",
        "checked_at": None, "requested_at": at.isoformat(), "reason": reason,
        "previous_note": previous.get("note", previous.get("previous_note", "")),
    }
    tool["is_published"] = False
    if tool.get("status") == "published":
        tool["status"] = "paused"
    tool.setdefault("passport", {})["security"] = REVIEW_LABELS["pending"]
