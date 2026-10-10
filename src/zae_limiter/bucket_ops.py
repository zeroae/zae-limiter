"""Planning an operator's reset or top-up of one (entity, resource) (ADR-149).

Pure: given every shard of the bucket as read, the limits resolved from config
and one clock reading, decide what one rf-locked, zero-consumption pass writes
on each shard. :class:`~zae_limiter.repository.Repository` reads, plans with
this module, builds the transaction and retries on conflict; nothing here does
I/O, so the sync repository shares it unchanged.

Each shard is first **materialised** exactly as an acquire's slow path would:
every dripping limit refilled, every pending calendar edge and window roll
applied (``RateLimiter._apply_reset_edge`` / ``_apply_window_roll``, the same
code). Only then is the operation applied to the named limits, as a token
**delta**, never a set: a fast-path debit landing between the read and the
write is kept and counts against the new balance.

The provisional decisions of the design (``docs/plans/2026-10-09-reset-and-
top-up-design.md`` §8) each live in one function here, so any one can be
swapped without touching the rest:

- D1, how a quota top-up is spread over shards: :func:`split_weighted`.
- D4, a session top-up with no live window opens one: :func:`_open_for_top_up`.
- D7, the shard count planned at: the caller raises lagging shards first.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

from .bucket import force_consume
from .models import BucketState, Limit
from .schema import WCU_LIMIT_NAME

Operation = Literal["reset", "top_up"]
RESET: Operation = "reset"
TOP_UP: Operation = "top_up"


@dataclass(frozen=True)
class ReadShard:
    """One shard of the bucket as read (strongly consistent)."""

    shard_id: int
    shard_count: int
    # Whether the item stores `shard_count` at all: the write's pin is
    # `attribute_not_exists` when it does not.
    shard_count_stored: bool
    rf_ms: int
    states: dict[str, BucketState]
    # R7: the grant size a legacy quota (no `gc`) is read at, per name.
    legacy_grants: dict[str, int] = field(default_factory=dict)


@dataclass
class ShardWrite:
    """What one shard's rf-locked pass writes (ADR-149 §4.2)."""

    shard_id: int
    shard_count: int
    shard_count_stored: bool
    expected_rf: int
    written_rf: int
    # `ADD b_{name}_tk` per limit, millitokens; zero deltas are omitted.
    deltas: dict[str, int] = field(default_factory=dict)
    # `SET b_{name}_gc`: a reset, roll or opener re-grants at the shard's count;
    # a top-up of a legacy quota freezes its inferred grant size.
    grant_counts: dict[str, int] = field(default_factory=dict)
    # `SET b_{name}_ws, b_{name}_rsa` for windows this pass moved.
    windows: dict[str, tuple[int, int]] = field(default_factory=dict)
    # `SET b_{name}_wa` for every window limit on the shard.
    applied_windows: dict[str, int] = field(default_factory=dict)
    # `SET b_{name}_tu`, millitokens: allowance above the grant this period.
    top_ups: dict[str, int] = field(default_factory=dict)
    # `REMOVE b_{name}_tu`: a reset or roll ended the period it was bought in.
    cleared_top_ups: set[str] = field(default_factory=set)
    # `REMOVE b_{name}_wtc`: a session reset leaves no pending roll to charge.
    cleared_window_marks: set[str] = field(default_factory=set)


@dataclass
class OperationPlan:
    """Every shard's write, and what the operation did per named limit."""

    writes: list[ShardWrite]
    # Per named limit, tokens: what a top-up granted (a dripping limit's is
    # bounded by its ceiling) or what a reset restored (negative when a balance
    # sat above its share).
    amounts: dict[str, int]

    @property
    def raises_ceiling(self) -> bool:
        """Whether any shard gains ``b_{name}_tu``, which needs the 0.17.0 gate."""
        return any(write.top_ups for write in self.writes)


