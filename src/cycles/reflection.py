"""reflection job — 決済済みトレードの LLM 振り返り (spec §3)。

trades.json の closed trades と reflections テーブルの差分から未振り返りを検知し、
1 件ずつ LLM slot 経由で振り返りを生成する。出力は reflections テーブル (done 記録)
と directional RAG の trade complete カード (news_collector への教訓供給)。

実行制御 (spec §3.6): JobGuard 配下の controller が同期実行され、各件の前に
slot busy / waiting_user_job / planning 実行中を確認して譲る。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from src.analysis.price_analyzer import load_user_notes   # 現行定義元 (price_analyzer.py:30)
from src.analysis.reflector import generate_close_reflection
from src.llm.factory import create_llm_client
from src.persistence.state_store import StateStore
from src.rag.directional_writer import record_trade_complete
from src.rag.embedder import make_embed_fn
from src.trading.position_manager import Order
from src.utils.clock import db_now

if TYPE_CHECKING:
    from src.data.orchestrator_store import OrchestratorStore

logger = logging.getLogger(__name__)

_NEW_QUOTA = 2      # 未試行 (新しい順) の優先枠 (spec §3.2b)
_TOTAL_QUOTA = 10   # 1 回の実行枠


def _load_states(orch_store) -> dict:
    """reflections の状態マップを一括ロードする (order_id → 行)。

    行ごとに問い合わせず 1 回で取り、parse と選択の両方で使い回す
    (レビュー MEDIUM-1)。
    """
    return {r.order_id: r for r in orch_store.get_reflections()}


def _is_due(state, now: datetime) -> bool:
    """未試行 / 期限到来済み retry のみ True (done・dead・期限前 retry は False)。"""
    if state is None:
        return True
    if state.status in ("done", "dead"):
        return False
    return state.next_retry_at is not None and state.next_retry_at <= now


def _parse_closed_trades(raw, orch_store, now: datetime,
                         states: dict) -> list[Order]:
    """trades.json の生 dict 群を行単位で Order に変換する。

    不正行が 1 つあっても他の行の処理を止めない (plan レビュー Medium-2)。
    order_id を持つ壊れ行は retry(parse_error) で記録する — デシリアライズ不具合
    (新フィールド追従漏れ等) は parser 修正で直り得るため即 dead にしない
    (即 dead は spec 上 instrument 不在のみ)。恒久的に壊れた行は backoff の末
    5 回で自然に dead へ落ちる。

    parse error の retry 記録も backoff 期限に従う (レビュー MEDIUM-1)。
    無条件に記録すると、毎時実行で 1/2/4/8h の backoff を待たず約 5 時間で
    dead に落ち、本来 15 時間ある修正猶予が失われる。
    """
    if not isinstance(raw, list):
        logger.warning(f"[REFLECT] trades.json is not a list "
                       f"({type(raw).__name__}) — skipped")
        return []
    orders: list[Order] = []
    for d in raw:
        if not isinstance(d, dict):
            logger.warning(f"[REFLECT] non-dict trade row skipped: {d!r:.80}")
            continue
        oid = d.get("order_id")
        if oid and not _is_due(states.get(oid), now):
            continue    # 済み (done/dead) か backoff 期限前 — 走査対象外
        try:
            order = Order.from_dict(dict(d))
        except Exception as e:
            if oid:
                try:
                    orch_store.mark_reflection_retry(
                        oid, pair=str(d.get("pair", "?")),
                        error=f"parse_error: {type(e).__name__}: {e}", now=now)
                except Exception:
                    logger.exception(
                        f"[REFLECT] {oid}: failed to record parse error")
            logger.warning(
                f"[REFLECT] broken trade row skipped (order_id={oid}): {e}")
            continue
        if order.status == "closed":
            orders.append(order)
    return orders


def _select_targets(
    closed: list[Order], orch_store: "OrchestratorStore", now: datetime,
    states: dict | None = None,
) -> list[Order]:
    """処理対象を枠規則で選ぶ (spec §3.2b)。

    eligible = 未試行 ∪ next_retry_at 到来済み retry。
    枠 10 = 未試行の新しい順 2 + 残り eligible の古い順 8 (融通あり)。

    states は controller が一括ロードしたものを受け取る (レビュー MEDIUM-1)。
    None なら自前でロードする (テスト・単体利用向け)。
    """
    if states is None:
        states = _load_states(orch_store)

    def _ts(o: Order) -> datetime:
        return o.closed_at or o.opened_at

    # trades.json は append-only で order_id 一意性を保証しない (append_trade)。
    # 重複行をそのまま通すと同一 order に LLM/RAG が 2 回走るため 1 件に潰す
    # (レビュー MEDIUM-3)。採用するのは「より新しい情報を持つ行」= closed_at が
    # 新しい方、同時刻なら後着行 (レビュー MEDIUM-2)。クラッシュ復旧や再
    # reconciliation で追記された行の方が正しい決済情報を持つため、先着を
    # 優先すると古い close price / PnL で振り返ることになる。
    chosen: dict[str, Order] = {}
    for o in closed:
        prev = chosen.get(o.order_id)
        if prev is None:
            chosen[o.order_id] = o
            continue
        logger.warning(
            f"[REFLECT] duplicate order_id in trades.json: {o.order_id} "
            f"— keeping the newest row")
        if _ts(o) >= _ts(prev):
            chosen[o.order_id] = o
    unique = list(chosen.values())

    eligible = [o for o in unique if _is_due(states.get(o.order_id), now)]
    untried = sorted(
        (o for o in eligible if o.order_id not in states),
        key=_ts, reverse=True,
    )
    fresh = untried[:_NEW_QUOTA]
    fresh_ids = {o.order_id for o in fresh}
    backfill = sorted(
        (o for o in eligible if o.order_id not in fresh_ids), key=_ts,
    )
    return (fresh + backfill)[:_TOTAL_QUOTA]


async def _reflect_and_record(
    config, store, orch_store, llm, embed_fn, order: Order,
    entry_analysis: str,
) -> tuple[str, bool]:
    """LLM 振り返り → RAG upsert。失敗は例外伝搬 (strict、spec §3.5)。"""
    pair_cfg = next(
        p for p in config.tradeable_instruments if p.symbol == order.pair)
    reflection = await generate_close_reflection(
        pair_cfg=pair_cfg, order=order, llm=llm,
        temperature=config.llm.reflection.temperature,
        user_notes=load_user_notes(config.user_notes_path, "reflect"),
        entry_analysis=entry_analysis,
    )
    await record_trade_complete(
        store, embed_fn, order, reflection.full_text,
        horizon=config.orchestrator.policy.trade_horizon,
    )
    return reflection.full_text, reflection.was_directionally_correct


def _process_one(config, store, orch_store, llm, embed_fn, order: Order) -> None:
    """1 件を処理する (slot 内で同期実行)。

    例外境界: instrument 判定より後の全処理 (文脈取得 / LLM / RAG / done 保存) を
    1 件単位の try に入れる (レビュー Medium-4)。失敗はこの中で retry 記録し、
    controller へは漏らさない。
    """
    now = db_now()
    pair_cfg = next(
        (p for p in config.tradeable_instruments if p.symbol == order.pair),
        None,
    )
    if pair_cfg is None:
        # 恒久不能: 現 instrument 設定に無い旧銘柄 → 即 dead (spec §3.2b)
        # DB 失敗を controller へ漏らさない (レビュー HIGH-1): try_run_scheduled は
        # fn の例外を再送出するため、ここで漏らすと batch の残りが巻き添えで落ちる。
        # 未記録なら次回 未試行として再度 dead 判定されるだけで実害はない。
        try:
            orch_store.mark_reflection_dead(
                order.order_id, pair=order.pair,
                error="pair not in tradeable instruments", now=now)
        except Exception:
            logger.exception(
                f"[REFLECT] {order.order_id}: failed to record dead state")
        logger.warning(
            f"[REFLECT] {order.order_id} ({order.pair}): pair not in "
            f"instruments — dead-lettered")
        return
    try:
        intent = orch_store.get_order_intent_by_order_id(order.order_id)
        plan_id = intent.plan_id if intent is not None else None
        entry_analysis = ""
        if plan_id is not None:
            entry_analysis = (
                orch_store.get_latest_plan_create_reasoning(plan_id) or "")
        text, correct = asyncio.run(_reflect_and_record(
            config, store, orch_store, llm, embed_fn, order, entry_analysis))
        # done 保存の失敗も retry に落とす (RAG upsert は冪等なので次回無害)
        orch_store.mark_reflection_done(
            order.order_id, plan_id=plan_id, pair=order.pair,
            close_reason=order.close_reason, realized_pnl=order.realized_pnl,
            reflection_text=text, was_directionally_correct=correct,
            now=db_now())
    except Exception as e:
        try:
            orch_store.mark_reflection_retry(
                order.order_id, pair=order.pair,
                error=f"{type(e).__name__}: {e}", now=db_now())
        except Exception:
            # retry 記録すら失敗 (DB 断等) — ログのみ。未記録なので次回 未試行として再処理
            logger.exception(
                f"[REFLECT] {order.order_id}: failed to record retry state")
        logger.warning(
            f"[REFLECT] {order.order_id} failed ({type(e).__name__}: {e}) "
            f"— will retry")
        return
    logger.info(f"[REFLECT] {order.order_id} ({order.pair}) reflected "
                f"(directionally_correct={correct} plan_id={plan_id})")


def run_reflection_cycle(config, store, orch_store, *, slot) -> None:
    """JobGuard 配下で同期実行される controller (spec §3.6)。

    slot busy / waiting_user_job / planning 実行中のいずれかで残りを次回へ。
    """
    now = db_now()
    try:
        raw = StateStore(config.state_dir).load_trades_raw()
    except Exception:
        logger.warning("[REFLECT] trades.json read failed — skipped",
                       exc_info=True)
        return
    # 状態マップは 1 回だけ取り、parse と選択で共有する (レビュー MEDIUM-1)。
    # parse 中の retry 記録はこのマップに反映されないが、その行はこの回の
    # 選択対象から外れる (parse 失敗行は closed に入らない) ため影響はない。
    states = _load_states(orch_store)
    closed = _parse_closed_trades(raw, orch_store, now, states)
    targets = _select_targets(closed, orch_store, now, states)
    if not targets:
        return
    llm = None
    embed_fn = None
    for order in targets:
        if slot.waiting_user_job:
            logger.info("[REFLECT] user job waiting — yielding")
            break
        if orch_store.has_running_planning_run(now=db_now()):
            logger.info("[REFLECT] planning in progress — yielding")
            break
        if llm is None:
            # 譲渡チェック通過後に構築する (「使う前に確認する」順序、レビュー LOW)。
            # 両者とも同期コンストラクタで接続は張らないため実コストは無い。
            llm = create_llm_client(config, "reflection")
            embed_fn = make_embed_fn(config)
        ran = slot.try_run_scheduled(
            _process_one, config, store, orch_store, llm, embed_fn, order)
        if not ran:
            break   # slot busy — 残りは次回
