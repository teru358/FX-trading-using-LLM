# ATRベースSL/TP算出 + LLM適応パラメータ Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** SL/TPをATRベースでコード側から算出し、LLMの提案値と比較記録。振り返りでATR倍率を学習更新。発注理由を網羅的に保存して精算時の振り返り精度を向上。

**Architecture:** `atr_calculator.py` がATRベースでSL/TPを算出し、`adaptive_params_store.py` がペア別倍率をYAMLで管理。`entry_context_builder.py` が発注時のニュース+テクニ��ル+SL/TP比較を網羅的テキスト化。振り返りLLMが `atr_params_suggestion` を返し、倍率を動的更新する。

**Tech Stack:** Python 3.12, SQLAlchemy, pandas_ta, PyYAML, pytest

---

## File Structure

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `src/trading/atr_calculator.py` | ATRベースSL/TP算出、スイングH/L微調整、LLM比較記録 |
| Create | `src/persistence/adaptive_params_store.py` | adaptive_params.yaml の読み書き、クランプ、変更履歴 |
| Create | `src/trading/entry_context_builder.py` | 発注理由の網羅的テキスト構築 |
| Create | `tests/test_atr_calculator.py` | ATR算出のテスト |
| Create | `tests/test_adaptive_params_store.py` | パラメータストアのテスト |
| Create | `tests/test_entry_context_builder.py` | コンテキスト構築のテスト |
| Modify | `src/data/session_store.py` | 9カラム追加 + 自動マイグレーション + create_session拡張 |
| Modify | `src/config.py` | ATR倍率デフォルト・上下限の設定追加 |
| Modify | `config/settings.yaml` | ATR���率設定追加 |
| Modify | `src/analysis/price_analyzer.py` | key_support/key_resistance パース追加 |
| Modify | `prompts/price_user.j2` | key_support/key_resistance 出力要求追加 |
| Modify | `src/analysis/reflector.py` | 振り返りプロンプト拡張 + atr_params_suggestion パース |
| Modify | `src/trading_cycle.py` | Phase 4b: ATR算出+コンテキスト保存、クローズ: パラメータ更新 |

---

### Task 1: ATR Calculator

**Files:**
- Create: `src/trading/atr_calculator.py`
- Create: `tests/test_atr_calculator.py`

- [ ] **Step 1: テスト作成**

```python
# tests/test_atr_calculator.py
from __future__ import annotations

import pytest

from src.trading.atr_calculator import calculate_sl_tp, SLTPResult


def test_long_basic():
    """LONG: SL = entry - ATR*mult, TP = entry + ATR*mult"""
    result = calculate_sl_tp(
        direction="buy",
        entry_price=1.1500,
        atr_value=0.0050,
        sl_atr_mult=1.5,
        tp_atr_mult=3.0,
        llm_sl=1.1480,
        llm_tp=1.1560,
        swing_highs=[1.1560, 1.1580],
        swing_lows=[1.1420, 1.1440],
        key_support=None,
        key_resistance=None,
    )
    assert result.computed_sl == pytest.approx(1.1425, abs=0.0001)  # 1.15 - 0.005*1.5
    assert result.computed_tp == pytest.approx(1.1650, abs=0.0001)  # 1.15 + 0.005*3.0
    assert result.adopted == "computed"
    assert result.llm_sl == 1.1480
    assert result.llm_tp == 1.1560
    assert result.atr_value == 0.0050


def test_short_basic():
    """SHORT: SL = entry + ATR*mult, TP = entry - ATR*mult"""
    result = calculate_sl_tp(
        direction="sell",
        entry_price=1.1500,
        atr_value=0.0050,
        sl_atr_mult=1.5,
        tp_atr_mult=3.0,
        llm_sl=1.1520,
        llm_tp=1.1440,
        swing_highs=[1.1560, 1.1580],
        swing_lows=[1.1420, 1.1440],
        key_support=None,
        key_resistance=None,
    )
    assert result.computed_sl == pytest.approx(1.1575, abs=0.0001)  # 1.15 + 0.005*1.5
    assert result.computed_tp == pytest.approx(1.1350, abs=0.0001)  # 1.15 - 0.005*3.0


def test_support_adjustment_long():
    """LONG: key_supportがcomputed_slより内側に���る場合、その外側に寄せる"""
    result = calculate_sl_tp(
        direction="buy",
        entry_price=1.1500,
        atr_value=0.0050,
        sl_atr_mult=1.5,
        tp_atr_mult=3.0,
        llm_sl=1.1480,
        llm_tp=1.1560,
        swing_highs=[],
        swing_lows=[],
        key_support=1.1460,  # computed_sl(1.1425)よりentry側 → SLをsupportの外に寄せる
        key_resistance=None,
    )
    # key_support(1.1460) > computed_sl(1.1425) → SLをkey_support外側に調整
    # ただし結果は computed_sl 以下にはならない
    assert result.computed_sl <= 1.1460


def test_resistance_adjustment_short():
    """SHORT: key_resistanceがcomputed_slより内側にある場合、その外側に寄せる"""
    result = calculate_sl_tp(
        direction="sell",
        entry_price=1.1500,
        atr_value=0.0050,
        sl_atr_mult=1.5,
        tp_atr_mult=3.0,
        llm_sl=1.1520,
        llm_tp=1.1440,
        swing_highs=[],
        swing_lows=[],
        key_support=None,
        key_resistance=1.1540,  # computed_sl(1.1575)よりentry側
    )
    assert result.computed_sl >= 1.1540


def test_comparison_text():
    """比較テキストが生成される"""
    result = calculate_sl_tp(
        direction="buy",
        entry_price=1.1500,
        atr_value=0.0050,
        sl_atr_mult=1.5,
        tp_atr_mult=3.0,
        llm_sl=1.1480,
        llm_tp=1.1560,
        swing_highs=[],
        swing_lows=[],
        key_support=None,
        key_resistance=None,
    )
    text = result.comparison_text()
    assert "ATR" in text
    assert "computed" in text
    assert "llm" in text
```

