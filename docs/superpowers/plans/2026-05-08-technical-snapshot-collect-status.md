# Technical Snapshot Collect Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `technical_snapshots` に `collect_status` カラムを追加し、毎収集サイクルで必ず 1 行 (ok / stale_price / failed) を書く + `run tech` で「最新収集試行 (全 status)」と「最新有効分析 (ok のみ)」を二段表示する。

**Architecture:** AnalysisStore の write API を `add_snapshot` (ok 専用) と `add_sentinel` (stale_price/failed) に分離。read API を用途別 (`get_recent_ok_snapshots` for 取引判定/LLM/aggregate、`get_latest_collect_row`/`get_latest_ok_row` for 表示) に分離。technical_collector は全例外経路で sentinel を書く。`run tech` view は 2 つの最新行 API を並べて表示する。

**Tech Stack:** Python 3.12, SQLAlchemy (sqlite), pytest, rich (CLI), FastAPI (/tech endpoint)

**Spec:** [`docs/superpowers/specs/2026-05-08-technical-snapshot-collect-status-design.md`](../specs/2026-05-08-technical-snapshot-collect-status-design.md)

**Test invocation:** すべて `python -m pytest tests/<file>::<test> -v` 形式 (`.venv/bin/python` などはローカル環境次第、`python -m pytest` を canonical とする)

---

## File Structure

| ファイル | 責務 | 編集タイプ |
|---|---|---|
| `src/data/analysis_store.py` | `_TechnicalSnapshot` モデル、`AnalysisStore` API、aggregate ロジック | Modify |
| `src/jobs/technical_collector.py` | `_collect_one` の sentinel 書き込み、prefetch 失敗時 sentinel | Modify |
| `src/views.py` | `run_tech_view` の二段取得 | Modify |
| `src/reporting/reporter.py` | `print_tech_summary` の二段表示フォーマット | Modify |
| `src/api/routes/data.py` | `/tech` エンドポイントの形状変更 | Modify |
| `src/cycles/_helpers.py` | `_build_macro_context` の API リネーム | Modify |
| `src/api/routes/health.py` | snapshot status の API リネーム | Modify |
| `src/rag/ask_context_builder.py` | LLM context の API リネーム | Modify |
| `tests/test_analysis_store.py` | API 刷新に伴う既存テスト更新 + 新規テスト | Modify |
| `tests/test_tech_view.py` | 二段表示テスト (新規 / 既存テスト置き換え) | Modify |
| `tests/test_technical_collector_sentinel.py` | sentinel 書き込みテスト | **Create** |

---

## Pre-flight: Reconcile WIP

現在 `src/data/analysis_store.py` / `src/views.py` / `src/reporting/reporter.py` / `tests/test_analysis_store.py` に未コミットの「`get_latest_snapshot()` + stale fallback」WIP がある。これは仕様で定義した band-aid そのもので、本 plan で完全に置き換える対象。

- [ ] **Step 0.1: WIP の現状を確認**

```bash
git status --short -- src/data/analysis_store.py src/views.py src/reporting/reporter.py tests/test_analysis_store.py tests/test_tech_view.py
git diff --stat HEAD -- src/data/analysis_store.py src/views.py src/reporting/reporter.py tests/test_analysis_store.py
```

Expected: 4 ファイル M + tests/test_tech_view.py が untracked。

- [ ] **Step 0.2: WIP をベースラインとして 1 コミットに固める**

このまま放置すると Task 1 の差分が読みにくくなる。WIP を「band-aid 一時コミット」として固定し、後で Task 1 の中で revert/上書きしていく方が差分が追える。

```bash
git add tests/test_tech_view.py
git add -u src/data/analysis_store.py src/views.py src/reporting/reporter.py tests/test_analysis_store.py
git commit -m "wip: temporary get_latest_snapshot fallback for run tech (to be replaced by collect_status design)"
```

- [ ] **Step 0.3: 緑であることを確認**

```
python -m pytest tests/test_analysis_store.py tests/test_tech_view.py -v
```

Expected: PASS (既存 9 テスト + WIP で追加された 2 テストが通る)。

---

## Merge Boundary 1: Task 1 完了時点で単独マージ可

Task 1 完了後は API のみ変更で挙動同一 (sentinel 未使用、display は従来どおり 1 行表示)。本番デプロイ可能。

## Task 1: Schema + ORM + AnalysisStore API 刷新

**Files:**
- Modify: `src/data/analysis_store.py:21-216`
- Modify: `tests/test_analysis_store.py` (全 `upsert_snapshot` を `add_snapshot` に置換、`get_latest_snapshot` 系テストを `get_latest_collect_row`/`get_latest_ok_row` 用に書き換え)

### Task 1.1: Migration entry + ORM column 追加

- [ ] **Step 1.1.1: 既存テストが緑であることを確認**

```
python -m pytest tests/test_analysis_store.py -v
```

Expected: PASS (Step 0.3 と同じ)。

- [ ] **Step 1.1.2: migration の失敗テストを書く**

`tests/test_analysis_store.py` 末尾に追加:

```python
def test_migration_adds_collect_status_column_with_ok_default(tmp_path):
    """ALTER TABLE で collect_status が追加され、既存行は 'ok' で埋まる。"""
    from sqlalchemy import text
    from src.data.price_store import _get_engine

    db_path = tmp_path / "test.db"
    # 旧スキーマで 1 行 INSERT (collect_status カラム無し状態をシミュレート)
    engine = _get_engine(db_path)
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE technical_snapshots ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "symbol VARCHAR NOT NULL, "
            "analyzed_at DATETIME NOT NULL, "
            "bias_score FLOAT, confidence FLOAT, direction_bias VARCHAR, "
            "stop_loss FLOAT, take_profit FLOAT, "
            "entry_zone_low FLOAT, entry_zone_high FLOAT, "
            "risk_reward_ratio FLOAT, reasoning_summary VARCHAR, "
            "market_regime VARCHAR, confidence_modifier FLOAT)"
        ))
        conn.execute(text(
            "INSERT INTO technical_snapshots (symbol, analyzed_at) "
            "VALUES ('USDJPY=X', '2026-05-01 12:00:00')"
        ))
        conn.commit()

    # 新 AnalysisStore を生成 → migration 走行
    store = AnalysisStore(db_path)

    # 既存行に collect_status='ok' が埋まる
    with engine.connect() as conn:
        result = conn.execute(text(
            "SELECT collect_status FROM technical_snapshots WHERE symbol='USDJPY=X'"
        )).scalar_one()
    assert result == "ok"
```

- [ ] **Step 1.1.3: テストが失敗することを確認**

```
python -m pytest tests/test_analysis_store.py::test_migration_adds_collect_status_column_with_ok_default -v
```

Expected: FAIL with "no such column: collect_status" (もしくは ORM の AttributeError)。

- [ ] **Step 1.1.4: ORM Column を追加 + migration entry を追加**

`src/data/analysis_store.py` の `_TechnicalSnapshot` クラスを編集:

```python
class _TechnicalSnapshot(_Base):
    """15分ごとのテクニカル分析スナップショット。"""
    __tablename__ = "technical_snapshots"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    symbol          = Column(String,  nullable=False, index=True)
    analyzed_at     = Column(DateTime, nullable=False)
    bias_score      = Column(Float)
    confidence      = Column(Float)
    direction_bias  = Column(String)
    stop_loss       = Column(Float)
    take_profit     = Column(Float)
    entry_zone_low  = Column(Float)
    entry_zone_high = Column(Float)
    risk_reward_ratio = Column(Float)
    reasoning_summary = Column(String)
    market_regime     = Column(String)
    confidence_modifier = Column(Float)
    collect_status    = Column(String, nullable=False, default="ok")
```

`_migrate()` の `migrations` リストに 1 エントリ追加:

```python
def _migrate(self) -> None:
    """既存テーブルに新カラムを追加する (ALTER TABLE、既にあれば何もしない)。"""
    migrations = [
        ("technical_snapshots", "market_regime", "VARCHAR"),
        ("technical_snapshots", "confidence_modifier", "FLOAT"),
        ("technical_snapshots", "collect_status", "VARCHAR NOT NULL DEFAULT 'ok'"),
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

- [ ] **Step 1.1.5: テスト緑を確認**

```
python -m pytest tests/test_analysis_store.py::test_migration_adds_collect_status_column_with_ok_default tests/test_analysis_store.py -v
```

Expected: PASS (新規テスト + 既存全テストが通る)。

- [ ] **Step 1.1.6: Commit**

```bash
git add src/data/analysis_store.py tests/test_analysis_store.py
git commit -m "feat(analysis_store): add collect_status column with ok default

ALTER TABLE で collect_status カラムを追加。既存行は SQLite NOT NULL DEFAULT
で 'ok' に埋まる。ORM model にも Column を宣言し snap.collect_status 参照を
可能にする。"
```

### Task 1.2: `add_snapshot` 追加 + `upsert_snapshot` 削除

- [ ] **Step 1.2.1: `add_snapshot` の失敗テストを書く**

`tests/test_analysis_store.py` 末尾に追加:

```python
def test_add_snapshot_writes_ok_status_row(store: AnalysisStore):
    """add_snapshot は collect_status='ok' で INSERT する。"""
    store.add_snapshot(_snapshot(bias=0.2))
    rows = store.get_recent_ok_snapshots("USDJPY=X", hours=8)
    assert len(rows) == 1
    assert rows[0].collect_status == "ok"
    assert rows[0].bias_score == 0.2
