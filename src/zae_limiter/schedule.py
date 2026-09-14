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

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cronsim import CronSim, CronSimError

__all__ = [
    "ParsedCron",
    "ScheduleEntry",
    "effective_params",
    "matches",
    "next_boundary",
    "parse_cron",
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
    # Nothing here is finer than a minute — `matches` compares minute/hour/day and
    # `next_boundary` steps by the minute — so a seconds field would be silently
    # widened to its whole minute. Reject it up front.
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
    """

    cron: str
    tz: str = "UTC"
    scale: float | None = None
    capacity: int | None = None
    refill_amount: int | None = None
    refill_period_seconds: int | None = None

    def __post_init__(self) -> None:
        parse_cron(self.cron, self.tz)  # raises ValueError on anything unusable

        absolutes = (self.capacity, self.refill_amount, self.refill_period_seconds)
        has_absolute = any(v is not None for v in absolutes)
        if (self.scale is not None) == has_absolute:
            raise ValueError(
                "a schedule entry must set exactly one of `scale` or the absolute "
                "fields (`capacity`/`refill_amount`/`refill_period_seconds`)"
            )
        if self.scale is not None and self.scale <= 0:
            raise ValueError(f"scale must be positive, got {self.scale}")
        for name, value in (
            ("capacity", self.capacity),
            ("refill_amount", self.refill_amount),
            ("refill_period_seconds", self.refill_period_seconds),
        ):
            if value is not None and value <= 0:
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
        if not matches(parse_cron(entry.cron, entry.tz), now_ms):
            continue
        if entry.scale is not None:
            # Scale capacity and refill together so time-to-fill is preserved
            # (§1.1). Truncate rather than round, so a scaled limit is never
            # larger than asked for; floor at 1 milli-unit, since a zero
            # capacity is unadmittable.
            return (
                max(1, int(cp_milli * entry.scale)),
                max(1, int(ra_milli * entry.scale)),
                rp_ms,
            )
        return (
            entry.capacity * 1000 if entry.capacity is not None else cp_milli,
            entry.refill_amount * 1000 if entry.refill_amount is not None else ra_milli,
            entry.refill_period_seconds * 1000
            if entry.refill_period_seconds is not None
            else rp_ms,
        )
    return cp_milli, ra_milli, rp_ms


# Step size and scan horizon, chosen by the finest field any entry constrains.
_MINUTE_MS = 60_000
_HOUR_MS = 3_600_000
_DAY_MS = 86_400_000
_GRANULARITY = {
    "minute": (_MINUTE_MS, 7 * _DAY_MS),
    "hour": (_HOUR_MS, 31 * _DAY_MS),
    "day": (_DAY_MS, 366 * _DAY_MS),
}


def _granularity(parsed: tuple[ParsedCron, ...]) -> tuple[int, int]:
    """Step and cap for a scan, from the finest field any entry constrains."""
    if any(len(p.minutes) < 60 for p in parsed):
        return _GRANULARITY["minute"]
    if any(len(p.hours) < 24 for p in parsed):
        return _GRANULARITY["hour"]
    return _GRANULARITY["day"]


def _active_index(parsed: tuple[ParsedCron, ...], now_ms: int) -> int | None:
    """Index of the first matching entry, or None. First match wins (§1.2)."""
    for i, p in enumerate(parsed):
        if matches(p, now_ms):
            return i
    return None


def next_boundary(
    sched: tuple[ScheduleEntry, ...],
    reset_sched: tuple[ScheduleEntry, ...] = (),
    *,
    now_ms: int,
) -> int | None:
    """The earliest instant after ``now_ms`` where the active entry changes.

    ``reset_sched`` is accepted but unused until the surface plan folds reset
    edges in as boundary candidates; taking it now keeps that a one-function
    change rather than a signature change across every caller. ``now_ms`` is
    **keyword-only on purpose**: the second positional slot belongs to
    ``reset_sched``, so a positional call would silently bind a timestamp to a
    schedule tuple (#500). Do not "simplify" it.

    Returns None when there is no schedule. Returns ``now_ms + cap`` when no
    transition is found within the horizon, which forces one cheap
    re-materialisation per active bucket per cap period rather than looping.

    This is a scan, not a library call, because the boundary set includes
    window *closings* and no cron library computes those (§3.2).

    The scan is two-phase. The coarse pass steps by the finest field any entry
    constrains, over a grid aligned to the **UTC** epoch — but window edges fall
    on *local* minutes, and a timezone offset need not be a whole number of
    steps, so a coarse probe is only an upper bound on the boundary. Asia/Kolkata
    (+05:30) puts a 09:00 local edge at 03:30Z, half a step off an hourly grid;
    a day-granularity edge at New York midnight is twenty hours off a UTC-midnight
    grid. A returned boundary that is *late* is the unsafe direction — ``vu``
    would keep the fast path on the old limits well inside the new window — so
    the step that straddles the change is re-walked at minute resolution. That
    costs at most 59 (hourly) or 1439 (daily) extra matches, once, and only on
    the step where the change actually happens.
    """
    if not sched:
        return None

    parsed = tuple(parse_cron(e.cron, e.tz) for e in sched)
    step, cap = _granularity(parsed)
    current = _active_index(parsed, now_ms)
    horizon = now_ms + cap

    # Align the coarse probes to the step grid. Cosmetic rather than load-bearing:
    # any grid of spacing `step` straddles the change (every window is at least one
    # local hour or one local day long), and the refinement below pins the exact
    # minute either way. It just keeps the probed instants tidy.
    lo = now_ms
    probe = (now_ms // step + 1) * step
    while probe <= horizon:
        if _active_index(parsed, probe) != current:
            # Refine: the true edge lies in (lo, probe]. `probe` itself is a
            # multiple of `step` and therefore minute-aligned, so this loop is
            # bounded and `probe` is always a valid answer if nothing earlier is.
            fine = (lo // _MINUTE_MS + 1) * _MINUTE_MS
            while fine < probe:
                if _active_index(parsed, fine) != current:
                    return fine
                fine += _MINUTE_MS
            return probe
        lo = probe
        probe += step
    return now_ms + cap