- [ ] **Step 2: テスト実行 — 失敗確認**

Run: `cd /home/teru/project/finance && uv run python -m pytest tests/test_atr_calculator.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 実装**

```python
# src/trading/atr_calculator.py
"""ATRベースのSL/TP算出。

LLM出力値と比較記録を生成し、計算値を優先採用する。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class SLTPResult:
    """ATR算出SL/TPとLLM提案値の比較記録。"""
    computed_sl: float
    computed_tp: float
    llm_sl: float
    llm_tp: float
    adopted: str          # "computed"
    atr_value: float
    sl_atr_mult: float
    tp_atr_mult: float
    key_support: float | None = None
    key_resistance: float | None = None

    def comparison_text(self) -> str:
        """SL/TP比較テキストを生成する（session/RAG保存用）。"""
        return (
            f"ATR(14)={self.atr_value:.5f} sl_mult={self.sl_atr_mult} tp_mult={self.tp_atr_mult}\n"
            f"computed: SL={self.computed_sl:.5f} TP={self.computed_tp:.5f}\n"
            f"llm: SL={self.llm_sl:.5f} TP={self.llm_tp:.5f}\n"
            f"adopted={self.adopted}"
        )


def calculate_sl_tp(
    direction: str,
    entry_price: float,
    atr_value: float,
    sl_atr_mult: float,
    tp_atr_mult: float,
    llm_sl: float,
    llm_tp: float,
    swing_highs: list[float],
    swing_lows: list[float],
    key_support: float | None,
    key_resistance: float | None,
) -> SLTPResult:
    """ATRベースでSL/TPを算出し、S/Rで微調整する。

    Args:
        direction: "buy" or "sell"
        entry_price: エントリー価格
        atr_value: ATR(14)の値
        sl_atr_mult: SL距離のATR倍率
        tp_atr_mult: TP距離のATR倍率
        llm_sl: LLMが提案したSL（記録用）
        llm_tp: LLMが提案したTP（記録用）
        swing_highs: 直近スイングハイ
        swing_lows: 直近スイングロー
        key_support: LLMが意識するサポート価格
        key_resistance: LLMが意識するレジスタンス価格

    Returns:
        SLTPResult: 算出結果と比較記録
    """
    sl_distance = atr_value * sl_atr_mult
    tp_distance = atr_value * tp_atr_mult

    if direction == "buy":
        computed_sl = entry_price - sl_distance
        computed_tp = entry_price + tp_distance

        # SL微調整: key_supportがcomputed_slよりentry側なら、supportの外側に寄せる
        if key_support is not None and computed_sl < key_support < entry_price:
            # supportの少し下にSLを置く（ATR×0.1のマージン）
            adjusted = key_support - atr_value * 0.1
            # ただし元のcomputed_slより遠くにはしない
            computed_sl = max(computed_sl, adjusted)

        # swing_lowsによる微調整
        nearby_lows = [l for l in swing_lows if computed_sl < l < entry_price]
        if nearby_lows:
            nearest_low = min(nearby_lows)
            adjusted = nearest_low - atr_value * 0.1
            computed_sl = max(computed_sl, adjusted)

    else:  # sell
        computed_sl = entry_price + sl_distance
        computed_tp = entry_price - tp_distance

        # SL微調整: key_resistanceがcomputed_slよりentry側なら、resistanceの外側に寄せる
        if key_resistance is not None and entry_price < key_resistance < computed_sl:
            adjusted = key_resistance + atr_value * 0.1
            computed_sl = min(computed_sl, adjusted)

        # swing_highsによる微調整
        nearby_highs = [h for h in swing_highs if entry_price < h < computed_sl]
        if nearby_highs:
            nearest_high = max(nearby_highs)
            adjusted = nearest_high + atr_value * 0.1
            computed_sl = min(computed_sl, adjusted)

    logger.info(
        f"[ATR SL/TP] {direction.upper()} entry={entry_price:.5f} "
        f"ATR={atr_value:.5f}×{sl_atr_mult}/{tp_atr_mult} "
        f"→ SL={computed_sl:.5f} TP={computed_tp:.5f} "
        f"(LLM: SL={llm_sl:.5f} TP={llm_tp:.5f})"
    )

    return SLTPResult(
        computed_sl=computed_sl,
        computed_tp=computed_tp,
        llm_sl=llm_sl,
        llm_tp=llm_tp,
        adopted="computed",
        atr_value=atr_value,
        sl_atr_mult=sl_atr_mult,
        tp_atr_mult=tp_atr_mult,
        key_support=key_support,
        key_resistance=key_resistance,
    )
```

- [ ] **Step 4: テスト実行 — パス確認**

Run: `cd /home/teru/project/finance && uv run python -m pytest tests/test_atr_calculator.py -v`
Expected: 5 passed

- [ ] **Step 5: コミット**

```bash
cd /home/teru/project/finance
git add src/trading/atr_calculator.py tests/test_atr_calculator.py
git commit -m "feat: add ATR-based SL/TP calculator with S/R adjustment"
```

---

### Task 2: AdaptiveParamsStore

**Files:**
- Create: `src/persistence/adaptive_params_store.py`
- Create: `tests/test_adaptive_params_store.py`

- [ ] **Step 1: テスト作成**

```python
# tests/test_adaptive_params_store.py
from __future__ import annotations

import pytest

from src.persistence.adaptive_params_store import AdaptiveParamsStore


@pytest.fixture
def store(tmp_path):
    defaults = {"sl_atr_mult": 1.5, "tp_atr_mult": 3.0}
    limits = {
        "sl_atr_mult_min": 0.5, "sl_atr_mult_max": 3.0,
        "tp_atr_mult_min": 1.0, "tp_atr_mult_max": 6.0,
    }
    return AdaptiveParamsStore(tmp_path, defaults, limits)


def test_get_params_default(store):
    """未登録ペアはデフォルト値を返す。"""
    params = store.get_params("EURUSD=X")
    assert params["sl_atr_mult"] == 1.5
    assert params["tp_atr_mult"] == 3.0


def test_update_and_get(store):
    """更新後に新しい値が取得できる。"""
    store.update_params(
        pair="EURUSD=X",
        new_params={"sl_atr_mult": 2.0, "tp_atr_mult": 3.5},
        reason="SLが狭すぎた",
        trade_id="trade-001",
    )
    params = store.get_params("EURUSD=X")
    assert params["sl_atr_mult"] == 2.0
    assert params["tp_atr_mult"] == 3.5


def test_clamp_to_limits(store):
    """上下限を超える値はクランプされる。"""
    store.update_params(
        pair="EURUSD=X",
        new_params={"sl_atr_mult": 10.0, "tp_atr_mult": 0.1},
        reason="extreme",
        trade_id=None,
    )
    params = store.get_params("EURUSD=X")
    assert params["sl_atr_mult"] == 3.0  # max
    assert params["tp_atr_mult"] == 1.0  # min


def test_delta_limit(store):
    """1回の変更幅は±0.5以内に制限される。"""
    store.update_params(
        pair="USDJPY=X",
        new_params={"sl_atr_mult": 1.5, "tp_atr_mult": 3.0},
        reason="init",
        trade_id=None,
    )
    store.update_params(
        pair="USDJPY=X",
        new_params={"sl_atr_mult": 3.0},  # +1.5 from 1.5 → clamped to +0.5
        reason="big jump",
        trade_id=None,
    )
    params = store.get_params("USDJPY=X")
    assert params["sl_atr_mult"] == 2.0  # 1.5 + 0.5


def test_history(store):
    """変更履歴が記録される。"""
    store.update_params("EURUSD=X", {"sl_atr_mult": 1.8}, "test1", "t1")
    store.update_params("EURUSD=X", {"sl_atr_mult": 2.0}, "test2", "t2")
    history = store.get_history("EURUSD=X", limit=3)
    assert len(history) == 2
    assert history[-1]["reason"] == "test2"


def test_history_max_10(store):
    """履歴は最大10件。"""
    for i in range(15):
        store.update_params("EURUSD=X", {"sl_atr_mult": 1.5 + (i % 3) * 0.1}, f"r{i}", f"t{i}")
    history = store.get_history("EURUSD=X", limit=100)
    assert len(history) <= 10


def test_persistence(tmp_path):
    """ファイルを再読み込みしても値が維持される。"""
    defaults = {"sl_atr_mult": 1.5, "tp_atr_mult": 3.0}
    limits = {"sl_atr_mult_min": 0.5, "sl_atr_mult_max": 3.0, "tp_atr_mult_min": 1.0, "tp_atr_mult_max": 6.0}
    store1 = AdaptiveParamsStore(tmp_path, defaults, limits)
    store1.update_params("EURUSD=X", {"sl_atr_mult": 2.0}, "test", "t1")

    store2 = AdaptiveParamsStore(tmp_path, defaults, limits)
    assert store2.get_params("EURUSD=X")["sl_atr_mult"] == 2.0
```

- [ ] **Step 2: テスト実行 — 失敗確認**

Run: `cd /home/teru/project/finance && uv run python -m pytest tests/test_adaptive_params_store.py -v`
Expected: FAIL

- [ ] **Step 3: 実装**

```python
# src/persistence/adaptive_params_store.py
"""LLMが更新するペア別動的パラメータの管理。

