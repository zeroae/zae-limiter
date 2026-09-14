# Scheduled Limits: Reset, Surface and Retries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add calendar-aligned quota reset, make schedules usable declaratively (YAML → CloudFormation → live buckets), and make `retry_after_seconds` honest across a boundary — so #471 and #473 can be closed alongside #469.

**Architecture:** `reset_schedule` is a second, **edge**-triggered tuple on `Limit` whose entries mean "when this window opens, set `tk` to the effective capacity"; it reuses `vu`, the materialisation pass and the negative-`ADD` shape the core plan already built. The declarative surface follows `Disabled` (ADR-125) exactly. `retry_after_seconds` becomes a piecewise walk across boundaries instead of a flat division.

**Tech Stack:** Python 3.11/3.12, `cronsim`, `zoneinfo`, PyYAML (manifest), CloudFormation custom resources, Click (CLI), pytest, LocalStack.

**Spec:** `docs/plans/2026-09-13-scheduled-limits-design.md` §§3.6, 5, 6, 7, 8.

**Depends on:** the evaluation-core plan (`schedule.py`, `vu`, materialisation, the unconditional clamp) and the provisioner plan (`bucket_sync.py`, which Task 8 extends). Neither can be skipped.

## Global Constraints

- **Standard 5-field cron at every boundary of the system** — API, YAML, CloudFormation, CLI display, audit events. The compact form is *purely* a storage encoding. A tolerant reader may accept compact on input (detection is trivial: standard cron always contains spaces, compact never does), but it is not the documented interface.
- **`reset_schedule` entries carry `cron` and `tz` only.** `scale`, `capacity`, `refill_amount` and `refill_period_seconds` must all be `None` — a reset overrides no parameters.
- **Reset is edge-triggered; `schedule` is level-triggered.** A `schedule` entry is active *while* it matches; a reset fires on the transition *into* matching. Conflating them makes a `0 0 * * *` reset depend on a request arriving inside that one minute.
- **Reset is to the shard's share**, `tk = effective_capacity_milli(now_ms)`. Resetting every shard to the undivided capacity multiplies the entity's quota by `shard_count`.
- **Never touch `tc`.** The total-consumed counter must stay monotonic; `.claude/rules/design-validation.md` exists because usage aggregation derives consumption from its deltas. This is what makes the native reset strictly safer than #471's `reset_bucket()`, which deleted the item and cleared `tc` with it.
- Sync codegen, lint rules, and the `pytest tests/unit/` gevent hazard are all as stated in the core plan's Global Constraints — they apply here unchanged.
- Feature branch off `main`; PRs via the `/pr` skill.

---

### Task 1: `reset_schedule` on `Limit`

**Files:** Modify `src/zae_limiter/schedule.py`, `src/zae_limiter/models.py` · Test `tests/unit/test_schedule.py`

**Interfaces:**
- Consumes: `ScheduleEntry`, `parse_cron` (core plan Task 1)
- Produces: `Limit.reset_schedule: tuple[ScheduleEntry, ...] = ()`; `ScheduleEntry.as_reset()` validation path

- [ ] **Step 1: Write the failing test**

```python
class TestResetScheduleValidation:
    def test_reset_entry_carries_cron_and_tz_only(self):
        e = ScheduleEntry(cron="0 0 * * *", tz="America/New_York")
        assert Limit.per_day("rpd", 10_000).with_reset_schedule((e,)).reset_schedule == (e,)

    @pytest.mark.parametrize("kwargs", [
        {"scale": 0.5}, {"capacity": 100},
        {"refill_amount": 10}, {"refill_period_seconds": 30},
    ])
    def test_rejects_a_reset_entry_carrying_a_modifier(self, kwargs):
        """A reset overrides no parameters; a modifier on one is a category error."""
        e = ScheduleEntry(cron="0 0 * * *", **kwargs)
        with pytest.raises(ValueError, match="reset"):
            Limit.per_day("rpd", 10_000).with_reset_schedule((e,))

    def test_a_bare_schedule_entry_is_invalid_for_the_params_tuple(self):
        """The same entry is legal as a reset and illegal as a param override."""
        with pytest.raises(ValueError, match="exactly one"):
            ScheduleEntry(cron="0 0 * * *")
```

That last test exposes the design tension to resolve in Step 3: `ScheduleEntry.__post_init__`
requires exactly one modifier, but a reset entry has none. Resolve it by giving `ScheduleEntry`
a private `_reset: bool = False` field that `__post_init__` checks, and constructing reset
entries through a classmethod:

```python
    @classmethod
    def reset(cls, cron: str, tz: str = "UTC") -> "ScheduleEntry":
        """An entry that resets the balance rather than changing the params."""
        return cls(cron=cron, tz=tz, _reset=True)
```

Update the tests above to use `ScheduleEntry.reset(...)` once that exists, keeping
`test_rejects_a_reset_entry_carrying_a_modifier` pointed at a *param* entry passed to
`with_reset_schedule`.

- [ ] **Step 2: Run and watch it fail** — `AttributeError: 'Limit' object has no attribute 'reset_schedule'`

- [ ] **Step 3: Implement.** Add `_reset: bool = False` to `ScheduleEntry` with the classmethod above; `__post_init__` requires exactly one modifier when `_reset` is False and **no** modifier when it is True. Add `reset_schedule` and `with_reset_schedule()` to `Limit`, validating that every entry has `_reset=True`.

Also add `Limit.per_day(name, rate, burst=None)` if it does not exist — a daily quota is the motivating case and `per_hour` already establishes the pattern.

- [ ] **Step 4: Run, regenerate sync, commit** — `✨ feat(models): add reset_schedule for calendar quota reset`

---

### Task 2: `prev_reset_edge` and boundaries across both tuples

**Files:** Modify `src/zae_limiter/schedule.py` · Test `tests/unit/test_schedule_boundary.py`

**Interfaces:**
- Consumes: `matches`, `parse_cron`, `next_boundary` (core plan Task 4)
- Produces: `prev_reset_edge(reset_sched, now_ms) -> int | None`; `next_boundary` starts *honouring* the `reset_sched` tuple it already accepts (core plan Task 4) — no signature change

**Detection is backwards.** The materialising pass asks "was there a rising edge since this item was last refilled?" — `prev_reset_edge(reset_sched, now) > rf`. That makes idle buckets correct for free: a bucket idle from 18:00 to 09:00 has `vu` at midnight, and the 09:00 pass sees the missed edge and applies the reset then.

- [ ] **Step 1: Write the failing test**

