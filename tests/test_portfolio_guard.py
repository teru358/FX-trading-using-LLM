"""portfolio_guard.check_portfolio_limits() / check_drawdown_kill_switch() のテスト。"""
from __future__ import annotations

from datetime import datetime, timedelta

from src.trading.portfolio_guard import (
    check_drawdown_kill_switch,
    check_portfolio_limits,
)
from src.trading.position_manager import Order


def _pos(pair: str, direction: str = "buy") -> Order:
    return Order.new(
        pair=pair,
        direction=direction,
        entry_price=150.0,
        stop_loss=149.0,
        take_profit=152.0,
        position_size=10000.0,
    )


def _closed(pnl: float, closed_at: datetime) -> Order:
    """クローズ済み Order を生成 (kill switch テスト用)。"""
    order = Order.new(
        pair="USDJPY=X", direction="buy",
        entry_price=150.0, stop_loss=149.0, take_profit=152.0,
        position_size=10000.0,
    )
    order.status = "closed"
    order.closed_at = closed_at
    order.close_price = 150.0
    order.close_reason = "take_profit" if pnl > 0 else "stop_loss"
    order.realized_pnl = pnl
    return order


def test_allows_when_no_positions():
    result = check_portfolio_limits(
        pair="USDJPY=X", direction="buy", position_size=10000,
        open_positions=[],
    )
    assert result is None


def test_blocks_when_max_total_reached():
    positions = [_pos("USDJPY=X"), _pos("EURUSD=X"), _pos("GBPUSD=X"), _pos("AUDUSD=X")]
    result = check_portfolio_limits(
        pair="USDCHF=X", direction="buy", position_size=10000,
        open_positions=positions, max_total_positions=4,
    )
    assert result is not None
    assert "Max total positions" in result


def test_blocks_when_currency_group_full():
    positions = [_pos("USDJPY=X"), _pos("EURJPY=X")]
    result = check_portfolio_limits(
        pair="GBPJPY=X", direction="buy", position_size=10000,
        open_positions=positions, max_positions_per_group=2,
    )
    assert result is not None
    assert "JPY group" in result


def test_allows_different_group():
    """JPYグループが埋まっていても、別グループのペアは開ける。"""
    positions = [_pos("USDJPY=X"), _pos("EURJPY=X")]
    result = check_portfolio_limits(
        pair="AUDUSD=X", direction="buy", position_size=10000,
        open_positions=positions, max_positions_per_group=2,
    )
    # AUDUSD is in USD group, not JPY-only
    # USD group has USDJPY (1 position) → still under limit
    assert result is None


def test_blocks_same_direction_in_group():
    positions = [_pos("USDJPY=X", "buy"), _pos("EURJPY=X", "buy")]
    result = check_portfolio_limits(
        pair="GBPJPY=X", direction="buy", position_size=10000,
        open_positions=positions,
        max_positions_per_group=3,  # enough room by count
        max_same_direction_per_group=2,
    )
    assert result is not None
    assert "buy positions" in result


def test_allows_opposite_direction_in_group():
    """グループ内に2つbuyがあっても、sellなら同方向制限に引っかからない。"""
    positions = [_pos("USDJPY=X", "buy"), _pos("EURJPY=X", "buy")]
    result = check_portfolio_limits(
        pair="GBPJPY=X", direction="sell", position_size=10000,
        open_positions=positions,
        max_positions_per_group=3,
        max_same_direction_per_group=2,
    )
    assert result is None


# ---------------------------------------------------------------------------
# Drawdown kill switch
# ---------------------------------------------------------------------------


def test_kill_switch_disabled_returns_none():
    """enabled=False なら違反状態でも None を返す。"""
    now = datetime(2026, 4, 15, 12, 0)
    trades = [_closed(-5000, now - timedelta(hours=1))]  # 50% DD
    result = check_drawdown_kill_switch(
        initial_balance=10_000,
        closed_trades=trades,
        enabled=False,
        max_drawdown_pct=0.10,
        now=now,
    )
    assert result is None


