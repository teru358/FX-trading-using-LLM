# askコマンド セマンティック検索強化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** askコマンドに全データソース横断のセマンティック検索を導入し、質問内容に関連する過去データ（取引結果・予測的中率・方向別RAG・振り返り）を含む詳細な回答を可能にする。

**Architecture:** 新規 `AskContextBuilder` クラスがユーザーの質問をベクトル化し、全ChromaDBコレクション（fx_news, fx_reflections, fx_reflections_bullish, fx_reflections_bearish）に並列セマンティック検索を実行。SQLiteから取引実績・予測的中率を集計。全結果をJinja2テンプレートの各セクションに注入する。

**Tech Stack:** Python 3.12, ChromaDB, SQLAlchemy, Jinja2, asyncio, pytest

---

## File Structure

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `src/rag/ask_context_builder.py` | 通貨ペア抽出、セマンティック検索、コンテキスト構築を一元管理 |
| Create | `tests/test_ask_context_builder.py` | AskContextBuilderのユニットテスト |
| Modify | `prompts/ask_system.txt` | 拡張したシステムプロンプト |
| Modify | `prompts/ask_user.j2` | セクション分けしたテンプレート |
| Modify | `src/trading_cycle.py:1215-1289` | `_build_ask_context` → `AskContextBuilder` に差し替え |

---

### Task 1: 通貨ペア抽出ユーティリティ

**Files:**
- Create: `src/rag/ask_context_builder.py`
- Create: `tests/test_ask_context_builder.py`

- [ ] **Step 1: テスト作成 — 通貨ペア抽出**

```python
# tests/test_ask_context_builder.py
from __future__ import annotations

import pytest

from src.rag.ask_context_builder import extract_pairs


def _make_instruments():
    """テスト用のInstrumentConfig風オブジェクトを返す。"""
    from dataclasses import dataclass

    @dataclass
    class FakeInstrument:
        symbol: str
        display_name: str

    return [
        FakeInstrument(symbol="USDJPY=X", display_name="USD/JPY"),
        FakeInstrument(symbol="EURUSD=X", display_name="EUR/USD"),
        FakeInstrument(symbol="GBPUSD=X", display_name="GBP/USD"),
    ]


def test_extract_pair_english():
    instruments = _make_instruments()
    assert extract_pairs("What about EURUSD?", instruments) == ["EURUSD=X"]


def test_extract_pair_slash():
    instruments = _make_instruments()
    assert extract_pairs("USD/JPY の見通しは？", instruments) == ["USDJPY=X"]


def test_extract_pair_japanese_dollar_yen():
    instruments = _make_instruments()
    assert extract_pairs("ドル円は上がる？", instruments) == ["USDJPY=X"]


def test_extract_pair_japanese_euro_dollar():
    instruments = _make_instruments()
    assert extract_pairs("ユーロドルについて", instruments) == ["EURUSD=X"]


def test_extract_multiple_pairs():
    instruments = _make_instruments()
    result = extract_pairs("EURUSDとUSDJPYの比較", instruments)
    assert "EURUSD=X" in result
    assert "USDJPY=X" in result


def test_extract_no_pair():
    instruments = _make_instruments()
    assert extract_pairs("今の相場はどう？", instruments) == []


def test_extract_pair_case_insensitive():
    instruments = _make_instruments()
    assert extract_pairs("eurusd is interesting", instruments) == ["EURUSD=X"]
```

- [ ] **Step 2: テスト実行 — 失敗を確認**

Run: `cd /home/teru/project/finance && uv run python -m pytest tests/test_ask_context_builder.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: extract_pairs 実装**

```python
# src/rag/ask_context_builder.py
"""askコマンド用のコンテキスト構築。

ユーザーの質問からペアを抽出し、全データソースに対して
セマンティック検索を実行してコンテキストを構築する。
"""

from __future__ import annotations

import asyncio
import logging
import re
from functools import partial

logger = logging.getLogger(__name__)

