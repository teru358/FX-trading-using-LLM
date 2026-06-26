"""Task F (F-4): bootstrap が mode=live で execution broker/position_mgr を注入する。

shadow (既定) では未注入 (回帰)。orchestrator.mode=live のとき発注先 broker は top-level
AppConfig.mode が決める (paper/live/live_test)。live_test は bridge dry_run gate を要求する
(codex Critical)。実 broker/接続は monkeypatch で避ける。
"""
from __future__ import annotations

from pathlib import Path

import pytest

import src.orchestrator.bootstrap as bs
from src.config.schema import AppConfig, InstrumentConfig
from src.orchestrator.runtime import OrchestratorRuntime


def _patch_heavy(monkeypatch, tmp_path: Path) -> None:
    from src.data.orchestrator_store import OrchestratorStore

    real_init = OrchestratorStore.__init__

    def fake_init(self, db_path):
        real_init(self, tmp_path / "orch.db")

    monkeypatch.setattr(OrchestratorStore, "__init__", fake_init)
    monkeypatch.setattr("src.llm.factory.create_llm_client", lambda config, role: object())
    monkeypatch.setattr(bs, "make_news_provider", lambda config, store: (lambda pair: {}))


def _patch_execution_deps(monkeypatch) -> dict:
    """create_broker / PositionManager / StateStore / create_notifier を stub 化。"""
    sentinel = {"broker": object(), "pos": object()}
    monkeypatch.setattr(
        "src.trading.live_broker.create_broker", lambda *a, **k: sentinel["broker"]
    )
    monkeypatch.setattr(
        "src.trading.position_manager.PositionManager", lambda *a, **k: sentinel["pos"]
    )
    monkeypatch.setattr("src.persistence.state_store.StateStore", lambda *a, **k: object())
    monkeypatch.setattr(
        "src.notifications.notifier.create_notifier", lambda enabled: object()
    )
    return sentinel


class _FakeAnalysisStore:
    def get_recent_ok_snapshots(self, symbol, *a, **k):
        return []


class _FakeCurrentPrice:
    def __init__(self, price, ts):
        self.price, self.timestamp, self.source = price, ts, "test"


class _FakePriceProvider:
    def get_current_price(self, symbol, is_monitor=False):
        from datetime import datetime
        return _FakeCurrentPrice(150.0, datetime(2026, 6, 27, 12, 0, 0))


def _live_config(*, app_mode: str, live_broker=None) -> AppConfig:
    trade = InstrumentConfig(
        symbol="USDJPY=X", display_name="USD/JPY", asset_type="fx",
        mode="trade", base_currency="USD", quote_currency="JPY",
    )
    cfg = AppConfig(mode=app_mode, live_broker=live_broker, instruments=[trade])
    cfg.orchestrator.enabled = True
    cfg.orchestrator.mode = "live"
    return cfg


def _build(cfg):
    return bs.build_orchestrator_runtime(
        cfg, store=object(), price_store=object(),
        analysis_store=_FakeAnalysisStore(), price_provider=_FakePriceProvider(),
    )


def test_shadow_mode_no_execution_broker(tmp_path: Path, monkeypatch) -> None:
    """mode=shadow (既定): runtime に execution_broker が注入されない (回帰)。"""
    _patch_heavy(monkeypatch, tmp_path)
    trade = InstrumentConfig(symbol="USDJPY=X", display_name="USD/JPY", asset_type="fx",
                             mode="trade", base_currency="USD", quote_currency="JPY")
    cfg = AppConfig(instruments=[trade])
    cfg.orchestrator.enabled = True  # mode は既定 shadow
    rt = _build(cfg)
    assert isinstance(rt, OrchestratorRuntime)
    assert rt._execution_broker is None
    assert rt._mode == "shadow"


def test_live_mode_with_paper_injects_paper_broker(tmp_path: Path, monkeypatch) -> None:
    """段階検証: orchestrator.mode=live + AppConfig.mode=paper でも execution_broker 注入。"""
    _patch_heavy(monkeypatch, tmp_path)
    sentinel = _patch_execution_deps(monkeypatch)
    rt = _build(_live_config(app_mode="paper"))
    assert rt._mode == "live"
    assert rt._execution_broker is sentinel["broker"]
    assert rt._execution_position_mgr is sentinel["pos"]
    assert rt._app_config is not None


def test_live_mode_with_live_injects_execution_broker(tmp_path: Path, monkeypatch) -> None:
    """orchestrator.mode=live + AppConfig.mode=live + live_broker=mt5: broker 注入。"""
    _patch_heavy(monkeypatch, tmp_path)
    sentinel = _patch_execution_deps(monkeypatch)
    rt = _build(_live_config(app_mode="live", live_broker="mt5"))
    assert rt._mode == "live"
    assert rt._execution_broker is sentinel["broker"]


def test_live_test_requires_bridge_dry_run(tmp_path: Path, monkeypatch) -> None:
    """codex Critical: live_test は bridge /health dry_run==true を確認できなければ
    起動を ValueError で弾く。dry_run=true なら成功。取得失敗も ValueError (安全側)。"""
    _patch_heavy(monkeypatch, tmp_path)
    _patch_execution_deps(monkeypatch)

    class _Resp:
        def __init__(self, dry_run):
            self._dry_run = dry_run

        def raise_for_status(self):
            pass

        def json(self):
            return {"dry_run": self._dry_run}

    from src.config.schema import Mt5Config

    cfg = _live_config(app_mode="live_test", live_broker="mt5")
    # bridge_url を設定 (dry_run gate が URL を要求する)。
    cfg.providers.mt5 = Mt5Config(bridge_url="http://bridge.test")

    # dry_run=false → ValueError (実発注リスク)
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(False))
    with pytest.raises(ValueError):
        _build(cfg)

    # 取得失敗 (例外) → ValueError (安全側)
    import httpx

    def _boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr("httpx.get", _boom)
    with pytest.raises(ValueError):
        _build(cfg)

    # dry_run=true → 起動成功 (execution_broker 注入)
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(True))
    rt = _build(cfg)
    assert rt._mode == "live"
    assert rt._execution_broker is not None
