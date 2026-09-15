# Scheduled (cron) Limits — Design

**Issue:** #222 · **Status:** Settled — ready for an implementation plan · **ADR:** 135 (unclaimed as of this writing)
**Date:** 2026-09-13

> Sections 0–1 were settled in an earlier session. Section 2 onward was settled in a second
> session, which also corrected §1.4's encoding and size estimates and §2.1/§2.2's reasoning.
> Numbers in this document are measured, not estimated, unless explicitly labelled otherwise.

## 0. Why now, and what it replaces

Rate limits are static today: once defined they hold until someone calls `set_limits()`.
Real deployments want limits that vary by time of day, day of week, calendar event, or
maintenance window.

The immediate driver is concrete. A teammate runs an **out-of-tree cron job that swaps an
entity/resource's capacity at each tick** (peak/off-peak). To make that work they needed
three library primitives, now parked at v1.0.0 pending this design:

| PR | Issue | Primitive |
|----|-------|-----------|
| #469 | — | clamp `tk` when `set_limits` shrinks capacity |
| #471 | #470 | `reset_bucket()` — delete bucket items to clear usage |
| #473 | #472 | `check_availability()` — capacity + wait time in one read |

#222's own "Alternatives Considered" already rejects "external scheduler calling
`set_limits()` on schedule" as adding operational complexity with no atomicity. So the
scheduler belongs in the library, and these primitives are symptoms of its absence.

**#469 and #471 are subsumed and closed; #473 is adopted and landed.** That split is a design
constraint, not a preference. #469 and #471 ask for mechanisms native scheduling supplies
directly, so landing them would ship two ways to do one thing (§3.3, §3.6). #473 is different
in kind: it is a **non-consuming read**, and nothing in this design replaces a read that must
not consume. It is landed with a corrected, shard-aware implementation; §7 records what
scheduling owes it.

**What native scheduling replaces:**

