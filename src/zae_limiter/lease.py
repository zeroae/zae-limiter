"""Lease management for rate limit acquisitions."""

import asyncio
import logging
import warnings
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from .bucket import (
    calculate_available,
    force_consume,
    retry_after_for_deficit,
    try_consume,
    window_end_in_force,
)
from .exceptions import LeaseExpiredError, RateLimitExceeded
from .models import BucketState, Limit, LimitStatus
from .schema import BUCKET_FIELD_RF, BUCKET_FIELD_TK, bucket_attr, calculate_bucket_ttl_seconds

# TransactionConflict retry constants (Issue #332)
_CONFLICT_MAX_RETRIES = 3
_CONFLICT_BASE_DELAY_S = 0.025  # 25ms, doubles each retry: 25ms, 50ms, 100ms

if TYPE_CHECKING:
    from .repository_protocol import RepositoryProtocol

logger = logging.getLogger(__name__)


@dataclass
class LeaseEntry:
    """Tracks a single bucket within a lease."""

    entity_id: str
    resource: str
    limit: Limit
    state: BucketState
    consumed: int = 0  # total consumed during this lease (tokens, not milli)
    # Original values from DynamoDB read, for ADD delta computation (ADR-115)
    _original_tokens_milli: int = 0  # stored tk before try_consume
    _original_rf_ms: int = 0  # shared rf from composite item read
    _is_new: bool = False  # True if item needs Create path (no existing item)
    # Config source tracking for TTL calculation (Issue #271)
    _has_custom_config: bool = False  # True if entity has custom limits (no TTL)
    # Write-on-enter tracking (Issue #309)
    _initial_consumed: int = 0  # consumption written to DynamoDB on enter
    # Shard the initial consumption was written to (GHSA-76rv). Adjustments
    # and rollbacks must target that same bucket item: the speculative path
    # picks a shard at random, so assuming shard 0 debits a bucket that never
    # held the consumption and, on rollback, credits it tokens it never lost.
    # The slow path targets the shard the fast path selected (issue #439), so
    # _commit_initial() reads and creates on this shard too.
    _shard_id: int = 0
    # Cached shard_count at acquire time; stamped on the item when
    # _commit_initial() creates a new shard bucket (issue #439).
    _shard_count: int = 1
    # Denormalized entity fields for speculative writes (Issue #315)
    _cascade: bool = False
    _parent_id: str | None = None
    # Whether the caller named this limit in acquire(consume=...) (Issue #455).
    # `consume` is the declared scope of a lease: only declared entries are
    # visible through `consumed` and adjustable through adjust()/consume()/
    # release(). The slow path still carries an entry for every resolved
    # limit because _commit_initial() needs them — build_composite_create()
    # writes only the states it is handed, and build_composite_normal()
    # advances the shared `rf` while crediting refill only to the limits it
    # is handed — but those undeclared entries are write-only carriers.
    _declared: bool = True
    # Next instant at which this limit's effective params change (#222 §2.1),
    # or None when it carries no schedule. Computed by the acquire path at the
    # **same** clock reading that drove `effective_params` for this entry's
    # refill, never re-derived at commit time: an item whose `tk` was
    # materialised under one window must not advertise a `vu` belonging to the
    # next one. `_commit_initial()` takes the minimum across the entries
    # sharing a bucket item, because `vu` is one item-level attribute.
    _boundary_ms: int | None = None
    # The next `reset_schedule` edge after that same clock reading, or None
    # when this limit carries no reset schedule (#222 §3.6). The reset half of
    # `_boundary_ms`, kept separate because `_commit_initial()` needs the two
    # apart: an edge crossed between the acquire path's reading and the
    # commit's is the one case `RateLimiter._apply_reset_edge()` cannot have
    # seen, and `rf` is stamped at the *later* reading.
    _reset_edge_ms: int | None = None
    # The duration window this pass opened, epoch ms, or None when it opened
    # none (ADR-139) — i.e. an anchor `_commit_initial` must fan out to the
    # item's siblings. Set by `_open_window_if_elapsed` on an existing bucket,
    # and on a create only for a shard N>0 whose sibling window had ended or
    # was absent (a shard joining a live window has nothing to propagate).
    # Taken at the acquire path's clock reading — never re-derived at commit
    # time, for the same reason
    # `_boundary_ms` is not: the two readings are a round trip apart, and a
    # window that elapsed in between must not silently move the anchor forward
    # past the boundary the admission was gated on.
    _window_start_ms: int | None = None
    # The end of the window in force at that same reading, or None when the
    # limit has no duration window. Set for every window-carrying entry, not
    # only the ones that opened a window, so `_commit_initial` can detect one
    # that elapsed **between** the two readings — the exact analogue of
    # `_reset_edge_ms`, and silent in the same way if unhandled.
    _window_end_ms: int | None = None
    # The `rsa` stored on the item as read, before the acquire path replaced
    # it on the state with the resolved config's length (ADR-139). None for a
    # create, or an item that carried none. `_commit_initial` compares the two
    # and re-stamps `rsa` when the config length changed: a resource- or
    # system-level `reset_after` change never fans out, and without this the
    # item would keep the old length — and the fast path read a window end
    # the slow path no longer enforces — until the window next moved.
    _stored_reset_after_seconds: int | None = None
    # True when the bucket item exists but this limit is missing from it —
    # configured after the item was created (#633). Newness is per limit:
    # `_is_new` stays the item-level create flag (a `Put` of the whole item),
    # while a seeded limit rides the normal `UpdateItem` and is SET in full
    # there instead of `ADD`ed to, since there is nothing to add to.
    _seed: bool = False


