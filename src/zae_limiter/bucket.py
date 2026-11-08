"""
Token bucket algorithm implementation using integer arithmetic.

This module implements a variant of the classic token bucket algorithm
optimized for distributed systems:

- **Integer Arithmetic**: All values stored as millitokens (x1000) to avoid
  floating-point precision issues across distributed nodes.
- **Lazy Refill**: Tokens are calculated on-demand rather than continuously,
  with drift compensation to prevent accumulated rounding errors.
- **Negative Buckets**: Buckets can go into debt to support post-hoc
  reconciliation for operations with unknown cost.

Key functions:
    refill_bucket: Calculate refilled tokens with drift compensation
    try_consume: Attempt to consume tokens (atomic check-and-consume)
    force_consume: Force consume tokens (can go negative for debt)
    calculate_retry_after: Calculate wait time until tokens available
    would_refill_satisfy: Check if refilling would allow a request to succeed

For conceptual explanation, see docs/guide/token-bucket.md
For implementation details, see docs/contributing/architecture.md
"""

from dataclasses import dataclass, replace

from . import fill_instant, schema
from .models import BucketState, Limit, LimitStatus, is_accrual_rate
from .schedule import ScheduleEntry, retry_after_with_schedule


@dataclass
class RefillResult:
    """Result of a bucket refill calculation."""

    new_tokens_milli: int
    new_last_refill_ms: int


@dataclass
class ConsumeResult:
    """Result of attempting to consume from a bucket."""

    success: bool
    new_tokens_milli: int
    new_last_refill_ms: int
    available: int  # tokens available before consume attempt
    retry_after_seconds: float  # 0 if success, time to wait if failed
    # A fill-instant state's fill instant and expression instant after the
    # consumption (#818), or None for a state stored as `tk`/`rf`.
    new_fill_instant: int | None = None
    new_expressed_ms: int | None = None


def refill_bucket(
    tokens_milli: int,
    last_refill_ms: int,
    now_ms: int,
    capacity_milli: int,
    refill_amount_milli: int,
    refill_period_ms: int,
) -> RefillResult:
    """
    Calculate refilled tokens using integer arithmetic.

    The refill is calculated as:
        tokens_to_add = elapsed_ms * refill_amount_milli / refill_period_ms

    We track how much time we "used" for the refill to avoid drift
    from accumulated rounding errors.

    The result is *always* clamped to ``capacity_milli``, including on the
    paths that add no tokens. A bucket holding more than its ceiling — because
    the ceiling shrank under it — is trimmed on the next pass rather than
    keeping the surplus forever. Debt (negative tokens) is untouched: the
    clamp is ``min()``, not a clamp into range.

    Args:
        tokens_milli: Current tokens in millitokens
        last_refill_ms: Last refill timestamp in epoch milliseconds
        now_ms: Current timestamp in epoch milliseconds
        capacity_milli: Maximum bucket capacity (ceiling) in millitokens
        refill_amount_milli: Refill amount numerator in millitokens
        refill_period_ms: Refill period denominator in milliseconds

    Returns:
        RefillResult with new token count and timestamp
    """
    elapsed_ms = now_ms - last_refill_ms

    if elapsed_ms <= 0:
        # Clamp even with no elapsed time: a capacity that shrank under a full
        # bucket — via set_limits, a schedule boundary, or a shard doubling —
        # leaves a surplus that must not survive (#222 §3.3, replaces #469).
        return RefillResult(min(capacity_milli, tokens_milli), last_refill_ms)

    # Integer division for tokens to add
    tokens_to_add = (elapsed_ms * refill_amount_milli) // refill_period_ms

    if tokens_to_add == 0:
        # Not enough time has passed for even 1 millitoken — but still clamp,
        # for the same reason as above.
        return RefillResult(min(capacity_milli, tokens_milli), last_refill_ms)

    # Track how much time we "consumed" for this refill to avoid drift
    # This is the inverse: time_used = tokens_added * period / amount
    time_used_ms = (tokens_to_add * refill_period_ms) // refill_amount_milli

    # Cap at capacity and update timestamp
    new_tokens = min(capacity_milli, tokens_milli + tokens_to_add)
    new_last_refill = last_refill_ms + time_used_ms

    return RefillResult(new_tokens, new_last_refill)


