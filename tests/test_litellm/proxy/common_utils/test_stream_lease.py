import asyncio

import pytest

from litellm.proxy.common_utils.sse_keepalive import StreamLease


class _NeverProducingGen:
    def __init__(self):
        self.aclose_calls = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(3600)  # never yields

    async def aclose(self):
        self.aclose_calls += 1


@pytest.mark.asyncio
async def test_close_is_idempotent_single_upstream_close():
    gen = _NeverProducingGen()
    task = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0)  # let task start
    lease = StreamLease(inner=gen, pending_task=task)
    await asyncio.gather(lease.close(), lease.close())  # concurrent double close
    assert gen.aclose_calls == 1
    assert task.cancelled() or task.done()
    await asyncio.sleep(0)
    assert task not in asyncio.all_tasks()  # no orphan


@pytest.mark.asyncio
async def test_close_cancels_task_before_closing_producer():
    order = []

    class G:
        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                order.append("cancelled")
                raise

        async def aclose(self):
            order.append("aclosed")

    g = G()
    task = asyncio.ensure_future(g.__anext__())
    await asyncio.sleep(0)
    await StreamLease(inner=g, pending_task=task).close()
    assert order == ["cancelled", "aclosed"]


@pytest.mark.asyncio
async def test_close_without_pending_task_still_closes_producer():
    gen = _NeverProducingGen()
    lease = StreamLease(inner=gen)  # no pending task
    await lease.close()
    assert gen.aclose_calls == 1


@pytest.mark.asyncio
async def test_set_pending_task_is_used_by_close():
    gen = _NeverProducingGen()
    lease = StreamLease(inner=gen)
    task = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0)
    lease.set_pending_task(task)
    await lease.close()
    assert task.cancelled() or task.done()
    assert gen.aclose_calls == 1