def test_kill_switch_allows_when_under_threshold():
    """DD がしきい値未満ならエントリー可 (None)。"""
    now = datetime(2026, 4, 15, 12, 0)
    trades = [
        _closed(+500, now - timedelta(hours=4)),   # balance 10500, peak 10500
        _closed(-400, now - timedelta(hours=2)),   # balance 10100, DD = (10500-10100)/10500 ≈ 3.8%
    ]
    result = check_drawdown_kill_switch(
        initial_balance=10_000,
        closed_trades=trades,
        enabled=True,
        max_drawdown_pct=0.10,
        now=now,
    )
    assert result is None


def test_kill_switch_blocks_when_dd_exceeds_threshold():
    """peak から 10% 以上落ちたら拒否される。"""
    now = datetime(2026, 4, 15, 12, 0)
    trades = [
        _closed(+2000, now - timedelta(hours=6)),   # balance 12000, peak 12000
        _closed(-1500, now - timedelta(hours=2)),   # balance 10500, DD = 12.5% > 10%
    ]
    result = check_drawdown_kill_switch(
        initial_balance=10_000,
        closed_trades=trades,
        enabled=True,
        max_drawdown_pct=0.10,
        now=now,
    )
    assert result is not None
    assert "drawdown kill switch" in result
    assert "12.5%" in result


def test_kill_switch_uses_initial_balance_as_initial_peak():
    """利益が出ていない状態で損失が出たら initial_balance を peak として計算する。"""
    now = datetime(2026, 4, 15, 12, 0)
    trades = [
        _closed(-1500, now - timedelta(hours=2)),  # 10000 → 8500, DD = 15%
    ]
    result = check_drawdown_kill_switch(
        initial_balance=10_000,
        closed_trades=trades,
        enabled=True,
        max_drawdown_pct=0.10,
        now=now,
    )
    assert result is not None
    assert "15.0%" in result


def test_kill_switch_lookback_scopes_trades():
    """lookback_days 指定で古いトレードが peak 計算から除外される。"""
    now = datetime(2026, 4, 15, 12, 0)
    trades = [
        # 古い大損 (lookback 外になるので peak 計算から除外される)
        _closed(-3000, now - timedelta(days=30)),
        # 直近の小さな利益と損失
        _closed(+200, now - timedelta(days=2)),    # running +200 → peak +200
        _closed(-100, now - timedelta(days=1)),    # running +100, DD = 100/(10000+200) ≈ 1%
    ]
    # lookback=0 (全期間): 古い大損で大きく凹み、peak=10000 → current=7100 → 29% DD
    result_all = check_drawdown_kill_switch(
        initial_balance=10_000, closed_trades=trades,
        enabled=True, max_drawdown_pct=0.10, lookback_days=0, now=now,
    )
    assert result_all is not None

    # lookback=7d: 古い大損は除外 → small DD → OK
    result_recent = check_drawdown_kill_switch(
        initial_balance=10_000, closed_trades=trades,
        enabled=True, max_drawdown_pct=0.10, lookback_days=7, now=now,
    )
    assert result_recent is None


def test_kill_switch_recovery_resets_peak():
    """peak 更新後に DD が縮小すれば再び通過する。"""
    now = datetime(2026, 4, 15, 12, 0)
    trades = [
        _closed(+2000, now - timedelta(hours=10)),  # 12000, peak 12000
        _closed(-1500, now - timedelta(hours=6)),   # 10500, DD 12.5% — 本来ブロック
        _closed(+2500, now - timedelta(hours=2)),   # 13000, peak 13000, DD 0%
    ]
    result = check_drawdown_kill_switch(
        initial_balance=10_000, closed_trades=trades,
        enabled=True, max_drawdown_pct=0.10, now=now,
    )
    assert result is None
