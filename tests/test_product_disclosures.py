from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app import main
from app.data import DemoStore
from app.listing_review import content_fingerprint, review_token
from app.persistence import SQLiteStateStore
from app.product_disclosures import RIGHTS_STATEMENT, RIGHTS_VERSION, parse_disclosures, display_disclosures


@pytest.fixture
def shop(monkeypatch):
    store = DemoStore()
    tool = store.tools[0]
    actor = {"user": {"id": tool["author_id"], "username": tool["author_username"], "display_name": tool["author_name"]}}
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "current_user", lambda request: actor["user"])
    monkeypatch.setattr(main, "generate_og", lambda *args: "/static/og/test.png")
    return TestClient(main.app), store, tool, actor


def edit_data(tool, **extra):
    return dict(name=tool["name"], tagline=tool["tagline"], description_md=tool["description_md"],
                price=tool["price"], capacity=tool["capacity"], support_days=tool["support_days"],
                estimated_delivery_days=tool.get("estimated_delivery_days", 1),
                fulfillment_type=tool.get("fulfillment_type", "custom"), purchase_notes=tool.get("purchase_notes", ""),
                faq_question=[x["question"] for x in tool.get("faq", [])],
                faq_answer=[x["answer"] for x in tool.get("faq", [])], **extra)


def new_data(**extra):
    return dict(name="条件入力テスト", tagline="実売商品ではありません", description_md="合成データで動作検証",
                category="業務効率化", distribution="download", price_type="paid", price=11000,
                prohibited_agreement="yes", **extra)


def test_new_listing_records_versioned_rights_and_no_invented_facts(shop):
    client, store, _, actor = shop
    assert client.post('/tools/new', data=new_data(), follow_redirects=False).status_code == 303
    tool = store.tools[0]
    assert tool['purchase_disclosures'] == {}
    assert not tool['is_published']
    consent = store.legal_consents[-1]
    assert (consent['user_id'], consent['tool_id'], consent['version'], consent['statement']) == (
        actor['user']['id'], tool['id'], RIGHTS_VERSION, RIGHTS_STATEMENT)
    assert tool['rights_confirmation']['confirmed_at'] == consent['created_at']
    page = client.get('/tools/' + tool['slug'])
    assert '未記載・購入前に出品者へ確認' in page.text
    assert '70</b>' not in page.text and '★ None' not in page.text
    assert f'href="/seller/tools/{tool["slug"]}/edit"' in page.text


def test_missing_rights_rejected_before_creation(shop):
    client, store, _, _ = shop
    before = len(store.tools), len(store.legal_consents)
    payload = new_data(); payload.pop('prohibited_agreement')
    assert client.post('/tools/new', data=payload).status_code == 422
    assert (len(store.tools), len(store.legal_consents)) == before


@pytest.mark.parametrize('field,value', [('resale','yes'), ('commercial_use','free'), ('additional_costs','x'*1001)])
def test_invalid_disclosure_does_not_mutate_listing(shop, field, value):
    client, store, tool, _ = shop
    before = deepcopy(tool)
    response = client.post('/seller/tools/' + tool['slug'] + '/edit', data=edit_data(
        tool, listing_rights_agreement='yes', **{'disclosure_' + field:value}))
    assert response.status_code == 422
    assert tool == before
    assert not [x for x in store.legal_consents if x.get('document_type') == 'listing_rights']


def test_changed_conditions_require_confirmation_and_reopen_review(shop):
    client, store, tool, _ = shop
    tool.update(is_published=True, status='published', safety_scan={'status':'passed'})
    path = '/seller/tools/' + tool['slug'] + '/edit'
    old = deepcopy(tool); old_token = review_token(tool)
    payload = edit_data(tool, disclosure_additional_costs='外部APIの従量料金は購入者負担', disclosure_resale='not_allowed')
    assert client.post(path, data=payload).status_code == 422
    assert tool == old
    payload['listing_rights_agreement'] = 'yes'
    assert client.post(path, data=payload, follow_redirects=False).status_code == 303
    assert tool['purchase_disclosures']['resale'] == 'not_allowed'
    assert tool['safety_scan']['status'] == 'pending' and not tool['is_published']
    assert review_token(tool) != old_token
    assert store.legal_consents[-1]['tool_slug'] == tool['slug']


def test_legacy_edit_preserves_conditions_without_fabricating_consent(shop):
    client, store, tool, _ = shop
    tool['purchase_disclosures'] = {'license':'1名・社内利用', 'resale':'not_allowed'}
    tool['safety_scan'] = {'status':'passed'}
    assert client.post('/seller/tools/' + tool['slug'] + '/edit', data=edit_data(tool), follow_redirects=False).status_code == 303
    assert tool['purchase_disclosures']['license'] == '1名・社内利用'
    assert tool['safety_scan']['status'] == 'passed'
    assert not [x for x in store.legal_consents if x.get('document_type') == 'listing_rights']


