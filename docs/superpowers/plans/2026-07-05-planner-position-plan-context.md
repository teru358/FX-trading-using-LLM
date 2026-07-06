# Planner 建玉・既存 plan 参照配線 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** planner に建玉 (position) と既存 plan (current_plan) を配線し、scale-in を「新シグナル明示」の構造化フィールドで統制する。建玉取得失敗時は決定的に direct_hold。

**Architecture:** spec `docs/superpowers/specs/2026-07-05-planner-position-plan-context.md` (codex レビュー反映済み) の P-1〜P-6。provider は raw dict list を返すだけ、整形 (pnl_r 等) は DecisionContextBuilder が build 時 quote.mid で行う。snapshot に position_json/current_plan_json を保存し検証可能性を担保。position/current_plan の実データ配線は **build() (planning 経路) のみ**。assemble() (watch tick 経路) は stub のまま (1s tick で position reload しない)。

**Tech Stack:** Python / SQLAlchemy (SQLite) / pytest。テスト実行は WSL: `wsl.exe -d Ubuntu-24.04 --cd /home/teru/project/finance -- bash -lc "uv run pytest ... -q"`。コミットは conventional commits・attribution footer なし。

**注意:** 既存の未追跡ゴミ (`<MagicMock ...>` ファイル、uv.lock の既存差分) には触れない。コミットは対象ファイルを明示指定で add。

---

### Task 1: ExecutionPlanDraft に scale_in / new_signal_evidence を追加 (P-2b 前半)

**Files:**
- Modify: `src/orchestrator/schemas.py` (ExecutionPlanDraft, ~line 236)
- Test: `tests/test_orchestrator_schemas.py` (既存ファイルに追記。無ければ ExecutionPlanDraft テストのある既存ファイルを grep で特定し追記)

- [ ] **Step 1: 失敗するテストを書く**

```python
def test_draft_scale_in_fields_parsed():
    raw = json.dumps({
        "direction": "long",
        "entry_conditions": [{"type": "price_at_or_below", "value": 150.0}],
        "action": {"sl": 149.0, "tp": 152.0},
        "invalidation": [],
        "expires_at": "2026-07-05T20:00:00",
        "reasoning_summary": "test",
        "scale_in": True,
        "new_signal_evidence": "1h RSI divergence formed after original entry",
    })
    draft = ExecutionPlanDraft.from_llm_json(raw)
    assert draft.scale_in is True
    assert "divergence" in draft.new_signal_evidence
    d = draft.to_storage_dict()
    assert d["scale_in"] is True
    assert d["new_signal_evidence"]


def test_draft_scale_in_defaults_false_when_absent():
    raw = json.dumps({
        "direction": "long",
        "entry_conditions": [{"type": "price_at_or_below", "value": 150.0}],
        "action": {}, "invalidation": [],
        "expires_at": "2026-07-05T20:00:00", "reasoning_summary": "t",
    })
    draft = ExecutionPlanDraft.from_llm_json(raw)
    assert draft.scale_in is False
    assert draft.new_signal_evidence is None


def test_draft_scale_in_true_requires_evidence():
    raw = json.dumps({
        "direction": "long",
        "entry_conditions": [{"type": "price_at_or_below", "value": 150.0}],
        "action": {}, "invalidation": [],
        "expires_at": "2026-07-05T20:00:00", "reasoning_summary": "t",
        "scale_in": True, "new_signal_evidence": "  ",
    })
    with pytest.raises(SchemaParseError):
        ExecutionPlanDraft.from_llm_json(raw)


def test_draft_scale_in_rejects_non_bool():
    """codex Medium: bool("false") is True の丸め込みを許さない。型は JSON bool のみ。"""
    base = {
        "direction": "long",
        "entry_conditions": [{"type": "price_at_or_below", "value": 150.0}],
        "action": {}, "invalidation": [],
        "expires_at": "2026-07-05T20:00:00", "reasoning_summary": "t",
    }
    with pytest.raises(SchemaParseError):
        ExecutionPlanDraft.from_llm_json(json.dumps({**base, "scale_in": "false"}))
    with pytest.raises(SchemaParseError):
        ExecutionPlanDraft.from_llm_json(
            json.dumps({**base, "scale_in": True, "new_signal_evidence": 123}))
```

- [ ] **Step 2: RED 確認** — `uv run pytest tests/test_orchestrator_schemas.py -q -k scale_in`
- [ ] **Step 3: 実装** — `ExecutionPlanDraft` に:

```python
    scale_in: bool = False
    new_signal_evidence: str | None = None
```

`__post_init__` 末尾に:

```python
        if self.scale_in and not (self.new_signal_evidence or "").strip():
            raise ValueError("scale_in=true requires non-empty new_signal_evidence")
```

`from_llm_json` の try 内・`cls(...)` の前に型検証 (ValueError は既存 except で
SchemaParseError に包まれる):

```python
            scale_in = data.get("scale_in", False)
            if not isinstance(scale_in, bool):
                raise ValueError(f"scale_in must be a JSON bool, got {scale_in!r}")
            evidence = data.get("new_signal_evidence")
            if evidence is not None and not isinstance(evidence, str):
                raise ValueError(
                    f"new_signal_evidence must be null or string, got {type(evidence).__name__}"
                )
```

`cls(...)` に:

```python
                scale_in=scale_in,
                new_signal_evidence=evidence,
```

`to_storage_dict` に `"scale_in": self.scale_in, "new_signal_evidence": self.new_signal_evidence,` を追加。

