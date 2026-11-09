"""Each shard's last image, for the fill-instant fast path (ADR-154, #819).

The fast path chooses, per hard limit, which of ADR-154's two writes to send:
the not-full write or the full write. It needs the item's parameters, schedule,
shard count and expression instant to compute either, and a guess at which side
of its clock reading the stored fill instant lies. Both come from the last image
this process saw of the shard (an ``ALL_NEW`` on success, an ``ALL_OLD`` on
failure), kept here with a per-limit estimate of how often a request finds the
bucket not full (proofs §6).

An image never decides an admission: every write is conditioned on the side it
claims and pinned on what it was computed from, so a stale image costs at most
a failed write, whose ``ALL_OLD`` replaces it. The one decision taken without a
write, rejecting a request larger than a shard's share, trusts only an image
younger than the config cache TTL, the bound ADR-153 gives configuration changes.

Plain synchronous code shared by ``Repository`` and ``SyncRepository`` and by
every ``namespace()`` scope. It takes no lock: a race between the sync thread
pool's callers only loses an entry or an observation.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

DEFAULT_FILL_IMAGE_SIZE = 10_000

#: An observation moves the estimate by ``1 / 2**_EWMA_SHIFT`` of the way.
_EWMA_SHIFT = 3

_Key = tuple[str, str, str, int]  # (namespace, entity, resource, shard)


@dataclass
class FillImage:
    """One shard's last item, when it was stored, and its utilisation estimates."""

    item: dict[str, Any]
    stored_at: float
    utilisation: dict[str, int] = field(default_factory=dict)


class FillImageCache:
    """The last item seen per shard storing fill instants, bounded, oldest out."""

    def __init__(
        self,
        max_entries: int = DEFAULT_FILL_IMAGE_SIZE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries < 1:
            raise ValueError(f"max_entries must be >= 1, got {max_entries}")
        self.max_entries = max_entries
        self._clock = clock
        self._images: dict[_Key, FillImage] = {}

    def __len__(self) -> int:
        return len(self._images)

    def get(self, key: _Key) -> FillImage | None:
        return self._images.get(key)

    def age(self, image: FillImage) -> float:
        """Seconds since ``image`` was stored."""
        return self._clock() - image.stored_at

    def store(self, key: _Key, item: dict[str, Any]) -> None:
        """Keep ``item`` as the shard's image; its utilisation estimates carry over."""
        previous = self._images.pop(key, None)
        self._images[key] = FillImage(
            item=item,
            stored_at=self._clock(),
            utilisation=previous.utilisation if previous is not None else {},
        )
        while len(self._images) > self.max_entries:
            self._images.pop(next(iter(self._images)), None)

    def forget(self, key: _Key) -> None:
        self._images.pop(key, None)

    def observe(self, key: _Key, limit_name: str, not_full: bool) -> None:
        """Fold one finding, "the bucket was not full", into the limit's estimate."""
        image = self._images.get(key)
        if image is None:
            return
        target = 1000 if not_full else 0
        # From 0, the idle bucket: one finding moves it an eighth of the way,
        # so a single "not full" never makes a lightly used limit look busy.
        current = image.utilisation.get(limit_name, 0)
        image.utilisation[limit_name] = current + ((target - current) >> _EWMA_SHIFT)

    def utilisation(self, key: _Key, limit_name: str) -> int | None:
        """The limit's estimate in per mille, or ``None`` before any finding."""
        image = self._images.get(key)
        return None if image is None else image.utilisation.get(limit_name)

    def clear(self) -> None:
        self._images.clear()
