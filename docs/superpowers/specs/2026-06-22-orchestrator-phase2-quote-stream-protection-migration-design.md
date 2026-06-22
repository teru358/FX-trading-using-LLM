# Orchestrator Phase 2/D — quote-stream producer + ポジション保護移設 実装設計 Spec

**Date:** 2026-06-22
**Status:** Draft for review
**Parent spec:** `2026-06-20-orchestrator-agent-loop-design-v2.md` (§5.5) / `2026-06-21-orchestrator-phase1to3-cadence-tick-execution-design.md` (Phase 2/D)
**Branch:** `feat/orchestrator-agent-loop` (継続)
**Scope:** Phase 1 (A〜C, cadence/market-state) 完了後の Phase 2/D。watch loop の駆動を polling producer 直読へ刷新し、本番ポジション保護を同じ quote stream を消費する watch 側 worker へ移設する。あわせて bid/ask による spread 実値化を行う。

---

## 0. 背景と前提 — spec v2 からの設計更新

Phase 1 (A〜C) は全完了 (HEAD `2a1bbd2`, full suite 1138 passed)。次は spec v2 §5.5 の「websocket tick 基盤 + ポジション保護移設」。

**親 spec の前提が現実のインフラと食い違っていたため、本 spec で更新する。** 親 spec は「websocket (MT5 bridge / provider 依存)」を前提に書かれていたが、実装調査の結果:

1. **websocket は現状インフラに存在しない。** 全 provider (MT5 bridge / Twelve Data 無料枠 / yfinance) が HTTP request/response。MT5 本体は websocket 非対応で、bridge に push サーバーを足すには Windows 側 PC の改修が要る。
2. **しかし bridge には `/quote/{symbol}` endpoint が既に実装済み** (`mt5_bridge/server.py:237-244`)。中身の `get_quote()` は `MetaTrader5.symbol_info_tick()` でリアルタイム bid/ask/spread/time を返す (`mt5_bridge/mt5_client.py:141-155`)。`symbol_info_tick` は足の確定を待たないため、**1分足の制約を受けず秒単位で最新 bid/ask が取れる。**
3. finance クライアント側に `/quote` を叩くコードは未実装 (現状 `Mt5OhlcvFetcher.fetch_current_price` は `/ohlcv` 1分足 close を使い `spread=None`)。

**結論:** 「watch が low-latency で live 価格を直読する」目的は **HTTP polling (`/quote`) で達成でき、websocket は不要**。さらに bridge `/quote` が既存なので、当初 Windows 工事が必要と見ていた **spread 実値化も bridge 改修ゼロで同じ producer に乗る**。

### 0.1 websocket を採用しない理由 (将来課題として記録)

polling (pull) と websocket (push) の差は本質的に「主導権」と「取りこぼし」:

| 観点 | HTTP polling (本 spec で採用) | websocket (将来課題) |
|---|---|---|
| 主導権 | finance が取りに行く | bridge が tick 発生ごとに送る |
| 取りこぼし | poll 間隔の隙間で tick が間引かれる | 無し (全 tick 補足) |
| 負荷 | 値動き無しでも空打ち | イベント駆動、アイドル時無通信 |
| 接続 | リクエスト毎に完結 (ステートレス、堅牢) | 常時接続の維持・再接続管理が要る |
| bridge 改修 | **不要** | 必要 (ws サーバー新規実装) |
| 適する用途 | watch entry 検知 (分〜時間) / 保護 (秒単位) | HFT・ミリ秒スキャルピング・全 tick 記録 |

watch entry は分〜時間スケール、保護 emergency close は秒単位 poll で十分 (既存 10 分 → 秒オーダーへ改善)。pair 数も少数 (USDJPY 中心) で空打ちコストが小さい。**全 tick 補足が要る用途 (tick DB・スキャルピング) を将来やるなら、その時に websocket + bridge ws サーバーを検討する。**

### 0.2 不変条件

- **bridge (Windows 側) は一切改修しない。** 作業は finance 側クライアントのみ。
- 通信は既存 HTTP REST のまま (`/quote` を短周期 polling)。新規通信インフラはゼロ。
- shadow 境界: `protect_live` 段でのみ保護目的で本番ポジションを実クローズ/SL更新する。新規 entry の発注は依然 shadow (broker 結線は Phase 3/F)。
- 後方互換: 新機能は `tick_migration_stage` (既定 `off`) でガード。既存 watch loop・既存 price_monitor を壊さない。
- TDD (RED→GREEN)。各段で code review。実 bridge/broker はテストで mock。