- [ ] **Step 4: GREEN 確認** (対象ファイル全体)
- [ ] **Step 5: Commit** — `feat: add scale_in/new_signal_evidence to ExecutionPlanDraft`

---

### Task 2: decision_snapshots に position_json / current_plan_json (P-5)

**Files:**
- Modify: `src/data/orchestrator_store.py` (_DecisionSnapshot ~line 45, _migrate ~line 319, create_snapshot ~line 336)
- Test: `tests/test_orchestrator_store.py` (create_snapshot テストのある既存ファイルに追記) + migration テスト

- [ ] **Step 1: 失敗するテストを書く**

```python
def test_snapshot_persists_position_and_current_plan(tmp_path):
    store = OrchestratorStore(tmp_path / "o.db")
    pos = {"count": 1, "items": [{"direction": "long", "entry_price": 150.0}]}
    cur = {"plan_id": 9, "status": "active"}
    sid = store.create_snapshot(
        pair="USDJPY=X", as_of_time=datetime(2026, 7, 5, 12, 0),
        position_json=pos, current_plan_json=cur,
    )
    snap = store.get_snapshot(sid)
    assert snap.position_json == pos
    assert snap.current_plan_json == cur


def test_snapshot_migration_adds_columns(tmp_path):
    """旧 schema (position_json 無し) の DB を開くと ALTER で列が生える。"""
    import sqlite3
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE decision_snapshots ("
        "snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT, pair VARCHAR NOT NULL,"
        "as_of_time DATETIME NOT NULL, quote_json JSON, technical_ref JSON,"
        "news_ref JSON, created_at DATETIME NOT NULL)"
    )
    conn.commit()
    conn.close()
    store = OrchestratorStore(db)
    sid = store.create_snapshot(
        pair="USDJPY=X", as_of_time=datetime(2026, 7, 5, 12, 0),
        position_json={"count": 0, "items": []},
    )
    assert store.get_snapshot(sid).position_json == {"count": 0, "items": []}
```

- [ ] **Step 2: RED 確認**
- [ ] **Step 3: 実装** — `_DecisionSnapshot` に列追加:

```python
    position_json     = Column(JSON)   # P-5: LLM が見た建玉ブロック (検証用)
    current_plan_json = Column(JSON)   # P-5: 同 current_plan (null 可)
```

`_migrate` の migrations リストに追記:

```python
            ("decision_snapshots", "position_json", "JSON"),
            ("decision_snapshots", "current_plan_json", "JSON"),
```

`create_snapshot` に kwargs `position_json: dict | None = None, current_plan_json: dict | None = None` を追加し `_DecisionSnapshot(...)` へ渡す。

- [ ] **Step 4: GREEN + store テスト全体回帰**
- [ ] **Step 5: Commit** — `feat: persist position/current_plan context in decision snapshots`

---

### Task 3: trade_plans に scale_in / new_signal_evidence (P-2b 後半)

**Files:**
- Modify: `src/data/orchestrator_store.py` (_TradePlan ~line 85, _migrate, create_trade_plan ~line 441)
- Test: 同上ファイルに追記

- [ ] **Step 1: 失敗するテストを書く**

```python
def test_trade_plan_persists_scale_in_fields(tmp_path):
    store = OrchestratorStore(tmp_path / "o.db")
    pid = store.create_trade_plan(
        pair="USDJPY=X", snapshot_id=1, horizon="day", direction="long",
        entry_conditions_json=[{"type": "price_at_or_below", "value": 150.0}],
        action_json={}, invalidation_json=[],
        expires_at=datetime(2026, 7, 5, 20, 0), created_by_run_id=1,
        scale_in=True, new_signal_evidence="new 1h breakout",
    )
    plan = store.get_trade_plan(pid)
    assert plan.scale_in is True
    assert plan.new_signal_evidence == "new 1h breakout"


def test_trade_plan_scale_in_defaults_null(tmp_path):
    store = OrchestratorStore(tmp_path / "o.db")
    pid = store.create_trade_plan(
        pair="USDJPY=X", snapshot_id=1, horizon="day", direction="long",
        entry_conditions_json=[{"type": "price_at_or_below", "value": 150.0}],
        action_json={}, invalidation_json=[],
        expires_at=datetime(2026, 7, 5, 20, 0), created_by_run_id=1,
    )
    plan = store.get_trade_plan(pid)
    assert plan.scale_in in (None, False)
    assert plan.new_signal_evidence is None
```

(取得 API は `get_trade_plan` (orchestrator_store.py:484)。migration テストも Task 2 と同型で trade_plans 版を1本追加。)

- [ ] **Step 2: RED 確認**
- [ ] **Step 3: 実装** — `_TradePlan` に:

```python
    scale_in            = Column(Boolean)  # P-2b: 建玉ありでの同方向 plan (null=旧データ)
    new_signal_evidence = Column(String)   # P-2b: scale_in の新シグナル根拠
```

(`Boolean` を sqlalchemy import に追加。) `_migrate` に:

```python
            ("trade_plans", "scale_in", "BOOLEAN"),
            ("trade_plans", "new_signal_evidence", "VARCHAR"),
```

`create_trade_plan` に kwargs `scale_in: bool | None = None, new_signal_evidence: str | None = None` を追加し `_TradePlan(...)` へ。

- [ ] **Step 4: GREEN + 回帰**
- [ ] **Step 5: Commit** — `feat: persist scale_in fields on trade plans`