```python
DAILY = (ScheduleEntry.reset(cron="0 0 * * *", tz="America/New_York"),)


class TestPrevResetEdge:
    def test_finds_the_most_recent_midnight(self):
        assert _iso(prev_reset_edge(DAILY, _ms("2026-09-15 09:00"))).startswith("2026-09-15T00:00")

    def test_an_idle_bucket_sees_the_missed_edge(self):
        """Idle 18:00 -> 09:00: the edge is in the past and must still be found."""
        rf = _ms("2026-09-14 18:00")
        assert prev_reset_edge(DAILY, _ms("2026-09-15 09:00")) > rf

    def test_two_missed_edges_report_only_the_latest(self):
        """Applying a reset is idempotent, so one edge is enough."""
        assert _iso(prev_reset_edge(DAILY, _ms("2026-09-16 09:00"))).startswith("2026-09-16T00:00")

    def test_no_edge_since_the_last_refill(self):
        rf = _ms("2026-09-15 01:00")
        assert prev_reset_edge(DAILY, _ms("2026-09-15 09:00")) <= rf

    def test_a_never_matching_expression_resets_nothing(self):
        never = (ScheduleEntry.reset(cron="0 0 30 2 *"),)   # February 30th
        assert prev_reset_edge(never, _ms("2026-09-15 09:00")) is None

    def test_empty_reset_schedule(self):
        assert prev_reset_edge((), _ms("2026-09-15 09:00")) is None


class TestNextBoundarySpansBothTuples:
    def test_a_reset_edge_is_a_boundary(self):
        params = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),)
        # From 22:00 the next param change is 09:00, but the reset fires at 00:00.
        assert _iso(next_boundary(params, DAILY, now_ms=_ms("2026-09-15 22:00"))).startswith(
            "2026-09-16T00:00"
        )

    def test_reset_only_schedule_still_produces_boundaries(self):
        assert _iso(next_boundary((), DAILY, now_ms=_ms("2026-09-15 09:00"))).startswith(
            "2026-09-16T00:00"
        )

    def test_neither_tuple_means_no_boundary(self):
        assert next_boundary((), (), now_ms=_ms("2026-09-15 09:00")) is None
```

- [ ] **Step 2: Run and watch it fail** — `ImportError: cannot import name 'prev_reset_edge'`

- [ ] **Step 3: Implement.** `prev_reset_edge` scans **backwards** from `now_ms` at the same adaptive granularity and caps as `next_boundary`, looking for the most recent transition from non-matching to matching. No edge within the cap means the expression never matches (`0 0 30 2 *`) — return `None`, which resets nothing.

`next_boundary` needs **no signature change** — core plan Task 4 already accepts `reset_sched`; it simply ignored it. Make the forward scan return the earliest instant at which *either* the active param entry changes *or* a reset edge fires. The existing `lease.py` and `processor.py` call sites keep working untouched, because they pass `sched` positionally and `now_ms=` by keyword; that is the whole reason Task 4 took the parameter early.

- [ ] **Step 4: Run, commit** — `✨ feat(models): detect reset edges and fold them into vu`

---

### Task 3: Apply the reset in the materialising pass

> **Expand before picking this up.** The steps below carry real assertions but compress
> the TDD cycle, and their fixture setup depends on `schedule.py`, which does not exist
> yet. Write the full failing-test/implement/pass cycle against the real signatures once
> the core plan has landed — writing it against invented ones now is the mistake this
> plan's own review calls out.

**Files:** Modify `src/zae_limiter/lease.py`, `src/zae_limiter/repository.py`, `src/zae_limiter_aggregator/processor.py` · Test `tests/unit/test_lease.py`, `tests/unit/test_aggregator_processor.py`

**Ordering:** compute effective params first, then set `tk` to the resulting effective capacity.

- [ ] **Step 1: Write the failing test**

```python
class TestResetMaterialisation:
    def test_reset_sets_tokens_to_the_effective_capacity(self):
        """Burn the quota, cross midnight, get the whole thing back at once."""
        ...  # state with tk=0, cp=10_000_000, rf just before midnight; now just after
        assert new_tokens_milli == 10_000_000

    def test_reset_uses_the_shards_share(self):
        """Resetting to the undivided capacity multiplies the quota by shard_count."""
        ...  # shard_count=4, cp=10_000_000
        assert new_tokens_milli == 2_500_000

    def test_reset_respects_a_concurrent_param_schedule(self):
        """A reset landing inside a 0.5x window restores half — the current limit,
        not the base. This is the intended reading; see 9."""
        ...
        assert new_tokens_milli == 5_000_000

    def test_reset_does_not_touch_tc(self):
        """The total-consumed counter must stay monotonic (design-validation.md)."""
        ...
        assert "tc" not in _written_attributes()

    def test_no_edge_means_an_ordinary_refill(self):
        ...


class TestAggregatorReset:
    def test_expresses_the_reset_as_a_negative_or_positive_add(self):
        """ADD (eff_cp - tk_observed) — the same delta shape as the clamp."""
        ...
        assert values[":rd_rpd"] == 10_000_000 - 2_000_000
```

- [ ] **Step 2: Run and watch them fail**

- [ ] **Step 3: Implement.** In `lease.py`'s slow-path refill, before computing `refill_amounts`: if `prev_reset_edge(limit.reset_schedule, now_ms)` is not `None` and is `> entry._original_rf_ms`, set the target token count to `state.effective_capacity_milli(now_ms)` instead of the incremental refill result. Because `refill_amounts` is a *delta* (`lease.py:384`), this flows through the existing `ADD` untouched — do not add a separate write.

In `processor.py`'s `try_refill_bucket`, apply the same check per limit and use `eff_cp - info.tk_milli` as the delta. It is safe as an `ADD` for the identical commutativity reason the clamp is.

- [ ] **Step 4: Run, regenerate sync, commit** — `✨ feat(limiter): reset the balance at a calendar edge`

---

### Task 4: Reset encoding

> **Expand before picking this up.** The steps below carry real assertions but compress
> the TDD cycle, and their fixture setup depends on `schedule.py`, which does not exist
> yet. Write the full failing-test/implement/pass cycle against the real signatures once
> the core plan has landed — writing it against invented ones now is the mistake this
> plan's own review calls out.

**Files:** Modify `src/zae_limiter/schedule.py`, `src/zae_limiter/repository.py` · Test `tests/unit/test_schedule_encoding.py`

Reset entries encode into their own `rsched` / `b_{name}_rsched` attributes with the same grammar minus the modifier token — `0 0 * * *` is `m0h0`, four bytes. A separate attribute rather than a tag inside `sched` mirrors the separate tuple and keeps the decoder from partitioning one list into two meanings.

- [ ] **Step 1: Write the failing test**

```python
class TestResetEncoding:
    def test_encodes_without_a_modifier_token(self):
        compact, tz = encode_reset((ScheduleEntry.reset("0 0 * * *", "America/New_York"),))
        assert compact == "m0h0"
        assert tz == "America/New_York"

    def test_round_trips(self):
        entries = (ScheduleEntry.reset("0 0 * * *", "America/New_York"),
                   ScheduleEntry.reset("0 12 * * SUN", "America/New_York"))
        compact, tz = encode_reset(entries)
        assert encode_reset(decode_reset(compact, tz)) == (compact, tz)

    def test_is_tiny(self):
        compact, _ = encode_reset((ScheduleEntry.reset("0 0 * * *"),))
        assert len(compact) <= 8
```

- [ ] **Step 2–4:** Implement `encode_reset` / `decode_reset` reusing the field encoder; stamp `rsched`/`rsched` overrides in `_sync_bucket_params` and `build_composite_create` beside `sched`; commit — `✨ feat(schema): encode reset schedules onto bucket items`

---

### Task 5: Boundary-aware `retry_after_seconds`

> **Expand before picking this up.** The steps below carry real assertions but compress
> the TDD cycle, and their fixture setup depends on `schedule.py`, which does not exist
> yet. Write the full failing-test/implement/pass cycle against the real signatures once
> the core plan has landed — writing it against invented ones now is the mistake this
> plan's own review calls out.

**Files:** Modify `src/zae_limiter/schedule.py`, `src/zae_limiter/bucket.py`, `src/zae_limiter/lease.py`, `src/zae_limiter/limiter.py` · Test `tests/unit/test_bucket.py`, `tests/unit/test_limiter.py`

**The flat estimate is wrong in the direction that matters.** It over-reports when a boundary raises the limit and **under**-reports when one lowers it — and lowering is the headline use case. Worked example from the spec: empty bucket, 500 tokens needed, 1000/min now, boundary in 10 s dropping to 500/min. Flat estimate **30 s**; real wait **50 s** (10 s yielding 167 tokens, then 333 remaining at half rate).