def fill_params(state: BucketState, t_ms: int) -> fill_instant.ShardParams:
    """The parameters ``state``'s schedule puts in force at ``t_ms``, undivided (#818)."""
    return fill_instant.params_at(
        t_ms,
        state.capacity_milli,
        state.refill_amount_milli,
        state.refill_period_ms,
        state.sched,
        state.shard_count,
    )


def fill_instant_at(state: BucketState, t_ms: int) -> tuple[int, int]:
    """``state``'s fill instant re-expressed up to ``t_ms``, and the instant it now is.

    Across every schedule boundary since it was expressed (ADR-155), so each
    window drips at its own rate and caps at its own ceiling. A reading
    before the expression instant (a lagging clock) keeps it as it is.
    """
    if state.fill_instant is None:
        raise ValueError(f"limit {state.limit_name!r} is not stored as a fill instant")
    expressed = state.expressed_ms if state.expressed_ms is not None else state.last_refill_ms
    if not state.sched or t_ms <= expressed:
        return state.fill_instant, expressed
    return fill_instant.re_express_across(
        state.fill_instant,
        expressed,
        t_ms,
        state.capacity_milli,
        state.refill_amount_milli,
        state.refill_period_ms,
        state.sched,
        state.shard_count,
    )


def _refilled(state: BucketState, now_ms: int) -> RefillResult:
    """The balance at ``now_ms``: exact from a fill instant, else the lazy refill."""
    if state.fill_instant is None:
        return refill_bucket(
            tokens_milli=state.tokens_milli,
            last_refill_ms=state.last_refill_ms,
            now_ms=now_ms,
            capacity_milli=state.ceiling_milli(now_ms),
            refill_amount_milli=state.effective_refill_amount_milli(now_ms),
            refill_period_ms=state.effective_refill_period_ms(now_ms),
        )
    z, expressed = fill_instant_at(state, now_ms)
    params = fill_params(state, max(now_ms, expressed))
    return RefillResult(fill_instant.balance_milli(z, now_ms, params), now_ms)


def balance_milli_at(state: BucketState, t_ms: int) -> int:
    """The balance at ``t_ms`` in millitokens: a fill instant's exactly, else the lazy refill."""
    return _refilled(state, t_ms).new_tokens_milli


def _moved(state: BucketState, amount: int, now_ms: int) -> tuple[int, int]:
    """The fill instant after consuming ``amount`` tokens at ``now_ms`` (a credit if negative).

    A debit records ``max(z, t) + c / r``, which is how a full bucket forgets
    the time it sat full (#808); a credit moves ``z`` back, capped at capacity
    for free (proofs Lemma 3). Returns the new fill instant and the instant it
    is expressed at.
    """
    z, expressed = fill_instant_at(state, now_ms)
    at = max(now_ms, expressed)
    params = fill_params(state, at)
    milli = amount * 1000
    if milli > 0:
        z = max(z, fill_instant.stamp(now_ms, params)) + fill_instant.span(milli, params)
    elif milli < 0:
        z = fill_instant.returned(z, -milli, params)
    return z, at


def express_at(state: BucketState, t_ms: int) -> None:
    """Re-express ``state``'s fill instant at ``t_ms``, across every boundary since it was.

    A no-op for a state stored as ``tk``, or a reading before its expression
    instant. Every fill instant one write stores is expressed at the item's one
    ``xi``, so none skips a boundary the next reader would not re-apply.
    """
    if state.fill_instant is None:
        return
    z, expressed = fill_instant_at(state, t_ms)
    state.fill_instant, state.expressed_ms = z, max(t_ms, expressed)


def adopt_schedule(state: BucketState, sched: tuple[ScheduleEntry, ...], now_ms: int) -> None:
    """Give ``state`` the schedule ``sched``, re-expressing a fill instant at ``now_ms``.

    The slow path takes the schedule from config, the fresher source. A fill
    instant is scaled by the refill amount the item's own schedule put in force,
    so read against another schedule it would be misread, ten times too far
    ahead or behind across a 0.1x window: it is re-expressed at ``now_ms`` from
    the parameters in force under the old schedule to those under the new,
    keeping the balance or trimming it to the new share (ADR-155).
    """
    if state.fill_instant is None or sched == state.sched:
        state.sched = sched
        return
    express_at(state, now_ms)
    assert state.expressed_ms is not None
    at = state.expressed_ms
    old = fill_params(state, at)
    state.sched = sched
    new = fill_params(state, at)
    if new != old:
        state.fill_instant = fill_instant.re_express(state.fill_instant, at, old, new)