adaptive_params.yaml を読み書きし、クランプ・変動幅制限・変更履歴を管理する。
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_MAX_DELTA = 0.5       # 1回の変更幅上限
_MAX_HISTORY = 10      # 変更履歴の最大保持件数
_FILENAME = "adaptive_params.yaml"


class AdaptiveParamsStore:
    """ペア別動的パラメータの管理。"""

    def __init__(self, state_dir: Path, defaults: dict, limits: dict) -> None:
        self._path = Path(state_dir) / _FILENAME
        self._defaults = dict(defaults)
        self._limits = limits
        self._data = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            with open(self._path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}
        data.setdefault("_schema_version", 1)
        data.setdefault("defaults", dict(self._defaults))
        data.setdefault("pairs", {})
        return data

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.dump(self._data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            if self._path.exists():
                shutil.copy2(self._path, self._path.with_suffix(".bak"))
            os.replace(tmp, self._path)
        except Exception:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise

    def get_params(self, pair: str) -> dict:
        """ペアの現在パラメータを返す。未登録ならdefaults。"""
        pair_data = self._data["pairs"].get(pair)
        if pair_data is None:
            return dict(self._defaults)
        result = dict(self._defaults)
        for key in self._defaults:
            if key in pair_data:
                result[key] = pair_data[key]
        return result

    def update_params(
        self,
        pair: str,
        new_params: dict,
        reason: str,
        trade_id: str | None,
    ) -> None:
        """パラメータを更新。クランプ + 変動幅制限を適用。"""
        current = self.get_params(pair)
        pair_data = self._data["pairs"].get(pair, {})

        updated = dict(current)
        for key, new_val in new_params.items():
            if key not in self._defaults:
                continue
            if new_val is None:
                continue

            old_val = current.get(key, self._defaults.get(key, new_val))

            # 変動幅制限
            delta = new_val - old_val
            delta = max(-_MAX_DELTA, min(_MAX_DELTA, delta))
            clamped_val = old_val + delta

            # 上下限クランプ
            min_key = f"{key}_min"
            max_key = f"{key}_max"
            if min_key in self._limits:
                clamped_val = max(self._limits[min_key], clamped_val)
            if max_key in self._limits:
                clamped_val = min(self._limits[max_key], clamped_val)

            updated[key] = round(clamped_val, 4)

        # 履歴に追���
        history = pair_data.get("history", [])
        history_entry = {
            **{k: updated[k] for k in self._defaults},
            "updated_at": datetime.now().isoformat(),
            "reason": reason,
            "trade_id": trade_id,
        }
        history.append(history_entry)
        if len(history) > _MAX_HISTORY:
            history = history[-_MAX_HISTORY:]

        # ペアデータ更新
        self._data["pairs"][pair] = {
            **{k: updated[k] for k in self._defaults},
            "updated_at": datetime.now().isoformat(),
            "reason": reason,
            "history": history,
        }

        self._save()
        logger.info(f"[ADAPTIVE] {pair}: updated {new_params} → {updated} | reason: {reason}")

    def get_history(self, pair: str, limit: int = 3) -> list[dict]:
        """変更履歴を返す。"""
        pair_data = self._data["pairs"].get(pair, {})
        history = pair_data.get("history", [])
        return history[-limit:]
```

- [ ] **Step 4: テスト実行 — パス確認**

Run: `cd /home/teru/project/finance && uv run python -m pytest tests/test_adaptive_params_store.py -v`
Expected: 7 passed

- [ ] **Step 5: コミット**

```bash
cd /home/teru/project/finance
git add src/persistence/adaptive_params_store.py tests/test_adaptive_params_store.py
git commit -m "feat: add AdaptiveParamsStore for per-pair ATR multiplier management"
```

---

### Task 3: EntryContextBuilder

**Files:**
- Create: `src/trading/entry_context_builder.py`
- Create: `tests/test_entry_context_builder.py`

- [ ] **Step 1: テスト作成**

```python
# tests/test_entry_context_builder.py
from __future__ import annotations

import pytest
from dataclasses import dataclass, field

from src.trading.entry_context_builder import build_entry_context
from src.trading.atr_calculator import SLTPResult


def _make_news():
    @dataclass
    class FakeNews:
        sentiment_score: float = -0.25
        confidence: float = 0.70
        key_themes: list = field(default_factory=lambda: ["ECB rate decision"])
        bullish_factors: list = field(default_factory=lambda: ["strong employment"])
        bearish_factors: list = field(default_factory=lambda: ["dovish guidance"])
        summary: str = "ECBの利下げ示唆"
    return FakeNews()


def _make_price():
    @dataclass
    class FakePrice:
        direction_bias: str = "short"
        bias_score: float = -0.50
        confidence: float = 0.80
        entry_zone: tuple = (1.1530, 1.1540)
        reasoning_summary: str = "SMA20<SMA50, MACD bearish"
    return FakePrice()


def _make_sltp():
    return SLTPResult(
        computed_sl=1.1575, computed_tp=1.1350,
        llm_sl=1.1535, llm_tp=1.1490,
        adopted="computed",
        atr_value=0.0050, sl_atr_mult=1.5, tp_atr_mult=3.0,
        key_support=1.1480, key_resistance=1.1560,
    )


def test_build_entry_context_contains_all_sections():
    text = build_entry_context(
        combined_score=-0.373,
        confidence=0.75,
        action="sell",
        news_weight=0.40,
        price_weight=0.60,
        news=_make_news(),
        price=_make_price(),
        sltp=_make_sltp(),
        macro_context="Nikkei short, S&P neutral",
    )
    assert "=== Signal Summary ===" in text
    assert "=== News Sentiment ===" in text
    assert "=== Technical Analysis ===" in text
    assert "=== SL/TP Decision ===" in text
    assert "=== Macro Context ===" in text
    assert "ECB" in text
    assert "ATR" in text
    assert "computed" in text


def test_build_entry_context_without_macro():
    text = build_entry_context(
        combined_score=0.30,
        confidence=0.70,
        action="buy",
        news_weight=0.40,
        price_weight=0.60,
        news=_make_news(),
        price=_make_price(),
        sltp=_make_sltp(),
        macro_context="",
    )
    assert "=== Signal Summary ===" in text
    assert "=== Macro Context ===" not in text
```

- [ ] **Step 2: テスト実行 — 失敗確認**

Run: `cd /home/teru/project/finance && uv run python -m pytest tests/test_entry_context_builder.py -v`
Expected: FAIL

- [ ] **Step 3: 実装**

```python
# src/trading/entry_context_builder.py
"""発注時のコンテキストを網羅的にテキスト化する。

ニュース + テクニカル + SL/TP比較 + マクロを含む完全なテキストを生成し、
session_store の analysis_summary と RAG entry ドキ���メントに保存する。
"""

from __future__ import annotations

from src.trading.atr_calculator import SLTPResult


def build_entry_context(
    combined_score: float,
    confidence: float,
    action: str,
    news_weight: float,
    price_weight: float,
    news,
    price,
    sltp: SLTPResult,
    macro_context: str = "",
) -> str:
    """発注理由を網羅的テキストとして構築する。

    Args:
        combined_score: 合成スコア
        confidence: 合成信頼度
        action: "buy" / "sell"
        news_weight: ニュース重み
        price_weight: テクニカル重み
        news: NewsSentiment オブジェクト
        price: PriceAnalysis オブジェクト
        sltp: SLTPResult (ATR算出結果)
        macro_context: マクロコンテキストテキスト

    Returns:
        セクション分けされたテキスト
    """
    sections = []

    # Signal Summary
    sections.append(
        f"=== Signal Summary ===\n"
        f"combined_score={combined_score:+.3f} confidence={confidence:.2f} action={action}\n"
        f"news_weight={news_weight} price_weight={price_weight}"
    )

    # News Sentiment
    themes = ", ".join(news.key_themes[:5]) if hasattr(news, "key_themes") and news.key_themes else "N/A"
    bullish = ", ".join(news.bullish_factors[:3]) if hasattr(news, "bullish_factors") and news.bullish_factors else "N/A"
    bearish = ", ".join(news.bearish_factors[:3]) if hasattr(news, "bearish_factors") and news.bearish_factors else "N/A"
    summary = getattr(news, "summary", "")
    sections.append(
        f"=== News Sentiment ===\n"
        f"score={news.sentiment_score:+.2f} confidence={news.confidence:.2f}\n"
        f"key_themes: {themes}\n"
        f"bullish_factors: {bullish}\n"
        f"bearish_factors: {bearish}\n"
        f"summary: {summary}"
    )

    # Technical Analysis
    entry_zone = getattr(price, "entry_zone", (0, 0))
    reasoning = getattr(price, "reasoning_summary", "")
    key_s = f" key_support={sltp.key_support:.5f}" if sltp.key_support else ""
    key_r = f" key_resistance={sltp.key_resistance:.5f}" if sltp.key_resistance else ""
    sections.append(
        f"=== Technical Analysis ===\n"
        f"direction={price.direction_bias} bias_score={price.bias_score:+.2f} confidence={price.confidence:.2f}\n"
        f"reasoning: {reasoning}\n"
        f"entry_zone=[{entry_zone[0]:.5f}, {entry_zone[1]:.5f}]{key_s}{key_r}"
    )

    # SL/TP Decision
    sections.append(
        f"=== SL/TP Decision ===\n"
        f"{sltp.comparison_text()}"
    )

    # Macro Context
    if macro_context:
        sections.append(
            f"=== Macro Context ===\n"
            f"{macro_context}"
        )

    return "\n\n".join(sections)
```

- [ ] **Step 4: テスト実行 — パス確認**

Run: `cd /home/teru/project/finance && uv run python -m pytest tests/test_entry_context_builder.py -v`
Expected: 2 passed

- [ ] **Step 5: コミット**

```bash
cd /home/teru/project/finance
git add src/trading/entry_context_builder.py tests/test_entry_context_builder.py
git commit -m "feat: add EntryContextBuilder for comprehensive trade entry recording"
```

---

### Task 4: 設定追加 + SessionStore スキーマ拡張

**Files:**
- Modify: `src/config.py`
- Modify: `config/settings.yaml`
- Modify: `src/data/session_store.py`

- [ ] **Step 1: config.py に ATR倍率設定を追��**

`src/config.py` の `TradingConfig` クラス（`rag_adjustment_hold_multiplier` の後に追加）:

```python
    # ATRベースSL/TP
    sl_atr_mult_default: float = 1.5
    tp_atr_mult_default: float = 3.0
    sl_atr_mult_min: float = 0.5
    sl_atr_mult_max: float = 3.0
    tp_atr_mult_min: float = 1.0
    tp_atr_mult_max: float = 6.0
```

`load_config()` の `TradingConfig(...)` コンストラクタ呼び���し（`rag_adjustment_hold_multiplier` の後）にも追加:

```python
        sl_atr_mult_default=t.get("sl_atr_mult_default", 1.5),
        tp_atr_mult_default=t.get("tp_atr_mult_default", 3.0),
        sl_atr_mult_min=t.get("sl_atr_mult_min", 0.5),
        sl_atr_mult_max=t.get("sl_atr_mult_max", 3.0),
        tp_atr_mult_min=t.get("tp_atr_mult_min", 1.0),
        tp_atr_mult_max=t.get("tp_atr_mult_max", 6.0),
```

- [ ] **Step 2: settings.yaml に追加**

`config/settings.yaml` の `trading:` セクション末尾（rag_adjustment の後）に追加:

```yaml
  # ATRベースSL/TP
  sl_atr_mult_default: 1.5
  tp_atr_mult_default: 3.0
  sl_atr_mult_min: 0.5
  sl_atr_mult_max: 3.0
  tp_atr_mult_min: 1.0
  tp_atr_mult_max: 6.0
```

- [ ] **Step 3: SessionStore にカラム追加 + 自動マイグレーション**

`src/data/session_store.py` を修正:

1. `_TradingSession` に9カラム追加:

```python
    # ATR SL/TP比較データ
    atr_value       = Column(Float)
    sl_atr_mult     = Column(Float)
    tp_atr_mult     = Column(Float)
    computed_sl     = Column(Float)
    computed_tp     = Column(Float)
    llm_sl          = Column(Float)
    llm_tp          = Column(Float)
    key_support     = Column(Float)
    key_resistance  = Column(Float)
```

2. `SessionStore.__init__` に自動マイグレーション追加:

```python
    def __init__(self, db_path) -> None:
        self._engine = _get_engine(db_path)
        self._migrate()

    def _migrate(self) -> None:
        """既存テーブルに新カラムが無ければ追加する。"""
        new_columns = [
            "atr_value", "sl_atr_mult", "tp_atr_mult",
            "computed_sl", "computed_tp",
            "llm_sl", "llm_tp",
            "key_support", "key_resistance",
        ]
        from sqlalchemy import text, inspect
        insp = inspect(self._engine)
        if "trading_sessions" not in insp.get_table_names():
            return
        existing = {c["name"] for c in insp.get_columns("trading_sessions")}
        with self._engine.begin() as conn:
            for col in new_columns:
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE trading_sessions ADD COLUMN {col} REAL"))
                    logger.info(f"[SESSION] Migration: added column {col}")
```

3. `create_session` シグネチャを拡張（既存パラメータの後にオプショナル追加）:

```python
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
        # ATR SL/TP比較データ（オプショナル）
        atr_value: float | None = None,
        sl_atr_mult: float | None = None,
        tp_atr_mult: float | None = None,
        computed_sl: float | None = None,
        computed_tp: float | None = None,
        llm_sl: float | None = None,
        llm_tp: float | None = None,
        key_support: float | None = None,
        key_resistance: float | None = None,
    ) -> None:
