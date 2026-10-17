"""Provider identity status only. Never store documents or biometric outputs."""
from datetime import datetime, timezone
from urllib.parse import urlparse


IDENTITY_STATES = {
    "verified": "verified", "requires_input": "rejected",
    "processing": "pending", "canceled": "rejected",
}


def verification_url(session: dict, *, user_id: str, reference: str) -> str:
    url = session.get("url") or ""
    if not isinstance(url, str) or not isinstance(session.get("metadata"), dict):
        raise RuntimeError("本人確認画面を確認できませんでした")
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        raise RuntimeError("本人確認画面を確認できませんでした") from None
    if (session.get("id") != reference
            or (session.get("metadata") or {}).get("user_id") != user_id
            or parsed.scheme != "https" or parsed.hostname != "verify.stripe.com"
            or parsed.username or parsed.password or port not in (None, 443)):
        raise RuntimeError("本人確認画面を確認できませんでした")
    return url


def apply_identity_event(application: dict, event: dict) -> None:
    obj = event["data"]["object"]
    status = obj["status"]
    # Stripe retries and delivery order can differ from event creation order.
    # A verified session cannot return to processing/requires_input.
    if application.get("status") == "verified" and status != "verified":
        return
    created = event.get("created", 0)
    if type(created) is not int or created < application.get("provider_event_created", 0):
        return
    application["provider_event_created"] = created
    application["provider_status"] = status
    application["status"] = IDENTITY_STATES[status]
    application["rejection_reason"] = None
    if status == "verified":
        application["verified_at"] = datetime.now(timezone.utc)
    elif status == "requires_input":
        application["rejection_reason"] = "確認を完了できませんでした。本人確認画面で案内を確認して、再提出してください。"
    elif status == "canceled":
        application["rejection_reason"] = "本人確認がキャンセルされました。再度手続きを開始してください。"
