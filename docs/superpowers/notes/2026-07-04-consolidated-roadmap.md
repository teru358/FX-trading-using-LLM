# orchestrator version2 — 検討事項 統合ロードマップ

**Date:** 2026-07-04 (**Updated: 2026-07-05** — §0b 進捗更新を追加)
**Status:** 検討メモ統合版 (spec ではない)。判断は順次ユーザー。
**Supersedes (統合元):** 以下 4 note の有効内容を本 note に統合した。旧 note は経緯・詳細根拠の記録として残す。

| 統合元 | 内容 | 統合後の扱い |
|---|---|---|
| `2026-06-25-adaptive-order-timing-brainstorm.md` | C' (観察モード) 設計素材 | §6 に要約。spec 化は Phase 4 |
| `2026-06-27-post-taskf-considerations.md` | Task F 後の棚卸し 17 項目 | §5 インベントリに全項目の現在状態を記載 (7 項目は後続に吸収済) |
| `2026-06-30-fx-freshness-and-live-test-activation.md` | FX 鮮度・live_test ブロッカー | **bridge 衝突は解決済み**。残手順を §2 Phase 0 に、却下案の記録を §7 に |
| `2026-07-04-premise-review-and-next-direction.md` | 方針ブレスト (承認ゲート/day 移行/forecast 退役/tech オミット) | §1 に要約。詳細根拠は元 note |

> 関連 spec: `2026-06-20-orchestrator-agent-loop-design-v2.md` (設計正本) / `2026-06-23-orchestrator-taskF-shadow-to-live-execution-design.md`。
> 関連 memory: [[finance_rag_adjustment_oversuppress]] / [[finance_risk_management_philosophy]] / [[finance_claude_cli_token_failure]]。

---

## 0. 現在地 (2026-07-04 時点)

- **Task F (shadow→live 発注昇格) 実装完了**、2巡レビュー対応済。Fiosracht で paper 検証中。
- **TZ バグ修正済み・デプロイ済** (FX technical ok 0→19 件、plan が risk gate まで到達)。
- **bridge 衝突は解決済み**: Mt5Client threading.Lock 実装完了 (4 コミット、bridge suite 24 / full 1275 passed) + 運用判断確定「stick・Fiosracht **両方を live_test 化**し、bridge を DRY_RUN=true で共有」。
- **判明済みの残バグ (未修正):**
  1. plan 品質: RR 計算ミス、SchemaParseError (未知 invalidation type `boj_intervention_signal` 等) — reject 19 件の原因
  2. claude-cli セッション上限 429 (深夜集中、PipelineFailed 142 件) — planning 主経路の可用性課題
- 通知は NullNotifier のまま (`DISCORD_SHADOW_WEBHOOK_URL` 未設定)。

---

## 0b. 進捗更新 (2026-07-05)

§0 以降に完了した作業。Phase 表 (§2) の該当行にも状態を反映済み。

### 完了 1: day horizon 移行 — 実装完了 (Phase 2-2 前倒し完了)

- spec: `2026-07-05-day-horizon-migration-design.md` (codex レビュー反映済)。オーバーレイ機構なし・直接設定値変更 + 構造変更 (TTL クランプ / prompt horizon 指針 / staleness config 化 / 15m 基底足+分粒度 interval / hindsight spread 込み採点)。
- 実装: 13 タスク subagent-driven + codex 1巡目 5 件修正 + 同族残存 3 箇所掃討 = **30 コミット (d8a209d→8c11099)、suite 1338 passed**。
- day 値の実体は gitignore された `config/settings.yaml` (19 項目、trading/schedule/MTF/orchestrator の 4 ブロック)。コミット正本は `settings.yaml.example` (c6a83cf)。**Fiosracht への適用は rsync 時** (live_test 切替後、rsync 安全手順厳守)。
- swing 戻しは config 編集のみで可能 (検証済)。
- **未決 adjudicate 1 件**: watch 銘柄が day 設定下で 1h 単一 TF 分析に縮退 (受容 (a) vs watch 専用 MTF (b)) → §3 に追加。

### 完了 2: 閉場中の無駄動作停止

