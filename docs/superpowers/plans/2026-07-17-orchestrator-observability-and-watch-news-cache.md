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
| `config/settings.yaml.example` | config 正本例 | TTL 2 値 sync (Task 2) |
| `src/orchestrator/_ttl_cache.py` | generic TTL + single-flight + stale-if-error | 新設・Task 3/4 で共有 (Task 3-pre) |
| `src/orchestrator/context_builder.py` | §7 context 組立 + news provider | `make_cached_news_provider` (TtlSingleFlightCache 利用) / `_build_news` を status/as_of 対応 (Task 3) |
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

- [ ] **Step 5: Sync settings.yaml.example**

`config/settings.yaml.example` の `orchestrator.entry` ブロックに 2 値を追記 (キー名・階層は既存 entry 項目に合わせる):

```yaml
    news_cache_ttl_seconds: 60.0
    news_cache_negative_ttl_seconds: 30.0
```

- [ ] **Step 6: Run config sync + orchestrator config tests**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_config_schema.py tests/test_config_example_sync.py tests/test_orchestrator_config.py -k 'news_cache or example or entry' -v"`
Expected: PASS (example とスキーマの同期・orchestrator config ロード)

- [ ] **Step 7: Commit**

```bash
git add src/config/schema.py config/settings.yaml.example tests/test_config_schema.py
git commit -m "feat(config): add news_cache_ttl_seconds / news_cache_negative_ttl_seconds"
```

---

## Task 3-pre: generic TTL キャッシュヘルパ (Task 3/4 で共有)

**Files:**
- Create: `src/orchestrator/_ttl_cache.py`
- Test: `tests/test_ttl_cache.py` (新規)

**背景 (指摘6対応):** watch 用 (dict) と material 用 (NewsSentiment) で TTL/single-flight/stale-if-error を別実装すると重複しズレる。共通の `TtlSingleFlightCache` に切り出し、両者から使う。

- [ ] **Step 1: Write the failing test**

`tests/test_ttl_cache.py` (新規):

```python
import threading
import time
from datetime import datetime, timedelta

from src.orchestrator._ttl_cache import TtlSingleFlightCache


class _Clock:
    def __init__(self, start): self.now = start
    def __call__(self): return self.now
    def advance(self, s): self.now += timedelta(seconds=s)


def _cache(clock, ttl=60, neg=30):
    return TtlSingleFlightCache(ttl_seconds=ttl, negative_ttl_seconds=neg, clock=clock)


def test_ttl_hit_calls_producer_once():
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    calls = {"n": 0}
    def produce():
        calls["n"] += 1
        return "v"
    c = _cache(clock)
    assert c.get("k", produce).value == "v"
    clock.advance(30)
    c.get("k", produce)
    assert calls["n"] == 1


def test_ttl_expiry_recomputes():
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    calls = {"n": 0}
    def produce():
        calls["n"] += 1
        return "v"
    c = _cache(clock)
    c.get("k", produce)
    clock.advance(61)
    c.get("k", produce)
    assert calls["n"] == 2


def test_failure_ttl_suppresses_recompute():
    """失敗後 negative TTL 内は producer を呼ばない (計算とログ両方を止める)。"""
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    calls = {"n": 0}
    def produce():
        calls["n"] += 1
        raise RuntimeError("boom")
    c = _cache(clock)
    c.get("k", produce)
    clock.advance(10)
    c.get("k", produce)
    assert calls["n"] == 1


def test_stale_if_error_keeps_last_success():
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    state = {"fail": False}
    def produce():
        if state["fail"]:
            raise RuntimeError("boom")
        return "good"
    c = _cache(clock)
    ok = c.get("k", produce)
    assert ok.status == "ok"
    clock.advance(61)
    state["fail"] = True
    stale = c.get("k", produce)
    assert stale.status == "stale"
    assert stale.value == "good"
    assert stale.success_at == ok.success_at   # 成功時刻を維持


def test_unavailable_when_never_succeeded():
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    def produce():
        raise RuntimeError("boom")
    c = _cache(clock)
    r = c.get("k", produce)
    assert r.status == "unavailable"
    assert r.value is None
    assert r.success_at is None


def test_success_clears_failure_state():
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    calls = {"n": 0}
    state = {"fail": True}
    def produce():
        calls["n"] += 1
        if state["fail"]:
            raise RuntimeError("boom")
        return "v"
    c = _cache(clock)
    c.get("k", produce)           # 失敗 (n=1)
    clock.advance(31)
    state["fail"] = False
    c.get("k", produce)           # 成功 (n=2, failure クリア)
    clock.advance(61)
    state["fail"] = True
    c.get("k", produce)           # 再失敗 → producer 呼ぶ (n=3)
    assert calls["n"] == 3


def test_single_flight_concurrent_miss():
    """2 スレッド同時 miss でも producer は 1 回だけ。デッドロックしない設計。

    Barrier は producer の *外* (get 呼び出し直前) で同期する。producer 内では
    Event で 1 本目を停止し、2 本目が lock 待ちに入る猶予を与えてから release する。
    """
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    calls = {"n": 0}
    release = threading.Event()
    entered = threading.Event()
    def produce():
        calls["n"] += 1
        entered.set()             # 1 本目が producer に入った
        release.wait(timeout=2.0) # release されるまで lock を保持
        return "v"
    c = _cache(clock)
    results = {}
    def worker(name):
        results[name] = c.get("k", produce).value
    t1 = threading.Thread(target=worker, args=("a",))
    t1.start()
    entered.wait(timeout=2.0)     # 1 本目が lock+producer に入るまで待つ
    t2 = threading.Thread(target=worker, args=("b",))
    t2.start()                    # 2 本目は lock 待ちで停止 (producer に入らない)
    time.sleep(0.1)               # 2 本目が lock 待ちに入る猶予
    release.set()                 # 1 本目を進ませる
    t1.join(timeout=2.0); t2.join(timeout=2.0)
    assert not t1.is_alive() and not t2.is_alive()   # デッドロックしていない
    assert calls["n"] == 1                            # producer は 1 回だけ
    assert results == {"a": "v", "b": "v"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_ttl_cache.py -v"`