---

### Task 4: bootstrap に make_position_provider (P-1 provider 側)

**Files:**
- Modify: `src/orchestrator/bootstrap.py` (make_quote_provider ~line 47 の近くに追加)
- Test: `tests/test_position_provider.py` (新規)

- [ ] **Step 1: 失敗するテストを書く**

```python
"""make_position_provider: raw position dict list を返す (整形は builder 側)。"""
from types import SimpleNamespace

from src.orchestrator.bootstrap import make_position_provider


def _config_with_state(tmp_path):
    # make_position_provider は config.state_dir しか読まないので fake で足りる
    return SimpleNamespace(state_dir=tmp_path / "state")


def test_provider_returns_empty_for_no_positions(tmp_path):
    config = _config_with_state(tmp_path)
    provider = make_position_provider(config)
    assert provider("USDJPY=X") == []


def test_provider_returns_raw_dicts_filtered_by_pair(tmp_path):
    from src.persistence.state_store import StateStore
    from src.trading.position_manager import PositionManager, Order

    config = _config_with_state(tmp_path)
    mgr = PositionManager(StateStore(config.state_dir), context="test")
    order = Order.new(
        pair="USDJPY=X", direction="buy", entry_price=150.0,
        stop_loss=149.0, take_profit=152.0, position_size=10000,
        signal_reason="original breakout",
    )
    mgr.open_position(order)  # 既存の建玉登録 API 名を position_manager.py で確認して使う

    provider = make_position_provider(config)
    items = provider("USDJPY=X")
    assert len(items) == 1
    assert items[0]["direction"] == "buy"          # raw のまま (long 変換は builder)
    assert items[0]["entry_price"] == 150.0
    assert items[0]["entry_reason"] == "original breakout"
    assert "initial_risk_price_distance" in items[0]
    assert provider("EURUSD=X") == []
```

- [ ] **Step 2: RED 確認**
- [ ] **Step 3: 実装** — bootstrap.py に追加 (make_quote_provider の後):

```python
def make_position_provider(config: "AppConfig") -> Callable[[str], list[dict]]:
    """planning context 用の raw position provider (spec P-1)。

    ProtectionWorker と同じく self-contained な PositionManager を1つ持ち、
    呼び出し毎に disk から reload する (planning は 60s 周期なので安価)。
    整形 (pnl_r/long 正規化) は DecisionContextBuilder 側 (codex High#2)。
    """
    from src.persistence.state_store import StateStore
    from src.trading.position_manager import PositionManager

    mgr = PositionManager(StateStore(config.state_dir), context="PlanningContext")

    def provider(pair: str) -> list[dict]:
        mgr.reload()
        out: list[dict] = []
        for o in mgr.get_account_state().open_positions:
            if o.pair != pair:
                continue
            out.append({
                "direction": o.direction,                     # "buy" | "sell" raw
                "entry_price": o.entry_price,
                "size": o.position_size,
                "opened_at": o.opened_at.isoformat() if o.opened_at else None,
                "mfe_r": o.max_favorable_r,
                "initial_risk_price_distance": o.initial_risk_price_distance,
                "is_scale_in": o.is_scale_in,
                "entry_reason": o.signal_reason or "",
            })
        return out

    return provider
```

(`Callable` が bootstrap の import に無ければ typing から追加。`mgr.open_position` 等のテスト用 API 名は position_manager.py の実物に合わせる。)

- [ ] **Step 4: GREEN 確認**
- [ ] **Step 5: Commit** — `feat: add position provider for planning context`

---

### Task 5: DecisionContextBuilder に position ブロック (P-1 builder 側)

**Files:**
- Modify: `src/orchestrator/context_builder.py` (__init__ ~line 64, _assemble ~line 132, _empty_position ~line 242)
- Test: `tests/test_context_builder_position.py` (新規)

- [ ] **Step 1: 失敗するテストを書く**

