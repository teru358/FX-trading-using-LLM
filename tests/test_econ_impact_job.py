# tests/test_econ_impact_job.py
import asyncio
import inspect

from src.jobs import econ_impact_job as ej
from src.jobs import technical_collector as tc


def test_econ_impact_moved_out_of_collector():
    assert hasattr(ej, "collect_econ_impact")
    assert hasattr(ej, "run_econ_impact_collection")
    assert "_collect_econ_impact" not in inspect.getsource(tc)


def test_econ_job_disabled_returns_early():
    class _Cfg:
        class economic_calendar:
            enabled = False
    # enabled=False で即 return (store 未使用)。例外なく完了。
    asyncio.run(ej.collect_econ_impact(_Cfg(), None, None, None, []))


def test_status_initial_values():
    import importlib
    importlib.reload(ej)
    st = ej.get_econ_impact_status()
    assert st == {"last_attempt": None, "last_success": None, "pending": 0, "failed": 0}


def test_dynamic_lookback_widens_with_gap():
    from src.utils.clock import db_now
    from datetime import timedelta

    class _Cfg:
        class economic_calendar:
            refresh_lookback_min = 60

    # 成功実績なし → 72h (4320)
    with ej._status_lock:
        ej._status["last_success"] = None
    assert ej._dynamic_lookback_min(_Cfg()) == 4320
    # 直近成功 → 下限 (60)
    with ej._status_lock:
        ej._status["last_success"] = db_now().isoformat()
    assert ej._dynamic_lookback_min(_Cfg()) == 60
    # 3 時間前成功 → 180 + 60 = 240
    with ej._status_lock:
        ej._status["last_success"] = (db_now() - timedelta(hours=3)).isoformat()
    assert ej._dynamic_lookback_min(_Cfg()) == 240
    # 7 日前成功 → 上限 4320 (72h)
    with ej._status_lock:
        ej._status["last_success"] = (db_now() - timedelta(days=7)).isoformat()
    assert ej._dynamic_lookback_min(_Cfg()) == 4320


def test_finalize_status_failure_not_success():
    with ej._status_lock:
        ej._status.update(last_attempt=None, last_success=None, pending=0, failed=0)
    # フェーズ完走したが 3 件全滅 + DB 再取得で 3 件残存
    ej._finalize_status(completed=True, failed=3, pending=3)
    st = ej.get_econ_impact_status()
    assert st["last_success"] is None   # 成功扱いしない
    assert st["failed"] == 3
    assert st["pending"] == 3
    # 全件成功で初めて last_success が進む
    ej._finalize_status(completed=True, failed=0, pending=0)
    assert ej.get_econ_impact_status()["last_success"] is not None
