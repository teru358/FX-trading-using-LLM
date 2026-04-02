# 方向別RAGデータ管理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 取引結果・予測サイクル・HOLD判断のRAGデータをbullish/bearishに分離し、シグナル結合時に方向別の過去データからスコア補正を行う。

**Architecture:** ChromaDBコレクションを `fx_reflections_bullish` / `fx_reflections_bearish` に分離し、SQLiteに `trading_sessions` テーブルを追加してセッション単位のライフサイクルを管理する。`signal_combiner.py` の `combine_signals()` 後に RAG補正レイヤーを挟み、`adjusted_score` で発注判断を行う。

**Tech Stack:** Python 3.12, SQLAlchemy, ChromaDB, pytest

---

## File Structure

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `src/rag/directional_store.py` | 方向別ChromaDBコレクション管理（bullish/bearish分離、検索、メタデータ付きupsert） |
| Create | `src/data/session_store.py` | `trading_sessions` テーブルのSQLAlchemy定義とCRUD |
| Create | `src/signals/rag_adjustment.py` | 方向別RAG検索→スコア補正ロジック |
| Create | `scripts/migrate_directional_rag.py` | 既存データ移行スクリプト |
| Create | `tests/test_rag_adjustment.py` | RAG補正ロジックのユニットテスト |
| Create | `tests/test_session_store.py` | SessionStoreのユニットテスト |
| Create | `tests/test_directional_store.py` | DirectionalStoreのユニットテスト |
| Modify | `src/config.py:95-114` | `TradingConfig` に `rag_adjustment` 設定追加 |
| Modify | `src/rag/vector_store.py:16-36` | `VectorStore.__init__` に方向別コレクション初期化追加 |
| Modify | `src/trading_cycle.py:452-478` | Phase 4b: 発注時にセッション作成+RAG entry注入 |
| Modify | `src/trading_cycle.py:312-341` | Phase 1.5: クローズ時にセッション更新+RAG complete注入 |
| Modify | `src/trading_cycle.py:421-450` | Phase 4a: レビュークローズ時も同様 |
| Modify | `src/trading_cycle.py:643-733` | forecast_cycle: 方向別コレクションへの蓄積 |
| Modify | `src/trading_cycle.py:205-255` | _review_hold_decisions: 方向別コレクションへの蓄積 |
| Modify | `config/settings.yaml` | `rag_adjustment` セクション追加 |

---

### Task 1: SessionStore — SQLiteテーブル定義とCRUD

**Files:**
- Create: `src/data/session_store.py`
- Create: `tests/test_session_store.py`

- [ ] **Step 1: テストファイル作成 — セッション作成と取得**

```python
# tests/test_session_store.py
from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from src.data.session_store import SessionStore


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / "test.db")


def test_create_and_get_session(store):
    store.create_session(
        session_id="sess-001",
        pair="EURUSD=X",
        direction="bearish",
        entry_price=1.15,
        stop_loss=1.16,
        take_profit=1.13,
        position_size=1000.0,
        signal_score=-0.35,
        signal_confidence=0.78,
        macro_context="DXY long",
        analysis_summary="Strong bearish signal",
        opened_at=datetime(2026, 4, 1, 9, 30),
    )
    session = store.get_session("sess-001")
    assert session is not None
    assert session.pair == "EURUSD=X"
    assert session.direction == "bearish"
    assert session.outcome is None  # not yet closed


def test_close_session(store):
    store.create_session(
        session_id="sess-002",
        pair="USDJPY=X",
        direction="bullish",
        entry_price=150.0,
        stop_loss=149.0,
        take_profit=152.0,
        position_size=1000.0,
        signal_score=0.40,
        signal_confidence=0.80,
        macro_context="",
        analysis_summary="Bullish setup",
        opened_at=datetime(2026, 4, 1, 15, 0),
    )
    store.close_session(
        session_id="sess-002",
        closed_at=datetime(2026, 4, 2, 10, 0),
        close_price=151.5,
        close_reason="take_profit",
        realized_pnl=1500.0,
        reflection_text="Good trade, trend followed through",
    )
    session = store.get_session("sess-002")
    assert session.outcome == "win"
    assert session.realized_pnl == 1500.0
    assert session.reflection_text == "Good trade, trend followed through"


def test_get_nonexistent_session(store):
    assert store.get_session("nonexistent") is None


def test_close_session_loss(store):
    store.create_session(
        session_id="sess-003",
        pair="EURUSD=X",
        direction="bullish",
        entry_price=1.15,
        stop_loss=1.14,
        take_profit=1.17,
        position_size=1000.0,
        signal_score=0.30,
        signal_confidence=0.70,
        macro_context="",
        analysis_summary="Weak bullish",
        opened_at=datetime(2026, 4, 1, 9, 0),
    )
    store.close_session(
        session_id="sess-003",
        closed_at=datetime(2026, 4, 1, 12, 0),
        close_price=1.14,
        close_reason="stop_loss",
        realized_pnl=-10.0,
        reflection_text="",
    )
    session = store.get_session("sess-003")
    assert session.outcome == "loss"
```

- [ ] **Step 2: テスト実行 — 失敗を確認**

Run: `cd /home/teru/project/finance && python -m pytest tests/test_session_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.data.session_store'`

- [ ] **Step 3: SessionStore実装**

```python
# src/data/session_store.py
"""取引セッションのライフサイクル管理ストア。"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, String, Text, select
from sqlalchemy.orm import Session

from src.data.price_store import _Base, _get_engine

logger = logging.getLogger(__name__)


class _TradingSession(_Base):
    """取引セッション: 発注→クローズの1サイクルを追跡する。"""
    __tablename__ = "trading_sessions"

    session_id        = Column(String, primary_key=True)
    pair              = Column(String, nullable=False, index=True)
    direction         = Column(String, nullable=False)       # "bullish" / "bearish"
    entry_price       = Column(Float, nullable=False)
    stop_loss         = Column(Float)
    take_profit       = Column(Float)
    position_size     = Column(Float)
    signal_score      = Column(Float)
    signal_confidence = Column(Float)
    macro_context     = Column(Text)
    analysis_summary  = Column(Text)
    opened_at         = Column(DateTime, nullable=False)
    closed_at         = Column(DateTime)
    close_price       = Column(Float)
    close_reason      = Column(String)
    realized_pnl      = Column(Float)
    outcome           = Column(String)                       # "win" / "loss" / None
    reflection_text   = Column(Text)
    created_at        = Column(DateTime, nullable=False, default=datetime.now)


class SessionStore:
    """trading_sessions テーブルの CRUD。"""

    def __init__(self, db_path) -> None:
        self._engine = _get_engine(db_path)

    def create_session(
        self,
        session_id: str,
        pair: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        position_size: float,
        signal_score: float,
        signal_confidence: float,
        macro_context: str,
        analysis_summary: str,
        opened_at: datetime,
    ) -> None:
        with Session(self._engine) as session:
            rec = _TradingSession(
                session_id=session_id,
                pair=pair,
                direction=direction,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                position_size=position_size,
                signal_score=signal_score,
                signal_confidence=signal_confidence,
                macro_context=macro_context,
                analysis_summary=analysis_summary,
                opened_at=opened_at,
                created_at=datetime.now(),
            )
            session.add(rec)
            session.commit()
        logger.info(f"[SESSION] Created: {session_id} {pair} {direction}")

    def get_session(self, session_id: str) -> _TradingSession | None:
        with Session(self._engine) as session:
            rec = session.get(_TradingSession, session_id)
            if rec:
                session.expunge(rec)
            return rec

    def close_session(
        self,
        session_id: str,
        closed_at: datetime,
        close_price: float,
        close_reason: str,
        realized_pnl: float,
        reflection_text: str = "",
    ) -> None:
        with Session(self._engine) as session:
            rec = session.get(_TradingSession, session_id)
            if rec is None:
                logger.warning(f"[SESSION] close_session: {session_id} not found")
                return
            rec.closed_at = closed_at
            rec.close_price = close_price
            rec.close_reason = close_reason
            rec.realized_pnl = realized_pnl
            rec.outcome = "win" if realized_pnl > 0 else "loss"
            rec.reflection_text = reflection_text or None
            session.commit()
        logger.info(
            f"[SESSION] Closed: {session_id} reason={close_reason} "
            f"pnl={realized_pnl:+.2f} outcome={'win' if realized_pnl > 0 else 'loss'}"
        )

    def update_reflection(self, session_id: str, reflection_text: str) -> None:
        with Session(self._engine) as session:
            rec = session.get(_TradingSession, session_id)
            if rec:
                rec.reflection_text = reflection_text
                session.commit()
```

