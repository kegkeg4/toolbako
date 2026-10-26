"""Anonymous catalog isolation against real disposable PostgreSQL."""
import asyncio
from copy import deepcopy

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.data import DemoStore
from app.database import DatabaseBoundaryMiddleware, PostgresStateStore, StorageUnavailable
from app.public_read import PublicView, PUBLIC_READ_PATHS, anonymous_read
from test_database import database, query


def test_route_gate_never_treats_credentials_or_writes_as_public():
    for path in PUBLIC_READ_PATHS:
        assert anonymous_read(Request({'type':'http','method':'GET','path':path,'headers':[]}))
    for method, path, headers in [
        ('POST','/tools',[]), ('GET','/tools/new',[]), ('GET','/tools/x',[]),
        ('GET','/admin',[]), ('GET','/orders/x',[]), ('GET','/tools',[(b'cookie',b'')]),
        ('GET','/login',[(b'cookie',b'toolbako_session=invalid')]),
        ('GET','/',[(b'authorization',b'Bearer invalid')]),
    ]:
        assert not anonymous_read(Request({'type':'http','method':method,'path':path,'headers':headers}))


def test_public_reads_do_not_wait_for_writer_or_observe_uncommitted_state(database):
    writer, dsn = database
    store = DemoStore(seed=False)
    item = deepcopy(DemoStore().tools[0])
    item.update(name='Committed', is_published=True, status='published')

    async def run():
        reader = PostgresStateStore(dsn, reuse_connections=True)
        try:
            async with writer.request(store):
                store.tools.append(item)
                store.registered_users['seller']={'id':'private-id','email':'secret@example.invalid',
                    'password_hash':'secret-hash','is_certified_creator':True}
                await writer.save()
                store.tools[0]['name']='Uncommitted'
                # Writer still owns its advisory lock and mutable in-memory state.
                view = await asyncio.wait_for(reader.public_view(catalog=True), 2)
                assert view.tools[0]['name']=='Committed'
                assert view.badges_for('seller')['is_certified_creator'] is True
                assert 'email' not in view.badges_for('seller')
                assert 'id' not in view.badges_for('seller')
                assert not hasattr(view,'orders') and not hasattr(view,'account_sessions')
                assert store.tools[0]['name']=='Uncommitted'
                view.tools[0]['name']='Read-view-only'
                assert (await reader.public_view(catalog=True)).tools[0]['name']=='Committed'
                store.tools[0]['is_published']=False
                await writer.save()
                # Fresh commit visibility, no TTL cache after withdrawal.
                withdrawn = await reader.public_view(catalog=True)
                assert withdrawn.list_tools()==[] and withdrawn.tools==[]
        finally:
            await reader.close()
        assert reader._public_slots._value == 2 and not reader._public_connections
    asyncio.run(run())


def test_reader_sees_atomic_database_commit_and_readonly_pool(database):
    writer, dsn = database
    async def run():
        store=DemoStore(seed=False)
        async with writer.request(store):
            store.tools=deepcopy(DemoStore().tools[:1]);await writer.save()
        reader=PostgresStateStore(dsn,reuse_connections=True)
        try:
            before=await reader.public_view(catalog=True)
            async with await writer.connect() as conn:
                async with conn.transaction():
                    await conn.execute("update toolbako_runtime.marketplace_state set payload=jsonb_set(payload,'{tools,0,name}','\"New committed name\"') where singleton")
                    assert (await reader.public_view(catalog=True)).tools[0]['name']==before.tools[0]['name']
                assert (await reader.public_view(catalog=True)).tools[0]['name']=='New committed name'
            connection=reader._public_connections[-1][0]
            with pytest.raises(Exception) as error:
                await connection.execute('delete from toolbako_runtime.marketplace_state')
            assert getattr(error.value,'sqlstate',None)=='25006'
            assert query(dsn,'select count(*) from toolbako_runtime.marketplace_state')==[(1,)]
            # Dead pooled socket is replaced only for the read-only SELECT.
            query(dsn,'select pg_terminate_backend(%s)',(connection.info.backend_pid,))
            assert (await reader.public_view(catalog=True)).tools
            views=await asyncio.gather(*(reader.public_view(catalog=True) for _ in range(12)))
            assert len({id(v.tools) for v in views})==12
            assert len(reader._public_connections)<=2
        finally:await reader.close()
    asyncio.run(run())


def test_empty_or_wrong_schema_fails_closed_without_seeding(database):
    backend,dsn=database
    async def run():
        with pytest.raises(StorageUnavailable):await backend.public_view(catalog=True)
        assert query(dsn,'select count(*) from toolbako_runtime.marketplace_state')==[(0,)]
        async with backend.request(DemoStore(seed=False)):await backend.save()
        async with await backend.connect() as conn:
            await conn.execute('update toolbako_runtime.schema_version set version=999')
        with pytest.raises(StorageUnavailable):await backend.public_view(catalog=False)
    asyncio.run(run())


def test_public_render_never_calls_shared_session_or_badge_store(monkeypatch):
    from app import main
    sample=deepcopy(DemoStore().tools[:1])
    view=PublicView(sample, {sample[0]['author_username']:{'is_certified_creator':True}})
    def forbidden(*args,**kwargs):raise AssertionError('shared store touched by anonymous render')
    monkeypatch.setattr(main.session_service,'current',forbidden)
    monkeypatch.setattr(main.store,'list_tools',forbidden)
    monkeypatch.setattr(main.store,'get_public',forbidden)
    monkeypatch.setitem(main.templates.env.globals,'creator_badges_for',forbidden)
    class ReadBackend:
        async def public_view(self,**kwargs):return view
        def request(self,*args):raise AssertionError('write boundary used')
    wrapper=DatabaseBoundaryMiddleware(main.app,backend=ReadBackend(),store=main.store,public_reads=True)
    client=TestClient(wrapper)
    for path in PUBLIC_READ_PATHS:
        response=client.get(path)
        assert response.status_code==200, path
        assert 'set-cookie' not in response.headers
    assert client.get('/tools?q='+sample[0]['name']).status_code==200
    assert 'creator-status-badge--certified' in client.get('/tools').text


def test_credential_routes_keep_original_database_boundary():
    from contextlib import asynccontextmanager
    calls=[]
    class Backend:
        async def public_view(self,**kwargs):calls.append('public');return PublicView([],{})
        @asynccontextmanager
        async def request(self,store):calls.append('locked');yield None
        async def save(self):calls.append('save')
    app=FastAPI()
    @app.api_route('/tools',methods=['GET','POST'])
    async def tools(request:Request):return {'public':hasattr(request.state,'public_view')}
    app.add_middleware(DatabaseBoundaryMiddleware,backend=Backend(),store=None,public_reads=True)
    c=TestClient(app)
    assert c.get('/tools').json()=={'public':True}
    assert c.get('/tools',headers={'cookie':'toolbako_session=invalid'}).json()=={'public':False}
    assert c.post('/tools').json()=={'public':False}
    assert calls==['public','locked','save','locked','save']
