# Technical Collection trade/watch Split — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `collect_all_technical` を watch 経路 (`collect_watch_technical`) と trade 経路 (`collect_trade_technical`) に分割し、別スケジュール起動点 + config 化された interval で回せるようにする (相関は trade 経路が PriceStore から watch 価格を再ロードして維持)。

**Architecture:** 既存ヘルパ (`_collect_one` 等) は無変更で再利用。本体ロジックを 2 公開関数に抽出し、`collect_all_technical` は両者を順次呼ぶ後方互換 wrapper に。`technical_times` のハードコードを `ScheduleConfig` の `technical_{trade,watch}_interval_hours` に移し、main.py で別スケジュール登録 + 初回 watch→trade 逐次実行。

**Tech Stack:** Python / asyncio / pandas / SQLAlchemy (PriceStore) / `schedule` ライブラリ / pytest。uv 管理。

**Spec:** [docs/superpowers/specs/2026-06-21-technical-collection-trade-watch-split-design.md](../specs/2026-06-21-technical-collection-trade-watch-split-design.md)

**実行環境メモ (worker 必読):**
- finance は uv 管理。テストは WSL 内: PowerShell から
  `wsl -d Ubuntu-24.04 -- bash -lc "cd /home/teru/project/finance && .venv/bin/python -m pytest ..."`。
  `python` 単体は PATH に無い。Bash tool は UNC パス上で動き `/mnt/c`・`/home` 直アクセス不可。
- GateGuard フックが Bash/Edit/Write の初回ごとに「事実提示」を要求する。
- コミットは対象ファイルを明示指定 (`git add -A` は MagicMock 名の SQLite ゴミを巻き込む)。
- branch: `feat/planner-watch-loop`。

---

## File Structure

- **Modify** `src/config/schema.py` — `ScheduleConfig` に 2 つの interval フィールド追加。
- **Modify** `src/jobs/technical_collector.py` — `collect_watch_technical` / `collect_trade_technical` を抽出、`collect_all_technical` を wrapper 化、`run_trade_technical_collection` / `run_watch_technical_collection` 追加、econ phase を `_collect_econ_impact` に抽出。
- **Create** `src/jobs/technical_schedule.py` — `technical_times_for(interval_hours)` 純関数 (テスト容易化のため独立ファイル)。
- **Modify** `main.py` — `technical_times` を config 駆動の 2 系統に置換、別スケジュール登録、初回 collection を watch→trade 逐次化。
- **Create** `tests/test_technical_collection_split.py` — 分割の振る舞いテスト (Task 3/4/5)。
- **Create** `tests/test_technical_schedule.py` — `technical_times_for` テスト (Task 1/2)。

---

## Task 1: config に technical interval フィールドを追加

**Files:**
- Modify: `src/config/schema.py` (ScheduleConfig, 行 312-315 付近)
- Test: `tests/test_technical_schedule.py`

- [ ] **Step 1: 既定値を確認する失敗テストを書く**

`tests/test_technical_schedule.py` を作成:

```python
"""technical 収集 interval の config 既定値とスケジュール生成のテスト。"""
from __future__ import annotations

from src.config.schema import ScheduleConfig


def test_schedule_config_technical_intervals_default_to_hourly():
    """新規 interval フィールドは既定で 1 (= 毎時、現状維持)。"""
    cfg = ScheduleConfig()
    assert cfg.technical_trade_interval_hours == 1
    assert cfg.technical_watch_interval_hours == 1
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_technical_schedule.py -v"`
Expected: FAIL — `AttributeError: 'ScheduleConfig' object has no attribute 'technical_trade_interval_hours'`

- [ ] **Step 3: ScheduleConfig にフィールドを追加**

`src/config/schema.py` の `ScheduleConfig` を編集:

```python
@dataclass
class ScheduleConfig:
    run_times: list[str] = field(default_factory=lambda: ["15:00", "21:00"])
    timezone: str = "Asia/Tokyo"
    # technical 収集の間隔 (時間)。既定は現状維持 = 毎時 (1h)。
    # trade は将来 cadence resolver で boost される土台、watch は低頻度固定用。
    technical_trade_interval_hours: int = 1
    technical_watch_interval_hours: int = 1
```

- [ ] **Step 4: テストを実行して成功を確認**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_technical_schedule.py -v"`
Expected: PASS

- [ ] **Step 5: コミット**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd /home/teru/project/finance && git add src/config/schema.py tests/test_technical_schedule.py && git commit -m 'feat: ScheduleConfig に technical trade/watch interval を追加'"
```

---

## Task 2: technical_times_for 純関数

**Files:**
- Create: `src/jobs/technical_schedule.py`
- Test: `tests/test_technical_schedule.py` (Task 1 で作成済み、追記)

- [ ] **Step 1: 失敗テストを追記**

`tests/test_technical_schedule.py` の末尾に追加:

```python
def test_technical_times_for_hourly():
    from src.jobs.technical_schedule import technical_times_for
    times = technical_times_for(1)
    assert len(times) == 24
    assert times[0] == "00:00"
    assert times[-1] == "23:00"


