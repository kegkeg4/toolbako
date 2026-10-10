"""Invoked in an isolated interpreter by test_database; no external payments."""
import asyncio
import json
import os
import re
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.main import app, database_store, store, stripe
from app.config import settings
from app.database import PostgresStateStore


def main():
    assert database_store is not None
    with TestClient(app) as seller:
        seller.headers["origin"] = settings.site_base_url
        home = seller.get("/")
        assert home.status_code == 200, home.text
        assert "最初のクリエイターを募集中" in home.text
        assert "<b>38</b>" not in home.text
        assert not store.orders and not store.registered_users
        signup = seller.post("/signup", data={
            "display_name": "登録検証", "username": "runtime_seller", "email": "seller@example.invalid",
            "password": "local-test-only-1234", "terms_agreement": "yes",
        }, follow_redirects=False)
        assert signup.status_code == 303, signup.text
        assert seller.get("/seller").status_code == 200
        created = seller.post("/tools/new", data={
            "name": "Runtime test tool", "tagline": "データ保存の検証ツール", "description_md": "テスト専用の商品です。外部販売はしません。",
            "category": "業務効率化", "distribution": "webapp", "price_type": "paid", "price": 1000,
            "fulfillment_type": "custom", "prohibited_agreement": "yes",
        }, follow_redirects=False)
        assert created.status_code == 303, created.text
        tool = store.tools[0]
        slug = tool["slug"]
        assert seller.get(f"/tools/{slug}").status_code == 200
        seller_id = store.registered_users["runtime_seller"]["id"]

        # Controlled fixture setup only: safety moderation and sandbox Connect.
        # No external account or real moderation result is produced by this test.
        async def prepare():
            async with database_store.request(store):
                store.tools[0]["status"] = "published"
                store.tools[0]["is_published"] = True
                store.tools[0]["safety_scan"]["status"] = "passed"
                store.connected_accounts[seller_id] = {"charges_enabled": True, "account_id": "acct_test_runtime"}
                await database_store.save()
        asyncio.run(prepare())
        with TestClient(app) as buyer:
            buyer.headers["origin"] = settings.site_base_url
            signup = buyer.post("/signup", data={
                "display_name": "購入検証", "username": "runtime_buyer", "email": "buyer@example.invalid",
                "password": "local-test-only-1234", "terms_agreement": "yes",
            }, follow_redirects=False)
            assert signup.status_code == 303, signup.text
            calls = []
            def provider(request):
                calls.append(request)
                assert request.url.host == "api.stripe.com"
                return httpx.Response(200, json={"id": "cs_runtime", "url": "https://checkout.stripe.com/test-runtime"})
            original_client = httpx.AsyncClient
            httpx.AsyncClient = lambda **kw: original_client(transport=httpx.MockTransport(provider), **kw)
            try:
                checkout = buyer.post(f"/checkout/{slug}", data={"purchase_agreement": "yes"}, follow_redirects=False)
                assert checkout.status_code == 303, checkout.text
                assert checkout.headers["location"] == "https://checkout.stripe.com/test-runtime"
                order_id = store.orders[0]["id"]
                repeated = buyer.post(f"/checkout/{slug}", data={"purchase_agreement": "yes"}, follow_redirects=False)
                assert repeated.status_code == 303
                assert len(calls) == 1
                assert buyer.get(f"/orders/{order_id}").status_code == 202
            finally:
                httpx.AsyncClient = original_client

        # A newly constructed repository sees both users, the tool and ONE order.
        restarted = PostgresStateStore(os.environ["DATABASE_URL"])
        async def verify():
            async with restarted.request(store):
                assert len(store.registered_users) == 2
                assert len(store.tools) == 1
                assert len(store.orders) == 1
                assert store.orders[0]["checkout_session_id"] == "cs_runtime"
                assert len(store.account_sessions) == 2
        asyncio.run(verify())

        # Payout HTTP contract, still disposable fixtures and no provider call.
        async def earned_fixture():
            async with database_store.request(store):
                store.identity_applications[seller_id] = {"status": "verified", "provider": "test_fixture"}
                store.connected_accounts[seller_id].update(payouts_enabled=True, details_submitted=True)
                store.orders[0].update(status="completed", payment_status="paid", payment_reference="pi_runtime")
                await database_store.save()
        asyncio.run(earned_fixture())
        object.__setattr__(settings, "stripe_charge_mode", "separate")
        page = seller.get("/payouts")
        assert page.status_code == 200, page.text
        token_match = re.search(r'name="request_key" value="([A-Za-z0-9_-]+)"', page.text)
        assert token_match, page.text
        token = token_match.group(1)
        data = {"amount": 200, "request_key": token}
        for _ in range(2):
            submitted = seller.post("/payouts", data=data, follow_redirects=False)
            assert submitted.status_code == 303, submitted.text
        page = seller.get("/payouts")
        assert "申請受付" in page.text and "¥650" in page.text
        async def verify_payout():
            async with restarted.request(store):
                assert len(store.payouts) == 1
                assert store.payouts[0]["amount"] == 200 and store.payouts[0]["net_amount"] == 40
        asyncio.run(verify_payout())
    print("Runtime app flow passed: signup -> listing -> Checkout reservation -> restart")


if __name__ == "__main__":
    main()