```

(`get_recent_ok_snapshots` も同 task 内で実装するため、依存は OK)

- [ ] **Step 1.2.2: テストが失敗することを確認**

```
python -m pytest tests/test_analysis_store.py::test_add_snapshot_writes_ok_status_row -v
```

Expected: FAIL with `AttributeError: 'AnalysisStore' object has no attribute 'add_snapshot'`。

- [ ] **Step 1.2.3: `add_snapshot` を実装、`upsert_snapshot` を削除**

`src/data/analysis_store.py` の `upsert_snapshot` メソッドを **`add_snapshot` にリネームし、collect_status='ok' を明示**:

```python
def add_snapshot(self, analysis: "PriceAnalysis") -> None:  # type: ignore[name-defined]
    """成功した分析を ok status で保存する。

    保存後 _prune_old(symbol) を呼び 48h 超の古い行を消す。
    """
    with Session(self._engine) as session:
        snap = _TechnicalSnapshot(
            symbol=analysis.pair,
            analyzed_at=analysis.analyzed_at,
            bias_score=analysis.bias_score,
            confidence=analysis.confidence,
            direction_bias=analysis.direction_bias,
            stop_loss=analysis.stop_loss,
            take_profit=analysis.take_profit,
            entry_zone_low=analysis.entry_zone[0],
            entry_zone_high=analysis.entry_zone[1],
            risk_reward_ratio=analysis.risk_reward_ratio,
            reasoning_summary=analysis.reasoning_summary,
            market_regime=analysis.market_regime,
            confidence_modifier=analysis.confidence_modifier,
            collect_status="ok",
        )
        session.add(snap)
        session.commit()
    logger.debug(f"Stored ok snapshot for {analysis.pair} (bias={analysis.bias_score:+.2f})")
    self._prune_old(analysis.pair)
```

`upsert_snapshot` メソッド本体は **削除**。

- [ ] **Step 1.2.4: 既存テストの caller を一括置換**

`tests/test_analysis_store.py` の `store.upsert_snapshot(...)` を全て `store.add_snapshot(...)` に置換 (合計 9 箇所、Step 0.2 でコミットした WIP 由来テスト含む)。

`tests/test_tech_view.py` の `store.upsert_snapshot(...)` も置換 (1 箇所、line 47)。

`src/jobs/technical_collector.py:302` の `analysis_store.upsert_snapshot(price_analysis)` を `analysis_store.add_snapshot(price_analysis)` に置換。

- [ ] **Step 1.2.5: 全テスト緑を確認**

```
python -m pytest tests/test_analysis_store.py tests/test_tech_view.py -v
```

Expected: PASS。

- [ ] **Step 1.2.6: Commit**

```bash
git add src/data/analysis_store.py src/jobs/technical_collector.py tests/test_analysis_store.py tests/test_tech_view.py
git commit -m "refactor(analysis_store): rename upsert_snapshot to add_snapshot, set collect_status='ok'

upsert_snapshot は曖昧な名前 (実態は INSERT のみ、UPSERT ではない) のため
add_snapshot にリネームし、collect_status='ok' を明示。caller も一括置換。"
```

### Task 1.3: `add_sentinel` 追加 (validation + truncate + prune)

- [ ] **Step 1.3.1: 失敗テストを書く (4 件)**

`tests/test_analysis_store.py` 末尾に追加:

```python
def test_add_sentinel_writes_stale_price_row(store: AnalysisStore):
    """stale_price sentinel は collect_status='stale_price'、bias=conf=0、reason 保存。"""
    store.add_sentinel(
        symbol="USDJPY=X",
        status="stale_price",
        reason="latest bar 7:00:00 ago",
    )
    with Session(store._engine) as session:
        rows = list(session.execute(
            select(_TechnicalSnapshot)
            .where(_TechnicalSnapshot.symbol == "USDJPY=X")
        ).scalars())
    assert len(rows) == 1
    r = rows[0]
    assert r.collect_status == "stale_price"
    assert r.direction_bias == "neutral"
    assert r.bias_score == 0.0
    assert r.confidence == 0.0
    assert r.stop_loss == 0.0
    assert r.take_profit == 0.0
    assert r.entry_zone_low == 0.0
    assert r.entry_zone_high == 0.0
    assert r.risk_reward_ratio == 0.0
    assert r.market_regime == "unknown"
    assert r.confidence_modifier == 0.0
    assert r.reasoning_summary == "latest bar 7:00:00 ago"


def test_add_sentinel_writes_failed_row(store: AnalysisStore):
    """failed sentinel も同様に書ける。"""
    store.add_sentinel(symbol="USDJPY=X", status="failed", reason="llm_error: TimeoutError")
    with Session(store._engine) as session:
        r = session.execute(
            select(_TechnicalSnapshot).where(_TechnicalSnapshot.symbol == "USDJPY=X")
        ).scalar_one()
    assert r.collect_status == "failed"
    assert r.reasoning_summary == "llm_error: TimeoutError"


def test_add_sentinel_invalid_status_raises(store: AnalysisStore):
    """status バリデーション: 許可外で ValueError。"""
    with pytest.raises(ValueError, match="sentinel status"):
        store.add_sentinel(symbol="USDJPY=X", status="weird", reason="x")


def test_add_sentinel_long_reason_truncated(store: AnalysisStore):
    """reason が 512 文字超なら truncate されて '... [truncated]' が付く。"""
    long_reason = "x" * 1000
    store.add_sentinel(symbol="USDJPY=X", status="failed", reason=long_reason)
    with Session(store._engine) as session:
        r = session.execute(
            select(_TechnicalSnapshot).where(_TechnicalSnapshot.symbol == "USDJPY=X")
        ).scalar_one()
    assert len(r.reasoning_summary) <= 512 + len(" ... [truncated]")
    assert r.reasoning_summary.endswith(" ... [truncated]")
    assert r.reasoning_summary.startswith("x" * 512)
```

ファイル冒頭の import に `from sqlalchemy.orm import Session` と `from sqlalchemy import select` と `from src.data.analysis_store import _TechnicalSnapshot` を追加 (既存 import に無ければ)。

- [ ] **Step 1.3.2: テストが失敗することを確認**

```
python -m pytest tests/test_analysis_store.py::test_add_sentinel_writes_stale_price_row tests/test_analysis_store.py::test_add_sentinel_writes_failed_row tests/test_analysis_store.py::test_add_sentinel_invalid_status_raises tests/test_analysis_store.py::test_add_sentinel_long_reason_truncated -v
```

Expected: 4 件 FAIL with `AttributeError: 'AnalysisStore' object has no attribute 'add_sentinel'`。

- [ ] **Step 1.3.3: `add_sentinel` を実装**

`src/data/analysis_store.py` の `add_snapshot` の直後に追加:

```python
_SENTINEL_ALLOWED = ("stale_price", "failed")
_SENTINEL_REASON_MAX_LEN = 512

def add_sentinel(
    self,
    symbol: str,
    status: str,
    reason: str,
    analyzed_at: "datetime | None" = None,
) -> None:
    """収集失敗を sentinel 行として保存。

    Args:
        status: 'stale_price' | 'failed' のみ。それ以外は ValueError。
        reason: 失敗理由。512 文字を超える場合は truncate して
                ' ... [truncated]' を付与。
        analyzed_at: 省略時 db_now()。テスト用に注入可能。

    保存後 _prune_old(symbol) を呼び 48h 超の sentinel 行も消す
    (失敗連続で sentinel が DB を膨張させないため)。
    """
    if status not in self._SENTINEL_ALLOWED:
        raise ValueError(
            f"sentinel status must be one of {self._SENTINEL_ALLOWED}, got {status!r}"
        )
    if len(reason) > self._SENTINEL_REASON_MAX_LEN:
        reason = reason[: self._SENTINEL_REASON_MAX_LEN] + " ... [truncated]"

    if analyzed_at is None:
        analyzed_at = db_now()

    with Session(self._engine) as session:
        snap = _TechnicalSnapshot(
            symbol=symbol,
            analyzed_at=analyzed_at,
            bias_score=0.0,
            confidence=0.0,
            direction_bias="neutral",
            stop_loss=0.0,
            take_profit=0.0,
            entry_zone_low=0.0,
            entry_zone_high=0.0,
            risk_reward_ratio=0.0,
            reasoning_summary=reason,
            market_regime="unknown",
            confidence_modifier=0.0,
            collect_status=status,
        )
        session.add(snap)
        session.commit()
    logger.debug(f"Stored {status} sentinel for {symbol}: {reason[:80]}")
    self._prune_old(symbol)
```

ファイル冒頭の import に `from datetime import datetime` を追加 (既存 import の `from datetime import timedelta` を `from datetime import datetime, timedelta` に変更)。

- [ ] **Step 1.3.4: テスト緑を確認**

```
python -m pytest tests/test_analysis_store.py -v
```

Expected: PASS。

- [ ] **Step 1.3.5: Commit**

```bash
git add src/data/analysis_store.py tests/test_analysis_store.py
git commit -m "feat(analysis_store): add add_sentinel for stale_price/failed rows

ValueError on invalid status, reason truncation at 512 chars, _prune_old
called after insert to prevent sentinel buildup."
```

### Task 1.4: `get_recent_ok_snapshots` 追加 + `aggregate` 内部呼び出し変更 + ok-only caller リネーム

- [ ] **Step 1.4.1: 失敗テストを書く**

`tests/test_analysis_store.py` 末尾に追加:

```python
def test_get_recent_ok_snapshots_excludes_sentinel(store: AnalysisStore):
    """ok + sentinel 混在 → ok のみ返す (sentinel は除外)。"""
    store.add_snapshot(_snapshot(bias=0.3, hours_ago=1))
    store.add_sentinel(symbol="USDJPY=X", status="stale_price", reason="x")
    store.add_snapshot(_snapshot(bias=0.4, hours_ago=0.5))
    rows = store.get_recent_ok_snapshots("USDJPY=X", hours=8)
    assert len(rows) == 2
    assert all(r.collect_status == "ok" for r in rows)


def test_aggregate_ignores_sentinel(store: AnalysisStore):
    """sentinel 混在でも aggregate は ok のみで集計する。"""
    store.add_snapshot(_snapshot(direction="long", bias=0.5, hours_ago=0))
    store.add_sentinel(symbol="USDJPY=X", status="failed", reason="x")
    result = store.aggregate("USDJPY=X", hours=8)
    assert result is not None
    assert result.direction_bias == "long"
    assert result.bias_score > 0.4  # sentinel の bias=0 が混ざっていれば下がる


