"""Render major screens with isolated synthetic accounts, not real sessions."""
import pytest
from fastapi.testclient import TestClient
import app.main as main
from app.data import DemoStore


@pytest.mark.parametrize('path', [
    '/', '/tools', '/tools?q=test', '/transfers', '/creators', '/match', '/compare',
    '/requests', '/support', '/about', '/terms', '/privacy', '/tokushoho',
    '/mypage', '/purchases', '/favorites', '/notifications', '/notifications/settings',
    '/tools/new', '/requests/new', '/library', '/subscriptions', '/seller',
    '/seller/payments', '/seller/analytics', '/seller/coupons', '/seller/transfers',
    '/messages', '/verification', '/nda', '/security', '/settings',
    '/settings/blocks', '/payouts', '/admin', '/tools/commit-senpai',
    '/seller/tools/commit-senpai/edit', '/admin/tools/commit-senpai/review',
])
def test_authenticated_page_renders(path, monkeypatch):
    monkeypatch.setattr(main, 'store', DemoStore())
    main.store.get('commit-senpai')['author_id'] = main.store.registered_users['demo_creator']['id']
    with TestClient(main.app) as client:
        client.get('/auth/demo?next=/')
        response = client.get(path)
        assert response.status_code == 200, (path, response.status_code)
        assert '<main' in response.text
