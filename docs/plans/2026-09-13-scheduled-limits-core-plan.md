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
- **`Repository._now_ms()` is the clock seam (#430) — but it does NOT cover the config cache.**
  Patch `_now_ms` to control time deterministically; never patch global `time`. The trap:
  `config_cache.py:99` (`_is_expired`) and `:103` (`_make_entry`) still call `time.time()`, so a
  test that advances `_now_ms` by an hour to cross a schedule boundary gets the **pre-boundary
  limits** back — the 60s config-cache TTL has not expired in real time. The seam controls refill
  math, not config staleness. Either call `repo.invalidate_config_cache()` after advancing the
  clock, or build the repository with `config_cache_ttl=0`. This will bite Tasks 11-13 and
  surface-plan Task 3 specifically; #430 scoped `config_cache` out on purpose.
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

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"capacity": 0},
            {"capacity": -5},
            {"capacity": 10, "refill_amount": 0},
            {"capacity": 10, "refill_period_seconds": 0},
        ],
    )
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
    before = int(datetime(2027, 3, 12, 9, 0, tzinfo=NY).timestamp())  # Fri, EST
    after = int(datetime(2027, 3, 16, 9, 0, tzinfo=NY).timestamp())  # Tue, EDT
    assert matches(parsed, before * 1000) and matches(parsed, after * 1000)
    assert datetime.utcfromtimestamp(before).hour == 14
    assert datetime.utcfromtimestamp(after).hour == 13


@pytest.mark.parametrize(
    "day,expected_minutes",
    [
        ("2027-03-14", 1380),  # spring forward: a 23-hour day
        ("2026-11-01", 1500),  # fall back: a 25-hour day
        ("2026-09-15", 1440),  # ordinary
    ],
)
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

BASE = (1_000_000, 1_000_000, 60_000)  # 1000 tokens/min, in milli-units
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
        sched = (
            ScheduleEntry(
                cron="* 0-6 * * *",
                tz="America/New_York",
                capacity=2000,
                refill_amount=500,
                refill_period_seconds=30,
            ),
        )
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
  - `next_boundary(sched: tuple[ScheduleEntry, ...], reset_sched: tuple[ScheduleEntry, ...] = (), *, now_ms: int) -> int | None` — `None` when both tuples are empty. **`now_ms` is keyword-only on purpose:** it is the second thing a caller wants to pass and the second *positional* slot belongs to `reset_sched`, so a positional call silently binds a timestamp to a schedule tuple. Every call site in this plan and the surface plan passes `now_ms=`.
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
        assert next_boundary((), now_ms=_ms("2026-09-15 06:00")) is None

    def test_finds_a_window_opening(self):
        assert _iso(next_boundary(BUSINESS, now_ms=_ms("2026-09-15 06:00"))).startswith(
            "2026-09-15T09:00"
        )

    def test_finds_a_window_closing(self):
        """The half of the problem no cron library solves."""
        assert _iso(next_boundary(BUSINESS, now_ms=_ms("2026-09-15 14:00"))).startswith(
            "2026-09-15T18:00"
        )

    def test_skips_the_weekend(self):
        assert _iso(next_boundary(BUSINESS, now_ms=_ms("2026-09-12 18:30"))).startswith(
            "2026-09-14T09:00"
        )

    def test_window_edge_follows_local_time_across_dst(self):
        before = next_boundary(BUSINESS, now_ms=_ms("2027-03-12 08:30"))
        after = next_boundary(BUSINESS, now_ms=_ms("2027-03-16 08:30"))
        assert datetime.utcfromtimestamp(before / 1000).hour == 14  # EST
        assert datetime.utcfromtimestamp(after / 1000).hour == 13  # EDT

    def test_no_transition_returns_now_plus_cap(self):
        """A schedule that always matches has no boundary; cap rather than loop forever."""
        always = (ScheduleEntry(cron="* * * * *", scale=0.5),)
        now = _ms("2026-09-15 06:00")
        assert next_boundary(always, now_ms=now) == now + 31 * 86_400_000

    def test_minute_granularity_uses_the_seven_day_cap(self):
        sched = (ScheduleEntry(cron="*/15 * * * *", scale=0.5),)
        now = _ms("2026-09-15 06:07")
        assert _iso(next_boundary(sched, now_ms=now)).startswith("2026-09-15T06:08")

    def test_is_the_minimum_across_entries(self):
        sched = (
            ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),
            ScheduleEntry(cron="* 7-8 * * *", tz="America/New_York", scale=0.8),
        )
        assert _iso(next_boundary(sched, now_ms=_ms("2026-09-15 06:00"))).startswith(
            "2026-09-15T07:00"
        )

    def test_boundary_is_where_effective_params_actually_change(self):
        """Two entries that resolve to the same numbers are not a boundary."""
        from zae_limiter.schedule import effective_params

        now = _ms("2026-09-15 06:00")
        b = next_boundary(BUSINESS, now_ms=now)
        assert effective_params(
            1_000_000, 1_000_000, 60_000, BUSINESS, b - 60_000
        ) != effective_params(1_000_000, 1_000_000, 60_000, BUSINESS, b)


class TestParseCacheIsHot:
    def test_repeated_boundary_calls_do_not_reparse(self):
        from zae_limiter.schedule import parse_cron

        parse_cron.cache_clear()
        next_boundary(BUSINESS, now_ms=_ms("2026-09-15 06:00"))
        first = parse_cron.cache_info().misses
        next_boundary(BUSINESS, now_ms=_ms("2026-09-15 06:00"))
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