# 日本語の通貨ペア名 → symbol マッピング
_JAPANESE_PAIR_MAP: dict[str, str] = {
    "ドル円": "USDJPY=X",
    "ユーロドル": "EURUSD=X",
    "ポンドドル": "GBPUSD=X",
    "ユーロ円": "EURJPY=X",
    "ポンド円": "GBPJPY=X",
}


def extract_pairs(message: str, instruments: list) -> list[str]:
    """質問テキストから通貨ペアシンボルを抽出する。

    日本語名、スラッシュ付き、生のペア名に対応。
    見つからなければ空リストを返す（全ペア横断検索を意味する）。
    """
    found: list[str] = []
    msg_lower = message.lower()

    # 1. 日本語ペア名
    for jp_name, symbol in _JAPANESE_PAIR_MAP.items():
        if jp_name in message:
            if symbol not in found:
                found.append(symbol)

    # 2. instrument の display_name と symbol からパターン生成
    for inst in instruments:
        # display_name: "USD/JPY" → "usdjpy", "usd/jpy"
        dn = inst.display_name
        dn_noslash = dn.replace("/", "").lower()
        dn_lower = dn.lower()
        # symbol: "USDJPY=X" → "usdjpy"
        sym_base = inst.symbol.replace("=X", "").lower()

        if dn_noslash in msg_lower or dn_lower in msg_lower or sym_base in msg_lower:
            if inst.symbol not in found:
                found.append(inst.symbol)

    return found
```

- [ ] **Step 4: テスト実行 — パスを確認**

Run: `cd /home/teru/project/finance && uv run python -m pytest tests/test_ask_context_builder.py -v`
Expected: 7 passed

- [ ] **Step 5: コミット**

```bash
cd /home/teru/project/finance
git add src/rag/ask_context_builder.py tests/test_ask_context_builder.py
git commit -m "feat: add extract_pairs utility for ask command pair detection"
```

---

### Task 2: セマンティック検索メソッド

**Files:**
- Modify: `src/rag/ask_context_builder.py`
- Modify: `tests/test_ask_context_builder.py`

- [ ] **Step 1: テスト追加 — セマンティック検索結果のマージとソート**

```python
# tests/test_ask_context_builder.py に追加

from src.rag.ask_context_builder import merge_and_rank_results


def test_merge_and_rank_by_distance():
    """距離が近い順にソートされる。"""
    results = [
        {"text": "a", "metadata": {"pair": "EURUSD=X"}, "distance": 0.5, "source": "news"},
        {"text": "b", "metadata": {"pair": "EURUSD=X"}, "distance": 0.1, "source": "bullish"},
        {"text": "c", "metadata": {"pair": "EURUSD=X"}, "distance": 0.3, "source": "reflections"},
    ]
    ranked = merge_and_rank_results(results, max_results=10)
    assert ranked[0]["text"] == "b"
    assert ranked[1]["text"] == "c"
    assert ranked[2]["text"] == "a"


def test_merge_and_rank_limits():
    """max_results で件数制限される。"""
    results = [
        {"text": f"item-{i}", "metadata": {}, "distance": 0.1 * i, "source": "news"}
        for i in range(20)
    ]
    ranked = merge_and_rank_results(results, max_results=5)
    assert len(ranked) == 5
```

- [ ] **Step 2: テスト実行 — 失敗を確認**

Run: `cd /home/teru/project/finance && uv run python -m pytest tests/test_ask_context_builder.py::test_merge_and_rank_by_distance -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: merge_and_rank_results 実装**

`src/rag/ask_context_builder.py` に追加:

```python
def merge_and_rank_results(
    results: list[dict],
    max_results: int = 10,
) -> list[dict]:
    """複数コレクションからの検索結果をマージし、距離順にソートする。

    Args:
        results: 各コレクションの検索結果リスト。各要素は
                 {"text", "metadata", "distance", "source"} を持つ。
        max_results: 返す最大件数。

    Returns:
        距離が近い順にソートされた上位N件。
    """
    results.sort(key=lambda r: r.get("distance", float("inf")))
    return results[:max_results]
```