```python
"""position ブロック整形 (P-1): raw dict → 正規化 + pnl_r 算出 + fail-safe。"""


def _builder(tmp_path, position_provider=None):
    db = tmp_path / "o.db"
    return DecisionContextBuilder(
        OrchestratorStore(db), AnalysisStore(db), OrchestratorConfig(),
        position_provider=position_provider,
    )


def _quote():
    return QuoteSnapshot(bid=151.0, ask=151.02, mid=151.01, spread=0.02,
                         source="test", observed_at=datetime(2026, 7, 5, 12, 0))


def test_no_provider_yields_empty_block(tmp_path):
    ctx = _builder(tmp_path).build(pair="USDJPY=X", now=datetime(2026, 7, 5, 12, 0),
                                   quote=_quote())
    assert ctx["position"] == {"count": 0, "items": []}


def test_position_shaped_with_pnl_r(tmp_path):
    raw = [{"direction": "buy", "entry_price": 150.0, "size": 10000,
            "opened_at": "2026-07-05T09:00:00", "mfe_r": 1.5,
            "initial_risk_price_distance": 0.5, "is_scale_in": False,
            "entry_reason": "breakout"}]
    ctx = _builder(tmp_path, lambda pair: raw).build(
        pair="USDJPY=X", now=datetime(2026, 7, 5, 12, 0), quote=_quote())
    item = ctx["position"]["items"][0]
    assert ctx["position"]["count"] == 1
    assert item["direction"] == "long"              # buy → long 正規化
    assert item["pnl_r"] == pytest.approx((151.01 - 150.0) / 0.5, abs=0.01)
    assert item["mfe_r"] == 1.5


def test_sell_position_pnl_r_sign_flipped(tmp_path):
    raw = [{"direction": "sell", "entry_price": 152.0, "size": 10000,
            "opened_at": None, "mfe_r": 0.0,
            "initial_risk_price_distance": 0.5, "is_scale_in": False,
            "entry_reason": ""}]
    ctx = _builder(tmp_path, lambda pair: raw).build(
        pair="USDJPY=X", now=datetime(2026, 7, 5, 12, 0), quote=_quote())
    assert ctx["position"]["items"][0]["direction"] == "short"
    assert ctx["position"]["items"][0]["pnl_r"] == pytest.approx((152.0 - 151.01) / 0.5, abs=0.01)


def test_zero_risk_distance_gives_null_pnl_r(tmp_path):
    raw = [{"direction": "buy", "entry_price": 150.0, "size": 1,
            "opened_at": None, "mfe_r": 0.0, "initial_risk_price_distance": 0.0,
            "is_scale_in": False, "entry_reason": ""}]
    ctx = _builder(tmp_path, lambda pair: raw).build(
        pair="USDJPY=X", now=datetime(2026, 7, 5, 12, 0), quote=_quote())
    assert ctx["position"]["items"][0]["pnl_r"] is None


def test_provider_exception_yields_unavailable(tmp_path):
    def boom(pair):
        raise RuntimeError("state file corrupt")
    ctx = _builder(tmp_path, boom).build(
        pair="USDJPY=X", now=datetime(2026, 7, 5, 12, 0), quote=_quote())
    assert ctx["position"] == {"count": None, "items": [], "status": "unavailable"}
```

- [ ] **Step 2: RED 確認**
- [ ] **Step 3: 実装** —

`__init__` に `position_provider: "PositionProvider | None" = None` を追加し
`self._position_provider = position_provider`。型 alias をモジュール先頭に:

```python
# pair -> raw open positions (bootstrap.make_position_provider が生成)。
# 整形 (buy→long / pnl_r) は builder 側 (spec P-1, codex High#2)。
PositionProvider = Callable[[str], list[dict]]
```

メソッド追加:

```python
    def _build_position(self, pair: str, quote_dict: dict[str, Any]) -> dict[str, Any]:
        """raw position を §7 position ブロックに整形する (P-1)。

        provider 失敗は status="unavailable" に倒す。このケースの安全は prompt
        でなく pipeline の決定的 fail-safe (P-4) が処理する。
        """
        if self._position_provider is None:
            return {"count": 0, "items": []}
        try:
            raw = self._position_provider(pair)
        except Exception:
            logger.warning(
                "[ORCH] position provider failed for %s — unavailable", pair,
                exc_info=True,
            )
            return {"count": None, "items": [], "status": "unavailable"}
        mid = quote_dict.get("mid")
        items = [self._position_item(p, mid) for p in raw]
        return {"count": len(items), "items": items}

    @staticmethod
    def _position_item(p: dict[str, Any], mid: float | None) -> dict[str, Any]:
        entry = p.get("entry_price")
        risk = p.get("initial_risk_price_distance") or 0.0
        pnl_r = None
        if mid is not None and entry is not None and risk > 0:
            delta = mid - entry
            if p.get("direction") == "sell":
                delta = -delta
            pnl_r = round(delta / risk, 2)
        return {
            "direction": "short" if p.get("direction") == "sell" else "long",
            "entry_price": entry,
            "size": p.get("size"),
            "opened_at": p.get("opened_at"),
            "pnl_r": pnl_r,
            "mfe_r": p.get("mfe_r"),
            "is_scale_in": bool(p.get("is_scale_in", False)),
            "entry_reason": (p.get("entry_reason") or "")[:200],
        }
```

`_empty_position` を新形状に変更:

```python
    @staticmethod
    def _empty_position() -> dict[str, Any]:
        return {"count": 0, "items": []}
```

`_assemble` に `position: dict[str, Any] | None = None` 引数を追加し
`"position": position if position is not None else self._empty_position(),` に変更。
`build()` は `position = self._build_position(pair, quote_dict)` を組んで `_assemble` へ渡す
(snapshot への保存は Task 6)。`assemble()` は引数を渡さない (stub のまま — watch tick
経路で position reload しない設計判断、plan ヘッダ参照)。

- [ ] **Step 4: GREEN + `uv run pytest tests/ -q -k context_builder` 回帰** (旧 stub 形状
  `{side: None, ...}` を前提にした既存テストがあれば新形状 `{count: 0, items: []}` に追従)
- [ ] **Step 5: Commit** — `feat: wire real position block into planning context`

---

### Task 6: current_plan ブロック + snapshot 保存 (P-2 / P-5 配線)

**Files:**
- Modify: `src/orchestrator/context_builder.py` (build ~line 95, _assemble)
- Test: `tests/test_context_builder_position.py` に追記

- [ ] **Step 1: 失敗するテストを書く**

