# Technical Snapshot Collect Status — Design Spec

**Date:** 2026-05-08
**Author:** finance team
**Status:** Approved (ready for plan)

## 1. 背景と問題

`run tech` は「直近に保存されたテクニカル分析スナップショットを表示」して、**毎時のテクニカル分析が正しく回っているか確認する** ためのビュー。

現状、`technical_snapshots` の唯一の書き手 `technical_collector._collect_one` は次の 3 経路で挙動が異なる:

| 経路 | 現挙動 | `run tech` での見え方 |
|---|---|---|
| 成功 (LLM 分析完了) | `upsert_snapshot` → 1 行 INSERT | bias / conf / dir 表示 |
| stale data (`_is_price_data_stale` = True) | 早期 return、行は書かれない | 「データなし」 |
| 例外 (LLM タイムアウト等) | 上位 try/except が握りつぶす、行は書かれない | 「データなし」 |

加えて outer loop の prefetch 失敗 (`prices.get(symbol) is None`) も snapshot 行を書かない。結果として、

- USDJPY=X / EURUSD=X が 2026-05-05〜06 以降 `technical_snapshots` に書かれず、原因不明 (stale skip か LLM 失敗かの判別不可)
- 一方 `analysis_store.aggregate` は ok 行のみで集計されているので、過去 N 時間に ok 行が無いと取引判定が即時 Ollama fallback に流れている (これも `run tech` から見えない)

応急処置として `get_latest_snapshot()` + views 側 stale fallback (`src/views.py:56-58`) を入れているが、これは「いつのデータか」「何が起きているか」を区別できない band-aid。

## 2. ゴール

1. **毎時 1 行を保証する**: enabled instrument に対し、技術収集サイクルごとに必ず 1 行が `technical_snapshots` に書かれる (成功/失敗いずれでも)
2. **取引判定への汚染を防ぐ**: sentinel 行 (失敗ログ) は `aggregate()` / LLM プロンプト / RAG context に絶対混ざらない
3. **`run tech` で 2 種類の状態を分離表示する**:
   - 「直近の収集試行は何時に・どの結果だったか」(全 status の最新)
   - 「直近の有効な分析は何時の・どんな内容か」(ok 行の最新)

## 3. 非ゴール (スコープ外)

- 過去 2 日 trade Phase 2 が止まった真因の特定 — 本変更後 sentinel が残るので次回失敗時は可視化される。過去ログ調査は別タスク
- SL/TP/RR 列の deprecated 問題 — 現挙動 (LLM 出力 0.0) 維持、別判断
- 失敗時のリトライ・通知 — 本変更は記録のみ。Discord 通知や自動再実行は将来検討

## 4. 設計

### 4.1 Schema

```sql
ALTER TABLE technical_snapshots
  ADD COLUMN collect_status VARCHAR NOT NULL DEFAULT 'ok';
```

- 値: `'ok'` / `'stale_price'` / `'failed'`
- SQLite の `NOT NULL DEFAULT 'ok'` で既存行は 'ok' で埋まる
- migration は既存の `AnalysisStore._migrate()` パターンに 1 エントリ追加

### 4.2 AnalysisStore API

#### Write 経路 (2 メソッドに分離、`upsert_snapshot` は削除)

```python
def add_snapshot(self, analysis: PriceAnalysis) -> None:
    """成功した分析を ok status で保存。
    PriceAnalysis 全フィールド + collect_status='ok' を 1 行 INSERT。
    保存後 _prune_old(symbol) を呼ぶ (既存挙動維持)。"""

_SENTINEL_ALLOWED = ("stale_price", "failed")

def add_sentinel(
    self,
    symbol: str,
    status: str,
    reason: str,
    analyzed_at: datetime | None = None,
) -> None:
    """収集失敗を sentinel 行として保存。

    Args:
        status: 'stale_price' | 'failed' のみ。それ以外は ValueError。
        reason: 失敗理由 (例外メッセージ等)。512 文字を超える場合は truncate して
                "... [truncated]" を付与。
        analyzed_at: 省略時 db_now()。テスト用に注入可能。

    保存値:
        - symbol, analyzed_at, collect_status, reasoning_summary=reason
        - direction_bias='neutral'
        - bias_score=0.0, confidence=0.0
        - stop_loss=0.0, take_profit=0.0
        - entry_zone_low=0.0, entry_zone_high=0.0
        - risk_reward_ratio=0.0
        - market_regime='unknown'
        - confidence_modifier=0.0
    """
```

