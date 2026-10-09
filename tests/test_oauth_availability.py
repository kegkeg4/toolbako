from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
import app.main as main


@pytest.mark.parametrize('providers, expected', [
    ((), ()), (('google',), ('google',)), (('twitter',), ('twitter',)),
    (('twitter', 'google', 'google', 'github'), ('google', 'twitter')),
    (('github', 'unknown'), ()), ('google', ()), (None, ()),
])
def test_social_providers_use_an_explicit_supported_allowlist(providers, expected):
    config = Settings(supabase_url='https://project.supabase.co',
                      supabase_anon_key='anon-test-key', oauth_providers=providers)
    assert config.enabled_oauth_providers == expected


def test_social_providers_require_the_auth_api_configuration():
    config = Settings(supabase_url='', supabase_anon_key='', oauth_providers=('google',))
    assert config.enabled_oauth_providers == ()


@pytest.mark.parametrize('page', ['/login', '/signup'])
@pytest.mark.parametrize('providers', [(), ('google',), ('twitter',), ('google', 'twitter')])
def test_auth_pages_only_advertise_enabled_providers(monkeypatch, page, providers):
    monkeypatch.setattr(main, 'settings', Settings(
        environment='staging', demo_mode=False, supabase_url='https://project.supabase.co',
        supabase_anon_key='anon-test-key', oauth_providers=providers,
    ))
    response = TestClient(main.app).get(page + '?next=%2Fsettings%3Ftab%3Dprofile%26source%3Dtest')
    assert response.status_code == 200
    for provider in ('google', 'twitter'):
        assert (f'/auth/oauth/{provider}?' in response.text) == (provider in providers)
    assert ('class="auth-divider"' in response.text) == bool(providers)
    assert 'type="email"' in response.text
    assert 'デモでログイン' not in response.text
    if providers:
        assert 'next=/settings%3Ftab%3Dprofile%26source%3Dtest' in response.text


@pytest.mark.parametrize('provider', ['google', 'twitter'])
def test_disabled_provider_is_rejected_before_redirect_or_oauth_session(monkeypatch, provider):
    monkeypatch.setattr(main, 'settings', Settings(
        environment='staging', demo_mode=False, supabase_url='https://project.supabase.co',
        supabase_anon_key='anon-test-key', oauth_providers=(),
    ))
    response = TestClient(main.app).get(f'/auth/oauth/{provider}', follow_redirects=False)
    assert response.status_code == 503
    assert 'メールアドレスで登録・ログインしてください' in response.text
    assert 'location' not in response.headers and 'set-cookie' not in response.headers


def test_enabled_provider_preserves_pkce_and_safe_return_destination(monkeypatch):
    monkeypatch.setattr(main, 'settings', Settings(
        environment='staging', demo_mode=False, supabase_url='https://project.supabase.co',
        supabase_anon_key='anon-test-key', oauth_providers=('google',),
    ))
    response = TestClient(main.app).get('/auth/oauth/google?next=https://example.invalid', follow_redirects=False)
    assert response.status_code == 303
    params = parse_qs(urlparse(response.headers['location']).query)
    assert params['provider'] == ['google'] and params['code_challenge_method'] == ['s256']
    assert len(params['code_challenge'][0]) == 43
    callback = parse_qs(urlparse(params['redirect_to'][0]).query)
    assert callback['next'] == ['/'] and callback['state'][0]


def test_demo_access_is_only_advertised_in_demo_mode(monkeypatch):
    monkeypatch.setattr(main, 'settings', Settings(environment='staging', demo_mode=False,
                                                supabase_url='', supabase_anon_key=''))
    assert 'デモでログイン' not in TestClient(main.app).get('/login').text
    monkeypatch.setattr(main, 'settings', Settings(environment='development', demo_mode=True,
                                                database_url='', supabase_url='', supabase_anon_key=''))
    assert 'デモでログイン' in TestClient(main.app).get('/login').text
