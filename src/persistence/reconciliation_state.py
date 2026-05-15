"""data/state/reconciliation.json の読み書き — bridge reconciliation skip
連続カウンタの永続ストア。

スキーマ:
    skipped_consecutive:  直近で連続して reconciliation を skip した回数
    stale_warned:         閾値到達時の warning ログを出し済みか (重複抑止)
    last_skip_at:         直近 skip 時刻 (ISO 8601 UTC) / 初期 None
    last_recovered_at:    直近で reconciliation が成功した時刻 / 初期 None

なぜ永続化が必要か:
    exit_check_cycle は呼び出しごとに create_broker() で新しい
    Mt5BridgeBrokerAdapter を生成する。Broker インスタンス変数で持つと
    毎サイクル 0 にリセットされて検知できない。state_dir 配下の JSON
    (halt_state.py と同じパターン) に永続化することで broker インスタンス
    を跨いで保持する。

書込みは `mutate()` 経由のロック付き read-modify-write のみ:
    - tmp file + atomic replace で部分書込み中の JSON を /status が読まないようにする
    - state_store._get_state_lock で halt_state / balance_snapshot とロックを共有

壊れた JSON は fail-safe で default 値を返す (= halt_state とは異なり、
ここで停止する必要は無いので silent fallback)。
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconciliationState:
    skipped_consecutive: int
    stale_warned: bool
    last_skip_at: str | None
    last_recovered_at: str | None


_DEFAULT = ReconciliationState(
    skipped_consecutive=0,
    stale_warned=False,
    last_skip_at=None,
    last_recovered_at=None,
)


def _path(state_dir: Path) -> Path:
    return Path(state_dir) / "reconciliation.json"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def read(state_dir: Path) -> ReconciliationState:
    """reconciliation.json を読む。不在 / 壊れていれば default を返す。"""
    p = _path(state_dir)
    if not p.exists():
        return _DEFAULT
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return ReconciliationState(
            skipped_consecutive=int(data.get("skipped_consecutive", 0)),
            stale_warned=bool(data.get("stale_warned", False)),
            last_skip_at=data.get("last_skip_at"),
            last_recovered_at=data.get("last_recovered_at"),
        )
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning(
            f"[RECONCILE] reconciliation.json corrupted "
            f"({type(e).__name__}: {e}) — returning default"
        )
        return _DEFAULT


def write(state_dir: Path, state: ReconciliationState) -> None:
    """atomic write (tmp + rename)。/status が読み出す対象なので、部分書込み
    中の JSON を読まれないようにする。
    """
    p = _path(Path(state_dir))
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(asdict(state), f, ensure_ascii=False, indent=2)
    tmp.replace(p)


def mutate(
    state_dir: Path,
    fn: Callable[[ReconciliationState], ReconciliationState],
) -> ReconciliationState:
    """state_dir 単位のロック下で read → fn(state) → write を原子的に実行。

    StateStore.transaction() / halt_state.mutate() / balance_snapshot.mutate()
    と同じロックレジストリ (_get_state_lock) を共有し、複数経路 (exit_check /
    API / future writers) からの read-modify-write を安全にシリアライズする。
    """
    from src.persistence.state_store import _get_state_lock

    lock = _get_state_lock(Path(state_dir))
    with lock:
        current = read(state_dir)
        new_state = fn(current)
        write(state_dir, new_state)
        return new_state


def increment_skip(state_dir: Path) -> ReconciliationState:
    """skip カウンタを +1 して永続化。新しい state を返す。"""
    def _swap(current: ReconciliationState) -> ReconciliationState:
        return replace(
            current,
            skipped_consecutive=current.skipped_consecutive + 1,
            last_skip_at=_now_iso(),
        )
    return mutate(state_dir, _swap)


def mark_stale_warned(state_dir: Path) -> ReconciliationState:
    """閾値到達 warning ログを出した印を付ける (冪等)。"""
    def _swap(current: ReconciliationState) -> ReconciliationState:
        if current.stale_warned:
            return current
        return replace(current, stale_warned=True)
    return mutate(state_dir, _swap)


def mark_recovered(state_dir: Path) -> ReconciliationState:
    """reconciliation 成功時にカウンタと warning フラグをリセット。"""
    def _swap(_current: ReconciliationState) -> ReconciliationState:
        return replace(
            _DEFAULT,
            last_recovered_at=_now_iso(),
        )
    return mutate(state_dir, _swap)