`add_sentinel` 入口で:
```python
if status not in self._SENTINEL_ALLOWED:
    raise ValueError(f"sentinel status must be one of {self._SENTINEL_ALLOWED}, got {status!r}")
truncated_reason = reason if len(reason) <= 512 else reason[:512] + " ... [truncated]"
```

#### Read 経路 (用途別 method、`get_recent_snapshots` / `get_latest_snapshot` は削除)

```python
def get_recent_ok_snapshots(
    self, symbol: str, hours: int = 8,
) -> list[_TechnicalSnapshot]:
    """ok status のみ。取引判定・LLM プロンプト・econ 分析で使う。
    WHERE collect_status='ok' AND analyzed_at >= now-hours, 新しい順。"""

def get_recent_snapshots_for_display(
    self, symbol: str, hours: int = 8,
) -> list[_TechnicalSnapshot]:
    """全 status。run tech / /tech エンドポイント表示用。
    WHERE analyzed_at >= now-hours (status 制約なし), 新しい順。"""
```

`aggregate(symbol, hours)` は内部で **必ず** `get_recent_ok_snapshots()` を呼ぶ → sentinel が取引判定に混ざらない。

### 4.3 Caller 振り分け

| ファイル:行 | 用途 | 移行先 |
|---|---|---|
| `src/jobs/technical_collector.py:302` | 成功時の保存 | `add_snapshot` |
| `src/jobs/technical_collector.py:199` (`_build_rag_contexts` の `prev_snapshots`) | LLM プロンプト previous_analysis | `get_recent_ok_snapshots` |
| `src/jobs/technical_collector.py:375` (Phase 1.5 macro snapshots) | LLM プロンプト macro context | `get_recent_ok_snapshots` |
| `src/jobs/technical_collector.py:506` (econ_impact_analyzer snapshot_briefs) | 経済指標分析の入力 | `get_recent_ok_snapshots` |
| `src/cycles/_helpers.py:163` (trading helper) | trading_cycle 内分析 | `get_recent_ok_snapshots` |
| `src/data/analysis_store.py:128` (`aggregate` 内部) | aggregate 内部呼び出し | `get_recent_ok_snapshots` |
| `src/rag/ask_context_builder.py:273` | ask LLM コンテキスト | `get_recent_ok_snapshots` |
| `src/api/routes/health.py:94` | health 概況 | `get_recent_ok_snapshots` (健全性確認は ok 必要) |
| `src/views.py:53` (`run_tech_view`) | run tech 表示 | `get_recent_snapshots_for_display` |
| `src/api/routes/data.py:58` (`/tech` endpoint) | run tech と同等 API | `get_recent_snapshots_for_display` |

`src/views.py:56-58` の `get_latest_snapshot` フォールバックは **削除** (新設計で不要)。

### 4.4 technical_collector の変更

#### `_collect_one` リファクタ

例外を上位に投げず、**全経路で sentinel または ok を必ず 1 行書く**:

```python
async def _collect_one(inst, ...):
    if price_data is None:
        # この分岐は collect_all_technical 側で sentinel 書き済みの場合のみ来る
        # (prefetch 成功時のみ price_data が渡される設計に修正)
        return

    # ① staleness check
    staleness = _is_price_data_stale(price_data, max_staleness=_max_staleness_for(inst))
    if staleness is not None:
        analysis_store.add_sentinel(
            symbol=inst.symbol,
            status="stale_price",
            reason=f"latest bar {staleness} ago (max {_max_staleness_for(inst)})",
        )
        logger.info(f"[COLLECT] {inst.display_name}: stale_price sentinel ({staleness} ago)")
        return

    # ② indicator 計算 + tech_score
    try:
        summary, tech_score, mtf_score = _compute_summary_and_score(inst, price_data, config)
    except Exception as e:
        analysis_store.add_sentinel(
            symbol=inst.symbol,
            status="failed",
            reason=f"indicator_error: {type(e).__name__}: {e}",
        )
        logger.error(f"[COLLECT] {inst.display_name}: failed sentinel (indicator) — {e}", exc_info=True)
        return

    # ③ RAG context (read-only、失敗しても全体失敗にはしないが try で囲む)
    try:
        news_ctx, refl_ctx, prev_ctx = _build_rag_contexts(inst, store, analysis_store, config)
        full_macro = _combine_macro(macro_context, correlation_context)
    except Exception as e:
        analysis_store.add_sentinel(
            symbol=inst.symbol,
            status="failed",
            reason=f"rag_context_error: {type(e).__name__}: {e}",
        )
        logger.error(f"[COLLECT] {inst.display_name}: failed sentinel (rag context) — {e}", exc_info=True)
        return

    # ④ LLM 分析
    try:
        price_analysis = await analyze_price_action(...)
    except Exception as e:
        analysis_store.add_sentinel(
            symbol=inst.symbol,
            status="failed",
            reason=f"llm_error: {type(e).__name__}: {e}",
        )
        logger.error(f"[COLLECT] {inst.display_name}: failed sentinel (llm) — {e}", exc_info=True)
        return

    # ⑤ 成功
    analysis_store.add_snapshot(price_analysis)
    logger.info(
        f"[COLLECT] {inst.display_name}: technical snapshot stored | "
        f"bias={price_analysis.bias_score:+.2f} conf={price_analysis.confidence:.2f} "
        f"dir={price_analysis.direction_bias}"
    )
```