```python
def test_current_plan_block_from_active_plan(tmp_path):
    db = tmp_path / "o.db"
    orch = OrchestratorStore(db)
    sid = orch.create_snapshot(pair="USDJPY=X", as_of_time=datetime(2026, 7, 5, 11, 0))
    orch.create_trade_plan(
        pair="USDJPY=X", snapshot_id=sid, horizon="day", direction="long",
        entry_conditions_json=[{"type": "price_at_or_below", "value": 149.8}],
        action_json={}, invalidation_json=[],
        expires_at=datetime(2026, 7, 5, 20, 0), created_by_run_id=1,
    )
    builder = DecisionContextBuilder(orch, AnalysisStore(db), OrchestratorConfig())
    ctx = builder.build(pair="USDJPY=X", now=datetime(2026, 7, 5, 12, 0), quote=_quote())
    cur = ctx["current_plan"]
    assert cur["direction"] == "long"
    assert cur["status"] == "active"
    assert "price_at_or_below" in cur["entry_summary"]


def test_current_plan_entry_summary_non_price_conditions(tmp_path):
    """type ごとの参照キー出し分け (codex Medium): value / value_pips / status。"""
    db = tmp_path / "o.db"
    orch = OrchestratorStore(db)
    sid = orch.create_snapshot(pair="USDJPY=X", as_of_time=datetime(2026, 7, 5, 11, 0))
    orch.create_trade_plan(
        pair="USDJPY=X", snapshot_id=sid, horizon="day", direction="long",
        entry_conditions_json=[
            {"type": "spread_below", "value_pips": 2.0},
            {"type": "technical_status_is", "status": "ok"},
        ],
        action_json={}, invalidation_json=[],
        expires_at=datetime(2026, 7, 5, 20, 0), created_by_run_id=1,
    )
    builder = DecisionContextBuilder(orch, AnalysisStore(db), OrchestratorConfig())
    ctx = builder.build(pair="USDJPY=X", now=datetime(2026, 7, 5, 12, 0), quote=_quote())
    assert "spread_below 2.0" in ctx["current_plan"]["entry_summary"]
    assert "technical_status_is ok" in ctx["current_plan"]["entry_summary"]


def test_current_plan_none_when_no_active(tmp_path):
    ctx = _builder(tmp_path).build(pair="USDJPY=X", now=datetime(2026, 7, 5, 12, 0),
                                   quote=_quote())
    assert ctx["current_plan"] is None


def test_snapshot_stores_position_and_current_plan(tmp_path):
    db = tmp_path / "o.db"
    orch = OrchestratorStore(db)
    raw = [{"direction": "buy", "entry_price": 150.0, "size": 1, "opened_at": None,
            "mfe_r": 0.0, "initial_risk_price_distance": 0.5, "is_scale_in": False,
            "entry_reason": ""}]
    builder = DecisionContextBuilder(
        orch, AnalysisStore(db), OrchestratorConfig(), position_provider=lambda p: raw)
    ctx = builder.build(pair="USDJPY=X", now=datetime(2026, 7, 5, 12, 0), quote=_quote())
    snap = orch.get_snapshot(ctx["snapshot_id"])
    assert snap.position_json["count"] == 1
    assert snap.current_plan_json is None
```

- [ ] **Step 2: RED 確認**
- [ ] **Step 3: 実装** —

```python
    def _build_current_plan(self, pair: str) -> dict[str, Any] | None:
        """該当 pair の active plan (最大1件、supersede 保証) の要約 (P-2)。

        approval gate 導入時に対象へ pending_approval を追加する (gate spec F-1)。
        """
        try:
            plans = self._orch.get_active_plans(pair)
        except Exception:
            logger.warning("[ORCH] current_plan read failed for %s", pair, exc_info=True)
            return None
        if not plans:
            return None
        plan = plans[0]
        return {
            "plan_id": plan.plan_id,
            "status": plan.status,
            "direction": plan.direction,
            "entry_summary": _entry_summary(plan.entry_conditions_json),
            "expires_at": plan.expires_at.isoformat() if plan.expires_at else None,
            "created_at": plan.created_at.isoformat() if plan.created_at else None,
        }
```

モジュール末尾に:

```python
def _entry_summary(conditions: list | None) -> str:
    """entry_conditions_json の短縮表記 (先頭2条件)。

    条件 type ごとに参照キーが違う (codex Medium): price 系=value /
    spread_below=value_pips / technical_status_is=status。既知キーを順に引き、
    どれも無ければ type のみ。
    """
    if not conditions:
        return ""

    def _one(c: dict) -> str:
        for key in ("value", "value_pips", "status"):
            if c.get(key) is not None:
                return f"{c.get('type')} {c[key]}"
        return str(c.get("type"))

    parts = [_one(c) for c in conditions[:2]]
    if len(conditions) > 2:
        parts.append(f"(+{len(conditions) - 2} more)")
    return ", ".join(parts)
```

`build()` を変更:

```python
        position = self._build_position(pair, quote_dict)
        current_plan = self._build_current_plan(pair)
        snapshot_id = self._orch.create_snapshot(
            pair=pair, as_of_time=now, quote_json=quote_dict,
            technical_ref=technical.get("_ref"), news_ref=news.get("_ref"),
            position_json=position, current_plan_json=current_plan,
        )
        ctx = self._assemble(pair, now, quote_dict, technical, news,
                             position=position, current_plan=current_plan)
```

`_assemble` に `current_plan: dict[str, Any] | None = None` 引数を追加し、返り値 dict に
`"current_plan": current_plan,` を追加 (assemble() 経路では None)。

- [ ] **Step 4: GREEN + context_builder 系回帰**
- [ ] **Step 5: Commit** — `feat: add current_plan block and snapshot persistence`

---

### Task 7: pipeline の決定的 fail-safe — position unavailable → direct_hold (P-4)

