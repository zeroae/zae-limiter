# Client-side rejection cache: design

**Date:** 2026-10-06
**Issue:** [#695](https://github.com/zeroae/zae-limiter/issues/695)
**Decision record:** [ADR-147](../adr/147-client-side-rejection-cache.md)
**Related:** [ADR-133](../adr/133-client-shard-creation.md), [ADR-134](../adr/134-random-shard-selection.md), [ADR-146](../adr/146-per-resource-cascade-policy.md), [#315](https://github.com/zeroae/zae-limiter/issues/315), [#674](https://github.com/zeroae/zae-limiter/issues/674), [#675](https://github.com/zeroae/zae-limiter/issues/675)

## Context

Since #315 the documentation has said that a speculative fast rejection costs
**0 RCU + 0 WCU**. It does not. A conditional write whose condition is false still
consumes write capacity:

> "If a `ConditionExpression` evaluates to false during a conditional write, DynamoDB
> still consumes write capacity from the table. The amount consumed is dependent on the
> size of the existing item (or a minimum of 1)."
> — [Working with items: capacity units consumed by conditional writes](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/WorkingWithItems.html#WorkingWithItems.ConditionalWrites.ReturnConsumedCapacity);
> restated in [Capacity unit consumption for write operations](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/read-write-operations.html#write-operation-consumption)

Measured on real DynamoDB on 2026-10-06 (on-demand table, us-east-1, CloudWatch
`ConsumedWriteCapacityUnits`, Sum per minute):

| Phase | Requests | WCU consumed |
|-------|----------|--------------|
| Condition fails, item exists | 1000 failed | 1000 |
| Control, condition passes | 1000 succeeded | 1000 |
| Condition fails, item missing | 1000 failed | 1000 |

The "free" claim most likely conflated two statements: the `UpdateItem` API reference
says requesting `ReturnValuesOnConditionCheckFailure` has "no additional cost" and
consumes "no read capacity units". That is about the **returned image** (which is why a
fast rejection is genuinely **0 RCU**), not about the failed write. #315's own
alternatives section says "speculative failures cost 1 WCU"; its cost table says 0.

Where the limiter spends write capacity without admitting anything:

| Path | WCU |
|------|-----|
| Fast rejection (429) | 1 per rejected request |
| Cascade rejection, parent exhausted | 3: child write, failed parent write, compensation |
| "Refill would help" fallback | 1 failed + 1 RCU + 1 write |
| Shard retry probes of other shards (`_MAX_SHARD_RETRIES = 2`) | up to 2 failed |

A rejected write also consumes the partition's physical ~1,000 WCU/s but never debits
the reserved `wcu` limit, which is only `ADD`ed on success. A client looping on 429s can
therefore throttle a partition without ever triggering write sharding (GHSA-76rv).

Every speculative response already returns the whole bucket state at no read cost:
`SpeculativeResult.buckets` (`ALL_NEW`) on success and `SpeculativeResult.old_buckets`
(`ALL_OLD`) on failure. The client discards it after one decision.

## Decision

The six decisions below were agreed with the owner on 2026-10-06.

1. **Cache the last bucket state seen**, per (namespace, entity, resource, shard), from
   every real response: speculative success (`ALL_NEW`), speculative failure
   (`ALL_OLD`), and the slow path's read and write. Not a bare "exhausted until T"
   flag: a state can be projected forward, which is what makes the cascade pre-check
   and the refill-from-image write (below) possible.

2. **Reject locally only when the projection falls short.** Before the speculative
   write, project the cached state to `now` with the same arithmetic the fast path's
   `would_refill_satisfy` uses (drip, schedules, quota resets, session windows). If a
   limit **declared in `consume`** still cannot cover the request, raise
   `RateLimitExceeded` built from that projection — statuses and
   `retry_after_seconds` as a real fast rejection would report them — with no DynamoDB
   call. Never reject locally when:
   - the cached `vu` has passed (a schedule boundary, or a `set_limits` fan-out's
     `vu = 0`: only the slow path knows the new parameters);
   - only `wcu` falls short (that is a sharding signal, not a rejection);
   - `acquire(limits=...)` passes an override that differs from the cached parameters;
   - the limiter is degraded or unavailable.

   **The cache may only reject, never admit.** Admission still requires a successful
   conditional write, so the cache cannot over-admit. Its only error is a bounded
   under-admission when tokens return by a route the projection cannot see.

3. **Trust an entry for at most `rejection_cache_ttl` seconds**, default **1.0**, set on
   `open()` / `connect()` / `builder()`; `0` disables the cache. Time-based refill is
   inside the projection, so the age cap only bounds what it cannot see: another
   process's refund or `release()`, an admin raising a limit or resetting a quota, an
   entity being re-enabled. Any real response refreshes the entry.

4. **Draw shards around known-short ones.** Each shard holds its own share, so an entry
   speaks for one shard only. Shard selection draws uniformly among shards **not**
   known short (ADR-134's randomness, minus known-dry shards); only when every shard is
   known short does the acquire reject locally, reporting the shard that fits soonest.
   A shard with no entry is unknown and is written to, so a stale count in the **entity
   cache** never hides a new shard. A doubling made by **another process** after every
   cached state was taken can: this process may reject locally for up to
   `rejection_cache_ttl` before its next real write learns the new count. `wcu`
   doubling is decided only on real responses.

5. **Invalidation.**

   | Event | Action |
   |-------|--------|
   | This process credits the bucket (`release()`, negative `adjust()`, rollback, cascade compensation) | Drop that entry |
   | Any real response for the bucket | Replace the entry |
   | An admin write through this repository (`set_limits`, `set_resource_defaults`, `set_system_defaults`, every `delete_*`, disable/enable, cascade policy) or `invalidate_config_cache()` | Clear the whole cache |
   | Cached state stamped `disabled` | Never reject from it; `ResourceDisabled` stays the server's answer |
   | Another process's change | Not visible; bounded by decision 3 |

6. **Ownership, memory, sync.** The cache lives on `Repository` beside `_entity_cache`
   and `_cascade_cache`, keyed with the namespace and shared across `namespace()`
   scopes. The limiter reaches it through `getattr`, so a third-party backend without
   it never rejects locally. At most `rejection_cache_size` entries (default 10,000),
   oldest first out. The sync twin is generated as usual and takes no lock: a plain
   dict whose reads and removals tolerate a missing key, so a race under the sync
   thread pool can only lose an entry — one extra DynamoDB call, never a wrong admit.
   `get_cache_stats()` counts local rejections.

## Phase 2: the cascade parent pre-check

Agreed with the owner on 2026-10-06. A cascade rejection costs 3 WCU today (child write,
failed parent write, child compensation) and reports the child's statuses followed by the
parent's. The parent's state is already cached under the parent's own key, because the
parallel parent write goes through `_speculative_consume_single`; the pre-check adds no
storage.

1. **When it runs.** Only when this child's **own trusted** cached state shows that it
   cascades on this resource: its stamp (`cascade` with a `parent_id`), from an entry that
   passes the same age, `vu` and TTL test as every other read, and no cached shard of the
   child stamped `disabled` (its answer is the server's 403). Never the entity-wide guess
   in `_entity_cache`, which can be another resource's policy (ADR-146), and never
   `_cascade_cache`, which is never expired or cleared: a policy changed elsewhere would
   then be believed with no bound, and a local rejection writes nothing, so nothing would
   relearn it (phase-2 review). So a rejection always has the child's statuses too.
2. **Parent rule.** The phase-1 rule on the parent's own shards: every parent shard
   known short for a limit in `consume` ⇒ `RateLimitExceeded` with no DynamoDB call. The
   parent's shard count is the larger of its entity-cache count and the counts its cached
   states carry. The parent's own `cascades` flag is ignored (a child's lease never
   reaches the grandparent, #686). A disabled parent state is unknown, so a disabled
   parent still reaches DynamoDB and its 403. The rejection reports the child's statuses
   (projected from its cached state, when there is one) followed by the parent's, as the
   server does; `retry_after_seconds` is the largest.
3. **Cascading child known short.** Phase 1 never rejects it locally, because a disabled
   parent's 403 outranks the child's 429. It may now, reporting the child's statuses as the
   server does, **only when the parent is known not to be disabled**: at least one of the
   parent's cached states for this resource is trusted and not stamped `disabled`, **and
   no** cached parent shard of any age is stamped `disabled` — a disable fan-out that
   stopped part-way, or ran in another process, leaves shards the server would answer with
   403 (phase-2 review). A disable made through this process clears the cache. Each cached
   state records its item's `parent_id` beside `cascades`.
4. **Parent shard steering.** The parallel write draws the parent's shard among those not
   known short (`select_shard(..., avoid=...)`), as phase 1 does for the child. `wcu`
   doubling stays on real responses; an uncached shard stays drawable.
5. **Cold cache.** Unchanged. The first cascade acquire per (process, entity) writes the
   child before it learns the parent, and decision 1 needs that child's own stamp anyway.
6. **Delivery.** A second PR stacked on #696, with its own fresh-context review.

## Phase 3: refill from the cached state

Agreed with the owner on 2026-10-06. When a bucket is low but refill since its last
materialisation covers the request, today's path pays a failed speculative write
(1 WCU), the slow path's read (1 RCU) and its locked write: **$1.375/M**. With a trusted
cached state the read is redundant — the state is what the read would return, unless
something wrote since, which the lock detects.

1. **When.** Only from a **trusted** cached state (decision 3's age cap, before `vu` and
   the bucket TTL, not disabled), and only when its **stored** balance cannot cover the
   request (the speculative write would fail) but its projection to now can. Every
   other case is unchanged: enough stored ⇒ the speculative write; short after refill ⇒
   phases 1–2.
2. **The write.** The slow path's rf-locked write (`build_composite_normal`), built from
   the cached state and the same refill arithmetic, conditioned on **everything the
   refill was computed from**, because other writers change each of these without
   moving `rf` (phase-3 review, all four reproduced as over-admission):
   - `rf = :cached_rf` (the lock itself);
   - `vu` absent — a limit change made elsewhere (`_sync_bucket_params`, the
     provisioner) rewrites `cp`/`ra`/`rp` and stamps `vu = 0`; the aggregator pins `vu`
     for the same reason (#508). An item that carries `vu` is not used at all;
   - each limit's `tk <=` its cached value — a refund, release, rollback or
     compensation elsewhere `ADD`s tokens, and the clamp computed against the lower
     cached balance would land above the ceiling;
   - `shard_count` equal to the cached one — a doubling elsewhere shrinks the
     per-shard ceiling and rate;
   - `cascade` absent or false — a policy turned on elsewhere stamps it (ADR-146);
   - not `disabled`, and the TTL not expired;
   - and `build_composite_normal`'s own floor `tk >= consumed − refill`, which is what
     refuses a debit made elsewhere (it lowers `tk` without moving `rf`). It predates
     phase 3, but it is a pin all the same and must not be relaxed.

   It also uses phase 3 only when the stamp names a parent or the entity's **existing**
   META record says it has none (a pre-#684 stamp can sit on a cascading child; an
   entity with no record yet may be created later under a parent — verification
   finding A); only within one config-cache window (`config_cache_ttl`, 60 s by
   default) of the bucket's last real slow pass, which re-reads `disabled` and the
   cascade policy from config (a stamp a fan-out missed is then re-checked as often as
   config itself — verification finding B); and only when the **warm config cache**
   resolves the bucket's limits with no schedule, reset or window and every configured
   limit already on the item: the slow path attaches those from config, and a
   resource- or system-level change never reaches the item. That answer also gives the
   write the TTL the slow path would stamp.

   **Standing rule:** any new writer that changes a bucket item must change something
   this condition checks, or phase 3 can admit against a state it cannot see. See
   `.claude/rules/code-review.md`.
3. **Lost condition.** Fall back to today's path (speculative write, then the slow
   path). The worst case costs one extra write, and only after something wrote between
   the state being cached and now.
4. **Cascade.** Not used for a bucket whose trusted state cascades: child and parent
   must commit in one transaction, which stays on today's path.
5. **Quotas, windows, schedules, seeds.** Not used when any limit on the cached item is
   a quota (calendar or session), carries a schedule, or is missing from the item:
   resets, window rolls, ADR-145 grants and #633 seeds live only on the full slow path.
   Plain dripping limits only.
6. **Delivery.** A fourth PR, base `main`, merged after the phase-2 PR, with its own
   fresh-context review.

## Building on this (designed in, delivered separately under #695)

- **Multi-resource acquire (#675, ADR-148).** The N-record fast path consults the same
  cache per (entity, resource) before writing anything.

## Cost

| Case | Before | After |
|------|--------|-------|
| First rejection in a process (no entry) | 0 RCU + 1 WCU | unchanged |
| Repeat rejection inside the TTL | 0 RCU + 1 WCU | **0 RCU + 0 WCU, 0 round trips** |
| Rejection predicted from our own last success | 0 RCU + 1 WCU | **0 + 0** |
| Admit | 0 RCU + 1 WCU | unchanged |
| Client looping on 429s at 1,000 req/s, one process | ~1,000 WCU/s | **~1 WCU/s** (one real write per TTL) |

Memory: one `BucketState` per entry, a few hundred bytes; ~a few MB at the default cap.

## Consequences

**Positive**
- Repeat 429s stop costing write capacity and stop eating partition throughput.
- Known-dry shards are no longer probed, removing up to 2 failed writes per acquire.
- The documentation's cost model becomes true: a fast rejection is reported as
  1 WCU when it reaches DynamoDB, 0 when it does not.

**Negative**
- **Bounded under-admission**: up to `rejection_cache_ttl` per process after tokens
  return by a route the projection cannot see. Operators who need exact admission at
  the cost of a write per 429 set it to `0`.
- Rejections are per process: N processes each pay one real write per TTL.
- A local rejection's statuses come from a projected state, not a fresh image. They
  are what the server would report if nothing else wrote meanwhile.

## Alternatives considered

- **"Exhausted until T" flags.** Simpler, but carries no balance: cannot predict a
  rejection from a success, and cannot drive the refill-from-cached-state write.
- **No age cap** (trust the projection until it fits). Zero writes on a dead limit,
  but a missed refund under-admits for as long as a quota's period — hours or days.
- **A shortfall margin** before rejecting locally. Reduces under-admission but makes
  every local 429 inexact; the age cap already bounds the error.
- **Reject on one known-short shard.** Rejects while siblings may hold tokens — worse
  than today's probing.
- **A shared cross-process cache** (e.g. ElastiCache). New infrastructure and a network
  hop to save one; out of scope.
- **Read before writing** (`GetItem`, 0.5 RCU) to avoid the failed write. A 429 would
  cost 0.5 RCU instead of 1 WCU — cheaper, but every admit would pay the read too.