- quote-stream producer を `is_market_open()` でゲート (閉場中は bridge /quote tick 停止、全 pair snapshot 確保後)。`b02d6e6`
- planning loop も同ゲート (閉場中は [AGGREGATE] news 集計・LLM planning が空回りしない)。`0ded758`
- どちらも単一基準 (cycles/jobs と同じ helper)・遷移ログ 1 回・月曜開場で自動再開。suite 1345。

### 完了 3: 承認ゲート spec 化 (Phase 2-3 の設計確定)

- spec: `2026-07-05-discord-approval-gate.md`。§1.2 の方針を設計に固定: **REST + bot 側 polling** (discord.py `tasks.loop` を FinanceCog に追加、persistent view ボタン、単独チャンネル)。
- 確定 7 点 (G-1〜G-7): 通信=REST polling / TTL 切れ=expired+メッセージ edit / 第3ボタンなし (放置=観察) / 単独チャンネル / 却下理由=modal 自由記述 / 承認は plan 単位 (置換で再承認) / **却下・放置 plan も watch が反実仮想追跡** (検証対象は plan でなく「却下という判断」)。
- finance 側 F-1〜F-7 (status 2 値追加 / gate 列 / supersede 拡張 / 反実仮想 watch=本体 / API 3 本 / config 既定 OFF / 3 ラベル metrics)。実装は未着手。

### 完了 4: planner 建玉・既存 plan 参照配線 — 実装完了 (§1.1 ①の恒久対応)

- 経緯: 新規 plan 時の建玉/既存 plan 参照状況を調査 → **planner は建玉を見ていない** (context の position は空 stub)・既存 plan は supersede (pair 最大 1) で構造的処理のみ、と判明。scale-in は全否定しない方針をユーザー確認 (「知らずに・シグナルなしに・むやみに」だけ否定)。
- spec: `2026-07-05-planner-position-plan-context.md` (codex spec レビュー 5 件 + plan レビュー 6 件反映)。
- 実装: 10 タスク subagent-driven + 各 2 段レビュー + 最終横断レビュー (ready) + codex 15 コミットレビュー 3 件対応 = **計 18 コミット (3cee892→c68c734)、suite 1388 passed**。
- 中身: position provider (毎 cycle disk reload) → builder 整形 (buy→long, pnl_r, MFE) / current_plan 要約 / **scale_in は決定的導出が正本** (建玉方向×draft 方向。LLM 申告は agent_outputs に保存し不一致率を測定可能) / 同方向 scale-in で新シグナル根拠空なら plan を作らない (redraft 1 回→決定的 reject) / position・current_plan 取得失敗→LLM 不呼び出し direct_hold / snapshot に position_json・current_plan_json 保存 (LLM が見たものを機械復元可能)。
- 不変 (P-6): `max_positions_per_pair: 2` (最終壁)・supersede 機構・watch 1s tick 経路。
- 運用注意: build→commit 間の position race (LLM 応答中に watch が建玉を作った場合、その plan は旧 snapshot 基準 — broker 壁が防衛。スコアカード①解釈時に注記)。

### 反映待ち

上記はすべて branch `feat/planner-watch-loop` 上。**稼働中デーモンへの反映は再起動時**。Fiosracht へは live_test 切替 (Phase 0) 後の rsync で。

---

## 1. 確定した方針・評価 (2026-07-04 ブレスト — 詳細根拠は premise-review note)

### 1.1 旧システム 5 問題 → スコアカード化

旧システムの 5 問題 (①同方向重ね ②シグナルなし発注 ③RAG 有効性不可視 ④confidence 不信 ⑤タイミング集中) を、paper/live_test 検証の**合否基準**として測定する (spec §11 metric とほぼ 1:1)。

- ①: 同方向連続エントリ率を測る (新ゲートは足さない。`max_positions_per_pair=2` は方向非依存という事実だけ記録)。**→ 2026-07-05 恒久対応実装済み (§0b-4)**: planner が建玉を見て判断 + scale-in は新シグナル根拠必須 (決定的 gate)。測定は trade_plans.scale_in / new_signal_evidence と申告 vs 導出の不一致率で SQL 化済み
- ②: plan 作成時の「条件既充足フラグ」+ 作成→trigger 経過時間を trace 記録 (即成立条件 = plan を偽装した成行、の検出)
- ③: planner 出力に `used_case_ids` 追加 → 事例引用あり/なし plan の hindsight 比較。**実証まで RAG 数値補正を新経路に持ち込まない**
- ④: **confidence は判断に使わない** (orchestrator では trace/通知のみ・ゲート決定的、を実装確認済)。hindsight から calibration curve を定期レポート化
- ⑤: trigger 時刻分布 vs 旧 cycle 発注時刻分布。**逆の失敗モード (plan が立たない/trigger しない = 静かすぎる障害) の監視必須** (TZ バグで 19h 無発注の前例)
- 旧 cycle 経路の物理削除時に `signal_confidence_threshold`・`rag_adjustment` も同時退場。

