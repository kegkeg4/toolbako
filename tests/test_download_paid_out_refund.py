"""A separate paid-out refund case never leaves buyer file access active."""
import asyncio
from dataclasses import replace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import main
from app.data import DemoStore


@pytest.mark.parametrize("status", ["opened", "submitting", "unknown", "held",
    "awaiting_confirmation", "awaiting_review", "buyer_refunded", "unrecognized"])
@pytest.mark.parametrize("actor", ["buyer", "seller"])
def test_separate_case_blocks_buyer_and_preserves_seller_access(monkeypatch, tmp_path, status, actor):
    store = DemoStore(seed=False)
    file = tmp_path / "synthetic.txt"
    file.write_bytes(b"artificial delivery")
    order = {"id": "order", "buyer_id": "buyer", "seller_id": "seller", "payment_status": "paid",
             "status": "completed", "paid_out_refund_case": {"status": status},
             "delivery": {"files": [{"id": "file", "scan_status": "passed", "name": file.name,
                                       "path": str(file)}]}}
    # No generic refund_status: the separate case itself must enforce the hold.
    store.orders = [order]
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "settings", replace(main.settings, demo_mode=False,
                                                   private_storage_path=str(tmp_path)))
    monkeypatch.setattr(main, "current_user", lambda request: {"id": actor})
    request = Request({"type": "http", "method": "GET", "path": "/orders/order/files/file", "headers": []})
    if actor == "buyer":
        with pytest.raises(HTTPException) as error:
            asyncio.run(main.download_delivery_file(request, "order", "file"))
        assert error.value.status_code == 404
        assert not store.audit_logs
    else:
        response = asyncio.run(main.download_delivery_file(request, "order", "file"))
        assert response.status_code == 200 and response.path == file.resolve()
        assert store.audit_logs[0]["action"] == "delivery.downloaded"
    assert order["payment_status"] == "paid" and "refund_status" not in order