def convert_to_fill_instant(state: BucketState) -> None:
    """Express a dripping state still stored as ``tk``/``rf`` as a fill instant, in place.

    Exact (#817): the balance at ``rf`` is ``tk``, a surplus above the share
    reads as a full bucket and a debt as a fill instant past ``rf``. A quota, or
    a state already holding a fill instant, is left alone. The slow path calls
    this only while ``schema.WRITE_FILL_INSTANTS`` is on; its write then
    converts the item, pinned on the ``tk`` and ``rf`` it read.
    """
    if state.fill_instant is not None or not state.is_dripping_state:
        return
    params = fill_params(state, state.last_refill_ms)
    state.fill_instant = fill_instant.from_tokens(state.tokens_milli, state.last_refill_ms, params)
    state.expressed_ms = state.last_refill_ms


def adopt_fill_instant(state: BucketState) -> None:
    """On the slow path: convert a dripping state stored as ``tk``/``rf``, if the switch is on.

    ``schema.WRITE_FILL_INSTANTS`` (#818) gates the conversion; a state read as
    a fill instant already holds one either way.
    """
    if schema.WRITE_FILL_INSTANTS:
        convert_to_fill_instant(state)


def express_created(state: BucketState, now_ms: int) -> None:
    """Give a dripping state the slow path creates full at ``now_ms`` its fill instant (#818).

    Only while ``schema.WRITE_FILL_INSTANTS`` is on; otherwise it is created as
    ``tk``, as before.
    """
    if (
        not schema.WRITE_FILL_INSTANTS
        or state.fill_instant is not None
        or not state.is_dripping_state
    ):
        return
    state.fill_instant = fill_instant.created_full(now_ms, fill_params(state, now_ms))
    state.expressed_ms = now_ms


def settle(state: BucketState, amount: int, now_ms: int) -> None:
    """:func:`force_consume` applied in place, keeping a fill instant exact.

    ``amount`` 0 materialises the balance at ``now_ms``. A fill instant moves
    first and the snapshot is read off it, so a credit on a full bucket stays
    at capacity in both.
    """
    if state.fill_instant is None:
        state.tokens_milli, state.last_refill_ms = force_consume(state, amount, now_ms)
        return
    state.fill_instant, state.expressed_ms = _moved(state, amount, now_ms)
    state.tokens_milli, state.last_refill_ms = _refilled(state, now_ms).new_tokens_milli, now_ms


def credit_back(state: BucketState, amount: int) -> None:
    """Undo an in-memory debit of ``amount`` tokens, at the instant it was made.

    A rejected pass's admitted limits are credited back before its quota moves
    are committed with nothing consumed (ADR-145 I5). A fill instant moves back
    by the debit's span, which leaves ``max(z, t)``: the same balance from the
    debit's instant on.
    """
    state.tokens_milli += amount * 1000
    if state.fill_instant is not None and amount > 0:
        at = state.expressed_ms if state.expressed_ms is not None else state.last_refill_ms
        state.fill_instant = fill_instant.returned(
            state.fill_instant, amount * 1000, fill_params(state, at)
        )


def apply_consume(state: BucketState, result: "ConsumeResult") -> None:
    """Apply a successful :func:`try_consume` to ``state`` in place."""
    state.tokens_milli = result.new_tokens_milli
    state.last_refill_ms = result.new_last_refill_ms
    if result.new_fill_instant is not None:
        state.fill_instant = result.new_fill_instant
        state.expressed_ms = result.new_expressed_ms