@dataclass
class Lease:
    """
    Manages an active rate limit acquisition.

    Tracks consumption across multiple entities/limits and handles
    rollback on exception.
    """

    repository: "RepositoryProtocol"
    entries: list[LeaseEntry] = field(default_factory=list)
    _committed: bool = False
    _rolled_back: bool = False
    _initial_committed: bool = False  # True after _commit_initial() succeeds (Issue #309)
    degraded: bool = False
    """Whether this is the no-op lease yielded under ``on_unavailable=ALLOW``.

    ``True`` only when the backend was unreachable and the limiter degraded
    to allowing the request (Issue #455). Such a lease has no entries, and
    ``adjust()``, ``consume()`` and ``release()`` are silent no-ops on it —
    the declared-scope check that normally reports keys outside ``consume``
    is skipped, so an outage never turns into a warning storm. Set
    explicitly where that lease is built, never inferred from an empty
    ``entries``: a real lease with nothing declared is not degraded.
    """
    # Keys in acquire(consume=...) that named no configured limit. acquire()
    # already reported them with the right advice, so adjust()/consume()/
    # release() must not report them again with the wrong one ("name the
    # limit in consume" — the caller did). Set where the slow path builds the
    # lease (Issue #455).
    _unknown_keys: frozenset[str] = frozenset()
    # Infrastructure buckets that must be written but must never surface
    # through the lease: today only the reserved `wcu` limit, refilled on the
    # slow path so an idle shard does not look exhausted and drive doubling
    # (ADR-133). Kept out of `entries` so `wcu` cannot reach `consumed`,
    # `adjust()`, or a RateLimitExceeded status — CLAUDE.md requires it to be
    # filtered from every user-facing surface. Only _commit_initial() reads it.
    _carriers: list[LeaseEntry] = field(default_factory=list)
    # Names of the declared limits, computed once at construction so the hot
    # path (adjust/consume/release on every request) allocates nothing to
    # answer "is this key declared?". Entries are never appended after the
    # lease is built.
    _declared_names: frozenset[str] = field(init=False, default=frozenset())

    def __post_init__(self) -> None:
        self._declared_names = frozenset(
            entry.limit.name for entry in self.entries if entry._declared
        )

    @property
    def consumed(self) -> dict[str, int]:
        """Total consumed amounts by limit name (declared limits only)."""
        result: dict[str, int] = {}
        for entry in self.entries:
            if not entry._declared:
                continue
            name = entry.limit.name
            result[name] = result.get(name, 0) + entry.consumed
        return result

    def _check_declared(self, amounts: dict[str, int], method: str) -> None:
        """Report keys that name no declared limit on this lease (Issue #455).

        A limit absent from ``consume`` was never checked at admission, so
        adjusting it afterwards would drive a bucket negative that never had
        the chance to reject; a typo (``tpmm`` for ``tpm``) would otherwise
        be silent forever. Emits ``FutureWarning`` now; becomes
        ``ValidationError`` in v1.0.0.

        ``FutureWarning`` rather than ``DeprecationWarning``: Python's default
        filters hide ``DeprecationWarning`` unless it is attributed to
        ``__main__``, and ``stacklevel`` attributes this one to application
        code, so it would never surface in a real deployment — the typo
        would stay silent, the exact failure Issue #455 exists to fix.
        ``FutureWarning`` is the category documented for warnings aimed at
        end users of an application and is shown by default.

        The degraded lease yielded under ``on_unavailable=ALLOW`` is exempt:
        it has no entries by design, and warning on every call during an
        outage would turn graceful degradation into noise.
        """
        # Fast exit, no allocation: every key is declared (the common case).
        if self.degraded or amounts.keys() <= self._declared_names:
            return
        # Keys acquire() already reported as unknown are skipped silently.
        undeclared = sorted(amounts.keys() - self._declared_names - self._unknown_keys)
        if not undeclared:
            return
        declared = sorted(self._declared_names)
        warnings.warn(
            f"lease.{method}() names limit(s) {undeclared} that were not declared in "
            f"acquire(consume=...); declared limits on this lease: {declared}. "
            "Undeclared keys are ignored. Name the limit in `consume` (an estimate "
            "of 0 is valid) to make it adjustable. This becomes a ValidationError "
            "in v1.0.0.",
            FutureWarning,
            # 1 = this helper, 2 = adjust()/consume()/release(), 3 = the caller
            stacklevel=3,
        )

    @property
    def _has_adjustments(self) -> bool:
        """Whether any post-enter adjustments were made (Issue #309)."""
        return any(entry.consumed != entry._initial_consumed for entry in self.entries)

    async def consume(self, **amounts: int) -> None:
        """
        Consume additional capacity from the buckets.

        Raises RateLimitExceeded if any bucket has insufficient capacity.

        Only limits declared in ``acquire(consume=...)`` can be consumed;
        other keys are reported (Issue #455) and ignored.

        Args:
            **amounts: Mapping of limit_name -> amount to consume
        """
        if self._committed or self._rolled_back:
            raise LeaseExpiredError()

        self._check_declared(amounts, "consume")

        now_ms = self.repository._now_ms()
        statuses: list[LimitStatus] = []
        updates: list[tuple[LeaseEntry, int, int]] = []  # (entry, new_tokens, new_refill)

        # Check all declared limits first
        for entry in self.entries:
            if not entry._declared:
                continue
            amount = amounts.get(entry.limit.name, 0)
            if amount <= 0:
                continue

            result = try_consume(entry.state, amount, now_ms)

            status = LimitStatus(
                entity_id=entry.entity_id,
                resource=entry.resource,
                limit_name=entry.limit.name,
                limit=entry.limit.per_shard(entry.state.shard_count, now_ms),
                available=result.available,
                requested=amount,
                exceeded=not result.success,
                retry_after_seconds=result.retry_after_seconds,
                resets_at_ms=window_end_in_force(entry.limit, entry.state, now_ms),
            )
            statuses.append(status)

            if result.success:
                updates.append((entry, result.new_tokens_milli, result.new_last_refill_ms))

        # Also include statuses for limits not being consumed (for full visibility)
        for entry in self.entries:
            if entry._declared and entry.limit.name not in amounts:
                available = calculate_available(entry.state, now_ms)
                statuses.append(
                    LimitStatus(
                        entity_id=entry.entity_id,
                        resource=entry.resource,
                        limit_name=entry.limit.name,
                        limit=entry.limit.per_shard(entry.state.shard_count, now_ms),
                        available=available,
                        requested=0,
                        exceeded=False,
                        retry_after_seconds=0.0,
                        resets_at_ms=window_end_in_force(entry.limit, entry.state, now_ms),
                    )
                )

        # Check for violations
        violations = [s for s in statuses if s.exceeded]
        if violations:
            raise RateLimitExceeded(statuses)

        # Apply updates to local state (will be persisted on commit)
        for entry, new_tokens, new_refill in updates:
            entry.state.tokens_milli = new_tokens
            entry.state.last_refill_ms = new_refill
            amount = amounts.get(entry.limit.name, 0)
            entry.consumed += amount
            # Update consumption counter if initialized (issue #179)
            if entry.state.total_consumed_milli is not None:
                entry.state.total_consumed_milli += amount * 1000

    async def adjust(self, **amounts: int) -> None:
        """
        Adjust consumption by delta (positive or negative).

        Never raises - allows bucket to go negative.
        Use for post-hoc reconciliation (e.g., LLM token counts).

        Only limits declared in ``acquire(consume=...)`` can be adjusted;
        other keys are reported (Issue #455) and ignored.

        Args:
            **amounts: Mapping of limit_name -> delta (positive = consume more)
        """
        if self._committed or self._rolled_back:
            raise LeaseExpiredError()

        self._check_declared(amounts, "adjust")
        self._apply_adjust(amounts)

    def _apply_adjust(self, amounts: dict[str, int]) -> None:
        """Apply adjust() deltas to declared entries (shared with release())."""
        now_ms = self.repository._now_ms()

        for entry in self.entries:
            if not entry._declared:
                continue
            amount = amounts.get(entry.limit.name, 0)
            if amount == 0:
                continue

            new_tokens, new_refill = force_consume(entry.state, amount, now_ms)
            entry.state.tokens_milli = new_tokens
            entry.state.last_refill_ms = new_refill
            entry.consumed += amount
            # Update consumption counter if initialized (issue #179)
            # Net tracking: counter decreases on release/adjust(negative)
            if entry.state.total_consumed_milli is not None:
                entry.state.total_consumed_milli += amount * 1000

    async def release(self, **amounts: int) -> None:
        """
        Return unused capacity to bucket.

        Equivalent to ``adjust()`` with every amount negated: the returned
        tokens are credited unconditionally, so the bucket can end up above
        its capacity until the next refill re-caps it.

        Only limits declared in ``acquire(consume=...)`` can be released;
        other keys are reported (Issue #455) and ignored.

        Args:
            **amounts: Mapping of limit_name -> amount to return
        """
        if self._committed or self._rolled_back:
            raise LeaseExpiredError()

        self._check_declared(amounts, "release")
        negated = {k: -v for k, v in amounts.items()}
        self._apply_adjust(negated)

    async def _commit_initial(self) -> None:
        """Write initial consumption to DynamoDB on context enter (Issue #309).

        Persists bucket state using ADD-based writes (ADR-115). Groups entries
        by (entity_id, resource, shard) to build one composite update per
        bucket item. Uses Normal write path first (ADD with refill, CONDITION
        rf=expected). On ConditionalCheckFailedException, falls back to Retry
        path.

        A new bucket is created on the shard the acquire selected, with the
        cached shard_count stamped on it (issue #439). The create is guarded
        by ``attribute_not_exists(PK)``, so if the aggregator's shard
        propagation wins the race the transaction fails its condition check
        and the Retry path debits the now-existing item under ``tk >=
        consumed`` — the same gate the speculative write uses, so the race
        can never over-admit.

        After successful write, records _initial_consumed on each entry so
        that _commit_adjustments() can compute deltas.
        """
        if self._initial_committed or self._committed or self._rolled_back:
            return

        now_ms = self.repository._now_ms()
        repo = self.repository

        # Group entries by (entity_id, resource, shard) — one item per bucket,
        # and the shard is part of a bucket's identity (GHSA-76rv). Carriers
        # join here and nowhere else: they must be written, never seen.
        groups: dict[tuple[str, str, int], list[LeaseEntry]] = {}
        for entry in (*self.entries, *self._carriers):
            key = (entry.entity_id, entry.resource, entry._shard_id)
            groups.setdefault(key, []).append(entry)

        # Build transaction items
        items: list[dict[str, Any]] = []
        # Rollovers this commit persists, per bucket item, for the fan-out
        # after the write (ADR-139). Keyed with the item's shard count so a
        # cascade's child and parent each fan out over their own.
        window_fanouts: dict[tuple[str, str, int, int], dict[str, tuple[int, int]]] = {}
        for (entity_id, resource, shard_id), group_entries in groups.items():
            is_new = group_entries[0]._is_new

            # Calculate TTL based on config source (Issue #271)
            has_custom_config = group_entries[0]._has_custom_config
            limits = [e.limit for e in group_entries]
            multiplier = self.repository._bucket_ttl_refill_multiplier

            if has_custom_config:
                ttl_seconds: int | None = 0  # 0 means REMOVE ttl
            elif multiplier <= 0:
                ttl_seconds = None
            else:
                ttl_seconds = calculate_bucket_ttl_seconds(limits, multiplier)

            # `vu` is one attribute for the whole item, so the earliest change
            # across every limit written here is what has to force the next
            # materialising pass (#222 §2.1). Undeclared entries count: they
            # are materialised by this same write, and a limit the caller did
            # not name still gates the fast path for everyone else. `None`
            # everywhere — the unscheduled majority — leaves `vu` untouched.
            boundaries = [e._boundary_ms for e in group_entries if e._boundary_ms is not None]
            vu = min(boundaries) if boundaries else None

            if is_new:
                first_entry = group_entries[0]
                items.append(
                    repo.build_composite_create(
                        entity_id=entity_id,
                        resource=resource,
                        states=[e.state for e in group_entries],
                        now_ms=now_ms,
                        ttl_seconds=ttl_seconds if ttl_seconds != 0 else None,
                        cascade=first_entry._cascade,
                        parent_id=first_entry._parent_id,
                        shard_id=shard_id,
                        shard_count=first_entry._shard_count,
                        vu=vu,
                        rf_ms=_monotonic_rf(now_ms, None, group_entries),
                    )
                )
                # A create fans out only when it anchored the entity's next
                # window: a shard N>0 whose sibling window had ended or was
                # absent (the limiter sets `_window_start_ms` for exactly that
                # case). A shard that joined a live window, or a shard 0, has
                # nothing to propagate — fanning its `now` out mid-window would
                # drag every sibling's window forward, a reset nobody earned.
                created = {
                    e.limit.name: (e._window_start_ms, e.state.reset_after_seconds)
                    for e in group_entries
                    if e._window_start_ms is not None and e.state.reset_after_seconds is not None
                }
                if created:
                    window_fanouts[(entity_id, resource, shard_id, first_entry._shard_count)] = (
                        created
                    )
            else:
                consumed: dict[str, int] = {}
                refill_amounts: dict[str, int] = {}
                # Every entry in the group participates, declared or not:
                # `ws` is per-limit but the write is one item, exactly as `vu`
                # is, and an undeclared quota sharing the item must still have
                # its window stamped or it will never roll. `rsa` rides with
                # every `ws`: the aggregator and a new shard's inheritance read
                # only the item, and a resource- or system-level `reset_after`
                # never reaches an existing bucket through the param sync.
                windows: dict[str, tuple[int, int]] = {}
                # The configured length where it differs from the item's and
                # the window did not move (a moved window carries it already).
                window_lengths: dict[str, int] = {}
                # Limits missing from this existing item, seeded in full on
                # this write (#633), and the windows they stamp without
                # anchoring the entity's next one (joined, or unsharded).
                seeds: dict[str, BucketState] = {}
                seed_windows: dict[str, tuple[int, int]] = {}
                # The lock compares the `rf` the item really holds, so it is
                # taken from a limit the item really has — never from a seed,
                # whose fresh state is stamped `now` (#633, mode 3). Every
                # entry read off the item shares one `rf`; the seed's own
                # `_original_rf_ms` is the item's too, as the last resort.
                expected_rf = next(
                    (e._original_rf_ms for e in group_entries if not e._seed),
                    group_entries[0]._original_rf_ms,
                )

                for entry in group_entries:
                    name = entry.limit.name
                    consumed_milli = entry.consumed * 1000
                    if entry._seed:
                        seeds[name] = entry.state
                        # An edge or a window end crossed between the acquire
                        # path's reading and this one restarts the allowance:
                        # the seed starts at the share in force now, less this
                        # acquire's consumption — the same re-expression the
                        # branches below apply to a limit the item carries.
                        restarted = (
                            entry._reset_edge_ms is not None and entry._reset_edge_ms <= now_ms
                        ) or (entry._window_end_ms is not None and entry._window_end_ms <= now_ms)
                        if restarted:
                            entry.state.tokens_milli = (
                                entry.state.effective_capacity_milli(now_ms) - consumed_milli
                            )
                            if entry._window_end_ms is not None and entry._window_end_ms <= now_ms:
                                entry._window_start_ms = now_ms
                                entry.state.window_start_ms = now_ms
                        rsa = entry.state.reset_after_seconds
                        ws = entry.state.window_start_ms
                        if (
                            entry.limit.reset_after is not None
                            and rsa is not None
                            and ws is not None
                        ):
                            if entry._window_start_ms is not None:
                                windows[name] = (entry._window_start_ms, rsa)
                            else:
                                seed_windows[name] = (ws, rsa)
                        continue
                    consumed[name] = consumed_milli  # to millitokens
                    refill_amounts[name] = (
                        entry.state.tokens_milli - entry._original_tokens_milli + consumed_milli
                    )
                    # No reset code is needed for an edge the acquire path
                    # already saw: it put `effective_capacity` on the state
                    # before `try_consume`, so the line above resolves to
                    # `eff_cp - stored_tk + consumed` on its own, and
                    # `build_composite_normal` turns that into the identical
                    # `ADD (eff_cp - tk_observed)` the aggregator writes.
                    #
                    # An edge crossed *between* the two clock readings is the
                    # exception, and it is silent rather than merely late.
                    # `_apply_reset_edge()` ran at the earlier reading and saw
                    # nothing, yet `rf` below is stamped at this one — so the
                    # next pass compares the edge against an `rf` already past
                    # it and never applies it either. A whole period's quota
                    # disappears, and the aggregator cannot rescue it: it reads
                    # the same poisoned `rf` off the stream image.
                    #
                    # Re-expressing it here cannot double-apply: the acquire
                    # path covers every edge at or before its own reading, and
                    # `_reset_edge_ms` is strictly after it. Admission was
                    # gated against the pre-reset balance, which is the
                    # conservative direction — one request may be rejected at
                    # the boundary, rather than a whole period silently lost.
                    if entry._reset_edge_ms is not None and entry._reset_edge_ms <= now_ms:
                        refill_amounts[name] = (
                            entry.state.effective_capacity_milli(now_ms)
                            - entry._original_tokens_milli
                        )
                    # A duration window that elapsed between the acquire
                    # path's reading and this one is the mirror of the edge
                    # case above. `_open_window_if_elapsed()` saw a live window
                    # at the earlier reading, yet this write persists at a
                    # reading already past its end — and ADR-139's anchoring
                    # rule is that the first *persisted* materialising pass
                    # past the end anchors the next window. Left alone, this
                    # write would be that pass without anchoring: `ws` stays
                    # on the dead window, the request is charged to a window
                    # that has closed, and the anchor slips to whichever
                    # request happens to come next.
                    #
                    # So anchor here, at this reading, and restore the balance
                    # the same way the reset edge does. `ws == rf` after the
                    # write, so no later pass re-applies it, and it cannot
                    # double-apply: the acquire path covers every window end
                    # at or before its own reading, and `_window_end_ms` is
                    # strictly after it. Admission was gated against the
                    # pre-roll balance, the conservative direction. `vu` for
                    # this item was computed from the dead window's end and so
                    # is already `<= rf`: the next acquire takes one slow pass
                    # and re-stamps it, the same cost a param boundary crossed
                    # in the gap already pays.
                    if entry._window_end_ms is not None and entry._window_end_ms <= now_ms:
                        entry._window_start_ms = now_ms
                        entry.state.window_start_ms = now_ms
                        refill_amounts[name] = (
                            entry.state.effective_capacity_milli(now_ms)
                            - entry._original_tokens_milli
                        )
                    rsa = entry.state.reset_after_seconds
                    if entry._window_start_ms is not None and rsa is not None:
                        windows[name] = (entry._window_start_ms, rsa)
                    elif (
                        entry.limit.reset_after is not None
                        and rsa is not None
                        and rsa != entry._stored_reset_after_seconds
                    ):
                        window_lengths[name] = rsa

                items.append(
                    repo.build_composite_normal(
                        entity_id=entity_id,
                        resource=resource,
                        consumed=consumed,
                        refill_amounts=refill_amounts,
                        now_ms=now_ms,
                        expected_rf=expected_rf,
                        ttl_seconds=ttl_seconds,
                        shard_id=shard_id,
                        vu=vu,
                        windows={**seed_windows, **windows},
                        window_lengths=window_lengths,
                        seeds=seeds,
                        # Computed after the loop above, which can anchor a
                        # window at this reading; the lock still compares the
                        # stored `expected_rf`.
                        rf_ms=_monotonic_rf(now_ms, expected_rf, group_entries),
                        # No boundary anywhere in the group means nothing on
                        # this item is scheduled — the group covers every
                        # limit sharing it, declared or not. Leaving `vu`
                        # alone would strand a `vu = 0` written by the #468
                        # fan-out (which stamps it on every fan-out, scheduled
                        # or not, to force exactly this pass), and the fast
                        # path would fail its `vu > now` guard forever.
                        clear_vu=not boundaries,
                    )
                )
                # The rollover fan-out. A create fans out only in the one case
                # above (it anchored the entity's next window). The item's
                # own `shard_count` is consulted beside the cached one, which
                # can lag it; a sibling the cache has not learned about yet
                # would otherwise keep its old window.
                if windows:
                    fanout_count = max(
                        max(e._shard_count, e.state.shard_count) for e in group_entries
                    )
                    window_fanouts[(entity_id, resource, shard_id, fanout_count)] = dict(windows)

        if not items:
            self._initial_committed = True
            return

        # Retry loop for TransactionConflict (Issue #332)
        condition_failed = False
        condition_exc: Exception | None = None
        for attempt in range(_CONFLICT_MAX_RETRIES + 1):
            try:
                await repo.transact_write(items)
                break  # success
            except Exception as exc:
                # Check ConditionalCheckFailed first — it takes priority over
                # TransactionConflict because it means the optimistic lock failed,
                # requiring the consumption-only retry path.
                if _is_condition_check_failure(exc):
                    condition_failed = True
                    condition_exc = exc
                    break
                if _is_transaction_conflict(exc):
                    if attempt < _CONFLICT_MAX_RETRIES:
                        delay = _CONFLICT_BASE_DELAY_S * (2**attempt)
                        logger.debug(
                            "TransactionConflict (attempt %d/%d), retrying in %.3fs",
                            attempt + 1,
                            _CONFLICT_MAX_RETRIES,
                            delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise  # exhausted retries, propagate
                raise  # other errors propagate unchanged

        if condition_failed:
            # Retry path: ADD consumption only, CONDITION tk>=consumed per limit
            logger.debug("Normal write failed (optimistic lock), retrying consumption-only")
            # A cancelled transaction rolls back every item, including a
            # new-shard Put whose own condition passed. Per-index reasons tell
            # the innocent Put apart from the one that lost its
            # attribute_not_exists race: re-issue the former as-is (the item
            # is still missing, so a consumption-only retry would fail its
            # tk >= consumed check and surface as a spurious rejection); debit
            # the latter consumption-only on that same shard (issue #439). A
            # single-item write has no reasons list: that group is the loser.
            reason_codes = (
                _get_cancellation_reason_codes(condition_exc) if condition_exc is not None else None
            )

            def _consumption_only(
                entity_id: str, resource: str, shard_id: int, group_entries: list[LeaseEntry]
            ) -> dict[str, Any] | None:
                consumed = {
                    e.limit.name: e.consumed * 1000 for e in group_entries if e.consumed > 0
                }
                # A limit the lost write was to seed may still be missing —
                # the lock can be lost to a writer that does not seed (#633).
                # Only a limit this retry debits is seeded here, and only an
                # unscheduled one. Any other is left to the next rf-locked
                # pass: this write stamps no `vu`, so a quota, session window
                # or schedule seeded here could be spent by the fast path past
                # its first boundary; and seeding a limit it does not debit
                # would write `cp` beside a stray `tk = 0` an older client
                # left, turning a repairable item into one that reads as
                # genuinely spent. The cost is one extra rejection, at most,
                # when the lock was lost on the very pass that would seed.
                seeds = {
                    e.limit.name: e.state
                    for e in group_entries
                    if e._seed
                    and e.consumed > 0
                    and not e.state.sched
                    and not e.state.reset_sched
                    and e.state.reset_after_seconds is None
                }
                if not consumed:
                    return None
                return repo.build_composite_retry(
                    entity_id=entity_id,
                    resource=resource,
                    consumed=consumed,
                    shard_id=shard_id,
                    seeds=seeds or None,
                )

            retry_items: list[dict[str, Any]] = []
            # Group behind each retry item, so a re-issued Put that loses its
            # own create race on the retry can be downgraded in place.
            retry_groups: list[tuple[tuple[str, str, int], list[LeaseEntry]]] = []
            for idx, (key, group_entries) in enumerate(groups.items()):
                failed_here = (
                    reason_codes is None
                    or idx >= len(reason_codes)
                    or reason_codes[idx] == "ConditionalCheckFailed"
                )
                if group_entries[0]._is_new and not failed_here:
                    retry_items.append(items[idx])
                    retry_groups.append((key, group_entries))
                    continue
                retry_item = _consumption_only(*key, group_entries)
                if retry_item is not None:
                    retry_items.append(retry_item)
                    retry_groups.append((key, group_entries))

            # Bounded: a re-issued Put may lose the create race exactly once
            # more (the shard now exists), after which every item is a
            # consumption-only debit whose failure is a real rejection.
            for retry_attempt in range(2):
                if not retry_items:
                    break
                try:
                    await repo.transact_write(retry_items)
                    break
                except Exception as retry_exc:
                    if not _is_condition_check_failure(retry_exc):
                        raise
                    codes = _get_cancellation_reason_codes(retry_exc)
                    downgraded: list[dict[str, Any]] = []
                    downgraded_groups: list[tuple[tuple[str, str, int], list[LeaseEntry]]] = []
                    lost_put = False
                    for i, item in enumerate(retry_items):
                        failed_here = (
                            codes is None
                            or i >= len(codes)
                            or codes[i] == ("ConditionalCheckFailed")
                        )
                        if isinstance(item, dict) and "Put" in item and failed_here:
                            key, group_entries = retry_groups[i]
                            fallback = _consumption_only(*key, group_entries)
                            if fallback is not None:
                                downgraded.append(fallback)
                                downgraded_groups.append(retry_groups[i])
                                lost_put = True
                            continue
                        if failed_here:
                            lost_put = False  # a debit failed: truly exhausted
                            break
                        downgraded.append(item)
                        downgraded_groups.append(retry_groups[i])
                    if not lost_put or retry_attempt == 1:
                        # Index-aligned with the items that were sent, so each
                        # failure image is attributed to its own bucket.
                        images = _retry_failure_images(
                            retry_exc, [key for key, _group in retry_groups]
                        )
                        statuses = _build_retry_failure_statuses(self.entries, now_ms, images)
                        raise RateLimitExceeded(statuses) from retry_exc
                    retry_items = downgraded
                    retry_groups = downgraded_groups

        # Record initial consumed amounts after successful write
        self._initial_committed = True
        for entry in self.entries:
            entry._initial_consumed = entry.consumed

        # The lease is committed before the fan-out runs: nothing the fan-out
        # does (it swallows its own failures) can leave it half-recorded.
        # Only when the rf-locked write itself landed -- the retry path stamps
        # no `ws`, so a rollover that fell back to it was never persisted.
        if not condition_failed:
            await self._fan_out_windows(window_fanouts)

    async def _fan_out_windows(
        self, window_fanouts: dict[tuple[str, str, int, int], dict[str, tuple[int, int]]]
    ) -> None:
        """Propagate each rollover this commit persisted to the item's siblings (ADR-139).

        After the commit, never inside it. The transaction is what makes the
        roll durable on this shard; the fan-out is what stops the entity's
        other shards anchoring windows of their own. Called only when the
        rf-locked write itself landed: the consumption-only retry stamps no
        `ws`, so a rollover that fell back to it -- including one the
        re-expression anchored in memory -- was never persisted, and the next
        pass on this shard re-opens the window and fans out then.

        A failure here is not a failed acquire -- the caller was admitted and
        the write landed -- so it is logged and swallowed. A sibling left on
        the old `ws` opens its own window when that one elapses, which is the
        behaviour without a fan-out; the next rollover re-converges the
        entity. Cost: ``(S - 1) × L`` conditional writes per rollover, none at
        all for an unsharded entity. It runs after `_initial_committed` is
        recorded, so the lease's bookkeeping never depends on it.

        The entity id is never logged: it is routinely an API key
        (`py/clear-text-logging-sensitive-data`), the same rule
        ``bump_shard_count``'s ``MAX_SHARD_COUNT`` warning follows.
        """
        for (entity_id, resource, shard_id, shard_count), windows in window_fanouts.items():
            if shard_count <= 1:
                continue
            try:
                written = await self.repository._propagate_window_start(
                    entity_id, resource, shard_id, shard_count, windows
                )
            except Exception:
                logger.warning(
                    "duration-window fan-out failed for resource=%s; siblings will "
                    "anchor their own windows until one converges them",
                    resource,
                    exc_info=True,
                )
                continue
            expected = (shard_count - 1) * len(windows)
            if written < expected:
                logger.debug(
                    "duration-window fan-out wrote %d of %d for resource=%s",
                    written,
                    expected,
                    resource,
                )

    async def _commit_adjustments(self) -> None:
        """Write post-enter adjustment deltas to DynamoDB on context exit (Issue #309).

        No-op if no adjust/consume/release calls were made during the context.
        Uses build_composite_adjust() for unconditional ADD, dispatched via
        write_each() (independent single-item writes, 1 WCU each).
        """
        if self._committed or self._rolled_back:
            return

        if not self._has_adjustments:
            self._committed = True
            return

        repo = self.repository

        # Group entries by (entity_id, resource, shard) — one adjust item per
        # bucket item, and the shard is part of a bucket's identity.
        groups: dict[tuple[str, str, int], list[LeaseEntry]] = {}
        for entry in self.entries:
            key = (entry.entity_id, entry.resource, entry._shard_id)
            groups.setdefault(key, []).append(entry)

        items: list[dict[str, Any]] = []
        for (entity_id, resource, shard_id), group_entries in groups.items():
            deltas: dict[str, int] = {}
            for entry in group_entries:
                delta = entry.consumed - entry._initial_consumed
                if delta != 0:
                    deltas[entry.limit.name] = delta * 1000  # to millitokens

            if deltas:
                item = repo.build_composite_adjust(
                    entity_id=entity_id,
                    resource=resource,
                    deltas=deltas,
                    shard_id=shard_id,
                )
                if item:
                    items.append(item)

        if items:
            await repo.write_each(items)

        self._committed = True

    async def _rollback(self) -> None:
        """Write compensating deltas to restore consumed tokens (Issue #309).

        On error, the initial consumption was already written to DynamoDB by
        _commit_initial(). This method restores those tokens by writing
        negative deltas using build_composite_adjust() via write_each()
        (independent single-item writes, 1 WCU each).
        """
        if self._committed or self._rolled_back:
            return

        self._rolled_back = True

        # Nothing was written to DynamoDB, no compensation needed
        if not self._initial_committed:
            return

        repo = self.repository

        # Group entries by (entity_id, resource, shard) — one adjust item per
        # bucket item, and the shard is part of a bucket's identity.
        groups: dict[tuple[str, str, int], list[LeaseEntry]] = {}
        for entry in self.entries:
            key = (entry.entity_id, entry.resource, entry._shard_id)
            groups.setdefault(key, []).append(entry)

        items: list[dict[str, Any]] = []
        for (entity_id, resource, shard_id), group_entries in groups.items():
            deltas: dict[str, int] = {}
            for entry in group_entries:
                # Negate only what was written on enter
                if entry._initial_consumed != 0:
                    deltas[entry.limit.name] = -entry._initial_consumed * 1000

            if deltas:
                item = repo.build_composite_adjust(
                    entity_id=entity_id,
                    resource=resource,
                    deltas=deltas,
                    shard_id=shard_id,
                )
                if item:
                    items.append(item)

        if items:
            try:
                await repo.write_each(items)
            except Exception:
                logger.warning(
                    "Failed to rollback consumed tokens for entities: %s",
                    list(groups.keys()),
                    exc_info=True,
                )


def _get_cancellation_reason_codes(exc: Exception) -> list[str] | None:
    """Extract CancellationReasons codes from a TransactionCanceledException.

    Returns a list of reason codes (e.g. ["ConditionalCheckFailed", "None"]),
    or None if the exception is not a TransactionCanceledException or has no reasons.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return None
    error_code = response.get("Error", {}).get("Code", "")
    exc_name = type(exc).__name__
    if exc_name != "TransactionCanceledException" and error_code != "TransactionCanceledException":
        return None
    reasons = response.get("CancellationReasons", [])
    return [r.get("Code", "None") for r in reasons]


def _is_condition_check_failure(exc: Exception) -> bool:
    """Check if an exception is a DynamoDB ConditionalCheckFailedException.

    For TransactionCanceledException, inspects CancellationReasons to distinguish
    ConditionalCheckFailed (returns True) from TransactionConflict (returns False).
    """
    exc_name = type(exc).__name__
    if exc_name == "ConditionalCheckFailedException":
        return True
    # Check CancellationReasons for TransactionCanceledException
    reason_codes = _get_cancellation_reason_codes(exc)
    if reason_codes is not None:
        return "ConditionalCheckFailed" in reason_codes
    # botocore ClientError fallback (non-transaction)
    if hasattr(exc, "response"):
        error_code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if error_code == "ConditionalCheckFailedException":
            return True
    return False


def _is_transaction_conflict(exc: Exception) -> bool:
    """Check if an exception is a DynamoDB TransactionConflict.

    TransactionConflict occurs when concurrent transactions touch the same items.
    Unlike ConditionalCheckFailed, this indicates transient contention that should
    be retried as-is (not via the consumption-only retry path).
    """
    reason_codes = _get_cancellation_reason_codes(exc)
    if reason_codes is not None:
        return "TransactionConflict" in reason_codes
    return False


def _monotonic_rf(now_ms: int, stored_rf: int | None, group: list[LeaseEntry]) -> int:
    """The ``rf`` a materialising write stamps: never backward, never below a window (ADR-139).

    ``max(now, stored rf, every applied window start on the item)``. A duration
    window rolls when ``ws > rf`` (``BucketState.window_rolled``), so ``rf`` is
    the only record that a shard has applied its window, and a writer whose
    clock runs **behind** the one that stamped the item would otherwise erase
    that record:

    * on an existing item, ``rf = now`` moves ``rf`` backward past ``ws``, the
      next pass reads ``ws > rf`` and resets the balance again, and every
      request from the slow clock refunds everything spent before it — an
      unbounded quota;
    * on a created shard, the inherited ``ws`` of a window a faster clock
      opened lands above ``rf = now``, so the shard re-rolls on its next pass.

    Holding ``rf`` at or above both closes each. Refill is unaffected in the
    direction that matters: ``bucket.refill_bucket`` treats a non-positive
    elapsed time as zero, so an ``rf`` ahead of a later reader's clock grants
    nothing rather than a negative refill. Only entries whose limit has a
    window vote with their ``ws``: a stale start left behind by a limit that no
    longer has one is not a window in force.

    Args:
        now_ms: The commit's clock reading.
        stored_rf: The ``rf`` read off the item, or ``None`` for a create.
        group: Every entry written to this one bucket item.
    """
    candidates = [now_ms]
    if stored_rf is not None:
        candidates.append(stored_rf)
    candidates.extend(
        e.state.window_start_ms
        for e in group
        if e.limit.reset_after is not None and e.state.window_start_ms is not None
    )
    return max(candidates)


def _retry_failure_images(
    exc: Exception, keys: list[tuple[str, str, int]]
) -> dict[tuple[str, str, int], dict[str, Any]]:
    """The ``ALL_OLD`` image each failed retry write returned, by bucket key (#633).

    ``build_composite_retry`` asks for ``ReturnValuesOnConditionCheckFailure``,
    so a single-item ``UpdateItem`` carries the item under ``Item`` on the
    error response and a transaction under ``CancellationReasons[i].Item``.
    Missing either way (a backend that returns none) yields ``{}``, and the
    statuses fall back to the in-memory state.
    """
    response = getattr(exc, "response", None) or {}
    reasons = response.get("CancellationReasons")
    if reasons is None:
        item = response.get("Item")
        return {keys[0]: item} if item and len(keys) == 1 else {}
    return {
        keys[i]: reason["Item"]
        for i, reason in enumerate(reasons)
        if i < len(keys) and reason.get("Item")
    }


def _build_retry_failure_statuses(
    entries: list[LeaseEntry],
    now_ms: int,
    images: dict[tuple[str, str, int], dict[str, Any]] | None = None,
) -> list[LimitStatus]:
    """Build LimitStatus list for a retry failure (rate limit exceeded).

    Only declared entries are reported (Issue #455): undeclared entries are
    write-only carriers that never gate admission.

    ``now_ms`` is the commit's single clock reading, not a fresh one (#222
    §3.5): a scheduled limit's effective refill rate depends on when you ask,
    so a second reading could quote a different window than the rejection it
    describes.

    ``images`` holds the item each failed retry write saw (#633). The in-memory
    state is exactly what just proved stale — the lock was lost because another
    writer moved the item — so where an image is available each status is
    built from the item's real balance and ``rf`` instead: ``exceeded`` is the
    condition the retry actually failed (``tk < consumed``) and
    ``retry_after_seconds`` the wait for that balance to cover the request.
    From the in-memory state the deficit was always 0, so every such rejection
    quoted ``0.0`` — a hot retry loop driven by the 429 itself.
    """
    if images:
        from_items = _retry_statuses(entries, now_ms, images)
        # The write failed, so some image must be short. One that is not means
        # the images cannot be matched to what failed: the rejection stands,
        # reported the way it was before the images existed.
        if any(status.exceeded for status in from_items):
            return from_items
    return _retry_statuses(entries, now_ms, None)


def _retry_statuses(
    entries: list[LeaseEntry],
    now_ms: int,
    images: dict[tuple[str, str, int], dict[str, Any]] | None,
) -> list[LimitStatus]:
    """One status per declared entry: from its image where there is one, else
    from the in-memory state (see :func:`_build_retry_failure_statuses`)."""
    statuses: list[LimitStatus] = []
    for entry in entries:
        if not entry._declared:
            continue
        item = (images or {}).get((entry.entity_id, entry.resource, entry._shard_id))
        raw_tk = (
            item.get(bucket_attr(entry.limit.name, BUCKET_FIELD_TK), {}).get("N") if item else None
        )
        if item is not None and raw_tk is not None:
            real = replace(
                entry.state,
                tokens_milli=int(raw_tk),
                last_refill_ms=int(item.get(BUCKET_FIELD_RF, {}).get("N", now_ms)),
            )
            result = try_consume(real, entry.consumed, now_ms)
            statuses.append(
                LimitStatus(
                    entity_id=entry.entity_id,
                    resource=entry.resource,
                    limit_name=entry.limit.name,
                    limit=entry.limit.per_shard(real.shard_count, now_ms),
                    available=result.available,
                    requested=entry.consumed,
                    exceeded=entry.consumed > 0 and int(raw_tk) < entry.consumed * 1000,
                    retry_after_seconds=result.retry_after_seconds,
                    resets_at_ms=window_end_in_force(entry.limit, real, now_ms),
                )
            )
            continue
        deficit_milli = max(0, entry.consumed * 1000 - entry.state.tokens_milli)
        # A sharded bucket refills at its share (GHSA-76rv); the undivided
        # rate would under-report the wait by shard_count. The walk takes the
        # undivided base and does both narrowings — schedule, then shard —
        # itself (#222 §7), and returns the next reset edge outright when one
        # lands before the deficit clears. A duration window answers with the
        # wait to its end instead (ADR-139), the same helper `try_consume`
        # uses, so the fast and slow paths cannot answer it differently.
        #
        # Both schedules and the window come off the **state**, not off
        # `entry.limit`. In production they are the same values —
        # `_do_acquire` attaches the resolved config's schedules and window
        # length to each state before admission, and `BucketState.from_limit`
        # stamps them onto a new one — but the state is the single source
        # `try_consume` also reads.
        retry_after = retry_after_for_deficit(entry.state, deficit_milli, now_ms)
        statuses.append(
            LimitStatus(
                entity_id=entry.entity_id,
                resource=entry.resource,
                limit_name=entry.limit.name,
                limit=entry.limit.per_shard(entry.state.shard_count, now_ms),
                available=entry.state.tokens_milli // 1000,
                requested=entry.consumed,
                exceeded=entry.consumed > 0,
                retry_after_seconds=retry_after,
                resets_at_ms=window_end_in_force(entry.limit, entry.state, now_ms),
            )
        )
    return statuses
