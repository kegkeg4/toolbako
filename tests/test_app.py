import hashlib
import hmac
import io
import json
import time
import zipfile
from datetime import date, timedelta

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_home_and_discovery():
    home = client.get('/')
    assert home.status_code == 200
    assert client.head('/').status_code == 200
    assert 'つくれる人と' in home.text
    assert 'AI TOOL CONCIERGE' in home.text
    assert '/static/home-premium.css' in home.text
    assert '/static/brand/toolbako-logo-header.png' in home.text
    assert '/static/brand/toolbako-icon.png' in home.text
    response = client.get('/tools?q=議事録')
    assert response.status_code == 200
    assert '議事録マジック' in response.text

def test_official_brand_assets_are_served():
    for path in (
        '/static/brand/toolbako-logo-header.png',
        '/static/brand/toolbako-logo-full.png',
        '/static/brand/toolbako-icon.png',
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers['content-type'] == 'image/png'

def test_operator_information_is_consistent():
    home = client.get('/')
    assert home.status_code == 200
    assert '運営：' in home.text
    assert '合同会社ONE' in home.text
    legal = client.get('/tokushoho')
    assert legal.status_code == 200
    for expected in (
        '合同会社ONE',
        '正式公開前に確定が必要です',
    ):
        assert expected in legal.text
    privacy = client.get('/privacy')
    assert '合同会社ONE' in privacy.text

def test_detail_and_seo():
    response = client.get('/tools/commit-senpai')
    assert response.status_code == 200
    assert 'twitter:card' in response.text
    assert '/static/generated/commit-senpai.jpg' in response.text

def test_protected_create_and_demo_login():
    assert client.get('/tools/new', follow_redirects=False).status_code == 303
    assert client.get('/auth/demo?next=/tools/new', follow_redirects=False).status_code == 303
    assert client.get('/tools/new').status_code == 200

def test_signup_preserves_safe_return_destination():
    member = TestClient(app)
    suffix = str(time.time_ns())[-10:]
    destination = '/tools/workflow-pocket/transfer'
    page = member.get(f'/signup?next={destination}')
    assert page.status_code == 200
    assert f'name="next" value="{destination}"' in page.text
    created = member.post('/signup', data={'display_name':'復帰導線確認','username':f'return_{suffix}','email':f'return_{suffix}@example.com','password':'password123','password_confirmation':'password123','terms_agreement':'yes','next':destination}, follow_redirects=False)
    assert created.status_code == 303
    assert created.headers['location'] == f'{destination}?welcome=1'
    blocked = TestClient(app).get('/signup?next=//example.com')
    assert 'name="next" value="/"' in blocked.text

def test_health_and_sitemap():
    assert client.get('/healthz').json()['status'] == 'ok'
    assert client.get('/sitemap.xml').status_code == 200

def test_marketplace_checkout_and_purchase_history():
    client.get('/auth/demo?next=/')
    checkout = client.get('/checkout/minutes-magic')
    assert checkout.status_code == 200
    assert '¥980' in checkout.text
    complete = client.post('/checkout/minutes-magic', data={'purchase_agreement':'yes'}, follow_redirects=False)
    assert complete.status_code == 303
    history = client.get('/purchases')
    assert '議事録マジック' in history.text

def test_direct_message_flow():
    client.get('/auth/demo?next=/')
    started = client.post('/messages/start/pixel-recipe', follow_redirects=False)
    assert started.status_code == 303
    thread_url = started.headers['location']
    assert client.get(thread_url).status_code == 200
    sent = client.post(thread_url, data={'body':'自社向けに調整できますか？'}, follow_redirects=False)
    assert sent.status_code == 303
    assert '自社向けに調整できますか？' in client.get(thread_url).text

def test_estimate_to_transaction_delivery_and_review():
    client.get('/auth/demo?next=/')
    started = client.post('/messages/start/copy-pocket', follow_redirects=False)
    thread_url = started.headers['location']
    conversation_id = thread_url.rsplit('/', 1)[-1]
    forged = client.post(
        f'/messages/{conversation_id}/proposal',
        data={'title':'購入者が偽装した見積もり','detail':'購入者は販売者の見積もりを作れません','amount':12000,'delivery_days':10},
        follow_redirects=False,
    )
    assert forged.status_code == 403
    proposed = client.post(
        f'/messages/{conversation_id}/demo-proposal',
        data={'title':'専用AI調整プラン','detail':'社内向けに調整します','amount':12000,'delivery_days':10},
        follow_redirects=False,
    )
    assert proposed.status_code == 303
    conversation = next(x for x in __import__('app.data', fromlist=['store']).store.conversations if x['id']==conversation_id)
    proposal_id = conversation['proposals'][0]['id']
    bought = client.post(f'/messages/{conversation_id}/proposals/{proposal_id}/buy', follow_redirects=False)
    assert bought.status_code == 303
    room_url = bought.headers['location']
    assert '取引ルーム' in client.get(room_url).text
    assert client.post(f'{room_url}/demo-deliver', follow_redirects=False).status_code == 303
    assert '正式な納品が届きました' in client.get(room_url).text
    assert client.post(f'{room_url}/actions/accept', follow_redirects=False).status_code == 303
    assert '取引が完了しました' in client.get(room_url).text
    assert client.post(f'{room_url}/submit-review', data={'rating':5,'comment':'とても良かったです'}, follow_redirects=False).status_code == 303

def test_identity_verification_demo():
    client.get('/auth/demo?next=/')
    assert client.get('/verification').status_code == 200
    assert client.post('/verification', follow_redirects=False).status_code == 303
    assert '本人確認が完了しています' in client.get('/verification').text

def test_signup_and_account_surfaces():
    fresh = TestClient(app)
    assert fresh.get('/signup').status_code == 200
    registered = fresh.post('/signup', data={'display_name':'テストユーザー','username':'test_user','email':'test@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False)
    assert registered.status_code == 303
    assert fresh.get('/favorites').status_code == 200
    assert fresh.get('/notifications').status_code == 200

def test_public_request_flow():
    client.get('/auth/demo?next=/')
    assert client.get('/requests').status_code == 200
    created = client.post('/requests/new', data={'title':'在庫を予測するAIツール','category':'データ分析','detail':'CSVから来月の在庫を予測したい','budget_min':30000,'budget_max':100000,'deadline':(date.today() + timedelta(days=14)).isoformat(),'terms_agreement':'yes'}, follow_redirects=False)
    assert created.status_code == 303
    detail_url = created.headers['location']
    assert '在庫を予測するAIツール' in client.get(detail_url).text
    assert client.post(f'{detail_url}/apply', data={'message':'自分で応募','amount':65000,'delivery_days':10}, follow_redirects=False).status_code == 403
    applicant = TestClient(app)
    applicant.post('/signup', data={'display_name':'応募者','username':'request_worker','email':'worker@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False)
    applied = applicant.post(f'{detail_url}/apply', data={'message':'要件を確認して丁寧に対応できます','amount':65000,'delivery_days':10}, follow_redirects=False)
    assert applied.status_code == 303

def test_coupon_and_payout_pages():
    buyer = TestClient(app)
    buyer.post('/signup', data={'display_name':'購入者','username':'coupon_buyer','email':'coupon@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False)
    bought = buyer.post('/checkout/pixel-recipe', data={'coupon':'WELCOME10','options':'team','purchase_agreement':'yes'}, follow_redirects=False)
    assert bought.status_code == 303
    order = __import__('app.data', fromlist=['store']).store.orders[0]
    assert order['discount'] == 550
    assert order['amount'] == 4950
    assert buyer.get('/payouts').status_code == 200

def test_block_user_action():
    client.get('/auth/demo?next=/')
    response = client.post('/u/mugi/block', follow_redirects=False)
    assert response.status_code == 303

def test_ai_tool_match_and_compare():
    match = client.post('/match', data={'purpose':'development','budget':3000,'skill':'developer','data_sensitivity':'high'})
    assert match.status_code == 200
    assert 'コミット先輩' in match.text
    assert '% MATCH' in match.text
    compare = client.get('/compare?slugs=commit-senpai,pixel-recipe')
    assert compare.status_code == 200
    assert 'データの扱い' in compare.text
    assert 'ソースコード' in compare.text

def test_tool_passport_and_update_follow():
    detail = client.get('/tools/commit-senpai')
    assert 'AIツールパスポート' in detail.text
    assert 'コードは端末内で処理・保存なし' in detail.text
    client.get('/auth/demo?next=/')
    followed = client.post('/tools/commit-senpai/follow-updates', follow_redirects=False)
    assert followed.status_code == 303
    assert client.get('/updates').status_code == 200
    assert 'Codexモデル向けレビュー規則を追加' in client.get('/updates').text

def test_security_and_incomplete_surface_fixes():
    fresh = TestClient(app)
    redirected = fresh.get('/auth/demo?next=//example.com', follow_redirects=False)
    assert redirected.headers['location'] == '/'
    assert '実際の請求や送金は発生しません' in fresh.get('/terms').text
    assert 'data-nav-toggle' in fresh.get('/').text
    assert 'aria-expanded="false"' in fresh.get('/').text

def test_payout_cannot_exceed_available_balance():
    seller = TestClient(app)
    seller.post('/signup', data={'display_name':'販売者','username':'safe_seller','email':'seller@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False)
    seller.post('/verification', follow_redirects=False)
    response = seller.post('/payouts', data={'amount':999999}, follow_redirects=False)
    assert response.status_code == 422

def test_invalid_thumbnail_is_rejected():
    creator = TestClient(app)
    creator.post('/signup', data={'display_name':'画像テスト','username':'image_creator','email':'image@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False)
    response = creator.post('/tools/new', data={'name':'画像テストツール','tagline':'画像を確認するテスト','description_md':'説明','category':'業務効率化','distribution':'webapp','price_type':'free','price':'0','prohibited_agreement':'yes'}, files={'thumbnail':('bad.png',b'not-an-image','image/png')})
    assert response.status_code == 422

def test_mypage_purchase_sales_and_reputation():
    member = TestClient(app)
    assert member.get('/mypage', follow_redirects=False).status_code == 303
    member.get('/auth/demo?next=/mypage')
    overview = member.get('/mypage')
    assert overview.status_code == 200
    assert '購入総額' in overview.text
    assert '確定売上' in overview.text
    assert 'クリエイター評価' in overview.text
    buying = member.get('/mypage?tab=buying')
    assert '議事録マジック' in buying.text
    selling = member.get('/mypage?tab=selling')
    assert 'SNS投稿AI カスタマイズ' in selling.text
    reputation = member.get('/mypage?tab=reputation')
    assert '専門用語を使わず説明' in reputation.text

def test_public_creator_profile_has_reviews():
    response = client.get('/u/demo_creator')
    assert response.status_code == 200
    assert '購入者からの口コミ' in response.text
    assert 'KADO商店' in response.text
    assert '本人確認済み' in response.text
    assert '得意なこと・相談の目安' in response.text
    assert '制作実績' in response.text


def test_creator_search_and_rich_profile_settings():
    listing = TestClient(app).get('/creators?q=業務自動化&verified=1')
    assert listing.status_code == 200
    assert 'つくれる人から探す' in listing.text
    assert 'デモクリエイター' in listing.text
    assert listing.headers.get('x-robots-tag') == 'noindex, nofollow'

    member = TestClient(app)
    member.post('/signup', data={'display_name':'設定確認','username':'profile_rich','email':'profile-rich@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False)
    saved = member.post('/settings', data={
        'display_name':'設定確認', 'headline':'個人店のAI導入を支援', 'bio':'初心者にもわかりやすく説明します。',
        'skills':'Python、業務自動化', 'availability':'受付中', 'response_time':'24時間以内',
        'pricing_note':'相談後にお見積りします。', 'experience_title':'業務改善支援', 'experience_period':'2025年〜',
        'experience_detail':'定型業務の自動化を支援。', 'portfolio_title':'予約整理AI',
        'portfolio_url':'https://example.com/work', 'portfolio_summary':'予約情報を見やすく整理。',
        'x_url':'', 'website_url':'https://example.com',
    }, follow_redirects=False)
    assert saved.status_code == 303
    profile = member.get('/u/profile_rich')
    assert profile.status_code == 200
    for expected in ('個人店のAI導入を支援', 'Python', '業務改善支援', '予約整理AI', '24時間以内'):
        assert expected in profile.text


def test_creator_credentials_display_filter_and_admin_controls():
    from app.data import store

    public_profile = TestClient(app).get('/u/demo_creator')
    assert public_profile.status_code == 200
    assert '認定クリエイター' in public_profile.text
    assert '創設メンバー' in public_profile.text
    assert '本人確認、取引実績、対応品質、ツール品質を運営が総合確認' in public_profile.text

    detail = TestClient(app).get('/tools/workflow-pocket')
    assert detail.status_code == 200
    assert '認定クリエイター' in detail.text
    assert '創設メンバー' in detail.text

    filtered = TestClient(app).get('/creators?badge=certified')
    assert filtered.status_code == 200
    assert 'デモクリエイター' in filtered.text
    assert 'value="certified" selected' in filtered.text

    suffix = str(time.time_ns())[-10:]
    username = f'badge_target_{suffix}'
    member = TestClient(app)
    member.post('/signup', data={
        'display_name':'称号確認ユーザー', 'username':username, 'email':f'{username}@example.com',
        'password':'password123', 'terms_agreement':'yes',
    }, follow_redirects=False)
    admin = TestClient(app)
    admin.get('/auth/demo?next=/admin')
    notification_count = len(store.notifications)
    try:
        console = admin.get('/admin')
        assert console.status_code == 200
        assert 'ユーザー・称号管理' in console.text
        assert '認定を付与' in console.text
        assert '創設を付与' in console.text
        assert member.post(f'/admin/users/{username}/badge/certified/grant', follow_redirects=False).status_code == 403
        granted = admin.post(f'/admin/users/{username}/badge/certified/grant', follow_redirects=False)
        assert granted.status_code == 303
        assert store.registered_users[username]['is_certified_creator'] is True
        assert '認定クリエイター' in member.get(f'/u/{username}').text
        assert any(item['user_id'] == store.registered_users[username]['id'] and '認定クリエイター' in item['title'] for item in store.notifications)
        revoked = admin.post(f'/admin/users/{username}/badge/certified/revoke', follow_redirects=False)
        assert revoked.status_code == 303
        assert store.registered_users[username]['is_certified_creator'] is False
    finally:
        store.registered_users.pop(username, None)
        del store.notifications[notification_count:]


def test_auto_close_blind_mutual_reviews_and_business_documents():
    from app.data import DemoStore
    from datetime import datetime, timedelta, timezone
    fresh = DemoStore()
    buyer = {'id':'lifecycle-buyer','username':'lifecycle_buyer','display_name':'購入者'}
    order = fresh.buy(buyer, 'minutes-magic')
    fresh.deliver(order, '正式な納品です')
    order['auto_close_at'] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert fresh.process_due_order_events() >= 1
    assert order['status'] == 'completed' and order['auto_completed'] is True
    seller = {'id':order['seller_id'],'username':order['seller_username'],'display_name':order['seller_name']}
    seller_review = fresh.add_review(order, buyer, 5, '良い取引でした')
    assert seller_review['published'] is False
    buyer_review = fresh.add_buyer_review(order, seller, 5, '連絡がスムーズでした')
    assert buyer_review['published'] is True and seller_review['published'] is True

    member = TestClient(app)
    member.get('/auth/demo?next=/')
    for kind, label in (('estimate','見積書'),('purchase-order','発注書'),('delivery-note','納品書')):
        document = member.get(f'/orders/demo-buy-001/documents/{kind}')
        assert document.status_code == 200
        assert label in document.text


def test_favorite_folders_and_notification_preferences():
    from app.data import store
    member = TestClient(app)
    member.post('/signup', data={'display_name':'整理確認','username':'organize_user','email':'organize@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False)
    user_id = store.registered_users['organize_user']['id']
    liked = member.post('/api/tools/minutes-magic/like')
    assert liked.status_code == 200 and liked.json()['liked'] is True
    made = member.post('/favorites/folders', data={'name':'仕事で検討'}, follow_redirects=False)
    assert made.status_code == 303
    folder = store.favorite_folders_for(user_id)[0]
    moved = member.post('/favorites/minutes-magic/folder', data={'folder_id':folder['id']}, follow_redirects=False)
    assert moved.status_code == 303
    folder_page = member.get(f"/favorites?folder={folder['id']}")
    assert '仕事で検討' in folder_page.text and '議事録マジック' in folder_page.text

    saved = member.post('/notifications/settings', data={'transactions':'on','requests':'on','updates':'on'}, follow_redirects=False)
    assert saved.status_code == 303
    before = len([item for item in store.notifications if item['user_id'] == user_id])
    store.notify(user_id, '非表示メッセージ', '設定確認', '/messages', 'messages')
    assert len([item for item in store.notifications if item['user_id'] == user_id]) == before
    store.notify(user_id, '取引のお知らせ', '表示確認', '/purchases', 'transactions')
    notice = next(item for item in store.notifications if item['user_id'] == user_id and item['title'] == '取引のお知らせ')
    opened = member.post(f"/notifications/{notice['id']}/open", follow_redirects=False)
    assert opened.status_code == 303 and opened.headers['location'] == '/purchases' and notice['read'] is True


def test_seller_dashboard_has_action_center_and_recent_orders():
    seller = TestClient(app)
    seller.get('/auth/demo?next=/seller')
    page = seller.get('/seller')
    assert page.status_code == 200
    assert '対応が必要な取引' in page.text
    assert '最近の取引' in page.text
    assert 'プロフィール' in page.text


def test_listing_faq_reviews_and_version_follow_notification():
    from app.data import store
    detail = TestClient(app).get('/tools/minutes-magic')
    assert detail.status_code == 200
    assert '購入にあたってのお願い' in detail.text
    assert 'よくある質問' in detail.text
    assert 'このツールの口コミ' in detail.text
    assert '設定が簡単で' in detail.text

    follower = TestClient(app)
    follower.post('/signup', data={'display_name':'更新確認','username':'version_follower','email':'version-follower@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False)
    follower_id = store.registered_users['version_follower']['id']
    assert follower.post('/tools/workflow-pocket/follow-updates', follow_redirects=False).status_code == 303
    seller = TestClient(app)
    seller.get('/auth/demo?next=/seller')
    added = seller.post('/seller/tools/workflow-pocket/versions', data={'version':'9.9.9-test','title':'更新通知の動作を確認'}, follow_redirects=False)
    assert added.status_code == 303
    try:
        assert any(item['user_id'] == follower_id and item.get('category') == 'updates' and '9.9.9-test' in item['body'] for item in store.notifications)
    finally:
        tool = store.get('workflow-pocket')
        tool['versions'] = [item for item in tool['versions'] if item['version'] != '9.9.9-test']
        store.update_followers.discard((follower_id, 'workflow-pocket'))

def test_trust_center_nda_and_security_flow():
    member = TestClient(app)
    member.post('/signup', data={'display_name':'信頼テスト','username':'trust_user','email':'trust@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False)
    submitted = member.post('/verification/submit', data={'legal_name':'信頼 太郎','birth_date':'1990-01-01','document_type':'drivers_license','address_confirmed':'yes'}, follow_redirects=False)
    assert submitted.status_code == 303
    assert '審査中' in member.get('/verification').text
    approved = member.post('/verification/demo-review', data={'result':'approve'}, follow_redirects=False)
    assert approved.status_code == 303
    assert '本人確認が完了しています' in member.get('/verification').text
    assert member.post('/nda', data={'agree':'yes'}, follow_redirects=False).status_code == 303
    assert '署名済み' in member.get('/nda').text
    assert member.post('/security/two-factor', follow_redirects=False).status_code == 303
    assert '有効' in member.get('/security').text

def test_seller_product_management_and_analytics():
    seller = TestClient(app)
    seller.get('/auth/demo?next=/seller')
    dashboard = seller.get('/seller')
    assert 'しごとポケットAI' in dashboard.text
    assert 'クリエイターランク' not in dashboard.text
    assert 'CREATOR RANK' in dashboard.text
    edit = seller.get('/seller/tools/workflow-pocket/edit')
    assert edit.status_code == 200
    assert '同時受付上限' in edit.text
    paused = seller.post('/seller/tools/workflow-pocket/status/paused', follow_redirects=False)
    assert paused.status_code == 303
    from app.data import store
    assert store.get('workflow-pocket')['is_published'] is False
    assert '受付休止' in seller.get('/seller').text
    seller.post('/seller/tools/workflow-pocket/status/published', follow_redirects=False)
    analytics = seller.get('/seller/analytics')
    assert analytics.status_code == 200
    assert '購入率' in analytics.text

def test_subscription_library_extra_payment_and_receipt():
    buyer = TestClient(app)
    buyer.post('/signup', data={'display_name':'定期購入者','username':'sub_buyer','email':'sub@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False)
    bought = buyer.post('/checkout/minutes-magic', data={'billing_type':'subscription','purchase_agreement':'yes'}, follow_redirects=False)
    assert bought.status_code == 303
    store = __import__('app.data', fromlist=['store']).store
    order = next(x for x in store.orders if x.get('buyer_username')=='sub_buyer' and x['tool_slug']=='minutes-magic')
    assert order['billing_type'] == 'subscription'
    assert '議事録マジック' in buyer.get('/subscriptions').text
    room = f"/orders/{order['id']}"
    buyer.post(f'{room}/demo-deliver', follow_redirects=False)
    buyer.post(f'{room}/actions/accept', follow_redirects=False)
    assert buyer.post(f'{room}/extra-payment', data={'amount':500,'note':'丁寧なサポート'}, follow_redirects=False).status_code == 303
    assert order['license_key'].startswith('TBX-')
    assert 'ライセンスキー' in buyer.get('/library').text
    receipt = buyer.get(f'{room}/receipt')
    assert receipt.status_code == 200
    assert '丁寧なサポート' in receipt.text

def test_creator_follow_and_quote_decline():
    buyer = TestClient(app)
    buyer.post('/signup', data={'display_name':'相談者','username':'quote_buyer','email':'quote@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False)
    assert buyer.post('/u/mugi/follow', follow_redirects=False).status_code == 303
    assert 'フォロー中' in buyer.get('/u/mugi').text
    started = buyer.post('/messages/start/pixel-recipe', follow_redirects=False)
    conversation_id = started.headers['location'].rsplit('/',1)[-1]
    buyer.post(f'/messages/{conversation_id}/demo-proposal', data={'title':'相談プラン','detail':'調整します','amount':3000,'delivery_days':4}, follow_redirects=False)
    store = __import__('app.data', fromlist=['store']).store
    conversation = next(x for x in store.conversations if x['id']==conversation_id)
    proposal = conversation['proposals'][-1]
    declined = buyer.post(f"/messages/{conversation_id}/proposals/{proposal['id']}/decline", follow_redirects=False)
    assert declined.status_code == 303
    assert proposal['status'] == 'declined'

def test_security_headers_and_readiness_are_exposed():
    response = client.get('/healthz')
    assert response.status_code == 200
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['x-frame-options'] == 'DENY'
    assert "frame-ancestors 'none'" in response.headers['content-security-policy']
    readiness = client.get('/readyz')
    assert readiness.status_code == 200
    assert readiness.json()['total'] >= 15
    assert any(x['key']=='payments' for x in readiness.json()['checks'])

def test_support_case_dispute_and_admin_resolution():
    member = TestClient(app)
    member.post('/signup', data={'display_name':'サポート利用者','username':'support_user','email':'support@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False)
    created = member.post('/support', data={'category':'security','subject':'不審な操作について','detail':'自分では行っていないように見える操作履歴を確認してほしいです。'}, follow_redirects=False)
    assert created.status_code == 303
    assert '不審な操作について' in member.get('/support').text
    admin = TestClient(app)
    admin.get('/auth/demo?next=/admin')
    console = admin.get('/admin')
    assert console.status_code == 200
    assert '本番公開チェック' in console.text
    assert '不審な操作について' in console.text
    store = __import__('app.data', fromlist=['store']).store
    case = next(x for x in store.support_cases if x['subject']=='不審な操作について')
    assert admin.post(f"/admin/cases/{case['id']}/investigating", follow_redirects=False).status_code == 303
    assert case['status'] == 'investigating'

def test_admin_finance_dashboard_and_payout_hold():
    admin = TestClient(app)
    admin.get('/auth/demo?next=/admin')
    console = admin.get('/admin')
    assert console.status_code == 200
    assert '売上・手数料・振込管理' in console.text
    assert 'ONE販売手数料' in console.text
    store = __import__('app.data', fromlist=['store']).store
    finance = store.finance_snapshot()
    assert finance['gross_paid'] >= finance['completed_gmv']
    assert finance['seller_payable'] >= finance['available_to_payout']
    assert finance['processor_fee_total'] >= 0
    seller_id = '00000000-0000-0000-0000-000000000001'
    store.connected_accounts[seller_id] = {'charges_enabled': True, 'payouts_enabled': True}
    ready = store.finance_snapshot()
    ready_seller = next(item for item in ready['sellers'] if item['seller_id'] == seller_id)
    assert ready_seller['payout_eligible'] == ready_seller['available']
    held = admin.post(f'/admin/sellers/{seller_id}/payouts/hold', follow_redirects=False)
    assert held.status_code == 303
    assert store.connected_accounts[seller_id]['payouts_paused'] is True
    paused_seller = next(item for item in store.finance_snapshot()['sellers'] if item['seller_id'] == seller_id)
    assert paused_seller['payout_eligible'] == 0
    assert paused_seller['blocked_for_payout'] == paused_seller['available']
    released = admin.post(f'/admin/sellers/{seller_id}/payouts/release', follow_redirects=False)
    assert released.status_code == 303
    assert store.connected_accounts[seller_id]['payouts_paused'] is False

def test_account_export_and_deletion_guard():
    member = TestClient(app)
    member.get('/auth/demo?next=/security')
    export = member.get('/account/export')
    assert export.status_code == 200
    assert 'attachment;' in export.headers['content-disposition']
    assert export.json()['profile']['username'] == 'demo_creator'
    blocked = member.post('/account/delete-request', data={'confirmation':'退会する'}, follow_redirects=False)
    assert blocked.status_code == 409

def test_stripe_webhook_rejects_unsigned_payload():
    response = client.post('/webhooks/stripe', content='{}', headers={'content-type':'application/json'})
    assert response.status_code == 400

def test_sqlite_state_snapshot_roundtrip(tmp_path):
    from app.data import DemoStore
    from app.persistence import SQLiteStateStore
    first = DemoStore()
    first.likes.add(('user-one','minutes-magic'))
    first.audit('user-one','test.saved','minutes-magic')
    snapshot = SQLiteStateStore(str(tmp_path/'state.sqlite3'))
    snapshot.save(first)
    second = DemoStore()
    assert snapshot.restore(second) is True
    assert ('user-one','minutes-magic') in second.likes
    assert second.audit_logs[0]['action'] == 'test.saved'

def test_signup_requires_versioned_legal_consent():
    member = TestClient(app)
    rejected = member.post('/signup', data={'display_name':'同意なし','username':'no_consent','email':'noconsent@example.com','password':'password123'}, follow_redirects=False)
    assert rejected.status_code == 422

def test_seller_connect_onboarding_demo():
    seller = TestClient(app)
    seller.get('/auth/demo?next=/seller/payments')
    page = seller.get('/seller/payments')
    assert page.status_code == 200
    assert '決済・売上受取設定' in page.text
    connected = seller.post('/seller/payments/onboard', follow_redirects=False)
    assert connected.status_code == 303
    assert '販売可能' in seller.get('/seller/payments').text

def test_signed_webhook_is_idempotent():
    from app.config import settings
    from app.data import store
    previous = settings.stripe_webhook_secret
    object.__setattr__(settings, 'stripe_webhook_secret', 'whsec_test_value')
    event = {'id':'evt_test_idempotent','type':'account.updated','data':{'object':{'id':'acct_missing','charges_enabled':True}}}
    payload = json.dumps(event,separators=(',',':')).encode()
    timestamp = int(time.time())
    digest = hmac.new(b'whsec_test_value',f'{timestamp}.'.encode()+payload,hashlib.sha256).hexdigest()
    headers = {'stripe-signature':f't={timestamp},v1={digest}','content-type':'application/json'}
    try:
        first = client.post('/webhooks/stripe', content=payload, headers=headers)
        second = client.post('/webhooks/stripe', content=payload, headers=headers)
    finally:
        object.__setattr__(settings, 'stripe_webhook_secret', previous)
    assert first.status_code == 200
    assert second.json()['duplicate'] is True
    assert 'evt_test_idempotent' in store.processed_webhook_events

def test_delivery_file_validation_blocks_spoofing_and_zip_traversal():
    from app.files import validate_delivery_file
    try:
        validate_delivery_file('fake.pdf',b'not a pdf')
        assert False, 'spoofed PDF must fail'
    except ValueError:
        pass
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes,'w') as archive:
        archive.writestr('../escape.txt','blocked')
    try:
        validate_delivery_file('unsafe.zip',archive_bytes.getvalue())
        assert False, 'zip traversal must fail'
    except ValueError:
        pass

def test_private_delivery_file_requires_order_participant(tmp_path):
    from app.config import settings
    from app.data import store
    previous = settings.private_storage_path
    object.__setattr__(settings,'private_storage_path',str(tmp_path))
    seller = TestClient(app)
    seller.get('/auth/demo?next=/orders/demo-sale-002')
    try:
        delivered = seller.post('/orders/demo-sale-002/deliver', data={'note':'安全な納品ファイルです。'}, files={'delivery_file':('guide.txt',b'private guide','text/plain')}, follow_redirects=False)
        assert delivered.status_code == 303
        order = next(x for x in store.orders if x['id']=='demo-sale-002')
        file_id = order['delivery']['files'][0]['id']
        download = seller.get(f'/orders/demo-sale-002/files/{file_id}')
        assert download.status_code == 200
        stranger = TestClient(app)
        stranger.post('/signup', data={'display_name':'第三者','username':'file_stranger','email':'stranger@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False)
        assert stranger.get(f'/orders/demo-sale-002/files/{file_id}').status_code == 404
    finally:
        object.__setattr__(settings,'private_storage_path',previous)

def test_email_password_login_roundtrip_uses_password_hash():
    from app.data import store
    member = TestClient(app)
    created = member.post('/signup', data={
        'display_name':'ログイン確認',
        'username':'login_roundtrip',
        'email':'login-roundtrip@example.com',
        'password':'password123',
        'terms_agreement':'yes',
    }, follow_redirects=False)
    assert created.status_code == 303
    account = store.registered_users['login_roundtrip']
    assert account['password_hash'] != 'password123'
    assert member.post('/logout', follow_redirects=False).status_code == 303
    rejected = member.post('/login', data={
        'email':'login-roundtrip@example.com',
        'password':'wrong-password',
        'next':'/mypage',
    }, follow_redirects=False)
    assert rejected.status_code == 401
    accepted = member.post('/login', data={
        'email':'login-roundtrip@example.com',
        'password':'password123',
        'next':'/mypage',
    }, follow_redirects=False)
    assert accepted.status_code == 303
    assert accepted.headers['location'] == '/mypage'
    assert member.get('/mypage').status_code == 200

def test_new_tool_stays_private_until_safety_review():
    from app.data import store
    creator = TestClient(app)
    creator.post('/signup', data={'display_name':'安全出品者','username':'draft_creator','email':'draft@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False)
    created = creator.post('/tools/new', data={
        'name':'公開前チェックツール','tagline':'審査前は外部から購入できません','description_md':'安全確認後に公開するための説明です。',
        'category':'業務効率化','distribution':'webapp','price_type':'paid','price':'1200','prohibited_agreement':'yes',
    }, follow_redirects=False)
    assert created.status_code == 303
    path = created.headers['location'].split('?')[0]
    slug = path.rsplit('/',1)[-1]
    tool = store.get(slug)
    assert tool['status'] == 'draft' and tool['is_published'] is False
    assert '出品者だけに表示されるプレビュー' in creator.get(path).text
    assert TestClient(app).get(path).status_code == 404
    assert creator.post(f'/seller/tools/{slug}/status/published', follow_redirects=False).status_code == 409
    admin = TestClient(app)
    admin.get('/auth/demo?next=/admin')
    assert admin.post(f'/admin/tools/{slug}/safety/passed', follow_redirects=False).status_code == 303
    assert creator.post(f'/seller/tools/{slug}/status/published', follow_redirects=False).status_code == 303
    assert TestClient(app).get(path).status_code == 200
    assert admin.post(f'/admin/tools/{slug}/toggle', follow_redirects=False).status_code == 303
    assert tool['status'] == 'draft' and tool['is_published'] is False
    assert TestClient(app).get(path).status_code == 404
    assert admin.post(f'/admin/tools/{slug}/toggle', follow_redirects=False).status_code == 303
    assert tool['status'] == 'published' and tool['is_published'] is True
    assert TestClient(app).get(path).status_code == 200
    assert admin.post(f'/admin/tools/{slug}/safety/failed', follow_redirects=False).status_code == 303
    assert tool['safety_scan']['status'] == 'failed' and tool['is_published'] is False
    assert TestClient(app).get(path).status_code == 404
    assert creator.get(path).status_code == 200

def test_session_cookie_is_opaque_and_private_pages_are_not_cached():
    member = TestClient(app)
    response = member.post('/signup', data={'display_name':'Cookie利用者','username':'opaque_cookie','email':'opaque@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False)
    cookie = response.headers.get('set-cookie','')
    assert 'opaque@example.com' not in cookie and 'Cookie利用者' not in cookie
    private = member.get('/mypage')
    assert 'no-store' in private.headers['cache-control']
    assert private.headers['vary'] == 'Cookie'
    static = member.get('/static/app.js')
    assert 'immutable' in static.headers['cache-control']
    public_home = TestClient(app).get('/')
    assert public_home.headers['cache-control'].startswith('public, max-age=60')

def test_seo_filtering_recovery_and_accessibility_surfaces():
    filtered = client.get('/tools?q=AI&price_type=paid&ai=Codex&sort=popular')
    assert '<meta name="robots" content="noindex, nofollow">' in filtered.text
    assert 'price_type=paid' in filtered.text and 'ai=Codex' in filtered.text
    recovery = client.get('/auth/recovery')
    assert recovery.status_code == 200
    assert 'data-recovery-token' in recovery.text
    assert 'noindex, nofollow' in recovery.text
    comparison = client.get('/compare?slugs=commit-senpai,pixel-recipe')
    assert 'role="row"' in comparison.text and 'role="columnheader"' in comparison.text and 'role="cell"' in comparison.text
    detail = client.get('/tools/commit-senpai')
    assert 'mobile-purchase-bar' in detail.text and 'aria-pressed=' in detail.text
    assert 'for="tool-report-reason"' in detail.text
    assert 'for="match-budget"' in client.get('/match').text

def test_encoded_open_redirects_are_rejected():
    member = TestClient(app)
    for unsafe in ('%2F%2Fevil.example','/%5C%5Cevil.example','/%0d%0aLocation:%20https://evil.example'):
        response = member.get(f'/auth/demo?next={unsafe}', follow_redirects=False)
        assert response.headers['location'] == '/'

def test_block_action_toggles_and_can_be_undone():
    from app.data import store
    member = TestClient(app)
    member.post('/signup', data={'display_name':'ブロック確認','username':'block_toggle','email':'block-toggle@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False)
    assert member.post('/u/haru/block', follow_redirects=False).status_code == 303
    user_id = store.registered_users['block_toggle']['id']
    assert (user_id,'haru') in store.blocks
    assert 'ブロックを解除' in member.get('/u/haru').text
    assert member.post('/u/haru/block', follow_redirects=False).status_code == 303
    assert (user_id,'haru') not in store.blocks

def test_subscription_webhooks_track_renewal_and_payment_failure():
    from app.config import settings
    from app.data import store
    previous = settings.stripe_webhook_secret
    object.__setattr__(settings, 'stripe_webhook_secret', 'whsec_subscription_test')
    order = {'id':'order-subscription-webhook','tool_slug':'minutes-magic','tool_name':'議事録マジック','buyer_id':'sub-webhook-user','buyer_email':'sub@example.com','seller_id':'seller-mugi','seller_email':None,'amount':680,'status':'in_progress','payment_status':'paid','payment_reference':'pi_subscription','sales_recorded':True,'created_at':__import__('datetime').datetime.now(__import__('datetime').timezone.utc),'updated_at':__import__('datetime').datetime.now(__import__('datetime').timezone.utc)}
    subscription = {'id':'sub-local-webhook','order_id':order['id'],'buyer_id':order['buyer_id'],'tool_name':order['tool_name'],'amount':680,'status':'active','provider_subscription_id':'sub_provider_123','next_billing_at':order['created_at']}
    store.orders.append(order); store.subscriptions.append(subscription)
    def post_event(event):
        payload = json.dumps(event,separators=(',',':')).encode(); timestamp = int(time.time())
        digest = hmac.new(b'whsec_subscription_test',f'{timestamp}.'.encode()+payload,hashlib.sha256).hexdigest()
        return client.post('/webhooks/stripe',content=payload,headers={'stripe-signature':f't={timestamp},v1={digest}','content-type':'application/json'})
    try:
        paid = {'id':'evt_invoice_paid_unique','type':'invoice.paid','data':{'object':{'id':'in_paid','metadata':{'order_id':order['id']},'subscription':'sub_provider_123','currency':'jpy','status':'paid','amount_paid':680}}}
        assert post_event(paid).status_code == 200
        assert subscription['status'] == 'active' and subscription.get('last_paid_at')
        assert subscription['payments'][0]['provider_invoice_id'] == 'in_paid'
        assert post_event(paid).json().get('duplicate') is True
        assert len(subscription['payments']) == 1
        failed = {'id':'evt_invoice_failed_unique','type':'invoice.payment_failed','data':{'object':{'id':'in_failed','metadata':{'order_id':order['id']},'subscription':'sub_provider_123','currency':'jpy'}}}
        assert post_event(failed).status_code == 200
        assert subscription['status'] == 'past_due'
        assert order['payment_status'] == 'past_due'
    finally:
        object.__setattr__(settings, 'stripe_webhook_secret', previous)
        store.orders.remove(order); store.subscriptions.remove(subscription)

def test_mismatched_checkout_webhook_is_rejected_without_claiming_event():
    from app.config import settings
    from app.data import store
    previous = settings.stripe_webhook_secret
    object.__setattr__(settings, 'stripe_webhook_secret', 'whsec_mismatch_test')
    order = {'id':'order-webhook-mismatch','tool_slug':'minutes-magic','tool_name':'議事録マジック','buyer_id':'mismatch-user','seller_id':'seller-mugi','amount':980,'status':'in_progress','payment_status':'pending','checkout_session_id':'cs_expected','sales_recorded':False,'created_at':__import__('datetime').datetime.now(__import__('datetime').timezone.utc),'updated_at':__import__('datetime').datetime.now(__import__('datetime').timezone.utc)}
    store.orders.append(order)
    event = {'id':'evt_checkout_mismatch_unique','type':'checkout.session.completed','data':{'object':{'id':'cs_wrong','metadata':{'order_id':order['id']},'payment_status':'paid','currency':'jpy','amount_total':1}}}
    payload = json.dumps(event,separators=(',',':')).encode(); timestamp = int(time.time())
    digest = hmac.new(b'whsec_mismatch_test',f'{timestamp}.'.encode()+payload,hashlib.sha256).hexdigest()
    try:
        response = client.post('/webhooks/stripe',content=payload,headers={'stripe-signature':f't={timestamp},v1={digest}','content-type':'application/json'})
        assert response.status_code == 400
        assert event['id'] not in store.processed_webhook_events
        assert order['payment_status'] == 'pending'
    finally:
        object.__setattr__(settings, 'stripe_webhook_secret', previous)
        store.orders.remove(order)


def test_csp_nonce_and_inline_policy_are_consistent():
    import re
    response = client.get('/')
    policy = response.headers['content-security-policy']
    assert "'unsafe-inline'" not in policy
    nonce = re.search(r"script-src 'self' 'nonce-([^']+)'", policy).group(1)
    assert f'<script type="application/ld+json" nonce="{nonce}">' in response.text
    assert ' onchange=' not in response.text.lower()
    assert "connect-src 'self'" in policy and "form-action 'self'" in policy
    sanitized = client.get('/', headers={'x-request-id':'<script>alert(1)</script>'})
    assert '<' not in sanitized.headers['x-request-id'] and '>' not in sanitized.headers['x-request-id']


def test_request_requires_agreement_and_short_application_is_rejected():
    member = TestClient(app)
    member.post('/signup', data={'display_name':'募集確認','username':'request_guard','email':'request-guard@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False)
    data = {'title':'合意確認用のAIツール','category':'データ分析','detail':'入力データを整理して結果を表示したいです','budget_min':30000,'budget_max':100000,'deadline':'2026-07-28'}
    assert member.post('/requests/new', data=data, follow_redirects=False).status_code == 422
    assert member.post('/requests/req-001/apply', data={'message':'短い','amount':50000,'delivery_days':10}, follow_redirects=False).status_code == 422
    assert member.post('/checkout/pixel-recipe', follow_redirects=False).status_code == 422


def test_notifications_are_only_marked_read_by_explicit_post():
    from app.data import store
    member = TestClient(app)
    member.post('/signup', data={'display_name':'通知確認','username':'notification_guard','email':'notification-guard@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False)
    user_id = store.registered_users['notification_guard']['id']
    store.notify(user_id, '未読テスト', 'GETだけでは既読にしない', '/tools')
    notice = next(item for item in store.notifications if item['user_id'] == user_id and item['title'] == '未読テスト')
    assert member.get('/notifications').status_code == 200
    assert notice['read'] is False
    assert member.post('/notifications/read-all', follow_redirects=False).status_code == 303
    assert notice['read'] is True


def test_profile_settings_are_publicly_reflected_and_urls_are_validated():
    member = TestClient(app)
    member.post('/signup', data={'display_name':'変更前','username':'profile_guard','email':'profile-guard@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False)
    saved = member.post('/settings', data={'display_name':'公開プロフィール','bio':'初心者にも分かりやすく説明します。','x_url':'https://x.com/profile_guard','website_url':'https://example.com/profile_guard'}, follow_redirects=False)
    assert saved.status_code == 303
    public = member.get('/u/profile_guard')
    assert '公開プロフィール' in public.text and '初心者にも分かりやすく説明します。' in public.text
    assert 'https://example.com/profile_guard' in public.text
    invalid = member.post('/settings', data={'display_name':'公開プロフィール','bio':'','x_url':'https://user:password@example.com','website_url':''}, follow_redirects=False)
    assert invalid.status_code == 422
    assert member.post('/verification', follow_redirects=False).status_code == 303
    member.post('/logout', follow_redirects=False)
    assert member.post('/login', data={'email':'profile-guard@example.com','password':'password123','next':'/settings'}, follow_redirects=False).status_code == 303
    assert '初心者にも分かりやすく説明します。' in member.get('/settings').text
    assert '本人確認が完了しています' in member.get('/verification').text


def test_subscription_cancel_is_period_end_reservation():
    from app.data import store
    from datetime import datetime, timedelta, timezone
    member = TestClient(app)
    member.post('/signup', data={'display_name':'解約確認','username':'cancel_guard','email':'cancel-guard@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False)
    user_id = store.registered_users['cancel_guard']['id']
    item = {'id':'subscription-cancel-guard','order_id':'order-cancel-guard','buyer_id':user_id,'tool_name':'解約確認ツール','amount':1000,'status':'active','next_billing_at':datetime.now(timezone.utc)+timedelta(days=20),'created_at':datetime.now(timezone.utc)}
    store.subscriptions.append(item)
    try:
        assert member.post(f"/subscriptions/{item['id']}/cancel", follow_redirects=False).status_code == 303
        assert item['status'] == 'active'
        assert item['cancel_at_period_end'] is True
        page = member.get('/subscriptions')
        assert '解約予約済み' in page.text and '終了予定' in page.text
        assert member.post(f"/subscriptions/{item['id']}/cancel", follow_redirects=False).status_code == 404
    finally:
        store.subscriptions.remove(item)


def test_private_delivery_path_cannot_escape_storage_root():
    from app.data import store
    from datetime import datetime, timezone
    member = TestClient(app)
    member.post('/signup', data={'display_name':'配信確認','username':'delivery_guard','email':'delivery-guard@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False)
    user_id = store.registered_users['delivery_guard']['id']
    now = datetime.now(timezone.utc)
    order = {'id':'delivery-path-guard','tool_slug':'custom-request','tool_name':'配信確認','buyer_id':user_id,'buyer_name':'配信確認','seller_id':'seller-guard','seller_name':'販売者','amount':1000,'base_amount':1000,'primary_payment_amount':1000,'platform_fee':100,'status':'completed','payment_status':'paid','messages':[],'delivery':{'note':'納品','created_at':now,'version':1,'files':[{'id':'outside','name':'hosts.txt','path':'/etc/hosts','size':1,'scan_status':'passed'}]},'revision_count':0,'cancel_reason':None,'reviewed':False,'extras':[],'created_at':now,'updated_at':now}
    store.orders.append(order)
    try:
        assert member.get('/orders/delivery-path-guard/files/outside').status_code == 404
    finally:
        store.orders.remove(order)


def test_store_concurrency_caps_orders_contracts_and_payouts():
    from concurrent.futures import ThreadPoolExecutor
    from app.data import DemoStore
    from datetime import datetime, timezone
    fresh = DemoStore()
    fresh.get('minutes-magic')['capacity'] = 2
    def buy(index):
        try:
            fresh.buy({'id':f'buyer-{index}','display_name':f'購入者{index}','username':f'buyer_{index}'}, 'minutes-magic')
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=10) as pool:
        assert sum(pool.map(buy, range(10))) == 2

    owner = {'id':'request-owner','display_name':'依頼者','username':'request_owner'}
    request_item = fresh.create_request(owner, {'title':'競合確認','category':'開発者ツール','detail':'同時選択を防ぐ','budget_min':1000,'budget_max':5000,'deadline':'2026-08-01'})
    applications = []
    for index in range(2):
        applicant = {'id':f'applicant-{index}','display_name':f'応募者{index}','username':f'applicant_{index}'}
        fresh.submit_application(request_item['id'], applicant, '同時契約を確認するための応募です', 2000, 5)
        applications.append(request_item['application_list'][-1])
    def contract(application):
        try:
            fresh.contract_request(owner, request_item['id'], application['id'], payment_pending=False)
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(contract, applications)) == 1

    now = datetime.now(timezone.utc)
    fresh.orders.append({'id':'payout-source','tool_slug':'custom','tool_name':'売上','buyer_id':'buyer','buyer_name':'購入者','seller_id':'payout-seller','seller_name':'販売者','amount':1000,'platform_fee':100,'status':'completed','payment_status':'paid','messages':[],'delivery':None,'reviewed':False,'created_at':now,'updated_at':now})
    def payout():
        try:
            fresh.create_payout('payout-seller', 900)
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(lambda _: payout(), range(2))) == 1

    support_user = {'id':'support-user','display_name':'相談者'}
    with ThreadPoolExecutor(max_workers=8) as pool:
        cases = list(pool.map(lambda _: fresh.create_support_case(support_user, 'payment', '重複確認', '同じ取引の相談です', 'same-order'), range(8)))
    assert len({case['id'] for case in cases}) == 1
    assert sum(case['order_id'] == 'same-order' for case in fresh.support_cases) == 1


def test_refund_webhook_uses_primary_payment_amount_after_tip():
    from app.config import settings
    from app.data import store
    from datetime import datetime, timezone
    previous = settings.stripe_webhook_secret
    object.__setattr__(settings, 'stripe_webhook_secret', 'whsec_refund_primary')
    now = datetime.now(timezone.utc)
    order = {'id':'refund-primary-guard','tool_slug':'minutes-magic','tool_name':'議事録マジック','buyer_id':'refund-buyer','buyer_email':None,'seller_id':'seller-mugi','seller_email':None,'amount':1500,'base_amount':1000,'primary_payment_amount':1000,'platform_fee':150,'status':'cancel_pending','payment_status':'paid','payment_reference':'pi_refund_primary','refund_status':'pending','sales_recorded':False,'messages':[],'delivery':None,'reviewed':False,'extras':[{'amount':500,'note':'追加支払い'}],'created_at':now,'updated_at':now}
    store.orders.append(order)
    event = {'id':'evt_refund_primary_unique','type':'charge.refunded','data':{'object':{'id':'ch_refund_primary','payment_intent':'pi_refund_primary','currency':'jpy','amount_refunded':1000,'metadata':{}}}}
    payload = json.dumps(event,separators=(',',':')).encode(); timestamp = int(time.time())
    digest = hmac.new(b'whsec_refund_primary',f'{timestamp}.'.encode()+payload,hashlib.sha256).hexdigest()
    try:
        response = client.post('/webhooks/stripe',content=payload,headers={'stripe-signature':f't={timestamp},v1={digest}','content-type':'application/json'})
        assert response.status_code == 200
        # Refunding the primary charge must not claim the additional payment
        # was refunded too. Its provider reference is missing in this fixture.
        assert order['refund_status'] == 'partial' and order['status'] == 'cancel_pending'
        assert order['refund_confirmed_amount'] == 1000
    finally:
        object.__setattr__(settings, 'stripe_webhook_secret', previous)
        store.orders.remove(order)
        store.processed_webhook_events.discard(event['id'])


def test_account_deletion_grace_period_can_be_cancelled():
    from app.data import store
    member = TestClient(app)
    member.post('/signup', data={'display_name':'退会確認','username':'deletion_guard','email':'deletion-guard@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False)
    requested = member.post('/account/delete-request', data={'confirmation':'退会する'}, follow_redirects=False)
    assert requested.status_code == 303
    user_id = store.registered_users['deletion_guard']['id']
    deletion = next(item for item in store.account_deletions if item['user_id'] == user_id and item['status'] == 'scheduled')
    member.post('/login', data={'email':'deletion-guard@example.com','password':'password123','next':'/security'}, follow_redirects=False)
    assert '退会申請を受け付けています' in member.get('/security').text
    assert member.post('/account/delete-cancel', follow_redirects=False).status_code == 303
    assert deletion['status'] == 'cancelled'


def test_oauth_uses_server_side_pkce_and_retires_implicit_tokens():
    from app.config import settings
    from urllib.parse import parse_qs, urlparse
    previous_url, previous_key = settings.supabase_url, settings.supabase_anon_key
    object.__setattr__(settings, 'supabase_url', 'https://project.supabase.co')
    object.__setattr__(settings, 'supabase_anon_key', 'anon-test-key')
    member = TestClient(app)
    try:
        response = member.get('/auth/oauth/google?next=/settings', follow_redirects=False)
        assert response.status_code == 303
        params = parse_qs(urlparse(response.headers['location']).query)
        assert params['provider'] == ['google']
        assert params['code_challenge_method'] == ['s256']
        assert len(params['code_challenge'][0]) == 43
        callback = parse_qs(urlparse(params['redirect_to'][0]).query)
        assert callback['next'] == ['/settings'] and callback['state'][0]
        assert member.post('/auth/session', json={'access_token':'never-accepted'}).status_code == 410
    finally:
        object.__setattr__(settings, 'supabase_url', previous_url)
        object.__setattr__(settings, 'supabase_anon_key', previous_key)


def test_direct_production_settings_always_disable_demo_mode():
    from app.config import Settings
    assert Settings(environment='production', demo_mode=True).demo_mode is False


def test_account_export_redacts_internal_payment_and_file_metadata():
    from app.data import store
    from datetime import datetime, timezone
    member = TestClient(app)
    member.post('/signup', data={'display_name':'出力確認','username':'export_guard','email':'export-guard@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False)
    user_id = store.registered_users['export_guard']['id']
    now = datetime.now(timezone.utc)
    order = {'id':'export-redaction','tool_slug':'custom','tool_name':'出力確認','buyer_id':user_id,'buyer_name':'出力確認','buyer_email':'export-guard@example.com','seller_id':'seller','seller_name':'販売者','seller_email':'seller-secret@example.com','amount':1000,'platform_fee':100,'status':'completed','payment_status':'paid','payment_reference':'pi_secret','checkout_session_id':'cs_secret','messages':[],'delivery':{'files':[{'id':'file','name':'tool.zip','path':'/private/server/tool.zip'}]},'reviewed':False,'created_at':now,'updated_at':now}
    store.orders.append(order)
    try:
        exported = member.get('/account/export')
        assert exported.status_code == 200
        body = exported.text
        assert 'pi_secret' not in body and 'cs_secret' not in body
        assert '/private/server/tool.zip' not in body and 'seller-secret@example.com' not in body
        assert 'tool.zip' in body
    finally:
        store.orders.remove(order)


def test_due_account_deletion_revokes_session_and_pseudonymizes_records():
    from app.data import store
    from datetime import datetime, timedelta, timezone
    member = TestClient(app)
    member.post('/signup', data={'display_name':'削除対象','username':'purge_guard','email':'purge-guard@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False)
    user_id = store.registered_users['purge_guard']['id']
    now = datetime.now(timezone.utc)
    order = {'id':'purge-record','tool_slug':'custom','tool_name':'保持対象','buyer_id':user_id,'buyer_name':'削除対象','buyer_email':'purge-guard@example.com','seller_id':'seller','seller_name':'販売者','amount':1000,'platform_fee':100,'status':'completed','payment_status':'paid','messages':[],'delivery':None,'reviewed':False,'created_at':now,'updated_at':now}
    deletion = {'id':'purge-request','user_id':user_id,'status':'scheduled','requested_at':now-timedelta(days=8),'delete_after':now-timedelta(days=1)}
    store.orders.append(order); store.account_deletions.append(deletion)
    try:
        assert member.get('/mypage', follow_redirects=False).status_code == 303
        assert 'purge_guard' not in store.registered_users
        assert deletion['status'] == 'completed'
        assert order['buyer_name'] == '退会済みユーザー' and order['buyer_email'] is None
    finally:
        store.orders.remove(order)
        store.account_deletions.remove(deletion)


def test_unknown_public_route_uses_friendly_html_404():
    response = TestClient(app).get('/no-such-public-page')
    assert response.status_code == 404
    assert response.headers['content-type'].startswith('text/html')
    assert 'ページが見つかりません' in response.text or '404' in response.text
    assert 'application/json' not in response.headers['content-type']


def test_seller_pause_action_is_accessible_and_requires_confirmation():
    seller = TestClient(app)
    seller.get('/auth/demo?next=/seller')
    page = seller.get('/seller')
    assert page.status_code == 200
    assert 'data-confirm="受付を休止すると、購入者はこの商品を新しく購入できなくなります。休止しますか？"' in page.text


def test_legacy_embedded_user_session_is_rejected():
    from app.main import session_service

    class LegacyRequest:
        def __init__(self):
            self.session = {'user': {'id': 'forged-user', 'username': 'forged-admin', 'is_admin': True}}

    request = LegacyRequest()
    assert session_service.current(request) is None
    assert request.session == {}


def test_malformed_checkout_webhook_can_be_corrected_and_retried():
    from app.config import settings
    from app.data import store
    from datetime import datetime, timezone

    previous_secret = settings.stripe_webhook_secret
    object.__setattr__(settings, 'stripe_webhook_secret', 'whsec_retry_guard')
    now = datetime.now(timezone.utc)
    order = {
        'id':'order-webhook-retry-guard','tool_slug':'minutes-magic','tool_name':'議事録マジック',
        'buyer_id':'retry-buyer','buyer_email':None,'seller_id':'seller-mugi','seller_email':None,
        'amount':980,'base_amount':980,'primary_payment_amount':980,'platform_fee':98,
        'status':'in_progress','payment_status':'pending','checkout_session_id':'cs_retry_guard',
        'sales_recorded':False,'messages':[],'delivery':None,'reviewed':False,
        'created_at':now,'updated_at':now,
    }
    tool = store.get('minutes-magic')
    previous_sales = tool['sales_count']
    notification_count = len(store.notifications)
    store.orders.append(order)
    event = {
        'id':'evt_checkout_retry_guard','type':'checkout.session.completed',
        'data':{'object':{'id':'cs_retry_guard','metadata':{'order_id':order['id']},
        'payment_status':'paid','currency':'jpy','amount_total':980,'payment_intent':'pi_retry_guard',
        'customer_details':['not-an-object']}},
    }

    def post_event(value):
        payload = json.dumps(value,separators=(',',':')).encode()
        timestamp = int(time.time())
        digest = hmac.new(b'whsec_retry_guard',f'{timestamp}.'.encode()+payload,hashlib.sha256).hexdigest()
        return client.post('/webhooks/stripe',content=payload,headers={
            'stripe-signature':f't={timestamp},v1={digest}','content-type':'application/json',
        })

    try:
        rejected = post_event(event)
        assert rejected.status_code == 400
        assert event['id'] not in store.processed_webhook_events
        assert order['payment_status'] == 'pending'
        event['data']['object']['customer_details'] = {'email':'retry@example.com'}
        accepted = post_event(event)
        assert accepted.status_code == 200
        assert event['id'] in store.processed_webhook_events
        assert order['payment_status'] == 'paid'
    finally:
        object.__setattr__(settings, 'stripe_webhook_secret', previous_secret)
        store.orders.remove(order)
        store.processed_webhook_events.discard(event['id'])
        tool['sales_count'] = previous_sales
        del store.notifications[notification_count:]


def test_admin_suspension_revokes_sessions_and_pauses_public_tools():
    from app.data import store

    member = TestClient(app)
    member.post('/signup', data={
        'display_name':'利用停止確認','username':'suspension_guard','email':'suspension-guard@example.com',
        'password':'password123','terms_agreement':'yes',
    }, follow_redirects=False)
    created = member.post('/tools/new', data={
        'name':'利用停止確認ツール','tagline':'利用停止時には自動的に公開を休止します',
        'description_md':'管理者による利用停止とセッション失効を検証する商品です。',
        'category':'業務効率化','distribution':'webapp','price_type':'paid','price':'1200',
        'prohibited_agreement':'yes',
    }, follow_redirects=False)
    slug = created.headers['location'].split('?')[0].rsplit('/',1)[-1]
    admin = TestClient(app)
    admin.get('/auth/demo?next=/admin')
    try:
        assert admin.post('/admin/users/demo_creator/suspend', follow_redirects=False).status_code == 409
        assert admin.post(f'/admin/tools/{slug}/safety/passed', follow_redirects=False).status_code == 303
        assert member.post(f'/seller/tools/{slug}/status/published', follow_redirects=False).status_code == 303
        suspended = admin.post('/admin/users/suspension_guard/suspend', follow_redirects=False)
        assert suspended.status_code == 303
        assert store.get(slug)['status'] == 'draft' and store.get(slug)['is_published'] is False
        assert TestClient(app).get(f'/tools/{slug}').status_code == 404
        assert member.get('/mypage', follow_redirects=False).status_code == 303
        assert member.post('/login', data={'email':'suspension-guard@example.com','password':'password123','next':'/mypage'}, follow_redirects=False).status_code == 403
    finally:
        admin.post('/admin/users/suspension_guard/restore', follow_redirects=False)
        tool = store.get(slug)
        if tool in store.tools:
            store.tools.remove(tool)


def test_blocking_is_bidirectional_and_removes_social_connections():
    from app.data import DemoStore

    fresh = DemoStore()
    buyer = {'id':'block-buyer-id','username':'block_buyer_unit','display_name':'購入者'}
    fresh.registered_users[buyer['username']] = {'id':buyer['id'],'display_name':buyer['display_name']}
    assert fresh.toggle_like(buyer['id'], 'workflow-pocket', buyer['username'])[0]
    assert fresh.follow_creator(buyer['id'], 'demo_creator', buyer['username'])
    assert fresh.follow_updates(buyer['id'], 'workflow-pocket')

    seller = {'id':'00000000-0000-0000-0000-000000000001','username':'demo_creator','display_name':'販売者'}
    assert fresh.toggle_block(seller, buyer['username']) is True
    assert (buyer['id'], 'workflow-pocket') not in fresh.likes
    assert (buyer['id'], 'demo_creator') not in fresh.creator_follows
    assert (buyer['id'], 'workflow-pocket') not in fresh.update_followers
    for action in (
        lambda: fresh.buy(buyer, 'workflow-pocket'),
        lambda: fresh.conversation_for(buyer, 'workflow-pocket'),
        lambda: fresh.follow_creator(buyer['id'], 'demo_creator', buyer['username']),
        lambda: fresh.toggle_like(buyer['id'], 'workflow-pocket', buyer['username']),
    ):
        try:
            action()
            assert False, 'blocked action unexpectedly succeeded'
        except ValueError as exc:
            assert str(exc) == 'blocked'


def test_instant_products_complete_immediately_and_custom_products_use_room():
    from app.data import DemoStore

    fresh = DemoStore()
    instant = fresh.buy({'id':'instant-buyer','username':'instant_buyer','display_name':'購入者'}, 'commit-senpai')
    assert instant['fulfillment_type'] == 'instant'
    assert instant['status'] == 'completed'
    assert instant['delivery'] and instant['license_key']
    custom = fresh.buy({'id':'custom-buyer','username':'custom_buyer','display_name':'購入者'}, 'minutes-magic')
    assert custom['fulfillment_type'] == 'custom'
    assert custom['status'] == 'in_progress'


def test_reopen_wait_notification_is_one_shot_and_respects_blocks():
    from app.data import DemoStore

    fresh = DemoStore()
    tool = fresh.get('commit-senpai')
    tool.update({'status':'paused','is_published':False})
    user = {'id':'waiter-id','username':'reopen_waiter','display_name':'待機者'}
    fresh.registered_users[user['username']] = {'id':user['id'],'display_name':user['display_name']}
    assert fresh.toggle_reopen_wait(user, tool['slug']) is True
    tool.update({'status':'published','is_published':True})
    assert fresh.notify_reopened(tool['slug']) == 1
    assert (user['id'], tool['slug']) not in fresh.reopen_waiters
    assert any(item['user_id'] == user['id'] and item['title'] == '販売が再開されました' for item in fresh.notifications)
    tool.update({'status':'paused','is_published':False})
    assert fresh.toggle_reopen_wait(user, tool['slug']) is True
    fresh.blocks.add((tool['author_id'], user['username']))
    tool.update({'status':'published','is_published':True})
    assert fresh.notify_reopened(tool['slug']) == 0


def test_seller_coupon_scope_limit_and_single_use():
    from copy import deepcopy
    from datetime import datetime, timedelta, timezone
    from app.data import DemoStore

    fresh = DemoStore()
    seller = {'id':'seller-nullpo','username':'nullpo','display_name':'nullpo'}
    fresh.registered_users[seller['username']] = seller.copy()
    coupon = fresh.create_coupon(seller, 'NULLPO20', 20, 700, 2, datetime.now(timezone.utc)+timedelta(days=7))
    buyer = {'id':'coupon-buyer','username':'coupon_buyer','display_name':'購入者'}
    first = fresh.buy(buyer, 'commit-senpai', coupon='NULLPO20')
    assert first['amount'] == 2000 and coupon['used_count'] == 1
    clone = deepcopy(fresh.get('commit-senpai'))
    clone.update({'slug':'commit-senpai-2','name':'コミット先輩2','sales_count':0})
    fresh.tools.append(clone)
    try:
        fresh.buy(buyer, clone['slug'], coupon='NULLPO20')
        assert False, 'coupon reuse unexpectedly succeeded'
    except ValueError as exc:
        assert str(exc) == 'coupon unavailable'
    try:
        fresh.buy({'id':'other-buyer','username':'other_buyer','display_name':'別購入者'}, 'pixel-recipe', coupon='NULLPO20')
        assert False, 'cross-seller coupon unexpectedly succeeded'
    except ValueError as exc:
        assert str(exc) == 'coupon unavailable'


def test_nda_requires_identity_and_adds_public_trust_badge():
    from app.data import store

    member = TestClient(app)
    username = f'nda_guard_{time.time_ns()}'
    email = f'{username}@example.com'
    registered = member.post('/signup', data={'display_name':'NDA確認','username':username,'email':email,'password':'password123','terms_agreement':'yes'}, follow_redirects=False)
    assert registered.status_code == 303
    assert member.post('/nda', data={'agree':'yes'}, follow_redirects=False).status_code == 403
    assert member.post('/verification', follow_redirects=False).status_code == 303
    assert member.post('/nda', data={'agree':'yes'}, follow_redirects=False).status_code == 303
    user_id = store.registered_users[username]['id']
    assert store.nda_records[user_id]['version'] == '2026-07-16'
    assert '機密保持契約締結済み' in member.get(f'/u/{username}').text


def test_new_trust_and_seller_management_pages_render():
    from datetime import datetime, timedelta, timezone

    member = TestClient(app)
    username = f'manage_{str(time.time_ns())[-10:]}'
    assert member.post('/signup', data={'display_name':'管理画面確認','username':username,'email':f'{username}@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False).status_code == 303
    assert member.get('/settings/blocks').status_code == 200
    assert member.get('/seller/coupons').status_code == 200
    expires_on = (datetime.now(timezone.utc).date()+timedelta(days=7)).isoformat()
    created = member.post('/seller/coupons', data={'code':f'CP{time.time_ns()}'[-20:],'percent':15,'max_discount':1500,'max_uses':20,'expires_on':expires_on}, follow_redirects=False)
    assert created.status_code == 303
    assert '発行済みクーポン' in member.get('/seller/coupons').text
    detail = member.get('/tools/commit-senpai')
    assert 'すぐに使える' in detail.text and '今すぐ購入して使う' in detail.text


def test_coupon_preview_matches_server_side_checkout_calculation():
    from datetime import datetime, timedelta, timezone
    from app.data import store

    buyer = TestClient(app)
    suffix = str(time.time_ns())[-10:]
    username = f'coupon_{suffix}'
    assert buyer.post('/signup', data={'display_name':'クーポン購入者','username':username,'email':f'{username}@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False).status_code == 303
    code = f'NP{suffix}'
    coupon = store.create_coupon({'id':'seller-nullpo','username':'nullpo'}, code, 25, 600, 10, datetime.now(timezone.utc)+timedelta(days=7))
    try:
        preview = buyer.post('/api/coupons/preview', data={'slug':'commit-senpai','code':code,'billing_type':'one_time'})
        assert preview.status_code == 200
        assert preview.json() == {'code':code,'subtotal':2500,'discount':600,'total':1900}
    finally:
        store.coupons.remove(coupon)


def test_hybrid_sale_conversations_are_separated_and_transfer_is_deduplicated():
    from app.data import DemoStore

    fresh = DemoStore()
    buyer = {'id':'hybrid-buyer','username':'hybrid_buyer','display_name':'複合購入者'}
    general = fresh.conversation_for(buyer, 'workflow-pocket', 'general')
    custom = fresh.conversation_for(buyer, 'workflow-pocket', 'customization')
    assert general['id'] != custom['id']
    assert general['conversation_type'] == 'general'
    assert custom['conversation_type'] == 'customization'

    inquiry, created = fresh.create_transfer_inquiry(buyer, 'workflow-pocket', 450000, '自社の業務改善サービスとして運営します。', '譲渡対象と引き継ぎ内容について相談したいです。')
    duplicate, duplicate_created = fresh.create_transfer_inquiry(buyer, 'workflow-pocket', 500000, '別の用途として検討しています。', '同じ案件へ重複して送信されないことを確認します。')
    assert created is True and duplicate_created is False
    assert duplicate['id'] == inquiry['id']
    assert inquiry['status'] == 'nda_pending'
    assert fresh.conversation_for(buyer, 'workflow-pocket', 'transfer')['id'] == inquiry['conversation_id']
    try:
        fresh.update_transfer_status(inquiry, {'id':inquiry['seller_id']}, 'negotiating')
        assert False, 'NDA未確認で条件交渉へ進めてはいけない'
    except ValueError as exc:
        assert str(exc) == 'nda required'
    seller = {'id':inquiry['seller_id'],'username':inquiry['seller_username'],'display_name':inquiry['seller_name']}
    fresh.sign_transfer_nda(inquiry, buyer, 'a'*64, 'b'*64)
    assert inquiry['status'] == 'nda_pending'
    fresh.sign_transfer_nda(inquiry, seller, 'a'*64, 'c'*64)
    assert inquiry['status'] == 'reviewing'
    fresh.update_transfer_status(inquiry, seller, 'negotiating')
    assert inquiry['status'] == 'negotiating'


def test_exclusive_transfer_end_to_end_permissions_and_nda_gate():
    from app.data import store

    market = client.get('/transfers')
    assert market.status_code == 200
    assert '完成したAI事業を' in market.text
    offer = client.get('/tools/workflow-pocket/transfer')
    assert offer.status_code == 200
    assert 'ソースコード・独占権' in offer.text

    buyer = TestClient(app)
    suffix = str(time.time_ns())[-10:]
    username = f'transfer_{suffix}'
    assert buyer.post('/signup', data={'display_name':'譲渡購入者','username':username,'email':f'{username}@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False).status_code == 303
    created = buyer.post('/tools/workflow-pocket/transfer', data={'offer_amount':470000,'intended_use':'自社の小規模事業者向けサービスとして継続運営します。','message':'収益証憑と外部APIの移管条件、引き継ぎ範囲について確認したいです。'}, follow_redirects=False)
    assert created.status_code == 303
    assert created.headers['location'].startswith('/transfer-inquiries/')
    inquiry_id = created.headers['location'].split('/')[2].split('?')[0]
    inquiry = next(item for item in store.transfer_inquiries if item['id'] == inquiry_id)
    conversation = next(item for item in store.conversations if item['id'] == inquiry['conversation_id'])
    buyer_id = store.registered_users[username]['id']
    seller_id = inquiry['seller_id']
    prior_acceptances = list(store.transfer_nda_acceptances)
    prior_buyer_identity = store.identity_applications.get(buyer_id)
    prior_seller_identity = store.identity_applications.get(seller_id)
    try:
        page = buyer.get(f'/transfer-inquiries/{inquiry_id}')
        assert page.status_code == 200
        assert '案件NDAの確認には本人確認が必要です' in page.text
        assert buyer.post('/account/delete-request', data={'confirmation':'退会する'}, follow_redirects=False).status_code == 409
        assert buyer.post(f'/transfer-inquiries/{inquiry_id}/nda', data={'agreement':'yes'}, follow_redirects=False).status_code == 403
        stranger = TestClient(app)
        other = f'other_{suffix}'
        stranger.post('/signup', data={'display_name':'第三者','username':other,'email':f'{other}@example.com','password':'password123','terms_agreement':'yes'})
        assert stranger.get(f'/transfer-inquiries/{inquiry_id}').status_code == 404

        seller = TestClient(app)
        seller.get('/auth/demo?next=/seller/transfers')
        assert inquiry['tool_name'] in seller.get('/seller/transfers').text
        assert seller.post(f'/transfer-inquiries/{inquiry_id}/status/negotiating', follow_redirects=False).status_code == 409

        assert buyer.post('/verification', follow_redirects=False).status_code == 303
        assert buyer.post(f'/transfer-inquiries/{inquiry_id}/nda', data={'agreement':'yes'}, follow_redirects=False).status_code == 303
        assert inquiry['status'] == 'nda_pending'
        assert seller.post('/verification', follow_redirects=False).status_code == 303
        assert seller.post(f'/transfer-inquiries/{inquiry_id}/nda', data={'agreement':'yes'}, follow_redirects=False).status_code == 303
        assert inquiry['status'] == 'reviewing'
        assert seller.post(f'/transfer-inquiries/{inquiry_id}/status/negotiating', follow_redirects=False).status_code == 303
        assert inquiry['status'] == 'negotiating'
        exported = buyer.get('/account/export').json()
        assert any(item['id'] == inquiry_id for item in exported['transfer_inquiries'])
        assert any(item['inquiry_id'] == inquiry_id for item in exported['transfer_nda_acceptances'])
        assert all('ip_hash' not in item for item in exported['transfer_nda_acceptances'])
        assert buyer.post(f'/transfer-inquiries/{inquiry_id}/withdraw', follow_redirects=False).status_code == 303
        assert inquiry['status'] == 'closed' and inquiry['closed_by'] == 'buyer'
        assert buyer.post(f'/transfer-inquiries/{inquiry_id}/withdraw', follow_redirects=False).status_code == 409
    finally:
        store.transfer_nda_acceptances[:] = prior_acceptances
        if prior_buyer_identity is None: store.identity_applications.pop(buyer_id, None)
        else: store.identity_applications[buyer_id] = prior_buyer_identity
        if prior_seller_identity is None: store.identity_applications.pop(seller_id, None)
        else: store.identity_applications[seller_id] = prior_seller_identity
        if inquiry in store.transfer_inquiries: store.transfer_inquiries.remove(inquiry)
        if conversation in store.conversations: store.conversations.remove(conversation)
        store.notifications = [item for item in store.notifications if item.get('url') != f'/transfer-inquiries/{inquiry_id}']


def test_exclusive_transfer_has_independent_review_gate():
    from app.data import store

    creator = TestClient(app)
    suffix = str(time.time_ns())[-10:]
    username = f'transfer_seller_{suffix}'
    creator.post('/signup', data={'display_name':'譲渡審査出品者','username':username,'email':f'{username}@example.com','password':'password123','terms_agreement':'yes'}, follow_redirects=False)
    assert creator.post('/verification', follow_redirects=False).status_code == 303
    created = creator.post('/tools/new', data={
        'name':f'譲渡審査ツール{suffix}', 'tagline':'通常販売と独占譲渡を別々に審査します',
        'description_md':'通常販売は公開したまま、独占譲渡条件のみを厳格に再審査する商品です。',
        'category':'開発者ツール', 'distribution':'download', 'price_type':'paid', 'price':'3200',
        'fulfillment_type':'instant', 'estimated_delivery_days':'1', 'customization_available':'yes',
        'exclusive_available':'yes', 'exclusive_price_min':'500000',
        'transfer_assets':['source_code','deployment_docs'], 'tech_stack':'Python / FastAPI',
        'monthly_revenue':'80000', 'monthly_profit':'60000', 'monthly_cost':'20000',
        'weekly_ops_hours':'2', 'handover_days':'21',
        'exclusive_summary':'ソースコードと導入手順を譲渡し、顧客情報および第三者アカウントは対象外とします。',
        'transfer_rights_agreement':'yes', 'prohibited_agreement':'yes',
    }, follow_redirects=False)
    assert created.status_code == 303
    slug = created.headers['location'].split('?')[0].rsplit('/', 1)[-1]
    tool = store.get(slug)
    admin = TestClient(app)
    admin.get('/auth/demo?next=/admin')
    try:
        assert tool['transfer_review_status'] == 'pending'
        assert admin.post(f'/admin/tools/{slug}/safety/passed', follow_redirects=False).status_code == 303
        assert creator.post(f'/seller/tools/{slug}/status/published', follow_redirects=False).status_code == 303
        assert TestClient(app).get(f'/tools/{slug}').status_code == 200
        assert TestClient(app).get(f'/tools/{slug}/transfer').status_code == 404
        assert slug not in TestClient(app).get('/transfers').text

        assert admin.post(f'/admin/tools/{slug}/transfer-review/approved', follow_redirects=False).status_code == 303
        assert tool['transfer_review_status'] == 'approved'
        assert TestClient(app).get(f'/tools/{slug}/transfer').status_code == 200

        base_edit = {
            'name':tool['name'], 'tagline':'通常商品の説明だけを更新しました', 'description_md':tool['description_md'],
            'price':'3200', 'capacity':'5', 'support_days':'7', 'estimated_delivery_days':'1',
            'fulfillment_type':'instant', 'customization_available':'yes', 'exclusive_available':'yes',
            'exclusive_price_min':'500000', 'transfer_assets':['source_code','deployment_docs'],
            'tech_stack':'Python / FastAPI', 'monthly_revenue':'80000', 'monthly_profit':'60000',
            'monthly_cost':'20000', 'weekly_ops_hours':'2', 'handover_days':'21',
            'exclusive_summary':'ソースコードと導入手順を譲渡し、顧客情報および第三者アカウントは対象外とします。',
            'transfer_rights_agreement':'yes',
        }
        assert creator.post(f'/seller/tools/{slug}/edit', data=base_edit, follow_redirects=False).status_code == 303
        assert tool['transfer_review_status'] == 'approved'

        changed_transfer = {**base_edit, 'exclusive_price_min':'540000'}
        assert creator.post(f'/seller/tools/{slug}/edit', data=changed_transfer, follow_redirects=False).status_code == 303
        assert tool['transfer_review_status'] == 'pending'
        assert TestClient(app).get(f'/tools/{slug}').status_code == 200
        assert TestClient(app).get(f'/tools/{slug}/transfer').status_code == 404
        assert '審査待ち' in creator.get(f'/seller/tools/{slug}/edit').text
    finally:
        if tool in store.tools: store.tools.remove(tool)
        store.identity_applications.pop(store.registered_users[username]['id'], None)