- [ ] **Step 4: テスト実行 — パスを確認**

Run: `cd /home/teru/project/finance && python -m pytest tests/test_session_store.py -v`
Expected: 4 passed

- [ ] **Step 5: コミット**

```bash
cd /home/teru/project/finance
git add src/data/session_store.py tests/test_session_store.py
git commit -m "feat: add SessionStore for trading session lifecycle tracking"
```

---

### Task 2: DirectionalStore — 方向別ChromaDBコレクション

**Files:**
- Create: `src/rag/directional_store.py`
- Create: `tests/test_directional_store.py`

- [ ] **Step 1: テストファイル作成**

```python
# tests/test_directional_store.py
from __future__ import annotations

import pytest

from src.rag.directional_store import DirectionalStore


@pytest.fixture
def store(tmp_path):
    return DirectionalStore(tmp_path / "test_rag")


def _dummy_embedding(dim: int = 768) -> list[float]:
    return [0.1] * dim


def test_upsert_and_query_bullish(store):
    store.upsert(
        entry_id="sess-001_entry",
        text="EURUSD bullish setup, strong momentum",
        embedding=_dummy_embedding(),
        direction="bullish",
        pair="EURUSD=X",
        session_id="sess-001",
        session_type="trade",
        phase="entry",
        signal_score=0.35,
        confidence=0.78,
    )
    results = store.query(
        query_embedding=_dummy_embedding(),
        direction="bullish",
        top_k=5,
    )
    assert len(results) == 1
    assert results[0]["metadata"]["session_id"] == "sess-001"


def test_upsert_bearish_not_in_bullish(store):
    store.upsert(
        entry_id="sess-002_entry",
        text="EURUSD bearish reversal",
        embedding=_dummy_embedding(),
        direction="bearish",
        pair="EURUSD=X",
        session_id="sess-002",
        session_type="trade",
        phase="entry",
        signal_score=-0.40,
        confidence=0.80,
    )
    bullish_results = store.query(
        query_embedding=_dummy_embedding(),
        direction="bullish",
        top_k=5,
    )
    assert len(bullish_results) == 0

    bearish_results = store.query(
        query_embedding=_dummy_embedding(),
        direction="bearish",
        top_k=5,
    )
    assert len(bearish_results) == 1


def test_query_complete_only(store):
    store.upsert(
        entry_id="sess-003_entry",
        text="Entry analysis",
        embedding=_dummy_embedding(),
        direction="bullish",
        pair="EURUSD=X",
        session_id="sess-003",
        session_type="trade",
        phase="entry",
        signal_score=0.30,
        confidence=0.70,
    )
    store.upsert(
        entry_id="sess-004_complete",
        text="Complete cycle with result",
        embedding=_dummy_embedding(),
        direction="bullish",
        pair="EURUSD=X",
        session_id="sess-004",
        session_type="trade",
        phase="complete",
        signal_score=0.40,
        confidence=0.80,
        outcome="win",
        realized_pnl=5.0,
    )
    results = store.query(
        query_embedding=_dummy_embedding(),
        direction="bullish",
        top_k=5,
        phase_filter="complete",
    )
    assert len(results) == 1
    assert results[0]["metadata"]["session_id"] == "sess-004"
```

- [ ] **Step 2: テスト実行 — 失敗を確認**

Run: `cd /home/teru/project/finance && python -m pytest tests/test_directional_store.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: DirectionalStore実装**

```python
# src/rag/directional_store.py
"""方向別ChromaDBコレクション管理。

bullish/bearish のデータを分離して蓄積・検索する。
"""

from __future__ import annotations

import logging
from pathlib import Path

import chromadb

logger = logging.getLogger(__name__)

_BULLISH_COL = "fx_reflections_bullish"
_BEARISH_COL = "fx_reflections_bearish"


class DirectionalStore:
    """方向別のChromaDBコレクションを管理する。"""

    def __init__(self, db_path: Path) -> None:
        db_path = Path(db_path)
        db_path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(db_path))
        self._bullish = self._client.get_or_create_collection(
            name=_BULLISH_COL,
            metadata={"hnsw:space": "cosine"},
        )
        self._bearish = self._client.get_or_create_collection(
            name=_BEARISH_COL,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(
            f"DirectionalStore ready (bullish={self._bullish.count()}, "
            f"bearish={self._bearish.count()})"
        )

    def _collection(self, direction: str):
        if direction == "bullish":
            return self._bullish
        elif direction == "bearish":
            return self._bearish
        raise ValueError(f"Invalid direction: {direction}")

    def upsert(
        self,
        entry_id: str,
        text: str,
        embedding: list[float],
        direction: str,
        pair: str,
        session_id: str,
        session_type: str,
        phase: str,
        signal_score: float,
        confidence: float,
        outcome: str | None = None,
        realized_pnl: float | None = None,
        close_reason: str | None = None,
    ) -> None:
        """方向別コレクションにドキュメントを追加する。"""
        metadata: dict = {
            "pair": pair,
            "session_id": session_id,
            "session_type": session_type,  # "trade" / "forecast" / "hold"
            "phase": phase,                # "entry" / "complete"
            "signal_score": signal_score,
            "confidence": confidence,
        }
        if outcome is not None:
            metadata["outcome"] = outcome
        if realized_pnl is not None:
            metadata["realized_pnl"] = realized_pnl
        if close_reason is not None:
            metadata["close_reason"] = close_reason

        col = self._collection(direction)
        col.upsert(
            ids=[entry_id],
            embeddings=[embedding],
            documents=[text],
            metadatas=[metadata],
        )
        logger.debug(f"Upserted {entry_id} to {direction} collection")

    def query(
        self,
        query_embedding: list[float],
        direction: str,
        top_k: int = 5,
        phase_filter: str | None = None,
    ) -> list[dict]:
        """方向別コレクションをベクトル検索する。"""
        col = self._collection(direction)
        if col.count() == 0:
            return []

        where = None
        if phase_filter:
            where = {"phase": {"$eq": phase_filter}}

        try:
            results = col.query(
                query_embeddings=[query_embedding],
                n_results=min(top_k, col.count()),
                where=where,
            )
        except Exception:
            return []

        entries = []
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]
        for i, doc in enumerate(docs):
            entries.append({
                "text": doc,
                "metadata": metas[i],
                "distance": distances[i] if i < len(distances) else None,
            })
        return entries

    def count(self, direction: str) -> int:
        return self._collection(direction).count()
