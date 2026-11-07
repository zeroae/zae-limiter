"""How many DynamoDB calls an apply keeps in flight (``ZAEL_PROVISIONER_WORKERS``).

One setting for every thread pool of an apply: the pre-read and the config
writes (``applier``), the pending-item writes (``pending``) and the owed
fan-outs (``handler``). An apply is a long chain of small, latency-bound
DynamoDB calls (a 1270-level initial apply is ~11k of them), so concurrency,
not throughput, is what bounds it.

None of the pools runs inside another, so at most ``WORKERS`` calls are in
flight at once and the shared client's connection pool (``handler._new_client``)
needs ``WORKERS`` connections plus a little slack.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: Environment variable on the provisioner function overriding ``WORKERS``.
WORKERS_ENV = "ZAEL_PROVISIONER_WORKERS"
DEFAULT_WORKERS = 8
MAX_WORKERS = 64


def workers_from_env() -> int:
    """``ZAEL_PROVISIONER_WORKERS``, clamped to 1..64; the default when unset or invalid."""
    raw = os.environ.get(WORKERS_ENV, "").strip()
    if not raw:
        return DEFAULT_WORKERS
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring %s=%r: not an integer", WORKERS_ENV, raw)
        return DEFAULT_WORKERS
    return max(1, min(MAX_WORKERS, value))


WORKERS = workers_from_env()