**A reset edge dominates.** If a reset boundary falls before the deficit clears by refill, that instant *is* the answer. For a daily quota this is the difference between reporting hours of drip-refill and reporting "at midnight" — the only useful answer. It is also the sharpest case for converting the **query** surface along with the rejection path: with only the latter converted, `acquire()` would say "at midnight" while `check_availability()` said "in eleven hours" about the same bucket at the same instant. (#473 was adopted and landed, not closed — see the design's §0.)

- [ ] **Step 1: Write the failing test**

```python
class TestBoundaryAwareRetryAfter:
    def test_under_reports_without_the_walk(self):
        """The spec's worked example: flat says 30s, the truth is 50s."""
        got = retry_after_with_schedule(
            deficit_milli=500_000, cp_milli=1_000_000, ra_milli=1_000_000, rp_ms=60_000,
            sched=(ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),),
            reset_sched=(), now_ms=_ms("2026-09-15 08:59:50"),
        )
        assert 49 <= got <= 51

    def test_over_reporting_case_shortens(self):
        """When a boundary raises the limit the real wait is shorter."""
        ...

    def test_a_reset_edge_dominates(self):
        """A daily quota's answer is 'at midnight', not hours of drip."""
        got = retry_after_with_schedule(
            deficit_milli=5_000_000, cp_milli=10_000_000, ra_milli=10_000_000,
            rp_ms=86_400_000, sched=(),
            reset_sched=(ScheduleEntry.reset("0 0 * * *", "America/New_York"),),
            now_ms=_ms("2026-09-15 23:00"),
        )
        assert 3500 <= got <= 3700     # ~1 hour, not ~12

    def test_falls_back_to_the_flat_estimate_past_the_walk_cap(self):
        ...

    def test_unscheduled_matches_the_existing_behaviour(self):
        """No schedule must produce exactly calculate_retry_after's answer."""
        ...
```

- [ ] **Step 2: Run and watch it fail**

- [ ] **Step 3: Implement** `retry_after_with_schedule(...)` in `schedule.py`: walk forward window by window using `next_boundary`, accumulating tokens at each window's effective rate until the deficit clears; if a reset edge falls inside the walk, return that instant directly; cap at 8 windows and fall back to the flat `calculate_retry_after`. Call it from **three** places: `lease.py`'s `_build_retry_failure_statuses` and `RateLimiter._admit_limit` (the two that build a `LimitStatus` on the rejection path), and `RateLimiter.check_availability()` — the non-consuming query, which builds `LimitStatus` too and which `available()` and `time_until_available()` are thin wrappers over. Miss the third and the number a user *sees* keeps the flat estimate.

  **`check_availability()` needs the effective capacity as well as the effective rate.** Two sites in it use the **base** `limit.capacity`: the clamp `min(total_across_shards, limit.capacity)` and the missing-bucket branch that reports `limit.capacity` outright. Inside a `scale: 0.5` window both over-report by 2x. Core plan Task 9 makes `calculate_available` schedule-aware inside `bucket.py`, and Task 10 fixes `Limit.from_bucket_state` on the rejection path — neither reaches these two, because they work from the `Limit` resolved out of *config*, not from a `BucketState`.

- [ ] **Step 4: Run, regenerate sync, commit** — `✨ feat(bucket): compute retry_after across schedule boundaries`

---

### Task 6: Manifest parsing

**Files:**
- Modify: `src/zae_limiter_provisioner/manifest.py` (`LimitDecl` at :9, `from_dict` :22, `to_dict` :34)
- Test: `tests/unit/test_provisioner_manifest.py`, `tests/unit/test_differ.py`

**Interfaces:**
- Consumes: `ScheduleEntry`, `parse_cron` (core plan Task 1)
- Produces: `LimitDecl.schedule` / `LimitDecl.reset_schedule`, carried through `to_dict()` into `Change.data`

**`differ.py` does not compare anything — do not write tests that assume it does.** Read
`compute_diff` (`differ.py:24-101`): it emits a `Change` for **every** manifest item
unconditionally, choosing only between `"create"` and `"update"` by whether the name appears in
the previous managed set. The `#PROVISIONER` record tracks *which items are managed*, never their
values, so there is no field-level comparison to teach about schedules. What a test can and
should pin is that `schedule` survives into `Change.data` via `to_dict()` — that is the actual
contract the applier depends on.

**`LimitDecl` is `@dataclass(frozen=True)`** and `to_dict()` is currently annotated
`-> dict[str, int]`. Adding schedules widens it to `dict[str, Any]`; mypy will catch the
annotation if you forget.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from zae_limiter_provisioner.manifest import LimitDecl, LimitsManifest

YAML = """
namespace: default
resources:
  gpt-4:
    limits:
      rpm:
        capacity: 1000
        schedule:
          - cron: "* 9-17 * * MON-FRI"
            tz: America/New_York
            scale: 0.5
          - cron: "* 0-6 * * *"
            tz: America/New_York
            capacity: 2000
      rpd:
        capacity: 10000
        refill_period: 86400
        reset_schedule:
          - cron: "0 0 * * *"
            tz: America/New_York
"""


class TestManifestSchedules:
    def test_parses_the_param_schedule(self):
        manifest = LimitsManifest.from_yaml(YAML)
        rpm = manifest.resources["gpt-4"].limits["rpm"]
        assert len(rpm.schedule) == 2
        assert rpm.schedule[0].cron == "* 9-17 * * MON-FRI"
        assert rpm.schedule[0].tz == "America/New_York"
        assert rpm.schedule[0].scale == 0.5
        assert rpm.schedule[1].capacity == 2000

    def test_parses_the_reset_schedule(self):
        manifest = LimitsManifest.from_yaml(YAML)
        rpd = manifest.resources["gpt-4"].limits["rpd"]
        assert rpd.reset_schedule[0].cron == "0 0 * * *"
        assert rpd.schedule == ()

    def test_absent_schedule_is_an_empty_tuple(self):
        manifest = LimitsManifest.from_yaml(
            "namespace: default\nresources:\n  gpt-4:\n"
            "    limits:\n      rpm:\n        capacity: 1000\n"
        )
        rpm = manifest.resources["gpt-4"].limits["rpm"]
        assert rpm.schedule == ()
        assert rpm.reset_schedule == ()

    def test_rejects_an_invalid_cron_at_parse_time(self):
        """A manifest that applies must be a manifest that evaluates, so
        `limits plan` surfaces this before anything is written."""
        with pytest.raises(ValueError, match="cron"):
            LimitsManifest.from_yaml(YAML.replace("* 9-17 * * MON-FRI", "* 99 * * *"))

    def test_rejects_an_unknown_timezone_at_parse_time(self):
        with pytest.raises(ValueError, match="timezone"):
            LimitsManifest.from_yaml(YAML.replace("America/New_York", "Mars/Olympus_Mons"))

    def test_rejects_extended_cron_tokens(self):
        """L/W/# parse without error in cronsim and would silently never
        fire — core plan Task 1 rejects them, and that must reach YAML."""
        with pytest.raises(ValueError, match="not supported"):
            LimitsManifest.from_yaml(YAML.replace('"0 0 * * *"', '"0 0 L * *"'))

    def test_rejects_a_reset_entry_carrying_a_modifier(self):
        """A reset changes the balance, not the parameters (§3.6)."""
        bad = YAML.replace(
            '          - cron: "0 0 * * *"\n            tz: America/New_York\n',
            '          - cron: "0 0 * * *"\n            tz: America/New_York\n'
            "            scale: 0.5\n",
        )
        with pytest.raises(ValueError, match="reset"):
            LimitsManifest.from_yaml(bad)


class TestScheduleSurvivesToChangeData:
    """`differ.py` compares nothing — it emits every manifest item every time
    (`compute_diff`, differ.py:24-101). What matters is that the schedule is
    carried in `Change.data` so the applier and the bucket fan-out can see it."""

    def test_to_dict_round_trips_the_schedule(self):
        decl = LimitDecl.from_dict(
            {
                "capacity": 1000,
                "schedule": [
                    {"cron": "* 9-17 * * MON-FRI", "tz": "America/New_York", "scale": 0.5}
                ],
            }
        )
        restored = LimitDecl.from_dict(decl.to_dict())
        assert restored == decl

    def test_change_data_carries_the_schedule(self):
        from zae_limiter_provisioner.differ import compute_diff

        manifest = LimitsManifest.from_yaml(YAML)
        changes = compute_diff(manifest, previous={})
        resource_change = next(c for c in changes if c.level == "resource")
        rpm = resource_change.data["limits"]["rpm"]
        assert rpm["schedule"][0]["cron"] == "* 9-17 * * MON-FRI"
        assert rpm["schedule"][0]["scale"] == 0.5

    def test_unscheduled_limits_carry_no_schedule_key(self):
        """Keep the wire shape minimal so an unscheduled manifest is unchanged."""
        from zae_limiter_provisioner.differ import compute_diff

        manifest = LimitsManifest.from_yaml(
            "namespace: default\nresources:\n  gpt-4:\n"
            "    limits:\n      rpm:\n        capacity: 1000\n"
        )
        changes = compute_diff(manifest, previous={})
        rpm = next(c for c in changes if c.level == "resource").data["limits"]["rpm"]
        assert "schedule" not in rpm
        assert "reset_schedule" not in rpm
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `uv run pytest tests/unit/test_provisioner_manifest.py -k "ManifestSchedules or ScheduleSurvives" -v`
Expected: FAIL with `AttributeError: 'LimitDecl' object has no attribute 'schedule'`

- [ ] **Step 3: Implement**

```python
@dataclass(frozen=True)
class LimitDecl:
    capacity: int
    refill_amount: int
    refill_period: int
    schedule: tuple[ScheduleEntry, ...] = ()
    reset_schedule: tuple[ScheduleEntry, ...] = ()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LimitDecl:
        capacity = d["capacity"]
        if "burst" in d:
            capacity = d["burst"]
        refill_amount = d.get("refill_amount", capacity)
        refill_period = d.get("refill_period", 60)
        for field_name, value in (
            ("capacity", capacity),
            ("refill_amount", refill_amount),
            ("refill_period", refill_period),
        ):
            if value <= 0:
                raise ValueError(f"{field_name} must be positive, got {value}")
        return cls(
            capacity=capacity,
            refill_amount=refill_amount,
            refill_period=refill_period,
            # ScheduleEntry validates cron, timezone and the modifier rules in
            # __post_init__, so an unusable manifest fails here rather than
            # inside the Lambda.
            schedule=tuple(ScheduleEntry(**entry) for entry in d.get("schedule", [])),
            reset_schedule=tuple(
                ScheduleEntry.reset(**entry) for entry in d.get("reset_schedule", [])
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "capacity": self.capacity,
            "refill_amount": self.refill_amount,
            "refill_period": self.refill_period,
        }
        if self.schedule:
            result["schedule"] = [_entry_to_dict(e) for e in self.schedule]
        if self.reset_schedule:
            result["reset_schedule"] = [
                {"cron": e.cron, "tz": e.tz} for e in self.reset_schedule
            ]
        return result
```

with a module-level helper that emits only the fields that are set, so `from_dict(to_dict(x)) == x`:

```python
def _entry_to_dict(entry: ScheduleEntry) -> dict[str, Any]:
    d: dict[str, Any] = {"cron": entry.cron, "tz": entry.tz}
    for name in ("scale", "capacity", "refill_amount", "refill_period_seconds"):
        value = getattr(entry, name)
        if value is not None:
            d[name] = value
    return d
```

The positivity validation above is the same rule issue #481's Task 6 adds; if that has already
landed, keep it and add only the two schedule fields.

`differ.py` needs **no change** — it passes `to_dict()` through into `Change.data` already.

- [ ] **Step 4: Run the test and watch it pass**

Run: `uv run pytest tests/unit/test_provisioner_manifest.py tests/unit/test_differ.py -v`
Expected: PASS

- [ ] **Step 5: Lint, type check, commit**

```bash
uv run ruff check --fix . && uv run ruff format . && uv run mypy
git add src/zae_limiter_provisioner/manifest.py tests/unit/test_provisioner_manifest.py
git commit -m "$(cat <<'EOF'
✨ feat(provisioner): parse schedules from the limits manifest

LimitDecl gains `schedule` and `reset_schedule`, parsed into
ScheduleEntry so cron, timezone and the reset-entry rules are validated
at parse time — `limits plan` now rejects an unusable manifest before
anything is written, rather than the Lambda raising at apply.

differ.py is unchanged: it compares nothing, emitting every manifest
item on every apply, so the only contract to pin is that the schedule
survives into Change.data via to_dict().

Refs #222
EOF
)"
```

---

### Task 7: CloudFormation round trip

**Files:**
- Modify: `src/zae_limiter/limits_cli.py` (`_limits_to_cfn` at :214, the tri-state `Disabled` emission at :177-195), `src/zae_limiter_provisioner/handler.py` (`_cfn_limits_to_manifest` at :299)
- Test: `tests/unit/test_limits_cli.py`, `tests/unit/test_provisioner_handler.py`

**Interfaces:**
- Consumes: `LimitDecl` schedule parsing (Task 6)
- Produces: `Schedule` / `ResetSchedule` properties on `Custom::ZaeLimiterLimits`

**The generator is a Click command over raw dicts, not a function over a manifest.**
`limits_cfn_template` (`limits_cli.py:153`) calls `_load_yaml(file_path)` and walks the raw
mapping — it never constructs a `LimitsManifest`. So the per-limit conversion belongs in
`_limits_to_cfn` (:214), which already emits `Capacity` / `RefillAmount` / `RefillPeriod` and
skips absent keys. The template is emitted as **YAML** via `click.echo(yaml.dump(...))` (:211),
so a test parses `result.output` with `yaml.safe_load`. The resource key is `TenantLimits`.

**Standard cron, never the compact form.** CloudFormation is user-facing IaC (§4).

- [ ] **Step 1: Write the failing test**

```python
import tempfile

import yaml
from click.testing import CliRunner

from zae_limiter.cli import cli


def _render(yaml_content: dict) -> dict:
    """Run `limits cfn-template` and parse the emitted template."""
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        yaml.dump(yaml_content, f)
        f.flush()
        result = CliRunner().invoke(
            cli, ["limits", "cfn-template", "--name", "test-app", "-f", f.name]
        )
    assert result.exit_code == 0, result.output
    return yaml.safe_load(result.output)


class TestCfnScheduleRoundTrip:
    MANIFEST = {
        "namespace": "test-ns",
        "resources": {
            "gpt-4": {
                "limits": {
                    "rpm": {
                        "capacity": 1000,
                        "schedule": [
                            {
                                "cron": "* 9-17 * * MON-FRI",
                                "tz": "America/New_York",
                                "scale": 0.5,
                            },
                            {
                                "cron": "* 0-6 * * *",
                                "tz": "America/New_York",
                                "capacity": 2000,
                            },
                        ],
                    },
                    "rpd": {
                        "capacity": 10000,
                        "refill_period": 86400,
                        "reset_schedule": [
                            {"cron": "0 0 * * *", "tz": "America/New_York"}
                        ],
                    },
                }
            }
        },
    }

    def _limits(self, template: dict) -> dict:
        props = template["Resources"]["TenantLimits"]["Properties"]
        return props["Resources"]["gpt-4"]["Limits"]

    def test_emits_schedule_in_pascal_case(self):
        limits = self._limits(_render(self.MANIFEST))
        assert limits["rpm"]["Schedule"] == [
            {"Cron": "* 9-17 * * MON-FRI", "Tz": "America/New_York", "Scale": 0.5},
            {"Cron": "* 0-6 * * *", "Tz": "America/New_York", "Capacity": 2000},
        ]

    def test_emits_reset_schedule(self):
        limits = self._limits(_render(self.MANIFEST))
        assert limits["rpd"]["ResetSchedule"] == [
            {"Cron": "0 0 * * *", "Tz": "America/New_York"}
        ]

    def test_omits_both_properties_when_absent(self):
        """Absent means inherit; an emitted empty list would not round-trip."""
        plain = {
            "namespace": "test-ns",
            "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 1000}}}},
        }
        limits = self._limits(_render(plain))
        assert "Schedule" not in limits["rpm"]
        assert "ResetSchedule" not in limits["rpm"]

    def test_cron_stays_standard_not_compact(self):
        """CloudFormation is user-facing IaC — the compact form is storage only."""
        limits = self._limits(_render(self.MANIFEST))
        assert limits["rpm"]["Schedule"][0]["Cron"] == "* 9-17 * * MON-FRI"
        assert "h9-17w1-5s500" not in yaml.dump(limits)


class TestCfnPropertiesBackToManifest:
    def test_schedule_survives_the_return_trip(self):
        from zae_limiter_provisioner.handler import _cfn_limits_to_manifest

        cfn = {
            "rpm": {
                "Capacity": 1000,
                "Schedule": [
                    {"Cron": "* 9-17 * * MON-FRI", "Tz": "America/New_York", "Scale": 0.5}
                ],
            }
        }
        assert _cfn_limits_to_manifest(cfn)["rpm"]["schedule"] == [
            {"cron": "* 9-17 * * MON-FRI", "tz": "America/New_York", "scale": 0.5}
        ]

    def test_reset_schedule_survives_the_return_trip(self):
        from zae_limiter_provisioner.handler import _cfn_limits_to_manifest

        cfn = {"rpd": {"Capacity": 10000, "ResetSchedule": [{"Cron": "0 0 * * *", "Tz": "UTC"}]}}
        assert _cfn_limits_to_manifest(cfn)["rpd"]["reset_schedule"] == [
            {"cron": "0 0 * * *", "tz": "UTC"}
        ]

    def test_absent_properties_stay_absent(self):
        from zae_limiter_provisioner.handler import _cfn_limits_to_manifest

        result = _cfn_limits_to_manifest({"rpm": {"Capacity": 1000}})["rpm"]
        assert "schedule" not in result
        assert "reset_schedule" not in result
```

- [ ] **Step 2: Run the tests and watch them fail**

```bash
uv run pytest tests/unit/test_limits_cli.py -k CfnSchedule -v
uv run pytest tests/unit/test_provisioner_handler.py -k CfnPropertiesBack -v
```

Expected: `KeyError: 'Schedule'` and `KeyError: 'schedule'`.

- [ ] **Step 3: Emit the properties**

In `limits_cli.py`, extend `_limits_to_cfn` (:214) — the same only-when-declared discipline the
`Disabled` tri-state uses at :177-195:

```python
_SCHEDULE_KEYS = (
    ("cron", "Cron"),
    ("tz", "Tz"),
    ("scale", "Scale"),
    ("capacity", "Capacity"),
    ("refill_amount", "RefillAmount"),
    ("refill_period_seconds", "RefillPeriodSeconds"),
)


def _schedule_to_cfn(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert manifest schedule entries to CFN PascalCase, omitting absent keys."""
    return [
        {pascal: entry[snake] for snake, pascal in _SCHEDULE_KEYS if snake in entry}
        for entry in entries
    ]
```

and inside `_limits_to_cfn`'s loop:

```python
        if "schedule" in limit:
            cfn_limit["Schedule"] = _schedule_to_cfn(limit["schedule"])
        if "reset_schedule" in limit:
            cfn_limit["ResetSchedule"] = _schedule_to_cfn(limit["reset_schedule"])
```

- [ ] **Step 4: Parse them back**

In `handler.py`, extend `_cfn_limits_to_manifest` (:299) with the inverse:

```python
        if "Schedule" in cfn_limit:
            limit["schedule"] = _cfn_schedule_to_manifest(cfn_limit["Schedule"])
        if "ResetSchedule" in cfn_limit:
            limit["reset_schedule"] = _cfn_schedule_to_manifest(cfn_limit["ResetSchedule"])
```

with the mirrored helper. Keep the key tables in one place if you can — a divergence between the
two directions is silent, and the round-trip tests above are the only thing that would catch it.

- [ ] **Step 5: Run both and watch them pass**

```bash
uv run pytest tests/unit/test_limits_cli.py tests/unit/test_provisioner_handler.py -v
```

- [ ] **Step 6: Lint, type check, commit**

```bash
uv run ruff check --fix . && uv run ruff format . && uv run mypy
git add -A
git commit -m "$(cat <<'EOF'
✨ feat(cli): round-trip schedules through CloudFormation

Schedule and ResetSchedule join Custom::ZaeLimiterLimits, emitted only
when declared — the same tri-state discipline ADR-125's Disabled uses,
because an emitted empty list would not round-trip as "inherit".

Standard 5-field cron in both directions: the compact form is a storage
encoding and never appears in user-facing IaC.

Refs #222
EOF
)"
```

---

### Task 8: The provisioner stamps schedules onto buckets

**Files:**
- Modify: `src/zae_limiter_provisioner/bucket_sync.py` (`build_bucket_param_update` at :67), `src/zae_limiter/infra/provisioner_builder.py` (:131-137)
- Test: `tests/unit/test_provisioner_bucket_sync.py`, `tests/unit/test_provisioner_builder.py`

**Interfaces:**
- Consumes: `LimitDecl.to_dict()` shape (Task 6), `encode` (core plan Task 5)
- Produces: `build_bucket_param_update` emitting `sched` / `sched_tz` / `rsched` and `vu = 0`

**This is where the provisioner plan and the core plan meet.** `build_bucket_param_update`
already exists on `fix/481-provisioner-bucket-sync` with the signature
`(limits, ttl_multiplier, stale_limit_names, now_ms)` returning
`(update_expr, expr_names, expr_values)` — read it before editing. Without PR #485's handler
wiring this does nothing, so that must land first.

**Core plan Task 14 already adds `schedule.py` to `provisioner_builder.py`.** If Task 14 has
landed, the vendoring test here is a regression guard rather than new work; if it has not, add
the `shutil.copy2` line. `bucket.py` is still not vendored into the provisioner and does not need
to be — this task encodes a schedule, it does not evaluate one.

**Note the incomplete `_default_` path.** `sync_bucket_params` queries
`BUCKET#{resource}#`, so an entity change targeting `_default_` matches zero real buckets and is
a silent no-op. That is tracked as **#487** and is deliberately not fixed here; a schedule set on
an entity's `_default_` config inherits the same gap until #487 lands.

- [ ] **Step 1: Write the failing test**

```python
class TestProvisionerStampsSchedules:
    SCHEDULED = {
        "rpm": {
            "capacity": 1000,
            "refill_amount": 1000,
            "refill_period": 60,
            "schedule": [
                {"cron": "* 9-17 * * MON-FRI", "tz": "America/New_York", "scale": 0.5}
            ],
        }
    }

    def _resolve(self, expr, names, values, alias):
        """Attribute name an alias points at, for asserting on SET/REMOVE."""
        return names[alias]

    def test_stamps_sched_and_tz_and_expires_vu(self):
        expr, names, values = build_bucket_param_update(
            self.SCHEDULED, ttl_multiplier=0, stale_limit_names=None, now_ms=0
        )
        assert values[":sched"] == {"S": "h9-17w1-5s500"}
        assert values[":sched_tz"] == {"S": "America/New_York"}
        assert values[":vu"] == {"N": "0"}
        assert "#sched = :sched" in expr
        assert "#vu = :vu" in expr

    def test_vu_is_zero_not_a_computed_boundary(self):
        """A future vu would leave the fast path spending a surplus over a
        lowered ceiling until natural refill caught up (§3.4)."""
        _expr, _names, values = build_bucket_param_update(
            self.SCHEDULED, ttl_multiplier=0, stale_limit_names=None, now_ms=1_789_000_000_000
        )
        assert values[":vu"] == {"N": "0"}

    def test_unscheduled_limits_remove_the_stamps(self):
        """Override, not merge: dropping a schedule must clear the item."""
        plain = {"rpm": {"capacity": 1000, "refill_amount": 1000, "refill_period": 60}}
        expr, names, values = build_bucket_param_update(
            plain, ttl_multiplier=0, stale_limit_names=None, now_ms=0
        )
        removed = {names[a.strip()] for a in expr.split("REMOVE")[1].split(",")}
        assert {"sched", "sched_tz", "vu"} <= removed
        assert ":sched" not in values

    def test_reset_schedule_stamps_rsched(self):
        decl = {
            "rpd": {
                "capacity": 10000,
                "refill_amount": 10000,
                "refill_period": 86400,
                "reset_schedule": [{"cron": "0 0 * * *", "tz": "America/New_York"}],
            }
        }
        _expr, _names, values = build_bucket_param_update(
            decl, ttl_multiplier=0, stale_limit_names=None, now_ms=0
        )
        assert values[":rsched"] == {"S": "m0h0"}

    def test_base_params_are_still_undivided_and_unscaled(self):
        """The schedule applies on top; cp/ra stay the base (§2.1)."""
        _expr, names, values = build_bucket_param_update(
            self.SCHEDULED, ttl_multiplier=0, stale_limit_names=None, now_ms=0
        )
        cp_alias = next(a for a, attr in names.items() if attr == bucket_attr("rpm", "cp"))
        assert values[f":{cp_alias[1:]}"] == {"N": "1000000"}


class TestProvisionerPackagingCarriesSchedule:
    def test_provisioner_package_vendors_schedule_py(self):
        """models.py imports effective_params from schedule.py, so an
        unvendored copy is an ImportError at cold start."""
        from zae_limiter.infra.provisioner_builder import build_provisioner_package

        with patch("aws_lambda_builders.builder.LambdaBuilder") as mock_builder_cls:
            mock_builder_cls.return_value.build.side_effect = _mock_builder_build
            zip_bytes = build_provisioner_package()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            assert "zae_limiter/schedule.py" in zf.namelist()
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `uv run pytest tests/unit/test_provisioner_bucket_sync.py -k StampsSchedules -v`
Expected: FAIL with `KeyError: ':sched'`

- [ ] **Step 3: Implement**

In `build_bucket_param_update`, after the existing `cp`/`ra`/`rp` loop:

```python
    scheduled = {
        name: decl["schedule"] for name, decl in limits.items() if decl.get("schedule")
    }
    reset = {
        name: decl["reset_schedule"]
        for name, decl in limits.items()
        if decl.get("reset_schedule")
    }

    if scheduled:
        entries = [ScheduleEntry(**e) for e in next(iter(scheduled.values()))]
        compact, tz = encode(tuple(entries))
        set_parts.append("#sched = :sched")
        expr_names["#sched"] = BUCKET_FIELD_SCHED
        expr_values[":sched"] = {"S": compact}
        set_parts.append("#sched_tz = :sched_tz")
        expr_names["#sched_tz"] = BUCKET_FIELD_SCHED_TZ
        expr_values[":sched_tz"] = {"S": tz or "UTC"}
    if reset:
        entries = [ScheduleEntry.reset(**e) for e in next(iter(reset.values()))]
        compact, _tz = encode_reset(tuple(entries))
        set_parts.append("#rsched = :rsched")
        expr_names["#rsched"] = BUCKET_FIELD_RSCHED
        expr_values[":rsched"] = {"S": compact}

    if scheduled or reset:
        # Force exactly one materialising pass, which trims any surplus over a
        # lowered ceiling before the fast path can spend it (§3.4).
        set_parts.append("#vu = :vu")
        expr_names["#vu"] = BUCKET_FIELD_VU
        expr_values[":vu"] = {"N": "0"}
    else:
        for alias, attr in (
            ("#sched", BUCKET_FIELD_SCHED),
            ("#sched_tz", BUCKET_FIELD_SCHED_TZ),
            ("#rsched", BUCKET_FIELD_RSCHED),
            ("#vu", BUCKET_FIELD_VU),
        ):
            expr_names[alias] = attr
            remove_parts.append(alias)
```

Per-limit overrides (`b_{name}_sched`) follow the same shape as core plan Task 13 — emit one only
where a limit's encoding differs from the item-level default. If every scheduled limit shares an
encoding, which is the normal case, the default alone is correct and smallest.

- [ ] **Step 4: Run the tests and watch them pass**

```bash
uv run pytest tests/unit/test_provisioner_bucket_sync.py tests/unit/test_provisioner_builder.py -v
```

- [ ] **Step 5: Lint, type check, commit**

```bash
uv run ruff check --fix . && uv run ruff format . && uv run mypy
git add -A
git commit -m "$(cat <<'EOF'
✨ feat(provisioner): stamp manifest schedules onto live buckets

build_bucket_param_update now carries sched/sched_tz/rsched and sets
vu = 0, so a manifest-applied schedule reaches the buckets that enforce
it rather than sitting in a config item nothing reads on the fast path.

vu = 0 rather than a computed boundary, for the same reason the async
fan-out uses it: a future vu leaves the fast path spending a surplus
over a lowered ceiling.

Refs #222, #481
EOF
)"
```

---

### Task 9: CLI display

**Files:**
- Modify: `src/zae_limiter/cli.py` (`_format_limit` at :2200, `resource_get_defaults` at :2339, `entity_get_limits` at :3345)
- Test: `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: `Limit.schedule` / `Limit.reset_schedule` (core plan Task 8, surface Task 1), `to_cron` (core plan Task 5)
- Produces: a `Schedule:` block and a `Reset:` line under each limit