**Files:**
- Modify: `src/orchestrator/planning_pipeline.py` (_pipeline ~line 135)
- Test: `tests/test_planning_pipeline.py` (既存に追記)

- [ ] **Step 1: 失敗するテストを書く** (既存 pipeline テストの fake planner/exec/store
  fixture を流用する。planner が呼ばれないことを assert するのが本質):

```python
async def test_position_unavailable_skips_llm_and_holds(pipeline_fixtures):
    """position.status=unavailable → LLM 不呼び出しで direct_hold (P-4)。"""
    pipeline, planner, orch = pipeline_fixtures  # 既存 fixture 形式に合わせる
    context = make_context()  # 既存 helper
    context["position"] = {"count": None, "items": [], "status": "unavailable"}
    result = await pipeline.run(pair="USDJPY=X", context=context, run_id=1)
    assert result.outcome == "direct_hold"
    assert planner.scan_calls == 0            # LLM を呼んでいない
    dec = orch.get_decision(result.decision_ids[0])
    assert "position unavailable" in dec.reasoning_summary
```

- [ ] **Step 2: RED 確認**
- [ ] **Step 3: 実装** — `_pipeline` 先頭 (Step 2 opportunity scan の前) に:

```python
        # P-4 決定的 fail-safe: 建玉が読めないときは planning しない。「建玉を知らずに
        # 重ねる」の再発防止をprompt (確率的) に依存させない (codex High#1)。
        position = context.get("position") or {}
        if position.get("status") == "unavailable":
            did = self._orch.record_decision(
                run_id=run_id, snapshot_id=snapshot_id, pair=pair,
                decision_type="direct_hold", decision="hold",
                reasoning_summary="position unavailable — planning skipped (fail-safe)",
                trade_horizon=horizon,
            )
            return PipelineResult(outcome="direct_hold", decision_ids=[did])
```

- [ ] **Step 4: GREEN + pipeline テスト回帰**
- [ ] **Step 5: Commit** — `feat: fail safe to direct_hold when position is unavailable`

---

### Task 8: pipeline の scale_in 決定的導出 + 永続化 (P-2b 配線)

**scale_in の正本は LLM 申告ではなく決定的導出** (codex plan review High):
`scale_in = 建玉 items に draft.direction と同方向が存在`。どちら向きの誤申告も
導出値で上書きし、scale-in なのに new_signal_evidence が無い plan は**作らない**
(予算内なら 1 回 redraft、尽きたら決定的 reject)。

**Files:**
- Modify: `src/orchestrator/planning_pipeline.py` (_pipeline draft 処理 ~line 176, _commit_plan ~line 269)
- Test: `tests/test_planning_pipeline.py` に追記 (fake exec agent の逐次 draft 返却は
  既存 revise ループテストの fake パターンに合わせる)

- [ ] **Step 1: 失敗するテストを書く**

```python
async def test_scale_in_coerced_false_without_position(pipeline_fixtures):
    """position 空なのに LLM が scale_in=true → 導出値 false で保存。"""
    pipeline, agents, orch = pipeline_fixtures
    agents.set_drafts([draft_kwargs(scale_in=True, new_signal_evidence="hallucinated")])
    context = make_context()   # position は {"count": 0, "items": []}
    result = await pipeline.run(pair="USDJPY=X", context=context, run_id=1)
    assert result.outcome == "plan_create"
    plan = orch.get_active_plans("USDJPY=X")[0]
    assert plan.scale_in is False
    assert plan.new_signal_evidence is None


async def test_same_direction_position_forces_scale_in(pipeline_fixtures):
    """同方向建玉があれば LLM が scale_in を省略しても scale-in 扱いになり、
    evidence 無し draft は redraft feedback を受ける (codex High の本丸)。"""
    pipeline, agents, orch = pipeline_fixtures
    agents.set_drafts([
        draft_kwargs(direction="long", scale_in=False),                  # 1st: 申告漏れ
        draft_kwargs(direction="long", scale_in=True,
                     new_signal_evidence="fresh 1h breakout after entry"),  # 2nd: 根拠あり
    ])
    context = make_context()
    context["position"] = {"count": 1, "items": [{"direction": "long"}]}
    result = await pipeline.run(pair="USDJPY=X", context=context, run_id=1)
    assert result.outcome == "plan_create"
    assert result.redraft_count == 1
    plan = orch.get_active_plans("USDJPY=X")[0]
    assert plan.scale_in is True
    assert plan.new_signal_evidence == "fresh 1h breakout after entry"


async def test_scale_in_rejected_when_evidence_never_provided(pipeline_fixtures):
    """redraft しても evidence が出ない → 決定的 reject (plan を作らない)。"""
    pipeline, agents, orch = pipeline_fixtures
    agents.set_drafts([
        draft_kwargs(direction="long", scale_in=False),
        draft_kwargs(direction="long", scale_in=False),
    ])
    context = make_context()
    context["position"] = {"count": 1, "items": [{"direction": "long"}]}
    result = await pipeline.run(pair="USDJPY=X", context=context, run_id=1)
    assert result.outcome == "reject"
    assert "new_signal_evidence" in result.reason
    assert orch.get_active_plans("USDJPY=X") == []


async def test_opposite_direction_position_is_not_scale_in(pipeline_fixtures):
    """逆方向建玉 (ドテン提案) は scale-in ではない → evidence 不要で通る。"""
    pipeline, agents, orch = pipeline_fixtures
    agents.set_drafts([draft_kwargs(direction="long", scale_in=False)])
    context = make_context()
    context["position"] = {"count": 1, "items": [{"direction": "short"}]}
    result = await pipeline.run(pair="USDJPY=X", context=context, run_id=1)
    assert result.outcome == "plan_create"
    assert orch.get_active_plans("USDJPY=X")[0].scale_in is False
```

