"""Mode labels and profile badges must not misrepresent Connect admission."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import DictLoader, Environment


@pytest.mark.parametrize('live,ready,verified,form',[
    (True,True,False,True),(True,False,False,False),
    (False,True,False,False),(False,True,True,True),
])
def test_payout_page_uses_mode_separately_from_execution_gate(live,ready,verified,form):
    source=(Path(__file__).resolve().parents[1]/'templates/payouts.html').read_text(encoding='utf-8')
    env=Environment(loader=DictLoader({'base.html':'{% block content %}{% endblock %}','payouts.html':source}),autoescape=True)
    html=env.get_template('payouts.html').render(live_payouts=live,payout_requests_ready=ready,
        demo_mode=False,user={'is_verified':verified},balance=1000,payout_minimum=161,
        payout_fee=160,payout_fee_free_threshold=3000,payout_auto_days=120,payout_request_key='opaque',
        request=SimpleNamespace(query_params={}),payouts=[],payout_labels={})
    assert ('name="request_key"' in html) is form
    if live:
        assert 'href="/verification"' not in html
        assert '振込を申請（テスト）' not in html and '（テスト決済）' not in html
    if live and not ready:
        assert '申請受付・実際の銀行振込はまだ開始されません' in html
        assert 'href="/seller/payments"' in html