**Display only.** Setting schedules stays API + manifest (§1.5); `-l` is **not** extended, and
API/CLI parity is satisfied by `zae-limiter limits apply` being the CLI path.

**Weekday and month render as names.** `to_cron` normalises, so an operator who typed `1-5` sees
`MON-FRI` back. That is the one visible consequence of canonical storage (§4.3) and the tests
below pin it deliberately rather than tolerating it.

**The existing tests mock the repository**, not DynamoDB: `@patch("zae_limiter.repository.Repository")`
with `mock_repo.get_limits = AsyncMock(return_value=[...])` and `Repository.open` as an
`AsyncMock` (`tests/unit/test_cli.py:4755-4782`). `get_entity_disabled` must also be stubbed or
the command raises after printing.

- [ ] **Step 1: Write the failing test**

```python
class TestScheduleDisplay:
    """`get-limits` renders schedules; it does not set them (§5.4)."""

    def _scheduled_limit(self):
        from zae_limiter.models import Limit
        from zae_limiter.schedule import ScheduleEntry

        return Limit.per_minute("rpm", 1000).with_schedule(
            (
                ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),
                ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", capacity=2000),
            )
        )

    def _invoke(self, mock_repo_class, runner, limits):
        mock_repo = Mock()
        mock_repo.get_limits = AsyncMock(return_value=limits)
        mock_repo.get_entity_disabled = AsyncMock(return_value=None)
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.return_value = mock_repo
        mock_repo_class.open = AsyncMock(return_value=mock_repo)
        return runner.invoke(cli, ["entity", "get-limits", "user-123", "-r", "gpt-4"])

    @patch("zae_limiter.repository.Repository")
    def test_renders_each_entry_as_canonical_cron(self, mock_repo_class, runner):
        result = self._invoke(mock_repo_class, runner, [self._scheduled_limit()])
        assert result.exit_code == 0
        assert "Schedule:" in result.output
        assert "* 9-17 * * MON-FRI" in result.output
        assert "America/New_York" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_renders_the_modifier_as_a_gloss(self, mock_repo_class, runner):
        result = self._invoke(mock_repo_class, runner, [self._scheduled_limit()])
        assert "50%" in result.output
        assert "capacity 2000" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_numeric_weekday_renders_as_names(self, mock_repo_class, runner):
        """The one visible normalisation of canonical storage (§4.3)."""
        from zae_limiter.models import Limit
        from zae_limiter.schedule import ScheduleEntry

        limit = Limit.per_minute("rpm", 1000).with_schedule(
            (ScheduleEntry(cron="* 9-17 * * 1-5", tz="America/New_York", scale=0.5),)
        )
        result = self._invoke(mock_repo_class, runner, [limit])
        assert "* 9-17 * * MON-FRI" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_renders_a_reset_line(self, mock_repo_class, runner):
        from zae_limiter.models import Limit
        from zae_limiter.schedule import ScheduleEntry

        limit = Limit.per_day("rpd", 10_000).with_reset_schedule(
            (ScheduleEntry.reset(cron="0 0 * * *", tz="America/New_York"),)
        )
        result = self._invoke(mock_repo_class, runner, [limit])
        assert "Reset:" in result.output
        assert "0 0 * * *" in result.output
        assert "refill to capacity" in result.output

    @patch("zae_limiter.repository.Repository")
    def test_unscheduled_limits_show_no_schedule_block(self, mock_repo_class, runner):
        from zae_limiter.models import Limit

        result = self._invoke(mock_repo_class, runner, [Limit.per_minute("rpm", 1000)])
        assert "Schedule:" not in result.output
        assert "Reset:" not in result.output
        assert "rpm: 1,000/min" in result.output
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `uv run pytest tests/unit/test_cli.py -k ScheduleDisplay -v`
Expected: FAIL — `assert 'Schedule:' in ''` (the block is not rendered)

- [ ] **Step 3: Implement**

Add a formatter beside `_format_limit` (:2200), returning the lines to print under a limit:

```python
def _format_schedule_lines(limit: Limit) -> list[str]:
    """Render a limit's schedules for display (§4.3, §5.4).

    Cron is rendered canonically, with weekday and month as names — the
    stored form is compact and normalised, so an operator who typed `1-5`
    sees `MON-FRI`. The gloss after the arrow states the effect.
    """
    lines: list[str] = []
    if limit.schedule:
        lines.append("    Schedule:")
        for entry in limit.schedule:
            if entry.scale is not None:
                effect = f"{entry.scale:.0%}"
            elif entry.capacity is not None:
                effect = f"capacity {entry.capacity:,}"
            else:
                effect = "refill override"
            lines.append(f"      {to_cron_display(entry)}  {entry.tz}  → {effect}")
    if limit.reset_schedule:
        lines.append("    Reset:")
        for entry in limit.reset_schedule:
            lines.append(
                f"      {to_cron_display(entry)}  {entry.tz}  → refill to capacity"
            )
    return lines