### 1.2 Discord 承認ゲート (採用推奨)

- plan lifecycle に `pending_approval` を追加。承認→active / 却下→rejected / TTL 超過→expired / 承認待ち中の条件成立→執行せず would_trigger 記録のみ。
- **承認/却下 = ラベル付きデータ**: 承認率 = planner 品質の人間評価。**却下 plan も shadow 追跡**し「却下 plan の hindsight 成績 vs 承認 plan」で人間ゲートの付加価値を測定 → 自動化卒業の実証根拠。
- 卒業基準は事前定義: 直近 N プランで承認率 ≥ X% かつ却下 plan が承認 plan を上回らない。**件数 + 期間の両建て** (複数週・重要指標週跨ぎ)。
- 3 段階: ①手動承認 (default-deny) → ②veto 窓付き自動 (T 分以内に却下なければ active 化) → ③完全自動。day 移行時は②が実質必須。
- **bot 連携は薄い結合**: 権威は finance 側 (管理 API `GET /orchestrator/plans/pending`, `POST .../approve|reject`、client.py パターン)。discord_bot は UI アダプタに徹し取引ロジックを持たない。ボタン UI なら通知送信主体を bot へ (webhook はボタン不可・fallback として残す)。承認 API は冪等、認証 2 層。
- 再承認は「改訂は常に再承認」で開始し、paper でスパム率を見て緩和判断。
- **paper 段階で即導入推奨** (無リスクでラベル蓄積開始)。

### 1.3 forecast サイクル退役 → planner outlook

- **orchestrator は forecast を読んでいない (grep 0 件・旧遺産確定)**。
- planner 出力に per-pair `outlook` (direction / 想定期間 / 想定変動幅) を毎 planning round 記録 (direct_hold 時も) → hindsight で採点 → **週 300 件超の判断サンプル** (confidence 較正・RAG 検証が取引を待たず回る)。
- `run_forecast_cycle` / ForecastStore は omit。

### 1.4 day horizon 移行 (条件付き賛成)

- サンプル不足は 2 種: 判断品質 = outlook で解決 / 執行品質 = day 移行で解決 (週 1-2 → 週 10-30 見込み)。
- **day_profile オーバーレイは未実装** (grep 0 件。detector 閾値連動 + trace 記録のみ実装済)。spec §4.6 の 6 項目 (ATR timeframe / SL・TP 距離 / time_stop / profit protection / state 閾値 / entry 感度) + plan TTL・planning 頻度の実配線が移行の本体。
- **前提 = live_test 切替 (MT5 データ経路)**。yfinance FX ~7h 遅延は day の直接障害。
- hindsight 採点は**スプレッド込み PnL 必須** (TP 距離短縮でスプレッド利益比率 2-3 倍)。
- swing 検証結果は day に転移しない → **実資金前の今が移行最適**。

### 1.5 swing データの扱い

- **ゼロから蓄積で問題なし** (swing は高々数十件・統計未成立。day は 2-4 週で追い越す)。リセットはせず horizon タグで分離。
- 3 層: (a) 生観測 (news/econ/technical snapshots/OHLCV) = 完全再利用 / (b) イベント・レジーム教訓 = 文脈として条件付き (数値教訓は転移不可) / (c) 判断統計・ATR 係数・較正・承認ラベル = day でゼロから (swing 値は対照群)。
- RAG case card に horizon メタデータ無し (確認済) → **読み側フィルタ「horizon キー無し = legacy swing」** + 新規カードから付与。SQL 側は `trade_horizon` 列既存。スコアカード集計は `WHERE horizon='day'`。
- 本物の損失はレジーム多様性のみ (カレンダー時間でしか貯まらない → 卒業基準の期間条件で担保)。

