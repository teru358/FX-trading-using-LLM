# Day Horizon Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** swing→day horizon 移行に必要な構造変更 7 件 (spec S-1〜S-5) + データ整合 (V-1) + day 設定値を実装する。

**Architecture:** 既定値は全て挙動不変 (swing 互換) に保ち、day 値は settings.yaml でのみ有効化する (spec D-1)。データ層 (PriceStore interval 対応) → MTF 15m 対応 → スケジューラ分粒度 → orchestrator 側 (TTL クランプ / プロンプト / spread 採点) → RAG タグ → 設定値适用、の依存順に進める。

**Tech Stack:** Python / SQLAlchemy 2.0 (SQLite) / pandas resample / pytest。テストは `uv run pytest`。

**Spec:** `docs/superpowers/specs/2026-07-05-day-horizon-migration-design.md` (codex 1巡目反映済み)

**調査済み事実 (plan 作成時に確認):**
- ScheduleConfig は loader で `_from_dict` 構築 ([loader.py:509]) → フィールド追加に loader 変更不要。**OrchestratorConfig はトップレベル列挙構築 ([loader.py:106]) → `plan_ttl_max_hours` は列挙追加が必要**。
- `shadow_triggers` に spread 列なし → S-5 は trigger 時の `spread_pips` 記録から必要 (2 列追加: `shadow_triggers.spread_pips` / `shadow_hindsight_evaluations.spread_cost_r`)。
- orchestrator_store は `_migrate()` を持たない → analysis_store のパターン ([analysis_store.py:51-66]) を移植。
- V-3 (reflection ATR 係数): adaptive_store の学習値は**旧 cycle 経路のみ**が消費 ([trading.py:213])。orchestrator draft は LLM 直指定 + risk gate 検証で ATR mult を使わない → **コード作業不要** (spec §8 OQ2 解消)。
- V-4 (recent_trade_stats window): `_empty_trade_stats()` が window_hours=24 のプレースホルダ ([context_builder.py:263-265]) で実集計は未実装 → **作業不要** (day 想定値 24h と既に一致)。
- V-2 (スコアカード WHERE horizon): スコアカードクエリ自体が未実装 → **作業不要** (規約は spec に記録済み)。
- `to_db_naive_datetime` は [clock.py:64] に存在。
- `test_config_example_sync.py` が settings 例との同期を検証 → config 追加時は example も更新。

---

### Task 1: PriceStore interval 列 (S-4a 前半)

**Files:**
- Modify: `src/data/price_store.py`
- Test: `tests/test_price_store_interval.py` (新規)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_price_store_interval.py
"""PriceStore interval 列 (spec S-4a): 1h/15m がキー共存し混在しないこと。"""
from datetime import datetime

import pandas as pd
import pytest

from src.data.price_store import PriceStore


@pytest.fixture
def store(tmp_path):
    return PriceStore(tmp_path / "price.db")


def _df(times, base=100.0):
    return pd.DataFrame(
        {
            "Open": [base] * len(times),
            "High": [base + 1] * len(times),
            "Low": [base - 1] * len(times),
            "Close": [base + 0.5] * len(times),
            "Volume": [0.0] * len(times),
        },
        index=pd.to_datetime(times),
    )


def test_intervals_do_not_mix(store):
    t = datetime(2026, 7, 1, 10, 0)
    store.upsert_ohlcv("USDJPY=X", _df([t]), interval="1h")
    store.upsert_ohlcv("USDJPY=X", _df([t], base=200.0), interval="15m")

    df_1h = store.load_ohlcv("USDJPY=X", datetime(2026, 7, 1), datetime(2026, 7, 2), interval="1h")
    df_15m = store.load_ohlcv("USDJPY=X", datetime(2026, 7, 1), datetime(2026, 7, 2), interval="15m")
    assert len(df_1h) == 1 and len(df_15m) == 1
    assert df_1h["Close"].iloc[0] == pytest.approx(100.5)
    assert df_15m["Close"].iloc[0] == pytest.approx(200.5)


def test_latest_earliest_are_per_interval(store):
    store.upsert_ohlcv("USDJPY=X", _df([datetime(2026, 7, 1, 10)]), interval="1h")
    store.upsert_ohlcv("USDJPY=X", _df([datetime(2026, 7, 1, 12)]), interval="15m")
    assert store.get_latest_date("USDJPY=X", interval="1h") == datetime(2026, 7, 1, 10)
    assert store.get_latest_date("USDJPY=X", interval="15m") == datetime(2026, 7, 1, 12)
    assert store.get_latest_date("USDJPY=X", interval="4h") is None


def test_default_interval_is_1h_backward_compat(store):
    """interval 未指定の既存呼び出しは 1h として動く (挙動不変)。"""
    t = datetime(2026, 7, 1, 10, 0)
    store.upsert_ohlcv("USDJPY=X", _df([t]))
    df = store.load_ohlcv("USDJPY=X", datetime(2026, 7, 1), datetime(2026, 7, 2))
    assert len(df) == 1
    assert store.get_latest_date("USDJPY=X") == t
    assert store.get_earliest_date("USDJPY=X") == t
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_price_store_interval.py -v`
Expected: FAIL (`upsert_ohlcv() got an unexpected keyword argument 'interval'`)

- [ ] **Step 3: Implement**

`src/data/price_store.py` を以下のとおり変更:

① `_OhlcvRow` に interval PK 列を追加:

```python
class _OhlcvRow(_Base):
    __tablename__ = "ohlcv"

    symbol   = Column(String,   primary_key=True)
    interval = Column(String,   primary_key=True, default="1h")  # "1h" | "15m" 等 (spec S-4a)
    bar_time = Column(DateTime, primary_key=True)  # datetime（各足種共用）
    open     = Column(Float)
    high     = Column(Float)
    low      = Column(Float)
    close    = Column(Float)
    volume   = Column(Float)
```

② `_migrate_schema` に interval 列なし旧テーブルの再構築を追加 (cache なので DROP、既存 date→bar_time 移行と同じパターン):

```python
def _migrate_schema(engine) -> None:
    """旧スキーマを新スキーマに自動移行する (cache のため DROP→再作成)。"""
    inspector = sa_inspect(engine)
    if "ohlcv" in inspector.get_table_names():
        cols = {c["name"] for c in inspector.get_columns("ohlcv")}
        if ("date" in cols and "bar_time" not in cols) or "interval" not in cols:
            with engine.connect() as conn:
                conn.execute(text("DROP TABLE ohlcv"))
                conn.commit()
            logger.warning(
                "OHLCV table migrated: schema changed (cache cleared, will refetch)"
            )
```

③ 4 メソッドに `interval: str = "1h"` キーワード引数を追加し WHERE/INSERT に反映:

```python
    def upsert_ohlcv(self, symbol: str, df: pd.DataFrame, *, interval: str = "1h") -> None:
        ...
                obj = _OhlcvRow(
                    symbol=symbol,
                    interval=interval,
                    bar_time=bar_time,
                    ...
                )
```

```python
    def load_ohlcv(
        self, symbol: str, start: datetime, end: datetime, *, interval: str = "1h"
    ) -> pd.DataFrame:
        ...
            stmt = (
                select(_OhlcvRow)
                .where(_OhlcvRow.symbol == symbol)
                .where(_OhlcvRow.interval == interval)
                .where(_OhlcvRow.bar_time >= start)
                .where(_OhlcvRow.bar_time <= end)
                .order_by(_OhlcvRow.bar_time)
            )