```

- [ ] **Step 4: テスト実行 — パスを確認**

Run: `cd /home/teru/project/finance && python -m pytest tests/test_directional_store.py -v`
Expected: 3 passed

- [ ] **Step 5: コミット**

```bash
cd /home/teru/project/finance
git add src/rag/directional_store.py tests/test_directional_store.py
git commit -m "feat: add DirectionalStore for bullish/bearish ChromaDB collections"
```

---

### Task 3: RAGスコア補正ロジック

**Files:**
- Create: `src/signals/rag_adjustment.py`
- Create: `tests/test_rag_adjustment.py`

- [ ] **Step 1: テストファイル作成**

```python
# tests/test_rag_adjustment.py
from __future__ import annotations

import pytest

from src.signals.rag_adjustment import compute_rag_adjustment, RagAdjustmentConfig


def _make_hits(outcomes: list[str], distances: list[float]) -> list[dict]:
    """テスト用のRAG検索結果を生成する。"""
    return [
        {
            "text": f"trade {i}",
            "metadata": {
                "outcome": outcome,
                "session_type": "trade",
                "phase": "complete",
            },
            "distance": dist,
        }
        for i, (outcome, dist) in enumerate(zip(outcomes, distances))
    ]


def test_bullish_with_high_win_rate():
    """bullishシグナル + bullish側の勝率が高い → 上方補正。"""
    cfg = RagAdjustmentConfig()
    same_hits = _make_hits(["win", "win", "win", "loss"], [0.2, 0.3, 0.25, 0.4])
    opposite_hits = _make_hits(["win", "loss"], [0.8, 0.9])
    adj = compute_rag_adjustment(
        combined_score=0.30,
        same_direction_hits=same_hits,
        opposite_direction_hits=opposite_hits,
        config=cfg,
    )
    assert adj > 0  # positive adjustment


def test_bullish_with_low_win_rate():
    """bullishシグナル + bullish側の勝率が低い → 下方補正。"""
    cfg = RagAdjustmentConfig()
    same_hits = _make_hits(["loss", "loss", "loss", "win"], [0.2, 0.3, 0.25, 0.4])
    opposite_hits = _make_hits(["win"], [0.9])
    adj = compute_rag_adjustment(
        combined_score=0.30,
        same_direction_hits=same_hits,
        opposite_direction_hits=opposite_hits,
        config=cfg,
    )
    assert adj < 0  # negative adjustment


def test_high_opposite_similarity_penalizes():
    """対向コレクションの類似度が高い → 補正が負方向に。"""
    cfg = RagAdjustmentConfig()
    same_hits = _make_hits(["win", "win"], [0.3, 0.3])
    opposite_hits = _make_hits(["win", "win", "win"], [0.1, 0.1, 0.15])  # very similar
    adj = compute_rag_adjustment(
        combined_score=0.30,
        same_direction_hits=same_hits,
        opposite_direction_hits=opposite_hits,
        config=cfg,
    )
    # opposite similarity penalty should push adjustment down
    assert adj < cfg.same_direction_weight * (1.0 - 0.5)  # less than max same boost


def test_clamped_to_max():
    """補正値がmax_adjustmentにクランプされる。"""
    cfg = RagAdjustmentConfig(max_adjustment=0.05)
    same_hits = _make_hits(["win"] * 5, [0.1] * 5)
    opposite_hits = []
    adj = compute_rag_adjustment(
        combined_score=0.30,
        same_direction_hits=same_hits,
        opposite_direction_hits=opposite_hits,
        config=cfg,
    )
    assert abs(adj) <= 0.05


def test_insufficient_hits_returns_zero():
    """ヒット数がmin_hits未満 → 補正なし。"""
    cfg = RagAdjustmentConfig(min_hits=2)
    same_hits = _make_hits(["win"], [0.3])
    opposite_hits = []
    adj = compute_rag_adjustment(
        combined_score=0.30,
        same_direction_hits=same_hits,
        opposite_direction_hits=opposite_hits,
        config=cfg,
    )
    assert adj == 0.0


def test_bearish_signal_symmetric():
    """bearishシグナルでも対称的に動作する。"""
    cfg = RagAdjustmentConfig()
    same_hits = _make_hits(["win", "win", "win"], [0.2, 0.3, 0.25])
    opposite_hits = _make_hits(["loss"], [0.8])
    adj = compute_rag_adjustment(
        combined_score=-0.30,
        same_direction_hits=same_hits,
        opposite_direction_hits=opposite_hits,
        config=cfg,
    )
    # bearish with high win rate should push further negative (negative adj)
    assert adj < 0


def test_weight_multipliers():
    """session_typeによる重み付けが反映される。"""
    cfg = RagAdjustmentConfig(
        trade_weight_multiplier=1.0,
        forecast_weight_multiplier=0.5,
    )
    trade_hits = [
        {"text": "t", "metadata": {"outcome": "win", "session_type": "trade", "phase": "complete"}, "distance": 0.3},
        {"text": "t", "metadata": {"outcome": "win", "session_type": "trade", "phase": "complete"}, "distance": 0.3},
    ]
    forecast_hits = [
        {"text": "f", "metadata": {"outcome": "win", "session_type": "forecast", "phase": "complete"}, "distance": 0.3},
        {"text": "f", "metadata": {"outcome": "win", "session_type": "forecast", "phase": "complete"}, "distance": 0.3},
    ]
    adj_trade = compute_rag_adjustment(0.30, trade_hits, [], cfg)
    adj_forecast = compute_rag_adjustment(0.30, forecast_hits, [], cfg)
    assert abs(adj_trade) > abs(adj_forecast)
```

- [ ] **Step 2: テスト実行 — 失敗を確認**

Run: `cd /home/teru/project/finance && python -m pytest tests/test_rag_adjustment.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: rag_adjustment実装**

