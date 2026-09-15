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
- Sync codegen, lint rules, and the `pytest tests/unit/` gevent hazard are all as stated in the core plan's Global Constraints — they apply here unchanged. Three of them bite repeatedly below and are worth restating:
  - **`Repository._now_ms()` does not cover the config cache.** `config_cache.py:99` and `:103` still call `time.time()`, so a test that jumps the injected clock across a boundary resolves the **pre**-jump `Limit` — schedule and all — for 60 real seconds. Call `invalidate_config_cache()` after every jump, or build with `config_cache_ttl=0`. This bites Tasks 3, 5 and 11 specifically.
  - **Never `pytest tests/unit/ -o "addopts="`.** It un-skips the gevent tests into the same process as the asyncio ones and hangs with no output. Run `uv run pytest tests/unit/ -q` and `uv run pytest tests/unit/ -m gevent -n 0 -q` separately.
  - **Never bare `uv run ruff format .`.** The local ruff is newer than pre-commit's pinned 0.9.2 and reformats 34 unrelated files, including the Python blocks inside these plan documents. Scope the formatter to the directories you touched.
- **Every `file.py:NNN` reference below predates the core plan's merge and has drifted.** The
  core plan is now complete — all 14 tasks are on `main` — so these tasks can and should be
  checked against real merged code rather than against a plan. Treat a line number as a hint
  and grep for the symbol: `check_availability` is at `limiter.py:2006`, not `:1941`, and its
  two base-capacity sites are `:2113`/`:2116`, not `:2049`/`:2052`;
  `build_bucket_param_update` is at `bucket_sync.py:83`, not `:67`. The *claims* those
  references support were spot-checked and still hold; only the offsets moved.
- Feature branch off `main`; PRs via the `/pr` skill.

---

### Task 1: `reset_schedule` on `Limit`

**Files:** Modify `src/zae_limiter/schedule.py`, `src/zae_limiter/models.py` · Test `tests/unit/test_schedule.py`

**Interfaces:**
- Consumes: `ScheduleEntry`, `parse_cron` (core plan Task 1)
- Produces: `Limit.reset_schedule: tuple[ScheduleEntry, ...] = ()`; `Limit.quota(name, amount, *, cron, tz)`; `ScheduleEntry.reset()` validation path

