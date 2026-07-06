"""make_position_provider: raw position dict list を返す (整形は builder 側)。"""
from types import SimpleNamespace

from src.orchestrator.bootstrap import make_position_provider


def _config_with_state(tmp_path):
    # make_position_provider は config.state_dir しか読まないので fake で足りる
    return SimpleNamespace(state_dir=tmp_path / "state")


def _open_position(config, **overrides):
    from src.persistence.state_store import StateStore
    from src.trading.position_manager import Order, PositionManager

    kwargs = dict(
        pair="USDJPY=X", direction="buy", entry_price=150.0,
        stop_loss=149.0, take_profit=152.0, position_size=10000,
        signal_reason="original breakout",
    )
    kwargs.update(overrides)
    mgr = PositionManager(StateStore(config.state_dir), context="test")
    mgr.open_position(Order.new(**kwargs))
    return mgr


def test_provider_returns_empty_for_no_positions(tmp_path):
    config = _config_with_state(tmp_path)
    provider = make_position_provider(config)
    assert provider("USDJPY=X") == []


def test_provider_returns_raw_dicts_filtered_by_pair(tmp_path):
    config = _config_with_state(tmp_path)
    _open_position(config)

    provider = make_position_provider(config)
    items = provider("USDJPY=X")
    assert len(items) == 1
    assert items[0]["direction"] == "buy"          # raw のまま (long 変換は builder)
    assert items[0]["entry_price"] == 150.0
    assert items[0]["entry_reason"] == "original breakout"
    assert "initial_risk_price_distance" in items[0]
    assert provider("EURUSD=X") == []


def test_provider_reloads_per_call(tmp_path):
    """provider 構築後に別インスタンスが open した建玉も見える (per-call reload)。"""
    config = _config_with_state(tmp_path)
    provider = make_position_provider(config)
    assert provider("USDJPY=X") == []

    # provider 構築後に、別の PositionManager インスタンス経由で open する
    _open_position(config, signal_reason="late entry")

    items = provider("USDJPY=X")
    assert len(items) == 1
    assert items[0]["entry_reason"] == "late entry"
