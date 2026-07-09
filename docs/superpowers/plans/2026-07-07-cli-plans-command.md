# CLI `plans` コマンド Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** finance の対話 CLI に `plans` コマンドを追加し、orchestrator が保持中の取引 plan を pending_approval (承認待ち) / active (発注監視中) に分けてターミナル表示する。

**Architecture:** 3層。(1) 取得層 `OrchestratorStore.get_plans_by_status()` を追加、(2) 整形層 `src/orchestrator/plan_view.py` (新規、CLI 非依存で F-5 API と共用可能)、(3) 表示層 `_cmd_plans()` を `src/cli.py` に追加。承認ゲート未実装でも pending は 0 件が返るだけで動く。

**Tech Stack:** Python, SQLAlchemy ORM (既存 `_TradePlan`), Rich table, pytest。

**Spec:** `docs/superpowers/specs/2026-07-07-cli-plans-command-design.md`
**Branch:** `feat/planner-watch-loop` (finance repo)

**前提知識 (このリポジトリ固有):**
- Windows から UNC (`\\wsl.localhost\Ubuntu-24.04\home\teru\project\finance`) でファイル編集し、
  テストは **PowerShell ツール**で WSL 経由実行する:
  `wsl.exe -d Ubuntu-24.04 --cd /home/teru/project/finance -- bash -lc "uv run pytest <args> 2>&1 | tail -N"`
  (Git Bash 経由の wsl.exe は文字化けで失敗。Windows 側で直接 `uv run pytest` は WSL の .venv を壊すので禁止)
- git 操作は Bash ツールで `cd //wsl.localhost/Ubuntu-24.04/home/teru/project/finance && git ...`。
- docs/ は .gitignore されているため docs のコミットは `git add -f` が必要 (今回 docs はコミット対象外)。
- コミットメッセージに attribution (Co-Authored-By 等) を付けない。
- working tree に無関係な untracked ファイル (MagicMock ゴミ, uv.lock) がある。
  **各タスクで指定したファイル以外は絶対に git add しない** (`git add .` 禁止)。
- `_TradePlan` の必須列: pair, status, created_at, updated_at (nullable=False)。
  他は nullable。`OrchestratorStore` は `self._engine` を持ち、`from sqlalchemy.orm import Session`
  で `with Session(store._engine) as s:` が使える (テストの ORM 直挿しで使用)。
- `create_trade_plan(status=...)` は `PLAN_STATUSES` 外を `ValueError` で拒否する。
  `PLAN_STATUSES = ("active","triggered","expired","invalidated","superseded","suspended","requires_replan")`
  に `pending_approval` は無い → pending plan は ORM 直挿しでのみ seed 可能。

---

### Task 1: 取得層 — `get_plans_by_status()`

**Files:**
- Modify: `src/data/orchestrator_store.py` (既存 `get_active_plans` の直後に追加)
- Test: `tests/test_orchestrator_store.py` (末尾に追記)

- [ ] **Step 1: Write the failing test**

`tests/test_orchestrator_store.py` の末尾に追記。冒頭の既存 import に `_TradePlan` と
`Session` を足す必要があるので、まずファイル冒頭の import 群に以下2行を追加する
(既存の `from src.data.orchestrator_store import OrchestratorStore` の下):

```python
from src.data.orchestrator_store import _TradePlan
from sqlalchemy.orm import Session
```

そして末尾に追記:

```python
def _seed_plan(store, *, pair, status, direction="long", created_offset_sec=0):
    """任意 status の plan を ORM 直挿しで seed する (pending_approval も可能)。

    create_trade_plan() は PLAN_STATUSES 外の status を拒否するため、テストでは
    ORM で直接 insert する。取得層は status 文字列一致で引くだけなので DB に行が
    あれば読める。created_at をずらして降順ソートを検証可能にする。
    """
    from datetime import timedelta
    from src.utils.clock import db_now
    now = db_now() + timedelta(seconds=created_offset_sec)
    with Session(store._engine) as s:
        plan = _TradePlan(
            pair=pair, snapshot_id=1, horizon="day", direction=direction,
            entry_conditions_json=[], action_json={"sl": 1.0, "tp": 2.0},
            invalidation_json=[], expires_at=now + timedelta(hours=8),
            status=status, created_by_run_id=1,
            created_at=now, updated_at=now,
        )
        s.add(plan)
        s.commit()
        return plan.plan_id


def test_get_plans_by_status_active_only(store: OrchestratorStore) -> None:
    a = _seed_plan(store, pair="USDJPY=X", status="active")
    _seed_plan(store, pair="USDJPY=X", status="suspended")
    rows = store.get_plans_by_status(("active",))
    ids = {p.plan_id for p in rows}
    assert a in ids
    assert all(p.status == "active" for p in rows)


def test_get_plans_by_status_pending_approval(store: OrchestratorStore) -> None:
    p = _seed_plan(store, pair="USDJPY=X", status="pending_approval")
    _seed_plan(store, pair="USDJPY=X", status="active")
    rows = store.get_plans_by_status(("pending_approval",))
    ids = {r.plan_id for r in rows}
    assert ids == {p}


def test_get_plans_by_status_empty_tuple_returns_empty(store: OrchestratorStore) -> None:
    _seed_plan(store, pair="USDJPY=X", status="active")
    assert store.get_plans_by_status(()) == []


def test_get_plans_by_status_orders_created_desc(store: OrchestratorStore) -> None:
    older = _seed_plan(store, pair="USDJPY=X", status="active", created_offset_sec=0)
    newer = _seed_plan(store, pair="USDJPY=X", status="active", created_offset_sec=10)
    rows = store.get_plans_by_status(("active",))
    assert [r.plan_id for r in rows[:2]] == [newer, older]


def test_get_plans_by_status_pair_filter(store: OrchestratorStore) -> None:
    usd = _seed_plan(store, pair="USDJPY=X", status="active")
    _seed_plan(store, pair="EURUSD=X", status="active")
    rows = store.get_plans_by_status(("active",), pair="USDJPY=X")
    assert {r.plan_id for r in rows} == {usd}
```

- [ ] **Step 2: Run test to verify it fails**

Run (PowerShell ツール):
`wsl.exe -d Ubuntu-24.04 --cd /home/teru/project/finance -- bash -lc "uv run pytest tests/test_orchestrator_store.py -k get_plans_by_status -v 2>&1 | tail -20"`

Expected: FAIL — `AttributeError: 'OrchestratorStore' object has no attribute 'get_plans_by_status'`

- [ ] **Step 3: Write minimal implementation**

`src/data/orchestrator_store.py` の `get_active_plans` メソッド (return 文の直後、
次の `# ── order_intents` コメントの前) に追加:

```python
    def get_plans_by_status(
        self, statuses: tuple[str, ...], pair: str | None = None,
    ) -> list[_TradePlan]:
        """指定 status 群の plan を created_at 降順で返す (pair 指定で絞り込み)。

        get_active_plans と違い任意 status を引ける汎用メソッド。承認ゲート
        (pending_approval) 表示や F-5 API がこれを共用する。空 statuses は
        in_(()) の DB 依存を避けて空リストを返す。
        """
        if not statuses:
            return []
        with Session(self._engine) as session:
            stmt = select(_TradePlan).where(_TradePlan.status.in_(statuses))
            if pair is not None:
                stmt = stmt.where(_TradePlan.pair == pair)
            stmt = stmt.order_by(_TradePlan.created_at.desc())
            plans = list(session.execute(stmt).scalars().all())
            for p in plans:
                session.expunge(p)
            return plans
```

注意: `select` は同ファイル冒頭で既に import 済み (get_active_plans が使用)。確認して
未 import なら `from sqlalchemy import select` を足す。

- [ ] **Step 4: Run test to verify it passes**

