# orchestrator 観測性 + watch news キャッシュ 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** planning cycle をログで可視化し、watch loop の毎秒 news 再集計を short-TTL キャッシュ (stale-if-error) で止める。

**Architecture:** (A) `[ORCH]` を activity.log に登録 + planning start/result を全経路で 1:1 ログ。(B) news_provider を pair 単位 single-flight + failure-TTL キャッシュでラップし、失敗時は直近成功値を stale として返す (live trigger 判断を変えない)。(C) plan 中断時の orphan は注記のみ (コード変更なし)。

**Tech Stack:** Python 3.12 / SQLAlchemy / dataclass config / pytest / uv (WSL 内実行厳守)。

**設計正本:** `docs/superpowers/specs/2026-07-17-orchestrator-observability-and-watch-news-cache-design.md`

**重要な運用制約:**
- finance の uv/pytest は**必ず WSL 内**で実行する: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest ..."`。Windows 側 UNC 経由で uv を実行すると `.venv` が破壊される。
- フル suite は順序依存フレークを持つため、回帰判定は**変更に関係する per-file テスト**で行う。
- main.py のロガーは `_logger` (アンダースコア前置)。

---

## File Structure

| ファイル | 責務 | 変更 |
|---|---|---|
| `src/logging_setup.py` | ログプレフィックス registry | `[ORCH]` 追加 (Task 1) |
| `src/config/schema.py` | config dataclass | `news_cache_ttl_seconds` / `news_cache_negative_ttl_seconds` 追加 (Task 2) |
| `src/orchestrator/context_builder.py` | §7 context 組立 + news provider | `make_cached_news_provider` 新設 / `_build_news` を status/as_of 対応 (Task 3) |
| `src/orchestrator/landing_providers.py` | material 用 news 集計 | `_aggregate` 例外伝搬 + TTL キャッシュ (Task 4) |
| `src/orchestrator/planning_pipeline.py` | planning pipeline | `PipelineResult` に reason/derived_rr 追加 + 全経路設定 (Task 5) |
| `src/orchestrator/material_landing.py` | material 判定 + 発火 pair 決定 | `pairs_to_plan` → `PlanningTarget` (Task 6) |
| `src/orchestrator/runtime.py` | planning/watch loop | planning start/result ログ + 1:1 契約 (Task 7) |
| `src/orchestrator/bootstrap.py` | 部品結線 | cached news_provider 配線 (Task 3) |

実装順: 2・3 (キャッシュ失敗セマンティクス) を最優先。5・6 (データ契約 + trigger) を 7 (ログ本体) より前に確定。

---

## Task 1: `[ORCH]` を activity.log registry に登録

**Files:**
- Modify: `src/logging_setup.py:73` (registry タプル末尾)
- Test: `tests/test_logging_setup.py`

- [ ] **Step 1: Write the failing test**

`tests/test_logging_setup.py` に追加 (無ければ新規作成):

```python
from src.logging_setup import _ACTIVITY_PREFIXES, _PREFIX_STYLES


def test_orch_prefix_registered_for_activity():
    """[ORCH] は activity.log 対象 (goes_to_activity_log=True) に登録されている。"""
    assert "[ORCH]" in _ACTIVITY_PREFIXES


def test_orch_prefix_has_style():
    """[ORCH] は着色スタイルを持つ。"""
    assert "[ORCH]" in _PREFIX_STYLES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_logging_setup.py -v"`
Expected: FAIL (`[ORCH]` not in `_ACTIVITY_PREFIXES`)

- [ ] **Step 3: Add registry entry**

`src/logging_setup.py` の `_PREFIX_REGISTRY` タプル末尾 (`[BRIDGE_GATE]` 行の後、閉じ括弧の前) に追加:

```python
    ("[ORCH]",         "bold cyan",     True),   # orchestrator planning / watch / trigger イベント
```

- [ ] **Step 4: Run test to verify it passes**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_logging_setup.py -v"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/logging_setup.py tests/test_logging_setup.py
git commit -m "feat(logging): register [ORCH] prefix for activity.log"
```

---

## Task 2: news キャッシュ config 2 値を追加

**Files:**
- Modify: `src/config/schema.py:677-708` (`OrchestratorEntryConfig`)
- Test: `tests/test_config_schema.py` (無ければ該当既存テストファイルに追加)

- [ ] **Step 1: Write the failing test**

`tests/test_config_schema.py` に追加:

```python
import math
import pytest
from src.config.schema import OrchestratorEntryConfig


def test_news_cache_ttl_defaults():
    cfg = OrchestratorEntryConfig()
    assert cfg.news_cache_ttl_seconds == 60.0
    assert cfg.news_cache_negative_ttl_seconds == 30.0


@pytest.mark.parametrize("field,bad", [
    ("news_cache_ttl_seconds", 0.0),
    ("news_cache_ttl_seconds", -1.0),
    ("news_cache_ttl_seconds", float("nan")),
    ("news_cache_negative_ttl_seconds", 0.0),
    ("news_cache_negative_ttl_seconds", float("inf")),
])
def test_news_cache_ttl_rejects_nonpositive_or_nonfinite(field, bad):
    with pytest.raises(ValueError):
        OrchestratorEntryConfig(**{field: bad})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_config_schema.py -k news_cache -v"`
Expected: FAIL (`news_cache_ttl_seconds` 属性が無い)

- [ ] **Step 3: Add fields + validation**

`src/config/schema.py` の `OrchestratorEntryConfig` に、`max_technical_age_seconds` の後 (フィールド定義末尾) に追加:

```python
    news_cache_ttl_seconds: float = 60.0
    news_cache_negative_ttl_seconds: float = 30.0
```

`__post_init__` の末尾 (`for name in ("spread_max_pips", "news_impact_min"):` ループの後) に追加:

```python
        # news キャッシュ TTL: <=0 / 非有限は無効 (キャッシュが常時 miss / 常時 hit になる)。
        for name in ("news_cache_ttl_seconds", "news_cache_negative_ttl_seconds"):
            val = getattr(self, name)
            if not isinstance(val, (int, float)) or isinstance(val, bool) or \
                    not math.isfinite(val) or val <= 0:
                raise ValueError(
                    f"orchestrator.entry.{name} must be finite and > 0, got {val!r}"
                )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_config_schema.py -k news_cache -v"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/config/schema.py tests/test_config_schema.py
git commit -m "feat(config): add news_cache_ttl_seconds / news_cache_negative_ttl_seconds"
```