def split_weighted(total: int, weights: dict[int, int]) -> dict[int, int]:
    """Split ``total`` over keys in proportion to ``weights`` (D1), exactly.

    ``floor(total * w / sum(w))`` each; the remainder goes to the lowest key, so
    the portions always sum to ``total``.
    """
    if not weights:
        return {}
    weight_sum = sum(weights.values())
    portions = {key: total * weight // weight_sum for key, weight in weights.items()}
    portions[min(weights)] += total - sum(portions.values())
    return portions


def split_by_headroom(total: int, headroom: dict[int, int]) -> dict[int, int]:
    """Split up to ``total`` in proportion to each key's ``headroom``, never above it.

    A dripping limit cannot be topped up past its ceiling, so the amount is
    bounded by the summed headroom. The remainder of the proportional split is
    handed out lowest key first, each still within its headroom.
    """
    capacity = sum(headroom.values())
    total = min(total, capacity)
    if total <= 0:
        return dict.fromkeys(headroom, 0)
    portions = {key: total * room // capacity for key, room in headroom.items()}
    remainder = total - sum(portions.values())
    for key in sorted(headroom):
        add = min(remainder, headroom[key] - portions[key])
        portions[key] += add
        remainder -= add
    return portions


def plan_operation(
    operation: Operation,
    limits: list[Limit],
    named: dict[str, int],
    shards: list[ReadShard],
    now_ms: int,
) -> OperationPlan:
    """Plan a reset or top-up over every shard of one (entity, resource).

    Args:
        operation: ``"reset"`` or ``"top_up"``.
        limits: The limits resolved from config for the pair.
        named: The limits the operation applies to. For a top-up the value is
            the whole-token amount to add; a reset ignores it.
        shards: Every existing shard, all at one ``shard_count`` (the caller
            raises lagging ones first, D7).
        now_ms: The clock reading every shard is materialised at.
    """
    from .limiter import RateLimiter  # deferred: limiter imports repository

    by_name = {limit.name: limit for limit in limits}
    writes: list[ShardWrite] = []
    working: list[dict[str, BucketState]] = []
    read_tokens: list[dict[str, int]] = []
    read_top_ups: list[dict[str, int | None]] = []
    granted: list[set[str]] = []
    materialised: list[dict[str, int]] = []

    for shard in shards:
        write = ShardWrite(
            shard_id=shard.shard_id,
            shard_count=shard.shard_count,
            shard_count_stored=shard.shard_count_stored,
            expected_rf=shard.rf_ms,
            written_rf=now_ms,
        )
        work: dict[str, BucketState] = {}
        tokens: dict[str, int] = {}
        top_ups: dict[str, int | None] = {}
        regranted: set[str] = set()
        for name, stored in shard.states.items():
            limit = by_name.get(name)
            if name != WCU_LIMIT_NAME and limit is None:
                continue  # no longer configured: left exactly as an acquire leaves it
            state = replace(stored)
            tokens[name] = stored.tokens_milli
            top_ups[name] = stored.topped_up_milli
            if limit is not None:
                # Config is the fresher source, exactly as on the slow path.
                state.sched = limit.schedule
                state.reset_sched = limit.reset_schedule
                state.reset_after_seconds = limit.reset_after_seconds
                if limit.is_quota and state.grant_count is None:
                    state.grant_count = shard.legacy_grants.get(name, state.shard_count)
                reset = RateLimiter._apply_reset_edge(limit, state, now_ms)
                rolled = RateLimiter._apply_window_roll(limit, state, now_ms)
                if limit.is_quota and (reset or rolled):
                    regranted.add(name)
            state.tokens_milli, _rf = force_consume(state, 0, now_ms)
            work[name] = state
        writes.append(write)
        working.append(work)
        read_tokens.append(tokens)
        read_top_ups.append(top_ups)
        granted.append(regranted)
        materialised.append({name: state.tokens_milli for name, state in work.items()})

    amounts: dict[str, int] = {}
    if operation == RESET:
        for name in named:
            limit = by_name[name]
            restored = 0
            for write, work, regranted, before in zip(
                writes, working, granted, materialised, strict=True
            ):
                target = work.get(name)
                if target is None:
                    continue
                _reset(limit, target, write, now_ms)
                if limit.is_quota:
                    regranted.add(name)
                restored += target.tokens_milli - before[name]
            amounts[name] = restored // 1000
    else:
        for name, amount in named.items():
            limit = by_name[name]
            holders = [i for i, work in enumerate(working) if name in work]
            if limit.reset_after is not None:
                for i in holders:
                    if _open_for_top_up(limit, working[i][name], writes[i], now_ms):
                        granted[i].add(name)
            if limit.is_quota:
                portions = split_weighted(
                    amount,
                    {i: max(1, writes[i].shard_count // _grant(working[i][name])) for i in holders},
                )
            else:
                portions = split_by_headroom(
                    amount,
                    {
                        i: max(
                            0,
                            (working[i][name].ceiling_milli(now_ms) - working[i][name].tokens_milli)
                            // 1000,
                        )
                        for i in holders
                    },
                )
            for i, portion in portions.items():
                _credit(limit, working[i][name], portion * 1000, writes[i], now_ms)
                if limit.is_quota and name not in granted[i]:
                    # A legacy grant size is frozen onto the item with the
                    # credit, so a later planner reads the size it was weighted at.
                    if shards[i].states[name].grant_count is None:
                        writes[i].grant_counts[name] = _grant(working[i][name])
            amounts[name] = sum(portions.values())

    for write, work, tokens, top_ups, regranted in zip(
        writes, working, read_tokens, read_top_ups, granted, strict=True
    ):
        for name, state in work.items():
            delta = state.tokens_milli - tokens[name]
            if delta:
                write.deltas[name] = delta
            limit = by_name.get(name)
            if limit is None:
                continue
            if name in regranted:
                write.grant_counts[name] = state.shard_count
            if top_ups[name] is not None and state.topped_up_milli is None:
                write.cleared_top_ups.add(name)
            elif state.topped_up_milli is not None and state.topped_up_milli != top_ups[name]:
                write.top_ups[name] = state.topped_up_milli
            if limit.reset_after is not None and state.window_start_ms is not None:
                # Every window on the shard is applied once materialised (a
                # pending roll was rolled, a reset or opener applied its own),
                # so its `ws` value is stamped as `wa` (#640), which also marks
                # an item written before the marker.
                write.applied_windows[name] = state.window_start_ms
        # ADR-140: `rf` never moves backward and never sits below a window
        # start the item carries, exactly as `lease._monotonic_rf` stamps it.
        # Strictly past the stored `rf`, so a writer locked on the value this
        # pass read (an aggregator refill of a stale image) always loses.
        write.written_rf = max([now_ms, write.expected_rf + 1, *write.applied_windows.values()])
    return OperationPlan(writes=writes, amounts=amounts)


def creation_vu(limits: list[Limit], states: dict[str, BucketState], now_ms: int) -> int | None:
    """The ``vu`` a bucket created by a top-up starts with (D5): the earliest
    boundary of any limit on it, as the acquire's create stamps it."""
    from .limiter import RateLimiter  # deferred: limiter imports repository

    boundaries = [
        boundary
        for limit in limits
        if (boundary := RateLimiter._materialisation_stamps(limit, states[limit.name], now_ms)[0])
        is not None
    ]
    return min(boundaries) if boundaries else None


def _grant(state: BucketState) -> int:
    """The grant count a quota shard holds its allowance at."""
    return state.grant_shard_count


def _reset(limit: Limit, state: BucketState, write: ShardWrite, now_ms: int) -> None:
    """Restore one named limit to its full share and start a new period (§2.1)."""
    if not limit.is_quota:
        # Debt is forgiven; the ceiling is the share.
        state.tokens_milli = state.ceiling_milli(now_ms)
        return
    state.grant_count = state.shard_count
    state.topped_up_milli = None
    state.tokens_milli = state.reset_target_milli(now_ms)
    rsa = limit.reset_after_seconds
    if rsa is None:
        return
    # Ended **and applied**, never removed: a rollover fan-out from a window
    # opened before the reset is guarded by `ws <= its own ws - rsa`, which
    # this stored `ws` fails, so it cannot land and re-roll the old window.
    # The next admitted request opens a fresh one (idle-restart, ADR-139).
    ended = now_ms - rsa * 1000
    state.window_start_ms = ended
    state.window_applied_ms = ended
    write.windows[limit.name] = (ended, rsa)
    if state.window_consumed_mark_milli is not None:
        state.window_consumed_mark_milli = None
        write.cleared_window_marks.add(limit.name)


def _open_for_top_up(limit: Limit, state: BucketState, write: ShardWrite, now_ms: int) -> bool:
    """D4: a session top-up with no live window opens one at ``now_ms``.

    A credit to an ended window would be wiped by the next opener, so the
    purchase starts the session: the shard is restored to its share exactly as
    an opener would, and the top-up lands on top. Returns whether it opened.
    """
    from .limiter import RateLimiter  # deferred: limiter imports repository

    end = state.window_end_ms
    if end is not None and now_ms < end:
        return False
    state.window_start_ms = now_ms
    RateLimiter._apply_window_roll(limit, state, now_ms, opened=True)
    rsa = limit.reset_after_seconds
    assert rsa is not None
    write.windows[limit.name] = (now_ms, rsa)
    return True


def _credit(
    limit: Limit, state: BucketState, amount_milli: int, write: ShardWrite, now_ms: int
) -> None:
    """Add ``amount_milli`` to one shard's balance (§2.2).

    A quota raises its ceiling by the whole amount (``tu += amount``), even
    where the credit lands in room spent this period: a later refund of the
    consumption it replaced must not be clamped at the old ceiling, or the
    purchase is lost. A ceiling looser than the balance cannot mint — a quota
    never drips — so the only cost is headroom for credits.
    """
    if amount_milli <= 0:
        return
    state.tokens_milli += amount_milli
    if limit.is_quota:
        state.topped_up_milli = (state.topped_up_milli or 0) + amount_milli