```

`to_cron_display(entry)` renders the entry's `cron` through the canonical name-rendering path
from core plan Task 5 — a stored `1-5` must come back as `MON-FRI`, so do not just echo
`entry.cron`, which is whatever the caller happened to construct with.

Then print the lines after `_format_limit` in both commands — `entity_get_limits` at :3385 and
`resource_get_defaults` at the matching loop:

```python
                for limit in limits:
                    click.echo(f"  {_format_limit(limit)}")
                    for line in _format_schedule_lines(limit):
                        click.echo(line)
```

- [ ] **Step 4: Run the test and watch it pass**

Run: `uv run pytest tests/unit/test_cli.py -k ScheduleDisplay -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Lint, type check, commit**

```bash
uv run ruff check --fix . && uv run ruff format . && uv run mypy
uv run pytest tests/unit/test_cli.py -q
git add src/zae_limiter/cli.py tests/unit/test_cli.py
git commit -m "$(cat <<'EOF'
✨ feat(cli): show schedules in get-limits output

entity get-limits and resource get-defaults gain a Schedule: block and a
Reset: line, each entry rendered as canonical cron plus a gloss saying
what it does.

Display only — `-l` is not extended; `limits apply` is the CLI path for
setting schedules (§1.5).

Refs #222
EOF
)"
```