- [ ] **Step 4: テスト実行 — パスを確認**

Run: `cd /home/teru/project/finance && uv run python -m pytest tests/test_ask_context_builder.py -v`
Expected: 9 passed

- [ ] **Step 5: コミット**

```bash
cd /home/teru/project/finance
git add src/rag/ask_context_builder.py tests/test_ask_context_builder.py
git commit -m "feat: add merge_and_rank_results for multi-collection search"
```

---

### Task 3: 取引実績サマリー生成

**Files:**
- Modify: `src/rag/ask_context_builder.py`
- Modify: `tests/test_ask_context_builder.py`

- [ ] **Step 1: テスト追加 — 取引実績サマリー**

```python
# tests/test_ask_context_builder.py に追加

from src.rag.ask_context_builder import build_trade_summary


def _make_sessions(outcomes: list[tuple[str, float, str]]):
    """(pair, pnl, close_reason) からダミーセッションを生成。"""
    from dataclasses import dataclass
    from datetime import datetime

    @dataclass
    class FakeSession:
        pair: str
        realized_pnl: float
        outcome: str
        close_reason: str

    return [
        FakeSession(
            pair=pair,
            realized_pnl=pnl,
            outcome="win" if pnl > 0 else "loss",
            close_reason=reason,
        )
        for pair, pnl, reason in outcomes
    ]


def test_trade_summary_single_pair():
    sessions = _make_sessions([
        ("EURUSD=X", 10.0, "take_profit"),
        ("EURUSD=X", -5.0, "stop_loss"),
        ("EURUSD=X", 8.0, "take_profit"),
    ])
    result = build_trade_summary(sessions, pairs=["EURUSD=X"])
    assert "EURUSD=X" in result
    assert "Win: 2" in result
    assert "Loss: 1" in result


def test_trade_summary_no_pair_gives_total():
    sessions = _make_sessions([
        ("EURUSD=X", 10.0, "take_profit"),
        ("USDJPY=X", -5.0, "stop_loss"),
    ])
    result = build_trade_summary(sessions, pairs=[])
    assert "Overall" in result
    assert "Total: 2" in result


def test_trade_summary_empty():
    result = build_trade_summary([], pairs=["EURUSD=X"])
    assert "No trade history" in result
```

- [ ] **Step 2: テスト実行 — 失敗を確認**

Run: `cd /home/teru/project/finance && uv run python -m pytest tests/test_ask_context_builder.py::test_trade_summary_single_pair -v`
Expected: FAIL

- [ ] **Step 3: build_trade_summary 実装**

`src/rag/ask_context_builder.py` に追加:

```python
def build_trade_summary(sessions: list, pairs: list[str]) -> str:
    """取引セッションリストからサマリーテキストを生成する。

    Args:
        sessions: _TradingSession オブジェクトのリスト（closed のみ）。
        pairs: 対象ペアのリスト。空の場合は全体集計。

    Returns:
        プロンプトに注入するサマリーテキスト。
    """
    if not sessions:
        return "=== Trade History ===\nNo trade history available."

    # ペアでフィルタ
    if pairs:
        filtered = [s for s in sessions if s.pair in pairs]
    else:
        filtered = list(sessions)

    if not filtered:
        return "=== Trade History ===\nNo trade history available."

    lines = []
    # ペア別に集計
    pair_groups: dict[str, list] = {}
    for s in filtered:
        pair_groups.setdefault(s.pair, []).append(s)

    label = "Overall" if not pairs else None
    for pair, group in pair_groups.items():
        header = label or pair
        wins = sum(1 for s in group if s.outcome == "win")
        losses = len(group) - wins
        total_pnl = sum(s.realized_pnl or 0 for s in group)
        avg_pnl = total_pnl / len(group) if group else 0
        win_rate = wins / len(group) * 100 if group else 0

        best = max(group, key=lambda s: s.realized_pnl or 0)
        worst = min(group, key=lambda s: s.realized_pnl or 0)

        recent = [s.outcome for s in group[-5:]]

        lines.append(f"=== Trade History: {header} ===")
        lines.append(
            f"Total: {len(group)} trades | Win: {wins} ({win_rate:.0f}%) | Loss: {losses}"
        )
        lines.append(f"Total PnL: {total_pnl:+.2f} | Avg PnL: {avg_pnl:+.2f}")
        lines.append(f"Last {len(recent)}: {', '.join(recent)}")
        lines.append(
            f"Best: {best.realized_pnl:+.2f} ({best.close_reason}) | "
            f"Worst: {worst.realized_pnl:+.2f} ({worst.close_reason})"
        )

    return "\n".join(lines)
```

