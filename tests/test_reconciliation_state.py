"""reconciliation_state.json の永続化テスト。

halt_state.py と同じパターンで state_dir 配下の JSON に連続 skip カウンタを
保存する。broker インスタンスを跨いで保持される必要があるため永続化する。
"""
from __future__ import annotations

from pathlib import Path

from src.persistence import reconciliation_state


def test_read_default_when_file_missing(tmp_path: Path):
    """state_dir に reconciliation.json が無ければ default (skipped=0) を返す。"""
    s = reconciliation_state.read(tmp_path)
    assert s.skipped_consecutive == 0
    assert s.stale_warned is False
    assert s.last_skip_at is None
    assert s.last_recovered_at is None


def test_increment_skip_writes_file(tmp_path: Path):
    """increment_skip で skipped_consecutive が +1 され JSON が書かれる。"""
    new = reconciliation_state.increment_skip(tmp_path)
    assert new.skipped_consecutive == 1
    assert new.last_skip_at is not None

    new2 = reconciliation_state.increment_skip(tmp_path)
    assert new2.skipped_consecutive == 2

    # 再読込で永続化を確認
    persisted = reconciliation_state.read(tmp_path)
    assert persisted.skipped_consecutive == 2


def test_mark_recovered_resets_counter(tmp_path: Path):
    """mark_recovered で skipped_consecutive が 0 に戻り stale_warned もリセット。"""
    reconciliation_state.increment_skip(tmp_path)
    reconciliation_state.increment_skip(tmp_path)
    reconciliation_state.mark_stale_warned(tmp_path)

    s = reconciliation_state.read(tmp_path)
    assert s.skipped_consecutive == 2
    assert s.stale_warned is True

    recovered = reconciliation_state.mark_recovered(tmp_path)
    assert recovered.skipped_consecutive == 0
    assert recovered.stale_warned is False
    assert recovered.last_recovered_at is not None


def test_mark_stale_warned_idempotent(tmp_path: Path):
    """mark_stale_warned は冪等 (2 回呼んでも fail しない)。"""
    reconciliation_state.increment_skip(tmp_path)
    reconciliation_state.mark_stale_warned(tmp_path)
    s1 = reconciliation_state.read(tmp_path)
    assert s1.stale_warned is True

    reconciliation_state.mark_stale_warned(tmp_path)
    s2 = reconciliation_state.read(tmp_path)
    assert s2.stale_warned is True


def test_read_handles_corrupted_json(tmp_path: Path):
    """JSON が壊れていれば default を返す (fail-safe)。"""
    (tmp_path / "reconciliation.json").write_text("{not valid json}", encoding="utf-8")
    s = reconciliation_state.read(tmp_path)
    assert s.skipped_consecutive == 0
    assert s.stale_warned is False


def test_write_uses_tmp_and_atomic_replace(tmp_path: Path):
    """write は tmp file 経由の atomic replace で部分書込みを露出させない。"""
    from dataclasses import replace
    state = replace(
        reconciliation_state.read(tmp_path),
        skipped_consecutive=5,
    )
    reconciliation_state.write(tmp_path, state)

    # tmp file は残らない
    assert not (tmp_path / "reconciliation.json.tmp").exists()
    # 本ファイルは正しく書かれている
    assert (tmp_path / "reconciliation.json").exists()
    reloaded = reconciliation_state.read(tmp_path)
    assert reloaded.skipped_consecutive == 5


def test_mutate_serializes_concurrent_increments(tmp_path: Path):
    """mutate() のロックで並行 increment が直列化される (race 防止)。"""
    import threading

    def _bump():
        for _ in range(20):
            reconciliation_state.increment_skip(tmp_path)

    threads = [threading.Thread(target=_bump) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = reconciliation_state.read(tmp_path)
    assert final.skipped_consecutive == 80, (
        "ロックが効いていれば 4 thread × 20 回 = 80 になる。"
        f"値 {final.skipped_consecutive} は read-modify-write race の兆候"
    )
