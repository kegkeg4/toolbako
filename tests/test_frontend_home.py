"""Template regressions with synthetic data only; no application/provider IO."""
from pathlib import Path
from types import SimpleNamespace
from html.parser import HTMLParser
from urllib.parse import urlsplit, parse_qs

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape


class Links(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.feed(html)
    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.append(dict(attrs))


def home(rating=None):
    root = Path(__file__).resolve().parents[1]
    env = Environment(loader=FileSystemLoader(root / 'templates'), autoescape=select_autoescape(['html']))
    # Only the hero is under test. Keep the unchanged card include out of this
    # tiny fixture instead of fabricating the full product/member state.
    template = env.from_string((root / 'templates/home.html').read_text(encoding='utf-8').replace(
        '{% include "_market_card.html" %}', ''))
    return template.render(categories=['業務効率化', '文章', '画像', '開発', '販売', '学習', '娯楽', 'データ', 'その他'],
        category_picks={}, trending=[dict(slug='synthetic', name='未評価商品', tagline='説明', accent='violet',
            rating=rating, sales_count=0, price_type='paid', price=1000)], newest=[], user=None,
        seo_jsonld=[], price_labels={}, legal_operator=SimpleNamespace(website='', name='検証運営'),
        platform_fee_percent=10, preview_mode=False, asset_version='test', unread_notification_count=0)


def test_unrated_hero_does_not_render_python_none():
    html = home()
    assert '★ None' not in html
    assert '★ 新着' in html


@pytest.mark.parametrize('rating', [4.8, 5.0])
def test_hero_preserves_real_rating(rating):
    assert f'★ {rating}' in home(rating)


def test_other_category_link_filters_other_instead_of_all_items():
    links = Links(home()).links
    other = [link for link in links if parse_qs(urlsplit(link.get('href', '')).query).get('category') == ['その他']]
    assert other and all(urlsplit(link['href']).path == '/tools' for link in other)
