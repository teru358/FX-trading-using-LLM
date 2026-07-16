"""Orchestrator Layer2 の strict output schema (design §5.4)。

LLM raw text を直接 plan にしない。許可 vocabulary / Literal を __post_init__ で
検証し、from_llm_json() で JSON parse + 構築する。

実装方針 (Task 2.2): Pydantic ではなく dataclass + 手動 JSON parse。
理由 — 既存コードベースの runtime データ構造は全て dataclass (TradeSignal 等) で
統一されており、`llm.chat()` は str 返しのみ (structured output 非対応) のため
Pydantic を使っても json.loads は不可避で、validation の利点は parse 後にしか
効かず実質同等。詳細は finance_phase2_impl_progress メモを参照。

`SchemaParseError` は fail-safe 用 (design §5.4: parse/validation error は新規 plan を
作らず agent_runs.status=failed に倒す)。pipeline 側で捕捉する。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class SchemaParseError(ValueError):
    """LLM 出力の JSON parse / schema 構築失敗。fail-safe トリガ。"""


# ---------------------------------------------------------------------------
# 共通ヘルパ
# ---------------------------------------------------------------------------


def _strip_fences(raw: str) -> str:
    """markdown コードフェンス (```json ... ```) を剥がして中身を返す。

    多重フェンス・閉じフェンス後の後続テキスト (例: "Note: ...") にも耐える。
    冪等になるまで剥がし続け、閉じフェンスは「最後の ``` 行」を境界に使う。
    """
    text = raw.strip()
    while text.startswith("```"):
        lines = text.splitlines()
        # 先頭フェンス行 (```json / ```) を除去
        lines = lines[1:]
        # 最後の閉じフェンス行を探して、それ以降 (後続ノート等) を捨てる
        close_idx = None
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].strip().startswith("```"):
                close_idx = i
                break
        if close_idx is not None:
            lines = lines[:close_idx]
        stripped = "\n".join(lines).strip()
        if stripped == text:  # 進展なし — 無限ループ防止
            break
        text = stripped
    return text


