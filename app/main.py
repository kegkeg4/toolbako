from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse
from xml.sax.saxutils import escape as xml_escape

import httpx
import markdown
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from PIL import Image

try:
    import nh3
except ImportError:  # Local compatibility; production installs nh3 from requirements.txt.
    nh3 = None
    import bleach

from .config import settings
from .data import AI_OPTIONS, CATEGORIES, DIST_LABELS, PRICE_LABELS, TRANSFER_ASSET_LABELS, TRANSFER_STATUS_LABELS, store
from .ogp import generate_og, generate_site_og
from .middleware import ProductionGuardMiddleware
from .monitoring import initialize_monitoring
from .production import readiness_summary
from .persistence import SQLiteStateStore
from .database import DatabaseBoundaryMiddleware, PostgresStateStore
from .integrations import EmailIntegration, StripeIntegration, StripeOutcomeUnknown
from .files import clamav_scan, validate_delivery_file
from .security import SessionService, hash_password, verify_password
from .web import clean_visible_text, safe_http_url, safe_next, slugify
from .webhooks import StripeWebhookService
from .mfa import SupabaseMFA, is_privileged_path
from .finance import FinanceConflict, PAYOUT_LABELS, balance as ledger_balance, request_payout
from .payout_worker import sandbox_payouts_ready
from .refunds import request_order_refunds
from .reconciliation import propose_recovery, approve_recovery
from .deployment_check import configuration_checks
from .supabase_api import supabase_headers

ROOT = Path(__file__).resolve().parent.parent


async def require_privileged_mfa(request: Request):
    if not (settings.is_production or settings.privileged_mfa_required):
        return
    if not is_privileged_path(request.url.path, request.method):
        return
    user = current_user(request)
    if user and not mfa.recent(request):
        destination = request.url.path if request.method == "GET" else "/security"
        raise HTTPException(303, "重要操作の前に2段階認証をお願いします", headers={"Location": f"/security/mfa?{urlencode({'next': destination})}"})


database_store = PostgresStateStore(settings.database_url, production=settings.is_production) if settings.database_url else None
app = FastAPI(title="ツールバコ", description="AIツールの共有・発見プラットフォーム", version="0.1.0", dependencies=[Depends(require_privileged_mfa)])
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, session_cookie="toolbako_session", same_site="lax", https_only=settings.site_base_url.startswith("https"), max_age=settings.session_max_age)
app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)
if database_store or settings.is_production:
    app.add_middleware(DatabaseBoundaryMiddleware, backend=database_store, store=store)
app.add_middleware(ProductionGuardMiddleware, settings=settings)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts) if settings.allowed_hosts else ["*"])
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")
state_store = SQLiteStateStore(settings.state_db_path if not database_store and not settings.is_production else "")
state_store.restore(store)
for restored_tool in store.tools:
    restored_tool.setdefault("customization_available", restored_tool.get("price_type") != "free")
    restored_tool.setdefault("exclusive_available", False)
    restored_tool.setdefault("transfer_review_status", "approved" if restored_tool.get("exclusive_available") else "not_requested")
    restored_tool.setdefault("exclusive_price_min", 0)
    restored_tool.setdefault("transfer_assets", [])
    restored_tool.setdefault("tech_stack", "")
    restored_tool.setdefault("monthly_revenue", 0)
    restored_tool.setdefault("monthly_profit", 0)
    restored_tool.setdefault("monthly_cost", 0)
    restored_tool.setdefault("weekly_ops_hours", 0)
    restored_tool.setdefault("handover_days", 14)
    restored_tool.setdefault("exclusive_summary", "")
for restored_username, restored_account in store.registered_users.items():
    demo_credential = bool(settings.demo_mode and restored_username == "demo_creator")
    restored_account.setdefault("is_founding_member", demo_credential)
    restored_account.setdefault("founding_member_since", restored_account.get("created_at") if demo_credential else None)
    restored_account.setdefault("is_certified_creator", demo_credential)
    restored_account.setdefault("certified_creator_since", restored_account.get("created_at") if demo_credential else None)
session_service = SessionService(store, settings.session_max_age)
mfa = SupabaseMFA(settings, session_service)


async def persist_state() -> None:
    # Serializing the complete compatibility snapshot can be expensive. Never do
    # this synchronous disk work on FastAPI's event loop.
    if database_store:
        await database_store.save()
    else:
        await asyncio.to_thread(state_store.save, store)


# The database boundary commits all requests. The legacy demo middleware only
# persists successful writes. Never commit a second time after releasing the DB.
app.state.persist = None if database_store else persist_state
stripe = StripeIntegration(settings.stripe_secret_key, settings.site_base_url, api_version=settings.stripe_api_version, charge_mode=settings.stripe_charge_mode, journal=database_store)
email_service = EmailIntegration(settings.email_api_key, settings.email_from)
DEFAULT_OG_PATH = generate_site_og()
TRANSFER_NDA_TEXT = "当事者は独占譲渡案件で開示される非公開情報を案件評価以外に使用せず、第三者へ開示せず、交渉終了時は合理的な範囲で破棄します。法令・裁判所・行政機関により開示が必要な場合を除きます。"
TRANSFER_NDA_DOCUMENT_HASH = hashlib.sha256(TRANSFER_NDA_TEXT.encode("utf-8")).hexdigest()


async def send_email_safely(to: str | None, subject: str, text: str) -> bool:
    """DB mode queues mail atomically with the transaction; a worker sends it."""
    if not settings.email_ready or not to:
        return False
    if database_store:
        database_store.enqueue_email(to, settings.email_from, subject, text)
        return True
    try:
        return await email_service.send(to, subject, text)
    except Exception:
        return False


async def sweep_due_orders() -> int:
    """Keep demo/staging lifecycle behavior deterministic without a cron worker."""
    changed = store.process_due_order_events()
    if changed:
        await persist_state()
    return changed

initialize_monitoring(settings)


establish_session = session_service.establish
update_current_session = session_service.update
revoke_user_sessions = session_service.revoke_user
current_user = session_service.current

PROFILE_CACHE_FIELDS = (
    "id", "email", "display_name", "avatar_url", "headline", "bio", "skills", "experience", "portfolio",
    "availability", "response_time", "pricing_note", "x_url", "website_url", "is_verified", "identity_status",
    "is_founding_member", "founding_member_since", "is_certified_creator", "certified_creator_since",
)


def creator_badges_for(username: str) -> dict[str, object]:
    account = store.registered_users.get(username) or {}
    return {
        "is_founding_member": bool(account.get("is_founding_member")),
        "founding_member_since": account.get("founding_member_since"),
        "is_certified_creator": bool(account.get("is_certified_creator")),
        "certified_creator_since": account.get("certified_creator_since"),
    }


def profile_badge_values(profile: dict) -> dict[str, object]:
    """Read the operator-only credential row embedded by PostgREST."""
    embedded = profile.get("creator_badges") or {}
    if isinstance(embedded, list):
        embedded = embedded[0] if embedded else {}
    return {
        "is_founding_member": bool(embedded.get("is_founding_member")),
        "founding_member_since": embedded.get("founding_member_since"),
        "is_certified_creator": bool(embedded.get("is_certified_creator")),
        "certified_creator_since": embedded.get("certified_creator_since"),
    }


templates.env.globals["creator_badges_for"] = creator_badges_for


def seo_metadata(request: Request) -> dict:
    path = request.url.path
    private_prefixes = (
        "/mypage", "/orders/", "/messages", "/seller", "/admin", "/settings", "/security",
        "/verification", "/library", "/payouts", "/purchases", "/subscriptions", "/notifications",
        "/updates", "/account/", "/checkout/", "/auth/", "/transfer-inquiries/",
    )
    private_paths = {"/login", "/signup", "/tools/new", "/requests/new", "/nda"}
    robots_value = "noindex, nofollow" if path.startswith(private_prefixes) or path in private_paths else "index, follow, max-image-preview:large"
    canonical_path = path.rstrip("/") or "/"
    query = request.query_params
    if path == "/tools":
        meaningful_category = query.get("category", "") if query.get("category", "") in CATEGORIES else ""
        noisy = any(query.get(key) for key in ("q", "price_type", "ai")) or query.get("sort", "new") != "new"
        if noisy:
            robots_value = "noindex, follow"
        elif meaningful_category:
            canonical_path = f"/tools?category={quote(meaningful_category)}"
    elif path == "/creators":
        if any(query.get(key) for key in ("q", "category", "verified", "badge", "sort")):
            robots_value = "noindex, follow"
    elif path == "/requests" and query.get("category") in CATEGORIES:
        canonical_path = f"/requests?category={quote(query['category'])}"
    elif query:
        # Operational flags, tabs and comparison combinations do not create new indexable pages.
        canonical_path = path.rstrip("/") or "/"
    canonical_url = f"{settings.site_base_url}{canonical_path}"
    # Preview hosts must not compete with the real site in search results.
    # Keep crawling enabled so search engines can actually read this directive.
    if not settings.is_production:
        robots_value = "noindex, nofollow"
    return {
        "preview_mode": not settings.is_production,
        "canonical_url": canonical_url,
        "robots_value": robots_value,
        "og_image_url": f"{settings.site_base_url}{DEFAULT_OG_PATH}",
        "seo_jsonld": [
            {"@context":"https://schema.org","@type":"Organization","name":settings.legal_business_name or "ツールバコ","url":settings.legal_website or settings.site_base_url,"brand":{"@type":"Brand","name":"ツールバコ","logo":f"{settings.site_base_url}/static/brand/toolbako-logo-full.png"}},
            {"@context":"https://schema.org","@type":"WebSite","name":"ツールバコ","url":settings.site_base_url,"publisher":{"@type":"Organization","name":settings.legal_business_name or "ツールバコ","url":settings.legal_website or settings.site_base_url},"potentialAction":{"@type":"SearchAction","target":f"{settings.site_base_url}/tools?q={{search_term_string}}","query-input":"required name=search_term_string"}},
        ],
    }


def context(request: Request, **values):
    status_labels = {"in_progress":"取引中","awaiting_acceptance":"納品確認待ち","completed":"取引完了","cancel_pending":"キャンセル確認中","cancelled":"キャンセル済み"}
    user = current_user(request)
    unread_notification_count = sum(1 for item in store.notifications if user and item.get("user_id") == user["id"] and not item.get("read"))
    legal_operator = {"name":settings.legal_business_name,"representative":settings.legal_representative,"address":settings.legal_address,"phone":settings.legal_phone,"email":settings.support_email,"website":settings.legal_website,"invoice_number":settings.legal_invoice_number}
    return {"request": request, "user": user, "admin_access":is_admin(user), "unread_notification_count":unread_notification_count, "categories": CATEGORIES, "ai_options": AI_OPTIONS, "price_labels": PRICE_LABELS, "dist_labels": DIST_LABELS, "status_labels": status_labels, "transfer_asset_labels":TRANSFER_ASSET_LABELS, "transfer_status_labels":TRANSFER_STATUS_LABELS, "site_url": settings.site_base_url, "asset_version":settings.asset_version, "csp_nonce":getattr(request.state, "csp_nonce", ""), "demo_mode":settings.demo_mode, "email_ready":settings.email_ready, "stripe_ready":settings.stripe_ready, "platform_fee_percent":round(settings.platform_fee_rate * 100, 2), "custom_fee_percent":round(settings.custom_fee_rate * 100, 2), "payout_schedule_label":settings.payout_schedule_label, "payout_minimum":settings.payout_minimum, "payout_fee":settings.payout_fee, "payout_fee_free_threshold":settings.payout_fee_free_threshold, "payout_auto_days":settings.payout_auto_days, "payout_weekday_label":settings.payout_weekday_label, "legal_operator":legal_operator, **seo_metadata(request), **values}


def render_md(value: str) -> str:
    raw = markdown.markdown(value, extensions=["fenced_code", "tables"])
    allowed_tags = {"p","h1","h2","h3","ul","ol","li","strong","em","a","code","pre","blockquote","table","thead","tbody","tr","th","td","br"}
    allowed_attributes = {"a":{"href","title"}}
    if nh3:
        return nh3.clean(raw, tags=allowed_tags, attributes=allowed_attributes, url_schemes={"http","https","mailto"}, link_rel="noopener noreferrer")
    return bleach.clean(raw, tags=allowed_tags, attributes={"a":["href","title","rel"]}, protocols={"http","https","mailto"})


templates.env.filters["markdown"] = render_md


def wants_html_error(request: Request) -> bool:
    if request.url.path.startswith(("/api/", "/webhooks/", "/healthz", "/readyz", "/deploymentz")):
        return False
    content_type = request.headers.get("content-type", "")
    return request.method == "GET" or "text/html" in request.headers.get("accept", "") or content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data"))


