"""OrchestratorRuntime.run_daily_summary_cycle の 1 日 1 回ガード (Phase1 Task A-1)。

daily_summary_time を跨いだ最初の cycle で 1 回だけ送り、同日二度目は no-op、
日付が変われば再送、フラグ off / notifier 未注入なら何もしないことを検証する。
実 Discord は叩かない (RecordingNotifier スパイ)。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.config.schema import OrchestratorConfig
from src.data.analysis_store import AnalysisStore
from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.context_builder import DecisionContextBuilder, QuoteSnapshot
from src.orchestrator.runtime import OrchestratorRuntime


class RecordingNotifier:
    """notify_daily_summary の呼び出しを記録する duck-typed スパイ。"""

    def __init__(self) -> None:
        self.days: list[datetime] = []

    async def notify_daily_summary(self, metrics, *, day) -> None:
        self.days.append(day)


def _build_runtime(tmp_path: Path, *, notifier, daily_time="07:00", flag=True):
    db = tmp_path / "orch.db"
    cfg = OrchestratorConfig(enabled=True)
    cfg.notifications.daily_summary_time = daily_time
    cfg.notifications.shadow_daily_summary = flag
    orch = OrchestratorStore(db)
    ctx = DecisionContextBuilder(orch, AnalysisStore(db), cfg)

    def quote_provider(pair: str) -> QuoteSnapshot:
        return QuoteSnapshot(
            bid=150.0, ask=150.0, mid=150.0, spread=None,
            source="test", observed_at=datetime(2026, 6, 21, 9, 0, 0),
        )

    return OrchestratorRuntime(
        config=cfg, orch_store=orch, context_builder=ctx,
        pairs=["USDJPY=X"], quote_provider=quote_provider,
        shadow_notifier=notifier,
    )


def test_fires_once_after_summary_time(tmp_path):
    n = RecordingNotifier()
    rt = _build_runtime(tmp_path, notifier=n)
    # 07:00 を過ぎた最初の cycle で送る。
    assert rt.run_daily_summary_cycle(datetime(2026, 6, 21, 7, 30)) is True
    assert len(n.days) == 1


def test_before_summary_time_no_fire(tmp_path):
    n = RecordingNotifier()
    rt = _build_runtime(tmp_path, notifier=n)
    # 06:59 はまだ送信時刻前。
    assert rt.run_daily_summary_cycle(datetime(2026, 6, 21, 6, 59)) is False
    assert n.days == []


def test_second_call_same_day_is_noop(tmp_path):
    n = RecordingNotifier()
    rt = _build_runtime(tmp_path, notifier=n)
    rt.run_daily_summary_cycle(datetime(2026, 6, 21, 7, 30))
    # 同日 2 回目は送らない。
    assert rt.run_daily_summary_cycle(datetime(2026, 6, 21, 18, 0)) is False
    assert len(n.days) == 1


def test_next_day_fires_again(tmp_path):
    n = RecordingNotifier()
    rt = _build_runtime(tmp_path, notifier=n)
    rt.run_daily_summary_cycle(datetime(2026, 6, 21, 7, 30))
    # 翌日は再送する。
    assert rt.run_daily_summary_cycle(datetime(2026, 6, 22, 7, 30)) is True
    assert len(n.days) == 2


def test_flag_off_no_fire(tmp_path):
    n = RecordingNotifier()
    rt = _build_runtime(tmp_path, notifier=n, flag=False)
    assert rt.run_daily_summary_cycle(datetime(2026, 6, 21, 9, 0)) is False
    assert n.days == []


def test_no_notifier_no_fire(tmp_path):
    rt = _build_runtime(tmp_path, notifier=None)
    # notifier 未注入なら no-op (例外も出さない)。
    assert rt.run_daily_summary_cycle(datetime(2026, 6, 21, 9, 0)) is False


def test_invalid_time_falls_back_to_0700(tmp_path):
    n = RecordingNotifier()
    rt = _build_runtime(tmp_path, notifier=n, daily_time="not-a-time")
    # 不正値は 07:00 扱い → 06:59 は前、07:30 は後。
    assert rt.run_daily_summary_cycle(datetime(2026, 6, 21, 6, 59)) is False
    assert rt.run_daily_summary_cycle(datetime(2026, 6, 21, 7, 30)) is True


def test_metrics_failure_retries_same_day(tmp_path, monkeypatch):
    """metrics 計算が一時失敗した日は送信済み扱いにせず、当日中に再試行する (Medium#4)。"""
    n = RecordingNotifier()
    rt = _build_runtime(tmp_path, notifier=n)

    calls = {"n": 0}

    def flaky_metrics(store, *, now, trade_horizon=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        from src.orchestrator.shadow_metrics import ShadowMetrics
        return ShadowMetrics()

    monkeypatch.setattr(
        "src.orchestrator.shadow_metrics.compute_shadow_metrics", flaky_metrics
    )
    # 1 回目: metrics 失敗 → False、日付未確定。
    assert rt.run_daily_summary_cycle(datetime(2026, 6, 21, 7, 30)) is False
    assert n.days == []
    # 2 回目 (同日): 再試行が成功して送信される。
    assert rt.run_daily_summary_cycle(datetime(2026, 6, 21, 7, 35)) is True
    assert len(n.days) == 1
