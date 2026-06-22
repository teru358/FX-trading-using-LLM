# Orchestrator Phase 2/D — quote-stream producer + ポジション保護移設 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** watch loop を polling producer 直読へ刷新し、本番ポジション保護を同じ quote stream を消費する watch 側 worker へ移設する。あわせて bridge `/quote` の bid/ask で spread を実値化する。

**Architecture:** finance 側のみ改修 (bridge=Windows 側は無改修)。`Mt5OhlcvFetcher.get_quote` で `/quote` を叩き、`QuoteStreamProducer` (daemon) が trade pairs を短周期 polling して最新 `QuoteSnapshot` を in-memory dict に保持。watch は producer 直読。保護は純関数を流用した `PriceProtectionWorker` へ移設し、`tick_migration_stage` 単調 4 段 enum (`off → producer → protect_shadow → protect_live`) で段階導入。

**Tech Stack:** Python 3, threading (daemon worker), httpx (既存 bridge client), SQLAlchemy ORM (orchestrator_store), pytest。

**Spec:** `docs/superpowers/specs/2026-06-22-orchestrator-phase2-quote-stream-protection-migration-design.md`

---

## File Structure

**新規作成:**
- `src/data/quote_stream.py` — `QuoteStreamProducer` (daemon polling producer) + `latest(pair)`。
- `src/orchestrator/position_protection_worker.py` — `PriceProtectionWorker` (保護判定の tick 駆動、純関数流用)。
- `tests/test_mt5_get_quote.py` — get_quote 単体。
- `tests/test_quote_stream.py` — producer 単体。
- `tests/test_watch_quote_provider_switch.py` — watch の provider 切替。
- `tests/test_protection_worker.py` — 保護 worker。
- `tests/test_protection_decisions_store.py` — store API。

**変更:**
- `src/data/mt5_ohlcv_fetcher.py` — `Quote` dataclass + `get_quote` + スカラ用 naive-local 正規化 helper。
- `src/config/schema.py` — `OrchestratorConfig.tick_migration_stage` / `quote_stream_poll_seconds`。
- `src/data/orchestrator_store.py` — `_ProtectionDecision` ORM + `record_protection_decision` / `compare_protection_decisions`。
- `src/orchestrator/bootstrap.py` — producer 生成 + stage に応じた quote_provider 差し替え + worker 配線。
- `src/orchestrator/runtime.py` — producer/worker のライフサイクル (start/stop) 組み込み。
- `src/jobs/price_monitor.py` — `protect_shadow` 以上のとき `source="price_monitor"` で判定を記録する薄い追記。
- `config/settings.yaml.example` — 新 config キーの記載。

---

# Phase D-1a: get_quote + producer + watch 直読 + spread 実値化

shadow 内。本番保護に触れない。

## Task 1: config — `tick_migration_stage` / `quote_stream_poll_seconds`

**Files:**
- Modify: `src/config/schema.py` (`OrchestratorConfig`)
- Test: `tests/test_config_schema.py` (既存があれば追記、無ければ新規)

- [ ] **Step 1: Write the failing test**

`tests/test_orchestrator_config_tick_stage.py` を新規作成:

```python
from src.config.schema import OrchestratorConfig


def test_tick_migration_stage_defaults_off():
    cfg = OrchestratorConfig()
    assert cfg.tick_migration_stage == "off"
    assert cfg.quote_stream_poll_seconds == 2


def test_tick_migration_stage_accepts_known_values():
    for stage in ("off", "producer", "protect_shadow", "protect_live"):
        cfg = OrchestratorConfig(tick_migration_stage=stage)
        assert cfg.tick_migration_stage == stage
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_orchestrator_config_tick_stage.py -v`
Expected: FAIL — `OrchestratorConfig` に `tick_migration_stage` 属性が無い (`TypeError: unexpected keyword argument` or `AttributeError`)。

- [ ] **Step 3: Write minimal implementation**

`src/config/schema.py` の `OrchestratorConfig` (dataclass) にフィールドを追加。既存フィールド群の末尾に (既存の `market_state_enabled: bool = False` 等の近く):

```python
    # Phase 2/D: tick migration 段階導入。off→producer→protect_shadow→protect_live の単調列。
    # producer 以上で quote-stream producer 起動 + watch 直読。protect_shadow 以上で保護 worker 起動。
    tick_migration_stage: str = "off"
    quote_stream_poll_seconds: int = 2
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_orchestrator_config_tick_stage.py -v`
Expected: PASS (2 passed)。

- [ ] **Step 5: Commit**

```bash
git add tests/test_orchestrator_config_tick_stage.py src/config/schema.py
git commit -m "feat: tick_migration_stage / quote_stream_poll_seconds config 追加 (Phase 2/D)"
```

---

## Task 2: `Mt5OhlcvFetcher.get_quote` — bid/ask quote 取得

**Files:**
- Modify: `src/data/mt5_ohlcv_fetcher.py` (`Quote` dataclass + `_bridge_time_to_local_naive` スカラ版 + `get_quote`)
- Test: `tests/test_mt5_get_quote.py`

**Note:** `Mt5OhlcvFetcher.__init__` は `bridge_url` / `request_timeout` / `api_key` を取り `self._url` / `self._timeout` / `self._headers` を持つ (`mt5_ohlcv_fetcher.py:72-81`)。`to_mt5_symbol` は `src.trading.symbol_mapping` に既存 import 済み (`mt5_ohlcv_fetcher.py:18`)。`Mt5UnreachableError` は同ファイル定義済み。

- [ ] **Step 1: Write the failing test**

`tests/test_mt5_get_quote.py` を新規作成:

```python
from datetime import datetime

import httpx
import pytest

from src.data.mt5_ohlcv_fetcher import Mt5OhlcvFetcher, Mt5UnreachableError


class _FakeResp:
    def __init__(self, status_code: int, json_body: dict | None = None) -> None:
        self.status_code = status_code
        self._json = json_body or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "err", request=None, response=None
            )

    def json(self) -> dict:
        return self._json


def _fetcher() -> Mt5OhlcvFetcher:
    return Mt5OhlcvFetcher(
        bridge_url="http://localhost:8812", request_timeout=5.0, api_key=""
    )


def test_get_quote_returns_bid_ask_mid_spread(monkeypatch):
    captured = {}

    def fake_get(url, params=None, timeout=None, headers=None):
        captured["url"] = url
        return _FakeResp(
            200,
            {
                "symbol": "USDJPY",
                "bid": 150.000,
                "ask": 150.020,
                "spread_points": 20,
                "time": "2026-06-22T00:00:00+00:00",  # UTC aware
            },
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    q = _fetcher().get_quote("USDJPY=X")

    # symbol 変換: URL は MT5 形式 (=X 除去)
    assert captured["url"].endswith("/quote/USDJPY")
    # spread は価格差 (ask - bid)、pips ではない
    assert q.spread == pytest.approx(0.020)
    assert q.mid == pytest.approx(150.010)
    assert q.bid == pytest.approx(150.000)
    assert q.ask == pytest.approx(150.020)
    assert q.source == "mt5"


def test_get_quote_observed_at_is_naive_local(monkeypatch):
    def fake_get(url, params=None, timeout=None, headers=None):
        return _FakeResp(
            200,
            {
                "symbol": "USDJPY", "bid": 150.0, "ask": 150.02,
                "spread_points": 20, "time": "2026-06-22T00:00:00+00:00",
            },
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    q = _fetcher().get_quote("USDJPY=X")

    # observed_at は naive (tzinfo を剥がした local 値)。aware だと runtime が
    # naive now との引き算で TypeError → quote_age_sec=None → 全 block。
    assert isinstance(q.observed_at, datetime)
    assert q.observed_at.tzinfo is None


def test_get_quote_unreachable_raises(monkeypatch):
    def fake_get(url, params=None, timeout=None, headers=None):
        return _FakeResp(503)

    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(Mt5UnreachableError):
        _fetcher().get_quote("USDJPY=X")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mt5_get_quote.py -v`