@app.exception_handler(StarletteHTTPException)
async def friendly_http_error(request: Request, exc: StarletteHTTPException):
    if not wants_html_error(request):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
    return templates.TemplateResponse(request, "error.html", context(request, status_code=exc.status_code, error_message=str(exc.detail or "操作を完了できませんでした")), status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def friendly_validation_error(request: Request, exc: RequestValidationError):
    if not wants_html_error(request):
        return JSONResponse({"detail": jsonable_encoder(exc.errors())}, status_code=422)
    return templates.TemplateResponse(request, "error.html", context(request, status_code=422, error_message="入力内容を確認してください。必須項目または形式に誤りがあります。"), status_code=422)


@app.exception_handler(StripeOutcomeUnknown)
async def stripe_outcome_unknown(request: Request, exc: StripeOutcomeUnknown):
    # Preserve the existing order/extra/contract reservation. A timeout is not
    # evidence of a failed payment and must not allow a fresh purchase ID.
    user = current_user(request)
    for order in store.orders:
        if exc.operation_id == f"checkout-{order['id']}":
            order["payment_reconciliation_required"] = True
    store.audit(user["id"] if user else None, "stripe.reconciliation_required", exc.operation_id)
    await persist_state()
    message = "決済サービスからの応答を確認できませんでした。重複決済を防ぐため注文を保留しています。再購入せず、購入履歴を確認してサポートへお問い合わせください。"
    return await friendly_http_error(request, HTTPException(502, message))


def available_balance(user_id: str) -> int:
    if database_store:
        return ledger_balance(store, user_id)
    return store.available_balance(user_id)


def own_tool(user: dict, slug: str) -> dict:
    tool = store.get(slug)
    if not tool: raise HTTPException(404, "商品が見つかりません")
    if tool.get("author_id") != user["id"]: raise HTTPException(403, "この商品は編集できません")
    return tool


def identity_verified(user: dict | None) -> bool:
    if not user: return False
    if user.get("is_verified") or user.get("identity_status") == "verified": return True
    return identity_verified_by_id(user["id"])


def identity_verified_by_id(user_id: str) -> bool:
    application = store.identity_applications.get(user_id)
    if application and application.get("status") == "verified": return True
    account = next((item for item in store.registered_users.values() if item.get("id") == user_id), None)
    return bool(account and (account.get("is_verified") or account.get("identity_status") == "verified"))


def can_offer_exclusive_transfer(user: dict) -> bool:
    return identity_verified(user) or (settings.demo_mode and user.get("username") == "demo_creator")


def validated_transfer_settings(
    enabled: str, price_min: int, assets: list[str], tech_stack: str,
    monthly_revenue: int, monthly_profit: int, monthly_cost: int,
    weekly_ops_hours: float, handover_days: int, summary: str, rights_agreement: str,
) -> dict:
    if enabled != "yes":
        return {"exclusive_available":False,"exclusive_price_min":0,"transfer_assets":[],"tech_stack":"","monthly_revenue":0,"monthly_profit":0,"monthly_cost":0,"weekly_ops_hours":0,"handover_days":14,"exclusive_summary":""}
    if rights_agreement != "yes": raise HTTPException(422, "独占譲渡する権利の確認が必要です")
    if not 10_000 <= price_min <= 100_000_000: raise HTTPException(422, "独占譲渡の希望価格は1万円〜1億円で入力してください")
    if not assets or len(assets) > len(TRANSFER_ASSET_LABELS) or any(asset not in TRANSFER_ASSET_LABELS for asset in assets): raise HTTPException(422, "譲渡対象を1つ以上選択してください")
    if not all(0 <= value <= 100_000_000 for value in (monthly_revenue, monthly_profit, monthly_cost)): raise HTTPException(422, "月次の金額は0円〜1億円で入力してください")
    if monthly_profit > monthly_revenue: raise HTTPException(422, "月間利益は月間売上以下で入力してください")
    if not 0 <= weekly_ops_hours <= 168 or not 1 <= handover_days <= 180: raise HTTPException(422, "運営時間または引き継ぎ期間を確認してください")
    tech_stack = clean_visible_text(tech_stack, 2, 300, "技術構成")
    summary = clean_visible_text(summary, 20, 1000, "独占譲渡の説明", preserve_lines=True)
    return {"exclusive_available":True,"exclusive_price_min":price_min,"transfer_assets":list(dict.fromkeys(assets)),"tech_stack":tech_stack,"monthly_revenue":monthly_revenue,"monthly_profit":monthly_profit,"monthly_cost":monthly_cost,"weekly_ops_hours":weekly_ops_hours,"handover_days":handover_days,"exclusive_summary":summary}


def is_admin(user: dict | None) -> bool:
    return bool(user and (user["id"] in settings.admin_user_ids or (settings.demo_mode and user["username"] == "demo_creator")))


async def save_thumbnail(upload: UploadFile | None, slug: str) -> str | None:
    if not upload or not upload.filename: return None
    if upload.content_type not in {"image/jpeg","image/png","image/webp"}: raise HTTPException(422, "商品画像はJPEG・PNG・WebPのみ対応しています")
    content = await upload.read(5_000_001)
    if len(content) > 5_000_000: raise HTTPException(422, "商品画像は5MB以下にしてください")
    try:
        image = Image.open(io.BytesIO(content))
        if image.format not in {"JPEG", "PNG", "WEBP"}: raise ValueError("unsupported image format")
        image.verify()
        image = Image.open(io.BytesIO(content))
        if image.width * image.height > 25_000_000: raise ValueError("image dimensions too large")
        image = image.convert("RGB")
        image.thumbnail((1200, 900))
    except Exception as exc:
        raise HTTPException(422, "商品画像を読み込めませんでした") from exc
    upload_dir = ROOT / "static" / "uploads"; upload_dir.mkdir(parents=True, exist_ok=True)
    path = upload_dir / f"{slug}.webp"; image.save(path, "WEBP", quality=88, method=6)
    return f"/static/uploads/{slug}.webp"


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    tools = store.list_tools()
    trending = store.list_tools(sort="trending")[:5]
    picks = {c: next((t for t in tools if t["category"] == c), None) for c in CATEGORIES}
    return templates.TemplateResponse(request, "home.html", context(request, trending=trending, newest=tools[:6], category_picks=picks, curated=store.get_public("oyatsu-gacha")))


@app.head("/", include_in_schema=False)
async def home_head():
    """Allow lightweight uptime checks without rendering the homepage."""
    return Response(status_code=200)


@app.get("/tools", response_class=HTMLResponse)
async def tools_list(request: Request, q: str = "", category: str = "", price_type: str = "", ai: str = "", sort: str = "new"):
    q = clean_visible_text(q, 0, 100, "検索語")
    category = category if category in CATEGORIES else ""
    price_type = price_type if price_type in PRICE_LABELS else ""
    ai = ai if ai in AI_OPTIONS else ""
    sort = sort if sort in {"new", "popular", "trending"} else "new"
    items = store.list_tools(q, category, price_type, ai, sort)
    return templates.TemplateResponse(request, "tools.html", context(request, tools=items, q=q, selected_category=category, selected_price=price_type, selected_ai=ai, sort=sort))


@app.get("/transfers", response_class=HTMLResponse)
async def transfer_market(request: Request):
    tools = [item for item in store.list_tools(sort="trending") if store.transfer_is_public(item)]
    user = current_user(request)
    inquiries = [item for item in store.transfer_inquiries if user and user["id"] in {item["buyer_id"], item["seller_id"]}]
    item_list_schema = {"@context":"https://schema.org","@type":"ItemList","name":"AIツールの独占譲渡案件","itemListElement":[{"@type":"ListItem","position":index+1,"name":tool["name"],"url":f"{settings.site_base_url}/tools/{tool['slug']}/transfer"} for index,tool in enumerate(tools)]}
    return templates.TemplateResponse(request, "transfers.html", context(request, tools=tools, inquiries=inquiries, seo_jsonld=seo_metadata(request)["seo_jsonld"]+[item_list_schema]))


@app.get("/match", response_class=HTMLResponse)
async def match_form(request: Request):
    return templates.TemplateResponse(request, "match.html", context(request, results=None))


@app.post("/match", response_class=HTMLResponse)
async def match_result(request: Request, purpose: str = Form(...), budget: int = Form(...), skill: str = Form(...), data_sensitivity: str = Form(...)):
    if purpose not in {"work","design","development","marketing","learning"} or skill not in {"beginner","standard","developer"} or data_sensitivity not in {"normal","high"}:
        raise HTTPException(422, "診断条件が不正です")
    results = store.match_tools(purpose, max(0, min(budget, 1_000_000)), skill, data_sensitivity)
    return templates.TemplateResponse(request, "match.html", context(request, results=results, selected={"purpose":purpose,"budget":budget,"skill":skill,"data_sensitivity":data_sensitivity}))


@app.get("/compare", response_class=HTMLResponse)
async def compare_tools(request: Request, slugs: str = "minutes-magic,commit-senpai,pixel-recipe"):
    selected = []
    for slug in [x.strip() for x in slugs.split(",") if x.strip()][:3]:
        item = store.get_public(slug)
        if item and item not in selected: selected.append(item)
    return templates.TemplateResponse(request, "compare.html", context(request, tools=selected, all_tools=store.list_tools(sort="popular")))


@app.get("/updates", response_class=HTMLResponse)
async def update_feed(request: Request):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/updates", 303)
    followed = [store.get(slug) for uid,slug in store.update_followers if uid == user["id"]]
    followed = [x for x in followed if x]
    return templates.TemplateResponse(request, "updates.html", context(request, tools=followed))


@app.get("/tools/new", response_class=HTMLResponse)
async def tool_new(request: Request):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/tools/new", 303)
    return templates.TemplateResponse(request, "tool_form.html", context(request, can_offer_exclusive=can_offer_exclusive_transfer(user)))


@app.post("/tools/new")
async def tool_create(
    request: Request, name: str = Form(...), tagline: str = Form(...), description_md: str = Form(...),
    category: str = Form(...), tags: str = Form(""), distribution: str = Form(...), price_type: str = Form(...),
    price: int = Form(0), fulfillment_type: str = Form("custom"), estimated_delivery_days: int = Form(1), purchase_notes: str = Form(""),
    faq_question: list[str] = Form(default=[]), faq_answer: list[str] = Form(default=[]), demo_url: str = Form(""),
    external_pay_url: str = Form(""), ai_used: list[str] = Form(default=[]),
    customization_available: str = Form("no"), exclusive_available: str = Form("no"), exclusive_price_min: int = Form(0),
    transfer_assets: list[str] = Form(default=[]), tech_stack: str = Form(""), monthly_revenue: int = Form(0),
    monthly_profit: int = Form(0), monthly_cost: int = Form(0), weekly_ops_hours: float = Form(0), handover_days: int = Form(14),
    exclusive_summary: str = Form(""), transfer_rights_agreement: str = Form("no"),
    prohibited_agreement: str = Form(...), thumbnail: UploadFile | None = None,
):
    user = current_user(request)
    if not user: return RedirectResponse("/login", 303)
    name = clean_visible_text(name, 1, 60, "商品名")
    tagline = clean_visible_text(tagline, 1, 100, "ひとこと説明")
    description_md = clean_visible_text(description_md, 1, 5000, "商品説明", preserve_lines=True)
    purchase_notes = clean_visible_text(purchase_notes, 0, 2000, "購入にあたってのお願い", preserve_lines=True)
    if category not in CATEGORIES or price_type not in PRICE_LABELS or distribution not in DIST_LABELS or fulfillment_type not in {"instant","custom"} or prohibited_agreement != "yes": raise HTTPException(422, "選択値が不正です")
    if any(ai not in AI_OPTIONS for ai in ai_used): raise HTTPException(422, "使用AIの選択値が不正です")
    demo_url = safe_http_url(demo_url)
    tag_list = [clean_visible_text(value.strip().lstrip("#"), 1, 24, "タグ") for value in tags.split(",") if value.strip()][:5]
    base = slugify(name); slug = base; suffix = 2
    while store.get(slug): slug, suffix = f"{base}-{suffix}", suffix + 1
    if price_type == "paid" and not 100 <= price <= 1_000_000: raise HTTPException(422, "価格は100円〜100万円で設定してください")
    if not 1 <= estimated_delivery_days <= 120: raise HTTPException(422, "納品目安は1〜120日で入力してください")
    if len(faq_question) > 5 or len(faq_answer) > 5: raise HTTPException(422, "よくある質問は5件以内で入力してください")
    faq = []
    for index in range(max(len(faq_question), len(faq_answer))):
        question = clean_visible_text(faq_question[index] if index < len(faq_question) else "", 0, 120, "よくある質問")
        answer = clean_visible_text(faq_answer[index] if index < len(faq_answer) else "", 0, 500, "よくある質問の回答", preserve_lines=True)
        if bool(question) != bool(answer): raise HTTPException(422, "よくある質問は質問と回答をセットで入力してください")
        if question: faq.append({"question":question,"answer":answer})
    thumbnail_url = await save_thumbnail(thumbnail, slug)
    if price_type == "consultation": fulfillment_type = "custom"
    transfer_settings = validated_transfer_settings(exclusive_available, exclusive_price_min, transfer_assets, tech_stack, monthly_revenue, monthly_profit, monthly_cost, weekly_ops_hours, handover_days, exclusive_summary, transfer_rights_agreement)
    if transfer_settings["exclusive_available"] and not can_offer_exclusive_transfer(user): raise HTTPException(403, "独占譲渡の出品には本人確認が必要です")
    item = store.create({"slug":slug,"name":name,"tagline":tagline,"description_md":description_md,"category":category,"price_type":price_type,"price":price if price_type == "paid" else 0,"distribution":distribution,"fulfillment_type":fulfillment_type,"estimated_delivery_days":estimated_delivery_days,"purchase_notes":purchase_notes,"faq":faq,"demo_url":demo_url,"external_pay_url":"","ai_used":ai_used[:6],"tags":tag_list,"author_name":user["display_name"],"author_username":user["username"],"author_id":user["id"],"author_email":user.get("email"),"thumbnail_url":thumbnail_url,"customization_available":customization_available == "yes" or price_type == "consultation","transfer_review_status":"pending" if transfer_settings["exclusive_available"] else "not_requested",**transfer_settings})
    generate_og(slug, name, tagline, user["username"])
    return RedirectResponse(f"/tools/{item['slug']}?created=1", 303)


@app.get("/tools/{slug}", response_class=HTMLResponse)
async def tool_detail(request: Request, slug: str):
    item = store.get_public(slug)
    viewer = current_user(request)
    is_preview = False
    is_unavailable = False
    if not item:
        candidate = store.get(slug)
        if candidate and candidate.get("status") == "paused" and candidate.get("safety_scan", {}).get("status") == "passed":
            item = candidate
            is_unavailable = True
        elif not candidate or not viewer or candidate.get("author_id") != viewer["id"]:
            raise HTTPException(404, "ツールが見つかりません")
        else:
            item = candidate
            is_preview = True
    seen = request.session.setdefault("seen_tools", [])
    if slug not in seen:
        item["view_count"] += 1
        request.session["seen_tools"] = (seen + [slug])[-100:]
    og_path = generate_og(slug, item["name"], item["tagline"], item["author_username"])
    related = [x for x in store.list_tools(category=item["category"], sort="popular") if x["slug"] != slug][:4]
    share_text = quote(f'「{item["name"]}」を見つけました！ {settings.site_base_url}/tools/{slug} #ツールバコ')
    user = viewer
    liked = bool(user and (user["id"], slug) in store.likes)
    following = bool(user and (user["id"], slug) in store.update_followers)
    tool_reviews = sorted([review for review in store.reviews if review.get("tool_slug") == slug and review.get("published", True)], key=lambda review:review["created_at"], reverse=True)
    review_count = len(tool_reviews)
    product_schema = {
        "@context":"https://schema.org", "@type":"SoftwareApplication", "name":item["name"],
        "description":item["tagline"], "applicationCategory":item["category"],
        "operatingSystem":item.get("passport", {}).get("requirements", "Web"),
        "image":f"{settings.site_base_url}{og_path}", "url":f"{settings.site_base_url}/tools/{slug}",
        "author":{"@type":"Person","name":item["author_name"],"url":f"{settings.site_base_url}/u/{item['author_username']}"},
        "offers":{"@type":"Offer","priceCurrency":"JPY","price":item["price"],"availability":"https://schema.org/InStock","url":f"{settings.site_base_url}/tools/{slug}"},
    }
    if item.get("rating") and review_count:
        product_schema["aggregateRating"] = {"@type":"AggregateRating","ratingValue":item["rating"],"reviewCount":review_count,"bestRating":5,"worstRating":1}
    breadcrumb_schema = {"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[{"@type":"ListItem","position":1,"name":"ツールを探す","item":f"{settings.site_base_url}/tools"},{"@type":"ListItem","position":2,"name":item["category"],"item":f"{settings.site_base_url}/tools?category={quote(item['category'])}"},{"@type":"ListItem","position":3,"name":item["name"],"item":f"{settings.site_base_url}/tools/{slug}"}]}
    sold_out = store.tool_active_orders(slug) >= item.get("capacity", 5)
    waiting_reopen = bool(user and (user["id"], slug) in store.reopen_waiters)
    interaction_blocked = bool(user and store.blocked_between(user["id"], user["username"], item.get("author_id", ""), item.get("author_username", "")))
    exclusive_public = store.transfer_is_public(item)
    if is_unavailable or sold_out:
        product_schema["offers"]["availability"] = "https://schema.org/OutOfStock"
    return templates.TemplateResponse(request, "tool_detail.html", context(request, tool=item, tool_reviews=tool_reviews, related=related, og_url=f"{settings.site_base_url}{og_path}", og_image_url=f"{settings.site_base_url}{og_path}", seo_jsonld=[product_schema,breadcrumb_schema], share_text=share_text, liked=liked, following=following, waiting_reopen=waiting_reopen, interaction_blocked=interaction_blocked, exclusive_public=exclusive_public, sold_out=sold_out, is_unavailable=is_unavailable, is_preview=is_preview, robots_value="noindex, nofollow" if is_preview else seo_metadata(request)["robots_value"]))


@app.post("/tools/{slug}/wait-reopen")
async def wait_for_tool_reopen(request: Request, slug: str):
    user = current_user(request)
    if not user: return RedirectResponse(f"/login?next=/tools/{slug}", 303)
    tool = store.get(slug)
    if not tool or (tool.get("status") == "published" and store.tool_active_orders(slug) < tool.get("capacity", 5)):
        raise HTTPException(409, "現在購入できます")
    try: waiting = store.toggle_reopen_wait(user, slug)
    except ValueError as exc: raise HTTPException(403, "再開通知を設定できません") from exc
    return RedirectResponse(f"/tools/{slug}?waiting={int(waiting)}", 303)


@app.post("/tools/{slug}/follow-updates")
async def tool_follow_updates(request: Request, slug: str):
    user = current_user(request)
    if not user: return RedirectResponse(f"/login?next=/tools/{slug}", 303)
    if not store.get_public(slug): raise HTTPException(404)
    try: following = store.follow_updates(user["id"], slug)
    except ValueError as exc: raise HTTPException(403, "このユーザーの更新はフォローできません") from exc
    return RedirectResponse(f"/tools/{slug}?following={int(following)}#updates", 303)


@app.post("/tools/{slug}/customize")
async def start_customization(request: Request, slug: str):
    user = current_user(request)
    if not user: return RedirectResponse(f"/login?next=/tools/{slug}", 303)
    tool = store.get_public(slug)
    if not tool or not tool.get("customization_available"): raise HTTPException(404, "この商品はカスタマイズ相談を受け付けていません")
    if tool["author_id"] == user["id"]: raise HTTPException(422, "自分自身には相談できません")
    try: conversation = store.conversation_for(user, slug, "customization")
    except ValueError as exc:
        if str(exc) == "blocked": raise HTTPException(403, "このユーザーとはメッセージできません") from exc
        raise HTTPException(404) from exc
    return RedirectResponse(f"/messages/{conversation['id']}", 303)


@app.get("/tools/{slug}/transfer", response_class=HTMLResponse)
async def transfer_offer(request: Request, slug: str):
    tool = store.get_public(slug)
    if not tool or not store.transfer_is_public(tool): raise HTTPException(404, "独占譲渡の受付はありません")
    user = current_user(request)
    existing = next((item for item in store.transfer_inquiries if user and item["buyer_id"] == user["id"] and item["tool_slug"] == slug and item["status"] in {"nda_pending","reviewing","negotiating"}), None)
    transfer_schema = {"@context":"https://schema.org","@type":"SoftwareApplication","name":f"{tool['name']} 独占譲渡","description":tool.get("exclusive_summary", tool["tagline"]),"applicationCategory":tool["category"],"operatingSystem":tool.get("passport",{}).get("requirements","Web"),"url":f"{settings.site_base_url}/tools/{slug}/transfer","author":{"@type":"Person","name":tool["author_name"],"url":f"{settings.site_base_url}/u/{tool['author_username']}"},"offers":{"@type":"Offer","priceCurrency":"JPY","price":tool["exclusive_price_min"],"availability":"https://schema.org/InStock","url":f"{settings.site_base_url}/tools/{slug}/transfer"}}
    breadcrumb_schema = {"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[{"@type":"ListItem","position":1,"name":"独占譲渡","item":f"{settings.site_base_url}/transfers"},{"@type":"ListItem","position":2,"name":tool["name"],"item":f"{settings.site_base_url}/tools/{slug}/transfer"}]}
    og_path = generate_og(slug, tool["name"], tool["tagline"], tool["author_username"])
    return templates.TemplateResponse(request, "transfer_offer.html", context(request, tool=tool, existing_inquiry=existing, buyer_identity_verified=identity_verified(user), seller_identity_verified=identity_verified_by_id(tool["author_id"]), seo_jsonld=[transfer_schema,breadcrumb_schema], og_image_url=f"{settings.site_base_url}{og_path}"))


@app.post("/tools/{slug}/transfer")
async def create_transfer_inquiry(request: Request, slug: str, offer_amount: int = Form(...), intended_use: str = Form(...), message: str = Form(...)):
    user = current_user(request)
    if not user: return RedirectResponse(f"/login?next=/tools/{slug}/transfer", 303)
    if not 10_000 <= offer_amount <= 100_000_000: raise HTTPException(422, "希望額は1万円〜1億円で入力してください")
    intended_use = clean_visible_text(intended_use, 10, 500, "利用目的", preserve_lines=True)
    message = clean_visible_text(message, 20, 1000, "相談内容", preserve_lines=True)
    try: inquiry, created = store.create_transfer_inquiry(user, slug, offer_amount, intended_use, message)
    except ValueError as exc:
        if str(exc) == "blocked": raise HTTPException(403, "このユーザーとは取引できません") from exc
        if str(exc) == "self inquiry": raise HTTPException(422, "自分の商品には問い合わせできません") from exc
        raise HTTPException(404, "独占譲渡の受付はありません") from exc
    if created:
        store.audit(user["id"], "transfer.inquiry_created", inquiry["id"], request.headers.get("x-request-id", ""))
    return RedirectResponse(f"/transfer-inquiries/{inquiry['id']}?created={int(created)}", 303)


@app.get("/transfer-inquiries/{inquiry_id}", response_class=HTMLResponse)
async def transfer_inquiry_detail(request: Request, inquiry_id: str):
    user = current_user(request)
    if not user: return RedirectResponse(f"/login?next=/transfer-inquiries/{inquiry_id}", 303)
    inquiry = store.transfer_inquiry_for(inquiry_id, user["id"])
    if not inquiry: raise HTTPException(404)
    tool = store.get(inquiry["tool_slug"])
    signer_ids = store.transfer_nda_signer_ids(inquiry_id)
    buyer_signed = inquiry["buyer_id"] in signer_ids
    seller_signed = inquiry["seller_id"] in signer_ids
    return templates.TemplateResponse(request, "transfer_inquiry.html", context(request, inquiry=inquiry, tool=tool, buyer_signed=buyer_signed, seller_signed=seller_signed, both_signed=buyer_signed and seller_signed, current_signed=user["id"] in signer_ids, can_sign_case_nda=identity_verified(user), transfer_nda_text=TRANSFER_NDA_TEXT, is_seller=user["id"] == inquiry["seller_id"]))


@app.post("/transfer-inquiries/{inquiry_id}/nda")
async def sign_transfer_inquiry_nda(request: Request, inquiry_id: str, agreement: str = Form(...)):
    user = current_user(request)
    if not user: raise HTTPException(401)
    inquiry = store.transfer_inquiry_for(inquiry_id, user["id"])
    if not inquiry: raise HTTPException(404)
    if not identity_verified(user): raise HTTPException(403, "案件NDAの確認には本人確認が必要です")
    if agreement != "yes": raise HTTPException(422, "秘密保持条件への同意が必要です")
    client_ip = request.client.host if request.client else "unknown"
    ip_hash = hmac.new(settings.session_secret.encode("utf-8"), client_ip.encode("utf-8"), hashlib.sha256).hexdigest()
    try: _, created = store.sign_transfer_nda(inquiry, user, TRANSFER_NDA_DOCUMENT_HASH, ip_hash)
    except ValueError as exc:
        if str(exc) == "blocked": raise HTTPException(403, "ブロック中は案件NDAを確認できません") from exc
        if str(exc) == "case closed": raise HTTPException(409, "終了済みの案件ではNDAを確認できません") from exc
        raise HTTPException(403) from exc
    if created: store.audit(user["id"], "transfer.nda_signed", inquiry_id, request.headers.get("x-request-id", ""))
    return RedirectResponse(f"/transfer-inquiries/{inquiry_id}?nda_signed={int(created)}", 303)


@app.post("/transfer-inquiries/{inquiry_id}/status/{status}")
async def transfer_inquiry_status(request: Request, inquiry_id: str, status: str):
    user = current_user(request)
    if not user: raise HTTPException(401)
    inquiry = store.transfer_inquiry_for(inquiry_id, user["id"])
    if not inquiry: raise HTTPException(404)
    try: store.update_transfer_status(inquiry, user, status)
    except ValueError as exc:
        if str(exc) == "nda required": raise HTTPException(409, "双方のNDA確認後に条件交渉へ進めます") from exc
        if str(exc) == "invalid transition": raise HTTPException(409, "終了済みの案件は更新できません") from exc
        raise HTTPException(403) from exc
    store.audit(user["id"], "transfer.status_updated", f"{inquiry_id}:{status}", request.headers.get("x-request-id", ""))
    return RedirectResponse(f"/transfer-inquiries/{inquiry_id}?updated=1", 303)


@app.post("/transfer-inquiries/{inquiry_id}/withdraw")
async def withdraw_transfer_inquiry(request: Request, inquiry_id: str):
    user = current_user(request)
    if not user: raise HTTPException(401)
    inquiry = store.transfer_inquiry_for(inquiry_id, user["id"])
    if not inquiry: raise HTTPException(404)
    try: store.withdraw_transfer_inquiry(inquiry, user)
    except ValueError as exc:
        if str(exc) == "case closed": raise HTTPException(409, "この案件はすでに終了しています") from exc
        raise HTTPException(403) from exc
    store.audit(user["id"], "transfer.inquiry_withdrawn", inquiry_id, request.headers.get("x-request-id", ""))
    return RedirectResponse(f"/transfer-inquiries/{inquiry_id}?withdrawn=1", 303)


@app.get("/checkout/{slug}", response_class=HTMLResponse)
async def checkout(request: Request, slug: str):
    user = current_user(request)
    if not user: return RedirectResponse(f"/login?next=/checkout/{slug}", 303)
    tool = store.get_public(slug)
    if not tool or tool.get("price_type") != "paid": raise HTTPException(404, "購入できない商品です")
    owned = next((x for x in store.orders if x["buyer_id"] == user["id"] and x["tool_slug"] == slug and x["status"] != "cancelled" and x.get("payment_status", "paid") not in {"cancelled", "expired"}), None)
    if store.blocked_between(user["id"], user["username"], tool["author_id"], tool["author_username"]): raise HTTPException(403, "この販売者の商品は購入できません")
    active_orders = store.tool_active_orders(slug)
    return templates.TemplateResponse(request, "checkout.html", context(request, tool=tool, owned=owned, active_orders=active_orders, sold_out=active_orders >= tool.get("capacity", 5), subscription_checkout_ready=settings.demo_mode or settings.stripe_charge_mode == "destination"))


@app.post("/api/coupons/preview")
async def coupon_preview(request: Request, slug: str = Form(...), code: str = Form(""), options: list[str] = Form(default=[]), billing_type: str = Form("one_time")):
    user = current_user(request)
    if not user: return JSONResponse({"error":"login_required"}, status_code=401)
    tool = store.get_public(slug)
    if not tool or tool.get("price_type") != "paid" or billing_type not in {"one_time","subscription"}:
        raise HTTPException(404)
    if billing_type == "subscription" and not tool.get("supports_subscription"):
        raise HTTPException(422, "この月額プランは利用できません")
    selected_options = [item for item in tool.get("options", []) if item["id"] in options]
    base_amount = tool["subscription_price"] if billing_type == "subscription" else tool["price"]
    subtotal = base_amount + sum(item["price"] for item in selected_options)
    try: normalized, discount, _ = store.coupon_discount(user["id"], tool, subtotal, code)
    except ValueError as exc: raise HTTPException(422, "クーポンが無効・期限切れ・利用済みです") from exc
    return {"code":normalized, "subtotal":subtotal, "discount":discount, "total":subtotal-discount}


@app.post("/checkout/{slug}")
async def checkout_complete(request: Request, slug: str, options: list[str] = Form(default=[]), coupon: str = Form(""), billing_type: str = Form("one_time"), purchase_agreement: str = Form(...)):
    user = current_user(request)
    if not user: return RedirectResponse("/login", 303)
    tool = store.get(slug)
    if tool and tool["author_id"] == user["id"]: raise HTTPException(403, "自分の商品は購入できません")
    if billing_type not in {"one_time","subscription"} or purchase_agreement != "yes": raise HTTPException(422)
    if billing_type == "subscription" and not settings.demo_mode and settings.stripe_charge_mode == "separate":
        raise HTTPException(409, "月額契約は売上分配の検証中です。現在は買い切りをご利用ください")
    try: order, created = store.buy(user, slug, options, coupon, billing_type, payment_pending=not settings.demo_mode, return_created=True)
    except ValueError as exc:
        if str(exc) == "blocked": raise HTTPException(403, "この販売者の商品は購入できません") from exc
        if str(exc) == "coupon unavailable": raise HTTPException(422, "クーポンが無効・期限切れ・利用済みです") from exc
        raise HTTPException(422, "購入できない商品です") from exc
    if not settings.demo_mode:
        if not created and order.get("payment_status") == "paid":
            raise HTTPException(409, "この商品は購入済みです")
        if not created and order.get("payment_status") == "pending" and order.get("checkout_session_url"):
            return RedirectResponse(order["checkout_session_url"], 303)
        if not created and order.get("payment_status") == "pending":
            if database_store:
                attempt = await database_store.operation_result(f"checkout-{order['id']}")
                # The process may have stopped after recording the provider's
                # response but before copying the session ID into the order.
                if attempt and attempt["status"] == "succeeded":
                    recovered = attempt["response"]
                    order["checkout_session_id"] = recovered["id"]
                    order["checkout_session_url"] = recovered["url"]
                    return RedirectResponse(recovered["url"], 303)
            if order.get("payment_reconciliation_required"):
                raise HTTPException(409, "決済結果を確認中です。再購入せず、購入履歴を確認してサポートへお問い合わせください")
            raise HTTPException(409, "決済画面を準備中です。数秒後にもう一度お試しください", headers={"Retry-After":"3"})
        if not created:
            raise HTTPException(409, "既存の契約状況を確認してください")
        account = store.connected_accounts.get(order["seller_id"])
        if not settings.stripe_ready or not account or not account.get("charges_enabled"):
            if created:
                with store._lock:
                    store.release_coupon(order)
                    if order in store.orders: store.orders.remove(order)
                    store.subscriptions[:] = [x for x in store.subscriptions if x.get("order_id") != order["id"]]
            raise HTTPException(503, "販売者の決済受取設定が完了していません")
        try: session = await stripe.create_checkout(order, account["account_id"])
        except RuntimeError as exc:
            if created:
                with store._lock:
                    store.release_coupon(order)
                    if order in store.orders: store.orders.remove(order)
                    store.subscriptions[:] = [x for x in store.subscriptions if x.get("order_id") != order["id"]]
            await persist_state()
            raise HTTPException(502, "決済画面を開始できませんでした") from exc
        order["checkout_session_id"] = session["id"]
        order["checkout_session_url"] = session["url"]
        return RedirectResponse(session["url"], 303)
    if created:
        store.notify(order["buyer_id"], "購入が完了しました", order["tool_name"], f"/orders/{order['id']}", "transactions")
        store.notify(order["seller_id"], "新しい注文が入りました", f"{order['buyer_name']}さんが購入しました", f"/orders/{order['id']}", "transactions")
    return RedirectResponse(f"/library?completed={order['id']}" if order.get("fulfillment_type") == "instant" else f"/purchases?completed={order['id']}", 303)


@app.get("/purchases", response_class=HTMLResponse)
async def purchases(request: Request):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/purchases", 303)
    await sweep_due_orders()
    orders = [x for x in store.orders if x["buyer_id"] == user["id"]]
    return templates.TemplateResponse(request, "purchases.html", context(request, orders=orders))


@app.get("/mypage", response_class=HTMLResponse)
async def mypage(request: Request, tab: str = "overview"):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/mypage", 303)
    await sweep_due_orders()
    if tab not in {"overview","buying","selling","reputation"}: tab = "overview"
    buying = sorted([x for x in store.orders if x["buyer_id"] == user["id"]], key=lambda x:x["updated_at"], reverse=True)
    selling = sorted([x for x in store.orders if x["seller_id"] == user["id"]], key=lambda x:x["updated_at"], reverse=True)
    reviews = sorted([x for x in store.reviews if x.get("seller_id") == user["id"] and x.get("published", True)], key=lambda x:x["created_at"], reverse=True)
    buyer_feedback = sorted([x for x in store.buyer_reviews if x.get("buyer_id") == user["id"] and x.get("published", True)], key=lambda x:x["created_at"], reverse=True)
    rating = round(sum(x["rating"] for x in reviews) / len(reviews), 1) if reviews else None
    breakdown = {n:sum(1 for x in reviews if x["rating"] == n) for n in range(5,0,-1)}
    favorites_count = sum(1 for uid,_ in store.likes if uid == user["id"])
    following_count = sum(1 for uid,_ in store.update_followers if uid == user["id"])
    unread_count = sum(1 for x in store.notifications if x["user_id"] == user["id"] and not x["read"])
    next_actions = [x for x in buying if x["status"] == "awaiting_acceptance"] + [x for x in selling if x["status"] == "in_progress"]
    metrics = {"spent":sum(x["amount"] for x in buying if x["status"] != "cancelled"),"earned":sum(x["amount"]-x["platform_fee"] for x in selling if x["status"] == "completed"),"sales":sum(1 for x in selling if x["status"] == "completed"),"rating":rating}
    return templates.TemplateResponse(request,"mypage.html",context(request,tab=tab,buying=buying,selling=selling,reviews=reviews,buyer_feedback=buyer_feedback,rating=rating,breakdown=breakdown,favorites_count=favorites_count,following_count=following_count,unread_count=unread_count,next_actions=next_actions,metrics=metrics))


@app.get("/favorites", response_class=HTMLResponse)
async def favorites(request: Request, folder: str = ""):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/favorites", 303)
    all_items = [item for item in store.list_tools() if (user["id"], item["slug"]) in store.likes]
    folders = store.favorite_folders_for(user["id"])
    valid_folder_ids = {item["id"] for item in folders}
    selected_folder = folder if folder in valid_folder_ids or folder == "unfiled" else ""
    assignment = store.favorite_assignments
    if selected_folder == "unfiled":
        items = [item for item in all_items if not assignment.get(f"{user['id']}\u241f{item['slug']}")]
    elif selected_folder:
        items = [item for item in all_items if assignment.get(f"{user['id']}\u241f{item['slug']}") == selected_folder]
    else:
        items = all_items
    folder_counts = {item["id"]:sum(1 for tool in all_items if assignment.get(f"{user['id']}\u241f{tool['slug']}") == item["id"]) for item in folders}
    unfiled_count = sum(1 for tool in all_items if not assignment.get(f"{user['id']}\u241f{tool['slug']}") )
    return templates.TemplateResponse(request, "favorites.html", context(request, tools=items, all_count=len(all_items), favorite_folders=folders, folder_counts=folder_counts, selected_folder=selected_folder, unfiled_count=unfiled_count, assignments=assignment))


@app.post("/favorites/folders")
async def create_favorite_folder(request: Request, name: str = Form(...)):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/favorites", 303)
    name = clean_visible_text(name, 1, 30, "フォルダ名")
    try: folder = store.create_favorite_folder(user["id"], name)
    except ValueError: raise HTTPException(409, "同じ名前のフォルダがあるか、作成上限に達しています")
    return RedirectResponse(f"/favorites?folder={folder['id']}", 303)


@app.post("/favorites/{slug}/folder")
async def move_favorite_to_folder(request: Request, slug: str, folder_id: str = Form("")):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/favorites", 303)
    if not store.get(slug): raise HTTPException(404)
    try: store.assign_favorite_folder(user["id"], slug, folder_id)
    except ValueError: raise HTTPException(409, "お気に入りまたはフォルダを確認できません")
    return RedirectResponse(f"/favorites?moved=1{'&folder='+folder_id if folder_id else ''}", 303)


@app.post("/favorites/folders/{folder_id}/delete")
async def delete_favorite_folder(request: Request, folder_id: str):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/favorites", 303)
    if not store.delete_favorite_folder(user["id"], folder_id): raise HTTPException(404)
    return RedirectResponse("/favorites?folder_deleted=1", 303)


@app.get("/notifications", response_class=HTMLResponse)
async def notifications(request: Request, filter: str = "all"):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/notifications", 303)
    filter = filter if filter in {"all","unread","messages","transactions","requests","updates"} else "all"
    items = sorted([item for item in store.notifications if item["user_id"] == user["id"]], key=lambda item:item["created_at"], reverse=True)
    if filter == "unread": items = [item for item in items if not item.get("read")]
    elif filter != "all": items = [item for item in items if item.get("category", "general") == filter]
    return templates.TemplateResponse(request, "notifications.html", context(request, notifications=items, notification_filter=filter))


@app.post("/notifications/read-all")
async def notifications_read_all(request: Request):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/notifications", 303)
    store.mark_notifications_read(user["id"])
    return RedirectResponse("/notifications", 303)


@app.post("/notifications/{notification_id}/open")
async def notification_open(request: Request, notification_id: str):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/notifications", 303)
    item = store.mark_notification_read(user["id"], notification_id)
    if not item: raise HTTPException(404)
    return RedirectResponse(safe_next(item.get("url")), 303)


@app.get("/notifications/settings", response_class=HTMLResponse)
async def notification_settings(request: Request):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/notifications/settings", 303)
    return templates.TemplateResponse(request, "notification_settings.html", context(request, preferences=store.notification_settings_for(user["id"])))


@app.post("/notifications/settings")
async def notification_settings_save(request: Request, messages: str = Form(""), transactions: str = Form(""), requests: str = Form(""), updates: str = Form("")):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/notifications/settings", 303)
    with store._lock:
        store.notification_preferences[user["id"]] = {"messages":messages=="on", "transactions":transactions=="on", "requests":requests=="on", "updates":updates=="on"}
    return RedirectResponse("/notifications/settings?saved=1", 303)


@app.get("/requests", response_class=HTMLResponse)
async def requests_list(request: Request, category: str = ""):
    category = category if category in CATEGORIES else ""
    items = [x for x in store.requests if x["status"] == "open" and (not category or x["category"] == category)]
    viewer = current_user(request)
    if viewer:
        items = [item for item in items if not item.get("owner_username") or not store.blocked_between(viewer["id"], viewer["username"], item["owner_id"], item["owner_username"])]
    return templates.TemplateResponse(request, "requests.html", context(request, requests=items, selected_category=category))


@app.get("/requests/new", response_class=HTMLResponse)
async def request_new(request: Request):
    if not current_user(request): return RedirectResponse("/login?next=/requests/new", 303)
    return templates.TemplateResponse(request, "request_form.html", context(request))


@app.post("/requests/new")
async def request_create(request: Request, title: str = Form(...), category: str = Form(...), detail: str = Form(...), budget_min: int = Form(...), budget_max: int = Form(...), deadline: str = Form(...), terms_agreement: str = Form(...)):
    user = current_user(request)
    if not user: return RedirectResponse("/login", 303)
    title = clean_visible_text(title, 3, 80, "募集タイトル")
    detail = clean_visible_text(detail, 10, 3000, "依頼内容", preserve_lines=True)
    try: deadline_date = datetime.strptime(deadline, "%Y-%m-%d").date()
    except ValueError as exc: raise HTTPException(422, "募集期限を正しい日付で入力してください") from exc
    today = datetime.now(timezone.utc).date()
    if category not in CATEGORIES or terms_agreement != "yes" or not 1000 <= budget_min <= budget_max <= 10_000_000 or not today <= deadline_date <= today + timedelta(days=365): raise HTTPException(422)
    item = store.create_request(user,{"title":title,"category":category,"detail":detail,"budget_min":budget_min,"budget_max":budget_max,"deadline":deadline})
    store.notify(user["id"],"募集を公開しました",item["title"],f"/requests/{item['id']}", "requests")
    return RedirectResponse(f"/requests/{item['id']}", 303)


@app.get("/requests/{request_id}", response_class=HTMLResponse)
async def request_detail(request: Request, request_id: str):
    item = next((x for x in store.requests if x["id"] == request_id), None)
    if not item: raise HTTPException(404)
    viewer = current_user(request)
    if viewer and item.get("owner_username") and store.blocked_between(viewer["id"], viewer["username"], item["owner_id"], item["owner_username"]): raise HTTPException(404)
    return templates.TemplateResponse(request, "request_detail.html", context(request, job=item))


@app.post("/requests/{request_id}/apply")
async def request_apply(request: Request, request_id: str, message: str = Form(...), amount: int = Form(...), delivery_days: int = Form(...)):
    user = current_user(request)
    if not user: return RedirectResponse("/login", 303)
    item = next((candidate for candidate in store.requests if candidate["id"] == request_id), None)
    if not item or item["status"] != "open" or item["owner_id"] == user["id"]: raise HTTPException(403)
    message = clean_visible_text(message, 10, 1000, "応募メッセージ", preserve_lines=True)
    if not 100 <= amount <= 10_000_000 or not 1 <= delivery_days <= 120: raise HTTPException(422)
    try: item, created = store.submit_application(request_id, user, message, amount, delivery_days)
    except ValueError: raise HTTPException(403)
    if created:
        store.notify(user["id"],"応募を送信しました",item["title"],f"/requests/{request_id}", "requests")
        store.notify(item["owner_id"],"募集に新しい応募が届きました",f"{user['display_name']}さんから提案が届きました",f"/requests/{request_id}", "requests")
    return RedirectResponse(f"/requests/{request_id}?applied=1", 303)


@app.post("/requests/{request_id}/applications/{application_id}/select")
async def select_application(request: Request, request_id: str, application_id: str):
    user = current_user(request)
    if not user: raise HTTPException(403)
    candidate_request = next((item for item in store.requests if item["id"] == request_id and item["owner_id"] == user["id"]), None)
    candidate_application = candidate_request and next((item for item in candidate_request["application_list"] if item["id"] == application_id and item["status"] == "submitted"), None)
    if not candidate_application: raise HTTPException(404)
    if not settings.demo_mode:
        account = store.connected_accounts.get(candidate_application["applicant_id"])
        if not settings.stripe_ready or not account or not account.get("charges_enabled"): raise HTTPException(503, "応募者の決済受取設定が完了していません")
    try: item, application, order = store.contract_request(user, request_id, application_id, payment_pending=not settings.demo_mode)
    except ValueError: raise HTTPException(409, "この募集はすでに契約済みです")
    if not settings.demo_mode:
        try: session = await stripe.create_checkout(order, account["account_id"])
        except RuntimeError as exc:
            store.rollback_request_contract(item, application, order)
            await persist_state()
            raise HTTPException(502,"決済画面を開始できませんでした") from exc
        order["checkout_session_id"] = session["id"]
        order["checkout_session_url"] = session["url"]
        return RedirectResponse(session["url"],303)
    store.notify(application["applicant_id"], "提案が採用されました", item["title"], f"/orders/{order['id']}", "requests")
    store.notify(user["id"], "クリエイターとの取引を開始しました", application["applicant_name"], f"/orders/{order['id']}", "transactions")
    return RedirectResponse(f"/orders/{order['id']}",303)


@app.get("/payouts", response_class=HTMLResponse)
async def payouts(request: Request):
    user=current_user(request)
    if not user: return RedirectResponse("/login?next=/payouts",303)
    return templates.TemplateResponse(request,"payouts.html",context(request,balance=available_balance(user["id"]),payouts=[x for x in store.payouts if x["seller_id"]==user["id"]], payout_labels=PAYOUT_LABELS, payout_requests_ready=settings.demo_mode or sandbox_payouts_ready(settings), payout_request_key=secrets.token_urlsafe(24)))


@app.post("/payouts")
async def payout_request(request: Request, amount: int = Form(...), request_key: str = Form("")):
    user=current_user(request)
    if not user or not user.get("is_verified"): raise HTTPException(403,"本人確認が必要です")
    if not settings.demo_mode and not sandbox_payouts_ready(settings):
        raise HTTPException(409,"本番の振込は公開前検証中です。実際の銀行振込はまだ開始されません")
    balance = available_balance(user["id"])
    if amount < settings.payout_minimum or (not database_store and amount > balance): raise HTTPException(422, f"振込申請は{settings.payout_minimum:,}円以上、振込可能残高以下で指定してください")
    if database_store and not re.fullmatch(r"[A-Za-z0-9_-]{32}", request_key):
        raise HTTPException(422, "申請画面を再読み込みしてください")
    try:
        if database_store:
            request_payout(store, settings, user["id"], amount, request_key=request_key)
        else:
            store.create_payout(user["id"], amount)
    except FinanceConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError: raise HTTPException(422, f"振込申請は{settings.payout_minimum:,}円以上、振込可能残高以下で指定してください")
    store.audit(user["id"], "payout.requested", str(amount), request.headers.get("x-request-id", ""))
    return RedirectResponse("/payouts?requested=1",303)


@app.post("/u/{username}/block")
async def block_user(request: Request, username: str, return_to: str = Form("")):
    user=current_user(request)
    if not user: return RedirectResponse("/login",303)
    if username == user["username"]: raise HTTPException(422, "自分自身はブロックできません")
    try: blocked = store.toggle_block(user, username)
    except ValueError as exc: raise HTTPException(404, "ユーザーが見つかりません") from exc
    store.audit(user["id"], "user.blocked" if blocked else "user.unblocked", username, request.headers.get("x-request-id", ""))
    destination = "/settings/blocks?updated=1" if return_to == "blocks" else f"/u/{username}?blocked={int(blocked)}"
    return RedirectResponse(destination,303)


@app.post("/u/{username}/follow")
async def follow_creator(request: Request, username: str):
    user = current_user(request)
    if not user: return RedirectResponse(f"/login?next=/u/{username}", 303)
    if username == user["username"]: raise HTTPException(422, "自分自身はフォローできません")
    if not any(x.get("author_username") == username for x in store.tools): raise HTTPException(404)
    try: followed = store.follow_creator(user["id"], username, user["username"])
    except ValueError as exc: raise HTTPException(403, "このユーザーはフォローできません") from exc
    return RedirectResponse(f"/u/{username}?followed={int(followed)}", 303)


@app.get("/orders/{order_id}", response_class=HTMLResponse)
async def transaction_room(request: Request, order_id: str):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/purchases", 303)
    await sweep_due_orders()
    order = store.get_order(order_id, user["id"])
    if not order: raise HTTPException(404, "取引が見つかりません")
    if not settings.demo_mode and order.get("payment_status") != "paid":
        payment_status = order.get("payment_status", "pending")
        status_code = 202 if payment_status == "pending" else (402 if payment_status == "past_due" else 410)
        return templates.TemplateResponse(request,"payment_pending.html",context(request,order=order,payment_status=payment_status),status_code=status_code)
    return templates.TemplateResponse(request, "transaction.html", context(request, order=order, room_open=store.room_is_open(order), review_open=store.review_is_open(order)))


@app.post("/orders/{order_id}/message")
async def transaction_message(request: Request, order_id: str, body: str = Form(...)):
    user = current_user(request); order = user and store.get_order(order_id, user["id"])
    if not order: raise HTTPException(404)
    if not settings.demo_mode and order.get("payment_status") != "paid": raise HTTPException(409,"支払い確定後に利用できます")
    if not store.room_is_open(order): raise HTTPException(409, "クローズした取引にはメッセージを追加できません")
    body = clean_visible_text(body, 1, 1000, "メッセージ", preserve_lines=True)
    store.order_message(order, user, body)
    recipient_id = order["seller_id"] if user["id"] == order["buyer_id"] else order["buyer_id"]
    store.notify(recipient_id, f"{user['display_name']}さんから取引メッセージ", order["tool_name"], f"/orders/{order_id}", "messages")
    return RedirectResponse(f"/orders/{order_id}", 303)


@app.post("/orders/{order_id}/demo-deliver")
async def demo_deliver(request: Request, order_id: str, note: str = Form("ツール一式と導入手順を納品します。内容をご確認ください。")):
    user = current_user(request); order = user and store.get_order(order_id, user["id"])
    if not order or not settings.demo_mode: raise HTTPException(403)
    if order.get("status") != "in_progress": raise HTTPException(409)
    note = clean_visible_text(note, 5, 1500, "納品内容", preserve_lines=True)
    try:
        store.deliver(order, note)
    except ValueError as exc:
        raise HTTPException(409, "現在の状態では納品できません") from exc
    store.audit(user["id"], "order.demo_delivery_simulated", order_id, request.headers.get("x-request-id", ""))
    store.notify(order["buyer_id"], "正式な納品が届きました", order["tool_name"], f"/orders/{order_id}", "transactions")
    return RedirectResponse(f"/orders/{order_id}", 303)


@app.post("/orders/{order_id}/deliver")
async def deliver_order(request: Request, order_id: str, note: str = Form(...), delivery_file: UploadFile | None = File(None)):
    user = current_user(request); order = user and store.get_order(order_id, user["id"])
    if not order or user["id"] != order["seller_id"]: raise HTTPException(403)
    if not settings.demo_mode and order.get("payment_status") != "paid": raise HTTPException(409)
    if order["status"] != "in_progress": raise HTTPException(409)
    note = clean_visible_text(note, 5, 1500, "納品内容", preserve_lines=True)
    file_record = None
    if delivery_file and delivery_file.filename:
        content = await delivery_file.read(50_000_001)
        try: suffix = validate_delivery_file(delivery_file.filename,content)
        except ValueError as exc: raise HTTPException(422,str(exc)) from exc
        scan_engine = "basic"
        if settings.clamav_host:
            try: clean, scan_result = await asyncio.to_thread(clamav_scan,content,settings.clamav_host,settings.clamav_port)
            except OSError as exc: raise HTTPException(503,"ファイル安全検査へ接続できません") from exc
            if not clean:
                store.audit(user["id"],"delivery.malware_blocked",order_id,request.headers.get("x-request-id", "")); raise HTTPException(422,"安全検査で問題が見つかったため納品できません")
            scan_engine = scan_result
        elif not settings.demo_mode:
            raise HTTPException(503,"本番のファイル安全検査が未設定です")
        file_id = hashlib.sha256(content).hexdigest()[:24]
        directory = Path(settings.private_storage_path) / order_id
        directory.mkdir(parents=True,exist_ok=True)
        directory.chmod(0o700)
        path = directory / f"{file_id}{suffix}"
        path.write_bytes(content)
        path.chmod(0o600)
        safe_name = clean_visible_text(Path(delivery_file.filename).name, 1, 120, "ファイル名")
        file_record = {"id":file_id,"name":safe_name,"path":str(path),"size":len(content),"sha256":hashlib.sha256(content).hexdigest(),"scan_status":"passed","scan_engine":scan_engine}
    try:
        store.deliver(order, note)
    except ValueError as exc:
        if file_record:
            Path(file_record["path"]).unlink(missing_ok=True)
        raise HTTPException(409, "現在の状態では納品できません") from exc
    if file_record: order["delivery"].setdefault("files",[]).append(file_record)
    store.audit(user["id"],"order.delivered",order_id,request.headers.get("x-request-id", ""))
    store.notify(order["buyer_id"], "正式な納品が届きました", order["tool_name"], f"/orders/{order_id}", "transactions")
    await send_email_safely(order.get("buyer_email"), f"[ツールバコ] {order['tool_name']} が納品されました", f"取引ルームで納品内容を確認し、承諾または修正依頼を行ってください。\n{settings.site_base_url}/orders/{order_id}")
    return RedirectResponse(f"/orders/{order_id}", 303)


@app.get("/orders/{order_id}/files/{file_id}")
async def download_delivery_file(request: Request, order_id: str, file_id: str):
    user = current_user(request); order = user and store.get_order(order_id,user["id"])
    if not order or not order.get("delivery"): raise HTTPException(404)
    item = next((x for x in order["delivery"].get("files",[]) if x["id"]==file_id and x.get("scan_status")=="passed"),None)
    if not item: raise HTTPException(404)
    storage_root = Path(settings.private_storage_path).resolve()
    path = Path(item["path"]).resolve()
    if path != storage_root and storage_root not in path.parents: raise HTTPException(404)
    if not path.is_file(): raise HTTPException(410,"ファイルが見つかりません")
    store.audit(user["id"],"delivery.downloaded",f"{order_id}:{file_id}",request.headers.get("x-request-id", ""))
    return FileResponse(path,filename=item["name"],media_type="application/octet-stream",headers={"Cache-Control":"no-store, private","X-Content-Type-Options":"nosniff"})


@app.post("/orders/{order_id}/actions/{action}")
async def order_transition(request: Request, order_id: str, action: str, reason: str = Form("")):
    user = current_user(request); order = user and store.get_order(order_id, user["id"])
    if not order: raise HTTPException(404)
    if not settings.demo_mode and order.get("payment_status") != "paid": raise HTTPException(409)
    if action not in {"accept","revise","cancel_request","cancel_accept","cancel_reject"}: raise HTTPException(404)
    if action in {"accept","revise"} and user["id"] != order["buyer_id"]: raise HTTPException(403)
    if action in {"cancel_accept","cancel_reject"} and order.get("cancel_requested_by") == user["id"]: raise HTTPException(403)
    if action == "cancel_request": reason = clean_visible_text(reason, 5, 500, "キャンセル理由", preserve_lines=True)
    if order.get("refund_status") in {"processing", "pending", "partial", "review"}:
        raise HTTPException(409, "返金の処理・照合中です。重複操作せず運営へお問い合わせください")
    if action == "cancel_accept" and not settings.demo_mode:
        if order.get("status") != "cancel_pending" or order.get("cancel_requested_by") == user["id"]: raise HTTPException(409)
        if order.get("payment_status") != "paid" or not order.get("payment_reference"): raise HTTPException(409,"返金対象の決済を確認できません")
        try:
            await request_order_refunds(store, order, stripe)
        except FinanceConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except StripeOutcomeUnknown:
            order["refund_status"] = "review"
            await persist_state()
            raise
        except RuntimeError as exc:
            order["refund_status"] = "review"
            await persist_state()
            raise HTTPException(502,"返金処理を開始できませんでした") from exc
    try: store.transition(order, action, reason, user["id"])
    except ValueError: raise HTTPException(409, "現在の状態では操作できません")
    store.audit(user["id"], f"order.{action}", order_id, request.headers.get("x-request-id", ""))
    recipient = order.get("seller_email") if user["id"] == order["buyer_id"] else order.get("buyer_email")
    action_label = {"accept":"納品承諾","revise":"修正依頼","cancel_request":"キャンセル申請","cancel_accept":"キャンセル合意","cancel_reject":"キャンセル却下"}[action]
    recipient_id = order["seller_id"] if user["id"] == order["buyer_id"] else order["buyer_id"]
    store.notify(recipient_id, f"取引が更新されました：{action_label}", order["tool_name"], f"/orders/{order_id}", "transactions")
    await send_email_safely(recipient, f"[ツールバコ] {order['tool_name']}: {action_label}", f"取引状態が更新されました。\n{settings.site_base_url}/orders/{order_id}")
    return RedirectResponse(f"/orders/{order_id}", 303)


@app.post("/orders/{order_id}/dispute")
async def open_dispute(request: Request, order_id: str, detail: str = Form(...)):
    user = current_user(request); order = user and store.get_order(order_id, user["id"])
    if not order or order["status"] == "cancelled": raise HTTPException(422, "この取引では紛争申請できません")
    detail = clean_visible_text(detail, 20, 2000, "状況", preserve_lines=True)
    case = store.create_support_case(user,"dispute",f"取引「{order['tool_name']}」の紛争申請",detail,order_id)
    order["dispute_status"] = case["status"]
    store.audit(user["id"], "dispute.opened", order_id, request.headers.get("x-request-id", ""))
    return RedirectResponse(f"/orders/{order_id}?dispute={case['id']}", 303)


@app.post("/orders/{order_id}/submit-review")
async def review_order(request: Request, order_id: str, rating: int = Form(...), comment: str = Form(""), communication: int = Form(5), accuracy: int = Form(5), on_time: int = Form(5)):
    user = current_user(request); order = user and store.get_order(order_id, user["id"])
    if not order or any(not 1 <= n <= 5 for n in {rating,communication,accuracy,on_time}): raise HTTPException(422)
    if not settings.demo_mode and order.get("payment_status") != "paid": raise HTTPException(409)
    comment = clean_visible_text(comment, 0, 500, "口コミ", preserve_lines=True)
    try: store.add_review(order, user, rating, comment, {"communication":communication,"accuracy":accuracy,"on_time":on_time})
    except ValueError: raise HTTPException(409)
    store.notify(order["seller_id"], "購入者から評価が届きました", order["tool_name"], f"/orders/{order_id}", "transactions")
    await send_email_safely(order.get("seller_email"), f"[ツールバコ] {order['tool_name']} に評価が届きました", f"評価: {rating}/5\n{settings.site_base_url}/mypage?tab=reputation")
    return RedirectResponse(f"/orders/{order_id}?reviewed=1", 303)


@app.post("/orders/{order_id}/submit-buyer-review")
async def review_buyer(request: Request, order_id: str, rating: int = Form(...), comment: str = Form(""), clarity: int = Form(5), communication: int = Form(5), schedule: int = Form(5)):
    user = current_user(request); order = user and store.get_order(order_id, user["id"])
    if not order or any(not 1 <= n <= 5 for n in {rating,clarity,communication,schedule}): raise HTTPException(422)
    if not settings.demo_mode and order.get("payment_status") != "paid": raise HTTPException(409)
    comment = clean_visible_text(comment, 0, 500, "購入者への評価", preserve_lines=True)
    try: store.add_buyer_review(order, user, rating, comment, {"clarity":clarity,"communication":communication,"schedule":schedule})
    except ValueError: raise HTTPException(409)
    store.audit(user["id"], "review.buyer_submitted", order_id, request.headers.get("x-request-id", ""))
    store.notify(order["buyer_id"], "クリエイターから評価が届きました", order["tool_name"], f"/orders/{order_id}", "transactions")
    await send_email_safely(order.get("buyer_email"), f"[ツールバコ] {order['tool_name']} の相互評価が更新されました", f"取引ルームで確認できます。\n{settings.site_base_url}/orders/{order_id}")
    return RedirectResponse(f"/orders/{order_id}?buyer_reviewed=1", 303)


@app.post("/orders/{order_id}/extra-payment")
async def extra_payment(request: Request, order_id: str, amount: int = Form(...), note: str = Form("追加対応へのお礼")):
    user = current_user(request); order = user and store.get_order(order_id, user["id"])
    if not order or user["id"] != order["buyer_id"] or order["status"] != "completed": raise HTTPException(403)
    if not settings.demo_mode and order.get("payment_status") != "paid": raise HTTPException(409)
    if not 100 <= amount <= 100_000: raise HTTPException(422, "追加支払いは100円〜10万円で入力してください")
    note = clean_visible_text(note, 1, 200, "追加支払いの内容")
    if not settings.demo_mode:
        account = store.connected_accounts.get(order["seller_id"])
        if not settings.stripe_ready or not account or not account.get("charges_enabled"): raise HTTPException(503,"追加支払いを開始できません")
        extra, created = store.reserve_extra_payment(order, amount, note)
        if not created and extra.get("checkout_session_url"): return RedirectResponse(extra["checkout_session_url"], 303)
        if not created: raise HTTPException(409, "決済画面を準備中です。数秒後にもう一度お試しください", headers={"Retry-After":"3"})
        try: session = await stripe.create_extra_checkout(order,extra,account["account_id"])
        except RuntimeError as exc:
            if created: store.rollback_extra_payment(order, extra)
            await persist_state()
            raise HTTPException(502,"追加支払いを開始できませんでした") from exc
        extra["checkout_session_id"] = session["id"]
        extra["checkout_session_url"] = session["url"]
        return RedirectResponse(session["url"],303)
    store.add_extra_payment(order, amount, note)
    store.notify(order["seller_id"], "追加支払いが届きました", f"{order['tool_name']} · ¥{amount:,}", f"/orders/{order_id}", "transactions")
    return RedirectResponse(f"/orders/{order_id}?extra=1", 303)


@app.get("/orders/{order_id}/receipt", response_class=HTMLResponse)
async def receipt(request: Request, order_id: str):
    user = current_user(request); order = user and store.get_order(order_id, user["id"])
    if not order or user["id"] != order["buyer_id"] or order["status"] != "completed": raise HTTPException(403)
    return templates.TemplateResponse(request, "receipt.html", context(request, order=order))


@app.get("/orders/{order_id}/documents/{kind}", response_class=HTMLResponse)
async def order_document(request: Request, order_id: str, kind: str):
    user = current_user(request); order = user and store.get_order(order_id, user["id"])
    document_types = {
        "estimate":{"title":"見積書","prefix":"Q","issuer":"seller","recipient":"buyer"},
        "purchase-order":{"title":"発注書","prefix":"PO","issuer":"buyer","recipient":"seller"},
        "delivery-note":{"title":"納品書","prefix":"DN","issuer":"seller","recipient":"buyer"},
    }
    if not order or kind not in document_types: raise HTTPException(404)
    if kind == "delivery-note" and not order.get("delivery"): raise HTTPException(409, "正式納品後に発行できます")
    definition = document_types[kind]
    parties = {
        "buyer":{"name":order["buyer_name"],"role":"購入者"},
        "seller":{"name":order["seller_name"],"role":"販売者"},
    }
    issue_at = order["delivery"]["created_at"] if kind == "delivery-note" else order["created_at"]
    document = {
        **definition,
        "number":f"TB-{definition['prefix']}-{issue_at.strftime('%Y%m')}-{order['id'][-8:].upper()}",
        "issued_at":issue_at,
        "issuer_party":parties[definition["issuer"]],
        "recipient_party":parties[definition["recipient"]],
    }
    return templates.TemplateResponse(request,"order_document.html",context(request,order=order,document=document))


@app.get("/library", response_class=HTMLResponse)
async def purchase_library(request: Request):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/library", 303)
    def has_access(order: dict) -> bool:
        if order.get("billing_type", "one_time") != "subscription": return True
        subscription = next((item for item in store.subscriptions if item.get("order_id") == order["id"]), None)
        return bool(subscription and subscription.get("status") == "active")
    orders = [x for x in store.orders if x["buyer_id"] == user["id"] and x["status"] == "completed" and has_access(x)]
    return templates.TemplateResponse(request, "library.html", context(request, orders=orders))


@app.get("/subscriptions", response_class=HTMLResponse)
async def subscriptions(request: Request):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/subscriptions", 303)
    items = [x for x in store.subscriptions if x["buyer_id"] == user["id"]]
    return templates.TemplateResponse(request, "subscriptions.html", context(request, subscriptions=items))


@app.post("/subscriptions/{subscription_id}/cancel")
async def cancel_subscription(request: Request, subscription_id: str):
    user = current_user(request)
    item = user and next((x for x in store.subscriptions if x["id"] == subscription_id and x["buyer_id"] == user["id"]), None)
    if not item or item["status"] != "active" or item.get("cancel_at_period_end"): raise HTTPException(404)
    if not settings.demo_mode:
        if not item.get("provider_subscription_id"): raise HTTPException(409,"定期契約情報を確認できません")
        try: await stripe.cancel_subscription(item["provider_subscription_id"],f"cancel-{item['id']}")
        except RuntimeError as exc: raise HTTPException(502,"解約を予約できませんでした") from exc
    item["cancel_at_period_end"] = True
    item["cancel_requested_at"] = datetime.now(timezone.utc)
    return RedirectResponse("/subscriptions?cancelled=1", 303)


@app.get("/seller", response_class=HTMLResponse)
async def seller_dashboard(request: Request):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/seller", 303)
    await sweep_due_orders()
    own_tools = [x for x in store.tools if x.get("author_id") == user["id"]]
    orders = sorted([x for x in store.orders if x["seller_id"] == user["id"]], key=lambda item:item["updated_at"], reverse=True)
    completed = [x for x in orders if x["status"] == "completed"]
    rank = store.creator_rank(user["username"], user.get("is_verified", False))
    action_orders = [item for item in orders if item["status"] == "in_progress" or (item["status"] == "cancel_pending" and item.get("cancel_requested_by") != user["id"])]
    account = store.registered_users.get(user["username"], {})
    transfer_inquiries = sorted([item for item in store.transfer_inquiries if item["seller_id"] == user["id"]], key=lambda item:item["updated_at"], reverse=True)
    profile_checks = [bool(account.get("headline")), bool(account.get("bio")), bool(account.get("skills")), bool(account.get("availability") and account.get("availability") != "受付状況未設定")]
    profile_score = round(sum(profile_checks) / len(profile_checks) * 100)
    return templates.TemplateResponse(request, "seller.html", context(request, tools=own_tools, orders=orders, recent_orders=orders[:8], action_orders=action_orders, transfer_inquiries=transfer_inquiries, rank=rank, profile_score=profile_score, active_by_slug={x["slug"]:store.tool_active_orders(x["slug"]) for x in own_tools}, gross=sum(x["amount"] for x in completed), fees=sum(x["platform_fee"] for x in completed), pending=sum(x["amount"]-x["platform_fee"] for x in orders if x["status"] not in {"completed","cancelled"})))


@app.get("/seller/transfers", response_class=HTMLResponse)
async def seller_transfers(request: Request):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/seller/transfers", 303)
    inquiries = sorted([item for item in store.transfer_inquiries if item["seller_id"] == user["id"]], key=lambda item:item["updated_at"], reverse=True)
    return templates.TemplateResponse(request, "seller_transfers.html", context(request, inquiries=inquiries))


@app.get("/seller/payments", response_class=HTMLResponse)
async def seller_payment_settings(request: Request):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/seller/payments",303)
    account = store.connected_accounts.get(user["id"])
    if account and settings.stripe_ready and not settings.demo_mode:
        try:
            remote = await stripe.retrieve_account(account["account_id"])
            account.update({"charges_enabled":bool(remote.get("charges_enabled")),"payouts_enabled":bool(remote.get("payouts_enabled")),"details_submitted":bool(remote.get("details_submitted"))})
        except RuntimeError:
            account["sync_error"] = True
    return templates.TemplateResponse(request,"seller_payments.html",context(request,account=account,stripe_ready=settings.stripe_ready))


@app.post("/seller/payments/onboard")
async def seller_payment_onboard(request: Request):
    user = current_user(request)
    if not user: raise HTTPException(401)
    if settings.demo_mode:
        store.connected_accounts[user["id"]] = {"account_id":f"acct_demo_{user['id'][:8]}","charges_enabled":True,"payouts_enabled":True,"details_submitted":True}
        store.audit(user["id"],"connect.demo_enabled",user["id"],request.headers.get("x-request-id", ""))
        return RedirectResponse("/seller/payments?connected=1",303)
    if not settings.stripe_ready: raise HTTPException(503,"Stripe Connectが未設定です")
    account = store.connected_accounts.get(user["id"])
    try:
        if not account:
            remote = await stripe.create_express_account(user,f"connect-{user['id']}")
            account = {"account_id":remote["id"],"charges_enabled":False,"payouts_enabled":False,"details_submitted":False}
            store.connected_accounts[user["id"]] = account
        link = await stripe.create_account_link(account["account_id"])
    except RuntimeError as exc: raise HTTPException(502,"決済受取設定を開始できませんでした") from exc
    store.audit(user["id"],"connect.onboarding_started",account["account_id"],request.headers.get("x-request-id", ""))
    return RedirectResponse(link["url"],303)


@app.get("/seller/tools/{slug}/edit", response_class=HTMLResponse)
async def seller_tool_edit(request: Request, slug: str):
    user = current_user(request)
    if not user: return RedirectResponse("/login", 303)
    tool = own_tool(user, slug)
    tool.setdefault("estimated_delivery_days", 1); tool.setdefault("purchase_notes", ""); tool.setdefault("faq", [])
    return templates.TemplateResponse(request, "tool_manage.html", context(request, tool=tool, active_orders=store.tool_active_orders(slug), can_offer_exclusive=can_offer_exclusive_transfer(user), saved=request.query_params.get("saved") == "1", version_added=request.query_params.get("version_added") == "1"))


@app.post("/seller/tools/{slug}/edit")
async def seller_tool_save(
    request: Request, slug: str, name: str = Form(...), tagline: str = Form(...), description_md: str = Form(...),
    price: int = Form(...), capacity: int = Form(...), support_days: int = Form(...), estimated_delivery_days: int = Form(...),
    purchase_notes: str = Form(""), faq_question: list[str] = Form(default=[]), faq_answer: list[str] = Form(default=[]),
    fulfillment_type: str = Form("custom"), supports_subscription: str = Form("no"), subscription_price: int = Form(0),
    customization_available: str = Form("no"), exclusive_available: str = Form("no"), exclusive_price_min: int = Form(0),
    transfer_assets: list[str] = Form(default=[]), tech_stack: str = Form(""), monthly_revenue: int = Form(0),
    monthly_profit: int = Form(0), monthly_cost: int = Form(0), weekly_ops_hours: float = Form(0), handover_days: int = Form(14),
    exclusive_summary: str = Form(""), transfer_rights_agreement: str = Form("no"), thumbnail: UploadFile | None = None,
):
    user = current_user(request)
    if not user: raise HTTPException(401)
    tool = own_tool(user, slug)
    name = clean_visible_text(name, 1, 60, "商品名")
    tagline = clean_visible_text(tagline, 1, 100, "ひとこと説明")
    description_md = clean_visible_text(description_md, 1, 5000, "商品説明", preserve_lines=True)
    purchase_notes = clean_visible_text(purchase_notes, 0, 2000, "購入にあたってのお願い", preserve_lines=True)
    minimum_price = 100 if tool.get("price_type") == "paid" else 0
    if not minimum_price <= price <= 1_000_000 or fulfillment_type not in {"instant","custom"} or not 1 <= capacity <= 50 or not 0 <= support_days <= 365 or not 1 <= estimated_delivery_days <= 120: raise HTTPException(422)
    if supports_subscription == "yes" and not 100 <= subscription_price <= 1_000_000: raise HTTPException(422)
    if tool.get("price_type") == "consultation": fulfillment_type = "custom"
    if len(faq_question) > 5 or len(faq_answer) > 5: raise HTTPException(422, "よくある質問は5件以内で入力してください")
    faq = []
    for index in range(max(len(faq_question), len(faq_answer))):
        question = clean_visible_text(faq_question[index] if index < len(faq_question) else "", 0, 120, "よくある質問")
        answer = clean_visible_text(faq_answer[index] if index < len(faq_answer) else "", 0, 500, "よくある質問の回答", preserve_lines=True)
        if bool(question) != bool(answer): raise HTTPException(422, "よくある質問は質問と回答をセットで入力してください")
        if question: faq.append({"question":question,"answer":answer})
    transfer_settings = validated_transfer_settings(exclusive_available, exclusive_price_min, transfer_assets, tech_stack, monthly_revenue, monthly_profit, monthly_cost, weekly_ops_hours, handover_days, exclusive_summary, transfer_rights_agreement)
    if transfer_settings["exclusive_available"] and not can_offer_exclusive_transfer(user): raise HTTPException(403, "独占譲渡の出品には本人確認が必要です")
    transfer_fields = ("exclusive_available", "exclusive_price_min", "transfer_assets", "tech_stack", "monthly_revenue", "monthly_profit", "monthly_cost", "weekly_ops_hours", "handover_days", "exclusive_summary")
    old_transfer = tuple(tuple(tool.get(key, [])) if key == "transfer_assets" else tool.get(key) for key in transfer_fields)
    new_transfer = tuple(tuple(transfer_settings.get(key, [])) if key == "transfer_assets" else transfer_settings.get(key) for key in transfer_fields)
    if not transfer_settings["exclusive_available"]:
        transfer_review_status = "not_requested"
    elif old_transfer == new_transfer and tool.get("transfer_review_status") in {"pending", "approved", "rejected"}:
        transfer_review_status = tool["transfer_review_status"]
    else:
        transfer_review_status = "pending"
    was_full = store.tool_active_orders(slug) >= tool.get("capacity", 5)
    thumbnail_url = tool.get("thumbnail_url")
    if thumbnail and thumbnail.filename:
        thumbnail_url = await save_thumbnail(thumbnail, slug)
    with store._lock:
        tool.update({"name":name,"tagline":tagline,"description_md":description_md,"price":price,"capacity":capacity,"support_days":support_days,"estimated_delivery_days":estimated_delivery_days,"fulfillment_type":fulfillment_type,"purchase_notes":purchase_notes,"faq":faq,"supports_subscription":supports_subscription=="yes","subscription_price":subscription_price if supports_subscription=="yes" else 0,"customization_available":customization_available == "yes" or tool.get("price_type") == "consultation","thumbnail_url":thumbnail_url,"transfer_review_status":transfer_review_status,**transfer_settings})
    if transfer_review_status == "pending" and old_transfer != new_transfer:
        store.audit(user["id"], "transfer.review_requested", slug, request.headers.get("x-request-id", ""))
    if was_full and store.tool_active_orders(slug) < capacity and tool.get("status") == "published":
        store.notify_reopened(slug)
    return RedirectResponse(f"/seller/tools/{slug}/edit?saved=1", 303)


@app.post("/seller/tools/{slug}/versions")
async def seller_tool_add_version(request: Request, slug: str, version: str = Form(...), title: str = Form(...)):
    user = current_user(request)
    if not user: raise HTTPException(401)
    tool = own_tool(user, slug)
    version = clean_visible_text(version, 1, 20, "バージョン")
    title = clean_visible_text(title, 3, 120, "更新内容")
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]{0,19}", version): raise HTTPException(422, "バージョンの形式を確認してください")
    with store._lock:
        versions = tool.setdefault("versions", [])
        if any(item.get("version", "").casefold() == version.casefold() for item in versions): raise HTTPException(409, "同じバージョンは登録済みです")
        versions.insert(0, {"version":version, "title":title, "date":datetime.now(timezone.utc).date().isoformat()})
        follower_ids = [uid for uid, followed_slug in store.update_followers if followed_slug == slug]
    for follower_id in follower_ids:
        store.notify(follower_id, f"{tool['name']} が更新されました", f"v{version} · {title}", f"/tools/{slug}#updates", "updates")
    return RedirectResponse(f"/seller/tools/{slug}/edit?version_added=1", 303)


@app.post("/seller/tools/{slug}/status/{status}")
async def seller_tool_status(request: Request, slug: str, status: str):
    user = current_user(request)
    if not user: raise HTTPException(401)
    tool = own_tool(user, slug)
    if status not in {"published","draft","paused"}: raise HTTPException(422)
    if status == "published" and tool.get("safety_scan",{}).get("status") != "passed":
        raise HTTPException(409, "安全チェック合格後に公開できます")
    previous_status = tool.get("status")
    with store._lock:
        tool["status"] = status; tool["is_published"] = status == "published"
    reopened = store.notify_reopened(slug) if status == "published" and previous_status != "published" else 0
    if reopened:
        store.audit(user["id"], "tool.reopened_notified", f"{slug}:{reopened}", request.headers.get("x-request-id", ""))
    return RedirectResponse("/seller?status_changed=1", 303)


@app.get("/seller/analytics", response_class=HTMLResponse)
async def seller_analytics(request: Request):
    user = current_user(request)
    if not user: return RedirectResponse("/login", 303)
    tools = [x for x in store.tools if x.get("author_id") == user["id"]]
    totals = {"views":sum(x["view_count"] for x in tools),"likes":sum(x["like_count"] for x in tools),"sales":sum(x["sales_count"] for x in tools)}
    totals["conversion"] = round(totals["sales"] / totals["views"] * 100, 1) if totals["views"] else 0
    return templates.TemplateResponse(request, "analytics.html", context(request, tools=tools, totals=totals))


@app.get("/seller/coupons", response_class=HTMLResponse)
async def seller_coupons(request: Request):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/seller/coupons", 303)
    items = [item for item in store.coupons if item["seller_id"] == user["id"]]
    return templates.TemplateResponse(request, "seller_coupons.html", context(request, coupons=items))


@app.post("/seller/coupons")
async def seller_coupon_create(request: Request, code: str = Form(...), percent: int = Form(...), max_discount: int = Form(...), max_uses: int = Form(...), expires_on: str = Form(...)):
    user = current_user(request)
    if not user: raise HTTPException(401)
    code = code.strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{4,20}", code) or not 1 <= percent <= 80 or not 100 <= max_discount <= 100_000 or not 1 <= max_uses <= 10_000:
        raise HTTPException(422, "クーポン設定を確認してください")
    try: expires_at = datetime.strptime(expires_on, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    except ValueError as exc: raise HTTPException(422, "有効期限を確認してください") from exc
    if not datetime.now(timezone.utc) < expires_at <= datetime.now(timezone.utc) + timedelta(days=366):
        raise HTTPException(422, "有効期限は1年以内で設定してください")
    try: store.create_coupon(user, code, percent, max_discount, max_uses, expires_at)
    except ValueError as exc: raise HTTPException(409, "このクーポンコードは使用済みです") from exc
    store.audit(user["id"], "coupon.created", code, request.headers.get("x-request-id", ""))
    return RedirectResponse("/seller/coupons?created=1", 303)


@app.post("/seller/coupons/{coupon_id}/toggle")
async def seller_coupon_toggle(request: Request, coupon_id: str):
    user = current_user(request)
    coupon = user and next((item for item in store.coupons if item["id"] == coupon_id and item["seller_id"] == user["id"]), None)
    if not coupon: raise HTTPException(404)
    with store._lock: coupon["active"] = not coupon.get("active", True)
    return RedirectResponse("/seller/coupons?updated=1", 303)


@app.post("/messages/start/{slug}")
async def start_message(request: Request, slug: str):
    user = current_user(request)
    if not user: return RedirectResponse(f"/login?next=/tools/{slug}", 303)
    tool = store.get_public(slug)
    if not tool: raise HTTPException(404)
    if tool["author_id"] == user["id"]: raise HTTPException(422, "自分自身には相談できません")
    try: conversation = store.conversation_for(user, slug)
    except ValueError as exc:
        if str(exc) == "blocked": raise HTTPException(403, "このユーザーとはメッセージできません") from exc
        raise HTTPException(404) from exc
    return RedirectResponse(f"/messages/{conversation['id']}", 303)


@app.get("/messages", response_class=HTMLResponse)
async def messages(request: Request):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/messages", 303)
    conversations = [x for x in store.conversations if user["id"] in {x["buyer_id"], x["seller_id"]}]
    return templates.TemplateResponse(request, "messages.html", context(request, conversations=conversations, active=None))


@app.get("/messages/{conversation_id}", response_class=HTMLResponse)
async def conversation(request: Request, conversation_id: str):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/messages", 303)
    conversations = [x for x in store.conversations if user["id"] in {x["buyer_id"], x["seller_id"]}]
    active = next((x for x in conversations if x["id"] == conversation_id), None)
    if not active: raise HTTPException(404)
    return templates.TemplateResponse(request, "messages.html", context(request, conversations=conversations, active=active))


@app.post("/messages/{conversation_id}")
async def send_message(request: Request, conversation_id: str, body: str = Form(...)):
    user = current_user(request)
    if not user: return RedirectResponse("/login", 303)
    body = clean_visible_text(body, 1, 1000, "メッセージ", preserve_lines=True)
    try: active = store.add_message(conversation_id, user, body)
    except ValueError: raise HTTPException(403)
    recipient_id = active["seller_id"] if user["id"] == active["buyer_id"] else active["buyer_id"]
    store.notify(recipient_id, f"{user['display_name']}さんからメッセージ", active["tool_name"], f"/messages/{conversation_id}", "messages")
    return RedirectResponse(f"/messages/{conversation_id}", 303)


@app.post("/messages/{conversation_id}/proposal")
async def create_proposal(request: Request, conversation_id: str, title: str = Form(...), detail: str = Form(...), amount: int = Form(...), delivery_days: int = Form(...)):
    user = current_user(request)
    conversation = next((x for x in store.conversations if x["id"] == conversation_id), None)
    if not user or not conversation or user["id"] not in {conversation["buyer_id"],conversation["seller_id"]}: raise HTTPException(403)
    if user["id"] != conversation["seller_id"]: raise HTTPException(403)
    if conversation.get("conversation_type") == "transfer": raise HTTPException(409, "独占譲渡は専用案件画面で進めてください")
    if store.blocked_between(conversation["seller_id"], conversation["seller_username"], conversation["buyer_id"], conversation["buyer_username"]): raise HTTPException(403, "ブロック中は見積りを送れません")
    title = clean_visible_text(title, 3, 80, "見積もり件名")
    detail = clean_visible_text(detail, 3, 1000, "見積もり内容", preserve_lines=True)
    if not 100 <= amount <= 1_000_000 or not 1 <= delivery_days <= 120: raise HTTPException(422)
    try: store.create_proposal(conversation_id, title, detail, amount, delivery_days)
    except ValueError: raise HTTPException(404)
    store.notify(conversation["buyer_id"], "見積り提案が届きました", f"{title} · ¥{amount:,}", f"/messages/{conversation_id}", "messages")
    return RedirectResponse(f"/messages/{conversation_id}", 303)


@app.post("/messages/{conversation_id}/demo-proposal")
async def create_simulated_demo_proposal(request: Request, conversation_id: str, title: str = Form(...), detail: str = Form(...), amount: int = Form(...), delivery_days: int = Form(...)):
    """Let a buyer trigger an explicitly labelled seller simulation in demo only."""
    user = current_user(request)
    conversation = user and next((x for x in store.conversations if x["id"] == conversation_id and x["buyer_id"] == user["id"]), None)
    if not settings.demo_mode or not conversation: raise HTTPException(403)
    if conversation.get("conversation_type") == "transfer": raise HTTPException(409, "独占譲渡は専用案件画面で進めてください")
    if store.blocked_between(conversation["buyer_id"], conversation["buyer_username"], conversation["seller_id"], conversation["seller_username"]): raise HTTPException(403, "ブロック中は見積りを作成できません")
    title = clean_visible_text(title, 3, 80, "見積もり件名")
    detail = clean_visible_text(detail, 3, 1000, "見積もり内容", preserve_lines=True)
    if not 100 <= amount <= 1_000_000 or not 1 <= delivery_days <= 120: raise HTTPException(422)
    try: store.create_proposal(conversation_id, title, detail, amount, delivery_days)
    except ValueError: raise HTTPException(404)
    store.audit(user["id"], "demo.seller_proposal_simulated", conversation_id, request.headers.get("x-request-id", ""))
    store.notify(conversation["buyer_id"], "見積り提案が届きました", f"{title} · ¥{amount:,}", f"/messages/{conversation_id}", "messages")
    return RedirectResponse(f"/messages/{conversation_id}", 303)


@app.post("/messages/{conversation_id}/proposals/{proposal_id}/buy")
async def buy_proposal(request: Request, conversation_id: str, proposal_id: str):
    user = current_user(request)
    if not user: return RedirectResponse("/login", 303)
    conversation = next((x for x in store.conversations if x["id"] == conversation_id and x["buyer_id"] == user["id"]), None)
    proposal = conversation and next((x for x in conversation["proposals"] if x["id"] == proposal_id and x["status"] == "open"), None)
    if not conversation or not proposal or conversation.get("conversation_type") == "transfer": raise HTTPException(409)
    account = store.connected_accounts.get(conversation["seller_id"])
    if not settings.demo_mode and (not settings.stripe_ready or not account or not account.get("charges_enabled")):
        raise HTTPException(503, "販売者の決済受取設定が完了していません")
    try: order = store.buy_proposal(user, conversation_id, proposal_id)
    except ValueError as exc:
        if str(exc) == "blocked": raise HTTPException(403, "ブロック中は購入できません") from exc
        raise HTTPException(409) from exc
    order.update({"buyer_email":user.get("email"),"seller_email":store.registered_users.get(conversation["seller_username"],{}).get("email"),"payment_status":"paid" if settings.demo_mode else "pending","sales_recorded":False,"checkout_cancel_path":f"/messages/{conversation_id}"})
    if not settings.demo_mode:
        try: session = await stripe.create_checkout(order, account["account_id"])
        except RuntimeError as exc:
            with store._lock:
                if order in store.orders: store.orders.remove(order)
                proposal["status"] = "open"
            await persist_state()
            raise HTTPException(502,"決済画面を開始できませんでした") from exc
        order["checkout_session_id"] = session["id"]
        order["checkout_session_url"] = session["url"]
        return RedirectResponse(session["url"],303)
    store.notify(order["buyer_id"], "見積りを購入しました", order["tool_name"], f"/orders/{order['id']}", "transactions")
    store.notify(order["seller_id"], "見積りが購入されました", order["tool_name"], f"/orders/{order['id']}", "transactions")
    return RedirectResponse(f"/orders/{order['id']}", 303)


@app.post("/messages/{conversation_id}/proposals/{proposal_id}/decline")
async def decline_proposal(request: Request, conversation_id: str, proposal_id: str):
    user = current_user(request)
    conversation = user and next((x for x in store.conversations if x["id"] == conversation_id and user["id"] in {x["buyer_id"],x["seller_id"]}), None)
    proposal = conversation and next((x for x in conversation["proposals"] if x["id"] == proposal_id and x["status"] == "open"), None)
    if not proposal or user["id"] != conversation["buyer_id"]: raise HTTPException(403)
    with store._lock:
        if proposal["status"] != "open": raise HTTPException(409)
        proposal["status"] = "declined"
    store.notify(conversation["seller_id"], "見積りが辞退されました", proposal["title"], f"/messages/{conversation_id}", "messages")
    return RedirectResponse(f"/messages/{conversation_id}", 303)


@app.post("/messages/{conversation_id}/proposals/{proposal_id}/edit")
async def edit_proposal(request: Request, conversation_id: str, proposal_id: str, title: str = Form(...), detail: str = Form(...), amount: int = Form(...), delivery_days: int = Form(...)):
    user = current_user(request)
    conversation = user and next((x for x in store.conversations if x["id"] == conversation_id and user["id"] in {x["buyer_id"],x["seller_id"]}), None)
    proposal = conversation and next((x for x in conversation["proposals"] if x["id"] == proposal_id and x["status"] == "open"), None)
    if not proposal or user["id"] != conversation["seller_id"]: raise HTTPException(403)
    if store.blocked_between(conversation["seller_id"], conversation["seller_username"], conversation["buyer_id"], conversation["buyer_username"]): raise HTTPException(403, "ブロック中は見積りを更新できません")
    title = clean_visible_text(title, 3, 80, "見積もり件名")
    detail = clean_visible_text(detail, 3, 1000, "見積もり内容", preserve_lines=True)
    if not 100 <= amount <= 1_000_000 or not 1 <= delivery_days <= 120: raise HTTPException(422)
    with store._lock:
        if proposal["status"] != "open": raise HTTPException(409)
        proposal.update({"title":title,"detail":detail,"amount":amount,"delivery_days":delivery_days,"expires_at":datetime.now(timezone.utc).replace(microsecond=0),"revision":proposal.get("revision",1)+1})
        proposal["expires_at"] += timedelta(days=7)
    store.notify(conversation["buyer_id"], "見積りが更新されました", f"{title} · ¥{amount:,}", f"/messages/{conversation_id}", "messages")
    return RedirectResponse(f"/messages/{conversation_id}", 303)


@app.get("/verification", response_class=HTMLResponse)
async def verification(request: Request):
    if not current_user(request): return RedirectResponse("/login?next=/verification", 303)
    user = current_user(request)
    application = store.identity_applications.get(user["id"])
    return templates.TemplateResponse(request, "verification.html", context(request, application=application, nda_signed=user["id"] in store.nda_signatures))


@app.post("/verification/submit")
async def submit_verification(request: Request, legal_name: str = Form(...), birth_date: str = Form(...), document_type: str = Form(...), address_confirmed: str = Form(...)):
    user = current_user(request)
    if not settings.demo_mode: raise HTTPException(404)
    if not user or document_type not in {"drivers_license","my_number","passport"} or address_confirmed != "yes": raise HTTPException(422)
    clean_visible_text(legal_name, 1, 80, "氏名")
    try: parsed_birth_date = datetime.strptime(birth_date, "%Y-%m-%d").date()
    except ValueError as exc: raise HTTPException(422, "生年月日を確認してください") from exc
    today = datetime.now(timezone.utc).date()
    if parsed_birth_date >= today or parsed_birth_date < today - timedelta(days=365*120): raise HTTPException(422, "生年月日を確認してください")
    # Demo verification deliberately discards entered identity data.
    with store._lock:
        store.identity_applications[user["id"]] = {"document_type":document_type,"demo_input_discarded":True,"status":"pending","submitted_at":datetime.now(timezone.utc),"rejection_reason":None}
    store.audit(user["id"], "identity.submitted", document_type, request.headers.get("x-request-id", ""))
    update_current_session(request, {**user,"identity_status":"pending"})
    return RedirectResponse("/verification?submitted=1", 303)


@app.post("/verification/start")
async def start_identity_verification(request: Request):
    user = current_user(request)
    if not user: raise HTTPException(401)
    if settings.demo_mode: return RedirectResponse("/verification",303)
    if not settings.stripe_ready: raise HTTPException(503,"本人確認サービスが未設定です")
    try: session = await stripe.create_identity_session(user["id"])
    except RuntimeError as exc: raise HTTPException(502,"本人確認を開始できませんでした") from exc
    with store._lock:
        store.identity_applications[user["id"]] = {"status":"pending","provider":"stripe_identity","provider_reference":session["id"],"submitted_at":datetime.now(timezone.utc),"rejection_reason":None}
    store.audit(user["id"],"identity.provider_started",session["id"],request.headers.get("x-request-id", ""))
    return RedirectResponse(session["url"],303)


@app.post("/verification/demo-review")
async def verify_demo(request: Request, result: str = Form("approve")):
    user = current_user(request)
    application = user and store.identity_applications.get(user["id"])
    if not user or not application or not settings.demo_mode or result not in {"approve","reject"}: raise HTTPException(403)
    with store._lock:
        application["status"] = "verified" if result == "approve" else "rejected"
        application["rejection_reason"] = None if result == "approve" else "書類の四隅が確認できませんでした。明るい場所で再提出してください。"
    update_current_session(request, {**user,"is_verified":result=="approve","identity_status":application["status"]})
    return RedirectResponse(f"/verification?{'completed' if result=='approve' else 'rejected'}=1", 303)


@app.post("/verification")
async def verify_demo_compat(request: Request):
    """Keep the original one-click demo path available for automated demos."""
    user = current_user(request)
    if not user or not settings.demo_mode: raise HTTPException(403)
    with store._lock:
        store.identity_applications[user["id"]] = {"document_type":"drivers_license","demo_input_discarded":True,"status":"verified","submitted_at":datetime.now(timezone.utc),"rejection_reason":None}
    update_current_session(request, {**user,"is_verified":True,"identity_status":"verified"})
    return RedirectResponse("/verification?completed=1", 303)


@app.get("/nda", response_class=HTMLResponse)
async def nda(request: Request):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/nda", 303)
    application = store.identity_applications.get(user["id"])
    can_sign = bool(user.get("is_verified") or (application and application.get("status") == "verified"))
    return templates.TemplateResponse(request, "nda.html", context(request, signed=user["id"] in store.nda_signatures, can_sign=can_sign))


@app.post("/nda")
async def sign_nda(request: Request, agree: str = Form(...)):
    user = current_user(request)
    if not user or agree != "yes": raise HTTPException(422)
    application = store.identity_applications.get(user["id"])
    if not user.get("is_verified") and not (application and application.get("status") == "verified"):
        raise HTTPException(403, "機密保持契約には本人確認が必要です")
    with store._lock:
        store.nda_signatures.add(user["id"])
        signed_at = datetime.now(timezone.utc)
        store.nda_records[user["id"]] = {"version":"2026-07-16", "signed_at":signed_at, "username":user["username"]}
        store.legal_consents.append({"user_id":user["id"], "document":"nda", "version":"2026-07-16", "accepted_at":signed_at})
    store.audit(user["id"], "nda.signed", "2026-07-16", request.headers.get("x-request-id", ""))
    return RedirectResponse("/nda?signed=1", 303)


@app.get("/security", response_class=HTMLResponse)
async def security(request: Request):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/security", 303)
    settings_data = store.security_settings.setdefault(user["id"], {"two_factor":False,"login_alerts":True,"sessions":1})
    settings_data["sessions"] = sum(1 for record in store.account_sessions.values() if record.get("user",{}).get("id") == user["id"])
    deletion = next((item for item in store.account_deletions if item["user_id"] == user["id"] and item["status"] == "scheduled"), None)
    return templates.TemplateResponse(request, "security.html", context(request, security=settings_data, deletion=deletion, mfa_ready=settings.supabase_ready, mfa_recent=mfa.recent(request)))


@app.get("/security/mfa", response_class=HTMLResponse)
async def mfa_page(request: Request, next: str = "/security"):
    if not current_user(request):
        return RedirectResponse("/login?next=/security/mfa", 303)
    if not settings.supabase_ready:
        raise HTTPException(503, "本番の2段階認証にはSupabase Authへの接続が必要です")
    factors = await mfa.factors(request)
    verified = [f for f in factors if f.get("status") == "verified"]
    return templates.TemplateResponse(request, "mfa.html", context(request, factors=verified, next=safe_next(next), enrollment=None, mfa_error=None))


@app.post("/security/mfa/enroll", response_class=HTMLResponse)
async def mfa_enroll(request: Request, next: str = Form("/security")):
    data = await mfa.enroll(request)
    # Use an image data URL, not raw provider SVG markup in the document.
    qr = str(data.get("totp", {}).get("qr_code", ""))
    enrollment = {"id": data["id"], "secret": data.get("totp", {}).get("secret", ""),
                  "qr": "data:image/svg+xml;base64," + base64.b64encode(qr.encode()).decode() if qr.startswith("<svg") else ""}
    store.audit(current_user(request)["id"], "security.mfa_enrollment_started", "totp")
    return templates.TemplateResponse(request, "mfa.html", context(request, factors=[], next=safe_next(next), enrollment=enrollment, mfa_error=None))


@app.post("/security/mfa/verify")
async def mfa_verify(request: Request, factor_id: str = Form(...), code: str = Form(...), next: str = Form("/security")):
    try:
        await mfa.verify(request, factor_id, code.strip())
    except HTTPException as exc:
        if exc.status_code not in {400, 422}:
            raise
        factors = await mfa.factors(request)
        return templates.TemplateResponse(request, "mfa.html", context(request, factors=factors, next=safe_next(next), enrollment=None, mfa_error=exc.detail), status_code=exc.status_code)
    store.audit(current_user(request)["id"], "security.mfa_verified", "totp")
    return RedirectResponse(safe_next(next), 303)


@app.post("/security/two-factor")
async def toggle_two_factor(request: Request):
    user = current_user(request)
    if not user: raise HTTPException(401)
    if not settings.demo_mode:
        raise HTTPException(503, "2段階認証は本番認証基盤側のMFA設定が必要です")
    data = store.security_settings.setdefault(user["id"], {"two_factor":False,"login_alerts":True,"sessions":1})
    data["two_factor"] = not data["two_factor"]
    store.audit(user["id"], "security.demo_two_factor_toggled", str(data["two_factor"]), request.headers.get("x-request-id", ""))
    return RedirectResponse(f"/security?two_factor={int(data['two_factor'])}", 303)


@app.post("/security/password-reset")
async def password_reset_demo(request: Request):
    user = current_user(request)
    if not user: raise HTTPException(401)
    if settings.supabase_ready and user.get("email"):
        async with httpx.AsyncClient(timeout=10) as client:
            result = await client.post(f"{settings.supabase_url}/auth/v1/recover", headers={"apikey":settings.supabase_anon_key}, json={"email":user["email"],"redirect_to":f"{settings.site_base_url}/auth/recovery"})
        if result.status_code >= 400: raise HTTPException(502,"再設定メールを送信できませんでした")
    store.audit(user["id"],"security.password_reset_requested",user["id"],request.headers.get("x-request-id", ""))
    return RedirectResponse("/security?reset_sent=1", 303)


@app.post("/security/sessions/revoke-others")
async def revoke_other_sessions(request: Request):
    user = current_user(request)
    if not user: raise HTTPException(401)
    revoked = revoke_user_sessions(user["id"], request.session.get("sid"))
    store.audit(user["id"], "security.sessions_revoked", str(revoked), request.headers.get("x-request-id", ""))
    return RedirectResponse(f"/security?sessions_revoked={revoked}", 303)


@app.post("/api/tools/{slug}/like")
async def like(request: Request, slug: str):
    user = current_user(request)
    if not user: return JSONResponse({"error":"login_required"}, status_code=401)
    if not store.get(slug): raise HTTPException(404)
    try: liked, count = store.toggle_like(user["id"], slug, user["username"])
    except ValueError as exc: raise HTTPException(403, "この商品はお気に入りに追加できません") from exc
    return {"liked":liked,"count":count}


@app.post("/api/report")
async def report(request: Request, target_type: str = Form(...), target_id: str = Form(...), reason: str = Form("")):
    user = current_user(request)
    if not user: return RedirectResponse("/login", 303)
    if target_type not in {"tool","user","message","request"}: raise HTTPException(422)
    target_id = target_id.strip()[:200]
    exists = {
        "tool": bool(store.get(target_id)),
        "user": bool(any(tool.get("author_username") == target_id for tool in store.tools) or target_id in store.registered_users),
        "message": any(any(str(message.get("id", "")) == target_id for message in conversation.get("messages", [])) for conversation in store.conversations),
        "request": any(item["id"] == target_id for item in store.requests),
    }[target_type]
    if not target_id or not exists: raise HTTPException(404, "通報対象が見つかりません")
    reason = clean_visible_text(reason, 0, 500, "通報理由", preserve_lines=True)
    report_id = hashlib.sha1(f'{user["id"]}{target_type}{target_id}{time.time_ns()}'.encode()).hexdigest()[:16]
    with store._lock:
        store.reports.append({"id":report_id,"target_type":target_type,"target_id":target_id,"reason":reason,"reporter":user["username"],"status":"open","created_at":datetime.now(timezone.utc)})
    store.audit(user["id"], "report.created", f"{target_type}:{target_id}", request.headers.get("x-request-id", ""))
    referer_path = urlparse(request.headers.get("referer", "")).path or "/tools"
    return RedirectResponse(f"{safe_next(referer_path)}?reported=1", 303)


@app.get("/login", response_class=HTMLResponse)
async def login(request: Request, next: str = "/"):
    return templates.TemplateResponse(request, "login.html", context(request, next=safe_next(next), oauth_ready=settings.supabase_ready))


@app.post("/login")
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...), next_path: str = Form("/", alias="next")):
    email = email.strip().lower()
    if len(email) > 254 or len(password) > 128: raise HTTPException(401, "メールアドレスまたはパスワードが違います")
    redirect_path = safe_next(next_path)
    if settings.is_production and not settings.supabase_ready: raise HTTPException(503, "認証基盤が未設定です")
    if settings.supabase_ready:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                result = await client.post(f"{settings.supabase_url}/auth/v1/token?grant_type=password",headers={"apikey":settings.supabase_anon_key,"Content-Type":"application/json"},json={"email":email,"password":password})
                if result.status_code != 200: return templates.TemplateResponse(request, "login.html", context(request, next=redirect_path, oauth_ready=settings.supabase_ready, error="メールアドレスまたはパスワードが違います", email=email), status_code=401)
                auth_user = result.json()["user"]
                profile_response = await client.get(f"{settings.supabase_url}/rest/v1/profiles?id=eq.{auth_user['id']}&select=*,creator_badges(*)",headers={"apikey":settings.supabase_anon_key,"Authorization":f"Bearer {result.json()['access_token']}"})
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise HTTPException(502, "認証サービスへ接続できませんでした") from exc
        profiles = profile_response.json() if profile_response.status_code == 200 else []
        if not profiles or profiles[0].get("is_banned"): raise HTTPException(403, "このアカウントではログインできません")
        metadata = auth_user.get("user_metadata") or {}; profile = profiles[0]
        session_user = {"id":auth_user["id"],"username":profile.get("username") or metadata.get("user_name") or f"user_{auth_user['id'][:8]}","display_name":profile.get("display_name") or metadata.get("display_name") or "ユーザー","email":auth_user.get("email"),"avatar_url":profile.get("avatar_url") or metadata.get("avatar_url"),"bio":profile.get("bio") or "","headline":profile.get("headline") or "","skills":profile.get("skills") or [],"experience":profile.get("experience") or [],"portfolio":profile.get("portfolio") or [],"availability":profile.get("availability") or "受付状況未設定","response_time":profile.get("response_time") or "未設定","pricing_note":profile.get("pricing_note") or "","x_url":profile.get("x_url") or "","website_url":profile.get("website_url") or "","is_verified":profile.get("identity_status")=="verified","identity_status":profile.get("identity_status") or "not_started"}
        session_user.update(profile_badge_values(profile))
        with store._lock:
            entry = store.registered_users.setdefault(session_user["username"], {})
            entry.update({key:session_user.get(key) for key in PROFILE_CACHE_FIELDS})
    else:
        entry = next(({"username":username,**data} for username,data in store.registered_users.items() if data.get("email")==email),None)
        if not verify_password(password, entry and entry.get("password_salt"), entry and entry.get("password_hash")):
            return templates.TemplateResponse(request, "login.html", context(request, next=redirect_path, oauth_ready=settings.supabase_ready, error="メールアドレスまたはパスワードが違います", email=email), status_code=401)
        if entry.get("is_banned"): raise HTTPException(403, "このアカウントではログインできません")
        identity = store.identity_applications.get(entry["id"], {})
        session_user = {"id":entry["id"],"username":entry["username"],"display_name":entry["display_name"],"email":entry["email"],"avatar_url":entry.get("avatar_url"),"headline":entry.get("headline", ""),"bio":entry.get("bio", ""),"skills":entry.get("skills", []),"experience":entry.get("experience", []),"portfolio":entry.get("portfolio", []),"availability":entry.get("availability", "受付状況未設定"),"response_time":entry.get("response_time", "未設定"),"pricing_note":entry.get("pricing_note", ""),"x_url":entry.get("x_url", ""),"website_url":entry.get("website_url", ""),"is_verified":identity.get("status") == "verified","identity_status":identity.get("status", "not_started"),**creator_badges_for(entry["username"])}
    establish_session(request, session_user)
    if settings.supabase_ready:
        mfa.attach_tokens(request, result.json())
    store.audit(session_user["id"],"account.logged_in",session_user["username"],request.headers.get("x-request-id", ""))
    if settings.email_ready and not await send_email_safely(session_user.get("email"), "ツールバコへのログインを確認しました", "あなたのアカウントへのログインがありました。心当たりがない場合は、すぐにパスワードを再設定し、サポートへご連絡ください。"):
        store.audit(session_user["id"], "email.login_alert_failed", session_user["username"], request.headers.get("x-request-id", ""))
    return RedirectResponse(redirect_path,303)


@app.get("/signup", response_class=HTMLResponse)
async def signup(request: Request, next: str = "/settings"):
    return templates.TemplateResponse(request, "signup.html", context(request, oauth_ready=settings.supabase_ready, next=safe_next(next)))


@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password(request: Request):
    return templates.TemplateResponse(request, "forgot_password.html", context(request, submitted=False))


@app.post("/forgot-password", response_class=HTMLResponse)
async def forgot_password_submit(request: Request, email: str = Form(...)):
    email = email.strip().lower()
    if len(email) <= 254 and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) and settings.supabase_ready:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(f"{settings.supabase_url}/auth/v1/recover", headers={"apikey":settings.supabase_anon_key}, json={"email":email,"redirect_to":f"{settings.site_base_url}/auth/recovery"})
        except httpx.HTTPError:
            pass
    # Always return the same response to prevent account enumeration.
    return templates.TemplateResponse(request, "forgot_password.html", context(request, submitted=True))


