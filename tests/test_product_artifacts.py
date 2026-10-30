from copy import deepcopy
from dataclasses import replace
import hashlib

import pytest
from fastapi.testclient import TestClient

from app import main, data, product_artifacts
from app.data import DemoStore
from app.product_artifacts import artifact_ready, download_scope


@pytest.fixture
def shop(monkeypatch, tmp_path):
    store = DemoStore()
    tool = store.tools[0]
    tool.update(fulfillment_type='instant', price_type='paid', price=11000, is_published=True, status='published')
    actor = {'user': {'id': tool['author_id'], 'username':tool['author_username'], 'display_name':tool['author_name']}}
    config = replace(main.settings, demo_mode=True, storage_provider='local', clamav_host='', private_storage_path=str(tmp_path))
    monkeypatch.setattr(main,'settings',config)
    monkeypatch.setattr(data,'settings',config)
    monkeypatch.setattr(main,'store',store)
    monkeypatch.setattr(main,'current_user',lambda request:actor['user'])
    return TestClient(main.app), store, tool, actor, config


def upload(client, tool, content=b'print("synthetic file")', **fields):
    return client.post('/seller/tools/'+tool['slug']+'/artifact', data={'guide':'検証用ファイルです。実売商品ではありません。','listing_rights_agreement':'yes',**fields},
                       files={'artifact_file':('test.py',content,'application/octet-stream')},follow_redirects=False)


def cloud_artifact():
    sha=hashlib.sha256(b'example').hexdigest()
    scope='product-'+'a'*32
    return {'version':'a'*32,'storage_scope':scope,'guide':'検証用の導入説明です。',
            'file':{'id':sha[:24],'sha256':sha,'scan_status':'passed','scan_engine':'clamav','name':'test.txt','size':7,
                    'storage_backend':'supabase','bucket':'toolbako-deliveries','object_key':f'deliveries/{scope}/'+ 'b'*32+'.txt'}}


def test_upload_is_private_versioned_and_reopens_review(shop):
    client, store, tool, _, _ = shop
    old = {'version':'previous'}; tool['instant_artifact']=old
    assert upload(client,tool).status_code == 303
    asset=tool['instant_artifact']
    assert artifact_ready(asset) and not artifact_ready(asset,production=True)
    assert asset['file']['sha256'] == hashlib.sha256(b'print("synthetic file")').hexdigest()
    assert not tool['is_published'] and tool['safety_scan']['status']=='pending'
    assert store.legal_consents[-1]['document_type']=='listing_rights'
    assert '検査済みのファイルを保存しました' in client.get('/seller/tools/'+tool['slug']+'/edit?artifact_saved=1').text


def test_upload_permission_and_rights_required(shop):
    client, _, tool, actor, _ = shop
    assert upload(client,tool,listing_rights_agreement='no').status_code == 422
    actor['user']={'id':'intruder','username':'intruder','display_name':'別の人'}
    assert upload(client,tool).status_code == 403
    assert 'instant_artifact' not in tool


@pytest.mark.parametrize('failure',['scanner_down','malware','scanner_unconfigured','storage_down'])
def test_production_upload_fails_closed(shop,monkeypatch,failure):
    client, _, tool, _, config=shop
    config=replace(config,demo_mode=False,clamav_host='' if failure=='scanner_unconfigured' else 'scanner',storage_provider='supabase')
    monkeypatch.setattr(main,'settings',config)
    def scan(*args):
        if failure=='scanner_down': raise OSError('unavailable')
        return failure!='malware','clamav'
    monkeypatch.setattr(product_artifacts,'clamav_scan',scan)
    class Storage:
        def __init__(self,*args): pass
        async def upload(self,*args): raise product_artifacts.ObjectStorageError('unavailable')
    monkeypatch.setattr(product_artifacts,'SupabaseObjectStorage',Storage)
    old=deepcopy(tool)
    assert upload(client,tool).status_code in {422,503}
    assert tool==old


def test_production_purchase_requires_file_and_keeps_purchased_version(shop,monkeypatch):
    _,store,tool,_,config=shop
    monkeypatch.setattr(data,'settings',replace(config,demo_mode=False))
    buyer={'id':'buyer','username':'buyer','display_name':'購入者'}
    before=deepcopy(store.orders)
    with pytest.raises(ValueError,match='artifact unavailable'): store.buy(buyer,tool['slug'],payment_pending=True)
    assert store.orders==before
    tool['instant_artifact']=cloud_artifact()
    order=store.buy(buyer,tool['slug'],payment_pending=True)
    store.complete_instant_order(order)
    assert order['status']=='in_progress' and order['delivery'] is None
    tool['instant_artifact']['guide']='将来の版'
    order['payment_status']='paid'; store.complete_instant_order(order)
    assert order['status']=='completed' and order['delivery']['files'][0]['sha256']==cloud_artifact()['file']['sha256']
    assert order['delivery']['note']=='検証用の導入説明です。'
    before=deepcopy(order);store.complete_instant_order(order);assert order==before


def test_purchased_file_authorization_refund_and_unchanged_version(shop):
    client,store,tool,actor,_=shop
    assert upload(client,tool,content=b'first version').status_code==303
    tool.update(is_published=True,status='published')
    actor['user']={'id':'buyer','username':'buyer','display_name':'購入者'}
    order=store.buy(actor['user'],tool['slug'])
    item=order['delivery']['files'][0]
    url=f'/orders/{order["id"]}/files/{item["id"]}'
    assert client.get(url).content==b'first version'
    tool['instant_artifact']['file']['path']='changed'
    assert client.get(url).content==b'first version'
    order['refund_status']='pending'; assert client.get(url).status_code==404
    order.pop('refund_status');order['payment_status']='pending';assert client.get(url).status_code==404
    order['payment_status']='paid';order['billing_type']='subscription';assert client.get(url).status_code==404
    order['billing_type']='one_time';actor['user']={'id':'stranger','username':'stranger','display_name':'他人'}
    assert client.get(url).status_code==404
    actor['user']=None;assert client.get(url).status_code==404


def test_signed_scope_only_accepts_order_snapshot():
    artifact=cloud_artifact();item=artifact['file'];order={'id':'order','instant_artifact':artifact}
    assert download_scope(order,item)==artifact['storage_scope']
    forged={**item,'object_key':item['object_key'].replace('b'*32,'c'*32)}
    with pytest.raises(ValueError): download_scope(order,forged)
    with pytest.raises(ValueError): download_scope({'id':'order'},item)


def test_publish_and_checkout_block_missing_real_file(shop,monkeypatch):
    client,store,tool,actor,config=shop
    monkeypatch.setattr(main,'settings',replace(config,demo_mode=False))
    tool['safety_scan']={'status':'passed'}
    assert client.post('/seller/tools/'+tool['slug']+'/status/published').status_code==409
    actor['user']={'id':'buyer','username':'buyer','display_name':'購入者'}
    assert client.get('/checkout/'+tool['slug']).status_code==409


def test_legacy_paid_order_without_file_does_not_fabricate_delivery(shop,monkeypatch):
    _,store,_,_,config=shop
    monkeypatch.setattr(data,'settings',replace(config,demo_mode=False))
    order={'fulfillment_type':'instant','status':'in_progress','payment_status':'paid'}
    store.complete_instant_order(order)
    assert order['status']=='in_progress' and 'delivery' not in order
