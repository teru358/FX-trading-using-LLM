# Task 6.2: technical 収集の trade/watch 分割 — 設計

**日付:** 2026-06-21
**branch:** `feat/planner-watch-loop`
**親 spec:** [2026-06-20-orchestrator-agent-loop-design-v2.md](2026-06-20-orchestrator-agent-loop-design-v2.md) §4.8 / §5.3 / §5.4 / §5.6
**スコープ:** orchestrator Phase 6 の最後の項目 (Task 6.2)。`collect_all_technical` を
trade/watch 別経路に分割する。

---

## 1. 目的と背景

現行 `collect_all_technical` ([src/jobs/technical_collector.py:352]) は

1. **Phase 1** — watch_only 銘柄を収集 (`analyze_price_action` = LLM)
2. **Phase 1.5** — trade×watch の価格相関を計算 (LLM なし、prefetch キャッシュ参照)
3. **Phase 2** — tradeable 銘柄を収集 (macro + 相関 context 付き)
4. **Phase 3** — 経済指標影響分析 (オプション、tradeable に依存)

を **1 関数・同一スケジュール** (`technical_times` = 毎時:00) で回し、watch も trade も
**同頻度**で LLM 分析を実行している。

親 spec §4.8 は、負荷削減のため watch 系収集と trade 系収集を **別の収集経路 (別関数・
別スケジュール起動点)** に分け、それぞれ独立した interval で回せるようにすることを要件と
している。trade のみ将来 material 駆動の頻度上昇 (§5.3 cadence resolver) を受け、watch は
低頻度固定とする。

**本タスクのスコープは「分割」までに限定する。** cadence resolver / 可変 interval /
PlannerAgent 駆動の頻度調整 (§5.3 / §5.6) は別タスク (後述 §7) とし、本タスクはその土台
(別経路化 + config 化された interval) を作る。

---

## 2. 確定した設計判断

ブレストで以下を確定した。

| 論点 | 決定 |
|---|---|
| 相関 (Phase 1.5) の watch 価格源 | **PriceStore から再ロード** (`price_store.load_ohlcv`)。watch 経路が prices.db に保存済みなので、別スケジュール・別タイミングでも最新の watch 価格を読める。プロセス内 dict 共有・スレッド間状態は不要 |
| 駆動方式 | **Option A: 2 スケジュール + 初回のみ順番**。watch 用・trade 用を別登録。定常は順序無保証 (prices.db で吸収)、初回 collection のみ watch→trade を逐次実行して cold start の相関欠損を回避 |
| base interval | 両方 1h (現状維持)。ただしハードコードを廃し **config 設定項目**にして調整可能化 |
| watch の頻度調整 | watch は固定 (config の interval) で **Planner 頻度調整の対象外**。boost は trade のみ (spec §5.3 / §5.4 準拠、負荷を trade に集中) |
| 頻度調整本体 (resolver/TTL/queue) | **別タスク** (§7)。本タスクには含めない |

**却下した案:**
- **Option B (単一関数が内部で watch→trade 順次)** — trade だけ別 interval / boost ができず、
  分離目的を満たさないため不採用。
- **Option C (queue 制 collection worker)** — 順序は完璧だが boost 判定 = cadence ロジックを
  本タスクに前倒しし、既存 `PriorityJobSlot` (LLM 逐次直列化) と二重直列化になる。将来
  §5.3/§5.6 を作る時に再設計リスク。ただしアイデア自体は優れているため §7 の頻度調整タスクへ
  引き継ぐ。
- **Option D (trade 経路が watch を lazy 補完)** — 順序を trade 起点で担保できるが、trade 経路
  の責務が太り、boost 時に watch にも LLM 負荷が乗る (trade 集中の意図とズレ) ため不採用。

---

## 3. アーキテクチャ (関数分割)

既存ヘルパ (`_collect_one` / `_compute_summary_and_score` / `_build_rag_contexts` /
`_fetch_instrument_ohlcv` / `_combine_macro` 等) は**そのまま再利用**し、変更しない。
`collect_all_technical` の本体ロジックを 2 つの公開収集関数へ抽出する。

```
collect_watch_technical(config, store, price_store, analysis_store, force, price_provider)
  └─ watch_only_instruments のみ prefetch → _collect_one (macro/correlation 無し)
     watch の OHLCV は _fetch_instrument_ohlcv の price_store= 引数で prices.db に保存される

collect_trade_technical(config, store, price_store, analysis_store, force, price_provider)
  ├─ tradeable_instruments を prefetch
  ├─ macro_ctx: watch の保存済み ok snapshot (analysis_store.get_recent_ok_snapshots)
  │            から構築 — 既存 Phase 1 後ロジックと同じ
  ├─ Phase 1.5 相関: watch 価格を *price_store.load_ohlcv で再ロード* して
  │                  trade 価格 (prefetch 済み) と相関計算
  ├─ _collect_one (macro_context + correlation_context 付き)
  └─ Phase 3 econ 影響分析 (tradeable 依存のためこちらに移動)

collect_all_technical(...)  ← 後方互換 wrapper
  └─ await collect_watch_technical(...); await collect_trade_technical(...)
```

### 3.1 相関の watch 価格再ロード

trade 経路の Phase 1.5 では、watch 価格を prefetch キャッシュではなく PriceStore から読む。

```python
# trade 経路内 (擬似コード)
watch_prices = {}
for w in config.watch_only_instruments:
    df = price_store.load_ohlcv(w.symbol, start, end)   # lookback 窓
    if df is not None and not df.empty and len(df) >= MIN_BARS:
        watch_prices[w.symbol] = _as_price_data(df)     # 既存 PriceData 形へ
trade_prices = { i.symbol: prices[i.symbol] for i in tradeable if i.symbol in prices }
correlations = compute_correlations(trade_prices, watch_prices, watch_names)
```