Expected: FAIL (`TtlSingleFlightCache` が無い)

- [ ] **Step 3: Implement `TtlSingleFlightCache`**

`src/orchestrator/_ttl_cache.py` (新規):

```python
"""pair (key) 単位の TTL + single-flight + stale-if-error キャッシュ。

watch 用 news provider (dict) と material 用 news provider (NewsSentiment) が
同一の失敗セマンティクスを共有するための汎用ヘルパ (spec §3.2/§3.4)。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CacheResult:
    """get() の返却。status = ok|stale|unavailable。"""
    value: Any                    # 成功値 (unavailable は None)
    status: str                   # "ok" | "stale" | "unavailable"
    success_at: datetime | None   # 最後に成功した時刻 (unavailable は None)


class TtlSingleFlightCache:
    def __init__(self, *, ttl_seconds: float, negative_ttl_seconds: float,
                 clock: Callable[[], datetime]) -> None:
        self._ttl = ttl_seconds
        self._neg_ttl = negative_ttl_seconds
        self._clock = clock
        self._cache: dict[str, tuple[Any, datetime]] = {}   # key -> (value, success_at)
        self._failures: dict[str, datetime] = {}            # key -> 直近失敗時刻
        self._locks: dict[str, Lock] = defaultdict(Lock)
        self._guard = Lock()

    def get(self, key: str, producer: Callable[[], Any]) -> CacheResult:
        now = self._clock()
        hit = self._cache.get(key)
        if hit is not None and (now - hit[1]).total_seconds() < self._ttl:
            return CacheResult(hit[0], "ok", hit[1])
        with self._guard:
            lock = self._locks[key]
        with lock:                                 # single-flight (producer は lock 外で待たない)
            hit = self._cache.get(key)
            if hit is not None and (now - hit[1]).total_seconds() < self._ttl:
                return CacheResult(hit[0], "ok", hit[1])
            failed_at = self._failures.get(key)
            if failed_at is not None and (now - failed_at).total_seconds() < self._neg_ttl:
                # failure TTL 内: producer を呼ばない (計算もログも止める)。
                return (CacheResult(hit[0], "stale", hit[1]) if hit is not None
                        else CacheResult(None, "unavailable", None))
            try:
                value = producer()
            except Exception as exc:
                self._failures[key] = now
                logger.warning("[ORCH] cache producer failed for %s: %s", key, exc)
                return (CacheResult(hit[0], "stale", hit[1]) if hit is not None
                        else CacheResult(None, "unavailable", None))
            self._cache[key] = (value, now)
            self._failures.pop(key, None)          # 成功で failure 解除
            return CacheResult(value, "ok", now)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_ttl_cache.py -v"`
