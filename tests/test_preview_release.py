import asyncio
from xml.etree import ElementTree

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

import app.main as main
from app.config import Settings
from app.middleware import ProductionGuardMiddleware


@pytest.mark.parametrize('path', ['/', '/tools', '/tools/commit-senpai', '/signup', '/login'])
def test_preview_pages_are_clearly_labeled_and_not_indexable(path):
    response = TestClient(main.app).get(path)
    assert response.status_code == 200
    assert '公開準備中 · デモ版' in response.text
    assert '実際のお支払い・送金は行われません' in response.text
    assert '商品・販売件数・評価にはサンプルを含みます' in response.text
    assert '<meta name="robots" content="noindex, nofollow">' in response.text
    assert response.headers['x-robots-tag'] == 'noindex, nofollow'


def test_preview_has_no_advertised_sitemap_and_allows_noindex_to_be_read():
    client = TestClient(main.app)
    robots = client.get('/robots.txt')
    assert robots.text == 'User-agent: *\nAllow: /\n'
    response = client.get('/sitemap.xml')
    assert response.status_code == 200
    assert list(ElementTree.fromstring(response.text)) == []
    assert response.headers['x-robots-tag'] == 'noindex, nofollow'


@pytest.mark.parametrize('environment', ['development', 'staging', 'production'])
def test_preview_header_depends_on_environment_not_demo_flag(environment):
    app = FastAPI()
    app.add_middleware(ProductionGuardMiddleware, settings=Settings(environment=environment, demo_mode=False, redis_url=''))

    @app.get('/example')
    def example():
        return {'ok': True}

    response = TestClient(app).get('/example')
    assert response.status_code == 200
    expected = None if environment == 'production' else 'noindex, nofollow'
    assert response.headers.get('x-robots-tag') == expected


def test_production_keeps_public_seo_and_filters_nonindexable(monkeypatch):
    monkeypatch.setattr(main, 'settings', Settings(environment='production'))

    def metadata(path, query=b''):
        return main.seo_metadata(Request({'type': 'http', 'path': path, 'headers': [], 'query_string': query}))

    assert metadata('/')['robots_value'] == 'index, follow, max-image-preview:large'
    assert metadata('/')['preview_mode'] is False
    assert metadata('/tools', b'q=AI')['robots_value'] == 'noindex, follow'
    assert metadata('/admin')['robots_value'] == 'noindex, nofollow'
    sitemap = asyncio.run(main.sitemap())
    assert b'<loc>' in sitemap.body
    assert b'Sitemap:' in asyncio.run(main.robots()).body


def test_staging_uses_test_notice_without_claiming_every_operation_is_simulated(monkeypatch):
    monkeypatch.setattr(main, 'settings', Settings(environment='staging', demo_mode=False))
    response = TestClient(main.app).get('/')
    assert '公開準備中 · 接続テスト環境' in response.text
    assert '実際のお支払い・送金は行われません' not in response.text
    assert '本物のカード情報を入力しないでください' in response.text
