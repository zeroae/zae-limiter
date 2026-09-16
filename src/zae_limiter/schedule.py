"""Cron schedule parsing, matching and evaluation (#222, ADR-135).

Pure stdlib plus ``cronsim``, with no imports from ``zae_limiter.models`` — so
``models`` can import ``ScheduleEntry`` from here without a cycle, and the
aggregator and provisioner Lambdas run exactly the same code path as the
client.

Parsing is cronsim's; matching is ours. cronsim gives fully expanded int sets
plus ``day_and``, which resolves the day-of-month/weekday OR rule — the single
highest-risk thing to hand-roll. Three things it does not do for us, each
confirmed by test:

1. Sunday is ``0`` for ``SUN``/``0`` but ``7`` for ``7``; we normalise to
   ``datetime.isoweekday()``'s 7.
2. ``L``/``W``/``#`` parse *without error* and inject sentinel ints and tuples
   into the sets, so a naive match silently never fires. We reject them.
3. The match loop itself.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cronsim import CronSim, CronSimError

__all__ = [
    "ParsedCron",
    "ScheduleEntry",
    "decode",
    "decode_reset",
    "effective_params",
    "encode",
    "encode_reset",
    "entry_params",
    "matches",
    "next_boundary",
    "next_reset_edge",
    "parse_cron",
    "prev_reset_edge",
    "retry_after_with_schedule",
    "to_cron",
]

# cronsim's sentinels for the extended tokens we do not support.
_SENTINELS = {CronSim.LAST, CronSim.LAST_WEEKDAY}

# cronsim needs a reference datetime to construct; we only want the parsed
# field sets, so any fixed instant will do.
_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class ParsedCron:
    """A cron expression expanded into field sets, ready to match against."""

    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_and: bool
    tz: ZoneInfo


def _plain_ints(values: Iterable[object], field: str) -> frozenset[int]:
    """Return ``values`` as plain ints, rejecting cronsim's extended-token members.

    Also the narrowing point for the type checker: cronsim types its day sets as
    ``int | tuple[int, int]`` precisely because ``FRI#2`` and ``5L`` land there.
    """
    out: set[int] = set()
    for v in values:
        if not isinstance(v, int) or v in _SENTINELS:
            raise ValueError(
                f"cron {field} field uses an extended token (L, W, or #), which is "
                f"not supported. These parse without error but would silently never "
                f"match, so they are rejected rather than accepted and ignored."
            )
        out.add(v)
    return frozenset(out)


@lru_cache(maxsize=512)
def parse_cron(cron: str, tz: str) -> ParsedCron:
    """Parse a 5-field cron expression and timezone into matchable field sets.

    Cached: the arguments are two strings and the result is a frozen dataclass of
    frozensets, so it is safe to share. Every evaluation path re-parses the same
    handful of expressions — ``effective_params`` once per entry per call, and
    ``next_boundary`` once per entry per call — so the cache turns a ``CronSim``
    construction into a dict lookup.
    """
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone {tz!r}: {exc}") from exc

    # cronsim also accepts a SIX-field expression whose leading field is seconds.
    # Nothing here is finer than a minute — `matches` compares minute/hour/day,
    # `next_boundary` steps by the minute, and the compact encoding (§4.1) has
    # five field slots — so a seconds field would be silently widened to its
    # whole minute and then be unrepresentable in storage. Reject it up front.
    if len(cron.split()) != 5:
        raise ValueError(
            f"invalid cron expression {cron!r}: exactly 5 fields are required "
            f"(minute hour day-of-month month day-of-week). A 6-field expression "
            f"with a leading seconds field parses but cannot be honoured: nothing "
            f"in this module is finer than one minute."
        )

    try:
        parsed = CronSim(cron, _EPOCH)
    except CronSimError as exc:
        raise ValueError(f"invalid cron expression {cron!r}: {exc}") from exc

    days = _plain_ints(parsed.days, "day-of-month")
    weekdays = _plain_ints(parsed.weekdays, "day-of-week")

    # isoweekday() is Mon=1..Sun=7. cronsim yields 0 for SUN/0 and 7 for 7,
    # so fold 0 into 7 and keep both spellings working.
    if 0 in weekdays:
        weekdays = (weekdays - {0}) | {7}

    return ParsedCron(
        minutes=frozenset(parsed.minutes),
        hours=frozenset(parsed.hours),
        days=days,
        months=frozenset(parsed.months),
        weekdays=weekdays,
        day_and=parsed.day_and,
        tz=zone,
    )


@dataclass(frozen=True)
class ScheduleEntry:
    """One window of a schedule: a cron pattern and the limit that applies in it.

    The cron expression is a **match pattern**, not a fire time: the entry is
    active for every minute the pattern matches (§1.2). ``* 9-17 * * MON-FRI``
    is business hours, not "once at the top of each of those hours".

    A **reset** entry (``ScheduleEntry.reset``) is the one exception to both
    halves of that sentence. It carries ``cron`` and ``tz`` and nothing else,
    because it overrides no parameters (§3.6), and it is read as an *edge*
    rather than a window: it fires on the transition into matching, not for
    every minute of it. The two kinds live in separate tuples on ``Limit``
    (``schedule`` and ``reset_schedule``) and are validated by opposite rules,
    so the flag is private and set only by the classmethod.
    """

    cron: str
    tz: str = "UTC"
    scale: float | None = None
    capacity: int | None = None
    refill_amount: int | None = None
    refill_period_seconds: int | None = None
    _reset: bool = False

    @classmethod
    def reset(cls, cron: str, tz: str = "UTC") -> ScheduleEntry:
        """An entry that resets the balance rather than changing the params.

        The instant this expression starts matching, the bucket's ``tk`` goes
        back to the effective capacity in force there (§3.6) — the one thing a
        token bucket cannot express, and what makes "10,000 a day, back to
        10,000 at midnight" different from a 24-hour refill period.

        Belongs in ``Limit.reset_schedule``, never in ``Limit.schedule``: the
        parameter tuple is resolved first-match-wins, so an entry that supplies
        no parameters would win its window and then shadow every entry below
        it.
        """
        return cls(cron=cron, tz=tz, _reset=True)

    def __post_init__(self) -> None:
        parse_cron(self.cron, self.tz)  # raises ValueError on anything unusable

        if self._reset:
            # A reset names an instant, not a parameter override (§3.6). The
            # positivity checks below are unreachable once this holds, since
            # every modifier is None.
            carried = sorted(
                name
                for name in ("scale", "capacity", "refill_amount", "refill_period_seconds")
                if getattr(self, name) is not None
            )
            if carried:
                raise ValueError(
                    f"a reset schedule entry carries `cron` and `tz` only; got {carried}. "
                    f"A reset overrides no parameters — it names the instant the balance "
                    f"goes back to the effective capacity."
                )
            return

        absolutes = (self.capacity, self.refill_amount, self.refill_period_seconds)
        has_absolute = any(v is not None for v in absolutes)
        if (self.scale is not None) == has_absolute:
            raise ValueError(
                "a schedule entry must set exactly one of `scale` or the absolute "
                "fields (`capacity`/`refill_amount`/`refill_period_seconds`)"
            )
        for name, value, is_absolute in (
            ("scale", self.scale, False),
            ("capacity", self.capacity, True),
            ("refill_amount", self.refill_amount, True),
            ("refill_period_seconds", self.refill_period_seconds, True),
        ):
            if value is None:
                continue
            # Non-finite first, because the positivity test cannot catch it: every
            # comparison against NaN is False, so `<= 0` *admits* a NaN, and an
            # infinity is trivially positive. Both then escape into storage and die
            # far from here — `scale` as "cannot convert float NaN to integer" from
            # inside `encode`, and an absolute worse still, encoding cleanly as the
            # byte string `cnan` that no later `decode` can read back. The isinstance
            # guard is load-bearing: `math.isfinite` converts its argument to a float,
            # so a bare call would turn an absurd-but-currently-workable integer
            # capacity of 10**400 into an OverflowError raised from validation.
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{name} must be a finite number, got {value}")
            # Then integrality, for the three `int | None` absolutes only —
            # `scale` is a float field by design. Same failure class as the
            # non-finite case and invisible to its guard, because this one is a
            # *type* problem: `math.isfinite(1.5)` is True, `c1.5` encodes
            # cleanly, and `decode`'s `int(tokens["c"])` then raises on every
            # later read of that config item (#569). `bool` is rejected
            # explicitly because it is an `int` subclass, so a bare
            # `isinstance(value, int)` would *admit* `capacity=True` and store
            # the equally unreadable `cTrue`.
            #
            # Rejected rather than coerced, integral floats included, to agree
            # with `zae_limiter_provisioner.handler._coerce_int` — the same
            # guard on the CloudFormation entrance since #561, which takes an
            # `int` or a string spelling one and refuses every float. The field
            # is declared `int`, so this narrows nothing the API documented.
            if is_absolute and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError(
                    f"{name} must be a whole number, got {value!r}. The absolute "
                    f"schedule fields are integers: a fractional or non-numeric "
                    f"value encodes into the stored schedule as bytes no later "
                    f"read can decode."
                )
            if value <= 0:
                raise ValueError(f"{name} must be positive, got {value}")


def matches(parsed: ParsedCron, now_ms: int) -> bool:
    """Is ``now_ms`` inside a window this expression matches?

    Converts the UTC instant into the expression's local timezone, which is
    always well-defined — so the nonexistent local hour at spring-forward and
    the doubled hour at fall-back never arise, and DST needs no special case.
    """
    d = datetime.fromtimestamp(now_ms / 1000, parsed.tz)
    if d.minute not in parsed.minutes:
        return False
    if d.hour not in parsed.hours:
        return False
    if d.month not in parsed.months:
        return False
    dom_ok = d.day in parsed.days
    dow_ok = d.isoweekday() in parsed.weekdays
    # When BOTH day fields are constrained, cron means OR, not AND.
    return (dom_ok and dow_ok) if parsed.day_and else (dom_ok or dow_ok)


def entry_params(
    cp_milli: int,
    ra_milli: int,
    rp_ms: int,
    entry: ScheduleEntry,
) -> tuple[int, int, int]:
    """The (capacity, refill_amount, refill_period) ``entry`` puts in force.

    The override arithmetic of §1.1/§1.2 with the *matching* taken out, so that
    a caller which already knows the entry applies — or which, like
    :func:`zae_limiter.schema._recovery_seconds`, must consider every window
    without a clock to pick one — shares this definition rather than restating
    it. All values are milli-units, matching ``bucket.py``.
    """
    if entry.scale is not None:
        # Scale capacity and refill together so time-to-fill is preserved
        # (§1.1). Truncate rather than round, so a scaled limit is never
        # larger than asked for; floor at 1 milli-unit, since a zero
        # capacity is unadmittable.
        #
        # The rate's floor is conditioned on the BASE rate, not on the
        # scaled result (#556). A quota has `refill_amount = 0` by
        # definition (ADR-137), and flooring that to 1 invents a drip the
        # limit is defined not to have: `BucketState.accrues()` then
        # answers True for a bucket that cannot accrue, and
        # `retry_after_with_schedule` walks a 1-millitoken-per-period rate
        # instead of going to the next reset edge. A limit that really
        # does drip still gets the floor, so a tiny scale cannot round a
        # live rate away to nothing.
        return (
            max(1, int(cp_milli * entry.scale)),
            0 if ra_milli == 0 else max(1, int(ra_milli * entry.scale)),
            rp_ms,
        )
    return (
        entry.capacity * 1000 if entry.capacity is not None else cp_milli,
        entry.refill_amount * 1000 if entry.refill_amount is not None else ra_milli,
        entry.refill_period_seconds * 1000 if entry.refill_period_seconds is not None else rp_ms,
    )


def effective_params(
    cp_milli: int,
    ra_milli: int,
    rp_ms: int,
    sched: tuple[ScheduleEntry, ...],
    now_ms: int,
) -> tuple[int, int, int]:
    """The (capacity, refill_amount, refill_period) in force at ``now_ms``.

    All values are milli-units, matching ``bucket.py``. Returns the base
    unchanged when no entry matches, so the unscheduled path costs one tuple
    check. **First matching entry wins** (§1.2). Never mutates or persists
    anything: the caller uses the result and discards it (§2.1 — ``tk`` is the
    only materialised quantity).
    """
    if not sched:
        return cp_milli, ra_milli, rp_ms

    for entry in sched:
        if matches(parse_cron(entry.cron, entry.tz), now_ms):
            return entry_params(cp_milli, ra_milli, rp_ms, entry)
    return cp_milli, ra_milli, rp_ms


# Scan granularity and horizon, chosen by the finest field any entry constrains.
_MINUTE_MS = 60_000
_DAY_MS = 86_400_000

# Scan cap per granularity unit. The unit also names the *local calendar* unit the
# match state is constant over, which is what the probe grid is built from.
_CAP = {
    "minute": 7 * _DAY_MS,
    "hour": 31 * _DAY_MS,
    "day": 366 * _DAY_MS,
}
_ADVANCE = {
    "minute": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
}

# How long until a pattern's firing set repeats, read off the COARSEST field it
# constrains — the opposite end of the expression from `_granularity`, which
# reads the finest. Long months and leap years, so the cycle is never short of a
# real gap; every branch rounds **up**, because too long only costs probes where
# too short hides an edge outright (#574).
_MINUTE_SECONDS = 60
_HOUR_SECONDS = 3_600
_DAY_SECONDS = 86_400
_WEEK_SECONDS = 7 * _DAY_SECONDS
_MONTH_SECONDS = 31 * _DAY_SECONDS
_YEAR_SECONDS = 366 * _DAY_SECONDS


def _granularity(parsed: tuple[ParsedCron, ...]) -> tuple[str, int]:
    """Unit and cap for a scan, from the finest field any entry constrains.

    The unit is not merely a step size: because the coarser units leave every
    finer cron field unconstrained (``hour`` implies all 60 minutes match,
    ``day`` implies all 60 minutes *and* all 24 hours), the match state is
    constant across one local calendar unit of that size. Every transition
    therefore falls exactly on a local unit start, which is what makes the
    probe grid below exhaustive.
    """
    if any(len(p.minutes) < 60 for p in parsed):
        return "minute", _CAP["minute"]
    if any(len(p.hours) < 24 for p in parsed):
        return "hour", _CAP["hour"]
    return "day", _CAP["day"]


def cycle_seconds(parsed: ParsedCron) -> int:
    """Upper bound on the gap between two consecutive firings of one pattern.

    Derived from the **coarsest** cron field the pattern constrains, because
    that is the cycle over which its firing set repeats: ``0 0 * * *``
    constrains the hour, so it repeats daily; ``0 0 1 * *`` constrains the
    day-of-month, so it repeats monthly.

    Exact for every pattern whose firing set repeats within its own cycle,
    which is every practical quota schedule. The one class it understates is a
    pattern that skips whole years — ``0 0 29 2 *`` fires on February 29th and
    so has a real gap near four years against the one year reported here.

    Day-of-month is tested before day-of-week deliberately: when both are
    constrained cron ORs them, so the answer must be the *wider* of the two
    cycles, and a month is wider than a week.

    Lives here rather than in ``schema.py`` because two unrelated callers ask
    the same question and drifting answers is exactly what #574 was:
    :func:`_reset_scan` sizes a scan horizon with it, and
    ``schema._reset_cycle_seconds`` sizes a quota bucket's TTL recovery horizon
    (#532).
    """
    if len(parsed.months) < 12:
        return _YEAR_SECONDS
    if len(parsed.days) < 31:
        return _MONTH_SECONDS
    if len(parsed.weekdays) < 7:
        return _WEEK_SECONDS
    if len(parsed.hours) < 24:
        return _DAY_SECONDS
    if len(parsed.minutes) < 60:
        return _HOUR_SECONDS
    return _MINUTE_SECONDS


def _reset_scan(parsed: ParsedCron) -> tuple[str, int]:
    """Probe step and scan horizon for **one** reset entry (#574).

    Step and horizon are different questions and are read off opposite ends of
    the expression. The *step* must be fine enough that the match state is
    constant across it, so it comes from :func:`_granularity` and the finest
    constrained field. The *horizon* asks how far away the next edge can be,
    which is :func:`cycle_seconds`' question and comes from the coarsest.

    Conflating the two is what #574 was: every practical reset pattern pins the
    minute, so every one of them — monthly, quarterly, annual alike — was
    scanned with the minute step's seven-day cap and reported no edge for most
    of its own cycle. ``next_reset_edge`` then returned ``None`` (surfacing as
    ``resets_at_ms: null`` in a 429 body, #545) and an annual quota's
    ``retry_after_seconds`` fell through to the flat estimate's ``0.0`` — "retry
    immediately" against a limit that cannot admit anything for months.

    The **maximum** of the two rather than the cycle outright, so the horizon
    can only ever grow: ``* * * * *`` constrains nothing and so has a
    one-minute cycle, but it is scanned at day granularity with a 366-day cap
    and shrinking that would hide edges the old code could see. In practice
    only the date-constrained patterns move — monthly to 31 days, quarterly and
    annual to 366.

    Not applied to :func:`_next_param_change`, whose cap is a
    re-materialisation interval rather than a search horizon: a coarse
    *parameter* window is still honoured exactly, because ``effective_params``
    is evaluated at read time. Only an *edge* can be missed by being out of
    reach.
    """
    unit, step_cap = _granularity((parsed,))
    return unit, max(step_cap, cycle_seconds(parsed) * 1000)


def _truncate(d: datetime, unit: str) -> datetime:
    """``d`` floored to the start of its local calendar ``unit`` — hour or day.

    The minute unit never arrives here: both probe helpers short-circuit it on
    integer arithmetic, because the local minute grid *is* the UTC one.
    """
    if unit == "day":
        return d.replace(hour=0, minute=0, second=0, microsecond=0)
    return d.replace(minute=0, second=0, microsecond=0)


def _unit_start(tz: ZoneInfo, t_ms: int, unit: str) -> int:
    """Epoch-ms at which the local calendar ``unit`` containing ``t_ms`` begins."""
    return int(_truncate(datetime.fromtimestamp(t_ms / 1000, tz), unit).timestamp() * 1000)


def _unit_after(tz: ZoneInfo, t_ms: int, unit: str) -> int:
    """Epoch-ms at which the local calendar ``unit`` *following* ``t_ms``'s begins."""
    d = datetime.fromtimestamp(t_ms / 1000, tz) + _ADVANCE[unit]
    return int(_truncate(d, unit).timestamp() * 1000)


def _next_probe(parsed: tuple[ParsedCron, ...], t_ms: int, unit: str) -> int:
    """The earliest local unit start, in any entry's zone, strictly after ``t_ms``.

    Probing a **UTC**-aligned grid of fixed spacing is what #540 was: a local unit
    shorter than the step can fall entirely between two probes and be skipped whole,
    so the scan reports a boundary a full window late. Local unit starts cannot skip
    a unit, however short the clock made it, because every unit contributes its own
    start to the grid.
    """
    t0 = (t_ms // _MINUTE_MS) * _MINUTE_MS
    if unit == "minute":
        # Every timezone offset in use is a whole number of minutes, so the local
        # minute grid *is* the UTC one. Worth the special case: this is the common
        # granularity and its cap is 10,080 probes, where the datetime round trip
        # below would dominate the scan.
        return t0 + _MINUTE_MS
    nxt = min(_unit_after(p.tz, t_ms, unit) for p in parsed)
    return max((nxt // _MINUTE_MS) * _MINUTE_MS, t0 + _MINUTE_MS)


def _prev_probe(parsed: tuple[ParsedCron, ...], t_ms: int, unit: str) -> int:
    """The latest local unit start, in any entry's zone, strictly before ``t_ms``."""
    t0 = (t_ms // _MINUTE_MS) * _MINUTE_MS
    if unit == "minute":
        return t0 - _MINUTE_MS
    starts = []
    for p in parsed:
        s = _unit_start(p.tz, t0, unit)
        if s >= t0:
            s = _unit_start(p.tz, t0 - _MINUTE_MS, unit)
        starts.append(s)
    prev = max(starts)
    return min((prev // _MINUTE_MS) * _MINUTE_MS, t0 - _MINUTE_MS)


def _active_index(parsed: tuple[ParsedCron, ...], now_ms: int) -> int | None:
    """Index of the first matching entry, or None. First match wins (§1.2)."""
    for i, p in enumerate(parsed):
        if matches(p, now_ms):
            return i
    return None


def _next_param_change(sched: tuple[ScheduleEntry, ...], now_ms: int) -> int:
    """The earliest instant after ``now_ms`` where the active ``schedule`` entry changes.

    Returns ``now_ms + cap`` when no transition is found within the horizon, which
    forces one cheap re-materialisation per active bucket per cap period rather
    than looping.

    This is a scan, not a library call, because the boundary set includes window
    *closings* and no cron library computes those (§3.2).

    The scan is two-phase. The coarse pass probes **local calendar unit starts**
    (see ``_next_probe``): the match state is constant across one such unit at
    the chosen granularity, so every transition falls on a probe and none can be
    skipped. A returned boundary that is *late* is the unsafe direction — ``vu``
    would keep the fast path on the old limits well inside the new window — so
    the step that straddles the change is still re-walked at minute resolution
    as a bounded backstop for any zone whose unit start we resolve imprecisely.
    That costs at most 59 (hourly) or 1439 (daily) extra matches, once, and only
    on the step where the change actually happens.
    """
    parsed = tuple(parse_cron(e.cron, e.tz) for e in sched)
    unit, cap = _granularity(parsed)
    current = _active_index(parsed, now_ms)
    horizon = now_ms + cap

    lo = (now_ms // _MINUTE_MS) * _MINUTE_MS
    probe = _next_probe(parsed, now_ms, unit)
    while probe <= horizon:
        if _active_index(parsed, probe) != current:
            # Refine: the true edge lies in (lo, probe]. Every probe is
            # minute-aligned, so this loop is bounded and `probe` is always a
            # valid answer if nothing earlier is.
            fine = lo + _MINUTE_MS
            while fine < probe:
                if _active_index(parsed, fine) != current:
                    return fine
                fine += _MINUTE_MS
            return probe
        lo = probe
        probe = _next_probe(parsed, probe, unit)
    return now_ms + cap


# ---------------------------------------------------------------------------
# Reset edges (§3.6)
#
# A `schedule` entry is level-triggered — it is active for every minute it
# matches. A `reset_schedule` entry is **edge**-triggered: it fires on the
# transition *into* matching and not for the rest of the window. `0 0 * * *`
# matches for exactly one minute, and reading it as a level would make the reset
# depend on a request happening to arrive inside that minute.
#
# Detection is therefore backwards. The materialising pass asks
# `prev_reset_edge(reset_sched, now) > rf` — was there a rising edge since this
# item was last refilled? — which makes idle buckets correct for free: a bucket
# idle from 18:00 to 09:00 has `vu` sitting at midnight, and the 09:00 pass sees
# the missed edge and applies the reset then. Two missed midnights apply once,
# because setting the balance to the effective capacity is idempotent.
# ---------------------------------------------------------------------------


def _unreachable_block(parsed: ParsedCron, t_ms: int) -> str | None:
    """The largest local calendar block containing ``t_ms`` that cannot match.

    ``"day"`` when the *date* fields rule the whole local day out, ``"hour"``
    when the date matches but the hour does not, ``None`` when only the minute
    can still decide. Exactly :func:`matches` with the minute test removed, and
    read at the same granularities the probe grid is built from.

    This is what keeps the widened reset horizon (#574) affordable. A search for
    a *match* may skip such a block whole, because every instant in it shares
    the state being walked away from: the date fields depend only on the local
    date, so if they fail, no minute of that local day matches; likewise the
    hour field within one local hour. A search for a **non**-match may not —
    the block is exactly what it is looking for — so both call sites below skip
    only when ``want`` is True.

    The bound that follows: a forward search costs at most one probe per
    non-matching local day in the horizon, plus at most 24 hour probes and 60
    minute probes on the first date that does match (the field sets are never
    empty, so a matching date always yields a matching minute). That is ~450
    probes over a 366-day horizon against the 527,040 a flat minute walk would
    cost.
    """
    d = datetime.fromtimestamp(t_ms / 1000, parsed.tz)
    if d.month not in parsed.months:
        return "day"
    dom_ok = d.day in parsed.days
    dow_ok = d.isoweekday() in parsed.weekdays
    if not ((dom_ok and dow_ok) if parsed.day_and else (dom_ok or dow_ok)):
        return "day"
    if d.hour not in parsed.hours:
        return "hour"
    return None


def _earliest_where(
    parsed: ParsedCron, want: bool, start_ms: int, horizon_ms: int, unit: str
) -> int | None:
    """Earliest minute at or after ``start_ms`` whose match state is ``want``.

    No minute refinement, unlike ``_next_param_change``: probes are local unit
    starts and the match state is constant across one unit, so the first probe
    with the wanted state *is* the earliest minute with it. Everything between
    the previous probe and this one shares the previous probe's state, which is
    the state we were walking away from.

    The horizon guard rides on the first ``matches`` rather than standing alone,
    because both call sites below pass a ``start_ms`` already inside the horizon;
    it is there so an answer can never be returned from beyond it.

    When looking for a match, whole local days and hours that
    :func:`_unreachable_block` rules out are stepped over in one probe. That
    skips only instants already known to be non-matching, so the grid stays
    exhaustive; see that function for the resulting bound.
    """
    t = -(-start_ms // _MINUTE_MS) * _MINUTE_MS
    if t <= horizon_ms and matches(parsed, t) == want:
        return t

    single = (parsed,)
    probe = _next_probe(single, t, unit)
    while probe <= horizon_ms:
        if matches(parsed, probe) == want:
            return probe
        block = _unreachable_block(parsed, probe) if want else None
        if block is None:
            probe = _next_probe(single, probe, unit)
        else:
            after = (_unit_after(parsed.tz, probe, block) // _MINUTE_MS) * _MINUTE_MS
            probe = max(after, probe + _MINUTE_MS)
    return None


def _latest_where(
    parsed: ParsedCron, want: bool, start_ms: int, floor_ms: int, unit: str
) -> int | None:
    """Latest minute at or before ``start_ms`` whose match state is ``want``.

    The mirror image of ``_earliest_where``, and capped the same way: nothing
    older than ``floor_ms`` is looked at, so an expression with no match in reach
    costs one horizon's worth of probes and reports nothing.

    Not quite a mirror in one place. Going forwards the answer is the probe
    itself; going backwards it is the **last minute of that probe's unit**,
    ``hi - 1``, since the whole unit shares the probe's state and ``hi`` is
    where that state stopped. `* 0 * * *` looked up from 05:00 must answer 00:59,
    not 00:00 — the caller turns that into the 00:00 edge, and doing it here
    would lose the window's length.

    Skips whole unreachable days and hours exactly as ``_earliest_where`` does,
    and under the same restriction to ``want=True``. ``hi`` moves back to the
    *start* of the skipped block rather than to the probe, which is what keeps
    the "everything in ``[probe, hi)`` shares ``probe``'s state" invariant true
    across a skip: a day or hour start is also a unit start at every
    granularity, so the probe below it is still exactly one unit earlier.
    """
    t = (start_ms // _MINUTE_MS) * _MINUTE_MS
    if t < floor_ms:
        return None
    if matches(parsed, t) == want:
        return t

    single = (parsed,)
    hi = t
    probe = _prev_probe(single, t, unit)
    while probe >= floor_ms:
        if matches(parsed, probe) == want:
            return hi - _MINUTE_MS
        block = _unreachable_block(parsed, probe) if want else None
        if block is None:
            hi = probe
        else:
            hi = min(hi, (_unit_start(parsed.tz, probe, block) // _MINUTE_MS) * _MINUTE_MS)
        probe = _prev_probe(single, hi, unit)
    return None


def _next_rising_edge(parsed: ParsedCron, now_ms: int, unit: str, cap: int) -> int | None:
    """The earliest rising edge strictly after ``now_ms``, or None within the cap.

    Standing *inside* a matching window, the next edge is not the next matching
    minute — that is this same window — so the window must be left first.
    """
    horizon = now_ms + cap
    start = (now_ms // _MINUTE_MS + 1) * _MINUTE_MS
    if matches(parsed, now_ms):
        gap = _earliest_where(parsed, False, start, horizon, unit)
        if gap is None:
            return None
        start = gap
    return _earliest_where(parsed, True, start, horizon, unit)


def _prev_rising_edge(parsed: ParsedCron, now_ms: int, unit: str, cap: int) -> int | None:
    """The most recent rising edge at or before ``now_ms``, or None within the cap.

    Two backwards searches rather than one: the latest matching minute is only
    the *inside* of the most recent window, and the edge is where that window
    opened. Finding a match that runs unbroken back to the cap proves no edge
    within the horizon, not an edge at the horizon.
    """
    floor = now_ms - cap
    on = _latest_where(parsed, True, now_ms, floor, unit)
    if on is None:
        return None
    off = _latest_where(parsed, False, on - _MINUTE_MS, floor, unit)
    if off is None:
        return None
    return off + _MINUTE_MS


def prev_reset_edge(reset_sched: tuple[ScheduleEntry, ...], now_ms: int) -> int | None:
    """The most recent reset edge at or before ``now_ms``, across every entry.

    The **maximum** across entries, not the first entry that has one: entries in
    a reset tuple are independent instants, not the priority-ordered overrides of
    ``schedule`` (§1.2), and the materialising pass only needs to know whether
    *any* edge has been missed since ``rf``. Applying a reset is idempotent, so
    the latest one subsumes every earlier one.

    Each entry gets its own step and horizon from :func:`_reset_scan`, because
    they are independent: a `0 0 * * *` neighbour must not shorten a
    `0 0 1 * *`'s horizon from 31 days to 7 and hide its edge.

    Returns None for an empty tuple and for an expression with no edge inside the
    cap, which resets nothing. (§3.6 names `0 0 30 2 *` for that case; cronsim
    rejects February 30th outright, so the constructible equivalent is
    `0 0 29 2 *` — a leap day, out of reach even of the 366-day horizon a
    month-constrained pattern earns, for three years in every four.)
    """
    edges = []
    for entry in reset_sched:
        parsed = parse_cron(entry.cron, entry.tz)
        unit, cap = _reset_scan(parsed)
        edge = _prev_rising_edge(parsed, now_ms, unit, cap)
        if edge is not None:
            edges.append(edge)
    return max(edges) if edges else None


def _forward_reset_scan(
    reset_sched: tuple[ScheduleEntry, ...], now_ms: int
) -> list[tuple[int | None, int]]:
    """``(edge or None, cap)`` per entry, scanned forward from ``now_ms``.

    One scanner, two readings. :func:`next_reset_edge` wants the honest "there
    is no edge in reach" and :func:`_next_reset_edge` wants the cap instead, and
    those are two ways of *reporting* the same scan — running it twice invites
    the two answers to disagree about the same expression.

    Each entry gets its own step and horizon from :func:`_reset_scan`, for the
    same reason :func:`prev_reset_edge` does: a ``0 0 * * *`` neighbour must not
    shorten a ``0 0 1 * *``'s horizon from 31 days to 7 and hide its edge.
    """
    out: list[tuple[int | None, int]] = []
    for entry in reset_sched:
        parsed = parse_cron(entry.cron, entry.tz)
        unit, cap = _reset_scan(parsed)
        out.append((_next_rising_edge(parsed, now_ms, unit, cap), cap))
    return out


def next_reset_edge(reset_sched: tuple[ScheduleEntry, ...], *, now_ms: int) -> int | None:
    """The first reset edge strictly after ``now_ms``, or None within the cap.

    The forward twin of :func:`prev_reset_edge`: same adaptive step, same
    horizon, same "no edge within the cap means the expression never matches"
    reading (§3.6 names ``0 0 30 2 *``; cronsim rejects February 30th outright,
    so the constructible equivalent is ``0 0 29 2 *``).

    Since #574 the horizon is the entry's own cycle (:func:`_reset_scan`), so
    None here means what it says rather than "coarser than seven days": every
    practical quota period — session, daily, weekly, monthly, quarterly, annual
    — reports a real edge from every instant in its cycle. That is what makes
    ``resets_at_ms`` in a 429 body (#545) worth reading.

    The **minimum** across entries, where ``prev_reset_edge`` takes the maximum,
    and both for the same reason: reset entries are independent instants, so
    looking backwards the latest one subsumes every earlier one, and looking
    forwards the earliest one is the first that will fire.

    Unlike :func:`_next_reset_edge` this reports None rather than ``now_ms +
    cap`` when nothing is in reach. The cap is the right answer for a ``vu``
    stamp, which must force a re-materialisation per horizon; it is the wrong
    answer for a *wait*, which would then quote a countdown to an instant at
    which nothing happens.

    ``now_ms`` is keyword-only for the same reason :func:`next_boundary`'s is
    (#500): the second positional slot is a schedule tuple everywhere else in
    this module, and a positional timestamp would bind to it silently.
    """
    edges = [edge for edge, _cap in _forward_reset_scan(reset_sched, now_ms) if edge is not None]
    return min(edges) if edges else None


def _next_reset_edge(reset_sched: tuple[ScheduleEntry, ...], now_ms: int) -> int:
    """The earliest reset edge after ``now_ms``, or ``now_ms + cap`` if none is in reach.

    The cap rather than None, matching ``_next_param_change``, and for a sharper
    reason here: reporting None would leave ``vu`` unset and the fast path
    spending pre-reset tokens indefinitely, because nothing else would ever
    demote the bucket to the pass that runs the *backwards* scan. Capping forces
    one materialisation per horizon, and the backwards scan then finds the edge
    from the far side.

    Since #574 this is a rare fallback rather than the everyday answer for a
    coarse quota — a yearly `0 0 1 1 *` now sees its own edge — and the cap it
    falls back to can never hide one: an entry reports None exactly when its
    real edge is *past* the cap, so ``vu`` still lands at or before that edge
    and the materialising pass that follows re-scans from closer in.
    """
    return min(
        now_ms + cap if edge is None else edge
        for edge, cap in _forward_reset_scan(reset_sched, now_ms)
    )


def next_boundary(
    sched: tuple[ScheduleEntry, ...],
    reset_sched: tuple[ScheduleEntry, ...] = (),
    *,
    now_ms: int,
) -> int | None:
    """The earliest instant after ``now_ms`` at which this bucket must re-materialise.

    That is the earlier of two unrelated events: the active ``schedule`` entry
    changing, which changes the effective parameters (§1.2), and a
    ``reset_schedule`` edge firing, which sets the balance back to the effective
    capacity (§3.6). Both invalidate a ``vu`` stamp, so ``vu`` is their minimum.

    ``now_ms`` is **keyword-only on purpose**: the second positional slot belongs
    to ``reset_sched``, so a positional call would silently bind a timestamp to a
    schedule tuple (#500). It looks like ceremony now that both tuples are real
    arguments; it is not. Do not "simplify" it.

    Returns None only when *neither* tuple has entries. A reset-only limit still
    produces boundaries, which is what makes a quota (ADR-137: no drip, reset
    only) materialise at all.
    """
    candidates = []
    if sched:
        candidates.append(_next_param_change(sched, now_ms))
    if reset_sched:
        candidates.append(_next_reset_edge(reset_sched, now_ms))
    return min(candidates) if candidates else None


def retry_after_with_schedule(
    deficit_milli: int,
    cp_milli: int,
    ra_milli: int,
    rp_ms: int,
    sched: tuple[ScheduleEntry, ...],
    reset_sched: tuple[ScheduleEntry, ...] = (),
    *,
    now_ms: int,
    shard_count: int = 1,
    max_windows: int = 8,
) -> float:
    """Seconds until ``deficit_milli`` clears, walking across boundaries (§7).

    The flat estimate divides the deficit by the rate in force *now*. That is
    wrong in both directions, and worse in the one that matters: it
    over-reports when a boundary raises the limit and **under**-reports when one
    lowers it, which is the headline use case. The spec's worked example — empty
    bucket, 500 tokens needed, 1000/min now, a boundary in 10 s dropping to
    500/min — is 30.001 s flat against 50.001 s real (10 s yielding 166_666
    millitokens, then 333_334 remaining at half rate).

    ``cp_milli``/``ra_milli``/``rp_ms`` are the **undivided base** — the values
    stored on the item, which scheduling never rewrites (§2.1) — and the shard
    share is taken *after* ``effective_params``, per the scale-then-divide rule.
    Handing in pre-divided numbers would apply the split twice.

    The walk steps window by window: at each one it asks how long the current
    effective rate needs, and whether a boundary or a reset edge arrives first.
    **A reset edge inside the window is the answer outright**, because it
    restores the whole balance in one lump. Under ADR-137 that is not a variant
    case — a limit drips *or* resets, so every limit carrying a
    ``reset_schedule`` has ``refill_amount == 0`` and the edge is the only
    finite answer there is. For a daily quota the choice is between reporting
    "retry now", wrong and repeatedly for as long as the quota stays exhausted,
    and reporting "at midnight".

    Capped at ``max_windows``, after which it falls back to the flat estimate
    rather than reporting a partial walk as a complete one. Eight is untouched
    by #574 and is not a second half of that fix: a quota's rate is zero in
    every window, so the zero-rate branch below returns the edge on **iteration
    one** as soon as ``next_reset_edge`` can see it. The 56-day reach an annual
    quota used to fall off was the product of the budget and the seven-day cap —
    each iteration advanced ``cursor`` by one cap, hunting for an edge the scan
    could not yet see. With the horizon fixed there is nothing to hunt, and the
    walk costs one iteration where it used to cost eight. Raising the budget
    instead would have papered over the horizon for a quota and done nothing at
    all for ``next_reset_edge``, whose ``resets_at_ms`` has no walk to rescue it.

    The fallback quotes
    the **base** rate rather than the window's: past the cap the walk has no
    view of the schedule at all, and the momentary rate of whichever window the
    caller happened to ask in is a worse guess than the nominal one — a 0.001x
    window would quote a seven-day countdown that the next minute contradicts.

    Returns the identical value ``bucket.calculate_retry_after`` does when
    neither tuple is set, so the unscheduled path is unchanged to the
    millisecond.

    Args:
        deficit_milli: How many millitokens the request is short.
        cp_milli: Undivided base capacity, in millitokens.
        ra_milli: Undivided base refill amount, in millitokens.
        rp_ms: Base refill period, in milliseconds.
        sched: The parameter schedule, applied by ``effective_params``.
        reset_sched: The reset schedule, whose next edge dominates.
        now_ms: The clock reading the returned wait is measured from.
        shard_count: Shares the effective rate, after scaling (GHSA-76rv).
        max_windows: Walk budget; beyond it, the flat estimate.

    Returns:
        Seconds until the deficit clears, or 0.0 when there is no deficit and
        when neither a rate nor an edge can produce a finite wait.
    """
    if deficit_milli <= 0:
        return 0.0

    divisor = max(1, shard_count)

    def _rate(ra: int) -> int:
        # A share that floors to zero has no finite wait; fall back to the
        # undivided *scheduled* rate, exactly as
        # `BucketState.retry_refill_amount_milli` does (#475). Falling back to
        # the base rate would quote a speed nothing in the system refills at
        # during the window.
        return (ra // divisor) or ra

    remaining = deficit_milli
    cursor = now_ms
    for _ in range(max_windows):
        _eff_cp, eff_ra, eff_rp = effective_params(cp_milli, ra_milli, rp_ms, sched, cursor)
        rate = _rate(eff_ra)
        edge = next_reset_edge(reset_sched, now_ms=cursor)
        boundary = next_boundary(sched, reset_sched, now_ms=cursor)

        if rate <= 0:
            # The edge is consulted BEFORE the rate gate (#530). Under ADR-137
            # a quota's rate is zero in *every* window, so gating on the rate
            # first exits on iteration 1 and falls back to a flat estimate of
            # 0.0 — "retry immediately", forever, which is the precise opposite
            # of this function's headline and silent with it. Nothing accrues
            # in this window, so the edge wins if it lands inside it; otherwise
            # step to the boundary, where the rate may resume.
            if edge is not None and (boundary is None or edge <= boundary):
                return (edge - now_ms + 1) / 1000.0
            if boundary is None:
                break  # no rate, no edge, no boundary — no finite wait
            cursor = boundary  # strictly after `cursor`, so this advances
            continue

        need_ms = (remaining * eff_rp) // rate
        window_end = boundary if boundary is not None else cursor + need_ms

        # The edge still wins over a positive rate when it lands first. A quota
        # inside a `scale` window no longer reaches here with a phantom
        # 1-millitoken drip — `effective_params` conditions that floor on the
        # base rate since #556 — but a limit that genuinely drips *and* resets
        # does, and for it the edge can still land before the deficit clears.
        # The walk must not depend on that floor being either present or absent.
        if edge is not None and edge <= min(window_end, cursor + need_ms):
            return (edge - now_ms + 1) / 1000.0
        if cursor + need_ms <= window_end:
            return (cursor + need_ms - now_ms + 1) / 1000.0

        remaining -= ((window_end - cursor) * rate) // eff_rp
        cursor = window_end

    # Identical arithmetic to `bucket.calculate_retry_after`, inlined because
    # importing it here is a cycle (`bucket` -> `models` -> `schedule`);
    # `test_unscheduled_matches_calculate_retry_after_exactly` is what keeps the
    # two the same to the millisecond. The zero-rate branch is carried across
    # too (#530): the cap is exactly where a quota that outran the walk lands,
    # and a copy that stopped at the rate arithmetic would reintroduce the 0.0
    # this function exists to remove. The edge is recomputed from `now_ms`, not
    # from the walk's `cursor`, because the value returned is a wait measured
    # from the caller's instant.
    flat_rate = _rate(ra_milli)
    if flat_rate <= 0:
        fallback_edge = next_reset_edge(reset_sched, now_ms=now_ms)
        if fallback_edge is None:
            return 0.0
        return (fallback_edge - now_ms + 1) / 1000.0
    return ((deficit_milli * rp_ms) // flat_rate + 1) / 1000.0


# ---------------------------------------------------------------------------
# Compact storage encoding (§4.1)
#
# Standard 5-field cron is the interface at every boundary of the system — API,
# YAML, CloudFormation, CLI display, audit events. The compact form below exists
# only in storage, because DynamoDB bills a WCU per 1 KB and a bucket item that
# crosses 1 KB doubles the write cost of every acquire on it forever.
#
# Wildcard fields are omitted, the rest are letter-tagged `m h D M w`, names are
# normalised to numbers, `scale` is an integer per-mille tagged `s`, the absolute
# overrides are `c`/`a`/`p`, and entries are joined with `;`. The timezone is
# hoisted to a single item-level attribute, so every entry in one schedule must
# agree on it. Example: `h9-17w1-5s500;h0-6c2000`.
#
# Decoding rebuilds a canonical 5-field cron string and hands it to
# `ScheduleEntry`, so cronsim remains the only parser and the §3.1 oracle test
# covers this path unchanged.
# ---------------------------------------------------------------------------

# Field tags, in cron field order.
_FIELD_TAGS = ("m", "h", "D", "M", "w")

# Modifier tags, in the order `encode` emits them.
_MODIFIER_TAGS = ("s", "c", "a", "p")

_ALL_TAGS = _FIELD_TAGS + _MODIFIER_TAGS

# A field spec contains only digits, `-`, `,`, `/` and `*`; a modifier value only
# digits. None of those collide with a tag letter, so the stream is unambiguous.
_TOKEN_RE = re.compile(rf"([{''.join(_ALL_TAGS)}])([^{''.join(_ALL_TAGS)}]+)")

_DOW_NAMES = {
    0: "SUN",
    1: "MON",
    2: "TUE",
    3: "WED",
    4: "THU",
    5: "FRI",
    6: "SAT",
    7: "SUN",
}
_MONTH_NAMES = {
    1: "JAN",
    2: "FEB",
    3: "MAR",
    4: "APR",
    5: "MAY",
    6: "JUN",
    7: "JUL",
    8: "AUG",
    9: "SEP",
    10: "OCT",
    11: "NOV",
    12: "DEC",
}

# Inverse maps for encoding. `7` wins for SUN because `_DOW_NAMES` lists it last,
# which is `datetime.isoweekday()`'s spelling and the one `parse_cron` normalises
# to — but see `_encode_dow_item` for the range case, where it must be 0.
_DOW_NUMBERS = {name: number for number, name in _DOW_NAMES.items()}
_MONTH_NUMBERS = {name: number for number, name in _MONTH_NAMES.items()}

_NAME_RE = re.compile(r"[A-Za-z]+")


def _name_to_number(spec: str, table: dict[str, int]) -> str:
    """Replace every alphabetic run in ``spec`` with its number."""

    def repl(m: re.Match[str]) -> str:
        name = m.group(0).upper()
        if name not in table:
            # Unreachable through `encode`: cronsim accepts exactly these three-letter
            # abbreviations and `ScheduleEntry.__post_init__` has already run. Kept so a
            # widened cronsim cannot silently store text this module does not understand.
            raise ValueError(  # pragma: no cover
                f"unknown name {m.group(0)!r} in cron field {spec!r}"
            )
        return str(table[name])

    return _NAME_RE.sub(repl, spec)


def _encode_dow_item(item: str) -> str:
    """Encode one comma-separated day-of-week item, resolving SUN's two numbers.

    cron spells Sunday both 0 and 7, and which one is correct depends on where it
    sits. Standalone, 7 is right: it is ``isoweekday()``'s spelling, the one
    ``parse_cron`` folds 0 into, and it keeps ``SAT,SUN`` ascending as ``6,7``.
    Inside a range or a step it must be 0 — ``SUN-THU`` is ``0-4`` (``7-4`` is a
    backwards range cronsim rejects) and ``SUN/2`` is ``0/2`` (``7/2`` is just
    ``{7}``). cronsim already rejects every range that *ends* at Sunday
    (``SAT-SUN``, ``MON-SUN``), so the only such item that reaches us is
    ``SUN-SUN``, and ``0-0`` is right there too.

    A bare ``0`` is folded to ``7`` for the same reason the names are normalised:
    so `differ.py` does not read ``0`` versus ``SUN`` versus ``7`` as a change.
    """
    bare = item.upper()
    if bare in _DOW_NUMBERS:
        return str(_DOW_NUMBERS[bare])
    if bare == "0":
        return "7"
    return _name_to_number(item, {**_DOW_NUMBERS, "SUN": 0})


def _encode_field(tag: str, spec: str) -> str:
    """Normalise one cron field to its stored spelling (names -> numbers)."""
    if tag == "w":
        return ",".join(_encode_dow_item(item) for item in spec.split(","))
    if tag == "M":
        return _name_to_number(spec, _MONTH_NUMBERS)
    return spec


def _encode_cron(cron: str) -> str:
    """Encode the five cron fields, omitting every wildcard."""
    fields = cron.split()
    if len(fields) != 5:  # pragma: no cover - parse_cron rejects this first
        raise ValueError(f"expected a 5-field cron expression, got {cron!r}")
    out = []
    for tag, spec in zip(_FIELD_TAGS, fields, strict=True):
        encoded = _encode_field(tag, spec)
        if encoded == "*":
            continue
        out.append(f"{tag}{encoded}")
    return "".join(out)


def _encode_entry(entry: ScheduleEntry) -> str:
    out = _encode_cron(entry.cron)
    if entry.scale is not None:
        # Per-mille, rounded rather than truncated: `int(2.3 * 1000)` is 2299,
        # because 2.3 has no exact binary representation. Sub-per-mille scales
        # floor at 1, mirroring `effective_params`' floor at one milli-unit, so
        # that `encode` is total and never emits an entry `decode` would reject.
        out += f"s{max(1, round(entry.scale * 1000))}"
    for tag, value in (
        ("c", entry.capacity),
        ("a", entry.refill_amount),
        ("p", entry.refill_period_seconds),
    ):
        if value is not None:
            out += f"{tag}{value}"
    return out


def encode(sched: tuple[ScheduleEntry, ...]) -> tuple[str, str | None]:
    """Encode a schedule into its compact storage form and its shared timezone.

    Returns ``("", None)`` for an empty schedule. Raises ``ValueError`` if the
    entries disagree on ``tz``: it is hoisted to one item-level attribute, so a
    schedule cannot carry two.

    The encoding is **canonical** — weekday and month names normalise to numbers
    and Sunday to a single spelling — so re-encoding a decoded schedule is
    byte-identical, and `differ.py` does not read ``MON-FRI`` against ``1-5`` as
    a change on every apply. The one lossy dimension is ``scale``, which is
    quantised to per-mille.
    """
    if not sched:
        return "", None
    timezones = {entry.tz for entry in sched}
    if len(timezones) > 1:
        raise ValueError(
            f"every entry in a schedule must share one timezone, since it is hoisted "
            f"to a single item-level attribute; got {sorted(timezones)}"
        )
    return ";".join(_encode_entry(entry) for entry in sched), sched[0].tz


def _tokenise(compact_entry: str) -> dict[str, str]:
    """Split one compact entry into ``{tag: value}``, rejecting anything else."""
    tokens: dict[str, str] = {}
    consumed = 0
    for match in _TOKEN_RE.finditer(compact_entry):
        if match.start() != consumed:
            break
        tag, value = match.group(1), match.group(2)
        if tag in tokens:
            raise ValueError(f"duplicate {tag!r} tag in compact schedule entry {compact_entry!r}")
        tokens[tag] = value
        consumed = match.end()
    if consumed != len(compact_entry):
        raise ValueError(
            f"malformed compact schedule entry {compact_entry!r}: "
            f"cannot parse from offset {consumed}"
        )
    return tokens


def _cron_from_tokens(tokens: dict[str, str]) -> str:
    return " ".join(tokens.get(tag, "*") for tag in _FIELD_TAGS)


def decode(compact: str, tz: str) -> tuple[ScheduleEntry, ...]:
    """Decode the compact storage form back into schedule entries.

    ``tz`` is the hoisted item-level timezone and is applied to every entry.
    Each entry is rebuilt as a canonical 5-field cron string and handed to
    ``ScheduleEntry``, so cronsim stays the only cron parser in the codebase.
    """
    if not compact:
        return ()
    entries = []
    for part in compact.split(";"):
        tokens = _tokenise(part)
        scale = int(tokens["s"]) / 1000 if "s" in tokens else None
        entries.append(
            ScheduleEntry(
                cron=_cron_from_tokens(tokens),
                tz=tz,
                scale=scale,
                capacity=int(tokens["c"]) if "c" in tokens else None,
                refill_amount=int(tokens["a"]) if "a" in tokens else None,
                refill_period_seconds=int(tokens["p"]) if "p" in tokens else None,
            )
        )
    return tuple(entries)


def encode_reset(sched: tuple[ScheduleEntry, ...]) -> tuple[str, str | None]:
    """Encode a reset schedule into its compact storage form and shared timezone.

    The same grammar as ``encode`` **minus the modifier tokens**, because a
    reset entry overrides no parameters (§3.6): ``0 0 * * *`` is ``m0h0``, four
    bytes. Reset entries live in their own ``rsched`` / ``b_{name}_rsched``
    attributes rather than tagged inside ``sched``, which mirrors the separate
    tuple on ``Limit`` and keeps the decoder from partitioning one list into
    two meanings (§4.1).

    Returns ``("", None)`` for an empty schedule. Raises ``ValueError`` if the
    entries disagree on ``tz``: it is hoisted to one item-level attribute —
    shared with the parameter schedule — so a limit cannot carry two.

    Canonical in exactly the same way ``encode`` is, and losslessly so: a reset
    entry carries no ``scale``, which is ``encode``'s one quantised dimension,
    so the reset round trip is exact rather than merely semantic.
    """
    if not sched:
        return "", None
    timezones = {entry.tz for entry in sched}
    if len(timezones) > 1:
        raise ValueError(
            f"every entry in a reset schedule must share one timezone, since it is "
            f"hoisted to a single item-level attribute; got {sorted(timezones)}"
        )
    return ";".join(_encode_cron(entry.cron) for entry in sched), sched[0].tz


def decode_reset(compact: str, tz: str) -> tuple[ScheduleEntry, ...]:
    """Decode the compact reset form back into schedule entries.

    ``tz`` is the hoisted item-level timezone and is applied to every entry.
    Entries are built through :meth:`ScheduleEntry.reset`, never
    ``ScheduleEntry(...)``: a reset entry carries no modifier and the ordinary
    constructor requires exactly one.

    A modifier tag in this attribute is **rejected rather than ignored**. It
    means either corruption or a parameter schedule stored under the wrong key,
    and an entry that silently reset the balance on a schedule meant only to
    scale it would be the worst available reading.
    """
    if not compact:
        return ()
    entries = []
    for part in compact.split(";"):
        tokens = _tokenise(part)
        modifiers = sorted(set(tokens) & set(_MODIFIER_TAGS))
        if modifiers:
            raise ValueError(
                f"reset schedule entry {part!r} carries the modifier token(s) "
                f"{modifiers}; a reset overrides no parameters"
            )
        entries.append(ScheduleEntry.reset(cron=_cron_from_tokens(tokens), tz=tz))
    return tuple(entries)


def _render_named(spec: str, table: dict[int, str]) -> str:
    """Render one field's numbers as names, leaving ``/step`` divisors alone.

    The step of ``1-5/2`` is a divisor, not a weekday, so only the part before
    the slash is renamed. A number with no name is left as-is: this is a display
    helper and must not raise on a corrupt attribute.
    """
    out = []
    for item in spec.split(","):
        base, slash, step = item.partition("/")
        renamed = re.sub(r"\d+", lambda m: table.get(int(m.group(0)), m.group(0)), base)
        out.append(renamed + slash + step)
    return ",".join(out)


def to_cron(compact_entry: str) -> str:
    """Render one compact entry as a canonical 5-field cron string, for display.

    Display only — nothing in the evaluation or storage paths goes through this.
    Weekday and month always come back as **names**, so an operator who typed
    ``1-5`` is shown ``MON-FRI``. That is semantically identical and re-encodes
    byte-for-byte, so it is safe to feed the result back into ``encode``.
    """
    tokens = _tokenise(compact_entry)
    fields = []
    for tag in _FIELD_TAGS:
        spec = tokens.get(tag, "*")
        if tag == "w":
            spec = _render_named(spec, _DOW_NAMES)
        elif tag == "M":
            spec = _render_named(spec, _MONTH_NAMES)
        fields.append(spec)
    return " ".join(fields)
