"""Concurrency for the generated sync limiter and lease (ADR-148).

Hand-written, not generated. The sync generator turns ``asyncio.gather`` into
``self._run_in_executor(...)``, which ``SyncRepository`` implements from its
``parallel_mode``. ``SyncRateLimiter`` and ``SyncLease`` gather too — over
the resources of a multi-resource acquire, and over the independent writes of
a reconcile or refund — and their injected ``_run_in_executor`` delegates
here, so they honour the repository's ``parallel_mode`` as well.

One thing differs from the repository's own executor: in ``threadpool`` mode
the work runs on a pool owned by this module, never on the repository's. Each
part of a multi-resource acquire can itself gather on the repository (a
cascade's child and parent writes); running the parts on the repository's
two-worker pool would leave those inner writes waiting for a worker held by
the very parts waiting on them.
"""

import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

#: Workers in the orchestration pool shared by every sync limiter and lease in
#: the process. Each holds one part of an acquire (or one reconcile write)
#: while it waits on DynamoDB; work beyond this queues, it never deadlocks.
ORCHESTRATION_WORKERS = 32

_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()
_local = threading.local()


def _orchestration_pool() -> ThreadPoolExecutor:
    """The process-wide orchestration pool, created on first use."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(
                max_workers=ORCHESTRATION_WORKERS, thread_name_prefix="zae-limiter"
            )
        return _pool


def _in_worker(fn: Callable[[], Any]) -> Any:
    """Run ``fn`` marked as orchestration work, so a nested gather runs serially."""
    _local.in_worker = True
    try:
        return fn()
    finally:
        _local.in_worker = False


def run_parallel(repository: Any, funcs: Sequence[Callable[[], Any]]) -> tuple[Any, ...]:
    """Run ``funcs`` concurrently by ``repository``'s ``parallel_mode``.

    - one call, or already inside orchestration work: serially, in place;
    - ``gevent`` or ``serial`` (and ``auto`` resolving to either): through the
      repository's own executor function;
    - ``threadpool`` (and ``auto`` resolving to it): on the orchestration pool;
    - a repository with no ``parallel_mode`` (a third-party
      ``SyncRepositoryProtocol``): serially.

    The attributes are read off the instance (``vars``), never through
    ``getattr``, so a test double answering every attribute is treated as a
    repository with no ``parallel_mode``.

    Returns:
        Each function's result, in order.
    """
    if len(funcs) <= 1 or getattr(_local, "in_worker", False):
        return tuple(fn() for fn in funcs)
    attrs = vars(repository) if hasattr(repository, "__dict__") else {}
    executor_fn = attrs.get("_executor_fn")
    if executor_fn is not None:
        return tuple(executor_fn(funcs))
    if "_parallel_mode" not in attrs:
        return tuple(fn() for fn in funcs)
    pool = _orchestration_pool()
    futures = [pool.submit(_in_worker, fn) for fn in funcs]
    return tuple(future.result() for future in futures)
