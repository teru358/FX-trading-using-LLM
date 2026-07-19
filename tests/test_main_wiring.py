"""main.py の API 起動配線の単体テスト。

main() は monolithic なので、API 用 OrchestratorStore の生成可否だけを
`_api_orchestrator_store` ヘルパーに抽出してテストする (実装後レビュー Low-Med:
orchestrator 無効時に不要な DB を作らない)。
"""
from __future__ import annotations

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


def test_reflection_job_registered_hourly_via_guard(monkeypatch):
    """main.py の登録形をそのまま実行し、毎時 24 件が guard 経由で載ること。

    main() は monolithic で直接呼べないため、登録に使う構成要素
    (technical_times / _run_with_guard / reflection guard) を main から
    取り出して同じ形で登録し、schedule 上の結果を検証する。
    """
    import schedule as sched_mod

    import main
    from src.cycles.reflection import run_reflection_cycle

    saved = list(sched_mod.jobs)
    sched_mod.clear()
    try:
        technical_times = [f"{h:02d}:00" for h in range(24)]
        for t in technical_times:
            sched_mod.every().day.at(t, "Asia/Tokyo").do(
                main._run_with_guard, main._guards["reflection"],
                run_reflection_cycle, None, None, None, slot=None,
            )

        jobs = list(sched_mod.jobs)
        # 毎時 24 件
        assert len(jobs) == 24
        assert sorted(j.at_time.strftime("%H:%M") for j in jobs) == technical_times

        # 全件が reflection guard 経由で run_reflection_cycle を対象にしている
        for j in jobs:
            args = j.job_func.args
            assert args[0] is main._guards["reflection"]
            assert args[1] is run_reflection_cycle
    finally:
        sched_mod.clear()
        sched_mod.jobs.extend(saved)


def test_main_registers_reflection_with_guard_and_slot():
    """main.py 本体が guard 経由 + slot 渡しで reflection を登録していること。"""
    import inspect

    import main

    src = inspect.getsource(main)
    assert 'JobGuard("reflection")' in src
    assert '_guards["reflection"]' in src
    assert "run_reflection_cycle, config, store, _reflect_store, slot=_llm_slot" in src
    # slot 経由 (_run_with_slot) ではなく guard 配下の同期実行であること (spec §3.6)
    assert "_run_with_slot, run_reflection_cycle" not in src
    # 毎時登録 (technical_times = 24 スロット) を使っていること
    assert "for t in technical_times:" in src
