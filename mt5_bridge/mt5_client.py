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


@dataclass
class ClosedDeal:
    ticket: int
    close_price: float
    profit: float
    swap: float
    commission: float
    closed_at: str
    reason: str


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

    def get_closed_deal(self, ticket: int) -> ClosedDeal | None:
        """指定 position/order ticket に紐づく close deal 実績を返す。

        server-side SL/TP で position が消えた後の reconciliation で、検知時点の
        current price ではなく MT5 の実決済価格・実現損益を使うための参照。
        """
        from datetime import datetime, timezone

        try:
            deals = self._mt5.history_deals_get(position=ticket)
        except TypeError:
            deals = None
        if not deals:
            return None

        entry_out = {
            getattr(self._mt5, "DEAL_ENTRY_OUT", 1),
            getattr(self._mt5, "DEAL_ENTRY_OUT_BY", 3),
        }
        close_deals = [
            d for d in deals
            if getattr(d, "entry", None) in entry_out
        ]
        if not close_deals:
            return None

        total_volume = sum(abs(float(getattr(d, "volume", 0.0))) for d in close_deals)
        if total_volume > 0:
            close_price = sum(
                float(getattr(d, "price", 0.0))
                * abs(float(getattr(d, "volume", 0.0)))
                for d in close_deals
            ) / total_volume
        else:
            close_price = float(getattr(close_deals[-1], "price", 0.0))

        profit = sum(float(getattr(d, "profit", 0.0)) for d in close_deals)
        swap = sum(float(getattr(d, "swap", 0.0)) for d in close_deals)
        commission = sum(float(getattr(d, "commission", 0.0)) for d in close_deals)
        closed_ts = max(int(getattr(d, "time", 0)) for d in close_deals)
        reason = str(getattr(close_deals[-1], "reason", ""))

        return ClosedDeal(
            ticket=ticket,
            close_price=close_price,
            profit=profit,
            swap=swap,
            commission=commission,
            closed_at=datetime.fromtimestamp(
                closed_ts, tz=timezone.utc,
            ).isoformat(),
            reason=reason,
        )

    def get_symbols(self) -> list[str]:
        symbols = self._mt5.symbols_get()
        if symbols is None:
            return []
        return [s.name for s in symbols]

    def copy_rates_range(
        self, symbol: str, interval: str,
        date_from: Any, date_to: Any,
    ) -> list[dict]:
        """MT5 から OHLCV を取得し dict のリストで返す。

        interval: "1m" | "5m" | "15m" | "30m" | "1h" | "4h" | "1d"
        date_from / date_to: datetime (tz-aware UTC 推奨)
        """
        from datetime import datetime, timezone

        tf_map = {
            "1m":  self._mt5.TIMEFRAME_M1,
            "5m":  self._mt5.TIMEFRAME_M5,
            "15m": self._mt5.TIMEFRAME_M15,
            "30m": self._mt5.TIMEFRAME_M30,
            "1h":  self._mt5.TIMEFRAME_H1,
            "4h":  self._mt5.TIMEFRAME_H4,
            "1d":  self._mt5.TIMEFRAME_D1,
        }
        tf = tf_map.get(interval)
        if tf is None:
            raise ValueError(f"unsupported interval: {interval}")

        if not self._mt5.symbol_select(symbol, True):
            raise RuntimeError(
                f"symbol_select({symbol}) failed: {self._mt5.last_error()}"
            )

        rates = self._mt5.copy_rates_range(symbol, tf, date_from, date_to)
        if rates is None:
            raise RuntimeError(
                f"copy_rates_range failed: {self._mt5.last_error()}"
            )

        bars: list[dict] = []
        for r in rates:
            # rates fields: time, open, high, low, close, tick_volume, spread, real_volume
            ts = datetime.fromtimestamp(int(r["time"]), tz=timezone.utc).isoformat()
            bars.append({
                "time": ts,
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["tick_volume"]),
            })
        return bars

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

    def close_position_live(self, ticket: int) -> dict:
        """MT5 のポジションをクローズ。symbol/volume/type は positions_get から取得。"""
        from datetime import datetime, timezone

        positions = self._mt5.positions_get(ticket=ticket)
        if not positions:
            raise RuntimeError(f"position {ticket} not found")
        p = positions[0]
        close_type = (
            self._mt5.ORDER_TYPE_SELL if p.type == 0 else self._mt5.ORDER_TYPE_BUY
        )
        tick = self._mt5.symbol_info_tick(p.symbol)
        if tick is None:
            raise RuntimeError(f"no tick for {p.symbol}")
        price = float(tick.bid if p.type == 0 else tick.ask)

        request = {
            "action": self._mt5.TRADE_ACTION_DEAL,
            "position": ticket,
            "symbol": p.symbol,
            "volume": p.volume,
            "type": close_type,
            "price": price,
            "deviation": 30,
            "magic": p.magic,
            "comment": "close by bridge",
            "type_time": self._mt5.ORDER_TIME_GTC,
            "type_filling": self._mt5.ORDER_FILLING_IOC,
        }
        result = self._mt5.order_send(request)
        if result is None or result.retcode != self._mt5.TRADE_RETCODE_DONE:
            raise RuntimeError(
                f"close_position {ticket} failed: retcode="
                f"{getattr(result, 'retcode', 'None')}"
            )
        ts = datetime.now(tz=timezone.utc).isoformat()
        return {
            "ticket": ticket,
            "close_price": float(result.price),
            "time": ts,
            "dry_run": False,
            "note": "",
        }

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
