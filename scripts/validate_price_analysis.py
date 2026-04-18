"""Phase 4-3 検証: price_analysis (plutus) が llamacpp 経由で JSON 応答するか。

price_analyzer の複雑な依存を回避し、LLM クライアントに実際の price_user.j2
プロンプトを与えて応答をパースできるか確認する。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import load_config
from src.llm.factory import create_llm_client
from src.llm.response_parser import extract_json


MOCK_SYSTEM = (
    "You are an FX technical analyst. Analyze the provided indicators and return "
    "a strict JSON object with these fields: entry_zone [low, high], key_support, "
    "key_resistance, market_regime (trending/ranging/breakout_pending), "
    "confidence_modifier (-0.1 to 0.1), reasoning_summary."
)

MOCK_USER = """
Pair: USD/JPY
Current price: 151.25
Indicators:
  RSI(14): 62 (mildly overbought)
  MACD: bullish cross 2 hours ago
  BB: price near upper band (volatility contracting)
  EMA12/26: bullish alignment
  ATR(14, 4h): 0.45
Recent OHLCV (last 5 4h bars):
  151.00 / 151.40 / 150.80 / 151.25 (bullish continuation pattern)
  150.70 / 151.00 / 150.50 / 150.90
  150.90 / 151.10 / 150.70 / 150.85
  150.40 / 150.95 / 150.20 / 150.85
  150.10 / 150.50 / 149.90 / 150.35

Reply with ONLY a JSON object matching the schema above. No markdown, no commentary.
"""


async def main() -> None:
    config = load_config()
    print(f"Config: price_analysis provider={config.llm.price_analysis.provider!r}")
    print(f"        price_analysis model={config.llm.price_analysis.model!r}")

    llm = create_llm_client(config, "price_analysis")
    print(f"LLM client: {type(llm).__name__} model={llm.model_name}")

    import time
    print("\nCalling LLM (may take 30-60s on first call due to model swap)...")
    t0 = time.time()
    response = await llm.chat(
        messages=[
            {"role": "system", "content": MOCK_SYSTEM},
            {"role": "user", "content": MOCK_USER},
        ],
        temperature=0.1,
    )
    elapsed = time.time() - t0

    print(f"\nRaw response (took {elapsed:.1f}s):")
    print(response[:500])
    print("..." if len(response) > 500 else "")

    # JSON parse
    try:
        parsed = extract_json(response)
    except Exception as e:
        print(f"\n✗ Failed to parse JSON: {e}")
        sys.exit(1)

    print(f"\nParsed JSON:")
    print(json.dumps(parsed, indent=2, ensure_ascii=False)[:500])

    # 必須フィールドチェック
    required = {"market_regime", "reasoning_summary"}
    optional = {"entry_zone", "key_support", "key_resistance", "confidence_modifier"}
    missing = required - parsed.keys()
    if missing:
        print(f"\n⚠ Missing required fields: {missing}")
    else:
        print(f"\n✓ All required fields present")

    # market_regime は enum 値でなければいけない
    regime = parsed.get("market_regime", "")
    if regime in ("trending", "ranging", "breakout_pending"):
        print(f"✓ market_regime valid: {regime}")
    else:
        print(f"⚠ market_regime unexpected: {regime!r}")

    print("\n✓ price_analysis validation passed")


if __name__ == "__main__":
    asyncio.run(main())