`collect_all_technical` 側の `try: await _collect_one(...) except Exception as e: logger.error(...)` ブロックは **削除** — `_collect_one` 内部で全例外を捕まえて sentinel まで完結させるため、二重防御で sentinel が書かれない経路を作らない。

ただし「想定外の例外で `_collect_one` 自体が例外を投げる」最終防衛線は outer loop に残す:

```python
for i, inst in enumerate(all_phase_instruments):
    try:
        await _collect_one(...)
    except Exception as e:
        # _collect_one が想定外で raise した場合の保険 (ここに来てはいけない設計)
        logger.error(f"[COLLECT] {inst.display_name}: unexpected raise from _collect_one — {e}", exc_info=True)
        try:
            analysis_store.add_sentinel(
                symbol=inst.symbol, status="failed",
                reason=f"unexpected_raise: {type(e).__name__}: {e}",
            )
        except Exception:
            pass  # sentinel 書き込みも失敗したら諦め (DB 不通等)
```

#### outer loop prefetch 失敗時の sentinel

現状 (line 338-348):
```python
for inst in all_instruments:
    try:
        prices[inst.symbol] = _fetch_instrument_ohlcv(inst, config, price_store, price_provider)
    except Exception as e:
        logger.warning(f"[PREFETCH] {inst.display_name}: OHLCV fetch failed: {e}")
```

変更後: prefetch 失敗時にエラー文字列を保持し、Phase 1/2 で `prices.get(symbol) is None` なら sentinel を書く。

```python
prefetch_errors: dict[str, str] = {}
for inst in all_instruments:
    try:
        prices[inst.symbol] = _fetch_instrument_ohlcv(inst, config, price_store, price_provider)
    except Exception as e:
        prefetch_errors[inst.symbol] = f"{type(e).__name__}: {e}"
        logger.warning(f"[PREFETCH] {inst.display_name}: OHLCV fetch failed: {e}")
```

Phase 1 / Phase 2 の prefetch ヒット判定:
```python
pd_cached = prices.get(inst.symbol)
if pd_cached is None:
    err = prefetch_errors.get(inst.symbol, "no cached price (unknown reason)")
    analysis_store.add_sentinel(
        symbol=inst.symbol, status="failed",
        reason=f"prefetch_failed: {err}",
    )
    logger.warning(f"[COLLECT] {inst.display_name}: failed sentinel (prefetch)")
    if i < len(<phase>) - 1:
        await asyncio.sleep(delay)
    continue
```

これで「prefetch も Phase 内処理も含め、毎時 1 行は書かれる」が保証される。

### 4.5 `run tech` 表示の二段分離

ビュー側 `print_tech_summary` (および `src/views.py:run_tech_view`) を **2 種類の情報を独立して取得** する形に変更:

```python
def run_tech_view(config, analysis_store):
    all_instruments = config.watch_only_instruments + config.tradeable_instruments
    rows = []
    lookback_h = config.rag.analysis_lookback_hours
    for inst in all_instruments:
        # ① 表示用: 全 status の最新行 (collect_status を取得)
        all_snaps = analysis_store.get_recent_snapshots_for_display(inst.symbol, hours=lookback_h)
        latest_collect = all_snaps[0] if all_snaps else None

        # ② 取引判定相当: ok のみの最新行 (bias/conf/dir を取得)
        ok_snaps = analysis_store.get_recent_ok_snapshots(inst.symbol, hours=lookback_h)
        latest_ok = ok_snaps[0] if ok_snaps else None

        rows.append((inst, latest_collect, latest_ok))
    print_tech_summary(rows, lookback_h)
```

`print_tech_summary` 表示フォーマット案 (テーブル形式):

