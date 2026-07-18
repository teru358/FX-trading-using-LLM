from datetime import datetime

from src.config.schema import OrchestratorConfig
from src.data.analysis_store import AnalysisStore
from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.context_builder import DecisionContextBuilder, QuoteSnapshot


def _builder(tmp_path, news_provider):
    db = tmp_path / "o.db"
    return DecisionContextBuilder(
        OrchestratorStore(db), AnalysisStore(db), OrchestratorConfig(),
        news_provider=news_provider,
    )


def _quote(now):
    return QuoteSnapshot(bid=150.0, ask=150.02, mid=150.01, spread=0.02,
                         source="test", observed_at=now)


def _direct_build_news(builder, pair, now):
    return builder._build_news(pair, now)


def test_build_news_stale_keeps_status_and_as_of(tmp_path):
    now = datetime(2026, 7, 17, 0, 5, 0)
    past = "2026-07-17T00:00:00"
    def provider(pair):
        return {"sentiment_score": -0.6, "confidence": 0.8,
                "top_reasons": ["x"], "status": "stale", "as_of": past}
    b = _builder(tmp_path, provider)
    news = _direct_build_news(b, "EURUSD=X", now)
    assert news["sentiment_score"] == -0.6
    assert news["status"] == "stale"
    assert news["_ref"]["status"] == "stale"
    assert news["_ref"]["as_of"] == past


def test_build_news_unavailable_marks_status(tmp_path):
    now = datetime(2026, 7, 17, 0, 5, 0)
    def provider(pair):
        return {"sentiment_score": None, "confidence": None,
                "top_reasons": [], "status": "unavailable", "as_of": None}
    b = _builder(tmp_path, provider)
    news = _direct_build_news(b, "EURUSD=X", now)
    assert news["sentiment_score"] is None
    assert news["status"] == "unavailable"
    assert news["_ref"]["status"] == "unavailable"


def test_build_news_legacy_provider_without_status_is_ok(tmp_path):
    """status を返さない旧形式 provider が正常値を返したら status="ok" に倒す (後方互換)。"""
    now = datetime(2026, 7, 17, 0, 5, 0)
    def legacy_provider(pair):
        return {"sentiment_score": 0.4, "confidence": 0.7, "top_reasons": ["x"]}
    b = _builder(tmp_path, legacy_provider)
    news = _direct_build_news(b, "EURUSD=X", now)
    assert news["sentiment_score"] == 0.4
    assert news["status"] == "ok"                       # unavailable でなく ok
    assert news["_ref"]["as_of"] == now.isoformat()     # as_of は now に倒す


def test_assemble_news_keeps_status(tmp_path):
    now = datetime(2026, 7, 17, 0, 5, 0)
    def provider(pair):
        return {"sentiment_score": None, "confidence": None,
                "top_reasons": [], "status": "unavailable", "as_of": None}
    b = _builder(tmp_path, provider)
    ctx = b.assemble(pair="EURUSD=X", now=now, quote=_quote(now))
    assert "_ref" not in ctx["news"]
    assert ctx["news"]["status"] == "unavailable"
