"""MT5 ブリッジ稼働率測定 + auto soft halt ジョブ。

Windows 側で動く MT5 ブリッジ (FastAPI) の `/health` を定期的に叩き、
応答有無・レイテンシを JSONL に追記する。さらに `/health` が
連続 N 回不通になった場合は bridge `/admin/halt mode=soft` を発動し、
Discord に通知する。

設計:
- bridge_url が空ならノーオペ (まだブリッジが立っていない初期段階で安全)
- 失敗 (接続不可・タイムアウト・5xx) も等しく 1 行記録 → 稼働率分母に含める
- 失敗してもデーモンは止めない (logger.warning のみ)
- 連続 `consecutive_unreachable_threshold` 回失敗で `/admin/halt mode=soft` 1 回発動
  (再開は手動 `/admin/resume`)。デフォルト heartbeat=5 分 × 3 = 約 15 分で halt。
- 発注経路 (Mt5BridgeBrokerAdapter) の auto soft halt とは独立カウンタ。
  どちらが先に閾値に達してもよく、bridge 側で多重 halt は冪等。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

from src.config import BASE_DIR, AppConfig
from src.utils.clock import local_now

logger = logging.getLogger(__name__)


# ── モジュールレベル状態 (単一プロセス前提、再起動でリセット) ──
# halt 状態自体は data/state/halt.json に永続化される (halt_state モジュール経由)。
# ここはあくまで「連続失敗カウンタ」のみを保持する。
_state: dict = {
    "consecutive_failures": 0,
}


def _reset_state_for_test() -> None:
    """ユニットテスト用: モジュール状態をリセット。"""
    _state["consecutive_failures"] = 0


def _state_dir_for(config: AppConfig) -> Path:
    """state_dir を解決 (テストでも置き換え可能 monkeypatch ポイント)。

    AppConfig.state_dir は既に絶対 Path (BASE_DIR/data/state) を返すが、
    テスト用スタブ config が Path("data/state") のような相対 Path を
    持っているケースにも対応するため、BASE_DIR を常に基準にする。
    Path 結合の規則上、引数が絶対 Path ならそちらが優先される。
    """
    return BASE_DIR / config.state_dir


def _fetch_and_update_balance(
    config: AppConfig, base: str, cfg, headers: dict[str, str],
) -> None:
    """bridge /account を叩いて balance.json を更新する。

    /health 成功時の piggyback。失敗時は警告ログのみ (新規 soft halt 経路は作らない)。
    paper → mt5 切替を検知したら Discord 通知を送る。

    transition 検出は mutate のクロージャ内で prev source を捕捉することで
    race-free。
    """
    from src.persistence import balance_snapshot

    state_dir = _state_dir_for(config)
    try:
        resp = httpx.get(
            f"{base}/account", timeout=cfg.request_timeout_seconds, headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        mt5_balance = float(data["balance"])
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as e:
        logger.warning(f"[MT5_HB] /account fetch failed: {type(e).__name__}: {e}")
        return

    # クロージャで prev source を mutate のロック下で捕捉 → transition 検出を race-free に
    captured: dict = {}

    def _swap(snap: "balance_snapshot.BalanceSnapshot") -> "balance_snapshot.BalanceSnapshot":
        captured["prev_source"] = snap.source
        return balance_snapshot.refresh_from_mt5(snap, mt5_balance)

    new_snap = balance_snapshot.mutate(state_dir, _swap)
    prev_source = captured.get("prev_source")

    if prev_source == "paper" and new_snap.source == "mt5":
        logger.info(
            f"[MT5_HB] balance.json source paper → mt5: "
            f"deposit ¥{mt5_balance:,.0f} (initial)"
        )
        if config.notifier.enabled:
            try:
                from src.notifications.notifier import create_notifier
                notifier = create_notifier(config.notifier.enabled)
                asyncio.run(notifier.send_embed(
                    title="✅ MT5 残高同期 完了 (paper → mt5)",
                    description=(
                        f"deposit (ROI 分母) を ¥{mt5_balance:,.0f} で確定しました。\n"
                        f"以降の lot 計算・DD は MT5 実残高ベースで動作します。"
                    ),
                    color=0x2ECC71,
                ))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[MT5_HB] paper→mt5 notify failed: {e}")


def _append_record(log_path: Path, record: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _probe(url: str, timeout: float) -> tuple[bool, float | None, int | None, str | None]:
    """ブリッジ /health に GET。Returns: (ok, latency_ms, http_status, error)."""
    started = time.perf_counter()
    try:
        resp = httpx.get(url, timeout=timeout)
        latency_ms = (time.perf_counter() - started) * 1000
        return (resp.is_success, latency_ms, resp.status_code, None)
    except httpx.HTTPError as e:
        latency_ms = (time.perf_counter() - started) * 1000
        return (False, latency_ms, None, f"{type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001
        latency_ms = (time.perf_counter() - started) * 1000
        return (False, latency_ms, None, f"{type(e).__name__}: {e}")


def _trigger_auto_soft_halt(
    config: AppConfig, base_url: str, api_key: str, timeout: float,
    failure_count: int,
) -> None:
    """finance halt 状態を先に確定し、Discord 通知を送ってから bridge POST。

    bridge 不通時にも Discord 通知が確実に出ることを保証する (本仕様の核心)。
    """
    from src.persistence import halt_state

    state_dir = _state_dir_for(config)
    reason = f"{failure_count} consecutive /health failures"

    # ① finance halt 状態を確定 (常に成功)
    _new_state, changed = halt_state.trigger_auto(state_dir, reason, "heartbeat")
    if not changed:
        logger.debug(
            "[MT5_HB] auto halt skipped: finance state already halted"
        )
        return  # 二重通知防止

    # ② Discord 通知 (bridge 状態に依存しない)
    if config.notifier.enabled:
        try:
            from src.notifications.notifier import create_notifier
            notifier = create_notifier(config.notifier.enabled)
            asyncio.run(notifier.send_embed(
                title="🛑 MT5 ブリッジ /health 不通 → 自動 SOFT HALT",
                description=(
                    f"reason: {reason}\n\n"
                    f"bridge `/health` が {failure_count} 回連続応答なし。\n"
                    f"新規発注をブロックしました (既存ポジ管理は継続)。\n\n"
                    f"復帰: bridge 復活確認後 `?resume`"
                ),
                color=0xE74C3C,
            ))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[MT5_HB] auto halt notify failed: {e}")
    logger.error(f"[MT5_HB] AUTO SOFT HALT triggered: {reason}")

    # ③ bridge /admin/halt POST (best-effort、失敗しても finance 状態は確定済)
    headers = {"X-Bridge-Api-Key": api_key} if api_key else {}
    try:
        httpx.post(
            f"{base_url}/admin/halt",
            json={"mode": "soft", "reason": f"auto: {reason}"},
            timeout=timeout, headers=headers,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[MT5_HB] bridge halt POST failed "
            f"(state already set locally): {e}"
        )


def run_mt5_heartbeat(config: AppConfig) -> None:
    """schedule ライブラリから呼ばれる同期エントリポイント。

    live_broker != "mt5" もしくは providers.mt5 未設定、または
    bridge_url 空ならノーオペで終了。
    失敗してもデーモンは止まらない。
    """
    if config.live_broker != "mt5" or config.providers.mt5 is None:
        logger.debug("[MT5_HB] live_broker != mt5, skipping")
        return
    cfg = config.providers.mt5
    base = cfg.bridge_url.rstrip("/")
    if not base:
        logger.debug("[MT5_HB] bridge_url not configured, skipping")
        return

    url = f"{base}/health"
    try:
        ok, latency_ms, status, error = _probe(url, cfg.request_timeout_seconds)
        record = {
            "ts": local_now(config).isoformat(timespec="seconds"),
            "url": url,
            "ok": ok,
            "latency_ms": round(latency_ms, 1) if latency_ms is not None else None,
            "http_status": status,
            "error": error,
        }
        log_path = BASE_DIR / cfg.log_path
        _append_record(log_path, record)

        if ok:
            _state["consecutive_failures"] = 0
            logger.info(
                f"[MT5_HB] OK  status={status} latency={record['latency_ms']}ms"
            )
            # /health 成功時に /account も叩いて balance.json を同期。
            # mode=live のときだけ実行する。live_test は paper 運用と同等
            # (実資金が動かない) ため balance.json を MT5 値で上書きしない。
            if config.mode == "live":
                headers = {"X-Bridge-Api-Key": cfg.api_key} if cfg.api_key else {}
                _fetch_and_update_balance(config, base, cfg, headers)
            return

        _state["consecutive_failures"] += 1
        logger.warning(
            f"[MT5_HB] FAIL ({_state['consecutive_failures']}/"
            f"{cfg.consecutive_unreachable_threshold}) "
            f"status={status} latency={record['latency_ms']}ms err={error}"
        )

        # ── auto soft halt 判定 ──
        # 二重発動防止は halt_state.trigger_auto 内の changed=False 判定で行う
        # (旧 _state["auto_halt_triggered"] は削除済)
        if _state["consecutive_failures"] >= cfg.consecutive_unreachable_threshold:
            _trigger_auto_soft_halt(
                config, base, cfg.api_key,
                cfg.request_timeout_seconds,
                _state["consecutive_failures"],
            )
    except Exception as e:  # noqa: BLE001 - デーモン停止防止
        logger.error(f"[MT5_HB] heartbeat job failed: {e}", exc_info=True)