**Why.** DynamoDB bills WCU per 1 KB, and a bucket item crossing 1 KB doubles the write cost of every acquire on it forever. Measured: the obvious JSON encoding reaches 917 B at 3 limits × 2 entries and first crosses 1 KB at 4 × 3 (1337 B). The compact form is **4.9x smaller** and holds the worst shared case to 805 B — against 721 B for the same item with no schedule at all. (This sentence previously said the JSON form *crossed* 1 KB at 3 × 2, which 917 B does not; #507.)

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
            [
                {"c": BUSINESS.cron, "z": BUSINESS.tz, "s": 0.5},
                {"c": NIGHTS.cron, "z": NIGHTS.tz, "cp": 2000},
            ],
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
    @pytest.mark.parametrize(
        "entries",
        [
            (BUSINESS,),
            (NIGHTS,),
            (BUSINESS, NIGHTS),
            (ScheduleEntry(cron="*/15 * * * SAT,SUN", tz="UTC", scale=0.25),),
            (ScheduleEntry(cron="0 0 1 JAN,JUL *", tz="UTC", capacity=5000),),
            (
                ScheduleEntry(
                    cron="* * * * *",
                    tz="UTC",
                    capacity=7,
                    refill_amount=3,
                    refill_period_seconds=30,
                ),
            ),
        ],
    )
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
        now = int(
            datetime(2026, 9, 15, 14, 0, tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000
        )
        base = (1_000_000, 1_000_000, 60_000)
        assert effective_params(*base, restored, now) == effective_params(
            *base, (BUSINESS, NIGHTS), now
        )


class TestDisplay:
    @pytest.mark.parametrize(
        "compact,expected",
        [
            ("h9-17w1-5s500", "* 9-17 * * MON-FRI"),
            ("h0-6c2000", "* 0-6 * * *"),
            ("m*/15w6,7s250", "*/15 * * * SAT,SUN"),
            ("m0h0D1M1,7c5000", "0 0 1 JAN,JUL *"),
        ],
    )
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
            tokens_milli=900_000,
            last_refill_ms=1000,
            now_ms=1000,
            capacity_milli=500_000,
            refill_amount_milli=500_000,
            refill_period_ms=60_000,
        )
        assert r.new_tokens_milli == 500_000
        assert r.new_last_refill_ms == 1000

    def test_clamps_when_elapsed_is_negative(self):
        r = refill_bucket(
            tokens_milli=900_000,
            last_refill_ms=2000,
            now_ms=1000,
            capacity_milli=500_000,
            refill_amount_milli=500_000,
            refill_period_ms=60_000,
        )
        assert r.new_tokens_milli == 500_000

    def test_clamps_when_elapsed_is_too_short_to_add_a_millitoken(self):
        r = refill_bucket(
            tokens_milli=900_000,
            last_refill_ms=1000,
            now_ms=1001,
            capacity_milli=500_000,
            refill_amount_milli=1,
            refill_period_ms=60_000,
        )
        assert r.new_tokens_milli == 500_000

    def test_does_not_disturb_a_bucket_already_at_or_below_capacity(self):
        r = refill_bucket(
            tokens_milli=100_000,
            last_refill_ms=1000,
            now_ms=1000,
            capacity_milli=500_000,
            refill_amount_milli=500_000,
            refill_period_ms=60_000,
        )
        assert r.new_tokens_milli == 100_000

    def test_leaves_debt_alone(self):
        """Buckets go negative for post-hoc reconciliation; clamping is min(), not max()."""
        r = refill_bucket(
            tokens_milli=-50_000,
            last_refill_ms=1000,
            now_ms=1000,
            capacity_milli=500_000,
            refill_amount_milli=500_000,
            refill_period_ms=60_000,
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

**Constructor note (verified against `processor.py:111`).** `BucketRefillState` requires
`namespace_id` — it has no default and `try_refill_bucket` uses it to build the key, so a test
that omits it fails with `TypeError` rather than an assertion. `LimitRefillInfo`'s field order is
`tc_delta, tk_milli, cp_milli, ra_milli, rp_ms`. `table` is a boto3 **Table resource**, so
`ExpressionAttributeValues` carries plain ints, not `{"N": "..."}`.

**Why a negative `ADD` is safe.** Same commutativity argument as the positive case: the delta removes exactly `T0 − eff_cp`, and concurrent consumption subtracts independently. During a boundary window the fast path is blocked by `vu` (Task 10), so consumption is bounded by slow paths already using the new cap.

- [ ] **Step 1: Write the failing test**

```python
class TestNegativeRefillDelta:
    def test_writes_a_negative_delta_to_trim_a_surplus(self):
        """A bucket holding more than its cap must be trimmed, not skipped."""
        table = MagicMock()
        state = BucketRefillState(
            namespace_id="ns123",
            entity_id="user-1",
            resource="gpt-4",
            shard_count=1,
            rf_ms=1000,
            limits={
                "rpm": LimitRefillInfo(
                    tk_milli=900_000,
                    cp_milli=500_000,
                    ra_milli=500_000,
                    rp_ms=60_000,
                    tc_delta=0,
                )
            },
        )
        assert try_refill_bucket(table, state, now_ms=1000) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":rd_rpm"] == -400_000

    def test_still_skips_when_nothing_to_do(self):
        table = MagicMock()
        state = BucketRefillState(
            namespace_id="ns123",
            entity_id="user-1",
            resource="gpt-4",
            shard_count=1,
            rf_ms=1000,
            limits={
                "rpm": LimitRefillInfo(
                    tk_milli=500_000,
                    cp_milli=500_000,
                    ra_milli=500_000,
                    rp_ms=60_000,
                    tc_delta=0,
                )
            },
        )
        assert try_refill_bucket(table, state, now_ms=1000) is False
        table.update_item.assert_not_called()

    def test_trims_against_the_per_shard_share(self):
        """Effective cap is capacity // shard_count; trimming to the undivided
        capacity would leave every shard holding the whole limit."""
        table = MagicMock()
        state = BucketRefillState(
            namespace_id="ns123",
            entity_id="user-1",
            resource="gpt-4",
            shard_count=4,
            rf_ms=1000,
            limits={
                "rpm": LimitRefillInfo(
                    tk_milli=400_000,
                    cp_milli=800_000,
                    ra_milli=800_000,
                    rp_ms=60_000,
                    tc_delta=0,
                )
            },
        )
        assert try_refill_bucket(table, state, now_ms=1000) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":rd_rpm"] == -200_000  # 400_000 -> 800_000//4 == 200_000
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

**Files:**
- Modify: `src/zae_limiter/models.py` (`Limit` at :186), `src/zae_limiter/repository.py` (config write :4784-4790, config read :4817)
- Test: `tests/unit/test_repository.py`

**Interfaces:**
- Consumes: `ScheduleEntry` (Task 1), `encode` / `decode` (Task 5)
- Produces: `Limit.schedule: tuple[ScheduleEntry, ...] = ()`; `Limit.with_schedule(schedule) -> Limit`; config items carry `l_{name}_sched` plus one item-level `sched_tz`

**Fixture note:** `tests/unit/test_repository.py` uses the moto-backed **`repo`** fixture (`tests/unit/test_repository.py:24`), not `test_repo` — that name belongs to the LocalStack integration fixture in `tests/fixtures/repositories.py:76`.

- [ ] **Step 1: Write the failing test**

```python
class TestScheduleConfigRoundTrip:
    """`l_{name}_sched` round-trips through set_limits/get_limits (§1.4)."""

    SCHED = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),)

    async def test_schedule_survives_set_and_get(self, repo):
        await repo.set_limits(
            "user-1",
            [Limit.per_minute("rpm", 1000).with_schedule(self.SCHED)],
            resource="gpt-4",
        )
        (limit,) = await repo.get_limits("user-1", resource="gpt-4")
        assert limit.schedule == self.SCHED

    async def test_limit_without_schedule_round_trips_as_empty(self, repo):
        await repo.set_limits("user-1", [Limit.per_minute("rpm", 1000)], resource="gpt-4")
        (limit,) = await repo.get_limits("user-1", resource="gpt-4")
        assert limit.schedule == ()

    async def test_replacing_a_limit_without_a_schedule_removes_the_stored_one(self, repo):
        """Override, not merge: a limit with no schedule has no schedule (§1.6).

        This is the test that catches a write path which merely *skips*
        `l_{name}_sched` when the schedule is empty instead of REMOVEing it.
        """
        await repo.set_limits(
            "user-1",
            [Limit.per_minute("rpm", 1000).with_schedule(self.SCHED)],
            resource="gpt-4",
        )
        await repo.set_limits("user-1", [Limit.per_minute("rpm", 1000)], resource="gpt-4")
        (limit,) = await repo.get_limits("user-1", resource="gpt-4")
        assert limit.schedule == ()

    async def test_two_limits_with_different_schedules(self, repo):
        """Schedules are per-limit (§2.2); tz is shared, the crons need not be."""
        night = (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", capacity=2000),)
        await repo.set_limits(
            "user-1",
            [
                Limit.per_minute("rpm", 1000).with_schedule(self.SCHED),
                Limit.per_minute("tpm", 100_000).with_schedule(night),
            ],
            resource="gpt-4",
        )
        by_name = {lim.name: lim for lim in await repo.get_limits("user-1", resource="gpt-4")}
        assert by_name["rpm"].schedule == self.SCHED
        assert by_name["tpm"].schedule == night


class TestScheduleValidation:
    def test_rejects_entries_disagreeing_on_timezone(self):
        """`sched_tz` is one item-level attribute, so entries must agree (§4.1)."""
        with pytest.raises(ValueError, match="timezone"):
            Limit.per_minute("rpm", 1000).with_schedule(
                (
                    ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),
                    ScheduleEntry(cron="* 0-6 * * *", tz="UTC", scale=0.5),
                )
            )

    def test_with_schedule_returns_a_new_limit(self):
        """`Limit` is frozen; `with_schedule` must not mutate."""
        base = Limit.per_minute("rpm", 1000)
        scheduled = base.with_schedule((ScheduleEntry(cron="* * * * *", tz="UTC", scale=0.5),))
        assert base.schedule == ()
        assert scheduled is not base
        assert scheduled.capacity == base.capacity
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `uv run pytest tests/unit/test_repository.py -k "ScheduleConfig or ScheduleValidation" -v`
Expected: FAIL with `AttributeError: 'Limit' object has no attribute 'with_schedule'`

- [ ] **Step 3: Add the field and the helper**

In `models.py`, add to `Limit` after `refill_period_seconds`:

```python
    schedule: tuple[ScheduleEntry, ...] = ()
```

Extend `Limit.__post_init__` with the shared-timezone rule, which mirrors `encode`'s constraint so an unstorable combination fails at construction rather than at write time:

```python
        if self.schedule:
            zones = {entry.tz for entry in self.schedule}
            if len(zones) > 1:
                raise ValueError(
                    f"all schedule entries on one limit must share a timezone, got "
                    f"{sorted(zones)}. The timezone is stored once per bucket item "
                    f"as `sched_tz`, not per entry."
                )
```

And the helper — the factory methods (`per_minute`, `per_hour`, …) do not take a schedule, and `Limit` is frozen, so this keeps call sites readable:

```python
    def with_schedule(self, schedule: tuple[ScheduleEntry, ...]) -> "Limit":
        """This limit with a schedule attached (§1.1).

        Returns a new instance; `Limit` is frozen. Validation runs through
        `__post_init__`, so a schedule whose entries disagree on timezone is
        rejected here rather than at the DynamoDB write.
        """
        return replace(self, schedule=schedule)
```

`replace` is already imported in `models.py` (it backs `per_shard`).

- [ ] **Step 4: Write and read the config attribute**

In `repository.py`'s config **write** path, alongside the existing `limit_attr` block at :4784:

```python
            if limit.schedule:
                compact, tz = schedule.encode(limit.schedule)
                base_item[schema.limit_attr(name, schema.LIMIT_FIELD_SCHED)] = {"S": compact}
                base_item[schema.CONFIG_FIELD_SCHED_TZ] = {"S": tz or "UTC"}
```

An empty schedule must **REMOVE** rather than skip — a config item is written with `put_item`, so an omitted attribute already disappears; confirm that against the write path you are editing, and if the level uses `update_item` instead, add the explicit REMOVE. `test_replacing_a_limit_without_a_schedule_removes_the_stored_one` is what distinguishes the two.

In the config **read** path at :4817, after the numeric fields are collected:

```python
        sched_attr = item.get(schema.limit_attr(name, schema.LIMIT_FIELD_SCHED), {}).get("S")
        sched = (
            schedule.decode(sched_attr, item.get(schema.CONFIG_FIELD_SCHED_TZ, {}).get("S", "UTC"))
            if sched_attr
            else ()
        )
```

Add to `schema.py` beside the existing `LIMIT_FIELD_*` constants at :96-98:

```python
LIMIT_FIELD_SCHED = "sched"  # compact-encoded schedule (§4.1)
CONFIG_FIELD_SCHED_TZ = "sched_tz"  # IANA name, hoisted out of every entry
```

- [ ] **Step 5: Run the test and watch it pass**

Run: `uv run pytest tests/unit/test_repository.py -k "ScheduleConfig or ScheduleValidation" -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Regenerate sync, lint, type check**

```bash
hatch run generate-sync
uv run ruff check --fix . && uv run ruff format . && uv run mypy
uv run pytest tests/unit/ -q
```

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
✨ feat(models): carry a schedule on Limit and persist it

Limit gains `schedule` plus a `with_schedule()` helper, stored as
`l_{name}_sched` with the timezone hoisted to one item-level `sched_tz`.

Entries on one limit must share a timezone — the encoding stores it once
per item, so a disagreeing pair is unstorable and is rejected at
construction rather than at the write.

Refs #222
EOF
)"
```