def _opt_float(value: Any, field_name: str) -> float | None:
    """None はそのまま、それ以外は有限 float 化。失敗時 SchemaParseError。

    LLM が数値を文字列 ("150.0") で返しても正規化する。bool / NaN / Infinity は
    拒否する — NaN の entry 値は derive_rr の min 比較 (NaN < x == False) で
    hard gate を偽通過させるため (R2 High#3)。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise SchemaParseError(f"{field_name} must be numeric, got {value!r}")
    try:
        f = float(value)
    except (TypeError, ValueError) as exc:
        raise SchemaParseError(f"{field_name} must be numeric, got {value!r}") from exc
    if not math.isfinite(f):
        raise SchemaParseError(f"{field_name} must be finite, got {f!r}")
    return f


def _loads(raw: str) -> dict[str, Any]:
    """フェンス除去 + json.loads。失敗時 SchemaParseError。"""
    try:
        data = json.loads(_strip_fences(raw))
    except (json.JSONDecodeError, ValueError) as exc:
        raise SchemaParseError(f"JSON parse failed: {exc}") from exc
    if not isinstance(data, dict):
        raise SchemaParseError(f"expected JSON object, got {type(data).__name__}")
    return data


# ---------------------------------------------------------------------------
# EntryCondition / InvalidationCondition (design §6.2 / §6.3 vocabulary)
# ---------------------------------------------------------------------------

_ENTRY_PRICE_TYPES = frozenset(
    {"price_at_or_below", "price_at_or_above", "breakout_above", "breakout_below"}
)
_ENTRY_TYPES = _ENTRY_PRICE_TYPES | {"spread_below", "technical_status_is"}


@dataclass(frozen=True)
class EntryCondition:
    """watch loop が評価する entry 述語。and のみ対応 (design §6.2)。

    type ごとに使うフィールドが異なる:
      - price_*/breakout_*: value
      - spread_below: value_pips
      - technical_status_is: status (=="ok" のみ)
    """

    type: str
    value: float | None = None
    value_pips: float | None = None
    status: str | None = None

    def __post_init__(self) -> None:
        if self.type not in _ENTRY_TYPES:
            raise ValueError(f"unknown entry condition type: {self.type!r}")
        if self.type in _ENTRY_PRICE_TYPES:
            if self.value is None:
                raise ValueError(f"{self.type} requires 'value'")
        elif self.type == "spread_below":
            if self.value_pips is None:
                raise ValueError("spread_below requires 'value_pips'")
        elif self.type == "technical_status_is":
            if self.status != "ok":
                raise ValueError("technical_status_is only supports status='ok'")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EntryCondition":
        if "type" not in data:
            raise SchemaParseError("entry condition missing 'type'")
        try:
            return cls(
                type=data["type"],
                value=_opt_float(data.get("value"), "entry value"),
                value_pips=_opt_float(data.get("value_pips"), "entry value_pips"),
                status=data.get("status"),
            )
        except SchemaParseError:
            raise
        except ValueError as exc:
            raise SchemaParseError(f"invalid entry condition: {exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type}
        if self.value is not None:
            out["value"] = self.value
        if self.value_pips is not None:
            out["value_pips"] = self.value_pips
        if self.status is not None:
            out["status"] = self.status
        return out


_INVAL_PRICE_TYPES = frozenset({"price_below", "price_above"})
_INVAL_MARKER_TYPES = frozenset({"technical_stale", "news_conflict", "expired"})
_INVAL_TYPES = _INVAL_PRICE_TYPES | _INVAL_MARKER_TYPES


@dataclass(frozen=True)
class InvalidationCondition:
    """plan 失効述語 (design §6.3)。price_below/above は value 必須、marker 系は不要。"""

    type: str
    value: float | None = None

    def __post_init__(self) -> None:
        if self.type not in _INVAL_TYPES:
            raise ValueError(f"unknown invalidation condition type: {self.type!r}")
        if self.type in _INVAL_PRICE_TYPES and self.value is None:
            raise ValueError(f"{self.type} requires 'value'")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InvalidationCondition":
        if "type" not in data:
            raise SchemaParseError("invalidation condition missing 'type'")
        try:
            return cls(
                type=data["type"],
                value=_opt_float(data.get("value"), "invalidation value"),
            )
        except SchemaParseError:
            raise
        except ValueError as exc:
            raise SchemaParseError(f"invalid invalidation condition: {exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type}
        if self.value is not None:
            out["value"] = self.value
        return out


# ---------------------------------------------------------------------------
# PlannerOpportunity (機会判定 ②)
# ---------------------------------------------------------------------------

_OPPORTUNITY = frozenset({"yes", "no"})
_OPP_DIRECTION = frozenset({"long", "short", "none"})


@dataclass(frozen=True)
class PlannerOpportunity:
    opportunity: str  # "yes" | "no"
    direction: str  # "long" | "short" | "none"
    score: float
    confidence: float
    reasoning_summary: str
    missing_inputs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.opportunity not in _OPPORTUNITY:
            raise ValueError(f"opportunity must be yes/no, got {self.opportunity!r}")
        if self.direction not in _OPP_DIRECTION:
            raise ValueError(f"direction must be long/short/none, got {self.direction!r}")
        if not 0.0 <= self.score <= 1.0:
            raise ValueError(f"score must be in [0,1], got {self.score}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0,1], got {self.confidence}")

    @classmethod
    def from_llm_json(cls, raw: str) -> "PlannerOpportunity":
        data = _loads(raw)
        try:
            return cls(
                opportunity=data["opportunity"],
                direction=data["direction"],
                score=float(data["score"]),
                confidence=float(data["confidence"]),
                reasoning_summary=data["reasoning_summary"],
                missing_inputs=list(data.get("missing_inputs", [])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SchemaParseError(f"invalid PlannerOpportunity: {exc}") from exc


# ---------------------------------------------------------------------------
# ExecutionPlanDraft (起案 ③)
# ---------------------------------------------------------------------------

_DRAFT_DIRECTION = frozenset({"long", "short"})


def _normalize_action_numbers(action: dict[str, Any]) -> dict[str, Any]:
    """action の sl/tp/rr を有限 float へ正規化した新 dict を返す。

    - str 数値 → float 変換 (ローカル LLM の揺れ対策)
    - bool / NaN / Infinity / 変換不能 → ValueError (呼び出し元で SchemaParseError 化)
    - 欠落・None → そのまま (gate の missing sl/tp issue の責務)
    """
    out = dict(action)
    for key in ("sl", "tp", "rr"):
        if key not in out or out[key] is None:
            continue
        v = out[key]
        if isinstance(v, bool):
            raise ValueError(f"action {key} must be numeric, got bool {v!r}")
        try:
            f = float(v)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"action {key} must be numeric, got {v!r}") from exc
        if not math.isfinite(f):
            raise ValueError(f"action {key} must be finite, got {f!r}")
        out[key] = f
    return out


@dataclass
class ExecutionPlanDraft:
    direction: str  # "long" | "short"
    entry_conditions: list[EntryCondition]
    action: dict[str, Any]  # sl, tp, size_policy, rr, comment
    invalidation: list[InvalidationCondition]
    expires_at: datetime
    reasoning_summary: str
    scale_in: bool = False
    new_signal_evidence: str | None = None

    def __post_init__(self) -> None:
        # scale_in × new_signal_evidence の cross-field 検証は schema では行わない:
        # ここで raise すると SchemaParseError → run 全体 failed (再起案なし) となり、
        # scale_in を申告しない LLM (feedback 再起案) と非対称になる (codex Medium)。
        # evidence 必須は pipeline の決定的 gate が一元処理する (正本は同方向建玉の導出)。
        if self.direction not in _DRAFT_DIRECTION:
            raise ValueError(f"direction must be long/short, got {self.direction!r}")
        if not self.entry_conditions:
            raise ValueError("entry_conditions must not be empty")
        # action の sl/tp/rr を有限 float へ正規化 (spec 2026-07-16 §2.A′)。
        # str 数値 ("149.0") はローカル LLM の揺れとして変換を許容。bool / NaN /
        # Infinity / 非数値は拒否 → from_llm_json 経由では SchemaParseError となり
        # redraft 救済 (§2.D) に乗る。欠落・None は gate の責務なので通す。
        # __post_init__ に置くのは runtime の draft 復元 (コンストラクタ直呼び)
        # もカバーするため。
        self.action = _normalize_action_numbers(self.action)

    @classmethod
    def from_llm_json(cls, raw: str) -> "ExecutionPlanDraft":
        data = _loads(raw)
        try:
            entries = [EntryCondition.from_dict(c) for c in data["entry_conditions"]]
            invals = [InvalidationCondition.from_dict(c) for c in data["invalidation"]]
            expires_at = datetime.fromisoformat(data["expires_at"])
            scale_in = data.get("scale_in", False)
            if not isinstance(scale_in, bool):
                raise ValueError(f"scale_in must be a JSON bool, got {scale_in!r}")
            evidence = data.get("new_signal_evidence")
            if evidence is not None and not isinstance(evidence, str):
                raise ValueError(
                    f"new_signal_evidence must be null or string, got {evidence!r}"
                )
            evidence = evidence.strip() or None if evidence is not None else None
            return cls(
                direction=data["direction"],
                entry_conditions=entries,
                action=dict(data["action"]),
                invalidation=invals,
                expires_at=expires_at,
                reasoning_summary=data["reasoning_summary"],
                scale_in=scale_in,
                new_signal_evidence=evidence,
            )
        except SchemaParseError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise SchemaParseError(f"invalid ExecutionPlanDraft: {exc}") from exc

    def to_storage_dict(self) -> dict[str, Any]:
        """plan 保存用に dataclass を JSON 安全な dict へ。

        条件は to_dict() で vocabulary 形へ正規化。expires_at は datetime のまま
        (OrchestratorStore._json_safe が ISO に正規化する)。
        """
        return {
            "direction": self.direction,
            "entry_conditions": [c.to_dict() for c in self.entry_conditions],
            "action": self.action,
            "invalidation": [c.to_dict() for c in self.invalidation],
            "expires_at": self.expires_at,
            "reasoning_summary": self.reasoning_summary,
            "scale_in": self.scale_in,
            "new_signal_evidence": self.new_signal_evidence,
        }


# ---------------------------------------------------------------------------
# PlannerFinalDecision (最終承認 ④)
# ---------------------------------------------------------------------------

_FINAL_DECISION = frozenset({"accept", "revise", "reject"})


@dataclass(frozen=True)
class PlannerFinalDecision:
    decision: str  # "accept" | "revise" | "reject"
    reasoning_summary: str
    final_score: float | None = None
    confidence: float | None = None
    revision_request: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.decision not in _FINAL_DECISION:
            raise ValueError(
                f"decision must be accept/revise/reject, got {self.decision!r}"
            )
        if self.decision == "revise" and self.revision_request is None:
            raise ValueError("decision='revise' requires revision_request")
        if self.final_score is not None and not 0.0 <= self.final_score <= 1.0:
            raise ValueError(f"final_score must be in [0,1], got {self.final_score}")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0,1], got {self.confidence}")

    @classmethod
    def from_llm_json(cls, raw: str) -> "PlannerFinalDecision":
        data = _loads(raw)
        try:
            final_score = data.get("final_score")
            confidence = data.get("confidence")
            return cls(
                decision=data["decision"],
                reasoning_summary=data["reasoning_summary"],
                final_score=None if final_score is None else float(final_score),
                confidence=None if confidence is None else float(confidence),
                revision_request=data.get("revision_request"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SchemaParseError(f"invalid PlannerFinalDecision: {exc}") from exc
