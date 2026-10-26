"""Owner isolation, current auth and unchanged UI against disposable PostgreSQL."""
import asyncio
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from starlette.middleware.sessions import SessionMiddleware

from app.data import DemoStore
from app.database import DatabaseBoundaryMiddleware, PostgresStateStore
from app.member_read import MEMBER_READ_PATHS, member_read
from app.security import SessionService
from test_database import database, query


def sample_store():
    store=DemoStore(seed=False)
    now=datetime.now(timezone.utc)
    tool=deepcopy(DemoStore().tools[0]); tool['status']='published'; tool['is_published']=True
    store.tools=[tool]
    for n in ('a','b'):
        user={'id':n,'username':n,'display_name':'Member '+n,'email':n+'@example.invalid'}
        store.registered_users[n]={**user,'password_hash':'DO-NOT-LOAD','password_salt':'DO-NOT-LOAD'}
        store.account_sessions['sid-'+n]={'user':user,'expires_at':now+timedelta(hours=1),'provider_tokens':{'access_token':'DO-NOT-LOAD'}}
        store.notifications.append({'id':'notice-'+n,'user_id':n,'title':'ONLY-'+n,'body':'Notice body','read':False,'created_at':now})
        store.favorite_folders[n]=[{'id':'folder-'+n,'name':'Folder '+n}]
        store.favorite_assignments[n+'␟'+tool['slug']]='folder-'+n
        store.likes.add((n,tool['slug']))
        store.subscriptions.append({'id':'sub-'+n,'buyer_id':n,'tool_name':'ONLY-'+n,'status':'active','amount':100,'next_billing_at':now+timedelta(days=30)})
    return store


def request_for(sid='sid-a',path='/notifications',method='GET',headers=()):
    return Request({'type':'http','method':method,'path':path,'query_string':b'','headers':list(headers),'session':{'sid':sid}})


def cookie(secret, sid='sid-a', **other):
    return TimestampSigner(secret).sign(base64.b64encode(json.dumps({'sid':sid,**other}).encode())).decode()


def test_member_route_gate_is_exact_and_requires_verified_session_scope():
    for path in MEMBER_READ_PATHS:assert member_read(request_for(path=path))
    for path in ['/mypage','/purchases','/seller','/payouts','/messages','/orders/x','/security','/admin','/login','/settings/blocks']:
        assert not member_read(request_for(path=path))
    for req in [request_for(method='POST'),request_for(sid=None),request_for(sid='x'*129),request_for(headers=[(b'authorization',b'Bearer x')])]:
        assert not member_read(req)
    raw=Request({'type':'http','method':'GET','path':'/notifications','headers':[(b'cookie',b'toolbako_session=sid-a')]})
    assert not member_read(raw)
    legacy=request_for();legacy.session['user']={'id':'a'};assert not member_read(legacy)


def test_member_view_has_only_owner_records_and_never_waits_for_writer(database):
    writer,dsn=database;store=sample_store()
    async def run():
        reader=PostgresStateStore(dsn,reuse_connections=True)
        try:
            async with writer.request(store):
                fresh=sample_store()
                for key in ['tools','registered_users','account_sessions','notifications','likes','favorite_folders','favorite_assignments','subscriptions']:
                    setattr(store,key,getattr(fresh,key))
                await writer.save()
                revision=query(dsn,'select revision from toolbako_runtime.marketplace_state')[0][0]
                store.notifications[0]['title']='UNCOMMITTED'
                view=await asyncio.wait_for(reader.member_view('sid-a','/favorites'),2)
                assert view.notifications[0]['title']=='ONLY-a'
                assert set(view.account_sessions)=={'sid-a'} and set(view.registered_users)=={'a'}
                assert 'provider_tokens' not in view.account_sessions['sid-a']
                assert 'password_hash' not in view.registered_users['a']
                assert not hasattr(view,'orders') and not hasattr(view,'payouts')
                assert view.likes=={('a',store.tools[0]['slug'])}
                assert set(view.favorite_folders)=={'a'}
                assert list(view.favorite_assignments)==['a␟'+store.tools[0]['slug']]
                assert view.subscriptions==[]
                subs=await reader.member_view('sid-b','/subscriptions')
                assert [x['id'] for x in subs.subscriptions]==['sub-b'] and subs.tools==[]
                assert query(dsn,'select revision from toolbako_runtime.marketplace_state')[0][0]==revision
                view.notifications[0]['title']='LOCAL-MUTATION'
                assert (await reader.member_view('sid-a','/notifications')).notifications[0]['title']=='ONLY-a'
                await writer.save()
                assert (await reader.member_view('sid-a','/notifications')).notifications[0]['title']=='UNCOMMITTED'
            views=await asyncio.gather(*(reader.member_view('sid-'+('a' if n%2==0 else 'b'),'/notifications') for n in range(12)))
            assert [v.notifications[0]['user_id'] for v in views]==['a','b']*6
            assert len(reader._public_connections)<=2
        finally:await reader.close()
    asyncio.run(run())