---

## Task 3: cached news provider (single-flight + stale-if-error) + `_build_news` 対応

**Files:**
- Modify: `src/orchestrator/context_builder.py` (`make_cached_news_provider` 新設 / `_build_news` 変更)
- Modify: `src/orchestrator/bootstrap.py:213` (配線)
- Test: `tests/test_cached_news_provider.py` (新規)

### Task 3a: `make_cached_news_provider` 本体

- [ ] **Step 1: Write the failing test**

`tests/test_cached_news_provider.py` (新規):

```python
from datetime import datetime, timedelta

from src.orchestrator.context_builder import make_cached_news_provider


class _Clock:
    def __init__(self, start: datetime):
        self.now = start
    def __call__(self) -> datetime:
        return self.now
    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _sentiment(score: float) -> dict:
    return {"sentiment_score": score, "confidence": 0.8, "top_reasons": ["x"]}


def test_ttl_hit_calls_inner_once():
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    calls = {"n": 0}
    def inner(pair):
        calls["n"] += 1
        return _sentiment(0.5)
    p = make_cached_news_provider(inner, ttl_seconds=60, negative_ttl_seconds=30, clock=clock)
    r1 = p("EURUSD=X")
    clock.advance(30)
    r2 = p("EURUSD=X")
    assert calls["n"] == 1                 # TTL 内は inner 1 回
    assert r1["status"] == "ok"
    assert r2["sentiment_score"] == 0.5


def test_ttl_expiry_recomputes():
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    calls = {"n": 0}
    def inner(pair):
        calls["n"] += 1
        return _sentiment(0.5)
    p = make_cached_news_provider(inner, ttl_seconds=60, negative_ttl_seconds=30, clock=clock)
    p("EURUSD=X")
    clock.advance(61)
    p("EURUSD=X")
    assert calls["n"] == 2                 # TTL 超過で再集計


def test_failure_ttl_suppresses_recompute():
    """失敗後 negative TTL 内は inner を呼ばない (計算とログ両方を止める)。"""
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    calls = {"n": 0}
    def inner(pair):
        calls["n"] += 1
        raise RuntimeError("boom")
    p = make_cached_news_provider(inner, ttl_seconds=60, negative_ttl_seconds=30, clock=clock)
    p("EURUSD=X")                          # 1 回目失敗
    clock.advance(10)
    p("EURUSD=X")                          # negative TTL 内 → inner 呼ばない
    assert calls["n"] == 1


def test_stale_if_error_keeps_last_success():
    """成功後の refresh 失敗で直近成功値 (status=stale) が返り as_of は成功時刻。"""
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    state = {"fail": False}
    def inner(pair):
        if state["fail"]:
            raise RuntimeError("boom")
        return _sentiment(-0.6)
    p = make_cached_news_provider(inner, ttl_seconds=60, negative_ttl_seconds=30, clock=clock)
    ok = p("EURUSD=X")
    success_as_of = ok["as_of"]
    clock.advance(61)                      # TTL 超過で refresh を試みる
    state["fail"] = True
    stale = p("EURUSD=X")
    assert stale["status"] == "stale"
    assert stale["sentiment_score"] == -0.6      # 直近成功値を維持
    assert stale["as_of"] == success_as_of       # as_of は成功時刻のまま


def test_unavailable_when_never_succeeded():
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    def inner(pair):
        raise RuntimeError("boom")
    p = make_cached_news_provider(inner, ttl_seconds=60, negative_ttl_seconds=30, clock=clock)
    r = p("EURUSD=X")
    assert r["status"] == "unavailable"
    assert r["sentiment_score"] is None
    assert r["as_of"] is None


def test_success_clears_failure_state():
    """失敗 → 成功 → 失敗 で 2 回目失敗が再び inner を呼ぶ。"""
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    calls = {"n": 0}
    state = {"fail": True}
    def inner(pair):
        calls["n"] += 1
        if state["fail"]:
            raise RuntimeError("boom")
        return _sentiment(0.5)
    p = make_cached_news_provider(inner, ttl_seconds=60, negative_ttl_seconds=30, clock=clock)
    p("EURUSD=X")                          # 失敗 (calls=1)
    clock.advance(31)                      # negative TTL 超過
    state["fail"] = False
    p("EURUSD=X")                          # 成功 (calls=2, failure クリア)
    clock.advance(61)                      # 成功 TTL 超過
    state["fail"] = True
    p("EURUSD=X")                          # 再度失敗 → inner 呼ぶ (calls=3)
    assert calls["n"] == 3


def test_single_flight_concurrent_miss():
    """2 スレッド同時 miss でも inner は 1 回だけ。"""
    import threading, time
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    calls = {"n": 0}
    barrier = threading.Barrier(2)
    def inner(pair):
        barrier.wait()                     # 両スレッドを同時に miss させる
        time.sleep(0.05)
        calls["n"] += 1
        return _sentiment(0.5)
    p = make_cached_news_provider(inner, ttl_seconds=60, negative_ttl_seconds=30, clock=clock)
    threads = [threading.Thread(target=lambda: p("EURUSD=X")) for _ in range(2)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert calls["n"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_cached_news_provider.py -v"`
Expected: FAIL (`make_cached_news_provider` が存在しない)

- [ ] **Step 3: Implement `make_cached_news_provider`**

`src/orchestrator/context_builder.py` の module 末尾 (`make_news_provider` の後) に追加。ファイル冒頭の import に不足があれば足す (`from collections import defaultdict` / `from threading import Lock` / `from datetime import datetime` / `from typing import Callable`):

