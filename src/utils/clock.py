"""時刻取得の統一ヘルパー。

`datetime.now()` の直呼びは tz-aware と naive を混在させて
``TypeError: can't subtract offset-naive and offset-aware datetimes`` を
発生させる潜在バグの温床。本モジュールが用途別に明示的な API を提供する。

| 用途 | ヘルパー | 戻り値 |
|---|---|---|
| 経過時間測定 (elapsed) | ``monotonic_now()`` | float 秒、単調増加 |
| ローカルタイム表示・スケジューラ | ``local_now(config)`` | tz-aware ``datetime`` |
| クロスタイムゾーン比較 | ``utc_now()`` | tz-aware UTC ``datetime`` |
| SQLite TIMESTAMP 列への書き込み | ``db_now()`` | naive datetime (現状規約: machine local) |
| 一部モジュール固定の naive UTC | ``db_utc_now()`` | naive UTC datetime |

**現状の DB 規約:** 既存の本番 SQLite データは ``datetime.now()`` の戻り値
(naive machine-local) をそのまま書き込んでいる。``db_now()`` はこの規約を
維持する。将来 UTC 統一に移行する場合は既存データのマイグレーションが必要。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from src.config import AppConfig


def utc_now() -> datetime:
    """tz-aware UTC 現在時刻。タイムゾーンを跨いで安全に比較できる。"""
    return datetime.now(timezone.utc)


def local_now(config: "AppConfig") -> datetime:
    """tz-aware ローカル (``config.timezone``) 現在時刻。

    スケジューラ起点の時刻表示・cycle 開始時刻記録に使用する。
    注意: ``db_now()`` は OS ローカル TZ であり、この設定とは独立。
    """
    return datetime.now(ZoneInfo(config.timezone))


def db_now() -> datetime:
    """naive ローカル時刻 — SQLite TIMESTAMP 列の既存データ規約に合わせる。

    既存の本番 DB は naive ローカルで書き込まれているため、新規書き込みも
    同じ規約を維持する。将来 UTC に統一したい場合はマイグレーションが必要。
    """
    return datetime.now()


def db_utc_now() -> datetime:
    """naive UTC 時刻 — ``econ_event_store`` のような UTC 固定モジュール用。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _resolve_local_tz(local_tz):
    """local_tz 未指定ならマシンのローカル TZ を返す。"""
    if local_tz is not None:
        return local_tz
    return datetime.now().astimezone().tzinfo


def to_db_naive_datetime(dt: datetime, local_tz=None) -> datetime:
    """tz-aware datetime を DB 規約 (naive machine-local) に変換する。

    DB 規約は ``db_now()`` = naive machine-local。tz-aware を ``replace(tzinfo=None)``
    で直に剥がすと (= naive UTC) JST 環境で db_now() との比較が 9 時間ズレる。UTC→
    ローカル変換してから tz を剥がす。naive 入力はそのまま返す。``local_tz`` を注入
    すると実行環境 TZ に依存せずテストできる。
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(_resolve_local_tz(local_tz)).replace(tzinfo=None)


def to_db_naive_index(index, local_tz=None):
    """tz-aware な pandas DatetimeIndex を DB 規約 (naive machine-local) に変換する。

    ``to_db_naive_datetime`` の index 版。tz-naive はそのまま返す。
    """
    if getattr(index, "tz", None) is not None:
        return index.tz_convert(_resolve_local_tz(local_tz)).tz_localize(None)
    return index


def monotonic_now() -> float:
    """単調増加クロック — 経過時間測定専用 (``time.monotonic()`` 薄ラッパー)。

    システム時刻調整 (NTP, タイムゾーン変更) の影響を受けず、float 秒で
    elapsed を返す。表示・保存には使えない (絶対時刻を持たないため)。
    """
    return time.monotonic()
