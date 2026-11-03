"""Caching must not pin missing assets or lose content negotiation headers."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import Response
from app.config import Settings
from app.middleware import ProductionGuardMiddleware


def cache_client(status=200):
    app = FastAPI()
    @app.api_route('/static/example.js', methods=['GET', 'HEAD'])
    @app.get('/page')
    def page():
        return Response('x' * 1024, status_code=status, media_type='text/plain', headers={'Vary': 'Accept-Language'})
    app.add_middleware(GZipMiddleware, minimum_size=500)
    app.add_middleware(ProductionGuardMiddleware, settings=Settings(environment='development', demo_mode=True, redis_url=''))
    return TestClient(app)


@pytest.mark.parametrize('status', [200, 304])
def test_only_versioned_successful_assets_are_immutable(status):
    client = cache_client(status)
    assert 'immutable' in client.get('/static/example.js?v=release-1').headers['cache-control']
    unversioned = client.get('/static/example.js').headers['cache-control']
    assert unversioned == 'public, max-age=0, must-revalidate'
    assert client.get('/static/example.js?v=').headers['cache-control'] == unversioned


@pytest.mark.parametrize('status', [404, 405, 500, 503])
def test_asset_errors_never_stay_cached_for_a_year(status):
    response = cache_client(status).get('/static/example.js?v=release-1')
    assert 'no-store' in response.headers['cache-control']
    assert 'immutable' not in response.headers['cache-control']


def test_private_gzip_response_preserves_all_vary_dimensions():
    client = cache_client()
    client.cookies.set('toolbako_session', 'synthetic-test-cookie')
    response = client.get('/page', headers={'Accept-Encoding': 'gzip'})
    assert response.headers['content-encoding'] == 'gzip'
    assert set(x.strip().lower() for x in response.headers['vary'].split(',')) == {'accept-language', 'accept-encoding', 'cookie'}
    assert 'no-store' in response.headers['cache-control']