@app.get("/auth/recovery", response_class=HTMLResponse)
async def password_recovery(request: Request):
    return templates.TemplateResponse(request, "password_recovery.html", context(request, error=None))


@app.post("/auth/recovery", response_class=HTMLResponse)
async def password_recovery_submit(request: Request, access_token: str = Form(...), password: str = Form(...), password_confirmation: str = Form(...)):
    if not settings.supabase_ready:
        raise HTTPException(503, "パスワード再設定基盤が未設定です")
    if not 10 <= len(password) <= 128 or password != password_confirmation or not 20 <= len(access_token) <= 4096:
        return templates.TemplateResponse(request, "password_recovery.html", context(request, error="パスワードは10文字以上で、確認欄と同じ内容を入力してください。"), status_code=422)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            result = await client.put(
                f"{settings.supabase_url}/auth/v1/user",
                headers={"apikey":settings.supabase_anon_key,"Authorization":f"Bearer {access_token}","Content-Type":"application/json"},
                json={"password":password},
            )
    except httpx.HTTPError:
        result = None
    if not result or result.status_code >= 400:
        return templates.TemplateResponse(request, "password_recovery.html", context(request, error="再設定リンクが無効または期限切れです。もう一度メールを送信してください。"), status_code=400)
    auth_user = result.json()
    if auth_user.get("id"):
        revoke_user_sessions(auth_user["id"])
        store.audit(auth_user["id"], "account.password_recovered", auth_user.get("email", ""), request.headers.get("x-request-id", ""))
    request.session.clear()
    return RedirectResponse("/login?password_reset=1", 303)