---

### Task 10: Failure handling

> **Expand before picking this up.** The steps below carry real assertions but compress
> the TDD cycle, and their fixture setup depends on `schedule.py`, which does not exist
> yet. Write the full failing-test/implement/pass cycle against the real signatures once
> the core plan has landed — writing it against invented ones now is the mistake this
> plan's own review calls out.

**Files:** Modify `src/zae_limiter/schedule.py`, `src/zae_limiter/repository.py` · Test `tests/unit/test_schedule.py`, `tests/integration/`

An unparseable **stored** schedule raises `RateLimiterUnavailable`, honouring the operator's existing `on_unavailable` setting. Treating it as "no schedule" would silently run at the **base** limit — so a parse error doubles a customer's limit when the schedule said `0.5x` — and, with `vu` left expired, would pin the bucket to the slow path permanently.

A **version marker** in the encoding (~6 B) lets the log distinguish "written by a newer client", which is the realistic trigger, from genuine corruption.

- [ ] **Step 1: Write the failing test**

```python
class TestCorruptStoredSchedule:
    def test_unparseable_schedule_raises_unavailable(self):
        with pytest.raises(RateLimiterUnavailable):
            decode("this is not a schedule", "UTC")

    def test_newer_version_marker_is_distinguishable(self):
        with pytest.raises(RateLimiterUnavailable, match="newer"):
            decode("v9:h9-17w1-5s500", "UTC")

    async def test_on_unavailable_allow_degrades(self, test_repo_allow):
        """The operator already chose what happens when the limiter cannot decide."""
        ...  # write a corrupt sched directly, then acquire
        async with limiter.acquire("user-1", "gpt-4", consume={"rpm": 1}) as lease:
            assert lease.degraded is True

    async def test_on_unavailable_block_raises(self, test_repo_block):
        ...
```