```python
# src/signals/rag_adjustment.py
"""方向別RAG検索結果からシグナルスコアの補正値を算出する。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class RagAdjustmentConfig:
    enabled: bool = True
    max_adjustment: float = 0.15
    min_hits: int = 2
    search_top_n: int = 5
    same_direction_weight: float = 0.10
    opposite_direction_weight: float = 0.10
    trade_weight_multiplier: float = 1.0
    forecast_weight_multiplier: float = 0.5
    hold_weight_multiplier: float = 0.3


def _session_type_weight(session_type: str, config: RagAdjustmentConfig) -> float:
    """session_typeに応じた重みを返す。"""
    weights = {
        "trade": config.trade_weight_multiplier,
        "forecast": config.forecast_weight_multiplier,
        "hold": config.hold_weight_multiplier,
    }
    return weights.get(session_type, 0.5)


def compute_rag_adjustment(
    combined_score: float,
    same_direction_hits: list[dict],
    opposite_direction_hits: list[dict],
    config: RagAdjustmentConfig,
) -> float:
    """方向別RAG検索結果からスコア補正値を算出する。

    Args:
        combined_score: 現在のcombined_score（正=bullish, 負=bearish）
        same_direction_hits: 同方向コレクションの検索結果
        opposite_direction_hits: 対向コレクションの検索結果
        config: 補正設定

    Returns:
        rag_adjustment: -max_adjustment 〜 +max_adjustment の補正値。
        combined_score が正の場合、正の補正値 = bullish強化、負 = bullish弱化。
        combined_score が負の場合、負の補正値 = bearish強化、正 = bearish弱化。
    """
    if not config.enabled:
        return 0.0

    # 有効ヒット数チェック（completeのみ）
    valid_same = [h for h in same_direction_hits if h.get("metadata", {}).get("phase") == "complete"]
    valid_opposite = [h for h in opposite_direction_hits if h.get("metadata", {}).get("phase") == "complete"]

    total_valid = len(valid_same) + len(valid_opposite)
    if total_valid < config.min_hits:
        return 0.0

    # 1. 同方向コレクション: 重み付き勝率 → 信頼度
    same_factor = 0.0
    if valid_same:
        weighted_wins = 0.0
        weighted_total = 0.0
        for h in valid_same:
            meta = h.get("metadata", {})
            w = _session_type_weight(meta.get("session_type", "trade"), config)
            weighted_total += w
            if meta.get("outcome") == "win":
                weighted_wins += w
        if weighted_total > 0:
            win_rate = weighted_wins / weighted_total
            same_factor = (win_rate - 0.5) * config.same_direction_weight

    # 2. 対向コレクション: 類似度が高いほど反転リスク
    opposite_factor = 0.0
    if valid_opposite:
        # ChromaDB distance: cosine distance (0=identical, 2=opposite)
        # similarity = 1 - distance
        similarities = []
        for h in valid_opposite:
            dist = h.get("distance")
            if dist is not None:
                similarities.append(max(0.0, 1.0 - dist))
        if similarities:
            avg_similarity = sum(similarities) / len(similarities)
            opposite_factor = -avg_similarity * config.opposite_direction_weight

    # 3. 合算
    adjustment = same_factor + opposite_factor

    # 4. bearishの場合は符号反転（bearish強化 = 負方向への補正）
    if combined_score < 0:
        adjustment = -adjustment

    # 5. クランプ
    adjustment = max(-config.max_adjustment, min(config.max_adjustment, adjustment))

    logger.info(
        f"RAG Adjustment: combined={combined_score:+.3f} adj={adjustment:+.4f} "
        f"(same: {len(valid_same)} hits, factor={same_factor:+.4f} | "
        f"opposite: {len(valid_opposite)} hits, factor={opposite_factor:+.4f})"
    )

    return adjustment
```

- [ ] **Step 4: テスト実行 — パスを確認**

Run: `cd /home/teru/project/finance && python -m pytest tests/test_rag_adjustment.py -v`
Expected: 8 passed

- [ ] **Step 5: コミット**

```bash
cd /home/teru/project/finance
git add src/signals/rag_adjustment.py tests/test_rag_adjustment.py
git commit -m "feat: add RAG adjustment logic for directional score correction"
```

---

### Task 4: 設定追加 — config.py と settings.yaml

**Files:**
- Modify: `src/config.py:95-114`
- Modify: `config/settings.yaml`

- [ ] **Step 1: RagAdjustmentConfig をconfigに統合**

`src/config.py` の `TradingConfig` クラス（95行目付近）に以下のフィールドを追加する:

```python
# src/config.py — TradingConfig に追加（profit_lock_score_floor の後に）
    # RAG方向別スコア補正
    rag_adjustment_enabled: bool = True
    rag_adjustment_max: float = 0.15
    rag_adjustment_min_hits: int = 2
    rag_adjustment_search_top_n: int = 5
    rag_adjustment_same_weight: float = 0.10
    rag_adjustment_opposite_weight: float = 0.10
    rag_adjustment_trade_multiplier: float = 1.0
    rag_adjustment_forecast_multiplier: float = 0.5
    rag_adjustment_hold_multiplier: float = 0.3
```

- [ ] **Step 2: settings.yaml に rag_adjustment セクション追加**

`config/settings.yaml` の `trading:` セクション末尾に追加:

```yaml
  # RAG方向別スコア補正
  rag_adjustment_enabled: true
  rag_adjustment_max: 0.15
  rag_adjustment_min_hits: 2
  rag_adjustment_search_top_n: 5
  rag_adjustment_same_weight: 0.10
  rag_adjustment_opposite_weight: 0.10
  rag_adjustment_trade_multiplier: 1.0
  rag_adjustment_forecast_multiplier: 0.5
  rag_adjustment_hold_multiplier: 0.3
```

- [ ] **Step 3: 設定の読み込みを確認**

Run: `cd /home/teru/project/finance && python -c "from src.config import load_config; c = load_config(); print(c.trading.rag_adjustment_enabled, c.trading.rag_adjustment_max)"`
Expected: `True 0.15`

- [ ] **Step 4: コミット**

```bash
cd /home/teru/project/finance
git add src/config.py config/settings.yaml
git commit -m "feat: add rag_adjustment config to TradingConfig and settings.yaml"
```

---

### Task 5: VectorStore に DirectionalStore を統合

**Files:**
- Modify: `src/rag/vector_store.py:16-36`

- [ ] **Step 1: VectorStore.__init__ に DirectionalStore を追加**

`src/rag/vector_store.py` の `VectorStore.__init__` を修正。既存の `_reflections` コレクションはそのまま残し（後方互換）、`DirectionalStore` を同じ `db_path` で初期化する:

```python
# src/rag/vector_store.py — 先頭のimportに追加
from src.rag.directional_store import DirectionalStore
```

```python
# src/rag/vector_store.py — VectorStore.__init__ の末尾（self._reflections の初期化後）に追加
        self.directional = DirectionalStore(db_path)
```

これにより `store.directional.upsert(...)` / `store.directional.query(...)` でアクセスできる。

- [ ] **Step 2: 動作確認**

Run: `cd /home/teru/project/finance && python -c "from pathlib import Path; from src.rag.vector_store import VectorStore; vs = VectorStore(Path('/tmp/test_vs')); print(type(vs.directional))"`
Expected: `<class 'src.rag.directional_store.DirectionalStore'>`

- [ ] **Step 3: コミット**

```bash
cd /home/teru/project/finance
git add src/rag/vector_store.py
git commit -m "feat: integrate DirectionalStore into VectorStore"
```

---

### Task 6: 取引サイクル統合 — 発注時のセッション作成

**Files:**
- Modify: `src/trading_cycle.py:452-478` (Phase 4b)

- [ ] **Step 1: trading_cycle 関数にSessionStoreとembed_fnの受け渡しを追加**

`src/trading_cycle.py` の `trading_cycle()` 関数シグネチャ（258行目）に `session_store` パラメータを追加:

