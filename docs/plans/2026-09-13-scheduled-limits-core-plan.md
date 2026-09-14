# Scheduled Limits: Evaluation Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make time-varying rate limits work through the Python API — a `Limit` carrying a cron schedule is enforced correctly by the fast path, the slow path, and the aggregator.

**Architecture:** The bucket item keeps its undivided base `cp`/`ra`/`rp` forever; every refiller computes `effective = f(base, sched, now)` at read time and discards it. The only materialised quantity is `tk`. One new item attribute, `vu` (valid-until, epoch ms), joins the fast path's conditional `UpdateItem` so an expired schedule window drops that request to the slow path, which re-materialises. The fast path never evaluates a schedule and never reads config.

**Tech Stack:** Python 3.11/3.12, `cronsim` (parsing, zero transitive deps), stdlib `zoneinfo` + `tzdata` (timezones), `croniter` (dev-only test oracle), aioboto3/boto3, pytest, LocalStack.

**Spec:** `docs/plans/2026-09-13-scheduled-limits-design.md` §§1–4 (this plan implements §§2–3 and §4's encoding; §5–7 are the surface plan).

## Global Constraints

- **The fast path must never read config and never evaluate a schedule.** It gains exactly one comparison. This is the load-bearing claim of the whole design and Task 13 asserts it with the `capacity_counter` fixture.
- **Base `cp`/`ra`/`rp` on bucket items are never rewritten by scheduling.** They stay the undivided base. Only `tk`, `rf` and `vu` move. Anything that writes an effective capacity onto an item is a bug.
- **Order of operations is scale-then-shard-divide.** `effective_params()` first, `// shard_count` second.
- **Native sync code is generated, never hand-edited.** After touching `repository.py`, `limiter.py`, `lease.py`, `config_cache.py`, `repository_protocol.py` or `infra/{stack_manager,discovery}.py`, run `hatch run generate-sync` and commit the regenerated files. The pre-commit hook and CI both verify this. The generated files are listed in `CLAUDE.md`.
- **`asyncio.gather(a, b)` is transformed into `self._run_in_executor(...)` by the sync transformer.** A list comprehension — not a generator expression — is required inside `gather`.
- **Do not disable or suppress lint rules** (ruff, mypy, cfn-lint) without asking. See `.claude/rules/lint-rules.md`.
- **Never run `pytest tests/unit/` with `-o "addopts="`** — it un-skips the gevent tests, which then monkey-patch the same process as the asyncio tests and hang indefinitely with no output. Run `uv run pytest tests/unit/ -q` (~3 min) and `uv run pytest tests/unit/ -m gevent -n 0 -q` (~2 s) separately. See `.claude/rules/testing.md`.
- **Millitokens everywhere below the model layer.** `Limit` is whole tokens; `BucketState` and every `bucket.py` function are millitokens and milliseconds.
- Commit messages follow `.claude/rules/commits.md`. Scopes used here: `models`, `bucket`, `schema`, `repository`, `aggregator`, `infra`, `test`.
- Feature branch off `main`; PRs opened with the `/pr` skill, never `gh pr create` (`.claude/rules/issue-skill.md`).

---

### Task 1: Dependencies and `ScheduleEntry`

**Files:**
- Create: `src/zae_limiter/schedule.py`
- Modify: `pyproject.toml`
- Test: `tests/unit/test_schedule.py`

**Interfaces:**
- Consumes: `cronsim.CronSim`, `cronsim.CronSimError`, `zoneinfo.ZoneInfo`
- Produces:
  - `ScheduleEntry(cron: str, tz: str = "UTC", scale: float | None = None, capacity: int | None = None, refill_amount: int | None = None, refill_period_seconds: int | None = None)` — frozen dataclass, validating in `__post_init__`
  - `ParsedCron` — frozen dataclass with `minutes/hours/days/months/weekdays: frozenset[int]`, `day_and: bool`, `tz: ZoneInfo`
  - `parse_cron(cron: str, tz: str) -> ParsedCron`

**The three traps** (all confirmed against cronsim during design — do not skip any):
1. cronsim maps `SUN` and `0` to `{0}` but leaves `7` as `{7}`. `datetime.isoweekday()` gives Mon=1…Sun=7.
2. `L`, `LW`, `FRI#2`, `5L` **parse without error** and inject `-1000`, `-1001` or tuples like `(5, 2)` into the sets. A naive `day in days` match then silently never fires. These must be rejected.
3. When both day-of-month and weekday are constrained, cron means **or**, not **and**. cronsim computes this as `day_and`.

- [ ] **Step 1: Add dependencies**

In `pyproject.toml`, add `"cronsim>=2.7"` and `"tzdata"` to the main `dependencies` list, add `"cronsim>=2.7"` and `"tzdata"` to the `[lambda]` extra, and add `"croniter>=6.0"` to the `[dev]` extra.

`tzdata` (339 KB) is required because the Lambda runtime image may not ship `/usr/share/zoneinfo`. `croniter` is **dev-only** — it must never appear in a runtime or Lambda dependency list.

Run: `uv sync --all-extras`

- [ ] **Step 2: Write the failing test**

```python
"""Tests for schedule parsing and validation (#222 §3.1)."""

import pytest

from zae_limiter.schedule import ScheduleEntry, parse_cron


class TestParseCron:
    def test_expands_fields_to_int_sets(self):
        p = parse_cron("* 9-17 * * MON-FRI", "America/New_York")
        assert p.hours == frozenset(range(9, 18))
        assert p.weekdays == frozenset({1, 2, 3, 4, 5})
        assert p.day_and is True

    def test_dom_and_dow_both_constrained_means_or(self):
        """`* * 13 * FRI` matches the 13th OR any Friday — the classic cron trap."""
        assert parse_cron("* * 13 * FRI", "UTC").day_and is False
        assert parse_cron("* * 13 * *", "UTC").day_and is True
        assert parse_cron("* * * * FRI", "UTC").day_and is True

    @pytest.mark.parametrize("expr", ["* * * * SUN", "* * * * 0", "* * * * 7"])
    def test_sunday_normalised_to_seven(self, expr):
        """cronsim gives SUN/0 -> {0} and 7 -> {7}; we normalise to isoweekday's 7."""
        assert 7 in parse_cron(expr, "UTC").weekdays

    @pytest.mark.parametrize("expr", ["* * L * *", "* * LW * *", "* * * * FRI#2", "* * * * 5L"])
    def test_rejects_extended_tokens(self, expr):
        """These parse without error and poison the sets with sentinels."""
        with pytest.raises(ValueError, match="not supported"):
            parse_cron(expr, "UTC")

    @pytest.mark.parametrize("expr", ["* 9-17 * *", "bogus * * * *", "* 99 * * *", "60 * * * *"])
    def test_rejects_malformed(self, expr):
        with pytest.raises(ValueError):
            parse_cron(expr, "UTC")

    def test_rejects_unknown_timezone(self):
        with pytest.raises(ValueError, match="timezone"):
            parse_cron("* * * * *", "Mars/Olympus_Mons")


class TestScheduleEntry:
    def test_scale_entry(self):
        e = ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5)
        assert e.scale == 0.5

    def test_absolute_entry(self):
        assert ScheduleEntry(cron="* 0-6 * * *", capacity=2000).capacity == 2000

    def test_rejects_both_kinds(self):
        with pytest.raises(ValueError, match="exactly one"):
            ScheduleEntry(cron="* * * * *", scale=0.5, capacity=100)

    def test_rejects_neither_kind(self):
        with pytest.raises(ValueError, match="exactly one"):
            ScheduleEntry(cron="* * * * *")

    @pytest.mark.parametrize("scale", [0, -1, -0.5])
    def test_rejects_non_positive_scale(self, scale):
        with pytest.raises(ValueError, match="scale"):
            ScheduleEntry(cron="* * * * *", scale=scale)

    @pytest.mark.parametrize("kwargs", [
        {"capacity": 0}, {"capacity": -5},
        {"capacity": 10, "refill_amount": 0},
        {"capacity": 10, "refill_period_seconds": 0},
    ])
    def test_rejects_non_positive_absolutes(self, kwargs):
        with pytest.raises(ValueError):
            ScheduleEntry(cron="* * * * *", **kwargs)

    def test_is_frozen_and_hashable(self):
        e = ScheduleEntry(cron="* * * * *", scale=0.5)
        with pytest.raises(Exception):
            e.cron = "x"  # type: ignore[misc]
        assert hash(e)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_schedule.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'zae_limiter.schedule'`

- [ ] **Step 4: Write minimal implementation**

```python
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

from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cronsim import CronSim, CronSimError

__all__ = ["ParsedCron", "ScheduleEntry", "parse_cron"]

# cronsim's sentinels for the extended tokens we do not support.
_SENTINELS = {CronSim.LAST, CronSim.LAST_WEEKDAY}


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


def _reject_extended(values: frozenset[object], field: str) -> None:
    for v in values:
        if not isinstance(v, int) or v in _SENTINELS:
            raise ValueError(
                f"cron {field} field uses an extended token (L, W, or #), which is "
                f"not supported. These parse without error but would silently never "
                f"match, so they are rejected rather than accepted and ignored."
            )


def parse_cron(cron: str, tz: str) -> ParsedCron:
    """Parse a 5-field cron expression and timezone into matchable field sets."""
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone {tz!r}: {exc}") from exc

    try:
        # cronsim needs a reference datetime to construct; we only want the
        # parsed field sets, so any fixed instant will do.
        import datetime as _dt

        parsed = CronSim(cron, _dt.datetime(2000, 1, 1, tzinfo=zone))
    except CronSimError as exc:
        raise ValueError(f"invalid cron expression {cron!r}: {exc}") from exc

    days = frozenset(parsed.days)
    weekdays = frozenset(parsed.weekdays)
    _reject_extended(days, "day-of-month")
    _reject_extended(weekdays, "day-of-week")

    # isoweekday() is Mon=1..Sun=7. cronsim yields 0 for SUN/0 and 7 for 7,
    # so fold 0 into 7 and keep both spellings working.
    if 0 in weekdays:
        weekdays = frozenset(weekdays - {0}) | {7}

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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_schedule.py -v`
Expected: PASS

- [ ] **Step 6: Lint, type check, commit**

```bash
uv run ruff check --fix . && uv run ruff format . && uv run mypy
git add pyproject.toml uv.lock src/zae_limiter/schedule.py tests/unit/test_schedule.py
git commit -m "$(cat <<'EOF'
✨ feat(models): add ScheduleEntry and cron parsing

cronsim parses (zero transitive deps, gives us the dom/dow OR rule via
day_and); we own the match loop. Rejects L/W/# outright — they parse
without error and inject sentinels that make a schedule silently never
fire. Normalises Sunday, which cronsim leaves as 0 or 7 depending on
spelling.

croniter lands in [dev] only, as the oracle for the next task.

Refs #222
EOF
)"
```

---

### Task 2: The matcher, verified against croniter

**Files:**
- Modify: `src/zae_limiter/schedule.py`
- Test: `tests/unit/test_schedule_oracle.py`

**Interfaces:**
- Consumes: `ParsedCron`, `parse_cron` from Task 1
- Produces: `matches(parsed: ParsedCron, now_ms: int) -> bool`

**Scan in UTC, match in local.** `datetime.fromtimestamp(ts, tz)` only ever converts UTC→local, which is always well-defined — so the nonexistent local hour at spring-forward and the doubled hour at fall-back never arise. DST needs no special case: a `9-17 America/New_York` window is 9–17 local on both sides of a transition; only the window edge's UTC instant moves.

- [ ] **Step 1: Write the failing oracle test**

```python
"""Our matcher must agree with croniter across a year including both DST switches.

croniter is a dev-only dependency used purely as an oracle. Its own
`match()` re-parses the expression on every call (362 us measured), which is
why it is unusable in the scan itself — but that cost is irrelevant here.
"""

import random
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from croniter import croniter

from zae_limiter.schedule import matches, parse_cron

NY = ZoneInfo("America/New_York")

EXPRESSIONS = [
    "* 9-17 * * MON-FRI",
    "*/15 0-6,22-23 * * *",
    "* * 13 * FRI",
    "0 0 * * SUN",
    "0 0 * * 7",
    "30 2 * * *",
    "* * 1 JAN,JUL *",
    "*/5 9-17/2 1-7 * MON",
]


def _sample_instants():
    """Random instants across 2027 plus dense coverage of both DST switches."""
    random.seed(7)
    lo = int(datetime(2027, 1, 1, tzinfo=NY).timestamp())
    hi = int(datetime(2028, 1, 1, tzinfo=NY).timestamp())
    samples = [random.randrange(lo, hi, 60) for _ in range(1500)]
    for switch in (datetime(2027, 3, 14, tzinfo=NY), datetime(2027, 11, 7, tzinfo=NY)):
        base = int(switch.timestamp())
        samples += list(range(base, base + 3 * 86400, 60))[::7]
    return samples


@pytest.mark.parametrize("expr", EXPRESSIONS)
def test_agrees_with_croniter(expr):
    parsed = parse_cron(expr, "America/New_York")
    for ts in _sample_instants():
        dt = datetime.fromtimestamp(ts, NY)
        assert matches(parsed, ts * 1000) == croniter.match(expr, dt), (
            f"{expr} disagreed at {dt.isoformat()}"
        )


def test_dst_window_edge_holds_local_and_moves_in_utc():
    """9am New York stays 9am local across spring-forward; its UTC instant shifts."""
    parsed = parse_cron("* 9-17 * * MON-FRI", "America/New_York")
    before = int(datetime(2027, 3, 12, 9, 0, tzinfo=NY).timestamp())   # Fri, EST
    after = int(datetime(2027, 3, 16, 9, 0, tzinfo=NY).timestamp())    # Tue, EDT
    assert matches(parsed, before * 1000) and matches(parsed, after * 1000)
    assert datetime.utcfromtimestamp(before).hour == 14
    assert datetime.utcfromtimestamp(after).hour == 13


@pytest.mark.parametrize("day,expected_minutes", [
    ("2027-03-14", 1380),   # spring forward: a 23-hour day
    ("2026-11-01", 1500),   # fall back: a 25-hour day
    ("2026-09-15", 1440),   # ordinary
])
def test_no_minute_skipped_or_doubled(day, expected_minutes):
    start = int(datetime.fromisoformat(f"{day} 00:00").replace(tzinfo=NY).timestamp())
    end = int(datetime.fromisoformat(f"{day} 23:59").replace(tzinfo=NY).timestamp()) + 60
    assert (end - start) // 60 == expected_minutes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_schedule_oracle.py -v`
Expected: FAIL with `ImportError: cannot import name 'matches'`

- [ ] **Step 3: Write minimal implementation**

Append to `schedule.py`:

```python
from datetime import datetime


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_schedule_oracle.py -v`
Expected: PASS — 8 parametrised cases at ~2,700 instants each, ~22,000 comparisons

- [ ] **Step 5: Lint, type check, commit**

```bash
uv run ruff check --fix . && uv run ruff format . && uv run mypy
git add src/zae_limiter/schedule.py tests/unit/test_schedule_oracle.py
git commit -m "$(cat <<'EOF'
✨ feat(models): match cron patterns as windows, not fire times

Scans in UTC and converts to local, so ambiguous and nonexistent local
times never arise and DST needs no special case.

Verified against croniter as an oracle: ~22,000 comparisons across 8
expressions and both 2027 DST transitions.

Refs #222
EOF
)"
```

---

### Task 3: `effective_params`

**Files:**
- Modify: `src/zae_limiter/schedule.py`
- Test: `tests/unit/test_schedule.py`

**Interfaces:**
- Consumes: `matches`, `ScheduleEntry`, `parse_cron`
- Produces: `effective_params(cp_milli: int, ra_milli: int, rp_ms: int, sched: tuple[ScheduleEntry, ...], now_ms: int) -> tuple[int, int, int]`

**First matching entry wins.** No match returns the base unchanged. `scale` multiplies capacity **and** refill amount together, preserving time-to-fill.

- [ ] **Step 1: Write the failing test**

```python
from zae_limiter.schedule import ScheduleEntry, effective_params

BASE = (1_000_000, 1_000_000, 60_000)   # 1000 tokens/min, in milli-units
TUE_1400 = int(datetime(2026, 9, 15, 14, 0, tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)
TUE_0300 = int(datetime(2026, 9, 15, 3, 0, tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)


class TestEffectiveParams:
    def test_no_schedule_returns_base(self):
        assert effective_params(*BASE, (), TUE_1400) == BASE

    def test_no_match_returns_base(self):
        sched = (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", scale=0.5),)
        assert effective_params(*BASE, sched, TUE_1400) == BASE

    def test_scale_halves_capacity_and_refill_together(self):
        """Time-to-fill must be preserved: halving only capacity would double refill speed."""
        sched = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),)
        cp, ra, rp = effective_params(*BASE, sched, TUE_1400)
        assert (cp, ra, rp) == (500_000, 500_000, 60_000)
        assert cp / ra == BASE[0] / BASE[1]

    def test_absolute_capacity_overrides(self):
        sched = (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", capacity=2000),)
        assert effective_params(*BASE, sched, TUE_0300) == (2_000_000, 1_000_000, 60_000)

    def test_absolute_refill_fields_override_individually(self):
        sched = (ScheduleEntry(
            cron="* 0-6 * * *", tz="America/New_York",
            capacity=2000, refill_amount=500, refill_period_seconds=30,
        ),)
        assert effective_params(*BASE, sched, TUE_0300) == (2_000_000, 500_000, 30_000)

    def test_first_matching_entry_wins(self):
        sched = (
            ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),
            ScheduleEntry(cron="* * * * *", tz="America/New_York", scale=0.1),
        )
        assert effective_params(*BASE, sched, TUE_1400)[0] == 500_000

    def test_scale_floors_to_at_least_one_millitoken(self):
        """A tiny scale must not produce a zero capacity, which is unadmittable."""
        sched = (ScheduleEntry(cron="* * * * *", scale=0.0000001),)
        cp, ra, _rp = effective_params(1000, 1000, 60_000, sched, TUE_1400)
        assert cp >= 1 and ra >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_schedule.py::TestEffectiveParams -v`
Expected: FAIL with `ImportError: cannot import name 'effective_params'`

- [ ] **Step 3: Write minimal implementation**

```python
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
    check. Never mutates or persists anything: the caller uses the result and
    discards it (§2.1 — ``tk`` is the only materialised quantity).
    """
    if not sched:
        return cp_milli, ra_milli, rp_ms

    for entry in sched:
        if not matches(parse_cron(entry.cron, entry.tz), now_ms):
            continue
        if entry.scale is not None:
            # Scale capacity and refill together so time-to-fill is preserved.
            # Floor at 1 milli-unit: a zero capacity is unadmittable.
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
```

`parse_cron` is called per entry per evaluation here. Task 4 introduces the memoization that makes that cheap; do not optimise it now, and do not cache inside `effective_params` — the cache belongs with the parse, not the evaluation.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_schedule.py -v`
Expected: PASS

- [ ] **Step 5: Lint, type check, commit**

```bash
uv run ruff check --fix . && uv run ruff format . && uv run mypy
git add src/zae_limiter/schedule.py tests/unit/test_schedule.py
git commit -m "✨ feat(models): resolve effective limit params from a schedule

First matching entry wins; scale multiplies capacity and refill together
so time-to-fill is preserved. Returns the base unchanged when nothing
matches.

Refs #222"
```

---

### Task 4: `next_boundary` and the parse cache

**Files:**
- Modify: `src/zae_limiter/schedule.py`
- Test: `tests/unit/test_schedule_boundary.py`

**Interfaces:**
- Consumes: `matches`, `effective_params`, `parse_cron`
- Produces:
  - `next_boundary(sched: tuple[ScheduleEntry, ...], reset_sched: tuple[ScheduleEntry, ...] = (), now_ms: int = 0) -> int | None` — `None` when both tuples are empty.
    **Take the two-tuple signature now even though `reset_sched` is unused here.** The surface plan adds reset edges as boundary candidates; accepting the parameter from the start means that lands as a behaviour change in one function rather than a signature change rippling through `lease.py` and `processor.py`.
  - `parse_cron` becomes `functools.lru_cache`-backed

**Why this is a scan and not a library call.** Every cron library computes *fire times*. Under match-pattern semantics, `get_next` from inside a window returns `now + 1 min`, so fire times give window **starts** and nothing about window **ends** — and `vu` needs both. There is no `next_non_match` in any of them.

**Granularity and caps.** Step by the finest field any entry constrains: minute if any entry pins minutes, else hour, else day. Cap at 7 d / 31 d / 366 d respectively, bounding the step count at 10080 / 744 / 366. At ~1.05 µs per step (0.40 µs match + 0.65 µs UTC→local), the realistic hourly case is ~0.8 ms.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for next_boundary (#222 §3.2)."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from zae_limiter.schedule import ScheduleEntry, next_boundary

NY = ZoneInfo("America/New_York")


def _ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=NY).timestamp() * 1000)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, NY).isoformat()


BUSINESS = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),)


class TestNextBoundary:
    def test_none_without_a_schedule(self):
        assert next_boundary((), _ms("2026-09-15 06:00")) is None

    def test_finds_a_window_opening(self):
        assert _iso(next_boundary(BUSINESS, _ms("2026-09-15 06:00"))).startswith(
            "2026-09-15T09:00"
        )

    def test_finds_a_window_closing(self):
        """The half of the problem no cron library solves."""
        assert _iso(next_boundary(BUSINESS, _ms("2026-09-15 14:00"))).startswith(
            "2026-09-15T18:00"
        )

    def test_skips_the_weekend(self):
        assert _iso(next_boundary(BUSINESS, _ms("2026-09-12 18:30"))).startswith(
            "2026-09-14T09:00"
        )

    def test_window_edge_follows_local_time_across_dst(self):
        before = next_boundary(BUSINESS, _ms("2027-03-12 08:30"))
        after = next_boundary(BUSINESS, _ms("2027-03-16 08:30"))
        assert datetime.utcfromtimestamp(before / 1000).hour == 14   # EST
        assert datetime.utcfromtimestamp(after / 1000).hour == 13    # EDT

    def test_no_transition_returns_now_plus_cap(self):
        """A schedule that always matches has no boundary; cap rather than loop forever."""
        always = (ScheduleEntry(cron="* * * * *", scale=0.5),)
        now = _ms("2026-09-15 06:00")
        assert next_boundary(always, now) == now + 31 * 86_400_000

    def test_minute_granularity_uses_the_seven_day_cap(self):
        sched = (ScheduleEntry(cron="*/15 * * * *", scale=0.5),)
        now = _ms("2026-09-15 06:07")
        assert _iso(next_boundary(sched, now)).startswith("2026-09-15T06:08")

    def test_is_the_minimum_across_entries(self):
        sched = (
            ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),
            ScheduleEntry(cron="* 7-8 * * *", tz="America/New_York", scale=0.8),
        )
        assert _iso(next_boundary(sched, _ms("2026-09-15 06:00"))).startswith(
            "2026-09-15T07:00"
        )

    def test_boundary_is_where_effective_params_actually_change(self):
        """Two entries that resolve to the same numbers are not a boundary."""
        from zae_limiter.schedule import effective_params

        now = _ms("2026-09-15 06:00")
        b = next_boundary(BUSINESS, now)
        assert effective_params(1_000_000, 1_000_000, 60_000, BUSINESS, b - 60_000) != \
               effective_params(1_000_000, 1_000_000, 60_000, BUSINESS, b)


class TestParseCacheIsHot:
    def test_repeated_boundary_calls_do_not_reparse(self):
        from zae_limiter.schedule import parse_cron

        parse_cron.cache_clear()
        next_boundary(BUSINESS, _ms("2026-09-15 06:00"))
        first = parse_cron.cache_info().misses
        next_boundary(BUSINESS, _ms("2026-09-15 06:00"))
        assert parse_cron.cache_info().misses == first
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_schedule_boundary.py -v`
Expected: FAIL with `ImportError: cannot import name 'next_boundary'`

- [ ] **Step 3: Write minimal implementation**

Wrap `parse_cron` with `@lru_cache(maxsize=512)` (it takes two hashable strings and returns an immutable value, so this is safe), then append:

```python
# Step size and scan horizon, chosen by the finest field any entry constrains.
_MINUTE_MS = 60_000
_HOUR_MS = 3_600_000
_DAY_MS = 86_400_000
_GRANULARITY = {
    "minute": (_MINUTE_MS, 7 * _DAY_MS),
    "hour": (_HOUR_MS, 31 * _DAY_MS),
    "day": (_DAY_MS, 366 * _DAY_MS),
}


def _granularity(sched: tuple[ScheduleEntry, ...]) -> tuple[int, int]:
    """Step and cap for a scan, from the finest field any entry constrains."""
    parsed = [parse_cron(e.cron, e.tz) for e in sched]
    if any(len(p.minutes) < 60 for p in parsed):
        return _GRANULARITY["minute"]
    if any(len(p.hours) < 24 for p in parsed):
        return _GRANULARITY["hour"]
    return _GRANULARITY["day"]


def _active_index(sched: tuple[ScheduleEntry, ...], now_ms: int) -> int | None:
    """Index of the first matching entry, or None."""
    for i, entry in enumerate(sched):
        if matches(parse_cron(entry.cron, entry.tz), now_ms):
            return i
    return None


def next_boundary(
    sched: tuple[ScheduleEntry, ...],
    reset_sched: tuple[ScheduleEntry, ...] = (),
    *,
    now_ms: int,
) -> int | None:
    """The earliest instant at or after ``now_ms`` where the active entry changes.

    ``reset_sched`` is accepted but unused until the surface plan folds reset
    edges in as boundary candidates; taking it now keeps that a one-function
    change rather than a signature change across every caller.

    Returns None when there is no schedule. Returns ``now_ms + cap`` when no
    transition is found within the horizon, which forces one cheap
    re-materialisation per active bucket per cap period rather than looping.

    This is a scan, not a library call, because the boundary set includes
    window *closings* and no cron library computes those (§3.2).
    """
    if not sched:
        return None

    step, cap = _granularity(sched)
    current = _active_index(sched, now_ms)

    # Align the first probe to the step grid so an hourly scan lands on the
    # hour rather than on an arbitrary offset from `now`.
    probe = (now_ms // step + 1) * step
    horizon = now_ms + cap
    while probe <= horizon:
        if _active_index(sched, probe) != current:
            return probe
        probe += step
    return now_ms + cap
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_schedule_boundary.py -v`
Expected: PASS

`test_no_transition_returns_now_plus_cap` expects the **hour** cap of 31 days for `* * * * *` because no entry constrains minutes or hours. If it fails expecting the day cap, check `_granularity`: `* * * * *` has 60 minutes and 24 hours, so it falls through to `"day"` — fix the test's expected value to `366 * 86_400_000` rather than weakening the function.

- [ ] **Step 5: Lint, type check, commit**

```bash
uv run ruff check --fix . && uv run ruff format . && uv run mypy
git add src/zae_limiter/schedule.py tests/unit/test_schedule_boundary.py
git commit -m "$(cat <<'EOF'
✨ feat(models): compute the next schedule boundary by scanning

No cron library can do this: they all compute fire times, which give
window starts and say nothing about window ends, and `vu` needs both.
croniter.match() is also 362us per call because it re-parses every time,
so the scan matches against pre-parsed sets at ~1us per step instead.

Granularity adapts to the finest constrained field, bounding the scan at
10080/744/366 steps. parse_cron is now lru_cached.

Refs #222
EOF
)"
```

---

### Task 5: Compact encoding

**Files:**
- Modify: `src/zae_limiter/schedule.py`
- Test: `tests/unit/test_schedule_encoding.py`

**Interfaces:**
- Consumes: `ScheduleEntry`
- Produces:
  - `encode(sched: tuple[ScheduleEntry, ...]) -> tuple[str, str | None]` — `(compact, tz)`, where `tz` is the single shared timezone hoisted out
  - `decode(compact: str, tz: str) -> tuple[ScheduleEntry, ...]`
  - `to_cron(compact_entry: str) -> str` — canonical 5-field cron for display

**Why.** DynamoDB bills WCU per 1 KB, and a bucket item crossing 1 KB doubles the write cost of every acquire on it forever. Measured: the obvious JSON encoding crosses 1 KB at 3 limits × 2 entries (917 B) and reaches 1337 B at 4 × 3. The compact form is **4.9x smaller** and holds the worst shared case to 805 B — against 721 B for the same item with no schedule at all.

**Grammar.** Wildcard fields omitted; remaining fields letter-tagged `m h D M w`; names normalised to numbers; `scale` as integer per-mille with tag `s`; absolute capacity `c`, refill amount `a`, period `p`. Entries separated by `;`. Timezone hoisted to one item-level attribute. Example: `h9-17w1-5s500;h0-6c2000`.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from zae_limiter.schedule import ScheduleEntry, decode, encode, to_cron

BUSINESS = ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5)
NIGHTS = ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", capacity=2000)


