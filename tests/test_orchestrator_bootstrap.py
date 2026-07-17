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
    # position provider は実 config.state_dir (repo data/state) に PositionManager /
    # StateStore を作り、クリーン環境では balance.json 等を生成してしまうので stub。
    monkeypatch.setattr(
        bs, "make_position_provider", lambda config: (lambda pair: [])
    )


def _patch_orch_store(monkeypatch, tmp_path: Path) -> None:
    """OrchestratorStore / news provider のみ stub (LLM factory は patch しない)。"""
    from src.data.orchestrator_store import OrchestratorStore

    real_init = OrchestratorStore.__init__

    def fake_init(self, db_path):
        real_init(self, tmp_path / "orch.db")

    monkeypatch.setattr(OrchestratorStore, "__init__", fake_init)
    monkeypatch.setattr(
        bs, "make_news_provider", lambda config, store: (lambda pair: {})
    )
    monkeypatch.setattr(
        bs, "make_position_provider", lambda config: (lambda pair: [])
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


# ── producer / protection scope (Codex High FIX 1) ────────────


def test_producer_covers_all_tradeable_even_with_pairs_subset(
    tmp_path: Path, monkeypatch
) -> None:
    """producer は planning subset (orchestrator.pairs) ではなく tradeable 全体を poll する。

    protect_live で price_monitor の利益保護がペア無関係に OFF になるため、保護スコープ
    (producer + 保護 worker) は subset 外の tradeable ペアも必ずカバーしなければならない。
    """
    _patch_heavy(monkeypatch, tmp_path)
    trade1 = InstrumentConfig(symbol="USDJPY=X", display_name="USD/JPY", asset_type="fx",
                              mode="trade", base_currency="USD", quote_currency="JPY")
    trade2 = InstrumentConfig(symbol="EURUSD=X", display_name="EUR/USD", asset_type="fx",
                              mode="trade", base_currency="EUR", quote_currency="USD")
    cfg = AppConfig(instruments=[trade1, trade2])
    cfg.orchestrator.enabled = True
    cfg.orchestrator.pairs = ["USDJPY=X"]  # planning は USDJPY のみに絞る (subset)
    cfg.orchestrator.tick_migration_stage = "producer"  # producer を立てる最小 stage

    rt = bs.build_orchestrator_runtime(
        cfg, store=object(), price_store=object(),
        analysis_store=_FakeAnalysisStore(), price_provider=_FakePriceProvider(),
    )
    assert isinstance(rt, OrchestratorRuntime)
    # planning scope は subset のまま (planning 挙動は不変)
    assert rt._pairs == ["USDJPY=X"]
    # producer は tradeable 全体を poll する (保護スコープ = 全 tradeable)
    assert rt._quote_producer is not None
    assert sorted(rt._quote_producer._pairs) == ["EURUSD=X", "USDJPY=X"]


# ── position provider 注入 (planner position/plan context Task 10) ──


def test_bootstrap_injects_position_provider(tmp_path: Path, monkeypatch) -> None:
    """build_orchestrator 経由の DecisionContextBuilder に position_provider が注入される。

    実 make_position_provider は repo の data/state に触るため sentinel で差し替え、
    「bootstrap が make_position_provider の戻り値をそのまま注入する」ことを検証する。
    """
    _patch_heavy(monkeypatch, tmp_path)

    def sentinel_provider(pair):
        return []

    monkeypatch.setattr(bs, "make_position_provider", lambda config: sentinel_provider)
    cfg = _config(enabled=True, tmp_path=tmp_path)

    rt = bs.build_orchestrator_runtime(
        cfg, store=object(), price_store=object(),
        analysis_store=_FakeAnalysisStore(), price_provider=_FakePriceProvider(),
    )
    assert rt._ctx._position_provider is sentinel_provider


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


# ── per-agent LLM 配線 (2026-06-24 per-agent llm config) ──────


def test_planner_and_exec_get_separate_agent_llms(tmp_path: Path, monkeypatch) -> None:
    """agents.yaml で Planner / ExecutionOpinion に別 provider/temperature を指定すると、
    別 client + 別 temperature の AgentLlm が配線される。"""
    from src.config.schema import AgentLlmConfig

    # 実 client 生成を避けるため _build_client を stub (provider/model/temperature を運ぶ)。
    monkeypatch.setattr(
        "src.llm.factory._build_client",
        lambda provider, pc, model: ("client", provider, model),
    )
    _patch_orch_store(monkeypatch, tmp_path)

    cfg = _config(enabled=True, tmp_path=tmp_path)
    cfg.llm.provider = "claude-cli"
    cfg.llm.price_analysis.model = "price-model"
    # provider == top-level → 既存 provider_config 流用 (provider_configs 不要)
    cfg.agent_llms.planner = AgentLlmConfig(
        provider="claude-cli", model="planner-model", temperature=0.11)
    cfg.agent_llms.execution_opinion = AgentLlmConfig(
        provider="claude-cli", model="exec-model", temperature=0.22)

    rt = bs.build_orchestrator_runtime(
        cfg, store=object(), price_store=object(),
        analysis_store=_FakeAnalysisStore(), price_provider=_FakePriceProvider(),
    )
    pipeline = rt._pipeline
    # Planner と ExecutionOpinion が別 client + 別 temperature
    assert pipeline._planner._temperature == 0.11
    assert pipeline._exec._temperature == 0.22
    assert pipeline._planner._llm == ("client", "claude-cli", "planner-model")
    assert pipeline._exec._llm == ("client", "claude-cli", "exec-model")


def test_no_agents_yaml_falls_back_to_price_analysis(tmp_path: Path, monkeypatch) -> None:
    """agents.yaml 無し → 両 agent が price_analysis client + 役割 temperature (従来動作)。"""
    monkeypatch.setattr(
        "src.llm.factory._build_client",
        lambda provider, pc, model: ("client", provider, model),
    )
    _patch_orch_store(monkeypatch, tmp_path)

    cfg = _config(enabled=True, tmp_path=tmp_path)  # agent_llms 全 fallback
    cfg.llm.provider = "claude-cli"
    cfg.llm.price_analysis.model = "price-model"
    cfg.llm.price_analysis.temperature = 0.1

    rt = bs.build_orchestrator_runtime(
        cfg, store=object(), price_store=object(),
        analysis_store=_FakeAnalysisStore(), price_provider=_FakePriceProvider(),
    )
    pipeline = rt._pipeline
    # 両方 price_analysis の temperature (役割 temperature)
    assert pipeline._planner._temperature == 0.1
    assert pipeline._exec._temperature == 0.1


def test_risk_gate_shared_and_configured(tmp_path: Path, monkeypatch) -> None:
    """gate は 1 個だけ構築され pipeline と runtime で共有、min_rr は config 値
    (spec 2.E / R2 High#2 — 二重構築だと live final gate に config が届かない)。"""
    monkeypatch.setattr(
        "src.llm.factory._build_client",
        lambda provider, pc, model: ("client", provider, model),
    )
    _patch_orch_store(monkeypatch, tmp_path)

    cfg = _config(enabled=True, tmp_path=tmp_path)
    cfg.llm.provider = "claude-cli"
    cfg.orchestrator.entry.min_rr = 2.5

    rt = bs.build_orchestrator_runtime(
        cfg, store=object(), price_store=object(),
        analysis_store=_FakeAnalysisStore(), price_provider=_FakePriceProvider(),
    )
    assert rt._pipeline._risk._min_rr == 2.5
    # 同一インスタンス共有 — runtime fallback (既定 1.5) が生成されていないこと。
    assert rt._risk_gate is rt._pipeline._risk
