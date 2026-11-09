# Architecture

This guide covers the internal architecture of zae-limiter, including the DynamoDB schema and token bucket implementation.

## DynamoDB Schema (Single Table)

All data is stored in a single DynamoDB table using a composite key pattern:

| Record Type | PK | SK |
|-------------|----|----|
| Entity metadata | `{ns}/ENTITY#{id}` | `#META` |
| Bucket (v0.9.0+) | `{ns}/BUCKET#{id}#{resource}#{shard}` | `#STATE` |
| Entity config | `{ns}/ENTITY#{id}` | `#CONFIG#{resource}` |
| Resource config | `{ns}/RESOURCE#{resource}` | `#CONFIG` |
| System config | `{ns}/SYSTEM#` | `#CONFIG` |
| Usage snapshot | `{ns}/ENTITY#{id}` | `#USAGE#{resource}#{window_key}` |
| System version | `{ns}/SYSTEM#` | `#VERSION` |
| Audit events | `{ns}/AUDIT#{entity_id}` | `#AUDIT#{timestamp}` |
| Namespace forward | `_/SYSTEM#` | `#NAMESPACE#{name}` |
| Namespace reverse | `_/SYSTEM#` | `#NSID#{id}` |

### Global Secondary Indexes

| Index | Purpose | Key Pattern |
|-------|---------|-------------|
| **GSI1** | Parent → Children lookup | `GSI1PK={ns}/PARENT#{id}` → `GSI1SK=CHILD#{id}` |
| **GSI2** | Resource aggregation | `GSI2PK={ns}/RESOURCE#{name}` → buckets/usage |
| **GSI3** | Entity config queries (sparse) + Bucket discovery by entity (KEYS_ONLY) | `GSI3PK={ns}/ENTITY_CONFIG#{resource}` → `GSI3SK=entity_id` or `GSI3PK={ns}/ENTITY#{id}` → `GSI3SK=BUCKET#{resource}#{shard}` |
| **GSI4** | Namespace item discovery (KEYS_ONLY) | `GSI4PK={ns}` → `GSI4SK=PK` |

### Access Patterns

| Pattern | Query |
|---------|-------|
| Get entity | `PK={ns}/ENTITY#{id}, SK=#META` |
| Get bucket (specific shard) | `PK={ns}/BUCKET#{id}#{resource}#{shard}, SK=#STATE` |
| Get buckets (all resources) | GSI3: `GSI3PK={ns}/ENTITY#{id}` then BatchGetItem on discovered PKs |
| Batch get buckets | `BatchGetItem` with multiple `PK={ns}/BUCKET#{id}#{resource}#0, SK=#STATE` pairs |
| Get children | GSI1: `GSI1PK={ns}/PARENT#{id}` |
| Resource capacity | GSI2: `GSI2PK={ns}/RESOURCE#{name}, SK begins_with BUCKET#` |
| Get version | `PK={ns}/SYSTEM#, SK=#VERSION` |
| Get audit events | `PK={ns}/AUDIT#{entity_id}, SK begins_with #AUDIT#` |
| Get usage snapshots | `PK={ns}/ENTITY#{id}, SK begins_with #USAGE#` |
| Get system config | `PK={ns}/SYSTEM#, SK=#CONFIG` |
| Get resource config | `PK={ns}/RESOURCE#{resource}, SK=#CONFIG` |
| Get entity config | `PK={ns}/ENTITY#{id}, SK=#CONFIG#{resource}` |
| List entities with custom limits | GSI3: `GSI3PK={ns}/ENTITY_CONFIG#{resource}` |
| Namespace forward lookup | `PK=_/SYSTEM#, SK=#NAMESPACE#{name}` |
| Namespace reverse lookup | `PK=_/SYSTEM#, SK=#NSID#{id}` |
| List all items in namespace | GSI4: `GSI4PK={ns}` |

### Namespace Isolation

All partition key values are prefixed with an opaque namespace ID (`{ns}/`), providing logical isolation between tenants within a single DynamoDB table. The reserved namespace `_` is used for the namespace registry itself (forward and reverse lookup records).

- **Namespace ID format**: 11-character opaque string generated via `secrets.token_urlsafe(8)`
- **Default namespace**: Automatically registered on first deploy (CLI) or `RepositoryBuilder.build()`
- **GSI4**: A KEYS_ONLY index on `GSI4PK=namespace_id` enables `purge_namespace()` to discover and delete all items belonging to a namespace

### Optimized Read Patterns