---

### Task 9: `BucketState.sched`; effective params become time-dependent

**Files:**
- Modify: `src/zae_limiter/models.py` (`BucketState` at :464, the three properties at :504-527), `src/zae_limiter/bucket.py` (10 attribute references across 5 functions), `src/zae_limiter/lease.py` (`_build_retry_failure_statuses` at :673, its `retry_refill_amount_milli` use at :688)
- Test: `tests/unit/test_bucket.py`

**Interfaces:**
- Consumes: `effective_params` (Task 3), `ScheduleEntry` (Task 1)
- Produces: `BucketState.sched`; `effective_capacity_milli(now_ms)`, `effective_refill_amount_milli(now_ms)`, `retry_refill_amount_milli(now_ms)` as **methods**

**Order is scale-then-shard-divide.** The schedule applies to the whole limit; shards split the result.

**`BucketState` requires `entity_id`, `resource` and `limit_name`** (`models.py:476-478`) — they have no defaults. Every construction below supplies them; a test that omits them fails with `TypeError`, not an assertion.

- [ ] **Step 1: Write the failing test**

```python
from datetime import datetime
from zoneinfo import ZoneInfo

from zae_limiter.models import BucketState
from zae_limiter.schedule import ScheduleEntry

NY = ZoneInfo("America/New_York")
TUE_1400 = int(datetime(2026, 9, 15, 14, 0, tzinfo=NY).timestamp() * 1000)  # inside 9-17
TUE_0300 = int(datetime(2026, 9, 15, 3, 0, tzinfo=NY).timestamp() * 1000)  # outside


def _state(**kwargs) -> BucketState:
    """A BucketState with the three required identity fields filled in."""
    base = dict(
        entity_id="user-1",
        resource="gpt-4",
        limit_name="rpm",
        tokens_milli=0,
        last_refill_ms=0,
        capacity_milli=1_000_000,
        refill_amount_milli=1_000_000,
        refill_period_ms=60_000,
    )
    base.update(kwargs)
    return BucketState(**base)


BUSINESS = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),)


class TestScheduledEffectiveParams:
    def test_scale_applies_before_shard_division(self):
        """Scale then divide: the schedule applies to the whole limit, shards
        split the result. Dividing first would floor twice against a smaller
        numerator and drift."""
        state = _state(shard_count=4, sched=BUSINESS)
        assert state.effective_capacity_milli(TUE_1400) == 125_000  # (1_000_000*0.5)//4
        assert state.effective_capacity_milli(TUE_0300) == 250_000  # 1_000_000//4, no match

    def test_refill_scales_with_capacity(self):
        state = _state(shard_count=1, sched=BUSINESS)
        assert state.effective_refill_amount_milli(TUE_1400) == 500_000
        assert state.effective_refill_amount_milli(TUE_0300) == 1_000_000

    def test_unscheduled_state_is_unchanged_at_any_instant(self):
        state = _state(shard_count=1)
        assert state.effective_capacity_milli(TUE_1400) == 1_000_000
        assert state.effective_capacity_milli(TUE_0300) == 1_000_000

    def test_absolute_entry_overrides_capacity(self):
        night = (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", capacity=2000),)
        state = _state(shard_count=1, sched=night)
        assert state.effective_capacity_milli(TUE_0300) == 2_000_000
        assert state.effective_capacity_milli(TUE_1400) == 1_000_000

    def test_retry_rate_falls_back_to_the_undivided_scheduled_rate(self):
        """A share that floors to 0 has no finite wait. Fall back to the
        *scheduled* undivided rate — falling back to the base rate would quote
        a speed nothing in the system refills at during the window."""
        state = _state(refill_amount_milli=1_000, shard_count=1024, sched=BUSINESS)
        assert state.effective_refill_amount_milli(TUE_1400) == 0
        assert state.retry_refill_amount_milli(TUE_1400) == 500  # 1_000*0.5, undivided

    def test_retry_rate_uses_the_share_when_it_is_non_zero(self):
        state = _state(shard_count=2, sched=BUSINESS)
        assert state.retry_refill_amount_milli(TUE_1400) == 250_000
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `uv run pytest tests/unit/test_bucket.py -k ScheduledEffectiveParams -v`
Expected: FAIL with `TypeError: BucketState.__init__() got an unexpected keyword argument 'sched'`

- [ ] **Step 3: Convert the properties to methods**

Add to `BucketState` after `shard_count` at :492:

```python
    # Compact-encoded schedule decoded from the bucket item (§4.1). Empty for
    # an unscheduled bucket, which is the overwhelming majority — the effective
    # methods below then return the stored values unchanged.
    sched: tuple[ScheduleEntry, ...] = ()
```

Replace the three properties at :504-527:

```python
def effective_capacity_milli(self, now_ms: int) -> int:
    """This shard's share of the capacity in force at ``now_ms``.

    Scale first, divide second (§2.1): the schedule applies to the whole
    limit and the shards split the result.
    """
    cp, _ra, _rp = effective_params(
        self.capacity_milli,
        self.refill_amount_milli,
        self.refill_period_ms,
        self.sched,
        now_ms,
    )
    return cp // self.shard_count


def effective_refill_amount_milli(self, now_ms: int) -> int:
    """This shard's share of the refill in force at ``now_ms``."""
    _cp, ra, _rp = effective_params(
        self.capacity_milli,
        self.refill_amount_milli,
        self.refill_period_ms,
        self.sched,
        now_ms,
    )
    return ra // self.shard_count


def retry_refill_amount_milli(self, now_ms: int) -> int:
    """Refill rate for a "seconds until available" estimate at ``now_ms``.

    ``effective_refill_amount_milli`` floors to 0 for a slow refill on a
    heavily sharded bucket, and a rate of 0 has no finite wait. Fall back
    to the *scheduled but undivided* rate — not the base rate, which
    during a ``scale`` window is a speed nothing refills at.
    """
    share = self.effective_refill_amount_milli(now_ms)
    if share:
        return share
    _cp, ra, _rp = effective_params(
        self.capacity_milli,
        self.refill_amount_milli,
        self.refill_period_ms,
        self.sched,
        now_ms,
    )
    return ra
```

`models.py` must now import `effective_params` and `ScheduleEntry` from `.schedule`. That direction is safe: `schedule.py` imports nothing from `models` (Task 1).

- [ ] **Step 4: Thread `now_ms` through every caller**

In `bucket.py`, all ten attribute references become calls. Each enclosing function already has `now_ms` in scope:

| Line | Function | Change |
|---|---|---|
| 124, 125 | `try_consume` | `state.effective_capacity_milli(now_ms)`, `…refill_amount_milli(now_ms)` |
| 147 | `try_consume` | `state.retry_refill_amount_milli(now_ms)` |
| 210, 211 | `calculate_available` | same two |
| 237, 238 | `calculate_time_until_available` | same two |
| 249 | `calculate_time_until_available` | `retry_refill_amount_milli(now_ms)` |
| 277, 278 | `force_consume` | same two |

In `lease.py`, `_build_retry_failure_statuses` (:673) has no clock. Add one:

```python
def _build_retry_failure_statuses(
    entries: list[LeaseEntry], now_ms: int
) -> list[LimitStatus]:
```

and change :688 to `entry.state.retry_refill_amount_milli(now_ms)`. Its caller must pass the slow path's single reading — the same one Task 12 threads — not a fresh `self.repository._now_ms()`, or the status quotes a different instant than the rejection it describes.

- [ ] **Step 5: Run the test and watch it pass, then the whole suite**

```bash
uv run pytest tests/unit/test_bucket.py -k ScheduledEffectiveParams -v
uv run pytest tests/unit/ -q
uv run pytest tests/unit/ -m gevent -n 0 -q
```

Expect fallout anywhere a test constructs a `BucketState` and reads the old properties. Those call sites become calls; the values are unchanged for an unscheduled bucket, so any test whose expectation *changes* is a signal to stop and look rather than to edit the number.

- [ ] **Step 6: Regenerate sync, lint, type check, commit**

```bash
hatch run generate-sync
uv run ruff check --fix . && uv run ruff format . && uv run mypy
git add -A
git commit -m "$(cat <<'EOF'
♻️ refactor(models): make effective bucket params a function of time

effective_capacity_milli, effective_refill_amount_milli and
retry_refill_amount_milli become methods taking now_ms, because with a
schedule the effective value depends on when you ask.

Order is scale-then-shard-divide: the schedule applies to the whole
limit and the shards split the result.

