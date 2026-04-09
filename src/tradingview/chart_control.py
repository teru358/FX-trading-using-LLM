"""TradingViewチャート操作（シンボル変更等）。"""
from __future__ import annotations

import asyncio
import json
import logging

from src.tradingview.cdp_client import CDPClient

logger = logging.getLogger(__name__)

_SYMBOL_MAP = {
    "USDJPY=X": "FX:USDJPY",
    "EURUSD=X": "FX:EURUSD",
    "GBPUSD=X": "FX:GBPUSD",
}


def to_tv_symbol(yf_symbol: str) -> str:
    """yfinanceシンボルをTradingView形式に変換する。"""
    return _SYMBOL_MAP.get(yf_symbol, yf_symbol)


def to_tv_ticker(yf_symbol: str) -> str:
    """yfinanceシンボルからTradingView ticker部分を返す（syminfo.ticker用）。

    例: "USDJPY=X" → "USDJPY", "EURUSD=X" → "EURUSD"
    """
    tv = to_tv_symbol(yf_symbol)
    # "FX:USDJPY" → "USDJPY"
    return tv.split(":")[-1] if ":" in tv else tv


class ChartControl:
    """TradingViewチャートの操作を行う。"""

    def __init__(self, cdp: CDPClient) -> None:
        self._cdp = cdp

    async def set_symbol(self, symbol: str) -> bool:
        """チャートのシンボルを変更する。"""
        tv_sym = to_tv_symbol(symbol)
        escaped = json.dumps(tv_sym)
        await self._cdp.evaluate(f"""
            (function() {{
                var chart = window.TradingViewApi._activeChartWidgetWV.value();
                chart.setSymbol({escaped}, {{}});
            }})()
        """, await_promise=False)

        for _ in range(25):
            await asyncio.sleep(0.2)
            ready = await self._cdp.evaluate("""
                (function() {
                    var spinner = document.querySelector('[class*="loader"]')
                        || document.querySelector('[data-name="loading"]');
                    return !(spinner && spinner.offsetParent !== null);
                })()
            """)
            if ready:
                logger.info(f"[TV] Symbol changed to {tv_sym}")
                return True
        return False

    async def get_symbol(self) -> str | None:
        """現在のチャートシンボルを取得する。"""
        return await self._cdp.evaluate("""
            (function() {
                try {
                    return window.TradingViewApi._activeChartWidgetWV.value().symbol();
                } catch(e) { return null; }
            })()
        """)