def try_consume(
    state: BucketState,
    requested: int,
    now_ms: int,
) -> ConsumeResult:
    """
    Attempt to consume tokens from a bucket.

    First refills the bucket based on elapsed time, then checks if
    there's enough capacity for the request.

    Args:
        state: Current bucket state
        requested: Number of tokens to consume
        now_ms: Current timestamp in epoch milliseconds

    Returns:
        ConsumeResult indicating success/failure and new state
    """
    # First, refill the bucket
    refill = _refilled(state, now_ms)

    current_tokens_milli = refill.new_tokens_milli
    requested_milli = requested * 1000
    available = current_tokens_milli // 1000

    # A soft limit (#467, ADR-151) is debited like any other and may go into
    # debt, but it is never a reason to reject. Deciding it here covers every
    # admission and reporting path at once: the slow path's `_admit_limit`,
    # `Lease.consume`, the speculative failure classifier and the rejection
    # cache (`would_refill_satisfy`), and every `LimitStatus` built from it.
    # A bypassed bucket (#311) is admitted the same way; its writes never
    # debit `tk`, so the "new" balance here is in-memory only.
    if current_tokens_milli >= requested_milli or state.soft or state.bypass:
        # Success - consume the tokens. A fill instant admits exactly when
        # this test does: its balance is exact, and `requested_milli` is a
        # whole number of millitokens.
        moved = _moved(state, requested, now_ms) if state.fill_instant is not None else None
        return ConsumeResult(
            success=True,
            new_tokens_milli=current_tokens_milli - requested_milli,
            new_last_refill_ms=refill.new_last_refill_ms,
            available=available,
            retry_after_seconds=0.0,
            new_fill_instant=moved[0] if moved is not None else None,
            new_expressed_ms=moved[1] if moved is not None else None,
        )
    else:
        # Failure - calculate retry time
        return ConsumeResult(
            success=False,
            new_tokens_milli=current_tokens_milli,
            new_last_refill_ms=refill.new_last_refill_ms,
            available=available,
            retry_after_seconds=retry_after_for_deficit(
                state, requested_milli - current_tokens_milli, now_ms
            ),
        )


def window_end_in_force(limit: Limit, state: BucketState, now_ms: int) -> int | None:
    """The live end of the duration window in force on ``state``, or ``None`` (ADR-139).

    The instant a duration quota's allowance returns, which is what
    :attr:`LimitStatus.resets_at_ms` carries for it. On the slow path it is
    read **after** ``RateLimiter._open_window_if_elapsed`` has run, so it is
    the end of the window this pass admitted against — a rejection at a
    boundary reports the window just opened, not the one that elapsed. Only
    the resolved ``limit`` decides whether a window is in force: a stale
    ``ws``/``rsa`` left on the item by a limit that no longer has one does
    not vote.

    **Never an instant at or before ``now_ms``.** An ended window's allowance
    is back on the next acquire, so "it returns at <a moment already past>"
    is not an answer a client can act on — it reads as "retry now", the hot
    loop #574 removed from the calendar form. Clamped here, the one place
    every status site reads it from, so a caller holding a later clock (the
    lease's retry-failure path reads the commit's) cannot surface one either.
    """
    if limit.reset_after is None:
        return None
    end = state.window_end_ms
    return end if end is not None and end > now_ms else None


def _duration_window_end(state: BucketState) -> int | None:
    """The window end a retry wait may be read from, or ``None``.

    Only a bucket of the **quota shape** — zero stored refill rate, the same
    stored-shape test :meth:`Limit.from_bucket_state` makes — answers a wait
    from its window. A corrupt item carrying a positive rate beside a stray
    ``ws``/``rsa`` is reconstructed as a dripping limit there, and must be
    quoted the drip's wait here, not a window's.
    """
    if state.refill_amount_milli != 0:
        return None
    return state.window_end_ms


def _restored_if_window_ended(state: BucketState, now_ms: int) -> BucketState:
    """The balance a fast-path image's shard will actually have on its next pass.

    A duration window that has ended on the **image** is over: the next
    materialising pass opens a new one and restores the balance (ADR-139).
    Judging the image's burnt balance instead would fast-reject with a wait to
    an end already past — ``retry_after_seconds = 0.0`` beside a
    ``resets_at_ms`` in the past, a hot retry loop.

    That is reachable, not theoretical: a resource- or system-level
    ``reset_after`` lengthened after the item was stamped leaves the item's
    ``rsa`` short while the slow path stamped ``vu`` from the longer config
    length, so the item fast-rejects (its ``vu`` is still in the future)
    between the two ends. Reading it as restored sends the request to the
    slow path, which resolves the config and either admits or rejects against
    the correct end.

    A **live** window the shard has not applied yet (``ws > wa``,
    :attr:`BucketState.window_rolled`) is restored on the next pass too, and
    is read the same way (#640). A rollover fan-out stamps ``vu = 0`` beside
    the new ``ws``, so a v0.15 fleet never shows the fast path such an image;
    a client predating ADR-139 re-stamps or REMOVEs ``vu`` from its own
    limits on its next write to that shard, and the fast path then sees the
    new window beside the old window's burnt balance. Judged as it stands,
    every request drawn to that shard would be fast-rejected until the window
    *ended* — the skipped roll the marker exists to prevent. Read as
    restored, the request goes to the slow path, which rolls it.
    """
    end = _duration_window_end(state)
    if end is None or (now_ms < end and not state.window_rolled):
        return state
    if now_ms < end:
        # A live, pending roll: the share less what the shard spent since the
        # fan-out's snapshot (#640) — the balance the slow path will restore.
        return replace(state, tokens_milli=state.window_roll_target_milli(now_ms))
    return replace(state, tokens_milli=state.reset_target_milli(now_ms))