```python
def make_cached_news_provider(
    inner: NewsProvider,
    *,
    ttl_seconds: float,
    negative_ttl_seconds: float,
    clock: Callable[[], datetime],
) -> NewsProvider:
    """news_provider を pair 単位 TTL キャッシュでラップする。

    - 成功 TTL 内: 再集計せず前回成功値を status="ok" で返す。
    - pair 単位 lock で single-flight (同時 miss の二重集計を防ぐ)。
    - failure TTL 内: inner を呼ばず stale (直近成功値・status="stale") /
      成功履歴なしは status="unavailable" を返す (計算とログ両方を止める)。
    - 成功で failure 状態を解除する。
    返却 dict には status (ok|stale|unavailable) と as_of (成功時刻 ISO / None) を必ず付ける。
    """
    cache: dict[str, tuple[dict, datetime]] = {}   # pair -> (成功 news, 成功時刻)
    failures: dict[str, datetime] = {}             # pair -> 直近失敗時刻
    locks: dict[str, Lock] = defaultdict(Lock)
    guard = Lock()

    def _ok(value: dict, at: datetime) -> dict:
        return {**value, "status": "ok", "as_of": at.isoformat()}

    def _stale(value: dict, at: datetime) -> dict:
        return {**value, "status": "stale", "as_of": at.isoformat()}

    def _unavailable() -> dict:
        return {
            "sentiment_score": None, "confidence": None,
            "top_reasons": [], "status": "unavailable", "as_of": None,
        }

    def provider(pair: str) -> dict:
        now = clock()
        hit = cache.get(pair)
        if hit is not None and (now - hit[1]).total_seconds() < ttl_seconds:
            return _ok(hit[0], hit[1])
        with guard:
            lock = locks[pair]
        with lock:                                 # single-flight
            hit = cache.get(pair)
            if hit is not None and (now - hit[1]).total_seconds() < ttl_seconds:
                return _ok(hit[0], hit[1])
            failed_at = failures.get(pair)
            if failed_at is not None and (now - failed_at).total_seconds() < negative_ttl_seconds:
                # failure TTL 内: inner を呼ばない (計算もログも止める)。
                return _stale(hit[0], hit[1]) if hit is not None else _unavailable()
            try:
                value = inner(pair)                # ← ここでのみ aggregate_news_sentiment が走る
            except Exception as exc:
                failures[pair] = now
                logger.warning("[ORCH] news aggregate failed for %s: %s", pair, exc)
                return _stale(hit[0], hit[1]) if hit is not None else _unavailable()
            cache[pair] = (value, now)
            failures.pop(pair, None)               # 成功で failure 解除
            return _ok(value, now)

    return provider
```

- [ ] **Step 4: Run test to verify it passes**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_cached_news_provider.py -v"`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/context_builder.py tests/test_cached_news_provider.py
git commit -m "feat(orchestrator): add cached news provider (single-flight + stale-if-error)"
```

### Task 3b: `_build_news` を status/as_of 対応 + bootstrap 配線

- [ ] **Step 1: Write the failing test**

`tests/test_context_builder_news.py` (無ければ新規):

```python
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
                         source="test", observed_at=now.isoformat())


def test_build_news_uses_provider_as_of(tmp_path):
    """provider の as_of を _ref に使う (now で上書きしない)。"""
    now = datetime(2026, 7, 17, 0, 5, 0)
    past = "2026-07-17T00:00:00"
    def provider(pair):
        return {"sentiment_score": -0.6, "confidence": 0.8,
                "top_reasons": ["x"], "status": "stale", "as_of": past}
    b = _builder(tmp_path, provider)
    ctx = b.assemble(pair="EURUSD=X", now=now, quote=_quote(now))
    assert ctx["news"]["sentiment_score"] == -0.6      # stale 値が通る
    # news_conflict 判定に使う sentiment が None にならないこと (High 指摘の核心)


def test_build_news_unavailable_passes_none(tmp_path):
    now = datetime(2026, 7, 17, 0, 5, 0)
    def provider(pair):
        return {"sentiment_score": None, "confidence": None,
                "top_reasons": [], "status": "unavailable", "as_of": None}
    b = _builder(tmp_path, provider)
    ctx = b.assemble(pair="EURUSD=X", now=now, quote=_quote(now))
    assert ctx["news"]["sentiment_score"] is None
```

- [ ] **Step 2: Run test to verify it fails or passes as baseline**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_context_builder_news.py -v"`
Expected: `test_build_news_uses_provider_as_of` は現状 as_of=now で上書きするため PASS するが `_ref` の as_of は誤り。まず現状の `_build_news` を読み、as_of/status を反映するよう変更する (下記 Step 3)。両テストが provider dict をそのまま尊重することを保証する。

- [ ] **Step 3: Modify `_build_news`**

`src/orchestrator/context_builder.py` の `_build_news` (現在 context_builder.py:256-279) を次に置き換える。provider が status/as_of を返す前提で、`_ref.as_of` を provider の as_of にする:

```python
    def _build_news(self, pair: str, now: datetime) -> dict[str, Any]:
        """注入された news_provider から §7 news ブロックを組む。

        provider 未注入なら従来の空 news に倒す (後方互換)。cached provider は
        例外を投げず status (ok|stale|unavailable) + as_of 付き dict を返すため、
        as_of をそのまま _ref に使い (now で上書きしない)、status を _ref に残す。
        provider が直接例外を投げるケース (非 cached provider) も従来通り空 news に倒す。
        """
        if self._news_provider is None:
            return {**self._empty_news(), "_ref": None}
        try:
            raw = self._news_provider(pair)
        except Exception:
            logger.exception("[ORCH] news_provider failed for %s — empty news", pair)
            return {**self._empty_news(), "_ref": None}
        status = raw.get("status")
        as_of = raw.get("as_of")
        return {
            "sentiment_score": raw.get("sentiment_score"),
            "confidence": raw.get("confidence"),
            "top_reasons": raw.get("top_reasons") or [],
            "_ref": (
                None if status == "unavailable"
                else {"source": "rag_aggregate", "as_of": as_of, "status": status}
            ),
        }
```

- [ ] **Step 4: Wire cached provider in bootstrap**

`src/orchestrator/bootstrap.py:213` の `news_provider=make_news_provider(config, store),` を次に変更。import に `make_cached_news_provider` / `db_now` を追加:

```python
        news_provider=make_cached_news_provider(
            make_news_provider(config, store),
            ttl_seconds=orch_cfg.entry.news_cache_ttl_seconds,
            negative_ttl_seconds=orch_cfg.entry.news_cache_negative_ttl_seconds,
            clock=db_now,
        ),
```

import 追加 (bootstrap.py:26 の `make_news_provider` import 行に足す):

```python
from src.orchestrator.context_builder import (
    DecisionContextBuilder,
    QuoteSnapshot,
    make_cached_news_provider,
    make_news_provider,
)
```

`db_now` import が bootstrap に無ければ追加: `from src.utils.clock import db_now`

- [ ] **Step 5: Run tests**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_context_builder_news.py tests/test_cached_news_provider.py -v"`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/context_builder.py src/orchestrator/bootstrap.py tests/test_context_builder_news.py
git commit -m "feat(orchestrator): _build_news honors provider status/as_of; wire cached news provider"
```

---

## Task 4: material provider の例外伝搬 + TTL キャッシュ (B-2)

**Files:**
- Modify: `src/orchestrator/landing_providers.py:110-135` (`make_news_material_provider`)
- Test: `tests/test_news_material_provider_cache.py` (新規)

- [ ] **Step 1: Write the failing test**

`tests/test_news_material_provider_cache.py` (新規):

```python
from datetime import datetime, timedelta

import pytest

from src.config.schema import AppConfig, InstrumentConfig
from src.orchestrator.landing_providers import make_news_material_provider


class _Clock:
    def __init__(self, start): self.now = start
    def __call__(self): return self.now
    def advance(self, s): self.now += timedelta(seconds=s)


class _Sent:
    def __init__(self, score, summary):
        self.sentiment_score = score
        self.summary = summary


def _config():
    return AppConfig(instruments=[
        InstrumentConfig(symbol="EURUSD=X", display_name="EUR/USD",
                         base_currency="EUR", quote_currency="USD", pip_value=0.0001),
    ])


def test_impact_and_key_aggregate_once(monkeypatch):
    """impact + key 連続呼び出しで aggregate は 1 回に集約 (TTL 内)。"""
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    calls = {"n": 0}
    def fake_aggregate(inst, store, config):
        calls["n"] += 1
        return _Sent(0.6, "sum")
    monkeypatch.setattr(
        "src.analysis.news_aggregator.aggregate_news_sentiment", fake_aggregate
    )
    impact, key = make_news_material_provider(
        _config(), store=object(), ttl_seconds=60,
        negative_ttl_seconds=30, clock=clock,
    )
    impact("EURUSD=X")
    key("EURUSD=X")
    assert calls["n"] == 1


def test_material_stale_if_error(monkeypatch):
    """成功後の refresh 失敗で前回 sentiment (impact) が維持される。"""
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    state = {"fail": False}
    def fake_aggregate(inst, store, config):
        if state["fail"]:
            raise RuntimeError("boom")
        return _Sent(0.6, "sum")
    monkeypatch.setattr(
        "src.analysis.news_aggregator.aggregate_news_sentiment", fake_aggregate
    )
    impact, key = make_news_material_provider(
        _config(), store=object(), ttl_seconds=60,
        negative_ttl_seconds=30, clock=clock,
    )
    assert impact("EURUSD=X") == pytest.approx(0.6)
    clock.advance(61)
    state["fail"] = True
    assert impact("EURUSD=X") == pytest.approx(0.6)      # stale 維持


def test_material_unavailable_returns_zero(monkeypatch):
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    def fake_aggregate(inst, store, config):
        raise RuntimeError("boom")
    monkeypatch.setattr(
        "src.analysis.news_aggregator.aggregate_news_sentiment", fake_aggregate
    )
    impact, key = make_news_material_provider(
        _config(), store=object(), ttl_seconds=60,
        negative_ttl_seconds=30, clock=clock,
    )
    assert impact("EURUSD=X") == 0.0
    assert key("EURUSD=X") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_news_material_provider_cache.py -v"`
Expected: FAIL (`make_news_material_provider` が ttl_seconds 引数を受けない)

- [ ] **Step 3: Rewrite `make_news_material_provider`**

`src/orchestrator/landing_providers.py` の `make_news_material_provider` (現在 landing_providers.py:100-135) を次に置き換える。`_aggregate` の try/except を削除し、TTL キャッシュ (§3.2 と同一失敗セマンティクス) を内部に持つ。import に `from collections import defaultdict` / `from threading import Lock` / `from datetime import datetime` / `from typing import Callable` を足す:

```python
def make_news_material_provider(
    config: "AppConfig",
    store: "VectorStore",
    *,
    ttl_seconds: float,
    negative_ttl_seconds: float,
    clock: Callable[[], datetime],
) -> tuple[Callable[[str], float], Callable[[str], str | None]]:
    """(get_news_impact, get_news_key) を返す。

    _aggregate は例外を握り潰さずキャッシュ層へ伝搬させる (stale-if-error を迂回しない)。
    TTL 内は再集計せず前回成功値を返し、failure TTL 内は inner を呼ばず stale /
    成功履歴なしは None。material 経路は失敗時 impact=0.0 / key=None を維持する。
    """
    from src.analysis.news_aggregator import aggregate_news_sentiment

    by_symbol = {inst.symbol: inst for inst in config.instruments}
    cache: dict[str, tuple[object, datetime]] = {}   # pair -> (NewsSentiment, 成功時刻)
    failures: dict[str, datetime] = {}
    locks: dict[str, Lock] = defaultdict(Lock)
    guard = Lock()

    def _aggregate(pair: str):
        """該当 pair の NewsSentiment を返す。集計不能/失敗時は None (例外は伝搬させず
        stale/None に変換するのはキャッシュ層 _cached_aggregate の責務)。"""
        inst = by_symbol.get(pair)
        if inst is None:
            return None
        return aggregate_news_sentiment(inst, store, config)   # 例外はそのまま送出

    def _cached_aggregate(pair: str):
        now = clock()
        hit = cache.get(pair)
        if hit is not None and (now - hit[1]).total_seconds() < ttl_seconds:
            return hit[0]
        with guard:
            lock = locks[pair]
        with lock:
            hit = cache.get(pair)
            if hit is not None and (now - hit[1]).total_seconds() < ttl_seconds:
                return hit[0]
            failed_at = failures.get(pair)
            if failed_at is not None and (now - failed_at).total_seconds() < negative_ttl_seconds:
                return hit[0] if hit is not None else None
            try:
                value = _aggregate(pair)
            except Exception as exc:
                failures[pair] = now
                logger.warning("[ORCH] news material aggregate failed for %s: %s", pair, exc)
                return hit[0] if hit is not None else None
            if value is not None:
                cache[pair] = (value, now)
                failures.pop(pair, None)
            return value

    def get_news_impact(pair: str) -> float:
        sent = _cached_aggregate(pair)
        if sent is None or sent.sentiment_score is None:
            return 0.0
        return abs(sent.sentiment_score)

    def get_news_key(pair: str) -> str | None:
        sent = _cached_aggregate(pair)
        if sent is None:
            return None
        basis = sent.summary or f"score={sent.sentiment_score}"
        return str(hash(basis))

    return get_news_impact, get_news_key
```

