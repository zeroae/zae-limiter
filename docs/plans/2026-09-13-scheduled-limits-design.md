# Scheduled (cron) Limits — Design

**Issue:** #222 · **Status:** In progress — Sections 0–1 settled, Section 2 onward open
**Date:** 2026-09-13

> This document captures a design brainstorm that was interrupted. Sections 0 and 1 were
> reviewed and approved; everything from Section 2 is still open. A later session should
> resume at Section 2 rather than re-deciding what is recorded here.

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

**What native scheduling replaces:**

| The cron job wants to | Native equivalent |
|---|---|
| Swap capacity at a boundary | Effective limit = `f(base, schedule, now)`, resolved where params are materialised |
| Reset usage at a boundary (daily quota) | Calendar-aligned refill — a variant of `refill_bucket()`, 0 extra RT, nothing deleted |
| "Can I proceed, and if not when?" | `acquire()` already answers this in 1 WCU (0 RCU + 0 WCU on fast rejection) with `retry_after_seconds` |

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

`Limit` gains `schedule: tuple[ScheduleEntry, ...] = ()`.

Validation at construction: cron parses (ranges, lists, steps, names), `tz` resolves,
`scale > 0`, absolute ints > 0, not both kinds set on one entry.

**`scale` multiplies capacity *and* refill_amount together**, so time-to-fill is preserved.
Halving only capacity would silently double refill speed relative to bucket size, which is
not what "half the limit" means to anyone.

### 1.2 Entry semantics: cron as a match pattern

An entry is **active while `now` matches all five cron fields as sets** — exactly how a cron
daemon matches the current minute. Peak hours are `* 9-17 * * MON-FRI`. **First matching
entry wins**; no match means the base limit applies.

This is stateless and O(1) per evaluation, needs no third-party dependency (parsing
ranges/lists/steps/names is ~60 lines), and gives an identical answer on the client and in
the aggregator. The rejected alternative — cron ticks as state transitions, where an entry
stays active until another fires — needs prev-fire computation across all entries (a
`croniter`/`cronsim` dependency, also in the Lambda) and makes the active entry depend on
history, so a clock skew across a tick lets two evaluators disagree.

### 1.3 Timezones

Per-entry IANA `tz`, defaulting to `UTC`, evaluated with stdlib `zoneinfo` so DST is handled.

**Cost:** the Lambda needs the `tzdata` wheel added to the `[lambda]` extra — the Lambda
runtime image may not ship `/usr/share/zoneinfo`. Roughly 20 bytes per entry for the stamped
tz string.

UTC-only was rejected: "9–5 weekdays" then has to be hand-converted and drifts an hour twice
a year, which is exactly the thing an operator gets wrong. A single namespace-level timezone
was rejected because multi-tenant namespaces spanning regions need per-tenant schedules
anyway.

### 1.4 Where the schedule is stored

| Item | Attribute | Notes |
|------|-----------|-------|
| Config item (system / resource / entity) | `l_{name}_sched` | Beside the existing ADR-114 `l_{name}_cp/ra/rp` |
| Bucket item | `b_{name}_sched` | Stamped by `build_composite_create` and by the `set_limits` fan-out |
| Bucket item | `vu` (item-level) | Valid-until, epoch ms — see §2.1 |

Encoding is a compact JSON list with short keys, e.g.
`[{"c":"* 9-17 * * 1-5","z":"America/New_York","s":0.5}]` — roughly 50–70 bytes per entry.

**Base `cp`/`ra`/`rp` on the bucket item stay exactly as they are today** — the undivided
base. Shard math (ADR-133/134) keeps dividing the base; the schedule applies on top. Nothing
else about the item changes.