---

## 1. スコープと達成価値

Phase 2/D は独立した 3 つの価値を 1 つの producer で達成する:

| # | 価値 | 現状の不満 | 達成手段 |
|---|---|---|---|
| ① | watch loop の応答性・quote 集約 | 固定 1s で plan 評価ごとに quote fetch (集約なし) | producer が最新 quote をキャッシュ、watch は直読 |
| ② | ポジション保護の応答性・一元化 | price_monitor が **10 分**ポーリング、orchestrator と別系統 | 保護を同じ quote stream を消費する watch 側 worker へ移設 |
| ③ | spread 実値化 | `spread=None` で spread gate が常に「不明→安全側 reject」 | `/quote` の bid/ask から実 spread を埋める |

実装順: **D-1 (producer + watch 直読 + spread) → D-2 (保護移設)**。D-1 は本番保護に触れないので先に安定化。D-2 は `protect_shadow` で並走比較を経てから `protect_live` へ。

---

## 2. config — `tick_migration_stage` 単調 4 段 enum

保護 worker は producer に依存する (producer が無ければ保護判定の入力 quote が無い)。独立フラグだと「producer off なのに保護 on」という無効状態が作れてしまうため、**単調に進む移行を 1 本の enum で表す。各段が前段を含むので無効組み合わせが型レベルで存在しない。**

```python
# OrchestratorConfig (src/config/schema.py)
tick_migration_stage: str = "off"      # off | producer | protect_shadow | protect_live
quote_stream_poll_seconds: int = 2     # producer の polling 周期
```

| stage | producer | watch 直読 | 保護 worker | 本番クローズ | 説明 |
|---|---|---|---|---|---|
| `off` (既定) | ✗ | ✗ (現行 fetch) | ✗ | ✗ | 現状維持。ロールバック先 |
| `producer` | ✓ | ✓ | ✗ | ✗ | ①③ のみ。watch が producer 直読、spread 実値化 |
| `protect_shadow` | ✓ | ✓ | 記録のみ | ✗ | ② 並走比較期。worker は判定を記録、price_monitor が実行 |
| `protect_live` | ✓ | ✓ | 実行 | ✓ | ② 切替後。worker が single writer、price_monitor 保護停止 |

- **ロールバック = 1 段下げる。** `protect_live` で異常 → `protect_shadow` (記録のみ・price_monitor 復帰) → `producer` (保護 worker 停止) → `off` (全現行)。
- bootstrap は stage を読んで段階的にコンポーネントを起動するだけ。整合性チェック不要 (無効状態が存在しない)。
- `quote_stream_poll_seconds` 既定 2s: `/quote` は秒単位 tick なので 2s poll で「最大 2s 遅れの最新 bid/ask」。LAN 内 HTTP 往復 (10〜100ms) に対し十分。負荷とのバランスで 1〜5s が実用域。

---

## 3. 新規クライアントメソッド — `Mt5OhlcvFetcher.get_quote`

bridge `/quote/{symbol}` を叩く finance 側メソッドを追加する (Windows 改修なし)。

- **場所:** `src/data/mt5_ohlcv_fetcher.py`。既存 `_fetch_dataframe` と同じ httpx GET パターン (URL・timeout・`X-Bridge-Api-Key` ヘッダ流用)。
- **symbol 変換 (必須):** bridge は受け取った文字列をそのまま `symbol_select()` に渡す (`mt5_bridge/mt5_client.py:143`)。内部 symbol は `USDJPY=X`、MT5 symbol は `USDJPY` なので、**`/ohlcv` と同じ `to_mt5_symbol(symbol)` 変換を必ずかけてから URL に入れる** (`mt5_ohlcv_fetcher.py:184` と同一)。変換漏れは `symbol_select` 失敗 → 404 になる。
- **シグネチャ:** `get_quote(self, symbol: str) -> Quote` (新規軽量 dataclass `Quote(bid, ask, mid, spread, spread_pips, observed_at, source="mt5")`)。
  - `mid = (bid + ask) / 2`。
  - **`spread = ask - bid` (価格差)** ← producer が `QuoteSnapshot.spread` に入れるのはこの値 (§4.1)。`spread_pips = (ask - bid) / pip_size(symbol)` は **診断/ログ用の別フィールド**で、watch には渡さない (理由 §4.5)。
  - **`observed_at` は naive local に正規化:** bridge の `time` は UTC aware ISO (`mt5_client.py:151`)。`fromisoformat` で parse 後、**既存 OHLCV 経路と同じ正規化 (UTC aware → `astimezone(local_tz)` → tzinfo を剥がす、DB 規約 = naive machine-local) を施してから `Quote.observed_at` に入れる**。OHLCV 側は Series 用 `_bridge_times_to_local_naive` を使っている (`mt5_ohlcv_fetcher.py:216`) ので、スカラ 1 件用の同等関数を足すか同ロジックをインライン化する。aware のまま渡すと runtime が `naive now - aware observed` で TypeError → `quote_age_sec=None` → freshness wall が "quote age unknown" で全 block する (`runtime.py:490`, `watch_evaluator.py:131`)。
