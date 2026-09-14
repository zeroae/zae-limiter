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
        assert _iso(next_boundary(params, DAILY, _ms("2026-09-15 22:00"))).startswith(
            "2026-09-16T00:00"
        )

    def test_reset_only_schedule_still_produces_boundaries(self):
        assert _iso(next_boundary((), DAILY, _ms("2026-09-15 09:00"))).startswith(
            "2026-09-16T00:00"
        )

    def test_neither_tuple_means_no_boundary(self):
        assert next_boundary((), (), _ms("2026-09-15 09:00")) is None
```

- [ ] **Step 2: Run and watch it fail** — `ImportError: cannot import name 'prev_reset_edge'`

- [ ] **Step 3: Implement.** `prev_reset_edge` scans **backwards** from `now_ms` at the same adaptive granularity and caps as `next_boundary`, looking for the most recent transition from non-matching to matching. No edge within the cap means the expression never matches (`0 0 30 2 *`) — return `None`, which resets nothing.

Change `next_boundary(sched, now_ms)` to `next_boundary(sched, reset_sched, now_ms)`: the forward scan now returns the earliest instant at which *either* the active param entry changes *or* a reset edge fires. Update the core plan's call sites in `lease.py` and `processor.py`.

- [ ] **Step 4: Run, commit** — `✨ feat(models): detect reset edges and fold them into vu`

---

### Task 3: Apply the reset in the materialising pass

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

**Files:** Modify `src/zae_limiter/schedule.py`, `src/zae_limiter/bucket.py`, `src/zae_limiter/lease.py` · Test `tests/unit/test_bucket.py`

**The flat estimate is wrong in the direction that matters.** It over-reports when a boundary raises the limit and **under**-reports when one lowers it — and lowering is the headline use case. Worked example from the spec: empty bucket, 500 tokens needed, 1000/min now, boundary in 10 s dropping to 500/min. Flat estimate **30 s**; real wait **50 s** (10 s yielding 167 tokens, then 333 remaining at half rate).

**A reset edge dominates.** If a reset boundary falls before the deficit clears by refill, that instant *is* the answer. For a daily quota this is the difference between reporting hours of drip-refill and reporting "at midnight" — the only useful answer, and the clearest demonstration that #473's `check_availability()` is subsumed rather than dropped.

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

- [ ] **Step 3: Implement** `retry_after_with_schedule(...)` in `schedule.py`: walk forward window by window using `next_boundary`, accumulating tokens at each window's effective rate until the deficit clears; if a reset edge falls inside the walk, return that instant directly; cap at 8 windows and fall back to the flat `calculate_retry_after`. Call it from the two places that build `LimitStatus` — `lease.py`'s `_build_retry_failure_statuses` and `RateLimiter._admit_limit`.

- [ ] **Step 4: Run, regenerate sync, commit** — `✨ feat(bucket): compute retry_after across schedule boundaries`

---

### Task 6: Manifest parsing

**Files:** Modify `src/zae_limiter_provisioner/manifest.py`, `src/zae_limiter_provisioner/differ.py` · Test `tests/unit/test_provisioner_manifest.py`, `tests/unit/test_differ.py`

- [ ] **Step 1: Write the failing test**

```python
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
            capacity: 2000
      rpd:
        capacity: 10000
        refill_period: 86400
        reset_schedule:
          - cron: "0 0 * * *"
            tz: America/New_York