ファイル冒頭に `logger = logging.getLogger(__name__)` が無ければ追加 (`import logging` も)。

- [ ] **Step 4: Update bootstrap caller**

`src/orchestrator/bootstrap.py:376` の `make_news_material_provider(config, store)` 呼び出しに TTL 引数を追加:

```python
        get_news_impact, get_news_key = make_news_material_provider(
            config, store,
            ttl_seconds=orch_cfg.entry.news_cache_ttl_seconds,
            negative_ttl_seconds=orch_cfg.entry.news_cache_negative_ttl_seconds,
            clock=db_now,
        )
```

- [ ] **Step 5: Run tests**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_news_material_provider_cache.py -v"`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/landing_providers.py src/orchestrator/bootstrap.py tests/test_news_material_provider_cache.py
git commit -m "feat(orchestrator): cache material news provider, propagate aggregate exceptions"
```

---

## Task 5: `PipelineResult` に reason/derived_rr を追加し全経路で設定

**Files:**
- Modify: `src/orchestrator/risk_gate.py:127-138` (`RiskGateResult` に `derived_rr`) + `pre_check` pass 経路
- Modify: `src/orchestrator/planning_pipeline.py:84-97` (dataclass) + 全 return 経路
- Test: `tests/test_planning_pipeline_result_contract.py` (新規) / `tests/test_risk_gate_worker.py` (追記)

**背景 (実コード確認済み):**
- reject 経路 (planning_pipeline.py:298/313/335) は**既に reason を設定済み** — Task 5b は「`_normalize_reason` を通す」+「derived_rr を流す」だけ。
- risk reject の reason には既に `derived rr X below min Y (claimed Z)` が入る (risk_gate.py:285)。
- `RiskGateResult` に `derived_rr` フィールドは無い → **正式追加する** (Task 5a-2)。pre_check の pass 経路で `derive_rr` を 1 回呼んで載せ、plan_create が RR をログに出せるようにする。

### Task 5a-1: reason 正規化ヘルパ + dataclass フィールド

- [ ] **Step 1: Write the failing test**

`tests/test_planning_pipeline_result_contract.py` (新規):

```python
from src.orchestrator.planning_pipeline import PipelineResult, _normalize_reason


def test_normalize_reason_strips_newlines_and_truncates():
    raw = "line1\nline2\r\n" + "x" * 500
    out = _normalize_reason(raw)
    assert "\n" not in out and "\r" not in out
    assert len(out) <= 200


def test_normalize_reason_handles_none():
    assert _normalize_reason(None) == ""


def test_pipeline_result_has_reason_and_derived_rr():
    r = PipelineResult(outcome="direct_hold", reason="no opportunity")
    assert r.reason == "no opportunity"
    assert r.derived_rr is None            # 既定 None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_planning_pipeline_result_contract.py -v"`
Expected: FAIL (`_normalize_reason` / `derived_rr` が無い)

- [ ] **Step 3: Add `derived_rr` field + `_normalize_reason`**

`src/orchestrator/planning_pipeline.py` の `PipelineResult` dataclass (planning_pipeline.py:84-97) の `reason: str | None = None` の後に追加:

```python
    derived_rr: float | None = None   # RR 導出済み経路のみ値・導出前 reject は None
```

module レベル (dataclass の後、`_pipeline` の前あたり) に追加:

```python
_REASON_MAX_LEN = 200


def _normalize_reason(reason: str | None) -> str:
    """ログ 1 行を壊さないよう改行除去 + 長さ制限する。"""
    if not reason:
        return ""
    flat = " ".join(str(reason).split())   # 改行・連続空白を単一空白へ
    return flat[:_REASON_MAX_LEN]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_planning_pipeline_result_contract.py -v"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/planning_pipeline.py tests/test_planning_pipeline_result_contract.py
git commit -m "feat(orchestrator): add PipelineResult.derived_rr + _normalize_reason helper"
```

### Task 5a-2: `RiskGateResult.derived_rr` + pre_check pass 経路で導出

- [ ] **Step 1: Write the failing test**

`tests/test_risk_gate_worker.py` に追記:

```python
def test_pre_check_pass_exposes_derived_rr(...):
    """pass 時、RiskGateResult.derived_rr に derive_rr の導出値が載る。

    (既存テストの pass fixture を流用。RR が min_rr 以上で通る draft/quote を使う。)
    """
    result = worker.pre_check(draft, ctx, include_executable_price=False)
    assert result.passed is True
    assert result.derived_rr is not None
    assert result.derived_rr >= worker._min_rr


def test_pre_check_reject_derived_rr_is_none_or_value(...):
    """reject 時 derived_rr は None (issues 文字列に既に RR が入るため冗長化しない)。"""
    result = worker.pre_check(draft_bad_rr, ctx, include_executable_price=False)
    assert result.passed is False
    assert result.derived_rr is None
```

(実引数は既存 `tests/test_risk_gate_worker.py` の pass/reject fixture に合わせる。)

- [ ] **Step 2: Run test to verify it fails**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_risk_gate_worker.py -k derived_rr -v"`
Expected: FAIL (`RiskGateResult` に `derived_rr` 属性が無い)

