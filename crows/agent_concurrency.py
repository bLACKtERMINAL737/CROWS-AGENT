"""Fair, process-wide admission control for specialist workflow execution.

The FastAPI runtime owns one event loop and the task store enforces a
single-server-process lock. A semaphore per event loop therefore coordinates
all workflows handled by that server without binding an asyncio primitive
to the module-import loop (which would break isolated test/event loops).
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from weakref import WeakKeyDictionary
from collections.abc import AsyncIterator


_semaphores: WeakKeyDictionary[asyncio.AbstractEventLoop, tuple[int, asyncio.Semaphore]] = WeakKeyDictionary()


def _semaphore_for_running_loop(limit: int) -> asyncio.Semaphore:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("Global agent execution limit must be a positive integer")
    loop = asyncio.get_running_loop()
    configured = _semaphores.get(loop)
    if configured is None:
        semaphore = asyncio.Semaphore(limit)
        _semaphores[loop] = (limit, semaphore)
        return semaphore
    configured_limit, semaphore = configured
    if configured_limit != limit:
        raise RuntimeError(
            "Crows global agent concurrency cannot change while the server event loop is running"
        )
    return semaphore


@asynccontextmanager
async def agent_execution_slot(limit: int) -> AsyncIterator[None]:
    """Acquire one FIFO-fair global execution slot and always release it."""
    semaphore = _semaphore_for_running_loop(limit)
    await semaphore.acquire()
    try:
        yield
    finally:
        semaphore.release()
