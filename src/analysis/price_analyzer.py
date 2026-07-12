from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime

from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class PriceAnalysis:
    pair: str
    direction_bias: str  # "long" | "short" | "neutral"
    bias_score: float  # -1.0 to +1.0
    confidence: float  # 0.0 to 1.0
    entry_zone: tuple[float, float]
    reasoning_summary: str
    analyzed_at: datetime
    market_regime: str = "unknown"       # "trending" | "ranging" | "breakout_pending" | "unknown"
    confidence_modifier: float = 0.0     # LLM による tech_score confidence の補正 (-0.1〜+0.1)
    # 後方互換: 旧LLM出力・snapshot aggが stop_loss/take_profit を含む場合に受容
    stop_loss: float = 0.0
    take_profit: float = 0.0
    risk_reward_ratio: float = 0.0


def load_user_notes(notes_path: Path, section: str = "price") -> str:
    """user_notes.md の指定セクション (price/news/reflect/plan) のテキストを返す。

    ## price / ## news / ## reflect / ## plan の見出しでセクションを分割する。
    見出しがない場合はファイル全体を "price" として扱う（後方互換）。
    空または有効テキストなしの場合は空文字を返す。
    """
    if not notes_path.exists():
        return ""
    try:
        text = notes_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning(f"user_notes read failed ({notes_path}): {type(e).__name__}: {e}")
        return ""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)

    def _clean(raw: str) -> str:
        lines = [l for l in raw.splitlines() if l.strip() and not l.strip().startswith("#") and l.strip() != "---"]
        return "\n".join(lines).strip()

    parts = re.split(r"^##\s+(price|news|reflect|plan)\s*$", text, flags=re.MULTILINE | re.IGNORECASE)
    if len(parts) == 1:
        # セクション見出しなし → 全体を "price" 扱い（後方互換）
        return _clean(parts[0]) if section.lower() == "price" else ""

    for i in range(1, len(parts), 2):
        if parts[i].lower() == section.lower():
            return _clean(parts[i + 1] if i + 1 < len(parts) else "")
    return ""