- [ ] **Step 2–4:** Implement; commit — `✨ feat(limiter): fail safe on an unreadable stored schedule`

---

### Task 11: E2E

> **Expand before picking this up.** The steps below carry real assertions but compress
> the TDD cycle, and their fixture setup depends on `schedule.py`, which does not exist
> yet. Write the full failing-test/implement/pass cycle against the real signatures once
> the core plan has landed — writing it against invented ones now is the mistake this
> plan's own review calls out.

**Files:** Modify `tests/e2e/test_localstack.py`

Boundary crossings use a `*/2` schedule and real waiting, marked `slow` — the Lambda's clock cannot be injected the way `Repository._now_ms()` can.

- [ ] **Step 1: Write the tests** — every case from spec §8:

```python
@pytest.mark.e2e
@pytest.mark.slow
class TestScheduleBoundaryE2E:
    async def test_shrink_boundary_trims_a_full_bucket(self):
        """The surplus must be unspendable, not a free burst."""

    async def test_grow_boundary_makes_capacity_available(self): ...

    async def test_boundary_while_a_lease_is_open(self):
        """adjust/release still land against the declared consume scope (#455)."""

    async def test_concurrent_traffic_at_the_boundary_never_over_admits(self):
        """Exactly one materialisation wins; total admitted <= the new ceiling."""

    async def test_cascade_with_different_schedules_on_child_and_parent(self): ...

    async def test_every_shard_converges_on_its_share(self): ...

    @pytest.mark.parametrize("aggregator", [True, False])
    async def test_crossing_works_with_and_without_the_aggregator(self, aggregator):
        """ADR-133: sharding and refill must work either way."""

    async def test_daily_quota_reset(self):
        """Burn it, cross the edge, get it back in one lump; tc keeps climbing."""

    async def test_idle_bucket_resets_on_wake_not_at_the_edge(self): ...

    async def test_manifest_applied_schedule_reaches_live_buckets(self): ...

    async def test_corrupt_stored_schedule_honours_on_unavailable(self): ...
```