Run: `wsl.exe -d Ubuntu-24.04 --cd /home/teru/project/finance -- bash -lc "uv run pytest tests/test_orchestrator_store.py -k get_plans_by_status -v 2>&1 | tail -20"`

Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
cd //wsl.localhost/Ubuntu-24.04/home/teru/project/finance && git add src/data/orchestrator_store.py tests/test_orchestrator_store.py && git commit -m "feat: OrchestratorStore.get_plans_by_status で任意 status の plan を取得"
```

---

### Task 2: 整形層 — `src/orchestrator/plan_view.py`

**Files:**
- Create: `src/orchestrator/plan_view.py`
- Test: `tests/test_plan_view.py` (新規)

- [ ] **Step 1: Write the failing test**

`tests/test_plan_view.py` を新規作成:

```python
"""plan_view.plan_to_row の整形テスト。

CLI 表示と将来の承認ゲート F-5 API が共用する整形層。_TradePlan を表示/API 用の
dict に変換する。sl/tp 欠損や entry 条件無しでも落ちず None/空へフォールバックする。
(spec: docs/superpowers/specs/2026-07-07-cli-plans-command-design.md §3.2)
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from src.orchestrator.plan_view import plan_to_row


def _plan(**over):
    """_TradePlan 相当の属性を持つダミー (plan_to_row は属性アクセスのみ)。"""
    base = dict(
        plan_id=7, pair="USDJPY=X", direction="long",
        entry_conditions_json=[{"type": "price_at_or_below", "value": 150.30}],
        action_json={"sl": 149.8, "tp": 151.5},
        expires_at=datetime(2026, 6, 27, 12, 0, 0),
        created_at=datetime(2026, 6, 20, 12, 0, 0),
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_plan_to_row_full():
    row = plan_to_row(_plan())
    assert row["plan_id"] == 7
    assert row["pair"] == "USDJPY=X"
    assert row["direction"] == "long"
    assert row["entry_summary"] == "price_at_or_below 150.3"
    assert row["sl"] == 149.8
    assert row["tp"] == 151.5
    assert row["expires_at"] == datetime(2026, 6, 27, 12, 0, 0)
    assert row["created_at"] == datetime(2026, 6, 20, 12, 0, 0)


def test_plan_to_row_missing_action_json():
    row = plan_to_row(_plan(action_json=None))
    assert row["sl"] is None
    assert row["tp"] is None


def test_plan_to_row_no_entry_conditions():
    row = plan_to_row(_plan(entry_conditions_json=[]))
    assert row["entry_summary"] == ""


def test_plan_to_row_malformed_entry_conditions_does_not_raise():
    """要素が dict でない壊れた entry_conditions でも落ちず空文字に倒す。"""
    row = plan_to_row(_plan(entry_conditions_json=["broken", None]))
    assert row["entry_summary"] == ""


def test_plan_to_row_malformed_action_json_type():
    """action_json が dict でない場合も sl/tp は None に倒す。"""
    row = plan_to_row(_plan(action_json="not-a-dict"))
    assert row["sl"] is None
    assert row["tp"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `wsl.exe -d Ubuntu-24.04 --cd /home/teru/project/finance -- bash -lc "uv run pytest tests/test_plan_view.py -v 2>&1 | tail -20"`

Expected: FAIL — `ModuleNotFoundError: No module named 'src.orchestrator.plan_view'`

- [ ] **Step 3: Write minimal implementation**

`src/orchestrator/plan_view.py` を新規作成:

```python
"""取引 plan の表示/API 用整形層 (CLI 非依存)。

CLI (`plans` コマンド) と将来の承認ゲート F-5 API がここを共用する。cli.py に置くと
prompt_toolkit / Rich / 取引サイクル系を引き込んで責務が逆流するため、軽量な独立
モジュールに切り出している。entry 条件の短縮表記は context_builder._entry_summary を
再利用する (DRY)。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.orchestrator.context_builder import _entry_summary

if TYPE_CHECKING:
    from src.data.orchestrator_store import _TradePlan


def _safe_entry_summary(conditions: Any) -> str:
    """entry_conditions_json が壊れていても落ちない _entry_summary ラッパ。

    _entry_summary は各要素が dict である前提で c.get() を呼ぶため、要素が str や
    None だと AttributeError になる。dict 要素だけに絞ってから渡し、それでも失敗
    したら空文字に倒す (表示は best-effort)。
    """
    if not isinstance(conditions, list):
        return ""
    safe = [c for c in conditions if isinstance(c, dict)]
    try:
        return _entry_summary(safe)
    except Exception:
        return ""