**ADR-137 and ADR-138 were accepted after this task was written, and ADR-137 changes its
shape.** [ADR-137](../adr/137-reset-replaces-drip.md): a limit drips **or** resets, never both
and never neither — `refill_amount = 0` is valid *only* alongside a non-empty `reset_schedule`,
and a positive rate alongside a reset is rejected at construction.
[ADR-138](../adr/138-fixed-reset-windows-only.md) scopes reset to fixed calendar windows, which
constrains no code here but must reach the guide (#524).

**Decision: the amount and the reset arrive in the same call — `Limit.quota(...)`.**

```python
Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")
```

This is not ergonomic sugar; it is the only shape ADR-137 leaves standing. The chained form

```python
Limit.quota("rpd", 10_000).with_reset_schedule(...)  # impossible, not merely verbose
```

cannot work, because the intermediate value — zero refill, no reset yet — is exactly the state
validation rejects, and `Limit.__post_init__` runs at construction. The explicit spelling dies
on the same line: `Limit.custom(name=..., refill_amount=0, refill_period_seconds=...)` raises
before there is an object to attach a reset to. So the general rule, which holds for any future
design here: **any form that builds the limit first and attaches the reset afterwards is dead
on arrival.** `Limit.quota()` is the ergonomic spelling of that rule; passing `reset_schedule`
into `custom()` would be the verbose equivalent of the same constraint.

`with_reset_schedule()` therefore survives only as a *replacement* operator on a limit that is
already a quota — swapping one reset schedule for another — never as the way a quota is built.
Clearing one with `with_reset_schedule(())` is a validation error for the same reason: it
leaves a bucket that can never recover.

**Rejected: let `with_reset_schedule()` silently zero the refill**, so that
`Limit.per_day("rpd", 10_000).with_reset_schedule(...)` "works". It throws away a number the
caller explicitly passed — the same class of problem ADR-137 rejected when it declined to leave
a configured-but-inert rate ("Keep the positive-rate rule and silently ignore the stored
rate"). A quota author who writes `per_day(..., 10_000)` and gets a limit whose stored
`refill_amount` is 0 has been silently overruled.

A quota may still carry a *parameter* schedule — `Limit.quota(...).with_schedule(...)` is
valid, because `with_schedule` never touches `refill_amount` and the intermediate value is
already a legal quota. Only the **reset** has to arrive with the amount.

**`with_reset_schedule()` does not exist yet** — it is this task's own deliverable, so the
decision above shapes it rather than changing it. Note that #524 records the held user-guide
PR #483 leading with exactly the now-invalid `Limit.per_day(...).with_reset_schedule(...)`
form; Task 12's docs pass and #524 both land on `Limit.quota()`.

**Downstream fixtures have been converted.** Tasks 3, 4, 5, 9, 10 and 11 were written before
ADR-137 and built their fixtures as `Limit.per_day("rpd", 10_000).with_reset_schedule(DAILY)`,
which now raises at construction. Every such site has since been rewritten as
`Limit.quota("rpd", 10_000, cron=..., tz=...)`, with `.with_schedule(...)` chained after it
where a parameter schedule is also wanted, so the fixtures in those tasks are the spelling to
copy. The only surviving `Limit.per_day(...).with_reset_schedule(...)` in this document is the
rejection test in Step 1 above, which asserts that it raises. Task 8's manifest fixture carries
the same conversion in its dict form: `refill_amount: 0` beside a `reset_schedule`, never the
capacity.

- [ ] **Step 1: Write the failing test**

```python
class TestQuotaFactory:
    def test_quota_sets_the_zero_refill_and_the_reset_together(self):
        q = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")
        assert q.capacity == 10_000
        assert q.refill_amount == 0  # ADR-137: a quota does not drip
        assert q.reset_schedule[0].cron == "0 0 * * *"
        assert q.reset_schedule[0].tz == "America/New_York"

    def test_a_zero_refill_without_a_reset_is_rejected(self):
        """ADR-137: never neither — the bucket could never recover."""
        with pytest.raises(ValueError, match="reset_schedule"):
            Limit.custom("rpd", capacity=10_000, refill_amount=0, refill_period_seconds=86_400)

    def test_a_positive_rate_alongside_a_reset_is_rejected(self):
        """ADR-137: never both — the drip returns the allowance a second time."""
        with pytest.raises(ValueError, match="refill_amount"):
            Limit.per_day("rpd", 10_000).with_reset_schedule(
                (ScheduleEntry.reset(cron="0 0 * * *", tz="America/New_York"),)
            )


class TestResetScheduleValidation:
    def test_reset_entry_carries_cron_and_tz_only(self):
        e = ScheduleEntry.reset(cron="0 0 * * *", tz="America/New_York")
        q = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")
        assert q.reset_schedule == (e,)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"scale": 0.5},
            {"capacity": 100},
            {"refill_amount": 10},
            {"refill_period_seconds": 30},
        ],
    )
    def test_rejects_a_reset_entry_carrying_a_modifier(self, kwargs):
        """A reset overrides no parameters; a modifier on one is a category error.

        The entry under test is a *param* entry handed to the reset tuple, which
        is the only way to construct one — `ScheduleEntry.reset()` takes no
        modifiers at all.
        """
        e = ScheduleEntry(cron="0 0 * * *", **kwargs)
        with pytest.raises(ValueError, match="reset"):
            Limit.quota("rpd", 10_000, cron="0 0 * * *").with_reset_schedule((e,))

    def test_a_bare_schedule_entry_is_invalid_for_the_params_tuple(self):
        """The same entry is legal as a reset and illegal as a param override."""
        with pytest.raises(ValueError, match="exactly one"):
            ScheduleEntry(cron="0 0 * * *")

    def test_clearing_a_quotas_reset_is_rejected(self):
        """`with_reset_schedule(())` on a zero-refill limit leaves a bucket that
        can never recover, so ADR-137 makes it unconstructible rather than
        silently restoring a drip."""
        with pytest.raises(ValueError, match="reset_schedule"):
            Limit.quota("rpd", 10_000, cron="0 0 * * *").with_reset_schedule(())
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

The tests above already use `ScheduleEntry.reset(...)`, and
`test_rejects_a_reset_entry_carrying_a_modifier` stays pointed at a *param* entry passed to
`with_reset_schedule` — that is the only way to construct an entry carrying a modifier, since
the classmethod does not accept one.

- [ ] **Step 2: Run and watch it fail** — `AttributeError: type object 'Limit' has no attribute 'quota'`

- [ ] **Step 3: Implement.** Add `_reset: bool = False` to `ScheduleEntry` with the classmethod above; `__post_init__` requires exactly one modifier when `_reset` is False and **no** modifier when it is True. Add `reset_schedule` and `with_reset_schedule()` to `Limit`, validating that every entry has `_reset=True`.

Then add the ADR-137 cross-field rule to `Limit.__post_init__`, replacing the bare
`refill_amount <= 0` check (`models.py:254`) — the two fields can no longer be validated
independently, and the message must explain the pairing rather than the field:

```python
if self.refill_amount < 0:
    raise ValueError("refill_amount must not be negative")
if self.refill_amount == 0 and not self.reset_schedule:
    raise ValueError(
        "refill_amount=0 means the limit does not drip, which is only valid "
        "with a reset_schedule; otherwise the bucket can never recover. "
        "Use Limit.quota(name, amount, cron=..., tz=...) (ADR-137)."
    )
if self.refill_amount > 0 and self.reset_schedule:
    raise ValueError(
        "a limit drips or resets, never both: a positive refill_amount "
        "alongside a reset_schedule grants roughly twice the intended "
        "allowance per period. Use Limit.quota(...) (ADR-137)."
    )
```

and the factory that makes the legal shape the reachable one:

```python
    @classmethod
    def quota(
        cls,
        name: str,
        amount: int,
        *,
        cron: str,
        tz: str = "UTC",
    ) -> "Limit":
        """An allowance of ``amount`` per calendar window, restored at each edge.

        A quota does not drip: the balance is *set* to the capacity when the
        window opens and does not recover in between (ADR-137). The window is a
        fixed calendar window — every entity on this schedule resets at the same
        wall-clock instant (ADR-138).
        """
        return cls(
            name=name,
            capacity=amount,
            refill_amount=0,
            refill_period_seconds=_QUOTA_REFILL_PERIOD_SECONDS,
            reset_schedule=(ScheduleEntry.reset(cron=cron, tz=tz),),
        )
```

**One thing Step 3 must settle, because the factory cannot construct without it:**
`refill_period_seconds` is still validated `> 0` and there is no sensible value for the
denominator of a rate that is zero. Pick a documented module constant
(`_QUOTA_REFILL_PERIOD_SECONDS = 1` is the least misleading: it reads as "0 per second", where
86_400 reads as a daily rate that is not what the limit does) and say in the docstring that the
field is inert while `refill_amount` is 0. Do **not** expose it as a `quota()` keyword; a knob
that changes nothing is worse than a constant. Note this does not rescue the TTL formula, which
divides by `refill_amount`, not by the period — that is ADR-137's named consequence and is
#222's to solve for resource- and system-level reset limits.

`Limit.per_day` already exists (`models.py:340`), so the earlier "add it if it does not exist"
note is discharged. It stays a *drip* factory and must not grow a reset parameter: a limit
built by `per_day` is a rate, and `quota` is the allowance.

- [ ] **Step 4: Run, regenerate sync, commit** — `✨ feat(models): add Limit.quota and reset_schedule for quotas`

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
        never = (ScheduleEntry.reset(cron="0 0 30 2 *"),)  # February 30th
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

**Files:**
- Modify: `src/zae_limiter/limiter.py` (`_do_acquire`'s entry loop at :1716-1766, `_try_parent_only_acquire`'s at :1509-1544), `src/zae_limiter/lease.py` (the `vu` computation core plan Task 12 adds around :376-393), `src/zae_limiter_aggregator/processor.py` (`LimitRefillInfo` :109, `BucketRefillState` :123, `ParsedBucketLimit` :307, `ParsedBucketRecord` :319, `_parse_bucket_record` :353, `_item_next_boundary` :621, `try_refill_bucket` :638)
- Test: `tests/unit/test_limiter.py`, `tests/unit/test_processor.py`

**Interfaces:**
- Consumes: `prev_reset_edge(reset_sched, now_ms) -> int | None` (Task 2); `next_boundary(sched, reset_sched=(), *, now_ms)` honouring both tuples (Task 2); `Limit.reset_schedule` (Task 1); `BucketState.effective_capacity_milli(now_ms)` (core plan Task 9); `decode_reset` and the `rsched` / `b_{name}_rsched` bucket attributes (**Task 4**)
- Produces: `RateLimiter._apply_reset_edge(limit, state, now_ms) -> bool`; `ParsedBucketLimit.reset_sched`, `ParsedBucketRecord.reset_sched`, `LimitRefillInfo.reset_sched`, `BucketRefillState.reset_sched`

**Do Task 4 before this task's aggregator half.** The numbering here follows the design's
section order (§3.6 reset before §4.1 encoding), not the dependency order. The client half
(`limiter.py`) reads `limit.reset_schedule` off the config-resolved `Limit` and depends on
nothing in Task 4. The aggregator half reads the schedule off the **item**, so it needs
`decode_reset` and it needs something to have written `rsched` — both of which are Task 4.
Either run 4 → 3, or run 3's client half, then 4, then 3's aggregator half. Do not write
`_parse_bucket_record`'s `rsched` branch against a `decode_reset` you have not read.

**The seam is *before* admission, not in `_commit_initial`.** This is the one correction that
matters and the compressed version of this task had it wrong. It said "in `lease.py`'s
slow-path refill, before computing `refill_amounts`" — that is `Lease._commit_initial()`
(`lease.py:376-393`), which runs **after** `RateLimiter._admit_limit()` has already called
`try_consume()` and decided whether to admit. A reset applied there restores the balance in
DynamoDB but not in the decision: a request arriving at 00:00:01 against a quota burnt at
23:59 would still raise `RateLimitExceeded`, and only the *next* request would see the
restored tokens. The reset has to land on `state.tokens_milli` between the capture of
`_original_tokens_milli` and the call to `_admit_limit` — `limiter.py:1734-1737` and
`limiter.py:1517-1521`.

**`lease.py` needs no reset code at all, and adding some would double-apply it.**
`_commit_initial` already computes

```python
refill_amounts[name] = entry.state.tokens_milli - entry._original_tokens_milli + consumed_milli
```

and `build_composite_normal` turns that into `ADD tk (refill - consumed)`. Put
`state.tokens_milli = effective_capacity` before admission and the algebra falls out exactly:
after `try_consume` the state holds `eff_cp - consumed`, `_original_tokens_milli` is still the
**stored** `tk`, so `refill_amounts = eff_cp - stored_tk` and the ADD delta is
`eff_cp - stored_tk - consumed`. That is the identical `ADD (eff_cp - tk_observed)` shape the
aggregator uses — the two writers agree by construction rather than by coincidence.
`lease.py` is touched in this task for one unrelated line: the `vu` fix below.

**`tc` is untouched by construction — and there is a test for it.** `build_composite_normal`
writes `tc_delta = c`, the consumption, from a code path the reset never reaches
(`repository.py:2234`). The counter therefore stays monotonic
(`.claude/rules/design-validation.md`), which is the whole reason the native reset is safer
than parked #471's `reset_bucket()`, which deleted the item and cleared `tc` with it.

**`wcu` never resets.** The reserved write-capacity limit is the per-partition DynamoDB write
ceiling, not a user limit: `try_refill_bucket` already exempts it from `effective_params`
(`processor.py:684-692`) and `_deserialize_composite_bucket` gives it `shard_count=1`. An
item-level `rsched` applies to every limit on the item by default, exactly as `sched` does, so
without an explicit exemption a user's daily reset would also bypass the aggregator's
consumption threshold for `wcu` and write a delta on every batch. On the client side `wcu`
rides as a carrier built by `Limit._carrier()` (`limiter.py:1456`), which never sets
`reset_schedule`, so it is exempt for free — assert that rather than assume it.

**A brand-new bucket never resets.** `BucketState.from_limit` starts it at its full share, so
there is nothing to restore and no stored `rf` to compare an edge against. Skip when
`is_new`.

**`vu` must include reset edges, or a reset-only limit never fires on the fast path.** Two
call sites compute the materialisation stamp and both pass the parameter tuple alone:

- `processor.py:621` `_item_next_boundary` — `next_boundary(s, now_ms=now_ms)` over the
  param schedules only.
- `lease.py`, the `vu` computation core plan Task 12 adds —
  `next_boundary(entry.limit.schedule, now_ms=now_ms)`.

For a limit carrying `reset_schedule` and **no** `schedule`, both return `None`, `vu` is
omitted, the speculative condition never fails, the slow path never runs, and the reset fires
only when something unrelated forces a materialising pass. The daily-quota case — the entire
motivation for §3.6 — is exactly that shape. Both sites must pass the reset tuple as
`next_boundary`'s second positional argument. `now_ms` stays keyword (#500).

**The config cache does not follow `Repository._now_ms()`.** The Global Constraints name this
task specifically. `config_cache.py:99` and `:103` use `time.time()`, so a test that jumps the
injected clock across midnight gets the **pre-jump** `Limit` back — including its
`reset_schedule` — for 60 real seconds. Every test below that moves the clock calls
`await repo.invalidate_config_cache()` immediately afterwards, and says so in a comment. The
alternative, `Repository.open(config_cache_ttl=0)`, is fine too; what is not fine is assuming
the seam covers it.

**The test module is `tests/unit/test_processor.py`.** The core plan calls it
`test_aggregator_processor.py` throughout; that file does not exist and never did (core plan
Task 14 ledger entry).

- [ ] **Step 1: Write the failing client-side test**

In `tests/unit/test_limiter.py`. The first class tests the decision in isolation; the second
drives it through a real `acquire()` against moto, because the isolated one cannot show that
the reset lands *before* admission.

```python
from datetime import datetime
from zoneinfo import ZoneInfo

from zae_limiter.models import BucketState, Limit
from zae_limiter.schedule import ScheduleEntry

NY = ZoneInfo("America/New_York")


def _ny(s: str) -> int:
    """An ISO local time in America/New_York, as epoch milliseconds."""
    return int(datetime.fromisoformat(s).replace(tzinfo=NY).timestamp() * 1000)


DAILY = (ScheduleEntry.reset(cron="0 0 * * *", tz="America/New_York"),)
# ADR-137: a quota is built in one call. `per_day(...).with_reset_schedule(...)`
# raises, because the intermediate value is a positive rate beside a reset.
RPD = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")


class TestApplyResetEdge:
    """The reset decision, isolated from DynamoDB (§3.6).

    `_apply_reset_edge` answers one question — "has a rising reset edge been
    crossed since this item was last refilled?" — and, when it has, sets the
    balance to the shard's share of the capacity in force *at that instant*.
    """

    def _state(self, **kwargs) -> BucketState:
        base = dict(
            entity_id="user-1",
            resource="gpt-4",
            limit_name="rpd",
            tokens_milli=0,
            last_refill_ms=_ny("2026-09-15 18:00"),
            capacity_milli=10_000_000,
            # A quota does not drip (ADR-137), so the stored rate is zero and
            # the period is the inert `_QUOTA_REFILL_PERIOD_SECONDS` (Task 1).
            refill_amount_milli=0,
            refill_period_ms=1_000,
        )
        base.update(kwargs)
        return BucketState(**base)

    def test_sets_tokens_to_the_effective_capacity(self):
        state = self._state()
        assert RateLimiter._apply_reset_edge(RPD, state, _ny("2026-09-16 09:00")) is True
        assert state.tokens_milli == 10_000_000

    def test_uses_the_shards_share(self):
        """Resetting every shard to the undivided capacity multiplies the
        entity's quota by shard_count (§3.6)."""
        state = self._state(shard_count=4)
        assert RateLimiter._apply_reset_edge(RPD, state, _ny("2026-09-16 09:00")) is True
        assert state.tokens_milli == 2_500_000

    def test_respects_a_concurrent_param_schedule(self):
        """A reset landing inside a 0.5x window restores half — the limit in
        force, not the base. Design §9 records the other reading and why it
        was not chosen."""
        limit = RPD.with_schedule(
            (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", scale=0.5),)
        )
        state = self._state(sched=limit.schedule)
        assert RateLimiter._apply_reset_edge(limit, state, _ny("2026-09-16 03:00")) is True
        assert state.tokens_milli == 5_000_000

    def test_no_edge_since_the_last_refill_changes_nothing(self):
        state = self._state(tokens_milli=42, last_refill_ms=_ny("2026-09-16 01:00"))
        assert RateLimiter._apply_reset_edge(RPD, state, _ny("2026-09-16 09:00")) is False
        assert state.tokens_milli == 42

    def test_an_edge_exactly_at_the_last_refill_does_not_re_fire(self):
        """`> rf`, not `>= rf`. The pass that applies a reset stamps `rf` at or
        after the edge, so `>=` would re-apply it on every subsequent request
        and refund everything spent since — an unbounded quota, not a daily one."""
        state = self._state(tokens_milli=42, last_refill_ms=_ny("2026-09-16 00:00"))
        assert RateLimiter._apply_reset_edge(RPD, state, _ny("2026-09-16 09:00")) is False
        assert state.tokens_milli == 42

    def test_two_missed_edges_apply_once(self):
        """Setting the balance to capacity is idempotent, so one edge is enough
        and `prev_reset_edge` reporting only the latest is sufficient."""
        state = self._state(last_refill_ms=_ny("2026-09-14 18:00"))
        assert RateLimiter._apply_reset_edge(RPD, state, _ny("2026-09-16 09:00")) is True
        assert state.tokens_milli == 10_000_000

    def test_a_limit_without_a_reset_schedule_is_untouched(self):
        state = self._state(tokens_milli=7)
        plain = Limit.per_day("rpd", 10_000)
        assert RateLimiter._apply_reset_edge(plain, state, _ny("2026-09-16 09:00")) is False
        assert state.tokens_milli == 7

    def test_a_never_matching_expression_resets_nothing(self):
        """February 30th. `prev_reset_edge` returns None and nothing happens —
        the same contract Task 2 pins from the other side."""
        limit = Limit.quota("rpd", 10_000, cron="0 0 30 2 *", tz="America/New_York")
        state = self._state(tokens_milli=7)
        assert RateLimiter._apply_reset_edge(limit, state, _ny("2026-09-16 09:00")) is False
        assert state.tokens_milli == 7

    def test_debt_is_cleared_rather_than_carried(self):
        """A reset *sets* the balance; it does not add to it. An entity that
        overdrew via adjust() starts the new day whole. This is what makes the
        aggregator's `ADD (eff_cp - tk_observed)` the same operation."""
        state = self._state(tokens_milli=-3_000_000)
        assert RateLimiter._apply_reset_edge(RPD, state, _ny("2026-09-16 09:00")) is True
        assert state.tokens_milli == 10_000_000


class TestResetMaterialisationThroughAcquire:
    """The reset must gate admission, not just the write that follows it."""

    @pytest.fixture
    def slow_path_limiter(self, limiter):
        """The same moto repository, forced onto the slow path.

        The speculative fast path is a conditional UpdateItem that never
        evaluates a schedule (§2.1); in production it reaches the slow path
        because `vu` expires at the reset edge (core plan Tasks 11-12). Here we
        go straight there, so this task can be tested before Task 11 lands.
        """
        return RateLimiter(repository=limiter._repository, speculative_writes=False)

    async def test_the_quota_comes_back_in_one_lump(self, limiter, slow_path_limiter):
        """Burn 10,000 at 23:00, cross midnight, spend 9,000 at 00:30.

        Without the reset the second acquire raises outright: a quota does not
        drip (ADR-137), so the 90 minutes between the two calls return exactly
        nothing and the balance is still 0. That is what makes this test
        discriminating rather than a restatement of the balance — and it is
        also why the final assertion is an exact 1_000_000 with no drip term.
        """
        repo = limiter._repository
        await repo.set_limits("reset-1", [RPD], resource="gpt-4")

        repo._now_ms = lambda: _ny("2026-09-15 23:00")
        async with slow_path_limiter.acquire("reset-1", "gpt-4", consume={"rpd": 10_000}):
            pass

        repo._now_ms = lambda: _ny("2026-09-16 00:30")
        # The clock seam does not reach config_cache.py (:99, :103 still call
        # time.time()), so without this the resolved Limit — and its
        # reset_schedule — is the one cached before the jump.
        await repo.invalidate_config_cache()

        async with slow_path_limiter.acquire("reset-1", "gpt-4", consume={"rpd": 9_000}):
            pass

        bucket = next(
            b for b in await repo.get_buckets("reset-1", resource="gpt-4") if b.limit_name == "rpd"
        )
        assert bucket.tokens_milli == 1_000_000

    async def test_the_reset_never_touches_tc(self, limiter, slow_path_limiter):
        """The total-consumed counter must stay monotonic across the edge.

        19,000 tokens were consumed across the two calls and `tc` must say so.
        An implementation that expressed the reset by rewriting the item, or by
        crediting `tc`, fails here — which is the failure mode #471's
        `reset_bucket()` had and `.claude/rules/design-validation.md` exists for.
        """
        repo = limiter._repository
        await repo.set_limits("reset-2", [RPD], resource="gpt-4")

        repo._now_ms = lambda: _ny("2026-09-15 23:00")
        async with slow_path_limiter.acquire("reset-2", "gpt-4", consume={"rpd": 10_000}):
            pass

        repo._now_ms = lambda: _ny("2026-09-16 00:30")
        await repo.invalidate_config_cache()
        async with slow_path_limiter.acquire("reset-2", "gpt-4", consume={"rpd": 9_000}):
            pass

        bucket = next(
            b for b in await repo.get_buckets("reset-2", resource="gpt-4") if b.limit_name == "rpd"
        )
        assert bucket.total_consumed_milli == 19_000_000

    async def test_an_idle_bucket_resets_on_wake_not_at_the_edge(self, limiter, slow_path_limiter):
        """Idle 18:00 -> 09:00 the next morning: the missed midnight is found
        by the backwards scan and applied by the 09:00 pass (§3.6, §9)."""
        repo = limiter._repository
        await repo.set_limits("reset-3", [RPD], resource="gpt-4")

        repo._now_ms = lambda: _ny("2026-09-15 18:00")
        async with slow_path_limiter.acquire("reset-3", "gpt-4", consume={"rpd": 10_000}):
            pass

        repo._now_ms = lambda: _ny("2026-09-16 09:00")
        await repo.invalidate_config_cache()
        async with slow_path_limiter.acquire("reset-3", "gpt-4", consume={"rpd": 10_000}):
            pass

        bucket = next(
            b for b in await repo.get_buckets("reset-3", resource="gpt-4") if b.limit_name == "rpd"
        )
        assert bucket.tokens_milli == 0

    async def test_a_parent_only_cascade_acquire_also_resets(self, limiter, slow_path_limiter):
        """`_try_parent_only_acquire` builds its own LeaseEntry list
        (limiter.py:1509-1544) and is a second, easily-missed seam.

        Without the reset there, a cascading child whose own bucket is fine
        but whose parent crossed the edge is rejected on the parent.
        """
        repo = limiter._repository
        await slow_path_limiter.create_entity("org-1")
        await slow_path_limiter.create_entity("key-1", parent_id="org-1", cascade=True)
        await repo.set_limits("org-1", [RPD], resource="gpt-4")
        await repo.set_limits("key-1", [RPD], resource="gpt-4")

        repo._now_ms = lambda: _ny("2026-09-15 23:00")
        async with slow_path_limiter.acquire("key-1", "gpt-4", consume={"rpd": 10_000}):
            pass

        repo._now_ms = lambda: _ny("2026-09-16 00:30")
        await repo.invalidate_config_cache()
        async with slow_path_limiter.acquire("key-1", "gpt-4", consume={"rpd": 9_000}):
            pass

        parent = next(
            b for b in await repo.get_buckets("org-1", resource="gpt-4") if b.limit_name == "rpd"
        )
        assert parent.tokens_milli == 1_000_000

    async def test_vu_is_stamped_for_a_reset_only_limit(self, limiter, slow_path_limiter):
        """A limit with a reset schedule and no parameter schedule still needs
        `vu`, or the fast path never yields and the reset never fires.

        This is the whole daily-quota shape, and the compressed version of this
        task did not cover it.
        """
        repo = limiter._repository
        await repo.set_limits("reset-4", [RPD], resource="gpt-4")

        repo._now_ms = lambda: _ny("2026-09-15 23:00")
        async with slow_path_limiter.acquire("reset-4", "gpt-4", consume={"rpd": 1}):
            pass

        item = await _raw_bucket_item(repo, "reset-4", "gpt-4", shard=0)
        assert int(item[schema.BUCKET_FIELD_VU]["N"]) == _ny("2026-09-16 00:00")
```

`_raw_bucket_item` is the helper core plan Task 12 adds to the test module; if you are running
this task first, copy it from there verbatim.

- [ ] **Step 2: Run the client tests and watch them fail**

```bash
uv run pytest tests/unit/test_limiter.py -k "ApplyResetEdge or ResetMaterialisationThroughAcquire" -v
```

Expected: every `TestApplyResetEdge` case fails with
`AttributeError: type object 'RateLimiter' has no attribute '_apply_reset_edge'`, and the
`ThroughAcquire` cases fail with `zae_limiter.exceptions.RateLimitExceeded` — the request the
reset was supposed to admit. `test_vu_is_stamped_for_a_reset_only_limit` fails with
`KeyError: 'vu'`.

- [ ] **Step 3: Implement the client half**

Add the helper beside `_admit_limit` in `limiter.py` (:1384):

```python
    @staticmethod
    def _apply_reset_edge(limit: Limit, state: BucketState, now_ms: int) -> bool:
        """Restore the balance if a calendar reset edge was crossed (§3.6).

        Detection is **backwards**: "was there a rising edge since this item was
        last refilled?", not "is a reset due?". That makes idle buckets correct
        for free — a bucket idle from 18:00 to 09:00 has its `vu` sitting at
        midnight, and the 09:00 pass sees the missed edge and applies it then.
        Two missed midnights apply once, because setting the balance to capacity
        is idempotent.

        The comparison is strictly `>`: the pass that applies a reset stamps
        `rf` at or after the edge, so `>=` would re-fire on every later request
        and refund everything spent since.

        The target is the **shard's share** of the capacity *in force at
        `now_ms`* — `effective_capacity_milli` already applies the parameter
        schedule and then divides by `shard_count`. Resetting every shard to the
        undivided capacity would multiply the entity's quota by `shard_count`.

        Must be called **before** `_admit_limit`, so the restored balance gates
        the request that crossed the edge rather than the one after it. Mutates
        `state` in place and returns whether it did; `_original_tokens_milli`
        and `_original_rf_ms` must already have been captured, because they are
        the *stored* values the ADD delta and the `rf` lock are built from.
        """
        if not limit.reset_schedule:
            return False
        edge = prev_reset_edge(limit.reset_schedule, now_ms)
        if edge is None or edge <= state.last_refill_ms:
            return False
        state.tokens_milli = state.effective_capacity_milli(now_ms)
        return True
```

Call it at both slow-path entry-building sites, immediately after the originals are captured
and before `_admit_limit`. In `_do_acquire` (:1734):

```python
                # Capture original values before try_consume modifies them (ADR-115)
                original_tk = state.tokens_milli
                original_rf = state.last_refill_ms

                # A calendar reset edge crossed since this item was last
                # refilled restores the balance *before* admission, so a
                # request arriving just after midnight is gated against the
                # restored quota rather than the burnt one (§3.6). A brand-new
                # bucket starts at its full share and has no stored `rf` to
                # compare an edge against.
                if not is_new:
                    self._apply_reset_edge(limit, state, now_ms)

                status, consumed = self._admit_limit(eid, resource, limit, state, consume, now_ms)
```

and in `_try_parent_only_acquire` (:1517), where every bucket exists by construction (the
method returns `None` if one is missing), so no `is_new` guard is needed:

```python
original_tk = existing.tokens_milli
original_rf = existing.last_refill_ms

self._apply_reset_edge(limit, existing, now_ms)

status, consumed = self._admit_limit(parent_id, resource, limit, existing, consume, now_ms)
```

Then fix `vu` in `lease.py`, in the computation core plan Task 12 adds — pass the reset tuple
as the second positional argument, keeping `now_ms` keyword-only (#500):

```python
boundaries = [
    b
    for entry in group_entries
    if (b := next_boundary(entry.limit.schedule, entry.limit.reset_schedule, now_ms=now_ms))
    is not None
]
vu = min(boundaries) if boundaries else None
```

`_wcu_carrier` needs no change: `Limit._carrier()` (:1456) never sets `reset_schedule`, so the
guard's first line exempts it. Add an assertion for that rather than relying on it silently —
it goes in the aggregator class below, where the same exemption is *not* free.

- [ ] **Step 4: Run the client tests and watch them pass**

```bash
uv run pytest tests/unit/test_limiter.py -k "ApplyResetEdge or ResetMaterialisationThroughAcquire" -v
```

- [ ] **Step 5: Write the failing aggregator test**

In `tests/unit/test_processor.py`, reusing the `_sched_state` / `_sched_record` helpers core
plan Task 14 already established (:2325, :2351).

```python
DAILY_RESET = (ScheduleEntry.reset(cron="0 0 * * *", tz="America/New_York"),)
DAILY_RESET_COMPACT = "m0h0"

WED_0030 = int(datetime(2026, 9, 16, 0, 30, tzinfo=NY).timestamp() * 1000)
TUE_2300 = int(datetime(2026, 9, 15, 23, 0, tzinfo=NY).timestamp() * 1000)


def _quota_state(**kwargs) -> BucketRefillState:
    """A 10,000/day quota bucket, 2,000 tokens left, last refilled at 23:00.

    `ra_milli=0` is the quota shape ADR-137 mandates: a limit drips or resets,
    never both, so the stored rate is zero and `rp_ms` is the inert
    `_QUOTA_REFILL_PERIOD_SECONDS` Task 1 picks. That is what makes the reset
    the *only* thing that can write to this bucket — an unreset one yields no
    refill delta at all, whatever the consumption threshold does.
    """
    base = dict(
        namespace_id="ns123",
        entity_id="user-1",
        resource="gpt-4",
        rf_ms=TUE_2300,
        limits={
            "rpd": LimitRefillInfo(
                tc_delta=0,
                tk_milli=2_000_000,
                cp_milli=10_000_000,
                ra_milli=0,
                rp_ms=1_000,
            )
        },
    )
    limit_reset = kwargs.pop("limit_reset", None)
    base.update(kwargs)
    state = BucketRefillState(**base)
    if limit_reset is not None:
        for name, reset in limit_reset.items():
            state.limits[name].reset_sched = reset
    return state


class TestAggregatorAppliesResets:
    """The aggregator expresses a reset as the same delta the client writes."""

    def test_expresses_the_reset_as_an_add_to_the_effective_capacity(self) -> None:
        table = MagicMock()
        state = _quota_state(reset_sched=DAILY_RESET)
        assert try_refill_bucket(table, state, now_ms=WED_0030) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":rd_rpd"] == 10_000_000 - 2_000_000

    def test_the_same_bucket_without_a_reset_writes_nothing(self) -> None:
        """Discriminates the test above. A quota's stored rate is 0 (ADR-137),
        so `refill_bucket` yields no delta and there is nothing to write —
        the reset is the whole of this bucket's recovery."""
        table = MagicMock()
        assert try_refill_bucket(table, _quota_state(), now_ms=WED_0030) is False
        table.update_item.assert_not_called()

    def test_the_reset_bypasses_the_consumption_threshold(self) -> None:
        """Explicitly pinned, because the threshold is a `continue` on the
        positive branch and a reset is usually positive. A hot bucket has the
        largest tc_delta and is exactly where the aggregator, not the client,
        is the refiller — gating the reset behind the threshold would turn it
        off on the buckets it matters most for, the same defect §3.3 records
        for the negative clamp."""
        table = MagicMock()
        state = _quota_state(reset_sched=DAILY_RESET)
        state.limits["rpd"].tc_delta = 9_000_000  # far above anything refill yields
        assert try_refill_bucket(table, state, now_ms=WED_0030) is True

    def test_no_edge_since_rf_writes_nothing(self) -> None:
        """rf is already past midnight, so the edge is not new."""
        table = MagicMock()
        state = _quota_state(reset_sched=DAILY_RESET, rf_ms=WED_0030 - 60_000)
        assert try_refill_bucket(table, state, now_ms=WED_0030) is False

    def test_the_reset_is_per_shard(self) -> None:
        table = MagicMock()
        state = _quota_state(reset_sched=DAILY_RESET, shard_count=4)
        assert try_refill_bucket(table, state, now_ms=WED_0030) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":rd_rpd"] == (10_000_000 // 4) - 2_000_000

    def test_the_reset_respects_a_concurrent_param_schedule(self) -> None:
        """Compute effective params first, then set the balance to the result."""
        table = MagicMock()
        night = (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", scale=0.5),)
        state = _quota_state(reset_sched=DAILY_RESET, sched=night)
        assert try_refill_bucket(table, state, now_ms=WED_0030) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":rd_rpd"] == 5_000_000 - 2_000_000

    def test_the_reset_never_writes_tc(self) -> None:
        """`try_refill_bucket` writes only `tk` deltas and `rf`/`vu`. Pinned
        because a reset is the one refill big enough to tempt an implementer
        into 'fixing up' the counter."""
        table = MagicMock()
        assert try_refill_bucket(table, _quota_state(reset_sched=DAILY_RESET), WED_0030) is True
        expr = table.update_item.call_args.kwargs["UpdateExpression"]
        assert "_tc" not in expr

    def test_wcu_is_exempt_from_the_item_level_reset(self) -> None:
        """`rsched` is item-level and applies to every limit by default, but
        `wcu` is the per-partition write ceiling, not a user limit — the same
        exemption `effective_params` already has (processor.py:684-692).

        Discriminating: at the base rate this bucket's positive delta is
        suppressed by the threshold, so anything written here came from the
        reset.
        """
        table = MagicMock()
        state = _quota_state(
            reset_sched=DAILY_RESET,
            limits={
                "wcu": LimitRefillInfo(
                    tc_delta=0,
                    tk_milli=0,
                    cp_milli=1_000_000,
                    ra_milli=1_000_000,
                    rp_ms=60_000,
                )
            },
        )
        assert try_refill_bucket(table, state, now_ms=WED_0030) is False

    def test_a_per_limit_reset_override_beats_the_item_default(self) -> None:
        """`b_{name}_rsched` mirrors `b_{name}_sched` (Task 4). The item-level
        default is Sunday-only and does not fire; the per-limit override is
        daily and does."""
        table = MagicMock()
        state = _quota_state(
            reset_sched=(ScheduleEntry.reset(cron="0 0 * * SUN", tz="America/New_York"),),
            limit_reset={"rpd": DAILY_RESET},
        )
        assert try_refill_bucket(table, state, now_ms=WED_0030) is True

    def test_an_undecodable_schedule_still_skips_the_whole_bucket(self) -> None:
        """`sched_error` short-circuits before any reset logic runs. Refilling
        — or resetting — at the base would silently undo a scale-down (§6)."""
        table = MagicMock()
        state = _quota_state(reset_sched=DAILY_RESET, sched_error="rsched 'zzz': bad")
        assert try_refill_bucket(table, state, now_ms=WED_0030) is False


class TestResetSchedIsCarriedFromTheStreamImage:
    """`rsched` / `b_{name}_rsched` reach the refill state (needs Task 4)."""

    def test_item_level_rsched_is_decoded(self) -> None:
        record = _sched_record(
            limits={"rpd": {"tk": 2_000_000, "cp": 10_000_000, "ra": 0, "rp": 1_000}},
            rf_ms=TUE_2300,
            rsched=DAILY_RESET_COMPACT,
            sched_tz="America/New_York",
        )
        parsed = _parse_bucket_record(record)
        assert parsed is not None
        assert parsed.reset_sched == decode_reset(DAILY_RESET_COMPACT, "America/New_York")

    def test_an_undecodable_rsched_is_reported_not_raised(self) -> None:
        """Same rule as `sched`: raising inside the stream handler aborts the
        whole batch, snapshots included, and the record retries until the
        stream stalls (core plan Task 14)."""
        record = _sched_record(
            limits={"rpd": {"tk": 0, "cp": 10_000_000, "ra": 0, "rp": 1_000}},
            rsched="not-a-schedule",
        )
        parsed = _parse_bucket_record(record)
        assert parsed is not None
        assert parsed.sched_error is not None
        assert parsed.reset_sched == ()
```

`_sched_record` needs `rsched` and `limit_rsched` keyword arguments, mirroring its existing
`sched` / `limit_sched` pair; add them in the same commit.

- [ ] **Step 6: Run the aggregator tests and watch them fail**

```bash
uv run pytest tests/unit/test_processor.py -k "AggregatorAppliesResets or ResetSchedIsCarried" -v
```

Expected: `TypeError: LimitRefillInfo.__init__() got an unexpected keyword argument
'reset_sched'` from the state helper, and
`TypeError: _sched_record() got an unexpected keyword argument 'rsched'` from the parse class.

- [ ] **Step 7: Implement the aggregator half**

Add `reset_sched: tuple[ScheduleEntry, ...] = ()` to `ParsedBucketLimit` (:307),
`ParsedBucketRecord` (:319), `LimitRefillInfo` (:109) and `BucketRefillState` (:123), populate
them in `_parse_bucket_record` through `_decode_schedule`'s sibling — reuse `_decode_schedule`'s
`(value, error)` shape so an undecodable `rsched` folds into the same `sched_error` channel:

```python
def _decode_reset_schedule(
    compact: str | None, tz: str
) -> tuple[tuple[ScheduleEntry, ...], str | None]:
    """Decode a stored compact reset schedule into ``(schedule, error)``.

    Reported rather than raised, for the identical reason ``_decode_schedule``
    is: ``aggregate_bucket_states`` runs outside any try block
    (``processor.py:216``), so a raise here is a poison pill for the whole
    batch. §6 puts the decision about an unreadable schedule on the client,
    where the operator's ``on_unavailable`` setting lives.
    """
    if not compact:
        return (), None
    try:
        return decode_reset(compact, tz), None
    except ValueError as e:
        return (), f"{compact!r} ({tz}): {e}"
```

In `_item_next_boundary` (:621), fold the reset tuples into the candidate set so a reset-only
item still gets a `vu`:

```python
def _item_next_boundary(state: BucketRefillState, now_ms: int) -> int | None:
    pairs = {(state.sched, state.reset_sched)} | {
        (info.sched or state.sched, info.reset_sched or state.reset_sched)
        for info in state.limits.values()
    }
    boundaries = [
        b
        for sched, reset in pairs
        if (sched or reset) and (b := next_boundary(sched, reset, now_ms=now_ms)) is not None
    ]
    return min(boundaries) if boundaries else None
```

In `try_refill_bucket` (:638), inside the per-limit loop, after the effective params are
computed and before `refill_bucket` runs:

```python
reset_sched = () if limit_name == WCU_LIMIT_NAME else (info.reset_sched or state.reset_sched)
reset_edge = prev_reset_edge(reset_sched, now_ms) if reset_sched else None
if reset_edge is not None and reset_edge > state.rf_ms:
    # A reset is "set the balance to the effective capacity", and as an
    # ADD that is `eff_cp - tk_observed` — the identical delta shape the
    # unconditional clamp uses, and safe for the identical commutativity
    # reason: it removes exactly the surplus (or adds exactly the
    # shortfall) while concurrent consumption subtracts independently.
    # It bypasses the consumption threshold below for the same reason
    # the negative clamp does: a hot bucket is where the aggregator is
    # the only refiller, so gating the reset there turns it off exactly
    # where it matters (§3.3, §3.6).
    refill_delta = effective_cp - info.tk_milli
    if refill_delta != 0:
        any_needs_refill = True
        add_parts.append(f"{bucket_attr(limit_name, BUCKET_FIELD_TK)} :rd_{limit_name}")
        expr_values[f":rd_{limit_name}"] = refill_delta
    continue
```

`wcu` is exempted by the `reset_sched` expression above; `tc` is never in this expression at
all, so it stays monotonic for free.

- [ ] **Step 8: Run the aggregator tests and watch them pass**

```bash
uv run pytest tests/unit/test_processor.py -k "AggregatorAppliesResets or ResetSchedIsCarried" -v
uv run pytest tests/unit/ -q
uv run pytest tests/unit/ -m gevent -n 0 -q
```

Never add `-o "addopts="` to the `tests/unit/` runs — it un-skips the gevent tests into the
same process as the asyncio ones and hangs with no output.

- [ ] **Step 9: Regenerate sync, lint, type check, commit**

`limiter.py` and `lease.py` are sync-codegen sources; `processor.py` is not.

```bash
hatch run generate-sync
uv run ruff check --fix .
uv run ruff format src/zae_limiter src/zae_limiter_aggregator tests/unit
uv run mypy
git add -A
git commit -m "$(cat <<'EOF'
✨ feat(limiter): reset the balance at a calendar edge

A crossed reset edge sets the balance to the shard's share of the
capacity in force at that instant, *before* admission — so the request
that crosses midnight is gated against the restored quota, not the one
after it. Detection is backwards (`prev_reset_edge > rf`), which makes
an idle bucket correct for free and two missed edges idempotent.

lease.py needs no reset code: its existing refill delta
(tokens - original + consumed) already resolves to `eff_cp - stored_tk`,
the same ADD shape the aggregator writes. `tc` is never in that
expression, so the counter stays monotonic — the reason this is safer
than the reset_bucket() #471 proposed.

`vu` now folds the reset tuple into next_boundary on both the client and
the aggregator: a limit with a reset schedule and no parameter schedule
would otherwise never expire `vu`, and the daily quota is exactly that
shape.

Refs #222
EOF
)"
```

---

### Task 4: Reset encoding

**Files:**
- Modify: `src/zae_limiter/schedule.py` (`__all__` :31, `_encode_cron` :438, `_tokenise` :494, `_cron_from_tokens` :514, beside `encode` :470 / `decode` :518), `src/zae_limiter/schema.py` (`BUCKET_FIELD_SCHED` :81, `LIMIT_FIELD_SCHED` :114), `src/zae_limiter/models.py` (`Limit.__post_init__` :255, `to_dict` :403, `from_dict` :419), `src/zae_limiter/repository.py` (`_serialize_composite_limits` :4971, `_deserialize_limits` :5010, `_build_bucket_param_update` :3256, `build_composite_create` :2079)
- Test: `tests/unit/test_schedule_encoding.py`, `tests/unit/test_models.py`, `tests/unit/test_repository.py`

**Interfaces:**
- Consumes: `ScheduleEntry.reset(cron, tz)` (**Task 1** — it does not exist in merged
  `schedule.py`; `__post_init__` there requires exactly one modifier, which a reset entry has
  none of). The merged field encoder (`_encode_cron`, `_encode_field`, `_tokenise`,
  `_cron_from_tokens`) is reused unchanged.
- Produces: `encode_reset(sched) -> tuple[str, str | None]`, `decode_reset(compact, tz) ->
  tuple[ScheduleEntry, ...]`; `schema.BUCKET_FIELD_RSCHED`, `schema.LIMIT_FIELD_RSCHED`;
  `l_{name}_rsched` on config items and `rsched` / `b_{name}_rsched` on bucket items

Reset entries encode into their own attributes with the same grammar **minus the modifier
tokens** — `0 0 * * *` is `m0h0`, four bytes. A separate attribute rather than a tag inside
`sched` mirrors the separate tuple and keeps the decoder from partitioning one list into two
meanings (§4.1).

**No version marker.** Design §4.1 promises one; core plan Task 5 shipped without it and said
so in its ledger entry. Task 10 owns that decision and takes it — see Task 10's first ruling.
Do **not** add a marker here on the strength of §4.1's sentence; it would break
`test_compact_shape` and every pinned fixture string in Tasks 8, 12, 13, 14 and surface Task 8,
for a distinction Task 10 concludes is not worth buying.

**`ScheduleEntry.reset()` is the only constructor `decode_reset` may use.** Building entries
with plain `ScheduleEntry(cron=...)` raises — `__post_init__` requires exactly one modifier
when `_reset` is False. Core plan Task 5's ledger flags this explicitly for whoever writes
this function.

**One hoisted timezone per item, across *both* tuples.** `sched_tz` is a single item-level
attribute (§4.1) and `encode`/`encode_reset` each return their own `tz`. Merged
`Limit.__post_init__` (`models.py:255-264`) validates only that `self.schedule`'s entries agree.
A limit whose `schedule` is `America/New_York` and whose `reset_schedule` is `UTC` therefore
constructs, serialises, and then comes back with one of the two silently reinterpreted in the
other's zone — forever, with no error anywhere. That is the same class of defect core plan
Task 8 found when `sched_tz` was written inside the per-limit loop. Widen the guard to the
union of both tuples, here rather than in Task 1, because the reason is storage and storage is
this task's subject.

**Three write paths carry a schedule, and only one of them is in the compressed text.**

| Item | Attribute | Written by | Read by |
|------|-----------|------------|---------|
| Config (system / resource / entity) | `l_{name}_rsched` | `_serialize_composite_limits` :4971 | `_deserialize_limits` :5010 |
| Bucket, on a limit change | `rsched`, `b_{name}_rsched` | `_build_bucket_param_update` :3256 | the aggregator |
| Bucket, on creation | `rsched`, `b_{name}_rsched` | `build_composite_create` :2079 | the aggregator |

The compressed text named only `_sync_bucket_params` and `build_composite_create`, and both
namings were wrong in a way worth stating:

1. **The config-item leg was missing entirely.** Surface Task 1 adds `Limit.reset_schedule` as
   an in-memory field and touches `schedule.py` and `models.py` only. Nothing persists it, so
   `resolve_limits()` returns limits whose `reset_schedule` is always `()` and Task 3's
   client-side reset never fires for stored config — which is every real deployment. This task
   adds `l_{name}_rsched` beside the `l_{name}_sched` core plan Task 8 merged, reusing the same
   hoisted `sched_tz`.
2. **The bucket fan-out lives in `_build_bucket_param_update` (:3256), not
   `_sync_bucket_params` (:3085).** `_sync_bucket_params` discovers shards and issues writes;
   the expression is built by `_build_bucket_param_update`, which is also what
   `_resolved_bucket_param_update` (:3209) calls per resource under the entity-wide
   `_default_` scope (#487). Editing the wrong one of the two leaves the `_default_` path
   unstamped.

**`build_composite_create` already stamps `sched`; add `rsched` beside it.** This was written
as a warning that nothing stamped the *parameter* schedule on a newly created bucket. The core
plan has since closed it: a bucket created by the slow path now carries `sched` / `sched_tz` /
`b_{name}_sched` and starts at the *scheduled* per-shard share (design §2.2, recorded in
CLAUDE.md's speculative-write section). So this task adds `rsched` to an existing stamp rather
than introducing one. Verify that before writing Step 7 — the reasoning still holds if it ever
regresses: a bucket created with `vu` and no schedule re-materialises at its first boundary and
then refills at the **base** rate forever, because both the aggregator and the slow path read
the schedule off the item, and a reset stamp without the parameter stamp is incoherent on its
own.

**`Limit.to_dict()` feeds the audit event `details`.** Core plan Task 8 found that a
`to_dict()` which drops the schedule makes the audit record for "attached a business-hours
schedule" byte-identical to one that attached nothing, and fixed it for `schedule`.
`reset_schedule` inherits the same requirement, emitted as **standard 5-field cron** — §4 lists
audit events explicitly among the boundaries where the compact form must not appear.

- [ ] **Step 1: Write the failing encoding test**

In `tests/unit/test_schedule_encoding.py`, beside the merged `encode`/`decode` classes.

```python
class TestResetEncoding:
    """Reset entries share the field grammar and drop the modifier tokens."""

    def test_encodes_without_a_modifier_token(self):
        compact, tz = encode_reset((ScheduleEntry.reset("0 0 * * *", "America/New_York"),))
        assert compact == "m0h0"
        assert tz == "America/New_York"

    def test_a_daily_reset_is_four_bytes(self):
        """The size claim in §4.1, asserted exactly rather than as `<= 8` —
        an encoder that returned the empty string would satisfy a bound."""
        compact, _ = encode_reset((ScheduleEntry.reset("0 0 * * *"),))
        assert len(compact) == 4

    def test_joins_entries_with_a_semicolon(self):
        compact, tz = encode_reset(
            (
                ScheduleEntry.reset("0 0 * * *", "America/New_York"),
                ScheduleEntry.reset("0 12 * * SUN", "America/New_York"),
            )
        )
        assert compact == "m0h0;m0h12w7"
        assert tz == "America/New_York"

    def test_weekday_names_normalise_exactly_as_the_param_encoder(self):
        """Storage is canonical (§4.3) so `differ.py` does not read SUN against
        7 as a change on every apply. Sunday's spelling is the case core plan
        Task 5 got wrong first time round, so it is pinned here too."""
        compact, _ = encode_reset((ScheduleEntry.reset("0 0 * * SUN-THU"),))
        assert compact == "m0h0w0-4"

    def test_empty_schedule_encodes_to_nothing(self):
        assert encode_reset(()) == ("", None)

    def test_rejects_entries_that_disagree_on_timezone(self):
        with pytest.raises(ValueError, match="one timezone"):
            encode_reset(
                (
                    ScheduleEntry.reset("0 0 * * *", "America/New_York"),
                    ScheduleEntry.reset("0 0 * * *", "UTC"),
                )
            )


class TestResetDecoding:
    def test_decodes_through_the_reset_constructor(self):
        """A reset entry carries no modifier, so `ScheduleEntry(...)` would
        raise its "exactly one" rule. `decode_reset` must use the classmethod."""
        (entry,) = decode_reset("m0h0", "America/New_York")
        assert entry.cron == "0 0 * * *"
        assert entry.tz == "America/New_York"
        assert entry.scale is None
        assert entry.capacity is None
        assert entry.refill_amount is None
        assert entry.refill_period_seconds is None

    def test_empty_compact_decodes_to_an_empty_tuple(self):
        assert decode_reset("", "UTC") == ()

    def test_rejects_a_modifier_token(self):
        """A reset overrides no parameters, so a stored `s500` is either
        corruption or a param schedule read out of the wrong attribute. Either
        way it must not decode into something that silently resets."""
        with pytest.raises(ValueError, match="modifier"):
            decode_reset("m0h0s500", "UTC")

    def test_rejects_junk(self):
        with pytest.raises(ValueError):
            decode_reset("this is not a schedule", "UTC")

    def test_round_trip_is_byte_identical_and_semantically_equal(self):
        """Three assertions, because `encode_reset(decode_reset(x)) == x` alone
        is satisfied by an encoder that throws information away — the exact
        criticism core plan Task 5's ledger makes of its own plan text."""
        entries = (
            ScheduleEntry.reset("0 0 * * *", "America/New_York"),
            ScheduleEntry.reset("30 2 1 JAN,JUL *", "America/New_York"),
        )
        compact, tz = encode_reset(entries)
        restored = decode_reset(compact, tz)

        assert len(restored) == len(entries)
        for original, back in zip(entries, restored, strict=True):
            assert parse_cron(back.cron, back.tz) == parse_cron(original.cron, original.tz)
        assert encode_reset(restored) == (compact, tz)
        assert decode_reset(*encode_reset(restored)) == restored

    def test_the_display_form_re_encodes_unchanged(self):
        """`to_cron` renders names back; feeding that to a fresh reset entry
        must produce the same bytes, or the CLI's output is not round-trippable
        (§4.3)."""
        compact, tz = encode_reset((ScheduleEntry.reset("0 0 * * 1-5", "UTC"),))
        rendered = to_cron(compact)
        assert rendered == "0 0 * * MON-FRI"
        assert encode_reset((ScheduleEntry.reset(rendered, tz),)) == (compact, tz)
```

- [ ] **Step 2: Run the encoding tests and watch them fail**

Run: `uv run pytest tests/unit/test_schedule_encoding.py -k "ResetEncoding or ResetDecoding" -v`
Expected: `NameError: name 'encode_reset' is not defined` at collection — the test module
imports it from `zae_limiter.schedule` and the import fails first, so the actual line is
`ImportError: cannot import name 'encode_reset' from 'zae_limiter.schedule'`.

- [ ] **Step 3: Implement the codec**

In `schedule.py`, below `decode` (:541). Both functions reuse the merged field helpers; the
only new logic is rejecting modifier tags on the way back in.

```python
def encode_reset(sched: tuple[ScheduleEntry, ...]) -> tuple[str, str | None]:
    """Encode a reset schedule into its compact storage form and shared timezone.

    The same grammar as ``encode`` minus the modifier tokens, because a reset
    entry overrides no parameters (§1.1). ``0 0 * * *`` is ``m0h0``, four bytes.
    Returns ``("", None)`` for an empty schedule, and raises if the entries
    disagree on ``tz`` — it is hoisted to one item-level attribute shared with
    the parameter schedule, so a limit cannot carry two.
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

    Built through ``ScheduleEntry.reset``, never ``ScheduleEntry(...)``: a reset
    entry carries no modifier and the ordinary constructor requires exactly one.

    A modifier tag in this attribute is rejected rather than ignored. It means
    either corruption or a parameter schedule stored under the wrong key, and
    an entry that silently reset the balance on a schedule meant to scale it
    would be the worst possible reading.
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
```

Add both names to `__all__` (:31).

- [ ] **Step 4: Run the encoding tests and watch them pass**

Run: `uv run pytest tests/unit/test_schedule_encoding.py -v`

- [ ] **Step 5: Write the failing model and storage tests**

In `tests/unit/test_models.py`:

```python
class TestResetScheduleSerialisation:
    RESET = (ScheduleEntry.reset(cron="0 0 * * *", tz="America/New_York"),)

    def test_to_dict_emits_standard_cron(self):
        """Audit events are one of §4's standard-cron boundaries, and
        `to_dict()` is what all three setters put in the event `details`."""
        limit = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")
        assert limit.to_dict()["reset_schedule"] == [
            {"cron": "0 0 * * *", "tz": "America/New_York"}
        ]

    def test_to_dict_omits_an_absent_reset_schedule(self):
        """Existing audit payloads and their tests stay byte-identical."""
        assert "reset_schedule" not in Limit.per_day("rpd", 10_000).to_dict()

    def test_from_dict_restores_it(self):
        limit = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")
        assert Limit.from_dict(limit.to_dict()) == limit

    def test_rejects_a_timezone_disagreement_across_the_two_tuples(self):
        """One hoisted `sched_tz` per item covers both tuples (§4.1). Without
        this guard one of the two is silently reinterpreted in the other's
        zone on the way back out of storage.

        The disagreement is introduced by `with_schedule` on an existing quota,
        not by `with_reset_schedule` on a drip: under ADR-137 the latter raises
        about `refill_amount` first and this test would pass for the wrong
        reason — `match="timezone"` is what keeps that honest.
        """
        with pytest.raises(ValueError, match="timezone"):
            Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="UTC").with_schedule(
                (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", scale=0.5),)
            )
```

In `tests/unit/test_repository.py`:

```python
class TestResetScheduleReachesStorage:
    RESET = (ScheduleEntry.reset(cron="0 0 * * *", tz="America/New_York"),)

    async def test_config_round_trips_the_reset_schedule(self, repo):
        """Without this leg `resolve_limits()` returns reset_schedule=() for
        every stored limit and Task 3's client reset never fires."""
        limit = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")
        await repo.set_limits("rs-1", [limit], resource="gpt-4")

        (stored,) = await repo.get_limits("rs-1", resource="gpt-4")
        assert stored.reset_schedule == self.RESET

    async def test_config_stores_the_compact_form_under_its_own_attribute(self, repo):
        """`rsched`, not a tag inside `sched` (§4.1)."""
        limit = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")
        await repo.set_limits("rs-2", [limit], resource="gpt-4")

        item = await _raw_config_item(repo, "rs-2", "gpt-4")
        assert item[schema.limit_attr("rpd", schema.LIMIT_FIELD_RSCHED)]["S"] == "m0h0"
        assert schema.limit_attr("rpd", schema.LIMIT_FIELD_SCHED) not in item
        assert item[schema.CONFIG_FIELD_SCHED_TZ]["S"] == "America/New_York"

    async def test_replacing_a_limit_without_one_removes_it(self, repo):
        """All three setters are full-replace PutItems (core plan Task 8), so
        an omitted attribute disappears — confirmed, not assumed."""
        await repo.set_limits(
            "rs-3",
            [Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")],
            resource="gpt-4",
        )
        await repo.set_limits("rs-3", [Limit.per_day("rpd", 10_000)], resource="gpt-4")

        (stored,) = await repo.get_limits("rs-3", resource="gpt-4")
        assert stored.reset_schedule == ()

    async def test_the_fan_out_stamps_rsched_on_an_existing_bucket(self, repo):
        await repo.create_entity("rs-4", parent_id=None, name="rs-4")
        await repo.set_limits("rs-4", [Limit.per_day("rpd", 10_000)], resource="gpt-4")
        await repo.speculative_consume("rs-4", "gpt-4", {"rpd": 1})

        await repo.set_limits(
            "rs-4",
            [Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")],
            resource="gpt-4",
        )

        item = await _raw_bucket_item(repo, "rs-4", "gpt-4", shard=0)
        assert item[schema.BUCKET_FIELD_RSCHED]["S"] == "m0h0"
        assert item[schema.BUCKET_FIELD_SCHED_TZ]["S"] == "America/New_York"

    async def test_removing_a_reset_schedule_removes_the_stamp(self, repo):
        """Override, not merge (§1.6): dropping a reset must clear the item, or
        the bucket keeps resetting after the operator stopped asking it to."""
        await repo.create_entity("rs-5", parent_id=None, name="rs-5")
        await repo.set_limits(
            "rs-5",
            [Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")],
            resource="gpt-4",
        )
        await repo.speculative_consume("rs-5", "gpt-4", {"rpd": 1})

        await repo.set_limits("rs-5", [Limit.per_day("rpd", 10_000)], resource="gpt-4")

        item = await _raw_bucket_item(repo, "rs-5", "gpt-4", shard=0)
        assert schema.BUCKET_FIELD_RSCHED not in item

    async def test_a_created_bucket_carries_the_stamp(self, repo):
        """A bucket created on the slow path must be born with its schedules.
        Both refillers read them off the item, so a bucket carrying `vu` and no
        schedule re-materialises at its first boundary and then refills at the
        base rate forever."""
        limiter = RateLimiter(repository=repo, speculative_writes=False)
        await repo.set_limits(
            "rs-6",
            [Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")],
            resource="gpt-4",
        )
        async with limiter.acquire("rs-6", "gpt-4", consume={"rpd": 1}):
            pass

        item = await _raw_bucket_item(repo, "rs-6", "gpt-4", shard=0)
        assert item[schema.BUCKET_FIELD_RSCHED]["S"] == "m0h0"
```

`_raw_config_item` mirrors the `_raw_bucket_item` helper core plan Task 12 adds:

```python
async def _raw_config_item(repo, entity_id, resource):
    """Read an entity config item straight from DynamoDB, undeserialised."""
    client = await repo._get_client()
    response = await client.get_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": schema.pk_entity(repo._namespace_id, entity_id)},
            "SK": {"S": schema.sk_config(resource)},
        },
    )
    return response["Item"]
```

- [ ] **Step 6: Run the model and storage tests and watch them fail**

```bash
uv run pytest tests/unit/test_models.py -k ResetScheduleSerialisation -v
uv run pytest tests/unit/test_repository.py -k ResetScheduleReachesStorage -v
```

Expected: `KeyError: 'reset_schedule'` from `to_dict`, and
`AttributeError: module 'zae_limiter.schema' has no attribute 'LIMIT_FIELD_RSCHED'` from the
storage class.

- [ ] **Step 7: Implement the storage legs**

Two constants in `schema.py`, in the blocks their siblings already occupy — deliberately two
names with the same value, exactly as `LIMIT_FIELD_SCHED` / `BUCKET_FIELD_SCHED` are (core
plan Task 8's ruling):

```python
# in the BUCKET_FIELD_* block, beside BUCKET_FIELD_SCHED (:81)
BUCKET_FIELD_RSCHED = "rsched"  # item-level default reset schedule (§3.6, §4.1)

# in the LIMIT_FIELD_* block, beside LIMIT_FIELD_SCHED (:114)
LIMIT_FIELD_RSCHED = "rsched"  # compact-encoded reset schedule (#222 §4.1)
```

`models.py` — widen the timezone guard in `Limit.__post_init__` (:258) to the union, and carry
the tuple through `to_dict`/`from_dict`:

```python
        if self.schedule or self.reset_schedule:
            zones = {entry.tz for entry in (*self.schedule, *self.reset_schedule)}
            if len(zones) > 1:
                raise ValueError(
                    f"all schedule entries on one limit must share a timezone, got "
                    f"{sorted(zones)}. The timezone is stored once per item as "
                    f"`sched_tz` and covers the parameter schedule and the reset "
                    f"schedule together, not per entry and not per tuple."
                )
```

```python
if self.reset_schedule:
    result["reset_schedule"] = [{"cron": e.cron, "tz": e.tz} for e in self.reset_schedule]
```

`repository.py` — in `_serialize_composite_limits` (:4971), beside the `LIMIT_FIELD_SCHED`
write, reusing the `hoisted_schedule_timezone(limits)` call that already runs once before the
loop (core plan Task 8). Widen that helper's input to both tuples in the same edit; it is the
function that stops two limits in different zones sharing one `sched_tz`, and it must now also
stop a limit's two tuples doing it:

```python
            if limit.reset_schedule:
                compact, _tz = schedule.encode_reset(limit.reset_schedule)
                base_item[schema.limit_attr(name, schema.LIMIT_FIELD_RSCHED)] = {"S": compact}
```

and in `_deserialize_limits` (:5010), beside the `sched_attr` read:

```python
rsched_attr = item.get(schema.limit_attr(name, schema.LIMIT_FIELD_RSCHED), {}).get("S")
limits.append(
    Limit(
        name=name,
        capacity=_get(schema.LIMIT_FIELD_CP),
        refill_amount=_get(schema.LIMIT_FIELD_RA),
        refill_period_seconds=_get(schema.LIMIT_FIELD_RP),
        schedule=schedule.decode(sched_attr, sched_tz) if sched_attr else (),
        reset_schedule=(schedule.decode_reset(rsched_attr, sched_tz) if rsched_attr else ()),
    )
)
```

In `_build_bucket_param_update` (:3256), mirror the `sched` handling core plan Task 13 adds —
an item-level default plus a per-limit override only where a limit differs, and a REMOVE in the
`else` branch. `vu = 0` is already written unconditionally outside both branches by Task 13 and
must **not** be duplicated or moved (#488: `SET` and `REMOVE` on one attribute in one
expression is a `ValidationException`):

```python
reset_scheduled = [limit for limit in limits if limit.reset_schedule]
if reset_scheduled:
    encodings = {
        limit.name: schedule.encode_reset(limit.reset_schedule) for limit in reset_scheduled
    }
    default_compact, default_tz = next(iter(encodings.values()))
    set_parts.append("#rsched = :rsched")
    expr_names["#rsched"] = schema.BUCKET_FIELD_RSCHED
    expr_values[":rsched"] = {"S": default_compact}
    # `sched_tz` is shared with the parameter schedule and the limit's
    # own validation guarantees they agree, so write it only if the
    # parameter branch did not.
    if "#sched_tz" not in expr_names:
        set_parts.append("#sched_tz = :sched_tz")
        expr_names["#sched_tz"] = schema.BUCKET_FIELD_SCHED_TZ
        expr_values[":sched_tz"] = {"S": default_tz or "UTC"}
    for i, (name, (compact, _tz)) in enumerate(encodings.items()):
        if compact == default_compact:
            continue
        alias = f"#lrsched{i}"
        set_parts.append(f"{alias} = :lrsched{i}")
        expr_names[alias] = schema.bucket_attr(name, schema.BUCKET_FIELD_RSCHED)
        expr_values[f":lrsched{i}"] = {"S": compact}
else:
    expr_names["#rsched"] = schema.BUCKET_FIELD_RSCHED
    remove_parts.append("#rsched")
    for i, limit in enumerate(limits):
        alias = f"#lrsched{i}"
        expr_names[alias] = schema.bucket_attr(limit.name, schema.BUCKET_FIELD_RSCHED)
        remove_parts.append(alias)
```

In `build_composite_create` (:2079), stamp both schedules onto the new item. The states handed
to this builder carry no `Limit`, so take the schedules from the `BucketState.sched` /
`BucketState.reset_sched` fields (core plan Task 9 and surface Task 5 add them) or add an
explicit parameter — whichever the builder's existing call sites make cleanest. Whatever you
choose, `test_a_created_bucket_carries_the_stamp` is the assertion that it worked.

- [ ] **Step 8: Run everything and watch it pass**

```bash
uv run pytest tests/unit/test_schedule_encoding.py tests/unit/test_models.py -v
uv run pytest tests/unit/test_repository.py -k ResetSchedule -v
uv run pytest tests/unit/ -q
uv run pytest tests/unit/ -m gevent -n 0 -q
```

- [ ] **Step 9: Regenerate sync, lint, type check, commit**

`repository.py` is a sync-codegen source; `schedule.py`, `models.py` and `schema.py` are not.

```bash
hatch run generate-sync
uv run ruff check --fix .
uv run ruff format src/zae_limiter tests/unit
uv run mypy
git add -A
git commit -m "$(cat <<'EOF'
✨ feat(schema): encode and persist reset schedules

encode_reset/decode_reset share the field grammar with the parameter
encoder and drop the modifier tokens, so `0 0 * * *` stores as `m0h0` —
four bytes. Reset entries live in their own `rsched` / `b_{name}_rsched`
attributes rather than tagged inside `sched`, mirroring the separate
tuple and keeping the decoder from partitioning one list into two
meanings.

Three write paths carry it, not the one the plan named: the config item
(`l_{name}_rsched`, without which resolve_limits() always returns an
empty reset schedule and nothing ever fires), the #468 bucket fan-out,
and bucket creation. Limit.to_dict() carries it too, as standard cron,
so the audit record for attaching a daily reset is not byte-identical to
attaching nothing.

The one hoisted `sched_tz` now covers both tuples, so a limit cannot
carry a business-hours schedule in New York and a reset in UTC and have
one of them silently reinterpreted.

No version marker: see Task 10 for that decision and its cost.

Refs #222
EOF
)"
```

---

### Task 5: Boundary-aware `retry_after_seconds`

**Files:**
- Modify: `src/zae_limiter/schedule.py` (beside `next_boundary` :260 and `prev_reset_edge`, Task 2), `src/zae_limiter/models.py` (`BucketState` :622), `src/zae_limiter/bucket.py` (`try_consume`'s failure branch :152-166), `src/zae_limiter/lease.py` (`_build_retry_failure_statuses` :673), `src/zae_limiter/limiter.py` (`check_availability` :1941, specifically :2047-2070), `src/zae_limiter/repository.py` (`_deserialize_composite_bucket` :4878)
- Test: `tests/unit/test_schedule_boundary.py`, `tests/unit/test_bucket.py`, `tests/unit/test_limiter.py`

**Interfaces:**
- Consumes: `next_boundary(sched, reset_sched=(), *, now_ms)` honouring both tuples (Task 2);
  `prev_reset_edge` (Task 2); `effective_params` (core plan Task 3); `BucketState.sched` and
  the `effective_*(now_ms)` methods (core plan Task 9); `decode_reset` and the `rsched`
  attribute (Task 4)
- Produces: `retry_after_with_schedule(deficit_milli, cp_milli, ra_milli, rp_ms, sched,
  reset_sched=(), *, now_ms, shard_count=1, max_windows=8) -> float`;
  `next_reset_edge(reset_sched, *, now_ms) -> int | None`; `BucketState.reset_sched`
- Already landed (#530): `bucket.calculate_retry_after` takes `next_reset_ms` (an absolute
  epoch-ms instant) and `now_ms`, and returns the wait to that instant when the refill rate is
  0. All four `LimitStatus` sites already call it with `next_reset_ms=None` and a real
  `now_ms`, each marked `TODO(#222 surface-plan Task 5)`. **Supplying `next_reset_edge(
  state.reset_sched, now_ms=now_ms)` at those four `TODO`s is the whole of the wiring** — the
  wait arithmetic is not this task's to write

**The flat estimate is wrong in the direction that matters.** It over-reports when a boundary
raises the limit and **under**-reports when one lowers it — and lowering is the headline use
case. The spec's worked example, re-derived against merged `bucket.py` rather than quoted:

> Empty bucket, 500 tokens needed, 1000/min now, boundary in 10 s dropping to 500/min.
>
> Flat: `calculate_retry_after(500_000, 1_000_000, 60_000)` is
> `(500_000 * 60_000) // 1_000_000 = 30_000 ms`, plus the rounding millisecond — **30.001 s**.
>
> Real: the first 10 s yield `(10_000 * 1_000_000) // 60_000 = 166_666` millitokens, leaving
> 333_334; at half rate that is `(333_334 * 60_000) // 500_000 = 40_000 ms`. Total **50.001 s**.

**The example holds exactly.** It is quoted in the design (§7) and in the compressed version of
this task, and both numbers survive the arithmetic in merged `refill_bucket` and
`calculate_retry_after`. Keep them.

**But that example does not exercise the thing core plan Task 4 fixed.** Its boundary is 09:00
in `America/New_York`, whose UTC offset is a whole number of hours, so the coarse hourly probe
lands exactly on the edge and the two-phase refinement never runs. Task 4 measured the original
single-phase scan returning a *late* boundary on 112 of 400 random schedules (worst case 21.5
hours) precisely because window edges fall on local minutes while the probe grid is aligned to
the UTC epoch. Any arithmetic in this task that was written against the old behaviour is
suspect. Add `Asia/Kolkata` (+05:30) to the cases below, where a 09:00 local edge falls at
03:30Z — half a step off an hourly grid — so this task's walk is pinned against the corrected
`next_boundary` rather than against a zone that cannot tell the two apart.

**A reset edge dominates.** If a `reset_schedule` edge falls before the deficit clears by
refill, that instant *is* the answer. For a daily quota it is the *entire* answer, since
ADR-137 leaves such a limit no drip at all: the choice is between reporting "retry now" —
wrong, repeatedly, for as long as the quota stays exhausted — and reporting "at midnight".

> ### ✅ Under ADR-137 a quota's rate is **zero** — settled by #530, and the walk below honours it
>
> ADR-137 was accepted after this task was written: a limit drips **or** resets, so every
> limit that carries a `reset_schedule` has `refill_amount == 0`. That is not a variant case
> here — it is the *only* shape a reset can arrive in. The original Step 3 loop tested the
> rate before it consulted the reset edge, so every quota left on iteration 1 and fell back to
> `calculate_retry_after(deficit, 0, rp)`, which returned `0.0` — "retry immediately", forever,
> until the edge actually passed. The precise opposite of this section's headline, and silent.
>
> **#530 settled both halves of that**, and Step 3's sketch below has been corrected to match:
>
> 1. **`bucket.calculate_retry_after` now takes the reset instant**, as
>    `calculate_retry_after(deficit_milli, refill_amount_milli, refill_period_ms,
>    next_reset_ms=None, *, now_ms=None)`. A positive rate is unchanged arithmetic and ignores
>    the instant; a zero rate with a known reset returns the wait until it; a zero rate with no
>    reset still returns `0.0`, a branch ADR-137 makes unreachable for any constructible limit
>    and which therefore once again means what its comment claims — a corrupt stored item. The
>    four `LimitStatus` sites in the table below all pass `None` today, because the real value
>    needs `BucketState.reset_sched`, which **this task adds**. *Task 5's job is therefore to
>    supply the value, not to re-derive the wait.*
> 2. **The walk consults the reset edge before the rate gate.** A window whose effective rate
>    is zero accrues nothing, so the answer is the edge when the edge arrives first, and
>    otherwise the walk steps to the next boundary where the rate may resume. That covers the
>    quota (`ra == 0` in every window, and `next_boundary` honours `reset_sched`, so the
>    boundary *is* the edge) and a `scale=0.0` window on a limit that does drip, with one
>    branch rather than a quota special case.
>
> The three tests below that paired a positive `ra_milli` with a `reset_sched` — a combination
> ADR-137 forbids outright, reachable through this function's raw-integer signature but through
> no limit a caller can configure — have been rewritten to the quota shape
> (`ra_milli=0`/`refill_amount_milli=0`). Note that
> `test_a_reset_edge_after_the_deficit_clears_does_not_win` could not be corrected in place:
> with `ra == 0` the deficit never clears by refill, so "the reset does not win" is
> unconstructible for a quota. It is replaced by the discriminator that survives ADR-137 — a
> quota whose deficit is **already clear** must report no wait rather than its next edge.

**There are four sites that build a `LimitStatus`, not the three the compressed text names.**
Enumerated against the merged tree rather than recalled:

| # | Site | Reached when | Source of `retry_after_seconds` |
|---|------|--------------|---------------------------------|
| 1 | `bucket.build_limit_status` via `declared_statuses` / `would_refill_satisfy` (:355, :388) | **speculative fast rejection** — the common case | `try_consume` → `calculate_retry_after` |
| 2 | `limiter._admit_limit` (:1410-1419) | slow-path admission | `try_consume` → `calculate_retry_after` |
| 3 | `lease._build_retry_failure_statuses` (:673) | slow-path optimistic-lock retry | `calculate_retry_after` directly |
| 4 | `limiter.check_availability` (:2060) | the non-consuming query | `calculate_retry_after` directly |

1 and 2 share one seam — `bucket.try_consume`'s failure branch — so converting `try_consume`
covers both. 3 and 4 compute directly and are converted individually. The compressed text
named 2, 3 and 4 and missed 1, which is the path most rejections actually take.

**`try_consume` needs both schedules on the `BucketState`, and as the core plan stands nothing
puts them there.** Core plan Task 9 adds the `sched` **field**; Tasks 12/13 write the `sched`
**attribute**; no task reads the attribute back in `_deserialize_composite_bucket` (:4878),
which is what builds every `BucketState` the client sees — including the ALL_OLD states behind
site 1. Verify that before writing Step 5. If it is still unpopulated, this task adds both
`sched` and `reset_sched` there, because a schedule-aware retry estimate computed from an empty
`sched` is the flat estimate with extra steps and a green test suite.

**`check_availability` also reports the wrong *capacity*, not just the wrong wait.** Two sites
in it use the **base** `limit.capacity`: the clamp `min(totals[limit.name], limit.capacity)`
(:2049) and the missing-bucket branch that reports `limit.capacity` outright (:2052). Inside a
`scale: 0.5` window both over-report by 2x. Core plan Task 9 makes `calculate_available`
schedule-aware inside `bucket.py`, and Task 10 fixes `Limit.from_bucket_state` on the rejection
path — neither reaches these two, because they work from the `Limit` resolved out of *config*,
not from a `BucketState`. Core plan Task 9's ledger entry confirms this is still true after its
own conversion: "I converted only the three `BucketState` accesses there."

**`check_availability` must also apply a *pending* reset.** It reads buckets and writes
nothing, so a bucket that crossed a reset edge and has not yet been touched by a request still
holds the burnt balance on disk. Without an adjustment the display says "0 remaining" and, once
the walk lands, "resets at midnight tomorrow" — while the very next `acquire()` restores the
quota immediately. The fix is the same read-time computation the slow path does, and costs
nothing: if `prev_reset_edge(limit.reset_schedule, now_ms) > bucket.last_refill_ms`, that
bucket's contribution is its effective share rather than `calculate_available(...)`.

**#473 landed; write against the real API.** `RateLimiter.check_availability(entity_id,
resource, needed=None, limits=None) -> Availability` (`limiter.py:1941`), with `available()`
(:2088) and `time_until_available()` (:2135) as thin wrappers over it. Converting
`check_availability` converts all three. Missing it would leave `acquire()` saying "at
midnight" while `check_availability()` said "in eleven hours" about the same bucket at the same
instant — the design's own reason (§7) for wiring the query surface.

- [ ] **Step 1: Write the failing walk test**

In `tests/unit/test_schedule_boundary.py`, reusing its `_ms` / `_iso` helpers (`_ms` already
defaults to `America/New_York`).

```python
IST_BUSINESS = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="Asia/Kolkata", scale=0.5),)
DAILY = (ScheduleEntry.reset(cron="0 0 * * *", tz="America/New_York"),)


