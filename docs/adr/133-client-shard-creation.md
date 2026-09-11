# ADR-133: Client-Side Shard Bucket Creation

**Status:** Proposed
**Date:** 2026-09-11
**Issue:** [#439](https://github.com/zeroae/zae-limiter/issues/439)

## Context

GHSA-76rv-2r9v-c5m6 mitigates DynamoDB hot partitions with pre-shard buckets: every
bucket carries a reserved `wcu` limit, and when a speculative write exhausts it the client
doubles `shard_count` on shard 0 and retries on another shard. That retry could never
succeed in a fresh deployment: the speculative `UpdateItem` requires `attribute_exists(PK)`,
the slow path's `batch_get_*` reads hardcoded shard 0, and `_commit_initial()` created
buckets on shard 0 only. The sole writer of a shard N>0 item was the aggregator's
`propagate_shard_count()` (Path 2), whose own comment — "Client already created this
shard" — shows the original design expected the client to create shards. With
`--no-aggregator`, or a lagging stream, every `BUCKET_MISSING` re-read and re-wrote
shard 0: writes stayed on the hot partition and about half of all acquires paid a full
slow-path fallback for nothing.

**Option A**: the client creates shard N>0 items itself on the slow path. **Option B**: make
the aggregator a hard requirement and document `--no-aggregator` deployments as unsharded.

## Decision

The client slow path must read and, when missing, create the **same shard the speculative
attempt selected** (Option A). `Repository.select_shard()` is the single place a shard is
drawn; the slow path receives that shard rather than drawing again. A shard N>0 item is
created with **effective per-shard tokens** — `capacity_milli // shard_count` for
application limits, `wcu` undivided, stored `cp`/`ra` undivided — exactly as the
aggregator's Path 2 does, and stamped with the cached `shard_count`. The create keeps
`attribute_not_exists(PK)`; if the aggregator wins the race, the transaction's condition
failure routes to the existing consumption-only retry on that same shard, whose
`tk >= consumed` condition is the same admission gate the speculative write uses.

## Rationale

The `security` label and the advisory make this a mitigation that must work in **every**
deployment. `--no-aggregator` is a supported mode (it is what the test suite's shared
minimal stack runs), so Option B would leave a documented security control inert there.

**Capacity bound.** Every refiller — the aggregator's `try_refill_bucket()` and the client
slow path, which carries `shard_count` on `BucketState` and refills toward
`capacity_milli // shard_count` — caps each shard at its effective share, so an entity with
N shards admits at most `capacity` per refill window in steady state, never `N x capacity`.
Neither `bump_shard_count()` nor Path 2 touches shard 0's balance when `shard_count`
doubles, so shard 0 may still hold up to `capacity` while shard 1 starts at `capacity/2`:
a one-time transient of up to **1.5x** after the first doubling, decaying as shard 0 drains
and never replenished above its new share. This ADR matches that behaviour rather than
introducing a reconciliation scheme.

**Race with the aggregator.** Client and aggregator both create under
`attribute_not_exists(PK)`, so exactly one succeeds. The client losing costs one extra
conditional write and never over-admits. The transaction still carries one item per
(entity, resource, shard), so the 100-item limit is unaffected; a transaction cancelled by a
*sibling* item's condition re-issues an innocent new-shard Put from its per-index reason.

**Cost (non-cascade, one user limit, warm config cache; RT = round trips):**

| Path | RT | RCU | WCU | Notes |
|------|----|-----|-----|-------|
| (a) Speculative hit on an existing shard | 1 | 0 | 1 | Unchanged steady state |
| (b) First acquire on a not-yet-created shard | 4 | 2.5 | 2 | Failed conditional (1 WCU), disable walk (3-key BatchGet, 1.5 RCU), META + bucket BatchGet (1 RCU), single-item `PutItem` (1 WCU); **once per shard** |
| (b') …after a wcu-driven doubling | 5 | 2.5 | 3 | (b) plus the `shard_count` bump, once per doubling |
| (c) Previous broken fallback | 4 | 2.5 | 2 | Same per-call cost as (b), paid on **every** acquire that drew a missing shard, every write landing on shard 0 |

One-item transactions are downgraded to `PutItem` (1 WCU); a cold config cache adds ~1.5 RCU.

## Consequences

**Positive:**
- Write sharding engages with or without the aggregator, and without stream lag.
- The `BUCKET_MISSING` fallback is now self-healing: one create, then fast-path hits.
- A shard-retry that finds a missing shard creates it instead of fast-rejecting the caller.

**Negative:**
- The slow path now carries a shard and its count through read, `LeaseEntry` and write;
  `batch_get_*` keys grow to `(entity_id, resource, shard_id)`.
- A cascading child never takes the child-only shard retry (it would bypass the parent);
  it pays the slow path instead — a rare-path extra round trip for correctness.
- Non-speculative clients (`speculative_writes=False`) never consume `wcu` and so never
  trigger doubling; they stay on shard 0.

## Related (tracked separately)

- `_sync_bucket_params()` reconciles shard 0 only; the parallel cascade fast path always
  writes the parent on shard 0.

## Alternatives Considered

### Option B — require the aggregator for write sharding
Rejected because: a security mitigation cannot depend on an optional component being
deployed and its stream being caught up.

### Create the new shard with full (undivided) tokens
Rejected because: it multiplies admitted capacity by `shard_count` and diverges from Path 2.

### Re-draw a random shard on the slow path
Rejected because: a second draw can land on shard 0 again, and a `BUCKET_MISSING` on shard N
would then never create shard N — the same failure with extra randomness.