def test_library_owner_filter_and_subscription_revocation_are_fresh(database, monkeypatch):
    from app import main
    writer, dsn = database
    store = sample_store()
    now = datetime.now(timezone.utc)
    store.orders = [dict(id='order-'+n, buyer_id=n, seller_id='seller', tool_name='Library '+n,
                         status='completed', billing_type='subscription', created_at=now,
                         license_key='LICENSE-'+n) for n in ('a','b')]
    store.orders.append(dict(store.orders[0],id='unpaid',status='pending_payment',license_key='UNPAID'))
    for sub in store.subscriptions:
        sub['order_id'] = 'order-'+sub['buyer_id']
    async def run():
        from psycopg.types.json import Jsonb
        from app.database import snapshot
        reader = PostgresStateStore(dsn,reuse_connections=True)
        try:
            # Artificial state only, directly seeded into the disposable DB.
            async with await writer.connect() as conn:
                await conn.execute('insert into toolbako_runtime.marketplace_state(singleton,payload) values(true,%s)', (Jsonb(snapshot(store)),))
            async with writer.request(DemoStore(seed=False)):
                view = await reader.member_view('sid-a','/library')
                assert [x['id'] for x in view.orders] == ['order-a']
                assert [x['id'] for x in view.subscriptions] == ['sub-a']
                monkeypatch.setattr(main,'store',store)
                req=request_for(path='/library');req.state.member_view=view
                req.state.member_sessions=SessionService(view,3600)
                response=await main.purchase_library(req)
                assert b'LICENSE-a' in response.body and b'LICENSE-b' not in response.body and b'UNPAID' not in response.body
            store.subscriptions[0]['status']='cancelled'
            async with await writer.connect() as conn:
                await conn.execute('update toolbako_runtime.marketplace_state set payload=%s,revision=revision+1 where singleton',(Jsonb(snapshot(store)),))
            view=await reader.member_view('sid-a','/library')
            req=request_for(path='/library');req.state.member_view=view
            req.state.member_sessions=SessionService(view,3600)
            response=await main.purchase_library(req)
            assert b'LICENSE-a' not in response.body
        finally:
            await reader.close()
    asyncio.run(run())


@pytest.mark.parametrize('mutation',['revoke','expired','banned','erasing','completed','wrong_id','missing_account','identity'])
def test_every_read_observes_authoritative_auth_changes(database,mutation):
    writer,dsn=database;store=DemoStore(seed=False)
    async def run():
        reader=PostgresStateStore(dsn,reuse_connections=True)
        try:
            async with writer.request(store):
                fresh=sample_store();store.registered_users=fresh.registered_users;store.account_sessions=fresh.account_sessions
                await writer.save()
                view=await reader.member_view('sid-a','/notifications')
                assert SessionService(view,3600).current(request_for())['id']=='a'
                if mutation=='revoke':store.account_sessions.pop('sid-a')
                elif mutation=='expired':store.account_sessions['sid-a']['expires_at']=datetime.now(timezone.utc)-timedelta(seconds=1)
                elif mutation=='banned':store.registered_users['a']['is_banned']=True
                elif mutation in {'erasing','completed'}:store.account_deletions.append({'user_id':'a','status':mutation})
                elif mutation=='wrong_id':store.registered_users['a']['id']='b'
                elif mutation=='missing_account':store.registered_users.pop('a')
                elif mutation=='identity':store.identity_applications['a']={'status':'verified'}
                await writer.save()
                view=await reader.member_view('sid-a','/notifications')
                user=SessionService(view,3600).current(request_for())
                if mutation=='identity':assert user['is_verified'] is True
                else:assert user is None
        finally:await reader.close()
    asyncio.run(run())