### 1.6 news 十分 / technical LLM オミット

- **news**: RSS 30 分 + deep_fetch で役割 (planner context / material trigger / 予定イベントは econ calendar) に十分。急変即応は価格経路 (market_state/C') が担う分業。拡張は寄与測定 (news 引用 plan vs 非引用の hindsight) の後。news_archive Phase 2 保留維持。day 移行でも不変。
- **technical LLM 分析はオミット推奨 (spec v2 からの設計変更)**: ①二段 LLM 冗長 (4-8B 解釈→14B 再解釈) ②material 判定が LLM 出力反転に依存 (`material_landing.py:93-98`) = LLM ゆらぎで偽反転 ③day 移行時の slot starvation を構造解消 ④最難関 refactor (§4.7 #8) 不要・決定的代替完備 (`compute_technical_score` / `compute_multi_tf_technical_score`)。
- 移行作業 3 点: ok status 再定義 (LLM 依存除去) / material 入力の決定的化 (閾値+ヒステリシス) / context builder 差し替え (tech_score・mtf_score・chart_patterns、**パターン DB 保存もここで解決**)。
- 副次効果: モデル構成が 14B + 8B + embed に簡素化 → VRAM 24GB 前提に余裕。

---

## 2. 実装順 — 5 フェーズ

### Phase 0 — 運用手順のみ・コード変更なし (今すぐ)

| # | 作業 | 備考 |
|---|---|---|
| 0-1 | bridge DRY_RUN=true 化 | `POST http://192.168.1.16:8812/admin/halt {"mode":"hard"}` → `/health` で `dry_run:true` 確認 |
| 0-2 | stick を live_test 化 | 実発注停止・情報収集継続 |
| 0-3 | Fiosracht を live_test 化 | `settings.yaml: mode:live_test + live_broker:mt5` + `mt5.yaml: bridge_url=192.168.1.16:8812` |
| 0-4 | `diag_planner_hold.py F` で FX stale→ok 検証 | day 移行・鮮度問題の前提クリア確認 |

### Phase 1 — 小さく即効の実装 (Phase 0 と並行可)

| # | 作業 | 出典 | 理由 |
|---|---|---|---|
| 1-1 | **plan 品質バグ修正** (SchemaParseError 未知 invalidation type / RR 計算ミス) | 06-30 | reject 19 件の原因。放置すると以後の全検証データが汚れる — 最優先 |
| 1-2 | forecast 退役 + planner `outlook` 追加 | §1.3 | 判断サンプル蓄積が即開始 |
| 1-3 | shadow webhook 設定 | 06-27 §5 | 承認ゲート前提 + live 監視インフラ |
| 1-4 | 観測性の最小追加: `used_case_ids` / 条件既充足フラグ / RAG horizon メタデータ | §1.1/§1.5 | スコアカードの計測材料 |
| 1-5 | 小修正: 起動バナー矛盾表示 (`(shadow) running — mode=live`) / M3 警告の要否判断 | 06-27 §12/§3 | 低リスク |

### Phase 2 — 中規模実装 (paper/live_test で検証しながら)

| # | 作業 | 順序根拠 |
|---|---|---|
| 2-1 | **technical LLM オミット** (移行作業 3 点) | day より先: boost 増でも slot 枯渇しない状態を先に作る |
| 2-2 | ~~**day 移行実装**~~ **✅ 実装完了 (2026-07-05, §0b-1)** — 適用は live_test 切替後の rsync + 再起動 | live_test (Phase 0) が前提。数値はスケール縮小の第一次値で観察→実測検算 |
| 2-3 | **承認ゲート** — **spec 化済み (2026-07-05, §0b-3)**: `2026-07-05-discord-approval-gate.md`。実装未着手 | paper で即起動しラベル蓄積開始 |
| 2-3b | **✅ planner 建玉・plan 参照配線 — 実装完了 (2026-07-05, §0b-4)** (優先度高でユーザー指示・前倒し) | §1.1 ①の恒久対応。スコアカード①の測定材料も同梱 |
| 2-4 | → paper の day モード検証 = **5 問題スコアカードの本番** | 2-1〜2-3 が揃った状態で |

### Phase 3 — 検証結果待ちの判断 (数週間後)

| # | 作業 | 出典 |
|---|---|---|
| 3-1 | スコアカード合格項目から live 昇格。承認ゲート卒業判定 (veto 窓→自動化) | §1.1/§1.2 |
| 3-2 | live 移行前必須: needs_reconcile runbook / orchestrator 建玉の reflection 発火確認 | 06-27 §4/§15 |
| 3-3 | 旧 cycle 経路の物理削除 + `signal_confidence_threshold`・`rag_adjustment` 同時退場 | 06-27 §2-1 + §1.1 |

### Phase 4 — 安定後・任意

| # | 作業 | 条件 |
|---|---|---|
| 4-1 | C' spec 化 (§6) | Task F 安定後 (承認ゲートとはレイヤー違いで共存可・確認済) |
| 4-2 | omit 残り: クラウド LLM API / feedly + forecast 関連コード物理削除 | 1 連携 1 コミット |
| 4-3 | 24B ctx 上限明文化 (16-24K) | 24B モデル採用時 |
| 4-4 | llama.cpp バッティング対策 (まず `--parallel`/slot で足りるか実測) | WebUI 併用が常態なら |
| 4-5 | watch=yfinance 固定の整理 / 設定ファイル全体整理 | 任意・保留継続 |

### 依存関係

```
live_test 切替 (Phase 0) ──→ day_profile (MT5 データ鮮度が前提)
tech オミット (2-1) ──→ day boost 増でも slot 枯渇しない (2-2 の前)
plan 品質修正 (1-1) ──→ 以後の全検証データの信頼性
webhook (1-3) ──→ 承認ゲート (2-3) ──→ ラベル蓄積 ──→ 卒業判定 (3-1)
forecast 退役+outlook (1-2) ──→ confidence 較正・RAG 検証 (取引を待たない)
```

**未解決の信頼性課題:** claude-cli 429 (深夜 PipelineFailed 142 件)。Phase 2 検証中に頻発するなら対策 (レート制御/フォールバック) の優先度を上げる。

---

## 3. 決定待ち事項

| 項目 | 決めること |
|---|---|
| ~~承認ゲート~~ | **採用決定・spec 化済み (§0b-3)**。残: 実装着手時期 / 卒業基準の数値 |
| ~~day 移行~~ | **実装完了 (§0b-1)**。残: 適用タイミング (live_test 切替とセット) |
| **watch MTF 縮退** (新規) | day 設定下で watch 銘柄が 1h 単一 TF 分析に縮退。受容 (a) vs watch 専用 MTF 設定 (b) |
| forecast 退役 | 実施判断 / outlook フィールド仕様 |
| technical LLM オミット | 実施判断 (spec v2 からの設計変更として明記) |
| 卒業基準 | 承認率 X% / N プラン / 期間条件の数値 |
| M3 警告 | `mode=live` + `stage<producer` 起動時に warning を出すか (リスク哲学と照らし判断) |
| max_redraft | 現行 1 で十分か / structural reject に別案を促すか (06-27 §10 — 機構は実装済)。scale-in gate も同予算を共有する点に留意 |

---

## 4. 検証中の観察項目 (paper/live_test 期間)

06-27 §1 の観察表は有効なまま継続:

| 観点 | 確認方法 |
|---|---|
| 旧 entry 停止 | `[CYCLE] orchestrator.mode=live — entry skip` が取引時刻に出る |
| 単一発注主体 | `execute_signal` 呼出が orchestrator 経路のみ |
| plan→trigger→execute | `📋 plan created` → `🧪 shadow trigger` → `✅ live execute` がログで追える (ログレベル INFO 必須) |
| sizing | min_lot 床に張り付いていないか |
| spread | gate reject 理由に spread 系が出ないか (**producer stage 必須** — M3) |
| recovery | `[ORCH-RECOVERY]` が誤検知しないか |

これに §1.1 スコアカード 5 項目 + 承認ゲートのラベル集計が加わる。

---

## 5. 旧 note 項目インベントリ (06-27 の 17 項目の現在状態)

| 06-27 項目 | 状態 | 行き先 |
|---|---|---|
| §1 paper 観察項目 | **有効** | 本 note §4 |
| §2-1 旧 cycle 削除 | 存続 | Phase 3-3 |
| §2-2 needs_reconcile 自動照合 | 存続 (先に runbook) | Phase 3-2 → 別 task |
| §2-3 material recheck ON 化 | 存続・live 初期検証後 | Phase 3 以降の判断 |
| §2-4 OANDA adapter | 存続・採用時のみ | 条件付き |
| §3 M3 警告判断 | 存続 | Phase 1-5 / 決定待ち |
| §4 needs_reconcile runbook | 存続 | Phase 3-2 |
| §5 shadow webhook | **承認ゲート前提に格上げ** | Phase 1-3 |
| §6 設定整理 | 保留継続 | Phase 4-5 |
| §7 C' との関係 | 有効 | Phase 4-1 (§6) |
| §9 forecast 必要性 | **確定 (退役)** | Phase 1-2 |
| §10 RiskGate→exec 再提出 | 実装済・調整判断のみ | 決定待ち (max_redraft) |
| §11 omit (TV は実施済) | 残 2 件存続 | Phase 4-2 |
| §12 起動バナー矛盾 | 存続 | Phase 1-5 |
| §13 watch=yfinance 固定 | 存続・任意 | Phase 4-5 |
| §14 テクニカル判定可視性 | **tech オミットに吸収** | Phase 2-1 (パターン DB 保存) |
| §15 reflection 発火確認 | 存続 | Phase 3-2 |
| §16 手動 SL/TP reflection | **承認/却下ラベルが実質代替** | Phase 2-3 に統合 |
| §17 llama.cpp バッティング | 存続・条件付き | Phase 4-4 |
| §18 24B ctx 上限 | 存続・条件付き | Phase 4-3 |
| §19 RAG 2 軸検証 | **スコアカード測定枠組みに統合** | Phase 1-4 → 検証 |

06-30 note: bridge 衝突 = **解決済み** (lock 実装 + 両系 live_test 化決定)。TwelveData 案 = 却下のまま (§7 に記録保持)。鮮度閾値緩和案 = 不要 (live_test で解消)。

---

## 6. C' (観察モード) 要約 — spec 化は Phase 4-1

06-25 ブレストの確定事項 (詳細は元 note):

- **トリガ:** 想定外の価格変位 (ヒゲ/介入/突発売買のスパイク)。持続トレンドと区別する。
- **中核の洞察:** スパイクに tech 再分析は無意味 —「その後の価格推移の方が重要」→ C' は tech 再分析を行わず、既存 `MarketStateDetector` 拡張で自己完結。
- **欠落 2 点:** ① state→新規発注ゲートの結線 (現状 state は cadence にしか効かない = C' の本体) ② スパイク検知 (ヒゲ vs 持続変動の区別、move_pct 窓では不足)。
- **事後挙動:** 一定期間「観察モード」で発注保留 → ボラ収束/レンジ復帰で通常 planning へ。
- **未決 6 点** (spec 化時に詰める): 範囲確定 / スパイク検知ロジック / 復帰条件 / アクション段階 / shadow・live 整合 (Task F の `_execute_live_trigger` 直前にゲートを挟む形を想定) / D (5 レイヤー) との線引き。
- **承認ゲートとの関係:** レイヤー違い (承認 = planning 層 / C' = 執行層) で共存可。

---

## 7. 却下・保留の記録 (再検討時の参照用)

- **TwelveData 切替 (FX 鮮度対策) — 却下:** 8 req/分 throttle 未実装で即 429 → 全部 yfinance フォールバック。加えて `fetch_ohlcv` の naive parse で +9h TZ 問題再発リスク。使うなら throttle + /quote batch 化 + tz 修正が前提 (06-30 に詳細)。live_test 化で不要に。
- **FX 鮮度閾値緩和 (6h→9h) — 未採用:** 古い technical で判断する妥協。live_test 化で不要に。
- **ollama 移行 — 非推奨:** モデル切替オーバーヘッドが低遅延原則と衝突。常駐 llama-server 路線維持。
- **GraphRAG — 不採用** ([[finance_graphrag_decision]])。
- **news_archive Phase 2 — 保留継続** (寄与測定の後に判断)。
- **設定ファイル全体整理 — 保留継続** ([[finance_config_simplification_deferred]])。
