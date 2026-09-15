# Utilization-Adaptive Limits — Design

**Issue:** #223 · **Milestone:** v1.3.0 · **Status:** Draft — decisions settled, not scheduled · **ADR:** unclaimed
**Date:** 2026-09-14 · **Depends on:** #222 ([scheduled limits design](2026-09-13-scheduled-limits-design.md))

> This document came out of an *evaluation* session: how #223 could be built, not a commitment
> to build it now. Every decision below was made explicitly; rejected alternatives are kept
> beside each one. Cost figures are **estimates** from DynamoDB on-demand pricing, not
> measurements, unless labelled otherwise.

## TL;DR

| # | Decision |
|---|----------|
| 0 | #223 is **not** evaluated at acquire time. An actor adjusts buckets; refillers enforce |
| A | The **aggregator is required** for adaptive limits (only for them) |
| B | **Sensor / controller split**: aggregator counts usage, a scheduled controller adjusts |
| 1 | No fresh usage data → **blind never loosens**: `factor = min(current, 1x)` |
| 2 | Controller = **target band + slow up / fast down**, replacing `1 / utilization` |
| 3 | Opt-in is a **`max_scale` multiplier** on `Limit`; `rate`/`burst` keep their meaning |
| 4 | **Pluggable fair-share policies**, exactly one active per **(resource, limit)** |
| 5 | Controller writes its **own bucket attribute** `af`, never `cp`/`ra` |
| 6 | `af` written to **every** adaptive bucket, **only when the factor changes** |
| 7 | Creators **stamp** `af`; the aggregator **repairs** it on active buckets |
| 8 | `max_scale` allowed at **any config level**; all checks run at config-write time |
| 9 | Cascade: count **non-cascading buckets only**; parent and child scale **independently** |
| 10 | Every surface reports the **effective** limit (base × factor) |
| 11 | Band + steps per (resource, limit); tick + freshness per stack |
| 12 | `min_scale` (below 1x) **designed now, shipped later** |

```
aggregator ──ADD──▶ usage counter (non-cascading buckets only)
                          │  every tick
                          ▼
                    controller Lambda
                    (band + AIMD, blind never loosens)
                          │  only when the factor changes
                          ▼
             UpdateItem af (+ vu = 0 on decrease) on every adaptive bucket
                          │
                          ▼
             refillers: effective = base × clamp(af, min_scale, max_scale)
             #222 unconditional clamp enforces a decrease
```

---

## 0. What the issue proposes, and why it changes

#223 computes the effective limit **inside `acquire()`** from a GSI2 aggregation of resource
usage. That does not survive contact with the current design:

| Problem | Detail |
|---------|--------|
| Breaks the fast path | The speculative path is 0 RCU + 1 WCU and reads no config (#315). A GSI2 aggregation adds a round trip and N RCU to **every** request |
| Wrong data source | Usage snapshots are `hourly` / `daily` / `monthly` windows (`processor.get_window_key`). Scaling needs minutes |
| Unstable formula | `factor = 1 / utilization` oscillates against elastic demand (§2) |
| Name collision | The issue's `burst` means "max scale"; in the library it is already the bucket ceiling (§3) |

The #222 design already placed #223 (§2.2 there): it is an **actor-materialized change**,
not a deterministic function. Nobody but the actor can compute it, so it rides the write path,
and refillers enforce the result through `tk`.

**One refinement to #222 §2.2.** That section says adaptive "writes the base". This design has
the actor write a **function input** (`af`) instead, and leaves the base untouched (§5). The
#223 ADR must restate §2.2 accordingly. The exclusion rule and its reason are unchanged.

## A. The aggregator becomes required — for adaptive only

Today the aggregator is an optimization: ADR-133 made write sharding work with
`--no-aggregator`. Adaptive limits are the first feature that **needs** it, because the
aggregator is the only component that sees every consumption.

- `--no-aggregator` stays valid for every stack that does not use adaptive limits
- Writing a `max_scale` limit to a stack without the aggregator **fails loudly**, like the
  `limits` commands already exit 1 when the provisioner is not deployed
- Record it in its own ADR. It does not supersede ADR-133; it scopes a requirement

## B. Sensor / controller split

| | Aggregator = sensor only (**chosen**) | Aggregator = sensor + controller |
|---|---|---|
| Work per batch | 1 `ADD` per active (resource, limit) | `ADD` + O(buckets) rewrites |
| Stream lag | Unchanged | Large fan-outs stall the batch |
| Knock-on | — | Late #317 refills → more requests fall off the 1-WCU fast path |
| Extra infra | Controller Lambda + EventBridge rule + IAM | None |

A stalled stream costs more than one more Lambda. The controller is separate.

**Infra implication:** the controller role follows the existing IAM naming (ADR-116,
component ≤ 8 chars, e.g. `ctrl`) and the permission-boundary / role-name-format options.

---

## 1. Stale data: blind never loosens

**Problem.** The controller cannot tell these apart — the counter stops moving in both:

| Situation | Counter |
|-----------|---------|
| Aggregator stalled (throttle, DLQ, bad deploy) | No updates |
| Resource genuinely idle (3am) | No updates |

**Decision.** No fresh data → `factor = min(current, 1x)`.

- Above 1x → drop to 1x
- At or below 1x → hold (only reachable once `min_scale` ships, §12)
- **Fresh** = counter updated within `freshness_ticks` (default 2) controller ticks

**Why it is fine for idle:** nobody is calling, so 1x costs nobody anything. The first caller
after idle waits ~1 aggregator batch (≤ 5 s `MaximumBatchingWindowInSeconds`) + 1 tick for
scale-up to resume.

**Rejected:**
- *Hold the last factor, then decay* — stays loose while blind, worst during a stall that
  coincides with a surge
- *Detect the stall* (stream `IteratorAge`, DLQ depth) — CloudWatch dependency, no user benefit
  over the chosen rule
- *Original rule "always 1x"* — a stall would lift an active overload cut from 0.5x to 1x (§12)

## 2. Controller: target band + slow up / fast down

**Problem.** `1 / utilization` against users who consume what they are given:

| Tick | Utilization | Factor | Users |
|------|-------------|--------|-------|
| 1 | 20% | 5x | use 5x more |
| 2 | 100% | 1x | drop back |
| 3 | 20% | 5x | … forever |

**Decision.** AIMD-style control with a deadband, measured against `estimated_capacity`:

```
u = rate_since_last_tick / estimated_capacity
if   u < band_low:   factor = min(factor + step_up,   max_policy_factor)
elif u > band_high:  factor = max(factor * step_down, min_policy_factor)
else:                no change, no write
```

- Replaces the issue's formula entirely; does not assume anything about user behaviour
- Protects the backend first (fast down), explores headroom cautiously (slow up)
- The deadband is also the cost control of §6

`rate_since_last_tick` is `(tc_now − tc_prev) / (t_now − t_prev)` on the counter of §B — a
**counter delta**, never a token delta (#179).

**Before implementation:** run `design-validator` on the defaults of §11.

**Rejected:** smoothing alone (still oscillates, slower); band alone with symmetric steps
(slow to protect the backend on a spike).

## 3. Naming: `max_scale`

**Problem.** `Limit.per_minute(name, rate, burst)` already sets `capacity = burst`,
`refill_amount = rate`. The issue's example uses `capacity=1000` as "base" and `burst=10000`
as "max scale" — both wrong under current naming. An adaptive limit needs **both** a burst
ceiling and a scale cap.

**Decision.** A multiplier on `Limit`:

```python
Limit.per_minute("tpm", 1000, burst=5000, max_scale=10)   # up to 10x
```

- Scales `capacity` **and** `refill_amount` together, so the burst ratio and time-to-fill are
  preserved — the same rule as #222's `ScheduleEntry.scale`
- `min_scale` (default `1.0`) is reserved beside it (§12)
- Default `max_scale = 1.0` means "not adaptive"

**Rejected:** absolute ceiling (`adaptive_max_rate=10000`; burst scaling then needs its own
field); a separate `Adaptive(...)` object (more surface for knobs that belong to the policy,
§11).

## 4. Fair share: pluggable policies

**Problem.** Who gets the headroom?

| User | Base | Same factor (2x) | Equal extra (+1000) | By demand |
|------|------|------------------|---------------------|-----------|
| Free | 1000 | 2000 | 2000 | who is maxed out |
| Pro | 5000 | 10000 | 6000 | who is maxed out |
| Enterprise | 20000 | 40000 | 21000 | who is maxed out |

**Decision.** Policies are pluggable in code; **exactly one is active per (resource, limit)**.

| Policy | State | Writes per change | Ships |
|--------|-------|-------------------|-------|
| `same_factor` | 1 factor per (resource, limit) | every adaptive bucket, same value | **first** |
| `equal_extra` | active-entity set | every adaptive bucket, per-bucket value | later |
| `by_demand` | per-bucket usage | per-bucket value | later |

The interface is the controller's: given the (resource, limit) state and the discovered
buckets, return a factor per bucket. `same_factor` returns one value.

**Scope rejected — per bucket:** Alice on `same_factor` and Bob on `by_demand` compete for one
pool, forcing an arbitrary, invisible ordering. Same argument #222 §2.2 uses to exclude a
schedule and a ramp-up on one bucket. **Per resource** was rejected as needlessly coarse:
each limit already has its own counter.

## 5. Where the controller writes: its own attribute

**Use case.** `gpt-4` `tpm`, 2am, factor 3x. Pro customer, base 5000 → 15000. At 02:00:01
billing downgrades them to 4000 via webhook (#224).

If the controller wrote `base × factor` into `cp`/`ra` (rejected):

| Time | Event | Bucket allows |
|------|-------|---------------|
| 02:00:00 | tick reads base 5000, picks 3.5x | 15000 |
| 02:00:01 | webhook fan-out writes 4000 × 3 | 12000 |
| 02:00:02 | tick's fan-out lands 5000 × 3.5 | **17500** ❌ |
| 02:01:00 | next tick re-reads config: 4000 × 3.5 | 14000 |

A downgraded customer gets more than their old plan until the next tick. Worse: anything that
reads `cp` back off the bucket compounds the factor.

**Decision.** Disjoint attributes, so there is no race to manage:

| Writer | Writes | Never writes |
|--------|--------|--------------|
| `set_limits`, manifest apply, webhooks (#468/#487 fan-out) | base `cp`/`ra`/`rp`, `b_{name}_mx` | `af` |
| Controller | `b_{name}_af` (and `vu = 0` on a decrease) | `cp`/`ra`/`rp` |

- **The base stays the base** (#222 §2.1 holds unchanged). The fan-out code is untouched
- Refillers compute the effective params where #222 already does (`BucketState._scheduled_params`,
  #512): `effective = base × clamp(af, min_scale, max_scale)`, then divide by `shard_count` —
  **scale first, divide second**, same order as schedules
- On a **decrease**, the controller sets `vu = 0` in the same `UpdateItem`. The fast-path
  condition `vu > :now` fails, the next acquire takes the slow path, and #222 §3.3's
  unconditional clamp pulls `tk` down immediately. An **increase** needs no `vu`: refill at the
  higher rate is enough
- Adaptive buckets carry no `sched` (exclusion, §8), so the slow path leaves `vu` absent after
  re-materializing
- A missing `af` means 1x

**Controller state.** AIMD needs the previous factor, so there is one small state record per
(resource, limit) — see §Storage.

## 6. Fan-out cost

One factor change = one `UpdateItem` per adaptive bucket (shards included). `UpdateItem` bills
the **full item size**: 1 WCU under 1 KB, 2 WCU if the composite bucket item is larger.

**Estimate, 10k adaptive bucket items:**

| Factor changes / day | WCU / day | $ / day |
|----------------------|-----------|---------|
| 10 (calm) | 100k–200k | $0.06–0.13 |
| 50 (busy) | 500k–1M | $0.31–0.63 |
| 1440 (band too narrow, changes every tick) | 14M–29M | $9–18 |

**The band width is the cost knob.** Plus discovery reads per change (§Open questions).

**Decision.** Write `af` to every adaptive bucket, only on change. Revisit only if measured
cost bites.

**Deferred alternatives:**
- *State record only*, refillers read it — 1 write per change, but no per-bucket `vu = 0`, so
  a decrease waits for the next refill; and only fits `same_factor`
- *Hybrid* — state record is truth, `af` pushed only to recently-active buckets. Cheapest at
  scale, two copies of the truth

## 7. Buckets created after a change

Because §6 writes **only on change**, a bucket created after the last change would sit at 1x
until the next change — possibly all night inside the band.

| Creator | When |
|---------|------|
| Client slow path | First acquire; new shard (ADR-133); after TTL expiry |
| Aggregator | Shard clone from shard 0 (Path 2) |

**Decision.**
1. **Stamp at creation.** The client slow path adds the (resource, limit) state record to the
   config `BatchGetItem` it already issues (`batch_get_configs`, #298) — +0.5 RCU on a config
   cache miss only. The aggregator's shard clone carries `af` across
2. **Aggregator repair.** Once per (resource, limit) per batch the aggregator reads the state
   record (~0.5 RCU, eventually consistent) and, inside the `rf`-locked refill write it
   already makes for an active bucket, `SET`s `af` if it differs. This covers the 60 s config
   cache handing a creator a stale factor

Active buckets converge within one batch. Idle buckets may hold a stale `af`; nobody is using
them, and their first slow-path pass re-stamps.

**Rejected:** stamp only (stale cache leaves a bucket on an old factor until the next change);
controller re-sweeps every tick (throws away §6's savings).

## 8. Opt-in and write-time validation

**Use case.** `gpt-4` `tpm` runs `same_factor`. Alice (enterprise) has her own entity-level
`tpm` for `gpt-4`. #222 §1.6: no inheritance across levels — her config replaces the resource
default entirely. Does she scale?

**Decision.** `max_scale` rides on the `Limit` at **any** level (entity, entity `_default_`,
resource, system), exactly like `schedule` does. The **policy** and `estimated_capacity` live
at (resource, limit). So Alice scales if her own limit sets `max_scale`. This matches the
issue's own example (`set_limits("user-123", …, adaptive=…)`).

Because one config item fully defines a bucket's limits (no inheritance), every check is local
to the item being written:

| Check | Result |
|-------|--------|
| `max_scale > 1` and `schedule` on the **same config item** | ❌ reject — #222 §2.2 exclusion, per bucket |
| `max_scale > 1` on a stack without the aggregator | ❌ reject (§A) |
| `max_scale > 1` and (resource, limit) has no policy or `estimated_capacity` | 🟡 accept, runs at 1x, CLI warns |
| `min_scale < 1` | ❌ reject until §12 ships |

**Exclusion reason, restated.** #223 is a closed loop. A schedule scaling its actuator at a
boundary gives the controller a time-varying loop gain it cannot observe. `af` being a separate
input does not change that.

**Rejected:** resource defaults only — customers with custom limits (usually the best ones)
could never scale.

## 9. Cascade

### 9a. Count each request once

With cascade, one request debits the child bucket **and** the parent bucket. Summing every
bucket's `tc` delta counts it twice (three times with a grandparent).

**Decision.** The aggregator adds to the usage counter only from bucket records whose
`cascade` attribute is `False` (already denormalized onto bucket items).

| Bucket | `cascade` | Counted | Why |
|--------|-----------|---------|-----|
| Alice (child) | True | ❌ | also lands on Acme |
| Acme (parent) | False | ✅ | holds Alice's + its own |
| Bob (standalone) | False | ✅ | lands only here |

**Known limitation.** A parent shard N>0 created by a child's cascade slow path is stamped
`cascade=False` / `parent_id=None` (the #474 note in CLAUDE.md). In a three-level hierarchy the
middle entity's extra shards are therefore counted twice. Utilization reads **high**, so the
controller scales **down** more than needed — the safe direction. Documented, not fixed here.

### 9b. Parents scale independently

Each bucket follows its own resolved `max_scale`. When a child scales but its parent does not,
the parent is the ceiling and the child's scale is unusable. The CLI warns on that
configuration.

**Rejected:** child inherits the parent's scale — breaks #222 §1.6's no-inheritance rule.

## 10. What users see: the effective limit

Same rule as #222: statuses report what is enforced.

| Reports | `check_availability` says | Problem |
|---------|---------------------------|---------|
| Base | limit 5000, available 12000 | available > limit |
| **Effective** | limit 15000, available 12000 | ✅ |

- `af` and `mx` come from the bucket items `check_availability` already `BatchGetItem`s — no
  extra read. The missing-bucket branch takes the factor from the state record in config
  resolution (§7)
- `RateLimitExceeded` statuses and `Limit.from_bucket_state` use effective params, exactly as
  #222 wires them
- **`retry_after_seconds` is an estimate.** #222 walks forward across known boundaries; a
  factor change is not predictable, so the wait uses the current effective rate. Document it
- CLI (`entity get-limits`, `resource get-defaults`): base as configured, plus
  `Adaptive: 5000 × 3.0 = 15000 (same_factor, updated 2m ago)`

**Rejected:** new `base` / `factor` fields on `LimitStatus` / `Availability` — public model and
exception change for data the CLI can already show.

## 11. Tuning knobs

| Knob | Default | Owner | Effect |
|------|---------|-------|--------|
| `band_low` / `band_high` | 0.70 / 0.90 of `estimated_capacity` | policy, per (resource, limit) | no change inside |
| `step_up` | +0.25x per tick | policy | 1x → 5x in 16 min |
| `step_down` | ×0.5 per tick | policy | 5x → 1x in 3 min |
| controller tick | 1 min | stack (deploy option) | EventBridge schedule |
| `freshness_ticks` | 2 | stack (deploy option) | §1 |

Split by owner: band and steps describe **how a resource behaves**; tick and freshness
describe **the controller Lambda's schedule**, which is infrastructure.

**Use case.** 23:00 traffic falls off → 23:01 1.25x → 23:16 5x (cap). 23:30 a batch job pushes
usage to 95% → 2.5x → 1.25x → 1x by 23:33.

**Rejected:** all constants (no escape hatch); everything per (resource, limit) including
tick (controller must run at the fastest tick any resource asks for).

## 12. Below 1x: `min_scale` — designed, not shipped

**Purpose.** Protect a resource that is genuinely overloaded (the upstream provider really caps
at 100k TPM). At 130% utilization and already at 1x, fast-down keeps cutting to `min_scale`.

**Cost.** The base is no longer a guarantee — a pricing and contract change for operators.

**Decision.** Reserve `min_scale` (default `1.0`) on `Limit` and let the controller's floor
honour it, but reject `min_scale < 1` at write time in the first release. Rule §1 was amended
to *blind never loosens* so that shipping `min_scale` later needs no change to stale handling.

---

## Storage

All keys namespace-prefixed (`{ns}/`). Flat attributes (ADR-111).

| Item | PK | SK | Attributes | Writers |
|------|----|----|------------|---------|
| Config (any level) | existing | existing | `l_{name}_mx` (and reserved `l_{name}_mn`) beside `cp/ra/rp` | admin (full-replace `PutItem`) |
| Adaptive policy | `{ns}/RESOURCE#{resource}` | `#ADAPTIVE#{limit}` | `policy`, `estimated_capacity`, `band_low`, `band_high`, `step_up`, `step_down`, `config_version` | admin |
| Adaptive state | `{ns}/RESOURCE#{resource}` | `#ADAPTIVE_STATE#{limit}` | `tc` (ADD), `tc_updated_ms`, `af`, `af_updated_ms`, `prev_tc`, `prev_read_ms` | aggregator: `ADD tc`, `SET tc_updated_ms`; controller: `SET af, prev_*` |
| Bucket | existing | `#STATE` | `b_{name}_mx`, `b_{name}_af` | creators + fan-out: `mx`; controller + aggregator repair + creators: `af` |

- Policy and state are **separate items** so the admin's full-replace `PutItem` on the policy
  never clobbers the controller's factor, and the controller never bumps `config_version`
- Aggregator and controller touch **disjoint attributes** of the state item; `ADD` commutes
- Per-limit `af` / `mx` on the bucket, because policies are per (resource, limit)

### Writer table additions

| Writer | UpdateExpression | Condition | Touches `rf`? |
|--------|------------------|-----------|---------------|
| Aggregator usage counter | `ADD tc :delta SET tc_updated_ms = :now` | none | No |
| Controller factor (state) | `SET af, af_updated_ms, prev_tc, prev_read_ms` | `af_updated_ms = :expected` (single controller) | No |
| Controller fan-out (bucket), increase | `SET b_{name}_af = :af` | `attribute_exists(PK)` | No |
| Controller fan-out (bucket), decrease | `SET b_{name}_af = :af, vu = :zero` | `attribute_exists(PK)` | No |
| Aggregator repair | adds `SET b_{name}_af = :af` to its existing refill | `rf = :expected_rf` (existing) | Yes (existing lock) |

## Cost summary (estimates)

| Path | Added cost |
|------|------------|
| Fast path, steady state | **none** |
| Slow path, config cache miss | +0.5 RCU (state record in the config BatchGet) |
| Aggregator, per (resource, limit) per batch | ~0.5 RCU (state read) + 1 WCU (counter `ADD`) |
| Controller, per tick per (resource, limit) | ~0.5 RCU state read; +1 WCU when the factor changes |
| Controller, per factor change | discovery reads + 1–2 WCU per adaptive bucket item |
| Decrease, per in-flight request | one failed conditional + one slow-path pass, as a #222 boundary |
| Infra | 1 Lambda + 1 EventBridge rule; 1440 invocations/day at a 1-min tick |

## Open questions for the implementation plan

1. **Bucket discovery for the fan-out.** GSI2 (`RESOURCE#{name}`, `BUCKET#` prefix) returns
   every bucket of the resource, adaptive or not. Filter by projected `b_{name}_mx`, or keep a
   registry? Quantify RCU per change
2. **Controller discovery across namespaces.** A wide-column registry
   (`{ns}/SYSTEM#`, `#ADAPTIVE_RESOURCES`, like `#ENTITY_CONFIG_RESOURCES` #288), iterated over
   the namespace registry
3. **Aggregator state read caching.** Reading the state record for every (resource, limit) in
   every batch, adaptive or not, is wasteful; an in-memory negative cache for ~1 tick
4. **Partial fan-out.** Reuse #487's `FanoutIncomplete` contract; writes are idempotent, so the
   next tick reconciles
5. **API / CLI / manifest surface.** `set_adaptive_policy(resource, limit, …)` naming, `limits`
   YAML shape, `Custom::ZaeLimiterLimits` round trip, `resource adaptive` CLI — run
   `api-cli-parity`
6. **Provisioner mirror.** `bucket_sync.py` must stamp `b_{name}_mx` like `cp/ra/rp`

## Known limitations

- Scale-up after idle lags by ~1 batch + 1 tick
- Three-level cascade double-counts middle-entity shards N>0 (§9a)
- `retry_after_seconds` does not anticipate factor changes (§10)
- `release()` / `adjust()` credits can push `tk` above the effective ceiling until the next
  refill — already true today and under #222 (§2.1 there)
- Idle buckets may hold a stale `af` until their next slow-path pass (§7)
- Un-upgraded clients ignore `af`/`vu` until an upgraded writer or the aggregator
  re-materializes — the same graceful degradation as #222 §2.1

## Testing sketch

| Level | What |
|-------|------|
| Unit | Controller step function: band, AIMD, caps, blind-never-loosens; effective params with `af` × shard divide; write-time validation matrix (§8); cascade counting filter |
| Unit (property) | Controller against a synthetic elastic-demand model converges into the band without oscillation — the `design-validator` scenarios |
| Integration (LocalStack) | Fan-out writes `af` + `vu = 0`; decrease forces slow path and clamps; creator stamping; aggregator repair under a stale config cache |
| E2E | Load pattern night → surge → idle; stalled aggregator (disable event source mapping) never loosens |

## Related

#223 (this), #222 (scheduled limits; exclusion rule, `vu`, clamp, effective params), #224
(webhooks — compatible, write the base), #225 (ramp-up — excluded with #223 by §8), #179
(counters, not token deltas), #298 (config BatchGet), #315 / #317 (fast path, aggregator
refill), #468 / #487 (fan-out), #474 (cascade shard stamping), ADR-111, ADR-116, ADR-125,
ADR-133, ADR-134
