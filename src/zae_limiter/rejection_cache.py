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

import functools
import inspect
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from .models import BucketState

_F = TypeVar("_F", bound=Callable[..., Any])


def clears_rejection_cache(method: _F) -> _F:
    """Clear the owner's rejection cache before **and after** an admin write.

    Before, so the write itself is never judged by a stale state; after (in a
    ``finally``), so a state an acquire stored while the write was in flight —
    one taken under the old parameters — does not outlive the call by up to
    the TTL. Works on both ``async def`` (``Repository``) and plain ``def``
    (the generated ``SyncRepository``) methods.
    """
    if inspect.iscoroutinefunction(method):

        @functools.wraps(method)
        async def async_wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            self._rejection_cache.clear()
            try:
                return await method(self, *args, **kwargs)
            finally:
                self._rejection_cache.clear()

        return cast(_F, async_wrapper)

    @functools.wraps(method)
    def sync_wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        self._rejection_cache.clear()
        try:
            return method(self, *args, **kwargs)
        finally:
            self._rejection_cache.clear()

    return cast(_F, sync_wrapper)


#: Default trust window for an entry, in seconds (ADR-147 decision 3).
DEFAULT_REJECTION_CACHE_TTL = 1.0

#: Default maximum number of entries (ADR-147 decision 6).
DEFAULT_REJECTION_CACHE_SIZE = 10_000

_Key = tuple[str, str, str, int]
_BucketKey = tuple[str, str, str]


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
        # Indexed by bucket so a lookup reads one small dict (its shards),
        # never the whole cache: `views` runs on every acquire. `_order` holds
        # every key, least recently stored first, for the size cap.
        self._buckets: dict[_BucketKey, dict[int, _Entry]] = {}
        self._order: dict[_Key, None] = {}
        self.local_rejections = 0

    @property
    def enabled(self) -> bool:
        return self.ttl_seconds > 0

    def __len__(self) -> int:
        return len(self._order)

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
        bucket_key = (namespace_id, entity_id, resource)
        key = (namespace_id, entity_id, resource, shard_id)
        self._buckets.setdefault(bucket_key, {})[shard_id] = _Entry(
            buckets=tuple(buckets),
            shard_count=shard_count,
            vu_ms=vu_ms,
            ttl_epoch=ttl_epoch,
            disabled=disabled,
            stored_at=self._clock(),
        )
        # Re-insert so `_order` stays "least recently stored first".
        self._order.pop(key, None)
        self._order[key] = None
        while len(self._order) > self.max_entries:
            try:
                oldest = next(iter(self._order))
            except (StopIteration, RuntimeError):  # pragma: no cover - thread race
                break
            self._drop(oldest)

    def views(
        self, namespace_id: str, entity_id: str, resource: str, now_ms: int
    ) -> dict[int, list[BucketState]]:
        """Bucket states that may be used to reject, keyed by shard.

        Leaves out every entry that cannot be trusted to answer "would this be
        rejected now?": older than the TTL, past its ``vu`` (the parameters may
        have changed), past its bucket TTL (the slow path recreates it full),
        or stamped disabled (``ResourceDisabled`` stays the server's answer).
        Reads only this bucket's shards, whatever the cache holds.
        """
        if not self.enabled:
            return {}
        shards = self._buckets.get((namespace_id, entity_id, resource))
        if not shards:
            return {}
        cutoff = self._clock() - self.ttl_seconds
        now_epoch = now_ms // 1000
        found: dict[int, list[BucketState]] = {}
        for shard_id, entry in list(shards.items()):
            if (
                entry.stored_at <= cutoff
                or entry.disabled
                or (entry.vu_ms is not None and entry.vu_ms <= now_ms)
                or (entry.ttl_epoch is not None and entry.ttl_epoch <= now_epoch)
            ):
                continue
            found[shard_id] = list(entry.buckets)
        return found

    def forget(self, namespace_id: str, entity_id: str, resource: str, shard_id: int) -> None:
        """Drop one shard's entry: tokens came back by a route we caused."""
        self._drop((namespace_id, entity_id, resource, shard_id))

    def clear(self) -> None:
        """Drop every entry: an admin change may have moved any limit."""
        self._buckets.clear()
        self._order.clear()

    def _drop(self, key: _Key) -> None:
        """Remove one entry from both structures; a missing key is fine."""
        self._order.pop(key, None)
        shards = self._buckets.get(key[:3])
        if shards is not None:
            shards.pop(key[3], None)
            if not shards:
                self._buckets.pop(key[:3], None)

    def record_local_rejection(self) -> None:
        self.local_rejections += 1