- [ ] **Step 3: Add field + populate on pass**

`src/orchestrator/risk_gate.py` の `RiskGateResult` (risk_gate.py:127-138) に追加:

```python
@dataclass
class RiskGateResult:
    passed: bool
    reject_class: str | None = None
    issues: list[str] = field(default_factory=list)
    derived_rr: float | None = None   # pass 時のみ導出 RR を載せる (reject は issues に文字列)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reject_class": self.reject_class,
            "issues": list(self.issues),
            "derived_rr": self.derived_rr,
        }
```

`pre_check` の pass 経路 (risk_gate.py:200) を次に変更 — pass 時に derive_rr を 1 回呼ぶ:

```python
        derived = derive_rr(
            draft, context.get("quote"),
            include_executable_price=include_executable_price,
        )
        return RiskGateResult(passed=True, reject_class=None, issues=[], derived_rr=derived)
```

- [ ] **Step 4: Run tests**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_risk_gate_worker.py -v"`
Expected: PASS (新 derived_rr テスト + 既存全 pass)

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/risk_gate.py tests/test_risk_gate_worker.py
git commit -m "feat(orchestrator): RiskGateResult exposes derived_rr on pass"
```

### Task 5b: 全 return 経路で reason 正規化 + derived_rr を流す

**実コード確認済みの現状:**
- reject 経路 (298/313/335) は既に reason 設定済み → `_normalize_reason` で包むだけ。
- plan_create (418) は `reason=final.reasoning_summary` 設定済み → `_normalize_reason` で包み + `derived_rr=risk.derived_rr` を追加。
- direct_hold (163/184) / failed (132/139) は reason 未設定 → 追加。
- risk reject (335) の derived_rr は None のまま (reason 文字列に既に RR あり)。scale-in reject も None。

- [ ] **Step 1: Write the failing test**

`tests/test_planning_pipeline_result_contract.py` に追加:

```python
def test_normalize_reason_is_idempotent_on_clean_input():
    assert _normalize_reason("plan created") == "plan created"
```

(全経路の reason 設定は既存 pipeline 統合テスト `tests/test_planning_pipeline.py` で PipelineResult を検証する。契約単体はここで固定。)

- [ ] **Step 2: Run test (baseline)**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_planning_pipeline_result_contract.py -v"`
Expected: PASS

- [ ] **Step 3: Wrap/add reason + derived_rr in every return path**

`src/orchestrator/planning_pipeline.py` の各 return:

1. **failed (132)**: `reason=_normalize_reason(f"fail-safe: {type(exc).__name__}: {exc}")` を追加。
2. **failed (139)**: `reason=_normalize_reason(f"unexpected: {type(exc).__name__}: {exc}")` を追加。
3. **direct_hold unavailable (163)**: `reason=_normalize_reason(f"{'/'.join(unavailable)} unavailable")` を追加。
4. **direct_hold no opportunity (184)**: `reason=_normalize_reason(opp.reasoning_summary or "no opportunity")` を追加。
5. **reject scale-in (254)**: 既存 reason を `_normalize_reason("scale-in without new_signal_evidence")` に、`derived_rr=None` 明示。
6. **reject planner (300)**: `reason=` を `_normalize_reason(f"planner reject: {final.reasoning_summary}")` に置換 (`_normalize_reason` で包む)。
7. **reject revise exhausted (315)**: `reason=` を `_normalize_reason(f"planner revise exhausted: {final.reasoning_summary}")` に置換。
8. **reject risk (335)**: `reason=` を `_normalize_reason(f"risk reject ({risk.reject_class}): {risk_reason}")` に置換。
9. **plan_create (414-420, `_commit_plan`)**: `reason=final.reasoning_summary` を `reason=_normalize_reason(final.reasoning_summary)` に置換し、`derived_rr=risk.derived_rr` を追加 (risk は pass した RiskGateResult なので derived_rr を持つ)。

- [ ] **Step 4: Run per-file tests**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_planning_pipeline.py tests/test_planning_pipeline_result_contract.py -v"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/planning_pipeline.py tests/test_planning_pipeline_result_contract.py
git commit -m "feat(orchestrator): normalize reason + flow derived_rr on all PipelineResult paths"
```

---

## Task 6: `pairs_to_plan` → `PlanningTarget(pair, triggers)`

**Files:**
- Modify: `src/orchestrator/material_landing.py:214-234` (`pairs_to_plan` + `PlanningTarget` 追加)
- Modify: `src/orchestrator/runtime.py:219` (呼び出し側)
- Test: `tests/test_material_landing_targets.py` (新規)

- [ ] **Step 1: Write the failing test**

`tests/test_material_landing_targets.py` (新規):

```python
from datetime import datetime, timedelta

from src.orchestrator.material_landing import MaterialLandingDetector, PlanningTarget


def _now():
    return datetime(2026, 7, 17, 0, 0, 0)


def _detector(**kw):
    """実 signature: keyword-only, get_latest_technical / material_bias_delta_min 必須。"""
    base = dict(
        get_latest_technical=lambda p: None,
        material_bias_delta_min=0.20,
        pairs=["EURUSD=X"],
        debounce_window_seconds=0,
        min_planning_interval_seconds=1800,
    )
    base.update(kw)
    return MaterialLandingDetector(**base)


def test_planning_target_cadence_when_no_material():
    """material 無し pair は periodic floor で triggers=() (cadence) として起動。"""
    det = _detector()
    targets = det.pairs_to_plan(_now())
    assert targets == [PlanningTarget(pair="EURUSD=X", triggers=())]


def test_planning_target_collects_multiple_triggers():
    """news + regime が同時に material なら triggers に両方入る (短絡しない)。"""
    now = _now()
    det = _detector(
        get_news_impact=lambda p: 0.9, material_news_impact_min=0.5,
        get_news_key=lambda p: "k1",
    )
    det.mark_regime("EURUSD=X", "active")        # normal→active で rank 上昇 push
    targets = det.pairs_to_plan(now)
    assert len(targets) == 1
    t = targets[0]
    assert t.pair == "EURUSD=X"
    assert "news" in t.triggers
    assert "regime" in t.triggers
