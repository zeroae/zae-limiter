"""Fill-instant arithmetic for dripping limits (ADR-154, ADR-155, ADR-156; #817).

A dripping limit's state on one bucket shard is its **fill instant** ``z``: the
instant its share is (or was) back at capacity, so the balance at ``t`` is
``C − r × max(0, z − t)`` (``docs/contributing/fill-instant-proofs.md`` §1).
ADR-154 stores it scaled by the limit's undivided refill amount in force,
``Z = z_ms × ra``, which makes every quantity here an exact integer. For one of
``S`` shards of a bucket with undivided capacity ``C_b`` (millitokens), refill
amount ``ra`` (millitokens) and period ``rp`` (ms):

- a debit of ``c`` millitokens moves ``Z`` by ``c × S × rp``;
- the not-full slack is ``(C_b − c × S) × rp``;
- the balance, scaled by ``S × rp``, is ``C_b × rp − max(0, Z − t × ra)``.

Nothing is divided, so no per-shard rate floors to zero. The one operation that
can land between integers, a re-expression against changed parameters, rounds
toward the smaller balance (the later ``Z``).

Pure: integers only, no I/O, and nothing imported beyond the standard library and
``schedule``, so both Lambda packages can vendor it. ``bucket.py`` projects a
stored fill instant through it (#818), so the aggregator zip vendors it; the
provisioner zip will once its parameter sync uses it (#820).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from .schedule import MAX_STORED_MILLI, MAX_TOKENS, ScheduleEntry, effective_params, next_boundary

#: The largest amount, in millitokens, any single write may debit or credit.
#: ``MAX_TOKENS`` bounds a limit's capacity (#570); an amount past it would only
#: push a fill instant toward ``MAX_STORED_MILLI`` (ADR-154).
MAX_AMOUNT_MILLI = MAX_TOKENS * 1000

#: Above this utilisation estimate (per mille) a stale image showing a full
#: bucket is more likely wrong than right: a request finds the bucket not full
#: with probability ``u`` (proofs §6), so the not-full write goes first.
NOT_FULL_FIRST_ABOVE_PERMILLE = 500


def _stored(value: int) -> int:
    """``value``, checked against DynamoDB's exact integer range."""
    if abs(value) > MAX_STORED_MILLI:
        raise ValueError(
            f"fill instant value {value} is outside DynamoDB's exact range "
            f"(|value| <= {MAX_STORED_MILLI})"
        )
    return value


def _amount(amount_milli: int, what: str) -> int:
    """``amount_milli``, checked as a non-negative amount no larger than ``MAX_AMOUNT_MILLI``."""
    if amount_milli < 0:
        raise ValueError(f"{what} must be non-negative, got {amount_milli}")
    if amount_milli > MAX_AMOUNT_MILLI:
        raise ValueError(
            f"{what} {amount_milli} millitokens exceeds MAX_TOKENS ({MAX_TOKENS} tokens)"
        )
    return amount_milli


@dataclass(frozen=True)
class ShardParams:
    """The parameters one shard's fill instant is expressed against.

    Every value is the limit's **undivided** value in force: ``capacity_milli``
    and ``refill_amount_milli`` belong to the whole bucket, and ``shard_count``
    says how many shards share them.
    """

    capacity_milli: int
    refill_amount_milli: int
    refill_period_ms: int
    shard_count: int

    def __post_init__(self) -> None:
        if self.capacity_milli <= 0:
            raise ValueError(f"capacity must be positive, got {self.capacity_milli}")
        if self.refill_amount_milli <= 0:
            # A quota (ADR-137) does not drip and has no fill instant.
            raise ValueError(
                f"refill amount must be positive for a dripping limit, "
                f"got {self.refill_amount_milli}"
            )
        if self.refill_period_ms <= 0:
            raise ValueError(f"refill period must be positive, got {self.refill_period_ms}")
        if self.shard_count < 1:
            raise ValueError(f"shard count must be at least 1, got {self.shard_count}")

    @property
    def capacity_units(self) -> int:
        """The shard's share in balance units: ``C_b × rp``."""
        return self.capacity_milli * self.refill_period_ms

    @property
    def units_per_milli(self) -> int:
        """Balance units per millitoken: ``S × rp``."""
        return self.shard_count * self.refill_period_ms

    def doubled(self) -> ShardParams:
        """The same limit split over twice as many shards."""
        return replace(self, shard_count=2 * self.shard_count)