def retry_after_for_deficit(state: BucketState, deficit_milli: int, now_ms: int) -> float:
    """Seconds until ``deficit_milli`` clears on this shard.

    A **duration window** answers from the item: the balance returns in one
    lump at ``ws + reset_after`` and a quota has no drip at all (ADR-137), so
    the wait to the window's end is the only finite answer — the same call the
    calendar form's reset branch makes, but a constant rather than a scan. The
    window is half-open, ``[ws, ws + rsa)``, so the end instant is already the
    next window's and no ``+1`` is added.

    An **ended** window reports 0 only when the restore actually clears the
    deficit: the next acquire opens a fresh window at the full share and is
    admitted, so "retry now" is true. A request larger than one shard's share
    (#475) is not cleared by any restore, and 0 there is a hot retry loop the
    429 itself drives (#574) — reachable on the fast path whenever an item's
    stored ``rsa`` is shorter than config while ``vu`` is still in the
    future. It is quoted the length of the window the next pass would open
    instead, the same answer the slow path gives after it rolls that window
    at ``now``. Routing such an image to the slow path was the alternative,
    and was rejected: it cannot answer better, it pays a read on every
    retry, and a ``BOTH_EXHAUSTED`` failure that passes the gate is doubled on
    its way to a rejection, which #480 forbids.

    Decided here rather than in ``schedule.retry_after_with_schedule`` because
    ``schedule.py`` may import nothing from ``models.py`` — that one-way
    dependency is what lets both Lambda packages vendor it — and the window
    anchor lives on the ``BucketState``. On the slow path ``rsa`` is the
    resolved config's (``_do_acquire`` attaches it), so a limit that lost its
    window carries none here.

    Everything else walks the schedule. The **undivided base** goes in, with
    ``shard_count`` alongside: the walk re-evaluates ``effective_params`` per
    window and takes the shard share afterwards, so handing it the pre-scaled,
    pre-divided ``effective_*`` values would apply both narrowings twice
    (#222 §7).
    """
    if deficit_milli <= 0:
        return 0.0
    end = _duration_window_end(state)
    if end is not None:
        if end > now_ms:
            return (end - now_ms) / 1000.0
        # The window has ended, so the next pass restores the full share. What
        # the request still lacks after that is ``requested - share``, with
        # ``requested`` recovered as the deficit plus the balance it was
        # measured against — the caller may hand in the burnt image or the
        # already-restored one, and both must agree.
        still_short = deficit_milli + state.tokens_milli - state.reset_target_milli(now_ms)
        if still_short <= 0:
            return 0.0
        # `end` is not None, so the window length is on the state.
        assert state.reset_after_seconds is not None
        return float(state.reset_after_seconds)
    return retry_after_with_schedule(
        deficit_milli=deficit_milli,
        cp_milli=state.capacity_milli,
        ra_milli=state.refill_amount_milli,
        rp_ms=state.refill_period_ms,
        sched=state.sched,
        reset_sched=state.reset_sched,
        now_ms=now_ms,
        shard_count=state.shard_count,
    )