- **エラー:** MT5 未接続時 bridge は 503/404 を返す。`fetch_current_price` と同様 `Mt5UnreachableError` に倒す。
- **bridge レスポンス形 (既存・確認済み):** `{symbol, bid: float, ask: float, spread_points: int, time: ISO8601 (UTC aware)}`。

---

## 4. D-1 — quote-stream producer (`src/data/quote_stream.py`、新規)

### 4.1 `QuoteStreamProducer`

- daemon スレッド。`start()`/`stop()` を持ち、orchestrator runtime のライフサイクルに組み込む (既存 `_notify_thread` 等と同じ daemon パターン)。
- **polling 対象:** 設定の **trade pairs 全部を常時** poll (動的集合管理を避ける。pair 数少数で空打ちコスト小)。
- **周期:** `quote_stream_poll_seconds` (既定 2s)。`self._stop.wait(timeout=poll_seconds)` で回す。
- **保持:** `{pair: QuoteSnapshot}` を `threading.Lock` 保護の in-memory dict に持つ。各 poll で上書き。

### 4.2 取得経路と degrade

各 pair について以下の優先順で 1 件の `QuoteSnapshot` を作る:

1. **MT5 enabled かつ trade pair:** `fetcher.get_quote(pair)` → `QuoteSnapshot(bid, ask, mid, spread=ask-bid, observed_at=naive_local, source="mt5")` (source=mt5)。**`QuoteSnapshot.spread` は価格差 (ask-bid)** であり pips ではない (§4.5)。
2. **`/quote` 失敗 (Mt5UnreachableError) or MT5 非対象 pair:** 既存 `price_provider.get_current_price(pair)` (`/ohlcv` 1分足 close or TD/yfinance) → `bid=ask=mid=price, spread=None` (現行 `make_quote_provider` と同じ安全側挙動)。

degrade しても producer は最新値を「更新する」(古い値で固まらない)。ただし **取得自体が例外で失敗したら最新値を更新しない** → 古い `observed_at` が残り、watch の freshness wall が stale を検知して trigger を止める (既存安全機構を活用、§4.4)。

### 4.3 `latest(pair) -> QuoteSnapshot | None`

- Lock 取得して最新 `QuoteSnapshot` を返す。未取得 pair は None。
- 鮮度の合否判定は producer では行わない (既存 watch_evaluator の freshness wall に委譲し、責務を二重化しない)。observed_at をそのまま載せるだけ。

### 4.4 watch loop の切替 (`src/orchestrator/runtime.py`)

- `tick_migration_stage` が `producer` 以上のとき、runtime の `quote_provider` を `producer.latest` を呼ぶ callable へ差し替える (bootstrap で注入)。`latest` が None を返した場合のみ従来 fetch へフォールバック (producer 起動直後にまだ 1 度も poll が完了していない過渡期の保険であり、§2 の stage 切替とは別軸。定常状態では producer 直読のみ)。
- watch loop 本体 (`_watch_loop` 固定 1s、`run_watch_cycle`、`_evaluate_plan`) は**不変**。quote の取得元だけが「毎回 fetch」→「producer がキャッシュした最新値」に変わる。
- `off` (既定): 現行の `make_quote_provider` 経由 fetch を維持 (ロールバック先)。

### 4.5 spread 実値化の効果と単位の整合 (重要)

