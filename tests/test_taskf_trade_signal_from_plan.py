from datetime import datetime
from types import SimpleNamespace

from src.config.schema import AppConfig, InstrumentConfig, TradingConfig
from src.orchestrator.runtime import _trade_signal_from_plan
from src.signals.signal_combiner import TradeSignal, _calculate_position_size


def _plan():
    return SimpleNamespace(
        plan_id=1, pair="USDJPY=X", direction="long",
        action_json={"sl": 149.0, "tp": 152.0, "rr": 2.0, "confidence": 0.7},
    )


def _quote():
    return SimpleNamespace(mid=150.0, bid=150.0, ask=150.02, spread=0.02,
                           source="mt5", observed_at=datetime(2026, 6, 27, 12, 0, 0))


def _config():
    return AppConfig(
        trading=TradingConfig(risk_per_trade=0.02, min_lot_size=1000.0, lot_unit=1000.0),
        instruments=[
            InstrumentConfig(symbol="USDJPY=X", display_name="USD/JPY", pip_value=0.01),
        ],
    )


def test_builds_valid_tradesignal_with_all_fields():
    sig = _trade_signal_from_plan(_plan(), "USDJPY=X", _quote(), _config(), balance=50000.0)
    assert isinstance(sig, TradeSignal)        # 全必須フィールドが揃い construct 成功
    assert sig.pair == "USDJPY=X"
    assert sig.action == "buy"                 # long → buy
    assert sig.stop_loss == 149.0
    assert sig.take_profit == 152.0
    assert sig.confidence == 0.7
    assert sig.entry_price == 150.0            # quote.mid
    # broker が comment に使う signal_reason が空でない
    assert sig.signal_reason
    # provenance フィールドも有効値 (news/price は dataclass インスタンス)
    assert sig.news is not None and sig.price is not None


def test_short_direction_maps_to_sell():
    plan = _plan()
    plan.direction = "short"
    sig = _trade_signal_from_plan(plan, "USDJPY=X", _quote(), _config(), balance=50000.0)
    assert sig.action == "sell"


def test_position_size_uses_risk_based_calc_not_fixed_1000():
    """codex High: position_size は固定 1000 でなく、_calculate_position_size による
    risk ベース算出 (balance × risk_per_trade × pip_value / SL距離, min_lot/unit 丸め) になる。"""
    # min_lot 床に張り付かない領域を選ぶ: pip_value=0.01, SL距離=0.01 (=pip_value、
    # fallback 分岐に入らない最小)、大きめ balance で lot が risk に比例して増える。
    cfg = _config()  # pip_value=0.01
    plan = SimpleNamespace(
        plan_id=1, pair="USDJPY=X", direction="long",
        action_json={"sl": 149.98, "tp": 152.0, "rr": 2.0, "confidence": 0.7},
    )
    sig = _trade_signal_from_plan(plan, "USDJPY=X", _quote(), cfg, balance=200000.0)
    assert sig.position_size and sig.position_size > 0
    # 既存 _calculate_position_size と同じ値になること (同一引数で直接呼んで突き合わせ)。
    expected = _calculate_position_size(
        balance=200000.0, risk_pct=0.02, entry=150.0, stop_loss=149.98,
        pip_value=0.01, min_lot_size=1000.0, lot_unit=1000.0,
    )
    assert sig.position_size == expected
    assert sig.position_size > 1000.0  # min_lot 床ではない (risk ベースが効いている)
    # balance を変えると position_size が変わる (固定 1000 でない証拠)。
    sig2 = _trade_signal_from_plan(plan, "USDJPY=X", _quote(), cfg, balance=400000.0)
    assert sig2.position_size != sig.position_size