class TestNextResetEdge:
    def test_finds_the_next_midnight(self):
        assert _iso(next_reset_edge(DAILY, now_ms=_ms("2026-09-15 09:00"))).startswith(
            "2026-09-16T00:00"
        )

    def test_standing_on_an_edge_returns_the_following_one(self):
        """Strictly after `now`, for the same reason `next_boundary` is: an
        answer at `now` makes a wait of zero look like a wait until a reset."""
        assert _iso(next_reset_edge(DAILY, now_ms=_ms("2026-09-16 00:00"))).startswith(
            "2026-09-17T00:00"
        )

    def test_a_never_matching_expression_has_no_edge(self):
        never = (ScheduleEntry.reset(cron="0 0 30 2 *"),)  # February 30th
        assert next_reset_edge(never, now_ms=_ms("2026-09-15 09:00")) is None

    def test_empty_reset_schedule(self):
        assert next_reset_edge((), now_ms=_ms("2026-09-15 09:00")) is None


class TestBoundaryAwareRetryAfter:
    BASE = dict(cp_milli=1_000_000, ra_milli=1_000_000, rp_ms=60_000)

    def test_the_specs_worked_example(self):
        """Flat says 30.001 s; the truth is 50.001 s — 10 s yielding 166_666
        millitokens, then 333_334 remaining at half rate."""
        got = retry_after_with_schedule(
            deficit_milli=500_000,
            **self.BASE,
            sched=BUSINESS,
            now_ms=_ms("2026-09-15 08:59:50"),
        )
        assert got == pytest.approx(50.001, abs=0.002)

    def test_the_flat_estimate_is_the_wrong_answer(self):
        """Discriminates the test above against an implementation that walks
        but never applies the window's effective rate."""
        flat = calculate_retry_after(500_000, 1_000_000, 60_000)
        assert flat == pytest.approx(30.001, abs=0.002)

    def test_a_non_whole_hour_offset_is_handled(self):
        """Asia/Kolkata is +05:30, so a 09:00 local edge is 03:30Z — half a
        step off the hourly probe grid. This is the case core plan Task 4's
        two-phase refinement exists for; America/New_York cannot tell a correct
        scan from a late one, because its offset is a whole number of hours."""
        got = retry_after_with_schedule(
            deficit_milli=500_000,
            **self.BASE,
            sched=IST_BUSINESS,
            now_ms=_ms("2026-09-15 08:59:50", IST),
        )
        assert got == pytest.approx(50.001, abs=0.002)

    def test_a_boundary_that_raises_the_limit_shortens_the_wait(self):
        """The over-reporting direction. From 08:59:50 the base 1000/min needs
        30 s; the 09:00 window doubles the rate, so 10 s of base refill leaves
        333_334 to clear at 2000/min = 10.000 s. Total 20.001 s."""
        doubling = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=2.0),)
        got = retry_after_with_schedule(
            deficit_milli=500_000,
            **self.BASE,
            sched=doubling,
            now_ms=_ms("2026-09-15 08:59:50"),
        )
        assert got == pytest.approx(20.001, abs=0.002)

    def test_a_reset_edge_dominates(self):
        """A daily quota's answer is 'at midnight', and it is the *only*
        answer: ADR-137 gives a reset-carrying limit `ra_milli == 0`, so there
        is no drip to fall back on. Before #530 this reported 0.0 — 'retry
        immediately', an hour early and repeatedly.
        """
        got = retry_after_with_schedule(
            deficit_milli=5_000_000,
            cp_milli=10_000_000,
            ra_milli=0,  # ADR-137: a limit drips or resets, never both
            rp_ms=86_400_000,
            sched=(),
            reset_sched=DAILY,
            now_ms=_ms("2026-09-15 23:00"),
        )
        assert got == pytest.approx(3600.001, abs=0.002)

    def test_a_quota_with_no_deficit_does_not_report_its_next_reset(self):
        """Discriminates the test above against "a quota always returns its
        next edge". Replaces the pre-ADR-137
        `test_a_reset_edge_after_the_deficit_clears_does_not_win`, whose
        premise — refill clearing the deficit before the edge — needs the
        positive rate beside a reset that ADR-137 forbids, and so cannot be
        built for a quota at all."""
        got = retry_after_with_schedule(
            deficit_milli=0,
            cp_milli=10_000_000,
            ra_milli=0,
            rp_ms=86_400_000,
            sched=(),
            reset_sched=DAILY,
            now_ms=_ms("2026-09-15 23:00"),
        )
        assert got == 0.0

    def test_a_zero_rate_with_no_reset_is_still_no_wait(self):
        """The other half of the discrimination: the edge, not the zero rate,
        is what produces a non-zero answer above. Unreachable through a
        constructible limit — it guards a corrupt stored item."""
        got = retry_after_with_schedule(
            deficit_milli=5_000_000,
            cp_milli=10_000_000,
            ra_milli=0,
            rp_ms=86_400_000,
            sched=(),
            reset_sched=(),
            now_ms=_ms("2026-09-15 23:00"),
        )
        assert got == 0.0

    def test_unscheduled_matches_calculate_retry_after_exactly(self):
        """Not 'approximately' — the unscheduled path must be the identical
        integer arithmetic, or every existing retry assertion in the suite
        drifts by a millisecond."""
        got = retry_after_with_schedule(
            deficit_milli=500_000, **self.BASE, sched=(), now_ms=_ms("2026-09-15 10:00")
        )
        assert got == calculate_retry_after(500_000, 1_000_000, 60_000)

    def test_a_cleared_deficit_is_no_wait(self):
        got = retry_after_with_schedule(
            deficit_milli=0, **self.BASE, sched=BUSINESS, now_ms=_ms("2026-09-15 10:00")
        )
        assert got == 0.0

    def test_the_shard_share_is_applied_inside_each_window(self):
        """Scale first, then divide (Global Constraints). Half of 1000/min
        across 2 shards is 250/min, so 500 tokens take 120 s, not 60."""
        got = retry_after_with_schedule(
            deficit_milli=500_000,
            **self.BASE,
            sched=BUSINESS,
            now_ms=_ms("2026-09-15 10:00"),
            shard_count=2,
        )
        assert got == pytest.approx(120.001, abs=0.002)

    def test_a_share_that_floors_to_zero_falls_back_to_the_undivided_rate(self):
        """Same rule as `BucketState.retry_refill_amount_milli` (#475): a share
        of zero has no finite wait, so report the undivided *scheduled* rate
        rather than dividing by zero or returning 0.0."""
        got = retry_after_with_schedule(
            deficit_milli=500,
            cp_milli=1_000,
            ra_milli=1_000,
            rp_ms=60_000,
            sched=BUSINESS,
            now_ms=_ms("2026-09-15 10:00"),
            shard_count=1024,
        )
        assert got == pytest.approx(calculate_retry_after(500, 500, 60_000), abs=0.002)

    def test_falls_back_to_the_flat_estimate_past_the_walk_cap(self):
        """A schedule that alternates every minute against a deficit that takes
        an hour exhausts the 8-window budget. The fallback must be the flat
        estimate, not a partial walk reported as if it were complete."""
        alternating = (ScheduleEntry(cron="*/2 * * * *", tz="UTC", scale=0.001),)
        got = retry_after_with_schedule(
            deficit_milli=10_000_000,
            **self.BASE,
            sched=alternating,
            now_ms=_ms("2026-09-15 10:00:00"),
        )
        assert got == calculate_retry_after(10_000_000, 1_000_000, 60_000)

    def test_the_walk_cap_is_honoured_rather_than_looping(self):
        """Pins the budget itself: nine windows must not be walked. Verified by
        counting boundary lookups rather than by timing."""
        alternating = (ScheduleEntry(cron="*/2 * * * *", tz="UTC", scale=0.001),)
        with patch("zae_limiter.schedule.next_boundary", wraps=next_boundary) as spy:
            retry_after_with_schedule(
                deficit_milli=10_000_000,
                **self.BASE,
                sched=alternating,
                now_ms=_ms("2026-09-15 10:00:00"),
            )
        assert spy.call_count == 8