| The cron job wants to | Native equivalent |
|---|---|
| Swap capacity at a boundary | Effective limit = `f(base, schedule, now)`, resolved by every refiller |
| Clamp the surplus after a shrink (#469) | `refill_bucket` clamps unconditionally on every path, §3.3 |
| Reset usage at a boundary (daily quota) (#471) | `reset_schedule` — calendar-aligned token reset, §3.6. 0 extra RT, nothing deleted, `tc` left monotonic |
| Show "how much is left, and when does it reset?" (#473) | **Nothing here replaces it.** `acquire()` answers "can I proceed" in 1 WCU (0 RCU + 0 WCU on fast rejection), but it is a write that *consumes*, and the caller is a display. The non-consuming query — `available()` / `time_until_available()` / the combined `check_availability()` they now both wrap — stays, and §7's boundary-aware estimate has to reach it |

## 1. Data model and storage — SETTLED

### 1.1 Approach: schedule rides with the `Limit`, denormalized onto bucket items

Rejected alternatives are recorded in §1.7.

```python
@dataclass(frozen=True)
class ScheduleEntry:
    cron: str                               # 5-field, matched as a pattern
    tz: str = "UTC"                         # IANA name, validated via zoneinfo
    # exactly one of:
    scale: float | None = None              # multiplier of base capacity AND refill_amount
    capacity: int | None = None             # absolute
    refill_amount: int | None = None        # optional; defaults to the base's value
    refill_period_seconds: int | None = None
```

`Limit` gains two independent tuples: `schedule: tuple[ScheduleEntry, ...] = ()`, which
answers *"what is the limit right now?"*, and `reset_schedule: tuple[ScheduleEntry, ...] = ()`,
which answers *"when does the balance go back to full, in one lump?"* (§3.6).

Validation at construction: cron parses (§3.1), `tz` resolves, `scale > 0`, absolute ints > 0,
not both kinds set on one entry. A `reset_schedule` entry carries `cron` and `tz` **only** —
`scale`, `capacity`, `refill_amount` and `refill_period_seconds` must all be `None`.

**`scale` multiplies capacity *and* refill_amount together**, so time-to-fill is preserved.
Halving only capacity would silently double refill speed relative to bucket size, which is
not what "half the limit" means to anyone.

### 1.2 Entry semantics: cron as a match pattern

An entry is **active while `now` matches all five cron fields as sets** — exactly how a cron
daemon matches the current minute. Peak hours are `* 9-17 * * MON-FRI`. **First matching
entry wins**; no match means the base limit applies.

This is stateless and O(1) per evaluation and gives an identical answer on the client and in
the aggregator. The rejected alternative — cron ticks as state transitions, where an entry
stays active until another fires — needs prev-fire computation across all entries and makes
the active entry depend on history, so a clock skew across a tick lets two evaluators
disagree.

> This choice has a consequence that shapes §3.2: because entries are *windows* rather than
> *fire times*, evaluating a schedule needs both the instant a window opens and the instant it
> closes. No cron library computes the latter. See §3.2.
>
> `reset_schedule` entries invert this: they are **edge**-triggered, firing on the transition
> *into* matching rather than being active throughout. See §3.6.

### 1.3 Timezones

Per-entry IANA `tz`, defaulting to `UTC`, evaluated with stdlib `zoneinfo` so DST is handled.

**Cost:** the Lambda needs the `tzdata` wheel (339 KB) added to the `[lambda]` extra — the
Lambda runtime image may not ship `/usr/share/zoneinfo`.

UTC-only was rejected: "9–5 weekdays" then has to be hand-converted and drifts an hour twice
a year, which is exactly the thing an operator gets wrong. A single namespace-level timezone
was rejected because multi-tenant namespaces spanning regions need per-tenant schedules
anyway.

### 1.4 Where the schedule is stored

| Item | Attribute | Notes |
|------|-----------|-------|
| Config item (system / resource / entity) | `l_{name}_sched` | Beside the existing ADR-114 `l_{name}_cp/ra/rp` |
| Bucket item | `sched` (item-level) | Default schedule for every limit on the item |
| Bucket item | `b_{name}_sched` | Only for a limit whose schedule differs from `sched` |
| Bucket item | `rsched` / `b_{name}_rsched` | Reset schedule (§3.6), same default-plus-override shape |
| Bucket item | `sched_tz` (item-level) | IANA name, hoisted out of every entry |
| Bucket item | `vu` (item-level) | Valid-until, epoch ms — see §2.1 |

**Base `cp`/`ra`/`rp` on the bucket item stay exactly as they are today** — the undivided
base. Shard math (ADR-133/134) keeps dividing the base; the schedule applies on top. See
§2.1 for why the base is never rewritten.

The encoding is specified in §4, which supersedes this section's earlier proposal of a
compact JSON list. The short version: **standard 5-field cron at every boundary of the
system, a compact non-JSON form in storage.** §4 records the measurements that forced it.

### 1.5 Declarative manifest

```yaml
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
```

Round-trips through the `Custom::ZaeLimiterLimits` CloudFormation resource the way `Disabled`
does (ADR-125).

The `-l name:rate/period` CLI shorthand is **not** extended — schedules are set via the API
or the manifest. API/CLI parity is satisfied by `zae-limiter limits apply` being the CLI path
for schedules, not by growing `-l` a cron mini-syntax.

### 1.6 Inheritance: none across config levels

The schedule rides with the limit at whichever level wins the existing four-tier resolution.
An entity-level `rpm` with no schedule **removes** the resource-level schedule for that
entity, exactly as it already replaces the numbers. This answers #222's open question
"override or merge?" with **override**. Merging across levels is YAGNI and would need a
conflict rule for overlapping cron windows.

This also delivers §2.2's exclusion rule across config levels for free: only one level ever
supplies a given limit's config, so two levels cannot contribute two different mechanisms.

### 1.7 Rejected alternatives (Section 1)

- **Schedule as its own config item**, referenced by id from bucket items. Keeps items small
  and lets limits share a schedule, but the aggregator then needs a read, it is a new item
  type with its own lifecycle and CLI, and "which schedule applies" becomes a second
  resolution walk. §4 solves the size problem by encoding, so this is not needed. The
  item-level `sched` default in §4 captures most of the sharing benefit with no indirection.
- **In-stack scheduler**: EventBridge → provisioner Lambda materialises limits at each
  boundary via `set_limits`. Works for un-upgraded clients and reuses #405, but costs
  O(buckets) writes per tick per scheduled resource, bounds boundary precision by
  scheduler+Lambda latency, and is literally the out-of-tree cron job moved in-stack — the
  thing #222 rejects.
- **Client-only resolution from cached config**: cheapest to build, but the aggregator keeps
  refilling from stale item params until a client slow-path pass syncs them, so a shrink
  over-refills on the fast path for up to a refill window.

## 2. Evaluation and enforcement — SETTLED

### 2.1 The fast path never evaluates a schedule

The speculative fast path is a conditional `UpdateItem` that reads no config and runs none of
our code. It must not learn what a schedule is. Instead, one item-level attribute:

**`vu` (valid-until, epoch ms)** — the earliest instant at which any limit on the item changes
effective params. Absent means "no schedule, never expires".

| Path | Behaviour |
|------|-----------|
| **Fast path** | Condition gains `(attribute_not_exists(vu) OR vu > :now)` beside the existing TTL and `disabled` guards (`repository.py:2615`). Passes → 1 WCU as today. Fails → `ALL_OLD` shows `vu <= now` → new `SpeculativeFailureReason.SCHEDULE_BOUNDARY` → slow path. Must be interpreted **before** the exhausted reasons so an expired window never looks like a rejection, exactly as `DISABLED` is today. |
| **Slow path** | The only place the client evaluates: `effective_params(base, sched, now)` → refill at the effective rate, clamp to the effective cap, write `tk`, `rf`, and `vu = next_boundary(sched, now)` under the existing `rf` optimistic lock. One write it already does. |
| **Aggregator** | Same evaluation from the item (no config read) inside its existing `rf`-locked refill; if `vu <= now` it re-materialises. |
| **Actor fan-out** (`set_limits`, manifest apply, #224 webhooks) | Writes base + `sched` to every shard (the #468 fan-out) and sets `vu = 0`. See §3.4. |
| **Idle buckets** | Nothing happens, correctly. First write after they wake pays one fallback. |

#### `tk` is the only materialised quantity

The bucket item keeps the undivided base `cp`/`ra`/`rp` forever. Every refiller computes
`effective = f(base, sched, now)` as local variables, uses them to drive the refill math, and
discards them. **Nowhere on the item does the effective number appear.**

What enforces the scheduled limit is therefore `tk` itself: the fast path's condition is only
`tk >= consumed`, and the ceiling is enforced entirely by refillers capping `tk` at the
effective capacity and growing it at the effective rate. `vu` is the guarantee that whoever
last wrote `tk` was using params still in force. That is the whole mechanism.

Materialising effective `cp`/`ra` onto the item was rejected because the fast path never reads
`cp` at all, so writing it buys nothing — while costing the only copy of the base needed to
compute the *next* window, forcing either a 1.5 RCU config read at every boundary or storing
the base twice.

A consequence worth naming: because `tk` is the sole enforcement, any writer that credits
tokens without capping can push `tk` above the effective ceiling and the fast path will spend
it. `release()` and `adjust()` do exactly that, via unconditional `ADD +delta`. This is
already true today against the base capacity; §3.3's unconditional clamp pulls it back on the
next refill pass. Schedules make it slightly more visible, because the ceiling can now drop
out from under a bucket that was legitimately full.

#### Cost per boundary

Once per *in-flight request* at the boundary, not once per bucket: every request in flight
fails the condition together. Each pays one failed conditional (1 WCU) plus one slow-path pass
(~1 RCU + 1 WCU). They then serialise on the `rf` optimistic lock, and the losers take
`build_composite_retry`, which is **safe** here — its `tk >= consumed` condition sees the
winner's already-landed clamp, because a loser only builds the retry *after* the lock failure.
So concurrency at a boundary is a cost note, not a correctness one. Steady-state fast-path
cost is unchanged.

At 10k active buckets and 4 boundaries/day, ~40k extra slow-path passes/day, roughly $0.05.

#### Compatibility

Un-upgraded clients do not send the condition, so they keep the old behaviour until an
upgraded writer or the aggregator re-materialises — graceful degradation, not a hard break.

### 2.2 Composition with the rest of v1.3.0, and the exclusion rule

Two families:

- **Deterministic functions** — #222 (time) and #225 (ramp-up, a function of
  `now − entity.created_at`) — are evaluated by any reader at read time with zero reads, so
  they belong denormalized on the bucket item.
- **Actor-materialized changes** — #223 (utilization-adaptive) and #224 (webhooks) — cannot
  be computed by anyone but the actor, so they belong on the path that already exists for
  "the base limit changed": `set_limits` → config write → fan-out to bucket items. They write
  the **base**; a function then transforms it at read time.

```
effective = function(base_params_on_item, sched_on_item, now)
                ▲                            ▲
   written by set_limits / adaptive /    stamped at bucket creation,
   webhook fan-out (actor path)          re-stamped by the set_limits fan-out
```

#### Exclusion rule

**At most one *function* per bucket**, and **adaptive (#223) is incompatible with any
function**. Webhooks (#224) stay compatible.

Scope: per bucket (entity + resource), not per limit — if a bucket uses cron, every limit on
it uses cron or none. Different limits on one bucket **may** carry different cron schedules
(halve `tpm` overnight while `rpm` holds flat); `vu` is then the min of each limit's next
boundary.

`schedule` and `reset_schedule` are the same mechanism (cron) and compose without ambiguity —
the parameter schedule sets the ceiling, the reset schedule sets the balance to it — so one
bucket may carry both.

Two distinct reasons, worth keeping separate:

- **#222 vs #225** is a bookkeeping problem. Both are functions applied to the same base, so
  allowing both forces a composition order — does a 0.5x business-hours scale apply before or
  after a 30%-of-target ramp? That ordering is arbitrary, invisible in the stored data, and a
  permanent source of "why is my limit 150 and not 200". Excluding them deletes the question.
- **#222 vs #223** is a control-stability problem. #223 is a closed loop: it measures
  utilization and writes the base to correct it. A schedule scaling its actuator by 0.5 at
  09:00 and back at 17:00 gives the controller a time-varying loop gain it cannot observe — it
  sees utilization against the effective limit but writes the base, so the two are a factor of
  two apart across a boundary it does not know exists.

Enforcement: cross-level exclusion needs none (§1.6). Same-level exclusion is one validation
check at config-write time, which belongs to whichever of #223/#225 lands second.

**The base stays the base.** A webhook shrink rewrites `b_rpm_cp`; the schedule never does.
So a webhook throttle during peak hours still gets the peak-hours modifier, and an operator
can emergency-throttle a scheduled resource. #467 (soft limits) and #311 (bypass) sit beside
this: they change what the fast-path condition *includes*, not what `cp`/`ra` are.

Consequence: **the `set_limits` fan-out becomes the single write path for all actor-driven
changes**, which is why #468 stopped being a bug fix and became infrastructure.

## 3. Evaluation core

### 3.1 `schedule.py` — a new pure-stdlib module

Added to `infra/lambda_builder.py`'s stub copy list beside `schema/bucket/models/exceptions`:

```python
ScheduleEntry                                   # the §1.1 dataclass lives here
matches(parsed, now_ms) -> bool                 # cron-as-set-pattern, §1.2
effective_params(cp_milli, ra_milli, rp_ms, sched, now_ms) -> tuple[int, int, int]
next_boundary(sched, reset_sched, *, now_ms) -> int  # epoch ms, min across both tuples
prev_reset_edge(reset_sched, now_ms) -> int | None  # most recent rising edge, §3.6
encode(sched) -> str / decode(s) -> tuple[...]  # §4
```

Plain ints in and out, no `models` import, so `models.py` can import `ScheduleEntry` from here
without a cycle and the aggregator runs the same code path as the client. `sched == ()`
returns the base unchanged, so the unscheduled path costs one tuple check.

#### Parsing: cronsim; matching: ours

`CronSim(expr, epoch0)` yields `minutes / hours / days / months / weekdays` as fully expanded
`int` sets plus **`day_and`** — cronsim computing the dom/dow rule for us. That rule is the
single highest-risk thing we would have hand-rolled: when *both* day-of-month and weekday are
constrained, cron means **or**, not **and**, so `* * 13 * FRI` matches Sunday the 13th *and*
Friday the 11th. `CronSimError` becomes `ScheduleEntry`'s validation error, so a schedule that
stores is a schedule that evaluates.

`cronsim` joins the runtime dependencies and the `[lambda]` extra beside `tzdata`: zero
transitive dependencies, Python 3.10+, `py.typed`, 1223 LOC.

Three things stay ours, each a confirmed trap:

1. **Sunday.** cronsim maps `SUN` and `0` to `{0}` but leaves `7` as `{7}`, unnormalized.
   `datetime.isoweekday()` gives Mon=1…Sun=7, so the match must accept both:
   `iso in weekdays or (iso == 7 and 0 in weekdays)`.
2. **Extended tokens fail silently.** `L`, `LW`, `FRI#2` and `5L` all **parse without error**
   and inject sentinels (`-1000`, `-1001`) or tuples (`(5, 2)`) into the sets. A naive
   `day in days` match then returns `False` forever — a schedule that silently never
   activates, with no error anywhere. `ScheduleEntry.__post_init__` **must** reject any parse
   whose `days`/`weekdays` contain non-`int` members.
3. The match loop itself.

#### `croniter` as a test-only oracle

`croniter` goes in the `[dev]` extra only. The shipped test asserts our matcher agrees with
`croniter.match()` across a year spanning both DST transitions. Already run during design:
**21,888 comparisons over 8 expressions — all agree** (`* 9-17 * * MON-FRI`,
`*/15 0-6,22-23 * * *`, `* * 13 * FRI`, `0 0 * * SUN` vs `0 0 * * 7`, `30 2 * * *`,
`* * 1 JAN,JUL *`, `*/5 9-17/2 1-7 * MON`). Zero runtime or Lambda cost.

### 3.2 `next_boundary` — our own scan

**No cron library can supply this.** Every one of them computes *fire times*. Under §1.2's
match-pattern semantics, `get_next` from inside a window returns `now + 1 min` (verified), so
fire times give window **starts** and nothing about window **ends** — and `vu` needs both.
There is no `next_non_match` in any of them.

`croniter.match()` is also disqualified as the scan's inner loop: it is a classmethod that
re-parses the expression on every call, and `match_range` is identical. Measured:

| approach | per step | 744-step hourly scan | 10080-step minute scan |
|---|---|---|---|
| `croniter.match()` | 362 µs | 270 ms | 3.6 s |
| pre-parsed sets + `zoneinfo` | **1.05 µs** | **0.8 ms** | 10.6 ms |
| `cronsim` iteration | 8.3 µs | 6 ms | *(starts only)* |

Algorithm: **scan in UTC, match in local, adaptive granularity.**

- Scanning UTC and converting to each entry's tz per step means the conversion only ever goes
  UTC→local, which is always well-defined, so nonexistent and ambiguous local times
  (spring-forward 02:00, fall-back 01:00) never arise.
- Step granularity is the finest field any entry constrains: minute if any entry pins minutes,
  else hour, else day. Cap the scan at 7 d / 31 d / 366 d respectively, bounding the step count
  at 10080 / 744 / 366.
- No transition within the cap → `vu = now + cap`. At hourly granularity that is one forced
  slow pass per active bucket per month.
- Memoize per `(encoded sched, current window)` so a 1000-record aggregator batch computes it
  once.
- `reset_schedule` contributes candidates too: the scan looks for the earliest instant at
  which *either* the parameter set changes *or* a reset edge fires, so `vu` is the min across
  both tuples.

DST needs no special case: a `9-17 America/New_York` window is 9–17 local on both sides of a
transition, so `effective_params` does not change at the transition instant — only the window
edge's UTC instant moves. Verified end to end: the 09:00 edge holds at 9 a.m. local while
moving **14:00Z → 13:00Z** across spring-forward, and the 23-hour and 25-hour days come out as
1380 and 1500 real minutes with no minute skipped or double-counted.

**Lever not built:** an analytic `next_boundary` (complement field sets, `vu` = min over four
candidates, since `NOT(m ∧ h ∧ M ∧ (dom ∨ dow))` decomposes) would remove the scan entirely.
At 0.8 ms memoized, it is premature. Recorded for if profiling ever demands it.

### 3.3 `refill_bucket` must clamp unconditionally — and so must the aggregator

`refill_bucket` returns early without applying `min(cap, tokens)` when `elapsed_ms <= 0` and
when `tokens_to_add == 0` (`bucket.py:85`). **Both early returns need the clamp**, or a
surplus over a lowered cap survives every pass that computes no refill.

That is necessary but not sufficient. `try_refill_bucket` computes
`refill_delta = new - old` and skips on `if refill_delta <= 0: continue`
(`processor.py:574`). With an unconditional clamp the delta at a shrink is *negative*, so the
aggregator would silently skip the trim while still re-stamping `vu`, unblocking the fast path
with a surplus above the new cap. **That guard must go.**

A negative `ADD` is safe there for the same commutativity reason the positive one is: the
delta removes exactly `T0 − eff_cp`, concurrent consumption subtracts independently, and
during a boundary window the fast path is blocked by `vu`, so consumption is bounded by slow
paths already using the new cap.

Two consequences:

- **This is what replaces #469.** Trimming happens on every refill path, always, rather than
  as a targeted clamp bolted onto `set_limits`. It matters most on hot buckets, where the
  aggregator's whole job is to keep tokens topped up so the client slow path never runs — so
  without the `processor.py` change, a `set_limits` shrink would never take effect on exactly
  the buckets that matter.
- It retires the documented "transient up to 1.5x capacity for one refill window" after a
  shard doubling: shard 0 is now clamped to its share on the next pass. That is a CLAUDE.md
  correction, not a regression.

### 3.4 The actor fan-out writes `vu = 0`

`_sync_bucket_params` (the #468 fan-out) gains `sched`, `sched_tz` and `vu = 0` alongside the
`cp`/`ra`/`rp` it already writes.

`vu = 0` rather than a computed boundary. Computing it is cheaper but unsafe: it leaves `vu`
in the future while `tk` still holds a surplus over the new cap, so the fast path admits
against it until natural refill catches up — precisely the burst #469 existed to prevent.
`vu = 0` forces one materialisation pass that trims.

Cost at 10k active buckets: one failed conditional plus one slow pass each, **≈ $0.014 per
admin operation**, on a path a human triggers.

### 3.5 Plumbing

`BucketState` gains `sched`. `effective_capacity_milli`, `effective_refill_amount_milli` and
`retry_refill_amount_milli` change from properties to methods taking `now_ms`, because the
effective capacity is now a function of time — 9 call sites in `bucket.py`, 1 in `lease.py`
where `_build_retry_failure_statuses` needs `now_ms` threaded from its caller. Order of
operations is **scale first, then `// shard_count`**.

`Limit.from_bucket_state()` and `Limit.per_shard()` feed `RateLimitExceeded` (#475), so they
must apply the schedule too — otherwise a rejection during a `0.5x` window quotes the base
capacity.

### 3.6 Calendar-aligned reset (`reset_schedule`)

Entries in this second tuple mean **"when this window opens, set `tk` to the effective
capacity."** They carry `cron` and `tz` and nothing else (§1.1).

This is the one thing a token bucket cannot express. A daily quota — 10,000 requests a day,
back to 10,000 at midnight — is *not* `refill_period_seconds: 86400`, which drips ~0.116
tokens/second continuously, so a caller who burns the quota at 00:01 earns it back a fraction
at a time across the following 24 hours. `reset_schedule` restores the balance in one lump at
a calendar instant.

**Edge-triggered, unlike `schedule`.** A `schedule` entry is active *while* it matches
(level); a reset fires on the transition *into* matching (edge). `0 0 * * *` matches for
exactly one minute, and conflating the two would make the reset depend on a request happening
to arrive inside that minute.

**A separate tuple, not a third entry kind**, because §1.2's entries are priority-ordered
parameter overrides resolved by first-match-wins. A reset entry overrides no parameters, so
putting it in that list lets it win the match and then supply nothing.

**Detection is backwards, not forwards.** The materialising pass asks
`prev_reset_edge(reset_sched, now) > rf` — was there a rising edge since this item was last
refilled? That makes idle buckets correct for free: a bucket idle from 18:00 to 09:00 has `vu`
sitting at midnight, and the 09:00 pass sees the missed edge and applies the reset then. Two
missed midnights apply once, because setting the balance to capacity is idempotent. The
backwards scan is capped exactly as §3.2's forward scan is; no edge within the cap plus an
`rf` older than the cap means the expression never matches (`0 0 30 2 *`), which resets
nothing.

**Per shard, the reset is to the shard's share** — `tk = effective_capacity_milli`, already
`capacity // shard_count` (§3.5). Resetting every shard to the undivided capacity would
multiply the entity's quota by `shard_count`.

**`tc` is untouched**, so the total-consumed counter stays monotonic. This is strictly safer
than #471's `reset_bucket()`, which deleted the item and cleared `tc` with it:
`.claude/rules/design-validation.md` exists precisely because usage aggregation derives
consumption from that counter's deltas.

Ordering when both fire at the same boundary: compute effective params first, then set `tk` to
the resulting effective capacity. The aggregator expresses the reset as
`ADD (eff_cp − tk_observed)` under its existing `rf` lock — the identical negative-delta shape
§3.3 already requires.

## 4. Encoding

**Standard 5-field cron at every boundary of the system — API, YAML, CloudFormation, CLI
display, audit events. The compact form is purely a storage encoding.** A tolerant reader may
accept the compact form on input (detection is trivial: standard cron always contains spaces,
compact never does), but it is not the documented interface.

### 4.1 The compact form

`[{"c":"* 9-17 * * MON-FRI","z":"America/New_York","s":0.5},…]` becomes:

```
h9-17w1-5s500;h0-6c2000
```

Wildcard fields omitted, remaining fields letter-tagged (`m h D M w`), names normalized to
numbers, `scale` as an integer per-mille, entries separated by `;`. Timezone hoisted to a
single item-level `sched_tz`. Per-limit `b_{name}_sched` written **only** when that limit's
schedule differs from the item-level `sched` default — schedules *may* differ per limit, but
sharing one is the normal case, and this reuses §1.6's override-not-merge idiom rather than
introducing a table and indices.

It decodes losslessly to a canonical 5-field cron string (`h9-17w1-5` → `* 9-17 * * 1-5`), so
cronsim remains the only parser and the §3.1 oracle test covers both forms unchanged.

Reset entries encode into their own `rsched` / `b_{name}_rsched` attributes with the same
grammar minus the modifier token, so they are very small — `0 0 * * *` is `m0h0`, 4 bytes.
Keeping them in a separate attribute rather than tagging them inside `sched` mirrors the
separate tuple and keeps the decoder from having to partition one list into two meanings.

**No version marker is carried.** An earlier draft of this section promised one (~6 B) so §6
could distinguish "written by a newer client" from "corrupt". It was not built: core plan
Task 5 (PR #504) shipped the encoding without it, and surface plan Task 10 (PR #514) decided
against adding it. The decisive argument is that a marker cannot classify anything *already*
written — an unmarked string stays ambiguous between "an older client wrote this" and "this is
corrupt" — so the distinction only works forward from the day it ships, against 6 B on every
scheduled bucket item forever and the 1 KB boundary §4.2 exists to defend.

What is lost is a log line, not behaviour: both readings produce the identical action
(`RateLimiterUnavailable` on the client, skip-the-bucket in the aggregator), so nothing
downstream branches on it. `_tokenise` already discriminates *structurally* — an unknown tag
raises `malformed compact schedule entry ...: cannot parse from offset N` where a cronsim
rejection reads `invalid cron expression ...` — which is a heuristic, not a proof, since
corruption can also fail at an offset. Adding a marker later is not a break provided the reader
treats its absence as v1, which it must do regardless for every item written before one exists.
Tracked as #515.

### 4.2 Why — measured

DynamoDB bills WCU per 1 KB, so a bucket item crossing 1 KB **doubles the write cost of every
acquire on that bucket, forever**. Measured against the real `build_composite_create` shape
using DynamoDB's own sizing rules:

| bucket item | JSON (§1.4's original proposal) | compact + item-level default |
|---|---|---|
| baseline, 2 user limits + `wcu`, no schedule | 479 B | 479 B |
| 2 limits × 2 entries | 733 B | **539 B** |
| 3 limits × 2 entries | 917 B | **600 B** |
| 4 limits × 3 entries | **1337 B — over** | **675 B** |
| 6 limits × 4 entries | **2129 B — over** | **805 B** |
| *6 limits, no schedule at all* | 721 B | 721 B |

The compact form is **4.9x smaller** per schedule. The JSON proposal crossed 1 KB at 4 limits
× 3 entries — not an exotic configuration.

(An earlier draft of this sentence said "3 limits × 2 entries", contradicting the table two
lines above it: 917 B is under 1024. The table itself was always right — it marks only the
4 × 3 and 6 × 4 rows as over. Corrected per #507.)

Note the last row: at 6 limits the item is **721 B before any schedule exists**. The limits
themselves dominate, and an item can already cross 1 KB from limit count alone with no
schedule anywhere. Schedules now contribute 84 B in the worst shared case. That is why there
is **no write-time size budget** — a pre-existing property of composite bucket items is not
something a schedule-specific gate should police.

**Levers not built,** for a pathological config (many limits each with a *distinct* schedule,
which lands at 1091 B): a dedupe table with per-limit indices, or bit-packing the field sets
into a binary attribute at ~8 B per entry. Neither is needed; both are recorded.

### 4.3 Display

Rendering back to cron normalizes: weekday and month always render as **names**
(`1-5` → `MON-FRI`, `1,7` → `JAN,JUL`). So an operator who typed numeric weekday or month gets
names back. This is semantically identical and re-encodes byte-for-byte — verified for
`* 9-17 * * MON-FRI`, `* 0-6 * * *`, `*/15 * * * SAT,SUN`, `0 0 1 JAN,JUL *` and
`* 9-17 * * 1-5`.

Canonical storage also makes `differ.py` correct by construction: it compares manifest against
stored state, and if storage kept the operator's verbatim text, `MON-FRI` versus `1-5` would
read as a change on every apply.

## 5. Surface

### 5.1 Python API — no signature changes

The schedule rides on the `Limit` (§1.1), so `set_limits`, `set_resource_defaults` and
`set_system_defaults` keep their current signatures, and `get_limits` /
`get_resource_defaults` return the schedule because it is a `Limit` field. The only new public
name is `ScheduleEntry`, exported from `zae_limiter`.

*(This supersedes the earlier open item's assumption of a `set_limits(schedule=...)`
parameter.)*

### 5.2 The provisioner gap — fixed first, in this work

`_apply_set` in `src/zae_limiter_provisioner/applier.py` is a bare `put_item` on the config
item and nothing else. The provisioner never touches bucket items except through `fanout.py`
for disable/enable. So **`zae-limiter limits apply` writes config that never reaches existing
buckets.** For entity-level limits, which carry no TTL, a manifest-applied change silently
does not take effect — the same class of bug #468 fixed on the `Repository` path, still open
on the provisioner path.

For scheduling this is blocking, not incidental: a manifest-applied schedule would land on
config and never reach a live bucket, so schedules would not work via the manifest at all.

**This is in scope for this work and lands first**, as a standalone commit before any
scheduling code. It is a pre-existing bug that predates #222, it is independently valuable
(it fixes plain limit numbers, not just schedules), and it is independently testable — apply
a manifest that changes an entity limit, assert the existing bucket item reflects it — so it
does not need scheduling to land to be verified.

Required: `_sync_bucket_params` mirrored in sync boto3 alongside the existing `fanout.py`
mirror — GSI3 KEYS_ONLY shard discovery with the same two-pass race mitigation ADR-125 uses,
`cp`/`ra`/`rp` plus TTL handling and stale-attribute removal — and then, once §3 lands, the
`sched` / `sched_tz` / `rsched` stamp and `vu = 0`.

**Scope is entity-level only.** `set_resource_defaults()` and `set_system_defaults()`
deliberately do not touch buckets: a bucket running on defaults carries a TTL and is recreated
with current params when it expires (#271, #296). The provisioner mirror inherits that rule
rather than widening it.

### 5.3 Manifest and CloudFormation

Follows `Disabled` (ADR-125) exactly. `LimitDecl` gains `schedule`, parsed into
`ScheduleEntry`s through the same cronsim validation, so a manifest that applies is a manifest
that evaluates. `differ.py` includes it in the comparison. `Custom::ZaeLimiterLimits` gains a
`Schedule` property emitted only when declared, mirroring `limits_cli.py:177`'s tri-state
handling. `reset_schedule` follows identically, as a `ResetSchedule` property.

### 5.4 CLI — display only

`entity get-limits`, `resource get-defaults` and `system get-defaults` gain a `Schedule:`
section showing each entry as canonical cron plus a human gloss
("`* 9-17 * * MON-FRI` America/New_York → 50%"), and a `Reset:` line for `reset_schedule`
("`0 0 * * *` America/New_York → refill to capacity"). Setting schedules is API + manifest,
per §1.5.

## 6. Failure handling

An unparseable stored schedule raises **`RateLimiterUnavailable`**, honouring the operator's
existing `on_unavailable` setting.

The alternative — treat it as no schedule plus a warning — silently runs at the **base** limit,
so a parse error doubles a customer's limit when the schedule said `0.5x`. That is the one
outcome nobody chose. It also interacts badly with `vu`: ignoring the schedule while leaving
`vu` expired puts the bucket on the slow path permanently.

A limiter that cannot determine the limit is definitionally unavailable. That knob already
exists, is already documented, and in `allow` mode degrades exactly the way the operator
asked, rather than inventing a fourth behaviour.

The realistic trigger is forward-compatibility, not corruption — a newer client writing an
encoding an older one cannot read — which is why §4.1 carries a version marker, so the log
distinguishes the two.

## 7. `retry_after_seconds` across a boundary

The naive computation — against the *current* effective rate — is **wrong in the direction
that matters**. It over-reports when a boundary raises the limit, and **under**-reports when a
boundary lowers one. Lowering is the headline use case.

Worked example: empty bucket, 500 tokens needed, 1000/min now, boundary in 10 s dropping to
500/min. Naive estimate **30 s**; real wait **50 s** — 10 s yielding 167 tokens, then 333
remaining at half rate.

This is fixed rather than documented: walk forward window by window using `next_boundary`,
accumulating tokens at each window's rate until the deficit clears, capped at ~8 windows with
a fall back to the flat estimate. The boundaries are memoized and it costs microseconds.

**It must reach the query surface, not only the rejection path.** The two places that build a
`LimitStatus` — `lease.py`'s `_build_retry_failure_statuses` and `RateLimiter._admit_limit` —
both run *after* a request was rejected. `RateLimiter.check_availability()` is a third call
site and is not reached by either: it is the non-consuming read, and `available()` and
`time_until_available()` are thin wrappers over it. Wiring only the first two would leave the
display on the flat estimate — the exact thing this section opens by calling wrong in the
direction that matters, and the number a user actually sees.

**A reset edge dominates the walk.** If a `reset_schedule` boundary falls before the deficit
clears by refill, that instant *is* the answer. For a daily quota this is the difference
between reporting hours of drip-refill and reporting "at midnight" — and midnight is the only
useful answer. It is also the sharpest case for wiring the walk into the query surface: with
only the rejection path converted, `acquire()` would say "at midnight" while
`check_availability()` said "in eleven hours" about the same bucket at the same instant.

## 8. Testing

**Unit.** The croniter oracle test (§3.1). Sunday `0`/`7`. `L`/`LW`/`FRI#2` rejection.
`effective_params`. `next_boundary` for starts, ends, both DST transitions, the no-transition
cap, and granularity selection. Encoding round-trip including the §4.3 name normalization.
`refill_bucket` clamping at **both** early returns. The aggregator's negative delta.
Mechanism-exclusion validation. For `reset_schedule`: `prev_reset_edge` across an idle gap,
idempotence when two edges were missed, edge-versus-level (a request arriving inside and
outside the matching minute must behave identically), rejection of an entry carrying a
modifier, and a never-matching expression resetting nothing.

**Integration (LocalStack).** `vu` in the future takes the fast path and reads **no config** —
asserted with the existing `capacity_counter` fixture, since this is the load-bearing claim of
the whole design. `vu` expired classifies as `SCHEDULE_BOUNDARY` ahead of the exhausted
reasons and materialises. The fan-out's `vu = 0` forces exactly one pass. `vu` is the min
across limits with differing schedules.

**E2E.** Boundary crossings use a `*/2` schedule and real waiting, marked `slow`, because the
Lambda's clock cannot be injected the way `Repository._now_ms()` can. Required cases:

- A **shrink** boundary against a full bucket: the surplus is trimmed and unspendable, not a
  free burst.
- A **grow** boundary: extra capacity is available promptly.
- A boundary crossed **while a lease is open**: `adjust`/`release` still land against the
  declared `consume` scope (issue #455).
- **Concurrent traffic at the instant of the boundary**: exactly one materialisation wins,
  losers take the retry path, total admitted never exceeds the new ceiling.
- **Cascade with child and parent on different schedules**, where only the parent's boundary
  fires.
- A **sharded** bucket: every shard converges on its share of the new cap.
- The same crossing **with and without the aggregator**, since ADR-133 means sharding and
  refill must work either way.
- A **daily quota reset**: burn the quota, cross the reset edge, confirm the balance returns
  to capacity in one lump, that `tc` keeps climbing across it, and that a bucket idle across
  the edge resets on wake rather than at the edge.
- A schedule applied **through the manifest and provisioner** reaching live buckets (§5.2).
- A deliberately **corrupt stored schedule**: `on_unavailable` honoured in both modes.

Plus the generated sync counterparts throughout.

## 9. Known limitations

- A boundary is enforced at the granularity of the first request after it, not at the instant
  itself. Idle buckets do not re-materialise until they wake.
- `vu` at the scan cap (§3.2) forces one slow pass per active bucket per cap period even when
  nothing changes.
- The exclusion rule (§2.2) is documented and validated at config-write time, but nothing
  prevents a sufficiently determined operator from writing conflicting state directly against
  the table.
- A reset applies on the first request after its edge, not at the edge itself, so an idle
  bucket's quota visibly returns late. Nothing observes a bucket that no one is using.
- A **resource- or system-level** schedule change reaches existing buckets only when their TTL
  expires and they are recreated, because a `vu` boundary re-materialises from the item's own
  stamped `sched` and never reads config. This is consistent with the existing treatment of
  default-derived params (#271, #296) rather than a new gap, but schedules make the staleness
  window easier to notice. Entity-level changes fan out immediately (§3.4, §5.2).
- A reset sets the balance to the *effective* capacity in force at that instant, so a reset
  landing inside a `scale: 0.5` window restores half. That is the intended reading of
  "reset to the current limit", but it is worth stating because the alternative reading —
  reset to the base — is equally defensible and was not chosen.

## Related

#222, #225, #223, #224, #467, #311, #405, #468 (fan-out infrastructure), #469/#470/#471/#472/#473
(parked primitives this replaces), #475, #477, #455, ADR-100, ADR-114, ADR-125, ADR-133,
ADR-134, epic #478.