def calculate_retry_after(
    deficit_milli: int,
    refill_amount_milli: int,
    refill_period_ms: int,
    next_reset_ms: int | None = None,
    *,
    now_ms: int | None = None,
) -> float:
    """
    Calculate seconds until deficit is refilled.

    A quota — a limit that never drips and is restored in one lump by a
    ``reset_schedule`` — has no refill rate to compute a wait from. Its honest
    "retry after" is the next reset instant, so callers that know it pass it in
    ``next_reset_ms`` and the three cases become (#530):

    ==============================  ==================================
    Condition                       Result
    ==============================  ==================================
    :func:`~.models.is_accrual_rate`  rate arithmetic; ``next_reset_ms``
                                    is ignored entirely
    not accruing, reset known       the wait until that reset instant
    not accruing, no reset          ``0.0``
    ==============================  ==================================

    The rate handed in is an **effective** one — scheduled, and narrowed to one
    shard — so "not accruing" is the temporal predicate
    (:meth:`BucketState.accrues`), not "is a quota". The two differ: a dripping
    limit whose per-shard share floors to zero is also not accruing. Every
    caller reaches this through ``BucketState.retry_refill_amount_milli``,
    which falls back to the *undivided* rate for exactly that case, so a
    floored share never arrives here as a zero.

    The last row is therefore unreachable for any constructible limit. ADR-137
    makes ``refill_amount = 0`` valid *only* alongside a ``reset_schedule``, so
    a zero rate that survives that fallback implies a reset exists — which is
    precisely why the reset has to be threaded through here rather than left to
    the four call sites. What remains of that branch once again means what it
    always claimed to: a corrupt stored item whose rate is itself 0.

    Args:
        deficit_milli: How many millitokens we're short
        refill_amount_milli: Refill rate numerator
        refill_period_ms: Refill rate denominator
        next_reset_ms: Absolute epoch-ms instant of the next ``reset_schedule``
            edge, or None when the limit has no reset (or the caller does not
            know it). Consulted only when the refill rate is 0, and only
            together with ``now_ms``.
        now_ms: The clock reading ``next_reset_ms`` is measured against. Must
            be supplied alongside it; the two travel together because the
            instant alone cannot be turned into a wait.

    Since #222 §7 this has **no production callers**: every one of them walks
    forward across schedule boundaries instead (``schedule.retry_after_with_
    schedule``), because the rate in force *now* is the wrong divisor as soon
    as a boundary falls inside the wait. It stays as the definition that walk
    is pinned against — the walk's unscheduled path and its ``max_windows``
    fallback must return the identical value to the millisecond, which
    ``test_unscheduled_matches_calculate_retry_after_exactly`` enforces. The
    walk cannot simply call it: ``bucket`` imports ``models`` imports
    ``schedule``, so the arithmetic is inlined there instead.

    Returns:
        Seconds until deficit is recovered (float), or 0.0 when the bucket
        neither refills nor resets and no wait can be computed.
    """
    if deficit_milli <= 0:
        return 0.0
    if not is_accrual_rate(refill_amount_milli):
        if next_reset_ms is not None and now_ms is not None:
            # A reset restores the whole balance at once, so the edge *is* the
            # answer — no rate is involved. The +1ms mirrors the rounding
            # millisecond the rate arithmetic below adds.
            wait_ms = next_reset_ms - now_ms
            if wait_ms <= 0:
                # The edge has already passed and nothing has applied it yet.
                # Report no wait rather than a negative one.
                return 0.0
            return (wait_ms + 1) / 1000.0
        # A bucket that neither refills nor resets has no finite wait. Under
        # ADR-137 no constructible limit reaches here, so this guards a corrupt
        # bucket item whose stored rate is itself 0 — report no wait rather
        # than raise ZeroDivisionError from inside an error path.
        return 0.0

    # time_ms = deficit * period / amount
    time_ms = (deficit_milli * refill_period_ms) // refill_amount_milli
    # Add 1ms to ensure we've fully refilled (rounding)
    return (time_ms + 1) / 1000.0


def calculate_available(
    state: BucketState,
    now_ms: int,
) -> int:
    """
    Calculate currently available tokens (can be negative).

    Args:
        state: Current bucket state
        now_ms: Current timestamp in epoch milliseconds

    Returns:
        Available tokens (may be negative if bucket is in debt)
    """
    refill = _refilled(state, now_ms)
    return refill.new_tokens_milli // 1000