```python
async def trading_cycle(
    config: AppConfig,
    position_mgr: PositionManager,
    store: VectorStore,
    price_store: PriceStore,
    analysis_store: AnalysisStore,
    hold_store: HoldDecisionStore,
    session_store: SessionStore | None = None,  # 追加
    price_provider: PriceProvider | None = None,
) -> None:
```

先頭のimportに追加:

```python
from src.data.session_store import SessionStore
from src.signals.rag_adjustment import compute_rag_adjustment, RagAdjustmentConfig
```

- [ ] **Step 2: Phase 4b — 発注時にセッション作成とRAG entry注入**

`src/trading_cycle.py` の Phase 4b（452行目付近）を修正。`order = broker.execute_signal(...)` の直後に以下を追加:

```python
            if order and session_store:
                direction = "bullish" if order.direction == "buy" else "bearish"
                # セッション作成
                session_store.create_session(
                    session_id=order.order_id,
                    pair=order.pair,
                    direction=direction,
                    entry_price=order.entry_price,
                    stop_loss=order.stop_loss,
                    take_profit=order.take_profit,
                    position_size=order.position_size,
                    signal_score=sig.combined_score,
                    signal_confidence=sig.confidence,
                    macro_context=macro_ctxs.get(sig.pair, ""),
                    analysis_summary=sig.detail_reason,
                    opened_at=order.opened_at,
                )
                # 方向別RAGにentry注入
                try:
                    entry_text = (
                        f"{order.pair} {direction} | score={sig.combined_score:+.3f} "
                        f"conf={sig.confidence:.2f} | entry={order.entry_price:.5f} "
                        f"SL={order.stop_loss:.5f} TP={order.take_profit:.5f} | "
                        f"{sig.detail_reason}"
                    )
                    embed_fn_local = partial(
                        embed_text,
                        ollama_base_url=config.llm.ollama.base_url,
                        model=config.rag.embedding_model,
                    )
                    embedding = await embed_fn_local(entry_text)
                    store.directional.upsert(
                        entry_id=f"{order.order_id}_entry",
                        text=entry_text,
                        embedding=embedding,
                        direction=direction,
                        pair=order.pair,
                        session_id=order.order_id,
                        session_type="trade",
                        phase="entry",
                        signal_score=sig.combined_score,
                        confidence=sig.confidence,
                    )
                except Exception as e:
                    logger.warning(f"[SESSION] RAG entry failed for {order.order_id}: {e}")
```

- [ ] **Step 3: コミット**

```bash
cd /home/teru/project/finance
git add src/trading_cycle.py
git commit -m "feat: create trading session and RAG entry on order execution"
```

---

### Task 7: 取引サイクル統合 — クローズ時のセッション更新

**Files:**
- Modify: `src/trading_cycle.py:312-341` (Phase 1.5)
- Modify: `src/trading_cycle.py:421-450` (Phase 4a close)

- [ ] **Step 1: Phase 1.5 — 決済時にセッション更新とRAG complete注入**

`src/trading_cycle.py` の Phase 1.5（`for closed_order in closed_this_run:` ループ内、319行目付近）で `store_reflection(...)` の後に以下を追加:

```python
                # セッション更新 + 方向別RAG complete注入
                if session_store:
                    direction = "bullish" if closed_order.direction == "buy" else "bearish"
                    session_store.close_session(
                        session_id=closed_order.order_id,
                        closed_at=closed_order.closed_at or datetime.now(),
                        close_price=closed_order.close_price or closed_order.entry_price,
                        close_reason=closed_order.close_reason or "manual",
                        realized_pnl=closed_order.realized_pnl or 0.0,
                        reflection_text=reflection.full_text if reflection else "",
                    )
                    try:
                        complete_text = (
                            f"{closed_order.pair} {direction} | "
                            f"score={closed_order.signal_reason} | "
                            f"entry={closed_order.entry_price:.5f} "
                            f"close={closed_order.close_price:.5f} | "
                            f"result={'win' if (closed_order.realized_pnl or 0) > 0 else 'loss'} "
                            f"pnl={closed_order.realized_pnl or 0:+.2f} | "
                            f"reason={closed_order.close_reason} | "
                            f"{reflection.full_text if reflection else ''}"
                        )
                        embedding = await embed_fn(complete_text)
                        store.directional.upsert(
                            entry_id=f"{closed_order.order_id}_complete",
                            text=complete_text,
                            embedding=embedding,
                            direction=direction,
                            pair=closed_order.pair,
                            session_id=closed_order.order_id,
                            session_type="trade",
                            phase="complete",
                            signal_score=0.0,  # signal_reason からパース可能だが簡略化
                            confidence=0.0,
                            outcome="win" if (closed_order.realized_pnl or 0) > 0 else "loss",
                            realized_pnl=closed_order.realized_pnl or 0.0,
                            close_reason=closed_order.close_reason,
                        )
                    except Exception as e:
                        logger.warning(f"[SESSION] RAG complete failed for {closed_order.order_id}: {e}")
```

- [ ] **Step 2: Phase 4a — レビュークローズ時も同様に追加**

Phase 4a の `for closed_order in reviewed_closed:` ループ（428行目付近）の `store_reflection(...)` の後にも、Step 1 と同じセッション更新 + RAG complete注入コードを追加する。

- [ ] **Step 3: コミット**

```bash
cd /home/teru/project/finance
git add src/trading_cycle.py
git commit -m "feat: update trading session and inject RAG complete on position close"
```

---

### Task 8: RAGスコア補正をシグナル結合に統合

**Files:**
- Modify: `src/trading_cycle.py:452-478` (Phase 4b)

- [ ] **Step 1: Phase 4b の発注判断前にRAG補正を挿入**

`src/trading_cycle.py` の Phase 4b（`for sig in signals:` ループ、454行目付近）で、`if sig.action != "hold":` の前にRAG補正ロジックを追加:

```python
    # Phase 4b: 新規シグナル実行（RAG方向別補正付き）
    embed_fn_adj = partial(
        embed_text,
        ollama_base_url=config.llm.ollama.base_url,
        model=config.rag.embedding_model,
    )
    rag_cfg = RagAdjustmentConfig(
        enabled=config.trading.rag_adjustment_enabled,
        max_adjustment=config.trading.rag_adjustment_max,
        min_hits=config.trading.rag_adjustment_min_hits,
        search_top_n=config.trading.rag_adjustment_search_top_n,
        same_direction_weight=config.trading.rag_adjustment_same_weight,
        opposite_direction_weight=config.trading.rag_adjustment_opposite_weight,
        trade_weight_multiplier=config.trading.rag_adjustment_trade_multiplier,
        forecast_weight_multiplier=config.trading.rag_adjustment_forecast_multiplier,
        hold_weight_multiplier=config.trading.rag_adjustment_hold_multiplier,
    )

    executed_orders = []
    for sig in signals:
        # RAG方向別スコア補正
        adjusted_score = sig.combined_score
        if rag_cfg.enabled and sig.action != "hold":
            try:
                query_text = sig.detail_reason
                query_embedding = await embed_fn_adj(query_text)
                same_dir = "bullish" if sig.combined_score > 0 else "bearish"
                opposite_dir = "bearish" if sig.combined_score > 0 else "bullish"
                same_hits = store.directional.query(
                    query_embedding=query_embedding,
                    direction=same_dir,
                    top_k=rag_cfg.search_top_n,
                    phase_filter="complete",
                )
                opposite_hits = store.directional.query(
                    query_embedding=query_embedding,
                    direction=opposite_dir,
                    top_k=rag_cfg.search_top_n,
                    phase_filter="complete",
                )
                adjustment = compute_rag_adjustment(
                    combined_score=sig.combined_score,
                    same_direction_hits=same_hits,
                    opposite_direction_hits=opposite_hits,
                    config=rag_cfg,
                )
                adjusted_score = sig.combined_score + adjustment
                logger.info(
                    f"[RAG ADJ] {sig.pair}: combined={sig.combined_score:+.3f} "
                    f"→ adjusted={adjusted_score:+.3f}"
                )
            except Exception as e:
                logger.warning(f"[RAG ADJ] {sig.pair}: failed — {e}")

        # 補正後スコアでaction再判定
        if adjusted_score != sig.combined_score:
            deadband = config.trading.signal_deadband
            if adjusted_score > deadband:
                sig.action = "buy"
            elif adjusted_score < -deadband:
                sig.action = "sell"
            else:
                sig.action = "hold"
            sig.combined_score = round(adjusted_score, 4)

        if sig.action != "hold":
            # ... 既存の発注ロジック（変更なし）
```