Expected: FAIL — `Mt5OhlcvFetcher` に `get_quote` が無い (`AttributeError`)。

- [ ] **Step 3: Write minimal implementation**

`src/data/mt5_ohlcv_fetcher.py` に追加。まず import に `ZoneInfo` 不要 (既存 `astimezone` で local 取得)。`_bridge_times_to_local_naive` の下にスカラ版を追加:

```python
def _bridge_time_to_local_naive(iso_str: str) -> datetime:
    """bridge の UTC aware ISO 時刻 1 件を DB 規約 (naive machine-local) へ変換。

    _bridge_times_to_local_naive の単一値版。aware のまま QuoteSnapshot に入れると
    runtime が naive now との引き算で TypeError → quote_age_sec=None → freshness wall
    が全 block する (spec §3 H1)。
    """
    aware = datetime.fromisoformat(iso_str)
    if aware.tzinfo is None:
        # 既に naive ならそのまま (bridge 仕様変更への保険)
        return aware
    local_tz = datetime.now().astimezone().tzinfo
    return aware.astimezone(local_tz).replace(tzinfo=None)
```

`Mt5UnreachableError` クラス定義の下あたりに `Quote` dataclass を追加 (ファイル冒頭で `from dataclasses import dataclass` を import):

```python
@dataclass
class Quote:
    """bridge /quote 由来のリアルタイム bid/ask quote。

    spread は価格差 (ask-bid)。spread_pips は診断/ログ用 (watch には渡さない、spec §4.5)。
    observed_at は DB 規約 naive local。
    """
    bid: float
    ask: float
    mid: float
    spread: float          # ask - bid (価格差)
    spread_pips: float     # 診断用
    observed_at: datetime
    source: str = "mt5"
```

`fetch_current_price` メソッドの下に `get_quote` を追加:

```python
    def get_quote(self, symbol: str) -> "Quote":
        """bridge /quote/{symbol} からリアルタイム bid/ask を取得する (spec §3)。

        symbol は to_mt5_symbol で MT5 形式に変換 (bridge は文字列をそのまま
        symbol_select に使うため変換漏れは 404)。MT5 未接続 (503/404) は
        Mt5UnreachableError に倒す。
        """
        from src.orchestrator.watch_evaluator import _pip_size_for

        mt5_symbol = to_mt5_symbol(symbol)
        try:
            resp = httpx.get(
                f"{self._url}/quote/{mt5_symbol}",
                timeout=self._timeout, headers=self._headers,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise Mt5UnreachableError(
                f"bridge /quote failed for {symbol}: {exc}"
            ) from exc
        data = resp.json()
        bid = float(data["bid"])
        ask = float(data["ask"])
        spread = ask - bid
        pip = _pip_size_for(symbol)
        return Quote(
            bid=bid, ask=ask, mid=(bid + ask) / 2.0,
            spread=spread, spread_pips=spread / pip,
            observed_at=_bridge_time_to_local_naive(data["time"]),
            source="mt5",
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_mt5_get_quote.py -v`
Expected: PASS (3 passed)。

- [ ] **Step 5: Commit**

```bash
git add tests/test_mt5_get_quote.py src/data/mt5_ohlcv_fetcher.py
git commit -m "feat: Mt5OhlcvFetcher.get_quote — bridge /quote で bid/ask 取得 (spec §3)"
```

---

## Task 3: `QuoteStreamProducer` — polling producer

**Files:**
- Create: `src/data/quote_stream.py`
- Test: `tests/test_quote_stream.py`

**Note:** `QuoteSnapshot` は `src.orchestrator.context_builder` の dataclass (`bid, ask, mid, spread: float|None, source, observed_at`)。`db_now` は `src.utils.clock`。

- [ ] **Step 1: Write the failing test**

`tests/test_quote_stream.py` を新規作成:

```python
from datetime import datetime, timedelta

from src.data.quote_stream import QuoteStreamProducer
from src.data.mt5_ohlcv_fetcher import Mt5UnreachableError, Quote


class _FakeFetcher:
    def __init__(self, quotes: dict[str, Quote]) -> None:
        self._quotes = quotes
        self.calls: list[str] = []

    def get_quote(self, pair: str) -> Quote:
        self.calls.append(pair)
        q = self._quotes.get(pair)
        if q is None:
            raise Mt5UnreachableError(f"no quote for {pair}")
        return q


class _FakePriceProvider:
    """/ohlcv fallback 用 (mid only, spread=None)。"""
    def __init__(self, price: float) -> None:
        self._price = price

    def get_current_price(self, pair: str):
        from src.data.price_fetcher import CurrentPrice
        return CurrentPrice(price=self._price, timestamp=datetime.now(), source="yfinance")


def _quote(bid: float, ask: float) -> Quote:
    return Quote(
        bid=bid, ask=ask, mid=(bid + ask) / 2, spread=ask - bid,
        spread_pips=(ask - bid) / 0.01, observed_at=datetime.now(), source="mt5",
    )


def test_poll_once_populates_latest_from_quote():
    fetcher = _FakeFetcher({"USDJPY=X": _quote(150.00, 150.02)})
    prod = QuoteStreamProducer(
        pairs=["USDJPY=X"], fetcher=fetcher,
        price_provider=_FakePriceProvider(150.0), mt5_enabled=True,
        poll_seconds=2,
    )
    prod.poll_once()
    snap = prod.latest("USDJPY=X")
    assert snap is not None
    assert snap.spread == 0.02  # 価格差 (ask-bid)
    assert snap.source == "mt5"
    assert snap.observed_at.tzinfo is None


def test_quote_failure_degrades_to_ohlcv_with_spread_none():
    fetcher = _FakeFetcher({})  # 常に Mt5UnreachableError
    prod = QuoteStreamProducer(
        pairs=["USDJPY=X"], fetcher=fetcher,
        price_provider=_FakePriceProvider(150.0), mt5_enabled=True,
        poll_seconds=2,
    )
    prod.poll_once()
    snap = prod.latest("USDJPY=X")
    assert snap is not None
    assert snap.spread is None       # degrade: spread 不明 → 安全側
    assert snap.bid == snap.ask == snap.mid == 150.0


def test_provider_exception_keeps_old_snapshot():
    fetcher = _FakeFetcher({"USDJPY=X": _quote(150.00, 150.02)})
    prod = QuoteStreamProducer(
        pairs=["USDJPY=X"], fetcher=fetcher,
        price_provider=_FakePriceProvider(150.0), mt5_enabled=True,
        poll_seconds=2,
    )
    prod.poll_once()
    old = prod.latest("USDJPY=X")

    # 以後 get_quote も price_provider も例外 → 最新値を更新しない
    def boom(pair):
        raise RuntimeError("explode")
    fetcher.get_quote = boom
    prod._price_provider.get_current_price = boom  # type: ignore[attr-defined]
    prod.poll_once()

    assert prod.latest("USDJPY=X") is old  # 古い snapshot がそのまま残る


def test_latest_unknown_pair_is_none():
    prod = QuoteStreamProducer(
        pairs=["USDJPY=X"], fetcher=_FakeFetcher({}),
        price_provider=_FakePriceProvider(150.0), mt5_enabled=True, poll_seconds=2,
    )
    assert prod.latest("EURUSD=X") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_quote_stream.py -v`
Expected: FAIL — `src.data.quote_stream` が無い (`ModuleNotFoundError`)。

