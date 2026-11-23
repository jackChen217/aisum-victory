"""How many AISUM searches this process runs at once, with users ahead of background work.

AISUM's limits are per team (read from https://acts.aedi.ai/status on 2026-11-22):
search_products_by_image 3 at once, search_products_by_text 8 at once. Going over
returns RATE_LIMITED. One outfit plan can already need 3 image searches together (crop
re-searches), so two people at once could go over without this.

- A user's search waits for a free slot.
- Background work (gallery refresh, precompute) runs inside `background()`: it only
  takes a slot that is free right now and nobody is waiting for. Otherwise it raises
  `Busy` and the caller drops the refresh. It never waits in front of a user.

The limit is per process. Cloud Run is capped at one instance (--max-instances=1) so
that this is the team's whole traffic.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
from collections import deque

AT_ONCE = {"search_products_by_image": 3, "search_products_by_text": 8}


class Busy(RuntimeError):
    """Background work found no free AISUM slot and stepped aside."""


class Gate:
    """A counting gate: waiters are served in order, and try_take never jumps the queue."""

    def __init__(self, size: int):
        self.size = size
        self.free = size
        self._waiters: deque[asyncio.Future] = deque()

    def try_take(self) -> bool:
        if self.free > 0 and not self._waiters:
            self.free -= 1
            return True
        return False

    async def take(self) -> None:
        if self.try_take():
            return
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        try:
            await fut
        except BaseException:
            if fut.done() and not fut.cancelled():
                self.give()          # handed a slot just as we were cancelled
            else:
                self._waiters.remove(fut)
            raise

    def give(self) -> None:
        while self._waiters:
            fut = self._waiters.popleft()
            if not fut.done():
                fut.set_result(None)  # the slot passes straight to the next waiter
                return
        self.free += 1


_gates: dict[str, Gate] = {}
_background = contextvars.ContextVar("aisum_background", default=False)
# Set when background work was refused a slot, so a half-done refresh can be dropped
# even if the caller swallowed the Busy (outfit crop re-search catches errors).
_refused = contextvars.ContextVar("aisum_refused", default=None)


def gate(tool: str) -> Gate | None:
    size = AT_ONCE.get(tool)
    if size is None:
        return None
    if tool not in _gates:
        _gates[tool] = Gate(size)
    return _gates[tool]


@contextlib.contextmanager
def background():
    """Run the block as background work. Yields a dict whose "refused" says whether any
    search inside was refused a slot (then the result is incomplete: drop it)."""
    state = {"refused": False}
    t1, t2 = _background.set(True), _refused.set(state)
    try:
        yield state
    finally:
        _background.reset(t1)
        _refused.reset(t2)


def is_background() -> bool:
    return _background.get()


@contextlib.asynccontextmanager
async def slot(tool: str):
    g = gate(tool)
    if g is None:
        yield
        return
    if _background.get():
        if not g.try_take():
            state = _refused.get()
            if state is not None:
                state["refused"] = True
            raise Busy(f"{tool}: no free slot for background work")
    else:
        await g.take()
    try:
        yield
    finally:
        g.give()


def reset() -> None:
    """Tests only: forget the gates (they belong to one event loop)."""
    _gates.clear()