```

- [ ] **Step 2: Run the walk tests and watch them fail**

Run: `uv run pytest tests/unit/test_schedule_boundary.py -k "NextResetEdge or BoundaryAwareRetryAfter" -v`
Expected: `ImportError: cannot import name 'retry_after_with_schedule' from
'zae_limiter.schedule'` at collection.

- [ ] **Step 3: Implement the walk**

In `schedule.py`. `next_reset_edge` is the forward twin of Task 2's `prev_reset_edge`; if Task 2
implemented `next_boundary`'s reset handling through an internal helper, promote that helper
rather than writing a second scanner — two scans of the same expression that can disagree is a
worse outcome than either.

```python
def next_reset_edge(reset_sched: tuple[ScheduleEntry, ...], *, now_ms: int) -> int | None:
    """The first reset edge strictly after ``now_ms``, or None within the cap.

    The forward twin of ``prev_reset_edge``: same adaptive granularity, same
    horizon, same "no edge within the cap means the expression never matches"
    reading (``0 0 30 2 *``). ``now_ms`` is keyword-only for the same reason
    ``next_boundary``'s is (#500).
    """


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

    ``cp_milli``/``ra_milli``/``rp_ms`` are the **undivided base** — the values
    stored on the item, which scheduling never rewrites (§2.1) — and the shard
    share is taken *after* ``effective_params``, per the scale-then-divide rule.

    The walk steps window by window: at each one it asks how long the current
    effective rate needs, and whether a boundary or a reset edge arrives first.
    A reset edge inside the window is the answer outright, because it restores
    the whole balance in one lump. Capped at ``max_windows``, after which it
    falls back to the flat estimate rather than reporting a partial walk as a
    complete one.

    Returns the identical value ``calculate_retry_after`` does when neither
    tuple is set, so the unscheduled path is unchanged to the millisecond.
    """
    if deficit_milli <= 0:
        return 0.0

    def _rate(ra: int) -> int:
        # A share that floors to zero has no finite wait; fall back to the
        # undivided *scheduled* rate, exactly as retry_refill_amount_milli does
        # (#475). Falling back to the base rate would quote a speed nothing in
        # the system refills at during the window.
        return (ra // shard_count) or ra

    remaining = deficit_milli
    cursor = now_ms
    for _ in range(max_windows):
        _eff_cp, eff_ra, eff_rp = effective_params(cp_milli, ra_milli, rp_ms, sched, cursor)
        rate = _rate(eff_ra)
        edge = next_reset_edge(reset_sched, now_ms=cursor)
        boundary = next_boundary(sched, reset_sched, now_ms=cursor)

        # The edge is consulted BEFORE the rate gate (#530). Under ADR-137 a
        # quota's rate is zero in *every* window, so gating on the rate first
        # exits on iteration 1 and never reaches the edge that is the answer.
        if rate <= 0:
            # Nothing accrues in this window. The edge wins if it lands inside
            # it; otherwise step to the boundary, where the rate may resume.
            if edge is not None and (boundary is None or edge <= boundary):
                return (edge - now_ms + 1) / 1000.0
            if boundary is None:
                break  # no rate, no edge, no boundary — no finite wait
            cursor = boundary  # next_boundary is strictly after cursor, so this advances
            continue

        need_ms = (remaining * eff_rp) // rate
        window_end = boundary if boundary is not None else cursor + need_ms

        if edge is not None and edge <= min(window_end, cursor + need_ms):
            return (edge - now_ms + 1) / 1000.0
        if cursor + need_ms <= window_end:
            return (cursor + need_ms - now_ms + 1) / 1000.0

        remaining -= ((window_end - cursor) * rate) // eff_rp
        cursor = window_end

    return calculate_retry_after(
        deficit_milli,
        _rate(ra_milli),
        rp_ms,
        next_reset_edge(reset_sched, now_ms=now_ms),
        now_ms=now_ms,
    )
```

`calculate_retry_after` lives in `bucket.py`, which imports `models`, which imports
`schedule` — so importing it here is a cycle. Inline the same arithmetic instead, with a
comment naming `bucket.calculate_retry_after` as the definition this must stay identical to;
`test_unscheduled_matches_calculate_retry_after_exactly` is what keeps them so. **The inlined
copy must carry #530's zero-rate branch too** — the `max_windows` fallback is exactly where a
quota that outran the walk lands, and an inlined copy that stops at the rate arithmetic
reintroduces the `0.0` this task exists to remove. The fallback recomputes the edge from
`now_ms`, not from the walk's `cursor`, because the value it returns is a wait measured from
the caller's instant.

- [ ] **Step 4: Run the walk tests and watch them pass**

Run: `uv run pytest tests/unit/test_schedule_boundary.py -v`

- [ ] **Step 5: Write the failing call-site tests**

In `tests/unit/test_bucket.py` (sites 1 and 2, through `try_consume`) and
`tests/unit/test_limiter.py` (site 4).

```python
class TestTryConsumeWalksBoundaries:
    """`try_consume` feeds both the fast-rejection statuses (via
    `declared_statuses`) and slow-path admission (via `_admit_limit`), so
    converting it converts two of the four LimitStatus sites at once."""

    def _state(self, **kwargs) -> BucketState:
        base = dict(
            entity_id="user-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=0,
            last_refill_ms=_ny("2026-09-15 08:59:50"),
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )
        base.update(kwargs)
        return BucketState(**base)

    def test_a_lowering_boundary_lengthens_the_estimate(self):
        result = try_consume(self._state(sched=BUSINESS), 500, _ny("2026-09-15 08:59:50"))
        assert result.success is False
        assert result.retry_after_seconds == pytest.approx(50.001, abs=0.002)

    def test_the_same_bucket_unscheduled_reports_the_flat_estimate(self):
        """Discriminates the test above."""
        result = try_consume(self._state(), 500, _ny("2026-09-15 08:59:50"))
        assert result.retry_after_seconds == pytest.approx(30.001, abs=0.002)

    def test_a_reset_edge_dominates_the_estimate(self):
        """`refill_amount_milli=0` is not an edge case here — ADR-137 makes it
        the only shape a `reset_sched` can arrive in, so this is what every
        quota rejection on the fast path looks like (#530)."""
        state = self._state(
            limit_name="rpd",
            capacity_milli=10_000_000,
            refill_amount_milli=0,  # ADR-137: a limit drips or resets
            refill_period_ms=86_400_000,
            last_refill_ms=_ny("2026-09-15 23:00"),
            reset_sched=DAILY,
        )
        result = try_consume(state, 5_000, _ny("2026-09-15 23:00"))
        assert result.success is False
        assert result.retry_after_seconds == pytest.approx(3600.001, abs=0.002)

    def test_a_successful_consume_still_reports_no_wait(self):
        result = try_consume(
            self._state(tokens_milli=1_000_000, sched=BUSINESS), 500, _ny("2026-09-15 10:00")
        )
        assert result.success is True
        assert result.retry_after_seconds == 0.0


class TestDeserialisedBucketsCarryBothSchedules:
    """The ALL_OLD states behind the fast-rejection path come from
    `_deserialize_composite_bucket`; an empty `sched` there makes every
    schedule-aware estimate above silently flat in production."""

    async def test_sched_and_rsched_reach_bucket_state(self, repo):
        # A quota may carry a *parameter* schedule as well — `with_schedule`
        # never touches `refill_amount`, so the intermediate value is already a
        # legal quota (Task 1). Only the reset has to arrive with the amount.
        limit = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York").with_schedule(
            (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", scale=0.5),)
        )
        await repo.create_entity("bs-1", parent_id=None, name="bs-1")
        await repo.set_limits("bs-1", [limit], resource="gpt-4")
        await repo.speculative_consume("bs-1", "gpt-4", {"rpd": 1})

        bucket = next(
            b for b in await repo.get_buckets("bs-1", resource="gpt-4") if b.limit_name == "rpd"
        )
        assert bucket.sched == limit.schedule
        assert bucket.reset_sched == limit.reset_schedule
```

```python
class TestCheckAvailabilityIsScheduleAware:
    """The query surface must agree with the rejection path at one instant."""

    NIGHT = (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", scale=0.5),)

    async def test_reports_the_scheduled_capacity_for_a_missing_bucket(self, limiter):
        """The no-bucket branch reports `limit.capacity` outright; inside a
        0.5x window that is twice what the first acquire would admit."""
        repo = limiter._repository
        repo._now_ms = lambda: _ny("2026-09-16 03:00")
        await repo.invalidate_config_cache()
        await repo.set_limits(
            "ca-1", [Limit.per_minute("rpm", 1000).with_schedule(self.NIGHT)], resource="gpt-4"
        )

        check = await limiter.check_availability("ca-1", "gpt-4")
        assert check.status("rpm").available == 500

    async def test_clamps_a_live_balance_to_the_scheduled_capacity(self, limiter):
        """The other base-capacity site: `min(total_across_shards,
        limit.capacity)`. A bucket full at 1000 entering a 0.5x window reports
        500, not 1000 — the surplus is unspendable (§3.3)."""
        repo = limiter._repository
        await repo.set_limits(
            "ca-2", [Limit.per_minute("rpm", 1000).with_schedule(self.NIGHT)], resource="gpt-4"
        )
        repo._now_ms = lambda: _ny("2026-09-15 14:00")  # outside the window
        await repo.invalidate_config_cache()
        async with limiter.acquire("ca-2", "gpt-4", consume={"rpm": 1}):
            pass

        repo._now_ms = lambda: _ny("2026-09-16 03:00")  # inside it
        await repo.invalidate_config_cache()
        check = await limiter.check_availability("ca-2", "gpt-4")
        assert check.status("rpm").available == 500

    async def test_the_wait_walks_boundaries(self, limiter):
        """`available()` and `time_until_available()` are thin wrappers over
        this, so all three surfaces convert together."""
        repo = limiter._repository
        await repo.set_limits("ca-3", [RPD], resource="gpt-4")
        repo._now_ms = lambda: _ny("2026-09-15 23:00")
        await repo.invalidate_config_cache()
        async with limiter.acquire("ca-3", "gpt-4", consume={"rpd": 10_000}):
            pass

        check = await limiter.check_availability("ca-3", "gpt-4", needed={"rpd": 5_000})
        assert check.status("rpd").retry_after_seconds == pytest.approx(3600.001, abs=0.5)

    async def test_a_pending_reset_is_reflected_in_the_balance(self, limiter):
        """The bucket crossed midnight and nothing has touched it since, so
        disk still holds the burnt balance. Without this the display says
        "0 remaining, resets tomorrow" while the very next acquire restores the
        quota immediately."""
        repo = limiter._repository
        await repo.set_limits("ca-4", [RPD], resource="gpt-4")
        repo._now_ms = lambda: _ny("2026-09-15 23:00")
        await repo.invalidate_config_cache()
        async with limiter.acquire("ca-4", "gpt-4", consume={"rpd": 10_000}):
            pass

        repo._now_ms = lambda: _ny("2026-09-16 00:30")
        await repo.invalidate_config_cache()
        check = await limiter.check_availability("ca-4", "gpt-4", needed={"rpd": 5_000})
        assert check.status("rpd").available == 10_000
        assert check.status("rpd").retry_after_seconds == 0.0

    async def test_an_unscheduled_entity_is_unchanged(self, limiter):
        """Every existing check_availability assertion in the suite must still
        hold; this is the regression guard for the two capacity sites."""
        repo = limiter._repository
        await repo.set_limits("ca-5", [Limit.per_minute("rpm", 1000)], resource="gpt-4")
        check = await limiter.check_availability("ca-5", "gpt-4", needed={"rpm": 1})
        assert check.status("rpm").available == 1000
        assert check.status("rpm").retry_after_seconds == 0.0
```

- [ ] **Step 6: Run the call-site tests and watch them fail**

```bash
uv run pytest tests/unit/test_bucket.py -k "TryConsumeWalksBoundaries or DeserialisedBuckets" -v
uv run pytest tests/unit/test_limiter.py -k CheckAvailabilityIsScheduleAware -v
```

Expected: `TypeError: BucketState.__init__() got an unexpected keyword argument 'reset_sched'`
from the bucket class, and from the limiter class `assert 1000 == 500` on the two capacity
tests — the base capacity reported inside a 0.5x window, which is the defect.

- [ ] **Step 7: Implement the four sites**

Add `reset_sched: tuple[ScheduleEntry, ...] = ()` to `BucketState` (`models.py:622`), beside
the `sched` field core plan Task 9 adds, and populate both in
`_deserialize_composite_bucket` (`repository.py:4878`) from the item's `sched` / `rsched` and
the hoisted `sched_tz`, honouring a `b_{name}_sched` / `b_{name}_rsched` override where one is
present — the same default-plus-override rule the aggregator applies (core plan Task 14).

Site 1+2 — `bucket.py`'s `try_consume` failure branch (:152-166):

```python
        deficit_milli = requested_milli - current_tokens_milli
        retry_after = retry_after_with_schedule(
            deficit_milli=deficit_milli,
            cp_milli=state.capacity_milli,
            ra_milli=state.refill_amount_milli,
            rp_ms=state.refill_period_ms,
            sched=state.sched,
            reset_sched=state.reset_sched,
            now_ms=now_ms,
            shard_count=state.shard_count,
        )
```

The undivided base and `shard_count` go in, not the effective values: the walk re-evaluates
`effective_params` per window and divides afterwards, so handing it pre-divided numbers would
apply the shard split twice. `calculate_time_until_available` (:227) takes the same change;
`calculate_retry_after` itself is unchanged and stays the definition the walk falls back to.

Site 3 — `lease.py`'s `_build_retry_failure_statuses` (:673):

```python
        retry_after = retry_after_with_schedule(
            deficit_milli=deficit_milli,
            cp_milli=entry.state.capacity_milli,
            ra_milli=entry.state.refill_amount_milli,
            rp_ms=entry.state.refill_period_ms,
            sched=entry.limit.schedule,
            reset_sched=entry.limit.reset_schedule,
            now_ms=now_ms,
            shard_count=entry.state.shard_count,
        )
```

`now_ms` is already threaded into this function by core plan Task 9.

Site 4 — `limiter.py`'s `check_availability` (:2047-2070). Four edits in one loop:

```python
        for limit in resolved_limits:
            eff_cp, eff_ra, eff_rp = effective_params(
                limit.capacity * 1000,
                limit.refill_amount * 1000,
                limit.refill_period_seconds * 1000,
                limit.schedule,
                now_ms,
            )
            ceiling = max(1, eff_cp // 1000)
            if limit.name in totals:
                available = min(totals[limit.name], ceiling)
            else:
                # No bucket yet: the first acquire creates it at the capacity
                # in force now, not at the base.
                available = ceiling
            if limit.name in pending_reset:
                # The bucket crossed a reset edge and nothing has written to it
                # since, so disk still holds the burnt balance. The next
                # acquire restores it; say so rather than reporting a wait the
                # caller will never actually serve.
                available = ceiling
            requested = needed.get(limit.name, 0)
            exceeded = requested > 0 and available < requested

            wait = 0.0
            if exceeded:
                # Shards are summed above, so the walk is handed the undivided
                # base with shard_count=1 — the sum of the shares *is* the
                # undivided rate, modulo flooring.
                wait = retry_after_with_schedule(
                    deficit_milli=(requested - available) * 1000,
                    cp_milli=limit.capacity * 1000,
                    ra_milli=limit.refill_amount * 1000,
                    rp_ms=limit.refill_period_seconds * 1000,
                    sched=limit.schedule,
                    reset_sched=limit.reset_schedule,
                    now_ms=now_ms,
                )
```

`pending_reset` is collected in the bucket loop above, where `last_refill_ms` is in scope:

```python
        pending_reset: set[str] = set()
        for bucket in await self._repository.get_buckets(entity_id):
            # ... the existing totals / refill_milli / period_ms accumulation ...
            if bucket.reset_sched:
                edge = prev_reset_edge(bucket.reset_sched, now_ms)
                if edge is not None and edge > bucket.last_refill_ms:
                    pending_reset.add(name)
```

The reported `limit` on the `LimitStatus` stays the **undivided configured** limit, unlike the
per-shard statuses in `RateLimitExceeded` (`Limit.per_shard()`, #475) — that asymmetry is
already documented in `check_availability`'s docstring and is unchanged here.

- [ ] **Step 8: Run the call-site tests, then the suite**

```bash
uv run pytest tests/unit/test_bucket.py tests/unit/test_limiter.py -v
uv run pytest tests/unit/ -q
uv run pytest tests/unit/ -m gevent -n 0 -q
```

- [ ] **Step 9: Regenerate sync, lint, type check, commit**

`bucket.py`, `schedule.py` and `models.py` are not codegen sources; `lease.py`, `limiter.py`
and `repository.py` are.

```bash
hatch run generate-sync
uv run ruff check --fix .
uv run ruff format src/zae_limiter tests/unit
uv run mypy
git add -A
git commit -m "$(cat <<'EOF'
✨ feat(bucket): compute retry_after across schedule boundaries

The flat estimate divides a deficit by the rate in force *now*, which
over-reports when a boundary raises the limit and under-reports when one
lowers it — and lowering is the headline case. retry_after_with_schedule
walks window by window at each window's effective rate, capped at eight
windows with a fall back to the flat estimate, and returns a reset edge
outright when one lands first: for a daily quota the only useful answer
is "at midnight", not eleven hours of drip.

Wired into all FOUR LimitStatus sites, not the three the plan listed.
bucket.try_consume covers the speculative fast rejection and slow-path
admission at once; lease._build_retry_failure_statuses and
RateLimiter.check_availability convert individually. Missing
check_availability would have left acquire() saying "at midnight" while
the display said "in eleven hours" about the same bucket.

check_availability also stopped reporting the base capacity inside a
scale window — both the clamp and the missing-bucket branch worked from
the config-resolved Limit, out of reach of the BucketState conversions —
and now reflects a reset that has fired but not yet been materialised.

Refs #222, #472, #475
EOF
)"
```

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

**Decision: in YAML, a non-empty `reset_schedule` flips the `refill_amount` default from
`capacity` to `0`.** The author never types the zero:

```yaml
limits:
  rpd:
    capacity: 10000
    reset_schedule:
      - cron: "0 0 * * *"
        tz: America/New_York
```

This is forced by [ADR-137](../adr/137-reset-replaces-drip.md), which was accepted after this
task was written. Without the flip, the shorthand default fills `refill_amount = capacity`,
producing a positive rate **and** a reset — precisely the configuration ADR-137 rejects. The
manifest that reads most naturally would be the one that fails, and it would fail with a
message about a field the author never wrote.

**Exactly two existing lines change** (`manifest.py:28` and the loop at `:30-40`):

1. `refill_amount = d.get("refill_amount", capacity)` becomes conditional on whether
   `reset_schedule` is present and non-empty — that single boolean is the whole discriminator.
2. `refill_amount` comes **out** of the shared `value <= 0` loop and gets its own rule: zero is
   permitted, but only alongside a reset. `capacity` and `refill_period` stay strictly positive
   and stay in the loop.

**An explicit non-zero `refill_amount` written beside a `reset_schedule` is still an error.** A
stated conflict is loud and worth rejecting; an omission just picks the right default. (An
explicit `refill_amount: 0` beside a reset is fine, and must be — `to_dict()` always emits the
field, so it is what a round trip produces.)

Two facts this task should not have to rediscover:

- **The provisioner never constructs `Limit` objects.** `applier.py:58` writes
  `l_{name}_{cp,ra,rp}` attributes straight onto the config item from the manifest dict, so
  there is no "which factory does the parser call" question to answer here. `Limit.quota()`
  (Task 1) is for humans writing Python; the YAML path never touches it, and the two surfaces
  agree because they enforce the same rule, not because they share code.
- **`from_dict`'s existing error message already says** "Limits are rejected at parse time so
  `limits plan` surfaces the problem before anything is written." That behaviour is the point
  and must be preserved for the new rule: a bad quota is caught by `limits plan`, in the CLI,
  before anything reaches the table.

`LimitDecl` currently has **no** `schedule` or `reset_schedule` fields at all — Task 6 adds
both. Everything above is therefore a constraint on code this task is about to write, not a
change to code that exists.

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

    def test_a_reset_schedule_flips_the_refill_amount_default_to_zero(self):
        """ADR-137: a quota does not drip, and the author never types the zero.
        The old default (`refill_amount = capacity`) would build the one
        configuration the ADR rejects out of the most natural manifest."""
        manifest = LimitsManifest.from_yaml(YAML)
        assert manifest.resources["gpt-4"].limits["rpd"].refill_amount == 0

    def test_an_unscheduled_limit_still_defaults_to_capacity(self):
        """The flip is conditional on the reset, and on nothing else."""
        manifest = LimitsManifest.from_yaml(YAML)
        rpm = manifest.resources["gpt-4"].limits["rpm"]
        assert rpm.refill_amount == 1000

    def test_rejects_an_explicit_rate_beside_a_reset(self):
        """A stated conflict is loud; only an omission gets the default."""
        bad = YAML.replace(
            "        capacity: 10000\n",
            "        capacity: 10000\n        refill_amount: 10000\n",
        )
        with pytest.raises(ValueError, match="reset"):
            LimitsManifest.from_yaml(bad)

    def test_an_explicit_zero_beside_a_reset_is_accepted(self):
        """`to_dict()` always emits refill_amount, so this is what a round trip
        produces; rejecting it would make the manifest unable to restate itself."""
        decl = LimitDecl.from_dict(
            {
                "capacity": 10_000,
                "refill_amount": 0,
                "reset_schedule": [{"cron": "0 0 * * *", "tz": "America/New_York"}],
            }
        )
        assert decl.refill_amount == 0

    def test_rejects_a_zero_rate_without_a_reset(self):
        """ADR-137: never neither. capacity and refill_period stay strictly
        positive; only refill_amount gained the conditional zero."""
        with pytest.raises(ValueError, match="reset_schedule"):
            LimitDecl.from_dict({"capacity": 10_000, "refill_amount": 0})

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

    def test_to_dict_round_trips_a_quota(self):
        """The emitted `refill_amount: 0` must survive re-parsing — the applier
        writes `l_rpd_ra = 0` from it, and a CFN round trip re-reads it."""
        decl = LimitDecl.from_dict(
            {
                "capacity": 10_000,
                "reset_schedule": [{"cron": "0 0 * * *", "tz": "America/New_York"}],
            }
        )
        assert decl.to_dict()["refill_amount"] == 0
        assert LimitDecl.from_dict(decl.to_dict()) == decl

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
        refill_period = d.get("refill_period", 60)

        # ADR-137: a limit drips or resets, never both. A reset flips the
        # shorthand default from `capacity` to 0 so the natural manifest — one
        # that names only the allowance and the schedule — is the valid one.
        resets = bool(d.get("reset_schedule"))
        refill_amount = d.get("refill_amount", 0 if resets else capacity)

        for field_name, value in (
            ("capacity", capacity),
            ("refill_period", refill_period),
        ):
            if value <= 0:
                raise ValueError(
                    f"{field_name} must be positive, got {value}. "
                    "Limits are rejected at parse time so `limits plan` surfaces the "
                    "problem before anything is written."
                )
        if refill_amount < 0:
            raise ValueError(f"refill_amount must not be negative, got {refill_amount}")
        if refill_amount == 0 and not resets:
            raise ValueError(
                "refill_amount=0 means the limit does not drip, which is only valid "
                "with a reset_schedule; otherwise the bucket can never recover (ADR-137)."
            )
        if refill_amount > 0 and resets:
            raise ValueError(
                "a limit drips or resets, never both: a positive refill_amount "
                f"({refill_amount}) alongside a reset_schedule grants roughly twice "
                "the intended allowance per period. Omit refill_amount and it "
                "defaults to 0 (ADR-137)."
            )
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
            result["reset_schedule"] = [{"cron": e.cron, "tz": e.tz} for e in self.reset_schedule]
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

The `capacity` / `refill_period` positivity validation above is the rule already on `main`
(`manifest.py:30-40`, also issue #481's Task 6); keep it, and change only what the decision
above names — `refill_amount` leaving that loop, its conditional default, and the two schedule
fields.

`differ.py` needs **no change** — it passes `to_dict()` through into `Change.data` already.

- [ ] **Step 4: Run the test and watch it pass**

Run: `uv run pytest tests/unit/test_provisioner_manifest.py tests/unit/test_differ.py -v`
Expected: PASS

- [ ] **Step 5: Lint, type check, commit**

```bash
# Never bare `uv run ruff format .` (Global Constraints) — it reformats 34
# unrelated files, these plan documents included. pre-commit runs ruff
# check and format at the pinned 0.9.2 over exactly these paths.
pre-commit run --files src/zae_limiter_provisioner/manifest.py tests/unit/test_provisioner_manifest.py tests/unit/test_differ.py
uv run mypy
git add src/zae_limiter_provisioner/manifest.py tests/unit/test_provisioner_manifest.py
git commit -m "$(cat <<'EOF'
✨ feat(provisioner): parse schedules from the limits manifest

LimitDecl gains `schedule` and `reset_schedule`, parsed into
ScheduleEntry so cron, timezone and the reset-entry rules are validated
at parse time — `limits plan` now rejects an unusable manifest before
anything is written, rather than the Lambda raising at apply.

A non-empty reset_schedule flips the refill_amount shorthand default
from `capacity` to 0 (ADR-137), so the natural quota manifest — an
allowance and a schedule, no zero typed — is the valid one rather than
the rejected drip-and-reset pairing. An explicit non-zero rate beside a
reset stays an error.

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
                        "reset_schedule": [{"cron": "0 0 * * *", "tz": "America/New_York"}],
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
        assert limits["rpd"]["ResetSchedule"] == [{"Cron": "0 0 * * *", "Tz": "America/New_York"}]

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
# Never bare `uv run ruff format .` (Global Constraints) — it reformats 34
# unrelated files, these plan documents included. pre-commit runs ruff
# check and format at the pinned 0.9.2 over exactly these paths.
pre-commit run --files src/zae_limiter_provisioner/bucket_sync.py src/zae_limiter/infra/provisioner_builder.py tests/unit/test_provisioner_bucket_sync.py tests/unit/test_provisioner_builder.py
uv run mypy
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
- Modify: `src/zae_limiter_provisioner/bucket_sync.py` (`build_bucket_param_update` at :83, **`_decode_limits` at :315 — see the landmine below**), `src/zae_limiter/infra/provisioner_builder.py` (:131-137)
- Test: `tests/unit/test_provisioner_bucket_sync.py`, `tests/unit/test_provisioner_builder.py`

**Interfaces:**
- Consumes: `LimitDecl.to_dict()` shape (Task 6), `encode` (core plan Task 5)
- Produces: `build_bucket_param_update` emitting `sched` / `sched_tz` / `rsched`; `_decode_limits` admitting the widened shape

> ### ⚠️ The exact-set filter in `_decode_limits` — this task is what breaks it
>
> `src/zae_limiter_provisioner/bucket_sync.py:334`, the closing comprehension of
> **`_decode_limits()`** (defined at `:315`), filters every decoded limit through an
> **exact set equality**:
>
> ```python
>     return {
>         name: decl
>         for name, decl in partial.items()
>         if decl.keys() == {"capacity", "refill_amount", "refill_period"}
>     }
> ```
>
> **It is correct today, and it is not an ADR-137 conflict.** A quota still decodes to those
> same three keys — ADR-137 only sets `refill_amount` to 0, it does not remove the field. The
> filter's purpose is to drop a *malformed* limit (one missing cp, ra or rp) rather than
> synthesise a default for it, and that purpose is untouched.
>
> **This task is what breaks it.** Task 8 widens the decoded shape with `schedule` and
> `reset_schedule`, and the moment a scheduled limit carries a fourth or fifth key the set
> stops matching and **that limit silently disappears from `resolve_bucket_limits()`** — and
> from `resolve_effective_limits()`, which shares `_walk()`. There is no exception and no log
> line: the comprehension simply does not yield it. Downstream, `_resolved_plan()` sees a
> bucket whose resolved limits omit the scheduled one and stamps the bucket as though the
> operator had deleted it. A manifest apply that adds a schedule would quietly *unstamp* the
> limit it was meant to schedule.
>
> Two smaller pieces of the same widening, so they are not rediscovered separately:
> `_MANIFEST_KEY` (`:60`) maps only `cp`/`ra`/`rp`, so an `l_{name}_sched` attribute is
> currently skipped before it ever reaches `partial`; and the loop body decodes every value as
> `int(value["N"])`, which a schedule (`{"S": ...}`) is not.
>
> **The fix belongs in this task, and it must be decided rather than discovered.** A superset
> check, an explicit required-keys/optional-keys split, or something else is Task 8's call —
> but make the call deliberately, in Step 3, and pin it with a test that a scheduled limit
> survives `resolve_bucket_limits()`. **The plan does not mention this anywhere else**; nothing
> downstream will catch it, because the failure mode is a silent omission rather than an error.

**This is where the provisioner plan and the core plan meet.** `build_bucket_param_update`
already exists on `main` with the signature
`(limits, ttl_multiplier, stale_limit_names, now_ms)` returning
`(update_expr, expr_names, expr_values)` — read it before editing. Without PR #485's handler
wiring this does nothing, so that must land first.

**`vu = 0` is already unconditional, and must stay that way (#488).** Core plan Task 13 landed
`set_parts.append("#vu = :vu_zero")` outside any `if`, scheduled or not, with a comment saying
`#vu` is SET so it must **never** join `remove_parts` — SETting and REMOVEing one attribute in
a single expression is a DynamoDB `ValidationException`. Step 3's sketch below still shows an
`else:` branch REMOVEing `#vu`, and `test_unscheduled_limits_remove_the_stamps` still asserts
it; both predate Task 13 and both must be dropped. Only `sched` / `sched_tz` / `rsched` belong
in that REMOVE branch. The merged comment also says `sched`/`sched_tz` are "deliberately left
alone here" because schedules are not manifest-expressible yet — this task is what makes them
expressible, so lifting that exemption is part of the work and the comment must be updated with
it.

**Core plan Task 14 already adds `schedule.py` to `provisioner_builder.py`.** If Task 14 has
landed, the vendoring test here is a regression guard rather than new work; if it has not, add
the `shutil.copy2` line. `bucket.py` is still not vendored into the provisioner and does not need
to be — this task encodes a schedule, it does not evaluate one.

**The `_default_` path is no longer incomplete.** #487 landed: `sync_bucket_params` translates
the entity-wide `_default_` scope into an **unscoped** `BUCKET#` discovery and resolves limits
per bucket resource through `resolve_bucket_limits()` / `_resolved_plan()`. A schedule set on
an entity's `_default_` config therefore reaches every one of that entity's buckets — which is
precisely why the `_decode_limits` landmine above matters on this path as much as any other.

- [ ] **Step 1: Write the failing test**

```python
class TestProvisionerStampsSchedules:
    SCHEDULED = {
        "rpm": {
            "capacity": 1000,
            "refill_amount": 1000,
            "refill_period": 60,
            "schedule": [{"cron": "* 9-17 * * MON-FRI", "tz": "America/New_York", "scale": 0.5}],
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
        assert values[":vu_zero"] == {"N": "0"}
        assert "#sched = :sched" in expr
        assert "#vu = :vu_zero" in expr

    def test_vu_is_zero_not_a_computed_boundary(self):
        """A future vu would leave the fast path spending a surplus over a
        lowered ceiling until natural refill caught up (§3.4). Already true on
        `main` since core plan Task 13; this is a regression guard."""
        _expr, _names, values = build_bucket_param_update(
            self.SCHEDULED, ttl_multiplier=0, stale_limit_names=None, now_ms=1_789_000_000_000
        )
        assert values[":vu_zero"] == {"N": "0"}

    def test_unscheduled_limits_remove_the_stamps(self):
        """Override, not merge: dropping a schedule must clear the item.

        `vu` is deliberately NOT in the removed set: core plan Task 13 SETs it
        to 0 on every fan-out, scheduled or not, and SET + REMOVE on one
        attribute is a ValidationException (#488).
        """
        plain = {"rpm": {"capacity": 1000, "refill_amount": 1000, "refill_period": 60}}
        expr, names, values = build_bucket_param_update(
            plain, ttl_multiplier=0, stale_limit_names=None, now_ms=0
        )
        removed = {names[a.strip()] for a in expr.split("REMOVE")[1].split(",")}
        assert {"sched", "sched_tz", "rsched"} <= removed
        assert "vu" not in removed
        assert values[":vu_zero"] == {"N": "0"}
        assert ":sched" not in values

    def test_reset_schedule_stamps_rsched(self):
        decl = {
            "rpd": {
                "capacity": 10000,
                # ADR-137 / Task 6: a non-empty reset_schedule flips the YAML
                # shorthand default to 0, and an explicit non-zero rate beside
                # a reset is a parse-time error. `to_dict()` always emits the
                # field, so 0 is what a round trip produces.
                "refill_amount": 0,
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
scheduled = {name: decl["schedule"] for name, decl in limits.items() if decl.get("schedule")}
reset = {
    name: decl["reset_schedule"] for name, decl in limits.items() if decl.get("reset_schedule")
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

if not scheduled and not reset:
    # Override, not merge: a limit re-applied without a schedule must lose
    # the stamp. `#vu` is NOT in this list — core plan Task 13 already SETs
    # it to 0 unconditionally above, and SET + REMOVE on one attribute is a
    # ValidationException (#488). The forced materialising pass (§3.4) is
    # therefore already in place on every fan-out, scheduled or not.
    for alias, attr in (
        ("#sched", BUCKET_FIELD_SCHED),
        ("#sched_tz", BUCKET_FIELD_SCHED_TZ),
        ("#rsched", BUCKET_FIELD_RSCHED),
    ):
        expr_names[alias] = attr
        remove_parts.append(alias)
```

The merged `#vu = :vu_zero` SET stays exactly where it is; do not move it under a
`if scheduled or reset:` guard and do not rename its `:vu_zero` placeholder — the two tests
above assert on it.

Per-limit overrides (`b_{name}_sched`) follow the same shape as core plan Task 13 — emit one only
where a limit's encoding differs from the item-level default. If every scheduled limit shares an
encoding, which is the normal case, the default alone is correct and smallest.

- [ ] **Step 4: Run the tests and watch them pass**

```bash
uv run pytest tests/unit/test_provisioner_bucket_sync.py tests/unit/test_provisioner_builder.py -v
```

- [ ] **Step 5: Lint, type check, commit**

```bash
# Never bare `uv run ruff format .` (Global Constraints) — it reformats 34
# unrelated files, these plan documents included. pre-commit runs ruff
# check and format at the pinned 0.9.2 over exactly these paths.
pre-commit run --files src/zae_limiter/cli.py tests/unit/test_cli.py
uv run mypy
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

        limit = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")
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
            lines.append(f"      {to_cron_display(entry)}  {entry.tz}  → refill to capacity")
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
# Never bare `uv run ruff format .` (Global Constraints) — it reformats 34
# unrelated files, these plan documents included. pre-commit runs ruff
# check and format at the pinned 0.9.2 over exactly these paths.
pre-commit run --files src/zae_limiter/repository.py tests/unit/test_repository.py tests/unit/test_limiter.py tests/unit/test_schedule_encoding.py tests/integration/test_schedule_failure.py
uv run mypy
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

**Files:**
- Modify: `src/zae_limiter/repository.py` (`_deserialize_limits` :5010, `_deserialize_composite_bucket` :4878), `docs/plans/2026-09-13-scheduled-limits-design.md` (§6)
- Test: `tests/unit/test_repository.py`, `tests/unit/test_limiter.py`, `tests/unit/test_schedule_encoding.py`, `tests/integration/test_schedule_failure.py`

**Interfaces:**
- Consumes: `decode` / `decode_reset` raising `ValueError` (core plan Task 5, surface Task 4);
  `RateLimiterUnavailable` (`exceptions.py:158`)
- Produces: no new public names. `Repository.get_limits()`, `Repository.resolve_limits()` and
  the bucket deserialiser raise `RateLimiterUnavailable` instead of `ValueError` on a stored
  schedule that will not decode.

**This task is writing design §6, not implementing it.** §6 exists as four paragraphs of intent
and has never been reconciled with what the two sides actually do. Part of the work here is
amending the design document — §6 is a design doc, not an accepted ADR, so
`.claude/rules/adr-rules.md`'s immutability rule does not apply; ADR-135 (Task 12) then records
the amended version. Say in the commit body that §6 was written by this task.

#### Decision 1: no version marker, and the "newer client" distinction is dropped

Design §4.1 says "A version marker is carried in the encoding (~6 B) so §6 can distinguish
'written by a newer client' from 'corrupt'." **It is not.** Core plan Task 5 shipped the
encoding without one and recorded why: the grammar, its pinned fixture strings, and every
downstream call site (core plan Tasks 8, 12, 13, 14 and surface Task 8) have no marker, and
adding one breaks `test_compact_shape` along with every literal like `"h9-17w1-5s500"` in those
tasks. The compressed version of *this* task then leaned on the marker as if it existed.

**Decision: do not add it. Drop the distinction and amend §4.1.** The costs, both ways:

*Cost of adding it.* Six bytes on every scheduled bucket item forever, against a 1 KB WCU
boundary the design spends all of §4.2 staying under. A fixture rewrite across five landed or
in-flight tasks, each of which pins the compact form byte-for-byte on purpose. And — the part
that decides it — the marker cannot help with anything already written: an unmarked string
would still be ambiguous between "an older client wrote this" and "this is corrupt", so the
distinction only works forward from the day it ships.

*Cost of dropping it.* The log line for an undecodable schedule says what failed and where, but
not *why*, and an operator debugging a mixed-version fleet has to infer it. That is the whole
loss. It is a log message, not behaviour: both readings produce the identical action —
`RateLimiterUnavailable` on the client, skip-the-bucket in the aggregator — so nothing
downstream branches on it.

Two things make this cheap to reverse, and both should be stated in the amended §4.1 so the
option stays open:

- The **tokeniser already discriminates structurally**. `_tokenise` (`schedule.py:494`) matches
  only the known tags `m h D M w s c a p` and raises `malformed compact schedule entry ...:
  cannot parse from offset N` on anything else. A newer client's new tag lands there with a
  precise offset, while a cronsim rejection reads `invalid cron expression ...`. That is a
  heuristic, not a proof — corruption can also fail at an offset — and the amended §4.1 must
  say so rather than overselling it.
- **Adding a marker later is not itself a break**, provided the reader treats its absence as
  v1. The reader has to be tolerant of absence anyway, for every item written before the marker
  existed. So deferring costs nothing structurally.

File a follow-up issue against the v1.x milestone for "versioned schedule encoding", referencing
this decision, so the option is tracked rather than forgotten.

#### Decision 2: `decode` raises `ValueError`; the *boundary* decides what that means

Three positions currently exist in the tree and they are not in conflict once the rule is
stated properly — but two of the three are merged, so this task must fit around them rather
than legislate over them.

| Where | Behaviour | Status |
|-------|-----------|--------|
| `schedule.decode` / `decode_reset` | raises `ValueError` | merged (core plan Task 5) |
| `zae_limiter_aggregator.processor._decode_schedule` (:335) | catches `ValueError`, reports it on `sched_error`, `try_refill_bucket` skips the bucket | merged (core plan Task 14) |
| `Repository._deserialize_limits` (:5010) | lets `ValueError` propagate out of `get_limits()` / `resolve_limits()` | merged (core plan Task 8), flagged in its ledger as "an asymmetry, and §6 is still unwritten" |

**The rule: the parser raises, and each boundary converts.**

1. **`schedule.py` is not modified by this task.** Two reasons, and both are hard constraints
   rather than preferences. It is pure stdlib plus `cronsim` with no `zae_limiter` imports so
   that `models.py` can import from it without a cycle and both Lambdas can vendor it — pulling
   in `exceptions.py` would end that. And `processor._decode_schedule` catches `ValueError`
   specifically; `RateLimiterUnavailable` is an `InfrastructureError`, not a `ValueError`, so
   raising it from `decode` would slip straight through that catch and re-arm the poison-pill
   failure core plan Task 14 fixed — a raise inside `aggregate_bucket_states` (`processor.py:216`)
   aborts the whole stream batch, snapshots included, and the record retries until the stream
   stalls. The compressed version of this task said "Modify `src/zae_limiter/schedule.py`" and
   asserted `pytest.raises(RateLimiterUnavailable)` on `decode(...)` directly; that would have
   broken merged code.
2. **The aggregator boundary is already correct and is left alone.** Skip the bucket, do not
   refill at the base rate (which would silently undo a scale-down), let usage extraction
   continue.
3. **The client boundary converts.** `Repository._deserialize_limits` and
   `_deserialize_composite_bucket` wrap the decode and raise `RateLimiterUnavailable`. That is
   the only change in `src/`.

**`acquire()` then honours `on_unavailable` with no new code, and that is the point.** Its
`except Exception` (`limiter.py:717`) yields a degraded lease under `ALLOW` and re-wraps under
`BLOCK`; the re-raise tuple is `(RateLimitExceeded, ValidationError, ResourceDisabled, Warning)`
and `RateLimiterUnavailable` is deliberately not in it. So the operator's existing knob applies
as §6 asks, without a fourth behaviour being invented. Treating an unreadable schedule as "no
schedule" instead would silently run at the **base** limit — a parse error doubling a
customer's limit when the schedule said `0.5x` — and, with `vu` left expired, would pin the
bucket to the slow path permanently.

**Known limitation, to be stated in §6 rather than fixed here.** `RateLimiter.acquire()`
resolves the mode *before* the try block (`limiter.py:674`), and `resolve_on_unavailable()`
(`repository.py:5116-5144`) swallows every exception and falls back to its cached value or
`"block"`. So a corrupt schedule on the **system config item specifically** makes the mode
itself unresolvable, and an operator who configured `allow` gets `block` unless the value was
already cached from an earlier successful read. Entity- and resource-level corruption is
unaffected. Fixing it means teaching `resolve_on_unavailable` to distinguish "cannot reach
DynamoDB" from "read a config item I cannot parse", which is a wider change than §6 needs.

- [ ] **Step 1: Write the failing test**

Three modules. First, a regression guard in `tests/unit/test_schedule_encoding.py` that the
parser's exception type has **not** changed — this is the test that would have caught the
compressed version's plan:

```python
class TestDecodeRaisesValueErrorForTheAggregatorsSake:
    """`processor._decode_schedule` catches `ValueError` specifically.

    Raising anything else from the parser slips through that catch and aborts
    the whole stream batch (processor.py:216 is outside any try), which is the
    poison pill core plan Task 14 fixed. The client-side conversion to
    RateLimiterUnavailable belongs at the Repository boundary, not here.
    """

    @pytest.mark.parametrize(
        "compact", ["this is not a schedule", "Xh9-17s500", "h9-17s500s600", "v9:h9-17"]
    )
    def test_decode_raises_value_error(self, compact):
        with pytest.raises(ValueError):
            decode(compact, "UTC")

    def test_decode_does_not_raise_rate_limiter_unavailable(self):
        """Explicit, because `RateLimiterUnavailable` is not a ValueError and
        the failure would be silent until a stream stalled in production."""
        with pytest.raises(ValueError) as excinfo:
            decode("this is not a schedule", "UTC")
        assert not isinstance(excinfo.value, RateLimiterUnavailable)

    def test_an_unknown_tag_is_distinguishable_from_a_bad_cron(self):
        """The heuristic that replaces the version marker §4.1 promised. A
        newer client's new tag fails in the tokeniser with an offset; a bad
        field spec fails in cronsim. Not a proof — corruption can also fail at
        an offset — but it is what the log has to work with."""
        with pytest.raises(ValueError, match="cannot parse from offset"):
            decode("h9-17q42", "UTC")
        with pytest.raises(ValueError, match="invalid cron expression"):
            decode("h99", "UTC")
```

Second, the conversion, in `tests/unit/test_repository.py`:

```python
class TestUnreadableStoredSchedule:
    """A schedule the client cannot read makes the limiter unavailable (§6).

    Not "no schedule": that runs at the *base* limit, so a parse error would
    double a customer's limit when the schedule said 0.5x, and with `vu` left
    expired it would pin the bucket to the slow path forever.
    """

    async def test_get_limits_raises_unavailable(self, repo):
        await repo.set_limits(
            "corrupt-1",
            [Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)],
            resource="gpt-4",
        )
        await _corrupt_config_sched(repo, "corrupt-1", "gpt-4", "rpm", "not-a-schedule")
        await repo.invalidate_config_cache()

        with pytest.raises(RateLimiterUnavailable, match="schedule"):
            await repo.get_limits("corrupt-1", resource="gpt-4")

    async def test_the_message_names_the_attribute_and_the_value(self, repo):
        """An operator debugging a mixed-version fleet has only this line, now
        that the version marker is not being added (Decision 1)."""
        await repo.set_limits(
            "corrupt-2",
            [Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)],
            resource="gpt-4",
        )
        await _corrupt_config_sched(repo, "corrupt-2", "gpt-4", "rpm", "h9-17q42")
        await repo.invalidate_config_cache()

        with pytest.raises(RateLimiterUnavailable) as excinfo:
            await repo.get_limits("corrupt-2", resource="gpt-4")
        message = str(excinfo.value)
        assert "l_rpm_sched" in message
        assert "h9-17q42" in message
        assert isinstance(excinfo.value.cause, ValueError)

    async def test_resolve_limits_raises_too(self, repo):
        """`resolve_limits` is what `acquire()`'s slow path calls; if only
        `get_limits` converted, the path that matters would still surface a
        bare ValueError."""
        await repo.set_limits(
            "corrupt-3",
            [Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)],
            resource="gpt-4",
        )
        await _corrupt_config_sched(repo, "corrupt-3", "gpt-4", "rpm", "not-a-schedule")
        await repo.invalidate_config_cache()

        with pytest.raises(RateLimiterUnavailable):
            await repo.resolve_limits("corrupt-3", "gpt-4")

    async def test_an_unreadable_reset_schedule_raises_the_same_way(self, repo):
        await repo.set_limits(
            "corrupt-4",
            [Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")],
            resource="gpt-4",
        )
        await _corrupt_config_sched(repo, "corrupt-4", "gpt-4", "rpd", "zzz", field="rsched")
        await repo.invalidate_config_cache()

        with pytest.raises(RateLimiterUnavailable, match="schedule"):
            await repo.get_limits("corrupt-4", resource="gpt-4")

    async def test_an_unreadable_bucket_schedule_raises(self, repo):
        """`_deserialize_composite_bucket` is the other decode site, and it is
        the one behind the speculative fast rejection's ALL_OLD states."""
        await repo.create_entity("corrupt-5", parent_id=None, name="corrupt-5")
        await repo.set_limits(
            "corrupt-5",
            [Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)],
            resource="gpt-4",
        )
        await repo.speculative_consume("corrupt-5", "gpt-4", {"rpm": 1})
        await _corrupt_bucket_sched(repo, "corrupt-5", "gpt-4", "not-a-schedule")

        with pytest.raises(RateLimiterUnavailable):
            await repo.get_buckets("corrupt-5", resource="gpt-4")

    async def test_an_unscheduled_limit_still_reads_normally(self, repo):
        """The guard must not turn every ValueError in the read path into an
        infrastructure error — only the schedule decode is wrapped."""
        await repo.set_limits("plain-1", [Limit.per_minute("rpm", 1000)], resource="gpt-4")
        (stored,) = await repo.get_limits("plain-1", resource="gpt-4")
        assert stored.capacity == 1000