Refs #222
EOF
)"
```

---

### Task 10: Rejections quote the scheduled capacity

**Files:**
- Modify: `src/zae_limiter/models.py` (`from_bucket_state` :357, `per_shard` :366)
- Test: `tests/unit/test_models.py`

**Interfaces:**
- Consumes: `BucketState.effective_*` methods (Task 9)
- Produces: `Limit.from_bucket_state(state, now_ms) -> Limit`

A rejection during a `0.5x` window must not promise the base capacity (#475).

- [ ] **Step 1: Write the failing test**

```python
class TestScheduledStatusCapacity:
    def test_quotes_the_scheduled_capacity_inside_the_window(self):
        state = _state(shard_count=1, sched=BUSINESS)
        assert Limit.from_bucket_state(state, now_ms=TUE_1400).capacity == 500
        assert Limit.from_bucket_state(state, now_ms=TUE_0300).capacity == 1000

    def test_shard_division_is_not_applied_twice(self):
        """`effective_capacity_milli` already divides by shard_count, so
        `from_bucket_state` must not also call `per_shard`."""
        state = _state(shard_count=4, sched=BUSINESS)
        assert Limit.from_bucket_state(state, now_ms=TUE_1400).capacity == 125

    def test_unscheduled_matches_the_previous_behaviour(self):
        state = _state(shard_count=4)
        assert Limit.from_bucket_state(state, now_ms=TUE_1400).capacity == 250

    def test_sub_token_share_clamps_to_one(self):
        """`Limit` validates capacity > 0, so a share that floors to 0 must
        clamp rather than raise from inside an error path."""
        state = _state(capacity_milli=1_000, shard_count=32, sched=BUSINESS)
        assert Limit.from_bucket_state(state, now_ms=TUE_1400).capacity == 1
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `uv run pytest tests/unit/test_models.py -k ScheduledStatusCapacity -v`
Expected: FAIL with `TypeError: from_bucket_state() got an unexpected keyword argument 'now_ms'`

- [ ] **Step 3: Implement**

```python
    @classmethod
    def from_bucket_state(cls, state: "BucketState", now_ms: int) -> "Limit":
        """Reconstruct a Limit from BucketState as that shard sees it *now*.

        ``effective_*`` already divides by ``shard_count``, so this must NOT
        also call ``per_shard`` — doing both divides twice. Shares clamp to one
        whole token because ``Limit`` validates ``capacity > 0`` and this runs
        on the rejection path, where raising would mask the real error.
        """
        return cls.custom(
            name=state.limit_name,
            capacity=max(1, state.effective_capacity_milli(now_ms) // 1000),
            refill_amount=max(1, state.effective_refill_amount_milli(now_ms) // 1000),
            refill_period_seconds=state.refill_period_ms // 1000,
        )
```

`per_shard` stays as it is — it is still used where a `Limit` (not a `BucketState`) is the input, on the slow path and in the lease statuses. Update every `from_bucket_state` call site to pass `now_ms`:

```bash
rg -n "from_bucket_state" src/ tests/ --glob '!sync_*' --glob '!test_sync_*'
```

- [ ] **Step 4: Run, regenerate sync, commit**

```bash
uv run pytest tests/unit/ -q
hatch run generate-sync
uv run ruff check --fix . && uv run ruff format . && uv run mypy
git add -A
git commit -m "$(cat <<'EOF'
🐛 fix(models): report the scheduled capacity in rate-limit errors

from_bucket_state now takes now_ms, so a rejection during a scale window
quotes the capacity that actually rejected it rather than the base.

Refs #222, #475
EOF
)"
```

---

### Task 11: `vu` joins the fast-path condition

**Files:**
- Modify: `src/zae_limiter/schema.py` (beside `BUCKET_FIELD_*` at :63-68), `src/zae_limiter/repository_protocol.py` (`SpeculativeFailureReason` at :28), `src/zae_limiter/repository.py` (`_speculative_consume_single` condition build :2628-2639, failure classification :2694-2704), `src/zae_limiter/limiter.py` (failure-reason routing)
- Test: `tests/unit/test_repository.py`

**Interfaces:**
- Consumes: nothing from earlier tasks — `vu` is written by Tasks 12 and 13
- Produces: `SpeculativeFailureReason.SCHEDULE_BOUNDARY`; `schema.BUCKET_FIELD_VU`

**Use the bound `now_ms`, never `self._now_ms()`.** Post-#430, `_speculative_consume_single` takes `now_ms: int | None = None` and binds it once (`repository.py:2552`); both existing clock-derived parts — the `ttl` stamp at :2623 and the `#ttl > :now_epoch` guard at :2630 — already derive from it. The `vu` comparison is a third, and calling `self._now_ms()` here would re-introduce exactly the extra reading #430 removed and break `TestClockSeam::test_acquire_reads_the_clock_once`.

**Ordering matters.** An expired `vu` must be classified **before** the exhausted reasons, exactly as `DISABLED` is at :2695 — otherwise an expired window looks like a rejection and the limiter raises `RateLimitExceeded` instead of re-materialising.

- [ ] **Step 1: Write the failing test**

```python
class TestScheduleBoundaryClassification:
    """`vu` gates the fast path; an expired one routes to the slow path (§2.1)."""

    LIMITS = [Limit.per_minute("rpm", 100)]

    async def _make_bucket(self, repo, entity_id="vu-1"):
        await repo.create_entity(entity_id, parent_id=None, name=entity_id)
        await repo.set_limits(entity_id, self.LIMITS, resource="gpt-4")
        await repo.speculative_consume(entity_id, "gpt-4", {"rpm": 1})
        return entity_id

    async def _set_vu(self, repo, entity_id, vu_ms):
        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, "gpt-4", 0)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression="SET #vu = :vu",
            ExpressionAttributeNames={"#vu": schema.BUCKET_FIELD_VU},
            ExpressionAttributeValues={":vu": {"N": str(vu_ms)}},
        )

    async def test_expired_vu_classifies_as_schedule_boundary(self, repo):
        entity_id = await self._make_bucket(repo)
        await self._set_vu(repo, entity_id, repo._now_ms() - 1)

        result = await repo.speculative_consume(entity_id, "gpt-4", {"rpm": 1})
        assert result.success is False
        assert result.failure_reason is SpeculativeFailureReason.SCHEDULE_BOUNDARY

    async def test_schedule_boundary_wins_over_exhausted(self, repo):
        """An empty bucket whose `vu` also expired must re-materialise, not
        reject — otherwise the caller sees RateLimitExceeded against a limit
        that may have just been raised."""
        entity_id = await self._make_bucket(repo, "vu-drained")
        await repo.speculative_consume(entity_id, "gpt-4", {"rpm": 99})
        await self._set_vu(repo, entity_id, repo._now_ms() - 1)

        result = await repo.speculative_consume(entity_id, "gpt-4", {"rpm": 1})
        assert result.failure_reason is SpeculativeFailureReason.SCHEDULE_BOUNDARY

    async def test_future_vu_does_not_affect_the_fast_path(self, repo):
        entity_id = await self._make_bucket(repo, "vu-future")
        await self._set_vu(repo, entity_id, repo._now_ms() + 3_600_000)

        result = await repo.speculative_consume(entity_id, "gpt-4", {"rpm": 1})
        assert result.success is True

    async def test_absent_vu_does_not_affect_the_fast_path(self, repo):
        """Unscheduled buckets carry no `vu` at all — the overwhelming case.

        (Task 13 stamps `vu = 0` on every fan-out, so an unscheduled bucket
        does carry one transiently between a `set_limits` and the next
        materialising pass. This helper's `set_limits` precedes the bucket's
        creation, so no fan-out reaches it and the attribute is genuinely
        absent here.)
        """
        entity_id = await self._make_bucket(repo, "vu-absent")
        result = await repo.speculative_consume(entity_id, "gpt-4", {"rpm": 1})
        assert result.success is True


class TestScheduleBoundaryUsesOneClockReading:
    """The `vu` comparison must reuse the bound `now_ms` (#430)."""

    async def test_adding_vu_does_not_add_a_clock_reading(self, repo):
        await repo.create_entity("vu-clock", parent_id=None, name="vu-clock")
        await repo.set_limits("vu-clock", [Limit.per_minute("rpm", 100)], resource="gpt-4")
        await repo.speculative_consume("vu-clock", "gpt-4", {"rpm": 1})

        readings: list[int] = []
        base = repo._now_ms()

        def fake_now_ms() -> int:
            readings.append(base + 60_000 * len(readings))
            return readings[-1]

        repo._now_ms = fake_now_ms
        await repo.speculative_consume("vu-clock", "gpt-4", {"rpm": 1})

        assert len(readings) == 1, (
            f"speculative_consume read the clock {len(readings)} times: {readings}"
        )
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `uv run pytest tests/unit/test_repository.py -k ScheduleBoundary -v`
Expected: FAIL with `AttributeError: BUCKET_FIELD_VU` (the helper touches it first)

- [ ] **Step 3: Add the constant and the enum member**

In `schema.py`, beside the other bucket fields at :63-68:

```python
BUCKET_FIELD_VU = "vu"  # valid-until, epoch ms — schedule materialisation stamp (§2.1)
```

In `repository_protocol.py`, add to `SpeculativeFailureReason` after `BUCKET_MISSING` (:40) and extend the class docstring:

```python
    SCHEDULE_BOUNDARY = "schedule_boundary"
```

> SCHEDULE_BOUNDARY means the item's `vu` has passed, so `tk` was last
> materialised under schedule parameters that no longer apply. The limiter
> must take the slow path to re-materialise — not retry on another shard,
> which cannot help and would create a shard needlessly.

The generated `sync_repository_protocol.py` picks this up from `hatch run generate-sync`.

- [ ] **Step 4: Add the condition and the classification**

In `_speculative_consume_single`, immediately after the `disabled` guard at :2636-2639:

```python
        # Reject a bucket whose schedule window has closed (§2.1). The fast
        # path cannot evaluate a schedule, so `vu` is a precomputed instant:
        # past it, `tk` was materialised under parameters that no longer apply.
        # Uses the bound `now_ms` — a fresh read here would re-introduce the
        # second clock reading #430 removed.
        attr_names["#vu"] = schema.BUCKET_FIELD_VU
        attr_values[":vu_now"] = {"N": str(now_ms)}
        condition_parts.append("(attribute_not_exists(#vu) OR #vu > :vu_now)")
