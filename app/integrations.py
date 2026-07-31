from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx


class StripeIntegration:
    def __init__(self, secret_key: str, base_url: str, *, api_version: str = "", charge_mode: str = "destination"):
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")
        self.api_version = api_version
        self.charge_mode = charge_mode if charge_mode in {"destination", "separate"} else "destination"

    async def _post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        if not self.secret_key: raise RuntimeError("Stripe is not configured")
        idempotency_key = str(data.pop("_idempotency_key", "")) or path
        headers = {"Authorization":f"Bearer {self.secret_key}","Idempotency-Key":idempotency_key}
        if self.api_version:
            headers["Stripe-Version"] = self.api_version
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(f"https://api.stripe.com/v1/{path}", data=data, headers=headers)
        if response.status_code >= 400:
            message = (response.json().get("error") or {}).get("message", "Stripe request failed")
            raise RuntimeError(message)
        return response.json()

    async def create_express_account(self, user: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return await self._post("accounts", {"_idempotency_key":idempotency_key,"type":"express","country":"JP","email":user.get("email", ""),"metadata[user_id]":user["id"],"capabilities[card_payments][requested]":"true","capabilities[transfers][requested]":"true"})

    async def create_account_link(self, account_id: str) -> dict[str, Any]:
        return await self._post("account_links", {"_idempotency_key":f"account-link-{account_id}-{uuid4()}","account":account_id,"refresh_url":f"{self.base_url}/seller/payments?refresh=1","return_url":f"{self.base_url}/seller/payments?returned=1","type":"account_onboarding"})

    async def retrieve_account(self, account_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20) as client:
            headers = {"Authorization":f"Bearer {self.secret_key}"}
            if self.api_version: headers["Stripe-Version"] = self.api_version
            response = await client.get(f"https://api.stripe.com/v1/accounts/{account_id}", headers=headers)
        if response.status_code >= 400: raise RuntimeError("Stripe account lookup failed")
        return response.json()

    async def create_checkout(self, order: dict[str, Any], destination: str) -> dict[str, Any]:
        cancel_path = order.get("checkout_cancel_path") or f"/checkout/{order['tool_slug']}"
        data: dict[str, Any] = {"_idempotency_key":f"checkout-{order['id']}","mode":"subscription" if order.get("billing_type")=="subscription" else "payment","success_url":f"{self.base_url}/orders/{order['id']}?payment=success","cancel_url":f"{self.base_url}{cancel_path}?payment=cancelled","line_items[0][quantity]":"1","line_items[0][price_data][currency]":"jpy","line_items[0][price_data][unit_amount]":str(order["amount"]),"line_items[0][price_data][product_data][name]":order["tool_name"],"metadata[order_id]":order["id"]}
        if order.get("buyer_email"):
            data["customer_email"] = order["buyer_email"]
        if order.get("billing_type") == "subscription":
            data["line_items[0][price_data][recurring][interval]"] = "month"
            data["subscription_data[metadata][order_id]"] = order["id"]
            if self.charge_mode == "destination":
                data["subscription_data[application_fee_percent]"] = "10"
                data["subscription_data[transfer_data][destination]"] = destination
            else:
                data["payment_intent_data[transfer_group]"] = f"order-{order['id']}"
        else:
            data["payment_intent_data[metadata][order_id]"] = order["id"]
            if self.charge_mode == "destination":
                data["payment_intent_data[application_fee_amount]"] = str(order["platform_fee"])
                data["payment_intent_data[transfer_data][destination]"] = destination
            else:
                data["payment_intent_data[transfer_group]"] = f"order-{order['id']}"
        return await self._post("checkout/sessions", data)

    async def create_extra_checkout(self, order: dict[str, Any], extra: dict[str, Any], destination: str) -> dict[str, Any]:
        data = {"_idempotency_key":f"extra-{extra['id']}","mode":"payment","success_url":f"{self.base_url}/orders/{order['id']}?extra=success","cancel_url":f"{self.base_url}/orders/{order['id']}?extra=cancelled","line_items[0][quantity]":"1","line_items[0][price_data][currency]":"jpy","line_items[0][price_data][unit_amount]":str(extra["amount"]),"line_items[0][price_data][product_data][name]":extra["note"],"metadata[order_id]":order["id"],"metadata[extra_id]":extra["id"],"payment_intent_data[metadata][order_id]":order["id"],"payment_intent_data[metadata][extra_id]":extra["id"]}
        if self.charge_mode == "destination":
            data["payment_intent_data[application_fee_amount]"] = str(round(extra["amount"]*.1))
            data["payment_intent_data[transfer_data][destination]"] = destination
        else:
            data["payment_intent_data[transfer_group]"] = f"order-{order['id']}"
        return await self._post("checkout/sessions", data)

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

    async def refund_payment(self, payment_intent: str, order_id: str) -> dict[str, Any]:
        data = {"_idempotency_key":f"refund-{order_id}","payment_intent":payment_intent,"metadata[order_id]":order_id}
        if self.charge_mode == "destination":
            data.update({"reverse_transfer":"true","refund_application_fee":"true"})
        return await self._post("refunds", data)

    async def create_identity_session(self, user_id: str) -> dict[str, Any]:
        return await self._post("identity/verification_sessions", {"_idempotency_key":f"identity-{user_id}-{uuid4()}","type":"document","metadata[user_id]":user_id,"return_url":f"{self.base_url}/verification?returned=1","options[document][require_matching_selfie]":"true"})


class EmailIntegration:
    def __init__(self, api_key: str, sender: str): self.api_key, self.sender = api_key, sender

    async def send(self, to: str, subject: str, text: str) -> bool:
        if not self.api_key or not self.sender or not to: return False
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post("https://api.resend.com/emails", headers={"Authorization":f"Bearer {self.api_key}","Content-Type":"application/json"}, json={"from":self.sender,"to":[to],"subject":subject,"text":text})
        return response.status_code < 300