- [ ] **Step 3: Write minimal implementation**

`src/data/quote_stream.py` を新規作成:

```python
"""quote-stream producer (spec §4)。trade pairs を短周期 polling し最新 quote を保持する。

MT5 enabled なら bridge /quote (bid/ask 実値、spread=ask-bid) を引く。失敗 / MT5 非対象は
/ohlcv 等の get_current_price (mid only, spread=None) へ degrade。取得例外時は最新値を
更新せず、古い observed_at を残す (watch の freshness wall が stale を検知して止める)。
"""
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from src.data.mt5_ohlcv_fetcher import Mt5UnreachableError
from src.orchestrator.context_builder import QuoteSnapshot
from src.utils.clock import db_now

if TYPE_CHECKING:
    from src.data.mt5_ohlcv_fetcher import Mt5OhlcvFetcher
    from src.data.price_provider import PriceProvider

logger = logging.getLogger(__name__)


class QuoteStreamProducer:
    def __init__(
        self, *, pairs: list[str], fetcher: "Mt5OhlcvFetcher | None",
        price_provider: "PriceProvider", mt5_enabled: bool, poll_seconds: int,
    ) -> None:
        self._pairs = list(pairs)
        self._fetcher = fetcher
        self._price_provider = price_provider
        self._mt5_enabled = mt5_enabled
        self._poll_seconds = poll_seconds
        self._lock = threading.Lock()
        self._latest: dict[str, QuoteSnapshot] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _build_snapshot(self, pair: str) -> QuoteSnapshot:
        """1 pair の最新 QuoteSnapshot を作る。例外は呼び出し側へ伝播。"""
        if self._mt5_enabled and self._fetcher is not None:
            try:
                q = self._fetcher.get_quote(pair)
                return QuoteSnapshot(
                    bid=q.bid, ask=q.ask, mid=q.mid, spread=q.spread,
                    source=q.source, observed_at=q.observed_at,
                )
            except Mt5UnreachableError:
                pass  # degrade to /ohlcv
        cp = self._price_provider.get_current_price(pair)
        observed = cp.timestamp or db_now()
        return QuoteSnapshot(
            bid=cp.price, ask=cp.price, mid=cp.price, spread=None,
            source=cp.source, observed_at=observed,
        )

    def poll_once(self) -> None:
        """全 pair を 1 回 poll する。pair 単位の取得失敗は最新値を更新しないだけ。"""
        for pair in self._pairs:
            try:
                snap = self._build_snapshot(pair)
            except Exception:
                logger.exception("[QUOTE-STREAM] build snapshot failed for %s", pair)
                continue  # 最新値を更新しない (古い observed_at が残る)
            with self._lock:
                self._latest[pair] = snap

    def latest(self, pair: str) -> "QuoteSnapshot | None":
        with self._lock:
            return self._latest.get(pair)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(timeout=self._poll_seconds)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="quote-stream", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_quote_stream.py -v`
Expected: PASS (4 passed)。

- [ ] **Step 5: Commit**

```bash
git add src/data/quote_stream.py tests/test_quote_stream.py
git commit -m "feat: QuoteStreamProducer — polling producer + /ohlcv degrade (spec §4)"
```

---

## Task 4: bootstrap で producer 生成 + watch quote_provider 差し替え

**Files:**
- Modify: `src/orchestrator/bootstrap.py` (`build_orchestrator_runtime`)
- Modify: `src/orchestrator/runtime.py` (producer のライフサイクル組み込み)
- Test: `tests/test_watch_quote_provider_switch.py`

**Note:** `build_orchestrator_runtime` は `bootstrap.py:109` 定義、`make_quote_provider(price_provider)` を `bootstrap.py:163` で生成し `quote_provider=` で `OrchestratorRuntime` に渡す (`bootstrap.py:175-180`)。runtime の `start()` は `runtime.py:772`、各 worker を stage/注入で条件起動する既存パターンがある (`_mstate_thread` 等)。runtime `__init__` は `quote_provider` を `self._quote_provider` に保持 (`runtime.py:65,79`)。

- [ ] **Step 1: Write the failing test**

`tests/test_watch_quote_provider_switch.py` を新規作成:

```python
from datetime import datetime

from src.data.quote_stream import QuoteStreamProducer
from src.orchestrator.context_builder import QuoteSnapshot


class _StubProducer:
    def __init__(self, snap):
        self._snap = snap
        self.start_called = False
        self.stop_called = False

    def latest(self, pair):
        return self._snap

    def start(self):
        self.start_called = True

    def stop(self):
        self.stop_called = True


def _snap():
    return QuoteSnapshot(
        bid=150.0, ask=150.02, mid=150.01, spread=0.02,
        source="mt5", observed_at=datetime.now(),
    )


def test_make_producer_quote_provider_reads_latest():
    from src.orchestrator.bootstrap import make_producer_quote_provider

    prod = _StubProducer(_snap())
    fallback_called = []

    def fallback(pair):
        fallback_called.append(pair)
        return _snap()

    provider = make_producer_quote_provider(prod, fallback)
    snap = provider("USDJPY=X")
    assert snap.source == "mt5"
    assert fallback_called == []  # latest が非 None なら fallback を呼ばない


def test_make_producer_quote_provider_falls_back_when_latest_none():
    from src.orchestrator.bootstrap import make_producer_quote_provider

    prod = _StubProducer(None)  # まだ poll 未完了
    fallback_called = []

    def fallback(pair):
        fallback_called.append(pair)
        return _snap()

    provider = make_producer_quote_provider(prod, fallback)
    snap = provider("USDJPY=X")
    assert snap is not None
    assert fallback_called == ["USDJPY=X"]  # None のとき従来 fetch にフォールバック
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_watch_quote_provider_switch.py -v`
Expected: FAIL — `make_producer_quote_provider` が `bootstrap` に無い (`ImportError`)。

- [ ] **Step 3: Write minimal implementation**

`src/orchestrator/bootstrap.py` の `make_quote_provider` の下に追加:

```python
def make_producer_quote_provider(producer, fallback: "QuoteProvider") -> QuoteProvider:
    """producer.latest を読む quote provider。latest が None (起動直後の過渡期) のみ
    従来 fetch にフォールバックする (spec §4.4)。"""

    def provider(pair: str) -> QuoteSnapshot:
        snap = producer.latest(pair)
        if snap is None:
            return fallback(pair)
        return snap

    return provider
```

次に `build_orchestrator_runtime` 内で stage を読んで producer を生成・差し替え。`quote_provider = make_quote_provider(price_provider)` (`bootstrap.py:163`) の直後に追加:

```python
    # Phase 2/D: tick_migration_stage が producer 以上なら quote-stream producer を立て
    # watch を producer 直読に切り替える (spec §4.4)。off は従来 fetch 維持。
    quote_producer = None
    stage = getattr(orch_cfg, "tick_migration_stage", "off")
    if stage in ("producer", "protect_shadow", "protect_live"):
        from src.data.quote_stream import QuoteStreamProducer

        mt5_fetcher = getattr(price_provider, "_mt5_fetcher", None)
        mt5_enabled = getattr(price_provider, "_mt5_enabled", False)
        quote_producer = QuoteStreamProducer(
            pairs=pairs, fetcher=mt5_fetcher, price_provider=price_provider,
            mt5_enabled=mt5_enabled,
            poll_seconds=getattr(orch_cfg, "quote_stream_poll_seconds", 2),
        )
        quote_provider = make_producer_quote_provider(quote_producer, quote_provider)
```

