"""position_manager.PositionManager のテスト。"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.trading.position_manager import Order, PositionManager


# ── open / close (BUY) ───────────────────────────────────────


def test_open_position(tmp_state_store, buy_order):
    """open_position 後にポジションが open_positions に追加される。"""
    pm = PositionManager(tmp_state_store, context="Test")
    pm.open_position(buy_order)
    account = pm.get_account_state()
    assert len(account.open_positions) == 1
    assert account.open_positions[0].pair == "USDJPY=X"
    assert account.open_positions[0].direction == "buy"


def test_close_position_profit(tmp_state_store, buy_order):
    """利益決済: close_price > entry_price → realized_pnl > 0、balance が増加する。"""
    pm = PositionManager(tmp_state_store, context="Test")
    pm.open_position(buy_order)

    close_price = 151.0  # entry=150.0 → +1.0 × 10000 = +10000
    closed = pm.close_position(buy_order.order_id, close_price, "take_profit")

    assert closed is not None
    assert closed.realized_pnl == pytest.approx(10_000.0)
    account = pm.get_account_state()
    assert account.balance == pytest.approx(110_000.0)
    assert len(account.open_positions) == 0
    assert len(account.closed_trades) == 1


def test_close_position_loss(tmp_state_store, buy_order):
    """損失決済: close_price < entry_price → realized_pnl < 0、balance が減少する。"""
    pm = PositionManager(tmp_state_store, context="Test")
    pm.open_position(buy_order)

    close_price = 149.0  # entry=150.0 → -1.0 × 10000 = -10000
    closed = pm.close_position(buy_order.order_id, close_price, "stop_loss")

    assert closed is not None
    assert closed.realized_pnl == pytest.approx(-10_000.0)
    account = pm.get_account_state()
    assert account.balance == pytest.approx(90_000.0)


def test_close_nonexistent_position(tmp_state_store):
    """存在しない order_id を close しようとすると None が返る。"""
    pm = PositionManager(tmp_state_store, context="Test")
    result = pm.close_position("nonexistent-id", 150.0, "manual")
    assert result is None


# ── close (SELL) — 方向乗算の確認 ───────────────────────────


def test_close_position_sell_profit(tmp_state_store, sell_order):
    """SELL 利益決済: close_price < entry_price → realized_pnl > 0。"""
    pm = PositionManager(tmp_state_store, context="Test")
    pm.open_position(sell_order)

    close_price = 149.0  # entry=150.0 → SELL なので (150-149) × 10000 = +10000
    closed = pm.close_position(sell_order.order_id, close_price, "take_profit")

    assert closed is not None
    assert closed.realized_pnl == pytest.approx(10_000.0)
    assert pm.get_account_state().balance == pytest.approx(110_000.0)


def test_close_position_sell_loss(tmp_state_store, sell_order):
    """SELL 損失決済: close_price > entry_price → realized_pnl < 0。"""
    pm = PositionManager(tmp_state_store, context="Test")
    pm.open_position(sell_order)

    close_price = 151.0
    closed = pm.close_position(sell_order.order_id, close_price, "stop_loss")

    assert closed is not None
    assert closed.realized_pnl == pytest.approx(-10_000.0)
    assert pm.get_account_state().balance == pytest.approx(90_000.0)


# ── get_account_state / get_open_position ──────────────────


def test_get_account_state(tmp_state_store, buy_order, sell_order):
    """複数ポジションを追加後、get_account_state が正しい値を返す。"""
    pm = PositionManager(tmp_state_store, context="Test")
    pm.open_position(buy_order)
    pm.open_position(sell_order)

    account = pm.get_account_state()
    assert account.balance == pytest.approx(100_000.0)
    assert account.initial_balance == pytest.approx(100_000.0)
    assert account.peak_balance == pytest.approx(100_000.0)
    assert len(account.open_positions) == 2
    assert account.total_trades == 0
    directions = {p.direction for p in account.open_positions}
    assert directions == {"buy", "sell"}


def test_get_account_state_includes_peak_balance(tmp_path):
    """get_account_state は balance.json の peak_balance を返す (DD 計算用)。"""
    from datetime import datetime, timezone

    from src.persistence.balance_snapshot import BalanceSnapshot
    from src.persistence.balance_snapshot import write as write_balance
    from src.persistence.state_store import StateStore

    store = StateStore(tmp_path)
    write_balance(
        store.state_dir,
        BalanceSnapshot(
            balance=95_000.0,
            deposit=100_000.0,
            peak_balance=110_000.0,
            source="paper",
            fetched_at=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        ),
    )
    pm = PositionManager(store, context="Test")
    account = pm.get_account_state()
    assert account.balance == pytest.approx(95_000.0)
    assert account.initial_balance == pytest.approx(100_000.0)
    assert account.peak_balance == pytest.approx(110_000.0)


def test_get_open_position_returns_matching(tmp_state_store, buy_order):
    pm = PositionManager(tmp_state_store, context="Test")
    pm.open_position(buy_order)
    found = pm.get_open_position("USDJPY=X")
    assert found is not None
    assert found.order_id == buy_order.order_id


def test_get_open_position_none_when_not_open(tmp_state_store):
    pm = PositionManager(tmp_state_store, context="Test")
    assert pm.get_open_position("USDJPY=X") is None


# ── update_stop_loss (BUY) ─────────────────────────────────


def test_update_stop_loss_buy_success(tmp_state_store, buy_order):
    """BUY: 新 SL が既存 SL より高ければ更新成功。"""
    pm = PositionManager(tmp_state_store, context="Test")
    pm.open_position(buy_order)  # SL=149.0

    assert pm.update_stop_loss(buy_order.order_id, 149.5) is True
    pos = pm.get_open_position("USDJPY=X")
    assert pos.stop_loss == pytest.approx(149.5)


def test_update_stop_loss_buy_rejects_equal_or_lower(tmp_state_store, buy_order):
    """BUY: 新 SL が既存 SL 以下 (同値含む) なら False で拒否。"""
    pm = PositionManager(tmp_state_store, context="Test")
    pm.open_position(buy_order)  # SL=149.0

    assert pm.update_stop_loss(buy_order.order_id, 149.0) is False  # 同値
    assert pm.update_stop_loss(buy_order.order_id, 148.5) is False  # 下方向
    pos = pm.get_open_position("USDJPY=X")
    assert pos.stop_loss == pytest.approx(149.0), "SL は変更されない"


# ── update_stop_loss (SELL) ────────────────────────────────


def test_update_stop_loss_sell_success(tmp_state_store, sell_order):
    """SELL: 新 SL が既存 SL より低ければ更新成功。"""
    pm = PositionManager(tmp_state_store, context="Test")
    pm.open_position(sell_order)  # SL=151.0

    assert pm.update_stop_loss(sell_order.order_id, 150.5) is True
    pos = pm.get_open_position("USDJPY=X")
    assert pos.stop_loss == pytest.approx(150.5)


def test_update_stop_loss_sell_rejects_equal_or_higher(tmp_state_store, sell_order):
    """SELL: 新 SL が既存 SL 以上 (同値含む) なら False で拒否。"""
    pm = PositionManager(tmp_state_store, context="Test")
    pm.open_position(sell_order)  # SL=151.0

    assert pm.update_stop_loss(sell_order.order_id, 151.0) is False  # 同値
    assert pm.update_stop_loss(sell_order.order_id, 151.5) is False  # 上方向
    pos = pm.get_open_position("USDJPY=X")
    assert pos.stop_loss == pytest.approx(151.0)


def test_update_stop_loss_unknown_id_returns_false(tmp_state_store):
    pm = PositionManager(tmp_state_store, context="Test")
    assert pm.update_stop_loss("nonexistent-id", 150.0) is False


# ── ステートレス性 (reload) ────────────────────────────────


def test_position_persists_across_instances(tmp_state_store, buy_order):
    """PositionManager は state_store から毎回 load するので、別インスタンスでも
    同じ state_dir を指していれば open positions / balance が再現される。"""
    pm1 = PositionManager(tmp_state_store, context="Test1")
    pm1.open_position(buy_order)

    # 同じ store で別インスタンスを作ると既存ポジションがロードされる
    pm2 = PositionManager(tmp_state_store, context="Test2")
    account = pm2.get_account_state()
    assert len(account.open_positions) == 1
    assert account.open_positions[0].order_id == buy_order.order_id


def test_closed_trades_and_balance_persist(tmp_state_store, buy_order):
    """close_position で balance / closed_trades が保存され、新インスタンスに
    引き継がれる。"""
    pm1 = PositionManager(tmp_state_store, context="Test1")
    pm1.open_position(buy_order)
    pm1.close_position(buy_order.order_id, 151.0, "take_profit")

    pm2 = PositionManager(tmp_state_store, context="Test2")
    account = pm2.get_account_state()
    assert account.balance == pytest.approx(110_000.0)
    assert len(account.open_positions) == 0
    assert len(account.closed_trades) == 1
    assert account.closed_trades[0].close_reason == "take_profit"


def test_initial_stop_loss_preserved_after_trailing(tmp_state_store):
    """Order.initial_stop_loss は Order.new で SL と同じ値にセットされ、
    その後 update_stop_loss で SL が更新されても initial_stop_loss は変わらない。
    これは trailing stop の follow ステージで「元の SL 距離」を再現するため必須。"""
    pm = PositionManager(tmp_state_store, context="Test")
    order = Order.new(
        pair="USDJPY=X",
        direction="buy",
        entry_price=100.0,
        stop_loss=99.0,       # 初期 SL
        take_profit=110.0,
        position_size=10000.0,
        signal_reason="trailing-init-test",
    )
    assert order.initial_stop_loss == pytest.approx(99.0)
    pm.open_position(order)

    # 段階的にトレーリング (half → breakeven → follow)
    assert pm.update_stop_loss(order.order_id, 99.5, stage="half") is True
    assert pm.update_stop_loss(order.order_id, 100.0, stage="breakeven") is True
    assert pm.update_stop_loss(order.order_id, 103.0, stage="follow") is True

    reloaded = PositionManager(tmp_state_store, context="Test2")
    pos = reloaded.get_open_position("USDJPY=X")
    assert pos is not None
    assert pos.stop_loss == pytest.approx(103.0), "最新 SL は反映"
    assert pos.initial_stop_loss == pytest.approx(99.0), "initial は保持されたまま"


# ── Race condition: 複数 PositionManager インスタンスの一貫性 ──────────────


def test_concurrent_mutations_sync_across_instances(tmp_state_store, buy_order, sell_order):
    """同じ state_dir を指す 2 つの PositionManager が相互の変更を取りこぼさない。

    回帰テスト: 以前は __init__ でのみ load していたため、インスタンス A が
    ポジションを close したあと、古い在メモリ state を持つ B が save すると
    A の変更が巻き戻っていた。transaction + reload で原子化されたことを検証する。
    """
    pm_a = PositionManager(tmp_state_store, context="A")
    pm_b = PositionManager(tmp_state_store, context="B")

    # A が BUY を open — B はまだ知らない (__init__ 時点の load のみ)
    pm_a.open_position(buy_order)
    assert len(pm_b.get_account_state().open_positions) == 0, "B は init 時点の古い state"

    # B が SELL を open — 内部で reload するので A の BUY を取りこぼさない
    pm_b.open_position(sell_order)

    # disk 上には 2 つのポジションが存在するはず
    fresh = PositionManager(tmp_state_store, context="fresh")
    pairs = {p.order_id for p in fresh.get_account_state().open_positions}
    assert buy_order.order_id in pairs
    assert sell_order.order_id in pairs
    assert len(pairs) == 2


def test_close_by_stale_instance_does_not_resurrect_other_positions(tmp_state_store, buy_order, sell_order):
    """B のクローズ操作が A の在メモリ state によって巻き戻らないこと。"""
    pm_a = PositionManager(tmp_state_store, context="A")
    pm_a.open_position(buy_order)
    pm_a.open_position(sell_order)

    pm_b = PositionManager(tmp_state_store, context="B")
    # A が BUY をクローズ
    pm_a.close_position(buy_order.order_id, 151.0, "take_profit")

    # B の在メモリ state は古い ([BUY, SELL]) が、close_position は reload するので
    # 「既にクローズ済み」として見つからないか、または SELL のみをクローズする
    closed_sell = pm_b.close_position(sell_order.order_id, 149.0, "manual")
    assert closed_sell is not None

    # disk 上にオープンポジションは残っていない
    fresh = PositionManager(tmp_state_store, context="fresh")
    assert len(fresh.get_account_state().open_positions) == 0
    assert len(fresh.get_account_state().closed_trades) == 2


# ── open_confidence / open_score (scale-in feature) ───────────


def test_order_new_records_open_confidence_and_score():
    order = Order.new(
        pair="USDJPY=X", direction="buy", entry_price=160.0,
        stop_loss=159.5, take_profit=161.0, position_size=1000.0,
        signal_reason="test",
        open_confidence=0.65, open_score=0.40,
    )
    assert order.open_confidence == 0.65
    assert order.open_score == 0.40


def test_order_new_defaults_open_confidence_score_to_none():
    order = Order.new(
        pair="USDJPY=X", direction="buy", entry_price=160.0,
        stop_loss=159.5, take_profit=161.0, position_size=1000.0,
    )
    assert order.open_confidence is None
    assert order.open_score is None


def test_order_from_dict_handles_missing_open_confidence_score():
    """legacy positions.json に open_conf/score が無い場合の fallback。"""
    legacy_dict = {
        "order_id": "abc-123",
        "pair": "USDJPY=X",
        "direction": "buy",
        "entry_price": 160.0,
        "stop_loss": 159.5,
        "take_profit": 161.0,
        "position_size": 1000.0,
        "initial_stop_loss": 159.5,
        "status": "open",
        "opened_at": "2026-04-29T12:00:00",
        "closed_at": None,
        "close_price": None,
        "close_reason": None,
        "realized_pnl": None,
        "signal_reason": "",
        "macro_context_at_entry": "",
        # open_confidence / open_score 不在
    }
    order = Order.from_dict(legacy_dict)
    assert order.open_confidence is None
    assert order.open_score is None


def test_order_to_dict_roundtrip_preserves_open_confidence_score():
    original = Order.new(
        pair="USDJPY=X", direction="buy", entry_price=160.0,
        stop_loss=159.5, take_profit=161.0, position_size=1000.0,
        open_confidence=0.72, open_score=0.55,
    )
    restored = Order.from_dict(original.to_dict())
    assert restored.open_confidence == 0.72
    assert restored.open_score == 0.55


def test_order_to_dict_roundtrip_preserves_is_scale_in():
    original = Order.new(
        pair="USDJPY=X", direction="buy", entry_price=160.0,
        stop_loss=159.5, take_profit=161.0, position_size=1000.0,
        open_confidence=0.78, open_score=0.55,
        is_scale_in=True,
    )
    restored = Order.from_dict(original.to_dict())
    assert restored.is_scale_in is True


# ── get_open_positions_by_pair ────────────────────────────────


def _setup_position_manager(tmp_path: Path) -> PositionManager:
    from src.persistence.state_store import StateStore
    return PositionManager(
        StateStore(tmp_path),
        context="Test",
    )


def test_get_open_positions_by_pair_returns_all_matching(tmp_path):
    mgr = _setup_position_manager(tmp_path)
    o1 = Order.new("USDJPY=X", "buy", 160.0, 159.5, 161.0, 1000.0)
    o2 = Order.new("USDJPY=X", "buy", 160.5, 160.0, 162.0, 1000.0,
                   open_confidence=0.70, open_score=0.50)
    o3 = Order.new("EURUSD=X", "sell", 1.10, 1.11, 1.09, 1000.0)
    mgr.open_position(o1)
    mgr.open_position(o2)
    mgr.open_position(o3)

    result = mgr.get_open_positions_by_pair("USDJPY=X")
    assert len(result) == 2
    assert {p.order_id for p in result} == {o1.order_id, o2.order_id}


def test_get_open_positions_by_pair_returns_empty_when_no_match(tmp_path):
    mgr = _setup_position_manager(tmp_path)
    o1 = Order.new("USDJPY=X", "buy", 160.0, 159.5, 161.0, 1000.0)
    mgr.open_position(o1)

    result = mgr.get_open_positions_by_pair("EURUSD=X")
    assert result == []


def test_get_open_positions_by_pair_excludes_closed(tmp_path):
    mgr = _setup_position_manager(tmp_path)
    o1 = Order.new("USDJPY=X", "buy", 160.0, 159.5, 161.0, 1000.0)
    mgr.open_position(o1)
    mgr.close_position(o1.order_id, 161.0, "take_profit")

    result = mgr.get_open_positions_by_pair("USDJPY=X")
    assert result == []


# ── close_position → balance.json propagation (paper_broker / paper_trader hook) ──


def test_close_position_updates_balance_json(tmp_state_store, buy_order):
    """close_position 後 balance.json が balance + peak で正しく更新されている。

    paper_broker / paper_trader は close 時に PositionManager.close_position を
    呼ぶので、ここを通すだけで balance_snapshot.mutate(...) 経由で
    balance.json が更新されるという回帰テスト。
    """
    from src.persistence import balance_snapshot

    pm = PositionManager(tmp_state_store, context="Test")
    pm.open_position(buy_order)  # entry=150.0, size=10000.0

    # +1.0 price × 10000 size = +10000 PnL
    pm.close_position(buy_order.order_id, 151.0, "test")

    snap = balance_snapshot.read(tmp_state_store.state_dir)
    assert snap.balance == pytest.approx(110_000.0)
    assert snap.peak_balance == pytest.approx(110_000.0)  # peak は max 維持
    assert snap.source == "paper"
    assert snap.deposit == pytest.approx(100_000.0)  # deposit は不変
