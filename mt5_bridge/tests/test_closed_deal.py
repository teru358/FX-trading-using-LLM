from __future__ import annotations

from types import SimpleNamespace

import pytest

from mt5_client import Mt5Client


def test_get_closed_deal_uses_out_deals_weighted_price_and_total_pnl():
    client = Mt5Client.__new__(Mt5Client)

    class _FakeMt5:
        DEAL_ENTRY_IN = 0
        DEAL_ENTRY_OUT = 1
        DEAL_ENTRY_OUT_BY = 3

        def history_deals_get(self, position):
            assert position == 111
            return [
                SimpleNamespace(
                    entry=self.DEAL_ENTRY_IN,
                    volume=0.01,
                    price=159.0,
                    profit=0.0,
                    swap=0.0,
                    commission=0.0,
                    time=1_700_000_000,
                    reason=0,
                ),
                SimpleNamespace(
                    entry=self.DEAL_ENTRY_OUT,
                    volume=0.006,
                    price=158.50,
                    profit=-300.0,
                    swap=-1.0,
                    commission=-0.5,
                    time=1_700_000_100,
                    reason=4,
                ),
                SimpleNamespace(
                    entry=self.DEAL_ENTRY_OUT,
                    volume=0.004,
                    price=158.40,
                    profit=-200.0,
                    swap=0.0,
                    commission=-0.5,
                    time=1_700_000_120,
                    reason=4,
                ),
            ]

    client._mt5 = _FakeMt5()

    deal = client.get_closed_deal(111)

    assert deal is not None
    assert deal.ticket == 111
    assert deal.close_price == pytest.approx(158.46)
    assert deal.profit == pytest.approx(-500.0)
    assert deal.swap == pytest.approx(-1.0)
    assert deal.commission == pytest.approx(-1.0)
    assert deal.closed_at.startswith("2023-11-14T22:15:20")


def test_get_closed_deal_returns_none_when_no_out_deal():
    client = Mt5Client.__new__(Mt5Client)

    class _FakeMt5:
        DEAL_ENTRY_IN = 0
        DEAL_ENTRY_OUT = 1
        DEAL_ENTRY_OUT_BY = 3

        def history_deals_get(self, position):
            return [
                SimpleNamespace(
                    entry=self.DEAL_ENTRY_IN,
                    volume=0.01,
                    price=159.0,
                    profit=0.0,
                    swap=0.0,
                    commission=0.0,
                    time=1_700_000_000,
                    reason=0,
                ),
            ]

    client._mt5 = _FakeMt5()

    assert client.get_closed_deal(111) is None