The `acquire()` operation uses `BatchGetItem` to fetch all required buckets in a
single DynamoDB round trip (see [Issue #133](https://github.com/zeroae/zae-limiter/issues/133)):

```{.python .lint-only}
# Before: N sequential GetItem calls
for entity_id, resource, limit_name in bucket_keys:
    bucket = await get_bucket(entity_id, resource, limit_name)

# After: 1 BatchGetItem call
buckets = await batch_get_buckets(bucket_keys)
```

This optimization is particularly beneficial for cascade scenarios where both
entity and parent buckets are fetched together, reducing latency from
2×N round trips to 1.

### Config Resolution (ADR-122)

Config resolution uses the 4-level hierarchy: **Entity (resource-specific) > Entity (`_default_`) > Resource > System**. This logic lives on `Repository.resolve_limits()`, not on `RateLimiter`. Each backend can use native resolution strategies (e.g., SQL `UNION ALL`, Redis Lua scripts). The DynamoDB implementation uses `BatchGetItem` for all 4 config keys in a single round trip, with `ConfigCache` as an internal caching layer (60s TTL by default).

```{.python .lint-only}
# Repository resolves limits internally (ADR-122)
limits, on_unavailable, config_source = await repo.resolve_limits(entity_id, resource)
# config_source: "entity", "entity_default", "resource", "system", or None
```

Cache management methods (`invalidate_config_cache()`, `get_cache_stats()`) are on `Repository`, not `RateLimiter`. The `config_cache_ttl` parameter is on the `Repository` constructor, beside `rejection_cache_ttl` and `rejection_cache_size` for the rejection cache ([ADR-147](../adr/147-client-side-rejection-cache.md)), and `stamp_ttl`, the longest a `bypass` or soft stamp stands before the slow path renews it from configuration ([ADR-151](../adr/151-non-enforcing-limits.md) decision 8).

### Item Structure

All records use **flat schema** (v0.6.0+, top-level attributes, no nested `data.M`).
See [ADR-111](../adr/111-flatten-all-records.md).

```{.python .lint-only}
# Entity record (FLAT structure):
{
    "PK": "{ns}/ENTITY#user-1",
    "SK": "#META",
    "entity_id": "user-1",
    "name": "User One",
    "parent_id": null,
    "metadata": {...}
}
```

**Bucket records** (composite, one item per entity+resource+shard, v0.9.0+):

```python
# Bucket record (FLAT structure, ADR-114, GHSA-76rv, ADR-154), v0.18.0+:
{
    "PK": "{ns}/BUCKET#user-1#gpt-4#0",  # per-(entity, resource, shard) PK
    "SK": "#STATE",
    "entity_id": "user-1",
    "resource": "gpt-4",
    "shard_count": 1,  # total shards for this entity+resource
    # tpm: 10,000/min, a dripping limit stored as a fill instant (ADR-154)
    "b_tpm_fi": 17040672030000000000,  # z_ms × ra: full again 3 s after 1704067200000
    "b_tpm_cp": 10000000,  # capacity_milli (undivided)
    "b_tpm_ra": 10000000,  # refill_amount_milli (undivided)
    "b_tpm_rp": 60000,  # refill_period_ms
    "b_tpm_tc": 500000,  # total_consumed_milli for tpm
    "b_tpm_rsched": "-",  # tpm has no reset: it must not inherit the item's (#541)
    # rpd: 1,000/day, a quota, which keeps tk (ADR-137, ADR-145)
    "b_rpd_tk": 999000,  # tokens_milli for the quota
    "b_rpd_cp": 1000000,
    "b_rpd_ra": 0,  # a quota does not drip
    "b_rpd_tc": 1000,
    "b_rpd_gc": 1,  # shard count its current grant was sized at
    "rsched": "1m0h0",  # item-level reset schedule ("0 0 * * *"), here rpd's
    # wcu: the reserved per-partition write budget, also a fill instant
    "b_wcu_fi": 1704067200001000000,  # z_ms × ra, undivided at any shard count
    "b_wcu_cp": 1000000,  # wcu capacity (1000 writes/sec)
    "b_wcu_tc": 1000,
    "xi": 1704067200000,  # instant the fill instants were last expressed
    "rf": 1704067200000,  # last refill / reset of the quota (not a fill-instant baseline)
    "sched_tz": "America/New_York",  # IANA zone shared by every schedule here
    "vu": 1704085200000,  # valid_until_ms: next boundary on this item (midnight local)
    "cascade": False,
    "GSI2PK": "{ns}/RESOURCE#gpt-4",
    "GSI2SK": "BUCKET#user-1#0",
    "GSI3PK": "{ns}/ENTITY#user-1",  # bucket discovery by entity
    "GSI3SK": "BUCKET#gpt-4#0",
    "ttl": 1234567890,
}
```

`sched` / `rsched` / `sched_tz` are the schedule denormalized onto the item (#222, ADR-135).
They are absent on an unscheduled bucket, and a limit whose schedule differs from the item
default is stamped with `b_{name}_sched` / `b_{name}_rsched`. On an item that carries a default,
a limit with **no** schedule is stamped with the reserved marker `-`
(`schema.BUCKET_SCHED_NONE`), because a missing override already means "inherit the default" and
cannot also mean "unscheduled" — that is what keeps a quota and a rate limit sharing one item
from acquiring each other's windows. `cp` / `ra` / `rp` stay the
**base** parameters: every reader applies the schedule on top of them at read time, and the
only materialised state is a quota's `tk` and a dripping limit's fill instant. `vu` is the
earliest instant at which any limit here changes effective parameters.

<a id="fill-instants"></a>**Fill instants (schema 0.11.0, ADR-154).** A dripping limit,
`wcu` included, stores `b_{name}_fi` in place of `b_{name}_tk`: its fill instant `z`, the
instant its share is back at capacity, scaled by its undivided refill amount in force
(`b_{name}_fi = z_ms × ra`) so every quantity is an exact integer. The balance at any instant
`t` is `C − r × max(0, z − t)`, exactly, where `C` and `r` are the shard's share of the
capacity and rate; every `z` before `t` reads as full, which is how a full bucket forgets idle
time (#808). The item gains one `xi` attribute, the instant its fill instants were last
expressed against their parameters and schedule. A quota keeps `tk`, `rf` and `gc`, so one item
can carry both forms, and `rf` is not a fill instant's baseline. An item written before v0.18.0
(dripping limits in `tk`) is converted on its next slow pass, in a write pinned on the `tk` and
`rf` it read; every bucket created stores the new form. A fill instant is read, debited and
written only under the schedule stored on its item, never under a schedule from config
(#841): read under another schedule's refill amount it reads as full or as a vast debt. A
schedule change reaches an existing item only through its fan-out, which re-expresses its fill
instants and rewrites the schedule in one write, as capacity and refill do (ADR-153).

The decisions are four ADRs: [ADR-153](../adr/153-admission-contract-for-dripping-limits.md)
(the admission bound), [ADR-154](../adr/154-fill-instant-fast-path.md) (storage and the fast
path's two writes), [ADR-155](../adr/155-fill-instant-writers.md) (every other writer is exact
and pinned on the parameters, shard count and `xi` it read) and
[ADR-156](../adr/156-dripping-shard-raises.md) (shard raises). The arithmetic and its proofs are
in [Fill-instant proofs](fill-instant-proofs.md); the pure functions are in
[`fill_instant.py`](https://github.com/zeroae/zae-limiter/blob/main/src/zae_limiter/fill_instant.py).

The `wcu` (write capacity unit) limit is a reserved infrastructure limit auto-injected on every bucket. It tracks per-partition write pressure and is hidden from user-facing output (get_buckets, RateLimitExceeded, usage snapshots). It is never divided by the shard count: each partition has its own budget. When it is exhausted on a bucket with no dripping limit (quotas only), the client doubles `shard_count` to spread writes across more DynamoDB partitions. A bucket holding a dripping limit is **raised** instead (ADR-156, `shard_raise.py`):

1. A **raise marker** `rx` (a deadline 2 s out, `schema.RAISE_MARKER_MS`) is set on every shard below the count. Every write that moves a fill instant, `xi` or the shard count refuses an item with a live marker (`schema.raise_guard`): an acquire answers contended, a debit, parameter sync, reset or top-up waits it out, and a return is dropped. A marker past its deadline is ignored. Each marker write's `ALL_NEW` is the raise's strongly consistent read.
2. One `TransactWriteItems` raises the count from `S` to `2S`, pinned on the markers and on everything it read, plus ADR-150's owner check. With `C` the undivided capacity and `b_i` shard `i`'s balance, it re-expresses each shard `i` at the new count keeping `min(b_i, C / 2S)`, creates shard `i + S` as a copy of its parent holding `max(0, b_i − C / 2S)` and a full `wcu`, and removes the markers. The total balance is conserved exactly ([proofs §8](fill-instant-proofs.md#8-other-writers-adr-155)).
3. A raise that finds another raise's live marker, a larger stored count, or a cancelled transaction removes its own markers and gives up.

A dripping shard the slow path creates while others of its bucket exist (a TTL swept it, or no raise created it) starts **empty**: creating it full would mint a share.

The `total_consumed_milli` counter tracks net consumption (increases on consume,
decreases on release) and is used by the aggregator Lambda to accurately calculate
consumption deltas. This counter is independent of token bucket refill, solving
the issue where `old_tokens - new_tokens` gives incorrect results when refill rate
exceeds consumption rate. See [Issue #179](https://github.com/zeroae/zae-limiter/issues/179).

**Usage snapshots use a FLAT structure** (no nested `data` map):

```python
# Usage snapshot (FLAT structure):
{
    "PK": "{ns}/ENTITY#user-1",
    "SK": "#USAGE#gpt-4#2024-01-01T14:00:00Z",
    "entity_id": "user-1",
    "resource": "gpt-4",  # Top-level attribute
    "window": "hourly",  # Top-level attribute
    "window_start": "...",  # Top-level attribute
    "tpm": 5000,  # Counter at top-level
    "total_events": 10,  # Counter at top-level
    "GSI2PK": "{ns}/RESOURCE#gpt-4",
    "ttl": 1234567890,
}
```

**Why snapshots are flat:** DynamoDB has a limitation where you cannot SET a map path
(`#data = if_not_exists(#data, :map)`) AND ADD to paths within it (`#data.counter`)
in the same UpdateExpression - it fails with "overlapping document paths" error.
Snapshots require atomic upsert (create-or-update) with ADD counters for usage
aggregation, so they use a flat structure to enable single-call atomic updates.

See: [Issue #168](https://github.com/zeroae/zae-limiter/issues/168)

**Config records use composite items** (v0.8.0+, ADR-114). All limits for a config level are stored in a single item:

```python
# Resource config (composite, FLAT structure):
{
    "PK": "{ns}/RESOURCE#gpt-4",  # or {ns}/SYSTEM# or {ns}/ENTITY#{id}
    "SK": "#CONFIG",  # or #CONFIG#{resource} for entity level
    "resource": "gpt-4",
    "l_tpm_cp": 100000,  # capacity for tpm limit
    "l_tpm_ra": 100000,  # refill_amount for tpm limit
    "l_tpm_rp": 60,  # refill_period_seconds for tpm limit
    "l_tpm_sched": "1h9-17w1-5s500",  # tpm's schedule, compact encoding (#222, #515)
    "l_rpd_rsched": "1m0h0",  # rpd's reset schedule ("0 0 * * *")
    "sched_tz": "America/New_York",  # one zone per item, hoisted out of the entries
    "config_version": 1,  # Atomic counter for cache invalidation
}
```

Config records use four-level precedence: **Entity (resource-specific) > Entity (_default_) > Resource > System > Constructor defaults**.

**Key builders:**

- `pk_system(namespace_id)` - Returns `{ns}/SYSTEM#`
- `pk_resource(namespace_id, resource)` - Returns `{ns}/RESOURCE#{resource}`
- `pk_entity(namespace_id, entity_id)` - Returns `{ns}/ENTITY#{entity_id}`
- `pk_bucket(namespace_id, entity_id, resource, shard_id)` - Returns `{ns}/BUCKET#{id}#{resource}#{shard}` (v0.9.0+)
- `sk_state()` - Returns `#STATE` (bucket state sort key)
- `sk_config()` - Returns `#CONFIG` (system/resource level)
- `sk_config(resource)` - Returns `#CONFIG#{resource}` (entity level)
- `sk_namespace(name)` - Returns `#NAMESPACE#{name}` (forward lookup)
- `sk_nsid(id)` - Returns `#NSID#{id}` (reverse lookup)
- `gsi3_pk_entity(namespace_id, entity_id)` - Returns `{ns}/ENTITY#{id}` (bucket discovery)
- `gsi3_sk_bucket(resource, shard_id)` - Returns `BUCKET#{resource}#{shard}` (bucket discovery)
- `parse_bucket_pk(pk)` - Parses `{ns}/BUCKET#{id}#{res}#{shard}` into components

**Audit entity IDs for config levels** (see [ADR-106](../adr/106-audit-entity-ids-for-config.md)):

- System config: Uses `$SYSTEM` as entity_id
- Resource config: Uses `$RESOURCE:{resource_name}` (e.g., `$RESOURCE:gpt-4`)

## Token Bucket Implementation

For a conceptual overview of the token bucket algorithm, see the [User Guide](../guide/token-bucket.md). This section covers implementation details for contributors.

### Core Functions

The algorithm is implemented in [`bucket.py`](https://github.com/zeroae/zae-limiter/blob/main/src/zae_limiter/bucket.py):

| Function | Purpose |
|----------|---------|
| `refill_bucket()` | Calculate refilled tokens with drift compensation |
| `try_consume()` | Atomic check-and-consume operation |
| `force_consume()` | Force consume (can go negative) |
| `calculate_retry_after()` | Wait time for a deficit at a single fixed rate — the reference definition the boundary walk is pinned against |
| `schedule.retry_after_with_schedule()` | Wait time across schedule boundaries and reset edges; what every `LimitStatus.retry_after_seconds` reports |
| `calculate_available()` | Calculate currently available tokens |
| `build_limit_status()` | Build a LimitStatus for a bucket check |
| `would_refill_satisfy()` | Check if refilling would allow a request to succeed (speculative writes) |

`bucket.py` reads a state stored either way: a quota's `tk` (and a dripping limit's on an item
written before v0.18.0) through `refill_bucket()`, a fill instant through the exact arithmetic
in [`fill_instant.py`](https://github.com/zeroae/zae-limiter/blob/main/src/zae_limiter/fill_instant.py)
below.

### Fill-Instant Arithmetic (ADR-154)

For one shard with share `C`, rate `r` and fill instant `z`, a request of `c ≤ C` tokens at
clock reading `t` (`d = c / r`):

| Operation | Condition | Records |
|-----------|-----------|---------|
| Admit, bucket not full (`plan_first` / `plan_retry` pick the side) | `t ≤ z ≤ t + (C − c) / r` | `z + d` |
| Admit, bucket full | `z < t` | `t + d` |
| Debit after the fact (positive adjustment, soft limit) | either side, no upper bound | as above |
| Return / negative adjustment | none | `z − d` |
| Reset | none | `t` |

The two admission writes together record `max(z, t) + d`, the strict token bucket's step; no
single DynamoDB write can (proofs §4), so the fast path predicts the side, retries from each
failed write's `ALL_OLD` with the same `t`, and answers a request still undecided after its
write budget as contended (#724). Stored values are scaled by the undivided refill
amount: `d` is `c × S × rp` and the slack `(C_b − c × S) × rp` (capacities in millitokens,
`rp` in milliseconds, `S` shards, `C_b` the undivided capacity), so nothing is divided and no
rounding enters. Every write is pinned on the `cp` / `ra` / `rp`, `shard_count` and `xi` it was
computed against (ADR-155), and a write that changes any of them, or the schedule, re-expresses
the fill instant in the same write and stamps a new `xi` (`re_express`: the balance is kept, or
trimmed to the new ceiling, never raised), so the pin on `xi` is also the pin on the schedule.

### Mathematical Formulas

The refill and drift formulas below apply to a dripping limit still stored as `tk` (an item
written before v0.18.0, until its next slow pass converts it). A quota does not drip, and a
fill instant needs neither.

**Refill calculation** (lazy, on-demand):

```
tokens_to_add = (elapsed_ms × refill_amount_milli) // refill_period_ms
```

**Drift compensation** (prevents accumulated rounding errors):

```
time_used_ms = (tokens_to_add × refill_period_ms) // refill_amount_milli
new_last_refill = last_refill_ms + time_used_ms
```

The inverse calculation ensures we only "consume" the time that corresponds to whole tokens, preventing drift over many refill cycles.

**Retry-after calculation** (a single fixed rate — an unscheduled limit):

```
time_ms = (deficit_milli × refill_period_ms) // refill_amount_milli
retry_seconds = (time_ms + 1) / 1000.0  # +1ms rounds up
```

With a schedule, the estimate walks forward window by window instead, accumulating tokens at
each window's own rate until the deficit clears (capped at eight windows, then the formula
above). A `reset_schedule` edge landing before the deficit clears is the answer outright: a
quota has no refill rate at all, so its next reset instant is the only finite wait.

### Integer Arithmetic for Precision

All token values are stored as **millitokens** (×1000) to avoid floating-point precision issues in distributed systems:

```python
# User sees: 100 tokens/minute
# Stored as: 100,000 millitokens/minute
capacity_milli = 100_000
```

**Why integers matter in distributed systems:**

- Floating-point operations can produce different results on different hardware
- DynamoDB stores numbers as strings, so precision loss can occur during serialization
- Rate limiting across multiple nodes requires identical calculations

### Refill Rate Storage

Refill rates are stored as a fraction (amount/period) rather than a decimal:

```python
# 100 tokens per minute stored as:
refill_amount_milli = 100_000  # millitokens (numerator)
refill_period_ms = 60_000  # milliseconds (denominator)
```

This avoids representing `1.6667 tokens/second` as a float. Instead:

```python
# 100 tokens/minute = 100,000 millitokens / 60,000 ms
# Integer division handles the math precisely
```

### Lazy Refill with Drift Compensation

Tokens are calculated on-demand rather than via a background timer. The `refill_bucket()` function:

1. Calculates elapsed time since last refill
2. Computes tokens to add using integer division
3. Tracks "time consumed" to prevent drift

```{.python .lint-only}
# From bucket.py:refill_bucket()
tokens_to_add = (elapsed_ms * refill_amount_milli) // refill_period_ms

# Drift compensation: only advance time for tokens actually added
time_used_ms = (tokens_to_add * refill_period_ms) // refill_amount_milli
new_last_refill = last_refill_ms + time_used_ms
```

Without drift compensation, repeated calls with small time intervals would accumulate rounding errors.

### Negative Buckets (Debt)

Buckets can go negative to support post-hoc reconciliation:

```{.python .lint-only}
# Estimate 500 tokens, actually used 2000
async with limiter.acquire(consume={"tpm": 500}) as lease:
    actual = await call_llm()  # Returns 2000 tokens
    await lease.adjust(tpm=2000 - 500)  # Bucket at -1500
```

The `force_consume()` function handles this:

```{.python .lint-only}
# From bucket.py:force_consume()
# Consume can go negative - no bounds checking
new_tokens_milli = refill.new_tokens_milli - (amount * 1000)
```

The debt is repaid as tokens refill over time. A bucket at -1,500 tokens needs 1.5 minutes to reach 0 (at 1,000 tokens/minute). In a fill instant, debt is a `z` more than one fill (`C / r`) past now; a positive adjustment is a debit write, never rejected and never dropped while the bucket holds the limit (ADR-155; skipped once the bucket or the limit is gone, #839).

### Design Decisions

| Decision | Rationale |
|----------|-----------|
| Integer over float | Identical results across distributed nodes; no precision drift |
| Lazy over continuous | No background timers; accurate retry_after; efficient |
| Negative allowed | Estimate-then-reconcile pattern; operations with unknown cost |
| Fraction over decimal | Exact representation of rates like 100/minute |

## Atomicity and Write Paths

### Write Path Overview

The `acquire()` context manager uses three distinct write paths, each with different
atomicity and cost characteristics:

| Write Path | Method | API Used | WCU Cost | Atomicity |
|------------|--------|----------|----------|-----------|
| Initial consumption | `_commit_initial()` | `transact_write()` | 2 WCU (transaction) or 1 WCU (single item) | Cross-item atomic |
| Post-enter adjustments | `_commit_adjustments()` | `write_each()` | 1 WCU per item | Independent per item |
| Rollback (on exception) | `_rollback()` | `write_each()` | 1 WCU per item | Independent per item |
| Aggregator quota reset or roll | `try_refill_bucket()` | `UpdateItem` | 1 WCU | Single item |
| Shard raise (ADR-156) | `Repository.raise_shards()`, `processor.raise_bucket_shards()` | `S` marker `UpdateItem`s, then one `TransactWriteItems` of `2S + 1` items | 1 WCU per marker, 2 per transaction item | Cross-item atomic |

### TransactWriteItems (Initial Consumption)

The initial consumption write (`_commit_initial()`) uses `transact_write()`, which
selects the DynamoDB API based on item count:

- **Single item**: Uses PutItem/UpdateItem (1 WCU) -- non-cascade case
- **Multiple items**: Uses TransactWriteItems (2 WCU per item) -- cascade case

```python
# Cascade: atomic multi-entity write
# 1. Consume from child entity bucket
# 2. Consume from parent entity bucket
# Both succeed or both fail
```

Transaction limits: max 100 items per transaction.

### Independent Writes (Adjustments and Rollbacks)

Adjustments (`_commit_adjustments()`) and rollbacks (`_rollback()`) use `write_each()`,
which dispatches each item independently as a single PutItem, UpdateItem, or DeleteItem
call (1 WCU each). This is safe because:

- Each write is exact on the item it lands on, so it needs no lock: a fill instant's return
  moves it earlier (capped at capacity by construction) and a debit is ADR-154's pair, retried
  with a fresh clock reading until it lands (ADR-155); a quota's credit is an `ADD` conditioned
  on staying within its ceiling and its period (#721)
- Partial success is acceptable -- each item's delta is self-contained
- No cross-item invariant needs to hold between adjustment writes

```python
# write_each: each item written independently
# Item 1: ADD child bucket delta    (1 WCU)
# Item 2: ADD parent bucket delta   (1 WCU)
# No transaction overhead
```

This halves the WCU cost compared to using TransactWriteItems for these paths.

### Speculative Write Path (Issue #315)

When `speculative_writes=True`, `acquire()` adds a fast path before the normal read-write flow:

| Write Path | Method | API Used | WCU Cost | Atomicity |
|------------|--------|----------|----------|-----------|
| Speculative consumption | `speculative_consume()` | Conditional `UpdateItem` | 1 WCU either way (a failed condition is still charged) | Single item |
| Speculative compensation | `_compensate_speculative()` via `write_each()` | `UpdateItem` | 1 WCU | Single item |
| Parallel speculative (issue #318) | `speculative_consume()` via `asyncio.gather` | 2x `UpdateItem` | 2 WCU | Independent items |
| Parent-only slow path | `_try_parent_only_acquire()` via `_commit_initial()` | `UpdateItem` | 1 WCU | Single item |

The speculative path uses `ReturnValuesOnConditionCheckFailure=ALL_OLD` to inspect bucket state
on failure without a separate read. On success, `ReturnValues=ALL_NEW` provides the post-write
state including denormalized `cascade` and `parent_id` fields.

**On an item storing fill instants** (every bucket created since v0.18.0) the flow below
changes ([ADR-154](../adr/154-fill-instant-fast-path.md)). The `tk` write in
step 2 is refused (`attribute_not_exists(xi)`), and its `ALL_OLD` becomes the image the
fill-instant write starts from; later requests on that shard start from this process's cached
image (`fill_images.FillImageCache`), so they skip it. Each hard limit, `wcu` included, then
gets the not-full or full write of [Fill-Instant Arithmetic](#fill-instant-arithmetic-adr-154),
chosen from the image and a per-shard utilisation estimate. A failed write is retried from its
own `ALL_OLD` with the same clock reading rather than falling back to the slow path, until the
request is admitted, rejected (a limit past its slack), or undecided after its write budget
(the contended 429, #724). Only what the two writes cannot express (a schedule boundary since
`xi`, a closed `vu` window, an expired stamp, a missing limit or bucket) takes the slow path.
`wcu` exhaustion raises the bucket (ADR-156) instead of doubling `shard_count`.

```
Speculative flow (first acquire — sequential, populates entity cache):
1. Pick shard: random.randrange(shard_count) from entity cache (shard 0 if no cache)
2. UpdateItem on PK={ns}/BUCKET#{id}#{resource}#{shard} with condition:
   attribute_exists(PK)
   AND attribute_not_exists(disabled)
   AND (attribute_not_exists(vu) OR vu > now_ms)
   AND all app limits tk >= consumed AND wcu tk >= 1000
   +- SUCCESS -> Populate entity cache (cascade, parent_id, shard_counts), Lease pre-committed
   |  +- cascade=False -> DONE
   |  +- cascade=True -> Speculative UpdateItem on parent (sequential)
   |     +- SUCCESS -> DONE (child + parent both speculative)
   |     +- FAIL -> [parent failure handling]
   +- FAIL -> Check ALL_OLD
      +- disabled stamped -> ResourceDisabled
      +- vu passed (schedule boundary) -> Fall back to normal path to re-materialise
      |  (never a shard retry — every shard crosses the same boundary — and never a
      |   fast rejection, since the new window may have raised the limit)
      +- No item (bucket missing) -> Fall back to normal path
      +- wcu exhausted -> Double shard_count (conditional write on shard 0), fall back
      +- App limits: Refill would help -> Fall back to normal path
      +- App limits: Refill won't help, multi-shard -> Retry on another shard (up to 2 retries)
      +- App limits: Refill won't help, single shard -> RateLimitExceeded (fast rejection)

Shard retry flow (when app limits exhausted on multi-shard entity):
1. Pick untried shard: random choice from remaining shards
2. Speculative UpdateItem on new shard
   +- SUCCESS -> Lease pre-committed on new shard, DONE
   +- FAIL -> Try next untried shard (up to _MAX_SHARD_RETRIES=2 total retries)
   +- All retries exhausted -> RateLimitExceeded

Speculative flow (subsequent acquire — parallel, issue #318):
1. Entity cache hit: cascade=True, parent_id known, shard_counts known
2. asyncio.gather(child_speculative, parent_speculative)
   +- BOTH SUCCEED -> DONE (1 round trip, 0 RCU, 2 WCU)
   +- CHILD FAILS, PARENT SUCCEEDS -> Compensate parent, check child
   +- CHILD SUCCEEDS, PARENT FAILS -> [parent failure handling]
   +- BOTH FAIL -> Check child ALL_OLD, fall back or fast-reject

Parent failure handling (shared by sequential and parallel paths):
   +- No ALL_OLD / missing limit -> Compensate child, fall back
   +- Refill won't help -> Compensate child, RateLimitExceeded
   +- Refill would help -> Parent-only slow path (read + write parent)
      +- SUCCESS -> DONE (child speculative + parent slow path)
      +- FAIL -> Compensate child, fall back to full slow path
```

Cascade and `parent_id` are denormalized into composite bucket items (via `build_composite_create`)
so the speculative path avoids a separate entity metadata lookup.

**Deferred cascade compensation:** When the child speculative write succeeds but the parent
fails, child compensation is deferred. If refill would help the parent, a parent-only slow
path is attempted: read parent buckets (0.5 RCU), refill + try_consume, write via single-item
UpdateItem (1 WCU). This avoids the cost of compensating the child (1 WCU), re-reading it
(0.5 RCU), and using TransactWriteItems for the full cascade write (4 WCU). The child is
only compensated when the parent-only path also fails.

**Entity metadata cache (issue #318, GHSA-76rv):** `Repository._entity_cache` stores
`{(namespace_id, entity_id): (cascade, parent_id, shard_counts)}` where `shard_counts` is
`dict[str, int]` mapping resource to shard count. The cache has no TTL: `parent_id` changes
only through `set_parent()` (ADR-150), which the next returned bucket item reveals, and
`shard_counts` is updated when the shard count grows. After the first acquire
populates the cache, `speculative_consume()` issues child and parent speculative writes
concurrently via `asyncio.gather`. This reduces cascade latency
from 2 sequential round trips to 1 parallel round trip. In the sync codepath,
`asyncio.gather(a, b)` is transformed to `self._run_in_executor(lambda: a, lambda: b)` using
a lazy `ThreadPoolExecutor(max_workers=2)` for true parallel execution.
The `_compensate_speculative()` method handles compensation for either child or parent when
one side of the parallel write fails.

The shard_count from the cache determines which shard to target. With multiple shards,
`speculative_consume()` picks a random shard via `random.randrange(shard_count)`. When the
speculative write succeeds, the returned `shard_count` from ALL_NEW updates the cache.

### Aggregator Write Paths (Issue #317, ADR-156)

The Lambda aggregator counts usage from each bucket's `tc` counter and writes buckets for three
reasons. Since v0.18.0 it never refills, trims or clones a **dripping** limit, `wcu` included
(`processor._drips`, #821): every write of a fill instant is exact (ADR-155), and the fast path
reads a fill instant's balance at any instant with nothing to refill.

| Write Path | Method | API Used | WCU Cost | Atomicity |
|------------|--------|----------|----------|-----------|
| Quota reset or roll | `try_refill_bucket()` | Conditional `UpdateItem` | 1 WCU either way (a failed condition is still charged) | Single item |
| Shard raise (a bucket holding a dripping limit) | `raise_bucket_shards()` | marker `UpdateItem`s + one `TransactWriteItems` | as the client's raise | Cross-item atomic |
| Shard doubling and propagation (quota-only buckets) | `try_proactive_shard()`, `propagate_shard_count()` | Conditional `UpdateItem` per existing shard; one `TransactWriteItems` per pre-created shard | 1 WCU per restamp, 2 per transaction item | Single item / per clone |

The aggregator processes DynamoDB Stream records in each batch to:

1. **Aggregate bucket states** -- `aggregate_bucket_states()` accumulates `tc` deltas and keeps
   the last NewImage per (entity_id, resource, shard_id) across all stream records in the batch
2. **Apply quota resets** -- `try_refill_bucket()` writes only when a quota crossed a reset edge
   or a session window rolled: `ADD #rt{i} :rd{i}` (the alias `#rt{i}` names `b_{limit}_tk`;
   tokens are positional because a limit name may contain `.` or `-`) and `SET rf = :new_rf`,
   conditioned on `rf = :expected_rf AND vu = :expected_vu` (optimistic lock, #508). When that
   write moves `vu` past a schedule boundary of a fill instant on the item, it re-expresses the
   item's fill instants in the same write, pinned on each and on `xi` as read
3. **Proactive sharding** -- `try_proactive_shard()` projects shard 0's `wcu` balance to now and,
   if it is below 20% of capacity, raises a bucket holding a dripping limit through ADR-156's
   raise (the same planner the client uses, `shard_raise.py`) or doubles `shard_count` on a
   quota-only bucket, never past `MAX_SHARD_COUNT` (32)
4. **Shard propagation** -- for a quota-only bucket, `propagate_shard_count()` propagates a
   `shard_count` change on shard 0 to the other shards (`shard_count < :new`) and pre-creates the
   new shards, each funded by an ADR-145 quota move. A raise already wrote every shard, so it
   propagates nothing for a bucket holding a dripping limit

```
Quota reset flow (per composite bucket shard):
1. Aggregate tc deltas + last NewImage across stream batch per (entity, resource, shard)
2. Any quota past a reset edge, or a session window rolled?
   +- NO -> SKIP (a dripping limit is never refilled)
   +- YES -> UpdateItem (ADD tk +reset, SET rf = :new_rf,
             condition rf = :expected_rf AND vu = :expected_vu)
      +- SUCCESS -> reset written (1 WCU)
      +- ConditionalCheckFailedException -> silently skip (another writer moved the item)

Proactive sharding flow (shard 0 only):
1. shard_count >= MAX_SHARD_COUNT (32) -> SKIP
2. wcu balance projected to now >= 20% of capacity -> SKIP
3. Holds a dripping limit?
   +- YES -> raise_bucket_shards(): markers, read, one transaction (ADR-156)
   +- NO  -> UpdateItem (SET shard_count = :new, condition shard_count = :old)
```

**Key design properties:**

- **No dripping refill** -- nothing refills a fill instant, so the aggregator cannot race the
  fast path on one, and `--no-aggregator` loses no refill
- **Optimistic lock on `rf` and `vu`** prevents a double reset with the client slow path or
  another aggregator invocation, and keeps a stale image from undoing a parameter change
- **No read for a reset** -- its state comes from the stream record's NewImage; a raise reads
  every shard strongly consistently, through its marker writes
- **A backstop** -- the client's own raise or doubling on `wcu` exhaustion is what protects a hot
  shard, and the aggregator rarely acts before the client has

### Optimistic Locking

Entity metadata uses version numbers for optimistic locking:

```python
# Read entity with version 5
# Update fails if version changed
condition_expression = "version = :expected_version"
```

## Project Structure

```
src/zae_limiter/
├── __init__.py            # Public API exports
├── models.py              # Limit, Entity, LimitStatus, BucketState, StackOptions, ...
├── exceptions.py          # RateLimitExceeded, RateLimiterUnavailable, etc.
├── naming.py              # Resource name validation
├── bucket.py              # Token bucket math (integer arithmetic)
├── fill_instant.py        # Fill-instant arithmetic for dripping limits (ADR-154)
├── fill_images.py         # Cached shard images for the fill-instant fast path
├── fill_sync.py           # Re-expressing fill instants on a parameter change (ADR-155)
├── shard_raise.py         # Shard raise planner, shared with the aggregator (ADR-156)
├── schema.py              # DynamoDB key builders (namespace-prefixed)
├── repository_protocol.py # RepositoryProtocol for backend abstraction
├── repository.py          # DynamoDB operations
├── config_cache.py        # Client-side config caching with TTL
├── lease.py               # Lease context manager
├── limiter.py             # RateLimiter, SyncRateLimiter
├── local.py               # LocalStack management commands
├── cli.py                 # CLI commands (deploy, delete, status, list, local, ...)
├── version.py             # Version tracking and compatibility
├── migrations/            # Schema migration framework
├── visualization/         # Usage snapshot formatting and display
└── infra/
    ├── stack_manager.py    # CloudFormation stack operations
    ├── lambda_builder.py   # Lambda deployment package builder
    ├── discovery.py        # Multi-stack discovery and listing
    └── cfn_template.yaml   # CloudFormation template

src/zae_limiter_aggregator/   # Lambda aggregator (top-level package)
├── __init__.py               # Re-exports handler, processor types
├── handler.py                # Lambda entry point (returns refills_written count)
├── processor.py              # Stream processing: usage snapshots, quota resets, shard raises
└── archiver.py               # S3 audit archival (gzip JSONL)
```

## Key Design Decisions

1. **Write-on-enter**: `acquire()` writes initial consumption to DynamoDB before yielding the lease, making tokens immediately visible to concurrent callers. On exception, a compensating write restores the consumed tokens
2. **Bucket can go negative**: `lease.adjust()` never throws, allows debt
3. **Cascade is per-entity config**: Set `cascade=True` on `create_entity()` to auto-cascade to parent on every `acquire()`
4. **Stored limits are the default (v0.5.0+)**: Limits resolved from System/Resource/Entity config automatically. Pass `limits` parameter to override
5. **Initial writes are atomic**: Multi-entity initial consumption uses `transact_write()` for cross-item atomicity
6. **Adjustments and rollbacks use independent writes**: `write_each()` dispatches each item as a single-item API call (1 WCU each), avoiding transaction overhead; each write is exact on the item it lands on, so none needs cross-item atomicity
7. **Speculative writes skip reads**: With `speculative_writes=True`, `acquire()` tries a conditional UpdateItem first, saving 1 round trip and 1 RCU when the bucket has sufficient tokens
8. **Entity metadata cache enables parallel cascade**: `Repository._entity_cache` stores `(cascade, parent_id, shard_counts)` per entity. The cache has no TTL: a bucket item the fast path returns overrules it (`parent_id` moves only through `set_parent()`, ADR-150), and `shard_counts` (`dict[str, int]`) is updated when the shard count grows. After first acquire, cascade speculative writes run concurrently via `asyncio.gather`
9. **Nothing refills a dripping limit**: a dripping limit stores its fill instant (ADR-154), whose balance is exact at any instant, so the aggregator only counts usage, applies quota resets, and backs up the client's sharding
10. **Pre-shard buckets (GHSA-76rv)**: Each bucket item lives on its own DynamoDB partition (`PK={ns}/BUCKET#{id}#{resource}#{shard}`). An auto-injected `wcu:1000` infrastructure limit tracks per-partition write pressure. When wcu is exhausted, the client raises a bucket holding a dripping limit (ADR-156: balances moved, never minted) and doubles shard_count on a quota-only one; that is what protects a hot shard. As a backstop, the aggregator does the same when the projected wcu balance is below 20% of capacity (never past `MAX_SHARD_COUNT`)
11. **Strict admission bound (ADR-153)**: over any window, a dripping limit admits at most its capacity plus the refill across the window plus `2·r·s` for clock skew and time in flight, summed over shards (plus, with several shards, the debt a positive adjustment leaves on a shard other than the one admitting: each shard is its own bucket); the fast path (ADR-154), every other writer (ADR-155) and shard raises (ADR-156) each hold it ([proofs](fill-instant-proofs.md))

## Next Steps

- [Development Setup](development.md) - Setting up your environment
- [Testing](testing.md) - Test organization and fixtures
