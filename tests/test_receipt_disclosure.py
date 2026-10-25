from datetime import datetime, timezone
from pathlib import Path

import pytest
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader


@pytest.mark.parametrize("test_receipt", [True, False])
def test_receipt_respects_private_contact_and_distinguishes_test_payment(test_receipt):
    env = Environment(loader=ChoiceLoader([
        DictLoader({"base.html": "{% block content %}{% endblock %}"}),
        FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"),
    ]), autoescape=True)
    html = env.get_template("receipt.html").render(
        order={"id": "test-order", "tool_name": "A112", "amount": 11000,
               "buyer_name": "Test buyer", "updated_at": datetime.now(timezone.utc)},
        legal_operator={"name": "合同会社ONE", "contact_on_request": True,
                        "address": "PRIVATE_ADDRESS_MUST_NOT_LEAK", "email": "info@onellc-ai.com",
                        "invoice_number": ""},
        demo_mode=False, test_receipt=test_receipt,
    )
    assert "PRIVATE_ADDRESS_MUST_NOT_LEAK" not in html
    assert "ご請求に応じて遅滞なく提供" in html
    assert "info@onellc-ai.com" in html
    assert "登録番号" not in html
    assert ("実際の支払い・税務申告には使用できません" in html) is test_receipt