```

In the failure branch, between the `DISABLED` return (ends :2704) and the exhausted classification:

```python
                    # A closed window outranks exhaustion: the limits that
                    # rejected this write are stale, and the new window may
                    # admit it. Must precede the exhausted checks.
                    vu_raw = old_item.get(schema.BUCKET_FIELD_VU, {}).get("N")
                    if vu_raw is not None and int(vu_raw) <= now_ms:
                        return SpeculativeResult(
                            success=False,
                            old_buckets=old_buckets,
                            cascade=old_cascade,
                            parent_id=old_parent_id,
                            shard_id=shard_id,
                            shard_count=old_shard_count,
                            failure_reason=SpeculativeFailureReason.SCHEDULE_BOUNDARY,
                        )
```

In `limiter.py`, route `SCHEDULE_BOUNDARY` to the slow path — the branch `BUCKET_MISSING` already takes. Find it with `rg -n "BUCKET_MISSING" src/zae_limiter/limiter.py`. Do **not** route it to a shard retry.

- [ ] **Step 5: Run the test and watch it pass**

```bash
uv run pytest tests/unit/test_repository.py -k ScheduleBoundary -v
uv run pytest tests/unit/test_limiter.py -k ClockSeam -v   # must stay green
uv run pytest tests/unit/ -q
```

- [ ] **Step 6: Regenerate sync, lint, commit**

```bash
hatch run generate-sync
uv run ruff check --fix . && uv run ruff format . && uv run mypy
git add -A
git commit -m "$(cat <<'EOF'
✨ feat(repository): gate the fast path on the schedule valid-until stamp

The speculative condition gains one comparison against `vu`. It reuses
the bound now_ms rather than reading the clock again (#430), so a
scheduled acquire still observes exactly one instant per write.

An expired `vu` classifies as SCHEDULE_BOUNDARY *before* the exhausted
reasons — otherwise a closed window looks like a rejection and the
caller sees RateLimitExceeded against limits that no longer apply.

Refs #222
EOF
)"
```

---

### Task 12: Slow-path materialisation

**Files:**
- Modify: `src/zae_limiter/repository.py` (`build_composite_normal` :2175, `build_composite_create` :2078), `src/zae_limiter/lease.py` (refill-amount computation :383, `_commit_initial`)
- Test: `tests/unit/test_repository.py`, `tests/unit/test_lease.py`

**Interfaces:**
- Consumes: `next_boundary` (Task 4), `BucketState.effective_*` (Task 9)
- Produces: `build_composite_normal(..., vu: int | None = None)`, `build_composite_create(..., vu: int | None = None)`

**One instant drives the whole slow-path pass — and it is the slow path's own.** #430 makes one `acquire()` observe one clock *per write*; `_do_acquire` and `_try_parent_only_acquire` still take their own reading, and that is correct. Inheriting the fast path's instant across a `BatchGetItem` and a transaction would stamp `rf` in the past and under-refill by the round-trip time. What matters is that the slow path's single reading drives **all three** of `effective_params(...)`, `next_boundary(...)` and the `rf` stamp. Splitting them is what #430 calls "an inconsistent `(rf, vu)` pair the next reader cannot trust".

A related non-hazard, recorded so nobody re-derives it: a boundary crossed *between* the fast path's rejection and the slow path's read is harmless. The slow path reads later, so it evaluates the new window — never the old one.

**The clamp is already handled — do not add a second one.** `lease.py:383` computes `refill_amounts[name] = entry.state.tokens_milli - entry._original_tokens_milli + consumed_milli`, a *delta* from the already-refilled state. Once Task 6 clamps inside `refill_bucket`, that delta goes negative on a surplus and `ADD tk (delta - consumed)` trims correctly. An explicit clamp here would double-apply it.

- [ ] **Step 1: Write the failing test**

```python
class TestSlowPathWritesVu:
    NOW = 1_789_000_000_000

    def test_writes_vu_when_given_one(self, repo):
        item = repo.build_composite_normal(
            "user-1",
            "gpt-4",
            consumed={"rpm": 1000},
            refill_amounts={"rpm": 0},
            now_ms=self.NOW,
            expected_rf=self.NOW - 1000,
            vu=self.NOW + 3_600_000,
        )
        upd = item["Update"]
        assert "#vu = :vu" in upd["UpdateExpression"]
        assert upd["ExpressionAttributeValues"][":vu"] == {"N": str(self.NOW + 3_600_000)}
        assert upd["ExpressionAttributeNames"]["#vu"] == schema.BUCKET_FIELD_VU

    def test_omits_vu_when_there_is_no_schedule(self):
        """`next_boundary` returns None for an unscheduled limit, and None must
        omit the attribute rather than write a null."""
        item = repo.build_composite_normal(
            "user-1",
            "gpt-4",
            consumed={"rpm": 1000},
            refill_amounts={"rpm": 0},
            now_ms=self.NOW,
            expected_rf=self.NOW - 1000,
            vu=None,
        )
        assert "#vu" not in item["Update"]["ExpressionAttributeNames"]
        assert ":vu" not in item["Update"]["ExpressionAttributeValues"]

    def test_negative_refill_delta_trims_the_surplus(self):
        """Task 6 makes this delta negative on a shrink; the ADD carries it."""
        item = repo.build_composite_normal(
            "user-1",
            "gpt-4",
            consumed={"rpm": 1000},
            refill_amounts={"rpm": -400_000},
            now_ms=self.NOW,
            expected_rf=self.NOW - 1000,
            vu=None,
        )
        values = item["Update"]["ExpressionAttributeValues"]
        assert values[":b_rpm_tk_delta"] == {"N": str(-400_000 - 1000)}

    def test_create_also_stamps_vu(self):
        """A bucket created on the slow path needs its first `vu`, or the very
        next acquire takes the fast path against an unmaterialised item."""
        item = repo.build_composite_create(
            "user-1",
            "gpt-4",
            states=[],
            now_ms=self.NOW,
            vu=self.NOW + 3_600_000,
        )
        assert item["Put"]["Item"][schema.BUCKET_FIELD_VU] == {"N": str(self.NOW + 3_600_000)}


class TestSlowPathUsesOneInstant:
    async def test_rf_and_vu_derive_from_the_same_reading(self, repo):
        """The (rf, vu) pair must be consistent — #430's named failure mode."""
        readings: list[int] = []
        base = repo._now_ms()

        def fake_now_ms() -> int:
            readings.append(base + 60_000 * len(readings))
            return readings[-1]

        repo._now_ms = fake_now_ms

        sched = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),)
        await repo.set_limits(
            "slow-1", [Limit.per_minute("rpm", 100).with_schedule(sched)], resource="gpt-4"
        )
        limiter = RateLimiter(repository=repo, speculative_writes=False)
        async with limiter.acquire("slow-1", "gpt-4", consume={"rpm": 1}):
            pass

        item = await _raw_bucket_item(repo, "slow-1", "gpt-4", shard=0)
        rf = int(item[schema.BUCKET_FIELD_RF]["N"])
        vu = int(item[schema.BUCKET_FIELD_VU]["N"])
        assert rf in readings, f"rf {rf} is not one of the readings {readings}"
        assert vu > rf, "vu must be the next boundary after the instant rf records"
```

Add the raw-item helper to the test module if it is not already there:

```python
async def _raw_bucket_item(repo, entity_id, resource, shard=0):
    """Read a bucket item straight from DynamoDB, bypassing deserialisation."""
    client = await repo._get_client()
    response = await client.get_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, resource, shard)},
            "SK": {"S": schema.sk_state()},
        },
    )
    return response["Item"]
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `uv run pytest tests/unit/test_repository.py -k SlowPath -v`
Expected: FAIL with `TypeError: build_composite_normal() got an unexpected keyword argument 'vu'`

- [ ] **Step 3: Accept `vu` in both builders**

In `build_composite_normal`, add `vu: int | None = None` to the signature and, beside the existing `set_parts` handling:

```python
        if vu is not None:
            set_parts.append("#vu = :vu")
            attr_names["#vu"] = schema.BUCKET_FIELD_VU
            attr_values[":vu"] = {"N": str(vu)}
```

In `build_composite_create`, add the same parameter and:

```python
        if vu is not None:
            item[schema.BUCKET_FIELD_VU] = {"N": str(vu)}
```

- [ ] **Step 4: Compute `vu` in the lease**

In `lease.py`, where the per-bucket group is assembled around :376-393, compute the item-level boundary as the **minimum** across the group's declared limits — `vu` is one attribute per item, and the earliest change is what must force the next pass:

```python
                boundaries = [
                    b
                    for entry in group_entries
                    if (b := next_boundary(entry.limit.schedule, now_ms=now_ms)) is not None
                ]
                vu = min(boundaries) if boundaries else None
```

Pass `vu=vu` into `build_composite_normal`. The `now_ms` used here must be the same one already in scope for the refill computation — do not read the clock again.

- [ ] **Step 5: Run the test and watch it pass**

```bash
uv run pytest tests/unit/test_repository.py -k SlowPath -v
uv run pytest tests/unit/test_limiter.py -k ClockSeam -v
uv run pytest tests/unit/ -q
```

- [ ] **Step 6: Regenerate sync, lint, commit**

```bash
hatch run generate-sync
uv run ruff check --fix . && uv run ruff format . && uv run mypy
git add -A
git commit -m "$(cat <<'EOF'
✨ feat(lease): materialise tokens and the next boundary on the slow path

The slow path's single clock reading now drives effective_params, the
next_boundary that becomes `vu`, and the `rf` stamp — so an item never
carries an (rf, vu) pair from two different instants.

`vu` is the minimum boundary across the item's limits, because it is one
item-level attribute and the earliest change has to force the pass.

Refs #222, #430
EOF
)"
```