```

(注: `MaterialLandingDetector.__init__` は keyword-only。`get_latest_technical` と `material_bias_delta_min` が必須。`mark_regime(pair, state)` は state 文字列 (normal/active/critical)。)

- [ ] **Step 2: Run test to verify it fails**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_material_landing_targets.py -v"`
Expected: FAIL (`PlanningTarget` が無い / `pairs_to_plan` が str list を返す)

- [ ] **Step 3: Add `PlanningTarget` + rewrite `pairs_to_plan`**

`src/orchestrator/material_landing.py` の import 付近 (ファイル上部) に追加:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class PlanningTarget:
    pair: str
    triggers: tuple[str, ...]   # ("news", "regime", ...) 空なら cadence floor 起因
```

`pairs_to_plan` (material_landing.py:214) を次に置き換える。個別 material 判定を集めて trigger 理由を作る (短絡しない):

```python
    def _material_triggers(self, pair: str) -> tuple[str, ...]:
        """該当する全 material 経路を集める (短絡しない)。空なら non-material。"""
        triggers: list[str] = []
        if self.technical_material(pair):
            triggers.append("technical")
        if self.news_material(pair):
            triggers.append("news")
        if self.event_window_material(pair):
            triggers.append("event")
        if self.regime_material(pair):
            triggers.append("regime")
        return tuple(triggers)

    def pairs_to_plan(self, now: datetime) -> list["PlanningTarget"]:
        out: list[PlanningTarget] = []
        for pair in self._pairs:
            triggers = self._material_triggers(pair)
            fire = False
            if triggers:
                # material 経路: debounce 窓を抜けたら起動。
                started = self._material_since.get(pair)
                if started is None:
                    self._material_since[pair] = now
                elif (now - started).total_seconds() >= self._debounce_window:
                    fire = True
            else:
                self._material_since.pop(pair, None)
                last = self._last_planned.get(pair)
                if last is None or (now - last).total_seconds() >= self._floor:
                    fire = True
            if fire:
                out.append(PlanningTarget(pair=pair, triggers=triggers))
        return out
```

(実コード確認済み: `technical_material` / `news_material` / `event_window_material` / `regime_material` は全て material_landing.py に存在する。上記 `_material_triggers` はそのまま使える。)

- [ ] **Step 4: Update runtime caller**

`src/orchestrator/runtime.py:219` の `target_pairs = self._detector.pairs_to_plan(now)` 以降を PlanningTarget 消費に変更。`for pair in target_pairs:` (runtime.py:222) を次のように target からpair と triggers を取り出す形にする:

```python
        if self._detector is not None:
            targets = self._detector.pairs_to_plan(now)
        else:
            # 後方互換: detector 未注入時は全 pair・trigger=cadence 扱い。
            from src.orchestrator.material_landing import PlanningTarget
            targets = [PlanningTarget(pair=p, triggers=()) for p in self._pairs]
        for target in targets:
            pair = target.pair
            trigger_label = "+".join(target.triggers) if target.triggers else "cadence"
            # ... 既存の start_run / try ブロックへ (pair は従来通り使う)
```

`trigger_label` は Task 7 の planning start ログで使う。ここでは変数を用意するだけ。

- [ ] **Step 5: Run tests**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_material_landing_targets.py tests/test_watch_loop_shadow.py -v"`
Expected: PASS (targets テスト + runtime の既存 planning テスト)

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/material_landing.py src/orchestrator/runtime.py tests/test_material_landing_targets.py
git commit -m "feat(orchestrator): pairs_to_plan returns PlanningTarget with trigger reasons"
```

---

## Task 7: planning start / result ログ + start:result 1:1 契約

**Files:**
- Modify: `src/orchestrator/runtime.py:211-281` (`run_planning_cycle`)
- Test: `tests/test_planning_cycle_logging.py` (新規)

- [ ] **Step 1: Write the failing test**

`tests/test_planning_cycle_logging.py` (新規)。既存の planning cycle テスト用ヘルパ (`tests/test_watch_loop_shadow.py` の seed パターン) を参照して runtime を組む。ログは caplog で検証:

```python
import logging

# 既存の runtime 構築ヘルパを流用する (test_watch_loop_shadow.py 等の seed パターンを参照)。
# ここでは擬似的に、各 outcome で start と result が 1:1 で出ることを検証する骨子を示す。


def _count(records, needle):
    return sum(1 for r in records if needle in r.getMessage())


def test_planning_start_and_result_are_one_to_one(caplog, planning_runtime_factory):
    """planning start 1 件につき result が 1 件対応する (正常 hold)。

    planning_runtime_factory は conftest / 既存ヘルパで用意する fixture。
    direct_hold を返す最小 pipeline stub を注入した runtime を返す。
    """
    rt = planning_runtime_factory(outcome="direct_hold")
    with caplog.at_level(logging.INFO):
        rt.run_planning_cycle()
    starts = _count(caplog.records, "[ORCH] planning start")
    results = _count(caplog.records, "[ORCH] planning result")
    assert starts >= 1
    assert starts == results


def test_planning_result_on_pipeline_failed(caplog, planning_runtime_factory):
    rt = planning_runtime_factory(outcome="failed")
    with caplog.at_level(logging.INFO):
        rt.run_planning_cycle()
    assert _count(caplog.records, "[ORCH] planning result") >= 1
    assert any("decision=failed" in r.getMessage() for r in caplog.records)


def test_planning_result_on_quote_failure(caplog, planning_runtime_factory):
    """pipeline 到達前 (quote provider 失敗) でも result が 1 件出る。"""
    rt = planning_runtime_factory(quote_raises=True)
    with caplog.at_level(logging.INFO):
        rt.run_planning_cycle()
    starts = _count(caplog.records, "[ORCH] planning start")
    results = _count(caplog.records, "[ORCH] planning result")
    assert starts == results
    assert any("decision=error" in r.getMessage() for r in caplog.records)
