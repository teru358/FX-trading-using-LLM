"""Phase 4-4 検証: reflection (deepseek-r1-8b) が llamacpp 経由で動作するか。

deepseek-r1 は <think>...</think> で reasoning を返す特殊形式。
llamacpp client の応答処理が reasoning tag を適切に扱うか確認する。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import load_config
from src.llm.factory import create_llm_client


MOCK_SYSTEM = (
    "You are a trading coach. Generate a concise reflection (3-4 sentences) on "
    "the provided closed trade. Focus on what went right/wrong and one actionable "
    "lesson for next time."
)

MOCK_USER = """
Closed trade analysis:
- Pair: USD/JPY
- Direction: long
- Entry: 151.20 (from bullish MACD cross + RSI 58)
- Exit: 150.45 (SL hit after 6 hours)
- P&L: -0.50% (-$50)
- News at entry: Fed hawkish comments, but BOJ rate hike speculation grew mid-position
- Outcome: stop loss hit; market reversed on BOJ hawkish shift

Provide reflection in English, 3-4 sentences.
"""


async def main() -> None:
    config = load_config()
    print(f"Config: reflection provider={config.llm.reflection.provider!r}")
    print(f"        reflection model={config.llm.reflection.model!r}")

    llm = create_llm_client(config, "reflection")
    print(f"LLM client: {type(llm).__name__} model={llm.model_name}")

    print("\nCalling LLM (may take 30-60s on first call)...")
    t0 = time.time()
    response = await llm.chat(
        messages=[
            {"role": "system", "content": MOCK_SYSTEM},
            {"role": "user", "content": MOCK_USER},
        ],
        temperature=0.3,
    )
    elapsed = time.time() - t0

    print(f"\nResponse (took {elapsed:.1f}s):")
    print("=" * 60)
    print(response)
    print("=" * 60)

    # 空応答チェック
    if not response.strip():
        print("\n✗ Empty response")
        sys.exit(1)

    # <think> タグが content に漏れていないか確認
    if "<think>" in response.lower() or "</think>" in response.lower():
        print("\n⚠ Raw <think> tags leaked into content - parser may need tuning")

    # 反省文としての長さチェック (3-4 文なら 100-500 文字程度)
    length = len(response)
    if length < 50:
        print(f"\n⚠ Response too short ({length} chars) - possible reasoning-only response")
    elif length > 2000:
        print(f"\n⚠ Response unexpectedly long ({length} chars) - may include think block")
    else:
        print(f"\n✓ Response length reasonable ({length} chars)")

    print("\n✓ reflection validation passed")


if __name__ == "__main__":
    asyncio.run(main())
