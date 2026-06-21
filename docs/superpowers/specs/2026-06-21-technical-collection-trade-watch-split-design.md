# Task 6.2: technical 収集の trade/watch 分割 — 設計

**日付:** 2026-06-21
**branch:** `feat/planner-watch-loop`
**親 spec:** [2026-06-20-orchestrator-agent-loop-design-v2.md](2026-06-20-orchestrator-agent-loop-design-v2.md) §4.8 / §5.3 / §5.4 / §5.6
**スコープ:** orchestrator Phase 6 の最後の項目 (Task 6.2)。`collect_all_technical` を
trade/watch 別経路に分割する。

---

## 1. 目的と背景

現行 `collect_all_technical` (`src/jobs/technical_collector.py`) は

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
| 駆動方式 | **論理的には trade/watch 別経路 (別関数 `collect_trade_technical` / `collect_watch_technical`)、物理 schedule は union dispatch で 1 slot 実行**。当初 Option A (2 スケジュール別登録・順序無保証) を採ったが、同時刻に別 schedule 登録すると `PriorityJobSlot` busy 時に片方が毎回 skip される問題があり、**union 時刻 1 job + 単一 slot 内 watch→trade 逐次** (`build_technical_dispatch`) に改めた (§5.1 と整合)。相関 (Phase 1.5) は trade 経路が PriceStore から watch 価格を再ロードするため、物理順序に依存しない。初回 collection のみ watch→trade を明示逐次実行して cold start の相関欠損を回避 |
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
# trade 経路内 (擬似コード) — _reload_watch_prices ヘルパ
from src.data.price_fetcher import PriceData
from src.data.correlation import _DEFAULT_ROLLING_WINDOW
from src.utils.clock import db_now

MIN_BARS = _DEFAULT_ROLLING_WINDOW + 5   # compute_correlations の要求 (= 25)
end = db_now()
start = end - timedelta(days=_CORR_LOOKBACK_DAYS)   # 既定 30d (バー数を確保)
watch_prices = {}
for w in config.watch_only_instruments:
    df = price_store.load_ohlcv(w.symbol, start, end)   # lookback 窓
    if df is None or df.empty or len(df) < MIN_BARS:
        continue
    pd_w = PriceData(
        symbol=w.symbol, df=df,
        current_price=float(df["Close"].iloc[-1]), fetched_at=end,
    )
    # 古いバーで相関を作り続けないよう stale 判定 (既存 _is_price_data_stale を流用)
    if _is_price_data_stale(pd_w, max_staleness=_max_staleness_for(w)) is not None:
        continue
    watch_prices[w.symbol] = pd_w
trade_prices = { i.symbol: prices[i.symbol] for i in tradeable if i.symbol in prices }
correlations = compute_correlations(trade_prices, watch_prices, watch_names)
```

- **欠損/不足時の扱い:** `load_ohlcv` が空・バー不足の watch symbol は **相関入力から除外**
  (現行の `if i.symbol in prices` ガードと同じ思想)。trade の technical 収集自体は継続し、
  相関 context が一部欠けるだけ。
- **stale 判定 (重要):** 分割後は watch 収集が失敗・停止しても prices.db に古いバーが残るため、
  trade 相関が古い watch 価格で作られ続ける穴がある。`_reload_watch_prices` は最新バー時刻を
  既存 `_is_price_data_stale` / `_max_staleness_for` (watch は 120h 閾値) で判定し、stale な
  watch symbol も相関入力から除外する。
- **`PriceData` 構築の明示:** 再ロードした df は `PriceData(symbol=w.symbol, df=df,
  current_price=float(df["Close"].iloc[-1]), fetched_at=db_now())` で構築する
  (`compute_correlations` は `PriceData.df` を読む)。
- lookback 窓は相関計算に必要な範囲 (`MIN_BARS = _DEFAULT_ROLLING_WINDOW + 5 = 25` バーを
  満たす)。`_CORR_LOOKBACK_DAYS = 30` を既定とする。

### 3.2 econ phase の移動

現 `collect_all_technical` 末尾の Phase 3 (経済指標影響分析、`_collect_econ_impact`)
は `related_pairs = [p for p in tradeable ...]` と tradeable に依存する。これを
`collect_trade_technical` の末尾に移す。watch 経路には含めない。

---

## 4. config 設計

technical **収集**用の interval を `ScheduleConfig` に追加し、trade/watch 別に持たせる。
収集スケジュールは config の interval から `technical_times_for()` で生成し、union dispatch
で回す。**exit_check 用の `technical_times` (毎時:00 固定) は main.py に残す** — SL/TP 確認・
ポジション再評価の頻度は technical 収集 interval の変更に波及させない (両者を独立させる)。

```python
@dataclass
class ScheduleConfig:
    run_times: list[str] = field(default_factory=lambda: ["15:00", "21:00"])
    timezone: str = "Asia/Tokyo"
    # 新規: technical 収集の間隔 (時間)。既定は現状維持 = 毎時 (1h)
    technical_trade_interval_hours: int = 1
    technical_watch_interval_hours: int = 1
