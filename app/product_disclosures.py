"""Seller-authored facts. Missing values never imply a license or free API use."""
from copy import deepcopy
from datetime import datetime, timezone

from fastapi import HTTPException

from .web import clean_visible_text

DISCLOSURE_FIELDS = {
    "audience": ("おすすめの利用者", 500),
    "deliverables": ("購入者へ渡すもの", 1000),
    "requirements": ("動作環境・必要なアカウント", 1000),
    "additional_costs": ("外部APIなどの追加費用", 1000),
    "license": ("利用ライセンス・利用人数", 1000),
    "data_handling": ("外部へのデータ送信・保存", 1000),
    "support_scope": ("サポート範囲・対象外", 1000),
}
PERMISSION_LABELS = {"unknown": "未記載・購入前に出品者へ確認", "allowed": "可（記載された条件の範囲内）", "not_allowed": "不可", "conditional": "条件付き（ライセンス欄を確認）"}
RIGHTS_VERSION = "2026-10-25-v1"
RIGHTS_STATEMENT = "この商品を販売・提供する権限を持ち、第三者の著作権その他の権利を侵害せず、使用するOSS・素材・外部AI/APIの規約に従うことを確認しました。"


def display_disclosures(tool):
    """Reuse explicit legacy descriptions without treating placeholder defaults as facts."""
    result = {}
    passport = tool.get("passport", {})
    placeholders = {"", "未設定", "購入前に確認", "販売者に確認してください"}
    for key in ("requirements", "license", "data_handling"):
        value = passport.get(key, "")
        if value not in placeholders:
            result[key] = value
    if "license" in result and isinstance(passport.get("commercial_use"), bool):
        result["commercial_use"] = "allowed" if passport["commercial_use"] else "not_allowed"
    result.update(tool.get("purchase_disclosures", {}))
    return result


def parse_disclosures(form, existing=None, legacy=None):
    """Omitted fields preserve old clients' data; explicitly blank fields clear it."""
    result = deepcopy(existing or {})
    displayed = display_disclosures(legacy) if legacy is not None else result.copy()
    for key, (label, maximum) in DISCLOSURE_FIELDS.items():
        field = f"disclosure_{key}"
        if field in form:
            value = clean_visible_text(str(form[field]), 0, maximum, label, preserve_lines=True)
            if value == displayed.get(key, ""):
                continue
            if value or key in displayed:
                result[key] = value
            else:
                result.pop(key, None)
    for key in ("commercial_use", "resale"):
        field = f"disclosure_{key}"
        if field in form:
            value = str(form[field])
            if value not in PERMISSION_LABELS:
                raise HTTPException(422, "商用利用・再販売の選択値が不正です")
            if value == displayed.get(key, "unknown"):
                continue
            if value == "unknown" and key not in displayed:
                result.pop(key, None)
            else:
                result[key] = value
    return result


def record_rights(store, tool, user):
    at = datetime.now(timezone.utc)
    record = {"user_id": user["id"], "document_type": "listing_rights", "version": RIGHTS_VERSION,
              "statement": RIGHTS_STATEMENT, "tool_id": tool.get("id"), "tool_slug": tool["slug"], "created_at": at}
    store.legal_consents.append(record)
    tool["rights_confirmation"] = {"version": RIGHTS_VERSION, "confirmed_at": at, "user_id": user["id"]}


def purchase_snapshot(tool):
    """Capture what the buyer saw; do not silently rewrite older purchases."""
    return {"version": 1, "captured_at": datetime.now(timezone.utc),
            "disclosures": deepcopy(tool.get("purchase_disclosures", {})),
            "legacy_passport": deepcopy(tool.get("passport", {})),
            "description_md": tool.get("description_md", ""),
            "purchase_notes": tool.get("purchase_notes", ""),
            "support_days": tool.get("support_days"),
            "estimated_delivery_days": tool.get("estimated_delivery_days"),
            "distribution": tool.get("distribution"), "fulfillment_type": tool.get("fulfillment_type")}
