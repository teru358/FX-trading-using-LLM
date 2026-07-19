"""main.py の API 起動配線の単体テスト。

main() は monolithic なので、API 用 OrchestratorStore の生成可否だけを
`_api_orchestrator_store` ヘルパーに抽出してテストする (実装後レビュー Low-Med:
orchestrator 無効時に不要な DB を作らない)。
"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from main import _api_orchestrator_store
from src.data.orchestrator_store import OrchestratorStore


def _make_config(*, orchestrator_enabled: bool, prices_db_path):
    return SimpleNamespace(
        orchestrator=SimpleNamespace(enabled=orchestrator_enabled),
        prices_db_path=prices_db_path,
    )


def test_api_orchestrator_store_none_when_orchestrator_disabled(tmp_path):
    """orchestrator.enabled=False なら None を返し、DB ファイルも作らない。"""
    db_path = tmp_path / "prices.db"
    config = _make_config(orchestrator_enabled=False, prices_db_path=db_path)

    result = _api_orchestrator_store(config)

    assert result is None
    assert not db_path.exists()


def test_api_orchestrator_store_created_when_orchestrator_enabled(tmp_path):
    """orchestrator.enabled=True なら OrchestratorStore インスタンスを返す。"""
    db_path = tmp_path / "prices.db"
    config = _make_config(orchestrator_enabled=True, prices_db_path=db_path)

    result = _api_orchestrator_store(config)

    assert isinstance(result, OrchestratorStore)


# ── reflection ジョブ登録 (plan Task 8) ────────────────────────────────


def test_reflection_guard_exists_without_skip_predicate():
    """reflection guard が存在し、休場スキップを持たないこと (spec §3.6)。

    決済は休場中も残るため market_skip_check を付けてはいけない。
    """
    import main

    guard = main._guards["reflection"]
    assert guard.name == "reflection"
    assert guard._skip_predicate is None


@contextmanager
def _isolated_schedule():
    """グローバル schedule レジストリを退避し、空の状態でテストへ貸し出す。"""
    import schedule as sched_mod

    saved = list(sched_mod.jobs)
    sched_mod.clear()
    try:
        yield sched_mod
    finally:
        sched_mod.clear()
        sched_mod.jobs.extend(saved)


def test_reflection_jobs_registered_hourly_via_guard():
    """main._register_reflection_jobs を実際に呼び、実レジストリの中身を検証する。

    テスト側ではジョブを 1 件も登録しない。登録するのは main.py の実物だけなので、
    登録処理を消す/頻度を変える/guard を外すといった変更はすべてここで落ちる。

    期待時刻は spec §3.6 の「毎時」をハードコードする。実装から導出すると
    「実装が自分自身と一致すること」を確かめるだけの自己参照テストに戻る。
    """
    import main
    from src.cycles.reflection import run_reflection_cycle

    expected_times = [f"{h:02d}:00" for h in range(24)]  # spec §3.6: 毎時

    with _isolated_schedule() as sched_mod:
        main._register_reflection_jobs(
            "Asia/Tokyo", run_reflection_cycle, "cfg", "store", "reflect_store",
        )
        jobs = list(sched_mod.jobs)

        # 毎時 24 スロット・各正時 (頻度低下・登録漏れをここで検出する)
        assert len(jobs) == 24
        assert sorted(j.at_time.strftime("%H:%M") for j in jobs) == expected_times

        for j in jobs:
            # guard 経由であること (_run_with_slot への差し替えを検出する)
            assert j.job_func.func is main._run_with_guard
            args = j.job_func.args
            assert args[0] is main._guards["reflection"]
            assert args[1] is run_reflection_cycle
            assert args[2:] == ("cfg", "store", "reflect_store")
            # LLM slot は guard 配下へ渡される (spec §3.6)
            assert j.job_func.keywords == {"slot": main._llm_slot}


def test_main_calls_reflection_registration_seam():
    """main() が seam を呼んでおり、直接ループで登録し直していないこと。"""
    import inspect

    import main

    src = inspect.getsource(main.main)
    assert "_register_reflection_jobs(" in src
    # slot 経由 (_run_with_slot) ではなく guard 配下であること (spec §3.6)
    assert "_run_with_slot, run_reflection_cycle" not in src
    # technical の cadence を借用しないこと (reflection は毎時固定の独立要件)
    assert "_register_reflection_jobs(\n        technical_times" not in src