- [ ] **Step 2: 動作確認 — ログでRAG補正が出力されることを確認**

Run: `cd /home/teru/project/finance && python main.py run trade` (手動確認)
Expected: ログに `[RAG ADJ]` が出力される（データ不足時は `min_hits` 未達で補正0）

- [ ] **Step 3: コミット**

```bash
cd /home/teru/project/finance
git add src/trading_cycle.py
git commit -m "feat: integrate RAG directional adjustment into signal execution"
```

---

### Task 9: 予測サイクルへの方向別RAG統合

**Files:**
- Modify: `src/trading_cycle.py:643-733` (forecast_cycle)

- [ ] **Step 1: forecast_cycle Phase 1 — 検証結果を方向別に蓄積**

`src/trading_cycle.py` の `forecast_cycle()` 内、Phase 1 の `if has_significant:` ブロック（702行目付近）を修正。既存の `store.upsert_reflection(...)` に加えて、方向別コレクションにも蓄積する:

```python
                if has_significant:
                    embedding = await embed_fn(summary_text)
                    # 既存: 統合コレクションにもupsert（後方互換）
                    store.upsert_reflection(
                        entry_id=f"forecast_summary_{pair_cfg.symbol}_{now.strftime('%Y-%m-%d')}",
                        text=summary_text,
                        embedding=embedding,
                        pair=pair_cfg.symbol,
                        cycle_time=review_ts,
                        action=recent_forecasts[-1].predicted_direction,
                        outcome_summary=summary_text,
                        lesson=lesson,
                    )
                    # 新規: 方向別に個別予測をcomplete蓄積
                    for fc in recent_forecasts:
                        _, fc_lesson, fc_significant = build_forecast_review(
                            pair=pair_cfg.symbol,
                            forecast=fc,
                            current_price=current_price,
                            review_ts=review_ts,
                            significance_atr_ratio=config.analysis.forecast_significance_atr_ratio,
                        )
                        if not fc_significant:
                            continue
                        fc_delta = current_price - fc.current_price
                        fc_actual = "bullish" if fc_delta > 0 else "bearish"
                        fc_correct = fc.predicted_direction == fc_actual
                        fc_direction = fc.predicted_direction
                        if fc_direction not in ("bullish", "bearish"):
                            continue
                        try:
                            fc_text = (
                                f"{pair_cfg.symbol} forecast {fc_direction} | "
                                f"score={fc.combined_score:+.3f} conf={fc.confidence:.2f} | "
                                f"predicted={fc_direction} actual={fc_actual} | "
                                f"{'correct' if fc_correct else 'incorrect'} | "
                                f"{fc_lesson}"
                            )
                            fc_embedding = await embed_fn(fc_text)
                            store.directional.upsert(
                                entry_id=f"forecast_{pair_cfg.symbol}_{fc.id}_complete",
                                text=fc_text,
                                embedding=fc_embedding,
                                direction=fc_direction,
                                pair=pair_cfg.symbol,
                                session_id=f"forecast_{fc.id}",
                                session_type="forecast",
                                phase="complete",
                                signal_score=fc.combined_score,
                                confidence=fc.confidence,
                                outcome="correct" if fc_correct else "incorrect",
                            )
                        except Exception as e:
                            logger.warning(f"[FORECAST/DIR] {pair_cfg.symbol}: {e}")
```

- [ ] **Step 2: forecast_cycle Phase 2 — 新規予測のentry蓄積**

Phase 2 の `forecast_store.save_forecast(...)` の後（727行目付近）に追加:

```python
            # 方向別RAGにentry注入
            if signal.predicted_direction in ("bullish", "bearish"):
                try:
                    entry_text = (
                        f"{pair_cfg.symbol} forecast {signal.predicted_direction} | "
                        f"score={signal.combined_score:+.3f} conf={signal.confidence:.2f} | "
                        f"price={signal.entry_price:.5f} | {signal.signal_reason}"
                    )
                    entry_embedding = await embed_fn(entry_text)
                    store.directional.upsert(
                        entry_id=f"forecast_{pair_cfg.symbol}_{now.strftime('%Y%m%d%H%M')}_entry",
                        text=entry_text,
                        embedding=entry_embedding,
                        direction=signal.predicted_direction,
                        pair=pair_cfg.symbol,
                        session_id=f"forecast_{pair_cfg.symbol}_{now.strftime('%Y%m%d%H%M')}",
                        session_type="forecast",
                        phase="entry",
                        signal_score=signal.combined_score,
                        confidence=signal.confidence,
                    )
                except Exception as e:
                    logger.warning(f"[FORECAST/DIR] entry upsert failed: {e}")
```

- [ ] **Step 3: コミット**

```bash
cd /home/teru/project/finance
git add src/trading_cycle.py
git commit -m "feat: integrate forecast cycle with directional RAG collections"
```

---

### Task 10: HOLD判断レビューの方向別RAG統合

**Files:**
- Modify: `src/trading_cycle.py:205-255` (_review_hold_decisions)

- [ ] **Step 1: _review_hold_decisions に方向別蓄積を追加**

`src/trading_cycle.py` の `_review_hold_decisions()` 内、`if worth_storing:` ブロック（237行目付近）に方向別蓄積を追加:

```python
            if worth_storing:
                embedding = await embed_fn(review_text)
                # 既存: 統合コレクション（後方互換）
                store.upsert_reflection(
                    entry_id=f"hold_{hold.pair}_{hold.id}",
                    text=review_text,
                    embedding=embedding,
                    pair=hold.pair,
                    cycle_time=datetime.now(),
                    action=hold.predicted_direction,
                    outcome_summary=review_text,
                    lesson=lesson,
                )
                # 新規: 方向別コレクション
                hold_direction = hold.predicted_direction
                # long/short/bullish/bearish の正規化
                if hold_direction in ("long", "bullish"):
                    hold_direction = "bullish"
                elif hold_direction in ("short", "bearish"):
                    hold_direction = "bearish"
                else:
                    hold_direction = "bullish" if hold.signal_score > 0 else "bearish"

                try:
                    store.directional.upsert(
                        entry_id=f"hold_{hold.pair}_{hold.id}_complete",
                        text=review_text,
                        embedding=embedding,
                        direction=hold_direction,
                        pair=hold.pair,
                        session_id=f"hold_{hold.id}",
                        session_type="hold",
                        phase="complete",
                        signal_score=hold.signal_score,
                        confidence=hold.confidence,
                        outcome="correct" if "CORRECT" in lesson else "incorrect",
                    )
                except Exception as e:
                    logger.warning(f"[HOLD/DIR] {hold.pair}: {e}")
```