---

### Task 13: The fan-out stamps the schedule and forces one pass

**Files:**
- Modify: `src/zae_limiter/repository.py` (`_sync_bucket_params` :3080)
- Test: `tests/unit/test_repository.py`, `tests/benchmark/test_capacity.py`

**Interfaces:**
- Consumes: `encode` (Task 5), `schema.BUCKET_FIELD_VU` (Task 11)
- Produces: bucket items carrying `sched` / `sched_tz` / `b_{name}_sched`, and `vu = 0` after any limit change

**`vu = 0` is written on EVERY fan-out, not only when a schedule exists.** This is what lets
#222 subsume parked PR #469 completely rather than partially. #496 made `refill_bucket` clamp
on its early-return paths, but the speculative fast path is still a pure `ADD` with no cap
math — so after a `set_limits` capacity shrink a bucket can spend its surplus before any
refiller trims it. An unconditional `vu = 0` forces exactly one materialising pass, which
clamps. On an unscheduled bucket that pass computes `next_boundary(()) -> None` and removes
`vu` again, so it is self-clearing and leaves no residue. **Do not nest the `vu = 0` write
inside `if scheduled:`** — that was the plan's original shape and it would have left every
unscheduled entity exposed, which is most of them. Note also that `vu` must NOT appear in the
`else` branch's REMOVE list: `SET` and `REMOVE` on one attribute in a single
`UpdateExpression` is the `ValidationException` #488 hit.

**The fan-out must also decide #508: does `_sync_bucket_params` bump `rf`?** This is a
REQUIREMENT of this task, not a suggestion — do not implement Task 13 without resolving it,
and state which option you took and why.

The aggregator guards `try_refill_bucket` with an optimistic lock on the shared `rf`
timestamp. That lock exists so a refill computed from a stale stream image cannot be applied
after another writer has moved the bucket on. But `_sync_bucket_params` rewrites `cp`/`ra`/`rp`
on every shard **without touching `rf`**, so the lock cannot distinguish a stream image
captured before the fan-out from one captured after — and an aggregator invocation holding a
pre-shrink image passes the condition and refills toward the **old, larger** capacity. The
over-refill is transient (since #496, `refill_bucket` clamps on every path, so the next
materialising pass trims it), but in the interval the bucket admits above the limit
`set_limits` was called to impose.

PR #506 already closed the **same structural gap for a different symptom**: a stale image could
compute `vu` from a superseded schedule and push the boundary into the future, cancelling the
one materialising pass the `vu = 0` above exists to force. It fixed that by adding
`AND #sched = :expected_sched` to the aggregator's condition — but only on the re-stamp path.
`cp`/`ra` remains unguarded, which is #508.

Two options:

1. **Pin per-limit `cp` in the aggregator's condition**, mirroring how #506 pinned `#sched`.
   Narrow, local to `processor.py`, and closes one attribute.
2. **Have the fan-out bump `rf`.** Closes the whole class — every attribute the fan-out writes,
   including ones added later — with the lock that already exists. But `rf` is the refill
   clock, so moving it forfeits the refill accrued since the last stamp, and that interacts
   with the `vu = 0` pass above: think it through rather than applying it reflexively.

Option 2 is the broader fix and this task owns the function, which is why the decision sits
here rather than in the aggregator. Whichever you choose, add a test that fails against the
current behaviour — a stale pre-shrink image must not be able to refill toward the old
capacity.

**`vu = 0`, not a computed boundary.** Computing it would leave `vu` in the future while `tk` still holds a surplus over the new cap, so the fast path would admit against it until natural refill caught up — the exact burst #469 existed to prevent. `vu = 0` forces one materialising pass that trims. Cost at 10k active buckets is one failed conditional plus one slow pass each, ≈ $0.014 per admin operation.

- [ ] **Step 1: Write the failing test**

```python
class TestFanOutStampsSchedule:
    SCHED = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),)

    async def test_set_limits_stamps_sched_and_expires_vu(self, repo):
        await repo.create_entity("fan-1", parent_id=None, name="fan-1")
        await repo.set_limits("fan-1", [Limit.per_minute("rpm", 1000)], resource="gpt-4")
        await repo.speculative_consume("fan-1", "gpt-4", {"rpm": 1})

        await repo.set_limits(
            "fan-1",
            [Limit.per_minute("rpm", 1000).with_schedule(self.SCHED)],
            resource="gpt-4",
        )

        item = await _raw_bucket_item(repo, "fan-1", "gpt-4", shard=0)
        assert item["sched"]["S"] == "h9-17w1-5s500"
        assert item["sched_tz"]["S"] == "America/New_York"
        assert item[schema.BUCKET_FIELD_VU]["N"] == "0"

    async def test_removing_a_schedule_removes_sched_but_still_expires_vu(self, repo):
        """The two stamps are not removed together, and must not be conflated.

        `sched`/`sched_tz` go away with the schedule. `vu` does **not**: it is
        SET to 0 on every fan-out, scheduled or not, and self-clears on the
        next materialising pass — which has not run yet at this assertion.
        """
        await repo.create_entity("fan-2", parent_id=None, name="fan-2")
        await repo.set_limits(
            "fan-2",
            [Limit.per_minute("rpm", 1000).with_schedule(self.SCHED)],
            resource="gpt-4",
        )
        await repo.speculative_consume("fan-2", "gpt-4", {"rpm": 1})

        await repo.set_limits("fan-2", [Limit.per_minute("rpm", 1000)], resource="gpt-4")

        item = await _raw_bucket_item(repo, "fan-2", "gpt-4", shard=0)
        assert "sched" not in item
        assert "sched_tz" not in item
        assert item[schema.BUCKET_FIELD_VU]["N"] == "0"

    async def test_a_never_scheduled_fan_out_still_expires_vu(self, repo):
        """The case the unconditional write exists for, and the common one.

        The test above also lands on the `else` branch, but by *removing* a
        schedule. This one never had a schedule at all and is shrinking a
        capacity — literally #469's scenario, and the shape most `set_limits`
        calls in production take. Nesting `vu = 0` back inside `if scheduled:`
        leaves this bucket free to spend its surplus over the lowered ceiling
        before any refiller trims it.
        """
        await repo.create_entity("fan-4", parent_id=None, name="fan-4")
        await repo.set_limits("fan-4", [Limit.per_minute("rpm", 1000)], resource="gpt-4")
        await repo.speculative_consume("fan-4", "gpt-4", {"rpm": 1})

        await repo.set_limits("fan-4", [Limit.per_minute("rpm", 10)], resource="gpt-4")

        item = await _raw_bucket_item(repo, "fan-4", "gpt-4", shard=0)
        assert "sched" not in item
        assert item[schema.BUCKET_FIELD_VU]["N"] == "0"

    async def test_base_params_stay_undivided_and_unscaled(self, repo):
        """The schedule never rewrites cp/ra — it applies on top (§2.1)."""
        await repo.create_entity("fan-3", parent_id=None, name="fan-3")
        await repo.set_limits("fan-3", [Limit.per_minute("rpm", 1000)], resource="gpt-4")
        await repo.speculative_consume("fan-3", "gpt-4", {"rpm": 1})

        await repo.set_limits(
            "fan-3",
            [Limit.per_minute("rpm", 1000).with_schedule(self.SCHED)],
            resource="gpt-4",
        )

        item = await _raw_bucket_item(repo, "fan-3", "gpt-4", shard=0)
        assert item[schema.bucket_attr("rpm", schema.BUCKET_FIELD_CP)]["N"] == "1000000"
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `uv run pytest tests/unit/test_repository.py -k FanOutStampsSchedule -v`
Expected: FAIL — `KeyError: 'sched'` on the scheduled tests, and `KeyError: 'vu'` on
`test_a_never_scheduled_fan_out_still_expires_vu`. Watch for that second one
specifically: it is the assertion that fails again if `vu = 0` is ever moved back
inside `if scheduled:`.

- [ ] **Step 3: Implement**

In `_sync_bucket_params`, after the `cp`/`ra`/`rp` loop and before the TTL handling:

Note `vu = 0` is appended **outside** the `if scheduled:` block, after it:

```python
        scheduled = [limit for limit in limits if limit.schedule]
        if scheduled:
            # One item-level default plus per-limit overrides only where a
            # limit differs (§4.1) — schedules may differ per limit but
            # sharing one is the normal case.
            encodings = {limit.name: schedule.encode(limit.schedule) for limit in scheduled}
            default_compact, default_tz = next(iter(encodings.values()))
            set_parts.append("#sched = :sched")
            expr_names["#sched"] = schema.BUCKET_FIELD_SCHED
            expr_values[":sched"] = {"S": default_compact}
            set_parts.append("#sched_tz = :sched_tz")
            expr_names["#sched_tz"] = schema.BUCKET_FIELD_SCHED_TZ
            expr_values[":sched_tz"] = {"S": default_tz or "UTC"}
            for i, (name, (compact, _tz)) in enumerate(encodings.items()):
                if compact == default_compact:
                    continue
                alias = f"#lsched{i}"
                set_parts.append(f"{alias} = :lsched{i}")
                expr_names[alias] = schema.bucket_attr(name, schema.BUCKET_FIELD_SCHED)
                expr_values[f":lsched{i}"] = {"S": compact}
        else:
            for alias, attr in (
                ("#sched", schema.BUCKET_FIELD_SCHED),
                ("#sched_tz", schema.BUCKET_FIELD_SCHED_TZ),
            ):
                expr_names[alias] = attr
                remove_parts.append(alias)
            for i, limit in enumerate(limits):
                alias = f"#lsched{i}"
                expr_names[alias] = schema.bucket_attr(limit.name, schema.BUCKET_FIELD_SCHED)
                remove_parts.append(alias)

        # Outside both branches, so it runs on EVERY fan-out: force exactly one
        # materialising pass, which trims any surplus over a lowered ceiling
        # before the fast path can spend it. Note `#vu` is SET here and must
        # therefore never be added to `remove_parts` above (#488).
        set_parts.append("#vu = :vu_zero")
        expr_names["#vu"] = schema.BUCKET_FIELD_VU
        expr_values[":vu_zero"] = {"N": "0"}