Expected: PASS (7 tests・single-flight デッドロックなし)

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/_ttl_cache.py tests/test_ttl_cache.py
git commit -m "feat(orchestrator): add TtlSingleFlightCache (shared TTL + single-flight + stale-if-error)"
```

---

## Task 3: cached news provider (TtlSingleFlightCache 利用) + `_build_news` 対応

**Files:**
- Modify: `src/orchestrator/context_builder.py` (`make_cached_news_provider` 新設 / `_build_news` 変更)
- Modify: `src/orchestrator/bootstrap.py:213` (配線)
- Test: `tests/test_cached_news_provider.py` (新規)

### Task 3a: `make_cached_news_provider` (TtlSingleFlightCache 利用)

- [ ] **Step 1: Write the failing test**

`tests/test_cached_news_provider.py` (新規):

```python
from datetime import datetime, timedelta

from src.orchestrator.context_builder import make_cached_news_provider


class _Clock:
    def __init__(self, start): self.now = start
    def __call__(self): return self.now
    def advance(self, s): self.now += timedelta(seconds=s)


def _sentiment(score):
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
    assert calls["n"] == 1
    assert r1["status"] == "ok"
    assert r1["as_of"] is not None
    assert r2["sentiment_score"] == 0.5


def test_stale_if_error_keeps_last_success():
    """成功後 refresh 失敗で status=stale・as_of は成功時刻を維持。"""
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    state = {"fail": False}
    def inner(pair):
        if state["fail"]:
            raise RuntimeError("boom")
        return _sentiment(-0.6)
    p = make_cached_news_provider(inner, ttl_seconds=60, negative_ttl_seconds=30, clock=clock)
    ok = p("EURUSD=X")
    success_as_of = ok["as_of"]
    clock.advance(61)
    state["fail"] = True
    stale = p("EURUSD=X")
    assert stale["status"] == "stale"
    assert stale["sentiment_score"] == -0.6
    assert stale["as_of"] == success_as_of


def test_unavailable_when_never_succeeded():
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    def inner(pair):
        raise RuntimeError("boom")
    p = make_cached_news_provider(inner, ttl_seconds=60, negative_ttl_seconds=30, clock=clock)
    r = p("EURUSD=X")
    assert r["status"] == "unavailable"
    assert r["sentiment_score"] is None
    assert r["as_of"] is None
```

(single-flight / failure TTL / 成功解除の網羅は Task 3-pre `tests/test_ttl_cache.py` でカバー済み。ここは news dict への status/as_of 写しだけ検証する。)

- [ ] **Step 2: Run test to verify it fails**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_cached_news_provider.py -v"`
Expected: FAIL (`make_cached_news_provider` が存在しない)

- [ ] **Step 3: Implement `make_cached_news_provider`**

`src/orchestrator/context_builder.py` の module 末尾 (`make_news_provider` の後) に追加。import に `from src.orchestrator._ttl_cache import TtlSingleFlightCache` / `from datetime import datetime` / `from typing import Callable` を (不足あれば) 足す:

```python
def make_cached_news_provider(
    inner: NewsProvider,
    *,
    ttl_seconds: float,
    negative_ttl_seconds: float,
    clock: Callable[[], datetime],
) -> NewsProvider:
    """news_provider を TtlSingleFlightCache でラップし、返却 dict に status/as_of を付ける。

    status: ok (新規成功) | stale (refresh 失敗で前回値) | unavailable (成功履歴なし)。
    as_of は成功時刻の ISO (unavailable は None)。stale でも as_of は成功時刻を維持する
    (現在時刻で上書きしない — news_conflict の失効を live trigger で消さないため, spec §3.2)。
    """
    cache = TtlSingleFlightCache(
        ttl_seconds=ttl_seconds, negative_ttl_seconds=negative_ttl_seconds, clock=clock,
    )

    def provider(pair: str) -> dict:
        res = cache.get(pair, lambda: inner(pair))
        if res.status == "unavailable":
            return {"sentiment_score": None, "confidence": None,
                    "top_reasons": [], "status": "unavailable", "as_of": None}
        as_of = res.success_at.isoformat() if res.success_at is not None else None
        return {**res.value, "status": res.status, "as_of": as_of}

    return provider
```