```

`_TradingSession` 生成部分にも追加:

```python
            rec = _TradingSession(
                ...  # 既存フィールド
                atr_value=atr_value,
                sl_atr_mult=sl_atr_mult,
                tp_atr_mult=tp_atr_mult,
                computed_sl=computed_sl,
                computed_tp=computed_tp,
                llm_sl=llm_sl,
                llm_tp=llm_tp,
                key_support=key_support,
                key_resistance=key_resistance,
            )
```

- [ ] **Step 4: 既存テスト実行**

Run: `cd /home/teru/project/finance && uv run python -m pytest tests/test_session_store.py -v`
Expected: 4 passed（既存テストは新カ���ムを渡さないが、デフォルトNULLで問題なし）

- [ ] **Step 5: コミット**

```bash
cd /home/teru/project/finance
git add src/config.py src/data/session_store.py
git add -f config/settings.yaml
git commit -m "feat: add ATR config, session schema migration, extended create_session"
```

---

### Task 5: LLM出力に key_support/key_resistance を追加

**Files:**
- Modify: `prompts/price_user.j2`
- Modify: `src/analysis/price_analyzer.py`

- [ ] **Step 1: price_user.j2 のJSON出力スキーマに追加**

`prompts/price_user.j2` のJSON出力セクション末尾を修正。`"reasoning_summary"` の前に2行追加:

```
  "key_support": <nearest_support_price>,
  "key_resistance": <nearest_resistance_price>,
