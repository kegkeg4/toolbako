"""Product CSS delivery regression; actual responsive layout is checked in CUA."""
import hashlib
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from jinja2 import Environment, FileSystemLoader


def test_product_page_loads_current_mobile_styles_instead_of_immutable_cached_css():
    root = Path(__file__).resolve().parents[1]
    env = Environment(loader=FileSystemLoader(root / 'templates'))
    env.filters['markdown'] = lambda value: value
    template = env.get_template('tool_detail.html')
    context = template.new_context({'asset_version': 'fixture-release'})
    html = ''.join(template.blocks['page_styles'](context))

    class Styles(HTMLParser):
        links = []

        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if tag == 'link' and values.get('rel') == 'stylesheet':
                self.links.append(values['href'])

    parser = Styles()
    parser.feed(html)
    matches = [urlsplit(link) for link in parser.links if urlsplit(link).path == '/static/ai-market.css']
    assert len(matches) == 1
    # Git may check out CRLF on Windows and LF on Linux; the asset version must
    # follow the same canonical bytes on both hosts.
    css = (root / 'static/ai-market.css').read_bytes().replace(b'\r\n', b'\n')
    digest = hashlib.sha256(css).hexdigest()[:12]
    assert parse_qs(matches[0].query)['v'] == [f'fixture-release-detail-{digest}']
