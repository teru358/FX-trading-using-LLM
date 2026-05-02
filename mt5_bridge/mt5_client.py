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

    def place_order_dry_run(
        self, symbol: str, side: str, volume_lots: float,
        sl: float | None = None, tp: float | None = None,
        magic: int = 0, comment: str = "",
    ) -> dict:
        """発注をシミュレート (MT5 には送らない)。
        現在の bid/ask を fill_price として使い、time_ns で擬似 ticket を生成する。
        """
        import time as _time
        from datetime import datetime, timezone

        if not self._mt5.symbol_select(symbol, True):
            raise RuntimeError(
                f"symbol_select({symbol}) failed: {self._mt5.last_error()}"
            )
        tick = self._mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(
                f"symbol_info_tick({symbol}) failed: {self._mt5.last_error()}"
            )
        fill_price = float(tick.ask if side == "buy" else tick.bid)
        ticket = _time.time_ns() // 1_000  # μs 解像度の擬似 ticket
        ts = datetime.now(tz=timezone.utc).isoformat()
        logger.info(
            f"[DRY_RUN] order: {side} {symbol} lot={volume_lots} "
            f"@ {fill_price} sl={sl} tp={tp} magic={magic} ticket={ticket}"
        )
        return {
            "ticket": ticket, "symbol": symbol, "side": side,
            "volume_lots": volume_lots, "fill_price": fill_price,
            "sl": sl, "tp": tp, "time": ts,
            "dry_run": True, "magic": magic,
        }

    def close_position_dry_run(
        self, ticket: int, symbol: str | None = None,
    ) -> dict:
        """ポジション close をシミュレート。

        DRY_RUN ではポジション ticket は擬似値なので MT5 の positions_get には存在しない。
        symbol が指定されていれば対向 tick の中値を close_price として返す。
        symbol 未指定なら 0.0 を返す (シャドウ用途では adapter 側で symbol を保持)。
        """
        from datetime import datetime, timezone

        close_price = 0.0
        if symbol:
            tick = self._mt5.symbol_info_tick(symbol)
            if tick is not None:
                close_price = float((tick.bid + tick.ask) / 2)
        ts = datetime.now(tz=timezone.utc).isoformat()
        logger.info(
            f"[DRY_RUN] close: ticket={ticket} symbol={symbol} @ {close_price}"
        )
        return {
            "ticket": ticket, "close_price": close_price, "time": ts,
            "dry_run": True, "note": "DRY_RUN: ticket not validated",
        }

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

    def calc_required_margin(
        self, symbol: str, side: str, volume_lots: float, price: float,
    ) -> float:
        """MT5 内蔵関数で必要証拠金を計算 (口座通貨建て、通貨換算込み)。"""
        action = (
            self._mt5.ORDER_TYPE_BUY if side == "buy" else self._mt5.ORDER_TYPE_SELL
        )
        margin = self._mt5.order_calc_margin(action, symbol, volume_lots, price)
        if margin is None:
            raise RuntimeError(
                f"order_calc_margin failed: {self._mt5.last_error()}"
            )
        return float(margin)

    def place_order_live(
        self, symbol: str, side: str, volume_lots: float,
        sl: float | None = None, tp: float | None = None,
        magic: int = 0, comment: str = "",
        filling_mode: str = "IOC",
        deviation_points: int = 30,
    ) -> dict:
        """MT5 へ実発注。retcode 解釈は呼出側 (server.py) で行う。"""
        from datetime import datetime, timezone

        if not self._mt5.symbol_select(symbol, True):
            raise RuntimeError(
                f"symbol_select({symbol}) failed: {self._mt5.last_error()}"
            )
        tick = self._mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(
                f"symbol_info_tick({symbol}) failed: {self._mt5.last_error()}"
            )
        price = float(tick.ask if side == "buy" else tick.bid)

        fm_map = {
            "IOC": self._mt5.ORDER_FILLING_IOC,
            "FOK": self._mt5.ORDER_FILLING_FOK,
            "RETURN": self._mt5.ORDER_FILLING_RETURN,
        }
        type_filling = fm_map.get(filling_mode.upper(), self._mt5.ORDER_FILLING_IOC)
        order_type = (
            self._mt5.ORDER_TYPE_BUY if side == "buy" else self._mt5.ORDER_TYPE_SELL
        )

        request = {
            "action": self._mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume_lots,
            "type": order_type,
            "price": price,
            "deviation": deviation_points,
            "magic": magic,
            "comment": comment,
            "type_time": self._mt5.ORDER_TIME_GTC,
            "type_filling": type_filling,
        }
        if sl is not None and sl > 0:
            request["sl"] = sl
        if tp is not None and tp > 0:
            request["tp"] = tp

        result = self._mt5.order_send(request)
        if result is None:
            raise RuntimeError(f"order_send returned None: {self._mt5.last_error()}")

        ts = datetime.now(tz=timezone.utc).isoformat()
        return {
            "retcode": int(result.retcode),
            "ticket": int(result.order),
            "symbol": symbol,
            "side": side,
            "volume_lots": float(result.volume),
            "fill_price": float(result.price) if result.price > 0 else price,
            "sl": sl, "tp": tp,
            "time": ts,
            "dry_run": False,
            "magic": magic,
            "comment_response": str(result.comment),
        }