- **欠損/不足時の扱い:** `load_ohlcv` が空・バー不足の watch symbol は **相関入力から除外**
  (現行の `if i.symbol in prices` ガードと同じ思想)。trade の technical 収集自体は継続し、
  相関 context が一部欠けるだけ。
- lookback 窓は相関計算に必要な範囲 (既存 `compute_correlations` が要求するバー数を満たす
  期間)。実装時に既存の相関要求バー数に合わせる。

### 3.2 econ phase の移動

現 `collect_all_technical` 末尾の Phase 3 (経済指標影響分析、[technical_collector.py:516-641])
は `related_pairs = [p for p in tradeable ...]` と tradeable に依存する。これを
`collect_trade_technical` の末尾に移す。watch 経路には含めない。

---

## 4. config 設計

現在 main.py にハードコードされている `technical_times` を `ScheduleConfig` へ移し、
trade/watch 別の interval を持たせる。

```python
@dataclass
class ScheduleConfig:
    run_times: list[str] = field(default_factory=lambda: ["15:00", "21:00"])
    timezone: str = "Asia/Tokyo"
    # 新規: technical 収集の間隔 (時間)。既定は現状維持 = 毎時 (1h)
    technical_trade_interval_hours: int = 1
    technical_watch_interval_hours: int = 1
```

- **既定値は両方 1 = 毎時:00 = 現状と完全に同じ挙動** (後方互換)。
- 時刻リスト生成を純関数に切り出してテスト可能にする:
  ```python
  def technical_times_for(interval_hours: int) -> list[str]:
      return [f"{h:02d}:00" for h in range(0, 24, max(1, interval_hours))]
  ```
- watch を将来 2〜3 にすれば「watch だけ低頻度」が config で実現する。

---

## 5. 呼び出し側 (main.py)

- 新たに同期 wrapper を 2 本追加 (仮称):
  - `run_trade_technical_collection(...)` → `asyncio.run(collect_trade_technical(...))`
  - `run_watch_technical_collection(...)` → `asyncio.run(collect_watch_technical(...))`
  - `gate.probe` は **trade 経路側のみ** で行う (balance 更新は trade 文脈で十分。watch は
    market context 収集のみで発注に関与しないため)。
- 既存 `run_technical_collection(...)` は **後方互換のため残す** (collect_all = watch+trade
  両方を 1 回ずつ)。
- スケジュール登録: `technical_trade_interval_hours` / `technical_watch_interval_hours` から
  それぞれ時刻リストを生成し、`_run_with_slot` + `_market_aware=True` で別登録する。
- **初回 collection (initial collection):** cold start の相関欠損を避けるため、watch→trade を
  **逐次実行**する。`--skip-tech` 指定時は両方スキップ (現状踏襲)。

---

## 6. テスト (TDD)

| # | 検証内容 |
|---|---|
| 1 | `collect_watch_technical` は watch_only のみ収集し tradeable を収集しない (保存 snapshot が watch symbol のみ) |
| 2 | `collect_trade_technical` は tradeable を収集し、相関 context に PriceStore から再ロードした watch 価格が反映される |
| 3 | watch 価格が prices.db に無い場合、trade 収集は継続し相関はそのペアを skip する (失敗しない) |
| 4 | econ phase は trade 経路で走り、watch 経路では走らない |
| 5 | `collect_all_technical` wrapper は従来どおり watch+trade 両方を収集 (後方互換) |
| 6 | `technical_times_for(interval_hours)` が interval を反映した時刻リストを返す (1→24個, 2→12個, ...) |

既存の `collect_all_technical` 系テストを壊さないこと (後方互換 wrapper で吸収)。

---

## 7. 別タスク (本タスク外) — 頻度調整機構

以下は **本タスクに含めない**。Task 6.2 完了後に別途ブレスト→設計→実装する。

親 spec §5.3 (3 経路 cadence resolver) / §5.4 (PlannerAgent 発火タイミング) /
§5.6 (可変 interval スケジューラ) の本体実装。具体的には:

- **runtime 可変 cadence config** と **cadence resolver** (`(pair, boosted_interval,
  expires_at, source)` の most-aggressive-wins)。
- **3 経路の boost 提案:** ① 経済カレンダー (proactive) / ② 市場 state (reactive,
  PriceMonitorWorker) / ③ PlannerAgent 駆動ヒント (TTL 付き、feedback ループ注意)。
- **self-scheduling ドライバ (§5.6):** `schedule` の固定 interval を、毎 tick resolver を
  引いて「経過が有効 interval を超えたら実行」する可変 interval 方式に置き換える。
- **boost は trade instrument のみ対象** (watch は base interval 固定)。

**queue 制収集のアイデア (ブレストでユーザー提案) はここで採用検討する:** 通常 tick =
`[watch, trade]` enqueue / boost 時 = `[trade]` のみ enqueue。ただし既存 `PriorityJobSlot`
(LLM 逐次直列化) との関係整理が必要。

**判断材料 (頻度調整タスクで再評価):**
- フィードバックループ防止のため Planner ヒントには TTL 必須 (§5.3 ③)。
- ローカル 14B の逐次 LLM キュー詰まり防止のため material フィルタ + debounce 必須
  (§5.4)。
- PlannerAgent は現在 shadow。本番収集頻度を動かすと shadow が本番のコスト・データ鮮度に
  影響するため、shadow のまま頻度制御だけ本番投入するかは要判断。