```

出力スキーマ全体:
```
{
  "direction_bias": "long|short|neutral",
  "bias_score": <float -1.0 to 1.0>,
  "confidence": <float 0.0 to 1.0>,
  "entry_zone": [<low_price>, <high_price>],
  "stop_loss": <price>,
  "take_profit": <price>,
  "risk_reward_ratio": <float>,
  "key_support": <nearest_support_price>,
  "key_resistance": <nearest_resistance_price>,
  "reasoning_summary": "ニュースやテクニカル指標を踏まえた分析根拠を日本語1文で記述"
}
```

また、Considerリストに追加（11番目として）:
```
11. Identify key support (nearest level below entry) and resistance (nearest level above entry) from swing highs/lows, Ichimoku kumo edges, or Bollinger Bands
```

- [ ] **Step 2: price_analyzer.py で key_support/key_resistance をパース**

`src/analysis/price_analyzer.py` の `analyze_price_action()` 内、`data = extract_json(response_text)` の後の値取得セクション（`direction = data.get(...)` 付近）に追加:

```python
        key_support = _to_float(data.get("key_support"), None)
        key_resistance = _to_float(data.get("key_resistance"), None)
```

`PriceAnalysis` dataclass に2フィールド追加:

```python
@dataclass
class PriceAnalysis:
    ...
    reasoning_summary: str
    analyzed_at: datetime
    key_support: float | None = None
    key_resistance: float | None = None
