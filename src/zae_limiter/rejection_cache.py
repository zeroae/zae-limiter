"""Client-side rejection cache: the last bucket state seen (ADR-147, #695).

A conditional write whose condition is false still consumes write capacity, so a
fast rejection costs 1 WCU. Every speculative response already carries the whole
bucket state at no read cost (``ALL_NEW`` on success, ``ALL_OLD`` on failure).
This cache keeps it per (namespace, entity, resource, shard) so the limiter can
project it to "now" and reject a request that cannot fit without a DynamoDB call.

It may only ever be used to **reject**. Admission always needs a successful
conditional write, so the cache cannot over-admit; its only error is a bounded
under-admission (at most ``ttl_seconds``) when tokens return by a route the
projection cannot see — another process's refund, an admin raising a limit.

Plain synchronous code shared by ``Repository`` and ``SyncRepository``, and by
every ``namespace()`` scope of either. It takes no lock: each operation tolerates
a key that a concurrent caller (the sync thread pool) removed, so a race can
only lose an entry — one more DynamoDB call, never a wrong admit.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

from .models import BucketState

#: Default trust window for an entry, in seconds (ADR-147 decision 3).
DEFAULT_REJECTION_CACHE_TTL = 1.0

#: Default maximum number of entries (ADR-147 decision 6).
DEFAULT_REJECTION_CACHE_SIZE = 10_000

_Key = tuple[str, str, str, int]


@dataclass(frozen=True)
class _Entry:
    buckets: tuple[BucketState, ...]
    shard_count: int
    vu_ms: int | None
    ttl_epoch: int | None
    disabled: bool
    stored_at: float


class RejectionCache:
    """The last bucket state seen per (namespace, entity, resource, shard)."""

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_REJECTION_CACHE_TTL,
        max_entries: int = DEFAULT_REJECTION_CACHE_SIZE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds < 0:
            raise ValueError(f"rejection_cache_ttl must be >= 0, got {ttl_seconds}")
        if max_entries < 1:
            raise ValueError(f"rejection_cache_size must be >= 1, got {max_entries}")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._clock = clock
        self._entries: dict[_Key, _Entry] = {}
        self.local_rejections = 0

    @property
    def enabled(self) -> bool:
        return self.ttl_seconds > 0

    def store(
        self,
        namespace_id: str,
        entity_id: str,
        resource: str,
        shard_id: int,
        buckets: list[BucketState],
        *,
        shard_count: int,
        vu_ms: int | None,
        ttl_epoch: int | None,
        disabled: bool,
    ) -> None:
        """Remember the state a real DynamoDB response just showed."""
        if not self.enabled:
            return
        key = (namespace_id, entity_id, resource, shard_id)
        # Re-insert so dict order is "least recently stored first".
        self._entries.pop(key, None)
        self._entries[key] = _Entry(
            buckets=tuple(buckets),
            shard_count=shard_count,
            vu_ms=vu_ms,
            ttl_epoch=ttl_epoch,
            disabled=disabled,
            stored_at=self._clock(),
        )
        while len(self._entries) > self.max_entries:
            try:
                del self._entries[next(iter(self._entries))]
            except (KeyError, StopIteration, RuntimeError):  # pragma: no cover - thread race
                break

    def views(
        self, namespace_id: str, entity_id: str, resource: str, now_ms: int
    ) -> dict[int, list[BucketState]]:
        """Bucket states that may be used to reject, keyed by shard.

        Leaves out every entry that cannot be trusted to answer "would this be
        rejected now?": older than the TTL, past its ``vu`` (the parameters may
        have changed), past its bucket TTL (the slow path recreates it full),
        or stamped disabled (``ResourceDisabled`` stays the server's answer).
        """
        if not self.enabled:
            return {}
        cutoff = self._clock() - self.ttl_seconds
        now_epoch = now_ms // 1000
        found: dict[int, list[BucketState]] = {}
        for key, entry in list(self._entries.items()):
            if key[:3] != (namespace_id, entity_id, resource):
                continue
            if (
                entry.stored_at <= cutoff
                or entry.disabled
                or (entry.vu_ms is not None and entry.vu_ms <= now_ms)
                or (entry.ttl_epoch is not None and entry.ttl_epoch <= now_epoch)
            ):
                continue
            found[key[3]] = list(entry.buckets)
        return found

    def forget(self, namespace_id: str, entity_id: str, resource: str, shard_id: int) -> None:
        """Drop one shard's entry: tokens came back by a route we caused."""
        self._entries.pop((namespace_id, entity_id, resource, shard_id), None)

    def clear(self) -> None:
        """Drop every entry: an admin change may have moved any limit."""
        self._entries.clear()

    def record_local_rejection(self) -> None:
        self.local_rejections += 1
