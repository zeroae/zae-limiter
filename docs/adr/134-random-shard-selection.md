# ADR-134: Random Shard Selection

**Status:** Proposed
**Date:** 2026-09-12
**Issue:** [#439](https://github.com/zeroae/zae-limiter/issues/439)

## Context

GHSA-76rv-2r9v-c5m6 mitigates per-entity DynamoDB hot-partition throttling with pre-shard
buckets: one entity's bucket writes are spread across `shard_count` partition keys of the form
`{ns}/BUCKET#{entity}#{resource}#{shard}`. The threat model is a **single** hot entity —
canonically a cascade parent whose 1000+ children each write to it on every acquire
([#116](https://github.com/zeroae/zae-limiter/issues/116)).

GHSA-w6c2-33wf-qfwf, the advisory closed by #439, describes the fix as selecting the slow-path
shard by "a shard computed from the entity/resource hash". The implementation does not do that:
it draws a shard at random on every call. That divergence has so far lived as an implementation
note rather than a recorded decision, so a future reader following the advisory literally would
reintroduce the bug it was meant to close.

A hash cannot serve this threat model. The entity id is already part of the partition key, so
distinct entities land on distinct partitions with no sharding at all; the only concentration
sharding has to fix is the one *within* a single entity. A hash of (entity, resource) is
constant for that entity's lifetime, so 100% of the hot parent's writes would still target one
shard and `shard_count > 1` would buy nothing in exactly the case the mechanism exists for.

## Decision

Shard selection is **random per call** — `random.randrange(shard_count)` in
`Repository.select_shard()`, the single place a shard is drawn — and is never derived from the
entity id, the resource, or any other stable key. Because selection is therefore not
reproducible, the slow path must be **told** which shard the fast path chose rather than
re-deriving it.

## Consequences

**Positive:**

- One hot entity's writes distribute across all of its shards, which is the property the
  mitigation requires. Cost is unchanged: 1 WCU either way, and no extra round trip.
- Satisfies GHSA-w6c2's actual requirement — slow-path reads and writes land on the same shard
  as the fast-path attempt — while declining its suggested mechanism. #439 threads the chosen
  shard through `_fetch_entity_and_buckets`, the `batch_get_*` keys, and `LeaseEntry` into
  `_commit_initial()`, so a miss on shard N reads and creates shard N.

**Negative:**

- Which shard holds which portion of an entity's budget is unpredictable. Per-shard effective
  limits are therefore `capacity_milli // shard_count` and `refill_amount_milli // shard_count`,
  so the shares sum to the configured limit whichever shard a caller draws.
- A single request larger than one shard's share is unadmittable on every shard — recorded as
  the known limitation in ADR-133 and tracked for observability in
  [#475](https://github.com/zeroae/zae-limiter/issues/475).
- A test that needs a specific shard must pass an explicit `shard_id`; assuming a given
  `acquire()` lands on a particular shard is flaky by construction.

## Alternatives Considered

### Hash of (entity, resource)

Rejected because: it is constant for an entity's lifetime, so the hot entity's writes stay on
one shard and sharding buys nothing for the only case it exists to fix.

### Hash including a time bucket

Rejected because: it spreads one entity's writes over time but still serializes all of its
concurrent writes onto a single shard within each window, which is where throttling occurs.

### Round-robin per client

Rejected because: there is no cross-process coordination, so independent clients degenerate to
random selection while each carries extra state.

## Related

- GHSA-76rv-2r9v-c5m6 (pre-shard buckets); GHSA-w6c2-33wf-qfwf (slow-path shard reuse)
- [ADR-133](133-client-shard-creation.md) — client-side shard creation, the mechanism that
  carries the selected shard through the slow path
- [#116](https://github.com/zeroae/zae-limiter/issues/116) — hot-partition risk with cascade
- [#475](https://github.com/zeroae/zae-limiter/issues/475) — observability for the per-shard
  request ceiling