def stamp(t_ms: int, params: ShardParams) -> int:
    """A clock reading ``t_ms``, scaled as a fill instant is: ``t × ra``."""
    return _stored(t_ms * params.refill_amount_milli)


def span(amount_milli: int, params: ShardParams) -> int:
    """How far a debit or credit of ``amount_milli`` moves the fill instant: ``c × S × rp``."""
    return _stored(_amount(amount_milli, "amount") * params.units_per_milli)


def exceeds_share(amount_milli: int, params: ShardParams) -> bool:
    """Whether ``amount_milli`` is more than one shard's share of the capacity in force."""
    return amount_milli * params.shard_count > params.capacity_milli


def balance_units(z: int, t_ms: int, params: ShardParams) -> int:
    """The balance at ``t_ms``, exact, in units of ``1 / (S × rp)`` millitoken.

    Negative when the shard is in debt. Divide by ``params.units_per_milli`` for
    millitokens; :func:`balance_milli` does so, rounding down.
    """
    return params.capacity_units - max(0, z - stamp(t_ms, params))


def balance_milli(z: int, t_ms: int, params: ShardParams) -> int:
    """The balance at ``t_ms`` in millitokens, rounded down."""
    return balance_units(z, t_ms, params) // params.units_per_milli


class Side(Enum):
    """Which side of a write's clock reading the fill instant lies on."""

    NOT_FULL = "not_full"
    FULL = "full"


@dataclass(frozen=True)
class Write:
    """One of ADR-154's two conditional writes on a fill instant.

    ``applies`` is the write's condition and ``apply`` its update, exactly as a
    conditional ``UpdateItem`` evaluates them against the stored value.
    """

    side: Side
    stamp: int
    span: int
    upper: int | None = None
    """Not-full only: the largest fill instant the write admits; ``None`` for a debit."""

    def applies(self, z: int) -> bool:
        """Whether the condition holds on a stored fill instant ``z``."""
        if self.side is Side.FULL:
            return z < self.stamp
        return self.stamp <= z and (self.upper is None or z <= self.upper)

    def apply(self, z: int) -> int:
        """The fill instant the write stores, given that its condition holds on ``z``."""
        if not self.applies(z):
            raise ValueError(f"{self.side.value} write does not apply to fill instant {z}")
        if self.side is Side.FULL:
            return _stored(self.stamp + self.span)
        return _stored(z + self.span)


def not_full_write(
    t_ms: int, amount_milli: int, params: ShardParams, *, bounded: bool = True
) -> Write:
    """The not-full write: ``Z += c × S × rp`` under ``t × ra ≤ Z`` (and the slack, if ``bounded``).

    Unbounded, it is the debit a positive adjustment or a soft limit makes
    (ADR-155): it is never refused for a lack of tokens.
    """
    at = stamp(t_ms, params)
    move = span(amount_milli, params)
    upper = None
    if bounded:
        upper = _stored(
            at
            + (params.capacity_milli - amount_milli * params.shard_count) * params.refill_period_ms
        )
    return Write(Side.NOT_FULL, at, move, upper)


def full_write(t_ms: int, amount_milli: int, params: ShardParams) -> Write:
    """The full write: ``Z = t × ra + c × S × rp`` under ``Z < t × ra``.

    Its condition does not bound the request, so a hard limit must not send it
    when the amount exceeds the per-shard capacity (:func:`plan_first`).
    """
    return Write(Side.FULL, stamp(t_ms, params), span(amount_milli, params))