```

`PriceAnalysis(...)` 構築時に追加:

```python
        analysis = PriceAnalysis(
            ...
            reasoning_summary=data.get("reasoning_summary", ""),
            analyzed_at=datetime.now(),
            key_support=key_support,
            key_resistance=key_resistance,
        )
```

- [ ] **Step 3: コミット**

```bash
cd /home/teru/project/finance
git add prompts/price_user.j2 src/analysis/price_analyzer.py
git commit -m "feat: add key_support/key_resistance to LLM output and PriceAnalysis"
```

---

### Task 6: 振り返りプロンプト拡張 + atr_params_suggestion パース

**Files:**
- Modify: `src/analysis/reflector.py`

- [ ] **Step 1: _CLOSE_REFLECTION_PROMPT を拡張**

`src/analysis/reflector.py` の `_CLOSE_REFLECTION_PROMPT` 文字列に、`{macro_context_section}` の後、`=== Task ===` の前にセクションを追加:

```python
{entry_analysis_section}
{sltp_analysis_section}
{param_history_section}
```

`=== Task ===` の評価項目を拡張:

```
=== Task ===
Evaluate this completed trade. Assess:
1. Was the directional call correct? (take_profit = yes, stop_loss = no)
2. Was the SL/TP placement appropriate given what actually happened?
3. If macro context is available: did the macro instruments correctly indicate the direction?
4. Was the news sentiment assessment correct? Did the key themes play out as expected?
5. Was the technical analysis direction correct? Were the key support/resistance levels respected?
6. Based on the SL/TP comparison: should the ATR multipliers be adjusted for this pair?
7. What is the ONE most actionable lesson for future {pair} trades?
{user_context}
Return ONLY valid JSON:
{{
  "outcome_summary": "<one sentence: what happened and the key reason>",
  "was_directionally_correct": true|false,
  "lesson": "<one specific, actionable lesson>",
  "confidence_assessment": "<was the entry timing and risk setup appropriate?>",
  "atr_params_suggestion": {{
    "sl_atr_mult": <new_value or null if no change>,
    "tp_atr_mult": <new_value or null if no change>,
    "reason": "<why this change, or 'no change needed'>"
  }}
}}
```

- [ ] **Step 2: generate_close_reflection のシグネチャを拡張**

`generate_close_reflection()` に新パラメータを追加:

```python
async def generate_close_reflection(
    pair_cfg,
    order: "Order",
    llm: LLMClient,
    temperature: float = 0.1,
    user_notes: str = "",
    macro_context_at_entry: str = "",
    entry_analysis: str = "",        # 新規: 発注時の全分析テキスト
    sltp_comparison: str = "",       # 新規: SL/TP比較テキスト
    param_history: str = "",         # 新規: ATR倍率変更履歴
) -> Reflection:
```

フォーマット時に注入:

```python
    entry_analysis_section = (
        f"=== Entry Analysis (Full Context) ===\n{entry_analysis}\n"
        if entry_analysis else ""
    )
    sltp_analysis_section = (
        f"=== SL/TP Analysis ===\n{sltp_comparison}\n"
        if sltp_comparison else ""
    )
    param_history_section = (
        f"=== Parameter History (last 3) ===\n{param_history}\n"
        if param_history else ""
    )