def calculate_time_until_available(
    state: BucketState,
    needed: int,
    now_ms: int,
) -> float:
    """
    Calculate seconds until `needed` tokens are available.

    Args:
        state: Current bucket state
        needed: Number of tokens needed
        now_ms: Current timestamp in epoch milliseconds

    Returns:
        Seconds until available (0.0 if already available)
    """
    refill = _refilled(state, now_ms)

    needed_milli = needed * 1000
    if refill.new_tokens_milli >= needed_milli:
        return 0.0

    return retry_after_for_deficit(state, needed_milli - refill.new_tokens_milli, now_ms)


def force_consume(
    state: BucketState,
    amount: int,
    now_ms: int,
) -> tuple[int, int]:
    """
    Force consume tokens without checking limits (can go negative).

    Used for post-hoc adjustments like LLM token reconciliation.

    Args:
        state: Current bucket state
        amount: Amount to consume (positive) or return (negative)
        now_ms: Current timestamp in epoch milliseconds

    Returns:
        Tuple of (new_tokens_milli, new_last_refill_ms)
    """
    # First refill
    refill = _refilled(state, now_ms)

    # Consume (can go negative)
    new_tokens_milli = refill.new_tokens_milli - (amount * 1000)

    return new_tokens_milli, refill.new_last_refill_ms


def build_limit_status(
    entity_id: str,
    resource: str,
    limit: Limit,
    state: BucketState,
    requested: int,
    now_ms: int,
) -> LimitStatus:
    """
    Build a LimitStatus for a bucket check.

    Args:
        entity_id: Entity being checked
        resource: Resource being accessed
        limit: Limit configuration
        state: Current bucket state
        requested: Amount requested
        now_ms: Current timestamp

    Returns:
        LimitStatus with full details
    """
    result = try_consume(state, requested, now_ms)

    return LimitStatus(
        entity_id=entity_id,
        resource=resource,
        limit_name=limit.name,
        limit=limit,
        available=result.available,
        requested=requested,
        exceeded=not result.success,
        retry_after_seconds=result.retry_after_seconds,
        resets_at_ms=window_end_in_force(limit, state, now_ms),
    )


def would_refill_satisfy(
    buckets: list[BucketState],
    consume: dict[str, int],
    now_ms: int,
) -> tuple[bool, list[LimitStatus]]:
    """Check if refilling buckets would allow the request to succeed.

    Used after a speculative UpdateItem fails to determine whether the slow
    path (which does refill) would help, or whether the request should be
    rejected immediately.

    Args:
        buckets: Current bucket states (from ALL_OLD, before refill)
        consume: Amount requested per limit (limit_name -> tokens)
        now_ms: Current timestamp for refill calculation

    Returns:
        Tuple of (would_satisfy, statuses) where:
        - would_satisfy: True if ALL declared limits pass after refill
        - statuses: LimitStatus for each declared limit (for RateLimitExceeded)
    """
    statuses = declared_statuses(buckets, consume, now_ms)
    any_exceeded = any(s.exceeded for s in statuses)
    return (not any_exceeded, statuses)


def declared_statuses(
    buckets: list[BucketState],
    consume: dict[str, int],
    now_ms: int,
) -> list[LimitStatus]:
    """Build a LimitStatus for every bucket whose limit is declared in ``consume``.

    This is the single definition of "declared" for status reporting
    (Issue #455): membership in ``consume``, not the amount. A declared
    zero-estimate limit (``{"tpm": 0}``) gets a passed status with
    ``requested=0``, exactly as on the slow path; a limit the caller never
    named — including the reserved ``wcu`` infrastructure limit that
    ``result.buckets`` carries — is skipped, so it never reaches
    ``RateLimitExceeded``.

    Args:
        buckets: Bucket states to report on (typically a speculative result)
        consume: Amount requested per limit (limit_name -> tokens)
        now_ms: Current timestamp for refill calculation
    """
    statuses: list[LimitStatus] = []
    for image in buckets:
        if image.limit_name not in consume:
            continue
        state = _restored_if_window_ended(image, now_ms)
        statuses.append(
            build_limit_status(
                entity_id=state.entity_id,
                resource=state.resource,
                limit=Limit.from_bucket_state(state).per_shard(
                    state.report_shard_count, now_ms, state.report_top_up_milli
                ),
                state=state,
                requested=consume[state.limit_name],
                now_ms=now_ms,
            )
        )
    return statuses
