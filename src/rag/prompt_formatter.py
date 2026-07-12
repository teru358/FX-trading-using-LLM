from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config import InstrumentConfig
    from src.data.analysis_store import _TechnicalSnapshot


def format_macro_context_for_prompt(
    snapshots: list["_TechnicalSnapshot"],
    instruments: list["InstrumentConfig"],
    realtime_provider: str = "yfinance",
) -> str:
    """監視専用銘柄（指数・参照FX等）のスナップショットをマクロコンテキストに整形する。"""
    if not snapshots:
        return ""
    name_map = {i.symbol: i.display_name for i in instruments}
    lines = [
        "=== Macro Reference Instruments ===",
        "Note: Rising equity indices generally indicate risk-on sentiment.",
    ]
    if realtime_provider != "yfinance":
        lines.append(
            "(注意: 以下の監視銘柄はyfinance経由のため最大15-20分の遅延あり。"
            "取引ペアはリアルタイムデータ。相関判断時は時間差を考慮すること)"
        )
    for snap in snapshots:
        ts = snap.analyzed_at.strftime("%Y-%m-%d %H:%M") if snap.analyzed_at else "?"
        name = name_map.get(snap.symbol, snap.symbol)
        source = "yfinance, ~15min delay" if realtime_provider != "yfinance" else ""
        suffix = f"  ({source})" if source else ""
        lines.append(
            f"[{ts}] {name:16s} direction={snap.direction_bias}, "
            f"bias={snap.bias_score:+.2f}, confidence={snap.confidence:.2f}{suffix}"
        )
    return "\n".join(lines)