- [ ] **Step 2: RED 確認**
- [ ] **Step 3: 実装** — `_pipeline` の `draft = clamp_draft_ttl(...)` 直後に:

```python
            # P-2b: scale_in の正本は決定的導出 (codex High)。建玉 items に draft と
            # 同方向があるかで決まり、LLM 申告と食い違えば導出値で上書きする。
            # 【順序重要】replace() は __post_init__ を再実行するため、evidence 空のまま
            # scale_in=True へ replace すると ValueError で fail-safe に落ちる。
            # 先に evidence を検証し、replace は妥当な組み合わせでのみ行う。
            items = (context.get("position") or {}).get("items") or []
            same_dir = any(it.get("direction") == draft.direction for it in items)
            if same_dir and not (draft.new_signal_evidence or "").strip():
                # scale-in なのに新シグナル根拠なし → plan は作らない。
                if redraft_count < max_redraft:
                    redraft_count += 1
                    feedback = [
                        "An open same-direction position exists: this plan is a"
                        " scale-in. Set scale_in=true and provide new_signal_evidence"
                        " describing a NEW signal that did not exist at the original"
                        " entry.",
                    ]
                    continue
                did = self._orch.record_decision(
                    run_id=run_id, snapshot_id=snapshot_id, pair=pair,
                    decision_type="reject", decision="reject",
                    reasoning_summary=(
                        "scale-in without new_signal_evidence (deterministic)"
                    ),
                    trade_horizon=horizon,
                )
                return PipelineResult(
                    outcome="reject", decision_ids=[did], redraft_count=redraft_count,
                    reason="scale-in without new_signal_evidence",
                )
            if draft.scale_in != same_dir:
                draft = replace(
                    draft, scale_in=same_dir,
                    new_signal_evidence=draft.new_signal_evidence if same_dir else None,
                )
```

`_commit_plan` の `create_trade_plan(...)` 呼び出しに:

```python
            scale_in=draft.scale_in, new_signal_evidence=draft.new_signal_evidence,
```

(`replace` は planning_pipeline で import 済み — clamp_draft_ttl が使用。)

- [ ] **Step 4: GREEN + 回帰**
- [ ] **Step 5: Commit** — `feat: derive scale_in deterministically in planning pipeline`

---

### Task 9: prompt 指針 + _compact_context + draft schema 文言 (P-3)

**Files:**
- Modify: `src/orchestrator/execution_opinion_agent.py` (_horizon_guidance ~line 86 の後に _position_guidance 追加、_compact_context ~line 108、draft system prompt の JSON schema 記述)
- Modify: `src/orchestrator/planner_agent.py` (_compact_context ~line 96、prompt 組み立て ~line 57/81)
- Test: `tests/test_horizon_guidance.py` 系の既存テストファイルに追記 (無ければ新規 `tests/test_position_guidance.py`)

- [ ] **Step 1: 失敗するテストを書く**

```python
from src.orchestrator.execution_opinion_agent import _position_guidance


def test_no_position_no_plan_yields_empty():
    assert _position_guidance({"position": {"count": 0, "items": []}}) == ""


def test_position_present_demands_scale_in_fields():
    ctx = {"position": {"count": 1, "items": [{"direction": "long"}]}}
    g = _position_guidance(ctx)
    assert "scale_in" in g
    assert "new_signal_evidence" in g


def test_current_plan_present_prefers_hold():
    ctx = {"position": {"count": 0, "items": []},
           "current_plan": {"plan_id": 1, "direction": "long"}}
    g = _position_guidance(ctx)
    assert "existing plan" in g.lower()


def test_compact_context_includes_current_plan():
    from src.orchestrator.planner_agent import _compact_context as planner_cc
    from src.orchestrator.execution_opinion_agent import _compact_context as exec_cc
    ctx = {"current_plan": {"plan_id": 1}}
    assert planner_cc(ctx)["current_plan"] == {"plan_id": 1}
    assert exec_cc(ctx)["current_plan"] == {"plan_id": 1}
```

- [ ] **Step 2: RED 確認**
- [ ] **Step 3: 実装** —

execution_opinion_agent.py の `_horizon_guidance` の直後に:

```python
def _position_guidance(context: dict[str, Any]) -> str:
    """建玉・既存 plan がある場合の指針文 (spec P-3)。無ければ空文字。"""
    parts: list[str] = []
    position = context.get("position") or {}
    if position.get("items"):
        parts.append(
            "An open position exists for this pair (decision_context.position)."
            " A same-direction plan is a scale-in: set scale_in=true and describe in"
            " new_signal_evidence a NEW signal that did not exist at the original"
            " entry (re-stating the original entry reason is not valid)."
            " An opposite-direction plan is a reversal: justify why the existing"
            " position's thesis is failing."
        )
    if context.get("current_plan"):
        parts.append(
            "An existing plan is already waiting for entry"
            " (decision_context.current_plan). If its premise still holds, prefer"
            " keeping it (direct_hold) over replacing it; a replacement must cite"
            " what changed."
        )
    return " ".join(parts)
```

