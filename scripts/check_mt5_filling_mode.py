"""OANDA-MT5 サーバーが対応する filling mode を確認するワンショット。

Phase 3b タスク 1: ブリッジ /order の live 実装で使う filling mode を確定するため、
main PC (Windows) で 1 度だけ実行する。

実行:
    cd C:\\Users\\terus\\mt5-bridge
    uv run python ..\\Project\\finance\\scripts\\check_mt5_filling_mode.py

期待結果 (推測):
    USDJPY: filling_mode=2 → ['IOC']
    EURUSD: filling_mode=2 → ['IOC']

    → IOC のみなら以降のタスクは IOC 固定。FOK も使えるなら fallback 候補にする。
"""
import MetaTrader5 as mt5

if not mt5.initialize():
    raise SystemExit(f"initialize failed: {mt5.last_error()}")

# filling_mode は bit field:
#   bit 0 (1) = ORDER_FILLING_FOK
#   bit 1 (2) = ORDER_FILLING_IOC
#   bit 2 (4) = ORDER_FILLING_RETURN
for symbol in ["USDJPY", "EURUSD"]:
    info = mt5.symbol_info(symbol)
    if info is None:
        print(f"{symbol}: not found")
        continue
    fm = info.filling_mode
    modes = []
    if fm & 1:
        modes.append("FOK")
    if fm & 2:
        modes.append("IOC")
    if fm & 4:
        modes.append("RETURN")
    print(f"{symbol}: filling_mode={fm} → {modes}")
    print(f"  digits={info.digits} point={info.point} spread={info.spread}")
    print(f"  volume_min={info.volume_min} volume_step={info.volume_step}")

mt5.shutdown()