def test_missing_and_empty_disclosures_have_same_review_hash(shop):
    _, _, tool, _ = shop
    previous = content_fingerprint(tool)
    tool['purchase_disclosures'] = {}
    assert content_fingerprint(tool) == previous
    assert parse_disclosures({'disclosure_license':'', 'disclosure_resale':'unknown'}) == {}


def test_legacy_conditions_are_preserved_but_new_conditions_take_precedence():
    legacy = {'passport': {'license': '旧条件', 'requirements':'Windows', 'commercial_use':True}}
    assert display_disclosures(legacy)['commercial_use'] == 'allowed'
    legacy['purchase_disclosures'] = {'license':'新条件', 'commercial_use':'not_allowed'}
    assert display_disclosures(legacy)['license'] == '新条件'
    assert display_disclosures(legacy)['commercial_use'] == 'not_allowed'
    assert display_disclosures({'passport':{'license':'購入前に確認','commercial_use':False,'requirements':'未設定'}}) == {}


def test_disclosures_escaped_and_same_on_checkout_and_review(shop):
    client, _, tool, actor = shop
    tool['purchase_disclosures'] = {'license':'<script>alert(1)</script>', 'additional_costs':'API別途月額1000円', 'commercial_use':'conditional', 'resale':'not_allowed'}
    tool.update(price_type='paid', price=11000, is_published=True, status='published')
    actor['user'] = {'id':'demo-creator', 'username':'demo_creator', 'display_name':'審査者'}
    for path in ['/tools/'+tool['slug'], '/checkout/'+tool['slug'], '/admin/tools/'+tool['slug']+'/review']:
        response = client.get(path)
        assert response.status_code == 200
        assert '&lt;script&gt;alert(1)&lt;/script&gt;' in response.text
        assert '<script>alert(1)</script>' not in response.text
        assert 'API別途月額1000円' in response.text


def test_legacy_form_roundtrip_and_explicit_clearing(shop):
    client, _, tool, _ = shop
    tool['passport'].update(license='旧ライセンス', commercial_use=True)
    tool['safety_scan'] = {'status':'passed'}
    path = '/seller/tools/' + tool['slug'] + '/edit'
    payload = edit_data(tool, disclosure_license='旧ライセンス', disclosure_commercial_use='allowed')
    assert client.post(path, data=payload, follow_redirects=False).status_code == 303
    assert tool['purchase_disclosures'] == {}
    assert tool['safety_scan']['status'] == 'passed'
    payload.update(disclosure_license='', disclosure_commercial_use='unknown', listing_rights_agreement='yes')
    assert client.post(path, data=payload, follow_redirects=False).status_code == 303
    displayed = display_disclosures(tool)
    assert displayed['license'] == '' and displayed['commercial_use'] == 'unknown'
    assert tool['safety_scan']['status'] == 'pending'


def test_unauthorized_seller_cannot_edit_conditions(shop):
    client, _, tool, actor = shop
    before = deepcopy(tool)
    actor['user'] = {'id':'someone-else', 'username':'other', 'display_name':'別の会員'}
    assert client.post('/seller/tools/'+tool['slug']+'/edit', data=edit_data(tool, disclosure_license='自由', listing_rights_agreement='yes')).status_code == 403
    assert tool == before


def test_purchase_conditions_are_detached_and_survive_save_restore(shop, tmp_path):
    client, store, tool, actor = shop
    tool.update(price_type='paid', price=11000, is_published=True, status='published')
    tool['purchase_disclosures'] = {'license':'1名・社内利用', 'resale':'not_allowed'}
    buyer = {'id':'conditions-buyer', 'username':'conditions-buyer', 'display_name':'購入者'}
    order = store.buy(buyer, tool['slug'], payment_pending=True)
    tool['purchase_disclosures']['license'] = '変更後'
    assert store.buy(buyer, tool['slug'], payment_pending=True)['product_conditions']['disclosures']['license'] == '1名・社内利用'
    actor['user'] = buyer
    page = client.get('/orders/' + order['id'])
    assert page.status_code == 200
    assert '注文時の商品説明・利用条件' in page.text and '1名・社内利用' in page.text
    assert '変更後' not in page.text
    db = SQLiteStateStore(tmp_path/'state.sqlite3')
    db.save(store)
    restored = DemoStore(); assert db.restore(restored)
    saved = restored.get_order(order['id'], buyer['id'])
    assert saved['product_conditions']['disclosures']['license'] == '1名・社内利用'
    assert saved['product_conditions']['captured_at'].tzinfo is not None
    (tmp_path/'state.sqlite3').unlink()


def test_copyright_report_uses_existing_queue_without_automatic_suspension(shop):
    client, store, tool, actor = shop
    original = deepcopy(tool)
    response = client.post('/support', data={'category':'copyright','subject':'著作権の確認依頼','detail':'対象商品と権利者の資料を記載した合成データの検証です。'}, follow_redirects=False)
    assert response.status_code == 303
    assert store.support_cases[-1]['category'] == 'copyright' or store.support_cases[0]['category'] == 'copyright'
    assert tool == original
    actor['user'] = None
    assert '著作権・第三者の権利侵害を報告する' in client.get('/support').text
