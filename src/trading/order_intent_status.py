"""ExecutionResult.outcome → order_intents.status の単一 mapping (spec §2 step6, Task F)。

broker の ExecutionResult.outcome (executed/skipped/halted/rejected/failed) を
order_intents の status enum (ORDER_INTENT_STATUSES) に明示変換する。enum 外を
record_order_result に渡すと ValueError になるため、ここで一元管理する。
"""
from __future__ import annotations

# spec §2 step6 の mapping 表。
EXECUTION_OUTCOME_TO_INTENT_STATUS: dict[str, str] = {
    "executed": "filled",      # 約定
    "skipped": "abandoned",    # 想定内抑制 (既存ポジ/hold/リスク制限) — plan は用済み
    "rejected": "rejected",    # gate/broker 拒否
    "halted": "rejected",      # halt 状態 → reject と同じく発注見送り
    "failed": "failed",        # 技術的失敗 (bridge 不通等)
}

# 想定内 (alert 不要) の outcome。それ以外は要注意通知。
_NON_ALERT_OUTCOMES = frozenset({"executed", "skipped"})


def intent_status_for_outcome(outcome: str) -> str:
    """outcome を order_intents.status へ変換する。未知 outcome は KeyError。"""
    return EXECUTION_OUTCOME_TO_INTENT_STATUS[outcome]


def is_alertable_outcome(outcome: str) -> bool:
    """要注意通知が必要な outcome か (halted/rejected/failed=True)。"""
    return outcome not in _NON_ALERT_OUTCOMES
