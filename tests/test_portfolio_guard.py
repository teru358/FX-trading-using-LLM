"""portfolio_guard.check_max_positions_per_pair() / check_drawdown_kill_switch() のテスト。"""
from __future__ import annotations

from datetime import datetime, timezone

from src.persistence.balance_snapshot import BalanceSnapshot
from src.trading.portfolio_guard import (
    check_drawdown_kill_switch,
    check_max_positions_per_pair,
)
from src.trading.position_manager import Order


def _snap(balance: float, peak_balance: float, deposit: float = 10_000.0) -> BalanceSnapshot:
    """テスト用 BalanceSnapshot ファクトリ。"""
    return BalanceSnapshot(
        balance=balance,
        deposit=deposit,
        peak_balance=peak_balance,
        source="paper",
        fetched_at=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    )


def _make_pair_order(pair: str, direction: str = "buy"):
    return Order.new(
        pair=pair, direction=direction, entry_price=160.0,
        stop_loss=159.5, take_profit=161.0, position_size=1000.0,
    )


def test_check_max_positions_per_pair_blocks_when_at_limit():
    open_pos = [_make_pair_order("USDJPY=X"), _make_pair_order("USDJPY=X")]
    rejection = check_max_positions_per_pair(
        "USDJPY=X", open_pos, max_positions_per_pair=2,
    )
    assert rejection is not None
    assert "USDJPY=X" in rejection
    assert "2" in rejection


def test_check_max_positions_per_pair_allows_when_below_limit():
    open_pos = [_make_pair_order("USDJPY=X")]
    rejection = check_max_positions_per_pair(
        "USDJPY=X", open_pos, max_positions_per_pair=2,
    )
    assert rejection is None


def test_check_max_positions_per_pair_counts_only_target_pair():
    open_pos = [_make_pair_order("EURUSD=X"), _make_pair_order("EURUSD=X")]
    rejection = check_max_positions_per_pair(
        "USDJPY=X", open_pos, max_positions_per_pair=2,
    )
    assert rejection is None


# ---------------------------------------------------------------------------
# Drawdown kill switch (Task 5: BalanceSnapshot ベース、lookback_days 廃止)
# ---------------------------------------------------------------------------


def test_kill_switch_disabled_returns_none():
    """enabled=False なら違反状態でも None を返す。"""
    snap = _snap(balance=5_000.0, peak_balance=10_000.0)  # 50% DD
    result = check_drawdown_kill_switch(
        snap, enabled=False, max_drawdown_pct=0.10,
    )
    assert result is None


def test_kill_switch_max_pct_zero_returns_none():
    """max_drawdown_pct <= 0 なら違反状態でも None を返す (kill switch 無効)。"""
    snap = _snap(balance=5_000.0, peak_balance=10_000.0)  # 50% DD
    result = check_drawdown_kill_switch(
        snap, enabled=True, max_drawdown_pct=0.0,
    )
    assert result is None


def test_kill_switch_peak_non_positive_returns_rejection():
    """peak_balance <= 0 なら 'non-positive' で拒否される。"""
    snap = _snap(balance=0.0, peak_balance=0.0)
    result = check_drawdown_kill_switch(
        snap, enabled=True, max_drawdown_pct=0.10,
    )
    assert result is not None
    assert "non-positive" in result


def test_kill_switch_allows_when_under_threshold():
    """DD がしきい値未満ならエントリー可 (None)。"""
    # peak 10500, current 10100 → DD = 400/10500 ≈ 3.8%
    snap = _snap(balance=10_100.0, peak_balance=10_500.0)
    result = check_drawdown_kill_switch(
        snap, enabled=True, max_drawdown_pct=0.10,
    )
    assert result is None


def test_kill_switch_blocks_at_exact_threshold():
    """DD がしきい値ぴったりでも拒否される (>= 判定)。"""
    # peak 10000, current 9000 → DD = 10.0%
    snap = _snap(balance=9_000.0, peak_balance=10_000.0)
    result = check_drawdown_kill_switch(
        snap, enabled=True, max_drawdown_pct=0.10,
    )
    assert result is not None
    assert "drawdown kill switch" in result
    assert "10.0%" in result


def test_kill_switch_blocks_when_dd_exceeds_threshold():
    """peak から 10% 以上落ちたら拒否される。"""
    # peak 12000, current 10500 → DD = 1500/12000 = 12.5%
    snap = _snap(balance=10_500.0, peak_balance=12_000.0)
    result = check_drawdown_kill_switch(
        snap, enabled=True, max_drawdown_pct=0.10,
    )
    assert result is not None
    assert "drawdown kill switch" in result
    assert "12.5%" in result


def test_kill_switch_message_includes_peak_and_current():
    """拒否メッセージに peak と current 値が含まれる。"""
    snap = _snap(balance=8_500.0, peak_balance=10_000.0)  # DD = 15%
    result = check_drawdown_kill_switch(
        snap, enabled=True, max_drawdown_pct=0.10,
    )
    assert result is not None
    assert "peak=10000" in result
    assert "current=8500" in result
    assert "15.0%" in result
