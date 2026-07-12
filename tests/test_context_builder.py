"""DecisionContextBuilder (spec §7 / §8.7) のテスト。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from src.analysis.technical_snapshot_data import TechnicalSnapshotData
from src.config.schema import AppConfig, InstrumentConfig, OrchestratorConfig
from src.data.analysis_store import AnalysisStore
from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.context_builder import (
    DecisionContextBuilder,
    QuoteSnapshot,
    make_news_provider,
)


def _as_snapshot(price) -> TechnicalSnapshotData:
    return TechnicalSnapshotData(
        pair=price.pair, analyzed_at=price.analyzed_at,
        bias_score=price.bias_score, confidence=price.confidence,
        direction_bias=price.direction_bias,
    )


@pytest.fixture
def builder(tmp_path: Path) -> DecisionContextBuilder:
    db = tmp_path / "orch.db"
    return DecisionContextBuilder(
        orch_store=OrchestratorStore(db),
        analysis_store=AnalysisStore(db),
        config=OrchestratorConfig(),
    )


def test_build_materializes_snapshot_and_returns_context(builder: DecisionContextBuilder) -> None:
    quote = QuoteSnapshot(
        bid=150.0, ask=150.02, mid=150.01, spread=0.02,
        source="mt5", observed_at=datetime(2026, 6, 20, 12, 0, 0),
    )
    ctx = builder.build(
        pair="USDJPY=X",
        now=datetime(2026, 6, 20, 12, 0, 0),
        quote=quote,
    )

    # snapshot_id がコンテキストに含まれ、DB に永続化されている
    assert "snapshot_id" in ctx
    snap = builder._orch.get_snapshot(ctx["snapshot_id"])
    assert snap is not None
    assert snap.pair == "USDJPY=X"

    # §7 標準コンテキストの必須フィールド
    assert ctx["pair"] == "USDJPY=X"
    assert ctx["quote"]["mid"] == 150.01
    assert ctx["technical"]["status"] == "missing"   # AnalysisStore 空
    assert ctx["policy"]["trade_horizon"] == "swing"
    assert ctx["news"]["sentiment_score"] is None


def test_build_reads_fresh_technical_from_store(builder: DecisionContextBuilder, bullish_price) -> None:
    """直近 (max_stale 内) の ok snapshot は status=ok で取り込む。"""
    # bullish_price.analyzed_at は conftest で datetime.now() (≒ 直近) なので fresh。
    builder._analysis.add_snapshot(_as_snapshot(bullish_price))
    quote = QuoteSnapshot(
        bid=150.0, ask=150.02, mid=150.01, spread=0.02,
        source="mt5", observed_at=datetime.now(),
    )
    ctx = builder.build(pair="USDJPY=X", now=datetime.now(), quote=quote)
    assert ctx["technical"]["status"] == "ok"
    assert ctx["technical"]["direction"] == "long"
    assert ctx["technical"]["bias_score"] == pytest.approx(0.7)


def test_build_falls_to_stale_when_latest_ok_is_too_old(
    builder: DecisionContextBuilder, bullish_price
) -> None:
    """max_stale を超えた古い ok snapshot は status=stale に倒す (古いデータで判断しない)。

    これは設計思想の中核: get_latest_ok_row のような lookback 非依存読みを
    decision context に使うと古いデータ汚染が起きる。DecisionContextBuilder は
    max_stale 窓で stale 判定し、古ければ stale に倒す。
    """
    from datetime import timedelta

    # analyzed_at を max_stale (既定 30min) より十分前にずらして保存する。
    bullish_price.analyzed_at = datetime.now() - timedelta(hours=6)
    builder._analysis.add_snapshot(_as_snapshot(bullish_price))
    quote = QuoteSnapshot(
        bid=150.0, ask=150.02, mid=150.01, spread=0.02,
        source="mt5", observed_at=datetime.now(),
    )
    ctx = builder.build(pair="USDJPY=X", now=datetime.now(), quote=quote)
    assert ctx["technical"]["status"] == "stale"
    # stale 時は direction/bias を判断材料に使わせない (None に倒す)
    assert ctx["technical"]["direction"] is None
    assert ctx["technical"]["bias_score"] is None


def test_build_treats_row_older_than_lookback_as_missing(
    builder: DecisionContextBuilder, bullish_price
) -> None:
    """lookback 窓 (24h) より古い行は窓外 → missing (stale ではない)。

    stale と missing の境界の最も鋭い部分: 窓内なら stale、窓外なら missing。
    """
    from datetime import timedelta

    bullish_price.analyzed_at = datetime.now() - timedelta(hours=25)
    builder._analysis.add_snapshot(_as_snapshot(bullish_price))
    quote = QuoteSnapshot(
        bid=150.0, ask=150.02, mid=150.01, spread=0.02,
        source="mt5", observed_at=datetime.now(),
    )
    ctx = builder.build(pair="USDJPY=X", now=datetime.now(), quote=quote)
    assert ctx["technical"]["status"] == "missing"
    assert ctx["technical"]["direction"] is None


def test_build_strips_internal_ref_from_returned_technical(
    builder: DecisionContextBuilder, bullish_price
) -> None:
    """内部キー _ref は返り値 technical ブロックから除去される (DB 内部 ID を露出しない)。

    ただし snapshot の technical_ref には保存される。"""
    builder._analysis.add_snapshot(_as_snapshot(bullish_price))
    quote = QuoteSnapshot(
        bid=150.0, ask=150.02, mid=150.01, spread=0.02,
        source="mt5", observed_at=datetime.now(),
    )
    ctx = builder.build(pair="USDJPY=X", now=datetime.now(), quote=quote)
    assert "_ref" not in ctx["technical"]
    # snapshot 側には technical_ref が保存されている
    snap = builder._orch.get_snapshot(ctx["snapshot_id"])
    assert snap.technical_ref is not None
    assert "snapshot_id" in snap.technical_ref


def test_build_uses_injected_news_provider(tmp_path: Path) -> None:
    """news_provider 注入時は build() の news ブロックが provider の戻り値で埋まる。"""
    db = tmp_path / "orch.db"
    captured: list[str] = []

    def news_provider(pair: str) -> dict:
        captured.append(pair)
        return {"sentiment_score": 0.42, "confidence": 0.8, "top_reasons": ["BoJ hold"]}

    builder = DecisionContextBuilder(
        orch_store=OrchestratorStore(db),
        analysis_store=AnalysisStore(db),
        config=OrchestratorConfig(),
        news_provider=news_provider,
    )
    quote = QuoteSnapshot(
        bid=150.0, ask=150.02, mid=150.01, spread=0.02,
        source="mt5", observed_at=datetime(2026, 6, 20, 12, 0, 0),
    )
    ctx = builder.build(
        pair="USDJPY=X", now=datetime(2026, 6, 20, 12, 0, 0), quote=quote,
    )

    assert captured == ["USDJPY=X"]                 # provider が pair で呼ばれた
    assert ctx["news"]["sentiment_score"] == 0.42
    assert ctx["news"]["confidence"] == 0.8
    assert ctx["news"]["top_reasons"] == ["BoJ hold"]
    # news_ref が snapshot に永続化されている (trace 用)
    snap = builder._orch.get_snapshot(ctx["snapshot_id"])
    assert snap.news_ref is not None


def test_build_falls_back_to_empty_news_when_provider_none(builder: DecisionContextBuilder) -> None:
    """news_provider 未注入 (None) なら従来の空 news に倒す (後方互換)。"""
    quote = QuoteSnapshot(
        bid=150.0, ask=150.02, mid=150.01, spread=0.02,
        source="mt5", observed_at=datetime(2026, 6, 20, 12, 0, 0),
    )
    ctx = builder.build(
        pair="USDJPY=X", now=datetime(2026, 6, 20, 12, 0, 0), quote=quote,
    )
    assert ctx["news"] == {"sentiment_score": None, "confidence": None, "top_reasons": []}
    snap = builder._orch.get_snapshot(ctx["snapshot_id"])
    assert snap.news_ref is None


def test_build_uses_injected_risk_state_provider(tmp_path: Path) -> None:
    """risk_state_provider 注入時は build()/assemble() の risk_state が provider 由来になる。

    休場中・halt 中を context に反映できないと risk_gate / watch の final wall が
    固定値 (market_open=True/halt=none) で素通りしてしまう (Codex High)。
    """
    db = tmp_path / "orch.db"
    captured: list[str] = []

    def risk_provider(pair: str) -> dict:
        captured.append(pair)
        return {"halt": "soft", "bridge_health": "down", "market_open": False, "cooldown": True}

    builder = DecisionContextBuilder(
        orch_store=OrchestratorStore(db),
        analysis_store=AnalysisStore(db),
        config=OrchestratorConfig(),
        risk_state_provider=risk_provider,
    )
    quote = QuoteSnapshot(
        bid=150.0, ask=150.02, mid=150.01, spread=0.02,
        source="mt5", observed_at=datetime(2026, 6, 20, 12, 0, 0),
    )
    now = datetime(2026, 6, 20, 12, 0, 0)
    ctx = builder.build(pair="USDJPY=X", now=now, quote=quote)
    assert captured == ["USDJPY=X"]
    assert ctx["risk_state"] == {
        "halt": "soft", "bridge_health": "down", "market_open": False, "cooldown": True,
    }
    # assemble() (watch 経路) も同じ provider を通す
    actx = builder.assemble(pair="USDJPY=X", now=now, quote=quote)
    assert actx["risk_state"]["market_open"] is False


def test_build_risk_state_none_provider_uses_empty(builder: DecisionContextBuilder) -> None:
    """risk_state_provider 未注入なら従来の楽観既定 (後方互換)。production は必ず注入する。"""
    quote = QuoteSnapshot(
        bid=150.0, ask=150.02, mid=150.01, spread=0.02,
        source="mt5", observed_at=datetime(2026, 6, 20, 12, 0, 0),
    )
    ctx = builder.build(
        pair="USDJPY=X", now=datetime(2026, 6, 20, 12, 0, 0), quote=quote,
    )
    assert ctx["risk_state"] == {
        "halt": "none", "bridge_health": "ok", "market_open": True, "cooldown": False,
    }


def test_build_risk_state_provider_failure_falls_safe(tmp_path: Path) -> None:
    """risk_state_provider が例外を投げたら安全側 (market_open=False) に倒す。

    gate 状態が取れないのに楽観既定に倒すと、休場/halt 中でも trigger しうる。安全側。
    """
    db = tmp_path / "orch.db"

    def boom(pair: str) -> dict:
        raise RuntimeError("gate read failed")

    builder = DecisionContextBuilder(
        orch_store=OrchestratorStore(db),
        analysis_store=AnalysisStore(db),
        config=OrchestratorConfig(),
        risk_state_provider=boom,
    )
    quote = QuoteSnapshot(
        bid=150.0, ask=150.02, mid=150.01, spread=0.02,
        source="mt5", observed_at=datetime(2026, 6, 20, 12, 0, 0),
    )
    ctx = builder.build(
        pair="USDJPY=X", now=datetime(2026, 6, 20, 12, 0, 0), quote=quote,
    )
    assert ctx["risk_state"]["market_open"] is False
    assert ctx["risk_state"]["bridge_health"] != "ok"


def test_build_survives_news_provider_failure(tmp_path: Path) -> None:
    """news_provider が例外を投げても build() は落ちず空 news に倒す (材料1つで全停止しない)。"""
    db = tmp_path / "orch.db"

    def boom_provider(pair: str) -> dict:
        raise RuntimeError("RAG store down")

    builder = DecisionContextBuilder(
        orch_store=OrchestratorStore(db),
        analysis_store=AnalysisStore(db),
        config=OrchestratorConfig(),
        news_provider=boom_provider,
    )
    quote = QuoteSnapshot(
        bid=150.0, ask=150.02, mid=150.01, spread=0.02,
        source="mt5", observed_at=datetime(2026, 6, 20, 12, 0, 0),
    )
    ctx = builder.build(
        pair="USDJPY=X", now=datetime(2026, 6, 20, 12, 0, 0), quote=quote,
    )
    assert ctx["news"] == {"sentiment_score": None, "confidence": None, "top_reasons": []}


class _FakeVectorStore:
    """get_recent_category_news だけ提供する最小スタブ (RAG/LLM なし)。"""

    def __init__(self, entries: list[dict]) -> None:
        self._entries = entries

    def get_recent_category_news(self, categories, lookback_hours):
        return self._entries


def _config_with_pair() -> AppConfig:
    inst = InstrumentConfig(
        symbol="USDJPY=X", display_name="USD/JPY", asset_type="fx",
        base_currency="USD", quote_currency="JPY",
    )
    return AppConfig(instruments=[inst])


def test_make_news_provider_maps_sentiment_to_section7_shape() -> None:
    """make_news_provider は aggregate_news_sentiment を §7 news dict に変換する。"""
    entries = [{
        "metadata": {
            "category": "fx", "sentiment_score": 0.5, "confidence": 0.8,
            "key_themes": "BoJ hawkish, yield gap", "summary": "JPY firm",
        },
    }]
    provider = make_news_provider(_config_with_pair(), _FakeVectorStore(entries))
    news = provider("USDJPY=X")

    assert news["sentiment_score"] == pytest.approx(0.5)
    assert news["confidence"] == pytest.approx(0.8)
    assert news["top_reasons"] == ["BoJ hawkish", "yield gap"]


def test_make_news_provider_unknown_pair_returns_empty_news() -> None:
    """config に無い pair は集計せず空 news に倒す (KeyError で落とさない)。"""
    provider = make_news_provider(_config_with_pair(), _FakeVectorStore([]))
    news = provider("GBPUSD=X")
    assert news == {"sentiment_score": None, "confidence": None, "top_reasons": []}


def test_news_provider_factory_integrates_with_builder(tmp_path: Path) -> None:
    """factory の戻り値を builder に注入すると news ブロックが埋まる (end-to-end)。"""
    db = tmp_path / "orch.db"
    entries = [{
        "metadata": {
            "category": "fx", "sentiment_score": 0.3, "confidence": 0.6,
            "key_themes": "risk-on", "summary": "calm",
        },
    }]
    provider = make_news_provider(_config_with_pair(), _FakeVectorStore(entries))
    builder = DecisionContextBuilder(
        orch_store=OrchestratorStore(db),
        analysis_store=AnalysisStore(db),
        config=OrchestratorConfig(),
        news_provider=provider,
    )
    quote = QuoteSnapshot(
        bid=150.0, ask=150.02, mid=150.01, spread=0.02,
        source="mt5", observed_at=datetime(2026, 6, 20, 12, 0, 0),
    )
    ctx = builder.build(
        pair="USDJPY=X", now=datetime(2026, 6, 20, 12, 0, 0), quote=quote,
    )
    assert ctx["news"]["sentiment_score"] == pytest.approx(0.3)
    assert ctx["news"]["top_reasons"] == ["risk-on"]


def test_build_maps_neutral_direction(builder: DecisionContextBuilder, bearish_price) -> None:
    """direction_bias が long/short 以外なら direction=neutral にマップされる。

    bearish_price を neutral に書き換えて neutral 分岐を踏ませる。"""
    bearish_price.direction_bias = "neutral"
    builder._analysis.add_snapshot(_as_snapshot(bearish_price))
    quote = QuoteSnapshot(
        bid=150.0, ask=150.02, mid=150.01, spread=0.02,
        source="mt5", observed_at=datetime.now(),
    )
    ctx = builder.build(pair="USDJPY=X", now=datetime.now(), quote=quote)
    assert ctx["technical"]["status"] == "ok"
    assert ctx["technical"]["direction"] == "neutral"