@app.post("/signup")
async def signup_submit(request: Request, display_name: str = Form(...), username: str = Form(...), email: str = Form(...), password: str = Form(...), password_confirmation: str = Form(""), terms_agreement: str = Form(...), next_path: str = Form("/settings", alias="next")):
    redirect_path = safe_next(next_path)
    display_name = clean_visible_text(display_name, 1, 60, "表示名")
    username = username.strip().lower()
    email = email.strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{3,30}", username) or not 10 <= len(password) <= 128 or (password_confirmation and password_confirmation != password) or len(email) > 254 or terms_agreement != "yes" or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email): raise HTTPException(422, "入力内容を確認してください")
    if username in store.registered_users or any(x.get("email") == email for x in store.registered_users.values()): raise HTTPException(409, "同じユーザー名またはメールアドレスが登録済みです")
    if settings.is_production and not settings.supabase_ready: raise HTTPException(503, "認証基盤が未設定です")
    if settings.supabase_ready:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                result = await client.post(f"{settings.supabase_url}/auth/v1/signup", headers={"apikey":settings.supabase_anon_key}, json={"email":email,"password":password,"data":{"display_name":display_name,"user_name":username,"terms_version":"2026-07-22","privacy_version":"2026-07-22"}})
        except httpx.HTTPError as exc:
            raise HTTPException(502, "登録サービスへ接続できませんでした") from exc
        if result.status_code >= 400: raise HTTPException(400, "登録できませんでした")
        return RedirectResponse(f"/login?{urlencode({'registered':'1','next':redirect_path})}", 303)
    user_id = secrets.token_hex(16)
    password_salt, password_hash = hash_password(password)
    with store._lock:
        if username in store.registered_users or any(item.get("email") == email for item in store.registered_users.values()): raise HTTPException(409, "同じユーザー名またはメールアドレスが登録済みです")
        store.registered_users[username] = {"id":user_id,"email":email,"display_name":display_name,"bio":"","x_url":"","website_url":"","password_salt":password_salt,"password_hash":password_hash}
        store.legal_consents.extend([{"user_id":user_id,"document_type":"terms","version":"2026-07-22","created_at":datetime.now(timezone.utc)},{"user_id":user_id,"document_type":"privacy","version":"2026-07-22","created_at":datetime.now(timezone.utc)}])
    establish_session(request, {"id":user_id,"username":username,"display_name":display_name,"email":email,"avatar_url":None,"bio":"","is_verified":False})
    store.audit(user_id, "account.created", username, request.headers.get("x-request-id", ""))
    separator = "&" if "?" in redirect_path else "?"
    return RedirectResponse(f"{redirect_path}{separator}welcome=1", 303)