- [ ] **Step 4: テスト実行 — パスを確認**

Run: `cd /home/teru/project/finance && uv run python -m pytest tests/test_ask_context_builder.py -v`
Expected: 12 passed

- [ ] **Step 5: コミット**

```bash
cd /home/teru/project/finance
git add src/rag/ask_context_builder.py tests/test_ask_context_builder.py
git commit -m "feat: add build_trade_summary for ask context"
```

---

### Task 4: 予測的中率サマリー生成

**Files:**
- Modify: `src/rag/ask_context_builder.py`
- Modify: `tests/test_ask_context_builder.py`

- [ ] **Step 1: テスト追加 — 予測的中率サマリー**

```python
# tests/test_ask_context_builder.py に追加

from src.rag.ask_context_builder import build_forecast_accuracy


def _make_forecasts(data: list[tuple[str, str, float]]):
    """(pair, predicted_direction, latest_price_delta) からダミー予測を生成。"""
    from dataclasses import dataclass

    @dataclass
    class FakeForecast:
        pair: str
        predicted_direction: str
        latest_price_delta: float
        stop_loss: float
        current_price: float
        reviewed: int

    return [
        FakeForecast(
            pair=pair,
            predicted_direction=direction,
            latest_price_delta=delta,
            stop_loss=1.0,
            current_price=1.5,
            reviewed=1,
        )
        for pair, direction, delta in data
    ]


def test_forecast_accuracy_basic():
    forecasts = _make_forecasts([
        ("EURUSD=X", "bullish", 0.01),   # correct (bullish, price up)
        ("EURUSD=X", "bullish", -0.01),  # incorrect
        ("EURUSD=X", "bearish", -0.01),  # correct (bearish, price down)
    ])
    result = build_forecast_accuracy({"EURUSD=X": forecasts}, pairs=["EURUSD=X"])
    assert "EURUSD=X" in result
    assert "Correct: 2" in result


def test_forecast_accuracy_empty():
    result = build_forecast_accuracy({}, pairs=[])
    assert "No forecast data" in result
```

- [ ] **Step 2: テスト実行 — 失敗を確認**

Run: `cd /home/teru/project/finance && uv run python -m pytest tests/test_ask_context_builder.py::test_forecast_accuracy_basic -v`
Expected: FAIL

- [ ] **Step 3: build_forecast_accuracy 実装**

`src/rag/ask_context_builder.py` に追加:

```python
def build_forecast_accuracy(
    forecasts_by_pair: dict[str, list],
    pairs: list[str],
) -> str:
    """予測レコードから的中率サマリーを生成する。

    Args:
        forecasts_by_pair: {symbol: [_ForecastRecord, ...]} 辞書。
        pairs: 対象ペアのリスト。空の場合は全ペア。

    Returns:
        プロンプトに注入するサマリーテキスト。
    """
    if not forecasts_by_pair:
        return "=== Forecast Accuracy ===\nNo forecast data available."

    target_pairs = pairs if pairs else list(forecasts_by_pair.keys())
    lines = ["=== Forecast Accuracy (24h) ==="]
    has_data = False

    for pair in target_pairs:
        records = forecasts_by_pair.get(pair, [])
        reviewed = [r for r in records if r.reviewed == 1 and r.latest_price_delta is not None]
        if not reviewed:
            continue
        has_data = True

        correct = 0
        for r in reviewed:
            delta = r.latest_price_delta
            if r.predicted_direction == "bullish" and delta > 0:
                correct += 1
            elif r.predicted_direction == "bearish" and delta < 0:
                correct += 1

        total = len(reviewed)
        accuracy = correct / total * 100 if total else 0
        lines.append(
            f"{pair}: {total} forecasts | Correct: {correct} ({accuracy:.0f}%) | "
            f"Incorrect: {total - correct}"
        )

    if not has_data:
        return "=== Forecast Accuracy ===\nNo forecast data available."

    return "\n".join(lines)
```

- [ ] **Step 4: テスト実行 — パスを確認**

Run: `cd /home/teru/project/finance && uv run python -m pytest tests/test_ask_context_builder.py -v`
Expected: 14 passed

- [ ] **Step 5: コミット**

```bash
cd /home/teru/project/finance
git add src/rag/ask_context_builder.py tests/test_ask_context_builder.py
git commit -m "feat: add build_forecast_accuracy for ask context"
```

---

### Task 5: AskContextBuilder クラス — 全体統合

**Files:**
- Modify: `src/rag/ask_context_builder.py`

- [ ] **Step 1: AskContextBuilder クラスを実装**

`src/rag/ask_context_builder.py` に `AskContextBuilder` クラスを追加:

