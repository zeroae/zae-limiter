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
    "matches",
    "next_boundary",
    "parse_cron",
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
    if not sched:
        return None

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