両 agent の `_compact_context` に `"current_plan": context.get("current_plan"),` を追加。
両 agent の prompt 組み立て (planner_agent.py:57/81 と execution_opinion_agent の draft
prompt の `_horizon_guidance(context)` 行) の直後に `_position_guidance(context),` を追加
(空文字は無害だが、`"\n".join(...)` 前に空要素を filter している場合はその流儀に従う)。
planner_agent.py の import に `_position_guidance` を追加 (既に `_horizon_guidance` を
import している行)。

execution_opinion_agent の **draft 用 system prompt の出力 JSON schema 記述**に
2 フィールドを追記 (実際の文言は既存 schema 記述の形式に合わせる):

```
  "scale_in": false,                  // true only when adding to an existing same-direction position
  "new_signal_evidence": null         // required when scale_in=true: the NEW signal justifying the add
```

- [ ] **Step 4: GREEN + agent 系テスト回帰**
- [ ] **Step 5: Commit** — `feat: add position/current_plan prompt guidance`

---

### Task 10: bootstrap 注入 + 統合確認 + 仕上げ

**Files:**
- Modify: `src/orchestrator/bootstrap.py` (~line 179 の DecisionContextBuilder 構築)
- Test: `tests/test_orchestrator_bootstrap.py` 系既存ファイルに追記
- Modify: 本 plan ファイル (実装完了メモ)

- [ ] **Step 1: 失敗するテストを書く** (bootstrap 構築テストの既存 fixture を流用):

```python
def test_bootstrap_injects_position_provider(...):
    """build_orchestrator 経由の DecisionContextBuilder に position_provider が注入される。"""
    # 既存の bootstrap テストと同じ構築手順で runtime/builder を作り:
    assert runtime._ctx._position_provider is not None
```

- [ ] **Step 2: RED 確認**
- [ ] **Step 3: 実装** — bootstrap.py:179 の構築に注入:

```python
    context_builder = DecisionContextBuilder(
        orch_store, analysis_store, orch_cfg,
        news_provider=make_news_provider(config, store),
        risk_state_provider=make_risk_state_provider(config),
        position_provider=make_position_provider(config),
    )
```

- [ ] **Step 4: GREEN 確認**
- [ ] **Step 5: フルスイート回帰** — `uv run pytest -q` 全体 (1345 passed + 新規分が基準)。
  旧 position stub 形状に依存する落ち穂があればここで拾って追従修正
- [ ] **Step 6: 本 plan 末尾に実装完了メモ (スイート数・コミット列・スコープ外事項) を追記
- [ ] **Step 7: Commit** — `feat: inject position provider into orchestrator bootstrap`
  (メモは `docs: ...` で別コミット。docs/ は gitignore 対象だが、このブランチでは
  specs/plans を `git add -f` で意図的にコミットする運用 — 既存 docs コミットと同じ)

---

## スコープ外 (P-6 明示)

- `max_positions_per_pair` の値変更 (2 のまま)
- supersede 機構の変更
- `recent_orders/exits/trade_stats` stub の配線
- assemble() (watch tick 経路) への position 配線
- RiskGateWorker への position チェック追加

---

## 実装完了メモ (2026-07-05)

全 10 タスク完了。フルスイート **1385 passed, 0 failed** (`uv run pytest -q`)。

### コミット列 (3cee892..HEAD, 時系列昇順)

```
d81d94a refactor: normalize new_signal_evidence and tighten draft tests
23f1268 feat: persist position/current_plan context in decision snapshots
f37ff0d feat: persist scale_in fields on trade plans
84c42d8 refactor: polish store column alignment and tighten scale_in test
5a945d4 feat: add position provider for planning context
07fcd7f feat: wire real position block into planning context
9b07891 feat: add current_plan block and snapshot persistence
d77ef62 refactor: harden current_plan shaping and pin snapshot parity
0fe3483 feat: fail safe to direct_hold when position is unavailable
a6287e3 feat: derive scale_in deterministically in planning pipeline
d668296 fix: persist raw draft claim before deterministic scale_in gate
d6b1835 feat: add position/current_plan prompt guidance
03e06ec feat: inject position provider into orchestrator bootstrap
```

review 対応コミット: d81d94a / 84c42d8 / d77ef62 / d668296 + 03e06ec (下記 carry-over)。

### carry-over 解消 (03e06ec に同梱)

- **Task 9 Important#1**: `_draft_summary` に `scale_in` / `new_signal_evidence`
  (coerce 済申告値) を追加 — planner final_decision が判断対象の値を要約で受け取る
- **Task 4 Minor#1**: `make_position_provider` を `get_open_positions_by_pair`
  ベースに変更 (opened_at 昇順の時系列順序)
- **Task 9 Minor#3**: position + current_plan 両方存在時に両指針が space-joined
  単一文字列で入るテストを追加
- **Task 9 Minor#2**: final_decision の user プロンプトに "scale-in" 指針が届く
  プロンプトレベルテスト (_FakeLLM capture) を追加

### スコープ外 (変更なし)

P-6 のとおり: `max_positions_per_pair=2` 据え置き、supersede 機構不変、
`recent_orders/exits/trade_stats` stub のまま、assemble() (watch tick 経路) への
position 配線なし、RiskGateWorker 不変。

### 運用ノート

反実仮想メトリクス (spec §4) は `agent_outputs.structured_payload_json.scale_in`
(LLM 申告) vs `trade_plans.scale_in` (決定論導出) の突合で取れる。