```

`get_latest_date` / `get_earliest_date` も同様に `*, interval: str = "1h"` を追加し `.where(_OhlcvRow.interval == interval)` を挟む。

- [ ] **Step 4: Run tests to verify pass + 既存回帰**

Run: `uv run pytest tests/test_price_store_interval.py tests/test_price_store.py -v` (後者が存在すれば。無ければ `uv run pytest tests -k price_store -v`)
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/data/price_store.py tests/test_price_store_interval.py
git commit -m "feat: PriceStore に interval PK 列を追加 (1h/15m キー共存, spec S-4a)"
```

---

### Task 2: Mt5OhlcvFetcher の interval 刻み差分フェッチ (S-4a 後半)

**Files:**
- Modify: `src/data/mt5_ohlcv_fetcher.py:170-210` (fetch メソッド)
- Test: `tests/test_mt5_ohlcv_fetcher.py` (既存に追加。無ければ新規 `tests/test_mt5_fetch_interval_step.py`)

- [ ] **Step 1: Write the failing test**

`_interval_delta` の純関数テストに加え、**実経路 (fetch の差分起点) の統合テスト**を書く (codex Med#1: 危険箇所は `fetch_from = latest + timedelta(hours=1)` 自体):

```python
# tests/test_mt5_fetch_interval_step.py
"""差分フェッチの刻みが interval 連動であること (spec S-4a: 1h 固定だと 15m で 45 分取り逃がす)。"""
from datetime import datetime, timedelta

import pandas as pd

from src.data.mt5_ohlcv_fetcher import Mt5OhlcvFetcher, _interval_delta


def test_interval_delta_15m():
    assert _interval_delta("15m") == timedelta(minutes=15)


def test_interval_delta_1h():
    assert _interval_delta("1h") == timedelta(hours=1)


def test_interval_delta_1d():
    assert _interval_delta("1d") == timedelta(days=1)


def test_interval_delta_unknown_falls_back_to_1h():
    assert _interval_delta("bogus") == timedelta(hours=1)


def _ohlcv_df(n=30, freq="15min"):
    idx = pd.date_range("2026-07-01", periods=n, freq=freq)
    return pd.DataFrame(
        {"Open": [1.0] * n, "High": [1.0] * n, "Low": [1.0] * n,
         "Close": [1.0] * n, "Volume": [0.0] * n},
        index=idx,
    )


class _FakeStore:
    """interval 伝搬と差分起点を記録する fake PriceStore。"""

    def __init__(self, latest):
        self._latest = latest
        self.calls = []

    def get_latest_date(self, symbol, *, interval="1h"):
        self.calls.append(("latest", interval))
        return self._latest

    def get_earliest_date(self, symbol, *, interval="1h"):
        # hist_start より古い earliest を返し、過去方向補完 (Step1) をスキップさせる
        return datetime(2020, 1, 1)

    def load_ohlcv(self, symbol, start, end, *, interval="1h"):
        self.calls.append(("load", interval))
        return _ohlcv_df()  # >= 20 本で DB 経路から return させる

    def upsert_ohlcv(self, symbol, df, *, interval="1h"):
        self.calls.append(("upsert", interval))


def test_diff_fetch_starts_at_latest_plus_interval(monkeypatch):
    """interval='15m' のとき差分起点 = latest + 15min であること (実経路)。"""
    latest = datetime(2026, 7, 1, 10, 0)
    store = _FakeStore(latest)
    fetcher = Mt5OhlcvFetcher(bridge_url="http://x:8812", request_timeout=5.0)

    captured = {}

    def _fake_fetch_and_upsert(symbol, start, end, *, interval, price_store):
        captured["start"] = start
        captured["interval"] = interval

    monkeypatch.setattr(fetcher, "_fetch_and_upsert", _fake_fetch_and_upsert)

    fetcher.fetch("USDJPY=X", period="7d", interval="15m", price_store=store)

    assert captured["start"] == latest + timedelta(minutes=15)
    assert captured["interval"] == "15m"
    # store の全 API に interval="15m" が伝搬していること
    assert ("latest", "15m") in store.calls
    assert ("load", "15m") in store.calls
```

注: `_fetch_and_upsert` の実シグネチャ (positional/keyword) は実装に合わせて fake を調整する。fetcher 構築は既存 `tests/test_mt5_ohlcv_fetcher.py:37` と同形。

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mt5_fetch_interval_step.py -v`
Expected: FAIL (`ImportError: cannot import name '_interval_delta'`)

- [ ] **Step 3: Implement**

`src/data/mt5_ohlcv_fetcher.py` にモジュール関数を追加:

```python
def _interval_delta(interval: str) -> timedelta:
    """interval 文字列 ("15m"/"1h"/"1d") を差分フェッチの刻みに変換する。

    不明な形式は安全側 (従来の 1h) に倒す。
    """
    try:
        if interval.endswith("m"):
            return timedelta(minutes=int(interval[:-1]))
        if interval.endswith("h"):
            return timedelta(hours=int(interval[:-1]))
        if interval.endswith("d"):
            return timedelta(days=int(interval[:-1]))
    except ValueError:
        pass
    return timedelta(hours=1)
```

`fetch()` 内の 3 箇所を変更:

```python
            # Step 1: 過去方向の補完
            latest = price_store.get_latest_date(symbol, interval=interval)
            ...
            earliest = price_store.get_earliest_date(symbol, interval=interval)
```

```python
            # Step 2: 差分フェッチ (最新バー以降、interval 刻み)
            latest = price_store.get_latest_date(symbol, interval=interval)
            if latest is not None:
                fetch_from = latest + _interval_delta(interval)
```

`load_ohlcv` / `upsert_ohlcv` 呼び出し (fetch 内とフォールバック経路) にも `interval=interval` を渡す。`_fetch_and_upsert` が内部で `price_store.upsert_ohlcv` を呼んでいる場合はそこにも `interval=interval` を伝搬する (シグネチャに interval が既にあるので引数を追加するだけ)。

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_mt5_fetch_interval_step.py tests -k mt5_ohlcv -v`
Expected: PASS (既存 mt5 fetcher テストも緑)

- [ ] **Step 5: Commit**

```bash
git add src/data/mt5_ohlcv_fetcher.py tests/test_mt5_fetch_interval_step.py
git commit -m "feat: MT5 差分フェッチの刻みを interval 連動に (15m の取り逃がし防止, spec S-4a)"
```

---

### Task 3: resample の 15m/30m 対応 + 基底足パラメータ化 (S-4b 前半)

**Files:**
- Modify: `src/data/resample.py`
- Test: `tests/test_resample.py` (既存に追加。無ければ新規)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_resample_15m.py
"""resample の 15m 基底足対応 (spec S-4b / codex High#1)。"""
import pandas as pd
import pytest

from src.data.resample import resample_ohlcv


def _df_15m(n=8):
    idx = pd.date_range("2026-07-01 09:00", periods=n, freq="15min")
    return pd.DataFrame(
        {
            "Open": [float(i) for i in range(n)],
            "High": [float(i) + 1 for i in range(n)],
            "Low": [float(i) - 1 for i in range(n)],
            "Close": [float(i) + 0.5 for i in range(n)],
            "Volume": [1.0] * n,
        },
        index=idx,
    )


def test_identity_when_target_equals_base():
    df = _df_15m()
    out = resample_ohlcv(df, "15m", base_interval="15m")
    assert len(out) == len(df)


def test_15m_base_to_1h():
    df = _df_15m(8)  # 2 時間分
    out = resample_ohlcv(df, "1h", base_interval="15m")
    assert len(out) == 2
    # 1 本目 = 09:00-09:45 の 4 本: Open=最初, Close=最後, High=max, Low=min, Vol=sum
    assert out["Open"].iloc[0] == 0.0
    assert out["Close"].iloc[0] == 3.5
    assert out["High"].iloc[0] == 4.0
    assert out["Low"].iloc[0] == -1.0
    assert out["Volume"].iloc[0] == 4.0


def test_downsample_below_base_raises():
    df = _df_15m()
    with pytest.raises(ValueError):
        resample_ohlcv(df, "15m", base_interval="1h")  # 1h 基底から 15m は作れない


def test_default_base_is_1h_backward_compat():
    idx = pd.date_range("2026-07-01 00:00", periods=8, freq="1h")
    df = pd.DataFrame(
        {"Open": range(8), "High": range(8), "Low": range(8),
         "Close": range(8), "Volume": [1.0] * 8},
        index=idx,
    ).astype(float)
    out = resample_ohlcv(df, "4h")
    assert len(out) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_resample_15m.py -v`
Expected: FAIL (`resample_ohlcv() got an unexpected keyword argument 'base_interval'` / 15m KeyError)

- [ ] **Step 3: Implement**

`src/data/resample.py`:

```python
# pandas の resample rule との対応。分足は "min" 単位 (pandas 2.x で "T" は非推奨)。
_RULE_MAP: dict[str, str] = {
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "6h": "6h",
    "8h": "8h",
    "12h": "12h",
    "1d": "1D",
    "1w": "1W",
}

# interval → 分数 (粒度比較用)。_RULE_MAP と同じキーを持つ。
_INTERVAL_MINUTES: dict[str, int] = {
    "15m": 15, "30m": 30, "1h": 60, "2h": 120, "4h": 240,
    "6h": 360, "8h": 480, "12h": 720, "1d": 1440, "1w": 10080,
}


def resample_ohlcv(
    df_base: pd.DataFrame, interval: str, base_interval: str = "1h"
) -> pd.DataFrame:
    """基底足 OHLCV を指定タイムフレームへ集約する。

    Args:
        df_base: 基底足の OHLCV DataFrame (既定 1h、day 移行後は 15m)。
        interval: 目的タイムフレーム。
        base_interval: df_base の足種。interval と同じなら恒等 (copy)。

    Raises:
        ValueError: 未知 interval、または target が base より細かい場合。
    """
    if interval not in _RULE_MAP:
        raise ValueError(f"unsupported interval: {interval!r}")
    if base_interval not in _INTERVAL_MINUTES:
        raise ValueError(f"unsupported base_interval: {base_interval!r}")
    if interval == base_interval:
        return df_base.copy()
    if _INTERVAL_MINUTES[interval] < _INTERVAL_MINUTES[base_interval]:
        raise ValueError(
            f"cannot resample {base_interval} base down to finer {interval}"
        )
    # (以降は既存の resample 本体をそのまま利用 — rule = _RULE_MAP[interval])
```

既存本体の「`interval == "1h"` なら copy」という恒等判定は上記の `interval == base_interval` に置き換える (docstring の 1h 前提記述も更新)。

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_resample_15m.py tests -k resample -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/data/resample.py tests/test_resample_15m.py
git commit -m "feat: resample に 15m/30m と基底足パラメータを追加 (spec S-4b)"
```

---

### Task 4: MTF の 15m 対応 (S-4b 後半)

**Files:**
- Modify: `src/data/mtf.py` (`compute_mtf_summaries` / `_bars_per_day_for_interval`)
- Modify: `src/jobs/technical_collector.py:200` (呼び出し側で base_interval を渡す)
- Test: `tests/test_mtf_15m.py` (新規)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mtf_15m.py
"""MTF の 15m 対応 (spec S-4b / codex High#1): bars_per_day と基底足伝搬。"""
from src.data.mtf import _bars_per_day_for_interval


def test_bars_per_day_15m():
    assert _bars_per_day_for_interval("15m") == 96


def test_bars_per_day_30m():
    assert _bars_per_day_for_interval("30m") == 48


def test_bars_per_day_existing_unchanged():
    assert _bars_per_day_for_interval("1d") == 1
    assert _bars_per_day_for_interval("1h") == 24
    assert _bars_per_day_for_interval("4h") == 6


def test_compute_mtf_summaries_from_15m_base():
    """15m 基底で short=15m / medium=1h の両 summary が生成される (実経路、codex Med#2)。"""
    import pandas as pd

    from src.config.schema import AnalysisConfig
    from src.data.mtf import compute_mtf_summaries

    # 4 日分の 15m OHLCV (384 本) — 各 TF の最低本数 (20) を満たす
    n = 384
    idx = pd.date_range("2026-06-27", periods=n, freq="15min")
    close = [150.0 + (i % 10) * 0.01 for i in range(n)]
    df = pd.DataFrame(
        {"Open": close, "High": [c + 0.02 for c in close],
         "Low": [c - 0.02 for c in close], "Close": close, "Volume": [1.0] * n},
        index=idx,
    )
    timeframes = {
        "long":   {"lookback_days": 15, "interval": "4h",  "enabled": False},
        "medium": {"lookback_days": 4,  "interval": "1h",  "enabled": True},
        "short":  {"lookback_days": 1,  "interval": "15m", "enabled": True},
    }
    summaries = compute_mtf_summaries(
        df, AnalysisConfig(), timeframes, base_interval="15m"
    )
    assert "short" in summaries
    assert "medium" in summaries
```

注: `AnalysisConfig()` が引数なしで構築できない場合は既存 MTF テストの cfg 生成パターンに合わせる。

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mtf_15m.py -v`
Expected: FAIL (`_bars_per_day_for_interval("15m")` が fallback の 24 を返す)

- [ ] **Step 3: Implement**

① `src/data/mtf.py` の `_bars_per_day_for_interval` に "Nm" 分岐を追加 ("Nh" 分岐の**前**に置く — "m" で終わる判定が先):

```python
def _bars_per_day_for_interval(interval: str) -> int:
    """interval 文字列から 1 日あたりのおおよそのバー数を返す。"""
    if interval == "1d":
        return 1
    if interval == "1h":
        return 24
    # "15m" / "30m" などの "Nm" 形式
    if interval.endswith("m"):
        try:
            m = int(interval[:-1])
            return max(1, (24 * 60) // m)
        except ValueError:
            pass
    # "4h" / "8h" などの "Nh" 形式
    if interval.endswith("h"):
        try:
            h = int(interval[:-1])
            return max(1, 24 // h)
        except ValueError:
            pass
    return 24  # fallback
```

② `compute_mtf_summaries` のシグネチャに `base_interval: str = "1h"` を追加し、内部の `resample_ohlcv(df_1h, interval)` を `resample_ohlcv(df_1h, interval, base_interval=base_interval)` に変更 (引数名 `df_1h` は既存のまま触らない — 呼び出し側の意味だけ変わる)。

③ `src/jobs/technical_collector.py` の呼び出し (line ~200):

```python
    summaries = compute_mtf_summaries(
        df_1h, config.analysis, timeframes,
        base_interval=config.trading.ohlcv_interval,
    )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_mtf_15m.py tests -k "mtf or multi_tf" -v`
Expected: PASS (既存 MTF テストは base_interval 既定 "1h" で挙動不変)

- [ ] **Step 5: Commit**

```bash
git add src/data/mtf.py src/jobs/technical_collector.py tests/test_mtf_15m.py
git commit -m "feat: MTF に 15m bars_per_day と基底足伝搬を追加 (spec S-4b)"
```

---

### Task 5: FX staleness の config 化 (S-3)

**Files:**
- Modify: `src/config/schema.py` (ScheduleConfig — `_from_dict` 構築のため loader 変更不要)
- Modify: `src/jobs/technical_collector.py:54-61`
- Test: `tests/test_technical_staleness_config.py` (新規)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_technical_staleness_config.py
"""FX staleness の config 化 (spec S-3): 既定 360min = 現行 6h と等価で挙動不変。"""
from datetime import timedelta

from src.config.schema import AppConfig, ScheduleConfig
from src.jobs.technical_collector import _max_staleness_for


class _FxInst:
    asset_type = "fx"


class _EtfInst:
    asset_type = "equity"


def test_default_is_6h_equivalent():
    cfg = AppConfig()
    assert _max_staleness_for(_FxInst(), cfg) == timedelta(hours=6)


def test_day_value_90min():
    cfg = AppConfig()
    cfg.schedule.technical_max_staleness_fx_minutes = 90
    assert _max_staleness_for(_FxInst(), cfg) == timedelta(minutes=90)


def test_watch_side_unchanged():
    cfg = AppConfig()
    cfg.schedule.technical_max_staleness_fx_minutes = 90
    assert _max_staleness_for(_EtfInst(), cfg) == timedelta(hours=120)
```

注: `AppConfig()` が引数なしで構築できない場合は、既存テスト (例 `tests/test_config_loader.py`) の AppConfig fixture 生成パターンに合わせること。

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_technical_staleness_config.py -v`
Expected: FAIL (`_max_staleness_for() takes 1 positional argument but 2 were given` / schema に新フィールドなし)

- [ ] **Step 3: Implement**

① `src/config/schema.py` の `ScheduleConfig` に追加:

```python
    # FX technical 鮮度閾値 (分)。既定 360 = 従来の 6h 定数と等価 (挙動不変)。
    # day horizon では 90 に短縮する (spec 2026-07-05 S-3)。watch 側 (120h) は定数のまま。
    technical_max_staleness_fx_minutes: int = 360
```

② `src/jobs/technical_collector.py`:

```python
def _max_staleness_for(inst: InstrumentConfig, config: AppConfig) -> timedelta:
    """銘柄タイプ別の stale 閾値を返す (FX は config、watch は定数)。"""
    if inst.asset_type == "fx":
        return timedelta(minutes=config.schedule.technical_max_staleness_fx_minutes)
    return _MAX_STALENESS_WATCH
```

`_MAX_STALENESS_FX` 定数は `_is_price_data_stale` のデフォルト引数として残す (他呼び出しの互換)。モジュール内の `_max_staleness_for(inst)` 呼び出し箇所を全て `_max_staleness_for(inst, config)` に更新する (grep: `_max_staleness_for(` — config はどの呼び出し元スコープにも既に存在する)。

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_technical_staleness_config.py tests -k "staleness or technical_collector" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/config/schema.py src/jobs/technical_collector.py tests/test_technical_staleness_config.py
git commit -m "feat: FX staleness 閾値を config 化 (既定 360min で挙動不変, spec S-3)"
```

---

### Task 6: スケジューラ/cadence の分粒度対応 (S-4c)

**Files:**
- Modify: `src/config/schema.py` (ScheduleConfig)
- Modify: `src/jobs/technical_schedule.py`
- Modify: `main.py:102` (cadence base) / `main.py:239` (dispatch times)
- Test: `tests/test_technical_schedule_minutes.py` (新規)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_technical_schedule_minutes.py
"""分粒度スケジュール (spec S-4c / codex High#3)。"""
from src.jobs.technical_schedule import technical_times_for, technical_times_for_minutes
from src.config.schema import ScheduleConfig


def test_minutes_30_generates_hh_mm():
    times = technical_times_for_minutes(30)
    assert len(times) == 48
    assert times[0] == "00:00"
    assert times[1] == "00:30"
    assert "12:30" in times


def test_minutes_60_equals_hourly():
    assert technical_times_for_minutes(60) == technical_times_for(1)


def test_minutes_nonpositive_falls_back_to_60():
    assert technical_times_for_minutes(0) == technical_times_for_minutes(60)


def test_effective_trade_interval_seconds():
    cfg = ScheduleConfig()
    assert cfg.effective_trade_interval_seconds() == 3600  # hours=1, minutes 未設定
    cfg.technical_trade_interval_minutes = 30
    assert cfg.effective_trade_interval_seconds() == 1800  # minutes 優先


def test_effective_trade_times_minutes_priority():
    cfg = ScheduleConfig()
    cfg.technical_trade_interval_minutes = 30
    assert cfg.effective_trade_times() == technical_times_for_minutes(30)
    cfg.technical_trade_interval_minutes = None
    assert cfg.effective_trade_times() == technical_times_for(1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_technical_schedule_minutes.py -v`
Expected: FAIL (ImportError: `technical_times_for_minutes`)

- [ ] **Step 3: Implement**

① `src/jobs/technical_schedule.py` に追加:

```python
def technical_times_for_minutes(interval_minutes: int) -> list[str]:
    """指定間隔 (分) の "HH:MM" 時刻リストを返す。

    interval_minutes=30 → 48 個。0/負値は 60 (毎時) に倒す。
    """
    step = interval_minutes if interval_minutes > 0 else 60
    return [
        f"{m // 60:02d}:{m % 60:02d}" for m in range(0, 24 * 60, step)
    ]
```

② `src/config/schema.py` の `ScheduleConfig` にフィールド + ヘルパを追加 (`_from_dict` 構築のため loader 変更不要):

```python
    # technical trade 収集の分粒度 interval。設定時は technical_trade_interval_hours
    # より優先。None (既定) なら従来の hours を使う = 挙動不変 (spec 2026-07-05 S-4c)。
    technical_trade_interval_minutes: int | None = None

    def effective_trade_interval_seconds(self) -> int:
        """cadence base 用の有効 trade 収集間隔 (秒)。minutes 優先。"""
        if self.technical_trade_interval_minutes:
            return self.technical_trade_interval_minutes * 60
        return self.technical_trade_interval_hours * 3600

    def effective_trade_times(self) -> list[str]:
        """schedule 登録用の有効 trade 収集時刻リスト。minutes 優先。"""
        from src.jobs.technical_schedule import technical_times_for, technical_times_for_minutes
        if self.technical_trade_interval_minutes:
            return technical_times_for_minutes(self.technical_trade_interval_minutes)
        return technical_times_for(self.technical_trade_interval_hours)
```

③ `main.py` の 2 箇所を差し替え:

```python
    # main.py:102 付近 (cadence base)
    trade_base = config.schedule.effective_trade_interval_seconds()
```

```python
    # main.py:239 付近 (union dispatch)
    _trade_tech_set = set(config.schedule.effective_trade_times())
```

watch 側 (`technical_watch_interval_hours`) は変更しない。

**注意:** `schedule` ライブラリの毎日時刻登録は "HH:MM" 形式をそのまま受けるため、union dispatch の時刻集合が "HH:MM" になっても登録処理は変更不要 (既存 news が "HH:MM" 分単位を使用済み)。

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_technical_schedule_minutes.py tests -k "schedule or cadence" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/jobs/technical_schedule.py src/config/schema.py main.py tests/test_technical_schedule_minutes.py
git commit -m "feat: technical trade 収集の分粒度 interval (minutes 優先, spec S-4c)"
```

---

### Task 7: plan TTL クランプ (S-1)

**Files:**
- Modify: `src/config/schema.py` (OrchestratorConfig) + `src/config/loader.py:106` (**列挙追加必須**)
- Modify: `src/orchestrator/planning_pipeline.py` (draft 直後にクランプ)
- Test: `tests/test_plan_ttl_clamp.py` (新規)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_plan_ttl_clamp.py
"""plan TTL クランプ (spec S-1 / codex Med#1): aware→naive 正規化 + 上限切り詰め。"""
from datetime import datetime, timedelta, timezone

from src.orchestrator.planning_pipeline import clamp_draft_ttl
from src.orchestrator.schemas import ExecutionPlanDraft, EntryCondition, InvalidationCondition
from src.utils.clock import db_now


def _draft(expires_at):
    return ExecutionPlanDraft(
        direction="long",
        entry_conditions=[EntryCondition.from_dict(
            {"type": "price_at_or_below", "value": 150.0})],
        action={"sl": 149.0, "tp": 152.0, "size_policy": "risk", "rr": 2.0, "comment": ""},
        invalidation=[InvalidationCondition.from_dict(
            {"type": "price_below", "value": 148.0})],
        expires_at=expires_at,
        reasoning_summary="test",
    )


def test_clamp_disabled_by_default_keeps_naive_expiry():
    exp = db_now() + timedelta(days=30)
    out = clamp_draft_ttl(_draft(exp), max_hours=0)
    assert out.expires_at == exp  # max_hours=0 = クランプ無効 (挙動不変)


def test_aware_expiry_is_normalized_to_naive_local():
    aware = datetime.now(timezone.utc) + timedelta(hours=2)
    out = clamp_draft_ttl(_draft(aware), max_hours=0)
    assert out.expires_at.tzinfo is None  # naive local (DB 規約)


def test_over_limit_is_clamped():
    exp = db_now() + timedelta(hours=48)
    out = clamp_draft_ttl(_draft(exp), max_hours=8)
    assert out.expires_at <= db_now() + timedelta(hours=8, seconds=5)


def test_under_limit_unchanged():
    exp = db_now() + timedelta(hours=3)
    out = clamp_draft_ttl(_draft(exp), max_hours=8)
    assert out.expires_at == exp
```

注: `EntryCondition.from_dict` / `InvalidationCondition.from_dict` の正確な dict 形は `src/orchestrator/schemas.py` の vocabulary に合わせること (ExecutionOpinionAgent の `_SYSTEM` に列挙がある)。

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_plan_ttl_clamp.py -v`
Expected: FAIL (ImportError: `clamp_draft_ttl`)

- [ ] **Step 3: Implement**

① `src/config/schema.py` の `OrchestratorConfig` に追加:

```python
    # plan expires_at の上限クランプ (時間)。0 = 無効 (従来挙動)。day 運用では 8 を
    # 設定し、LLM が長すぎる TTL を出しても決定的に切り詰める (spec 2026-07-05 S-1)。
    plan_ttl_max_hours: int = 0
```

② `src/config/loader.py` の `_build_orchestrator_config` の列挙に追加 (**忘れると読まれない — `execution_opinion_recheck_enabled` 列挙漏れバグの前例**):

```python
        plan_ttl_max_hours=data.get("plan_ttl_max_hours", 0),
```

③ `src/orchestrator/planning_pipeline.py` にモジュール関数を追加:

```python
from dataclasses import replace
from datetime import timedelta

from src.utils.clock import db_now, to_db_naive_datetime


def clamp_draft_ttl(draft: "ExecutionPlanDraft", *, max_hours: int) -> "ExecutionPlanDraft":
    """draft.expires_at を DB 規約 (naive local) に正規化し、上限でクランプする。

    LLM 出力の expires_at は +00:00 付きだと aware になる (schemas.from_llm_json)。
    naive DB 値と比較する前に必ず正規化する (codex Med#1)。max_hours<=0 はクランプ
    無効だが正規化は常に行う (aware のまま保存すると runtime の naive 比較が壊れる)。
    """
    normalized = to_db_naive_datetime(draft.expires_at)
    if normalized != draft.expires_at:
        draft = replace(draft, expires_at=normalized)
    if max_hours <= 0:
        return draft
    cap = db_now() + timedelta(hours=max_hours)
    if draft.expires_at > cap:
        logger.info(
            "[ORCH] plan TTL clamped: %s -> %s (max %dh)",
            draft.expires_at, cap, max_hours,
        )
        draft = replace(draft, expires_at=cap)
    return draft
```

`ExecutionPlanDraft` は `@dataclass` (frozen ではない) なので `replace` が使える。

④ `_pipeline` の draft 受領直後 (再起案ループ内、`self._persist_opinion` の**前**) に挿入 — これでクランプ後値が opinion 保存 (:152) と plan 保存 (:250) の両方に流れる:

```python
            draft = await self._exec.draft(
                pair=pair, direction=direction, context=context,
                revision_feedback=feedback,
            )
            draft = clamp_draft_ttl(draft, max_hours=self._config.plan_ttl_max_hours)
            self._persist_opinion(run_id, pair, draft)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_plan_ttl_clamp.py tests -k "planning_pipeline or config_loader" -v`
Expected: PASS (loader テストで plan_ttl_max_hours の読み込みも確認できればなお良い — `tests/test_config_loader.py` に 1 ケース追加: yaml `orchestrator: {plan_ttl_max_hours: 8}` → 8)

- [ ] **Step 5: Commit**

```bash
git add src/config/schema.py src/config/loader.py src/orchestrator/planning_pipeline.py tests/test_plan_ttl_clamp.py tests/test_config_loader.py
git commit -m "feat: plan TTL クランプ + aware/naive 正規化 (spec S-1, codex Med#1)"
```

---

### Task 8: プロンプトの horizon 指針 (S-2)

**Files:**
- Modify: `src/orchestrator/context_builder.py:149` (policy に plan_ttl_max_hours を追加)
- Modify: `src/orchestrator/execution_opinion_agent.py` (`_build_user_prompt`)
- Modify: `src/orchestrator/planner_agent.py` (scan/final の user prompt)
- Test: `tests/test_prompt_horizon_guidance.py` (新規)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prompt_horizon_guidance.py
"""プロンプトの horizon 指針 (spec S-2): day/swing で指針文が切り替わる。"""
from src.orchestrator.execution_opinion_agent import ExecutionOpinionAgent, _horizon_guidance
from src.orchestrator.planner_agent import _horizon_guidance as planner_guidance


def _ctx(horizon, ttl=8):
    return {"policy": {"trade_horizon": horizon, "advice_memo": None,
                       "plan_ttl_max_hours": ttl}}


def test_day_guidance_mentions_ttl_and_atr():
    text = _horizon_guidance(_ctx("day"))
    assert "DAY" in text
    assert "8 hours" in text
    assert "1h ATR" in text
    assert "RR >= 2" in text


def test_swing_guidance_mentions_days():
    text = _horizon_guidance(_ctx("swing"))
    assert "SWING" in text


def test_exec_user_prompt_contains_guidance():
    class _Llm:
        client = None
        temperature = 0.2
    agent = ExecutionOpinionAgent(_Llm())
    prompt = agent._build_user_prompt("USDJPY=X", "long", _ctx("day"), None)
    assert "DAY" in prompt


def test_planner_guidance_shared_semantics():
    assert "DAY" in planner_guidance(_ctx("day"))
    assert "SWING" in planner_guidance(_ctx("swing"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_prompt_horizon_guidance.py -v`
Expected: FAIL (ImportError: `_horizon_guidance`)

- [ ] **Step 3: Implement**

① `src/orchestrator/context_builder.py` の policy ブロック (:149) に追加:

```python
            "policy": {
                "trade_horizon": self._config.policy.trade_horizon,
                "advice_memo": self._config.policy.advice_memo or None,
                "plan_ttl_max_hours": self._config.plan_ttl_max_hours,
            },
```

② `src/orchestrator/execution_opinion_agent.py` にモジュール関数を追加し `_build_user_prompt` から使う:

```python
def _horizon_guidance(context: dict[str, Any]) -> str:
    """context.policy から horizon 別の運用指針文を組む (spec 2026-07-05 S-2)。"""
    policy = context.get("policy") or {}
    horizon = policy.get("trade_horizon", "swing")
    if horizon == "day":
        ttl = policy.get("plan_ttl_max_hours") or 0
        ttl_line = (
            f" expires_at must be within {ttl} hours from now." if ttl else ""
        )
        return (
            "Operating horizon: DAY trade. The plan must complete within hours,"
            " never overnight." + ttl_line +
            " Entry conditions must be reachable from the current price"
            " (guideline: 0.3-1.5x the 1h ATR). Keep RR >= 2."
        )
    return (
        "Operating horizon: SWING trade. Plans may span days."
        " Prefer pullback/retest conditional entries over chasing extended moves."
    )
```

`_build_user_prompt` の `lines` に 1 行追加 (`decision_context:` の前):

```python
        lines = [
            f"pair: {pair}",
            f"intended direction: {direction}",
            _horizon_guidance(context),
            "decision_context:",
            json.dumps(_compact_context(context), ensure_ascii=False),
        ]
```

③ `src/orchestrator/planner_agent.py` に同名のモジュール関数を追加 (execution_opinion_agent から import すると循環しないならば `from src.orchestrator.execution_opinion_agent import _horizon_guidance` の再利用でも可 — 循環 import になる場合のみ複製し、テストが両方を検証する)。`scan_opportunity` / `final_decision` の user prompt 組み立ての `"decision_context:"` の前に `_horizon_guidance(context)` を 1 行挿入する。

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_prompt_horizon_guidance.py tests -k "planner_agent or execution_opinion" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/context_builder.py src/orchestrator/execution_opinion_agent.py src/orchestrator/planner_agent.py tests/test_prompt_horizon_guidance.py
git commit -m "feat: planner/execution プロンプトに horizon 別指針を追加 (spec S-2)"
```

---

### Task 9: shadow_triggers への spread_pips 記録 (S-5 前半)

**Files:**
- Modify: `src/data/orchestrator_store.py` (`_ShadowTrigger` ORM + `record_shadow_trigger` + `_migrate` 新設)
- Modify: `src/orchestrator/runtime.py:673` (trigger 記録時に quote の spread を渡す)
- Test: `tests/test_orchestrator_store.py` (既存に追加) または新規 `tests/test_trigger_spread_record.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trigger_spread_record.py
"""trigger 時の spread_pips 記録 (spec S-5): hindsight spread 採点の入力。"""
from datetime import datetime

import pytest

from src.data.orchestrator_store import OrchestratorStore


@pytest.fixture
def store(tmp_path):
    return OrchestratorStore(tmp_path / "orch.db")


def test_record_shadow_trigger_stores_spread_pips(store):
    trig_id = store.record_shadow_trigger(
        plan_id=1, decision_id=None, pair="USDJPY=X", direction="long",
        triggered_at=datetime(2026, 7, 1, 10, 0), trigger_price=150.0,
        sl=149.5, tp=151.0, rr=2.0, snapshot_id=None,
        risk_gate_result=None, spread_pips=1.2,
    )
    row = store.get_shadow_trigger_by_id(trig_id)  # get_shadow_trigger は plan_id 用 (codex Low#1)
    assert row.spread_pips == pytest.approx(1.2)


def test_spread_pips_optional_backward_compat(store):
    trig_id = store.record_shadow_trigger(
        plan_id=2, decision_id=None, pair="USDJPY=X", direction="long",
        triggered_at=datetime(2026, 7, 1, 10, 0), trigger_price=150.0,
        sl=149.5, tp=151.0, rr=2.0, snapshot_id=None, risk_gate_result=None,
    )
    row = store.get_shadow_trigger_by_id(trig_id)
    assert row.spread_pips is None
```

注: `record_shadow_trigger` の実シグネチャ ([orchestrator_store.py:901]) に合わせて既存必須引数を調整すること (このテストの引数名は実装時に実物と突き合わせる)。trigger 行の取得ヘルパが無ければ Session direct query でも可。

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_trigger_spread_record.py -v`
Expected: FAIL (`unexpected keyword argument 'spread_pips'`)

- [ ] **Step 3: Implement**

① `_ShadowTrigger` に列追加:

```python
    spread_pips           = Column(Float)   # trigger 時の spread (spec 2026-07-05 S-5)
```

② `OrchestratorStore.__init__` に `_migrate()` を新設して呼ぶ (analysis_store のパターンを移植 — 冪等 ALTER):

```python
    def _migrate(self) -> None:
        """既存テーブルに新カラムを追加する (ALTER TABLE、既にあれば何もしない)。"""
        migrations = [
            ("shadow_triggers", "spread_pips", "FLOAT"),
            ("shadow_hindsight_evaluations", "spread_cost_r", "FLOAT"),
        ]
        with self._engine.connect() as conn:
            for table, col, col_type in migrations:
                try:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
                    conn.commit()
                    logger.info(f"[MIGRATE] Added {table}.{col}")
                except Exception:
                    pass  # カラムが既に存在
```

(`spread_cost_r` の ALTER もここで一緒に入れる — Task 10 の ORM 列と対になる。`text` import を確認。)

③ `record_shadow_trigger` に `spread_pips: float | None = None` を追加し ORM へ渡す。

④ `src/orchestrator/runtime.py` の `record_shadow_trigger(` 呼び出し (:673) に追加:

```python
                spread_pips=self._spread_pips(pair, quote.spread),
```

**根拠 (codex High#1):** `QuoteSnapshot` は `spread` (価格差) しか持たず `spread_pips` 属性は存在しない ([context_builder.py:46])。`getattr(quote, "spread_pips", None)` と書くと常に None になり S-5 全体が死ぬ。runtime には既存の pips 変換ヘルパ `_spread_pips(pair, spread)` ([runtime.py:453], None 透過) があるのでそれを使う。

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_trigger_spread_record.py tests -k orchestrator_store -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/data/orchestrator_store.py src/orchestrator/runtime.py tests/test_trigger_spread_record.py
git commit -m "feat: shadow trigger に spread_pips を記録 + orchestrator_store に冪等 migrate (spec S-5)"
```

---

### Task 10: hindsight の spread 込み採点 (S-5 後半)

**Files:**
- Modify: `src/orchestrator/hindsight_evaluator.py` (`HindsightResult` + `evaluate`)
- Modify: `src/data/orchestrator_store.py` (`_ShadowHindsightEvaluation` ORM 列 + `update_hindsight_evaluation`)
- Modify: `src/orchestrator/runtime.py:481-501` (evaluate 呼び出しと update に spread を配線)
- Modify: `src/orchestrator/shadow_notifier.py` (HindsightInfo + 表示 1 項目)
- Test: `tests/test_hindsight_spread.py` (新規)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hindsight_spread.py
"""hindsight の spread 込み採点 (spec S-5 / D-8): pnl_r は控除後、spread_cost_r に内訳。"""
from datetime import datetime

import pandas as pd
import pytest

from src.orchestrator.hindsight_evaluator import HindsightEvaluator


def _provider_factory(df):
    return lambda pair, start, end: df


def _df_flat(close=150.0, n=8):
    idx = pd.date_range("2026-07-01 10:00", periods=n, freq="1h")
    return pd.DataFrame(
        {"Open": [close] * n, "High": [close + 0.1] * n,
         "Low": [close - 0.1] * n, "Close": [close] * n, "Volume": [0.0] * n},
        index=idx,
    )


def _evaluate(spread_pips):
    ev = HindsightEvaluator(ohlcv_provider=_provider_factory(_df_flat()))  # ctor は keyword-only
    return ev.evaluate(
        pair="USDJPY=X", direction="long", trigger_price=150.0,
        sl=149.5, tp=151.0,
        triggered_at=datetime(2026, 7, 1, 10, 0), horizon_seconds=3600 * 8,
        spread_pips=spread_pips,
    )


def test_spread_cost_deducted_from_pnl_r():
    # risk = 0.5, USDJPY pip = 0.01。spread 5pips = 0.05 → cost_r = 0.1
    with_spread = _evaluate(5.0)
    without = _evaluate(None)
    assert with_spread.spread_cost_r == pytest.approx(0.1)
    assert with_spread.pnl_r == pytest.approx(without.pnl_r - 0.1)


def test_no_spread_keeps_gross_and_zero_cost():
    res = _evaluate(None)
    assert res.spread_cost_r is None
    # mark-to-market フラットなので gross pnl_r ≈ 0
    assert res.pnl_r == pytest.approx(0.0, abs=1e-9)
```

(コンストラクタは `HindsightEvaluator(*, ohlcv_provider)` の keyword-only — [hindsight_evaluator.py:47] 確認済み。)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_hindsight_spread.py -v`
Expected: FAIL (`unexpected keyword argument 'spread_pips'`)

- [ ] **Step 3: Implement**

① `HindsightResult` dataclass に `spread_cost_r: float | None = None` を追加。

② `evaluate(...)` に `spread_pips: float | None = None` を追加し、`risk` 確定後に cost を計算、**全 pnl_r 分岐の後**に控除:

```python
        from src.orchestrator.watch_evaluator import _pip_size_for

        spread_cost_r: float | None = None
        if spread_pips is not None and spread_pips > 0:
            spread_cost_r = (spread_pips * _pip_size_for(pair)) / risk
```

`return HindsightResult(...)` の直前 (has_data=True の経路) に:

```python
        if spread_cost_r is not None:
            pnl_r -= spread_cost_r
```

`HindsightResult(..., spread_cost_r=spread_cost_r)` を渡す。has_data=False の早期 return は変更しない。

③ `_ShadowHindsightEvaluation` ORM に列追加 (ALTER は Task 9 の `_migrate` で追加済み):

```python
    spread_cost_r     = Column(Float)   # R 換算の spread コスト内訳 (spec 2026-07-05 S-5)
```

④ `update_hindsight_evaluation` に `spread_cost_r: float | None = None` を追加し `ev.spread_cost_r = spread_cost_r` を保存。

⑤ `src/orchestrator/runtime.py` の hindsight poll (:481-501):
- trigger 行から `spread_pips=getattr(trig, "spread_pips", None)` を `self._hindsight.evaluate(...)` に渡す。
- 成功時の `update_hindsight_evaluation(...)` に `spread_cost_r=result.spread_cost_r` を渡す。

⑥ `src/orchestrator/shadow_notifier.py` の `HindsightInfo` dataclass に `spread_cost_r: float | None` フィールドを追加し、`notify_hindsight_evaluated` のメッセージ組み立てに `f"spread_cost={_fmt_opt(info.spread_cost_r)}"` を 1 項目追加。runtime 側の HindsightInfo 構築箇所 (:346 付近) にも値を渡す。

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_hindsight_spread.py tests -k "hindsight or shadow_notifier" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/hindsight_evaluator.py src/data/orchestrator_store.py src/orchestrator/runtime.py src/orchestrator/shadow_notifier.py tests/test_hindsight_spread.py
git commit -m "feat: hindsight を spread 込み採点に (pnl_r 控除 + spread_cost_r 内訳, spec S-5)"
```

---

### Task 11: RAG case card の horizon タグ — write 側のみ (V-1)

**Files:**
- Modify: `src/rag/directional_writer.py` (全 record_* 関数)
- Modify: `src/cycles/trading.py` / `src/cycles/forecast.py` (呼び出し側で horizon を渡す)
- Test: `tests/test_directional_writer_horizon.py` (新規)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_directional_writer_horizon.py
"""RAG case card の horizon タグ (spec V-1 write 側): 新規カードにのみ付与。"""
import asyncio
from types import SimpleNamespace

import pytest

from src.rag.directional_writer import record_trade_entry


class _FakeDirectional:
    def __init__(self):
        self.calls = []

    def upsert(self, **kwargs):
        self.calls.append(kwargs)


class _FakeStore:
    def __init__(self):
        self.directional = _FakeDirectional()


async def _embed(_text):
    return [0.0] * 8


def _order():
    return SimpleNamespace(
        order_id="o1", pair="USDJPY=X", direction="buy",
        entry_price=150.0, stop_loss=149.5, take_profit=151.0,
    )


def _signal():
    return SimpleNamespace(combined_score=0.4, confidence=0.7, detail_reason="test")


def test_horizon_passed_to_metadata():
    store = _FakeStore()
    asyncio.run(record_trade_entry(store, _embed, _order(), _signal(), horizon="day"))
    assert store.directional.calls[0]["horizon"] == "day"


def test_horizon_omitted_when_none():
    """horizon=None ならキー自体を渡さない (「キー無し = legacy swing」規約)。"""
    store = _FakeStore()
    asyncio.run(record_trade_entry(store, _embed, _order(), _signal()))
    assert "horizon" not in store.directional.calls[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_directional_writer_horizon.py -v`
Expected: FAIL (`unexpected keyword argument 'horizon'`)

- [ ] **Step 3: Implement**

① `src/rag/directional_writer.py` の各 record_* 関数 (`record_trade_entry` / `record_trade_complete` / `record_forecast_entry` / forecast complete / hold 系 — ファイル内の全 upsert 呼び出し) に `horizon: str | None = None` キーワード引数を追加し、upsert kwargs に条件付きで渡す:

```python
async def record_trade_entry(
    store: VectorStore,
    embed_fn: EmbedFn,
    order: Any,
    signal: Any,
    horizon: str | None = None,
) -> None:
    ...
        extra = {"horizon": horizon} if horizon else {}
        store.directional.upsert(
            entry_id=f"{order.order_id}_entry",
            ...
            confidence=signal.confidence,
            **extra,
        )
```

(`DirectionalStore.upsert` が `**kwargs` を metadata に落とす形か、明示引数かは実装を確認。明示引数なら `horizon: str | None = None` を upsert 側にも追加し、None なら metadata に含めない。)

② 呼び出し側 (`src/cycles/trading.py` / `src/cycles/forecast.py` の record_* 呼び出し全箇所) に `horizon=config.orchestrator.policy.trade_horizon` を追加 (config が同スコープにあることは呼び出し箇所で確認 — 無ければ引数で受け渡す)。

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_directional_writer_horizon.py tests -k "directional or rag" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/rag/directional_writer.py src/rag/directional_store.py src/cycles/trading.py src/cycles/forecast.py tests/test_directional_writer_horizon.py
git commit -m "feat: RAG case card に horizon メタデータを付与 (write 側のみ, spec V-1)"
```

---

### Task 12: day 設定値の適用 + 全体回帰

**Files:**
- Modify: `config/settings.yaml` + `config/settings.yaml.example` (実ファイル名注意 — `test_config_example_sync.py` が同期を要求)
- Test: 既存全 suite

- [ ] **Step 1: settings.yaml に day 値を適用 (spec §5.2 の表どおり)**

```yaml
# --- day horizon 移行 (spec 2026-07-05) ---
orchestrator:
  plan_ttl_max_hours: 8
  policy:
    trade_horizon: day
  entry:
    max_technical_age_seconds: 5400
  firing:
    min_planning_interval_seconds: 900
  hindsight:
    horizon_seconds: 28800

schedule:
  technical_trade_interval_minutes: 30
  technical_max_staleness_fx_minutes: 90

trading:
  atr_timeframe: "1h"
  sl_atr_mult_default: 2.0
  tp_atr_mult_default: 4.0
  ohlcv_interval: "15m"
  no_progress_watch_hours: 2
  no_progress_exit_hours: 4
  stale_position_review_hours: 8
  timeout_cooldown_hours: 1
  stale_signal_hours: 2
  reversal_min_holding_minutes: 60

analysis:
  multi_timeframe:
    long:   { lookback_days: 15, interval: "4h", enabled: true }
    medium: { lookback_days: 4,  interval: "1h", enabled: true }
    short:  { lookback_days: 1,  interval: "15m", enabled: true }
```

**注意:** 既存の settings.yaml のキー構造 (ネストの深さ・既存値) に合わせてマージすること。上記は追加/変更差分の意味であり、ファイル全体の置換ではない。`test_config_example_sync.py` が要求する example ファイルにも同じキーを反映する。

- [ ] **Step 2: 全 suite 実行**

Run: `uv run pytest tests -x -q`
Expected: 全 passed (直近基準 1275+ 本 + 本 plan の新規分)

- [ ] **Step 3: spec Review Checklist の照合**

spec §9 の全項目を目視確認し、満たせないものがあれば直す。特に:
- 既定値のみで起動した場合に挙動不変か (`plan_ttl_max_hours=0` / `technical_trade_interval_minutes=None` / `technical_max_staleness_fx_minutes=360` / PriceStore interval 既定 "1h")
- day yaml 適用時に S-1〜S-5 が全て効くか

- [ ] **Step 4: Commit**

```bash
git add config/
git commit -m "feat: day horizon 設定値を適用 (swing→day, spec 2026-07-05 §5.2)"
```

**運用メモ:** このコミットの Fiosracht への適用 (rsync/deploy) は spec §6 の手順どおり live_test 切替完了後。rsync 時は [[finance_rsync_safety]] の除外セット厳守。

---

## Self-Review (plan 作成時実施済み)

- **Spec coverage:** S-1→Task 7 / S-2→Task 8 / S-3→Task 5 / S-4a→Task 1-2 / S-4b→Task 3-4 / S-4c→Task 6 / S-5→Task 9-10 / V-1→Task 11 / §5.2 設定値→Task 12。V-2/V-3/V-4 は調査の結果コード作業不要 (冒頭「調査済み事実」参照)。
- **順序依存:** Task 1→2 (PriceStore API が先)、Task 3→4 (resample が先)、Task 9→10 (spread 記録が先、_migrate も Task 9 で両列分投入)、Task 12 は最後。
- **挙動不変の担保:** Task 1-11 の全既定値が swing 互換。day 化は Task 12 の yaml のみ。
- **既知の不確定点 (実装時に実物と突き合わせる箇所):** `record_shadow_trigger` の正確な既存シグネチャ (Task 9)、`_fetch_and_upsert` のシグネチャ (Task 2)、`DirectionalStore.upsert` の metadata 受け渡し形 (Task 11)、`AppConfig`/`AnalysisConfig` テスト用構築 (Task 5/4)。いずれも該当ファイルに実装があり、テストが形を強制する。
- **codex plan レビュー (2026-07-05) 反映済み:** High#1 spread 経路 (`QuoteSnapshot.spread` + `runtime._spread_pips` 使用に修正) / Med#1 fetch 差分起点の実経路テスト追加 / Med#2 `compute_mtf_summaries` 15m 基底の統合テスト追加 / Low×3 (getter 名 `get_shadow_trigger_by_id`・example 実ファイル名 `settings.yaml.example`・evaluator keyword-only ctor)。

---

## 実装完了メモ (2026-07-05)

全 12 タスク実装完了 (subagent-driven + 2 段レビュー)。デプロイ時の運用注意:

1. **OHLCV cache 再構築バースト:** PriceStore の interval 列追加 (Task 1) により、デプロイ後の初回起動で既存 ohlcv テーブルが DROP → 全銘柄再フェッチされる。初回起動直後の MT5 bridge への fetch 集中は想定内の挙動。
2. **day 値の適用タイミング:** 本コミットの Fiosracht への rsync は spec §6 手順 (live_test 切替完了後)。rsync は [[finance_rsync_safety]] の除外セット厳守。
3. **切替後早期の実測検算 (spec §6-5):** 1h/15m ATR 分布と実 spread 分布を集計し「spread / TP 距離 < 5%」を確認。崩れていれば sl/tp_atr_mult を上方調整 (RR=2 維持)。
4. **starvation 監視 (spec §7):** technical LLM オミット (roadmap Phase 2-1) 未実施のため、planning floor 900s + 収集 30min での LLM slot 競合を `data_freshness_snapshots` で監視。悪化時は min_planning_interval_seconds を 1800 へ戻す。
5. **settings.yaml は gitignore 対象:** day 値の正本はコミットされた `config/settings.yaml.example` と本 spec §5.2。Fiosracht への適用はローカル `config/settings.yaml` の rsync (または手動編集)。リポジトリの settings.yaml.example だけ見て「未適用」と誤認しないこと。
6. **watch 経路の 1h 固定 (Task 13 follow-up):** spec §9-5 の NG を修正済み — watch 銘柄は `_ohlcv_interval_for` で 1h 固定、MTF は base より細かい TF を skip。day 化しても watch (yfinance ETF) の収集は従来どおり。
