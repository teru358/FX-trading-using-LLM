"""MetaTrader5 Python パッケージの薄いラッパー。

read-only 操作のみ実装。発注・ポジション操作は意図的に未実装。

`MetaTrader5` は Windows でのみ pip install できるため、Linux 上でも
import エラーにならないよう lazy import + skeleton stub を提供する
(open-source 化や CI 上での skeleton チェック用)。
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


def _import_mt5():
    """Windows のみ MetaTrader5 を import。それ以外は ImportError を投げる。"""
    if sys.platform != "win32":
        raise ImportError(
            "MetaTrader5 package is Windows-only. "
            "Run this bridge on Windows (or via Wine, unsupported)."
        )
    import MetaTrader5 as mt5  # noqa: PLC0415 — lazy import 必須
    return mt5


@dataclass
class AccountInfo:
    login: int
    server: str
    currency: str
    balance: float
    equity: float
    margin: float
    free_margin: float
    leverage: int
    name: str
    trade_mode: int  # 0=demo, 1=contest, 2=real (MT5 仕様)


@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float
    spread_points: int
    time: str  # ISO 8601


@dataclass
class Position:
    ticket: int
    symbol: str
    type: str          # "buy" | "sell"
    volume: float
    price_open: float
    price_current: float
    sl: float
    tp: float
    profit: float
    swap: float
    magic: int
    comment: str
    time: str          # ISO 8601


class Mt5Client:
    """MT5 ターミナルへの接続を保持し、read-only 照会だけを提供する。"""

    def __init__(self, login: int, password: str, server: str) -> None:
        self._login = login
        self._password = password
        self._server = server
        self._mt5: Any = None  # MetaTrader5 module
        self._connected = False

    def connect(self) -> None:
        """MT5 ターミナルを起動 (or 既起動なら attach) し、ログインする。"""
        self._mt5 = _import_mt5()
        if not self._mt5.initialize():
            err = self._mt5.last_error()
            raise RuntimeError(f"MT5 initialize() failed: {err}")
        if not self._mt5.login(login=self._login, password=self._password,
                               server=self._server):
            err = self._mt5.last_error()
            self._mt5.shutdown()
            raise RuntimeError(f"MT5 login failed: {err}")
        self._connected = True
        logger.info(f"MT5 connected: login={self._login} server={self._server}")

    def disconnect(self) -> None:
        if self._mt5 is not None and self._connected:
            self._mt5.shutdown()
            self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def ping(self) -> bool:
        """terminal_info() で生存確認。"""
        if not self._connected:
            return False
        try:
            info = self._mt5.terminal_info()
            return info is not None
        except Exception:  # noqa: BLE001
            return False

    def get_account(self) -> AccountInfo:
        info = self._mt5.account_info()
        if info is None:
            raise RuntimeError(f"account_info() failed: {self._mt5.last_error()}")
        return AccountInfo(
            login=info.login,
            server=info.server,
            currency=info.currency,
            balance=info.balance,
            equity=info.equity,
            margin=info.margin,
            free_margin=info.margin_free,
            leverage=info.leverage,
            name=info.name,
            trade_mode=info.trade_mode,
        )

    def get_quote(self, symbol: str) -> Quote:
        # symbol_info_tick がスプレッド込みの最新 bid/ask を返す
        if not self._mt5.symbol_select(symbol, True):
            raise RuntimeError(f"symbol_select({symbol}) failed: {self._mt5.last_error()}")
        tick = self._mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"symbol_info_tick({symbol}) failed: {self._mt5.last_error()}")
        info = self._mt5.symbol_info(symbol)
        spread = int(info.spread) if info is not None else 0
        from datetime import datetime, timezone
        ts = datetime.fromtimestamp(tick.time, tz=timezone.utc).isoformat()
        return Quote(
            symbol=symbol, bid=float(tick.bid), ask=float(tick.ask),
            spread_points=spread, time=ts,
        )

    def get_positions(self) -> list[Position]:
        positions = self._mt5.positions_get()
        if positions is None:
            return []
        from datetime import datetime, timezone
        result: list[Position] = []
        for p in positions:
            # MT5 type: 0=buy, 1=sell
            ptype = "buy" if p.type == 0 else "sell"
            ts = datetime.fromtimestamp(p.time, tz=timezone.utc).isoformat()
            result.append(Position(
                ticket=p.ticket, symbol=p.symbol, type=ptype, volume=p.volume,
                price_open=p.price_open, price_current=p.price_current,
                sl=p.sl, tp=p.tp, profit=p.profit, swap=p.swap,
                magic=p.magic, comment=p.comment, time=ts,
            ))
        return result

    def get_symbols(self) -> list[str]:
        symbols = self._mt5.symbols_get()
        if symbols is None:
            return []
        return [s.name for s in symbols]