@app.get("/auth/oauth/{provider}")
async def oauth(request: Request, provider: str, next: str = "/"):
    if provider not in {"twitter", "google"}: raise HTTPException(404)
    next = safe_next(next)
    if not settings.supabase_ready:
        if settings.is_production: raise HTTPException(503, "認証基盤が未設定です")
        return RedirectResponse(f"/auth/demo?next={quote(next)}", 303)
    oauth_state = secrets.token_urlsafe(24)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
    request.session["oauth_state"] = oauth_state
    request.session["oauth_code_verifier"] = code_verifier
    callback = f"{settings.site_base_url}/auth/callback?{urlencode({'next':next,'state':oauth_state})}"
    authorize_query = urlencode({
        "provider": provider,
        "redirect_to": callback,
        "code_challenge": code_challenge,
        "code_challenge_method": "s256",
    })
    return RedirectResponse(f"{settings.supabase_url}/auth/v1/authorize?{authorize_query}", 303)


@app.get("/auth/demo")
async def demo_login(request: Request, next: str = "/"):
    if not settings.demo_mode: raise HTTPException(404)
    establish_session(request, {"id":"00000000-0000-0000-0000-000000000001","username":"demo_creator","display_name":"デモクリエイター","avatar_url":None,"bio":"AIと小さな便利を作っています。"})
    return RedirectResponse(safe_next(next), 303)