"""


class TestManifestSchedules:
    def test_parses_both_tuples(self):
        m = LimitsManifest.from_yaml(YAML)
        rpm = m.resources["gpt-4"].limits["rpm"]
        assert len(rpm.schedule) == 2
        assert rpm.schedule[0].scale == 0.5
        assert m.resources["gpt-4"].limits["rpd"].reset_schedule[0].cron == "0 0 * * *"

    def test_rejects_an_invalid_cron_at_parse_time(self):
        """A manifest that applies must be a manifest that evaluates."""
        with pytest.raises(ValueError, match="cron"):
            LimitsManifest.from_yaml(YAML.replace("* 9-17 * * MON-FRI", "* 99 * * *"))

    def test_rejects_extended_tokens(self):
        with pytest.raises(ValueError, match="not supported"):
            LimitsManifest.from_yaml(YAML.replace("0 0 * * *", "0 0 L * *"))

    def test_absent_schedule_is_an_empty_tuple(self):
        m = LimitsManifest.from_yaml(YAML.replace("        schedule:\n", "        _x:\n"))
        ...


class TestDifferSeesScheduleChanges:
    def test_a_changed_schedule_is_a_change(self):
        ...

    def test_an_identical_schedule_is_not_spuriously_different(self):
        """Storage is canonical, so this is a string compare — MON-FRI and 1-5
        must not read as a change on every apply."""
        ...
```

- [ ] **Step 2–4:** Add `schedule` and `reset_schedule` to `LimitDecl`, parsed into `ScheduleEntry`s via the same validation (so parse-time errors surface in `limits plan`); include both in `differ.py`'s comparison; commit — `✨ feat(provisioner): parse schedules from the limits manifest`

---

### Task 7: CloudFormation round trip

**Files:** Modify `src/zae_limiter/limits_cli.py` (tri-state emission ~177-195), `src/zae_limiter_provisioner/handler.py` · Test `tests/unit/test_limits_cli.py`

Follows `Disabled` exactly: a `Schedule` and a `ResetSchedule` property on `Custom::ZaeLimiterLimits`, emitted **only when declared**.

- [ ] **Step 1: Write the failing test**

```python
class TestCfnScheduleRoundTrip:
    def test_emits_schedule_only_when_declared(self):
        tpl = generate_cfn_template(LimitsManifest.from_yaml(YAML))
        props = tpl["Resources"]["..."]["Properties"]
        assert props["Schedule"] == [
            {"Cron": "* 9-17 * * MON-FRI", "Tz": "America/New_York", "Scale": 0.5},
            {"Cron": "* 0-6 * * *", "Tz": "America/New_York", "Capacity": 2000},
        ]

    def test_omits_both_properties_when_absent(self):
        ...

    def test_cron_survives_the_round_trip_as_standard_cron(self):
        """CloudFormation is user-facing IaC: never the compact form."""
        ...
```

- [ ] **Step 2–4:** Implement emission and the handler-side parse; commit — `✨ feat(cli): round-trip schedules through CloudFormation`

---

### Task 8: The provisioner stamps schedules onto buckets

**Files:** Modify `src/zae_limiter_provisioner/bucket_sync.py`, `src/zae_limiter/infra/provisioner_builder.py` · Test `tests/unit/test_provisioner_bucket_sync.py`

**This is where the provisioner plan and the core plan meet.** `build_bucket_param_update` gains the `sched` / `sched_tz` / `rsched` stamp and `vu = 0`, so a manifest-applied schedule reaches live buckets. Without the provisioner plan's Task 4 wiring, this does nothing.

The provisioner Lambda must now vendor `schedule.py` — and, if the reset path calls into refill math, `bucket.py` too, which it currently does **not** copy (`provisioner_builder.py:131-139`).

- [ ] **Step 1: Write the failing test**

```python
class TestProvisionerStampsSchedules:
    def test_stamps_sched_and_expires_vu(self):
        expr, names, values = build_bucket_param_update(
            {"rpm": {"capacity": 1000, "refill_amount": 1000, "refill_period": 60,
                     "schedule": "h9-17w1-5s500", "schedule_tz": "America/New_York"}},
            ttl_multiplier=0, stale_limit_names=None, now_ms=0,
        )
        assert values[":sched"] == {"S": "h9-17w1-5s500"}
        assert values[":vu"] == {"N": "0"}

    def test_removes_the_stamps_when_a_schedule_is_dropped(self):
        ...

    def test_provisioner_lambda_vendors_schedule_py(self, tmp_path):
        assert (build_provisioner(tmp_path) / "zae_limiter" / "schedule.py").exists()
```

- [ ] **Step 2–4:** Implement; commit — `✨ feat(provisioner): stamp manifest schedules onto live buckets`

---

### Task 9: CLI display

**Files:** Modify `src/zae_limiter/cli.py` · Test `tests/unit/test_cli.py`

`entity get-limits`, `resource get-defaults` and `system get-defaults` gain a `Schedule:` section showing each entry as canonical cron plus a human gloss, and a `Reset:` line. Setting schedules stays API + manifest (§1.5) — `-l` is **not** extended.

- [ ] **Step 1: Write the failing test**

```python
class TestScheduleDisplay:
    def test_renders_canonical_cron_with_names(self):
        out = runner.invoke(cli, ["entity", "get-limits", "user-1", "--resource", "gpt-4"]).output
        assert "* 9-17 * * MON-FRI" in out
        assert "America/New_York" in out
        assert "50%" in out

    def test_renders_numeric_input_as_names(self):
        """The one visible normalisation: 1-5 comes back as MON-FRI."""
        ...

    def test_renders_a_reset_line(self):
        assert "Reset:" in out and "refill to capacity" in out

    def test_unscheduled_limits_show_no_schedule_section(self):
        ...
```

- [ ] **Step 2–4:** Implement; commit — `✨ feat(cli): show schedules in get-limits output`

---

### Task 10: Failure handling

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

**Placeholder scan — and an honest limit.** Tasks 1, 2 and 5 carry complete test code. Tasks 3, 4, and 6 through 11 carry complete *structure*, exact assertions and real file targets, but several test bodies use `...` for fixture setup, and Tasks 4, 6, 7, 8, 9 compress Steps 2-4 into one line. This is weaker than the core plan's Tasks 1-7 and weaker than the skill asks for. The reason is specific rather than general: these tasks depend on signatures the core plan produces (`encode`, `_sync_bucket_params`'s final shape, `LimitStatus` construction) that do not exist yet, and on CFN/CLI code I have not read line by line. **Before starting any of Tasks 3-11, expand that task against the then-current code.** Writing invented bodies now would be worse than saying so.

**Type consistency.** `ScheduleEntry.reset()` is the only constructor for reset entries and sets `_reset=True`, which `__post_init__` branches on — so the "exactly one modifier" rule and the "no modifier" rule never both apply to one entry. `next_boundary` already takes `reset_sched` as a defaulted, unused second tuple in the core plan's Task 4, specifically so Task 2 here is a behaviour change inside one function rather than a signature change rippling through `lease.py` and `processor.py`. `now_ms` is keyword-only there for the same reason — a positional call cannot silently bind a timestamp to `reset_sched` once two tuples are in play.

**Cross-plan ordering.** Provisioner plan → core plan → this plan. Task 8 here is inert without the provisioner plan's Task 4; Tasks 1-5 here are inert without the core plan's `schedule.py`.