- [ ] **Step 4: Run test to verify it passes**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_cached_news_provider.py -v"`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/context_builder.py tests/test_cached_news_provider.py
git commit -m "feat(orchestrator): cached news provider via TtlSingleFlightCache"
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
    # observed_at は datetime を渡す (QuoteSnapshot 側で扱う型に合わせる — .isoformat 呼び出しは
    # _enrich_ages 側が行うため、build/_build_news 経路では datetime を渡す)。
    return QuoteSnapshot(bid=150.0, ask=150.02, mid=150.01, spread=0.02,
                         source="test", observed_at=now)


def _direct_build_news(builder, pair, now):
    """_build_news を直接呼んで _ref/status/as_of を検証する (private だが契約検証のため)。"""
    return builder._build_news(pair, now)


def test_build_news_stale_keeps_provider_as_of(tmp_path):
    """provider の stale as_of を _ref にそのまま使う (now で上書きしない)。"""
    now = datetime(2026, 7, 17, 0, 5, 0)
    past = "2026-07-17T00:00:00"
    def provider(pair):
        return {"sentiment_score": -0.6, "confidence": 0.8,
                "top_reasons": ["x"], "status": "stale", "as_of": past}
    b = _builder(tmp_path, provider)
    news = _direct_build_news(b, "EURUSD=X", now)
    assert news["sentiment_score"] == -0.6           # stale 値が通る (news_conflict 生存)
    assert news["_ref"]["status"] == "stale"
    assert news["_ref"]["as_of"] == past             # 成功時刻を維持 (now でない)


def test_build_news_unavailable_keeps_ref_marker(tmp_path):
    """unavailable でも _ref に status を残す (取得失敗の監査痕跡)。"""
    now = datetime(2026, 7, 17, 0, 5, 0)
    def provider(pair):
        return {"sentiment_score": None, "confidence": None,
                "top_reasons": [], "status": "unavailable", "as_of": None}
    b = _builder(tmp_path, provider)
    news = _direct_build_news(b, "EURUSD=X", now)
    assert news["sentiment_score"] is None           # fail-open (§3.7)
    assert news["_ref"] is not None
    assert news["_ref"]["status"] == "unavailable"
    assert news["_ref"]["as_of"] is None


def test_build_news_ok_uses_success_as_of(tmp_path):
    now = datetime(2026, 7, 17, 0, 5, 0)
    at = "2026-07-17T00:05:00"
    def provider(pair):
        return {"sentiment_score": 0.3, "confidence": 0.7,
                "top_reasons": [], "status": "ok", "as_of": at}
    b = _builder(tmp_path, provider)
    news = _direct_build_news(b, "EURUSD=X", now)
    assert news["_ref"]["status"] == "ok"
    assert news["_ref"]["as_of"] == at
```

- [ ] **Step 2: Run test to verify it fails**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_context_builder_news.py -v"`
Expected: FAIL (現状 `_build_news` は `_ref` に status を持たず、unavailable で `_ref=None` にする)

- [ ] **Step 3: Modify `_build_news`**

`src/orchestrator/context_builder.py` の `_build_news` (現在 context_builder.py:256-279) を次に置き換える。provider の status/as_of を `_ref` に残し、unavailable も監査痕跡として `_ref` を保存する (指摘2対応):

```python
    def _build_news(self, pair: str, now: datetime) -> dict[str, Any]:
        """注入された news_provider から §7 news ブロックを組む。

        provider 未注入なら従来の空 news に倒す (後方互換)。cached provider は
        例外を投げず status (ok|stale|unavailable) + as_of 付き dict を返すため、
        as_of をそのまま _ref に使い (now で上書きしない)、status を _ref に残す。
        unavailable も _ref に status を残す (取得失敗の監査痕跡・spec §3.6)。
        provider が直接例外を投げるケース (非 cached provider) は従来通り空 news に倒す。
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
            "_ref": {"source": "rag_aggregate", "as_of": as_of, "status": status},
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


def test_material_failure_ttl_suppresses_recompute(monkeypatch):
    """失敗後 negative TTL 内は aggregate を再度呼ばない (計算・ログ抑止)。"""
    clock = _Clock(datetime(2026, 7, 17, 0, 0, 0))
    calls = {"n": 0}
    def fake_aggregate(inst, store, config):
        calls["n"] += 1
        raise RuntimeError("boom")
    monkeypatch.setattr(
        "src.analysis.news_aggregator.aggregate_news_sentiment", fake_aggregate
    )
    impact, key = make_news_material_provider(
        _config(), store=object(), ttl_seconds=60,
        negative_ttl_seconds=30, clock=clock,
    )
    impact("EURUSD=X")                    # 1 回目失敗 (calls=1)
    clock.advance(10)
    key("EURUSD=X")                       # negative TTL 内 → aggregate 呼ばない
    assert calls["n"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_news_material_provider_cache.py -v"`