class TestEncode:
    def test_compact_shape(self):
        compact, tz = encode((BUSINESS, NIGHTS))
        assert compact == "h9-17w1-5s500;h0-6c2000"
        assert tz == "America/New_York"

    def test_is_much_smaller_than_json(self):
        import json
        compact, _ = encode((BUSINESS, NIGHTS))
        as_json = json.dumps(
            [{"c": BUSINESS.cron, "z": BUSINESS.tz, "s": 0.5},
             {"c": NIGHTS.cron, "z": NIGHTS.tz, "cp": 2000}],
            separators=(",", ":"),
        )
        assert len(compact) * 4 < len(as_json)

    def test_empty_schedule(self):
        assert encode(()) == ("", None)

    def test_rejects_mixed_timezones(self):
        """tz is hoisted to one item-level attribute, so entries must agree."""
        with pytest.raises(ValueError, match="timezone"):
            encode((BUSINESS, ScheduleEntry(cron="* 0-6 * * *", tz="UTC", scale=0.5)))


class TestRoundTrip:
    @pytest.mark.parametrize("entries", [
        (BUSINESS,), (NIGHTS,), (BUSINESS, NIGHTS),
        (ScheduleEntry(cron="*/15 * * * SAT,SUN", tz="UTC", scale=0.25),),
        (ScheduleEntry(cron="0 0 1 JAN,JUL *", tz="UTC", capacity=5000),),
        (ScheduleEntry(cron="* * * * *", tz="UTC", capacity=7, refill_amount=3,
                       refill_period_seconds=30),),
    ])
    def test_semantic_round_trip(self, entries):
        compact, tz = encode(entries)
        restored = decode(compact, tz or "UTC")
        # Re-encoding must be byte-identical: the encoding is canonical.
        assert encode(restored) == (compact, tz)

    def test_decoded_entries_evaluate_identically(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from zae_limiter.schedule import effective_params

        compact, tz = encode((BUSINESS, NIGHTS))
        restored = decode(compact, tz)
        now = int(datetime(2026, 9, 15, 14, 0,
                           tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)
        base = (1_000_000, 1_000_000, 60_000)
        assert effective_params(*base, restored, now) == effective_params(*base, (BUSINESS, NIGHTS), now)


class TestDisplay:
    @pytest.mark.parametrize("compact,expected", [
        ("h9-17w1-5s500", "* 9-17 * * MON-FRI"),
        ("h0-6c2000", "* 0-6 * * *"),
        ("m*/15w6,7s250", "*/15 * * * SAT,SUN"),
        ("m0h0D1M1,7c5000", "0 0 1 JAN,JUL *"),
    ])
    def test_renders_canonical_cron_with_names(self, compact, expected):
        """Weekday and month always render as names, which is the one visible
        normalisation: an operator who typed 1-5 gets MON-FRI back."""
        assert to_cron(compact) == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_schedule_encoding.py -v`
Expected: FAIL with `ImportError: cannot import name 'encode'`

- [ ] **Step 3: Write minimal implementation**

Implement `encode` / `decode` / `to_cron` per the grammar above. Required details, each covered by a test:

- Field order is `m h D M w`; omit any field whose value is `*`.
- `DOW_NAMES = {0:"SUN",1:"MON",…,6:"SAT",7:"SUN"}` and `MONTH_NAMES = {1:"JAN",…}` for rendering; the inverse map for encoding. Encode normalises names to numbers; `to_cron` renders numbers back to names for weekday **and** month.
- Modifier tags: `s` = per-mille scale (`int(scale * 1000)`), `c` = capacity, `a` = refill_amount, `p` = refill_period_seconds.
- `encode` raises `ValueError` if entries disagree on `tz`, since it is hoisted to one attribute.
- Tokenise in `decode` with `re.findall(r"([mhDMwscap])([^mhDMwscap]+)", entry)` — field specs contain only digits, `-`, `,`, `/` and `*`, none of which collide with the tag letters.
- `decode` reconstructs a canonical 5-field cron string and hands it to `ScheduleEntry`, so cronsim stays the only parser and Task 2's oracle covers this path unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_schedule_encoding.py -v`
Expected: PASS

- [ ] **Step 5: Add a size regression test**

```python
def test_bucket_item_stays_under_one_kb():
    """A bucket item crossing 1 KB doubles the WCU cost of every acquire on it."""
    from zae_limiter.schedule import encode
    compact, tz = encode((BUSINESS, NIGHTS))
    # 4 limits x 2 entries, shared schedule: item-level `sched` + `sched_tz`
    overhead = len("sched") + len(compact) + len("sched_tz") + len(tz) + len("vu") + 7
    assert overhead < 200, f"schedule overhead {overhead} B is larger than budgeted"
```

- [ ] **Step 6: Lint, type check, commit**

```bash
uv run ruff check --fix . && uv run ruff format . && uv run mypy
git add src/zae_limiter/schedule.py tests/unit/test_schedule_encoding.py
git commit -m "$(cat <<'EOF'
✨ feat(schema): compact storage encoding for schedules

Standard 5-field cron at every API boundary; a compact form in storage.
4.9x smaller, which matters because a bucket item crossing the 1 KB WCU
boundary doubles the write cost of every acquire on it forever — the
obvious JSON encoding crosses it at 3 limits x 2 entries.

Decodes to a canonical cron string, so cronsim stays the only parser.

Refs #222
EOF
)"
```

---

### Task 6: `refill_bucket` clamps unconditionally

**Files:**
- Modify: `src/zae_limiter/bucket.py` (both early returns, around lines 78-92)
- Test: `tests/unit/test_bucket.py`

**Interfaces:**
- Consumes: nothing new
- Produces: no signature change — `refill_bucket` simply stops returning a value above `capacity_milli`

**This task has no schedule dependency and can be done first.** It is what actually replaces #469: instead of a targeted clamp bolted onto `set_limits`, trimming happens on every refill path, always.

Both early returns currently skip the clamp: `elapsed_ms <= 0` returns the input untouched, and `tokens_to_add == 0` returns the input untouched. A surplus over a lowered cap therefore survives every pass that computes no refill.

**Expected fallout:** this retires the documented "transient up to 1.5x capacity for one refill window" after a shard doubling — shard 0 is now clamped to its share on the next pass. Update that claim in `CLAUDE.md` as part of this task.

- [ ] **Step 1: Write the failing test**

```python
class TestUnconditionalClamp:
    """A surplus over a lowered cap must not survive a pass that adds no tokens (#469)."""

    def test_clamps_when_no_time_has_passed(self):
        r = refill_bucket(
            tokens_milli=900_000, last_refill_ms=1000, now_ms=1000,
            capacity_milli=500_000, refill_amount_milli=500_000, refill_period_ms=60_000,
        )
        assert r.new_tokens_milli == 500_000
        assert r.new_last_refill_ms == 1000

    def test_clamps_when_elapsed_is_negative(self):
        r = refill_bucket(
            tokens_milli=900_000, last_refill_ms=2000, now_ms=1000,
            capacity_milli=500_000, refill_amount_milli=500_000, refill_period_ms=60_000,
        )
        assert r.new_tokens_milli == 500_000

    def test_clamps_when_elapsed_is_too_short_to_add_a_millitoken(self):
        r = refill_bucket(
            tokens_milli=900_000, last_refill_ms=1000, now_ms=1001,
            capacity_milli=500_000, refill_amount_milli=1, refill_period_ms=60_000,
        )
        assert r.new_tokens_milli == 500_000

    def test_does_not_disturb_a_bucket_already_at_or_below_capacity(self):
        r = refill_bucket(
            tokens_milli=100_000, last_refill_ms=1000, now_ms=1000,
            capacity_milli=500_000, refill_amount_milli=500_000, refill_period_ms=60_000,
        )
        assert r.new_tokens_milli == 100_000

    def test_leaves_debt_alone(self):
        """Buckets go negative for post-hoc reconciliation; clamping is min(), not max()."""
        r = refill_bucket(
            tokens_milli=-50_000, last_refill_ms=1000, now_ms=1000,
            capacity_milli=500_000, refill_amount_milli=500_000, refill_period_ms=60_000,
        )
        assert r.new_tokens_milli == -50_000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_bucket.py::TestUnconditionalClamp -v`
Expected: FAIL — `assert 900000 == 500000` on the first three

- [ ] **Step 3: Write minimal implementation**

In `refill_bucket`, change both early returns to clamp:

```python
    if elapsed_ms <= 0:
        # Clamp even with no elapsed time: a capacity that shrank under a full
        # bucket — via set_limits, a schedule boundary, or a shard doubling —
        # leaves a surplus that must not survive (#222 §3.3, replaces #469).
        return RefillResult(min(capacity_milli, tokens_milli), last_refill_ms)

    tokens_to_add = (elapsed_ms * refill_amount_milli) // refill_period_ms

    if tokens_to_add == 0:
        # Not enough time for even one millitoken — but still clamp, for the
        # same reason as above.
        return RefillResult(min(capacity_milli, tokens_milli), last_refill_ms)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_bucket.py -v`
Expected: PASS

- [ ] **Step 5: Run the full unit suite and fix fallout**

Run: `uv run pytest tests/unit/ -q`

Expect failures in shard-related tests asserting the old 1.5x transient. Those assertions are now wrong, not the code — update them to expect the clamped share, and update the "transient up to 1.5x capacity for one refill window" sentence in `CLAUDE.md`'s Pre-Shard Buckets section.

- [ ] **Step 6: Lint, type check, commit**

```bash
uv run ruff check --fix . && uv run ruff format . && uv run mypy
git add src/zae_limiter/bucket.py tests/unit/test_bucket.py CLAUDE.md
git commit -m "$(cat <<'EOF'
🐛 fix(bucket): clamp tokens on every refill path, unconditionally

Both early returns skipped min(cap, tokens), so a surplus over a lowered
cap survived every pass that computed no refill. This is what replaces
#469: trimming happens everywhere, always, rather than as a targeted
clamp on set_limits.

Retires the documented 1.5x transient after a shard doubling — shard 0
is now clamped to its share on the next pass.

Refs #222, #469
EOF
)"
```

---

### Task 7: The aggregator must apply negative deltas

**Files:**
- Modify: `src/zae_limiter_aggregator/processor.py` (`try_refill_bucket`, the `if refill_delta <= 0: continue` guard at line 574)
- Test: `tests/unit/test_aggregator_processor.py`

**Interfaces:**
- Consumes: `refill_bucket` from Task 6
- Produces: no signature change

**Why Task 6 alone is not enough.** `try_refill_bucket` computes `refill_delta = new - old` and skips on `refill_delta <= 0`. With Task 6's clamp the delta at a shrink is *negative*, so the aggregator silently skips the trim — and on a hot bucket the aggregator's whole job is to keep tokens topped up so the client slow path never runs. Without this change, a `set_limits` shrink never takes effect on exactly the buckets that matter.

**Why a negative `ADD` is safe.** Same commutativity argument as the positive case: the delta removes exactly `T0 − eff_cp`, and concurrent consumption subtracts independently. During a boundary window the fast path is blocked by `vu` (Task 10), so consumption is bounded by slow paths already using the new cap.

- [ ] **Step 1: Write the failing test**

```python
class TestNegativeRefillDelta:
    def test_writes_a_negative_delta_to_trim_a_surplus(self):
        """A bucket holding more than its cap must be trimmed, not skipped."""
        table = MagicMock()
        state = BucketRefillState(
            entity_id="user-1", resource="gpt-4", shard_count=1, rf_ms=1000,
            limits={"rpm": LimitRefillInfo(
                tk_milli=900_000, cp_milli=500_000, ra_milli=500_000,
                rp_ms=60_000, tc_delta=0,
            )},
        )
        assert try_refill_bucket(table, state, now_ms=1000) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":rd_rpm"] == -400_000

    def test_still_skips_when_nothing_to_do(self):
        table = MagicMock()
        state = BucketRefillState(
            entity_id="user-1", resource="gpt-4", shard_count=1, rf_ms=1000,
            limits={"rpm": LimitRefillInfo(
                tk_milli=500_000, cp_milli=500_000, ra_milli=500_000,
                rp_ms=60_000, tc_delta=0,
            )},
        )
        assert try_refill_bucket(table, state, now_ms=1000) is False
        table.update_item.assert_not_called()

    def test_trims_against_the_per_shard_share(self):
        """Effective cap is capacity // shard_count; trimming to the undivided
        capacity would leave every shard holding the whole limit."""
        table = MagicMock()
        state = BucketRefillState(
            entity_id="user-1", resource="gpt-4", shard_count=4, rf_ms=1000,
            limits={"rpm": LimitRefillInfo(
                tk_milli=400_000, cp_milli=800_000, ra_milli=800_000,
                rp_ms=60_000, tc_delta=0,
            )},
        )
        assert try_refill_bucket(table, state, now_ms=1000) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":rd_rpm"] == -200_000   # 400_000 -> 800_000//4 == 200_000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_aggregator_processor.py::TestNegativeRefillDelta -v`
Expected: FAIL — `try_refill_bucket` returns False and writes nothing

Check the exact constructor names for `BucketRefillState` and `LimitRefillInfo` in `processor.py` before writing the test and adjust the kwargs to match; the shapes above reflect the fields named in `CLAUDE.md` but the field order is not guaranteed.

- [ ] **Step 3: Write minimal implementation**

Replace the guard:

```python
        refill_delta = result.new_tokens_milli - info.tk_milli
        if refill_delta == 0:
            continue

        if refill_delta > 0:
            # Only top up when projected tokens will not cover the observed
            # consumption rate for the next batch window.
            if result.new_tokens_milli >= max(0, info.tc_delta):
                continue
        # A negative delta is a clamp: the bucket holds more than its effective
        # cap after a shrink (set_limits, a schedule boundary, or a shard
        # doubling). It is safe as an ADD for the same commutativity reason the
        # positive case is — it removes exactly the surplus, and concurrent
        # consumption subtracts independently (#222 §3.3).
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_aggregator_processor.py -v`
Expected: PASS

- [ ] **Step 5: Lint, type check, commit**

```bash
uv run ruff check --fix . && uv run ruff format . && uv run mypy
git add src/zae_limiter_aggregator/processor.py tests/unit/test_aggregator_processor.py
git commit -m "$(cat <<'EOF'
🐛 fix(aggregator): apply negative refill deltas to trim a surplus

`if refill_delta <= 0: continue` meant the aggregator skipped the clamp
while still topping the bucket up, so on a hot bucket — where the
aggregator exists precisely to keep the slow path from running — a
capacity shrink never took effect.

Safe as an ADD for the same reason the positive delta is.

Refs #222, #469
EOF
)"
```

---

### Task 8: `Limit.schedule` and config serialisation

**Files:** Modify `src/zae_limiter/models.py`, `src/zae_limiter/repository.py` (config write ~4761, config read ~4794) · Test `tests/unit/test_repository.py`

**Interfaces:**
- Consumes: `ScheduleEntry`, `encode`, `decode` (Tasks 1, 5)
- Produces: `Limit.schedule: tuple[ScheduleEntry, ...] = ()`; config items carry `l_{name}_sched` plus one item-level `sched_tz`

- [ ] **Step 1: Write the failing test**

```python
class TestScheduleConfigRoundTrip:
    async def test_schedule_survives_set_and_get(self, test_repo):
        sched = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),)
        await test_repo.set_limits(
            "user-1", [Limit.per_minute("rpm", 1000).with_schedule(sched)], resource="gpt-4"
        )
        (limit,) = await test_repo.get_limits("user-1", resource="gpt-4")
        assert limit.schedule == sched

    async def test_limit_without_schedule_round_trips_as_empty(self, test_repo):
        await test_repo.set_limits("user-1", [Limit.per_minute("rpm", 1000)], resource="gpt-4")
        (limit,) = await test_repo.get_limits("user-1", resource="gpt-4")
        assert limit.schedule == ()

    async def test_replacing_a_limit_without_a_schedule_removes_the_stored_one(self, test_repo):
        """No inheritance and no merge: a limit with no schedule has no schedule (1.6)."""
        sched = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),)
        await test_repo.set_limits(
            "user-1", [Limit.per_minute("rpm", 1000).with_schedule(sched)], resource="gpt-4"
        )
        await test_repo.set_limits("user-1", [Limit.per_minute("rpm", 1000)], resource="gpt-4")
        (limit,) = await test_repo.get_limits("user-1", resource="gpt-4")
        assert limit.schedule == ()
```

- [ ] **Step 2: Run it and watch it fail** — `uv run pytest tests/unit/test_repository.py -k Schedule -v`; expected `AttributeError: 'Limit' object has no attribute 'schedule'`

- [ ] **Step 3: Implement**

Add to `Limit`: `schedule: tuple[ScheduleEntry, ...] = ()`, and a `with_schedule(self, schedule)` helper returning `replace(self, schedule=schedule)` — `Limit` is frozen and the factory methods (`per_minute`, `per_hour`, ...) do not take a schedule, so a helper keeps call sites readable. Extend `__post_init__` to reject a schedule whose entries disagree on `tz` (matching `encode`'s constraint, so an invalid combination fails at construction rather than at write).

In the config **write** path, alongside `limit_attr(name, LIMIT_FIELD_CP)`: when `limit.schedule`, call `encode(limit.schedule)` and write `limit_attr(name, "sched")` plus the item-level `sched_tz`. When it is empty, **REMOVE** both rather than skipping — the third test above is the one that catches a skip.

In the config **read** path, decode `l_{name}_sched` with the item's `sched_tz`.

- [ ] **Step 4: Run it and watch it pass**; then `hatch run generate-sync`

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix . && uv run ruff format . && uv run mypy
git add -A && git commit -m "✨ feat(models): carry a schedule on Limit and persist it

Refs #222"
```

---

### Task 9: `BucketState.sched`; effective params become time-dependent

**Files:** Modify `src/zae_limiter/models.py` (properties at 494-527), `src/zae_limiter/bucket.py` (9 call sites), `src/zae_limiter/lease.py:689` · Test `tests/unit/test_bucket.py`

**Interfaces:**
- Consumes: `effective_params` (Task 3)
- Produces: `BucketState.sched`; `effective_capacity_milli(now_ms)`, `effective_refill_amount_milli(now_ms)`, `retry_refill_amount_milli(now_ms)` as **methods**

**Order is scale-then-shard-divide.** The schedule applies to the whole limit; shards split the result.

- [ ] **Step 1: Write the failing test**

```python
class TestScheduledEffectiveParams:
    def test_scale_applies_before_shard_division(self):
        """Dividing first then scaling would compound the flooring differently."""
        state = BucketState(
            limit_name="rpm", tokens_milli=0, capacity_milli=1_000_000,
            refill_amount_milli=1_000_000, refill_period_ms=60_000, last_refill_ms=0,
            shard_count=4,
            sched=(ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),),
        )
        assert state.effective_capacity_milli(TUE_1400) == 125_000    # (1_000_000*0.5)//4
        assert state.effective_capacity_milli(TUE_0300) == 250_000    # base//4, no match

    def test_unscheduled_state_is_unchanged(self):
        state = BucketState(
            limit_name="rpm", tokens_milli=0, capacity_milli=1_000_000,
            refill_amount_milli=1_000_000, refill_period_ms=60_000, last_refill_ms=0,
            shard_count=1,
        )
        assert state.effective_capacity_milli(TUE_1400) == 1_000_000

    def test_retry_rate_falls_back_to_the_undivided_scheduled_rate(self):
        """A share that floors to 0 has no finite wait; fall back to the
        scheduled-but-undivided rate, not the base rate."""
        state = BucketState(
            limit_name="rpm", tokens_milli=0, capacity_milli=1_000_000,
            refill_amount_milli=1_000, refill_period_ms=60_000, last_refill_ms=0,
            shard_count=1024,
            sched=(ScheduleEntry(cron="* * * * *", scale=0.5),),
        )
        assert state.effective_refill_amount_milli(TUE_1400) == 0
        assert state.retry_refill_amount_milli(TUE_1400) == 500
```

- [ ] **Step 2: Run it and watch it fail** — `TypeError: 'int' object is not callable`

- [ ] **Step 3: Implement.** Add `sched: tuple[ScheduleEntry, ...] = ()` to `BucketState`. Convert the three properties to methods:

```python
    def effective_capacity_milli(self, now_ms: int) -> int:
        """This shard's share of the capacity in force at ``now_ms``.

        Scale first, divide second: the schedule applies to the whole limit
        and shards split the result (2.1).
        """
        cp, _ra, _rp = effective_params(
            self.capacity_milli, self.refill_amount_milli, self.refill_period_ms,
            self.sched, now_ms,
        )
        return cp // self.shard_count
```

`effective_refill_amount_milli` mirrors it on `ra`. `retry_refill_amount_milli(now_ms)` returns `self.effective_refill_amount_milli(now_ms) or <the undivided scheduled ra>` — note it must fall back to the **scheduled** undivided rate, not `self.refill_amount_milli`, or the estimate quotes a rate no shard refills at.

Thread `now_ms` through all 9 `bucket.py` call sites (every enclosing function already takes it) and through `lease.py`'s `_build_retry_failure_statuses`, which needs `now_ms` added to its signature and supplied by its caller.

- [ ] **Step 4: Run `uv run pytest tests/unit/ -q`**, fix fallout, `hatch run generate-sync`

- [ ] **Step 5: Commit** — `♻️ refactor(models): make effective bucket params a function of time`

---

### Task 10: Rejections quote the scheduled capacity

**Files:** Modify `src/zae_limiter/models.py` (`Limit.per_shard` 366, `from_bucket_state` 357) · Test `tests/unit/test_models.py`

- [ ] **Step 1: Write the failing test**

```python
def test_rate_limit_exceeded_quotes_the_scheduled_capacity():
    """During a 0.5x window, a rejection must not promise the base capacity (#475)."""
    state = BucketState(
        limit_name="rpm", tokens_milli=0, capacity_milli=1_000_000,
        refill_amount_milli=1_000_000, refill_period_ms=60_000, last_refill_ms=0,
        shard_count=1,
        sched=(ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),),
    )
    assert Limit.from_bucket_state(state, now_ms=TUE_1400).capacity == 500
    assert Limit.from_bucket_state(state, now_ms=TUE_0300).capacity == 1000
```

- [ ] **Step 2: Watch it fail** — `TypeError: from_bucket_state() got an unexpected keyword argument 'now_ms'`

- [ ] **Step 3: Implement.** `from_bucket_state(cls, state, now_ms)` builds from `state.effective_capacity_milli(now_ms)` / `effective_refill_amount_milli(now_ms)` — which already include the shard division, so it must **not** also call `per_shard`. Update every call site (grep `from_bucket_state`) to pass `now_ms`.

- [ ] **Step 4: Run, regenerate sync, commit** — `🐛 fix(models): report scheduled capacity in rate-limit errors`

---

### Task 11: `vu` joins the fast-path condition

**Files:** Modify `src/zae_limiter/repository.py` (condition build ~2600-2620, classification ~2660-2715), `src/zae_limiter/repository_protocol.py:28` · Test `tests/unit/test_repository.py`

**Interfaces:** Produces `SpeculativeFailureReason.SCHEDULE_BOUNDARY`

**Ordering matters.** An expired `vu` must be classified **before** the exhausted reasons, exactly as `DISABLED` is at `repository.py:2672` — otherwise an expired window looks like a rejection and the limiter raises `RateLimitExceeded` instead of re-materialising.

- [ ] **Step 1: Write the failing test**

```python
class TestScheduleBoundaryClassification:
    async def test_expired_vu_classifies_as_schedule_boundary(self, test_repo):
        ...  # create a bucket, then set vu into the past directly
        result = await test_repo.speculative_consume("user-1", "gpt-4", {"rpm": 1})
        assert result.failure_reason is SpeculativeFailureReason.SCHEDULE_BOUNDARY

    async def test_schedule_boundary_wins_over_exhausted(self, test_repo):
        """An empty bucket whose vu also expired must re-materialise, not reject."""
        ...  # drain the bucket AND expire vu
        result = await test_repo.speculative_consume("user-1", "gpt-4", {"rpm": 1})
        assert result.failure_reason is SpeculativeFailureReason.SCHEDULE_BOUNDARY

    async def test_future_vu_does_not_affect_the_fast_path(self, test_repo):
        result = await test_repo.speculative_consume("user-1", "gpt-4", {"rpm": 1})
        assert result.success is True

    async def test_absent_vu_does_not_affect_the_fast_path(self, test_repo):
        """Unscheduled buckets carry no vu at all."""
        result = await test_repo.speculative_consume("user-1", "gpt-4", {"rpm": 1})
        assert result.success is True
```

- [ ] **Step 2: Watch it fail** — `AttributeError: SCHEDULE_BOUNDARY`

- [ ] **Step 3: Implement.** Add `SCHEDULE_BOUNDARY = "schedule_boundary"` to both `SpeculativeFailureReason` enums (async and generated sync). Beside the existing `disabled` guard:

```python
        # Reject a bucket whose schedule window has closed (2.1). The fast
        # path cannot evaluate a schedule, so `vu` is a precomputed instant:
        # past it, tk was materialised under params that no longer apply.
        attr_names["#vu"] = schema.BUCKET_FIELD_VU
        attr_values[":now_ms"] = {"N": str(self._now_ms())}
        condition_parts.append("(attribute_not_exists(#vu) OR #vu > :now_ms)")
```

In the failure branch, immediately after the `DISABLED` check and **before** the exhausted classification:

```python
                    vu = old_item.get(schema.BUCKET_FIELD_VU, {}).get("N")
                    if vu is not None and int(vu) <= self._now_ms():
                        return SpeculativeResult(
                            success=False, old_buckets=old_buckets,
                            cascade=old_cascade, parent_id=old_parent_id,
                            shard_id=shard_id, shard_count=old_shard_count,
                            failure_reason=SpeculativeFailureReason.SCHEDULE_BOUNDARY,
                        )
```

Add `BUCKET_FIELD_VU = "vu"` to `schema.py`. In `limiter.py`, route `SCHEDULE_BOUNDARY` to the slow path — the same branch `BUCKET_MISSING` takes, **not** a shard retry: resharding cannot help and would create a shard needlessly.

- [ ] **Step 4: Run, regenerate sync, commit** — `✨ feat(repository): gate the fast path on the schedule valid-until stamp`

---

### Task 12: Slow-path materialisation

**Files:** Modify `src/zae_limiter/repository.py` (`build_composite_normal` 2170), `src/zae_limiter/lease.py` (~377-393) · Test `tests/unit/test_lease.py`

**One instant drives the whole slow-path pass — and it is the slow path's own.** #430 makes
one `acquire()` observe one clock *on the fast path*; `_do_acquire` and `_try_parent_only_acquire`
still take their own reading, and that is correct rather than an oversight. Inheriting the fast
path's instant across a `BatchGetItem` and a transaction would stamp `rf` in the past and
under-refill by the round-trip time. What matters is that the slow path's single reading drives
**all three** of `effective_params(...)`, `next_boundary(...)` and the `rf` stamp in
`build_composite_normal`. Splitting them is what issue #430 names as "an inconsistent `(rf, vu)`
pair the next reader cannot trust" — thread one `now_ms` from `_do_acquire` through `lease.py`
into the builder, and assert it with a test that patches `_now_ms` to a per-call counter and
checks `rf` and `vu` derive from the same reading.

A related non-hazard, recorded so nobody re-derives it: a boundary crossing *between* the fast
path's rejection and the slow path's read is harmless. The slow path reads later, so it evaluates
the new window — never the old one.

**The clamp is already handled — do not add a second one.** `lease.py:384` computes `refill_amounts[name] = entry.state.tokens_milli - entry._original_tokens_milli + consumed_milli`, a *delta* from the already-refilled state. Once Task 6 clamps inside `refill_bucket`, that delta goes negative on a surplus and `ADD tk (delta - consumed)` trims correctly. Adding an explicit clamp here would double-apply it.

- [ ] **Step 1: Write the failing test**

```python
class TestSlowPathMaterialisation:
    def test_writes_vu_from_the_next_boundary(self):
        item = repo.build_composite_normal(
            "user-1", "gpt-4", consumed={"rpm": 1000}, refill_amounts={"rpm": 0},
            now_ms=NOW, expected_rf=NOW - 1000, vu=NOW + 3_600_000,
        )
        upd = item["Update"]
        assert ":vu" in upd["ExpressionAttributeValues"]
        assert "#vu = :vu" in upd["UpdateExpression"]

    def test_omits_vu_when_there_is_no_schedule(self):
        item = repo.build_composite_normal(
            "user-1", "gpt-4", consumed={"rpm": 1000}, refill_amounts={"rpm": 0},
            now_ms=NOW, expected_rf=NOW - 1000, vu=None,
        )
        assert "#vu" not in item["Update"]["ExpressionAttributeNames"]

    def test_negative_refill_delta_trims_the_surplus(self):
        """Task 6 makes this delta negative; the ADD must carry it through."""
        item = repo.build_composite_normal(
            "user-1", "gpt-4", consumed={"rpm": 1000}, refill_amounts={"rpm": -400_000},
            now_ms=NOW, expected_rf=NOW - 1000, vu=None,
        )
        v = item["Update"]["ExpressionAttributeValues"]
        assert v[":b_rpm_tk_delta"] == {"N": str(-400_000 - 1000)}
```

- [ ] **Step 2: Watch it fail** — `TypeError: unexpected keyword argument 'vu'`

- [ ] **Step 3: Implement.** Add `vu: int | None = None` to `build_composite_normal`; when not None, append `#vu = :vu` to `set_parts` with `attr_names["#vu"] = schema.BUCKET_FIELD_VU`. Do the same for `build_composite_create` (a new bucket needs its first `vu`).

In `lease.py`, compute `vu = next_boundary(entry.limit.schedule, now_ms)` per bucket group — the **minimum** across the group's limits, since `vu` is item-level — and pass it. In `_do_acquire`'s refill computation, source the capacity and refill from `state.effective_capacity_milli(now_ms)` / `effective_refill_amount_milli(now_ms)`; Task 9 already routed `try_consume` through those.

- [ ] **Step 4: Run, regenerate sync, commit** — `✨ feat(lease): materialise tokens and the next boundary on the slow path`

---

### Task 13: The fan-out stamps the schedule and forces one pass

**Files:** Modify `src/zae_limiter/repository.py` (`_sync_bucket_params` 3057) · Test `tests/integration/test_repository_ops.py`

**`vu = 0`, not a computed boundary.** Computing it would leave `vu` in the future while `tk` still holds a surplus over the new cap, so the fast path would admit against it until natural refill caught up — the exact burst #469 existed to prevent. `vu = 0` forces one materialising pass that trims. Cost at 10k active buckets is ~$0.014 per admin operation.

- [ ] **Step 1: Write the failing test**

```python
class TestFanOutStampsSchedule:
    async def test_set_limits_stamps_sched_and_expires_vu(self, test_repo):
        ...  # create a bucket, then set_limits with a schedule
        item = await _raw_bucket_item(test_repo, "user-1", "gpt-4", shard=0)
        assert item["sched"]["S"] == "h9-17w1-5s500"
        assert item["sched_tz"]["S"] == "America/New_York"
        assert item["vu"]["N"] == "0"

    async def test_removing_a_schedule_removes_the_stamps(self, test_repo):
        ...  # set a schedule, then set_limits again without one
        item = await _raw_bucket_item(test_repo, "user-1", "gpt-4", shard=0)
        assert "sched" not in item and "vu" not in item

    async def test_every_shard_is_stamped(self, test_repo):
        """Keying only shard 0 left the rest enforcing their birth params (#468)."""
        ...  # force shard_count to 4, create all shards, then set_limits
        for shard in range(4):
            item = await _raw_bucket_item(test_repo, "user-1", "gpt-4", shard=shard)
            assert item["vu"]["N"] == "0"


class TestFastPathReadsNoConfig:
    async def test_future_vu_costs_zero_rcu(self, test_repo, capacity_counter):
        """The load-bearing claim of the whole design (2.1)."""
        ...  # warm the bucket and the config cache, then reset the counter
        capacity_counter.reset()
        async with limiter.acquire("user-1", "gpt-4", consume={"rpm": 1}):
            pass
        assert capacity_counter.rcu == 0
        assert capacity_counter.wcu == 1
```

- [ ] **Step 2: Watch it fail** — `KeyError: 'sched'`

- [ ] **Step 3: Implement.** In `_sync_bucket_params`, after the `cp`/`ra`/`rp` loop: when any limit carries a schedule, `encode` it and SET `sched` (item-level default) plus `sched_tz`, and SET `vu = 0`. Write `b_{name}_sched` only for a limit whose encoding differs from the item-level default (4.1). When no limit carries a schedule, REMOVE `sched`, `sched_tz`, every `b_{name}_sched`, and `vu`.

- [ ] **Step 4: Run integration** — `uv run pytest tests/integration/ -v` with LocalStack up

- [ ] **Step 5: Regenerate sync, commit** — `✨ feat(repository): stamp schedules onto buckets and force one pass`

---

### Task 14: Aggregator schedule awareness and Lambda packaging

**Files:** Modify `src/zae_limiter_aggregator/processor.py`, `src/zae_limiter/infra/lambda_builder.py`, `src/zae_limiter/infra/provisioner_builder.py` · Test `tests/unit/test_aggregator_processor.py`, `tests/unit/test_lambda_builder.py`

**Packaging detail.** `lambda_builder.py` copies `schema.py`, `bucket.py`, `models.py`, `exceptions.py` (lines 141-151); `provisioner_builder.py` copies `schema.py`, `models.py`, `exceptions.py` (lines 131-139) and **does not vendor `bucket.py`**. `schedule.py` must be added to **both**. `cronsim` and `tzdata` must be in the `[lambda]` extra (Task 1) or the Lambda will `ImportError` at cold start.

- [ ] **Step 1: Write the failing test**

```python
class TestAggregatorRespectsSchedules:
    def test_refills_at_the_scheduled_rate_not_the_base(self):
        """During a 0.5x window the aggregator must not top up toward the base."""
        state = BucketRefillState(
            entity_id="user-1", resource="gpt-4", shard_count=1, rf_ms=TUE_1400 - 60_000,
            sched=(ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),),
            limits={"rpm": LimitRefillInfo(tk_milli=0, cp_milli=1_000_000,
                                           ra_milli=1_000_000, rp_ms=60_000, tc_delta=600_000)},
        )
        table = MagicMock()
        assert try_refill_bucket(table, state, now_ms=TUE_1400) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":rd_rpm"] == 500_000

    def test_restamps_vu_when_it_has_expired(self):
        ...  # expired vu -> the refill write also SETs a new vu


class TestLambdaPackaging:
    def test_both_builders_vendor_schedule_py(self, tmp_path):
        assert (build_aggregator(tmp_path) / "zae_limiter" / "schedule.py").exists()
        assert (build_provisioner(tmp_path) / "zae_limiter" / "schedule.py").exists()

    def test_lambda_extra_carries_cronsim_and_tzdata(self):
        reqs = _get_runtime_requirements()
        assert any(r.startswith("cronsim") for r in reqs)
        assert any(r.startswith("tzdata") for r in reqs)
```

- [ ] **Step 2: Watch them fail**

- [ ] **Step 3: Implement.** Parse `sched`/`sched_tz`/`b_{name}_sched` in the stream-record parser into `BucketRefillState.sched` and `ParsedBucketLimit`; in `try_refill_bucket`, replace `info.cp_milli // shard_count` with `effective_params(...)` then `// shard_count`; when the record's `vu` has expired, add `SET vu = :vu` with `next_boundary(...)` to the refill write. Add the `shutil.copy2` line for `schedule.py` to both builders.

- [ ] **Step 4: Run unit + a LocalStack E2E with the aggregator enabled**

- [ ] **Step 5: Commit** — `✨ feat(aggregator): refill at the scheduled rate and re-stamp vu`

---

## Sequencing

Tasks 1–5 build `schedule.py` bottom-up and touch nothing else in the codebase — they are pure additions and can be reviewed as a unit.

Tasks 6–7 are the unconditional clamp. **They have no dependency on Tasks 1–5 and could go first.** They are the #469 replacement, they are independently valuable, and they carry the one behaviour change to existing code that this plan makes — so landing them early and separately keeps that change reviewable on its own rather than buried among schedule machinery.

Tasks 8–14 wire scheduling into `repository.py`, `limiter.py` and the aggregator. Each needs `hatch run generate-sync` and the regenerated files committed.

## Self-Review

**Spec coverage.** §3.1 → Tasks 1-2. §3.2 → Task 4. §3.3 → Tasks 6-7 (both halves: `bucket.py` *and* `processor.py`, which is the part that makes #469 genuinely unnecessary). §3.5 → Task 9. §4 → Task 5. §2.1 → Tasks 11-13. §1.1 validation → Task 1. Effective-params resolution (§2.1) → Task 3.

**Not covered here, by design:** §5 (surface), §6 (failure handling), §7 (`retry_after`), §3.6 (`reset_schedule`) — all in the surface plan. §5.2 (provisioner sync) is its own plan.

**Placeholder scan.** Tasks 1-7 carry complete code. Tasks 8-14 carry complete structure, real signatures and real assertions, but several test bodies use `...` for fixture setup that depends on helpers this repo already has (`_raw_bucket_item`, forcing a shard count, expiring `vu` directly). Those are setup, not logic — the assertions they lead to are exact. Resolve each against `tests/integration/` conventions when the task is picked up.

**One test body below is knowingly approximate:** Task 14's `test_refills_at_the_scheduled_rate_not_the_base` asserts `:rd_rpm == 500_000`, which assumes the refill threshold lets a half-rate top-up through at `tc_delta=600_000`. Verify the arithmetic against `try_refill_bucket`'s actual threshold when implementing rather than trusting the literal.

**Type consistency.** `effective_params(cp_milli, ra_milli, rp_ms, sched, now_ms) -> tuple[int, int, int]` is consumed with that exact signature in Tasks 4 and 9. `next_boundary(sched, reset_sched=(), *, now_ms)` returns `None` only when there is no schedule at all, which Task 12 must handle by omitting `vu` rather than writing null. The keyword-only `now_ms` prevents a positional call silently binding a timestamp to `reset_sched` once the surface plan starts passing two tuples. `encode` returns `(compact, tz | None)` and `decode(compact, tz)` takes a non-optional `tz`, so Task 13 must supply `"UTC"` when `encode` returned `None` — or skip writing both attributes, which is what an empty schedule means.

**One risk not covered by a test.** `parse_cron` is `lru_cache`d on `(cron, tz)`, and `ParsedCron` holds a `ZoneInfo`. `ZoneInfo` instances are themselves cached by the stdlib, so this is safe — but if the cache is ever keyed on a `ScheduleEntry` instead, `scale` being a float makes `ScheduleEntry` hashable-but-imprecise. Key the cache on strings only, as written.