def plan_first(
    t_ms: int,
    amount_milli: int,
    params: ShardParams,
    *,
    image: int | None = None,
    utilisation_permille: int | None = None,
    full_first: bool = False,
    soft: bool = False,
) -> Write | None:
    """The first write of an admission, or ``None`` to reject it without a write.

    ``image`` is the last fill instant this client saw, which admissions by
    others can only have moved later (proofs Lemma 2): an image showing the
    bucket not full is still right, so the not-full write goes. An image showing
    it full, or no image, sends the full write, unless the shard's utilisation
    estimate makes not-full the likelier side (proofs §6) and ``full_first`` is
    not asked for. A hard limit whose amount exceeds the per-shard capacity in
    force is rejected here, because the full write's condition would admit it.
    """
    if not soft and exceeds_share(amount_milli, params):
        return None
    if utilisation_permille is not None and utilisation_permille < 0:
        raise ValueError(f"utilisation must be non-negative, got {utilisation_permille}")
    if image is not None and image >= stamp(t_ms, params):
        return not_full_write(t_ms, amount_milli, params, bounded=not soft)
    if (
        not full_first
        and utilisation_permille is not None
        and utilisation_permille > NOT_FULL_FIRST_ABOVE_PERMILLE
    ):
        return not_full_write(t_ms, amount_milli, params, bounded=not soft)
    return full_write(t_ms, amount_milli, params)


def plan_retry(
    t_ms: int,
    amount_milli: int,
    params: ShardParams,
    failure_image: int,
    *,
    soft: bool = False,
) -> Write | None:
    """The next write after one failed, from the fill instant the failure returned.

    The same ``t_ms`` as the first write: a fresh reading would let a bucket seen
    not full become full in between (proofs §5). ``None`` means the request is
    rejected: the failure showed the bucket past its slack, which only more
    admissions can follow.
    """
    if not soft and exceeds_share(amount_milli, params):
        return None
    at = stamp(t_ms, params)
    if failure_image < at:
        return full_write(t_ms, amount_milli, params)
    write = not_full_write(t_ms, amount_milli, params, bounded=not soft)
    if write.upper is not None and failure_image > write.upper:
        return None
    return write


def debit_write(
    t_ms: int, amount_milli: int, params: ShardParams, *, image: int | None = None
) -> Write:
    """A debit of ``amount_milli`` with no admission test (ADR-155).

    A positive adjustment, or a soft limit's debit after admission. It records
    ``max(z, t) + c / r`` like an admission, so it must still choose its side,
    from ``image``; it is retried with a fresh ``t_ms`` until one lands, because
    dropping it would forgive the debt.
    """
    if image is not None and image >= stamp(t_ms, params):
        return not_full_write(t_ms, amount_milli, params, bounded=False)
    return full_write(t_ms, amount_milli, params)


def returned(z: int, amount_milli: int, params: ShardParams) -> int:
    """``z`` after returning ``amount_milli``: a release, rollback or negative adjustment.

    Exact on every item with no condition: ``b ← min(C, b + c)``, the cap coming
    for free because every fill instant before now reads as full (proofs Lemma 3).
    """
    return _stored(z - span(amount_milli, params))


def topped_up(z: int, amount_milli: int, params: ShardParams) -> int:
    """``z`` after a top-up of ``amount_milli`` (ADR-149): capped at capacity, like a return."""
    return returned(z, amount_milli, params)


def reset(t_ms: int, params: ShardParams) -> int:
    """The fill instant of a shard reset to full at ``t_ms``, forgiving any debt."""
    return stamp(t_ms, params)


def created_full(t_ms: int, params: ShardParams) -> int:
    """A bucket's first shard, created full at ``t_ms`` (ADR-156)."""
    return stamp(t_ms, params)


def created_empty(t_ms: int, params: ShardParams) -> int:
    """A shard created empty at ``t_ms`` because others of its bucket exist (ADR-156)."""
    return _stored(stamp(t_ms, params) + params.capacity_units)


