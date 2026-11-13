"""Cancellation must not abandon running work or hide its failure."""

import asyncio

import pytest

from projecthub.contracts import finish_task, run_blocking


def test_cancelled_waiter_keeps_work_until_completion():
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        completed = []

        async def work():
            started.set()
            await release.wait()
            completed.append(True)
            return 42

        task = asyncio.create_task(work())
        waiter = asyncio.create_task(finish_task(task))
        await asyncio.wait_for(started.wait(), 3)
        for message in ("first", "again"):
            waiter.cancel(message)
            await asyncio.sleep(0)
            assert not waiter.done()
            assert not task.cancelled()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert completed == [True]
        assert task.result() == 42

    asyncio.run(scenario())


def test_work_failure_is_not_hidden_by_waiter_cancellation():
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        failure = OSError("operation failed")

        async def work():
            started.set()
            await release.wait()
            raise failure

        waiter = asyncio.create_task(finish_task(asyncio.create_task(work())))
        await asyncio.wait_for(started.wait(), 3)
        waiter.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(OSError) as raised:
            await waiter
        assert raised.value is failure

    asyncio.run(scenario())


def test_blocking_work_returns_values_and_propagates_errors():
    async def scenario():
        assert await run_blocking(pow, 2, exp=3) == 8
        with pytest.raises(ZeroDivisionError):
            await run_blocking(lambda: 1 / 0)

    asyncio.run(scenario())
