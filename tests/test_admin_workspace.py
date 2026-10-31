from copy import deepcopy
from dataclasses import replace

import pytest
from html.parser import HTMLParser
from fastapi.testclient import TestClient

from app import main
from app.data import DemoStore
from app.listing_review import REVIEW_CHECKS, review_token


class Tags(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.source, self.elements = source, []
        self.feed(source)
    def handle_starttag(self, tag, attrs): self.elements.append((tag, dict(attrs)))
    def tags(self, tag): return [attrs for name, attrs in self.elements if name == tag]
    def has_class(self, value): return any(value in attrs.get('class','').split() for _,attrs in self.elements)


@pytest.fixture
def workspace(monkeypatch):
    store = DemoStore()
    actor = {"user": {"id": "admin-check", "username": "demo_creator", "display_name": "運営"}}
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "current_user", lambda request: actor["user"])
    monkeypatch.setattr(main, "database_store", None)
    return TestClient(main.app), store, actor


def test_admin_panels_keep_operations_in_admin_and_load_only_selected_data(workspace, monkeypatch):
    client, store, _ = workspace
    def expensive(*args, **kwargs): raise AssertionError("unrelated finance query")
    monkeypatch.setattr(store, "finance_snapshot", expensive)
    for section in main.ADMIN_SECTIONS:
        if section in {"finance", "readiness"}: continue
        response = client.get('/admin?section='+section)
        assert response.status_code == 200
        page = Tags(response.text)
        assert any(a.get('aria-current') == 'page' and a.get('href') == '/admin?section='+section for a in page.tags('a'))
        assert not page.has_class('site-header')
        assert not page.has_class('site-footer')
        for link in page.tags('a'):
            assert link['href'].startswith(('/admin', '#')) or link.get('target') == '_blank'
        assert not page.has_class('finance-console') or section == 'operation-health'
    assert client.get('/admin?section=../../base').status_code == 404


def test_admin_sections_remain_admin_only(workspace):
    client, _, actor = workspace
    actor['user'] = {'id':'ordinary', 'username':'ordinary', 'display_name':'一般会員'}
    for section in main.ADMIN_SECTIONS:
        assert client.get('/admin?section='+section).status_code == 403


def test_review_missing_instant_file_explains_before_action_and_cannot_publish(workspace, monkeypatch):
    client, store, _ = workspace
    monkeypatch.setattr(main, 'settings', replace(main.settings, demo_mode=False, admin_user_ids=('admin-check',)))
    tool = store.tools[0]
    tool.update(fulfillment_type='instant', status='draft', is_published=False, safety_scan={'status':'pending'})
    page = Tags(client.get(f'/admin/tools/{tool["slug"]}/review').text)
    assert page.has_class('admin-sidebar')
    assert any(a.get('class') == 'button' and 'disabled' in a for a in page.tags('button'))
    assert '取引ルームで納品' in page.source
    payload = {'token':review_token(tool), 'checks':list(REVIEW_CHECKS), 'note':'掲載内容と販売条件を確認しました。'}
    before = deepcopy(tool)
    assert client.post(f'/admin/tools/{tool["slug"]}/safety/passed',data=payload).status_code == 409
    assert tool == before
    tool['safety_scan']={'status':'passed'}
    assert client.post(f'/admin/tools/{tool["slug"]}/toggle').status_code == 409
    assert not tool['is_published']
    tool['fulfillment_type']='custom'
    payload['token']=review_token(tool)
    assert client.post(f'/admin/tools/{tool["slug"]}/safety/passed',data=payload,follow_redirects=False).status_code == 303


def listing_data(**changes):
    return {'name':'納品条件の検証', 'tagline':'合成データのテスト', 'description_md':'テスト用商品です。',
            'category':'業務効率化', 'distribution':'download', 'price_type':'paid', 'price':1000,
            'prohibited_agreement':'yes', 'fulfillment_type':'instant', **changes}


def test_registration_requires_instant_file_before_creating_listing(workspace, monkeypatch):
    client, store, _ = workspace
    data=listing_data(distribution=next(iter(main.DIST_LABELS)))
    before=len(store.tools)
    response=client.post('/tools/new', data=data, follow_redirects=False)
    assert response.status_code == 422
    assert '納品ファイルと導入手順' in response.text
    assert len(store.tools)==before
    async def scanner(*args):
        from fastapi import HTTPException
        raise HTTPException(503, '検査接続待ち')
    monkeypatch.setattr(main,'store_artifact',scanner)
    response=client.post('/tools/new',data={**data,'artifact_guide':'検証用の導入手順です。'},files={'artifact_file':('test.txt',b'synthetic','text/plain')})
    assert response.status_code==503
    assert len(store.tools)==before
    monkeypatch.setattr(main,'generate_og',lambda *args:None)
    response=client.post('/tools/new',data={**data,'fulfillment_type':'custom'},follow_redirects=False)
    assert response.status_code==303 and '/tools/' in response.headers['location']
    assert len(store.tools)==before+1


def test_registration_attaches_scanned_file_and_preserves_private_state(workspace, monkeypatch, tmp_path):
    client,store,_=workspace
    monkeypatch.setattr(main,'settings',replace(main.settings,demo_mode=True,storage_provider='local',clamav_host='',private_storage_path=str(tmp_path)))
    monkeypatch.setattr(main,'generate_og',lambda *args:None)
    data=listing_data(distribution=next(iter(main.DIST_LABELS)),artifact_guide='検証用の導入手順です。')
    response=client.post('/tools/new',data=data,files={'artifact_file':('test.txt',b'synthetic','text/plain')},follow_redirects=False)
    assert response.status_code==303
    slug=response.headers['location'].split('?')[0].rsplit('/',1)[-1]
    tool=store.get(slug)
    assert main.artifact_ready(tool['instant_artifact'])
    assert tool['safety_scan']['status']=='pending'
    assert not tool['is_published']
