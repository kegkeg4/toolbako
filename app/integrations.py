from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx

from .config import settings


class StripeOutcomeUnknown(Exception):
    """The provider may have succeeded. Never roll back the local reservation."""
    def __init__(self, operation_id: str):
        super().__init__("Stripe operation outcome requires reconciliation")
        self.operation_id = operation_id


class StripeIntegration:
    def __init__(self, secret_key: str, base_url: str, *, api_version: str = "", charge_mode: str = "destination", journal=None):
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")
        self.api_version = api_version
        self.charge_mode = charge_mode if charge_mode in {"destination", "separate"} else "destination"
        self.journal = journal

    async def _post(self, path: str, data: dict[str, Any], *, account: str | None = None) -> dict[str, Any]:
        if not self.secret_key: raise RuntimeError("Stripe is not configured")
        # Never enable money movement merely by adding a live API key. The
        # sandbox implementation must still pass real-account release tests.
        if self.secret_key.startswith(("sk_live_", "rk_live_")):
            raise RuntimeError("本番決済は、永続取引台帳・売上分配・返金の実装検証が完了するまで停止しています")
        data = dict(data)
        idempotency_key = str(data.pop("_idempotency_key", ""))
        if not idempotency_key:
            raise ValueError("Every Stripe mutation needs an explicit idempotency key")
        if self.journal:
            journal_data = {**data, "_stripe_account": account} if account else data
            cached = await self.journal.begin_operation(idempotency_key, path, journal_data)
            if cached is not None:
                return cached
        headers = {"Authorization":f"Bearer {self.secret_key}","Idempotency-Key":idempotency_key}
        if account:
            headers["Stripe-Account"] = account
        if self.api_version:
            headers["Stripe-Version"] = self.api_version
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(f"https://api.stripe.com/v1/{path}", data=data, headers=headers)
        except httpx.HTTPError as exc:
            if self.journal:
                await self.journal.finish_operation(idempotency_key, "unknown")
            raise StripeOutcomeUnknown(idempotency_key) from exc
        if response.status_code >= 500 or response.status_code == 409:
            if self.journal:
                await self.journal.finish_operation(idempotency_key, "unknown")
            raise StripeOutcomeUnknown(idempotency_key)
        if response.status_code >= 400:
            if self.journal:
                await self.journal.finish_operation(idempotency_key, "rejected")
            raise RuntimeError("決済サービスがリクエストを受け付けませんでした。設定・取引状態を確認してください")
        try:
            result = response.json()
            if not isinstance(result, dict) or not isinstance(result.get("id"), str):
                raise ValueError("Invalid provider object")
        except ValueError as exc:
            if self.journal:
                await self.journal.finish_operation(idempotency_key, "unknown")
            raise StripeOutcomeUnknown(idempotency_key) from exc
        if self.journal:
            await self.journal.finish_operation(idempotency_key, "succeeded", result)
        return result

    async def create_express_account(self, user: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        data = {"_idempotency_key":idempotency_key,"type":"express","country":"JP","email":user.get("email", ""),"metadata[user_id]":user["id"],"capabilities[card_payments][requested]":"true","capabilities[transfers][requested]":"true"}
        if self.charge_mode == "separate":
            data["settings[payouts][schedule][interval]"] = "manual"
        return await self._post("accounts", data)

    async def _get(self, path: str, *, account: str | None = None, params=None):
        headers = {"Authorization": f"Bearer {self.secret_key}"}
        if self.api_version: headers["Stripe-Version"] = self.api_version
        if account: headers["Stripe-Account"] = account
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(f"https://api.stripe.com/v1/{path}", params=params, headers=headers)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict): raise ValueError("invalid object")
            return result
        except (httpx.HTTPError, ValueError):
            raise RuntimeError("Stripeの取引状態を確認できませんでした") from None

    async def retrieve_payment(self, payment_intent: str):
        return await self._get(f"payment_intents/{payment_intent}", params={"expand[]": "latest_charge.balance_transaction"})

    async def retrieve_balance(self, account: str):
        return await self._get("balance", account=account)

    async def retrieve_payout(self, payout_id: str, account: str):
        return await self._get(f"payouts/{payout_id}", account=account)

    async def list_financial_objects(self, path: str, *, account=None, params=None):
        """Bounded, complete GET traversal; never infer absence from one page."""
        filters = dict(params or {})
        filters["limit"] = "100"
        objects, cursors = [], set()
        for _ in range(10):
            page = await self._get(path, account=account, params=filters)
            batch = page.get("data")
            if (page.get("object") != "list" or not isinstance(batch, list)
                    or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in batch)
                    or type(page.get("has_more")) is not bool):
                raise RuntimeError("Stripeの照合一覧を確認できません")
            objects.extend(batch)
            if not page["has_more"]:
                return objects
            if not batch or batch[-1]["id"] in cursors:
                break
            cursors.add(batch[-1]["id"])
            filters["starting_after"] = batch[-1]["id"]
        raise RuntimeError("照合件数が上限を超えています。運営による詳細確認が必要です")

    async def create_allocation_transfer(self, payout: dict, allocation: dict, source_charge: str):
        if self.charge_mode != "separate":
            raise RuntimeError("分配方式が一致しません")
        return await self._post("transfers", {
            "_idempotency_key": f"allocation-{allocation['id']}",
            "amount": str(allocation["transfer_amount"]), "currency": "jpy",
            "destination": payout["account_id"], "source_transaction": source_charge,
            "transfer_group": f"order-{allocation['order_id']}",
            "metadata[payout_id]": payout["id"], "metadata[allocation_id]": allocation["id"],
        })

    async def create_bank_payout(self, payout: dict):
        return await self._post("payouts", {
            "_idempotency_key": f"bank-payout-{payout['id']}",
            "amount": str(payout["net_amount"]), "currency": "jpy", "method": "standard",
            "metadata[payout_id]": payout["id"],
        }, account=payout["account_id"])

    def reversal_data(self, allocation: dict):
        return {
            "_idempotency_key": f"reverse-allocation-{allocation['id']}",
            "amount": str(allocation["transfer_amount"]), "metadata[allocation_id]": allocation["id"],
        }

    async def reverse_allocation(self, allocation: dict):
        return await self._post(f"transfers/{allocation['transfer_id']}/reversals", self.reversal_data(allocation))

    async def create_account_link(self, account_id: str) -> dict[str, Any]:
        return await self._post("account_links", {"_idempotency_key":f"account-link-{account_id}-{uuid4()}","account":account_id,"refresh_url":f"{self.base_url}/seller/payments?refresh=1","return_url":f"{self.base_url}/seller/payments?returned=1","type":"account_onboarding"})

    async def retrieve_account(self, account_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20) as client:
            headers = {"Authorization":f"Bearer {self.secret_key}"}
            if self.api_version: headers["Stripe-Version"] = self.api_version
            response = await client.get(f"https://api.stripe.com/v1/accounts/{account_id}", headers=headers)
        if response.status_code >= 400: raise RuntimeError("Stripe account lookup failed")
        return response.json()

    def checkout_data(self, order: dict[str, Any], destination: str) -> dict[str, Any]:
        """One canonical payload for submission and read-only reconciliation."""
        if self.charge_mode == "separate" and order.get("billing_type") == "subscription":
            raise RuntimeError("月額契約は請求ごとの売上分配を検証するまで受付を停止しています")
        cancel_path = order.get("checkout_cancel_path") or f"/checkout/{order['tool_slug']}"
        data: dict[str, Any] = {"_idempotency_key":f"checkout-{order['id']}","mode":"subscription" if order.get("billing_type")=="subscription" else "payment","success_url":f"{self.base_url}/orders/{order['id']}?payment=success","cancel_url":f"{self.base_url}{cancel_path}?payment=cancelled","line_items[0][quantity]":"1","line_items[0][price_data][currency]":"jpy","line_items[0][price_data][unit_amount]":str(order["amount"]),"line_items[0][price_data][product_data][name]":order["tool_name"],"metadata[order_id]":order["id"]}
        if order.get("buyer_email"):
            data["customer_email"] = order["buyer_email"]
        if order.get("billing_type") == "subscription":
            data["line_items[0][price_data][recurring][interval]"] = "month"
            data["subscription_data[metadata][order_id]"] = order["id"]
            if self.charge_mode == "destination":
                rate = order.get("platform_fee_rate", settings.platform_fee_rate)
                data["subscription_data[application_fee_percent]"] = str(round(rate * 100, 2))
                data["subscription_data[transfer_data][destination]"] = destination
            # payment_intent_data is invalid in subscription mode. The eventual
            # separate-transfer worker must allocate each paid invoice itself.
        else:
            data["payment_intent_data[metadata][order_id]"] = order["id"]
            if self.charge_mode == "destination":
                data["payment_intent_data[application_fee_amount]"] = str(order["platform_fee"])
                data["payment_intent_data[transfer_data][destination]"] = destination
            else:
                data["payment_intent_data[transfer_group]"] = f"order-{order['id']}"
        return data

    async def create_checkout(self, order: dict[str, Any], destination: str) -> dict[str, Any]:
        return await self._post("checkout/sessions", self.checkout_data(order, destination))

    def extra_checkout_data(self, order: dict[str, Any], extra: dict[str, Any], destination: str) -> dict[str, Any]:
        data = {"_idempotency_key":f"extra-{extra['id']}","mode":"payment","success_url":f"{self.base_url}/orders/{order['id']}?extra=success","cancel_url":f"{self.base_url}/orders/{order['id']}?extra=cancelled","line_items[0][quantity]":"1","line_items[0][price_data][currency]":"jpy","line_items[0][price_data][unit_amount]":str(extra["amount"]),"line_items[0][price_data][product_data][name]":extra["note"],"metadata[order_id]":order["id"],"metadata[extra_id]":extra["id"],"payment_intent_data[metadata][order_id]":order["id"],"payment_intent_data[metadata][extra_id]":extra["id"]}
        if self.charge_mode == "destination":
            data["payment_intent_data[application_fee_amount]"] = str(round(extra["amount"] * order.get("platform_fee_rate", settings.platform_fee_rate)))
            data["payment_intent_data[transfer_data][destination]"] = destination
        else:
            data["payment_intent_data[transfer_group]"] = f"order-{order['id']}"
        return data

    async def create_extra_checkout(self, order: dict[str, Any], extra: dict[str, Any], destination: str) -> dict[str, Any]:
        return await self._post("checkout/sessions", self.extra_checkout_data(order, extra, destination))

    async def create_transfer(self, *, amount: int, destination: str, order_id: str, source_transaction: str | None = None) -> dict[str, Any]:
        """Release a seller's net balance for separate charges and transfers.

        This method is intentionally unused while ``STRIPE_CHARGE_MODE`` is
        ``destination``. The production payout worker should call it only after
        acceptance/hold rules pass and should persist the returned transfer ID.
        """
        if self.charge_mode != "separate":
            raise RuntimeError("explicit transfers require STRIPE_CHARGE_MODE=separate")
        data: dict[str, Any] = {"_idempotency_key":f"transfer-order-{order_id}","amount":str(amount),"currency":"jpy","destination":destination,"metadata[order_id]":order_id}
        if source_transaction:
            data["source_transaction"] = source_transaction
        return await self._post("transfers", data)

    async def cancel_subscription(self, subscription_id: str, idempotency_key: str) -> dict[str, Any]:
        return await self._post(f"subscriptions/{subscription_id}", {"_idempotency_key":idempotency_key,"cancel_at_period_end":"true"})

    def refund_data(self, payment_intent: str, order_id: str, *, extra_id: str | None = None) -> dict[str, Any]:
        key = f"refund-extra-{order_id}-{extra_id}" if extra_id else f"refund-{order_id}"
        data = {"_idempotency_key":key,"payment_intent":payment_intent,"metadata[order_id]":order_id}
        if extra_id:
            data["metadata[extra_id]"] = extra_id
        if self.charge_mode == "destination":
            data.update({"reverse_transfer":"true","refund_application_fee":"true"})
        return data

    async def refund_payment(self, payment_intent: str, order_id: str, *, extra_id: str | None = None) -> dict[str, Any]:
        return await self._post("refunds", self.refund_data(payment_intent, order_id, extra_id=extra_id))

    async def create_identity_session(self, user_id: str) -> dict[str, Any]:
        return await self._post("identity/verification_sessions", {"_idempotency_key":f"identity-{user_id}-{uuid4()}","type":"document","metadata[user_id]":user_id,"return_url":f"{self.base_url}/verification?returned=1","options[document][require_matching_selfie]":"true"})


class EmailIntegration:
    def __init__(self, api_key: str, sender: str): self.api_key, self.sender = api_key, sender

    async def send(self, to: str, subject: str, text: str, *, idempotency_key: str | None = None) -> bool:
        if not self.api_key or not self.sender or not to: return False
        headers = {"Authorization":f"Bearer {self.api_key}","Content-Type":"application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post("https://api.resend.com/emails", headers=headers, json={"from":self.sender,"to":[to],"subject":subject,"text":text})
        return response.status_code < 300
