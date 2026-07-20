"""スケジューラ登録が単一の timezone を通ることの回帰ガード。

背景: config 再編成 (2026-07-20) の初版設計は news_collection.timezone を
デッドコードと誤判定した。main.py の schedule 登録経路にテストが無く、
既存テストを全件通しても検出できなかったため、ここを塞ぐ。
"""
import pytest
import schedule as schedule_lib

from main import daily_at


def test_daily_at_uses_given_timezone():
    """daily_at で登録したジョブに tz が渡ること。

    schedule ライブラリは at_time_zone に tzinfo オブジェクトを保持する
    (文字列ではない) ため、str() で比較する。
    """
    schedule_lib.clear()
    try:
        daily_at("09:00", "Asia/Tokyo").do(lambda: None)
        job = schedule_lib.get_jobs()[0]
        assert str(job.at_time_zone) == "Asia/Tokyo"
    finally:
        schedule_lib.clear()


def test_daily_at_rejects_empty_timezone():
    """tz 未指定の登録を弾く。

    旧構成では tz / news_tz / fetch_timezone の3系統が混在し、
    どれを渡すかが登録箇所ごとにばらついていた。空を弾くことで
    「渡し忘れて OS ローカル TZ に落ちる」経路を塞ぐ。
    """
    schedule_lib.clear()
    try:
        with pytest.raises(ValueError, match="timezone"):
            daily_at("09:00", "")
    finally:
        schedule_lib.clear()