def plan_to_row(plan: "_TradePlan") -> dict[str, Any]:
    """_TradePlan 1件を表示/API 用の dict に整形する (純関数)。

    sl/tp は action_json から生値を取り、欠損・型不整合時は None。表示整形 (「-」等) は
    呼び出し側 (表示層) の責務。フィールドキーは承認ゲート spec F-5 と揃える。
    壊れた JSON でも 1件で全体が落ちないよう best-effort に倒す。
    """
    action = plan.action_json if isinstance(plan.action_json, dict) else {}
    return {
        "plan_id": plan.plan_id,
        "pair": plan.pair,
        "direction": plan.direction,
        "entry_summary": _safe_entry_summary(plan.entry_conditions_json),
        "sl": action.get("sl"),
        "tp": action.get("tp"),
        "expires_at": plan.expires_at,
        "created_at": plan.created_at,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `wsl.exe -d Ubuntu-24.04 --cd /home/teru/project/finance -- bash -lc "uv run pytest tests/test_plan_view.py -v 2>&1 | tail -20"`

Expected: PASS (5 passed — full / action欠損 / entry無し / malformed entry / malformed action型)

- [ ] **Step 5: Commit**

```bash
cd //wsl.localhost/Ubuntu-24.04/home/teru/project/finance && git add src/orchestrator/plan_view.py tests/test_plan_view.py && git commit -m "feat: plan_view.plan_to_row — CLI/API 共用の plan 整形層 (reasoning は F-5 時に追加)"
```

---

### Task 3: 表示層 — `_cmd_plans()` + CLI 配線

**Files:**
- Modify: `src/cli.py` (整形/表示関数の追加 + dispatch + help テキスト)
- Test: `tests/test_cli_plans.py` (新規 — 表示ヘルパの薄い検証)

このタスクは Rich 出力が主で自動テストしにくいため、表示から切り離せる小さな純関数
`_fmt_dt` / `_fmt_num` を作ってそれをテストし、`_cmd_plans` 本体は手動確認 (Task 4) とする。

- [ ] **Step 1: Write the failing test**

`tests/test_cli_plans.py` を新規作成:

```python
"""cli plans 表示ヘルパ (_fmt_dt / _fmt_num) のテスト。

Rich テーブル本体 (_cmd_plans) は手動確認だが、None フォールバックを持つ整形
ヘルパは純関数として単体検証する。
(spec: docs/superpowers/specs/2026-07-07-cli-plans-command-design.md §3.3)
"""
from __future__ import annotations

from datetime import datetime
from io import StringIO
from types import SimpleNamespace

import src.cli as cli
from src.cli import _cmd_plans, _fmt_dt, _fmt_num


def test_fmt_dt_formats_month_day_hour_min():
    assert _fmt_dt(datetime(2026, 7, 8, 9, 5, 0)) == "07-08 09:05"


def test_fmt_dt_none_returns_dash():
    assert _fmt_dt(None) == "-"


def test_fmt_num_formats_three_decimals():
    assert _fmt_num(149.8) == "149.800"


def test_fmt_num_none_returns_dash():
    assert _fmt_num(None) == "-"


def test_fmt_num_non_numeric_returns_dash():
    """float 変換できない値 (壊れた action_json) でも落ちず「-」。"""
    assert _fmt_num("bad") == "-"


class _FakeStore:
    """OrchestratorStore の get_plans_by_status だけ差し替える fake。"""

    def __init__(self, mapping):
        self._mapping = mapping  # status tuple の先頭 -> list[_TradePlan 相当]

    def get_plans_by_status(self, statuses, pair=None):
        return self._mapping.get(statuses[0], [])


def _fake_plan(pair="USDJPY=X", direction="long"):
    return SimpleNamespace(
        plan_id=1, pair=pair, direction=direction,
        entry_conditions_json=[{"type": "price_at_or_below", "value": 150.3}],
        action_json={"sl": 149.8, "tp": 151.5},
        expires_at=datetime(2026, 7, 8, 9, 0, 0),
        created_at=datetime(2026, 7, 7, 22, 0, 0),
    )


def _capture_console(monkeypatch):
    """_console を StringIO 出力の Console に差し替え、出力バッファを返す。"""
    from rich.console import Console
    buf = StringIO()
    monkeypatch.setattr(cli, "_console", Console(file=buf, force_terminal=False, width=200))
    return buf


def test_cmd_plans_shows_pending_and_active(monkeypatch):
    monkeypatch.setattr(
        cli, "OrchestratorStore",
        lambda _p: _FakeStore({"pending_approval": [_fake_plan()], "active": [_fake_plan()]}),
    )
    buf = _capture_console(monkeypatch)
    _cmd_plans(SimpleNamespace(prices_db_path="ignored"))
    out = buf.getvalue()
    assert "pending_approval" in out
    assert "active" in out
    assert "USDJPY=X" in out


def test_cmd_plans_empty_shows_nashi(monkeypatch):
    monkeypatch.setattr(
        cli, "OrchestratorStore",
        lambda _p: _FakeStore({}),  # 両方 0 件
    )
    buf = _capture_console(monkeypatch)
    _cmd_plans(SimpleNamespace(prices_db_path="ignored"))
    out = buf.getvalue()
    assert out.count("なし") == 2  # pending / active 両方 (なし)


def test_cmd_plans_db_error_is_reported(monkeypatch):
    def _boom(_p):
        raise RuntimeError("db locked")
    monkeypatch.setattr(cli, "OrchestratorStore", _boom)
    buf = _capture_console(monkeypatch)
    _cmd_plans(SimpleNamespace(prices_db_path="ignored"))
    out = buf.getvalue()
    assert "DB 読み取り失敗" in out
    assert "RuntimeError" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `wsl.exe -d Ubuntu-24.04 --cd /home/teru/project/finance -- bash -lc "uv run pytest tests/test_cli_plans.py -v 2>&1 | tail -20"`

Expected: FAIL — `ImportError: cannot import name '_fmt_dt' from 'src.cli'`

- [ ] **Step 3: Write minimal implementation**

(3a) `src/cli.py` の import 群 (先頭付近、`from src.trading.position_manager import PositionManager`
の下あたり) に追加:

```python
from src.data.orchestrator_store import OrchestratorStore
from src.orchestrator.plan_view import plan_to_row
```

(3b) `_cmd_status` 関数の直前に、整形ヘルパと表示コマンドを追加:

```python
def _fmt_dt(dt) -> str:
    """datetime を MM-DD HH:MM に整形。None は「-」。"""
    return dt.strftime("%m-%d %H:%M") if dt is not None else "-"


def _fmt_num(v) -> str:
    """数値を小数3桁に整形。None・非数値 (壊れた action_json) は「-」。"""
    if v is None:
        return "-"
    try:
        return f"{float(v):.3f}"
    except (TypeError, ValueError):
        return "-"


def _plans_table(title: str, rows: list[dict]) -> Table:
    """整形済み row 群から Rich テーブルを作る (0件でも見出しは呼び出し側で出す)。"""
    tbl = Table(title=title, box=box.SIMPLE, show_header=True, padding=(0, 1))
    tbl.add_column("ペア")
    tbl.add_column("方向", justify="center")
    tbl.add_column("entry条件")
    tbl.add_column("SL", justify="right")
    tbl.add_column("TP", justify="right")
    tbl.add_column("期限", justify="right")
    tbl.add_column("作成", justify="right")
    for r in rows:
        arrow = "📈 long" if r["direction"] == "long" else "📉 short"
        tbl.add_row(
            r["pair"], arrow, r["entry_summary"] or "-",
            _fmt_num(r["sl"]), _fmt_num(r["tp"]),
            _fmt_dt(r["expires_at"]), _fmt_dt(r["created_at"]),
        )
    return tbl


def _cmd_plans(config: AppConfig) -> None:
    try:
        store = OrchestratorStore(config.prices_db_path)
        pending = [plan_to_row(p) for p in store.get_plans_by_status(("pending_approval",))]
        active = [plan_to_row(p) for p in store.get_plans_by_status(("active",))]
    except Exception as e:
        _console.print(f"[red]DB 読み取り失敗: {type(e).__name__}: {e}[/red]")
        return

    _console.print()
    if pending:
        _console.print(_plans_table("承認待ち (pending_approval)", pending))
    else:
        _console.print("[bold]承認待ち (pending_approval)[/bold]  [dim](なし)[/dim]")
    _console.print()
    if active:
        _console.print(_plans_table("発注監視中 (active)", active))
    else:
        _console.print("[bold]発注監視中 (active)[/bold]  [dim](なし)[/dim]")
    _console.print()
```

(3c) dispatch ループ (`elif cmd == "audit":` ブロックの後、`else:` の前) に追加:

```python
            elif cmd in ("plans", "plan"):
                _cmd_plans(config)
```

(3d) help テキスト (`_cmd_audit` の `audit review` 行の下、`close` 行の上) に追加:

```python
  [cyan]plans[/cyan]           — 保持中の取引plan(承認待ち/監視中)を表示
```

- [ ] **Step 4: Run test to verify it passes**

Run: `wsl.exe -d Ubuntu-24.04 --cd /home/teru/project/finance -- bash -lc "uv run pytest tests/test_cli_plans.py -v 2>&1 | tail -25"`

Expected: PASS (8 passed — _fmt_dt×2 / _fmt_num×3 / _cmd_plans×3)

注意: `_cmd_plans` テストは `cli.OrchestratorStore` と `cli._console` を monkeypatch する。
そのため Step 3(3a) の `from src.data.orchestrator_store import OrchestratorStore` は
`src.cli` の名前空間に束縛されている必要がある (モジュール属性として差し替え可能)。

- [ ] **Step 5: Commit**

```bash
cd //wsl.localhost/Ubuntu-24.04/home/teru/project/finance && git add src/cli.py tests/test_cli_plans.py && git commit -m "feat: CLI plans コマンド — 保持中plan(承認待ち/監視中)を表示"
```

---

### Task 4: 全 suite 検証 + 手動スモーク

**Files:** なし (検証のみ)

- [ ] **Step 1: app 側 full suite**

Run: `wsl.exe -d Ubuntu-24.04 --cd /home/teru/project/finance -- bash -lc "uv run pytest -q 2>&1 | tail -5"`

Expected: 全件 PASS (直近 green 基準 1389 passed + 本実装の新規: 取得層5 + 整形層5 + 表示層8 = 18 件 = 1407 前後 passed / 0 failed)

- [ ] **Step 2: import 健全性チェック (plan_view が cli を引き込まないこと)**

Run: `wsl.exe -d Ubuntu-24.04 --cd /home/teru/project/finance -- bash -lc "uv run python -c 'import sys; import src.orchestrator.plan_view; assert \"src.cli\" not in sys.modules, \"plan_view must not import cli\"; print(\"OK: plan_view is cli-independent\")' 2>&1 | tail -5"`

Expected: `OK: plan_view is cli-independent` (F-5 API が plan_view を使っても重い CLI 依存を引かないことの担保)

- [ ] **Step 3: 手動スモーク (任意、対話 CLI を起動できる環境で)**

対話 CLI を起動し `plans` を入力:

```bash
wsl.exe -d Ubuntu-24.04 --cd /home/teru/project/finance -- bash -lc "uv run python main.py"
# プロンプトで: plans
```

Expected: 「承認待ち (pending_approval) (なし)」と、active plan があれば「発注監視中 (active)」
テーブルが表示される。承認ゲート未実装のため pending は常に (なし)。plan が DB に無くても
エラーにならず両方 (なし) 表示になること。

---

## 実装後メモ

- **DB migration 不要** (読み取り専用、schema 変更なし)。ただし**実行中の finance プロセスには
  新しい CLI コマンドはロードされない** — 反映するには通常どおりプロセス再起動が必要。
- 承認ゲート (spec 2026-07-05) 実装時: F-5 API は `get_plans_by_status(("pending_approval",))` と
  `plan_to_row` を再利用し、`plan_to_row` に reasoning_summary を追加拡張する。