```

with two helpers that write a bad attribute straight to the item, since no public API can
produce one:

```python
async def _corrupt_config_sched(repo, entity_id, resource, limit_name, value, field="sched"):
    """Overwrite one stored schedule attribute with an undecodable string."""
    attr = schema.limit_attr(limit_name, getattr(schema, f"LIMIT_FIELD_{field.upper()}"))
    client = await repo._get_client()
    await client.update_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": schema.pk_entity(repo._namespace_id, entity_id)},
            "SK": {"S": schema.sk_config(resource)},
        },
        UpdateExpression="SET #a = :v",
        ExpressionAttributeNames={"#a": attr},
        ExpressionAttributeValues={":v": {"S": value}},
    )


async def _corrupt_bucket_sched(repo, entity_id, resource, value, shard=0):
    client = await repo._get_client()
    await client.update_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, resource, shard)},
            "SK": {"S": schema.sk_state()},
        },
        UpdateExpression="SET #a = :v",
        ExpressionAttributeNames={"#a": schema.BUCKET_FIELD_SCHED},
        ExpressionAttributeValues={":v": {"S": value}},
    )
```

Third, the `on_unavailable` behaviour, in `tests/unit/test_limiter.py`:

```python
class TestUnreadableScheduleHonoursOnUnavailable:
    """The operator already chose what happens when the limiter cannot decide.

    No new code makes this work — `acquire()`'s `except Exception` handler
    (limiter.py:717) does it, because RateLimiterUnavailable is not in the
    re-raise tuple. These tests pin that it stays that way.
    """

    async def _corrupt(self, limiter, entity_id):
        repo = limiter._repository
        await repo.set_limits(
            entity_id,
            [Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)],
            resource="gpt-4",
        )
        await _corrupt_config_sched(repo, entity_id, "gpt-4", "rpm", "not-a-schedule")
        await repo.invalidate_config_cache()

    async def test_block_raises_rate_limiter_unavailable(self, limiter):
        await self._corrupt(limiter, "ou-1")
        slow = RateLimiter(repository=limiter._repository, speculative_writes=False)
        with pytest.raises(RateLimiterUnavailable):
            async with slow.acquire(
                "ou-1", "gpt-4", consume={"rpm": 1}, on_unavailable=OnUnavailable.BLOCK
            ):
                pass

    async def test_allow_degrades(self, limiter):
        await self._corrupt(limiter, "ou-2")
        slow = RateLimiter(repository=limiter._repository, speculative_writes=False)
        async with slow.acquire(
            "ou-2", "gpt-4", consume={"rpm": 1}, on_unavailable=OnUnavailable.ALLOW
        ) as lease:
            assert lease.degraded is True

    async def test_a_degraded_lease_is_not_inferred_from_empty_entries(self, limiter):
        """Invariant 2 in CLAUDE.md: never infer degradation from entries == [].
        Pinned here because this is the second producer of such a lease."""
        await self._corrupt(limiter, "ou-3")
        slow = RateLimiter(repository=limiter._repository, speculative_writes=False)
        async with slow.acquire(
            "ou-3", "gpt-4", consume={"rpm": 1}, on_unavailable=OnUnavailable.ALLOW
        ) as lease:
            lease.adjust({"rpm": 5})  # must not raise the declared-scope error

    async def test_a_readable_schedule_is_unaffected(self, limiter):
        """Discriminates the two above against "always degrade when scheduled"."""
        repo = limiter._repository
        await repo.set_limits(
            "ou-4",
            [Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)],
            resource="gpt-4",
        )
        slow = RateLimiter(repository=repo, speculative_writes=False)
        async with slow.acquire("ou-4", "gpt-4", consume={"rpm": 1}) as lease:
            assert lease.degraded is False
