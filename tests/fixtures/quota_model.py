"""Reference model for quota shard allocation (ADR-145, Refs #637, Refs #642).

Ported verbatim in behaviour from the design session's throwaway model
(``docs/plans/2026-09-28-quota-shard-grant-design.md`` §3.1). Not product code —
a test oracle. ``NEW.grant`` independently implements the ADR-145 rule (it is
the oracle ``models.plan_quota_grant`` is validated against, not a wrapper
around it); ``OLD.grant`` is today's clamp rule (clamp every sibling above
``C // S``, new shard ``min(taken, C // S)``, full share when no sibling
exists).

A quota of ``capacity`` per period. Shards ``0..S-1``, ``S`` doubles ``1 ->
32``. Shards appear lazily (a random draw hits a missing one, or a
stale-cache / aggregator-clone shard is seeded later). A shard's quota state
is ``Q(tk, gc, period)``; ``gc`` is the shard count its current-period grant
was sized at (the stored ``b_{q}_gc``).
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_SHARD_COUNT = 32


@dataclass
class Q:
    tk: int
    gc: int
    period: int


class QuotaModel:
    """The design's reference model: ``QuotaModel(capacity, rule="NEW"|"OLD")``."""

    def __init__(self, capacity: int, rule: str) -> None:
        self.capacity = capacity
        self.rule = rule
        self.S = 1
        self.period = 0
        self.shards: dict[int, Q] = {}  # only shards carrying the quota
        self.admitted: dict[int, int] = {}

    # --- helpers -------------------------------------------------------
    def share(self) -> int:
        return self.capacity // self.S

    def current(self, i: int) -> bool:
        return i in self.shards and self.shards[i].period == self.period

    def ceiling(self, q: Q) -> int:
        return self.capacity // (q.gc if self.rule == "NEW" else self.S)

    def touch(self, i: int) -> None:
        """A slow/materialising pass on shard i: apply a pending reset, clamp."""
        q = self.shards[i]
        if q.period != self.period:
            q.tk, q.gc, q.period = self.share(), self.S, self.period
        q.tk = min(q.tk, self.ceiling(q))

    def cover(self, j: int) -> int | None:
        best = None
        for i, q in self.shards.items():
            if i != j and q.period == self.period and j % q.gc == i % q.gc:
                if best is None or q.gc > self.shards[best].gc:
                    best = i
        return best

    # --- operations ------------------------------------------------------
    def grant(self, j: int) -> None:
        """Create shard j, or seed the quota onto an existing shard lacking it."""
        assert j not in self.shards
        share = self.share()
        if self.rule == "OLD":
            taken = 0
            for q in self.shards.values():
                if q.tk > share:
                    taken += q.tk - share
                    q.tk = share
            others = bool(self.shards)
            tk = min(taken, share) if others and taken else (share if not others else 0)
            self.shards[j] = Q(tk, self.S, self.period)
            return
        i = self.cover(j)
        if i is None:
            tk = share
        else:
            tk = min(share, max(0, self.shards[i].tk))
            self.shards[i].tk -= tk
        self.shards[j] = Q(tk, self.S, self.period)

    def double(self) -> None:
        if self.S < MAX_SHARD_COUNT:
            self.S *= 2

    def spend(self, i: int, n: int) -> bool:
        if i not in self.shards:
            self.grant(i)
        self.touch(i)
        q = self.shards[i]
        if q.tk >= n:
            q.tk -= n
            self.admitted[self.period] = self.admitted.get(self.period, 0) + n
            return True
        return False

    def next_period(self) -> None:
        self.period += 1

    # --- accounting ----------------------------------------------------
    def accounted(self) -> int:
        """admitted + held (current) + still-mintable uncovered slots."""
        held = sum(q.tk for i, q in self.shards.items() if self.current(i))
        mint = sum(
            self.share() for j in range(self.S) if not self.current(j) and self.cover(j) is None
        )
        return self.admitted.get(self.period, 0) + held + mint
