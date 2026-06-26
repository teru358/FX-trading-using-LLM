"""起動時 order_intent recovery job (spec §3, F-2)。

クラッシュで lease 超過した未完了 order_intent を 3 分岐で処理する (replan モデル:
旧 plan/intent を terminal 化し、再発注は次 planning が新 plan を作る)。
- status=pending (未送信) → retryable: intent=abandoned + plan=invalidated (新 plan で再発注)
- status=submitted & order_id null (送信直後クラッシュ・建玉不明) → needs_reconcile:
  intent/plan は触らず隔離 + alert (建玉あるかもしれない。再 trigger 禁止、自動照合は F 外)
- status=submitted & order_id あり (約定確定・status 補正前) → intent=filled に補正

broker には触れない (自動照合しない = スコープ境界)。
"""
from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def recover_pending_intents(orch, *, now: datetime) -> dict[str, int]:
    """recovery 候補を 3 分岐で処理する。集計 dict を返す。"""
    summary = {"retryable": 0, "needs_reconcile": 0, "corrected_filled": 0}
    for intent in orch.get_stale_or_unconfirmed_intents(now=now):
        if intent.status == "pending":
            # 未送信クラッシュ: terminal 化して新 plan で再発注 (同一 plan は蘇生しない)。
            orch.set_recovery_status(plan_id=intent.plan_id, recovery_status="retryable")
            orch.record_order_result(plan_id=intent.plan_id, status="abandoned")
            orch.update_plan_status(intent.plan_id, "invalidated")
            summary["retryable"] += 1
        elif intent.status == "submitted" and intent.order_id is None:
            # 送信直後クラッシュ: 建玉あるかもしれない → 隔離 (terminal 化しない) + alert。
            orch.set_recovery_status(
                plan_id=intent.plan_id, recovery_status="needs_reconcile",
            )
            summary["needs_reconcile"] += 1
            logger.warning(
                "[ORCH-RECOVERY] needs_reconcile: plan %s submitted but order_id "
                "unknown — 再 trigger 禁止・要手動確認 (建玉照合が必要)", intent.plan_id,
            )
        elif intent.status == "submitted" and intent.order_id is not None:
            # 約定確定だが status 補正前: filled に補正 (plan は triggered のまま)。
            orch.record_order_result(
                plan_id=intent.plan_id, status="filled", order_id=intent.order_id,
            )
            summary["corrected_filled"] += 1
    if any(summary.values()):
        logger.info("[ORCH-RECOVERY] %s", summary)
    return summary
