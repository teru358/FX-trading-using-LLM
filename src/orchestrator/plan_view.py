"""取引 plan の表示/API 用整形層 (CLI 非依存)。

CLI (`plans` コマンド) と将来の承認ゲート F-5 API がここを共用する。cli.py に置くと
prompt_toolkit / Rich / 取引サイクル系を引き込んで責務が逆流するため、軽量な独立
モジュールに切り出している。entry 条件の短縮表記は context_builder._entry_summary を
再利用する (DRY)。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.orchestrator.context_builder import _entry_summary

if TYPE_CHECKING:
    from src.data.orchestrator_store import _TradePlan


def _safe_entry_summary(conditions: Any) -> str:
    """entry_conditions_json が壊れていても落ちない _entry_summary ラッパ。

    _entry_summary は各要素が dict である前提で c.get() を呼ぶため、要素が str や
    None だと AttributeError になる。dict 要素だけに絞ってから渡し、それでも失敗
    したら空文字に倒す (表示は best-effort)。
    """
    if not isinstance(conditions, list):
        return ""
    safe = [c for c in conditions if isinstance(c, dict)]
    try:
        return _entry_summary(safe)
    except Exception:
        return ""


def plan_to_row(plan: "_TradePlan") -> dict[str, Any]:
    """_TradePlan 1件を表示/API 用の dict に整形する (純関数)。

    sl/tp は action_json から生値を取り、欠損・型不整合時は None。表示整形 (「-」等) は
    呼び出し側 (表示層) の責務。フィールドキーは承認ゲート spec F-5 と揃える。
    壊れた JSON でも 1件で全体が落ちないよう best-effort に倒す。
    """
    action = plan.action_json if isinstance(plan.action_json, dict) else {}
    return {
        "plan_id": plan.plan_id,
        "pair": plan.pair,
        "direction": plan.direction,
        "entry_summary": _safe_entry_summary(plan.entry_conditions_json),
        "sl": action.get("sl"),
        "tp": action.get("tp"),
        "expires_at": plan.expires_at,
        "created_at": plan.created_at,
    }