**`QuoteSnapshot.spread` は価格差 (ask-bid) であり pips ではない。** 既存 `QuoteSnapshot.spread` は価格差として定義され (`context_builder.py:51`)、watch 側 `freshness_issues` が `spread / pip_size` で pips 化して閾値比較する (`watch_evaluator.py:158-159`)。**producer が誤って pips 値 (spread_pips) を `QuoteSnapshot.spread` に入れると、watch 側がさらに `/pip` するため二重 pips 化で巨大化し、全 trigger が reject される。** よって producer が渡すのは必ず `spread = ask - bid` (価格差)。`spread_pips` は get_quote の診断フィールドに留め、`QuoteSnapshot` には載せない。

この前提を守れば、producer が `/quote` 経由で `spread` を実値 (価格差) で埋めることで、`watch_evaluator.freshness_issues` の spread チェック (`spread is None → "spread unknown"`) が実際の `spread/pip > spread_max_pips` 判定に変わる。**MT5 接続時のみ実値、MT5 未接続/非対象 pair は従来通り None で安全側 reject。** watch_evaluator のコード変更は不要 (既存ロジックがそのまま価格差を pips 化して機能する)。

---

## 5. D-2 — ポジション保護移設 (`src/orchestrator/position_protection_worker.py`、新規)

### 5.1 純関数の流用 (作り直さない)

`src/trading/position_protection.py` の純関数をそのまま **import 流用**する:
- `compute_mfe_update(pos, current) -> ProtectionStateUpdate`
- `compute_profit_protection_action(pos, current, cfg) -> ProtectionAction`
- `more_protective_sl(pos, left, right)` / `current_r` / `risk_distance` 等

これらは副作用の無い純計算なので、駆動 (ポーリング→tick) を変えても結果は同一。**「駆動を変える」のが本質で、判定ロジックは変えない。**

### 5.1.1 close アクションは D-2 では実行しない (現行挙動と一致させる)

`compute_profit_protection_action` は giveback 条件で `action="close"` を返すことがある (`position_protection.py:116`)。**しかし既存 price_monitor の `_apply_profit_protection` は `action == "raise_sl"` のときだけ実行し、`close` を実質無視している** (`price_monitor.py:90` — `action_target` は raise_sl のときのみ非 None、close は SL 更新もクローズもしない)。

「移設 = 既存ポーリングと同結果」を真に成立させるため、**D-2 の worker も `raise_sl` のみ実行し、`close` は price_monitor と同じく無視する** (記録は §5.4 のため両 source とも残してよいが、実クローズはしない)。これにより並走比較 (§5.4) が close 局面でも一致する。

**giveback による close 実行の有効化は、移設ではなく意図的な挙動変更 (bug fix)** なので、本 spec のスコープ外とし別タスク (D-3 等) に切り出す。D-2 ではあくまで現行挙動を tick 駆動へ移すだけに限定する。

### 5.2 worker の構造

- daemon スレッド (`PriceProtectionWorker` 相当)。`tick_migration_stage >= protect_shadow` のときだけ起動。
- producer の最新 quote (`producer.latest(pair)`) を消費し、`position_mgr` の open positions に対し保護判定を回す。
- **周期:** producer と同じ `quote_stream_poll_seconds` で回す (worker は producer に独自周期を要求せず、最新 quote を読むだけ)。producer の更新と worker の読みは Lock 越しなので tick 漏れは保護判定に影響しない (最新値を都度読む)。

### 5.3 stage による挙動分岐

- **`protect_shadow` (並走比較期):** 保護判定 (`compute_profit_protection_action` の `action`/`target_sl` + `mfe_r`/`giveback_r`) を `protection_decisions` テーブルに **記録のみ**。**実クローズ/SL更新は一切しない。** 既存 price_monitor は従来通り 10 分ポーリングで実行する。**並走比較のため、price_monitor 側にも保護判定を `protection_decisions` に `source="price_monitor"` で記録する薄い追記が必要** (`_apply_profit_protection` 内で `compute_profit_protection_action` の結果を、実行とは別に記録する。実行ロジックは変えない)。この追記は `tick_migration_stage >= protect_shadow` のときだけ有効化し、`off`/`producer` では price_monitor を完全無改変に保つ。
- **`protect_live` (切替後):** worker が SL 更新を実行する (§5.1.1 より `raise_sl` のみ、close は現行同様 D-2 では実行しない)。**single execution writer:** SL 更新は `broker.update_remote_sl` / `position_mgr.update_protection_state` の 1 経路に集約。この段では **price_monitor の保護経路 (profit protection) を停止** (二重実行防止)。price_monitor の他機能 (急変動アラート / emergency close 経路) は本 spec のスコープ外として現行のまま残す (emergency close は profit protection とは別経路であり、その移設は別タスク)。