@app.get("/auth/callback", response_class=HTMLResponse)
async def auth_callback(request: Request, code: str = "", state: str = "", next: str = "/", error: str = ""):
    """Complete social login server-side using one-time PKCE credentials."""
    expected_state = str(request.session.pop("oauth_state", ""))
    code_verifier = str(request.session.pop("oauth_code_verifier", ""))
    if error or not code or not code_verifier or not expected_state or not hmac.compare_digest(state, expected_state):
        return templates.TemplateResponse(request, "auth_callback.html", context(request, auth_error="ログインを確認できませんでした。最初からやり直してください。"), status_code=400)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            token_response = await client.post(
                f"{settings.supabase_url}/auth/v1/token?grant_type=pkce",
                headers={**supabase_headers(settings.supabase_anon_key),"Content-Type":"application/json"},
                json={"auth_code":code,"code_verifier":code_verifier},
            )
            token_data = token_response.json() if token_response.status_code == 200 else {}
            token = str(token_data.get("access_token", ""))
            auth_user = token_data.get("user") or {}
            profile_response = await client.get(
                f"{settings.supabase_url}/rest/v1/profiles?id=eq.{auth_user.get('id','')}&select=*,creator_badges(*)",
                headers={"apikey":settings.supabase_anon_key,"Authorization":f"Bearer {token}"},
            ) if token and auth_user.get("id") else None
    except (httpx.HTTPError, ValueError):
        token_response = None
        profile_response = None
        auth_user = {}
    profiles = profile_response.json() if profile_response is not None and profile_response.status_code == 200 else []
    if token_response is None or token_response.status_code != 200 or not profiles or profiles[0].get("is_banned"):
        return templates.TemplateResponse(request, "auth_callback.html", context(request, auth_error="アカウントを確認できませんでした。運営へお問い合わせください。"), status_code=403)
    metadata = auth_user.get("user_metadata") or {}; profile = profiles[0]
    session_user = {
        "id":auth_user["id"],
        "username":profile.get("username") or metadata.get("user_name") or f"user_{auth_user['id'][:8]}",
        "display_name":profile.get("display_name") or metadata.get("full_name") or metadata.get("name") or "ユーザー",
        "email":auth_user.get("email"),
        "avatar_url":profile.get("avatar_url") or metadata.get("avatar_url"),
        "headline":profile.get("headline") or "",
        "bio":profile.get("bio") or "",
        "skills":profile.get("skills") or [],
        "experience":profile.get("experience") or [],
        "portfolio":profile.get("portfolio") or [],
        "availability":profile.get("availability") or "受付状況未設定",
        "response_time":profile.get("response_time") or "未設定",
        "pricing_note":profile.get("pricing_note") or "",
        "x_url":profile.get("x_url") or "",
        "website_url":profile.get("website_url") or "",
        "is_verified":profile.get("identity_status") == "verified",
        "identity_status":profile.get("identity_status") or "not_started",
    }
    session_user.update(profile_badge_values(profile))
    establish_session(request, session_user)
    with store._lock:
        entry = store.registered_users.setdefault(session_user["username"], {})
        entry.update({key:session_user.get(key) for key in PROFILE_CACHE_FIELDS})
    store.audit(session_user["id"], "account.oauth_logged_in", session_user["username"], request.headers.get("x-request-id", ""))
    mfa.attach_tokens(request, token_data)
    await persist_state()
    return RedirectResponse(safe_next(next), 303)