**Open cost question (measure, do not guess):** a 2-user-limit bucket item is roughly
350–450 bytes today including attribute names. Two schedule entries per limit adds ~250
bytes. WCU is billed per 1 KB, so the item must stay under 1 KB or **every acquire doubles in
write cost**. Measure before committing to the encoding; a stamped reference the aggregator
caches is the fallback if inline does not fit.

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
```

Round-trips through the `Custom::ZaeLimiterLimits` CloudFormation resource the way `Disabled`
does (ADR-125).

The `-l name:rate/period` CLI shorthand is **not** extended — schedules are set via the API
or the manifest. A CLI cron mini-syntax is a later nicety, not a need.

### 1.6 Inheritance: none across config levels

The schedule rides with the limit at whichever level wins the existing four-tier resolution.
An entity-level `rpm` with no schedule **removes** the resource-level schedule for that
entity, exactly as it already replaces the numbers. This answers #222's open question
"override or merge?" with **override**. Merging across levels is YAGNI and would need a
conflict rule for overlapping cron windows.

### 1.7 Rejected alternatives (Section 1)

- **Schedule as its own config item**, referenced by id from bucket items. Keeps items small
  and lets limits share a schedule, but the aggregator then needs a read, it is a new item
  type with its own lifecycle and CLI, and "which schedule applies" becomes a second
  resolution walk. More machinery for a size problem encoding can solve.
- **In-stack scheduler**: EventBridge → provisioner Lambda materialises limits at each
  boundary via `set_limits`. Works for un-upgraded clients and reuses #405, but costs
  O(buckets) writes per tick per scheduled resource, bounds boundary precision by
  scheduler+Lambda latency, and is literally the out-of-tree cron job moved in-stack — the
  thing #222 rejects.
- **Client-only resolution from cached config**: cheapest to build, but the aggregator keeps
  refilling from stale item params until a client slow-path pass syncs them, so a shrink
  over-refills on the fast path for up to a refill window.

## 2. Evaluation and enforcement — OPEN, resume here

The shape is decided; the details are not.

### 2.1 Decided: the fast path never evaluates a schedule

The speculative fast path is a conditional `UpdateItem` that reads no config. It must not
learn what a schedule is. Instead, one item-level attribute:

**`vu` (valid-until, epoch ms)** — the earliest instant at which any limit on the item changes
effective params. Absent means "no schedule, never expires".

| Path | Behaviour |
|------|-----------|
| **Fast path** | Condition gains `(attribute_not_exists(vu) OR vu > :now)` beside the existing TTL and `disabled` guards. Passes → 1 WCU as today. Fails → `ALL_OLD` shows `vu <= now` → new `SpeculativeFailureReason.SCHEDULE_BOUNDARY` → slow path. Must be interpreted **before** the exhausted/refill reasons so an expired window never looks like a rejection. No schedule evaluation, no config dependency, no CPU on the hot path. |
| **Slow path** | The only place the client evaluates: `effective_params(base, sched, now)` → refill at the effective rate, clamp `min(tk, eff_cp)`, write params + `vu = next_boundary(sched, now)` under the existing `rf` optimistic lock. One write it already does. |
| **Aggregator** | Same evaluation from `b_{name}_sched` on the item (no config read) inside its existing `rf`-locked refill; if `vu <= now` it re-materialises. Active buckets self-correct within one stream latency even if every client stays on the fast path. |
| **Actor fan-out** (`set_limits`, manifest apply, later #223 adaptive / #224 webhooks) | Writes base + `sched` to every shard (the #468 fan-out) and either computes `vu` or sets `vu = 0` to force one slow pass. |
| **Idle buckets** | Nothing happens, correctly. First write after they wake pays one fallback. |

**Cost per boundary:** once per *active* bucket — one failed conditional (1 WCU) plus one
slow-path pass (~1 RCU + 1 WCU). At 10k active buckets and 4 boundaries/day that is ~40k
extra slow-path passes/day, roughly $0.05. Steady-state fast-path cost is unchanged.

**Compatibility:** un-upgraded clients do not send the condition, so they keep the old
behaviour until an upgraded writer or the aggregator re-materialises — graceful degradation,
not a hard break.

### 2.2 Decided: how this composes with the rest of v1.3.0

Two families, and they compose rather than compete:

- **Deterministic modifiers** — #222 (time) and #225 (ramp-up, a function of
  `now − entity.created_at`) — can be evaluated by any writer at write time with zero reads,
  so they belong denormalized on the bucket item.
- **Actor-materialized changes** — #223 (utilization-adaptive) and #224 (webhooks) — cannot
  be computed at read time by anyone but the actor, so they belong on the path that already
  exists for "the base limit changed": `set_limits` → config write → fan-out to bucket items.

```
effective = modifiers(base_params_on_item, policy_on_item, now, created_at)
                ▲                              ▲
   written by set_limits / adaptive /      stamped at bucket creation,
   webhook fan-out (actor path)            re-stamped by the set_limits fan-out