```

- [ ] **Step 3: Reflection dataclass に atr_params_suggestion を追加**

```python
@dataclass
class Reflection:
    entry_id: str
    pair: str
    cycle_time: datetime
    action: str
    outcome_summary: str
    was_directionally_correct: bool
    lesson: str
    confidence_assessment: str
    full_text: str
    atr_params_suggestion: dict | None = None  # 新規
```

LLMレスポンスのパースでも追加:

```python
    atr_suggestion = data.get("atr_params_suggestion")
```

Return時に追加:

```python
    return Reflection(
        ...
        atr_params_suggestion=atr_suggestion,
    )
```

- [ ] **Step 4: コミット**

```bash
cd /home/teru/project/finance
git add src/analysis/reflector.py
git commit -m "feat: extend reflection prompt with SL/TP analysis and atr_params_suggestion"
```

---

### Task 7: trading_cycle.py 統合

**Files:**
- Modify: `src/trading_cycle.py`

- [ ] **Step 1: Phase 4b — 発注時にATR算出 + コンテキスト保存**

`src/trading_cycle.py` の Phase 4b（`for sig in signals:` ループ内）で、RAG補正後・発注前にATR算出を挟む。

importに追加:
```python
from src.trading.atr_calculator import calculate_sl_tp
from src.trading.entry_context_builder import build_entry_context
from src.persistence.adaptive_params_store import AdaptiveParamsStore
```

`trading_cycle()` 関数の先頭で AdaptiveParamsStore を初期化:
```python
    adaptive_store = AdaptiveParamsStore(
        state_dir=config.state_dir,
        defaults={
            "sl_atr_mult": config.trading.sl_atr_mult_default,
            "tp_atr_mult": config.trading.tp_atr_mult_default,
        },
        limits={
            "sl_atr_mult_min": config.trading.sl_atr_mult_min,
            "sl_atr_mult_max": config.trading.sl_atr_mult_max,
            "tp_atr_mult_min": config.trading.tp_atr_mult_min,
            "tp_atr_mult_max": config.trading.tp_atr_mult_max,
        },
    )
```

Phase 4b の `if sig.action != "hold":` 内、`broker.execute_signal()` の前に:

```python
            # ATRベースSL/TP算出
            atr_params = adaptive_store.get_params(sig.pair)
            # ATR値を取得: IndicatorSummaryが直接手に入らないので、
            # price_storeからOHLCVを取得してATRを算出
            sltp_result = None
            try:
                from src.data.price_fetcher import fetch_ohlcv
                price_data = fetch_ohlcv(
                    sig.pair, config.trading.lookback_days,
                    config.trading.ohlcv_interval, price_store=price_store,
                )
                if price_data and len(price_data.df) >= 14:
                    import pandas_ta as ta
                    atr_series = ta.atr(
                        price_data.df["High"], price_data.df["Low"],
                        price_data.df["Close"], length=14,
                    )
                    atr_val = float(atr_series.iloc[-1]) if atr_series is not None and not atr_series.empty else None
                    if atr_val and atr_val > 0:
                        sltp_result = calculate_sl_tp(
                            direction=sig.action,
                            entry_price=sig.entry_price,
                            atr_value=atr_val,
                            sl_atr_mult=atr_params["sl_atr_mult"],
                            tp_atr_mult=atr_params["tp_atr_mult"],
                            llm_sl=sig.stop_loss,
                            llm_tp=sig.take_profit,
                            swing_highs=list(sig.price.entry_zone) if sig.price else [],
                            swing_lows=[],
                            key_support=getattr(sig.price, "key_support", None),
                            key_resistance=getattr(sig.price, "key_resistance", None),
                        )
                        # TradeSignal のSL/TPを計算値で上書き
                        sig.stop_loss = sltp_result.computed_sl
                        sig.take_profit = sltp_result.computed_tp
                        # ポジションサイズを再計算（SL距離が変わるため）
                        sig.position_size = _calculate_position_size(
                            balance=position_mgr.get_account_state().balance,
                            risk_pct=config.trading.risk_per_trade,
                            entry=sig.entry_price,
                            stop_loss=sltp_result.computed_sl,
                            pip_value=next(p for p in config.tradeable_instruments if p.symbol == sig.pair).pip_value,
                            min_lot_size=config.trading.min_lot_size,
                            lot_unit=config.trading.lot_unit,
                        )
            except Exception as e:
                logger.warning(f"[ATR] {sig.pair}: ATR SL/TP calculation failed — {e}")
