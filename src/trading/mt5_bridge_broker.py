"""mt5_bridge への HTTP クライアント実装の BrokerAdapter。

DRY_RUN は bridge 側のフラグで制御するので、このアダプターは常に同じ呼び出し方を行う。
Phase 3a では `Mt5BridgeBrokerAdapter` 単独でも shadow モード経由でも使える。
Phase 3b で MT5 真実 (/positions) との reconciliation + 通知統合 + 自動 soft halt
判定を実装。
"""
from __future__ import annotations

import logging
import time as _time
from pathlib import Path

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
        notifier=None,
        consecutive_unreachable_threshold: int = 3,
        consecutive_reject_threshold: int = 3,
        state_dir: Path | None = None,
    ) -> None:
        if not bridge_url:
            raise ValueError("bridge_url is required")
        if state_dir is None:
            raise ValueError("state_dir is required (halt state lookup)")
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
        # Phase 3b: reconciliation 用キャッシュ + 通知
        self._notifier = notifier
        self._cached_external_positions: list[dict] = []
        # Phase 3b タスク 14: 発注経路の auto soft halt 判定 (タスク 9 の OHLCV
        # ProviderHealthTracker とは別管理: 目的・閾値・発動アクションが異なる)
        # Phase 3c で時間ベース廃止 → 連続不通 N 回で halt (heartbeat 経路と統一)
        self._unreachable_threshold = consecutive_unreachable_threshold
        self._reject_threshold = consecutive_reject_threshold
        self._consecutive_unreachable = 0
        self._consecutive_rejects = 0
        # finance halt 状態の参照先 (data/state/)
        self._state_dir = state_dir

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

        # finance 側 halt 状態を確認。bridge が halted 中の場合 (heartbeat / order
        # 経路で auto halt 発動済 or 手動 halt 中)、ここで早期 return して bridge
        # への発注を行わない。bridge 不通時にも有効 (halt.json はローカル読出)。
        from src.persistence import halt_state
        if halt_state.is_halted(self._state_dir):
            logger.info(
                f"[MT5_BRIDGE] {signal.pair} skipped — soft-halted (finance state)"
            )
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
            self._record_bridge_unreachable()
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
            return None    # 証拠金不足は bridge 健全 → カウンタ非増加
        if resp.status_code == 423:
            logger.info(f"[MT5_BRIDGE] {signal.pair} skipped — bridge soft-halted")
            return None    # soft halt は意図的 → カウンタ非増加
        if resp.status_code == 409:
            logger.warning(f"[MT5_BRIDGE] {signal.pair} order rejected: {resp.text}")
            self._record_bridge_reject()
            return None
        if resp.status_code in (503, 504):
            logger.error(
                f"[MT5_BRIDGE] {signal.pair} bridge unavailable: {resp.status_code}"
            )
            self._record_bridge_unreachable()
            return None
        if resp.status_code != 200:
            logger.error(
                f"[MT5_BRIDGE] unexpected response {resp.status_code}: {resp.text}"
            )
            return None

        data = resp.json()
        actual_lots = float(data["volume_lots"])
        # 部分約定判定 (reconciliation の _PARTIAL_THRESHOLD_LOW と同じ閾値を共有)
        partial = actual_lots < requested_lots * (1.0 - _PARTIAL_THRESHOLD_LOW)
        self._record_bridge_success()

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
        """部分約定の Discord 通知 (タスク 13)。"""
        if self._notifier is None:
            return
        try:
            self._notifier.send_embed(
                title="⚠️ MT5 部分約定",
                description=(
                    f"**{order.pair}** {order.direction.upper()}\n"
                    f"要求: {requested_lots:.4f} lot\n"
                    f"約定: {actual_lots:.4f} lot ({actual_lots/requested_lots*100:.0f}%)\n"
                    f"fill: {order.entry_price:.5f}\n"
                    f"ticket: `{order.order_id}`"
                ),
                color=0xF39C12,    # 橙
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[MT5_BRIDGE] notify partial fill failed: {e}")

    def _notify_margin_insufficient(self, signal, error_data: dict) -> None:
        """証拠金不足の Discord 通知 (タスク 13)。"""
        if self._notifier is None:
            return
        try:
            self._notifier.send_embed(
                title="⛔ MT5 発注拒否 (証拠金不足)",
                description=(
                    f"**{signal.pair}** {signal.action.upper()} {signal.position_size:.0f} unit\n"
                    f"detail: {error_data.get('detail', 'unknown')}"
                ),
                color=0xE74C3C,    # 赤
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[MT5_BRIDGE] notify margin failed: {e}")

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

    def close_position(
        self,
        order_id: str,
        close_price: float,
        reason: str,
        position_mgr: PositionManager,
    ) -> Order | None:
        """review-based 早期決済 (exit_check 等) 経由で呼ばれる能動 close。

        paper モードと違い、MT5 サーバーへ close 指令を送って確認した上で内部 state
        を更新する必要がある。これを怠ると次サイクルの reconciliation で MT5 側に
        残った同 ticket を orphan として検出し、hard halt が発動する。

        - mt5: prefix のついた order_id (= MT5 ticket 由来) は safe_close で MT5 close
        - paper UUID 由来の order_id (shadow モードで paper 側) はそのまま内部 close
        - safe_close 失敗時は内部 state を変更せず None を返す (reconciliation
          サイクルが MT5 真実を見て後続処理する)
        """
        if not order_id.startswith("mt5:"):
            # paper / shadow primary 由来 → 内部 state 更新のみ
            return position_mgr.close_position(order_id, close_price, reason)
        try:
            ticket = int(order_id.removeprefix("mt5:"))
        except ValueError:
            logger.warning(f"[MT5_BRIDGE] invalid mt5 order_id: {order_id}")
            return None
        if not self.safe_close(ticket):
            logger.warning(
                f"[MT5_BRIDGE] active close skipped — safe_close failed for "
                f"ticket={ticket}, internal state preserved (reconcile will retry)"
            )
            return None
        return position_mgr.close_position(order_id, close_price, reason)

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
        """部分 close 検知の Discord 通知 (タスク 13、内部 state 修正済み)。"""
        if self._notifier is None:
            return
        try:
            self._notifier.send_embed(
                title="⚠️ MT5 部分 close 検知",
                description=(
                    f"**{pos.pair}** ticket {pos.order_id}\n"
                    f"内部記録: {internal_lots:.4f} lot\n"
                    f"MT5 残量: {mt5_lots:.4f} lot\n"
                    f"差分: {internal_lots - mt5_lots:.4f} lot\n"
                    f"※ 自動再 close は Phase 4 実装予定。手動確認推奨。"
                ),
                color=0xE67E22,    # 濃橙
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[MT5_BRIDGE] notify partial close failed: {e}")

    # ── Task 14 (Phase 3c 改訂): 発注経路の auto soft halt 判定 ──

    def _record_bridge_success(self) -> None:
        """発注成功または bridge 健全応答時に呼ぶ。両カウンタリセット。"""
        self._consecutive_rejects = 0
        self._consecutive_unreachable = 0

    def _record_bridge_reject(self) -> None:
        """MT5 retcode REJECT (HTTP 409) 受領時に呼ぶ。連続 N 回で auto soft halt。"""
        self._consecutive_rejects += 1
        if self._consecutive_rejects >= self._reject_threshold:
            self._auto_soft_halt(
                f"{self._reject_threshold} consecutive rejects"
            )

    def _record_bridge_unreachable(self) -> None:
        """bridge 不通 (connection error / 5xx) 受領時に呼ぶ。連続 N 回で auto soft halt。"""
        self._consecutive_unreachable += 1
        if self._consecutive_unreachable >= self._unreachable_threshold:
            self._auto_soft_halt(
                f"{self._unreachable_threshold} consecutive unreachable"
            )

    def _auto_soft_halt(self, reason: str) -> None:
        """bridge を soft halt 状態にする (auto)。再開は手動 (POST /admin/resume)。"""
        try:
            httpx.post(
                f"{self._url}/admin/halt",
                json={"mode": "soft", "reason": f"auto: {reason}"},
                timeout=self._timeout, headers=self._headers(),
            )
            logger.error(f"[MT5_BRIDGE] AUTO SOFT HALT triggered: {reason}")
            if self._notifier:
                try:
                    self._notifier.send_embed(
                        title="🛑 MT5 ブリッジ 自動 SOFT HALT 発動",
                        description=f"reason: {reason}\n\n手動再開: /resume",
                        color=0xE74C3C,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[MT5_BRIDGE] notify auto soft halt failed: {e}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[MT5_BRIDGE] auto halt API failed: {e}")