def test_technical_times_for_every_two_hours():
    from src.jobs.technical_schedule import technical_times_for
    times = technical_times_for(2)
    assert times == ["00:00", "02:00", "04:00", "06:00", "08:00", "10:00",
                     "12:00", "14:00", "16:00", "18:00", "20:00", "22:00"]


def test_technical_times_for_zero_or_negative_falls_back_to_hourly():
    """0 や負値は 1 (毎時) に倒す (range step >= 1 ガード)。"""
    from src.jobs.technical_schedule import technical_times_for
    assert len(technical_times_for(0)) == 24
    assert len(technical_times_for(-3)) == 24
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_technical_schedule.py -v"`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.jobs.technical_schedule'`

- [ ] **Step 3: 純関数を実装**

`src/jobs/technical_schedule.py` を作成:

```python
"""technical 収集スケジュールの時刻リスト生成 (純関数、テスト容易化のため独立)。"""
from __future__ import annotations


def technical_times_for(interval_hours: int) -> list[str]:
    """指定間隔 (時間) の "HH:00" 時刻リストを返す。

    interval_hours=1 → 毎時 (24 個)、2 → 12 個。0/負値は 1 に倒す。
    """
    step = max(1, interval_hours)
    return [f"{h:02d}:00" for h in range(0, 24, step)]
```

- [ ] **Step 4: テストを実行して成功を確認**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_technical_schedule.py -v"`
Expected: PASS (4 tests)

