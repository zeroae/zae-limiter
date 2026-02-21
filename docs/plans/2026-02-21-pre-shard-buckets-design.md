# Pre-Shard Buckets Design

**Advisory:** [GHSA-76rv-2r9v-c5m6](https://github.com/zeroae/zae-limiter/security/advisories/GHSA-76rv-2r9v-c5m6)
**Date:** 2026-02-21
**Status:** Approved

## Problem

All rate limit buckets for a single entity share the same DynamoDB partition key (`{ns}/ENTITY#{id}`). A high-traffic entity can exceed DynamoDB's per-partition throughput limit (~1,000 WCU/sec), causing throttling that degrades service for that entity and potentially co-located entities.

With multiple resources per entity, the problem compounds: 10 resources at 100 writes/sec each = 1,000 WCU on one partition.

## Solution Overview

1. Move buckets to their own PK: `{ns}/BUCKET#{entity}#{resource}#{shard}` — one partition per (entity, resource, shard)
2. Auto-inject a reserved `wcu:1000` infrastructure limit on every bucket to track partition write pressure in-band
3. When `wcu` is exhausted, the client doubles the shard count; the aggregator can proactively shard or merge

**$0.625/M preserved on the hot path.**

## Data Model

### Bucket Item (New PK Scheme)

| Key | Value |
|-----|-------|
| PK | `{ns}/BUCKET#{entity_id}#{resource}#{shard_id}` |
| SK | `#STATE` |
| GSI2PK | `{ns}/RESOURCE#{resource}` |
| GSI2SK | `BUCKET#{entity_id}#{shard_id}` |
| GSI3PK | `{ns}/ENTITY#{entity_id}` |
| GSI3SK | `BUCKET#{resource}#{shard_id}` |
| GSI4PK | `{ns}` |
| GSI4SK | `BUCKET#{entity_id}#{resource}#{shard_id}` |

Shard 0 uses suffix `#0`. Unsharded buckets always have `shard_id=0`.

### Bucket Attributes

Existing attributes unchanged (`b_rps_tk`, `b_rps_cp`, etc.), plus:

| Attribute | Type | Description |
|-----------|------|-------------|
| `shard_count` | N | Number of shards (default 1) |
| `b_wcu_tk` | N | Current tokens for infrastructure limit |
| `b_wcu_cp` | N | Capacity: 1000 (auto-injected) |
| `b_wcu_ra` | N | Refill amount: 1000 |
| `b_wcu_rp` | N | Refill period: 1 (per second) |

`wcu` is a reserved limit name. Users cannot create limits named `wcu`; rejected with `ValidationError`. The `wcu` limit is hidden from all user-facing output (`RateLimitExceeded`, `get_status()`, usage snapshots).

### Limit Types

| Type | Examples | Divided on shard? | Visible to user? |
|------|----------|-------------------|-----------------|
| Application | `rps`, `tpm` | Yes (`original / shard_count`) | Yes |
| Infrastructure | `wcu` | No (per-partition) | No |

Bucket items store **original (undivided)** limits. Effective limits are derived at runtime by the refill logic (aggregator and client slow path).

### GSI Usage

| GSI | Projection | Bucket use |
|-----|-----------|-----------|
| GSI1 | ALL | **Not used** for buckets (preserves $0.625/M) |
| GSI2 | ALL | Resource capacity aggregation (existing, updated GSI2SK to include shard) |
| GSI3 | KEYS_ONLY | **New:** Bucket discovery (`GSI3PK={ns}/ENTITY#{id}`) |
| GSI4 | KEYS_ONLY | Namespace discovery (existing, updated GSI4SK) |

GSI3 is overloaded: `ENTITY_CONFIG#` prefix for entity configs (existing), `ENTITY#` prefix for bucket discovery (new). No collision.

## Access Patterns

| Pattern | Method | Key |
|---------|--------|-----|
| Single bucket read/write | Table GetItem/UpdateItem | `PK={ns}/BUCKET#{id}#{resource}#{shard}, SK=#STATE` |
| All buckets for entity | GSI3 Query + BatchGetItem | `GSI3PK={ns}/ENTITY#{id}, GSI3SK begins_with BUCKET#` |
| All shards for (entity, resource) | GSI3 Query | `GSI3PK={ns}/ENTITY#{id}, GSI3SK begins_with BUCKET#{resource}#` |
| Resource capacity (all entities) | GSI2 Query | `GSI2PK={ns}/RESOURCE#{resource}, GSI2SK begins_with BUCKET#` |
| All items in namespace | GSI4 Query | `GSI4PK={ns}` |

## Acquire Flow (Hot Path)

1. `acquire(entity='A', resource='r')`
2. Entity cache lookup: `(cascade, parent_id, {resource: shard_count})`
3. Pick `shard_id = random(0, shard_count - 1)`
4. Compute PK: `{ns}/BUCKET#A#r#{shard_id}`
5. `speculative_consume()` with `ADD b_wcu_tk -1` in UpdateExpression and `b_wcu_tk >= 1` in ConditionExpression
6. `ALL_NEW` returns `shard_count` — update entity cache

**No extra round trips. Same structure as today.**

### Failure Handling

| Cause | ALL_OLD signal | Action |
|-------|---------------|--------|
| `wcu` exhausted (`b_wcu_tk < 1`) | Infrastructure limit hit | Trigger doubling (bump `shard_count` on shard 0) |
| App limit exhausted (`b_rps_tk < consumed`, `b_wcu_tk >= 1`) | Shard drained | Retry on another random shard (max 2 retries) |
| Both exhausted | Partition hot + shard drained | Trigger doubling (priority) |
| Bucket missing | First write for this shard | Fall back to slow path (create bucket) |
| `ProvisionedThroughputExceededException` | DynamoDB throttle | Retry on `#s1`; if exists, discover `shard_count`; if not, `RateLimiterUnavailable` |

### Shard Retry on False Rejection

With N shards, one shard may be drained while others have capacity. On application limit exhaustion (not `wcu`), retry on a different random shard (max 2 retries, 3 total attempts).

Retries hit **different partitions** — no extra pressure on the exhausted shard.

## Shard Doubling

Shard count doubles: 1 → 2 → 4 → 8. Since shards are consumed uniformly, if one shard's `wcu` is exhausted, all shards are likely near exhaustion.

### Client-Side (Reactive)

On `wcu` exhaustion:
1. Bump `shard_count` on shard 0 (conditional write: `shard_count = :old_count`)
2. Retry acquire on a new shard (lazy creation on first access)
3. Cost: +1 RT, +1 WCU (one-time per doubling event)

### Aggregator (Proactive)

The aggregator observes `wcu` consumption via DynamoDB streams:
1. At threshold (e.g., 80% of capacity), bump `shard_count` on shard 0
2. Propagate `shard_count` to all existing shard items (+N WCU)
3. Optionally pre-create new shard bucket items to avoid lazy-creation slow path
4. Can merge shards when traffic drops (future work)

### Shard 0 as Source of Truth

Shard 0 (original PK suffix `#0`) is the authority for `shard_count`.

- **Warm cache clients:** route to random shards using cached `shard_count`
- **Stale cache clients:** route to fewer shards; self-correcting via own `wcu` exhaustion or cache TTL
- **Cold cache clients:** first acquire hits shard 0, learns `shard_count` from `ALL_NEW`
- **Shard 0 throttled (DynamoDB):** probe `#s1` via retry logic; if it exists, discover `shard_count`

| Actor | Extra RT | Extra RCU | Extra WCU |
|-------|---------|-----------|-----------|
| Triggering client | +1 | 0 | +1 (update shard 0) |
| Same client (cached) | 0 | 0 | 0 |
| Other client (stale cache) | 0 | 0 | 0 |
| Other client (cold cache) | 0 | 0 | 0 |
| Aggregator propagation | 0 | 0 | +N shards |

## Cascade

Parent is unaware of child shards. Each cascade write goes to the parent's own bucket PK (different partition). Parent protects itself with its own `wcu` limit and can shard independently.

The parallel cascade optimization (issue #318) still works: child + parent speculative writes fire concurrently via `asyncio.gather`, hitting different partitions.

## Entity Cache

Updated structure:

```
entity_cache = {
    entity_id: (cascade, parent_id, {resource: shard_count})
}
```

Populated from `ALL_NEW` response on speculative write success. `shard_count` per resource enables independent sharding granularity.

## Aggregator Changes

### Breaks (Must Fix)

| Component | Issue | Fix |
|-----------|-------|-----|
| `_parse_bucket_record()` | SK check fails (`#STATE` not `#BUCKET#`), resource/entity extraction from wrong key | Parse new PK format to extract entity_id, resource, shard_id |
| `aggregate_bucket_states()` | Merges shard states together (keys by entity+resource) | Key must include `shard_id`: `(ns, entity, resource, shard)` |
| `try_refill_bucket()` | Wrong PK/SK construction, missing effective limit computation | Use new PK, divide capacity/refill by `shard_count` |

### New Responsibilities

| Responsibility | Description |
|----------------|-------------|
| Proactive sharding | Detect high `wcu` consumption, bump `shard_count` before exhaustion |
| Shard count propagation | On `shard_count` change, update all shard items |
| Effective limit refill | `effective_capacity = capacity / shard_count` for application limits |
| `wcu` filtering | Filter `wcu` from `extract_deltas()` (no usage snapshots for internal limits) |

### New Dataclass Fields

| Dataclass | New fields |
|-----------|-----------|
| `ParsedBucketRecord` | `shard_id: int`, `shard_count: int` |
| `BucketRefillState` | `shard_id: int`, `shard_count: int` |

## Migration Strategy

**Clean break** — bump schema version in the VERSION record. New code creates bucket items at new PKs on first access. Old bucket items are ignored and expire via TTL.

Since buckets are ephemeral (they refill from limits config), losing old bucket state means entities briefly start with full tokens after upgrade. No data loss.

## Cost and Latency Impact

### Steady-State (Unsharded)

| Operation | RT | RCU | WCU | Change |
|-----------|-----|-----|-----|--------|
| Speculative acquire (success) | 1 | 0 | 1 | None |
| Speculative acquire (fast reject) | 1 | 0 | 0 | None |
| Speculative fallback (refill helps) | 3 | 1 | 2 | None |
| Cascade (both speculative success) | 1 | 0 | 2 | None |

### Sharded Steady-State (N shards)

| Operation | RT | RCU | WCU |
|-----------|-----|-----|-----|
| First shard has tokens | 1 | 0 | 1 |
| Shard retry (first drained) | 2 | 0 | 2 |
| Shard retry (2 drained) | 3 | 0 | 3 |

### Doubling Events (Rare)

| Action | RT | RCU | WCU |
|--------|-----|-----|-----|
| Client bumps shard_count | +1 | 0 | +1 |
| Client retries on new shard | +1 | 0 | +1 |
| Aggregator propagation | — | 0 | +N |

### Net Impact (Users Who Never Hit Partition Limits)

**Zero extra cost.** The `wcu` limit adds attributes to the same bucket item and conditions to the same `UpdateItem`. GSI3 KEYS_ONLY replication is ~0.1 WCU (negligible).

## Limitations

1. **Shard count only doubles** — no fine-grained scaling
2. **No shard merging in v1** — follow-up work for the aggregator
3. **Stale shard_count across clients** — self-correcting via `wcu` exhaustion or cache TTL
4. **get_status() for sharded entities** — GSI3 query + BatchGetItem (more expensive than single table query, but off hot path)
5. **Breaking schema change** — requires schema version bump

## Future Work

| Feature | Description |
|---------|-------------|
| Shard merging | Aggregator consolidates shards when traffic drops |
| Adaptive `wcu` ceiling | Adjust based on observed DynamoDB adaptive capacity |
| Shard-aware CloudWatch metrics | Per-shard operational visibility |
| RCU sharding | Same pattern for read-heavy workloads (3,000 RCU ceiling) |