そして `OrchestratorRuntime(...)` 呼び出しに `quote_producer=quote_producer` を渡す引数を追加 (runtime 側で受ける)。`runtime.py` の `__init__` に `quote_producer=None` キーワードを足し `self._quote_producer = quote_producer` を保持。`start()` の冒頭 (loops 起動前) に:

```python
        if self._quote_producer is not None:
            self._quote_producer.start()
```

`stop()` の最後に:

```python
        if self._quote_producer is not None:
            self._quote_producer.stop()
```

> `orch_cfg` は `build_orchestrator_runtime` 内の OrchestratorConfig 変数。実際の変数名はファイルを確認し合わせる (`config.orchestrator` 等)。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_watch_quote_provider_switch.py -v`
Expected: PASS (2 passed)。

- [ ] **Step 5: Run broader regression**

Run: `pytest tests/test_orchestrator_bootstrap.py tests/test_orchestrator_e2e.py -v`
Expected: PASS (既存 bootstrap/e2e が壊れていない = `off` 既定で従来経路維持)。

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/bootstrap.py src/orchestrator/runtime.py tests/test_watch_quote_provider_switch.py
git commit -m "feat: stage>=producer で watch を producer 直読に切替 (spec §4.4)"
```

---

## Task 5: producer 経由 quote で quote_age_sec が算出される回帰ガード (H1)

**Files:**
- Test: `tests/test_quote_stream.py` (追記)

**Note:** runtime `_enrich_ages` は `now - datetime.fromisoformat(observed)` で age を出す (`runtime.py:490`)。producer の `observed_at` は datetime オブジェクトだが、context_builder が isoformat して snapshot に保存 → watch 時に parse される。ここでは「naive datetime 同士の引き算が成功する」ことを直接検証する (aware が混ざると TypeError → None)。

- [ ] **Step 1: Write the failing test**

`tests/test_quote_stream.py` に追記:

```python
def test_producer_snapshot_observed_at_subtractable_with_naive_now():
    """producer の observed_at が naive なので naive now と引き算でき age が出る (H1 回帰)。"""
    from src.utils.clock import db_now

    fetcher = _FakeFetcher({"USDJPY=X": _quote(150.00, 150.02)})
    prod = QuoteStreamProducer(
        pairs=["USDJPY=X"], fetcher=fetcher,
        price_provider=_FakePriceProvider(150.0), mt5_enabled=True, poll_seconds=2,
    )
    prod.poll_once()
    snap = prod.latest("USDJPY=X")
    age = (db_now() - snap.observed_at).total_seconds()  # TypeError なら H1 退行
    assert age >= 0
```

- [ ] **Step 2: Run test to verify it fails (or passes if Quote already correct)**

Run: `pytest tests/test_quote_stream.py::test_producer_snapshot_observed_at_subtractable_with_naive_now -v`
Expected: PASS (Task 2 で naive 正規化済みなら通る)。**もし FAIL するなら Task 2 の `_bridge_time_to_local_naive` を見直す** (aware が漏れている)。これは H1 のガードテストなので、ここで通ることを確認するのが目的。

- [ ] **Step 3: (実装変更不要 — ガードテストのみ)**

実装は Task 2 で完了済み。このタスクは回帰ガードの追加のみ。

- [ ] **Step 4: Commit**

```bash
git add tests/test_quote_stream.py
git commit -m "test: producer observed_at の naive 性を H1 回帰ガード"
```

---

## Task 6: settings.yaml.example と D-1a 通し確認

**Files:**
- Modify: `config/settings.yaml.example`

- [ ] **Step 1: settings.yaml.example に新キーを記載**

`config/settings.yaml.example` の orchestrator セクションに追記 (既存 `market_state_enabled` 等の近く):

```yaml
  # Phase 2/D: tick migration 段階導入 (off→producer→protect_shadow→protect_live)。
  # producer 以上で quote-stream producer 起動 + watch 直読 + spread 実値化。
  tick_migration_stage: "off"
  quote_stream_poll_seconds: 2
```

- [ ] **Step 2: D-1a 全テスト + フルスイート回帰**

Run: `pytest tests/test_mt5_get_quote.py tests/test_quote_stream.py tests/test_watch_quote_provider_switch.py tests/test_orchestrator_config_tick_stage.py -v`
Expected: PASS (全て)。

Run: `pytest -q`
Expected: 既存全 pass + 新規分。失敗ゼロ。

- [ ] **Step 3: Commit**

```bash
git add config/settings.yaml.example
git commit -m "docs: settings.yaml.example に tick_migration_stage 追記 (D-1a)"
```

---

# Phase D-2a: 保護 worker (記録のみ) + protection_decisions + 並走比較

shadow 内。実クローズ/SL更新しない。

## Task 7: `protection_decisions` ORM + store API

**Files:**
- Modify: `src/data/orchestrator_store.py` (`_ProtectionDecision` ORM + `record_protection_decision` + `compare_protection_decisions`)
- Test: `tests/test_protection_decisions_store.py`

**Note:** orchestrator_store は `_Base` / `_get_engine` を `price_store` から import (`orchestrator_store.py:21`)。ORM model は `Column(...)` で定義し `__tablename__` を持つ (例 `_DecisionSnapshot` `orchestrator_store.py:45-55`)。`create_all` は `OrchestratorStore.__init__` 内で呼ばれている (既存パターン)。`db_now` import 済み。Session の使い方は既存 record メソッドに倣う。

- [ ] **Step 1: Write the failing test**

`tests/test_protection_decisions_store.py` を新規作成:

```python
from datetime import timedelta
from pathlib import Path

from src.data.orchestrator_store import OrchestratorStore
from src.utils.clock import db_now


def test_record_and_compare_protection_decisions(tmp_path: Path):
    orch = OrchestratorStore(tmp_path / "orch.db")
    now = db_now()

    # 同一 order_id・近接 ts で両 source が同じ判定 → 一致
    orch.record_protection_decision(
        ts=now, pair="USDJPY=X", order_id="o1", source="price_monitor",
        action="raise_sl", stage="breakeven", target_sl=150.0,
        mfe_r=0.6, giveback_r=0.1,
    )
    orch.record_protection_decision(
        ts=now + timedelta(seconds=1), pair="USDJPY=X", order_id="o1",
        source="tick_worker", action="raise_sl", stage="breakeven",
        target_sl=150.0, mfe_r=0.6, giveback_r=0.1,
    )

    rows = orch.compare_protection_decisions(since=now - timedelta(minutes=5))
    assert len(rows) == 1
    r = rows[0]
    assert r["order_id"] == "o1"
    assert r["action_match"] is True
    assert r["target_sl_match"] is True


def test_compare_detects_mismatch(tmp_path: Path):
    orch = OrchestratorStore(tmp_path / "orch.db")
    now = db_now()
    orch.record_protection_decision(
        ts=now, pair="USDJPY=X", order_id="o2", source="price_monitor",
        action="none", stage=None, target_sl=None, mfe_r=0.1, giveback_r=0.0,
    )
    orch.record_protection_decision(
        ts=now, pair="USDJPY=X", order_id="o2", source="tick_worker",
        action="raise_sl", stage="half", target_sl=149.5, mfe_r=0.4, giveback_r=0.0,
    )
    rows = orch.compare_protection_decisions(since=now - timedelta(minutes=5))
    assert len(rows) == 1
    assert rows[0]["action_match"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_protection_decisions_store.py -v`
Expected: FAIL — `record_protection_decision` が無い (`AttributeError`)。

- [ ] **Step 3: Write minimal implementation**

`src/data/orchestrator_store.py` の ORM model 群の末尾 (他の `class _XXX(_Base)` の後) に追加:

```python
class _ProtectionDecision(_Base):
    """保護判定の記録 (price_monitor / tick_worker 並走比較用、spec §5.4)。"""
    __tablename__ = "protection_decisions"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    ts         = Column(DateTime, nullable=False, index=True)
    pair       = Column(String, nullable=False, index=True)
    order_id   = Column(String, nullable=False, index=True)
    source     = Column(String, nullable=False)  # price_monitor | tick_worker
    action     = Column(String, nullable=False)  # none | raise_sl | close
    stage      = Column(String)                  # half | breakeven | lock | null
    target_sl  = Column(Float)
    mfe_r      = Column(Float)
    giveback_r = Column(Float)
```

`OrchestratorStore` クラスのメソッド群に追加 (既存 record メソッドの Session 使い方に合わせる):

```python
    def record_protection_decision(
        self, *, ts, pair: str, order_id: str, source: str,
        action: str, stage: "str | None", target_sl: "float | None",
        mfe_r: "float | None", giveback_r: "float | None",
    ) -> None:
        """保護判定を 1 件記録する (spec §5.4)。実クローズ/SL更新とは独立。"""
        with Session(self._engine) as session:
            session.add(_ProtectionDecision(
                ts=ts, pair=pair, order_id=order_id, source=source,
                action=action, stage=stage, target_sl=target_sl,
                mfe_r=mfe_r, giveback_r=giveback_r,
            ))
            session.commit()

    def compare_protection_decisions(self, *, since) -> list[dict]:
        """同 order_id の price_monitor / tick_worker 判定をペアリングし一致を返す。

        ts が近接する両 source のうち各 order_id の最新ペアを比較する簡易版。
        """
        with Session(self._engine) as session:
            rows = session.execute(
                select(_ProtectionDecision)
                .where(_ProtectionDecision.ts >= since)
                .order_by(_ProtectionDecision.ts)
            ).scalars().all()

        by_key: dict[str, dict[str, _ProtectionDecision]] = {}
        for r in rows:
            by_key.setdefault(r.order_id, {})[r.source] = r  # 最新が後勝ち

        out: list[dict] = []
        for order_id, srcs in by_key.items():
            pm = srcs.get("price_monitor")
            tw = srcs.get("tick_worker")
            if pm is None or tw is None:
                continue  # 片側のみは比較対象外
            out.append({
                "order_id": order_id,
                "action_match": pm.action == tw.action,
                "target_sl_match": pm.target_sl == tw.target_sl,
                "price_monitor_action": pm.action,
                "tick_worker_action": tw.action,
            })
        return out
```

`self._engine` の参照名は既存メソッドに合わせる (orchestrator_store が engine をどう保持しているか確認して一致させる)。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_protection_decisions_store.py -v`
Expected: PASS (2 passed)。

- [ ] **Step 5: Commit**

```bash
git add src/data/orchestrator_store.py tests/test_protection_decisions_store.py
git commit -m "feat: protection_decisions ORM + record/compare API (spec §5.4 M5)"
```

---

## Task 8: `PriceProtectionWorker` — 保護判定 (記録のみ / raise_sl のみ)

**Files:**
- Create: `src/orchestrator/position_protection_worker.py`
- Test: `tests/test_protection_worker.py`

**Note:** 純関数は `src.trading.position_protection` の `compute_mfe_update(pos, current) -> ProtectionStateUpdate` (fields: `max_favorable_r`, `current_r`, `giveback_r`, `max_favorable_price`) と `compute_profit_protection_action(pos, current, cfg) -> ProtectionAction` (fields: `action`, `target_sl`, `stage`, `reason`)。**worker は `action="close"` を実行しない (spec §5.1.1)。** `protect_shadow` は記録のみ。

**重要 — 実 `Order` を使う:** 純関数は `Order` の `direction`(`"buy"`/`"sell"`、`"long"` ではない)、`initial_risk_price_distance`、`max_favorable_r: float`、`initial_stop_loss`、`entry_price` を参照する (`position_protection.py:30-58`)。SimpleNamespace で模すと属性欠落で `AttributeError` になるため、**テストでは実 `Order.new(...)` を使う**。`Order` は `src.trading.position_manager`。`Order.new(pair, direction, entry_price, stop_loss, ...)` が `initial_risk_price_distance` 等を自動計算する (`position_manager.py:62-70`)。

- [ ] **Step 1: Write the failing test**

`tests/test_protection_worker.py` を新規作成:

```python
from datetime import datetime
from types import SimpleNamespace

from src.orchestrator.position_protection_worker import PriceProtectionWorker
from src.trading.position_manager import Order


class _RecordingStore:
    def __init__(self):
        self.records = []

    def record_protection_decision(self, **kw):
        self.records.append(kw)


def _cfg():
    return SimpleNamespace(
        protect_half_r=0.3, protect_breakeven_r=0.5, protect_lock_r=1.0,
        giveback_close_r=0.4, giveback_close_min_mfe_r=0.8,
    )


def _pos(*, entry=150.0, sl=149.0, take_profit=152.0, max_fav_r=0.0,
         max_fav_price=None) -> Order:
    # 実 Order を使う (純関数は direction='buy'/'sell' と initial_risk_price_distance 等を要求)。
    o = Order.new(
        pair="USDJPY=X", direction="buy", entry_price=entry,
        stop_loss=sl, take_profit=take_profit, position_size=1.0,
    )
    o.max_favorable_r = max_fav_r
    o.max_favorable_price = max_fav_price
    return o


class _Producer:
    def __init__(self, mid):
        self._mid = mid

    def latest(self, pair):
        from src.orchestrator.context_builder import QuoteSnapshot
        return QuoteSnapshot(
            bid=self._mid, ask=self._mid, mid=self._mid, spread=None,
            source="mt5", observed_at=datetime.now(),
        )


def test_shadow_records_decision_without_executing():
    store = _RecordingStore()
    broker_calls = []
    worker = PriceProtectionWorker(
        producer=_Producer(mid=150.6),  # entry 150 / sl 149 → +0.6R 相当
        position_provider=lambda: [_pos()],
        store=store, cfg=_cfg(),
        broker=SimpleNamespace(update_remote_sl=lambda *a, **k: broker_calls.append(a)),
        mode="protect_shadow",
    )
    worker.run_once()
    assert len(store.records) == 1
    assert store.records[0]["source"] == "tick_worker"
    assert broker_calls == []  # protect_shadow は実 SL 更新しない


def test_close_action_is_not_executed():
    """giveback で action=close になっても worker は実行しない (H4)。"""
    store = _RecordingStore()
    broker_calls = []
    # MFE 1.0R を既に記録済み (max_fav_price=150.0+1.0R=151.0) で、現在 150.1 まで
    # 戻すと giveback ≈ 0.9R ≥ giveback_close_r(0.4) かつ MFE 1.0R ≥ min(0.8) → close。
    pos = _pos(max_fav_r=1.0, max_fav_price=151.0)
    worker = PriceProtectionWorker(
        producer=_Producer(mid=150.1),
        position_provider=lambda: [pos],
        store=store, cfg=_cfg(),
        broker=SimpleNamespace(update_remote_sl=lambda *a, **k: broker_calls.append(a)),
        mode="protect_shadow",
    )
    worker.run_once()
    # close でも shadow なので実行ゼロ。記録はされてよい。
    assert broker_calls == []