```python
class AskContextBuilder:
    """askコマンド用のコンテキストを構築する。

    ユーザーの質問をベクトル化し、全データソースに対して
    セマンティック検索を実行。結果をマージしてテンプレートに注入可能な
    辞書として返す。
    """

    def __init__(
        self,
        config,
        store,
        analysis_store,
        position_mgr,
        session_store=None,
        forecast_store=None,
    ) -> None:
        self._config = config
        self._store = store
        self._analysis_store = analysis_store
        self._position_mgr = position_mgr
        self._session_store = session_store
        self._forecast_store = forecast_store

    async def build(self, user_message: str) -> dict[str, str]:
        """全コンテキストを構築して辞書で返す。

        Returns:
            {
                "open_positions": str,
                "semantic_results": str,
                "trade_summary": str,
                "forecast_accuracy": str,
                "technical_snapshots": str,
                "news_context": str,
            }
        """
        config = self._config

        # 1. ペア抽出
        all_instruments = config.watch_only_instruments + config.tradeable_instruments
        pairs = extract_pairs(user_message, all_instruments)

        # 2. 質問をベクトル化
        from src.rag.embedder import embed_text
        query_embedding = await embed_text(
            text=user_message,
            ollama_base_url=config.llm.ollama.base_url,
            model=config.rag.embedding_model,
        )

        # 3. 全コレクションに並列セマンティック検索
        semantic_results = await self._semantic_search(query_embedding, pairs)

        # 4. 既存コンテキスト（テクニカル、ニュース、ポジション）
        technical = self._build_technical_snapshots(pairs)
        news = self._build_news_context()
        positions = self._build_positions()

        # 5. 取引実績サマリー
        trade_summary = self._build_trade_summary(pairs)

        # 6. 予測的中率サマリー
        forecast_accuracy = self._build_forecast_accuracy(pairs)

        return {
            "open_positions": positions,
            "semantic_results": semantic_results,
            "trade_summary": trade_summary,
            "forecast_accuracy": forecast_accuracy,
            "technical_snapshots": technical,
            "news_context": news,
        }

    async def _semantic_search(self, query_embedding: list[float], pairs: list[str]) -> str:
        """全ChromaDBコレクションに並列セマンティック検索を実行する。"""
        store = self._store
        all_results: list[dict] = []

        # ニュースコレクション検索
        try:
            if pairs:
                for pair in pairs:
                    hits = store.query_news(
                        query_embedding=query_embedding,
                        pair=pair,
                        top_k=5,
                        lookback_hours=self._config.rag.news_lookback_hours,
                    )
                    for h in hits:
                        h["source"] = "news"
                        h["distance"] = h.get("distance", 0.5)
                    all_results.extend(hits)
            else:
                # ペア指定なし: カテゴリ別ニュースを取得
                for cat in ("fx", "global", "japan"):
                    entries = store.get_recent_category_news(
                        [cat], lookback_hours=self._config.rag.news_lookback_hours,
                    )
                    for e in entries[:3]:
                        e["source"] = "news"
                        e["distance"] = 0.5  # 時間ベース取得のためデフォルト距離
                        all_results.append(e)
        except Exception as e:
            logger.warning(f"[ASK] News search failed: {e}")

        # 振り返りコレクション検索
        try:
            refl_col = store._reflections
            if refl_col.count() > 0:
                where = None
                if pairs and len(pairs) == 1:
                    where = {"pair": {"$eq": pairs[0]}}
                results = refl_col.query(
                    query_embeddings=[query_embedding],
                    n_results=min(3, refl_col.count()),
                    where=where,
                )
                for i, doc in enumerate(results.get("documents", [[]])[0]):
                    all_results.append({
                        "text": doc,
                        "metadata": results["metadatas"][0][i],
                        "distance": results["distances"][0][i] if results.get("distances") else 0.5,
                        "source": "reflection",
                    })
        except Exception as e:
            logger.warning(f"[ASK] Reflection search failed: {e}")

        # 方向別コレクション検索（bullish + bearish）
        for direction in ("bullish", "bearish"):
            try:
                where = None
                if pairs and len(pairs) == 1:
                    where = {"pair": {"$eq": pairs[0]}}
                # DirectionalStore.query は where を直接サポートしないので
                # コレクションを直接使う
                col = store.directional._collection(direction)
                if col.count() > 0:
                    query_params = {
                        "query_embeddings": [query_embedding],
                        "n_results": min(3, col.count()),
                    }
                    if where:
                        query_params["where"] = where
                    results = col.query(**query_params)
                    for i, doc in enumerate(results.get("documents", [[]])[0]):
                        meta = results["metadatas"][0][i]
                        all_results.append({
                            "text": doc,
                            "metadata": meta,
                            "distance": results["distances"][0][i] if results.get("distances") else 0.5,
                            "source": direction,
                        })
            except Exception as e:
                logger.warning(f"[ASK] {direction} search failed: {e}")

        # マージ＆ランキング
        ranked = merge_and_rank_results(all_results, max_results=10)

        if not ranked:
            return "=== Related Context ===\nNo related data found."

        lines = ["=== Related Context (by relevance) ==="]
        for r in ranked:
            source = r.get("source", "unknown")
            meta = r.get("metadata", {})
            text = r.get("text", "")[:200]
            # ソースごとにタグを付ける
            session_type = meta.get("session_type", "")
            if session_type:
                tag = session_type  # "trade", "forecast", "hold"
            else:
                tag = source  # "news", "reflection"
            lines.append(f"[{tag}] {text}")

        return "\n".join(lines)

    def _build_technical_snapshots(self, pairs: list[str]) -> str:
        """テクニカルスナップショットを構築する。"""
        config = self._config
        all_instruments = config.watch_only_instruments + config.tradeable_instruments

        if pairs:
            instruments = [i for i in all_instruments if i.symbol in pairs]
            # ペア指定があってもwatch銘柄は含める
            instruments += config.watch_only_instruments
            # 重複除去
            seen = set()
            unique = []
            for i in instruments:
                if i.symbol not in seen:
                    seen.add(i.symbol)
                    unique.append(i)
            instruments = unique
        else:
            instruments = all_instruments

        lines = ["=== Technical Snapshots ==="]
        for inst in instruments:
            snaps = self._analysis_store.get_recent_snapshots(
                inst.symbol, hours=config.rag.analysis_lookback_hours,
            )
            if snaps:
                s = snaps[0]
                lines.append(
                    f"{inst.display_name}: bias={s.bias_score:+.2f} conf={s.confidence:.2f} "
                    f"dir={s.direction_bias} RR={s.risk_reward_ratio:.1f} | {s.reasoning_summary}"
                )
            else:
                lines.append(f"{inst.display_name}: no snapshot")

        return "\n".join(lines)

    def _build_news_context(self) -> str:
        """ニュースコンテキストを構築する（既存ロジック互換）。"""
        lines = ["=== News Context ==="]
        for cat in ("fx", "global", "japan"):
            entries = self._store.get_recent_category_news(
                [cat], lookback_hours=self._config.rag.news_lookback_hours,
            )
            if entries:
                for e in entries[:3]:
                    meta = e.get("metadata", {})
                    summary = meta.get("summary") or e.get("text", "")[:80]
                    score = meta.get("sentiment_score", 0.0)
                    lines.append(f"[{cat}] {summary} (sentiment={score:+.2f})")
        return "\n".join(lines)

    def _build_positions(self) -> str:
        """オープンポジションを構築する。"""
        account = self._position_mgr.get_account_state()
        lines = ["=== Open Positions ==="]
        if account.open_positions:
            for pos in account.open_positions:
                lines.append(
                    f"{pos.pair} {pos.direction.upper()} entry={pos.entry_price:.5f} "
                    f"SL={pos.stop_loss:.5f} TP={pos.take_profit:.5f}"
                )
        else:
            lines.append("No open positions.")
        return "\n".join(lines)

    def _build_trade_summary(self, pairs: list[str]) -> str:
        """取引実績サマリーを構築する。"""
        if not self._session_store:
            return ""
        from sqlalchemy import select
        from sqlalchemy.orm import Session as SASession
        from src.data.session_store import _TradingSession

        with SASession(self._session_store._engine) as sa_session:
            stmt = select(_TradingSession).where(_TradingSession.outcome.isnot(None))
            if pairs:
                stmt = stmt.where(_TradingSession.pair.in_(pairs))
            results = sa_session.execute(stmt).scalars().all()
            for r in results:
                sa_session.expunge(r)

        return build_trade_summary(list(results), pairs)

    def _build_forecast_accuracy(self, pairs: list[str]) -> str:
        """予測的中率サマリーを構築する。"""
        if not self._forecast_store:
            return ""
        config = self._config
        target_pairs = pairs if pairs else [i.symbol for i in config.tradeable_instruments]
        forecasts_by_pair: dict[str, list] = {}
        for pair in target_pairs:
            records = self._forecast_store.get_recent_forecasts(pair, hours=24)
            if records:
                forecasts_by_pair[pair] = records
        return build_forecast_accuracy(forecasts_by_pair, pairs)
```

