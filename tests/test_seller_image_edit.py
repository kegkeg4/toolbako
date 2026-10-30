import io
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import main


class Inputs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.fields = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        field = dict(attrs)
        if tag == 'input' and field.get('name'):
            self.fields[field['name']] = field


@pytest.mark.parametrize('price, expected', [(0, ''), (300000, '300000')])
def test_transfer_price_initial_value_does_not_block_edit(monkeypatch, price, expected):
    seller = TestClient(main.app)
    seller.get('/auth/demo?next=/seller')
    tool = main.store.get('workflow-pocket')
    monkeypatch.setitem(tool, 'exclusive_price_min', price)
    page = seller.get('/seller/tools/workflow-pocket/edit')
    assert page.status_code == 200
    field = Inputs(page.text).fields['exclusive_price_min']
    assert field['value'] == expected
    assert 'required' not in field
    assert not field['value'] or int(field['value']) >= int(field['min'])


def test_image_save_with_unset_transfer_price_and_repeat_save(monkeypatch, tmp_path):
    # Exercise real multipart parsing and image encoding using only local demo data.
    monkeypatch.setattr(main, 'ROOT', tmp_path)
    seller = TestClient(main.app)
    seller.get('/auth/demo?next=/seller')
    original = main.store.get('workflow-pocket')
    tool = dict(original, slug='image-edit-fixture', exclusive_available=False,
                exclusive_price_min=0, thumbnail_url=None)
    real_get = main.store.get
    monkeypatch.setattr(main.store, 'get', lambda slug: tool if slug == tool['slug'] else real_get(slug))
    data = dict(name=tool['name'], tagline=tool['tagline'], description_md=tool['description_md'],
                price=tool['price'], capacity=5, support_days=7, estimated_delivery_days=1,
                exclusive_price_min='')
    png = io.BytesIO()
    Image.new('RGB', (8, 8), '#227799').save(png, 'PNG')
    path = '/seller/tools/image-edit-fixture/edit'
    result = seller.post(path, data=data, files={'thumbnail': ('fixture.png', png.getvalue(), 'image/png')}, follow_redirects=False)
    assert result.status_code == 303
    assert result.headers['location'] == path + '?saved=1'
    image_url = tool['thumbnail_url']
    with Image.open(tmp_path / image_url.lstrip('/')) as saved:
        assert saved.format == 'WEBP'
        assert saved.size == (8, 8)
    assert tool['exclusive_available'] is False
    assert tool['exclusive_price_min'] == 0
    page = seller.get(result.headers['location'])
    assert '変更を保存しました' in page.text
    assert Inputs(page.text).fields['exclusive_price_min']['value'] == ''
    assert seller.post(path, data=data, follow_redirects=False).status_code == 303
    assert tool['thumbnail_url'] == image_url