```
=== Technical Snapshots (lookback 8h) ===

Symbol     Mode    Collect              Status         Last ok              Bias   Conf  Dir
USDJPY=X   trade   18:00 (5m ago)       ⚠ stale_price  14:00 (4h ago)       +0.12  0.65  long
EURUSD=X   trade   18:00 (5m ago)       ✓ ok           18:00 (5m ago)       -0.08  0.55  short
SPY        watch   18:00 (5m ago)       ✓ ok           18:00 (5m ago)       +0.20  0.70  long
1321.T     watch   17:00 (1h ago)       ✗ failed       16:00 (2h ago)       +0.05  0.50  neutral
^VIX       watch   (no data)            —              (no data)            —      —     —

Status legend: ✓ ok = analysis succeeded | ⚠ stale_price = price data too old | ✗ failed = error during analysis
Reasons (for non-ok):
  USDJPY=X: latest bar 7h ago (max 6:00:00)
  1321.T:   llm_error: TimeoutError: ...
```

色分け閾値:
- collect age: ≤1.5h 緑、≤3h 黄、>3h 赤
- last ok age: ≤2h 緑、≤8h 黄、>8h 赤、無し=赤

ok 行が見つからない場合 `Bias / Conf / Dir` は `—` 表示、`Last ok` は `(no recent ok)` 表示。

### 4.6 /tech エンドポイントの形状変更

`src/api/routes/data.py:tech()` も同じ二段分離方針で返す:

```json
{
  "lookback_hours": 8,
  "snapshots": [
    {
      "symbol": "USDJPY=X",
      "display_name": "USD/JPY",
      "mode": "trade",
      "latest_collect": {
        "analyzed_at": "2026-05-08T18:00:00Z",
        "collect_status": "stale_price",
        "reason": "latest bar 7:00:00 ago (max 6:00:00)"
      },
      "latest_ok": {
        "analyzed_at": "2026-05-08T14:00:00Z",
        "direction_bias": "long",
        "bias_score": 0.12,
        "confidence": 0.65,
        ...
      }
    },
    ...
  ]
}
```

`latest_ok` が無いペアは `null`。`latest_collect` が無いペア (新規 instrument 等) も `null`。

### 4.7 既存の `get_latest_snapshot` フォールバック撤去

`src/views.py:56-58` の応急処置を削除:
```python
if not snaps:
    latest = analysis_store.get_latest_snapshot(inst.symbol)
    snaps = [latest] if latest is not None else []
```

新設計では `get_recent_snapshots_for_display` で sentinel も含めて取れるので、lookback 内に行が無い = 本当に何も走っていない (= 真にデータなし)。lookback 外の古いデータを表示する band-aid は不要。

`get_latest_snapshot()` メソッド本体も削除、テストも更新。

## 5. テスト戦略

### 5.1 AnalysisStore

| テスト | 確認内容 |
|---|---|
| `test_add_sentinel_stale_price` | `add_sentinel('stale_price', "...")` で行が書かれ、collect_status='stale_price'、bias=conf=0.0、direction_bias='neutral' |
| `test_add_sentinel_failed` | `add_sentinel('failed', "...")` で同様に書かれる |
| `test_add_sentinel_invalid_status_raises` | `add_sentinel('weird')` は ValueError |
| `test_add_sentinel_long_reason_truncated` | 1000 文字 reason → 512 + "... [truncated]" で保存 |
| `test_get_recent_ok_snapshots_excludes_sentinel` | ok + sentinel 混在 → ok のみ返す |
| `test_get_recent_snapshots_for_display_includes_all` | 全 status 返す (新しい順) |
| `test_aggregate_ignores_sentinel` | sentinel + ok 混在 → aggregate は ok のみで集計 |
| `test_aggregate_with_only_sentinel_returns_none` | sentinel のみ → aggregate は None |
| `test_migration_existing_rows_get_ok` | migration 前 (collect_status カラム不在) → 後で全行 'ok' |
| `test_add_snapshot_sets_ok_status` | `add_snapshot(analysis)` 後、collect_status='ok' |

### 5.2 technical_collector

| テスト | 確認内容 |
|---|---|
| `test_collector_stale_writes_sentinel` | `_is_price_data_stale` True → `add_sentinel('stale_price', ...)`、`add_snapshot` 未呼出 |
| `test_collector_indicator_error_writes_failed` | `_compute_summary_and_score` raise → `add_sentinel('failed', "indicator_error: ...")` |
| `test_collector_rag_context_error_writes_failed` | `_build_rag_contexts` raise → `add_sentinel('failed', "rag_context_error: ...")` |
| `test_collector_llm_error_writes_failed` | `analyze_price_action` raise → `add_sentinel('failed', "llm_error: ...")` |
| `test_collector_success_writes_ok` | 全成功 → `add_snapshot(analysis)` 1 回呼出、sentinel 未呼出 |
| `test_collector_prefetch_failure_writes_failed` | outer loop で `_fetch_instrument_ohlcv` 失敗 → `add_sentinel('failed', "prefetch_failed: ...")` |
| `test_collector_unexpected_raise_outer_loop_safety` | `_collect_one` を mock で raise させる → outer loop が sentinel を書く |
| `test_collector_phase2_continues_after_one_failure` | trade pair 1 個 raise → 次の pair も処理される (既存挙動) |