@app.post("/auth/session")
async def auth_session(request: Request):
    """Reject the retired implicit-token callback; OAuth now uses PKCE."""
    return JSONResponse({"error":"implicit_oauth_disabled"}, status_code=410)


@app.post("/logout")
async def logout(request: Request):
    user = current_user(request)
    sid = request.session.get("sid")
    if sid: store.account_sessions.pop(sid, None)
    request.session.clear()
    if user:
        store.audit(user["id"], "account.logged_out", user["username"], request.headers.get("x-request-id", ""))
    return RedirectResponse("/", 303)


def creator_snapshot(username: str) -> tuple[dict, list[dict], list[dict]] | None:
    """Build the public creator card/profile from one consistent source."""
    items = [item for item in store.list_tools() if item.get("author_username") == username]
    account = store.registered_users.get(username)
    if not items and not account:
        return None
    reviews = sorted(
        [
            review for review in store.reviews
            if review.get("published", True) and (
                review.get("seller_username") == username
                or (username == "demo_creator" and review.get("seller_id") == "00000000-0000-0000-0000-000000000001")
            )
        ],
        key=lambda review: review["created_at"],
        reverse=True,
    )
    rating = round(sum(review["rating"] for review in reviews) / len(reviews), 1) if reviews else None
    completed_sales = sum(item.get("sales_count", 0) for item in items) or sum(
        1 for order in store.orders if order.get("seller_username") == username and order.get("status") == "completed"
    )
    verification = account and store.identity_applications.get(account.get("id"))
    verified = username == "demo_creator" or bool(verification and verification.get("status") == "verified")
    fallback_name = items[0]["author_name"] if items else username
    profile_data = {
        "username": username,
        "display_name": (account or {}).get("display_name") or fallback_name,
        "headline": (account or {}).get("headline", ""),
        "bio": (account or {}).get("bio", ""),
        "skills": list((account or {}).get("skills") or []),
        "experience": list((account or {}).get("experience") or []),
        "portfolio": list((account or {}).get("portfolio") or []),
        "availability": (account or {}).get("availability") or "受付状況未設定",
        "response_time": (account or {}).get("response_time") or "未設定",
        "pricing_note": (account or {}).get("pricing_note", ""),
        "x_url": (account or {}).get("x_url", ""),
        "website_url": (account or {}).get("website_url", ""),
        "rating": rating,
        "review_count": len(reviews),
        "completed_sales": completed_sales,
        "verified": verified,
        "nda_signed": bool(account and account.get("id") in store.nda_signatures),
        "rank": store.creator_rank(username, verified),
        "followers": sum(1 for _, followed_username in store.creator_follows if followed_username == username),
        "categories": sorted({item.get("category", "") for item in items if item.get("category")}),
        "tool_count": len(items),
        "latest_tool_at": max((item.get("created_at") for item in items), default=datetime.min.replace(tzinfo=timezone.utc)),
        **creator_badges_for(username),
    }
    return profile_data, items, reviews


@app.get("/creators", response_class=HTMLResponse)
async def creators(request: Request, q: str = "", category: str = "", verified: str = "", badge: str = "", sort: str = "recommended"):
    q = q.strip()[:100]
    category = category if category in CATEGORIES else ""
    verified_only = verified == "1"
    badge = badge if badge in {"certified", "founding"} else ""
    sort = sort if sort in {"recommended", "rating", "sales", "new"} else "recommended"
    usernames = {item.get("author_username") for item in store.list_tools() if item.get("author_username")}
    usernames.update(store.registered_users)
    cards = []
    for username in usernames:
        snapshot = creator_snapshot(username)
        if not snapshot:
            continue
        card, items, _ = snapshot
        if not items:
            continue
        if category and not any(item.get("category") == category for item in items):
            continue
        if verified_only and not card["verified"]:
            continue
        if badge == "certified" and not card["is_certified_creator"]:
            continue
        if badge == "founding" and not card["is_founding_member"]:
            continue
        if q:
            haystack = " ".join([
                card["display_name"], card["headline"], card["bio"], *card["skills"],
                *(item.get("name", "") for item in items), *(item.get("tagline", "") for item in items),
            ]).casefold()
            if q.casefold() not in haystack:
                continue
        cards.append(card)
    if sort == "rating":
        cards.sort(key=lambda item: (item["rating"] or 0, item["review_count"]), reverse=True)
    elif sort == "sales":
        cards.sort(key=lambda item: item["completed_sales"], reverse=True)
    elif sort == "new":
        cards.sort(key=lambda item: item["latest_tool_at"], reverse=True)
    else:
        cards.sort(key=lambda item: (item["is_certified_creator"], item["verified"], item["rating"] or 0, item["completed_sales"], item["tool_count"]), reverse=True)
    return templates.TemplateResponse(request, "creators.html", context(request, creators=cards, q=q, selected_category=category, verified_only=verified_only, selected_badge=badge, sort=sort))


@app.get("/u/{username}", response_class=HTMLResponse)
async def profile(request: Request, username: str):
    snapshot = creator_snapshot(username)
    if not snapshot: raise HTTPException(404, "ユーザーが見つかりません")
    profile_data, items, reviews = snapshot
    viewer = current_user(request); followed = bool(viewer and (viewer["id"],username) in store.creator_follows)
    blocked_by_viewer = bool(viewer and (viewer["id"], username) in store.blocks)
    reference = store.user_reference(username)
    blocked = bool(viewer and reference and store.blocked_between(viewer["id"], viewer["username"], reference["id"], username))
    return templates.TemplateResponse(request, "profile.html", context(request, profile=profile_data, tools=items, reviews=reviews, followed=followed, blocked=blocked, blocked_by_viewer=blocked_by_viewer))


@app.post("/u/{username}/consult")
async def consult_creator(request: Request, username: str):
    user = current_user(request)
    if not user: return RedirectResponse(f"/login?next=/u/{username}", 303)
    if username == user["username"]: raise HTTPException(422, "自分自身には相談できません")
    item = next((tool for tool in store.list_tools() if tool.get("author_username") == username), None)
    if not item: raise HTTPException(409, "現在相談できる出品がありません")
    try: conversation = store.conversation_for(user, item["slug"])
    except ValueError as exc: raise HTTPException(403, "このユーザーとは相談できません") from exc
    return RedirectResponse(f"/messages/{conversation['id']}", 303)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/settings", 303)
    profile_settings = {**user, **store.registered_users.get(user["username"], {})}
    return templates.TemplateResponse(request, "settings.html", context(request, profile_settings=profile_settings))


@app.get("/settings/blocks", response_class=HTMLResponse)
async def blocked_users_settings(request: Request):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/settings/blocks", 303)
    usernames = sorted(username for blocker_id, username in store.blocks if blocker_id == user["id"])
    blocked_users = []
    for username in usernames:
        snapshot = creator_snapshot(username)
        reference = store.user_reference(username)
        blocked_users.append({"username":username, "display_name":snapshot[0]["display_name"] if snapshot else username, "id":reference and reference["id"]})
    return templates.TemplateResponse(request, "blocked_users.html", context(request, blocked_users=blocked_users))


@app.post("/settings")
async def settings_save(
    request: Request, display_name: str = Form(...), headline: str = Form(""), bio: str = Form(""),
    skills: str = Form(""), availability: str = Form("受付状況未設定"), response_time: str = Form("未設定"),
    pricing_note: str = Form(""), experience_title: str = Form(""), experience_period: str = Form(""),
    experience_detail: str = Form(""), portfolio_title: str = Form(""), portfolio_url: str = Form(""),
    portfolio_summary: str = Form(""), x_url: str = Form(""), website_url: str = Form(""),
):
    user = current_user(request)
    if not user: return RedirectResponse("/login", 303)
    display_name = clean_visible_text(display_name, 1, 60, "表示名")
    headline = clean_visible_text(headline, 0, 80, "肩書き・ひとこと")
    bio = clean_visible_text(bio, 0, 600, "自己紹介", preserve_lines=True)
    skill_list = []
    for skill in re.split(r"[,、\n]", skills):
        skill = clean_visible_text(skill, 0, 30, "スキル")
        if skill and skill not in skill_list:
            skill_list.append(skill)
    if len(skill_list) > 10: raise HTTPException(422, "スキルは10件以内で入力してください")
    allowed_availability = {"受付中", "内容次第", "満枠対応中", "受付休止中", "受付状況未設定"}
    allowed_response_times = {"1時間以内", "6時間以内", "12時間以内", "24時間以内", "2日以内", "3日以内", "未設定"}
    if availability not in allowed_availability or response_time not in allowed_response_times: raise HTTPException(422, "受付状況または返信目安が不正です")
    pricing_note = clean_visible_text(pricing_note, 0, 240, "料金の目安", preserve_lines=True)
    experience_title = clean_visible_text(experience_title, 0, 80, "経歴の名称")
    experience_period = clean_visible_text(experience_period, 0, 40, "経歴の期間")
    experience_detail = clean_visible_text(experience_detail, 0, 240, "経歴の説明", preserve_lines=True)
    portfolio_title = clean_visible_text(portfolio_title, 0, 80, "制作実績の名称")
    portfolio_summary = clean_visible_text(portfolio_summary, 0, 240, "制作実績の説明", preserve_lines=True)
    portfolio_url = safe_http_url(portfolio_url.strip())
    x_url = safe_http_url(x_url.strip())
    website_url = safe_http_url(website_url.strip())
    experience = [{"title":experience_title,"period":experience_period,"detail":experience_detail}] if any((experience_title, experience_period, experience_detail)) else []
    portfolio = [{"title":portfolio_title,"url":portfolio_url,"summary":portfolio_summary}] if any((portfolio_title, portfolio_url, portfolio_summary)) else []
    profile_values = {"display_name":display_name, "headline":headline, "bio":bio, "skills":skill_list, "experience":experience, "portfolio":portfolio, "availability":availability, "response_time":response_time, "pricing_note":pricing_note, "x_url":x_url, "website_url":website_url}
    if settings.supabase_ready:
        if not settings.supabase_service_role_key:
            raise HTTPException(503, "プロフィール保存基盤が未設定です")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.patch(
                    f"{settings.supabase_url}/rest/v1/profiles?id=eq.{user['id']}",
                    headers={**supabase_headers(settings.supabase_service_role_key),"Content-Type":"application/json","Prefer":"return=representation"},
                    json=profile_values,
                )
        except httpx.HTTPError as exc:
            raise HTTPException(502, "プロフィールを保存できませんでした") from exc
        if response.status_code >= 400 or not response.json():
            raise HTTPException(502, "プロフィールを保存できませんでした")
    with store._lock:
        entry = store.registered_users.setdefault(user["username"], {"id":user["id"], "email":user.get("email")})
        entry.update(profile_values)
        for tool in store.tools:
            if tool.get("author_id") == user["id"]:
                tool["author_name"] = display_name
    update_current_session(request, {**user, **profile_values})
    store.audit(user["id"], "profile.updated", user["username"], request.headers.get("x-request-id", ""))
    return RedirectResponse("/settings?saved=1", 303)


@app.get("/support", response_class=HTMLResponse)
async def support_center(request: Request):
    user = current_user(request)
    cases = [x for x in store.support_cases if user and x["user_id"] == user["id"]]
    return templates.TemplateResponse(request, "support.html", context(request, cases=cases, support_email=settings.support_email))


