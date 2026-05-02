"""mt5_bridge への HTTP クライアント実装の BrokerAdapter。

DRY_RUN は bridge 側のフラグで制御するので、このアダプターは常に同じ呼び出し方を行う。
Phase 3a では `Mt5BridgeBrokerAdapter` 単独でも shadow モード経由でも使える。
Phase 3b で MT5 真実 (/positions) との reconciliation を実装。
"""
from __future__ import annotations

import logging
import time as _time

import httpx

from src.signals.scale_in import PreExecResult, evaluate_pre_execution_checks
from src.signals.signal_combiner import TradeSignal
from src.trading.broker_adapter import BrokerAdapter
from src.trading.position_manager import Order, PositionManager
from src.trading.symbol_mapping import to_mt5_symbol

logger = logging.getLogger(__name__)

# Phase 3b reconciliation: volume 差の閾値
_PARTIAL_THRESHOLD_LOW = 0.01      # 1%: 端数 / 通常 partial の境界
_PARTIAL_THRESHOLD_HIGH = 0.30     # 30%: 通常 / 異常 の境界


class Mt5BridgeBrokerAdapter(BrokerAdapter):
    """mt5_bridge (Windows main PC) 経由で MT5 に発注するアダプター。

    Phase 3a では bridge 側 DRY_RUN モードを前提とし、実際の発注は MT5 に届かない。
    `check_and_close_positions` は Phase 3a では paper と同じローカル SL/TP 判定を
    使う (shadow 比較を apples-to-apples にするため)。Phase 3b で MT5 真実
    との reconciliation に置き換える。
    """

    def __init__(
        self,
        bridge_url: str,
        api_key: str = "",
        request_timeout_seconds: float = 10.0,
        lot_size_units: int = 100_000,
        magic_number: int = 12345,
        max_positions_per_pair: int = 2,
        scale_in_enabled: bool = False,
        scale_in_conf_margin: float = 0.05,
        scale_in_score_margin: float = 0.05,
        drawdown_kill_switch_enabled: bool = False,
        drawdown_kill_switch_max_pct: float = 0.10,
        drawdown_kill_switch_lookback_days: int = 0,
        notifier=None,
    ) -> None:
        if not bridge_url:
            raise ValueError("bridge_url is required")
        self._url = bridge_url.rstrip("/")
        self._api_key = api_key
        self._timeout = request_timeout_seconds
        self._lot_units = lot_size_units
        self._magic = magic_number
        self._max_per_pair = max_positions_per_pair
        self._scale_in_enabled = scale_in_enabled
        self._scale_in_conf_margin = scale_in_conf_margin
        self._scale_in_score_margin = scale_in_score_margin
        self._dd_enabled = drawdown_kill_switch_enabled
        self._dd_max_pct = drawdown_kill_switch_max_pct
        self._dd_lookback = drawdown_kill_switch_lookback_days
        # Phase 3b: reconciliation 用キャッシュ + 通知
        self._notifier = notifier
        self._cached_external_positions: list[dict] = []

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["X-Bridge-Api-Key"] = self._api_key
        return h

    def execute_signal(
        self, signal: TradeSignal, position_mgr: PositionManager,
        macro_context: str = "",
    ) -> Order | None:
        if signal.action == "hold":
            return None

        direction = "buy" if signal.action == "buy" else "sell"

        # 発注前チェック (ペア上限 / scale-in / drawdown kill switch)
        result = evaluate_pre_execution_checks(
            signal=signal,
            position_mgr=position_mgr,
            max_positions_per_pair=self._max_per_pair,
            scale_in_enabled=self._scale_in_enabled,
            scale_in_conf_margin=self._scale_in_conf_margin,
            scale_in_score_margin=self._scale_in_score_margin,
            drawdown_kill_switch_enabled=self._dd_enabled,
            drawdown_kill_switch_max_pct=self._dd_max_pct,
            drawdown_kill_switch_lookback_days=self._dd_lookback,
        )
        if result.status == "skip":
            logger.info(f"[MT5_BRIDGE] [SKIP] {signal.pair}: {result.reason}")
            return None

        is_scale_in = result.status == "scale_in"

        # 発注 payload (送信時は MT5 形式に変換)
        payload = {
            "symbol": to_mt5_symbol(signal.pair),
            "side": direction,
            "volume_lots": signal.position_size / self._lot_units,
            "sl": signal.stop_loss,
            "tp": signal.take_profit,
            "magic": self._magic,
            "comment": signal.signal_reason[:32],
        }
        requested_lots = payload["volume_lots"]
        try:
            resp = httpx.post(
                f"{self._url}/order", json=payload,
                timeout=self._timeout, headers=self._headers(),
            )
        except httpx.HTTPError as e:
            logger.error(f"[MT5_BRIDGE] bridge order failed: {e}")
            return None
        except Exception as e:  # noqa: BLE001
            logger.error(f"[MT5_BRIDGE] unexpected error: {e}", exc_info=True)
            return None

        # ── HTTP status 分岐 ──
        if resp.status_code == 422:
            try:
                err = resp.json()
            except Exception:  # noqa: BLE001
                err = {"detail": resp.text}
            logger.warning(
                f"[MT5_BRIDGE] {signal.pair} insufficient margin: {err}"
            )
            self._notify_margin_insufficient(signal, err)
            return None
        if resp.status_code == 423:
            logger.info(f"[MT5_BRIDGE] {signal.pair} skipped — bridge soft-halted")
            return None
        if resp.status_code == 409:
            logger.warning(f"[MT5_BRIDGE] {signal.pair} order rejected: {resp.text}")
            return None
        if resp.status_code in (503, 504):
            logger.error(
                f"[MT5_BRIDGE] {signal.pair} bridge unavailable: {resp.status_code}"
            )
            return None
        if resp.status_code != 200:
            logger.error(
                f"[MT5_BRIDGE] unexpected response {resp.status_code}: {resp.text}"
            )
            return None

        data = resp.json()
        actual_lots = float(data["volume_lots"])
        partial = actual_lots < requested_lots * 0.99    # 1% 以上少なければ部分約定

        # 部分約定なら position_size を実約定量に修正
        actual_size = signal.position_size * (actual_lots / requested_lots) if requested_lots > 0 else signal.position_size

        # bridge ticket を order_id に組み込む (paper UUID と区別)。
        # pair は内部正規形 (signal.pair = "USDJPY=X") のまま保持。
        order = Order.new(
            pair=signal.pair, direction=direction,
            entry_price=float(data["fill_price"]),
            stop_loss=signal.stop_loss, take_profit=signal.take_profit,
            position_size=actual_size,
            signal_reason=signal.signal_reason,
            macro_context_at_entry=macro_context,
            open_confidence=signal.confidence,
            open_score=signal.combined_score,
            is_scale_in=is_scale_in,
        )
        order.order_id = f"mt5:{data['ticket']}"
        position_mgr.open_position(order)
        ticket = data["ticket"]

        if partial:
            logger.warning(
                f"[MT5_BRIDGE] partial fill: {signal.pair} requested={requested_lots:.4f} "
                f"actual={actual_lots:.4f} ({actual_lots/requested_lots*100:.0f}%)"
            )
            self._notify_partial_fill(order, requested_lots, actual_lots)
        elif is_scale_in and result.decision is not None:
            logger.info(
                f"[SCALE] [mt5_bridge] {signal.pair} {direction.upper()} ticket={ticket} | "
                f"new conf={signal.confidence:.3f} score={signal.combined_score:+.3f} | "
                f"prev_max conf={result.decision.prev_max_conf:.3f} "
                f"score={result.decision.prev_max_abs_score:.3f}"
            )
        else:
            logger.info(
                f"[ORDER] [mt5_bridge] {signal.pair} {direction.upper()} ticket={ticket} | "
                f"fill={data['fill_price']} dry_run={data.get('dry_run')}"
            )
        return order

    def _notify_partial_fill(
        self, order: Order, requested_lots: float, actual_lots: float,
    ) -> None:
        """部分約定の Discord 通知 (タスク 13 で notifier 統合)。"""
        # Phase 3b 暫定: 警告ログのみ (タスク 13 で _notifier 経由に置換)
        pass

    def _notify_margin_insufficient(self, signal, error_data: dict) -> None:
        """証拠金不足の Discord 通知 (タスク 13 で notifier 統合)。"""
        pass

    def _fetch_mt5_positions(self) -> list[dict] | None:
        """bridge /positions から最新ポジ一覧を取得。失敗時は None。"""
        try:
            resp = httpx.get(
                f"{self._url}/positions",
                timeout=self._timeout, headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.warning(f"[MT5_BRIDGE] /positions fetch failed: {e}")
            return None

    def safe_close(self, ticket: int, max_retries: int = 3) -> bool:
        """close 指令 → 1秒待機 → ポジション確認 → 残ってれば retry。

        全 retry 失敗で hard halt を発動。bridge 不通時は retry を中断して
        reconciliation に委譲 (戻り値 False)。
        """
        for attempt in range(max_retries):
            try:
                httpx.post(
                    f"{self._url}/positions/{ticket}/close",
                    timeout=self._timeout, headers=self._headers(),
                )
            except httpx.HTTPError as e:
                logger.warning(f"[CLOSE] attempt {attempt+1} HTTP error: {e}")
                if attempt == max_retries - 1:
                    self._trigger_hard_halt(
                        f"close failed (HTTP) ticket={ticket} after "
                        f"{max_retries} retries: {e}"
                    )
                    return False
                _time.sleep(2 ** attempt)
                continue

            _time.sleep(1.0)    # MT5 側の処理反映待ち

            positions = self._fetch_mt5_positions()
            if positions is None:
                logger.warning(
                    f"[CLOSE] verify: bridge unreachable, "
                    f"defer to reconcile (ticket={ticket})"
                )
                return False

            if ticket not in {int(p["ticket"]) for p in positions}:
                logger.info(f"[CLOSE] verified: ticket={ticket} closed")
                return True

            logger.warning(
                f"[CLOSE] ticket={ticket} still open, "
                f"retry {attempt+1}/{max_retries}"
            )
            _time.sleep(2 ** attempt)

        self._trigger_hard_halt(
            f"close failed: ticket={ticket} still open after {max_retries} retries"
        )
        return False

    def _trigger_hard_halt(self, reason: str) -> None:
        """bridge /admin/halt mode=hard を叩く + Discord 通知。"""
        try:
            httpx.post(
                f"{self._url}/admin/halt",
                json={"mode": "hard", "reason": f"auto: {reason}"},
                timeout=self._timeout, headers=self._headers(),
            )
            logger.error(f"[MT5_BRIDGE] AUTO HARD HALT: {reason}")
            if self._notifier:
                self._notifier.send_embed(
                    title="🛑 MT5 自動 HARD HALT",
                    description=(
                        f"reason: {reason}\n\n"
                        f"⚠️ 自動再開不可。main PC で手動操作必須:\n"
                        f"1. mt5_bridge/.env で `DRY_RUN=true` を確認\n"
                        f"2. logs/hard_halt.flag を削除\n"
                        f"3. bridge 再起動"
                    ),
                    color=0xC0392B,
                )
        except Exception as e:
            logger.error(f"[MT5_BRIDGE] auto hard halt API failed: {e}")

    def check_and_close_positions(
        self, open_positions: list[Order],
        current_prices: dict[str, float],
        position_mgr: PositionManager,
    ) -> list[Order]:
        """Phase 3b reconciliation: MT5 を真実として 4 種の不整合を分類処理。

        - 完全 close (内部 open, MT5 不在): 内部 close (server_sl_tp)
        - 部分 close (1% 未満): 端数 → 無視
        - 部分 close (1-30%): adjust_position_size
        - 部分 close (30% 以上): hard halt
        - Orphan (bot magic, 内部 state 無し): hard halt
        - External (他 magic): キャッシュに保持、干渉せず
        - bridge 不通: ノーオペ
        """
        mt5_positions = self._fetch_mt5_positions()
        if mt5_positions is None:
            logger.warning("[MT5_BRIDGE] reconciliation skipped — bridge unreachable")
            return []

        # MT5 side: bot magic / 他 magic で分離
        bot_mt5 = {
            int(p["ticket"]): float(p["volume"])
            for p in mt5_positions if int(p.get("magic", 0)) == self._magic
        }
        self._cached_external_positions = [
            p for p in mt5_positions
            if int(p.get("magic", 0)) != self._magic
        ]
        if self._cached_external_positions:
            logger.info(
                f"[RECONCILE] external positions detected: "
                f"{len(self._cached_external_positions)} (display only, no interference)"
            )

        # Internal side: bot magic ポジ抽出 (mt5: prefix が目印)
        internal_bot = {
            pos.order_id: pos for pos in open_positions
            if pos.order_id.startswith("mt5:")
        }
        internal_tickets: set[int] = set()
        for pos in internal_bot.values():
            try:
                internal_tickets.add(int(pos.order_id.removeprefix("mt5:")))
            except ValueError:
                continue

        # Orphan 検出 (MT5 にあるが内部に無い、bot magic)
        orphans = set(bot_mt5.keys()) - internal_tickets
        if orphans:
            self._trigger_hard_halt(
                f"orphan positions detected: tickets={sorted(orphans)} "
                f"(bot magic but no internal record)"
            )
            return []

        closed: list[Order] = []
        for pos in list(internal_bot.values()):
            try:
                ticket = int(pos.order_id.removeprefix("mt5:"))
            except ValueError:
                continue
            internal_lots = pos.position_size / self._lot_units

            # ── パターン 1: 完全 close 検出 ──
            if ticket not in bot_mt5:
                close_price = current_prices.get(pos.pair, pos.entry_price)
                logger.info(
                    f"[RECONCILE] full close: {pos.pair} ticket={ticket} "
                    f"@ {close_price} (server-side close detected)"
                )
                closed_order = position_mgr.close_position(
                    pos.order_id, close_price, "server_sl_tp",
                )
                if closed_order:
                    closed.append(closed_order)
                continue

            # ── パターン 2-4: volume 差で分岐 ──
            mt5_lots = bot_mt5[ticket]
            if mt5_lots >= internal_lots:
                continue    # 内部 ≤ MT5 = 一致 (or 異常な増加だが auto-correct しない)

            diff_pct = (internal_lots - mt5_lots) / internal_lots

            if diff_pct < _PARTIAL_THRESHOLD_LOW:
                continue    # 端数 / 浮動小数点ノイズ → 無視

            if diff_pct >= _PARTIAL_THRESHOLD_HIGH:
                # 異常な大量消失 → hard halt
                self._trigger_hard_halt(
                    f"volume mismatch {diff_pct*100:.0f}%: ticket={ticket} "
                    f"internal={internal_lots:.4f} mt5={mt5_lots:.4f}"
                )
                return closed    # 以降の処理は中断

            # 通常 partial close → 内部 state 修正
            new_size = pos.position_size * (mt5_lots / internal_lots)
            logger.warning(
                f"[RECONCILE] partial close: {pos.pair} ticket={ticket} "
                f"internal_lots={internal_lots:.4f} mt5_lots={mt5_lots:.4f} "
                f"diff={diff_pct*100:.1f}% → adjust to {new_size:.2f} unit"
            )
            position_mgr.adjust_position_size(
                pos.order_id, new_size, reason="reconcile_partial_close",
            )
            self._notify_partial_close(pos, internal_lots, mt5_lots, new_size)

        return closed

    def _notify_partial_close(
        self, pos: Order, internal_lots: float, mt5_lots: float, new_size: float,
    ) -> None:
        """部分 close 検知の Discord 通知 (内部 state 修正済み)。タスク 13 で本格実装。"""
        # Phase 3b 暫定: 警告ログのみ (タスク 13 で _notifier 経由に置換)
        pass
