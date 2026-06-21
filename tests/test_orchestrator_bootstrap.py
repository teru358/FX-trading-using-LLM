"""orchestrator bootstrap (Phase 6 Task 6.1/6.3) の結線・enable gate テスト。

build_orchestrator_runtime が:
  - enabled=false なら None (既存 trading cycle に影響なし)
  - enabled=true なら全部品を結線した OrchestratorRuntime を返す
  - pairs は tradeable instruments のみ (watch 除外)
  - broker adapter を渡さない (shadow 境界)
  - quote provider が CurrentPrice → QuoteSnapshot に変換する
を検証する。実 DB / 実 LLM 接続は monkeypatch で避ける。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.config.schema import AppConfig, InstrumentConfig
from src.orchestrator import bootstrap as bs
from src.orchestrator.runtime import OrchestratorRuntime


class _FakeCurrentPrice:
    def __init__(self, price: float, ts: datetime | None, source: str = "test") -> None:
        self.price = price
        self.timestamp = ts
        self.source = source


class _FakePriceProvider:
    def __init__(self, price: float = 150.0, ts: datetime | None = None) -> None:
        self._price = price
        self._ts = ts

    def get_current_price(self, symbol: str, is_monitor: bool = False):
        return _FakeCurrentPrice(self._price, self._ts)


def _config(*, enabled: bool, tmp_path: Path) -> AppConfig:
    trade = InstrumentConfig(
        symbol="USDJPY=X", display_name="USD/JPY", asset_type="fx",
        mode="trade", base_currency="USD", quote_currency="JPY",
    )
    watch = InstrumentConfig(
        symbol="EURUSD=X", display_name="EUR/USD", asset_type="fx",
        mode="watch", base_currency="EUR", quote_currency="USD",
    )
    cfg = AppConfig(instruments=[trade, watch])
    cfg.orchestrator.enabled = enabled
    return cfg


def _patch_heavy(monkeypatch, tmp_path: Path) -> None:
    """OrchestratorStore / LLM 生成を軽量 fake に差し替える (実 DB / 接続を避ける)。"""
    from src.data.orchestrator_store import OrchestratorStore

    # OrchestratorStore は temp DB に向ける (実 data/prices.db を汚さない)。
    real_init = OrchestratorStore.__init__

    def fake_init(self, db_path):
        real_init(self, tmp_path / "orch.db")

    monkeypatch.setattr(OrchestratorStore, "__init__", fake_init)

    # LLM クライアント生成は接続を張らないが、provider 設定差異を避けるため stub。
    monkeypatch.setattr(
        "src.llm.factory.create_llm_client", lambda config, role: object()
    )
    # news provider は RAG/LLM スタックを引くので stub (store 不要化)。
    monkeypatch.setattr(
        bs, "make_news_provider", lambda config, store: (lambda pair: {})
    )


class _FakeAnalysisStore:
    def get_recent_ok_snapshots(self, symbol, *a, **k):
        return []


def test_disabled_returns_none(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(enabled=False, tmp_path=tmp_path)
    rt = bs.build_orchestrator_runtime(
        cfg, store=None, price_store=None, analysis_store=None,
        price_provider=_FakePriceProvider(),
    )
    assert rt is None


def test_enabled_builds_runtime_with_tradeable_pairs_only(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_heavy(monkeypatch, tmp_path)
    cfg = _config(enabled=True, tmp_path=tmp_path)

    rt = bs.build_orchestrator_runtime(
        cfg, store=object(), price_store=object(),
        analysis_store=_FakeAnalysisStore(),
        price_provider=_FakePriceProvider(),
    )
    assert isinstance(rt, OrchestratorRuntime)
    # tradeable のみ (watch EURUSD=X は除外)
    assert rt._pairs == ["USDJPY=X"]
    # 全部品が結線されている
    assert rt._pipeline is not None
    assert rt._detector is not None
    assert rt._hindsight is not None
    assert rt._notifier is not None


def test_enabled_respects_configured_pairs_subset(tmp_path: Path, monkeypatch) -> None:
    """orch.pairs が非空なら tradeable との intersection に絞る (Codex Medium)。"""
    _patch_heavy(monkeypatch, tmp_path)
    trade1 = InstrumentConfig(symbol="USDJPY=X", display_name="USD/JPY", asset_type="fx",
                              mode="trade", base_currency="USD", quote_currency="JPY")
    trade2 = InstrumentConfig(symbol="GBPUSD=X", display_name="GBP/USD", asset_type="fx",
                              mode="trade", base_currency="GBP", quote_currency="USD")
    cfg = AppConfig(instruments=[trade1, trade2])
    cfg.orchestrator.enabled = True
    cfg.orchestrator.pairs = ["USDJPY=X"]  # 片方だけに絞る
    rt = bs.build_orchestrator_runtime(
        cfg, store=object(), price_store=object(),
        analysis_store=_FakeAnalysisStore(), price_provider=_FakePriceProvider(),
    )
    assert rt._pairs == ["USDJPY=X"]


def test_enabled_configured_pairs_excludes_watch_and_unknown(
    tmp_path: Path, monkeypatch
) -> None:
    """orch.pairs に watch / 未知 symbol が混ざっても tradeable のみ採用する。"""
    _patch_heavy(monkeypatch, tmp_path)
    cfg = _config(enabled=True, tmp_path=tmp_path)  # USDJPY=X(trade), EURUSD=X(watch)
    cfg.orchestrator.pairs = ["USDJPY=X", "EURUSD=X", "XAUUSD=X"]  # watch + 未知 混在
    rt = bs.build_orchestrator_runtime(
        cfg, store=object(), price_store=object(),
        analysis_store=_FakeAnalysisStore(), price_provider=_FakePriceProvider(),
    )
    assert rt._pairs == ["USDJPY=X"]


def test_enabled_configured_pairs_none_tradeable_returns_none(
    tmp_path: Path, monkeypatch
) -> None:
    """orch.pairs が tradeable と全く交差しないなら None (起動しない)。"""
    _patch_heavy(monkeypatch, tmp_path)
    cfg = _config(enabled=True, tmp_path=tmp_path)
    cfg.orchestrator.pairs = ["EURUSD=X"]  # watch のみ
    rt = bs.build_orchestrator_runtime(
        cfg, store=object(), price_store=object(),
        analysis_store=_FakeAnalysisStore(), price_provider=_FakePriceProvider(),
    )
    assert rt is None


def test_enabled_no_broker_adapter(tmp_path: Path, monkeypatch) -> None:
    """shadow 境界: runtime に broker / 発注 path が無い。"""
    _patch_heavy(monkeypatch, tmp_path)
    cfg = _config(enabled=True, tmp_path=tmp_path)

    rt = bs.build_orchestrator_runtime(
        cfg, store=object(), price_store=object(),
        analysis_store=_FakeAnalysisStore(),
        price_provider=_FakePriceProvider(),
    )
    # runtime は broker 属性を一切持たない (発注経路なし)。
    assert not hasattr(rt, "_broker")
    assert not hasattr(rt, "broker")


def test_enabled_but_no_tradeable_returns_none(tmp_path: Path, monkeypatch) -> None:
    _patch_heavy(monkeypatch, tmp_path)
    watch = InstrumentConfig(
        symbol="EURUSD=X", display_name="EUR/USD", asset_type="fx",
        mode="watch", base_currency="EUR", quote_currency="USD",
    )
    cfg = AppConfig(instruments=[watch])
    cfg.orchestrator.enabled = True
    rt = bs.build_orchestrator_runtime(
        cfg, store=object(), price_store=object(), analysis_store=object(),
        price_provider=_FakePriceProvider(),
    )
    assert rt is None


# ── quote provider ────────────────────────────────────────────


def test_quote_provider_maps_current_price() -> None:
    ts = datetime(2026, 6, 21, 12, 0, 0)
    qp = bs.make_quote_provider(_FakePriceProvider(price=150.25, ts=ts))
    q = qp("USDJPY=X")
    assert q.mid == 150.25 and q.bid == 150.25 and q.ask == 150.25
    # bid/ask が無い provider のため spread は不明 (None)。gate は安全側で扱う。
    assert q.spread is None
    assert q.observed_at == ts


def test_quote_provider_missing_timestamp_falls_back_to_now() -> None:
    """CurrentPrice.timestamp が無ければ db_now (naive) で補完する。"""
    qp = bs.make_quote_provider(_FakePriceProvider(price=1.1, ts=None))
    q = qp("EURUSD=X")
    assert isinstance(q.observed_at, datetime)
    # DB 規約 (db_now) に揃え naive を維持する。
    assert q.observed_at.tzinfo is None


# ── risk_state provider (Codex High) ──────────────────────────


def test_risk_state_provider_market_closed(tmp_path: Path, monkeypatch) -> None:
    """休場中は market_open=False を返す (planning/watch を gate で止める)。"""
    cfg = _config(enabled=True, tmp_path=tmp_path)
    monkeypatch.setattr(bs, "market_skip_check", lambda: True)
    monkeypatch.setattr(
        bs, "_read_halt",
        lambda config: (False, "none", "ok"),
    )
    rp = bs.make_risk_state_provider(cfg)
    rs = rp("USDJPY=X")
    assert rs["market_open"] is False


def test_risk_state_provider_halt_active(tmp_path: Path, monkeypatch) -> None:
    """soft halt 中は halt!=none を返す。"""
    cfg = _config(enabled=True, tmp_path=tmp_path)
    monkeypatch.setattr(bs, "market_skip_check", lambda: False)
    monkeypatch.setattr(
        bs, "_read_halt",
        lambda config: (True, "soft", "degraded"),
    )
    rp = bs.make_risk_state_provider(cfg)
    rs = rp("USDJPY=X")
    assert rs["halt"] == "soft"
    assert rs["bridge_health"] == "degraded"
    assert rs["market_open"] is True


def test_risk_state_provider_all_clear(tmp_path: Path, monkeypatch) -> None:
    """市場開・halt 無しなら楽観既定相当 (gate 通過)。"""
    cfg = _config(enabled=True, tmp_path=tmp_path)
    monkeypatch.setattr(bs, "market_skip_check", lambda: False)
    monkeypatch.setattr(bs, "_read_halt", lambda config: (False, "none", "ok"))
    rp = bs.make_risk_state_provider(cfg)
    rs = rp("USDJPY=X")
    assert rs == {
        "halt": "none", "bridge_health": "ok", "market_open": True, "cooldown": False,
    }
