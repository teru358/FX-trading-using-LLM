"""data/state/balance.json の読み書きと MT5 同期。

スキーマ:
    balance:        現在残高 (live: MT5 同期、paper: close 時 += realized_pnl)
    deposit:        入金額 (ROI 分母、初回 MT5 fetch 成功で確定、以降不変)
    peak_balance:   DD 計算用 max
    source:         "paper" | "mt5"
    fetched_at:     ISO 8601 (UTC)

Bootstrap 規則: ファイル不在時は全モード共通で PAPER_DEFAULT (¥10,000) で生成。
既存 positions.json.account_balance は読まない (廃棄)。
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

PAPER_DEFAULT: float = 10_000.0
OBSERVER_DEFAULT: float = 100_000.0


@dataclass(frozen=True)
class BalanceSnapshot:
    balance: float
    deposit: float
    peak_balance: float
    source: Literal["paper", "mt5"]
    fetched_at: str


def _path(state_dir: Path) -> Path:
    return state_dir / "balance.json"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _bootstrap_paper(state_dir: Path) -> BalanceSnapshot:
    snap = BalanceSnapshot(
        balance=PAPER_DEFAULT,
        deposit=PAPER_DEFAULT,
        peak_balance=PAPER_DEFAULT,
        source="paper",
        fetched_at=_now_iso(),
    )
    write(state_dir, snap)
    logger.info(f"[BALANCE] bootstrapped paper default: {PAPER_DEFAULT}")
    return snap


def read(state_dir: Path) -> BalanceSnapshot:
    """balance.json を読む。不在/破損なら paper デフォルトで再生成して返す。"""
    p = _path(state_dir)
    if not p.exists():
        return _bootstrap_paper(state_dir)
    try:
        with p.open(encoding="utf-8") as f:
            data = json.load(f)
        return BalanceSnapshot(**data)
    except (json.JSONDecodeError, TypeError, KeyError) as e:
        logger.error(
            f"[BALANCE] balance.json corrupted ({type(e).__name__}: {e}), "
            f"regenerating paper default"
        )
        return _bootstrap_paper(state_dir)


def write(state_dir: Path, snap: BalanceSnapshot) -> None:
    """atomic write (tmp + rename)。"""
    p = _path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(asdict(snap), f, ensure_ascii=False, indent=2)
    tmp.replace(p)


def update_peak(snap: BalanceSnapshot, new_balance: float) -> BalanceSnapshot:
    """新 balance を反映し peak を max 更新した snapshot を返す (immutable)。

    paper モードでの close 時に呼ぶ。source / deposit は不変。
    """
    return BalanceSnapshot(
        balance=new_balance,
        deposit=snap.deposit,
        peak_balance=max(snap.peak_balance, new_balance),
        source=snap.source,
        fetched_at=_now_iso(),
    )


def refresh_from_mt5(snap: BalanceSnapshot, mt5_balance: float) -> BalanceSnapshot:
    """MT5 fetch 結果で snapshot 更新。

    source == "paper" の場合は初回 MT5 fetch とみなし deposit/peak も MT5 値で確定。
    source == "mt5" の場合は balance のみ上書き、deposit 不変、peak は max。
    """
    if snap.source == "paper":
        return BalanceSnapshot(
            balance=mt5_balance,
            deposit=mt5_balance,
            peak_balance=mt5_balance,
            source="mt5",
            fetched_at=_now_iso(),
        )
    return BalanceSnapshot(
        balance=mt5_balance,
        deposit=snap.deposit,
        peak_balance=max(snap.peak_balance, mt5_balance),
        source="mt5",
        fetched_at=_now_iso(),
    )


def is_stale(snap: BalanceSnapshot, threshold_minutes: int = 30) -> bool:
    """fetched_at が閾値以上前なら True (live モードでのみ意味あり)。"""
    fetched = datetime.fromisoformat(snap.fetched_at)
    age_min = (datetime.now(tz=timezone.utc) - fetched).total_seconds() / 60.0
    return age_min >= threshold_minutes