- [ ] **Step 5: コミット**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd /home/teru/project/finance && git add src/jobs/technical_schedule.py tests/test_technical_schedule.py && git commit -m 'feat: technical_times_for 純関数を追加'"
```

---

## Task 3: collect_watch_technical を抽出

watch_only 銘柄のみを収集する経路。macro/correlation は持たない。既存ヘルパ `_collect_one` /
`_fetch_instrument_ohlcv` をそのまま使う。

**Files:**
- Modify: `src/jobs/technical_collector.py`
- Test: `tests/test_technical_collection_split.py`

- [ ] **Step 1: 失敗テストを書く**

`tests/test_technical_collection_split.py` を作成:

```python
"""collect_watch_technical / collect_trade_technical 分割の振る舞いテスト。"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd

from src.data.analysis_store import AnalysisStore
from src.utils.clock import db_now


def _inst(symbol: str, display: str, asset_type: str = "fx"):
    return SimpleNamespace(
        symbol=symbol, display_name=display, asset_type=asset_type,
        news_categories=["fx"],
        base_currency="USD", quote_currency="JPY",
    )


def _fresh_price_data(symbol: str):
    bar_time = db_now() - timedelta(minutes=15)
    df = pd.DataFrame(
        {"Open": [150.0] * 100, "High": [150.5] * 100, "Low": [149.5] * 100,
         "Close": [150.0] * 100, "Volume": [1000] * 100},
        index=pd.date_range(end=bar_time, periods=100, freq="1h"),
    )
    return SimpleNamespace(symbol=symbol, df=df, current_price=150.0)


def _split_config(watch, tradeable):
    config = MagicMock()
    config.watch_only_instruments = watch
    config.tradeable_instruments = tradeable
    config.news_collection.inter_pair_delay_seconds = 0.0
    config.economic_calendar.enabled = False
    config.paper_provider = "twelvedata"
    return config


def _patch_collectible(monkeypatch):
    """prefetch を fresh data に、_collect_one を「行を書くだけ」の stub に差し替える。"""
    import src.jobs.technical_collector as tc

    monkeypatch.setattr(tc, "is_market_open", lambda *a, **kw: True)
    monkeypatch.setattr(tc, "create_llm_client",
                        lambda *a, **kw: MagicMock(model_name="test"))
    monkeypatch.setattr(tc, "_fetch_instrument_ohlcv",
                        lambda inst, *a, **kw: _fresh_price_data(inst.symbol))

    async def _fake_collect_one(inst, config, store, price_store, analysis_store,
                                llm, macro_context="", correlation_context="",
                                price_provider=None, price_data=None):
        # ok 行の代わりに sentinel を書いて「収集された」ことを記録する
        analysis_store.add_sentinel(symbol=inst.symbol, status="failed",
                                    reason=f"stub collected macro={bool(macro_context)} "
                                           f"corr={bool(correlation_context)}")

    monkeypatch.setattr(tc, "_collect_one", _fake_collect_one)


def test_collect_watch_only_collects_watch_not_trade(tmp_path, monkeypatch):
    """collect_watch_technical は watch のみ収集し、trade symbol には触れない。"""
    from src.jobs.technical_collector import collect_watch_technical

    store = AnalysisStore(tmp_path / "test.db")
    watch = [_inst("SPY", "S&P500", "index")]
    tradeable = [_inst("USDJPY=X", "USD/JPY")]
    _patch_collectible(monkeypatch)

    asyncio.run(collect_watch_technical(
        config=_split_config(watch, tradeable), store=MagicMock(),
        price_store=MagicMock(), analysis_store=store, force=True,
    ))

    assert store.get_latest_collect_row("SPY") is not None
    assert store.get_latest_collect_row("USDJPY=X") is None
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_technical_collection_split.py -v"`
Expected: FAIL — `ImportError: cannot import name 'collect_watch_technical'`

- [ ] **Step 3: collect_watch_technical を実装**

`src/jobs/technical_collector.py` に新規関数を追加 (`collect_all_technical` の上)。
既存 watch Phase 1 のループ本体 (現 396-438 行) をこの関数へ移す:

```python
async def collect_watch_technical(
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    force: bool = False,
    price_provider: "PriceProvider | None" = None,
) -> None:
    """watch_only 銘柄のみのテクニカル分析を収集する (macro/correlation 無し)。"""
    if not force and not is_market_open():
        return

    watch_only = config.watch_only_instruments
    if not watch_only:
        return

    llm_price = create_llm_client(config, "price_analysis")
    delay = config.news_collection.inter_pair_delay_seconds
    logger.info(f"[COLLECT] Watch technical: {len(watch_only)} watch-only instruments")

    for i, inst in enumerate(watch_only):
        try:
            price_data = _fetch_instrument_ohlcv(inst, config, price_store, price_provider)
        except Exception as e:
            analysis_store.add_sentinel(
                symbol=inst.symbol, status="failed",
                reason=f"prefetch_failed: {type(e).__name__}: {e}",
            )
            logger.warning(f"[COLLECT] {inst.display_name}: failed sentinel (prefetch)")
            if i < len(watch_only) - 1:
                await asyncio.sleep(delay)
            continue
        try:
            await _collect_one(
                inst, config, store, price_store, analysis_store, llm_price,
                price_provider=price_provider, price_data=price_data,
            )
        except Exception as e:
            logger.error(
                f"[COLLECT] {inst.display_name}: unexpected raise from _collect_one — {e}",
                exc_info=True,
            )
            try:
                analysis_store.add_sentinel(
                    symbol=inst.symbol, status="failed",
                    reason=f"unexpected_raise: {type(e).__name__}: {e}",
                )
            except Exception as sentinel_err:
                logger.error(
                    f"[COLLECT] {inst.display_name}: sentinel write also failed: "
                    f"{type(sentinel_err).__name__}: {sentinel_err}",
                    exc_info=False,
                )
        if i < len(watch_only) - 1:
            await asyncio.sleep(delay)
```

- [ ] **Step 4: テストを実行して成功を確認**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_technical_collection_split.py -v"`
Expected: PASS

- [ ] **Step 5: コミット**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd /home/teru/project/finance && git add src/jobs/technical_collector.py tests/test_technical_collection_split.py && git commit -m 'feat: collect_watch_technical を抽出'"
```

---

## Task 4: collect_trade_technical を抽出 (相関は PriceStore 再ロード)

tradeable 銘柄を収集。macro は watch の保存済み snapshot から、相関は watch 価格を PriceStore
から再ロードして計算。econ phase もこちらに移す。

**Files:**
- Modify: `src/jobs/technical_collector.py`
- Test: `tests/test_technical_collection_split.py` (追記)

- [ ] **Step 1: 失敗テストを追記**

`tests/test_technical_collection_split.py` の末尾に追加:

```python
def _watch_ohlcv_df(n: int = 60):
    """相関計算に十分なバー数 (>= rolling_window + 5 = 25) の DataFrame。"""
    end = db_now() - timedelta(minutes=15)
    idx = pd.date_range(end=end, periods=n, freq="1h")
    closes = [150.0 + (j % 7) * 0.1 for j in range(n)]
    return pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes,
         "Close": closes, "Volume": [1000] * n},
        index=idx,
    )


def test_collect_trade_reloads_watch_prices_from_pricestore(tmp_path, monkeypatch):
    """collect_trade_technical は trade を収集し、watch 価格を PriceStore.load_ohlcv で
    再ロードして compute_correlations に渡す。"""
    import src.jobs.technical_collector as tc
    from src.jobs.technical_collector import collect_trade_technical

    store = AnalysisStore(tmp_path / "test.db")
    watch = [_inst("SPY", "S&P500", "index")]
    tradeable = [_inst("USDJPY=X", "USD/JPY")]
    _patch_collectible(monkeypatch)

    # PriceStore.load_ohlcv が watch の df を返す mock
    price_store = MagicMock()
    price_store.load_ohlcv.return_value = _watch_ohlcv_df()

    # compute_correlations が watch_prices を受け取ったか捕捉
    captured = {}

    def _fake_corr(trade_prices, watch_prices, watch_names, **kw):
        captured["watch_symbols"] = sorted(watch_prices.keys())
        captured["trade_symbols"] = sorted(trade_prices.keys())
        return []

    monkeypatch.setattr(tc, "compute_correlations", _fake_corr)
    monkeypatch.setattr(tc, "format_macro_context_for_prompt", lambda *a, **kw: "MACRO")

    asyncio.run(collect_trade_technical(
        config=_split_config(watch, tradeable), store=MagicMock(),
        price_store=price_store, analysis_store=store, force=True,
    ))

    assert store.get_latest_collect_row("USDJPY=X") is not None
    assert store.get_latest_collect_row("SPY") is None  # watch は収集しない
    assert captured["watch_symbols"] == ["SPY"]  # PriceStore から再ロードされた
    assert captured["trade_symbols"] == ["USDJPY=X"]
    price_store.load_ohlcv.assert_called()  # watch 価格を再ロードした


def test_collect_trade_skips_watch_with_missing_prices(tmp_path, monkeypatch):
    """watch 価格が prices.db に無い (空 df) 場合、trade 収集は継続し相関から除外する。"""
    import src.jobs.technical_collector as tc
    from src.jobs.technical_collector import collect_trade_technical

    store = AnalysisStore(tmp_path / "test.db")
    watch = [_inst("SPY", "S&P500", "index")]
    tradeable = [_inst("USDJPY=X", "USD/JPY")]
    _patch_collectible(monkeypatch)

    price_store = MagicMock()
    price_store.load_ohlcv.return_value = pd.DataFrame()  # 空 = データ無し

    captured = {}

    def _fake_corr(trade_prices, watch_prices, watch_names, **kw):
        captured["watch_symbols"] = sorted(watch_prices.keys())
        return []

    monkeypatch.setattr(tc, "compute_correlations", _fake_corr)
    monkeypatch.setattr(tc, "format_macro_context_for_prompt", lambda *a, **kw: "MACRO")

    asyncio.run(collect_trade_technical(
        config=_split_config(watch, tradeable), store=MagicMock(),
        price_store=price_store, analysis_store=store, force=True,
    ))

    # trade は収集され、相関の watch 入力は空 (SPY 除外)
    assert store.get_latest_collect_row("USDJPY=X") is not None
    assert captured["watch_symbols"] == []
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_technical_collection_split.py -v"`
Expected: FAIL — `ImportError: cannot import name 'collect_trade_technical'`

- [ ] **Step 3: collect_trade_technical を実装**

`src/jobs/technical_collector.py` に追加。相関の watch 価格を PriceStore から再ロードする
ヘルパ `_reload_watch_prices` を新設。econ phase 本体 (現 515-641 行) は `_collect_econ_impact`
へ抽出する。

まず import に追記 (既存 `from src.data.correlation import ...` 行に `_DEFAULT_ROLLING_WINDOW`
を足す):

```python
from src.data.correlation import (
    PairCorrelation, compute_correlations, format_correlation_context,
    _DEFAULT_ROLLING_WINDOW,
)
```

ファイル冒頭ヘルパ群の近くに定数とヘルパを追加:

```python
_CORR_LOOKBACK_DAYS = 30  # 相関に十分なバー数を確保する lookback


def _reload_watch_prices(
    config: AppConfig,
    price_store: PriceStore,
) -> dict[str, "PriceData"]:
    """相関計算用に watch 価格を PriceStore から再ロードする。

    空・バー不足の watch symbol は除外する (相関入力から外れるだけで trade 収集は継続)。
    """
    from src.data.price_fetcher import PriceData
    from src.utils.clock import db_now

    min_bars = _DEFAULT_ROLLING_WINDOW + 5  # compute_correlations の要求 (= 25)
    end = db_now()
    start = end - timedelta(days=_CORR_LOOKBACK_DAYS)
    out: dict[str, PriceData] = {}
    for w in config.watch_only_instruments:
        try:
            df = price_store.load_ohlcv(w.symbol, start, end)
        except Exception as e:
            logger.debug(f"[CORR] watch reload failed {w.symbol}: {e}")
            continue
        if df is None or df.empty or len(df) < min_bars:
            continue
        last_close = float(df["Close"].iloc[-1])
        out[w.symbol] = PriceData(
            symbol=w.symbol, df=df, current_price=last_close, fetched_at=end,
        )
    return out
```

次に `collect_trade_technical` 本体:

```python
async def collect_trade_technical(
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    force: bool = False,
    price_provider: "PriceProvider | None" = None,
) -> None:
    """tradeable 銘柄のテクニカル分析を収集する (macro + 相関 + econ 付き)。"""
    if not force and not is_market_open():
        return

    tradeable = config.tradeable_instruments
    watch_only = config.watch_only_instruments
    if not tradeable:
        return

    llm_price = create_llm_client(config, "price_analysis")
    delay = config.news_collection.inter_pair_delay_seconds
    logger.info(f"[COLLECT] Trade technical: {len(tradeable)} tradeable instruments")

    # trade 価格を prefetch
    prices: dict[str, "PriceData"] = {}
    prefetch_errors: dict[str, str] = {}
    for inst in tradeable:
        try:
            prices[inst.symbol] = _fetch_instrument_ohlcv(
                inst, config, price_store, price_provider,
            )
        except Exception as e:
            prefetch_errors[inst.symbol] = f"{type(e).__name__}: {e}"
            logger.warning(f"[PREFETCH] {inst.display_name}: OHLCV fetch failed: {e}")

    # macro context: watch の保存済み ok snapshot から構築
    macro_snapshots = []
    for inst in watch_only:
        snaps = analysis_store.get_recent_ok_snapshots(inst.symbol, hours=8)
        if snaps:
            macro_snapshots.append(snaps[0])
    macro_ctx = format_macro_context_for_prompt(
        macro_snapshots, watch_only, realtime_provider=config.paper_provider,
    )

    # Phase 1.5 相関: watch 価格を PriceStore から再ロードして計算
    correlations: list[PairCorrelation] = []
    if watch_only:
        try:
            watch_prices = _reload_watch_prices(config, price_store)
            trade_prices = {i.symbol: prices[i.symbol] for i in tradeable if i.symbol in prices}
            watch_names = {inst.symbol: inst.display_name for inst in watch_only}
            correlations = compute_correlations(trade_prices, watch_prices, watch_names)
            logger.info(f"[CORR] Computed {len(correlations)} correlation pairs")
        except Exception as e:
            logger.error(f"[CORR] Correlation computation failed: {e}", exc_info=True)

    for i, inst in enumerate(tradeable):
        pd_cached = prices.get(inst.symbol)
        if pd_cached is None:
            err = prefetch_errors.get(inst.symbol, "no cached price (unknown reason)")
            analysis_store.add_sentinel(
                symbol=inst.symbol, status="failed", reason=f"prefetch_failed: {err}",
            )
            logger.warning(f"[COLLECT] {inst.display_name}: failed sentinel (prefetch)")
            if i < len(tradeable) - 1:
                await asyncio.sleep(delay)
            continue
        try:
            corr_ctx = format_correlation_context(correlations, inst.symbol)
            await _collect_one(
                inst, config, store, price_store, analysis_store, llm_price,
                macro_context=macro_ctx, correlation_context=corr_ctx,
                price_provider=price_provider, price_data=pd_cached,
            )
        except Exception as e:
            logger.error(
                f"[COLLECT] {inst.display_name}: unexpected raise from _collect_one — {e}",
                exc_info=True,
            )
            try:
                analysis_store.add_sentinel(
                    symbol=inst.symbol, status="failed",
                    reason=f"unexpected_raise: {type(e).__name__}: {e}",
                )
            except Exception as sentinel_err:
                logger.error(
                    f"[COLLECT] {inst.display_name}: sentinel write also failed: "
                    f"{type(sentinel_err).__name__}: {sentinel_err}",
                    exc_info=False,
                )
        if i < len(tradeable) - 1:
            await asyncio.sleep(delay)

    # Phase 3: 経済指標影響分析 (tradeable 依存)
    await _collect_econ_impact(config, store, price_store, analysis_store, tradeable)
    logger.info("=== Trade technical collection complete ===")
```

econ phase 本体は `_collect_econ_impact` ヘルパに抽出する:

```python
async def _collect_econ_impact(
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    tradeable: list[InstrumentConfig],
) -> None:
    """経済指標影響分析 (オプション)。現 collect_all_technical Phase 3 をそのまま移植。"""
    if not config.economic_calendar.enabled:
        return
    # ↓ 現 src/jobs/technical_collector.py 行 517-641 の try ブロック本体を
    #   インデント調整してそのまま貼り付ける (ロジック変更なし)。
    #   `tradeable` は引数で受け取る。ローカル import (db_now / EconEventStore /
    #   refresh_recent_events / analyze_event_impact / PairReaction / SnapshotBrief /
    #   classify_surprise / make_embed_fn) も含めて移植する。
```

> **実装注意:** econ phase はロジックを一切変えず移植するだけ。元の try/except 構造・ログ・
> import を保持する。`tradeable` 参照は引数経由に変わるだけ。元コードの最も外側
> `if config.economic_calendar.enabled:` は関数冒頭の early-return に置き換える。

- [ ] **Step 4: テストを実行して成功を確認**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_technical_collection_split.py -v"`
Expected: PASS (3 tests)

- [ ] **Step 5: コミット**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd /home/teru/project/finance && git add src/jobs/technical_collector.py tests/test_technical_collection_split.py && git commit -m 'feat: collect_trade_technical を抽出 (相関は PriceStore 再ロード) + econ 移設'"
```

---

## Task 5: collect_all_technical を後方互換 wrapper 化

**Files:**
- Modify: `src/jobs/technical_collector.py`
- Test: `tests/test_technical_collection_split.py` (追記)

- [ ] **Step 1: 後方互換テストを追記**

`tests/test_technical_collection_split.py` の末尾に追加:

```python
def test_collect_all_runs_both_watch_and_trade(tmp_path, monkeypatch):
    """collect_all_technical wrapper は watch + trade 両方を収集する (後方互換)。"""
    import src.jobs.technical_collector as tc
    from src.jobs.technical_collector import collect_all_technical

    store = AnalysisStore(tmp_path / "test.db")
    watch = [_inst("SPY", "S&P500", "index")]
    tradeable = [_inst("USDJPY=X", "USD/JPY")]
    _patch_collectible(monkeypatch)

    price_store = MagicMock()
    price_store.load_ohlcv.return_value = _watch_ohlcv_df()
    monkeypatch.setattr(tc, "compute_correlations", lambda *a, **kw: [])
    monkeypatch.setattr(tc, "format_macro_context_for_prompt", lambda *a, **kw: "MACRO")

    asyncio.run(collect_all_technical(
        config=_split_config(watch, tradeable), store=MagicMock(),
        price_store=price_store, analysis_store=store, force=True,
    ))

    assert store.get_latest_collect_row("SPY") is not None       # watch 収集された
    assert store.get_latest_collect_row("USDJPY=X") is not None   # trade も収集された
```

- [ ] **Step 2: テストを実行 (旧 collect_all_technical 実装でも通る想定だが挙動確認)**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_technical_collection_split.py::test_collect_all_runs_both_watch_and_trade -v"`
Expected: 旧実装でも PASS する可能性が高い。wrapper 化で重複ロジックを消すのが目的。

- [ ] **Step 3: collect_all_technical を wrapper に置換**

`src/jobs/technical_collector.py` の旧 `collect_all_technical` 本体 (現 352-643 行) を、
2 関数を順次呼ぶ wrapper に**置換**する (watch Phase 1 / 相関 / trade Phase 2 / econ の
重複ロジックは Task 3/4 へ移したので削除):

```python
async def collect_all_technical(
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    force: bool = False,
    price_provider: "PriceProvider | None" = None,
) -> None:
    """全有効銘柄のテクニカル分析を収集する (後方互換 wrapper)。

    watch → trade の順に収集する。trade 経路は watch の保存済み価格を相関に使うため、
    1 回の実行では watch を先に回す (初回 cold start でも相関が成立する)。
    """
    if not force and not is_market_open():
        return
    await collect_watch_technical(
        config, store, price_store, analysis_store,
        force=force, price_provider=price_provider,
    )
    await collect_trade_technical(
        config, store, price_store, analysis_store,
        force=force, price_provider=price_provider,
    )
    logger.info("=== Technical collection complete ===")
```

- [ ] **Step 4: 全関連テストを実行して成功を確認 (既存 sentinel/gate テスト含む)**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_technical_collection_split.py tests/test_technical_collector_sentinel.py tests/test_technical_collector_gate.py -v"`
Expected: PASS (全件)。

> **注意:** `test_collect_all_prefetch_failure_writes_failed_sentinel` 等の既存テストは
> `collect_all_technical` を直接呼ぶ。wrapper 経由でも prefetch 失敗 → failed sentinel の
> 挙動が保たれることを確認する。`test_collect_all_phase1_prefetch_failure_writes_failed_sentinel`
> は watch_only の prefetch 失敗を見るので collect_watch_technical 側で sentinel が残ること、
> trade 側 prefetch 失敗テストは collect_trade_technical 側で残ることを確認。失敗したら
> Task 3/4 のループの sentinel 書き込みを見直す。

- [ ] **Step 5: コミット**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd /home/teru/project/finance && git add src/jobs/technical_collector.py tests/test_technical_collection_split.py && git commit -m 'refactor: collect_all_technical を watch/trade wrapper に置換'"
```

---

## Task 6: 同期 wrapper を分割 (run_trade / run_watch)

**Files:**
- Modify: `src/jobs/technical_collector.py`
- Test: `tests/test_technical_collector_gate.py` (gate probe の挙動確認、追記)

- [ ] **Step 1: gate probe テストを追記**

まず既存 `tests/test_technical_collector_gate.py` を読み、`run_technical_collection` が
`gate.probe(caller="tech", sync_balance=True)` を呼ぶ既存テストのパターンを確認する。
末尾に追記:

```python
def test_run_trade_technical_probes_gate(monkeypatch):
    """run_trade_technical_collection は gate.probe を呼ぶ (balance 同期は trade 文脈)。"""
    import src.jobs.technical_collector as tc
    from unittest.mock import MagicMock

    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(tc, "collect_trade_technical", _noop)

    gate = MagicMock()
    tc.run_trade_technical_collection(
        config=MagicMock(), store=MagicMock(), price_store=MagicMock(),
        analysis_store=MagicMock(), force=True, gate=gate,
    )
    gate.probe.assert_called_once_with(caller="tech", sync_balance=True)


def test_run_watch_technical_does_not_probe_gate(monkeypatch):
    """run_watch_technical_collection は gate.probe を呼ばない (発注非関与)。"""
    import src.jobs.technical_collector as tc
    from unittest.mock import MagicMock

    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(tc, "collect_watch_technical", _noop)

    gate = MagicMock()
    tc.run_watch_technical_collection(
        config=MagicMock(), store=MagicMock(), price_store=MagicMock(),
        analysis_store=MagicMock(), force=True, gate=gate,
    )
    gate.probe.assert_not_called()
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_technical_collector_gate.py -v -k 'trade or watch'"`
Expected: FAIL — `AttributeError: module 'src.jobs.technical_collector' has no attribute 'run_trade_technical_collection'`

- [ ] **Step 3: 同期 wrapper を 2 本追加**

`src/jobs/technical_collector.py` に追加 (既存 `run_technical_collection` の近く)。既存
`run_technical_collection` は残す:

```python
def run_trade_technical_collection(
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    force: bool = False,
    price_provider: "PriceProvider | None" = None,
    gate: "BridgeHealthGate | None" = None,
) -> None:
    """trade 経路の同期ラッパー。gate probe (balance 同期) は trade 文脈で行う。"""
    if gate is not None:
        gate.probe(caller="tech", sync_balance=True)
    asyncio.run(collect_trade_technical(
        config, store, price_store, analysis_store,
        force=force, price_provider=price_provider,
    ))


def run_watch_technical_collection(
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    force: bool = False,
    price_provider: "PriceProvider | None" = None,
    gate: "BridgeHealthGate | None" = None,
) -> None:
    """watch 経路の同期ラッパー。gate probe はしない (発注に関与しないため)。"""
    asyncio.run(collect_watch_technical(
        config, store, price_store, analysis_store,
        force=force, price_provider=price_provider,
    ))
```

- [ ] **Step 4: テストを実行して成功を確認**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd /home/teru/project/finance && .venv/bin/python -m pytest tests/test_technical_collector_gate.py -v"`
Expected: PASS (既存 + 新規)

- [ ] **Step 5: コミット**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd /home/teru/project/finance && git add src/jobs/technical_collector.py tests/test_technical_collector_gate.py && git commit -m 'feat: run_trade/run_watch_technical_collection 同期 wrapper を追加'"
```

---

## Task 7: main.py の結線 (別スケジュール + 初回逐次)

**Files:**
- Modify: `main.py`

- [ ] **Step 1: 現状を読む**

`main.py` の以下を確認:
- 行 158: `technical_times = [f"{h:02d}:00" for h in range(24)]` (ハードコード)
- 行 247-251: `technical_times` を **exit_check** (SL/TP 確認) が利用 — **変更しない**
- 行 263-270: technical 収集スケジュール登録 (`run_technical_collection` を `_run_with_slot` +
  `_market_aware=True`) — ここを置換
- 行 374-380: 初回 collection (`run_technical_collection(..., force=is_fresh_start, ...)`) — 置換

> **注意:** 行 158 の `technical_times` は exit_check (247-251) がまだ使うので**残す**。
> このタスクで触るのは技術収集 (263-270) と初回 (374-380) のみ。

- [ ] **Step 2: import とスケジュール時刻生成を追加**

`main.py` の technical collector import 箇所を更新 (既存
`from src.jobs.technical_collector import run_technical_collection` を拡張):

```python
from src.jobs.technical_collector import (
    run_technical_collection,
    run_trade_technical_collection,
    run_watch_technical_collection,
)
from src.jobs.technical_schedule import technical_times_for
```

行 158 の直後に config 駆動の 2 系統の時刻を追加 (行 158 の `technical_times` は残す):

```python
    # technical 収集は trade/watch 別 interval (既定は両方 1h = 毎時、現状維持)
    trade_tech_times = technical_times_for(config.schedule.technical_trade_interval_hours)
    watch_tech_times = technical_times_for(config.schedule.technical_watch_interval_hours)
```

- [ ] **Step 3: スケジュール登録を 2 系統に置換**

`main.py` 行 263-270 の技術収集登録ブロックを置換:

```python
    # 4a. テクニカル分析 (trade 経路・LLMあり・gate probe あり)
    for t in trade_tech_times:
        schedule.every().day.at(t, news_tz).do(
            _run_with_slot,
            run_trade_technical_collection, config, store, price_store, analysis_store,
            price_provider=price_provider,
            gate=bridge_gate,
            _market_aware=True,
        )
    # 4b. テクニカル分析 (watch 経路・LLMあり・gate probe なし・低頻度固定)
    for t in watch_tech_times:
        schedule.every().day.at(t, news_tz).do(
            _run_with_slot,
            run_watch_technical_collection, config, store, price_store, analysis_store,
            price_provider=price_provider,
            _market_aware=True,
        )
```

- [ ] **Step 4: 初回 collection を watch→trade 逐次に置換**

`main.py` 行 374-380 の初回 technical 収集を置換 (cold start の相関欠損回避):

```python
    if args.skip_tech:
        _console.print("[dim]--skip-tech: 初回テクニカル収集をスキップ[/dim]")
    else:
        # cold start の相関欠損を避けるため watch → trade の順で逐次実行
        run_watch_technical_collection(
            config, store, price_store, analysis_store,
            force=is_fresh_start, price_provider=price_provider,
        )
        run_trade_technical_collection(
            config, store, price_store, analysis_store,
            force=is_fresh_start, price_provider=price_provider, gate=bridge_gate,
        )
```

- [ ] **Step 5: import が解決し起動が壊れないことを確認**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd /home/teru/project/finance && .venv/bin/python -c 'import main' && echo IMPORT_OK"`
Expected: `IMPORT_OK` (構文・import エラーなし)

- [ ] **Step 6: コミット**

```bash
wsl -d Ubuntu-24.04 -- bash -lc "cd /home/teru/project/finance && git add main.py && git commit -m 'feat: main.py で technical 収集を trade/watch 別スケジュール化 + 初回逐次'"
```

---

## Task 8: 全テスト suite green 確認 + code review + memory 更新

**Files:** なし (検証 + memory のみ)

- [ ] **Step 1: 全テスト suite を実行**

Run: `wsl -d Ubuntu-24.04 -- bash -lc "cd /home/teru/project/finance && .venv/bin/python -m pytest -q"`
Expected: 全件 PASS (1076 + 新規分)。`test_job_guard::test_exception_in_fn_releases_guard` は
既知の順序依存 flake (本変更と無関係) なので、もし赤なら単独再実行で green を確認。

- [ ] **Step 2: code review エージェントを起動**

ecc:python-reviewer または ecc:code-reviewer で
`src/jobs/technical_collector.py` / `main.py` / `src/config/schema.py` /
`src/jobs/technical_schedule.py` の差分をレビュー。CRITICAL/HIGH を対処、MEDIUM は可能なら対処。

- [ ] **Step 3: 進捗 memory を更新**

`finance_phase2_impl_progress.md` の「残タスク」から Task 6.2 を完了に移し、Phase 6 完了を
記録。新しい別タスク (頻度調整機構 = cadence resolver / §5.3・§5.6、queue 制アイデア含む) を
次タスクとして追記。MEMORY.md のインデック行も更新。

---

## Self-Review Notes (記入済み)

- **Spec coverage:** §3 分割 (Task 3/4/5)、§3.1 相関再ロード (Task 4)、§3.2 econ 移設 (Task 4)、
  §4 config (Task 1/2)、§5 main.py 結線 + 初回逐次 (Task 7)、§6 テスト (Task 1-6)、
  §7 別タスク (Task 8 Step 3 で memory に引き継ぎ)。全カバー。
- **型整合:** `collect_watch_technical` / `collect_trade_technical` /
  `run_trade_technical_collection` / `run_watch_technical_collection` / `technical_times_for` /
  `_reload_watch_prices` / `_collect_econ_impact` の名前は全タスクで一貫。
  `compute_correlations(trade_prices, watch_prices, watch_names)` は実シグネチャ
  ([src/data/correlation.py:58]) と一致。`PriceData(symbol, df, current_price, fetched_at)` は
  実 dataclass ([price_fetcher.py:22]) と一致。`load_ohlcv(symbol, start, end)` は実シグネチャ
  ([price_store.py:104]) と一致。`_DEFAULT_ROLLING_WINDOW=20` ([correlation.py:20]) で
  min_bars=25。
- **Placeholder:** econ phase 移植は「現 517-641 行をロジック変更なしで移植」と明示
  (実コード行参照 + 移植する import 名の列挙あり)。関数名「(仮称)」は確定名として採用済み。
