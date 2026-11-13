import asyncio

import pytest

from crows.agent_concurrency import agent_execution_slot


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_consume_global_execution_slot():
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def hold_slot():
        async with agent_execution_slot(1):
            entered.set()
            await blocked.wait()

    holder = asyncio.create_task(hold_slot())
    await entered.wait()

    async def wait_for_slot():
        async with agent_execution_slot(1):
            return "acquired"

    waiter = asyncio.create_task(wait_for_slot())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder

    async with agent_execution_slot(1):
        assert True


@pytest.mark.asyncio
async def test_global_execution_slot_rejects_invalid_limit():
    for invalid in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            async with agent_execution_slot(invalid):
                pass