### 5.4 比較検証 — `protection_decisions` テーブル

並走比較を定量化するための新テーブル:

```
protection_decisions(
  id, ts, pair, order_id,
  source,           -- "price_monitor" | "tick_worker"
  action,           -- "none" | "raise_sl" | "close"
  stage,            -- "half" | "breakeven" | "lock" | null
  target_sl,
  mfe_r, giveback_r
)
```

**所有 store / ORM / migration (M5):** 既存 orchestrator 系テーブルは `OrchestratorStore` の SQLAlchemy ORM model + `_Base.metadata.create_all()` に乗っている (`orchestrator_store.py`)。protection_decisions も同パターンに従う:
- **model:** `_ProtectionDecision(_Base)` を orchestrator_store の ORM 群に追加。`create_all()` で既存 DB に**自動追加される** (新規テーブルなので既存行への migration 不要)。保存先 DB は orchestrator store の DB (orch.db) に同居させる (比較が同一 store API で完結し、price_monitor / worker 双方から参照しやすい)。
- **書込 API:** `OrchestratorStore.record_protection_decision(*, ts, pair, order_id, source, action, stage, target_sl, mfe_r, giveback_r) -> None`。worker (§5.3) と price_monitor 追記 (§5.3) の双方がこれを呼ぶ。
- **比較クエリ API:** `OrchestratorStore.compare_protection_decisions(*, since) -> list[...]` (同 `order_id`・近接 `ts` で source をペアリングし、`action`/`target_sl` の一致/不一致を返す)。

- `protect_shadow` 期は両 source が同テーブルに記録。比較クエリで `action` と `target_sl` の一致率を出す (テスト or 簡易スクリプト)。
- Review Checklist「保護移設が既存ポーリングと並走比較で同結果か」をこの一致率で満たす。一致が確認できてから `protect_live` へ昇格する運用判断。

### 5.5 shadow 境界の越境 (protect_live のみ)

`protect_live` で初めて orchestrator が**保護目的で**本番ポジションを実クローズ/SL更新する (これまで守ってきた shadow 境界を保護目的で越える)。

- 越えるのは **保護経路のみ** (emergency close / profit protection SL 更新)。**新規 entry の発注は依然 shadow** (broker entry 結線は Phase 3/F)。
- single execution writer 原則を保護クローズでも維持 (`position_mgr.close_position` / `broker.update_remote_sl` の 1 箇所)。
- MT5 authoritative ガード等、既存 price_monitor が持っていた安全条件 (price source が mt5 でなければ emergency close を skip 等) を worker でも踏襲する。

---

## 6. テスト (TDD)

### get_quote (§3)
- `/quote` 成功で bid/ask/mid/spread(=ask-bid 価格差)/observed_at が実値で埋まる。
- **observed_at が naive local に正規化される** (aware で返らない)。aware bridge time を入力し、出力が naive かつ local 値であることを検証。
- **URL に `to_mt5_symbol` 変換後の symbol が入る** (`USDJPY=X` → `USDJPY`)。
- `QuoteSnapshot.spread` が価格差 (ask-bid) であり pips でないこと。
- bridge 503/404 で `Mt5UnreachableError`。

### D-1 producer (§4)
- producer が poll で最新 `QuoteSnapshot` を保持し `latest(pair)` で返す。
- `/quote` 成功で spread 実値 (価格差)、`/quote` 失敗で `/ohlcv` へ degrade (spread=None) する。
- **producer 経由 quote で watch の `quote_age_sec` が正しく算出される** (observed_at が naive local なので `naive now - observed` が成功し None にならない) — H1 回帰ガード。
- 取得例外時に最新値を更新せず、古い `observed_at` が残る (freshness wall が止められる状態)。
- poll 周期で値が更新される。

### watch 切替 (§4.4)
- `stage >= producer` で quote_provider が `producer.latest` 直読に分岐。
- `stage = off` で現行 fetch 経路を維持 (回帰)。
- spread 実値化後、watch_evaluator が `spread_pips > max` で block、実値なら通す。

