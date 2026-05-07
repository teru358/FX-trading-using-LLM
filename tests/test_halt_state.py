"""HaltState read/write/mutate のユニットテスト。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.persistence import halt_state
from src.persistence.halt_state import (
    HaltState,
    mutate,
    read,
    write,
)


def test_read_returns_default_when_missing(tmp_path: Path):
    """halt.json 不在時は default (soft_halted=False) を返し、ファイルは作らない。"""
    state = read(tmp_path)
    assert state.soft_halted is False
    assert state.auto_triggered is False
    assert state.reason == ""
    assert state.since is None
    assert state.triggered_by == ""
    # halt.json は存在しない (balance.json と異なり bootstrap しない)
    assert not (tmp_path / "halt.json").exists()


def test_write_then_read_roundtrip(tmp_path: Path):
    """write → read で同一の HaltState を取得できる。"""
    state = HaltState(
        soft_halted=True,
        auto_triggered=True,
        reason="3 consecutive /health failures",
        since="2026-05-05T03:47:48+00:00",
        triggered_by="heartbeat",
    )
    write(tmp_path, state)
    assert read(tmp_path) == state
    # ディスクには JSON として書き込まれている
    p = tmp_path / "halt.json"
    assert json.loads(p.read_text())["soft_halted"] is True


def test_read_returns_halted_on_corruption(tmp_path):
    """halt.json が JSON 破損していたら fail-safe で soft_halted=True を返す。"""
    p = tmp_path / "halt.json"
    p.write_text("{this is not json", encoding="utf-8")

    s = halt_state.read(tmp_path)
    assert s.soft_halted is True
    assert s.triggered_by == "corruption"
    assert "corrupted" in s.reason


def test_read_returns_halted_on_schema_mismatch(tmp_path):
    """halt.json のキー欠損も fail-safe で halted を返す。"""
    p = tmp_path / "halt.json"
    p.write_text('{"soft_halted": true}', encoding="utf-8")  # 他のキー欠損

    s = halt_state.read(tmp_path)
    assert s.soft_halted is True
    assert s.triggered_by == "corruption"


def test_is_halted_corruption_fail_safe(tmp_path):
    """is_halted も corruption 時に True を返す。"""
    p = tmp_path / "halt.json"
    p.write_text("garbage", encoding="utf-8")
    assert halt_state.is_halted(tmp_path) is True


from src.persistence.halt_state import (
    clear,
    is_halted,
    trigger_auto,
    trigger_manual,
)


def test_trigger_auto_first_call_sets_state_and_returns_changed(tmp_path: Path):
    """初回 trigger_auto は changed=True で state を書く。"""
    state, changed = trigger_auto(
        tmp_path, reason="3 consecutive /health failures", triggered_by="heartbeat"
    )
    assert changed is True
    assert state.soft_halted is True
    assert state.auto_triggered is True
    assert state.reason == "3 consecutive /health failures"
    assert state.triggered_by == "heartbeat"
    assert state.since  # non-empty ISO string
    # ディスクにも反映
    assert read(tmp_path) == state


def test_trigger_auto_when_already_halted_is_noop(tmp_path: Path):
    """既に halted 中の trigger_auto は changed=False で既存 state を返す。"""
    first, _ = trigger_auto(tmp_path, reason="first", triggered_by="heartbeat")
    second, changed = trigger_auto(
        tmp_path, reason="second", triggered_by="order_unreachable"
    )
    assert changed is False
    # state は first のまま (reason / triggered_by 上書きされない)
    assert second == first


def test_trigger_manual_first_call_sets_state(tmp_path: Path):
    """trigger_manual は auto_triggered=False で state を書く。"""
    state, changed = trigger_manual(tmp_path, reason="user requested halt")
    assert changed is True
    assert state.soft_halted is True
    assert state.auto_triggered is False
    assert state.triggered_by == "manual"
    assert state.reason == "user requested halt"


def test_trigger_manual_when_already_halted_is_noop(tmp_path: Path):
    """既に halted 中の trigger_manual も changed=False。"""
    trigger_manual(tmp_path, reason="first")
    second, changed = trigger_manual(tmp_path, reason="second")
    assert changed is False


def test_clear_resets_to_default(tmp_path: Path):
    """clear は全フィールドを default にリセット。"""
    trigger_auto(tmp_path, reason="x", triggered_by="heartbeat")
    cleared = clear(tmp_path)
    assert cleared.soft_halted is False
    assert cleared.auto_triggered is False
    assert cleared.reason == ""
    assert cleared.since is None
    assert cleared.triggered_by == ""


def test_clear_is_idempotent_when_not_halted(tmp_path: Path):
    """halt.json 不在から clear しても default を返す。"""
    cleared = clear(tmp_path)
    assert cleared.soft_halted is False


def test_is_halted_reads_state(tmp_path: Path):
    """is_halted は read を経由して soft_halted を返す。"""
    assert is_halted(tmp_path) is False
    trigger_auto(tmp_path, reason="x", triggered_by="heartbeat")
    assert is_halted(tmp_path) is True
    clear(tmp_path)
    assert is_halted(tmp_path) is False


def test_mutate_is_atomic_under_lock(tmp_path: Path):
    """mutate が read-modify-write をロック下で実行 (同一プロセス race-free)。"""
    # 単純な race 検証は難しいので、最低限 mutate が書込を行い、結果が永続化される
    # ことを確認する (StateStore と同じ _get_state_lock を共有することは実装契約)
    trigger_manual(tmp_path, reason="initial")
    new_state = mutate(
        tmp_path,
        lambda s: HaltState(
            soft_halted=s.soft_halted,
            auto_triggered=s.auto_triggered,
            reason=s.reason + "_mutated",
            since=s.since,
            triggered_by=s.triggered_by,
        ),
    )
    assert new_state.reason == "initial_mutated"
    assert read(tmp_path) == new_state


def test_trigger_auto_changed_returns_true_only_on_first_call(tmp_path):
    """trigger_auto の changed は 1 回目のみ True、以降の二重発動では False。

    smoke test: 同一 cycle 内の連続発動が冪等であることを確認する。
    実際の race condition (並行スレッドからの呼出) は test_trigger_auto_atomic_under_concurrency
    で扱う。
    """
    _, c1 = halt_state.trigger_auto(tmp_path, "first", "heartbeat")
    _, c2 = halt_state.trigger_auto(tmp_path, "second", "order_unreachable")
    _, c3 = halt_state.trigger_auto(tmp_path, "third", "price_provider")
    assert (c1, c2, c3) == (True, False, False)


def test_trigger_auto_atomic_under_concurrency(tmp_path):
    """並行スレッドから同時に trigger_auto を呼んでも changed=True は 1 回だけ。

    旧実装は lock 外で `before_halted = read(...)` を行っていたため、両スレッドが
    halted=False を読んだ直後に交互に mutate に入ると、両方が changed=True と判定し
    Discord 通知が二重発火する race があった。captured["changed"] を _swap 内で
    確定する atomic 版なら、後発スレッドは current.soft_halted=True を見て changed=False
    のまま終わる。
    """
    import threading

    results: list[bool] = []
    barrier = threading.Barrier(8)

    def worker(idx: int) -> None:
        barrier.wait()  # 全スレッド揃ってから一斉発火
        _, changed = halt_state.trigger_auto(
            tmp_path, f"worker {idx}", "heartbeat"
        )
        results.append(changed)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 8 スレッドのうち changed=True を返したのは 1 つだけ
    assert results.count(True) == 1
    assert results.count(False) == 7


def test_trigger_auto_changed_when_corruption(tmp_path):
    """corruption 状態 (read で fail-safe halted) を経由しても trigger_auto は changed=False を返す。

    fail-safe halted が既に立っているので、新たな halt は重複扱い。
    """
    p = tmp_path / "halt.json"
    p.write_text("garbage", encoding="utf-8")
    # 1 回目: corruption により既に halted 状態 → changed=False
    _new, changed = halt_state.trigger_auto(tmp_path, "heartbeat fail", "heartbeat")
    assert changed is False