def test_all_member_templates_render_without_global_store_and_match_original(database,monkeypatch):
    from app import main
    writer,dsn=database;store=sample_store()
    async def seed():
        async with writer.request(DemoStore(seed=False)):
            for key in ['tools','registered_users','account_sessions','notifications','likes','favorite_folders','favorite_assignments','subscriptions']:
                setattr(writer._store,key,getattr(store,key))
            await writer.save()
    asyncio.run(seed())
    monkeypatch.setattr(main,'store',store)
    monkeypatch.setattr(main.session_service,'store',store)
    c=TestClient(main.app);c.cookies.set('toolbako_session',cookie(main.settings.session_secret))
    originals={path:c.get(path).text for path in MEMBER_READ_PATHS}
    backend=PostgresStateStore(dsn,reuse_connections=False)
    def forbidden(*args,**kwargs):raise AssertionError('shared session or badge store used')
    monkeypatch.setattr(main.session_service,'current',forbidden)
    monkeypatch.setitem(main.templates.env.globals,'creator_badges_for',forbidden)
    class ForbiddenStore:
        def __getattr__(self,name):raise AssertionError('shared store used: '+name)
    monkeypatch.setattr(main,'store',ForbiddenStore())
    wrapper=DatabaseBoundaryMiddleware(main.app,backend=backend,store=store,member_reads=True,session_max_age=3600)
    wrapped=SessionMiddleware(wrapper,secret_key=main.settings.session_secret,session_cookie='toolbako_session')
    c=TestClient(wrapped);c.cookies.set('toolbako_session',cookie(main.settings.session_secret))
    import re
    normalize=lambda s:re.sub(r'nonce="[^"]*"','nonce=""',s)
    for path in sorted(MEMBER_READ_PATHS):
        response=c.get(path)
        assert response.status_code==200,path
        assert normalize(response.text)==normalize(originals[path]),path
        assert 'no-store' in response.headers['cache-control']
        assert 'ONLY-b' not in response.text


def test_signed_cookie_boundary_and_write_fallback(database):
    writer,dsn=database;store=DemoStore(seed=False)
    async def seed():
        async with writer.request(store):
            fresh=sample_store();store.registered_users=fresh.registered_users;store.account_sessions=fresh.account_sessions
            await writer.save()
    asyncio.run(seed())
    calls=[]
    class Tracked(PostgresStateStore):
        @asynccontextmanager
        async def request(self,store):
            calls.append('locked')
            async with super().request(store) as timing:yield timing
    backend=Tracked(dsn)
    app=FastAPI()
    @app.api_route('/notifications',methods=['GET','POST'])
    async def page(request:Request):
        service=getattr(request.state,'member_sessions',SessionService(store,3600))
        user=service.current(request)
        return {'user':user and user['id'],'fast':hasattr(request.state,'member_view')}
    app.add_middleware(DatabaseBoundaryMiddleware,backend=backend,store=store,member_reads=True)
    app.add_middleware(SessionMiddleware,secret_key='test-secret',session_cookie='toolbako_session')
    c=TestClient(app);c.cookies.set('toolbako_session',cookie('test-secret'))
    assert c.get('/notifications?user_id=b').json()=={'user':'a','fast':True}
    assert c.post('/notifications').json()=={'user':'a','fast':False}
    c.cookies.set('toolbako_session',cookie('wrong-secret'))
    assert c.get('/notifications').json()=={'user':None,'fast':False}
    # A banned record must be revoked durably by the unchanged locked path.
    async def ban():
        async with backend.request(store):
            store.registered_users['a']['is_banned']=True;await backend.save()
    asyncio.run(ban());c.cookies.set('toolbako_session',cookie('test-secret'))
    assert c.get('/notifications').json()=={'user':None,'fast':False}
    assert 'sid-a' not in store.account_sessions
    assert calls.count('locked')==4


@pytest.mark.parametrize('failure',['commit','handler'])
def test_failed_request_does_not_issue_uncommitted_session_cookie(failure):
    from starlette.responses import JSONResponse
    class Backend:
        @asynccontextmanager
        async def request(self,store):yield None
        async def save(self):raise RuntimeError('synthetic commit failure')
    app=FastAPI()
    @app.post('/login')
    async def login(request:Request):
        request.session.clear();request.session['sid']='uncommitted-new-session'
        return JSONResponse({},status_code=500 if failure=='handler' else 200)
    app.add_middleware(DatabaseBoundaryMiddleware,backend=Backend(),store=None)
    app.add_middleware(SessionMiddleware,secret_key='test-secret',session_cookie='toolbako_session')
    c=TestClient(app)
    response=c.post('/login')
    assert response.status_code in {500,503}
    assert 'set-cookie' not in response.headers
    c.cookies.set('toolbako_session',cookie('test-secret'))
    response=c.post('/login')
    from http.cookies import SimpleCookie
    jar=SimpleCookie();jar.load(response.headers['set-cookie'])
    encoded=jar['toolbako_session'].value
    data=json.loads(base64.b64decode(TimestampSigner('test-secret').unsign(encoded)))
    assert data=={'sid':'sid-a'}