```

- [ ] **Step 2: Run the tests and watch them fail**

```bash
uv run pytest tests/unit/test_schedule_encoding.py -k DecodeRaisesValueError -v
uv run pytest tests/unit/test_repository.py -k UnreadableStoredSchedule -v
uv run pytest tests/unit/test_limiter.py -k UnreadableScheduleHonoursOnUnavailable -v
```

Expected: the `test_schedule_encoding.py` class **passes** already — it is a regression guard,
and a guard that fails on day one is describing a change nobody asked for. The
`test_repository.py` class fails with
`ValueError: malformed compact schedule entry 'not-a-schedule'` where
`RateLimiterUnavailable` was expected. `test_block_raises_rate_limiter_unavailable` passes
(`except Exception` already wraps it), `test_allow_degrades` passes, and
`test_a_degraded_lease_is_not_inferred_from_empty_entries` passes — which is the finding, not a
gap: **the `on_unavailable` half of §6 is already satisfied by existing machinery** and only
the exception type at the Repository boundary is missing. Say so in the commit body rather than
inventing work to make a red phase.

- [ ] **Step 3: Implement**

One helper in `repository.py`, used at both decode sites:

```python
def _decode_stored_schedule(
    self, attr_name: str, compact: str, tz: str, *, reset: bool = False
) -> tuple[schedule.ScheduleEntry, ...]:
    """Decode a stored schedule, or declare the limiter unavailable (§6).

    A limiter that cannot determine the limit is definitionally
    unavailable, and ``on_unavailable`` is the knob that already exists for
    that — in ``allow`` mode it degrades exactly the way the operator asked.
    The alternative, treating an unreadable schedule as *no* schedule, runs
    at the **base** limit: a parse error would then double a customer's
    limit when the schedule said ``0.5x``, and with ``vu`` left expired the
    bucket would be pinned to the slow path permanently.

    The parser keeps raising ``ValueError`` and is not touched: it is pure
    stdlib plus cronsim so that ``models`` can import it without a cycle and
    both Lambdas can vendor it, and the aggregator's ``_decode_schedule``
    catches ``ValueError`` specifically — raising an ``InfrastructureError``
    there would slip through that catch and poison a whole stream batch.

    The message carries the attribute name and the stored value because
    there is no version marker to say whether a newer client wrote this;
    see Task 10's Decision 1.
    """
    decoder = schedule.decode_reset if reset else schedule.decode
    try:
        return decoder(compact, tz)
    except ValueError as exc:
        raise RateLimiterUnavailable(
            f"stored schedule in {attr_name} could not be decoded: {compact!r} ({tz}): {exc}",
            cause=exc,
            stack_name=self.stack_name,
        ) from exc
```

Route `_deserialize_limits` (:5010) and `_deserialize_composite_bucket` (:4878) through it.
Nothing else changes: `acquire()` already does the rest.

- [ ] **Step 4: Run the tests and watch them pass**

```bash
uv run pytest tests/unit/test_repository.py -k UnreadableStoredSchedule -v
uv run pytest tests/unit/test_limiter.py -k UnreadableScheduleHonoursOnUnavailable -v
uv run pytest tests/unit/ -q
uv run pytest tests/unit/ -m gevent -n 0 -q
```

- [ ] **Step 5: Add the integration test**

A unit test with moto cannot show the fast path's behaviour against a real conditional write,
and the interesting question — what an in-flight `acquire()` does when the item it is about to
read carries an unreadable schedule — needs LocalStack. New file
`tests/integration/test_schedule_failure.py`, following `.claude/rules/testing.md`'s
`make_test_repo(stack, namespace)` pattern:

```python
@pytest.mark.integration
class TestUnreadableScheduleIntegration:
    async def test_the_fast_path_is_unaffected_until_vu_expires(self, test_repo):
        """`vu` in the future keeps the request on the speculative path, which
        reads no config and decodes nothing — so a corrupt *config* schedule is
        invisible until the bucket next materialises. This is the load-bearing
        claim of §2.1 seen from the failure side."""

    async def test_an_expired_vu_surfaces_the_error(self, test_repo):
        """Once `vu` expires the slow path resolves config, hits the corrupt
        attribute, and `on_unavailable` applies."""

    async def test_the_aggregator_skips_rather_than_stalling(self, test_repo):
        """The other half of the reconciliation: a corrupt *bucket* schedule
        must leave usage snapshots flowing. Assert the snapshot for the same
        entity still appears while the bucket is never refilled."""
