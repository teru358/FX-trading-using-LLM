"""data/state/halt.json の読み書き — finance 側 soft halt 状態の権威的ストア。

スキーマ:
    soft_halted:     現在 halt 中か
    auto_triggered:  True = heartbeat/order 連続失敗 / False = manual
    reason:          halt の理由 (Discord 通知 / API レスポンスで使う)
    since:           halt 発動時刻 (ISO 8601 UTC) / 解除時 None
    triggered_by:    "heartbeat" / "order_unreachable" / "order_reject" / "manual"

balance_snapshot と異なり、不在時に bootstrap はしない (default を返すだけ)。
理由: paper モードでは halt.json は不要で、ファイルが無い状態 = 「halted=False」を
そのまま意味する。書込は trigger_* / clear() の経路でのみ行う。
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HaltState:
    soft_halted: bool
    auto_triggered: bool
    reason: str
    since: str | None
    triggered_by: str


_DEFAULT = HaltState(
    soft_halted=False,
    auto_triggered=False,
    reason="",
    since=None,
    triggered_by="",
)


def _path(state_dir: Path) -> Path:
    return state_dir / "halt.json"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def read(state_dir: Path) -> HaltState:
    """halt.json を読む。

    - 不在: default (soft_halted=False) を返す
    - 破損 (JSON parse error / スキーマ不一致): fail-safe halted (soft_halted=True) を返す
      → reason="halt.json corrupted (...)" / triggered_by="corruption"
      → ユーザーが手動確認・修復・?resume するまで新規発注を止める
    """
    p = _path(state_dir)
    if not p.exists():
        return _DEFAULT
    try:
        with p.open(encoding="utf-8") as f:
            data = json.load(f)
        return HaltState(**data)
    except (json.JSONDecodeError, TypeError, KeyError, ValueError) as e:
        logger.error(
            f"[HALT] halt.json corrupted ({type(e).__name__}: {e}) — "
            f"fail-safe halted, manual ?resume required"
        )
        return HaltState(
            soft_halted=True,
            auto_triggered=True,
            reason=f"halt.json corrupted: {type(e).__name__}: {e}",
            since=_now_iso(),
            triggered_by="corruption",
        )


def write(state_dir: Path, state: HaltState) -> None:
    """atomic write (tmp + rename)。"""
    p = _path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(asdict(state), f, ensure_ascii=False, indent=2)
    tmp.replace(p)


def mutate(
    state_dir: Path,
    fn: Callable[[HaltState], HaltState],
) -> HaltState:
    """state_dir 単位のロック下で read → fn(state) → write を原子的に実行する。

    StateStore.transaction() / balance_snapshot.mutate() と同じロックレジストリ
    (_get_state_lock) を共有し、複数経路 (heartbeat / order / API) からの
    read-modify-write を安全にシリアライズする。
    """
    from src.persistence.state_store import _get_state_lock

    lock = _get_state_lock(state_dir)
    with lock:
        state = read(state_dir)
        new_state = fn(state)
        write(state_dir, new_state)
        return new_state


def is_halted(state_dir: Path) -> bool:
    """soft_halted を読み出すショートカット (execute_signal が cycle 毎に呼ぶ)。"""
    return read(state_dir).soft_halted


def trigger_auto(
    state_dir: Path,
    reason: str,
    triggered_by: str,
) -> tuple[HaltState, bool]:
    """auto soft halt を発動する。既に halted 中なら no-op。

    changed 判定は mutate のロック内で行う (race-free)。

    Returns:
        (state, changed): changed=True なら今回の呼出で状態が変化した
                          (Discord 通知などをトリガすべき)。
                          changed=False なら既に halted 中で no-op。
    """
    captured = {"changed": False}

    def _swap(current: HaltState) -> HaltState:
        if current.soft_halted:
            return current  # 既存 state を保持
        captured["changed"] = True
        return HaltState(
            soft_halted=True,
            auto_triggered=True,
            reason=reason,
            since=_now_iso(),
            triggered_by=triggered_by,
        )

    new_state = mutate(state_dir, _swap)
    return new_state, captured["changed"]


def trigger_manual(
    state_dir: Path,
    reason: str,
) -> tuple[HaltState, bool]:
    """manual soft halt を発動する。既に halted 中なら no-op。

    changed 判定は trigger_auto と同じく lock 内 atomic。
    """
    captured = {"changed": False}

    def _swap(current: HaltState) -> HaltState:
        if current.soft_halted:
            return current
        captured["changed"] = True
        return HaltState(
            soft_halted=True,
            auto_triggered=False,
            reason=reason,
            since=_now_iso(),
            triggered_by="manual",
        )

    new_state = mutate(state_dir, _swap)
    return new_state, captured["changed"]


def clear(state_dir: Path) -> HaltState:
    """halt 状態を default にリセットする (resume の最終ステップ)。

    既に解除済の場合も冪等に default を返す。
    """

    def _swap(_current: HaltState) -> HaltState:
        return _DEFAULT

    return mutate(state_dir, _swap)
