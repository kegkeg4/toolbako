"""Public seller guidance, with synthetic state and no provider requests."""
import asyncio
from html.parser import HTMLParser
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import app.main as main
from app.config import Settings
from app.public_read import anonymous_read


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.meta, self.links, self.anchors, self.headings, self.text = {}, {}, [], [], []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta':
            self.meta[attrs.get('name') or attrs.get('property')] = attrs.get('content')
        elif tag == 'link':
            self.links[attrs.get('rel')] = attrs.get('href')
        elif tag == 'a':
            self.anchors.append(attrs.get('href', ''))
        elif tag == 'h1':
            self.headings.append(tag)

    def handle_data(self, data):
        self.text.append(data)


@pytest.mark.parametrize('environment', ['production', 'staging', 'development'])
def test_seller_guide_is_public_but_only_production_is_indexable(monkeypatch, environment):
    monkeypatch.setattr(main, 'settings', Settings(environment=environment,
        site_base_url='https://market.example', platform_fee_rate=0.125, custom_fee_rate=0.15))
    response = TestClient(main.app).get('/sell?from=footer')
    assert response.status_code == 200
    page = Page(response.text)
    assert page.headings == ['h1']
    assert page.links['canonical'] == 'https://market.example/sell'
    assert page.meta['robots'] == ('index, follow, max-image-preview:large'
        if environment == 'production' else 'noindex, nofollow')
    assert 'AIツール' in page.meta['og:title'] and '販売' in page.meta['og:title']
    assert page.meta['og:title'] == page.meta['twitter:title']
    assert page.meta['description'] == page.meta['og:description'] == page.meta['twitter:description']
    visible_text = ''.join(page.text)
    assert '12.5%' in visible_text and '15.0%' in visible_text
    assert {'/tools/new', '/support#guide-sell', '/tokushoho'} <= set(page.anchors)


def test_seller_guide_discovery_does_not_publish_private_routes(monkeypatch):
    monkeypatch.setattr(main, 'settings', Settings(environment='production', site_base_url='https://market.example'))
    xml = ElementTree.fromstring(asyncio.run(main.sitemap()).body)
    locations = {item.text for item in xml.findall('{*}url/{*}loc')}
    assert {'https://market.example/sell', 'https://market.example/support'} <= locations
    assert not any('/seller' in url or '/tools/new' in url for url in locations)
    for path in ('/tools/new', '/seller', '/seller/payments'):
        request = Request({'type': 'http', 'path': path, 'headers': [], 'query_string': b''})
        assert main.seo_metadata(request)['robots_value'] == 'noindex, nofollow'
    client = TestClient(main.app)
    for path in ('/', '/support'):
        assert '/sell' in Page(client.get(path).text).anchors


@pytest.mark.parametrize('method,headers,allowed', [
    ('GET', [], True), ('POST', [], False),
    ('GET', [(b'cookie', b'')], False),
    ('GET', [(b'authorization', b'Bearer invalid')], False),
])
def test_seller_guide_uses_existing_anonymous_read_boundary(method, headers, allowed):
    request = Request({'type': 'http', 'method': method, 'path': '/sell', 'headers': headers})
    assert anonymous_read(request) is allowed
