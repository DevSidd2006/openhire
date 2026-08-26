"""
Evaluation background-execution boundary (Chunk 4, Step 15).

`EvaluationService` never touches `asyncio.create_task` (or any other
concrete execution mechanism) directly. It depends only on the
`EvaluationDispatcher` protocol below - schedule a coroutine, get nothing
back. That is the entire seam a real, durable worker eventually plugs into:

    EvaluationService -> EvaluationDispatcher -> [temporary local execution]
                                               -> existing evaluation pipeline

Later, swap `AsyncTaskEvaluationDispatcher` for one backed by a durable
queue in `core/container.py` (the ONE place, exactly like every other
temporary stub in this backend - see repositories/memory.py) and nothing
in `EvaluationService` changes.

Why local `asyncio.create_task` and not a queue now
----------------------------------------------------
No message broker, task queue, or worker process exists anywhere in this
project today, and Chunk 4's brief is explicit: don't introduce
Redis/Celery/RabbitMQ/Kafka "merely because they are common choices," and
keep this chunk runnable locally. `asyncio.create_task` inside the same
process the API already runs in is the smallest mechanism that satisfies
the one hard requirement (Step 5: an HTTP request must not block on a full
multi-agent evaluation) without adding infrastructure this project has no
other use for yet.

What this honestly does NOT provide (documented, not hidden)
--------------------------------------------------------------
  * No cross-process durability: if the process restarts while an
    evaluation is RUNNING, that task is gone. The stored `EvaluationJob`
    is left at RUNNING with no automatic recovery - a real queue/worker is
    what "Step 15: production-worker dependency" hands to.
  * No back-pressure/concurrency limit: every triggered evaluation gets
    its own task immediately. Fine at today's scale; a real queue is
    exactly where that control belongs later.
  * A task's result is discarded by asyncio unless something keeps a
    reference to it - `create_task` returns a `Task` that would otherwise
    be free to be garbage-collected mid-flight (a well-known asyncio
    pitfall). This dispatcher keeps every in-flight task in a set for
    exactly that reason - see `schedule` below - so this ONE gotcha does
    not silently drop evaluations even at today's temporary scale.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Protocol, Set

from core.logging import get_logger, log_context

logger = get_logger("services.evaluation_dispatcher")


class EvaluationDispatcher(Protocol):
    """Schedule one evaluation's background execution.

    `run` is a zero-arg async callable (already bound to whatever it needs -
    see `EvaluationService.trigger_evaluation`'s `lambda: self._run(...)`) so
    this protocol never needs to know anything about evaluations,
    repositories, or agents. A durable-queue implementation would instead
    serialize `evaluation_id` onto the queue and let a separate worker
    process resolve `run` itself - a detail this protocol deliberately
    doesn't commit to either way.
    """

    def schedule(self, evaluation_id: str, run: Callable[[], Awaitable[None]]) -> None: ...


class AsyncTaskEvaluationDispatcher:
    """TEMPORARY local dispatcher - see module docstring.

    Must be a long-lived, container-level singleton (core/container.py),
    NOT recreated per request the way `EvaluationService` itself is: the
    in-flight `asyncio.Task` set below is what keeps scheduled evaluations
    alive, and a fresh instance per request would have no tasks to keep
    alive at all.
    """

    def __init__(self) -> None:
        self._tasks: Set[asyncio.Task] = set()

    def schedule(self, evaluation_id: str, run: Callable[[], Awaitable[None]]) -> None:
        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(lambda t: self._on_done(evaluation_id, t))

    def _on_done(self, evaluation_id: str, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            # EvaluationService._run wraps its own body in a broad
            # try/except and always saves a FAILED EvaluationJob on error -
            # reaching this callback with an exception means something
            # escaped THAT safety net (a genuine bug), so it is logged
            # loudly rather than silently swallowed by asyncio.
            logger.error(
                "PERSISTENCE_FAILURE evaluation task raised unexpectedly: %s",
                exc,
                exc_info=exc,
                extra=log_context(event="evaluation_task_crashed", evaluation_id=evaluation_id),
            )

    async def aclose(self) -> None:
        """Best-effort shutdown notice - see core/container.py's `aclose`
        convention. Does NOT wait for in-flight evaluations to finish and
        does NOT cancel them: shutdown must not block indefinitely on a
        long-running LLM call, and cancelling mid-evaluation would leave an
        `EvaluationJob` stuck at RUNNING with no chance to record why. Any
        evaluation still running when the process exits is exactly the
        "worker crash" gap a durable queue/worker closes - this only makes
        sure it is LOGGED, not silently lost from view.
        """
        if self._tasks:
            logger.warning(
                "shutting down with %d evaluation(s) still running - they will "
                "be abandoned (see AsyncTaskEvaluationDispatcher's docstring)",
                len(self._tasks),
                extra=log_context(event="evaluation_dispatcher_shutdown",
                                  in_flight=len(self._tasks)),
            )


__all__ = ["AsyncTaskEvaluationDispatcher", "EvaluationDispatcher"]