def test_aggregate_with_only_sentinel_returns_none(store: AnalysisStore):
    """sentinel のみ (ok 行ゼロ) → aggregate は None。"""
    store.add_sentinel(symbol="USDJPY=X", status="stale_price", reason="x")
    store.add_sentinel(symbol="USDJPY=X", status="failed", reason="y")
    result = store.aggregate("USDJPY=X", hours=8)
    assert result is None
```

- [ ] **Step 1.4.2: テスト失敗を確認**

```
python -m pytest tests/test_analysis_store.py::test_get_recent_ok_snapshots_excludes_sentinel tests/test_analysis_store.py::test_aggregate_ignores_sentinel tests/test_analysis_store.py::test_aggregate_with_only_sentinel_returns_none -v
```

Expected: FAIL — `get_recent_ok_snapshots` が存在しない。

- [ ] **Step 1.4.3: `get_recent_ok_snapshots` を実装、`get_recent_snapshots` を削除、`aggregate` 内部を切替**

`src/data/analysis_store.py` の `get_recent_snapshots` メソッドを以下に **置き換え**:

```python
def get_recent_ok_snapshots(
    self, symbol: str, hours: int = 8,
) -> list[_TechnicalSnapshot]:
    """ok status のみ、lookback 内を新しい順で返す。

    取引判定 (aggregate 内部) / LLM プロンプト previous_analysis /
    RAG context / econ 分析 / health 確認で使う。sentinel 行は除外する。
    """
    since = db_now() - timedelta(hours=hours)
    with Session(self._engine) as session:
        stmt = (
            select(_TechnicalSnapshot)
            .where(_TechnicalSnapshot.symbol == symbol)
            .where(_TechnicalSnapshot.analyzed_at >= since)
            .where(_TechnicalSnapshot.collect_status == "ok")
            .order_by(_TechnicalSnapshot.analyzed_at.desc())
        )
        return list(session.execute(stmt).scalars().all())
```

`aggregate` の `snapshots = self.get_recent_snapshots(symbol, hours)` (line 128) を `snapshots = self.get_recent_ok_snapshots(symbol, hours)` に変更。

- [ ] **Step 1.4.4: caller を 一括置換 (ok-only 用途)**

以下 6 箇所を `get_recent_snapshots` → `get_recent_ok_snapshots` に置換:

- `src/jobs/technical_collector.py:199` (`prev_snapshots` for LLM previous_analysis)
- `src/jobs/technical_collector.py:375` (Phase 1.5 macro snapshots)
- `src/jobs/technical_collector.py:506` (econ_impact_analyzer snapshot_briefs)
- `src/cycles/_helpers.py:163` (`_build_macro_context`)
- `src/rag/ask_context_builder.py:273` (LLM context)
- `src/api/routes/health.py:94` (snapshot 健全性)

例 — `src/cycles/_helpers.py:163` の場合:

```python
# Before
snaps = analysis_store.get_recent_snapshots(inst.symbol, hours=8)
# After
snaps = analysis_store.get_recent_ok_snapshots(inst.symbol, hours=8)
```

`src/views.py:53` と `src/api/routes/data.py:58` (display 用途) は **次の Task 1.5 で別 API に切替** するためここでは触らない。

- [ ] **Step 1.4.5: 既存テストの想定値が変わっていないか確認**

```
python -m pytest tests/test_analysis_store.py -v
```

Expected: 全テスト PASS。aggregate の挙動は ok-only に絞られたが、これまでのテストは全行 ok 想定だったので変わらない。

- [ ] **Step 1.4.6: 関連テスト全体 run**

```
python -m pytest tests/test_ask_context_builder.py tests/test_analysis_store.py tests/test_tech_view.py -v
```

Expected: PASS (`get_recent_snapshots` 直接呼びを書いていなければ通る)。もし FAIL があれば caller 漏れなので grep で再確認:

```bash
grep -rn "get_recent_snapshots" src tests
```

Expected: 残っているのは `views.py` / `data.py` (display 用、次 Task で削除) と本ファイル内の定義 (今ここで削除済み) のみ。

- [ ] **Step 1.4.7: Commit**

```bash
git add src/data/analysis_store.py src/jobs/technical_collector.py src/cycles/_helpers.py src/rag/ask_context_builder.py src/api/routes/health.py tests/test_analysis_store.py
git commit -m "refactor(analysis_store): replace get_recent_snapshots with get_recent_ok_snapshots

ok-only filter を WHERE 句に追加。caller 6 箇所を一括リネーム
(取引判定/LLM/RAG/econ/health)。aggregate 内部も ok-only に。
display 用途 (views, /tech) は次 task で別 API に切替。"
```

### Task 1.5: `get_latest_collect_row` / `get_latest_ok_row` 追加 + display caller リネーム + `get_latest_snapshot` 削除

- [ ] **Step 1.5.1: 失敗テストを書く (4 件)**

`tests/test_analysis_store.py` 末尾に追加:

```python
def test_get_latest_collect_row_returns_newest_any_status(store: AnalysisStore):
    """sentinel + ok + 古い ok → 最新の sentinel が返る (status 制約なし、lookback なし)。"""
    store.add_snapshot(_snapshot(bias=0.1, hours_ago=2))
    store.add_snapshot(_snapshot(bias=0.2, hours_ago=1))
    store.add_sentinel(symbol="USDJPY=X", status="stale_price", reason="latest")
    latest = store.get_latest_collect_row("USDJPY=X")
    assert latest is not None
    assert latest.collect_status == "stale_price"


def test_get_latest_collect_row_returns_none_when_empty(store: AnalysisStore):
    """データなし → None。"""
    latest = store.get_latest_collect_row("USDJPY=X")
    assert latest is None


def test_get_latest_ok_row_skips_sentinel(store: AnalysisStore):
    """最新が sentinel + 古い ok → 古い ok が返る。"""
    store.add_snapshot(_snapshot(bias=0.3, hours_ago=2))
    store.add_sentinel(symbol="USDJPY=X", status="failed", reason="recent")
    latest_ok = store.get_latest_ok_row("USDJPY=X")
    assert latest_ok is not None
    assert latest_ok.collect_status == "ok"
    assert latest_ok.bias_score == 0.3


def test_get_latest_ok_row_returns_none_when_only_sentinel(store: AnalysisStore):
    """sentinel のみ → None。"""
    store.add_sentinel(symbol="USDJPY=X", status="failed", reason="x")
    latest_ok = store.get_latest_ok_row("USDJPY=X")
    assert latest_ok is None
```

- [ ] **Step 1.5.2: テスト失敗を確認**

```
python -m pytest tests/test_analysis_store.py::test_get_latest_collect_row_returns_newest_any_status tests/test_analysis_store.py::test_get_latest_collect_row_returns_none_when_empty tests/test_analysis_store.py::test_get_latest_ok_row_skips_sentinel tests/test_analysis_store.py::test_get_latest_ok_row_returns_none_when_only_sentinel -v
```

Expected: 4 件 FAIL。

- [ ] **Step 1.5.3: 新 method を実装、`get_latest_snapshot` を削除**

`src/data/analysis_store.py` の `get_latest_snapshot` メソッドを以下 2 メソッドで **置き換え**:

```python
def get_latest_collect_row(self, symbol: str) -> _TechnicalSnapshot | None:
    """全 status の最新 1 行 (lookback 非依存)。

    run tech / /tech 表示の「Last collect / Status / Reason」用。
    取引判定・LLM 入力からは絶対呼ばない (lookback 非依存ゆえ古いデータ
    汚染リスクあり)。

    _prune_old(48h) で 48h より古い行は INSERT 時に消えるが、休場中で
    INSERT が走らない期間は古い行が保持される (e.g., 金曜の最終行が
    日曜まで残る)。
    """
    with Session(self._engine) as session:
        stmt = (
            select(_TechnicalSnapshot)
            .where(_TechnicalSnapshot.symbol == symbol)
            .order_by(_TechnicalSnapshot.analyzed_at.desc())
            .limit(1)
        )
        return session.execute(stmt).scalars().first()


def get_latest_ok_row(self, symbol: str) -> _TechnicalSnapshot | None:
    """ok status の最新 1 行 (lookback 非依存)。

    run tech / /tech 表示の「Last ok / Bias / Conf / Dir」用。
    取引判定からは絶対呼ばない (lookback 非依存)。
    """
    with Session(self._engine) as session:
        stmt = (
            select(_TechnicalSnapshot)
            .where(_TechnicalSnapshot.symbol == symbol)
            .where(_TechnicalSnapshot.collect_status == "ok")
            .order_by(_TechnicalSnapshot.analyzed_at.desc())
            .limit(1)
        )
        return session.execute(stmt).scalars().first()
```

- [ ] **Step 1.5.4: display caller を新 API に切替 (Task 1 内では adapter で旧 print_tech_summary 互換に維持)**

`src/views.py:run_tech_view` を以下に変更:

```python
def run_tech_view(config: AppConfig, analysis_store: AnalysisStore) -> None:
    """保存済みテクニカルスナップショットを表示する（新規取得なし）。"""
    all_instruments = config.watch_only_instruments + config.tradeable_instruments
    snapshots_by_symbol: dict[str, list] = {}
    for inst in all_instruments:
        # Task 1: 旧 print_tech_summary 互換のため、最新 1 行 (status 不問) を list-of-one で渡す。
        # Task 3 で print_tech_summary シグネチャを変更し、latest_collect / latest_ok を分離する。
        latest = analysis_store.get_latest_collect_row(inst.symbol)
        snapshots_by_symbol[inst.symbol] = [latest] if latest is not None else []
    display_names = {inst.symbol: inst.display_name for inst in all_instruments}
    print_tech_summary(snapshots_by_symbol, display_names, config.rag.analysis_lookback_hours)