- [ ] **Step 2: 動作確認 — インポートチェック**

Run: `cd /home/teru/project/finance && uv run python -c "from src.rag.ask_context_builder import AskContextBuilder; print('OK')"`
Expected: `OK`

- [ ] **Step 3: コミット**

```bash
cd /home/teru/project/finance
git add src/rag/ask_context_builder.py
git commit -m "feat: add AskContextBuilder with full semantic search integration"
```

---

### Task 6: プロンプトテンプレート更新

**Files:**
- Modify: `prompts/ask_system.txt`
- Modify: `prompts/ask_user.j2`

- [ ] **Step 1: ask_system.txt を更新**

```
You are an expert FX swing trader and technical analyst with 20 years of experience.
The user will ask questions or share observations about the FX market.

You have access to the following context data:
- Technical analysis snapshots (current direction, bias score, confidence)
- Semantic search results: related past data (trade outcomes, forecast verifications, hold decisions)
- Trade history summary (win rate, PnL)
- Forecast accuracy
- Recent news with sentiment scores
- Open positions

Response rules:
- When data supports your answer, cite specific numbers
- Reference similar past patterns when available
- When asked about direction, clearly state bullish/bearish rationale
- Distinguish between facts (from data) and your interpretation
- Always respond in Japanese
```

- [ ] **Step 2: ask_user.j2 を更新**