```

`broker.execute_signal()` の後、セッション作成時に `sltp_result` と `entry_context` ���追加:

```python
            if order and session_store:
                # 網羅的コンテキスト構築
                entry_ctx = ""
                if sltp_result:
                    entry_ctx = build_entry_context(
                        combined_score=sig.combined_score,
                        confidence=sig.confidence,
                        action=sig.action,
                        news_weight=config.trading.news_weight,
                        price_weight=config.trading.price_weight,
                        news=sig.news,
                        price=sig.price,
                        sltp=sltp_result,
                        macro_context=macro_ctxs.get(sig.pair, ""),
                    )

                direction = "bullish" if order.direction == "buy" else "bearish"
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
                    analysis_summary=entry_ctx or sig.detail_reason,
                    opened_at=order.opened_at,
                    # ATR比較データ
                    atr_value=sltp_result.atr_value if sltp_result else None,
                    sl_atr_mult=sltp_result.sl_atr_mult if sltp_result else None,
                    tp_atr_mult=sltp_result.tp_atr_mult if sltp_result else None,
                    computed_sl=sltp_result.computed_sl if sltp_result else None,
                    computed_tp=sltp_result.computed_tp if sltp_result else None,
                    llm_sl=sltp_result.llm_sl if sltp_result else None,
                    llm_tp=sltp_result.llm_tp if sltp_result else None,
                    key_support=sltp_result.key_support if sltp_result else None,
                    key_resistance=sltp_result.key_resistance if sltp_result else None,
                )
```

- [ ] **Step 2: クローズ振り返り時にパラメータ更新**

Phase 1.5 と Phase 4a のクローズ振り返りで、`generate_close_reflection()` 呼び出しに追加パラメータを渡し、返り値から `atr_params_suggestion` を処理する。

`generate_close_reflection()` の呼び出し箇所を修正:

```python
                # session_storeからエントリー分析テキストを取得
                entry_analysis = ""
                sltp_comparison = ""
                param_history_text = ""
                if session_store:
                    sess = session_store.get_session(closed_order.order_id)
                    if sess:
                        entry_analysis = sess.analysis_summary or ""
                        if sess.atr_value and sess.computed_sl:
                            sltp_comparison = (
                                f"ATR(14)={sess.atr_value:.5f} sl_mult={sess.sl_atr_mult} tp_mult={sess.tp_atr_mult}\n"
                                f"computed: SL={sess.computed_sl:.5f} TP={sess.computed_tp:.5f}\n"
                                f"llm: SL={sess.llm_sl:.5f} TP={sess.llm_tp:.5f}\n"
                                f"Actual close: {closed_order.close_price:.5f} ({closed_order.close_reason})"
                            )
                        history = adaptive_store.get_history(closed_order.pair, limit=3)
                        if history:
                            param_history_text = "\n".join(
                                f"[{h.get('updated_at', '?')}] sl={h.get('sl_atr_mult')} tp={h.get('tp_atr_mult')} reason={h.get('reason', '')}"
                                for h in history
                            )

                reflection = await generate_close_reflection(
                    pair_cfg=pair_cfg,
                    order=closed_order,
                    llm=llm_reflect,
                    temperature=config.llm.reflection.temperature,
                    user_notes=load_user_notes(config.user_notes_path, "reflect"),
                    entry_analysis=entry_analysis,
                    sltp_comparison=sltp_comparison,
                    param_history=param_history_text,
                )

                # ATRパラメータ更新
                if reflection.atr_params_suggestion and session_store:
                    suggestion = reflection.atr_params_suggestion
                    new_params = {}
                    if suggestion.get("sl_atr_mult") is not None:
                        new_params["sl_atr_mult"] = suggestion["sl_atr_mult"]
                    if suggestion.get("tp_atr_mult") is not None:
                        new_params["tp_atr_mult"] = suggestion["tp_atr_mult"]
                    if new_params:
                        adaptive_store.update_params(
                            pair=closed_order.pair,
                            new_params=new_params,
                            reason=suggestion.get("reason", "LLM suggestion"),
                            trade_id=closed_order.order_id,
                        )
```

- [ ] **Step 3: `_calculate_position_size` のインポート確認**

`_calculate_position_size` は `signal_combiner.py` のモジュール内関数。Phase 4bで使うためにインポートを追加:

```python
from src.signals.signal_combiner import _calculate_position_size
```

- [ ] **Step 4: コミット**

```bash
cd /home/teru/project/finance
git add src/trading_cycle.py
git commit -m "feat: integrate ATR SL/TP + entry context + adaptive params into trading cycle"
```

---

### Task 8: 全テスト実行と最終確認

- [ ] **Step 1: 全テスト実行**

Run: `cd /home/teru/project/finance && uv run python -m pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 2: 設定読み込み確認**

Run: `cd /home/teru/project/finance && uv run python -c "from src.config import load_config; c = load_config(); print(c.trading.sl_atr_mult_default, c.trading.tp_atr_mult_default)"`
Expected: `1.5 3.0`

- [ ] **Step 3: AdaptiveParamsStore 動作確認**

Run: `cd /home/teru/project/finance && uv run python -c "from pathlib import Path; from src.persistence.adaptive_params_store import AdaptiveParamsStore; s = AdaptiveParamsStore(Path('data/state'), {'sl_atr_mult': 1.5, 'tp_atr_mult': 3.0}, {'sl_atr_mult_min': 0.5, 'sl_atr_mult_max': 3.0, 'tp_atr_mult_min': 1.0, 'tp_atr_mult_max': 6.0}); print(s.get_params('EURUSD=X'))"`
Expected: `{'sl_atr_mult': 1.5, 'tp_atr_mult': 3.0}`

- [ ] **Step 4: 最終コミット**

```bash
cd /home/teru/project/finance
git add -A
git commit -m "feat: complete ATR-based SL/TP system with adaptive parameters"
```
