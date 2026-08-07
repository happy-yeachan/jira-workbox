"""Bounded concurrency, in one place.

Both shapes of work in this tool — fanning a read across N objects, and
applying N writes — want the same thing: at most K requests in flight, results
as soon as each lands, and no task left running when the consumer goes away.

`map_bounded` yields instead of taking a callback, because the caller is
usually an async generator that has to `yield` an SSE progress event per
result, and a callback cannot do that.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


def chunked(items: Sequence[T], size: int) -> Iterator[list[T]]:
    """Split into fixed-size chunks (the last one may be shorter)."""
    if size < 1:
        raise ValueError("chunk size must be >= 1")
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


async def map_bounded(
    items: Sequence[T],
    worker: Callable[[T], Awaitable[R]],
    *,
    limit: int = 8,
    ordered: bool = False,
) -> AsyncIterator[tuple[int, T, R]]:
    """Run ``worker`` over ``items`` with at most ``limit`` in flight.

    Yields ``(index, item, result)`` as each finishes — completion order by
    default, input order with ``ordered=True`` (which buffers results that
    finish early, so it costs memory but makes tables and CSV deterministic).

    ``worker`` is expected not to raise: return a result object that describes
    the failure, so one bad item cannot abort the run. If it does raise, the
    remaining tasks are cancelled and awaited before the exception propagates.
    The same cleanup runs when the consumer stops iterating (client disconnect),
    so nothing outlives this iterator.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")

    source = iter(enumerate(items))
    pending: set[asyncio.Task[tuple[int, T, R]]] = set()
    buffered: dict[int, tuple[T, R]] = {}
    next_out = 0

    async def run(index: int, item: T) -> tuple[int, T, R]:
        return index, item, await worker(item)

    def spawn() -> bool:
        entry = next(source, None)
        if entry is None:
            return False
        index, item = entry
        pending.add(asyncio.create_task(run(index, item)))
        return True

    try:
        for _ in range(limit):
            if not spawn():
                break

        while pending:
            done, still = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            pending = set(still)
            for task in done:
                index, item, result = task.result()
                spawn()
                if not ordered:
                    yield index, item, result
                    continue
                buffered[index] = (item, result)
                while next_out in buffered:
                    held_item, held_result = buffered.pop(next_out)
                    yield next_out, held_item, held_result
                    next_out += 1
    finally:
        for task in pending:
            if not task.done():
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