```

(注: `planning_runtime_factory` fixture は実装時に用意する。既存 `tests/test_taskf_live_execution_helpers.py` の `make_live_runtime` パターンを流用し、outcome を制御できる pipeline stub を注入する。fixture を conftest.py か本テストファイル内に定義する。)

- [ ] **Step 2: Run test to verify it fails**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_planning_cycle_logging.py -v"`
Expected: FAIL (planning start/result ログが出ない)

- [ ] **Step 3: Add start/result logging with 1:1 lifecycle**

`src/orchestrator/runtime.py` の `run_planning_cycle` (runtime.py:211-281) を、start 1 件に result 1 件が必ず対応する構造に変更する。Task 6 で用意した `trigger_label` を start ログに使う。各 pair ループ本体を次の骨子にする:

```python
        for target in targets:
            pair = target.pair
            trigger_label = "+".join(target.triggers) if target.triggers else "cadence"
            logger.info("[ORCH] planning start: pair=%s trigger=%s", pair, trigger_label)
            result_logged = False

            def _log_result(decision: str, reason: str = "", *, extra: str = "") -> None:
                nonlocal result_logged
                logger.info(
                    "[ORCH] planning result: pair=%s decision=%s%s%s",
                    pair, decision,
                    f" {extra}" if extra else "",
                    f" reason={reason}" if reason else "",
                )
                result_logged = True

            run_id = self._orch.start_run(...)   # 既存
            try:
                quote = self._quote_provider(pair)
                ctx = self._ctx.build(pair=pair, now=now, quote=quote)
                self._orch.attach_snapshot(run_id, ctx["snapshot_id"])
                if self._pipeline is not None:
                    result = asyncio.run(self._pipeline.run(pair=pair, context=ctx, run_id=run_id))
                    if result.outcome == "failed":
                        committed = False
                        self._orch.finish_run(run_id, status="failed",
                            error_type="PipelineFailed", error_message=result.error)
                        _log_result("failed", result.reason or (result.error or ""))
                    else:
                        self._orch.finish_run(run_id, status="ok")
                        self._notify_planning_result(pair, result)
                        rr = f"rr={result.derived_rr:.2f}" if result.derived_rr is not None else ""
                        extra = f"plan_id={result.plan_id}" if result.outcome == "plan_create" else ""
                        _log_result(result.outcome, result.reason or "",
                                    extra=" ".join(x for x in (extra, rr) if x))
                else:
                    self._orch.record_decision(..., reasoning_summary="phase1 observe: no planning agent wired yet", ...)
                    self._orch.finish_run(run_id, status="ok")
                    _log_result("direct_hold", "phase1 observe")
            except Exception as exc:
                logger.exception("[ORCH] planning cycle failed for %s", pair)
                self._orch.finish_run(run_id, status="failed",
                    error_type=type(exc).__name__, error_message=str(exc))
                if not result_logged:
                    _log_result("error", f"{type(exc).__name__}: {exc}")
            finally:
                if not result_logged:
                    # start は出したが result を出せていない経路の最終保険 (契約: start:result = 1:1)。
                    _log_result("error", "no result recorded")
                if self._detector is not None:
                    if committed:
                        self._detector.mark_committed(pair, now)
                    else:
                        self._detector.mark_attempted(pair, now)
```

(注: 既存の変数 `committed` の初期化・`start_run` 引数・`record_decision` 引数は現行コードを保持する。上記は差分の骨子で、既存の trace 記録 (attach_snapshot 等) を消さないこと。reason はログ内で改行を含まない前提 — PipelineResult.reason は Task 5 で正規化済み。)

- [ ] **Step 4: Run tests**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_planning_cycle_logging.py tests/test_watch_loop_shadow.py -v"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/runtime.py tests/test_planning_cycle_logging.py
git commit -m "feat(orchestrator): log planning start/result with 1:1 lifecycle guarantee"
```

---

## Task 8: フェーズ遷移 DEBUG 整理 + orphan 注記 (C)

**Files:**
- Modify: `src/orchestrator/planning_pipeline.py` (scan→draft→gate 遷移を DEBUG で 1 箇所ずつ)
- Doc: spec §4 の orphan 注記は既に spec に記録済み — 追加のコード変更なし

- [ ] **Step 1: Add DEBUG phase logs (no test — DEBUG は挙動でなく可視性)**

`src/orchestrator/planning_pipeline.py` の pipeline 主要フェーズ境界に DEBUG ログを 1 行ずつ足す (activity.log を汚さない・finance.log のみ)。既存の INFO ログは変えない:

```python
        logger.debug("[ORCH] phase scan: pair=%s", pair)
        # ... scan_opportunity の直前

        logger.debug("[ORCH] phase draft: pair=%s attempt=%d", pair, redraft_count + 1)
        # ... draft の直前 (ループ内)

        logger.debug("[ORCH] phase gate: pair=%s", pair)
        # ... risk pre_check の直前
```

- [ ] **Step 2: Verify DEBUG logs don't leak to activity**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_planning_pipeline.py -v"`
Expected: PASS (既存テストが緑・DEBUG 追加で挙動不変)

- [ ] **Step 3: Commit**

```bash
git add src/orchestrator/planning_pipeline.py
git commit -m "chore(orchestrator): add DEBUG phase-transition logs in planning pipeline"
```

---

## 最終確認 (全 Task 完了後)

- [ ] **関連 per-file テストを一括実行して回帰確認**

Run:
```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest \
  tests/test_logging_setup.py \
  tests/test_config_schema.py \
  tests/test_cached_news_provider.py \
  tests/test_context_builder_news.py \
  tests/test_news_material_provider_cache.py \
  tests/test_planning_pipeline.py \
  tests/test_planning_pipeline_result_contract.py \
  tests/test_material_landing_targets.py \
  tests/test_planning_cycle_logging.py \
  tests/test_watch_loop_shadow.py \
  -v"
```
Expected: 全 PASS。

- [ ] **spec との突き合わせ**: §2 (A-1〜A-5)・§3 (B / B-2 / 3.6 / 3.7)・§4 (C 注記) が各 Task でカバーされているか確認。

- [ ] **最終レビュー依頼** (requesting-code-review skill / codex): news キャッシュの失敗セマンティクス (stale-if-error) と planning start:result 1:1 契約を重点確認。