```

`src/api/routes/data.py:tech()` の `get_recent_snapshots(...)` 呼び出しを `get_latest_collect_row(symbol)` に置き換え (シグネチャは Task 3 で本格変更):

```python
# Before (line 58):
snaps = state.analysis_store.get_recent_snapshots(
    inst.symbol, hours=state.config.rag.analysis_lookback_hours
)
if snaps:
    s = snaps[0]
    snapshots.append({...})

# After:
s = state.analysis_store.get_latest_collect_row(inst.symbol)
if s is not None:
    snapshots.append({...})
```

`src/views.py` 内のインポートに変更不要 (既存)。

- [ ] **Step 1.5.5: 既存表示テスト (test_tech_view.py) を新 API に追従して書き換え**

`tests/test_tech_view.py:test_run_tech_view_uses_latest_trade_snapshot_when_outside_lookback` は WIP の `get_latest_snapshot` fallback テスト。新設計では `get_latest_collect_row` で同じことを保証するので、テストを書き換える:

```python
def test_run_tech_view_uses_latest_collect_row_even_outside_lookback(
    tmp_path, monkeypatch,
):
    """run_tech_view は lookback 非依存で最新 1 行を取得する (休場中でも見える)。"""
    from src.views import run_tech_view

    store = AnalysisStore(tmp_path / "prices.db")
    store.add_snapshot(_snapshot(hours_ago=24))  # lookback (8h) 外

    captured = {}

    def _capture(snapshots_by_symbol, display_names, lookback_hours):
        captured["snapshots_by_symbol"] = snapshots_by_symbol
        captured["display_names"] = display_names
        captured["lookback_hours"] = lookback_hours

    monkeypatch.setattr("src.views.print_tech_summary", _capture)

    run_tech_view(_config(lookback_hours=8), store)

    snaps = captured["snapshots_by_symbol"]["USDJPY=X"]
    assert len(snaps) == 1
    assert snaps[0].symbol == "USDJPY=X"
    assert captured["display_names"]["USDJPY=X"] == "USD/JPY"
```

(`upsert_snapshot` → `add_snapshot` 置換は Task 1.2 で完了済み)

- [ ] **Step 1.5.6: 全テスト緑を確認**

```
python -m pytest tests/test_analysis_store.py tests/test_tech_view.py -v
```

Expected: PASS。`get_latest_snapshot` 系の旧テスト名 (`test_get_latest_snapshot_returns_most_recent_even_outside_lookback`) を新名で同等に維持しているため、削除分のテストも自然に置き換わる。

- [ ] **Step 1.5.7: 残存 caller の grep 確認**

```bash
grep -rn "get_latest_snapshot\b\|get_recent_snapshots\b\|upsert_snapshot\b" src tests
```

Expected: 0 件。すべてのリネーム完了。

- [ ] **Step 1.5.8: 全プロジェクトテスト run**

```
python -m pytest -v --tb=short
```

Expected: PASS (全 600+ テスト)。失敗があれば caller 漏れか想定外の依存箇所。

- [ ] **Step 1.5.9: Commit**

```bash
git add src/data/analysis_store.py src/views.py src/api/routes/data.py tests/test_tech_view.py
git commit -m "refactor(analysis_store): replace get_latest_snapshot with get_latest_collect_row + get_latest_ok_row

