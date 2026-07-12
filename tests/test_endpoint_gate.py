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