```

**The base stays the base.** An adaptive shrink rewrites `b_rpm_cp`; the schedule never does
— it is applied on top. So an adaptive shrink during peak hours still gets the peak-hours
modifier. No feature overwrites another feature's output. #467 (soft limits) and #311
(bypass) sit beside this: they change what the fast-path condition *includes*, not what
`cp`/`ra` are.

Consequence: **the `set_limits` fan-out becomes the single write path for all actor-driven
changes**, which is why #468 stopped being a bug fix and became infrastructure for this
milestone.

### 2.3 Open — to settle in Section 2 and beyond

1. **`effective_params(base, sched, now)`** — exact signature and where it lives (`bucket.py`
   beside `refill_bucket`?). Must be importable by the Lambda stub (`schema.py`, `bucket.py`,
   `models.py`, `exceptions.py` are the only files copied in).
2. **`next_boundary(sched, now)`** — step forward until the matching-entry set changes, capped
   at 24 h (no transition → `vu = now + 24h`, one forced slow pass per active bucket per day).
   Minute-stepping is ≤1440 cheap checks; if every entry's minute field is `*`, step by hours
   (≤168 for a week). Memoize per `(sched, current window)` so a 1000-record aggregator batch
   does not recompute. Also yields #225 ramp-up for free (next boundary = next step time).
3. **`refill_bucket` must clamp unconditionally.** It returns early without applying
   `min(cap, tokens)` when `tokens_to_add == 0` (`bucket.py:85`), so a surplus over a lowered
   cap survives every pass that computes no refill. This is also what #469 was reaching for
   and is a prerequisite here.
4. **Config/manifest/CLI surface** — `set_limits(schedule=...)`, `get_limits` display, manifest
   parsing in `zae_limiter_provisioner/manifest.py`, CFN round trip, `resource get-defaults`
   output.
5. **Error handling** — invalid cron or unknown tz on a *stored* item (written by an older or
   buggier writer). The fast path cannot validate; the slow path and aggregator must fail
   safe. Proposal to evaluate: treat an unparseable schedule as "no schedule" plus a
   `logger.warning`, never an exception on the admission path.
6. **`retry_after_seconds` across a boundary.** Computed against the *current* effective rate,
   so if capacity rises at 17:00 the real wait may be shorter. v1 says "at most this long";
   refining it to consider the next boundary is a follow-up. Record as a known limitation.
7. **Testing** — unit tests for the cron matcher (ranges, lists, steps, names, DST
   transitions), `effective_params`, `next_boundary`; a LocalStack test crossing a real
   boundary; a test that the fast path never reads config when `vu` is in the future.
8. **ADR number** — pick at write time by checking the highest on `main` *and* in open PRs
   (#393 holds 126–132). Do **not** pre-claim a number in an issue title or branch name; that
   practice caused the #304 and #320 collisions.

## Related

#222, #225, #223, #224, #467, #311, #405, #468 (fan-out infrastructure), #469/#470/#471/#472/#473
(parked primitives this replaces), #477, ADR-100, ADR-114, ADR-125, ADR-133, ADR-134, epic #478.