display 用 API を 2 系統に分離 (全 status の最新 / ok のみの最新)。
lookback 非依存で休場期間中も最後の収集行を表示できる。
views.py / /tech は Task 1 では adapter で旧シグネチャ互換に維持し、
本格的な二段表示は Task 3 で実装。"
```

### Task 1.6: WIP 一時コミットを整理 (rebase で squash 可、optional)

- [ ] **Step 1.6.1: ログを確認**

```bash
git log --oneline | head -10
```

Step 0.2 の "wip: temporary get_latest_snapshot fallback" コミットが Task 1.x の前にあるはず。

- [ ] **Step 1.6.2 (optional): WIP コミットを Task 1 群に squash**

ログをきれいにするなら、interactive rebase で WIP コミット → Task 1.2 (rename) に squash。WIP の get_latest_snapshot 追加は Task 1.5 で完全削除されているため、最終的にはノーオペ。残しても悪さはしないので **任意**。

```bash
# 任意 — ログを整理したい場合のみ
git rebase -i <main-branch-of-task1>~7  # 直近 7 コミットを squash 検討
```

スキップして次に進んでも OK。

---

### Task 1 完了 — Merge Boundary 1

この時点で:
- collect_status カラムが migration されている
- API が `add_snapshot` / `add_sentinel` / `get_recent_ok_snapshots` / `get_latest_collect_row` / `get_latest_ok_row` に刷新済
- collector はまだ sentinel を書かない (write 経路は `add_snapshot` のみ呼ぶ)
- aggregate / 全 LLM/RAG/取引/econ/health caller が ok-only に切替
- views / /tech は最新 1 行表示に切替済 (旧 print_tech_summary 互換 adapter)

**この状態は本番デプロイ可能**。挙動は変わらない (sentinel 行が DB に存在しないため `get_latest_collect_row` は ok 行のみ返す)。

ユーザー確認 / マージ後に Task 2+3 に進む。

---

## Merge Boundary 2: Task 2+3 を 同一 PR・同一マージ で出す

**重要:** Task 2 だけマージすると、collector が sentinel を書き始めるが既存 `print_tech_summary` は先頭行の `direction_bias='neutral'` / `bias=0.0` / `conf=0.0` をそのまま「有効な分析結果」として表示してしまい、ユーザーから見ると「中立判定が出ている」と誤読される。Task 3 の表示変更とセットで初めて正しく機能する。

**実装の流れ:** Task 2 → Task 3 を順に書き、まとめて 1 PR で出す。デプロイ単位は 1 つ。

## Task 2: technical_collector の sentinel 書き込み

**Files:**
- Modify: `src/jobs/technical_collector.py:251-422` (`_collect_one`、outer loop)
- Create: `tests/test_technical_collector_sentinel.py`

### Task 2.1: `_collect_one` stale data → sentinel

- [ ] **Step 2.1.1: 失敗テストを書く**

`tests/test_technical_collector_sentinel.py` を新規作成:

```python
"""technical_collector の sentinel 書き込みテスト。"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.data.analysis_store import AnalysisStore
from src.jobs.technical_collector import _collect_one
from src.utils.clock import db_now


def _inst(symbol: str = "USDJPY=X", asset_type: str = "fx"):
    return SimpleNamespace(
        symbol=symbol,
        display_name="USD/JPY",
        asset_type=asset_type,
        news_categories=["fx"],
    )


def _stale_price_data(symbol: str, hours_ago: float):
    """staleness check で stale 判定される PriceData を作る (FX は 6h 超で stale)。"""
    bar_time = db_now() - timedelta(hours=hours_ago)
    df = pd.DataFrame(
        {"Open": [150.0], "High": [150.5], "Low": [149.5],
         "Close": [150.0], "Volume": [1000]},
        index=pd.DatetimeIndex([bar_time]),
    )
    return SimpleNamespace(symbol=symbol, df=df, current_price=150.0)


def _config(analysis_lookback_hours: int = 8):
    return MagicMock(
        rag=MagicMock(
            news_lookback_hours=24,
            reflection_lookback_count=3,
            analysis_lookback_hours=analysis_lookback_hours,
        ),
        analysis=MagicMock(),
        paper_provider="twelvedata",
    )


def test_collect_one_stale_writes_stale_price_sentinel(tmp_path):
    """stale data → add_sentinel('stale_price', ...) が呼ばれ、add_snapshot は呼ばれない。"""
    store = AnalysisStore(tmp_path / "test.db")
    inst = _inst()
    price_data = _stale_price_data("USDJPY=X", hours_ago=10)  # FX 6h を超える stale

    asyncio.run(_collect_one(
        inst=inst, config=_config(), store=MagicMock(),
        price_store=MagicMock(), analysis_store=store,
        llm=MagicMock(),
        price_data=price_data,
    ))

    latest = store.get_latest_collect_row("USDJPY=X")
    assert latest is not None
    assert latest.collect_status == "stale_price"
    assert "ago" in (latest.reasoning_summary or "")
    assert store.get_latest_ok_row("USDJPY=X") is None
```

- [ ] **Step 2.1.2: テスト失敗を確認**

```
python -m pytest tests/test_technical_collector_sentinel.py::test_collect_one_stale_writes_stale_price_sentinel -v
```

Expected: FAIL — sentinel が書かれず latest is None (現コードは早期 return)。

- [ ] **Step 2.1.3: `_collect_one` の stale 経路で sentinel を書く**

`src/jobs/technical_collector.py:271-278` の stale check を編集:

```python
# Before
staleness = _is_price_data_stale(price_data, max_staleness=_max_staleness_for(inst))
if staleness is not None:
    logger.info(
        f"[COLLECT] {inst.display_name}: stale data (latest bar {staleness} ago), "
        f"skipping LLM analysis"
    )
    return

# After
staleness = _is_price_data_stale(price_data, max_staleness=_max_staleness_for(inst))
if staleness is not None:
    analysis_store.add_sentinel(
        symbol=inst.symbol,
        status="stale_price",
        reason=f"latest bar {staleness} ago (max {_max_staleness_for(inst)})",
    )
    logger.info(
        f"[COLLECT] {inst.display_name}: stale_price sentinel ({staleness} ago)"
    )
    return
```

- [ ] **Step 2.1.4: テスト緑を確認**

```
python -m pytest tests/test_technical_collector_sentinel.py::test_collect_one_stale_writes_stale_price_sentinel -v
```

Expected: PASS。

- [ ] **Step 2.1.5: Commit**

```bash
git add src/jobs/technical_collector.py tests/test_technical_collector_sentinel.py
git commit -m "feat(technical_collector): write stale_price sentinel when price data is stale

stale data 検出時に skip するだけでなく add_sentinel を呼んで失敗を記録する。
run tech から '何時に何で skip したか' が見えるようになる。"
```

### Task 2.2: `_collect_one` indicator/rag/llm 例外 → sentinel

- [ ] **Step 2.2.1: 失敗テストを書く (3 件)**

`tests/test_technical_collector_sentinel.py` 末尾に追加:

```python
def _fresh_price_data(symbol: str = "USDJPY=X"):
    """staleness check を通過する fresh PriceData。"""
    bar_time = db_now() - timedelta(minutes=15)
    df = pd.DataFrame(
        {"Open": [150.0] * 100, "High": [150.5] * 100, "Low": [149.5] * 100,
         "Close": [150.0] * 100, "Volume": [1000] * 100},
        index=pd.date_range(end=bar_time, periods=100, freq="1h"),
    )
    return SimpleNamespace(symbol=symbol, df=df, current_price=150.0)


def test_collect_one_indicator_error_writes_failed_sentinel(tmp_path, monkeypatch):
    """compute_indicators が raise → failed sentinel + skip。"""
    store = AnalysisStore(tmp_path / "test.db")

    def _raise(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "src.jobs.technical_collector._compute_summary_and_score", _raise,
    )

    asyncio.run(_collect_one(
        inst=_inst(), config=_config(), store=MagicMock(),
        price_store=MagicMock(), analysis_store=store,
        llm=MagicMock(), price_data=_fresh_price_data(),
    ))

    latest = store.get_latest_collect_row("USDJPY=X")
    assert latest is not None
    assert latest.collect_status == "failed"
    assert "indicator_error" in (latest.reasoning_summary or "")
    assert "boom" in (latest.reasoning_summary or "")


def test_collect_one_rag_context_error_writes_failed_sentinel(tmp_path, monkeypatch):
    """_build_rag_contexts が raise → failed sentinel + skip。"""
    store = AnalysisStore(tmp_path / "test.db")

    monkeypatch.setattr(
        "src.jobs.technical_collector._compute_summary_and_score",
        lambda *a, **kw: (MagicMock(), MagicMock(), None),
    )

    def _raise(*a, **kw):
        raise RuntimeError("rag down")

    monkeypatch.setattr(
        "src.jobs.technical_collector._build_rag_contexts", _raise,
    )

    asyncio.run(_collect_one(
        inst=_inst(), config=_config(), store=MagicMock(),
        price_store=MagicMock(), analysis_store=store,
        llm=MagicMock(), price_data=_fresh_price_data(),
    ))

    latest = store.get_latest_collect_row("USDJPY=X")
    assert latest is not None
    assert latest.collect_status == "failed"
    assert "rag_context_error" in (latest.reasoning_summary or "")


def test_collect_one_llm_error_writes_failed_sentinel(tmp_path, monkeypatch):
    """analyze_price_action が raise → failed sentinel + skip。"""
    store = AnalysisStore(tmp_path / "test.db")

    monkeypatch.setattr(
        "src.jobs.technical_collector._compute_summary_and_score",
        lambda *a, **kw: (MagicMock(), MagicMock(), None),
    )
    monkeypatch.setattr(
        "src.jobs.technical_collector._build_rag_contexts",
        lambda *a, **kw: ("", "", ""),
    )

    async def _raise_async(*a, **kw):
        raise TimeoutError("llm timeout")

    monkeypatch.setattr(
        "src.jobs.technical_collector.analyze_price_action", _raise_async,
    )

    asyncio.run(_collect_one(
        inst=_inst(), config=_config(), store=MagicMock(),
        price_store=MagicMock(), analysis_store=store,
        llm=MagicMock(), price_data=_fresh_price_data(),
    ))

    latest = store.get_latest_collect_row("USDJPY=X")
    assert latest is not None
    assert latest.collect_status == "failed"
    assert "llm_error" in (latest.reasoning_summary or "")
    assert "timeout" in (latest.reasoning_summary or "").lower()
```

- [ ] **Step 2.2.2: テスト失敗を確認**

```
python -m pytest tests/test_technical_collector_sentinel.py::test_collect_one_indicator_error_writes_failed_sentinel tests/test_technical_collector_sentinel.py::test_collect_one_rag_context_error_writes_failed_sentinel tests/test_technical_collector_sentinel.py::test_collect_one_llm_error_writes_failed_sentinel -v
```

Expected: 3 件 FAIL — 例外が `_collect_one` から伝播。

- [ ] **Step 2.2.3: `_collect_one` の各 phase に try/except + sentinel 書き込みを追加**

`src/jobs/technical_collector.py:251-307` の `_collect_one` を以下に置き換え:

```python
async def _collect_one(
    inst: InstrumentConfig,
    config: AppConfig,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    llm: LLMClient,
    macro_context: str = "",
    correlation_context: str = "",
    price_provider: "PriceProvider | None" = None,
    price_data: "PriceData | None" = None,
) -> None:
    """1銘柄のOHLCVを取得してテクニカル分析を実行し、スナップショットを保存する。

    全経路で必ず 1 行 (ok / stale_price / failed) を analysis_store に書く。
    上位は本関数を try/except する必要は無い (内部で全例外を sentinel 化)。

    price_data が渡された場合は内部フェッチをスキップする (prefetch キャッシュ経由)。
    """
    # Phase 1: OHLCV 取得 (prefetch されていなければここで取得)
    if price_data is None:
        price_data = _fetch_instrument_ohlcv(inst, config, price_store, price_provider)

    # Phase 2: 鮮度チェック (古ければ stale_price sentinel + skip)
    staleness = _is_price_data_stale(price_data, max_staleness=_max_staleness_for(inst))
    if staleness is not None:
        analysis_store.add_sentinel(
            symbol=inst.symbol,
            status="stale_price",
            reason=f"latest bar {staleness} ago (max {_max_staleness_for(inst)})",
        )
        logger.info(
            f"[COLLECT] {inst.display_name}: stale_price sentinel ({staleness} ago)"
        )
        return

    # Phase 3: インジケータ + tech_score (失敗 → failed sentinel)
    try:
        summary, tech_score, mtf_score = _compute_summary_and_score(inst, price_data, config)
    except Exception as e:
        analysis_store.add_sentinel(
            symbol=inst.symbol, status="failed",
            reason=f"indicator_error: {type(e).__name__}: {e}",
        )
        logger.error(
            f"[COLLECT] {inst.display_name}: failed sentinel (indicator) — {e}",
            exc_info=True,
        )
        return

    # Phase 4: RAG コンテキスト構築 (失敗 → failed sentinel)
    try:
        news_ctx, refl_ctx, prev_ctx = _build_rag_contexts(inst, store, analysis_store, config)
        full_macro = _combine_macro(macro_context, correlation_context)
    except Exception as e:
        analysis_store.add_sentinel(
            symbol=inst.symbol, status="failed",
            reason=f"rag_context_error: {type(e).__name__}: {e}",
        )
        logger.error(
            f"[COLLECT] {inst.display_name}: failed sentinel (rag context) — {e}",
            exc_info=True,
        )
        return

    # Phase 5: LLM 分析 (失敗 → failed sentinel)
    try:
        price_analysis = await analyze_price_action(
            pair_cfg=inst,
            price_data=price_data,
            summary=summary,
            llm=llm,
            temperature=config.llm.price_analysis.temperature,
            news_context=news_ctx,
            reflection_context=refl_ctx,
            previous_analysis=prev_ctx,
            macro_context=full_macro,
            user_notes_path=config.user_notes_path,
            tech_score=tech_score,
            mtf_score=mtf_score,
        )
    except Exception as e:
        analysis_store.add_sentinel(
            symbol=inst.symbol, status="failed",
            reason=f"llm_error: {type(e).__name__}: {e}",
        )
        logger.error(
            f"[COLLECT] {inst.display_name}: failed sentinel (llm) — {e}",
            exc_info=True,
        )
        return

    # Phase 6: 成功 → ok 行保存
    analysis_store.add_snapshot(price_analysis)
    logger.info(
        f"[COLLECT] {inst.display_name}: technical snapshot stored | "
        f"bias={price_analysis.bias_score:+.2f} conf={price_analysis.confidence:.2f} "
        f"dir={price_analysis.direction_bias}"
    )
```

- [ ] **Step 2.2.4: テスト緑を確認**

```
python -m pytest tests/test_technical_collector_sentinel.py -v
```

Expected: PASS (Step 2.1 + 2.2 のテスト合計 4 件)。

- [ ] **Step 2.2.5: Commit**

```bash
git add src/jobs/technical_collector.py tests/test_technical_collector_sentinel.py
git commit -m "feat(technical_collector): write failed sentinel for indicator/rag/llm errors

各 phase の例外を捕捉して add_sentinel('failed', ...) を呼ぶ。
これで '毎時 1 行は必ず書かれる' が保証される (市場オープン中)。"
```

### Task 2.3: outer loop の prefetch 失敗 + 想定外 raise → sentinel

- [ ] **Step 2.3.1: 失敗テストを書く (2 件)**

`tests/test_technical_collector_sentinel.py` 末尾に追加:

```python
def test_collect_all_prefetch_failure_writes_failed_sentinel(tmp_path, monkeypatch):
    """outer loop の prefetch 失敗 → failed sentinel を書く。"""
    from src.jobs.technical_collector import collect_all_technical

    store = AnalysisStore(tmp_path / "test.db")

    config = MagicMock()
    config.watch_only_instruments = []
    config.tradeable_instruments = [_inst()]
    config.news_collection.inter_pair_delay_seconds = 0.0
    config.economic_calendar.enabled = False
    config.paper_provider = "twelvedata"

    monkeypatch.setattr(
        "src.jobs.technical_collector.is_market_open",
        lambda *a, **kw: True,
    )
    monkeypatch.setattr(
        "src.jobs.technical_collector.create_llm_client",
        lambda *a, **kw: MagicMock(model_name="test"),
    )

    def _fetch_fail(*a, **kw):
        raise ConnectionError("bridge down")

    monkeypatch.setattr(
        "src.jobs.technical_collector._fetch_instrument_ohlcv", _fetch_fail,
    )

    asyncio.run(collect_all_technical(
        config=config, store=MagicMock(), price_store=MagicMock(),
        analysis_store=store, force=True,
    ))

    latest = store.get_latest_collect_row("USDJPY=X")
    assert latest is not None
    assert latest.collect_status == "failed"
    assert "prefetch_failed" in (latest.reasoning_summary or "")
    assert "ConnectionError" in (latest.reasoning_summary or "")


def test_collect_all_unexpected_raise_in_collect_one_writes_sentinel(tmp_path, monkeypatch):
    """_collect_one が想定外で raise しても outer loop が sentinel を書く保険。"""
    from src.jobs.technical_collector import collect_all_technical

    store = AnalysisStore(tmp_path / "test.db")

    config = MagicMock()
    config.watch_only_instruments = []
    config.tradeable_instruments = [_inst()]
    config.news_collection.inter_pair_delay_seconds = 0.0
    config.economic_calendar.enabled = False
    config.paper_provider = "twelvedata"

    monkeypatch.setattr(
        "src.jobs.technical_collector.is_market_open",
        lambda *a, **kw: True,
    )
    monkeypatch.setattr(
        "src.jobs.technical_collector.create_llm_client",
        lambda *a, **kw: MagicMock(model_name="test"),
    )
    monkeypatch.setattr(
        "src.jobs.technical_collector._fetch_instrument_ohlcv",
        lambda *a, **kw: _fresh_price_data(),
    )

    async def _raise_unexpected(*a, **kw):
        raise SystemError("totally unexpected")

    monkeypatch.setattr(
        "src.jobs.technical_collector._collect_one", _raise_unexpected,
    )

    asyncio.run(collect_all_technical(
        config=config, store=MagicMock(), price_store=MagicMock(),
        analysis_store=store, force=True,
    ))

    latest = store.get_latest_collect_row("USDJPY=X")
    assert latest is not None
    assert latest.collect_status == "failed"
    assert "unexpected_raise" in (latest.reasoning_summary or "")
```

- [ ] **Step 2.3.2: テスト失敗を確認**

```
python -m pytest tests/test_technical_collector_sentinel.py::test_collect_all_prefetch_failure_writes_failed_sentinel tests/test_technical_collector_sentinel.py::test_collect_all_unexpected_raise_in_collect_one_writes_sentinel -v
```

Expected: FAIL — outer loop は現状 sentinel を書かない。

- [ ] **Step 2.3.3: outer loop に prefetch 失敗 + raise 保険を追加**

`src/jobs/technical_collector.py:334-422` の `collect_all_technical` 内、prefetch とフェーズループ部分を編集:

```python
# Step 0 (prefetch) を編集
all_instruments = list(watch_only) + list(tradeable)
prices: dict[str, "PriceData"] = {}
prefetch_errors: dict[str, str] = {}
for inst in all_instruments:
    try:
        prices[inst.symbol] = _fetch_instrument_ohlcv(
            inst, config, price_store, price_provider,
        )
    except Exception as e:
        prefetch_errors[inst.symbol] = f"{type(e).__name__}: {e}"
        logger.warning(
            f"[PREFETCH] {inst.display_name}: OHLCV fetch failed: {e}"
        )
logger.info(
    f"[PREFETCH] cached {len(prices)}/{len(all_instruments)} symbols"
)
```

Phase 1 ループ (line 354-370) を編集:

```python
# Phase 1: 監視専用銘柄
if watch_only:
    logger.info(f"[COLLECT] Phase 1: {len(watch_only)} watch-only instruments")
for i, inst in enumerate(watch_only):
    pd_cached = prices.get(inst.symbol)
    if pd_cached is None:
        err = prefetch_errors.get(inst.symbol, "no cached price (unknown reason)")
        analysis_store.add_sentinel(
            symbol=inst.symbol, status="failed",
            reason=f"prefetch_failed: {err}",
        )
        logger.warning(
            f"[COLLECT] {inst.display_name}: failed sentinel (prefetch)"
        )
        if i < len(watch_only) - 1:
            await asyncio.sleep(delay)
        continue
    try:
        logger.debug(f"[COLLECT] {inst.display_name}: starting technical analysis...")
        await _collect_one(
            inst, config, store, price_store, analysis_store, llm_price,
            price_provider=price_provider, price_data=pd_cached,
        )
    except Exception as e:
        # _collect_one 内部で全例外捕捉済みのはず — ここに来る場合は想定外
        logger.error(
            f"[COLLECT] {inst.display_name}: unexpected raise from _collect_one — {e}",
            exc_info=True,
        )
        try:
            analysis_store.add_sentinel(
                symbol=inst.symbol, status="failed",
                reason=f"unexpected_raise: {type(e).__name__}: {e}",
            )
        except Exception:
            pass  # sentinel 書き込みも失敗 → DB 不通等、諦める
    if i < len(watch_only) - 1:
        await asyncio.sleep(delay)
```

Phase 2 ループ (line 404-422) も同じパターンで編集 (watch_only → tradeable に置換、`macro_context=macro_ctx, correlation_context=corr_ctx` を維持):

```python
# Phase 2: 取引対象
if tradeable:
    if watch_only:
        await asyncio.sleep(delay)
    logger.info(f"[COLLECT] Phase 2: {len(tradeable)} tradeable instruments")
for i, inst in enumerate(tradeable):
    pd_cached = prices.get(inst.symbol)
    if pd_cached is None:
        err = prefetch_errors.get(inst.symbol, "no cached price (unknown reason)")
        analysis_store.add_sentinel(
            symbol=inst.symbol, status="failed",
            reason=f"prefetch_failed: {err}",
        )
        logger.warning(
            f"[COLLECT] {inst.display_name}: failed sentinel (prefetch)"
        )
        if i < len(tradeable) - 1:
            await asyncio.sleep(delay)
        continue
    try:
        corr_ctx = format_correlation_context(correlations, inst.symbol)
        logger.debug(f"[COLLECT] {inst.display_name}: starting technical analysis...")
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
        except Exception:
            pass
    if i < len(tradeable) - 1:
        await asyncio.sleep(delay)
```

- [ ] **Step 2.3.4: テスト緑を確認**

```
python -m pytest tests/test_technical_collector_sentinel.py -v
```

Expected: PASS (合計 6 件)。

- [ ] **Step 2.3.5: 関連テスト全体 run**

```
python -m pytest tests/test_technical_collector_sentinel.py tests/test_technical_collector_gate.py -v
```

Expected: PASS。

- [ ] **Step 2.3.6: Commit (まだ push しない — Task 3 とまとめてマージ)**

```bash
git add src/jobs/technical_collector.py tests/test_technical_collector_sentinel.py
git commit -m "feat(technical_collector): outer loop writes prefetch_failed and unexpected_raise sentinels

prefetch 失敗時に prefetch_errors map で理由を保持し sentinel 書き込み。
_collect_one が想定外で raise した場合の保険として outer try/except も追加
(_collect_one 内部で本来は全例外捕捉済み)。"
```

---

## Task 3: run tech / /tech 表示の二段分離

**Files:**
- Modify: `src/views.py:48-61` (`run_tech_view`)
- Modify: `src/reporting/reporter.py:260-329` (`print_tech_summary` シグネチャ + フォーマット)
- Modify: `src/api/routes/data.py:50-80` (`/tech` エンドポイント)
- Modify: `tests/test_tech_view.py` (シグネチャ変更に追従、新規テスト追加)

### Task 3.1: `print_tech_summary` 新シグネチャ + フォーマット

- [ ] **Step 3.1.1: 失敗テストを書く (3 件)**

`tests/test_tech_view.py` 末尾に追加:

```python
def test_print_tech_summary_shows_collect_status_and_latest_ok(monkeypatch):
    """sentinel 最新 + 古い ok → Status は sentinel、Bias 列は ok 値。"""
    import io
    from datetime import timedelta
    from rich.console import Console
    from src.reporting import reporter

    buf = io.StringIO()
    monkeypatch.setattr(
        reporter,
        "console",
        Console(file=buf, force_terminal=False, width=200),
    )

    inst = SimpleNamespace(symbol="USDJPY=X", display_name="USD/JPY", mode="trade")
    latest_collect = SimpleNamespace(
        analyzed_at=db_now() - timedelta(minutes=5),
        collect_status="stale_price",
        reasoning_summary="latest bar 7:00:00 ago",
    )
    latest_ok = SimpleNamespace(
        analyzed_at=db_now() - timedelta(hours=4),
        collect_status="ok",
        direction_bias="long",
        bias_score=0.12,
        confidence=0.65,
    )

    reporter.print_tech_summary([(inst, latest_collect, latest_ok)])

    output = buf.getvalue()
    assert "USD/JPY" in output
    assert "stale_price" in output
    assert "long" in output
    assert "0.12" in output


def test_print_tech_summary_no_data(monkeypatch):
    """latest_collect=None, latest_ok=None → '(no data)' 表示。"""
    import io
    from rich.console import Console
    from src.reporting import reporter

    buf = io.StringIO()
    monkeypatch.setattr(
        reporter,
        "console",
        Console(file=buf, force_terminal=False, width=200),
    )

    inst = SimpleNamespace(symbol="USDJPY=X", display_name="USD/JPY", mode="trade")
    reporter.print_tech_summary([(inst, None, None)])

    output = buf.getvalue()
    assert "USD/JPY" in output
    assert "no data" in output


def test_print_tech_summary_only_sentinel(monkeypatch):
    """sentinel あり、ok 無し → Status 表示、Bias 列は '—'。"""
    import io
    from datetime import timedelta
    from rich.console import Console
    from src.reporting import reporter

    buf = io.StringIO()
    monkeypatch.setattr(
        reporter,
        "console",
        Console(file=buf, force_terminal=False, width=200),
    )

    inst = SimpleNamespace(symbol="USDJPY=X", display_name="USD/JPY", mode="trade")
    latest_collect = SimpleNamespace(
        analyzed_at=db_now() - timedelta(minutes=10),
        collect_status="failed",
        reasoning_summary="llm_error: TimeoutError",
    )

    reporter.print_tech_summary([(inst, latest_collect, None)])

    output = buf.getvalue()
    assert "USD/JPY" in output
    assert "failed" in output
    assert "no recent ok" in output
```

- [ ] **Step 3.1.2: テスト失敗を確認**

```
python -m pytest tests/test_tech_view.py::test_print_tech_summary_shows_collect_status_and_latest_ok tests/test_tech_view.py::test_print_tech_summary_no_data tests/test_tech_view.py::test_print_tech_summary_only_sentinel -v
```

Expected: FAIL — 新シグネチャ未実装。

- [ ] **Step 3.1.3: `print_tech_summary` を新シグネチャに書き換え**

`src/reporting/reporter.py:260-329` の `print_tech_summary` を以下に置き換え:

```python
def _format_age(snap_at, now):
    """analyzed_at から age を 'Xm ago' / 'Xh ago' / 'Xd ago' で返す。"""
    delta = now - snap_at
    sec = int(delta.total_seconds())
    if sec < 60:
        return f"{sec}s ago"
    minutes = sec // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f}h ago"
    days = hours / 24
    return f"{days:.1f}d ago"


_STATUS_GLYPH = {
    "ok":          "[green]✓ ok[/green]",
    "stale_price": "[yellow]⚠ stale_price[/yellow]",
    "failed":      "[red]✗ failed[/red]",
}


def print_tech_summary(rows: list) -> None:
    """銘柄別の最新 collect 行 + 最新 ok 行を二段表示する。

    Args:
        rows: (instrument, latest_collect_row | None, latest_ok_row | None) のリスト
    """
    console.print()
    console.print(Rule(
        "[bold cyan]Technical Snapshots[/bold cyan]  "
        "[dim](Latest collection attempt + last successful analysis)[/dim]",
        style="cyan",
    ))

    tbl = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold bright_white on grey23",
        border_style="grey50",
        padding=(0, 1),
        expand=False,
    )
    tbl.add_column("Pair",     width=10)
    tbl.add_column("Mode",     width=6)
    tbl.add_column("Collect",  width=18)
    tbl.add_column("Status",   width=22)
    tbl.add_column("Last ok",  width=18)
    tbl.add_column("Bias",     width=7,  justify="right")
    tbl.add_column("Conf",     width=5,  justify="right")
    tbl.add_column("Dir",      width=10)
    tbl.add_column("Reason / Notes", min_width=30, style="dim")

    now = db_now()
    reasons_below = []  # 非 ok の reason を表の下に列挙

    for inst, latest_collect, latest_ok in rows:
        name = inst.display_name
        mode = getattr(inst, "mode", "—")

        if latest_collect is None and latest_ok is None:
            tbl.add_row(name, mode, "[dim](no data)[/dim]", "—", "—", "—", "—", "—", "")
            continue

        # Collect 列
        if latest_collect is not None:
            collect_at = latest_collect.analyzed_at.strftime("%m-%d %H:%M")
            collect_age = _format_age(latest_collect.analyzed_at, now)
            collect_str = f"{collect_at} ({collect_age})"
            status_str = _STATUS_GLYPH.get(
                latest_collect.collect_status,
                f"? {latest_collect.collect_status}",
            )
            if latest_collect.collect_status != "ok":
                reasons_below.append(
                    (name, latest_collect.collect_status, latest_collect.reasoning_summary or "")
                )
        else:
            collect_str = "[dim](no data)[/dim]"
            status_str = "—"

        # Last ok / Bias / Conf / Dir
        if latest_ok is not None:
            ok_at = latest_ok.analyzed_at.strftime("%m-%d %H:%M")
            ok_age = _format_age(latest_ok.analyzed_at, now)
            ok_str = f"{ok_at} ({ok_age})"
            bias_str = f"{latest_ok.bias_score:+.2f}" if latest_ok.bias_score is not None else "—"
            conf_str = f"{latest_ok.confidence:.2f}" if latest_ok.confidence is not None else "—"
            dir_str = latest_ok.direction_bias or "—"
        else:
            ok_str = "[dim](no recent ok)[/dim]"
            bias_str = "—"
            conf_str = "—"
            dir_str = "—"

        notes = ""
        if latest_collect is not None and latest_collect.collect_status != "ok":
            notes = (latest_collect.reasoning_summary or "")[:60]

        tbl.add_row(
            name, mode, collect_str, status_str, ok_str,
            bias_str, conf_str, dir_str, notes,
        )

    console.print(tbl)

    if reasons_below:
        console.print()
        console.print("[dim]Status legend: ✓ ok = analysis succeeded | "
                      "⚠ stale_price = price data too old | "
                      "✗ failed = error during analysis[/dim]")
        console.print("[dim]Reasons (non-ok):[/dim]")
        for name, status, reason in reasons_below:
            console.print(f"  [dim]{name} ({status}): {reason}[/dim]")
    console.print()
```

旧 `print_tech_summary` の引数 `snapshots_by_symbol`, `display_names`, `lookback_hours` は **削除**。

ファイル冒頭の import に `from src.utils.clock import db_now` が無ければ追加 (Step 0.2 の WIP で既に追加済み)。`from datetime import timedelta` は使わなくなるので削除可 (元々 WIP で追加した経路のみで使われていた)。

- [ ] **Step 3.1.4: テスト緑を確認**

```
python -m pytest tests/test_tech_view.py::test_print_tech_summary_shows_collect_status_and_latest_ok tests/test_tech_view.py::test_print_tech_summary_no_data tests/test_tech_view.py::test_print_tech_summary_only_sentinel -v
```

Expected: PASS。

### Task 3.2: `run_tech_view` を新シグネチャに対応

- [ ] **Step 3.2.1: 既存テストを新シグネチャに合わせる**

`tests/test_tech_view.py:test_run_tech_view_uses_latest_collect_row_even_outside_lookback` (Task 1.5.5 で書き換えたもの) は `print_tech_summary(snapshots_by_symbol, display_names, lookback_hours)` で capture している。これを新シグネチャ `print_tech_summary(rows)` に合わせて書き換え:

```python
def test_run_tech_view_uses_latest_collect_row_even_outside_lookback(
    tmp_path, monkeypatch,
):
    """run_tech_view は lookback 非依存で最新 1 行を取得する (休場中でも見える)。"""
    from src.views import run_tech_view

    store = AnalysisStore(tmp_path / "prices.db")
    store.add_snapshot(_snapshot(hours_ago=24))  # lookback (8h) 外

    captured = {}

    def _capture(rows):
        captured["rows"] = rows

    monkeypatch.setattr("src.views.print_tech_summary", _capture)

    run_tech_view(_config(lookback_hours=8), store)

    rows = captured["rows"]
    assert len(rows) == 1
    inst, latest_collect, latest_ok = rows[0]
    assert inst.symbol == "USDJPY=X"
    assert latest_collect is not None
    assert latest_collect.symbol == "USDJPY=X"
    assert latest_ok is not None
    assert latest_ok.collect_status == "ok"
```

- [ ] **Step 3.2.2: 新規テストを書く (二段表示)**

`tests/test_tech_view.py` 末尾に追加:

```python
def test_run_tech_view_separates_latest_collect_and_latest_ok(
    tmp_path, monkeypatch,
):
    """sentinel 最新 + 古い ok → latest_collect は sentinel、latest_ok は古い ok。"""
    from src.views import run_tech_view
    from datetime import timedelta

    store = AnalysisStore(tmp_path / "prices.db")
    store.add_snapshot(_snapshot(hours_ago=4))           # 古い ok
    store.add_sentinel(symbol="USDJPY=X", status="stale_price", reason="recent fail")

    captured = {}
    monkeypatch.setattr(
        "src.views.print_tech_summary",
        lambda rows: captured.setdefault("rows", rows),
    )
    run_tech_view(_config(lookback_hours=8), store)

    rows = captured["rows"]
    assert len(rows) == 1
    _inst, latest_collect, latest_ok = rows[0]
    assert latest_collect.collect_status == "stale_price"
    assert latest_ok.collect_status == "ok"
```

- [ ] **Step 3.2.3: テスト失敗を確認**

```
python -m pytest tests/test_tech_view.py -v
```

Expected: 既存テストが シグネチャ不一致で FAIL、新規テストも未実装で FAIL。

- [ ] **Step 3.2.4: `run_tech_view` を新シグネチャ + 二段取得に書き換え**

`src/views.py:run_tech_view` を以下に置き換え:

```python
def run_tech_view(config: AppConfig, analysis_store: AnalysisStore) -> None:
    """保存済みテクニカルスナップショットを表示する（新規取得なし）。

    全 status の最新行 (latest_collect) と ok 限定の最新行 (latest_ok) を
    並べて表示する。lookback 非依存なので休場中も最後の収集試行が見える。
    """
    all_instruments = config.watch_only_instruments + config.tradeable_instruments
    rows = []
    for inst in all_instruments:
        latest_collect = analysis_store.get_latest_collect_row(inst.symbol)
        latest_ok = analysis_store.get_latest_ok_row(inst.symbol)
        rows.append((inst, latest_collect, latest_ok))
    print_tech_summary(rows)
```

- [ ] **Step 3.2.5: テスト緑を確認**

```
python -m pytest tests/test_tech_view.py -v
```

Expected: PASS。

### Task 3.3: `/tech` エンドポイントを新形状に変更

- [ ] **Step 3.3.1: 失敗テストを書く**

`tests/test_api_endpoints.py` を確認して既存の `/tech` テストがある場合は更新、無ければ新規追加。検索:

```bash
grep -n "/tech\|def tech\|tech_endpoint" tests/test_api_endpoints.py
```

無ければ末尾に追加 (`tests/test_api_endpoints.py`):

```python
def test_tech_endpoint_returns_latest_collect_and_latest_ok(client, tmp_path):
    """/tech は latest_collect (全 status) と latest_ok (ok のみ) を返す。"""
    from src.api._state import state
    from src.data.analysis_store import AnalysisStore
    from src.analysis.price_analyzer import PriceAnalysis
    from src.utils.clock import db_now
    from datetime import timedelta

    store = AnalysisStore(tmp_path / "prices.db")
    state.analysis_store = store
    state.config.tradeable_instruments = [
        type("Inst", (), {"symbol": "USDJPY=X", "display_name": "USD/JPY", "mode": "trade"})()
    ]
    state.config.watch_only_instruments = []

    # 古い ok + 直近 sentinel を投入
    store.add_snapshot(PriceAnalysis(
        pair="USDJPY=X", direction_bias="long", bias_score=0.2, confidence=0.6,
        entry_zone=(149.5, 150.5), stop_loss=149.0, take_profit=152.0,
        risk_reward_ratio=2.0, reasoning_summary="ok",
        analyzed_at=db_now() - timedelta(hours=4),
    ))
    store.add_sentinel(symbol="USDJPY=X", status="stale_price", reason="latest bar 7h ago")

    resp = client.get("/tech", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    body = resp.json()
    assert "snapshots" in body
    snaps = body["snapshots"]
    assert len(snaps) == 1
    s = snaps[0]
    assert s["symbol"] == "USDJPY=X"
    assert s["latest_collect"] is not None
    assert s["latest_collect"]["collect_status"] == "stale_price"
    assert s["latest_ok"] is not None
    assert s["latest_ok"]["direction_bias"] == "long"


def test_tech_endpoint_returns_null_when_no_data(client, tmp_path):
    """データなし → latest_collect / latest_ok とも null。"""
    from src.api._state import state
    from src.data.analysis_store import AnalysisStore

    store = AnalysisStore(tmp_path / "prices.db")
    state.analysis_store = store
    state.config.tradeable_instruments = [
        type("Inst", (), {"symbol": "GBPUSD=X", "display_name": "GBP/USD", "mode": "trade"})()
    ]
    state.config.watch_only_instruments = []

    resp = client.get("/tech", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    body = resp.json()
    s = body["snapshots"][0]
    assert s["latest_collect"] is None
    assert s["latest_ok"] is None
```

(client fixture と verify_api_key の扱いは既存 test_api_endpoints.py に従う。`X-API-Key: "test-key"` は既存テストの fixture 値に合わせる)

- [ ] **Step 3.3.2: テスト失敗を確認**

```
python -m pytest tests/test_api_endpoints.py::test_tech_endpoint_returns_latest_collect_and_latest_ok tests/test_api_endpoints.py::test_tech_endpoint_returns_null_when_no_data -v
```

Expected: FAIL — 旧形状 (snapshots[].direction_bias 等が直 field) を返している。

- [ ] **Step 3.3.3: `/tech` エンドポイントを新形状に書き換え**

`src/api/routes/data.py:tech()` を以下に置き換え:

```python
@router.get("/tech", dependencies=[Depends(verify_api_key)])
def tech() -> dict[str, Any]:
    """銘柄別の最新収集行 (全 status) と最新 ok 行 (run tech と同等)。"""
    assert state.config is not None and state.analysis_store is not None

    all_instruments = (
        state.config.watch_only_instruments + state.config.tradeable_instruments
    )
    snapshots = []
    for inst in all_instruments:
        latest_collect = state.analysis_store.get_latest_collect_row(inst.symbol)
        latest_ok = state.analysis_store.get_latest_ok_row(inst.symbol)

        latest_collect_dict = None
        if latest_collect is not None:
            latest_collect_dict = {
                "analyzed_at": latest_collect.analyzed_at.isoformat() if latest_collect.analyzed_at else None,
                "collect_status": latest_collect.collect_status,
                "reason": latest_collect.reasoning_summary,
            }

        latest_ok_dict = None
        if latest_ok is not None:
            latest_ok_dict = {
                "analyzed_at": latest_ok.analyzed_at.isoformat() if latest_ok.analyzed_at else None,
                "direction_bias": latest_ok.direction_bias,
                "bias_score": latest_ok.bias_score,
                "confidence": latest_ok.confidence,
                "entry_zone_low": latest_ok.entry_zone_low,
                "entry_zone_high": latest_ok.entry_zone_high,
                "stop_loss": latest_ok.stop_loss,
                "take_profit": latest_ok.take_profit,
                "risk_reward_ratio": latest_ok.risk_reward_ratio,
                "reasoning_summary": latest_ok.reasoning_summary,
                "market_regime": latest_ok.market_regime,
            }

        snapshots.append({
            "symbol": inst.symbol,
            "display_name": inst.display_name,
            "mode": getattr(inst, "mode", None),
            "latest_collect": latest_collect_dict,
            "latest_ok": latest_ok_dict,
        })

    return {"snapshots": snapshots}
```

- [ ] **Step 3.3.4: テスト緑を確認**

```
python -m pytest tests/test_api_endpoints.py::test_tech_endpoint_returns_latest_collect_and_latest_ok tests/test_api_endpoints.py::test_tech_endpoint_returns_null_when_no_data -v
```

Expected: PASS。

- [ ] **Step 3.3.5: 全テストを run**

```
python -m pytest -v --tb=short
```

Expected: PASS (全テスト)。新シグネチャ依存テスト (`test_print_tech_summary_marks_snapshot_outside_lookback_as_stale` など、Step 0.2 でコミットした WIP 由来のもの) が **削除/置換されている** ことを確認。残っていたら個別に対応:

```bash
grep -n "snapshots_by_symbol\|lookback_hours" tests/test_tech_view.py
```

Expected: ヒットなし (新シグネチャに統一)。

- [ ] **Step 3.3.6: Commit**

```bash
git add src/views.py src/reporting/reporter.py src/api/routes/data.py tests/test_tech_view.py tests/test_api_endpoints.py
git commit -m "feat(view): two-stage display of latest collect row and latest ok row

print_tech_summary を新シグネチャ (rows: list[(inst, latest_collect, latest_ok)])
に変更し、Status / Last ok を独立列で表示。/tech エンドポイントも同形状で
latest_collect / latest_ok を返す JSON に変更。
旧 lookback ベースの stale 表示は廃止 (lookback 非依存に)。"
```

### Task 3.4: discord_bot 等の追従確認 (out-of-scope だが念のため)

- [ ] **Step 3.4.1: discord_bot が /tech を消費していないか確認**

```bash
grep -rn "snapshots_by_symbol\|/tech\b\|tech endpoint" $(find . -name "discord_bot" -type d -not -path '*/node_modules/*' -not -path '*/.venv/*' 2>/dev/null) 2>/dev/null
```

Expected: ヒットあれば本仕様外で別途追従が必要 — 検出されたら一覧をユーザーに報告して判断を仰ぐ。

ヒット無し → 本 plan の対象外作業なし。

---

### Task 2+3 完了 — Merge Boundary 2

この時点で:
- collector が毎収集サイクルで必ず 1 行 (ok/stale_price/failed) を書く
- `run tech` が collect status と最新 ok を独立して表示
- /tech エンドポイントも同形状

**Task 2 と Task 3 のコミットを 1 PR でマージ**。デプロイ後、次の収集サイクルで sentinel が記録され始める。

```bash
# Task 2.1〜3.3 のコミットを単一ブランチで集約済みの想定
git log --oneline | head -10  # 確認
# PR 作成は別フロー (例: gh pr create)
```

---

## Self-Review

### Spec Coverage

| 仕様セクション | 実装 task |
|---|---|
| 4.1 Schema (DB Migration + ORM) | Task 1.1 |
| 4.2 add_snapshot | Task 1.2 |
| 4.2 add_sentinel (validation, truncate, prune) | Task 1.3 |
| 4.2 get_recent_ok_snapshots | Task 1.4 |
| 4.2 get_latest_collect_row / get_latest_ok_row | Task 1.5 |
| 4.2 get_recent_snapshots / get_latest_snapshot 削除 | Task 1.4 / 1.5 |
| 4.2 aggregate を ok-only 経由 | Task 1.4 |
| 4.3 Caller 振り分け (技術収集 / cycles helper / ask context / health / aggregate / view / api) | Task 1.4 (ok-only) + Task 1.5 (display) |
| 4.4 _collect_one リファクタ (stale / indicator / rag / llm の sentinel) | Task 2.1 + 2.2 |
| 4.4 outer loop prefetch failure + unexpected raise sentinel | Task 2.3 |
| 4.5 print_tech_summary 二段表示 | Task 3.1 + 3.2 |
| 4.6 /tech エンドポイント形状変更 | Task 3.3 |
| 4.7 get_latest_snapshot fallback 撤去 | Task 1.5 (旧 fallback コード削除) |
| 5.1 AnalysisStore テスト | Task 1.x の各 step |
| 5.2 collector テスト | Task 2.x の各 step |
| 5.3 run tech view テスト | Task 3.1 + 3.2 |
| 5.4 /tech endpoint テスト | Task 3.3 |
| 6 実装順序 (Task 1 単独 / Task 2+3 同一マージ) | Merge Boundary 1 / 2 で明示 |

### Placeholder scan
- 「TBD / TODO / fill in」: 0
- 「実装後に決める」「適切に handle」: 0
- すべてのコードは具体的に提示済み

### Type consistency
- API 名: `add_snapshot`, `add_sentinel`, `get_recent_ok_snapshots`, `get_latest_collect_row`, `get_latest_ok_row` を全 task で統一
- カラム名: `collect_status` を全 task で統一
- status 値: `'ok'`, `'stale_price'`, `'failed'` を全 task で統一
- `print_tech_summary` 新シグネチャ: `rows: list[(inst, latest_collect, latest_ok)]` を Task 3.1 / 3.2 / 3.3 で一貫
