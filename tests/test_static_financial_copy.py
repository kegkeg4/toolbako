"""Public legal-page rendering follows the existing financial gates, without IO."""
import asyncio
from dataclasses import replace
import re

import pytest
from starlette.requests import Request

from app import live_checkout, main
from app.config import Settings


@pytest.fixture
def render_page(monkeypatch):
    def render(settings, page="tokushoho", *, connection_verified=True):
        monkeypatch.setattr(main, "settings", settings)
        # Pure configuration verification is separately covered by its own suite.
        # Keep the actual mode/demo/operation helpers and route/template here.
        monkeypatch.setattr(live_checkout, "_live_configuration_verified",
                            lambda candidate: candidate is settings and connection_verified)
        request = Request({"type": "http", "method": "GET", "scheme": "https",
                           "server": ("toolbako.example", 443), "path": "/" + page,
                           "query_string": b"", "headers": [], "session": {}})
        response = asyncio.run(main.static_page(request))
        assert response.status_code == 200
        return response.body.decode("utf-8")
    return render


def config(**changes):
    return Settings(**{
        "environment": "production", "demo_mode": False,
        "stripe_connection_mode": "live_financial", "stripe_charge_mode": "separate",
        "stripe_live_checkout_enabled": True, "stripe_live_payouts_enabled": True,
        **changes,
    })


@pytest.mark.parametrize("mode,checkout,payouts,expected_checkout,expected_payouts", [
    ("live_financial", True, True, True, True),
    ("live_financial", True, False, True, False),
    ("live_financial", False, True, False, True),
    ("live_financial", False, False, False, False),
    ("live_setup_only", True, True, False, False),
    ("live_read_only", True, True, False, False),
    ("sandbox", True, True, False, False),
])
def test_public_copy_uses_each_actual_gate_independently(
    render_page, mode, checkout, payouts, expected_checkout, expected_payouts,
):
    settings = config(stripe_connection_mode=mode, stripe_live_checkout_enabled=checkout,
                      stripe_live_payouts_enabled=payouts)
    html = render_page(settings)
    assert ("現在は公開準備中で、本番の請求は行いません。" in html) is not expected_checkout
    assert ("振込申請の受付・本番の銀行振込は行いません。" in html) is not expected_payouts
    assert ("申請した週の翌週木曜日が処理予定日です。" in html) is expected_payouts
    assert ("未申請売上の自動振込は行いません。" in html) is expected_payouts
    assert "120日" not in html and "正式公開前に確定・掲載します" not in html
    assert settings.payout_auto_days == 120  # The unapproved policy/config is not changed.
    assert "月額契約は受け付けません。" in html


def test_production_label_does_not_enable_either_financial_disclosure(render_page):
    html = render_page(config(stripe_connection_mode="live_setup_only",
                              stripe_live_checkout_enabled=False, stripe_live_payouts_enabled=False))
    assert "本番の請求は行いません。" in html
    assert "本番の銀行振込は行いません。" in html


def test_demo_never_displays_live_financial_rules(render_page):
    html = render_page(config(environment="development", demo_mode=True))
    assert "現在は試作デモです" in html
    assert "本番の請求は行いません。" in html
    assert "本番の銀行振込は行いません。" in html
    assert "翌週木曜日が処理予定日です。" not in html


def test_unverified_checkout_configuration_retains_waiting_text(render_page):
    html = render_page(config(), connection_verified=False)
    assert "本番の請求は行いません。" in html


def test_live_payout_disclosure_uses_configured_fees_without_promising_arrival(render_page):
    html = render_page(config(payout_minimum=500, payout_fee=30, payout_fee_free_threshold=5000))
    assert "確定した売上は500円以上から振込を申請できます。" in html
    assert "5000円未満の振込手数料は30円、同額以上は無料です。" in html
    assert "銀行休業日・審査・残高確定により入金日は前後します。" in html
    assert "取引未完了、返金・紛争・運営保留中の売上は振込対象外です。" in html
    assert "運営が確認したうえで振込を実行します。" in html


def section(html, heading):
    return re.search(r"<h2>" + re.escape(heading) + r"</h2>(.*?)(?=<h2>|<p class=\"updated\")",
                     html, re.DOTALL).group(1).strip()


@pytest.mark.parametrize("page,heading,date", [
    ("tokushoho", "返品・キャンセル", "2026年11月3日"),
    ("terms", "4. キャンセル・返金・紛争", "2026年10月25日"),
])
def test_existing_refund_terms_do_not_change_with_activation(render_page, page, heading, date):
    live = config()
    preparation = replace(live, stripe_connection_mode="live_setup_only",
                          stripe_live_checkout_enabled=False, stripe_live_payouts_enabled=False)
    before, after = [render_page(value, page) for value in (preparation, live)]
    assert section(before, heading) == section(after, heading)
    assert "本規定は、法令上認められる購入者の権利を制限するものではありません。" in after
    assert "購入者都合による返品・返金は、原則として受け付けません。" in after
    assert "最終更新日：" + date in after


def test_privacy_page_keeps_its_own_update_date(render_page):
    html = render_page(config(), "privacy")
    assert "最終更新日：2026年7月22日" in html
    assert "2026年11月3日" not in html
