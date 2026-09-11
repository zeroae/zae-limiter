# ADR-133: Client-Side Shard Bucket Creation

**Status:** Proposed
**Date:** 2026-09-11
**Issue:** [#439](https://github.com/zeroae/zae-limiter/issues/439)

## Context

GHSA-76rv-2r9v-c5m6 mitigates DynamoDB hot partitions with pre-shard buckets: every
bucket carries a reserved `wcu` limit, and when a speculative write exhausts it the client
doubles `shard_count` on shard 0 and retries on another shard. That retry could never
succeed in a fresh deployment. The speculative `UpdateItem` requires `attribute_exists(PK)`,
the slow path's `batch_get_buckets` / `batch_get_entity_and_buckets` hardcoded shard 0, and
`_commit_initial()` created buckets on shard 0 only. The sole writer of a shard N>0 item was
the aggregator's stream-driven `propagate_shard_count()` (Path 2), whose own comment —
"Client already created this shard" — shows the original design expected the client to be
able to create shards. With `--no-aggregator`, or a lagging stream, the client bumped
`shard_count`, selected shard 1, got `BUCKET_MISSING`, and the slow path re-read and
re-wrote shard 0: every write stayed on the hot partition, and with `shard_count=2` about
half of all acquires paid a full slow-path fallback for nothing.

Two options were on the table. **Option A**: the client creates shard N>0 items itself on
the slow path. **Option B**: make the aggregator a hard requirement of write sharding and
document that `--no-aggregator` deployments are unsharded.

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

**Transient capacity.** Neither the client's `bump_shard_count()` nor the aggregator's Path 2
touches shard 0's own balance when `shard_count` doubles; the aggregator only stops refilling
it above its new effective ceiling. Creating shard 1 at `capacity/2` while shard 0 still holds
up to `capacity` therefore admits up to **1.5x** capacity for one refill window after the
first doubling, decaying as shard 0 drains. This ADR matches that existing behaviour rather
than introducing a reconciliation scheme; the client slow path still refills existing shards
toward the undivided `cp` (pre-existing, unchanged here).

**Race with the aggregator.** Client and aggregator both create under
`attribute_not_exists(PK)`, so exactly one succeeds. The client losing costs one extra
conditional write and never over-admits. The transaction still carries one item per
(entity, resource, shard), so the 100-item limit is unaffected.

**Cost (non-cascade, one user limit; RT = round trips):**

| Path | RT | RCU | WCU | Notes |
|------|----|-----|-----|-------|
| (a) Speculative hit on an existing shard | 1 | 0 | 1 | Unchanged steady state |
| (b) First acquire on a not-yet-created shard | 3–4 | ~1 (+disable walk) | 3 | 1 failed conditional + 2 transactional create; **once per shard** |
| (c) Previous broken fallback | 3–4 | ~1 (+disable walk) | 3 | Same per-call cost, paid on **every** acquire that drew a missing shard, all landing on shard 0 |

A wcu-driven doubling additionally pays 1 WCU for the `shard_count` bump, once.

## Consequences

**Positive:**
- Write sharding engages with or without the aggregator, and without stream lag.
- The `BUCKET_MISSING` fallback is now self-healing: one create, then fast-path hits.
- A shard-retry that finds a missing shard creates it instead of fast-rejecting the caller.

**Negative:**
- The slow path now carries a shard through read, `LeaseEntry` and write; `batch_get_*`
  keys grow to `(entity_id, resource, shard_id)`.
- `_sync_bucket_params()` still targets shard 0 only, so limit-parameter changes may
  reconcile only shard 0 (out of scope, tracked separately).
- Non-speculative clients (`speculative_writes=False`) never consume `wcu` and so never
  trigger doubling; they stay on shard 0.

## Alternatives Considered

### Option B — require the aggregator for write sharding
Rejected because: a security mitigation cannot depend on an optional component being
deployed and its stream being caught up.

### Create the new shard with full (undivided) tokens
Rejected because: it multiplies the entity's admitted capacity by `shard_count` and
diverges from the aggregator's Path 2, which the client must stay compatible with.

### Re-draw a random shard on the slow path
Rejected because: a second draw can land on shard 0 again, and a `BUCKET_MISSING` on shard N
would then never create shard N — the same failure with extra randomness.