def from_tokens(tokens_milli: int, refilled_ms: int, params: ShardParams) -> int:
    """Convert a legacy ``tk``/``rf`` shard: ``Z = rf × ra + (C_b − tk × S) × rp``.

    Exact. The balance at ``rf`` is ``tk``; a surplus above the share reads as a
    full bucket, and a debt as a fill instant beyond ``rf`` plus one fill.
    """
    return _stored(
        refilled_ms * params.refill_amount_milli
        + (params.capacity_milli - tokens_milli * params.shard_count) * params.refill_period_ms
    )


def re_express(z: int, tau_ms: int, old: ShardParams, new: ShardParams) -> int:
    """``z`` re-expressed at ``tau_ms`` from ``old`` parameters to ``new`` (ADR-155).

    The balance at ``tau_ms`` is kept, or trimmed to the new share, and never
    raised: ``z' = τ + (C' − min(b, C')) / r'``. When the new fill instant falls
    between integers it is rounded up, toward the smaller balance.
    """
    numerator = balance_units(z, tau_ms, old)
    denominator = old.units_per_milli
    # The kept balance in the new units: min(b, C'/S') × S' × rp', floored.
    scaled = numerator * new.units_per_milli
    if scaled >= new.capacity_units * denominator:
        kept = new.capacity_units
    else:
        kept = scaled // denominator
    return _stored(stamp(tau_ms, new) + new.capacity_units - kept)


def params_at(
    t_ms: int,
    capacity_milli: int,
    refill_amount_milli: int,
    refill_period_ms: int,
    sched: tuple[ScheduleEntry, ...],
    shard_count: int,
) -> ShardParams:
    """The shard parameters a limit's schedule puts in force at ``t_ms``."""
    cp, ra, rp = effective_params(
        capacity_milli, refill_amount_milli, refill_period_ms, sched, t_ms
    )
    return ShardParams(cp, ra, rp, shard_count)


def re_express_across(
    z: int,
    expressed_ms: int,
    now_ms: int,
    capacity_milli: int,
    refill_amount_milli: int,
    refill_period_ms: int,
    sched: tuple[ScheduleEntry, ...],
    shard_count: int,
) -> tuple[int, int]:
    """``z`` re-expressed at every schedule boundary after ``expressed_ms`` up to ``now_ms``.

    Each window drips at its own rate and caps at its own ceiling, which is the
    strict bucket with time-varying parameters (proofs §8). Today's refill
    credits the whole interval at the rate in force at its end, which admitted
    1000 against 117 across a boundary from 0.1x to 1x. The base parameters are
    undivided; the schedule picks the parameters in force.

    Returns the new fill instant and the instant it was last expressed: the
    latest boundary crossed, or ``expressed_ms`` when none was. Linear in the
    boundaries crossed.
    """
    current = params_at(
        expressed_ms, capacity_milli, refill_amount_milli, refill_period_ms, sched, shard_count
    )
    last = expressed_ms
    while sched:
        boundary = next_boundary(sched, now_ms=last)
        if boundary is None or boundary > now_ms:
            break
        following = params_at(
            boundary, capacity_milli, refill_amount_milli, refill_period_ms, sched, shard_count
        )
        z = re_express(z, boundary, current, following)
        current, last = following, boundary
    return z, last


def split(z: int, t_ms: int, params: ShardParams) -> tuple[ShardParams, int, int]:
    """Split one shard at ``t_ms`` as a raise from ``S`` to ``2S`` shards does (ADR-156).

    The shard keeps ``min(b, C / 2S)`` and the shard it splits off, ``i + S``,
    gets ``max(0, b − C / 2S)``, which is at most one new share. The total is
    conserved exactly, and a shard in debt keeps its debt. Returns the new
    parameters, the shard's new fill instant and the new shard's.
    """
    new = params.doubled()
    at = stamp(t_ms, params)
    # The balance in the new units (S' × rp = 2 × S × rp), against the new share.
    doubled_balance = 2 * balance_units(z, t_ms, params)
    share = params.capacity_units
    kept = min(doubled_balance, share)
    moved = max(0, doubled_balance - share)
    return new, _stored(at + share - kept), _stored(at + share - moved)