@app.post("/support")
async def create_support_request(request: Request, category: str = Form(...), subject: str = Form(...), detail: str = Form(...)):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/support", 303)
    if category not in {"account","payment","identity","tool","security","other"}: raise HTTPException(422)
    subject = clean_visible_text(subject, 3, 100, "件名")
    detail = clean_visible_text(detail, 20, 2000, "詳しい状況", preserve_lines=True)
    item = store.create_support_case(user, category, subject, detail)
    store.audit(user["id"], "support.created", item["id"], request.headers.get("x-request-id", ""))
    if user.get("email"):
        try: sent = await email_service.send(user["email"],f"[ツールバコ] お問い合わせを受け付けました #{item['id'][:8]}",f"{subject}\n\n受付番号: {item['id']}\n通常2営業日以内にご連絡します。")
        except httpx.HTTPError: sent = False
        if settings.email_ready and not sent: store.audit(user["id"],"email.delivery_failed",item["id"],request.headers.get("x-request-id", ""))
    return RedirectResponse(f"/support?created={item['id']}",303)


@app.get("/account/export")
async def account_export(request: Request):
    user = current_user(request)
    if not user: raise HTTPException(401)
    def safe_export(value):
        """Remove provider secrets and server paths from a portable user export."""
        if isinstance(value, list): return [safe_export(item) for item in value]
        if not isinstance(value, dict): return value
        blocked = {"path","file_path","payment_reference","checkout_session_id","checkout_url","provider_reference","provider_subscription_id","provider_payment_intent","stripe_payment_intent","buyer_email","seller_email","ip_hash"}
        return {key:safe_export(item) for key,item in value.items() if key not in blocked and not key.startswith(("stripe_","provider_"))}
    with store._lock:
        purchases = safe_export([x for x in store.orders if x["buyer_id"]==user["id"]])
        sales = safe_export([x for x in store.orders if x["seller_id"]==user["id"]])
        reviews = safe_export([x for x in store.reviews if x.get("seller_id")==user["id"] or x.get("author")==user["display_name"]])
        buyer_reviews = safe_export([x for x in store.buyer_reviews if x.get("buyer_id")==user["id"] or x.get("author_id")==user["id"]])
        cases = safe_export([x for x in store.support_cases if x["user_id"]==user["id"]])
        transfer_cases = safe_export([x for x in store.transfer_inquiries if user["id"] in {x["buyer_id"],x["seller_id"]}])
        transfer_ndas = safe_export([x for x in store.transfer_nda_acceptances if x["user_id"]==user["id"]])
    payload = {"exported_at":datetime.now(timezone.utc),"profile":{k:v for k,v in user.items() if k not in {"access_token"}},"purchases":purchases,"sales":sales,"reviews":reviews,"buyer_reviews":buyer_reviews,"support_cases":cases,"transfer_inquiries":transfer_cases,"transfer_nda_acceptances":transfer_ndas}
    store.audit(user["id"], "account.exported", user["id"], request.headers.get("x-request-id", ""))
    return JSONResponse(jsonable_encoder(payload), headers={"Content-Disposition":f'attachment; filename="toolbako-{user["username"]}-export.json"'})


@app.post("/account/delete-request")
async def account_delete_request(request: Request, confirmation: str = Form(...)):
    user = current_user(request)
    if not user or confirmation != "退会する": raise HTTPException(422, "確認欄に「退会する」と入力してください")
    active = any(user["id"] in {x["buyer_id"],x["seller_id"]} and x["status"] not in {"completed","cancelled"} for x in store.orders)
    active_transfer = any(user["id"] in {x["buyer_id"],x["seller_id"]} and x["status"] not in {"declined","closed"} for x in store.transfer_inquiries)
    unsettled_payout = any(p["seller_id"] == user["id"] and p.get("status") not in {"paid", "completed", "cancelled"} for p in store.payouts)
    if active or active_transfer or unsettled_payout or available_balance(user["id"]) > 0: raise HTTPException(409, "未完了の取引・譲渡相談、または未振込・照合中の売上があります")
    with store._lock:
        existing = next((item for item in store.account_deletions if item["user_id"] == user["id"] and item["status"] == "scheduled"), None)
        if existing: raise HTTPException(409, "退会申請はすでに受け付けています")
        item = {"id":hashlib.sha1(f'{user["id"]}{time.time_ns()}'.encode()).hexdigest()[:16],"user_id":user["id"],"status":"scheduled","requested_at":datetime.now(timezone.utc),"delete_after":datetime.now(timezone.utc)+timedelta(days=7)}
        store.account_deletions.append(item)
        store.audit(user["id"], "account.deletion_scheduled", item["id"], request.headers.get("x-request-id", ""))
    revoke_user_sessions(user["id"])
    request.session.clear()
    return RedirectResponse("/?deletion_scheduled=1",303)


@app.post("/account/delete-cancel")
async def account_delete_cancel(request: Request):
    user = current_user(request)
    if not user: return RedirectResponse("/login?next=/security", 303)
    with store._lock:
        item = next((candidate for candidate in store.account_deletions if candidate["user_id"] == user["id"] and candidate["status"] == "scheduled"), None)
        if not item: raise HTTPException(404, "取り消せる退会申請がありません")
        if item["delete_after"] <= datetime.now(timezone.utc): raise HTTPException(409, "取り消し期限を過ぎています")
        item["status"] = "cancelled"
        item["cancelled_at"] = datetime.now(timezone.utc)
        store.audit(user["id"], "account.deletion_cancelled", item["id"], request.headers.get("x-request-id", ""))
    return RedirectResponse("/security?deletion_cancelled=1", 303)


async def sync_creator_badges_to_supabase(account: dict, badge_state: dict[str, object]) -> None:
    """Persist creator credentials with the service role; clients have read-only access."""
    if not settings.supabase_ready:
        return
    if not settings.supabase_service_role_key:
        raise HTTPException(503, "認定情報の保存基盤が未設定です")

    def wire_time(value: object) -> object:
        return value.isoformat() if isinstance(value, datetime) else value

    payload = {
        "profile_id": account["id"],
        "is_founding_member": bool(badge_state.get("is_founding_member")),
        "founding_member_since": wire_time(badge_state.get("founding_member_since")),
        "is_certified_creator": bool(badge_state.get("is_certified_creator")),
        "certified_creator_since": wire_time(badge_state.get("certified_creator_since")),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    headers = {
        **supabase_headers(settings.supabase_service_role_key),
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{settings.supabase_url}/rest/v1/creator_badges?on_conflict=profile_id",
                headers=headers,
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(502, "認定情報を保存できませんでした") from exc
    if response.status_code >= 400:
        raise HTTPException(502, "認定情報を保存できませんでした")


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    user = current_user(request)
    if not is_admin(user): raise HTTPException(403, "管理者のみアクセスできます")
    users = sorted([{"username":username, **account} for username,account in store.registered_users.items()], key=lambda item:item.get("display_name") or item["username"])
    return templates.TemplateResponse(request, "admin.html", context(request, tools=store.tools, users=users, reports=store.reports, cases=store.support_cases, audit_logs=store.audit_logs[:50], readiness=readiness_summary(settings, runtime_database_ready=bool(database_store and await database_store.probe())), finance=store.finance_snapshot(ledger=bool(database_store)), finance_controls_ready=settings.demo_mode or sandbox_payouts_ready(settings), payout_labels=PAYOUT_LABELS))


@app.post("/admin/sellers/{seller_id}/payouts/{action}")
async def admin_seller_payout_hold(request: Request, seller_id: str, action: str):
    user = current_user(request)
    if not is_admin(user) or action not in {"hold", "release"}: raise HTTPException(403)
    if not settings.demo_mode and not sandbox_payouts_ready(settings):
        raise HTTPException(503, "本番の振込保留はStripe Connectと金融台帳を接続した後に有効化されます")
    if not any(order.get("seller_id") == seller_id for order in store.orders): raise HTTPException(404)
    paused = action == "hold"
    store.set_seller_payout_hold(seller_id, paused)
    if not paused and database_store:
        for payout in store.payouts:
            # A refund/unknown outcome is NEVER unblocked by releasing an
            # administrative seller hold. Only this specific hold is resumable.
            if payout.get("finance_v2") and payout["seller_id"] == seller_id and payout["status"] == "held" and payout.get("hold_reason") == "運営による振込保留":
                payout.update(status="requested", hold_reason="")
    store.audit(user["id"], "admin.payout_hold" if paused else "admin.payout_release", seller_id, request.headers.get("x-request-id", ""))
    return RedirectResponse("/admin#finance", 303)


@app.post("/admin/payouts/{payout_id}/cancel")
async def admin_cancel_payout(request: Request, payout_id: str):
    user = current_user(request)
    if not is_admin(user): raise HTTPException(403)
    if not database_store or not sandbox_payouts_ready(settings):
        raise HTTPException(409, "分配取消は接続済みのテスト環境のみ操作できます")
    payout = next((p for p in store.payouts if p["id"] == payout_id and p.get("finance_v2")), None)
    if not payout: raise HTTPException(404)
    if payout.get("provider_payout_id") or await database_store.operation_result(f"bank-payout-{payout_id}") or payout["status"] in {"bank_pending", "paid", "bank_failed"}:
        raise HTTPException(409, "銀行振込を開始した申請は取り消せません。Stripeの結果照合が必要です")
    if payout["status"] == "cancelled":
        return RedirectResponse("/admin#finance", 303)
    payout["status"] = "reversing"
    store.audit(user["id"], "payout.cancel_requested", payout_id, request.headers.get("x-request-id", ""))
    return RedirectResponse("/admin#finance", 303)


@app.post("/admin/payouts/{payout_id}/reconcile/{action}")
async def admin_reconcile_payout(request: Request, payout_id: str, action: str, proposal_id: str = Form("")):
    user = current_user(request)
    if not is_admin(user) or action not in {"propose", "approve"}:
        raise HTTPException(403)
    if not database_store or not sandbox_payouts_ready(settings):
        raise HTTPException(409, "振込の照合は接続済みのテスト環境のみ操作できます")
    if not mfa.recent(request):
        return RedirectResponse("/security/mfa?next=/admin", 303)
    payout = next((p for p in store.payouts if p["id"] == payout_id and p.get("finance_v2")), None)
    if not payout:
        raise HTTPException(404)
    try:
        if action == "propose":
            await propose_recovery(database_store, store, stripe, settings, payout, user["id"])
        else:
            await approve_recovery(database_store, store, stripe, settings, payout, user["id"], proposal_id)
    except (FinanceConflict, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from None
    return RedirectResponse("/admin#finance", 303)


@app.post("/admin/tools/{slug}/toggle")
async def admin_toggle(request: Request, slug: str):
    user = current_user(request)
    if not is_admin(user): raise HTTPException(403)
    item = store.get(slug)
    if not item: raise HTTPException(404)
    with store._lock:
        publishing = not item.get("is_published", True)
        if publishing and item.get("safety_scan",{}).get("status") != "passed":
            raise HTTPException(409, "安全チェック前の商品は公開できません")
        previous_status = item.get("status", "draft")
        item["is_published"] = publishing
        item["status"] = "published" if publishing else "draft"
    if publishing and previous_status != "published":
        store.notify_reopened(slug)
    store.audit(user["id"], "admin.tool_visibility", slug, request.headers.get("x-request-id", ""))
    return RedirectResponse("/admin", 303)


@app.post("/admin/reports/{report_id}/{status}")
async def admin_report_status(request: Request, report_id: str, status: str):
    user = current_user(request)
    if not is_admin(user) or status not in {"resolved","dismissed"}: raise HTTPException(403)
    item = next((x for x in store.reports if x.get("id") == report_id), None)
    if not item: raise HTTPException(404)
    with store._lock:
        item["status"] = status; item["resolved_at"] = datetime.now(timezone.utc)
    store.audit(user["id"], f"admin.report_{status}", report_id, request.headers.get("x-request-id", ""))
    return RedirectResponse("/admin#reports",303)


@app.post("/admin/cases/{case_id}/{status}")
async def admin_case_status(request: Request, case_id: str, status: str):
    user = current_user(request)
    if not is_admin(user) or status not in {"investigating","resolved","rejected"}: raise HTTPException(403)
    item = next((x for x in store.support_cases if x["id"] == case_id), None)
    if not item: raise HTTPException(404)
    with store._lock:
        item["status"] = status; item["updated_at"] = datetime.now(timezone.utc)
        if item.get("order_id"):
            order = next((x for x in store.orders if x["id"] == item["order_id"]), None)
            if order: order["dispute_status"] = status
    store.notify(item["user_id"], f"お問い合わせの状態が更新されました", f"{item['subject']} · {status}", f"/support?case={case_id}", "security" if item.get("category") in {"security","dispute"} else "general")
    store.audit(user["id"], f"admin.case_{status}", case_id, request.headers.get("x-request-id", ""))
    return RedirectResponse("/admin#cases",303)


@app.post("/admin/tools/{slug}/safety/{status}")
async def admin_tool_safety(request: Request, slug: str, status: str):
    user = current_user(request)
    if not is_admin(user) or status not in {"passed","failed","pending"}: raise HTTPException(403)
    item = store.get(slug)
    if not item: raise HTTPException(404)
    labels = {"passed":"安全チェック済み","failed":"要修正","pending":"再審査中"}
    with store._lock:
        item["safety_scan"] = {"status":status,"label":labels[status],"checked_at":datetime.now(timezone.utc).date().isoformat() if status!="pending" else None}
        if status in {"failed","pending"}:
            item["status"] = "paused" if item.get("status") == "published" else item.get("status", "draft")
            item["is_published"] = False
    store.notify(item["author_id"], f"安全審査：{labels[status]}", item["name"], f"/seller/tools/{slug}/edit", "security")
    store.audit(user["id"], f"admin.safety_{status}", slug, request.headers.get("x-request-id", ""))
    return RedirectResponse("/admin#tools",303)


@app.post("/admin/tools/{slug}/transfer-review/{status}")
async def admin_tool_transfer_review(request: Request, slug: str, status: str):
    user = current_user(request)
    if not is_admin(user) or status not in {"approved", "rejected", "pending"}: raise HTTPException(403)
    item = store.get(slug)
    if not item or not item.get("exclusive_available"): raise HTTPException(404)
    if status == "approved":
        seller_verified = identity_verified_by_id(item["author_id"])
        seller_verified = seller_verified or (settings.demo_mode and item.get("author_username") == "demo_creator")
        if not seller_verified: raise HTTPException(409, "販売者の本人確認が完了していません")
    labels = {"approved":"掲載を承認", "rejected":"要修正", "pending":"再審査中"}
    with store._lock:
        item["transfer_review_status"] = status
    store.notify(item["author_id"], f"独占譲渡審査：{labels[status]}", item["name"], f"/seller/tools/{slug}/edit", "security")
    store.audit(user["id"], f"admin.transfer_{status}", slug, request.headers.get("x-request-id", ""))
    return RedirectResponse("/admin#tools",303)


@app.post("/admin/users/{username}/{action}")
async def admin_user_status(request: Request, username: str, action: str):
    user = current_user(request)
    if not is_admin(user) or action not in {"suspend","restore"}: raise HTTPException(403)
    account = store.registered_users.get(username)
    if not account: raise HTTPException(404)
    if username == user["username"]: raise HTTPException(409, "自分自身の利用を停止できません")
    banned = action == "suspend"
    with store._lock:
        account["is_banned"] = banned
        if banned:
            for tool in store.tools:
                if tool.get("author_id") == account.get("id"):
                    tool["status"] = "draft"; tool["is_published"] = False
    if banned: revoke_user_sessions(account["id"])
    store.audit(user["id"], f"admin.user_{action}", username, request.headers.get("x-request-id", ""))
    return RedirectResponse("/admin#users", 303)


@app.post("/admin/users/{username}/badge/{badge}/{action}")
async def admin_creator_badge(request: Request, username: str, badge: str, action: str):
    user = current_user(request)
    if not is_admin(user) or badge not in {"founding", "certified"} or action not in {"grant", "revoke"}:
        raise HTTPException(403)
    account = store.registered_users.get(username)
    if not account:
        raise HTTPException(404)

    enabled = action == "grant"
    changed_at = datetime.now(timezone.utc)
    candidate = creator_badges_for(username)
    if badge == "founding":
        candidate["is_founding_member"] = enabled
        candidate["founding_member_since"] = changed_at if enabled else None
        label = "創設メンバー"
    else:
        candidate["is_certified_creator"] = enabled
        candidate["certified_creator_since"] = changed_at if enabled else None
        label = "認定クリエイター"
    await sync_creator_badges_to_supabase(account, candidate)
    store.set_creator_badge(username, badge, enabled, changed_at)
    store.notify(
        account["id"],
        f"{label}バッジが{'付与' if enabled else '解除'}されました",
        "公開プロフィールと出品ページの表示が更新されました。",
        f"/u/{username}",
        "general",
    )
    store.audit(user["id"], f"admin.creator_badge_{badge}_{action}", username, request.headers.get("x-request-id", ""))
    return RedirectResponse("/admin#users", 303)


@app.get("/about", response_class=HTMLResponse)
@app.get("/terms", response_class=HTMLResponse)
@app.get("/privacy", response_class=HTMLResponse)
@app.get("/tokushoho", response_class=HTMLResponse)
async def static_page(request: Request):
    slug = request.url.path.strip("/")
    return templates.TemplateResponse(request, "static_page.html", context(request, page=slug))


@app.get("/sitemap.xml")
async def sitemap():
    if not settings.is_production:
        return Response('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>', media_type="application/xml")
    static_urls = ["", "/tools", "/transfers", "/creators", "/requests", "/match", "/about", "/terms", "/privacy", "/tokushoho"]
    dynamic_urls = [f"/tools/{x['slug']}" for x in store.list_tools()] + [f"/requests/{x['id']}" for x in store.requests if x.get("status") == "open"]
    dynamic_urls.extend(f"/tools/{x['slug']}/transfer" for x in store.list_tools() if store.transfer_is_public(x))
    profiles = sorted({x["author_username"] for x in store.list_tools()})
    dynamic_urls.extend(f"/u/{username}" for username in profiles)
    urls = list(dict.fromkeys(static_urls + dynamic_urls))
    body = '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(
        f"<url><loc>{xml_escape(f'{settings.site_base_url}{u}')}</loc></url>" for u in urls
    ) + "</urlset>"
    return Response(body, media_type="application/xml")


@app.get("/robots.txt")
async def robots():
    if not settings.is_production:
        # Disallow: / would hide the noindex response headers from crawlers.
        # This is indexing hygiene, not access control or privacy protection.
        return PlainTextResponse("User-agent: *\nAllow: /\n")
    private_paths = ["/admin", "/account", "/auth", "/checkout", "/library", "/messages", "/mypage", "/notifications", "/orders", "/payouts", "/purchases", "/security", "/seller", "/settings", "/subscriptions", "/updates", "/verification"]
    return PlainTextResponse("User-agent: *\nAllow: /\n" + "".join(f"Disallow: {path}\n" for path in private_paths) + f"Sitemap: {settings.site_base_url}/sitemap.xml\n")


@app.get("/healthz")
async def health(): return {"status":"ok", "mode":"supabase" if settings.supabase_ready else "demo"}


@app.get("/readyz")
async def readiness():
    result = readiness_summary(settings, runtime_database_ready=bool(database_store and await database_store.probe()))
    return JSONResponse(result, status_code=200 if result["ready"] or settings.demo_mode else 503)


@app.get("/deploymentz")
async def deployment_health():
    # Allows a secure DB/Auth staging build to start before money movement is
    # approved. /readyz remains the independent, strict public launch verdict.
    configured = all(configuration_checks(settings).values())
    connected = configured and bool(database_store and await database_store.probe())
    return JSONResponse({"status": "ok" if connected else "unavailable", "live_payments_enabled": False},
                        status_code=200 if connected else 503, headers={"Cache-Control": "no-store"})


stripe_webhook_service = StripeWebhookService(settings, store, send_email_safely, journal=database_store)


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    return await stripe_webhook_service.handle(request)