Expected: FAIL (`make_news_material_provider` が ttl_seconds 引数を受けない)

- [ ] **Step 3: Rewrite `make_news_material_provider` (TtlSingleFlightCache 共有)**

`src/orchestrator/landing_providers.py` の `make_news_material_provider` (現在 landing_providers.py:100-135) を次に置き換える。`_aggregate` の try/except を削除して例外をキャッシュ層に伝搬させ、TTL/single-flight/stale-if-error は Task 3-pre の `TtlSingleFlightCache` を共有する (指摘6)。import に `from src.orchestrator._ttl_cache import TtlSingleFlightCache` / `from datetime import datetime` / `from typing import Callable` を足す:

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

    集計は TtlSingleFlightCache で共有キャッシュする (watch 経路と同一失敗セマンティクス)。
    _aggregate は例外を握り潰さずキャッシュ層へ伝搬させる (stale-if-error を迂回しない)。
    material 経路は失敗時 (unavailable) に impact=0.0 / key=None を維持する。stale は
    直近成功 NewsSentiment を返す。
    """
    from src.analysis.news_aggregator import aggregate_news_sentiment

    by_symbol = {inst.symbol: inst for inst in config.instruments}
    cache = TtlSingleFlightCache(
        ttl_seconds=ttl_seconds, negative_ttl_seconds=negative_ttl_seconds, clock=clock,
    )

    def _produce(pair: str):
        """NewsSentiment を返す。pair 未登録は None を成功値として扱う (集計対象外)。
        aggregate_news_sentiment の例外はそのまま送出しキャッシュ層が stale/unavailable に変換。"""
        inst = by_symbol.get(pair)
        if inst is None:
            return None
        return aggregate_news_sentiment(inst, store, config)

    def _sentiment(pair: str):
        """cache 経由で NewsSentiment (or None) を得る。unavailable は None。"""
        res = cache.get(pair, lambda: _produce(pair))
        if res.status == "unavailable":
            return None
        return res.value                          # ok / stale とも成功 NewsSentiment (or None)

    def get_news_impact(pair: str) -> float:
        sent = _sentiment(pair)
        if sent is None or sent.sentiment_score is None:
            return 0.0
        return abs(sent.sentiment_score)

    def get_news_key(pair: str) -> str | None:
        sent = _sentiment(pair)
        if sent is None:
            return None
        basis = sent.summary or f"score={sent.sentiment_score}"
        return str(hash(basis))

    return get_news_impact, get_news_key
```

(注: `_produce` が `None` を返す (pair 未登録) 場合、TtlSingleFlightCache はそれを成功値としてキャッシュする — 例外ではないため。集計対象外の pair は毎回 None で問題ない。実 pair の集計例外のみが failure TTL の対象になる。)

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

### Task 5a-2: `RiskGateResult.derived_rr` + pre_check で 1 回導出し pass/reject に載せる

**spec 契約 (§150):** `derived_rr` は plan_create (pass) **と risk reject の両方**で「RR を導出できたら」値を持つ。structural reject と導出前 reject (scale-in) だけ None。→ `pre_check` で `derive_rr` を 1 回だけ計算し、判定と `RiskGateResult` の両方に使う (指摘5)。

- [ ] **Step 1: Write the failing test**

`tests/test_risk_gate_worker.py` に追記:

```python
def test_pre_check_pass_exposes_derived_rr(...):
    """pass 時、RiskGateResult.derived_rr に derive_rr の導出値が載る。"""
    result = worker.pre_check(draft, ctx, include_executable_price=False)
    assert result.passed is True
    assert result.derived_rr is not None
    assert result.derived_rr >= worker._min_rr


def test_pre_check_rr_reject_exposes_derived_rr(...):
    """RR 起因の fixable reject でも derived_rr に導出値が載る (spec §150)。

    (RR が min_rr 未満で通らない draft/quote を使う。)
    """
    result = worker.pre_check(draft_low_rr, ctx, include_executable_price=False)
    assert result.passed is False
    assert result.reject_class == "fixable"
    assert result.derived_rr is not None            # 導出できたので値あり
    assert result.derived_rr < worker._min_rr


def test_pre_check_structural_reject_derived_rr_none(...):
    """structural reject は RR を導出しない (halt 等) → derived_rr=None。"""
    result = worker.pre_check(draft, ctx_halted, include_executable_price=False)
    assert result.passed is False
    assert result.reject_class == "structural"
    assert result.derived_rr is None
```

(実引数は既存 `tests/test_risk_gate_worker.py` の pass / low-rr reject / structural(halt) fixture に合わせる。)

- [ ] **Step 2: Run test to verify it fails**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_risk_gate_worker.py -k derived_rr -v"`
Expected: FAIL (`RiskGateResult` に `derived_rr` 属性が無い)

- [ ] **Step 3: Add field + compute derive_rr once in pre_check**

`src/orchestrator/risk_gate.py` の `RiskGateResult` (risk_gate.py:127-138) に `derived_rr` を追加:

```python
@dataclass
class RiskGateResult:
    passed: bool
    reject_class: str | None = None
    issues: list[str] = field(default_factory=list)
    derived_rr: float | None = None   # RR を導出できた経路 (pass / RR起因 fixable reject) で値・structural/導出前は None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reject_class": self.reject_class,
            "issues": list(self.issues),
            "derived_rr": self.derived_rr,
        }
```

`pre_check` (risk_gate.py:189-200) を、structural 判定後に `derive_rr` を 1 回計算し、fixable 判定にその値を渡しつつ結果にも載せる形に変更する。現状 `_fixable_issues` 内部で derive_rr を呼んでいる (risk_gate.py:274) が、これを pre_check 側で 1 回計算して `_fixable_issues` に渡す (二重呼び出しを避ける):

```python
        structural = self._structural_issues(context)
        if structural:
            # structural は RR 判定前に確定 → derived_rr は載せない。
            return RiskGateResult(passed=False, reject_class=STRUCTURAL, issues=structural)

        # RR を 1 回だけ導出し、fixable 判定と結果表示で共有する (spec §150)。
        derived = derive_rr(
            draft, context.get("quote"),
            include_executable_price=include_executable_price,
        )
        fixable = self._fixable_issues(
            draft, context, include_executable_price=include_executable_price,
            derived_rr=derived,
        )
        if fixable:
            # derived が計算できていれば載せる (RR起因/その他 fixable いずれも導出は試みた)。
            return RiskGateResult(
                passed=False, reject_class=FIXABLE, issues=fixable, derived_rr=derived,
            )

        return RiskGateResult(passed=True, reject_class=None, issues=[], derived_rr=derived)
```

`_fixable_issues` (risk_gate.py:225) の signature に `derived_rr: float | None` を追加し、内部の `derived = derive_rr(...)` 呼び出し (risk_gate.py:274-277) を引数 `derived_rr` の使用に置き換える (再計算しない):

```python
    def _fixable_issues(
        self, draft, context: dict[str, Any], *,
        include_executable_price: bool, derived_rr: float | None,
    ) -> list[str]:
        ...
        # RR: 引数で受けた derived_rr を使う (pre_check が 1 回計算済み)。
        derived = derived_rr
        if derived is None:
            if sl is not None and tp is not None:
                issues.append("rr underivable (no entry candidate)")
        elif derived < self._min_rr:
            claimed = action.get("rr")
            issues.append(
                f"derived rr {derived:.2f} below min {self._min_rr}"
                f" (claimed {claimed if claimed is not None else 'none'})"
            )
        ...
```

(注: `_fixable_issues` の他の issue 判定 (SL/TP side・spread 等) は現行のまま。derive_rr の内部呼び出し 1 箇所だけを引数使用に置換する。derived が None のときは reject でも derived_rr=None になる = 導出不能 reject。RR 起因で min 未満なら値が載る。)

- [ ] **Step 4: Run tests**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_risk_gate_worker.py -v"`
Expected: PASS (新 derived_rr テスト 3 本 + 既存全 pass)

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/risk_gate.py tests/test_risk_gate_worker.py
git commit -m "feat(orchestrator): RiskGateResult.derived_rr computed once, exposed on pass and RR reject"
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
5. **reject scale-in (254)**: 既存 reason を `_normalize_reason("scale-in without new_signal_evidence")` に、`derived_rr=None` 明示 (RR 導出前の reject)。
6. **reject planner (300)**: `reason=` を `_normalize_reason(f"planner reject: {final.reasoning_summary}")` に置換。derived_rr は None (risk gate 前の reject)。
7. **reject revise exhausted (315)**: `reason=` を `_normalize_reason(f"planner revise exhausted: {final.reasoning_summary}")` に置換。derived_rr は None (risk gate 前)。
8. **reject risk (335)**: `reason=` を `_normalize_reason(f"risk reject ({risk.reject_class}): {risk_reason}")` に置換し、`derived_rr=risk.derived_rr` を追加 (risk は fixable reject の RiskGateResult なので RR 起因なら値を持つ・spec §150)。
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
    """news + regime が同時に material なら triggers に両方入る (短絡しない)。

    debounce_window_seconds=0 でも初回呼び出しは窓を開始するだけで fire しない。
    一度呼んで窓を開き、次の評価 (>= 窓) で fire させる。
    """
    now = _now()
    det = _detector(
        get_news_impact=lambda p: 0.9, material_news_impact_min=0.5,
        get_news_key=lambda p: "k1",
    )
    det.mark_regime("EURUSD=X", "active")        # normal→active で rank 上昇 push
    det.pairs_to_plan(now)                        # 窓開始 (fire しない)
    targets = det.pairs_to_plan(now + timedelta(seconds=1))   # 窓を抜けて fire
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

- [ ] **Step 5: Update existing tests that assume `list[str]` (指摘3)**

API 変更で `pairs_to_plan` が `list[PlanningTarget]` を返すため、既存テストを追従させる:

1. `tests/test_material_landing.py` — `assert det.pairs_to_plan(...) == ["USDJPY=X"]` 形式の全 assert (148/150/152/167/169/182/185/187/204/208/210/211 等) を `PlanningTarget` 比較に直す。ヘルパを足す:
```python
from src.orchestrator.material_landing import PlanningTarget

def _pairs(targets):
    """PlanningTarget list → pair 文字列 list (既存 assert の互換用)。"""
    return [t.pair for t in targets]
```
各 assert を `assert _pairs(det.pairs_to_plan(_t(200))) == ["USDJPY=X"]` の形に置換する。

2. `tests/test_market_state_detector.py:216-217` — `landing.pairs_to_plan(...)` の戻りを同様に `_pairs(...)` で包んで比較:
```python
assert _pairs(landing.pairs_to_plan(NOW + timedelta(seconds=1))) == ["USDJPY=X"]
```
(同ファイルに `_pairs` ヘルパと `PlanningTarget` import を足す。)

3. `tests/test_orchestrator_runtime.py:331-337` — `_FakeDetector.pairs_to_plan` を `PlanningTarget` を返すよう変更:
```python
        def pairs_to_plan(self, now):
            from src.orchestrator.material_landing import PlanningTarget
            return [PlanningTarget(pair=p, triggers=()) for p in self.fire]
```
同ファイル内に他の stub detector (383/423 行の `pairs_to_plan`) があれば同様に PlanningTarget を返すよう直す。

- [ ] **Step 6: Run tests**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_material_landing_targets.py tests/test_material_landing.py tests/test_market_state_detector.py tests/test_orchestrator_runtime.py tests/test_watch_loop_shadow.py -v"`
Expected: PASS (新 targets テスト + 追従した既存テスト全て)

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/material_landing.py src/orchestrator/runtime.py \
  tests/test_material_landing_targets.py tests/test_material_landing.py \
  tests/test_market_state_detector.py tests/test_orchestrator_runtime.py
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

- [ ] **Step 3: Remove duplicate `📋 plan created` log**

`src/orchestrator/runtime.py` の `_notify_planning_result` (runtime.py:1210-1216) の `📋 plan created` INFO ログブロックを削除する (通知本体 L1218 以降は残す)。plan_create の可視化は Task 7 の統一 result ログに一本化する (二重回避・指摘4):

```python
    def _notify_planning_result(self, pair: str, result: "PipelineResult") -> None:
        """plan_create / reject を shadow 通知する。direct_hold/failed は通知しない。

        plan 作成の可視ログは run_planning_cycle の統一 result ログに一本化した
        (旧 📋 plan created INFO はここから削除)。本メソッドは通知のみ担う。
        """
        if self._notifier is None:
            return
        # ... 以降 (PlanCreatedInfo / notify_plan_created / notify_plan_rejected) は既存のまま
```

- [ ] **Step 4: Add start/result logging with 1:1 lifecycle**

`src/orchestrator/runtime.py` の `run_planning_cycle` (runtime.py:211-281) を、start 1 件に result 1 件が必ず対応する構造に変更する。**start ログは `start_run()` 成功後に出す** (start_run が DB エラーで落ちたら start ログも出ない = 1:1 維持・指摘4)。`run_id=None` で外側 try に包み、finally で result 保険を打つ。Task 6 の `trigger_label` を使う:

```python
        for target in targets:
            pair = target.pair
            trigger_label = "+".join(target.triggers) if target.triggers else "cadence"
            run_id = None
            committed = False              # 既存初期化を維持
            result_logged = False

            def _log_result(decision: str, reason: str = "", *, extra: str = "") -> None:
                nonlocal result_logged
                # reason は PipelineResult 経由なら正規化済み。例外経路の生 reason も
                # ここで改行除去する (1 行を壊さない・指摘4)。
                flat = " ".join(str(reason).split())[:200] if reason else ""
                logger.info(
                    "[ORCH] planning result: pair=%s decision=%s%s%s",
                    pair, decision,
                    f" {extra}" if extra else "",
                    f" reason={flat}" if flat else "",
                )
                result_logged = True

            try:
                run_id = self._orch.start_run(...)   # 既存引数を保持
                # start_run 成功後に start ログ (失敗時は start も出ない → 1:1 維持)。
                logger.info("[ORCH] planning start: pair=%s trigger=%s", pair, trigger_label)
                quote = self._quote_provider(pair)
                ctx = self._ctx.build(pair=pair, now=now, quote=quote)
                self._orch.attach_snapshot(run_id, ctx["snapshot_id"])
                committed = True           # snapshot 到達 (既存セマンティクス)
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
                        extra = ""
                        if result.outcome == "plan_create":
                            extra = f"plan_id={result.plan_id} dir={result.direction or '?'}"
                        _log_result(result.outcome, result.reason or "",
                                    extra=" ".join(x for x in (extra, rr) if x))
                else:
                    self._orch.record_decision(..., reasoning_summary="phase1 observe: no planning agent wired yet", ...)
                    self._orch.finish_run(run_id, status="ok")
                    _log_result("direct_hold", "phase1 observe")
            except Exception as exc:
                logger.exception("[ORCH] planning cycle failed for %s", pair)
                if run_id is not None:
                    self._orch.finish_run(run_id, status="failed",
                        error_type=type(exc).__name__, error_message=str(exc))
                if not result_logged:
                    _log_result("error", f"{type(exc).__name__}: {exc}")
            finally:
                if not result_logged and run_id is not None:
                    # start は出したが result を出せていない経路の最終保険 (契約: start:result = 1:1)。
                    _log_result("error", "no result recorded")
                if self._detector is not None:
                    if committed:
                        self._detector.mark_committed(pair, now)
                    else:
                        self._detector.mark_attempted(pair, now)
```

**契約の要点:** start ログは start_run 成功直後にのみ出る。start_run が落ちれば start も result も出ず (1:1 は「start が出たら result も出る」の意味で保たれる)。start 後に落ちた全経路 (quote/build/pipeline failed/例外) は except または finally で result を出す。

(注: 既存の `start_run` 引数・`record_decision` 引数・trace 記録 (attach_snapshot 等) は現行コードを保持し消さない。`committed` の遷移は既存セマンティクスに合わせる — 上記は差分骨子。)

- [ ] **Step 5: Run tests**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd ~/project/finance && uv run pytest tests/test_planning_cycle_logging.py tests/test_watch_loop_shadow.py -v"`
Expected: PASS

- [ ] **Step 6: Commit**

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
  tests/test_config_example_sync.py \
  tests/test_orchestrator_config.py \
  tests/test_ttl_cache.py \
  tests/test_cached_news_provider.py \
  tests/test_context_builder_news.py \
  tests/test_news_material_provider_cache.py \
  tests/test_risk_gate_worker.py \
  tests/test_planning_pipeline.py \
  tests/test_planning_pipeline_result_contract.py \
  tests/test_material_landing_targets.py \
  tests/test_material_landing.py \
  tests/test_market_state_detector.py \
  tests/test_orchestrator_runtime.py \
  tests/test_planning_cycle_logging.py \
  tests/test_watch_loop_shadow.py \
  -v"
```
Expected: 全 PASS。フル suite は順序フレークを持つため per-file で判定する。

- [ ] **spec との突き合わせ**: §2 (A-1〜A-5)・§3 (B / B-2 / 3.6 / 3.7)・§4 (C 注記) が各 Task でカバーされているか確認。

- [ ] **最終レビュー依頼** (requesting-code-review skill / codex): news キャッシュの失敗セマンティクス (stale-if-error) と planning start:result 1:1 契約を重点確認。