```

Add the two bucket-field constants to `schema.py` beside `BUCKET_FIELD_VU`:

```python
BUCKET_FIELD_SCHED = "sched"  # item-level default schedule (§4.1)
BUCKET_FIELD_SCHED_TZ = "sched_tz"  # IANA name, hoisted out of every entry
```

- [ ] **Step 4: Add the capacity assertion — the load-bearing claim**

This belongs in `tests/benchmark/test_capacity.py`, which is **sync and moto-backed**: the `capacity_counter` fixture (`tests/fixtures/capacity.py:158`) counts at the boto3 sync client level and requires `sync_limiter`. It has no `.rcu` / `.wcu` / `.reset()` — the API is `with capacity_counter.counting():` and the per-operation counters.

```python
    def test_future_vu_costs_no_reads(self, sync_limiter, capacity_counter):
        """A scheduled bucket on the fast path must read no config (§2.1).

        This is the load-bearing claim of the whole design: `vu` exists so the
        speculative condition can gate on a schedule without evaluating one.
        """
        limits = [
            Limit.per_minute("rpm", 1_000_000).with_schedule(
                (ScheduleEntry(cron="* * * * *", tz="UTC", scale=0.5),)
            )
        ]
        # Warm the bucket, the entity cache and the config cache.
        with sync_limiter.acquire("vu-cap", "api", limits=limits, consume={"rpm": 1}):
            pass

        with capacity_counter.counting():
            with sync_limiter.acquire("vu-cap", "api", limits=limits, consume={"rpm": 1}):
                pass

        assert capacity_counter.get_item == 0, "fast path must not read config"
        assert capacity_counter.batch_get_item == [], "fast path must not batch-read"
        assert capacity_counter.query == 0, "fast path must not query"
        assert capacity_counter.update_item == 1, "one conditional UpdateItem, as unscheduled"
```

- [ ] **Step 5: Run both, then the suite**

```bash
uv run pytest tests/unit/test_repository.py -k FanOutStampsSchedule -v
uv run pytest tests/benchmark/test_capacity.py -k future_vu -o "addopts=" -v
uv run pytest tests/unit/ -q
```

Benchmarks disable xdist with `-o "addopts="`; that is safe **here** because `tests/benchmark/` contains no gevent tests. Never do it on `tests/unit/`.

- [ ] **Step 6: Regenerate sync, lint, commit**

```bash
hatch run generate-sync
uv run ruff check --fix . && uv run ruff format . && uv run mypy
git add -A
git commit -m "$(cat <<'EOF'
✨ feat(repository): stamp schedules onto buckets and force one pass

The #468 fan-out now carries `sched`/`sched_tz` (plus per-limit
overrides where a limit differs) to every shard, and sets `vu = 0` on
every fan-out — scheduled or not, since an unscheduled capacity shrink
needs the forced materialising pass just as much.

vu = 0 rather than a computed boundary: a future `vu` would leave the
fast path admitting against a surplus over a lowered ceiling until
natural refill caught up, which is the burst #469 existed to prevent.

Refs #222, #468, #469
EOF
)"
```

---

### Task 14: Aggregator schedule awareness and Lambda packaging

**Files:**
- Modify: `src/zae_limiter_aggregator/processor.py` (`LimitRefillInfo` :100, `BucketRefillState` :111, `ParsedBucketLimit` :283, `ParsedBucketRecord` :294, `_parse_bucket_record` :306, `try_refill_bucket` :547), `src/zae_limiter/infra/lambda_builder.py` (:142-151), `src/zae_limiter/infra/provisioner_builder.py` (:131-139)
- Test: `tests/unit/test_aggregator_processor.py`, `tests/unit/test_lambda_builder.py`, `tests/unit/test_provisioner_builder.py`

**Interfaces:**
- Consumes: `effective_params`, `next_boundary`, `decode` (Tasks 3, 4, 5)
- Produces: `BucketRefillState.sched`; schedule-aware refill; `vu` re-stamping

**`BucketRefillState` requires `namespace_id`** (`processor.py:118`) — it has no default, and `try_refill_bucket` uses it to build the key. `LimitRefillInfo`'s field order is `tc_delta, tk_milli, cp_milli, ra_milli, rp_ms`.

**`table` is a boto3 Table resource, not a client**, so `ExpressionAttributeValues` are plain ints (`processor.py:588`), not `{"N": "..."}`.

**Packaging.** `lambda_builder.py` copies `schema.py` (:142), `bucket.py` (:145), `models.py` (:148), `exceptions.py` (:151). `provisioner_builder.py` copies `schema.py` (:131), `models.py` (:134), `exceptions.py` (:137) and **does not vendor `bucket.py`**. `schedule.py` must be added to **both**, because Task 9 makes `models.py` import it and an unvendored import is an `ImportError` at cold start. `cronsim` and `tzdata` come from the `[lambda]` extra (Task 1).

- [ ] **Step 1: Write the failing test**

```python
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from zae_limiter.schedule import ScheduleEntry
from zae_limiter_aggregator.processor import (
    BucketRefillState,
    LimitRefillInfo,
    try_refill_bucket,
)

NY = ZoneInfo("America/New_York")
TUE_1400 = int(datetime(2026, 9, 15, 14, 0, tzinfo=NY).timestamp() * 1000)
BUSINESS = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),)


def _state(**kwargs) -> BucketRefillState:
    base = dict(
        namespace_id="ns123",
        entity_id="user-1",
        resource="gpt-4",
        rf_ms=TUE_1400 - 60_000,
        limits={
            "rpm": LimitRefillInfo(
                tc_delta=600_000,
                tk_milli=0,
                cp_milli=1_000_000,
                ra_milli=1_000_000,
                rp_ms=60_000,
            )
        },
    )
    base.update(kwargs)
    return BucketRefillState(**base)


class TestAggregatorRespectsSchedules:
    def test_refills_at_the_scheduled_rate_not_the_base(self):
        """During a 0.5x window the aggregator tops up toward the scheduled
        ceiling. One minute at the halved rate is 500_000 millitokens, and the
        600_000 consumption estimate keeps it under the skip threshold."""
        table = MagicMock()
        assert try_refill_bucket(table, _state(sched=BUSINESS), now_ms=TUE_1400) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":rd_rpm"] == 500_000

    def test_unscheduled_same_bucket_is_skipped_entirely(self):
        """Discriminates the test above: at the base rate a full minute yields
        1_000_000, which already covers the 600_000 estimate, so no write."""
        table = MagicMock()
        assert try_refill_bucket(table, _state(), now_ms=TUE_1400) is False
        table.update_item.assert_not_called()

    def test_scheduled_ceiling_is_per_shard(self):
        table = MagicMock()
        state = _state(sched=BUSINESS, shard_count=2)
        assert try_refill_bucket(table, state, now_ms=TUE_1400) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":rd_rpm"] == 250_000  # (1_000_000*0.5)//2


class TestAggregatorRestampsVu:
    def test_expired_vu_is_replaced_with_the_next_boundary(self):
        table = MagicMock()
        state = _state(sched=BUSINESS, vu_ms=TUE_1400 - 1)
        assert try_refill_bucket(table, state, now_ms=TUE_1400) is True
        expr = table.update_item.call_args.kwargs["UpdateExpression"]
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert "vu = :new_vu" in expr
        assert values[":new_vu"] > TUE_1400

    def test_future_vu_is_left_alone(self):
        table = MagicMock()
        state = _state(sched=BUSINESS, vu_ms=TUE_1400 + 3_600_000)
        try_refill_bucket(table, state, now_ms=TUE_1400)
        assert ":new_vu" not in table.update_item.call_args.kwargs["ExpressionAttributeValues"]


class TestLambdaPackaging:
    def test_aggregator_package_vendors_schedule(self):
        from zae_limiter.infra.lambda_builder import build_lambda_package

        with patch("aws_lambda_builders.builder.LambdaBuilder") as mock_builder_cls:
            mock_builder_cls.return_value.build.side_effect = _mock_builder_build
            zip_bytes = build_lambda_package()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            assert "zae_limiter/schedule.py" in zf.namelist()

    def test_provisioner_package_vendors_schedule(self):
        from zae_limiter.infra.provisioner_builder import build_provisioner_package

        with patch("aws_lambda_builders.builder.LambdaBuilder") as mock_builder_cls:
            mock_builder_cls.return_value.build.side_effect = _mock_builder_build
            zip_bytes = build_provisioner_package()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            assert "zae_limiter/schedule.py" in zf.namelist()

    def test_lambda_extra_carries_cronsim_and_tzdata(self):
        from zae_limiter.infra.lambda_builder import _get_runtime_requirements

        reqs = _get_runtime_requirements()
        assert any(r.startswith("cronsim") for r in reqs)
        assert any(r.startswith("tzdata") for r in reqs)