- [ ] **Step 2:** Run against LocalStack; `zae-limiter local up` first, env vars per `.claude/rules/testing.md`
- [ ] **Step 3:** Commit — `✅ test(limiter): end-to-end schedule boundary coverage`

---

### Task 12: ADR-135 and documentation

**Files:** Create `docs/adr/135-scheduled-limits.md`; modify `CLAUDE.md`, `docs/guide/`, `docs/cli.md`, `docs/api/`

- [ ] **Step 1:** Write ADR-135 from the design doc. **Verify 135 is still unclaimed** against `main` and open PRs before using it — #393 holds 126-132, and `main` currently tops out at 134. Do not pre-claim a number in a branch name; that practice caused the #304 and #320 collisions.
- [ ] **Step 2:** Update `CLAUDE.md`: the schedule attributes in the DynamoDB writer table, `SCHEDULE_BOUNDARY` in the failure-reason list, `cronsim`/`tzdata`/`croniter` in Dependencies, and the retired 1.5x shard transient if the core plan has not already done it.
- [ ] **Step 3:** Run the `docs-updater` agent per `.claude/rules/docs-parity.md`.
- [ ] **Step 4:** Commit — `📝 docs(adr): record the scheduled limits design as ADR-135`

---

## Self-Review

**Spec coverage.** §3.6 → Tasks 1-4. §4.1 reset encoding → Task 4. §5.1 → Task 1 (no signature changes; the schedule rides on `Limit`). §5.2 → Task 8, building on the provisioner plan. §5.3 → Tasks 6-7. §5.4 → Task 9. §6 → Task 10. §7 → Task 5. §8 → Task 11. §9 limitations → documented in Task 12's ADR.

**Expansion status.** Tasks 1, 2, 6, 7, 8 and 9 carry complete code: full TDD cycles, real
fixture setup, and assertions verified against the tree at `cc1ff1dc`. Tasks 3, 4, 5, 10 and 11
remain compressed and each now carries an explicit "expand before picking this up" note. That
split is deliberate and is the same rule applied twice: a task whose target code exists gets
written against it; a task whose target is `schedule.py` does not get written against a guess.

**Verified against the tree, not recalled.** Four things the earlier draft of Tasks 6-9 had wrong:

1. **`differ.py` compares nothing.** `compute_diff` (`differ.py:24-101`) emits a `Change` for
   every manifest item on every apply, choosing only `"create"` vs `"update"` by name. The draft
   had tests asserting change *detection* ("an identical schedule is not spuriously different"),
   which is false by construction. Task 6 now pins the real contract: the schedule survives into
   `Change.data` through `to_dict()`, and `differ.py` needs no change at all.
2. **The CFN generator is a Click command over raw dicts.** `limits_cfn_template`
   (`limits_cli.py:153`) calls `_load_yaml` and walks the mapping; it never builds a
   `LimitsManifest`. The draft's `generate_cfn_template(LimitsManifest.from_yaml(...))` does not
   exist. Task 7 now targets `_limits_to_cfn` (:214) and its inverse `_cfn_limits_to_manifest`
   (`handler.py:299`), and parses the emitted **YAML** (`click.echo(yaml.dump(...))`, :211) under
   the `TenantLimits` resource key.
3. **The Lambda builders return `bytes`.** `build_provisioner_package()` returns a zip, so the
   draft's `build_provisioner(tmp_path) / "zae_limiter" / "schedule.py"` is not a thing. Task 8
   now uses `zipfile.ZipFile(io.BytesIO(...)).namelist()` with the `LambdaBuilder` patch that
   `test_provisioner_builder.py:32` already establishes.
4. **CLI tests mock the repository, not DynamoDB.** `@patch("zae_limiter.repository.Repository")`
   with `get_limits` and `get_entity_disabled` as `AsyncMock`s and `Repository.open` stubbed
   (`test_cli.py:4755-4782`). Task 9 follows that, and stubs `get_entity_disabled` — omitting it
   makes the command raise after printing, which reads as a formatting bug.

**Spec coverage.** §3.6 → Tasks 1-4. §4.1 reset encoding → Task 4. §5.1 → Task 1 (no signature
changes; the schedule rides on `Limit`). §5.2 → Task 8, building on PR #485. §5.3 → Tasks 6-7.
§5.4 → Task 9. §6 → Task 10. §7 → Task 5. §8 → Task 11. §9 limitations → Task 12's ADR.

**Type consistency.** `ScheduleEntry.reset()` is the only constructor for reset entries and sets
`_reset=True`, so the "exactly one modifier" rule and the "no modifier" rule never both apply to
one entry. `next_boundary` already takes `reset_sched` as a defaulted second tuple in the core
plan's Task 4, so Task 2 here is a behaviour change inside one function rather than a signature
change across `lease.py` and `processor.py`; `now_ms` is keyword-only there for the same reason.
`LimitDecl.to_dict()` widens from `dict[str, int]` to `dict[str, Any]` in Task 6 — mypy catches
the annotation if it is missed.

**Known-incomplete, carried deliberately.** Task 8 stamps schedules through
`sync_bucket_params`, which queries `BUCKET#{resource}#` and therefore no-ops on an entity's
`_default_` config. That is **#487**, is not fixed here, and means a `_default_`-level schedule
inherits the same gap until #487 lands. Task 8 says so inline so a reviewer does not read it as
an oversight.

**Cross-plan ordering.** Provisioner plan (PR #485) → core plan → this plan. Task 8 here is inert
without PR #485's handler wiring; Tasks 1-5 here are inert without the core plan's
`schedule.py`.