### D-2 保護移設 (§5)
- 純関数流用で price_monitor と**同一入力同一 action/target_sl** (判定ロジック不変の確認)。
- **`action="close"` 局面で worker が実クローズしない** (price_monitor と同じく raise_sl のみ実行) — H4 回帰ガード。close 実行は別タスク。
- `protect_shadow`: 実 SL更新せず `protection_decisions` に記録のみ。price_monitor 側も `source="price_monitor"` で記録する追記が `protect_shadow` 以上でのみ有効。
- `protect_live`: single writer (`broker.update_remote_sl`/`position_mgr.update_protection_state`) 経由で SL更新、price_monitor の profit protection 経路は停止。
- `stage = off` / `producer`: 保護 worker 不起動 + price_monitor 完全無改変 (回帰)。
- `record_protection_decision` / `compare_protection_decisions` の store API が動作し、`create_all()` で新テーブルが既存 orch.db に追加される。

### 比較検証 (§5.4)
- 同局面で price_monitor と tick_worker の `action`/`target_sl` が一致 (並走比較クエリ)。

---

## 7. 実装順序とフェーズ境界

| 段 | stage | 内容 | 本番影響 |
|---|---|---|---|
| D-1a | (`off`→`producer`) | get_quote + producer + watch 直読 + spread 実値化 | shadow 内 |
| D-2a | (`producer`→`protect_shadow`) | 保護 worker (記録のみ) + `protection_decisions` テーブル + price_monitor 側記録追記 (§5.3) + 並走比較 | shadow 内 |
| D-2b | (`protect_shadow`→`protect_live`) | 保護 worker 実行 + price_monitor 保護停止 + single writer | **本番保護** |

各段で TDD → code review → commit。D-1a は本番に触れないので先に安定化。D-2b は並走比較で一致を確認してからの運用判断 (config 昇格)。

## 8. Review Checklist

- [ ] §3 get_quote が bid/ask/spread 実値を返し、MT5 未接続で安全に `Mt5UnreachableError` に倒れるか。
- [ ] §3 (H1) get_quote の observed_at が **naive local** に正規化され、watch の `quote_age_sec` が None にならないか。
- [ ] §3 (H3) get_quote が **`to_mt5_symbol` 変換**を URL にかけているか (`USDJPY=X`→`USDJPY`)。
- [ ] §4.5 (H2) producer が `QuoteSnapshot.spread` に **価格差 (ask-bid)** を入れ、pips 値を入れていないか (二重 pips 化で全 reject を防ぐ)。
- [ ] §5.1.1 (H4) worker が `action="close"` を **実行せず** price_monitor と同一挙動か (close 実行は別タスク)。
- [ ] §5.4 (M5) protection_decisions の ORM model / store API / `create_all()` 追加 / 比較クエリが定義され、保存先 DB が明確か。
- [ ] §4 producer が最新 quote を保持し、`/quote` 失敗で `/ohlcv`+spread=None へ degrade するか。
- [ ] §4 取得例外時に古い observed_at が残り、freshness wall が stale を検知して trigger を止めるか。
- [ ] §4.4 watch loop が `stage>=producer` で producer 直読、`off` で現行 fetch を保つか (live 直読 / planning は snapshot 経由の 2 系統を壊さないか)。
- [ ] §5.1 保護純関数を流用し (作り直さず)、price_monitor と同一 action/target_sl を出すか。
- [ ] §5.3 `protect_shadow` で実クローズせず記録のみ、`protect_live` で single writer に集約し price_monitor 保護を停止するか。
- [ ] §5.4 並走比較で既存ポーリングと同結果を定量確認できるか。
- [ ] §5.5 越境は保護経路のみで、新規 entry 発注は shadow のままか。
- [ ] §2 `off` で全現行経路が無改変か (回帰)。

## 9. 将来課題 (本 spec 外)

- **websocket (push 型) 化:** 全 tick 補足が要る用途 (tick DB・約定マイクロ構造分析・ミリ秒スキャルピング戦略) をやる場合に検討。bridge 側 ws サーバー新規実装 + finance 側 ws クライアント (接続維持・再接続管理) が必要。本 spec の producer インターフェース (`latest(pair)`) は push 駆動に差し替えても上位 (watch/保護 worker) を変えずに済む形にしておく。
- **保護 worker の tick イベント駆動化:** 現状 polling 追従。将来 producer が push 化したら、worker を「quote 更新通知駆動」に寄せられる。