```

Fill each body out following `tests/integration/test_provisioner.py`'s style; they are the same
shape as the repository tests above with a real table underneath.

- [ ] **Step 6: Amend design §6 and §4.1**

In `docs/plans/2026-09-13-scheduled-limits-design.md`:

- §6 gains the boundary table from Decision 2, the statement that the parser raises
  `ValueError` and each boundary converts, and the `resolve_on_unavailable` known limitation.
- ~~§4.1's last line changes…~~ **Already done — do not redo this.** §4.1 was amended ahead of
  this task, because as written it asserted in the present tense that a marker *is* carried,
  which was simply false about merged code and would mislead anyone reading it in the interim.
  It now states that no marker is carried, why, the tokeniser heuristic that replaces it, and
  that adding one later is non-breaking for a reader tolerant of absence. Tracked as #515.
- §9 gains the `resolve_on_unavailable` limitation.

- [ ] **Step 7: Lint, type check, commit**

`repository.py` is a sync-codegen source.

```bash
hatch run generate-sync
uv run ruff check --fix .
uv run ruff format src/zae_limiter tests/unit tests/integration
uv run mypy
git add -A
git commit -m "$(cat <<'EOF'
✨ feat(limiter): fail safe on an unreadable stored schedule

A stored schedule that will not decode now raises RateLimiterUnavailable
out of the config and bucket read paths, so acquire()'s existing handler
applies the operator's on_unavailable setting to it — degrading in allow
mode and raising in block mode. Treating it as "no schedule" would run
at the base limit, doubling a customer's limit when the schedule said
0.5x, and would pin the bucket to the slow path with `vu` expired.

The parser is deliberately unchanged: schedule.py stays free of any
zae_limiter import so models.py can use it without a cycle and both
Lambdas can vendor it, and the aggregator catches ValueError
specifically — an InfrastructureError raised there would slip through
and poison a whole stream batch. The conversion belongs at the client
boundary, and the aggregator's skip-the-bucket handling is already
correct and untouched.

Design §6 was unwritten; this commit writes it, and amends §4.1 to say
that no version marker is carried. The marker would have cost six bytes
per item forever plus a fixture rewrite across five tasks, to distinguish
two cases that produce identical behaviour — and could not have
classified anything already written. The tokeniser's offset message is
the heuristic that replaces it; adding a real marker later stays
non-breaking.

Refs #222
EOF
)"
```

---

### Task 11: E2E

**Files:**
- Modify: `tests/e2e/test_localstack.py`
- Test: the same file — this task is tests only

**Interfaces:**
- Consumes: everything. This is the task that proves the schedule survives a real
  CloudFormation stack, a real DynamoDB table, a real stream and a real Lambda.
- Produces: nothing importable.

**Two groups, and the split is deliberate.** Design §8 says boundary crossings "use a `*/2`
schedule and real waiting, marked `slow`, because the Lambda's clock cannot be injected the way
`Repository._now_ms()` can". The second half of that sentence is true; the first half follows
from it only for the cases that actually involve the Lambda.

- **Group A — clock-injected, no `slow` marker.** Everything enforced by the client:
  materialisation, trimming, the fast-path `vu` gate, cascade, sharding, leases, the reset, the
  provisioner, and the failure modes. `Repository._now_ms()` (#430) drives the `rf` stamp, the
  `vu` comparison in the speculative condition, the `ttl` stamp and its guard, and every refill
  computation, so a jumped clock is *self-consistent* across all of them. Eleven crossings at
  two real minutes each is twenty-two minutes of CI for no additional coverage.
- **Group B — real `*/2` waiting, marked `slow` and `monitoring`.** Only the cases that require
  the aggregator to observe the boundary itself, because it reads `time.time()` inside a Lambda
  container this test cannot reach.

Use dates within a day or two of real time (`2026-09-15` / `2026-09-16`). DynamoDB's own TTL
reaper runs on real time and does not care about the injected clock, so a bucket stamped with a
`ttl` computed from an instant years in the past could be swept mid-test.

**Two traps that will cost a day each if rediscovered.**

- **The config cache does not follow the clock seam.** `config_cache.py:99` and `:103` call
  `time.time()`. Every clock jump below is followed by `await repo.invalidate_config_cache()`.
  Without it the post-jump `acquire()` resolves the **pre**-jump `Limit`, the schedule appears
  not to apply, and the obvious "fix" is to weaken an assertion.
- **`vu` is what routes a request to the slow path, and the fan-out sets it to 0.** A test that
  calls `set_limits` and then asserts on the *first* subsequent acquire is observing the
  `vu = 0` forced pass (core plan Task 13), not the boundary. Where the boundary is the subject,
  warm the bucket first and let `vu` settle to a real boundary.

**Sync counterparts.** `tests/e2e/test_localstack.py` has no generated twin; §8's "plus the
generated sync counterparts throughout" is discharged by the generated unit twins of Tasks 3
and 5 (`test_sync_limiter.py`, `test_sync_repository.py`). One sync smoke test goes here anyway,
because `SyncRepository` reaches DynamoDB through boto3 rather than aioboto3 and nothing else in
this file crosses a boundary on that client.

- [ ] **Step 1: Write the Group A tests**

Append to `tests/e2e/test_localstack.py`. `BUSINESS` halves the limit from 09:00 to 17:59 local
on weekdays; 2026-09-15 is a Tuesday.

```python
NY = ZoneInfo("America/New_York")


def _ny(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=NY).timestamp() * 1000)


BUSINESS = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),)
NIGHT_DOUBLE = (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", capacity=2000),)
DAILY_RESET = (ScheduleEntry.reset(cron="0 0 * * *", tz="America/New_York"),)

BEFORE = _ny("2026-09-15 08:00")  # outside every window above
INSIDE = _ny("2026-09-15 10:00")  # inside BUSINESS
NIGHT = _ny("2026-09-16 03:00")  # inside NIGHT_DOUBLE
LATE = _ny("2026-09-15 23:00")  # before the daily reset
AFTER_RESET = _ny("2026-09-16 00:30")


class TestE2EScheduleBoundaries:
    """Schedule enforcement against a real table, with an injected clock."""

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def sched_repo(self, shared_minimal_stack, unique_name_class):
        """Namespace-scoped Repository on the shared *minimal* stack.

        Minimal on purpose: the aggregator writes to the same bucket items and
        would make every token assertion below racy. The aggregator's own
        behaviour at a boundary is Group B.
        """
        ns = f"sched-{unique_name_class}"
        repo = await Repository.open(
            stack=shared_minimal_stack.name,
            region=shared_minimal_stack.region,
            endpoint_url=shared_minimal_stack.endpoint_url,
            config_cache_ttl=0,
        )
        await repo.register_namespace(ns)
        scoped = await repo.namespace(ns)
        yield scoped
        await repo.close()

    @staticmethod
    async def _at(repo, instant: int) -> None:
        """Move the injected clock and drop the config cache.

        The seam (#430) does not cover config_cache.py, which still reads
        time.time() — so without the second line every call after a jump
        resolves the limits cached before it.
        """
        repo._now_ms = lambda: instant
        await repo.invalidate_config_cache()

    @pytest.mark.asyncio(loop_scope="class")
    async def test_a_shrink_boundary_trims_a_full_bucket(self, sched_repo):
        """§8: the surplus must be unspendable, not a free burst.

        Fill to 1000 outside the window, cross into a 0.5x window, and the
        bucket must not admit 1000 — it holds at most 500. This is what
        replaces #469, seen end to end.
        """
        limiter = RateLimiter(repository=sched_repo)
        await self._at(sched_repo, BEFORE)
        await sched_repo.set_limits(
            "shrink-1",
            [Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)],
            resource="gpt-4",
        )
        async with limiter.acquire("shrink-1", "gpt-4", consume={"rpm": 1}):
            pass

        await self._at(sched_repo, INSIDE)
        async with limiter.acquire("shrink-1", "gpt-4", consume={"rpm": 500}):
            pass
        with pytest.raises(RateLimitExceeded) as excinfo:
            async with limiter.acquire("shrink-1", "gpt-4", consume={"rpm": 1}):
                pass

        # #475: the status quotes the shard's share of the *scheduled* capacity.
        (violation,) = excinfo.value.violations
        assert violation.limit.capacity == 500

    @pytest.mark.asyncio(loop_scope="class")
    async def test_a_grow_boundary_makes_capacity_available(self, sched_repo):
        """§8. An absolute entry raising the ceiling to 2000 must be spendable
        promptly — one materialising pass, not a refill window's wait."""
        limiter = RateLimiter(repository=sched_repo)
        await self._at(sched_repo, BEFORE)
        await sched_repo.set_limits(
            "grow-1",
            [Limit.per_minute("rpm", 1000).with_schedule(NIGHT_DOUBLE)],
            resource="gpt-4",
        )
        async with limiter.acquire("grow-1", "gpt-4", consume={"rpm": 1000}):
            pass

        await self._at(sched_repo, NIGHT)
        async with limiter.acquire("grow-1", "gpt-4", consume={"rpm": 2000}) as lease:
            assert lease.consumed["rpm"] == 2000

    @pytest.mark.asyncio(loop_scope="class")
    async def test_a_future_vu_keeps_the_fast_path_and_reads_no_config(self, sched_repo):
        """§2.1's load-bearing claim, from the e2e side: inside a window and
        with `vu` still ahead, a request is one conditional UpdateItem.

        Asserted through the bucket item rather than a capacity counter (which
        is moto-only): `rf` must not move, because only a materialising pass
        stamps it.
        """
        limiter = RateLimiter(repository=sched_repo)
        await self._at(sched_repo, INSIDE)
        await sched_repo.set_limits(
            "fast-1",
            [Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)],
            resource="gpt-4",
        )
        async with limiter.acquire("fast-1", "gpt-4", consume={"rpm": 1}):
            pass
        before = await _raw_bucket_item(sched_repo, "fast-1", "gpt-4", shard=0)

        async with limiter.acquire("fast-1", "gpt-4", consume={"rpm": 1}):
            pass
        after = await _raw_bucket_item(sched_repo, "fast-1", "gpt-4", shard=0)

        assert after[schema.BUCKET_FIELD_RF] == before[schema.BUCKET_FIELD_RF]
        assert int(after[schema.BUCKET_FIELD_VU]["N"]) == _ny("2026-09-15 18:00")

    @pytest.mark.asyncio(loop_scope="class")
    async def test_a_boundary_crossed_while_a_lease_is_open(self, sched_repo):
        """§8, with #455: `adjust` still lands against the declared scope, and
        a limit the caller did not declare is still not adjustable — crossing a
        boundary mid-lease must not widen or narrow that."""
        limiter = RateLimiter(repository=sched_repo)
        await self._at(sched_repo, BEFORE)
        await sched_repo.set_limits(
            "lease-1",
            [
                Limit.per_minute("rpm", 1000).with_schedule(BUSINESS),
                Limit.per_minute("tpm", 100_000),
            ],
            resource="gpt-4",
        )

        async with limiter.acquire("lease-1", "gpt-4", consume={"rpm": 10}) as lease:
            await self._at(sched_repo, INSIDE)
            lease.adjust({"rpm": 5})
            with pytest.warns(FutureWarning):
                lease.adjust({"tpm": 50})

        buckets = {
            b.limit_name: b for b in await sched_repo.get_buckets("lease-1", resource="gpt-4")
        }
        assert buckets["rpm"].tokens_milli == (1000 - 15) * 1000
        assert buckets["tpm"].tokens_milli == 100_000 * 1000

    @pytest.mark.asyncio(loop_scope="class")
    async def test_concurrent_traffic_at_the_boundary_never_over_admits(self, sched_repo):
        """§8: exactly one materialisation wins, the losers take the retry path
        (`tk >= consumed`, which sees the winner's clamp), and the total
        admitted never exceeds the new ceiling.

        Twenty concurrent requests for 50 each against a 500 ceiling: at most
        ten may succeed.
        """
        limiter = RateLimiter(repository=sched_repo)
        await self._at(sched_repo, BEFORE)
        await sched_repo.set_limits(
            "race-1",
            [Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)],
            resource="gpt-4",
        )
        async with limiter.acquire("race-1", "gpt-4", consume={"rpm": 1}):
            pass

        await self._at(sched_repo, INSIDE)

        async def one() -> bool:
            try:
                async with limiter.acquire("race-1", "gpt-4", consume={"rpm": 50}):
                    return True
            except RateLimitExceeded:
                return False

        results = await asyncio.gather(*[one() for _ in range(20)])
        assert sum(results) <= 10

        bucket = next(
            b
            for b in await sched_repo.get_buckets("race-1", resource="gpt-4")
            if b.limit_name == "rpm"
        )
        assert bucket.tokens_milli >= 0

    @pytest.mark.asyncio(loop_scope="class")
    async def test_cascade_with_different_schedules_on_child_and_parent(self, sched_repo):
        """§8: only the parent's boundary fires. The child keeps its full
        1000 and is admitted by its own bucket; the parent's 0.5x window is
        what rejects, and the status names the *parent*."""
        limiter = RateLimiter(repository=sched_repo)
        await self._at(sched_repo, BEFORE)
        await limiter.create_entity("casc-org")
        await limiter.create_entity("casc-key", parent_id="casc-org", cascade=True)
        await sched_repo.set_limits(
            "casc-org",
            [Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)],
            resource="gpt-4",
        )
        await sched_repo.set_limits("casc-key", [Limit.per_minute("rpm", 1000)], resource="gpt-4")
        async with limiter.acquire("casc-key", "gpt-4", consume={"rpm": 1}):
            pass

        await self._at(sched_repo, INSIDE)
        with pytest.raises(RateLimitExceeded) as excinfo:
            async with limiter.acquire("casc-key", "gpt-4", consume={"rpm": 900}):
                pass
        assert {v.entity_id for v in excinfo.value.violations} == {"casc-org"}

    @pytest.mark.asyncio(loop_scope="class")
    async def test_every_shard_converges_on_its_share(self, sched_repo):
        """§8. Shares must sum to the scheduled ceiling, not to a multiple of
        it — scale first, then divide (Global Constraints).

        `speculative_consume` takes an explicit `shard_id` precisely so a test
        can target a shard; assuming an `acquire()` lands on one is flaky by
        construction (ADR-134).
        """
        limiter = RateLimiter(repository=sched_repo)
        await self._at(sched_repo, BEFORE)
        await sched_repo.set_limits(
            "shard-1",
            [Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)],
            resource="gpt-4",
        )
        async with limiter.acquire("shard-1", "gpt-4", consume={"rpm": 1}):
            pass
        await sched_repo.bump_shard_count("shard-1", "gpt-4", 1)
        for shard in (0, 1):
            await sched_repo.speculative_consume("shard-1", "gpt-4", {"rpm": 1}, shard_id=shard)

        await self._at(sched_repo, INSIDE)
        for shard in (0, 1):
            await sched_repo.speculative_consume("shard-1", "gpt-4", {"rpm": 1}, shard_id=shard)

        buckets = [
            b
            for b in await sched_repo.get_buckets("shard-1", resource="gpt-4")
            if b.limit_name == "rpm"
        ]
        assert len(buckets) == 2
        for b in buckets:
            assert b.effective_capacity_milli(INSIDE) == 250_000  # (1_000_000 * 0.5) // 2
            assert b.tokens_milli <= 250_000
        assert sum(b.tokens_milli for b in buckets) <= 500_000

    @pytest.mark.asyncio(loop_scope="class")
    async def test_a_daily_quota_resets_in_one_lump(self, sched_repo):
        """§8: burn it, cross the edge, get it back at once, and `tc` keeps
        climbing across the boundary — the property #471's reset_bucket()
        destroyed and `.claude/rules/design-validation.md` exists to protect."""
        limiter = RateLimiter(repository=sched_repo)
        await self._at(sched_repo, LATE)
        await sched_repo.set_limits(
            "quota-1",
            [Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")],
            resource="gpt-4",
        )
        async with limiter.acquire("quota-1", "gpt-4", consume={"rpd": 10_000}):
            pass
        with pytest.raises(RateLimitExceeded):
            async with limiter.acquire("quota-1", "gpt-4", consume={"rpd": 1}):
                pass

        await self._at(sched_repo, AFTER_RESET)
        async with limiter.acquire("quota-1", "gpt-4", consume={"rpd": 9_000}):
            pass

        bucket = next(
            b
            for b in await sched_repo.get_buckets("quota-1", resource="gpt-4")
            if b.limit_name == "rpd"
        )
        assert bucket.tokens_milli == 1_000_000
        assert bucket.total_consumed_milli == 19_000_000

    @pytest.mark.asyncio(loop_scope="class")
    async def test_an_idle_bucket_resets_on_wake_not_at_the_edge(self, sched_repo):
        """§8 and §9: nothing observes a bucket no one is using, so the reset
        lands on the first request after the edge. Asserted from both sides —
        the item is untouched at 00:30, and restored after the 09:00 request."""
        limiter = RateLimiter(repository=sched_repo)
        await self._at(sched_repo, LATE)
        await sched_repo.set_limits(
            "idle-1",
            [Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")],
            resource="gpt-4",
        )
        async with limiter.acquire("idle-1", "gpt-4", consume={"rpd": 10_000}):
            pass

        await self._at(sched_repo, AFTER_RESET)
        idle = next(
            b
            for b in await sched_repo.get_buckets("idle-1", resource="gpt-4")
            if b.limit_name == "rpd"
        )
        assert idle.tokens_milli == 0  # the edge passed; nothing applied it

        await self._at(sched_repo, _ny("2026-09-16 09:00"))
        async with limiter.acquire("idle-1", "gpt-4", consume={"rpd": 1}):
            pass
        woken = next(
            b
            for b in await sched_repo.get_buckets("idle-1", resource="gpt-4")
            if b.limit_name == "rpd"
        )
        assert woken.tokens_milli == 9_999_000

    @pytest.mark.asyncio(loop_scope="class")
    async def test_check_availability_agrees_with_acquire_at_one_instant(self, sched_repo):
        """§7's reason for wiring the query surface: the display and the
        rejection must not describe the same bucket differently.

        Checked against #530: the `Limit.quota` shape is correct and ~3600 is
        the right expectation, but the test needs **both** `check_availability`
        (site 4) and `try_consume` (sites 1-2) to pass `next_reset_ms`. Note
        that the agreement assertion alone does not discriminate — before
        Task 5 wires them, both sides report `0.0` and
        `approx(0.0, rel=0.01)` passes. The `approx(3600)` line is the one
        doing the work.
        """
        limiter = RateLimiter(repository=sched_repo)
        await self._at(sched_repo, LATE)
        await sched_repo.set_limits(
            "agree-1",
            [Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")],
            resource="gpt-4",
        )
        async with limiter.acquire("agree-1", "gpt-4", consume={"rpd": 10_000}):
            pass

        check = await limiter.check_availability("agree-1", "gpt-4", needed={"rpd": 5_000})
        with pytest.raises(RateLimitExceeded) as excinfo:
            async with limiter.acquire("agree-1", "gpt-4", consume={"rpd": 5_000}):
                pass

        displayed = check.status("rpd").retry_after_seconds
        rejected = excinfo.value.retry_after_seconds
        assert displayed == pytest.approx(rejected, rel=0.01)
        assert displayed == pytest.approx(3600, abs=5)  # at midnight, not "now" (#530)

    @pytest.mark.asyncio(loop_scope="class")
    async def test_a_corrupt_stored_schedule_honours_on_unavailable(self, sched_repo):
        """§8 and §6, both modes against a real table."""
        limiter = RateLimiter(repository=sched_repo)
        await self._at(sched_repo, INSIDE)
        await sched_repo.set_limits(
            "corrupt-e2e",
            [Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)],
            resource="gpt-4",
        )
        await _corrupt_config_sched(sched_repo, "corrupt-e2e", "gpt-4", "rpm", "not-a-schedule")
        await sched_repo.invalidate_config_cache()

        slow = RateLimiter(repository=sched_repo, speculative_writes=False)
        with pytest.raises(RateLimiterUnavailable):
            async with slow.acquire(
                "corrupt-e2e", "gpt-4", consume={"rpm": 1}, on_unavailable=OnUnavailable.BLOCK
            ):
                pass

        async with slow.acquire(
            "corrupt-e2e", "gpt-4", consume={"rpm": 1}, on_unavailable=OnUnavailable.ALLOW
        ) as lease:
            assert lease.degraded is True

    @pytest.mark.asyncio(loop_scope="class")
    async def test_the_sync_client_enforces_the_same_boundary(self, sched_repo):
        """§8's "plus the generated sync counterparts". SyncRepository reaches
        DynamoDB through boto3, not aioboto3, and nothing else in this file
        crosses a boundary on that client."""
        sync_repo = SyncRepository.open(
            stack=sched_repo.stack_name,
            region=sched_repo.region,
            endpoint_url=sched_repo.endpoint_url,
            config_cache_ttl=0,
        )
        try:
            sync_repo = sync_repo.namespace(sched_repo.namespace)
            sync_repo._now_ms = lambda: BEFORE
            sync_repo.set_limits(
                "sync-1",
                [Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)],
                resource="gpt-4",
            )
            limiter = SyncRateLimiter(repository=sync_repo)
            with limiter.acquire("sync-1", "gpt-4", consume={"rpm": 1}):
                pass

            sync_repo._now_ms = lambda: INSIDE
            sync_repo.invalidate_config_cache()
            with limiter.acquire("sync-1", "gpt-4", consume={"rpm": 499}):
                pass
            with pytest.raises(RateLimitExceeded):
                with limiter.acquire("sync-1", "gpt-4", consume={"rpm": 100}):
                    pass
        finally:
            sync_repo.close()


class TestE2EScheduleThroughTheProvisioner:
    """§8: a schedule applied through the manifest must reach live buckets.

    Modelled on TestE2EProvisionerReachesLiveBuckets (:1228): `_handle_cli` is
    invoked in-process, because the provisioner is sync boto3 and needs no
    deployed Lambda to exercise this path.
    """

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def prov_repo(self, shared_minimal_stack, unique_name_class):
        ns = f"schedprov-{unique_name_class}"
        repo = await Repository.open(
            stack=shared_minimal_stack.name,
            region=shared_minimal_stack.region,
            endpoint_url=shared_minimal_stack.endpoint_url,
            config_cache_ttl=0,
        )
        await repo.register_namespace(ns)
        scoped = await repo.namespace(ns)
        yield scoped
        await repo.close()

    @pytest.mark.asyncio(loop_scope="class")
    async def test_an_applied_schedule_reaches_an_existing_bucket(self, prov_repo):
        from zae_limiter_provisioner.handler import _handle_cli

        limiter = RateLimiter(repository=prov_repo)
        prov_repo._now_ms = lambda: _ny("2026-09-15 08:00")
        await prov_repo.set_limits("prov-1", [Limit.per_minute("rpm", 1000)], resource="gpt-4")
        async with limiter.acquire("prov-1", "gpt-4", consume={"rpm": 1}):
            pass

        result = _handle_cli(
            {
                "action": "apply",
                "table_name": prov_repo.table_name,
                "namespace_id": prov_repo._namespace_id,
                "manifest": {
                    "namespace": "default",
                    "entities": {
                        "prov-1": {
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
                                                }
                                            ],
                                        }
                                    }
                                }
                            }
                        }
                    },
                },
            },
            None,
        )
        assert result["status"] == "applied"
        assert result["errors"] == []

        item = await _raw_bucket_item(prov_repo, "prov-1", "gpt-4", shard=0)
        assert item[schema.BUCKET_FIELD_SCHED]["S"] == "h9-17w1-5s500"
        assert item[schema.BUCKET_FIELD_SCHED_TZ]["S"] == "America/New_York"
        assert item[schema.BUCKET_FIELD_VU]["N"] == "0"

        # And it is enforced, not merely stored.
        prov_repo._now_ms = lambda: _ny("2026-09-15 10:00")
        await prov_repo.invalidate_config_cache()
        async with limiter.acquire("prov-1", "gpt-4", consume={"rpm": 499}):
            pass
        with pytest.raises(RateLimitExceeded):
            async with limiter.acquire("prov-1", "gpt-4", consume={"rpm": 100}):
                pass
