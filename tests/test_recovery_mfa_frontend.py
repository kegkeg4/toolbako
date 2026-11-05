"""Synthetic template and JS tests for recovery-only TOTP; no Auth connection."""
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess

from jinja2 import Environment, FileSystemLoader, select_autoescape
import pytest


ROOT = Path(__file__).resolve().parents[1]
TOKEN = 'synthetic-recovery-access-token-only'
CSRF = 'synthetic-csrf-only'
FACTOR = {'id': 'a1111111-1111-4111-8111-111111111111', 'friendly_name': 'Synthetic app'}


class Elements(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.items = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.items.append((tag, dict(attrs)))

    def named(self, tag, name):
        return [attrs for kind, attrs in self.items if kind == tag and attrs.get('name') == name]


def render(**overrides):
    values = {'recovery_access_token': TOKEN, 'recovery_mfa_csrf': CSRF,
              'recovery_factors': [FACTOR], 'error': None, 'user': None,
              'legal_operator': {}, 'seo_jsonld': [], 'asset_version': 'test',
              'robots_value': 'noindex, nofollow', 'canonical_url': 'https://synthetic.invalid/auth/recovery/mfa',
              'og_image_url': '/static/brand/toolbako-icon.png', **overrides}
    env = Environment(loader=FileSystemLoader(ROOT / 'templates'), autoescape=select_autoescape(['html']))
    text = env.get_template('password_recovery_mfa.html').render(values)
    return text, Elements(text)


def test_single_verified_factor_uses_only_recovery_mfa_form():
    text, dom = render(code='123456', password='never-echo-password', password_confirmation='never-echo-password')
    forms = [attrs for tag, attrs in dom.items if tag == 'form']
    assert len(forms) == 1 and forms[0]['action'] == '/auth/recovery/mfa'
    assert forms[0]['method'] == 'post' and 'data-recovery-mfa-form' in forms[0]
    assert 'data-recovery-form' not in forms[0]
    assert dom.named('input', 'access_token')[0]['value'] == TOKEN
    assert dom.named('input', 'recovery_mfa_csrf')[0]['value'] == CSRF
    assert dom.named('input', 'factor_id')[0]['value'] == FACTOR['id']
    code = dom.named('input', 'code')[0]
    assert code['type'] == 'text' and code['inputmode'] == 'numeric'
    assert code['autocomplete'] == 'one-time-code' and code['pattern'] == '[0-9]{6}'
    assert code['minlength'] == code['maxlength'] == '6'
    assert 'value' not in code and 'data-password-input' not in code
    assert not dom.named('input', 'password') and not dom.named('input', 'password_confirmation')
    assert '123456' not in text and 'never-echo-password' not in text
    assert '<h1>再設定の本人確認</h1>' in text
    assert '確認してパスワード設定へ</button>' in text
    assert '確認後、新しいパスワードを入力します' in text
    assert 'data-recovery-error' not in text
    for tag, attrs in dom.items:
        for key in ('href', 'src', 'action'):
            assert TOKEN not in attrs.get(key, '') and CSRF not in attrs.get(key, '')


def test_multiple_verified_factors_render_an_escaped_selection():
    second = {'id': 'b2222222-2222-4222-8222-222222222222', 'friendly_name': '<script>synthetic</script>'}
    text, dom = render(recovery_factors=[FACTOR, second])
    assert len(dom.named('select', 'factor_id')) == 1
    assert not dom.named('input', 'factor_id')
    options = [attrs['value'] for tag, attrs in dom.items if tag == 'option']
    assert options == [FACTOR['id'], second['id']]
    assert '<script>synthetic</script>' not in text
    assert '&lt;script&gt;synthetic&lt;/script&gt;' in text


@pytest.mark.parametrize('missing', ['recovery_factors', 'recovery_access_token', 'recovery_mfa_csrf'])
def test_missing_recovery_proof_has_no_form_and_one_actionable_error(missing):
    text, dom = render(**{missing: [] if missing == 'recovery_factors' else ''})
    assert not [attrs for tag, attrs in dom.items if tag == 'form']
    assert not dom.named('input', 'access_token')
    assert len([attrs for _, attrs in dom.items if attrs.get('role') == 'alert']) == 1
    assert 'href="/support#guide-mfa"' in text and 'href="/login"' in text


@pytest.mark.parametrize('factors', [[], [FACTOR]])
def test_server_mfa_error_is_one_alert_and_code_is_never_echoed(factors):
    text, dom = render(recovery_factors=factors, error='Synthetic retry message', code='123456')
    assert len([attrs for _, attrs in dom.items if attrs.get('role') == 'alert']) == 1
    assert text.count('Synthetic retry message') == 1 and '123456' not in text
    assert bool(dom.named('input', 'code')) is bool(factors)


def test_recovery_mfa_browser_state():
    node = shutil.which('node')
    if not node:
        bundled = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
        if bundled.is_file():
            node = str(bundled)
    if not node:
        pytest.skip('Existing Node.js is required for the offline DOM tests')
    result = subprocess.run([node, '--test', str(Path(__file__).with_name('recovery_mfa_interactions.cjs'))],
                            capture_output=True, text=True, encoding='utf8', errors='replace', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