```

- **既定値は両方 1 = 毎時:00。** §5.1 の union ディスパッチにより、各 :00 で 1 slot 内に
  watch→trade を順次実行する = 現行の単一 `run_technical_collection` (watch+trade を 1 回) と
  **挙動等価** (後方互換)。
- 時刻リスト生成を純関数に切り出してテスト可能にする:
  ```python
  def technical_times_for(interval_hours: int) -> list[str]:
      return [f"{h:02d}:00" for h in range(0, 24, max(1, interval_hours))]
  ```
- watch を将来 2〜3 にすれば「watch だけ低頻度」が config で実現する。
- **設定例の更新 (変更対象・確認項目):** `config/settings.yaml.example` の `schedule:` に
  `technical_trade_interval_hours` / `technical_watch_interval_hours` を追記する。運用で
  調整する項目なので example に載っていないと発見されない。実装後の確認項目に
  「example が schema と一致しているか」を含める。

---

## 5. 呼び出し側 (main.py)

### 5.1 単一 slot 内で watch→trade を逐次実行する (重要)

**問題 (code review High):** `_run_with_slot` は単一の共有 `_llm_slot` を使い、
`PriorityJobSlot.try_run_scheduled()` は slot busy 時に **queue せず skip** する
([priority_job_slot.py:70-74]、[main.py:59-76])。watch 用・trade 用を**同時刻に別々の
`_run_with_slot` で登録すると、片方が毎回 skip され得る**。既定 (両方 1h = 両方毎時:00) では
watch か trade が毎時欠落する。「現状維持」にならない。

**対応:** 別々に登録せず、**trade 時刻と watch 時刻の和集合 (union) を作り、各時刻につき
1 つの scheduled job を 1 回だけ `_run_with_slot` 登録する**。そのジョブは単一 slot 取得の
中で「この時刻が trade 時刻集合に入っていれば trade を、watch 時刻集合に入っていれば watch を」
**watch→trade の順で逐次実行**する。これにより:
- 同時刻 (両方該当) → 1 slot 内で watch→trade を順次実行 (skip なし)。
- trade のみの時刻 → trade だけ実行。watch のみの時刻 → watch だけ実行。
- watch→trade 順なので相関の watch 価格鮮度も担保される (cold start も含む)。

```python
# main.py (擬似コード)
from src.jobs.technical_schedule import technical_times_for

trade_set = set(technical_times_for(config.schedule.technical_trade_interval_hours))
watch_set = set(technical_times_for(config.schedule.technical_watch_interval_hours))

def _run_technical_at(t: str):
    """時刻 t に該当する経路を単一 slot 内で watch→trade 順に実行する。"""
    if t in watch_set:
        run_watch_technical_collection(
            config, store, price_store, analysis_store,
            price_provider=price_provider,
        )
    if t in trade_set:
        run_trade_technical_collection(
            config, store, price_store, analysis_store,
            price_provider=price_provider, gate=bridge_gate,
        )

for t in sorted(trade_set | watch_set):
    schedule.every().day.at(t, news_tz).do(
        _run_with_slot, _run_technical_at, t, _market_aware=True,
    )
```

> **注意:** `run_watch_technical_collection` / `run_trade_technical_collection` は内部で
> `asyncio.run(...)` する同期関数なので、`_run_technical_at` から順次呼んでよい (それぞれ別の
> event loop を生成して完了する)。1 つの `_run_with_slot` スレッド内で逐次なので slot 取得は 1 回。

### 5.2 同期 wrapper

- 新たに同期 wrapper を 2 本追加:
  - `run_trade_technical_collection(...)` → `asyncio.run(collect_trade_technical(...))`。
    `gate.probe` は **trade 経路側のみ** (balance 更新は trade 文脈で十分。watch は発注非関与)。
  - `run_watch_technical_collection(...)` → `asyncio.run(collect_watch_technical(...))`。
    gate probe しない。
- 既存 `run_technical_collection(...)` は **後方互換のため残す** (collect_all = watch+trade
  両方を 1 回ずつ)。

### 5.3 exit_check は毎時固定で温存する (code review Medium)

現行 `technical_times` ([main.py:158]) は technical 収集だけでなく **exit_check (SL/TP 確認・
ポジション再評価)** の毎時実行にも使われている ([main.py:247-251])。technical の
trade/watch interval を変えた副作用で exit_check の SL/TP 確認まで 2h/3h 間隔になる事故を防ぐ。

- **exit_check 用の時刻は毎時固定で残す** (`technical_times = [f"{h:02d}:00" for h in range(24)]`
  をそのまま exit_check 登録に使い続ける)。technical interval config の影響を受けさせない。
- technical 収集だけが `trade_set`/`watch_set` の union 時刻を使う。

### 5.4 初回 collection

- **初回 collection (initial collection):** cold start の相関欠損を避けるため、watch→trade を
  **逐次実行**する (§5.1 の `_run_technical_at` と同じ順序)。`--skip-tech` 指定時は両方スキップ
  (現状踏襲)。

---

## 6. テスト (TDD)

| # | 検証内容 |
|---|---|
| 1 | `collect_watch_technical` は watch_only のみ収集し tradeable を収集しない (保存 snapshot が watch symbol のみ) |
| 2 | `collect_trade_technical` は tradeable を収集し、相関 context に PriceStore から再ロードした watch 価格が反映される |
| 3 | watch 価格が prices.db に無い場合、trade 収集は継続し相関はそのペアを skip する (失敗しない) |
| 4 | **watch 価格が stale (最新バーが閾値超) の場合、相関入力から除外される** (古いバーで相関を作らない) |
| 5 | econ phase は trade 経路で走り、watch 経路では走らない |
| 6 | `collect_all_technical` wrapper は従来どおり watch+trade 両方を収集 (後方互換) |
| 7 | `technical_times_for(interval_hours)` が interval を反映した時刻リストを返す (1→24個, 2→12個, ...) |
| 8 | **union ディスパッチ `_run_technical_at(t)`: t が trade 時刻のみ→trade のみ実行 / watch 時刻のみ→watch のみ / 両方→watch→trade 順で両方 (1 slot 内)** |

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
