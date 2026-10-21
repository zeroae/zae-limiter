"""Who a cancellation is for, in the async code and in its generated sync twin.

Hand-written, not generated: ``asyncio.current_task()`` has no sync
equivalent, so the generator maps ``caller_is_cancelled`` to
``sync_caller_is_cancelled`` (``IMPORT_NAME_REWRITES`` in
``scripts/generate_sync.py``).
"""

from __future__ import annotations

import asyncio


def caller_is_cancelled() -> bool:
    """True if a cancellation is pending against the running task.

    Awaiting a task that someone else cancelled raises ``CancelledError`` in
    the awaiter too, though nobody cancelled the awaiter. ``cancelling()``
    tells the two apart: it counts the cancellations requested of the running
    task itself.
    """
    try:
        task = asyncio.current_task()
    except RuntimeError:  # no running event loop: nothing can be cancelling it
        return False
    return task is not None and task.cancelling() > 0


def sync_caller_is_cancelled() -> bool:
    """Always False: a sync join cannot be cancelled.

    A ``concurrent.futures.CancelledError`` out of ``.result()`` means the
    spawn itself was cancelled before it ran, or its greenlet was killed. An
    interrupt aimed at the caller arrives as ``KeyboardInterrupt``, not as a
    cancellation.
    """
    return False