```

The packaging tests go in the existing builder test modules, which already define `_mock_builder_build` and import `io`, `zipfile` and `patch` — reuse them rather than redefining.

- [ ] **Step 2: Run the tests and watch them fail**

```bash
uv run pytest tests/unit/test_aggregator_processor.py -k "Schedules or Restamps" -v
uv run pytest tests/unit/test_lambda_builder.py tests/unit/test_provisioner_builder.py -k schedule -v
```

Expected: `TypeError: BucketRefillState.__init__() got an unexpected keyword argument 'sched'`, and `AssertionError` on the two `namelist()` checks.

- [ ] **Step 3: Carry the schedule through the parser**

Add to `ParsedBucketRecord` and `BucketRefillState`:

```python
    sched: tuple[ScheduleEntry, ...] = ()
    vu_ms: int | None = None
```

In `_parse_bucket_record`, decode the item-level attributes from the NewImage:

```python
    sched_compact = new_image.get(BUCKET_FIELD_SCHED, {}).get("S")
    sched_tz = new_image.get(BUCKET_FIELD_SCHED_TZ, {}).get("S", "UTC")
    sched = decode(sched_compact, sched_tz) if sched_compact else ()
    vu_raw = new_image.get(BUCKET_FIELD_VU, {}).get("N")
    vu_ms = int(vu_raw) if vu_raw is not None else None
```

Carry both through `aggregate_bucket_states` into `BucketRefillState`.

- [ ] **Step 4: Make the refill schedule-aware**

In `try_refill_bucket`, replace the effective-limit computation (currently `info.cp_milli // state.shard_count` and the `ra` equivalent) with:

```python
        eff_cp, eff_ra, eff_rp = effective_params(
            info.cp_milli, info.ra_milli, info.rp_ms, state.sched, now_ms
        )
        effective_cp = eff_cp // state.shard_count
        effective_ra = eff_ra // state.shard_count
```

and pass `effective_cp` / `effective_ra` / `eff_rp` into `refill_bucket`. Then, beside the `SET rf = :new_rf` construction at :599:

```python
    if state.sched and state.vu_ms is not None and state.vu_ms <= now_ms:
        boundary = next_boundary(state.sched, now_ms=now_ms)
        if boundary is not None:
            update_expr = update_expr.replace("SET rf = :new_rf", "SET rf = :new_rf, vu = :new_vu")
            expr_values[":new_vu"] = boundary
```

Prefer building the SET clause from a list rather than string-replacing it if the surrounding code makes that clean — the replace above is shown for clarity about *what* changes, not as a style recommendation.

- [ ] **Step 5: Vendor `schedule.py` into both packages**

In `lambda_builder.py` after :151:

```python
        # schedule.py — cron evaluation for scheduled limits (#222). Required
        # because models.py imports effective_params from it.
        shutil.copy2(zae_limiter_path / "schedule.py", dest_zae_limiter / "schedule.py")
```

The same line in `provisioner_builder.py` after :137. Update both module docstrings, which enumerate the vendored stub.

- [ ] **Step 6: Run everything**

```bash
uv run pytest tests/unit/test_aggregator_processor.py -v
uv run pytest tests/unit/test_lambda_builder.py tests/unit/test_provisioner_builder.py -v
uv run pytest tests/unit/ -q
uv run pytest tests/unit/ -m gevent -n 0 -q
```

- [ ] **Step 7: Lint, type check, commit**

```bash
uv run ruff check --fix . && uv run ruff format . && uv run mypy
git add -A
git commit -m "$(cat <<'EOF'
✨ feat(aggregator): refill at the scheduled rate and re-stamp vu

The aggregator decodes the bucket's own `sched` — no config read — and
refills toward the scheduled ceiling at the scheduled rate. An expired
`vu` is replaced with the next boundary in the same rf-locked write.

schedule.py is vendored into both Lambda packages; models.py imports it,
so an unvendored copy is an ImportError at cold start.

Refs #222
EOF
)"
```

---

## Sequencing

Tasks 1–5 build `schedule.py` bottom-up and touch nothing else in the codebase — they are pure additions and can be reviewed as a unit.

Tasks 6–7 are the unconditional clamp. **They have no dependency on Tasks 1–5 and could go first.** They are the #469 replacement, they are independently valuable, and they carry the one behaviour change to existing code that this plan makes — so landing them early and separately keeps that change reviewable on its own rather than buried among schedule machinery.

Tasks 8–14 wire scheduling into `repository.py`, `limiter.py` and the aggregator. Each needs `hatch run generate-sync` and the regenerated files committed.

## Self-Review

**Spec coverage.** §3.1 → Tasks 1-2. §3.2 → Task 4. §3.3 → Tasks 6-7 (both halves: `bucket.py` *and* `processor.py`, which is the part that makes #469 genuinely unnecessary). §3.5 → Task 9. §4 → Task 5. §2.1 → Tasks 11-13. §1.1 validation → Task 1. Effective-params resolution (§2.1) → Task 3.

**Not covered here, by design:** §5 (surface), §6 (failure handling), §7 (`retry_after`), §3.6 (`reset_schedule`) — all in the surface plan. §5.2 (provisioner sync) is its own plan.

**Placeholder scan.** All fourteen tasks now carry complete code. The `...` fixture stubs are
gone: every test constructs what it needs, and the helpers they lean on (`_state`,
`_raw_bucket_item`, `_set_vu`) are written out where first used.

**Verified against the tree, not recalled.** Every file path and line number in Tasks 8-14 was
checked at `cc1ff1dc` (post-#430 merge). Six things the earlier draft had wrong:

1. **`BucketState` requires `entity_id`, `resource` and `limit_name`** (`models.py:476-478`).
   Every `BucketState(...)` in the old Tasks 9 and 10 omitted the first two and would have failed
   with `TypeError` before reaching an assertion. Tasks 9-10 now build through a `_state()` helper.
2. **`BucketRefillState` requires `namespace_id`** (`processor.py:118`). Task 7's three
   constructions omitted it — corrected in place even though Task 7 was nominally complete.
3. **Task 11 called `self._now_ms()` inside the condition build.** Post-#430 that would
   re-introduce the second clock reading the merge just removed and break
   `TestClockSeam::test_acquire_reads_the_clock_once`. It now uses the bound `now_ms` parameter,
   which `_speculative_consume_single` already takes (`repository.py:2552`).
4. **`capacity_counter` is a moto/sync fixture** requiring `sync_limiter`
   (`tests/fixtures/capacity.py:158`), with no `.rcu` / `.wcu` / `.reset()`. Task 13's assertion
   paired it with an async LocalStack repo and a non-existent API; it now lives in
   `tests/benchmark/test_capacity.py` using `with capacity_counter.counting():` and the real
   per-operation counters.
5. **The Lambda builders return `bytes`, not a path**, and are named `build_lambda_package` /
   `build_provisioner_package`. Task 14's packaging assertions now go through
   `zipfile.ZipFile(io.BytesIO(...)).namelist()`, matching `test_provisioner_builder.py:37`.
6. **Unit tests use the moto-backed `repo` fixture** (`tests/unit/test_repository.py:24`), not
   `test_repo` — that name is the LocalStack integration fixture.

**The #430 dividend is spent where it matters.** Tasks 11, 12 and 13 control time by patching
`Repository._now_ms`, reusing the `_counting_clock` shape from `TestClockSeam`
(`tests/unit/test_limiter.py:307`). No test in this plan sleeps or patches global `time`.

**Task 14's arithmetic is now verified rather than flagged.** The earlier draft warned that
`:rd_rpm == 500_000` assumed a threshold it had not checked. Traced through
`try_refill_bucket`: one minute at the halved rate yields exactly 500_000 millitokens, and the
600_000 `tc_delta` estimate keeps `projected >= consumption_estimate` false so the write
proceeds. The unscheduled control case yields 1_000_000, which *does* clear the threshold and is
skipped — so the pair genuinely discriminates rather than both passing for the same reason.

**Spec coverage.** §3.1 → Tasks 1-2. §3.2 → Task 4. §3.3 → Tasks 6-7 (both halves: `bucket.py`
*and* `processor.py`, which is what makes #469 unnecessary). §3.5 → Task 9. §4 → Tasks 5, 13.
§2.1 → Tasks 11-13. §1.1 validation → Tasks 1, 8. §1.6 override-not-merge → Task 8.
§1.4 storage layout → Tasks 8, 13.

**Not covered here, by design:** §5 (surface), §6 (failure handling), §7 (`retry_after`), §3.6
(`reset_schedule`) — all in the surface plan. §5.2 (provisioner sync) is its own plan, now
issue #481 / PR #485.

**Type consistency.** `effective_params(cp_milli, ra_milli, rp_ms, sched, now_ms) -> tuple[int,
int, int]` is consumed with that signature in Tasks 4, 9 and 14. `next_boundary(sched,
reset_sched=(), *, now_ms)` returns `None` only when there is no schedule, which Task 12 handles
by omitting `vu` rather than writing a null; the keyword-only `now_ms` stops a positional call
binding a timestamp to `reset_sched` once the surface plan passes two tuples. `encode` returns
`(compact, tz | None)` and `decode(compact, tz)` takes a non-optional `tz`, so Tasks 8 and 13
both supply `"UTC"` when `encode` returned `None`. `from_bucket_state(state, now_ms)` returns a
`Limit` already divided by `shard_count`, so Task 10 must not also call `per_shard`.

**One risk not covered by a test.** `parse_cron` is `lru_cache`d on `(cron, tz)` and
`ParsedCron` holds a `ZoneInfo`, which the stdlib caches itself — safe as written. If the cache
key is ever changed to a `ScheduleEntry`, `scale` being a float makes the key hashable but
imprecise. Key on strings only.