### 5.3 run tech view

| テスト | 確認内容 |
|---|---|
| `test_run_tech_view_separates_collect_and_ok` | sentinel 最新 + ok は古い → Status 列 sentinel 表示、Bias 列は古い ok 値 |
| `test_run_tech_view_no_data` | 何も無い → "(no data)" 表示 |
| `test_run_tech_view_only_sentinel` | sentinel のみ、ok 無し → Status 表示、Bias 列は "—" |
| `test_run_tech_view_only_ok` | ok のみ → 両列同じ analyzed_at と値 |

### 5.4 /tech endpoint

| テスト | 確認内容 |
|---|---|
| `test_tech_endpoint_returns_latest_collect_and_latest_ok` | 両フィールドが返る、status 別 |
| `test_tech_endpoint_null_when_no_data` | データなし時 latest_collect / latest_ok とも null |

## 6. 実装順序 (順次マージ)

各 Task は前 Task の API に依存するため **直列実装、直列マージ**。Task 1 は API 削除を含む基盤変更で大きめだが 1 コミット。

### Task 1: AnalysisStore API 刷新 + 全 caller リネーム
- schema migration (collect_status カラム追加)
- `add_snapshot` / `add_sentinel` / `get_recent_ok_snapshots` / `get_recent_snapshots_for_display` 追加
- `upsert_snapshot` / `get_recent_snapshots` / `get_latest_snapshot` 削除
- 全 caller 置換 (4.3 の表すべて、views の get_latest_snapshot fallback 削除含む)
- `aggregate` 内部を `get_recent_ok_snapshots` 経由に
- 既存テストを新 API に更新、`test_get_latest_snapshot_*` 系削除
- 新規テスト 5.1 追加

これだけで sentinel 機能は未使用 (collector はまだ書かない)。aggregate / display はカラム追加だけで挙動同じ。

### Task 2: technical_collector の sentinel 書き込み実装
- `_collect_one` リファクタ (4.4)
- outer loop の prefetch 失敗時 sentinel + 想定外 raise の保険
- 5.2 のテスト追加

この時点で sentinel が DB に蓄積され始める。`run tech` はまだ旧表示なので sentinel は ok 経路で空扱い (`get_recent_snapshots_for_display` が sentinel も返す)。

### Task 3: run tech / /tech 表示の二段分離
- `src/views.py:run_tech_view` の二段取得
- `src/reporting/reporter.py:print_tech_summary` の表示フォーマット変更 (4.5)
- `src/api/routes/data.py:tech()` の JSON 形状変更 (4.6)
- 5.3 / 5.4 のテスト追加

この時点で `run tech` から「いつ・どの結果」が見えるようになる。

### Task 4 (オプション、後回しでも可): 過去 2 日の Phase 2 停止調査
- 本仕様の対象外。本変更マージ後、次の収集サイクルで sentinel が記録され始めたら、その reason から原因特定を進める

## 7. ロールバック計画

各 Task はコミット単位で revert 可能:
- Task 3 revert → 表示は旧フォーマット (sentinel カラムは残るが影響なし)
- Task 2 revert → sentinel 書き込み停止 (DB に既存の sentinel 行は残るが aggregate は ok のみなので影響なし)
- Task 1 revert → schema migration を逆向きに (`ALTER TABLE technical_snapshots DROP COLUMN collect_status` ※ SQLite 3.35+ 対応)。新カラムが残ったままでも旧コードは無視するので必須ではない

## 8. 移行時の注意

- 本変更直後は `latest_ok` がしばらく空になる instrument があり得る (sentinel が複数積まれて初めて ok が来るまで)。これは設計どおりだが、ユーザーへ「最初の数サイクルで sentinel が並ぶのは正常」と一言案内する想定
- migration `NOT NULL DEFAULT 'ok'` は SQLite で問題なく走る (既存行に DEFAULT 値が適用される) が、念のため migration 後に `SELECT COUNT(*) FROM technical_snapshots WHERE collect_status IS NULL` が 0 を確認するスモークテストを Task 1 のテストに含める
