# CLI `plans` コマンド — 保持中 plan 状況表示 design (2026-07-07)

日付: 2026-07-07
状態: 設計確定 (実装前)
関連: `2026-07-05-discord-approval-gate.md` (F-5 の plan 一覧 API と取得層・整形を共用する)

## 1. 目的 / 背景

finance の対話 CLI (`src/cli.py`) から、orchestrator が現在保持している取引 plan
(`_TradePlan`) の状況をターミナルで確認したい。

「保持中の plan」= これから発注されうる生きた plan。具体的には:

- **active** — 承認済み (または gate OFF で自動 active) で watch が発注条件を監視中
- **pending_approval** — Discord 承認ゲートで人間の承認待ち (arm 前)

この2つを分けて一覧表示する。triggered / expired / rejected 等の終端・履歴 status は
今回のスコープ外 (「保持中 = 監視対象」に絞る)。

## 2. 承認ゲートとの関係 (順序と齟齬回避)

Discord 承認ゲート (`2026-07-05-discord-approval-gate.md`) は未実装だが、本コマンドは
それに **依存しない**。理由と整合方針:

- 依存の向きは「承認ゲート → 本コマンドが作る取得層」の一方向。承認ゲート spec F-5 の
  `GET /orchestrator/plans?status=pending_approval` は、本コマンドが追加する
  `get_plans_by_status()` をそのまま再利用できる。
- 現時点で `PLAN_STATUSES` に `pending_approval` は無いが、取得層は **status 文字列で
  DB を引くだけ** なので、該当行が無ければ 0 件が返るのみ。`PLAN_STATUSES` 定数への
  追加は本スコープでは不要 (承認ゲート spec F-1 の担当)。
- したがって承認ゲートを先に実装する必要はない。齟齬回避は「取得層・整形フィールドを
  F-5 の仕様に合わせておく」ことで担保する (下記 §3.1 / §3.2)。

## 3. 構造 (3層)

### 3.1 取得層 — `OrchestratorStore.get_plans_by_status()`

`src/data/orchestrator_store.py` に汎用メソッドを1つ追加:

```python
def get_plans_by_status(
    self, statuses: tuple[str, ...], pair: str | None = None,
) -> list[_TradePlan]:
    """指定 status 群の plan を created_at 降順で返す (pair 指定で絞り込み)。"""
```

- 既存 `get_active_plans()` は `status == "active"` 固定。これは変更せず残す
  (runtime / context_builder が呼んでおり、pair 絞り込み + active 限定の意味論が
  明確なため)。CLI は汎用の `get_plans_by_status` を使う。両者の並存は許容
  (get_active_plans を get_plans_by_status のラッパに置換する統合は本スコープ外・
  YAGNI)。
- 実装は `select(_TradePlan).where(_TradePlan.status.in_(statuses))`、pair 指定時は
  `.where(_TradePlan.pair == pair)`、`.order_by(_TradePlan.created_at.desc())`。
  返却前に `session.expunge(p)` (既存メソッドと同じ detach パターン)。
- 空 `statuses` タプルは空リストを返す (`in_(())` の DB 依存挙動を避けるため、
  クエリ前に `if not statuses: return []`)。
- 承認ゲート spec F-5 の API はこのメソッドに `("pending_approval",)` を渡せばよい。

### 3.2 整形層 — `_plan_row(plan) -> dict`

`src/cli.py` 内に plan 1件を表示用 dict にする純関数を置く。フィールドは承認ゲート
spec F-5 の一覧 API 出力と揃える:

| キー | 由来 |
|---|---|
| `plan_id` | `plan.plan_id` |
| `pair` | `plan.pair` |
| `direction` | `plan.direction` (long/short) |
| `entry_summary` | `_entry_summary(plan.entry_conditions_json)` を **context_builder から import 再利用** |
| `sl` | `(plan.action_json or {}).get("sl")` |
| `tp` | `(plan.action_json or {}).get("tp")` |
| `expires_at` | `plan.expires_at` (datetime or None) |
| `created_at` | `plan.created_at` |

- `_entry_summary` は `src/orchestrator/context_builder.py` の既存関数
  (先頭2条件を短縮表記)。**再定義せず import して使う** (DRY)。
- action_json に sl/tp が無い旧データや None は「-」表示にフォールバック。

### 3.3 表示層 — `_cmd_plans(config)`

`src/cli.py` に追加。処理:

1. `OrchestratorStore(config.prices_db_path)` を構築 (bootstrap と同じパス)。
2. pending = `get_plans_by_status(("pending_approval",))`、
   active = `get_plans_by_status(("active",))`。
3. 2つの Rich テーブルをセクション見出し付きで出力 (既存 `_cmd_status` の Table 構築に倣う)。
   0 件のセクションは見出し + `[dim](なし)[/dim]` を出す (承認ゲート稼働後に自然に埋まる)。

テーブル列: ペア / 方向 / entry条件 / SL / TP / 期限 / 作成。
- 方向は `📈 long` / `📉 short` (status の _cmd_status に倣った絵文字表記)。
- 期限・作成は `%m-%d %H:%M` に整形。None は「-」。
- SL/TP は数値なら `f"{v:.3f}"`、無ければ「-」。

### 3.4 CLI 配線

`src/cli.py` の dispatch ループ (`elif cmd == ...` 群) と help テキストに追加:

- dispatch: `elif cmd in ("plans", "plan"): _cmd_plans(config)`
- help: `[cyan]plans[/cyan]           — 保持中の取引plan(承認待ち/監視中)を表示`

## 4. データフロー

```
ユーザー入力 "plans"
  → _cmd_plans(config)
      → OrchestratorStore(config.prices_db_path)
      → get_plans_by_status(("pending_approval",))  → pending 行
      → get_plans_by_status(("active",))            → active 行
      → 各 plan を _plan_row() で整形 (_entry_summary 再利用)
      → Rich Table 2枚を _console.print
```

## 5. エラー処理

- DB / テーブル未初期化 (orchestrator を一度も起動していない環境): 取得層の例外は
  `_cmd_plans` で捕捉し `[yellow]orchestrator plan なし (DB 未初期化)[/yellow]` を表示。
  CLI ループ全体の `except Exception` にも守られるが、専用の分かりやすい文言を優先する。
- action_json / entry_conditions_json の欠損・型不整合: 整形層で個別に「-」/空文字へ
  フォールバックし、1件の不整合で全体が落ちないようにする。

## 6. テスト観点

- **取得層** (`tests/`): in-memory or tmp DB に status 違いの plan を複数投入し、
  - `get_plans_by_status(("active",))` が active のみを created_at 降順で返す
  - `get_plans_by_status(("pending_approval",))` が pending のみ返す (該当0件なら空)
  - 空タプルで空リスト
  - pair 絞り込みが効く
- **整形層**: `_plan_row()` が sl/tp あり plan・action_json 欠損 plan・entry 条件無し
  plan で期待 dict / フォールバックを返す
- **表示層は薄いので手動確認中心**: 実 DB で `plans` を叩き pending 0 件表示 + active
  表示を目視 (Step は plan の実装計画側に記載)。

## 7. スコープ外

- 承認ゲート本体: `pending_approval`/`rejected` の status 追加 (F-1)、状態遷移
  (approve/reject/TTL sweep, F-4)、REST API (F-5)、bot cog (§4) — すべて承認ゲート
  spec の担当。本コマンドはそれらの実装前後で挙動が変わらない (pending が 0→N になるだけ)。
- triggered / expired / rejected / superseded 等の履歴表示。
- plan の CLI からの操作 (承認・却下・削除) — 表示専用。
