# tests/test_endpoint_gate.py
import asyncio
import threading
import time

from src.llm.endpoint_gate import endpoint_gate


def test_same_endpoint_serializes():
    order: list[str] = []

    async def _job(name: str, hold: float):
        async with endpoint_gate("http://localhost:8080"):
            order.append(f"{name}:start")
            await asyncio.sleep(hold)
            order.append(f"{name}:end")

    async def _main():
        await asyncio.gather(_job("a", 0.05), _job("b", 0.01))

    asyncio.run(_main())
    assert order in (
        ["a:start", "a:end", "b:start", "b:end"],
        ["b:start", "b:end", "a:start", "a:end"],
    )


def test_different_endpoints_do_not_block():
    t0 = time.monotonic()

    async def _job(url: str):
        async with endpoint_gate(url):
            await asyncio.sleep(0.1)

    async def _main():
        await asyncio.gather(_job("http://a:1"), _job("http://b:2"))

    asyncio.run(_main())
    elapsed = time.monotonic() - t0
    assert elapsed < 0.19  # 直列なら ~0.2s、並行なら ~0.1s


def test_released_on_exception():
    async def _fail():
        async with endpoint_gate("http://c:3"):
            raise RuntimeError("boom")

    async def _ok():
        async with endpoint_gate("http://c:3"):
            return True

    async def _main():
        try:
            await _fail()
        except RuntimeError:
            pass
        return await asyncio.wait_for(_ok(), timeout=1.0)

    assert asyncio.run(_main()) is True


def test_cross_thread_serialization():
    order: list[str] = []

    def _thread_job(name: str, hold: float):
        async def _run():
            async with endpoint_gate("http://shared:9"):
                order.append(f"{name}:start")
                await asyncio.sleep(hold)
                order.append(f"{name}:end")
        asyncio.run(_run())

    t1 = threading.Thread(target=_thread_job, args=("x", 0.05))
    t2 = threading.Thread(target=_thread_job, args=("y", 0.05))
    t1.start(); t2.start()
    t1.join(timeout=5); t2.join(timeout=5)
    assert len(order) == 4
    assert order[1].endswith(":end") and order[3].endswith(":end")
    assert order[0].split(":")[0] == order[1].split(":")[0]


def test_cancelled_waiter_does_not_leak_semaphore():
    URL = "http://cancel:7"

    async def _main():
        release_holder = asyncio.Event()

        async def _holder():
            async with endpoint_gate(URL):
                await release_holder.wait()

        holder = asyncio.create_task(_holder())
        await asyncio.sleep(0.05)  # holder acquires

        async def _waiter():
            async with endpoint_gate(URL):
                pass  # should never reach

        waiter = asyncio.create_task(_waiter())
        await asyncio.sleep(0.05)  # waiter starts waiting
        waiter.cancel()
        try:
            await waiter
        except asyncio.CancelledError:
            pass

        release_holder.set()  # holder releases
        await holder

        async def _third():
            async with endpoint_gate(URL):
                return True

        return await asyncio.wait_for(_third(), timeout=2.0)

    assert asyncio.run(_main()) is True


def test_cancel_racing_acquisition_no_double_release():
    """cancel が acquire 完了と同一 tick で届いても double-release しない。"""
    URL = "http://race:11"

    async def _main():
        # holder が gate を保持
        release_holder = asyncio.Event()
        async def _holder():
            async with endpoint_gate(URL):
                await release_holder.wait()
        holder = asyncio.create_task(_holder())
        await asyncio.sleep(0.02)

        # waiter を起こしてすぐ (single tick 後) cancel — acquire 完了直前/直後の境界を狙う
        async def _waiter():
            async with endpoint_gate(URL):
                await asyncio.sleep(0.01)
        waiter = asyncio.create_task(_waiter())
        await asyncio.sleep(0)  # single tick yield only
        release_holder.set()    # holder release と waiter cancel を近接させる
        waiter.cancel()
        try:
            await waiter
        except asyncio.CancelledError:
            pass
        await holder

        # gate は解放済みのはず: 2 連続取得が両方成功する (permit=1 が leak/double でない)
        async def _acq():
            async with endpoint_gate(URL):
                return True
        assert await asyncio.wait_for(_acq(), timeout=2.0) is True
        assert await asyncio.wait_for(_acq(), timeout=2.0) is True

    asyncio.run(_main())


def test_llamacpp_chat_serializes_via_gate(monkeypatch):
    """LlamaCppClient.chat が同一 endpoint で直列化される (gate 経由)。"""
    from src.llm import client as client_mod
    from src.llm.llamacpp_client import LlamaCppClient

    # CB singleton をリセットして CLOSED (allow_request=True) を保証。
    # gate の直列化だけを観測し、full-suite の順序汚染で OPEN 状態が残る可能性を排除する。
    client_mod._circuit_breakers.pop("llamacpp", None)

    client = LlamaCppClient(base_url="http://serialize-test:8080", model="m")

    active = {"n": 0, "max": 0}

    async def _fake_do_chat(messages, temperature=0.1):
        active["n"] += 1
        active["max"] = max(active["max"], active["n"])
        await asyncio.sleep(0.02)
        active["n"] -= 1
        return "ok"

    monkeypatch.setattr(client, "_do_chat", _fake_do_chat)

    async def _main():
        results = await asyncio.gather(
            *[client.chat([{"role": "user", "content": "x"}]) for _ in range(4)]
        )
        return results

    results = asyncio.run(_main())
    # gate が効いていれば同時実行は常に 1 (max==1)。CB で弾かれれば結果が "ok" にならない。
    assert results == ["ok", "ok", "ok", "ok"]
    assert active["max"] == 1