- [ ] **Step 2: コミット**

```bash
cd /home/teru/project/finance
git add src/trading_cycle.py
git commit -m "feat: integrate hold decision review with directional RAG"
```

---

### Task 11: 呼び出し元の更新 — SessionStore の初期化と注入

**Files:**
- Modify: `src/trading_cycle.py` (run_trading_cycle ヘルパー)
- Modify: `main.py` (if SessionStore is instantiated there)

- [ ] **Step 1: SessionStoreの初期化箇所を特定して追加**

`src/trading_cycle.py` の `run_trading_cycle()` (505行目付近) で SessionStore を初期化し、`trading_cycle()` に渡す:

```python
def run_trading_cycle(
    config: AppConfig,
    store: VectorStore,
    ...
) -> None:
    ...
    session_store = SessionStore(config.prices_db_path)
    asyncio.run(trading_cycle(
        config, position_mgr, store, price_store, analysis_store, hold_store,
        session_store=session_store,
        price_provider=price_provider,
    ))
```

同様に `main.py` から `trading_cycle` を直接呼ぶ箇所があれば更新する。

- [ ] **Step 2: 動作確認**

Run: `cd /home/teru/project/finance && python -c "from src.config import load_config; from src.data.session_store import SessionStore; c = load_config(); s = SessionStore(c.prices_db_path); print('OK')"`
Expected: `OK`

- [ ] **Step 3: コミット**

```bash
cd /home/teru/project/finance
git add src/trading_cycle.py main.py
git commit -m "feat: initialize and inject SessionStore into trading cycle"
```

---

### Task 12: 既存データ移行スクリプト

**Files:**
- Create: `scripts/migrate_directional_rag.py`

- [ ] **Step 1: 移行スクリプト作成**

```python
# scripts/migrate_directional_rag.py
"""既存の trades.json と fx_reflections を方向別コレクションに移行する。

冪等性あり: 再実行しても重複しない（upsert使用）。

Usage:
    cd /home/teru/project/finance
    python scripts/migrate_directional_rag.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from datetime import datetime
from functools import partial
from pathlib import Path

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.data.session_store import SessionStore
from src.llm.embedder import embed_text
from src.rag.vector_store import VectorStore

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _parse_signal_reason(reason: str) -> tuple[float, float]:
    """signal_reason 文字列から score と confidence をパースする。"""
    score, conf = 0.0, 0.0
    m_score = re.search(r"score=([+\-]?\d+\.?\d*)", reason)
    m_conf = re.search(r"conf=(\d+\.?\d*)", reason)
    if m_score:
        score = float(m_score.group(1))
    if m_conf:
        conf = float(m_conf.group(1))
    return score, conf


async def main():
    config = load_config()
    store = VectorStore(config.rag_db_path)
    session_store = SessionStore(config.prices_db_path)

    embed_fn = partial(
        embed_text,
        ollama_base_url=config.llm.ollama.base_url,
        model=config.rag.embedding_model,
    )

    # Step 1: trades.json → trading_sessions + 方向別 ChromaDB
    trades_path = config.state_dir / "trades.json"
    if not trades_path.exists():
        logger.warning(f"trades.json not found at {trades_path}")
        return

    with open(trades_path, encoding="utf-8") as f:
        trades = json.load(f)

    logger.info(f"Migrating {len(trades)} trades...")

    bullish_count = 0
    bearish_count = 0

    for trade in trades:
        session_id = trade["order_id"]
        direction = "bullish" if trade["direction"] == "buy" else "bearish"
        score, conf = _parse_signal_reason(trade.get("signal_reason", ""))
        realized_pnl = trade.get("realized_pnl", 0.0)
        outcome = "win" if realized_pnl > 0 else "loss"

        # SessionStore に INSERT（既存なら skip）
        existing = session_store.get_session(session_id)
        if existing is None:
            session_store.create_session(
                session_id=session_id,
                pair=trade["pair"],
                direction=direction,
                entry_price=trade["entry_price"],
                stop_loss=trade.get("stop_loss", 0.0),
                take_profit=trade.get("take_profit", 0.0),
                position_size=trade.get("position_size", 0.0),
                signal_score=score,
                signal_confidence=conf,
                macro_context=trade.get("macro_context_at_entry", ""),
                analysis_summary=trade.get("signal_reason", ""),
                opened_at=datetime.fromisoformat(trade["opened_at"]),
            )
            if trade.get("closed_at"):
                session_store.close_session(
                    session_id=session_id,
                    closed_at=datetime.fromisoformat(trade["closed_at"]),
                    close_price=trade.get("close_price", trade["entry_price"]),
                    close_reason=trade.get("close_reason", "manual"),
                    realized_pnl=realized_pnl,
                )

        # 方向別 ChromaDB に complete ドキュメント生成
        macro_summary = trade.get("macro_context_at_entry", "")
        if macro_summary and len(macro_summary) > 200:
            macro_summary = macro_summary[:200] + "..."

        complete_text = (
            f"{trade['pair']} {direction} | score={score:+.3f} conf={conf:.2f} | "
            f"entry={trade['entry_price']:.5f} close={trade.get('close_price', 0):.5f} | "
            f"result={outcome} pnl={realized_pnl:+.2f} | "
            f"reason={trade.get('close_reason', 'unknown')} | "
            f"{macro_summary}"
        )

        embedding = await embed_fn(complete_text)
        store.directional.upsert(
            entry_id=f"{session_id}_complete",
            text=complete_text,
            embedding=embedding,
            direction=direction,
            pair=trade["pair"],
            session_id=session_id,
            session_type="trade",
            phase="complete",
            signal_score=score,
            confidence=conf,
            outcome=outcome,
            realized_pnl=realized_pnl,
            close_reason=trade.get("close_reason"),
        )

        if direction == "bullish":
            bullish_count += 1
        else:
            bearish_count += 1

    # Step 2: 既存 fx_reflections の処理
    legacy_count = 0
    try:
        legacy_col = store._reflections
        all_entries = legacy_col.get(include=["documents", "metadatas", "embeddings"])
        ids = all_entries.get("ids", [])
        docs = all_entries.get("documents", [])
        metas = all_entries.get("metadatas", [])
        embeddings = all_entries.get("embeddings", [])

        for i, doc_id in enumerate(ids):
            meta = metas[i] if i < len(metas) else {}
            action = meta.get("action", "")

            # 方向判定
            if action in ("bullish", "long", "buy"):
                direction = "bullish"
            elif action in ("bearish", "short", "sell"):
                direction = "bearish"
            else:
                # テキスト内容から推定
                doc_text = docs[i] if i < len(docs) else ""
                if "bullish" in doc_text.lower() or "buy" in doc_text.lower():
                    direction = "bullish"
                elif "bearish" in doc_text.lower() or "sell" in doc_text.lower():
                    direction = "bearish"
                else:
                    logger.debug(f"Skipping undetermined direction: {doc_id}")
                    continue

            emb = embeddings[i] if i < len(embeddings) else None
            if emb is None:
                continue

            store.directional.upsert(
                entry_id=f"legacy_{doc_id}",
                text=docs[i],
                embedding=emb,
                direction=direction,
                pair=meta.get("pair", "unknown"),
                session_id=f"legacy_{doc_id}",
                session_type="trade",
                phase="complete",
                signal_score=0.0,
                confidence=0.0,
            )
            legacy_count += 1
    except Exception as e:
        logger.warning(f"Legacy migration error: {e}")

    # Step 3: 検証
    logger.info("=== Migration Summary ===")
    logger.info(f"Trades migrated: {len(trades)}")
    logger.info(f"  bullish: {bullish_count}")
    logger.info(f"  bearish: {bearish_count}")
    logger.info(f"Legacy reflections migrated: {legacy_count}")
    logger.info(f"DirectionalStore bullish count: {store.directional.count('bullish')}")
    logger.info(f"DirectionalStore bearish count: {store.directional.count('bearish')}")

    # サンプル検索テスト
    test_emb = await embed_fn("EURUSD sell bearish")
    test_results = store.directional.query(test_emb, "bearish", top_k=1)
    if test_results:
        logger.info(f"Sample search OK: {test_results[0]['metadata'].get('session_id')}")
    else:
        logger.info("Sample search: no results (collection may be empty)")

    logger.info("=== Migration complete ===")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: 移行スクリプト実行**

Run: `cd /home/teru/project/finance && python scripts/migrate_directional_rag.py`
Expected: 16 trades migrated, bullish: 4, bearish: 12, legacy reflections migrated

- [ ] **Step 3: コミット**

```bash
cd /home/teru/project/finance
git add scripts/migrate_directional_rag.py
git commit -m "feat: add migration script for directional RAG data"
```

---

### Task 13: 統合テスト

**Files:**
- Create: `tests/test_integration_directional.py`

- [ ] **Step 1: 統合テスト作成**

```python
# tests/test_integration_directional.py
"""方向別RAGの統合テスト: SessionStore + DirectionalStore + RAG補正の連携確認。"""