```jinja
{{ open_positions }}

{{ semantic_results }}

{{ trade_summary }}

{{ forecast_accuracy }}

{{ technical_snapshots }}

{{ news_context }}

=== User's Question / Comment ===
{{ user_message }}

上記のコンテキストをもとに、具体的なデータを引用しながら日本語で回答してください。
過去の類似パターンやトレード実績があれば積極的に参照してください。
```

- [ ] **Step 3: コミット**

```bash
cd /home/teru/project/finance
git add prompts/ask_system.txt prompts/ask_user.j2
git commit -m "feat: update ask prompts with semantic context sections"
```

---

### Task 7: trading_cycle.py の統合

**Files:**
- Modify: `src/trading_cycle.py:1215-1289`

- [ ] **Step 1: `_run_ask` を AskContextBuilder に差し替え**

`src/trading_cycle.py` の `_run_ask()` 関数（1264行目付近）を以下に置き換える:

```python
async def _run_ask(
    user_message: str,
    config: AppConfig,
    store: VectorStore,
    analysis_store: AnalysisStore,
) -> str:
    from src.data.session_store import SessionStore
    from src.data.analysis_store import ForecastStore
    from src.rag.ask_context_builder import AskContextBuilder

    state_store = StateStore(config.state_dir)
    position_mgr = PositionManager(state_store, config.trading.initial_balance, context="Ask")
    session_store = SessionStore(config.prices_db_path)
    forecast_store = ForecastStore(config.prices_db_path)

    builder = AskContextBuilder(
        config=config,
        store=store,
        analysis_store=analysis_store,
        position_mgr=position_mgr,
        session_store=session_store,
        forecast_store=forecast_store,
    )
    context_dict = await builder.build(user_message)

    llm = create_llm_client(config, "price_analysis")

    # テンプレートにセクション別コンテキストを注入
    from src.analysis.prompt_loader import render_prompt, load_prompt
    user_prompt = render_prompt(
        "ask_user.j2",
        user_message=user_message,
        **context_dict,
    )
    messages = [
        {"role": "system", "content": load_prompt("ask_system.txt")},
        {"role": "user", "content": user_prompt},
    ]
    logger.info(f"[ASK] LLM呼び出し中 ({len(user_message)} chars, context={len(user_prompt)} chars)...")
    response = await llm.chat(messages, temperature=config.llm.price_analysis.temperature)
    import re as _re
    response = _re.sub(r"<think>.*?</think>", "", response, flags=_re.DOTALL).strip()
    return response
```

- [ ] **Step 2: `_build_ask_context` を削除**

`src/trading_cycle.py` から `_build_ask_context()` 関数（1215-1261行目）を削除する。

- [ ] **Step 3: `run_ask` の外部インターフェースは変更なし**

既存の `run_ask()` 同期ラッパー（1282-1289行目）はシグネチャ変更なし。CLI/TUI/APIの呼び出し元に影響なし。

- [ ] **Step 4: コミット**

```bash
cd /home/teru/project/finance
git add src/trading_cycle.py
git commit -m "feat: replace _build_ask_context with AskContextBuilder in trading_cycle"
```

---

### Task 8: 全テスト実行と最終確認

- [ ] **Step 1: 全テスト実行**

Run: `cd /home/teru/project/finance && uv run python -m pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 2: ask コマンドの手動テスト（Ollama起動中のみ）**

Run: `cd /home/teru/project/finance && uv run python main.py run ask "EURUSDの最近の成績は？"`
Expected: 取引実績、予測的中率、セマンティック検索結果を含む日本語の回答

- [ ] **Step 3: 最終コミット**

```bash
cd /home/teru/project/finance
git add -A
git commit -m "feat: complete ask command semantic search enhancement"
```