```

- [ ] **Step 2: Run Group A**

```bash
zae-limiter local up
export AWS_ENDPOINT_URL=http://localhost:4566 AWS_ACCESS_KEY_ID=test \
       AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1
uv run pytest tests/e2e/test_localstack.py -k "E2ESchedule" -v
```

Expected on a tree without the implementation: `test_a_shrink_boundary_trims_a_full_bucket`
admits 1000 inside the 0.5x window and the `pytest.raises` block fails with
`Failed: DID NOT RAISE`; the reset tests fail with `RateLimitExceeded` on the post-midnight
acquire.

- [ ] **Step 3: Write the Group B tests**

The only cases that need the Lambda's own clock. `*/2 * * * *` matches even minutes, so a window
is one minute long and a boundary arrives at most sixty seconds away — compute the wait rather
than sleeping a flat 120 s.

```python
@pytest.mark.slow
class TestE2EScheduleWithTheAggregator:
    """§8: the same crossing with the aggregator running.

    Real waiting, because the Lambda reads its own clock inside a container
    this test cannot reach — which is also why these are the only cases that
    wait. Everything client-enforced is in TestE2EScheduleBoundaries with an
    injected clock.
    """

    ALTERNATING = (ScheduleEntry(cron="*/2 * * * *", tz="UTC", scale=0.5),)

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def aggr_repo(self, shared_aggregator_stack, unique_name_class):
        ns = f"schedaggr-{unique_name_class}"
        repo = await Repository.open(
            stack=shared_aggregator_stack.name,
            region=shared_aggregator_stack.region,
            endpoint_url=shared_aggregator_stack.endpoint_url,
            config_cache_ttl=0,
        )
        await repo.register_namespace(ns)
        scoped = await repo.namespace(ns)
        yield scoped
        await repo.close()

    @staticmethod
    async def _sleep_to_the_next_even_minute() -> None:
        """Wait until `*/2` next changes state, plus a second of slack."""
        now = time.time()
        await asyncio.sleep(60 - (now % 60) + 1)

    @pytest.mark.asyncio(loop_scope="class")
    async def test_the_aggregator_trims_to_the_scheduled_ceiling(self, aggr_repo):
        """The aggregator's whole job is keeping hot buckets off the slow path,
        so a shrink it does not apply is a shrink that never lands on the
        buckets that matter (§3.3). Drive traffic across a boundary and assert
        the stored balance never exceeds the scheduled share."""
        limiter = RateLimiter(repository=aggr_repo)
        await aggr_repo.set_limits(
            "aggr-sched",
            [Limit.per_minute("rpm", 1000).with_schedule(self.ALTERNATING)],
            resource="gpt-4",
        )
        async with limiter.acquire("aggr-sched", "gpt-4", consume={"rpm": 1}):
            pass

        await self._sleep_to_the_next_even_minute()
        for _ in range(20):
            try:
                async with limiter.acquire("aggr-sched", "gpt-4", consume={"rpm": 1}):
                    pass
            except RateLimitExceeded:
                pass
        await asyncio.sleep(15)  # stream + Lambda

        bucket = next(
            b
            for b in await aggr_repo.get_buckets("aggr-sched", resource="gpt-4")
            if b.limit_name == "rpm"
        )
        now_ms = aggr_repo._now_ms()
        assert bucket.tokens_milli <= bucket.effective_capacity_milli(now_ms)

    @pytest.mark.asyncio(loop_scope="class")
    async def test_the_aggregator_restamps_an_expired_vu(self, aggr_repo):
        """`vu = 0` after a fan-out must be replaced by a real boundary by
        whichever refiller gets there first, or the bucket is pinned to the
        slow path. Here that is the aggregator."""
        limiter = RateLimiter(repository=aggr_repo)
        await aggr_repo.set_limits(
            "aggr-vu",
            [Limit.per_minute("rpm", 1000).with_schedule(self.ALTERNATING)],
            resource="gpt-4",
        )
        async with limiter.acquire("aggr-vu", "gpt-4", consume={"rpm": 1}):
            pass
        # Force vu = 0 through the fan-out, then let the aggregator see it.
        await aggr_repo.set_limits(
            "aggr-vu",
            [Limit.per_minute("rpm", 900).with_schedule(self.ALTERNATING)],
            resource="gpt-4",
        )
        async with limiter.acquire("aggr-vu", "gpt-4", consume={"rpm": 1}):
            pass
        await asyncio.sleep(15)

        item = await _raw_bucket_item(aggr_repo, "aggr-vu", "gpt-4", shard=0)
        assert int(item[schema.BUCKET_FIELD_VU]["N"]) > aggr_repo._now_ms()

    @pytest.mark.asyncio(loop_scope="class")
    async def test_the_same_crossing_without_the_aggregator(
        self, aggr_repo, shared_minimal_stack, unique_name_class
    ):
        """ADR-133: sharding and refill must work either way, so the assertion
        above must hold on a stack with no Lambda at all. The minimal-stack
        half of §8's with-and-without pair; the client-enforced cases in
        TestE2EScheduleBoundaries are the rest of it."""
        repo = await Repository.open(
            stack=shared_minimal_stack.name,
            region=shared_minimal_stack.region,
            endpoint_url=shared_minimal_stack.endpoint_url,
            config_cache_ttl=0,
        )
        try:
            ns = f"schednoaggr-{unique_name_class}"
            await repo.register_namespace(ns)
            scoped = await repo.namespace(ns)
            limiter = RateLimiter(repository=scoped)
            await scoped.set_limits(
                "noaggr-sched",
                [Limit.per_minute("rpm", 1000).with_schedule(self.ALTERNATING)],
                resource="gpt-4",
            )
            async with limiter.acquire("noaggr-sched", "gpt-4", consume={"rpm": 1}):
                pass

            await self._sleep_to_the_next_even_minute()
            for _ in range(20):
                try:
                    async with limiter.acquire("noaggr-sched", "gpt-4", consume={"rpm": 1}):
                        pass
                except RateLimitExceeded:
                    pass

            bucket = next(
                b
                for b in await scoped.get_buckets("noaggr-sched", resource="gpt-4")
                if b.limit_name == "rpm"
            )
            assert bucket.tokens_milli <= bucket.effective_capacity_milli(scoped._now_ms())
        finally:
            await repo.close()
```

- [ ] **Step 4: Run Group B**

```bash
uv run pytest tests/e2e/test_localstack.py -k "E2EScheduleWithTheAggregator" -v
```

Budget roughly three minutes. If a crossing assertion is flaky, raise the post-traffic sleep
before weakening the assertion — LocalStack's stream-to-Lambda latency is the usual cause and
the existing `test_usage_snapshot_generation` (:763) already sleeps 10 s for the same reason.

- [ ] **Step 5: Run the whole e2e suite and stop LocalStack**

```bash
uv run pytest tests/e2e/test_localstack.py -v
uv run pytest tests/e2e/test_localstack.py -m "not slow" -v   # what CI runs by default
zae-limiter local down
```

- [ ] **Step 6: Lint and commit**

No `src/` change, so no sync codegen.

```bash
uv run ruff check --fix .
uv run ruff format tests/e2e
git add tests/e2e/test_localstack.py
git commit -m "$(cat <<'EOF'
✅ test(limiter): end-to-end schedule boundary coverage

Every case design §8 requires, against a real CloudFormation stack: the
shrink that must not leave a spendable surplus, the grow, a boundary
crossed mid-lease with the #455 declared scope intact, concurrent
traffic at the instant of the boundary, cascade with only the parent
scheduled, per-shard convergence, the daily reset with tc still
climbing, an idle bucket waking after its edge, a manifest-applied
schedule reaching a live bucket, a corrupt schedule in both
on_unavailable modes, and the sync client.

Split into two groups rather than waiting everywhere. Everything the
client enforces uses the #430 clock seam, which drives the rf stamp, the
vu comparison, the ttl guard and every refill computation consistently —
so a jumped clock is a real crossing, not a simulated one. Only the
cases that need the Lambda to observe the boundary itself use a `*/2`
schedule and real waiting, and only those are marked slow.

Refs #222
EOF
)"
```

---

### Task 12: ADR-135 and documentation

**Files:** Create `docs/adr/135-scheduled-limits.md`; modify `CLAUDE.md`, `docs/guide/`, `docs/cli.md`, `docs/api/`

> ⚠️ **The user-facing docs are a how-to, not a changelog.** This task writes to two
> audiences and they take opposite treatments, so decide per file before writing a word.
>
> **`docs/adr/135-*.md` and `CLAUDE.md` carry the reasoning.** An ADR exists to record why a
> decision was taken and what was rejected; `CLAUDE.md` is a developer reference. Both should
> be complete.
>
> **`docs/guide/`, `docs/cli.md` and `docs/api/` carry none of it.** A reader there has no
> history to reconcile. The test to apply to every sentence: *if it exists to explain what
> used to be true, what changed, or what an earlier design did, cut it.* No "previously", no
> "note that this no longer", no rejected alternatives, no justification of the design. Show
> the API that exists and what it does. Someone learning the feature did not attend the design
> discussion and does not need to know there was one.
>
> A worked example of getting this wrong, from the guide (#524): an early draft explained why
> `Limit.per_day(...).with_reset_schedule(...)` is rejected. No reader has ever written that
> line — the form they meet is the one that exists. Explaining a rejected shape teaches a wrong
> thing first and then unteaches it.
>
> **Two things that are not history and must stay:** the version admonition (it tells a reader
> on an older release why something is absent), and any *current* limitation — ADR-138's
> fixed-window restriction in particular, stated as a fact about the feature rather than as a
> decision taken.
>
> **Do not frame a configurable thing by one of its values.** The reset period is whatever the
> cron says; a section called "daily quotas" with three midnight examples teaches that quotas
> are a midnight feature. Show the range — a session cap resetting every few hours, a monthly
> plan on the 1st, a weekly cap — with one example worked in full and the rest as one-liners.

- [ ] **Step 1:** Write ADR-135 from the design doc. **Verify 135 is still unclaimed** against `main` and open PRs before using it — #393 holds 126-132. Do not pre-claim a number in a branch name; that practice caused the #304 and #320 collisions. Note `main` now tops out at **138**: ADR-136 (entity config bucket TTL), ADR-137 (a limit drips or resets, never both) and ADR-138 (fixed calendar reset windows only) all landed after this plan was written, and 135 remains free only by accident. ADR-135 must not restate or contradict 137 and 138 — read both first and reference them rather than re-deciding what they settled.
- [ ] **Step 2:** Update `CLAUDE.md`: the schedule attributes in the DynamoDB writer table, `SCHEDULE_BOUNDARY` in the failure-reason list, `cronsim`/`tzdata`/`croniter` in Dependencies, and the retired 1.5x shard transient if the core plan has not already done it.
- [ ] **Step 3:** Run the `docs-updater` agent per `.claude/rules/docs-parity.md`.
- [ ] **Step 4:** Commit — `📝 docs(adr): record the scheduled limits design as ADR-135`

---

## Self-Review

**Spec coverage.** §3.6 → Tasks 1-4. §4.1 reset encoding → Task 4. §5.1 → Task 1 (no signature changes; the schedule rides on `Limit`). §5.2 → Task 8, building on the provisioner plan. §5.3 → Tasks 6-7. §5.4 → Task 9. §6 → Task 10. §7 → Task 5. §8 → Task 11. §9 limitations → documented in Task 12's ADR.

**Expansion status: complete.** Every task now carries a full TDD cycle with real code. Tasks
1, 2, 6, 7, 8 and 9 were written against the tree at `cc1ff1dc`. Tasks 3, 4, 5, 10 and 11 were
expanded later, against `main` at `3601df0a`, once `schedule.py` had merged (core plan Tasks
1-5) along with Task 8 (`Limit.schedule` and config serialisation) and Task 14 (the aggregator).
Their "expand before picking this up" banners are gone because the precondition they named —
"`schedule.py` does not exist yet" — is discharged: every signature in them was read, not
inferred. What the later pass found is recorded under **Corrected during expansion** below;
several of those findings are corrections to the *earlier* tasks and to the core plan, not just
to the five that were compressed.

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
`schedule.py`. **Within this plan, Task 4 precedes Task 3's aggregator half** — the numbering
follows the design's section order (§3.6 before §4.1), not the dependency order, and Task 3
cannot parse `rsched` off a bucket item before Task 4 defines `decode_reset` and stamps it.
Both tasks say so inline.

---

## Corrected during expansion

Found by reading merged `schedule.py`, `models.py`, `bucket.py`, `lease.py`, `limiter.py`,
`repository.py` and `zae_limiter_aggregator/processor.py` against the compressed task text, plus
the core plan's SDD ledger. Each is fixed in the task named, not papered over.

1. **The version marker §4.1 promises does not exist, and Task 10 depended on it.** Core plan
   Task 5 shipped the encoding without one and said so; Task 10's compressed text asserted
   `pytest.raises(RateLimiterUnavailable, match="newer")` against a marker nothing writes.
   **Resolved in Task 10 (Decision 1): not added, distinction dropped, §4.1 amended.** Costs
   stated both ways there; the short version is that the marker buys a log-message distinction,
   not behaviour, cannot classify anything already written, and can be added later without a
   break as long as the reader treats its absence as v1. A follow-up issue tracks it.

2. **Three sides disagreed on an undecodable stored schedule, and two of the three are merged.**
   `processor._decode_schedule` reports it (`sched_error`) because a raise there poisons a whole
   stream batch; `Repository._deserialize_limits` lets `ValueError` propagate; Task 10 demanded
   `RateLimiterUnavailable`. **Resolved in Task 10 (Decision 2): the parser raises `ValueError`
   and each boundary converts.** `schedule.py` is *not* modified — raising an
   `InfrastructureError` from `decode` would slip through the aggregator's `except ValueError`
   and re-arm the poison pill core plan Task 14 fixed, and would end `schedule.py`'s
   import-freedom. Task 10 also records that it is *writing* §6, which was never written, and
   that `acquire()`'s existing `except Exception` already satisfies the `on_unavailable` half
   with no new code.

3. **`encode_reset`, `decode_reset` and `ScheduleEntry.reset` do not exist.** `ScheduleEntry`'s
   `__post_init__` requires exactly one modifier, which a reset entry has none of.
   **`ScheduleEntry.reset` is created by Task 1** (its Step 3 already says so); `encode_reset` /
   `decode_reset` by **Task 4**, which must build through the classmethod and never through
   `ScheduleEntry(...)`.

4. **Task 5's worked example holds exactly** — 30.001 s flat against 50.001 s real, re-derived
   against merged `refill_bucket` and `calculate_retry_after` rather than quoted. **But it does
   not exercise core plan Task 4's two-phase fix**: `America/New_York`'s offset is a whole number
   of hours, so the coarse hourly probe lands on the edge and the refinement never runs. Task 5
   adds an `Asia/Kolkata` (+05:30) case, where a 09:00 local edge is 03:30Z.

5. **`check_availability()` is real and is written against.** `RateLimiter.check_availability(
   entity_id, resource, needed=None, limits=None) -> Availability` at `limiter.py:1941`, with
   `available()` and `time_until_available()` as wrappers.

6. **`next_boundary` is called `next_boundary(sched, reset_sched=(), *, now_ms=...)` everywhere**
   in the expanded text (#500). No positional `now_ms` was reintroduced.

7. **There are four `LimitStatus` sites, not three.** The compressed Task 5 named
   `_build_retry_failure_statuses`, `_admit_limit` and `check_availability` and missed
   `bucket.build_limit_status` via `declared_statuses` / `would_refill_satisfy` — the
   **speculative fast rejection**, which is the path most rejections actually take. Task 5
   converts `bucket.try_consume`, which covers that site and `_admit_limit` together.

8. **`Limit.reset_schedule` was never persisted anywhere.** Task 1 adds the field and touches
   only `schedule.py` and `models.py`; no task wrote `l_{name}_rsched` on the config item. So
   `resolve_limits()` would have returned `reset_schedule=()` for every stored limit and Task 3's
   client-side reset would never have fired in any real deployment. **Task 4 adds the config-item
   leg**, plus `Limit.to_dict()`/`from_dict()` (which feed the audit event `details` — the exact
   defect core plan Task 8 found for `schedule`).

9. **The bucket fan-out lives in `_build_bucket_param_update` (:3256), not `_sync_bucket_params`
   (:3085).** Editing the latter misses `_resolved_bucket_param_update` (:3209), the per-resource
   path used under the entity-wide `_default_` scope (#487). Corrected in Task 4.

10. **A limit's two tuples can disagree on timezone, and one hoisted `sched_tz` cannot hold
    both.** Merged `Limit.__post_init__` validates only `self.schedule`. Task 4 widens the guard
    to the union — same class of defect as core plan Task 8's last-one-wins `sched_tz`.

11. **`vu` ignored the reset tuple on both sides.** `processor._item_next_boundary` (:621) and
    the `vu` computation core plan Task 12 adds to `lease.py` both pass the parameter schedule
    alone, so a limit with a reset schedule and *no* parameter schedule gets `vu = None`, never
    expires, never reaches the slow path, and never resets — which is precisely the daily-quota
    shape §3.6 exists for. Fixed in Task 3, with a test.

12. **The reset seam is before admission, not in `_commit_initial`.** The compressed Task 3 put
    it in `lease.py`'s slow-path refill, which runs *after* `try_consume` has already gated the
    request: the acquire that crosses midnight would still be rejected and only the next one
    would see the restored quota. Task 3 moves it between the capture of
    `_original_tokens_milli` and the call to `_admit_limit`, where the existing delta formula
    resolves to exactly the aggregator's `ADD (eff_cp - tk_observed)`.

13. **A reset must bypass the aggregator's consumption threshold, and `wcu` must be exempt from
    resets.** `try_refill_bucket` `continue`s on a positive delta once projected tokens cover the
    estimate — which would turn the reset off on hot buckets, the same defect §3.3 records for
    the negative clamp. And `rsched` is item-level, so without an exemption a user's daily reset
    would also apply to the per-partition write ceiling. Both fixed in Task 3.

14. **Nothing populates `BucketState.sched` from the item.** Core plan Task 9 adds the field,
    Tasks 12/13 write the attribute, no task reads it back in `_deserialize_composite_bucket`
    (:4878) — which builds every `BucketState` the client sees, including the ALL_OLD states
    behind the fast-rejection path. Flagged as a precondition check in Task 5, which adds it
    alongside `reset_sched` if it is still missing, because a schedule-aware estimate computed
    from an empty `sched` is the flat estimate with a green suite.

15. **`build_composite_create` stamps no `sched`.** Core plan Task 12 adds only `vu` to it and
    Task 13 touches only the fan-out, so a bucket created on the slow path carries `vu` and no
    schedule — it re-materialises at its first boundary and then refills at the **base** rate
    forever. Flagged in Task 4, which adds both stamps there if the core plan has not.

16. **`check_availability` reports a *pending* reset as unavailable.** It reads and writes
    nothing, so a bucket that crossed an edge still holds the burnt balance on disk; without an
    adjustment the display says "0 remaining, resets tomorrow" while the very next `acquire()`
    restores the quota. Fixed in Task 5.

17. **A corrupt schedule on the *system* config item escapes `on_unavailable`.** `acquire()`
    resolves the mode before its try block, and `resolve_on_unavailable()` swallows every
    exception and falls back to its cache or `"block"` — so an operator who configured `allow`
    gets `block`. Recorded as a known limitation in Task 10 and added to design §9 rather than
    fixed; the fix means teaching that method to distinguish "cannot reach DynamoDB" from "read
    a config item I cannot parse".

18. **`Limit.per_day` already exists** (`models.py:340`), so Task 1's "add it if it does not
    exist" is discharged. Left as written; it is harmless and self-checking.

19. **Two stale names from the core plan, carried over.** The aggregator test module is
    `tests/unit/test_processor.py`, not `test_aggregator_processor.py`; and `uv run ruff format .`
    must never be run bare in this repo (local ruff reformats 34 unrelated files including these
    plan documents — core plan Task 4's ledger). Every expanded step scopes the formatter to the
    directories it touched.

## Corrected after ADR-137 / ADR-138 (2026-09-15)

ADR-137 and ADR-138 were accepted, and the core plan reached completion, after every task
above was written. This pass reconciled the document with both.

20. **Seventeen fixtures built a quota out of a drip.** Tasks 3, 4, 5, 9, 10 and 11 spelled it
    `Limit.per_day("rpd", 10_000).with_reset_schedule(DAILY)`, which raises at construction
    under ADR-137 — a positive rate beside a reset. All seventeen are now
    `Limit.quota("rpd", 10_000, cron=..., tz=...)`, with `.with_schedule(...)` chained after it
    at the one site that wanted a parameter schedule too. The only surviving old spelling is
    Task 1's own rejection test. Task 1's note warning that downstream tasks were unconverted
    was removed, since it is no longer true.

21. **Quota fixtures carried a drip rate in their stored parameters.** `_state()` and
    `_quota_state()` in Task 3, and the `_sched_record` images beside them, set
    `refill_amount = 10_000` on limits described as daily quotas. A quota stores `0` and the
    inert `_QUOTA_REFILL_PERIOD_SECONDS`; the values and the docstrings reasoning from them are
    now consistent. This *strengthens* two tests rather than weakening them — an unreset quota
    bucket yields no refill delta at all, so the reset is unambiguously the only writer.

22. **`retry_after_with_schedule` returns 0.0 for every quota** — flagged in Task 5, not
    fixed. Its loop gates on a positive rate before it consults the reset edge, and ADR-137
    makes a quota's rate zero by definition, so the walk exits on iteration 1 into
    `calculate_retry_after(deficit, 0, rp)`, which merged `bucket.py:188` returns `0.0` from.
    "Retry immediately, forever, until midnight" is the exact opposite of the section's
    headline claim, and it is silent. The ordering is Task 5's call; the three tests written
    against the pre-ADR-137 shape are named in that flag and deliberately left unchanged.

23. **`_decode_limits`'s exact-set filter is a silent landmine for Task 8** — flagged in
    Task 8, not fixed. `bucket_sync.py:334` admits a decoded limit only when
    `decl.keys() == {"capacity", "refill_amount", "refill_period"}`. Correct today (a quota
    still decodes to those three keys), and broken the moment Task 8 widens the shape with
    `schedule` / `reset_schedule`: the limit drops out of `resolve_bucket_limits()` with no
    exception and no log line, and the fan-out then unstamps the limit it was asked to
    schedule. Whether the fix is a superset check or an allow-list is Task 8's call — but it
    must be decided in Step 3, not discovered.

24. **Task 8 would have reintroduced #488.** Its Step 3 sketch and
    `test_unscheduled_limits_remove_the_stamps` both REMOVEd `vu`. Core plan Task 13 now SETs
    `vu = 0` unconditionally on every fan-out, scheduled or not, and SET + REMOVE on one
    attribute is a `ValidationException`. The REMOVE branch is now `sched` / `sched_tz` /
    `rsched` only.

25. **Two "not yet" notes had become false.** Task 8's `_default_` gap closed when #487
    landed, and `build_composite_create` now stamps `sched` / `sched_tz` / `b_{name}_sched`, so
    Task 4 adds `rsched` beside an existing stamp rather than introducing one. Both notes were
    rewritten rather than deleted, since their reasoning still guards against a regression.

26. **Four steps still ran the formatter bare**, contradicting ledger entry 19's own claim that
    "every expanded step scopes the formatter". Tasks 6, 8, 9 and 10 now run
    `pre-commit run --files <paths>`, which applies ruff check and format at the pinned 0.9.2
    over exactly the files that task touches.

27. **Every line reference in the document has drifted** past the core plan's merge. Recorded
    in the Global Constraints with the offsets spot-checked during this pass, rather than
    rewritten task by task — the claims they support were verified and still hold.
