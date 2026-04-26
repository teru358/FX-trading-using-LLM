"""MT5 ブリッジ — Windows 側で動く FastAPI サービス。

stick PC (Linux) で動く finance daemon が、main PC に常駐する MT5 +
MetaTrader5 Python パッケージへ HTTP でアクセスするための薄いラッパー。

Phase 1+2 (このコミット): heartbeat + 読み取り系 endpoint のみ実装。
発注機能は意図的に未実装 (資金 0 の本番口座保護)。
"""
