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
from datetime import datetime, timezone
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


def _raw_closed_at(d: dict) -> datetime | None:
    """raw 行の closed_at を naive datetime にする。欠落・不正なら None。

    tzinfo は必ず落とす (レビュー LOW-A)。混在したまま比較すると
    `can't compare offset-naive and offset-aware datetimes` が飛び、
    この式は行ごとの try の外なので trades.json 全体が処理不能になる
    (HIGH-2 と同じ failure mode)。現状の write 経路は全て naive db_now()
    だが、その不変条件はこのファイルの外にある — mt5_bridge_broker.py が
    bridge 供給テキストに無防備な fromisoformat をかけるため、offset 付き
    payload が来れば伝播しうる。

    offset 付きは UTC に寄せてから naive 化する。単に tzinfo を捨てると
    「+09:00 の 10:00」と「naive の 10:00」が同値になり、db_now() 規約
    (naive machine-local) と 9 時間ずれる。clock.py の db_utc_now() と
    同じ正規化。
    """
    v = d.get("closed_at")
    if not isinstance(v, str):
        return None
    try:
        ts = datetime.fromisoformat(v)
    except ValueError:
        return None
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts


def _dedupe_raw_rows(raw: list) -> list:
    """order_id ごとに候補を 1 件へ絞る。

    trades.json は append-only で order_id 一意性を保証しない (append_trade)。
    採用するのは「実際の約定が新しい行」= closed_at が新しい方で、比較できない
    ときのみ最終出現行に倒す。

    append 順と closed_at は decoupled であることに注意 (レビュー HIGH-1)。
    append 順 = bot が決済に気づいた時刻だが、closed_at は MT5 deal history の
    実約定時刻 (mt5_bridge_broker.py が close_position_with_result へ渡し、
    position_manager.py の `closed_at or db_now()` が書く)。サーバー側 SL/TP が
    bot 停止中に発火し、locally observed close の後に reconcile されると
    後着行の closed_at が先着より古くなる。最終出現規則だけだとこの経路で
    古い約定情報を採ってしまうため、closed_at を正本にする。

    絞り込みを parse より前に置くのが要点 (レビュー MEDIUM)。parse 後に潰すと、
    後着が壊れているとき候補が先着 1 件だけになり、古い情報で done 確定して
    後着の補正内容が永久に失われる。raw 段階で 1 件にすれば、後着が壊れて
    いれば先着へフォールバックせず retry に留まる。1 order_id につき parse
    試行が 1 回になるため、壊れた重複行による attempt の多重進行も解消する。

    order_id が無い / 文字列でない行は潰しようがないのでそのまま通す
    (parse 側が従来どおり行ごとに弾く)。ここで例外を出すと行ごとの try の外
    なので trades.json 全体が処理不能になる (レビュー HIGH-2)。
    """
    chosen: dict[str, int] = {}     # order_id → raw 内の採用インデックス
    passthrough: list[int] = []
    for i, d in enumerate(raw):
        oid = d.get("order_id") if isinstance(d, dict) else None
        # str 以外 (list 等) は hashable とは限らず dict キーにできない。
        if not oid or not isinstance(oid, str):
            passthrough.append(i)
            continue
        prev = chosen.get(oid)
        if prev is None:
            chosen[oid] = i
            continue
        logger.warning(
            f"[REFLECT] duplicate order_id in trades.json: {oid} "
            f"— keeping the row with the newest closed_at")
        prev_ts = _raw_closed_at(raw[prev])
        cur_ts = _raw_closed_at(d)
        if prev_ts is None or cur_ts is None:
            chosen[oid] = i         # 比較不能 — append-only の最終出現に倒す
        elif cur_ts >= prev_ts:
            chosen[oid] = i
    keep = sorted(set(chosen.values()) | set(passthrough))
    return [raw[i] for i in keep]


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

    重複 order_id は parse 前に最終出現行へ絞る (レビュー MEDIUM、
    _dedupe_raw_rows)。ここが重複排除の正本で、_select_targets 側では行わない。
    """
    if not isinstance(raw, list):
        logger.warning(f"[REFLECT] trades.json is not a list "
                       f"({type(raw).__name__}) — skipped")
        return []
    orders: list[Order] = []
    for d in _dedupe_raw_rows(raw):
        if not isinstance(d, dict):
            logger.warning(f"[REFLECT] non-dict trade row skipped: {d!r:.80}")
            continue
        oid = d.get("order_id")
        # str 以外は states の dict キーに使えない (unhashable もあり得る)。
        # ここは行ごとの try の外なので、例外を出すと trades.json 全体が
        # 処理不能になる (レビュー HIGH-2)。状態照会を諦めて parse へ回し、
        # 従来どおり Order.from_dict の try で 1 行に封じ込める。
        if not isinstance(oid, str):
            oid = None
        # dedupe → _is_due の順。_is_due は order_id だけをキーにするため
        # 重複グループの全行を同一に扱う (グループを分割しない)。よって
        # 逆順でも結果は同じで、この順序は候補を先に絞る分だけ安価という
        # 理由で選んでいる — 正しさが依存しているわけではない。
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

    重複 order_id の排除はここでは行わない — 正本は parse 前の
    _dedupe_raw_rows (レビュー MEDIUM)。両段で潰すとどちらが正本か不明瞭に
    なるうえ、parse 後に潰しても壊れた後着行は既に候補から消えている。
    """
    if states is None:
        states = _load_states(orch_store)

    def _ts(o: Order) -> datetime:
        return o.closed_at or o.opened_at

    eligible = [o for o in closed if _is_due(states.get(o.order_id), now)]
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