def test_off_or_producer_mode_does_nothing():
    store = _RecordingStore()
    worker = PriceProtectionWorker(
        producer=_Producer(mid=150.6),
        position_provider=lambda: [_pos()],
        store=store, cfg=_cfg(), broker=None, mode="producer",
    )
    worker.run_once()
    assert store.records == []  # producer 段では保護 worker は何もしない
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_protection_worker.py -v`
Expected: FAIL — `position_protection_worker` が無い (`ModuleNotFoundError`)。

- [ ] **Step 3: Write minimal implementation**

`src/orchestrator/position_protection_worker.py` を新規作成:

```python
"""tick 駆動のポジション保護 worker (spec §5)。

純関数 (compute_mfe_update / compute_profit_protection_action) を流用し駆動だけ
tick 化する。close は price_monitor と同じく実行しない (spec §5.1.1, H4)。
- protect_shadow: 判定を protection_decisions に記録のみ (実 SL 更新なし)。
- protect_live: raise_sl のみ broker.update_remote_sl で実行 (close は別タスク)。
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from src.trading.position_protection import (
    compute_mfe_update, compute_profit_protection_action,
)
from src.utils.clock import db_now

logger = logging.getLogger(__name__)


class PriceProtectionWorker:
    def __init__(
        self, *, producer, position_provider: Callable[[], list],
        store, cfg, broker, mode: str, poll_seconds: int = 2,
    ) -> None:
        self._producer = producer
        self._positions = position_provider
        self._store = store
        self._cfg = cfg
        self._broker = broker
        self._mode = mode  # producer | protect_shadow | protect_live
        self._poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None

    def run_once(self) -> None:
        if self._mode not in ("protect_shadow", "protect_live"):
            return
        for pos in self._positions():
            snap = self._producer.latest(pos.pair)
            if snap is None:
                continue
            current = snap.mid
            try:
                update = compute_mfe_update(pos, current)
                action = compute_profit_protection_action(pos, current, self._cfg)
            except Exception:
                logger.exception("[PROT-WORKER] eval failed for %s", pos.order_id)
                continue

            # 記録は両 mode で残す (比較用)。
            self._store.record_protection_decision(
                ts=db_now(), pair=pos.pair, order_id=pos.order_id,
                source="tick_worker", action=action.action, stage=action.stage,
                target_sl=action.target_sl, mfe_r=update.max_favorable_r,
                giveback_r=update.giveback_r,
            )

            # 実行は protect_live かつ raise_sl のみ (close は実行しない, H4)。
            if self._mode == "protect_live" and action.action == "raise_sl":
                if self._broker is not None and action.target_sl is not None:
                    self._broker.update_remote_sl(pos.order_id, action.target_sl)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("[PROT-WORKER] run_once failed")
            self._stop.wait(timeout=self._poll_seconds)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="prot-worker", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
```

> `record_protection_decision` を `protect_shadow`/`protect_live` 双方で残すのは比較のため。`test_off_or_producer_mode_does_nothing` は `mode="producer"` で早期 return することを検証する。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_protection_worker.py -v`
Expected: PASS (3 passed)。`Order.new` が `initial_risk_price_distance` を自動計算するので純関数は属性不足で落ちない。`test_close_action_is_not_executed` が close を返さない場合は、`max_fav_r`/`max_fav_price`/`mid` の値を giveback 条件 (`giveback_r >= 0.4` かつ `max_favorable_r >= 0.8`) を満たすよう調整する。

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/position_protection_worker.py tests/test_protection_worker.py
git commit -m "feat: PriceProtectionWorker — 保護判定 tick 駆動 (記録のみ / raise_sl, close 非実行) (spec §5)"
```

---

## Task 9: price_monitor に並走記録の薄い追記 (protect_shadow 以上のみ)

**Files:**
- Modify: `src/jobs/price_monitor.py` (`_apply_profit_protection` 周辺)
- Test: `tests/test_price_monitor_protection_record.py`

**Note:** `_apply_profit_protection(pos, current, cfg, position_mgr, broker, remote_sync_enabled)` (`price_monitor.py:73`) は `compute_mfe_update` と `compute_profit_protection_action` を既に呼んでいる (`price_monitor.py:82,89`)。**実行ロジックは変えず**、`compute_profit_protection_action` の結果を `protection_decisions` に `source="price_monitor"` で記録する 1 呼び出しを足すだけ。記録するか否かは引数で渡される store の有無 + stage で制御 (price_monitor は orchestrator config を直接持たないので、bootstrap/main で stage>=protect_shadow のとき store を渡す形にする)。

- [ ] **Step 1: Write the failing test**

`tests/test_price_monitor_protection_record.py` を新規作成:

```python
from types import SimpleNamespace

from src.jobs.price_monitor import _apply_profit_protection
from src.trading.position_manager import Order


class _RecStore:
    def __init__(self):
        self.records = []

    def record_protection_decision(self, **kw):
        self.records.append(kw)


def _cfg():
    return SimpleNamespace(
        protect_half_r=0.3, protect_breakeven_r=0.5, protect_lock_r=1.0,
        giveback_close_r=0.4, giveback_close_min_mfe_r=0.8,
    )


def _pos() -> Order:
    return Order.new(
        pair="USDJPY=X", direction="buy", entry_price=150.0,
        stop_loss=149.0, take_profit=152.0, position_size=1.0,
    )


class _PosMgr:
    def update_protection_state(self, *a, **k): ...
    def clear_pending_protection_target(self, *a, **k): ...
    def update_stop_loss(self, *a, **k): ...


def test_records_to_store_when_store_provided():
    store = _RecStore()
    _apply_profit_protection(
        _pos(), 150.6, _cfg(), _PosMgr(), broker=None,
        remote_sync_enabled=False, decision_store=store,
    )
    assert len(store.records) == 1
    assert store.records[0]["source"] == "price_monitor"


def test_no_record_when_store_none():
    # store 未指定 (off/producer 段) では完全無改変 = 記録しない
    _apply_profit_protection(
        _pos(), 150.6, _cfg(), _PosMgr(), broker=None,
        remote_sync_enabled=False, decision_store=None,
    )
    # 例外なく完了すれば OK (記録対象 store が無い)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_price_monitor_protection_record.py -v`
Expected: FAIL — `_apply_profit_protection` が `decision_store` キーワードを受けない (`TypeError`)。

- [ ] **Step 3: Write minimal implementation**

`src/jobs/price_monitor.py` の `_apply_profit_protection` シグネチャに `decision_store=None` を追加し、`action = compute_profit_protection_action(...)` (`price_monitor.py:89`) の直後に記録を挿入。**既存の実行ロジック (raise_sl 適用) は一切変えない:**

```python
    action = compute_profit_protection_action(pos, current, cfg)

    # 並走比較用の記録 (spec §5.3)。decision_store が渡されたとき (stage>=protect_shadow)
    # のみ記録する。実行ロジックは変えない。
    if decision_store is not None:
        from src.utils.clock import db_now
        decision_store.record_protection_decision(
            ts=db_now(), pair=pos.pair, order_id=pos.order_id,
            source="price_monitor", action=action.action, stage=action.stage,
            target_sl=action.target_sl, mfe_r=state.max_favorable_r,
            giveback_r=state.giveback_r,
        )

    action_target = action.target_sl if action.action == "raise_sl" else None
    # ...(以降は既存のまま)
```

> 呼び出し元 (`monitor_open_positions` 内の `_apply_profit_protection(...)` 呼び出し) には、stage>=protect_shadow のとき orchestrator store を `decision_store=` で渡すよう main/bootstrap 経路で配線する。off/producer では None のまま (完全無改変)。この配線は Task 11 (main 統合) で行う。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_price_monitor_protection_record.py -v`
Expected: PASS (2 passed)。

- [ ] **Step 5: Run price_monitor regression**

Run: `pytest tests/test_price_monitor.py -v` (既存 price_monitor テスト)
Expected: PASS (既存挙動が壊れていない = `decision_store` 既定 None で従来通り)。

- [ ] **Step 6: Commit**

```bash
git add src/jobs/price_monitor.py tests/test_price_monitor_protection_record.py
git commit -m "feat: price_monitor に並走記録の薄い追記 (decision_store 渡時のみ, spec §5.3)"
```

---

## Task 10: bootstrap/runtime で worker 配線 (protect_shadow 以上)

**Files:**
- Modify: `src/orchestrator/bootstrap.py` (worker 生成)
- Modify: `src/orchestrator/runtime.py` (worker ライフサイクル)
- Test: `tests/test_protection_worker_wiring.py`

**Note:** worker は `position_provider` (open positions を返す callable)、`store` (OrchestratorStore)、`cfg` (trading protect config)、`broker`、`mode` (=stage) を要する。bootstrap が producer を作る箇所 (Task 4) の近くで、stage>=protect_shadow のとき worker も生成し runtime に渡す。runtime の start/stop に組み込む。

- [ ] **Step 1: Write the failing test**

`tests/test_protection_worker_wiring.py` を新規作成:

```python
from src.orchestrator.runtime import OrchestratorRuntime


def test_runtime_starts_and_stops_protection_worker(monkeypatch):
    started = {"prod": False, "worker": False}
    stopped = {"prod": False, "worker": False}

    class _StubProd:
        def start(self): started["prod"] = True
        def stop(self): stopped["prod"] = True
        def latest(self, pair): return None

    class _StubWorker:
        def start(self): started["worker"] = True
        def stop(self): stopped["worker"] = True

    # 最小 runtime を組み (enabled=True 必須)。既存の最小構築ヘルパを使うか
    # OrchestratorConfig(enabled=True) + 必須引数の stub を渡す。
    # ここでは quote_producer / protection_worker のライフサイクルのみ検証する。
    rt = _make_minimal_runtime(
        monkeypatch, quote_producer=_StubProd(), protection_worker=_StubWorker(),
    )
    rt.start()
    assert started["prod"] and started["worker"]
    rt.stop()
    assert stopped["prod"] and stopped["worker"]
```

> `_make_minimal_runtime` は既存テスト (`test_runtime_*.py`) の runtime 構築パターンを流用したローカルヘルパ。`OrchestratorConfig(enabled=True)` + 既存の最小 stub (orch_store, context_builder, pairs, quote_provider, detector, pipeline) を渡し、`quote_producer=` / `protection_worker=` を追加で渡す。実装時に既存 `test_runtime_notifications.py` の構築を参照して写す。

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_protection_worker_wiring.py -v`
Expected: FAIL — runtime が `protection_worker` 引数を受けない / `_make_minimal_runtime` 未定義。

- [ ] **Step 3: Write minimal implementation**

runtime `__init__` に `protection_worker=None` キーワードを追加し `self._protection_worker = protection_worker` を保持。`start()` の producer 起動の隣に:

```python
        if self._protection_worker is not None:
            self._protection_worker.start()
```

`stop()` の producer 停止の隣に:

```python
        if self._protection_worker is not None:
            self._protection_worker.stop()
```

bootstrap の Task 4 で producer を作るブロックを拡張し、stage>=protect_shadow のとき worker を生成:

```python
    protection_worker = None
    if stage in ("protect_shadow", "protect_live") and quote_producer is not None:
        from src.orchestrator.position_protection_worker import PriceProtectionWorker

        protection_worker = PriceProtectionWorker(
            producer=quote_producer,
            position_provider=lambda: position_mgr.get_account_state().open_positions,
            store=orch_store, cfg=config.trading, broker=broker,
            mode=stage,
            poll_seconds=getattr(orch_cfg, "quote_stream_poll_seconds", 2),
        )
```

`OrchestratorRuntime(...)` に `protection_worker=protection_worker` を渡す。

> `position_provider` は `position_mgr.get_account_state().open_positions` で open positions (`list[Order]`) を返す (`position_manager.py:134,220`)。`position_mgr` / `broker` / `orch_store` / `config.trading` の正確な参照は bootstrap の既存変数に合わせる。bootstrap が `position_mgr`/`broker` を持たない場合は、`build_orchestrator_runtime` の引数に追加するか main から渡す経路を確認する (worker は本番 position_mgr/broker を要するため、この配線は shadow boundary を越える protect_live で初めて副作用を持つ点に注意)。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_protection_worker_wiring.py -v`
Expected: PASS。

- [ ] **Step 5: Regression**

Run: `pytest tests/test_orchestrator_bootstrap.py tests/test_runtime_notifications.py -v`
Expected: PASS (既存 runtime 構築が壊れていない)。

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/bootstrap.py src/orchestrator/runtime.py tests/test_protection_worker_wiring.py
git commit -m "feat: protect_shadow 以上で保護 worker を runtime に配線 (spec §5.2)"
```

---

## Task 11: main 統合 — price_monitor へ decision_store を stage で渡す

**Files:**
- Modify: `main.py` (price_monitor 呼び出しに decision_store を条件付き注入)
- Test: 手動確認 + 既存 e2e 回帰 (新規ユニットは Task 9 でカバー済み)

**Note:** `main.py` で price_monitor は `run_price_monitor(config, price_provider, bridge_gate)` 経由でスケジュール登録される (`main.py:342-353` 近辺)。stage>=protect_shadow のとき orchestrator store を `_apply_profit_protection` まで届ける必要がある。`run_price_monitor` → `monitor_open_positions` → `_apply_profit_protection` の経路に `decision_store` を通すか、price_monitor が orchestrator store を参照できる形にする。

- [ ] **Step 1: 経路を確認し decision_store を通す**

`main.py` で orchestrator store が構築される箇所を確認 (`OrchestratorStore(...)`)。`config.orchestrator.tick_migration_stage` が `protect_shadow`/`protect_live` のとき、その store を price_monitor 経路へ渡す。`run_price_monitor` / `monitor_open_positions` / `_apply_profit_protection` に `decision_store` 引数を順に通す (デフォルト None で後方互換)。

```python
# main.py 内、price_monitor スケジュール登録の引数に追加 (擬似):
stage = config.orchestrator.tick_migration_stage
prot_store = orch_store if stage in ("protect_shadow", "protect_live") else None
# run_price_monitor(..., decision_store=prot_store) として渡す
```

`monitor_open_positions` 内の `_apply_profit_protection(...)` 呼び出しに `decision_store=decision_store` を追加。

- [ ] **Step 2: 回帰テスト**

Run: `pytest tests/test_price_monitor.py tests/test_orchestrator_e2e.py -q`
Expected: PASS (off 既定で従来通り、store 未注入)。

- [ ] **Step 3: Commit**

```bash
git add main.py src/jobs/price_monitor.py
git commit -m "feat: stage>=protect_shadow で price_monitor に decision_store を注入 (spec §5.3)"
```

---

## Task 12: D-2a 通し確認 + 並走比較テスト

**Files:**
- Test: `tests/test_protection_parallel_compare.py`

- [ ] **Step 1: Write the comparison test**

`tests/test_protection_parallel_compare.py` を新規作成:

```python
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from src.data.orchestrator_store import OrchestratorStore
from src.jobs.price_monitor import _apply_profit_protection
from src.orchestrator.context_builder import QuoteSnapshot
from src.orchestrator.position_protection_worker import PriceProtectionWorker
from src.trading.position_manager import Order
from src.utils.clock import db_now


def _cfg():
    return SimpleNamespace(
        protect_half_r=0.3, protect_breakeven_r=0.5, protect_lock_r=1.0,
        giveback_close_r=0.4, giveback_close_min_mfe_r=0.8,
    )


def _pos() -> Order:
    # 同一 order_id を両 source で使う (比較は order_id でペアリング)
    o = Order.new(
        pair="USDJPY=X", direction="buy", entry_price=150.0,
        stop_loss=149.0, take_profit=152.0, position_size=1.0,
    )
    o.order_id = "o1"
    return o


class _PosMgr:
    def update_protection_state(self, *a, **k): ...
    def clear_pending_protection_target(self, *a, **k): ...
    def update_stop_loss(self, *a, **k): ...


class _Producer:
    def latest(self, pair):
        return QuoteSnapshot(
            bid=150.6, ask=150.6, mid=150.6, spread=None,
            source="mt5", observed_at=datetime.now(),
        )


def test_price_monitor_and_worker_agree_on_same_position(tmp_path: Path):
    """同一局面 (current=150.6) で両 source の action が一致する (spec §5.4)。"""
    store = OrchestratorStore(tmp_path / "orch.db")
    now = db_now()

    # price_monitor 経路 (実行はするが broker=None で副作用なし) + 記録
    _apply_profit_protection(
        _pos(), 150.6, _cfg(), _PosMgr(), broker=None,
        remote_sync_enabled=False, decision_store=store,
    )
    # tick worker 経路 (protect_shadow = 記録のみ)
    worker = PriceProtectionWorker(
        producer=_Producer(), position_provider=lambda: [_pos()],
        store=store, cfg=_cfg(), broker=None, mode="protect_shadow",
    )
    worker.run_once()

    rows = store.compare_protection_decisions(since=now - timedelta(minutes=5))
    assert len(rows) == 1
    assert rows[0]["action_match"] is True
```

- [ ] **Step 2: Run test**

Run: `pytest tests/test_protection_parallel_compare.py -v`
Expected: PASS — 同一純関数・同一 current なので action が一致。

- [ ] **Step 3: D-2a フルスイート回帰**

Run: `pytest -q`
Expected: 失敗ゼロ。

- [ ] **Step 4: Commit**

```bash
git add tests/test_protection_parallel_compare.py
git commit -m "test: price_monitor と tick_worker の並走比較一致を検証 (spec §5.4)"
```

---

# Phase D-2b: protect_live — worker が実 SL 更新 / price_monitor 保護停止

**本番保護に触れる。** 並走比較で一致を確認後の config 昇格で有効化。

## Task 13: protect_live で worker が raise_sl を実行 + price_monitor 保護停止

**Files:**
- Modify: `src/orchestrator/position_protection_worker.py` (実装は Task 8 で済 — `protect_live` 分岐は既にある)
- Modify: `src/jobs/price_monitor.py` (`protect_live` のとき profit protection 実行をスキップ)
- Test: `tests/test_protection_worker.py` (追記) + `tests/test_price_monitor_protection_record.py` (追記)

**Note:** Task 8 の worker は既に `protect_live` かつ `raise_sl` で `broker.update_remote_sl` を呼ぶ。残りは price_monitor 側で `protect_live` のとき profit protection の**実行**を止めること (二重実行防止)。記録は続けてよいが SL 適用はしない。

- [ ] **Step 1: Write the failing test (worker side)**

`tests/test_protection_worker.py` に追記:

```python
def test_protect_live_executes_raise_sl():
    store = _RecordingStore()
    broker_calls = []
    worker = PriceProtectionWorker(
        producer=_Producer(mid=150.6), position_provider=lambda: [_pos()],
        store=store, cfg=_cfg(),
        broker=SimpleNamespace(
            update_remote_sl=lambda order_id, sl: broker_calls.append((order_id, sl))
        ),
        mode="protect_live",
    )
    worker.run_once()
    assert len(broker_calls) == 1  # raise_sl が broker に届く
```

- [ ] **Step 2: Run (should already pass from Task 8)**

Run: `pytest tests/test_protection_worker.py::test_protect_live_executes_raise_sl -v`
Expected: PASS (Task 8 実装で `protect_live` 分岐済み)。FAIL なら Task 8 の分岐を確認。

- [ ] **Step 3: Write the failing test (price_monitor side)**

`tests/test_price_monitor_protection_record.py` に追記:

```python
def test_protect_live_skips_sl_application():
    """protect_live では price_monitor は記録するが SL 適用を実行しない (二重防止)。"""
    store = _RecStore()
    applied = []

    class _PosMgrTrack(_PosMgr):
        def update_stop_loss(self, *a, **k):
            applied.append(a)

    _apply_profit_protection(
        _pos(), 150.6, _cfg(), _PosMgrTrack(), broker=None,
        remote_sync_enabled=False, decision_store=store,
        protection_mode="protect_live",
    )
    assert len(store.records) == 1     # 記録はする
    assert applied == []               # SL 適用はしない
```

- [ ] **Step 4: Run test to verify it fails**

Run: `pytest tests/test_price_monitor_protection_record.py::test_protect_live_skips_sl_application -v`
Expected: FAIL — `_apply_profit_protection` が `protection_mode` を受けない or SL 適用を止めない。

- [ ] **Step 5: Implement price_monitor skip**

`_apply_profit_protection` に `protection_mode="legacy"` 引数を追加。記録の後、`protection_mode == "protect_live"` なら **SL 適用をスキップして return** (記録は済んでいる):

```python
    # protect_live では実 SL 適用を worker 側に委譲し、price_monitor は記録のみで停止
    # (single execution writer, spec §5.3)。
    if protection_mode == "protect_live":
        return False, False

    action_target = action.target_sl if action.action == "raise_sl" else None
    # ...(以降の SL 適用は legacy/protect_shadow のときだけ実行)
```

main の price_monitor 配線 (Task 11) で `protection_mode=stage` も渡す。

- [ ] **Step 6: Run tests to verify pass**

Run: `pytest tests/test_protection_worker.py tests/test_price_monitor_protection_record.py -v`
Expected: PASS (全て)。

- [ ] **Step 7: Regression**

Run: `pytest tests/test_price_monitor.py -q`
Expected: PASS (legacy 既定で従来 SL 適用が動く)。

- [ ] **Step 8: Commit**

```bash
git add src/orchestrator/position_protection_worker.py src/jobs/price_monitor.py tests/test_protection_worker.py tests/test_price_monitor_protection_record.py main.py
git commit -m "feat: protect_live で worker が SL 更新、price_monitor 保護停止 (single writer, spec §5.3)"
```

---

## Task 14: 全体回帰 + Review Checklist 確認

- [ ] **Step 1: フルスイート**

Run: `pytest -q`
Expected: 失敗ゼロ。新規テスト全 pass。

- [ ] **Step 2: 決定性確認 (順序非依存)**

Run: `pytest tests/test_quote_stream.py tests/test_protection_worker.py tests/test_mt5_get_quote.py -p no:randomly -v`
Expected: PASS。

- [ ] **Step 3: spec Review Checklist を 1 項目ずつ確認**

spec §8 の各チェック項目を、対応テストで満たしていることを確認:
- H1 (observed_at naive) → Task 5
- H2 (spread 価格差) → Task 2
- H3 (to_mt5_symbol) → Task 2
- H4 (close 非実行) → Task 8
- M5 (store) → Task 7
- `off` 回帰 → Task 4 Step 5 / Task 6 / Task 9 Step 5

- [ ] **Step 4: 最終コミット (もし未コミットの調整があれば)**

```bash
git add -A
git commit -m "chore: Phase 2/D 全体回帰確認"
```

---

## 完了条件

- `tick_migration_stage=off` (既定) で全既存挙動が無改変 (回帰グリーン)。
- `producer` で watch が producer 直読 + spread 実値化 (MT5 接続時)。
- `protect_shadow` で worker が記録のみ、price_monitor と並走比較で一致を定量確認。
- `protect_live` で worker が raise_sl を single writer 実行、price_monitor 保護停止。
- close 実行・emergency close 移設・websocket 化は本 plan のスコープ外 (将来課題)。