from __future__ import annotations

from datetime import datetime

import pytest

from src.data.session_store import SessionStore
from src.rag.directional_store import DirectionalStore
from src.signals.rag_adjustment import compute_rag_adjustment, RagAdjustmentConfig


def _dummy_embedding(dim: int = 768) -> list[float]:
    return [0.1] * dim


@pytest.fixture
def session_store(tmp_path):
    return SessionStore(tmp_path / "test.db")


@pytest.fixture
def directional_store(tmp_path):
    return DirectionalStore(tmp_path / "test_rag")


def test_full_trade_lifecycle(session_store, directional_store):
    """発注 → セッション作成 → RAG entry → クローズ → RAG complete → 補正参照。"""
    sid = "integration-001"

    # 1. セッション作成
    session_store.create_session(
        session_id=sid,
        pair="EURUSD=X",
        direction="bearish",
        entry_price=1.15,
        stop_loss=1.16,
        take_profit=1.13,
        position_size=1000.0,
        signal_score=-0.35,
        signal_confidence=0.78,
        macro_context="DXY strong",
        analysis_summary="Strong bearish signal",
        opened_at=datetime(2026, 4, 1, 9, 30),
    )

    # 2. RAG entry注入
    directional_store.upsert(
        entry_id=f"{sid}_entry",
        text="EURUSD bearish strong momentum DXY",
        embedding=_dummy_embedding(),
        direction="bearish",
        pair="EURUSD=X",
        session_id=sid,
        session_type="trade",
        phase="entry",
        signal_score=-0.35,
        confidence=0.78,
    )

    # 3. クローズ
    session_store.close_session(
        session_id=sid,
        closed_at=datetime(2026, 4, 2, 10, 0),
        close_price=1.13,
        close_reason="take_profit",
        realized_pnl=20.0,
        reflection_text="Trade followed through as expected",
    )

    # 4. RAG complete注入
    directional_store.upsert(
        entry_id=f"{sid}_complete",
        text="EURUSD bearish win take_profit pnl=+20.0",
        embedding=_dummy_embedding(),
        direction="bearish",
        pair="EURUSD=X",
        session_id=sid,
        session_type="trade",
        phase="complete",
        signal_score=-0.35,
        confidence=0.78,
        outcome="win",
        realized_pnl=20.0,
        close_reason="take_profit",
    )

    # 5. 検証
    session = session_store.get_session(sid)
    assert session.outcome == "win"

    hits = directional_store.query(_dummy_embedding(), "bearish", top_k=5, phase_filter="complete")
    assert len(hits) == 1
    assert hits[0]["metadata"]["outcome"] == "win"


def test_rag_adjustment_with_real_data(directional_store):
    """DirectionalStoreのデータを使って補正値が算出できることを確認。"""
    # bearish wins を蓄積
    for i in range(3):
        directional_store.upsert(
            entry_id=f"bear-{i}_complete",
            text=f"EURUSD bearish win trade {i}",
            embedding=_dummy_embedding(),
            direction="bearish",
            pair="EURUSD=X",
            session_id=f"bear-{i}",
            session_type="trade",
            phase="complete",
            signal_score=-0.30,
            confidence=0.75,
            outcome="win",
            realized_pnl=10.0,
        )

    # bearishシグナルで補正を計算
    same_hits = directional_store.query(_dummy_embedding(), "bearish", top_k=5, phase_filter="complete")
    opposite_hits = directional_store.query(_dummy_embedding(), "bullish", top_k=5, phase_filter="complete")

    cfg = RagAdjustmentConfig()
    adj = compute_rag_adjustment(
        combined_score=-0.30,
        same_direction_hits=same_hits,
        opposite_direction_hits=opposite_hits,
        config=cfg,
    )
    # bearish方向のwin rateが高い → bearish強化（負方向の補正）
    assert adj < 0
```

- [ ] **Step 2: テスト実行**

Run: `cd /home/teru/project/finance && python -m pytest tests/test_integration_directional.py -v`
Expected: 2 passed

- [ ] **Step 3: コミット**

```bash
cd /home/teru/project/finance
git add tests/test_integration_directional.py
git commit -m "test: add integration tests for directional RAG system"
```

---

### Task 14: 全テスト実行と最終確認

- [ ] **Step 1: 全テスト実行**

Run: `cd /home/teru/project/finance && python -m pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 2: 移行スクリプト実行（本番データ）**

Run: `cd /home/teru/project/finance && python scripts/migrate_directional_rag.py`
Expected: 16 trades migrated successfully

- [ ] **Step 3: 設定確認**

Run: `cd /home/teru/project/finance && python -c "from src.config import load_config; c = load_config(); print('rag_adj:', c.trading.rag_adjustment_enabled, c.trading.rag_adjustment_max)"`
Expected: `rag_adj: True 0.15`

- [ ] **Step 4: 最終コミット**

```bash
cd /home/teru/project/finance
git add -A
git commit -m "feat: complete directional RAG system with migration and tests"
```
