# ADR-115: ADD-Based Writes with Lazy Refill

**Status:** Proposed
**Date:** 2026-01-28
**Issue:** [#248](https://github.com/zeroae/zae-limiter/issues/248)
**Depends on:** [ADR-114](114-composite-bucket-items.md)

## Context

The current write pattern uses PutItem (full item replacement) inside
TransactWriteItems. Under concurrent writes, the last writer overwrites previous
writers' consumption and the `total_consumed_milli` counter — a lost update bug.
Token balance and aggregator accuracy both degrade under contention.

Refill is currently computed and stored in the token balance on each write. When
two writers read the same state and both apply refill, the winning PutItem
correctly reflects one refill window, but the losing writer's consumption is lost
entirely. Separating refill from consumption in the write path would allow atomic
consumption tracking independent of refill timing.

The composite bucket item (ADR-114) provides a single item per entity+resource,
enabling a shared refill timestamp that doubles as an optimistic lock for all
limits simultaneously.

## Decision

Writers must use DynamoDB ADD to atomically decrement token balances and increment
consumption counters. Refill must not be stored in `tk`; instead, effective tokens
must be computed at read time as `min(stored_tk + elapsed * rate, burst)`. A single
shared `rf` attribute must serve as both the refill baseline and the optimistic
lock. The repository must implement four write paths: Create (PutItem with
`attribute_not_exists`), Normal (ADD with refill+consumption, condition `rf =
:expected`), Retry (ADD consumption only, condition `tk >= :consumed` per limit),
and Adjust (unconditional ADD, may go negative).

## Consequences

**Positive:**
- No lost updates: concurrent consumptions are correctly counted via atomic ADD
- No double refill: single `rf` lock ensures only one writer claims each refill window
- Retry requires no re-read (1 WCU, consumption-only ADD)
- Negative tokens prevented on acquire (condition `tk >= :consumed`); allowed on adjust by design
- Aggregator sees correct consumption counters regardless of contention
- Lock condition is always `rf = :expected` regardless of limit count

**Negative:**
- Four write paths increase repository complexity
- Retry path may reject requests that were initially approved on stale data
- Under high contention (~500ms window), refill is slightly under-counted (limiter becomes more restrictive, not less)
- Lease adjust becomes the only path that can push tokens negative, changing the current invariant

## Alternatives Considered

### PutItem with optimistic locking (version counter)
Rejected because: full item replacement still loses concurrent writers'
consumption — ADD is required for correct concurrent accounting.

### Per-limit refill timestamps
Rejected because: grows condition expression with limit count, risks partial
refill window claims, and adds an attribute per limit. Single `rf` is simpler
and free since all limits are updated on every acquire.

### ADD for consumption, no optimistic lock
Rejected because: without the `rf` lock, concurrent writers each compute and ADD
refill independently, causing double-refill proportional to contention.
