"""reflection の closed_at 正規化が DB 規約 (naive machine-local) に従うこと。

レビュー Medium: `_raw_closed_at` が offset 付きを **naive UTC** へ寄せていたため、
naive machine-local (= db_now() 規約、本番 JST) の行と比較すると 9 時間ずれた。

  10:00 naive local  vs  11:00+09:00 (= naive local 11:00)
  → 本来 11:00 の行を採るべきだが、UTC 正規化だと 11:00+09:00 が 02:00 UTC に
    なり 10:00 より古いと誤判定され、10:00 の行が残っていた。

正しい規約は clock.py の to_db_naive_datetime (astimezone(local) → tzinfo 剥がし)。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.cycles.reflection import _dedupe_raw_rows, _raw_closed_at
from src.utils.clock import to_db_naive_datetime

_LOCAL = datetime.now().astimezone().tzinfo
_UTC_OFFSET_HOURS = datetime.now().astimezone().utcoffset().total_seconds() / 3600.0


def test_raw_closed_at_matches_to_db_naive_datetime():
    """_raw_closed_at は clock.to_db_naive_datetime と同じ結果を返す。"""
    aware = datetime(2026, 7, 20, 11, 0, tzinfo=timezone(timedelta(hours=9)))

    got = _raw_closed_at({"closed_at": aware.isoformat()})

    assert got == to_db_naive_datetime(aware)
    assert got.tzinfo is None


def test_raw_closed_at_naive_passthrough():
    """naive 入力はそのまま (既に DB 規約)。"""
    naive = datetime(2026, 7, 20, 10, 0)

    assert _raw_closed_at({"closed_at": naive.isoformat()}) == naive


def test_aware_local_equals_same_wallclock_naive():
    """ローカル offset 付きは同じ壁時計の naive と一致する。"""
    wall = datetime(2026, 7, 20, 10, 0)
    aware_local = wall.replace(tzinfo=_LOCAL)

    assert _raw_closed_at({"closed_at": aware_local.isoformat()}) == wall


def test_jst_aware_and_naive_local_mixed_picks_newer():
    """JST aware と naive-local 混在で、壁時計の新しい方を採る (回帰)。

    レビュアー実測ケース: 10:00 naive local と 11:00+09:00。
    UTC 正規化だと 10:00 が残ってしまう (JST 環境)。
    """
    naive_older = {
        "order_id": "d1", "realized_pnl": 1.0,
        "closed_at": datetime(2026, 7, 20, 10, 0).isoformat()}
    aware_newer = {
        "order_id": "d1", "realized_pnl": 999.0,
        "closed_at": datetime(
            2026, 7, 20, 11, 0,
            tzinfo=timezone(timedelta(hours=_UTC_OFFSET_HOURS))).isoformat()}

    kept = _dedupe_raw_rows([naive_older, aware_newer])

    assert len(kept) == 1
    assert kept[0]["realized_pnl"] == 999.0, "naive-local 規約で比較されていない"


def test_naive_local_newer_than_aware_wins():
    """逆向き: naive-local が新しければそちらを採る。"""
    aware_older = {
        "order_id": "d1", "realized_pnl": 1.0,
        "closed_at": datetime(
            2026, 7, 20, 9, 0,
            tzinfo=timezone(timedelta(hours=_UTC_OFFSET_HOURS))).isoformat()}
    naive_newer = {
        "order_id": "d1", "realized_pnl": 999.0,
        "closed_at": datetime(2026, 7, 20, 15, 0).isoformat()}

    kept = _dedupe_raw_rows([aware_older, naive_newer])

    assert len(kept) == 1
    assert kept[0]["realized_pnl"] == 999.0


def test_cross_offset_comparison_still_correct():
    """異なる offset 同士は絶対時刻で正しく比較される。"""
    # 12:00 UTC は 18:00+09:00 より後 (18:00+09:00 = 09:00 UTC)。
    older = {
        "order_id": "d1", "realized_pnl": 1.0,
        "closed_at": datetime(
            2026, 7, 20, 18, 0,
            tzinfo=timezone(timedelta(hours=9))).isoformat()}
    newer = {
        "order_id": "d1", "realized_pnl": 999.0,
        "closed_at": datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc).isoformat()}

    kept = _dedupe_raw_rows([newer, older])

    assert len(kept) == 1
    assert kept[0]["realized_pnl"] == 999.0


def test_malformed_and_missing_return_none():
    assert _raw_closed_at({}) is None
    assert _raw_closed_at({"closed_at": "not-a-date"}) is None
    assert _raw_closed_at({"closed_at": 12345}) is None
