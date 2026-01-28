# Composite Bucket Items

**Date:** 2026-01-28
**Status:** Proposed
**Scope:** schema, repository, aggregator, limiter, lease

## Problem

Each rate limit (rpm, tpm) is stored as a separate DynamoDB item. For an entity with
N limits on one resource, `acquire()` costs:

- **Read:** 1 BatchGetItem fetching N+1 items (META + N buckets)
- **Write:** 1 TransactWriteItems with N PutItems = 2N WCU (transaction 2x tax)

With 2 limits and cascade, cost is ~3 RCU + 8 WCU = 11 CU per acquire.

Additionally, the current PutItem-based writes suffer from **lost updates** under
concurrency: the last writer overwrites all previous writers' consumption, and the
`total_consumed_milli` counter (used by the aggregator) is also overwritten.

## Solution

Consolidate all limits for an entity+resource into a **single composite DynamoDB item**.
Use **ADD for consumption** with **lazy refill on read** and **optimistic locking via
a single shared refill timestamp**.

### Composite Item Schema

All limits for an entity+resource share one item. A single `rf` (refill timestamp)
attribute serves as both the refill baseline and the optimistic lock for the entire
item. Per-limit attributes use the prefix `b_{limit_name}_{field}`:

```
PK:   ENTITY#user-123
SK:   #BUCKET#gpt-4

# Shared attributes
entity_id:  "user-123"
resource:   "gpt-4"
rf:         1706000000     # shared refill timestamp (ms) — also the optimistic lock
ttl:        1704067200
GSI2PK:     RESOURCE#gpt-4
GSI2SK:     BUCKET#user-123

# Per-limit bucket data (b_{limit}_{field})
b_rpm_tk:  99000        # tokens (millitokens)
b_rpm_cp:  100000       # capacity (millitokens)
b_rpm_bx:  100000       # burst max (millitokens)
b_rpm_ra:  100000       # refill amount (millitokens)
b_rpm_rp:  60000        # refill period (ms)
b_rpm_tc:  1000         # total consumed counter (millitokens)

b_tpm_tk:  9500000
b_tpm_cp:  10000000
b_tpm_bx:  10000000
b_tpm_ra:  10000000
b_tpm_rp:  60000
b_tpm_tc:  500000
```

**Attribute name mapping:**

| Full name              | Short | Per-limit? | Description            |
|------------------------|-------|------------|------------------------|
| `tokens_milli`         | `tk`  | Yes        | Current token balance  |
| `capacity_milli`       | `cp`  | Yes        | Capacity               |
| `burst_milli`          | `bx`  | Yes        | Burst maximum          |
| `refill_amount_milli`  | `ra`  | Yes        | Refill amount          |
| `refill_period_ms`     | `rp`  | Yes        | Refill period          |
| `total_consumed_milli` | `tc`  | Yes        | Total consumed counter |
| `last_refill_ms`       | `rf`  | **No**     | Shared refill timestamp |

**Key schema changes from current design:**

- SK drops the `#{limit_name}` suffix: `#BUCKET#gpt-4` instead of `#BUCKET#gpt-4#rpm`
- All limit data is inlined as prefixed flat attributes
- A single `rf` attribute replaces per-limit refill timestamps
- GSI2SK is per-entity (not per-entity-per-limit): 1 GSI2 entry instead of N
- `limit_name` attribute removed (encoded in attribute prefix)

**Why a single `rf`:**

- **Simpler lock:** Condition is always `rf = :expected` regardless of limit count
  (no AND chain growing with N limits)
- **Fewer attributes:** 6 per limit instead of 7 (saves ~90 bytes at 10 limits)
- **Coherent refill:** All limits share the same refill baseline — refill is
  computed once from `rf` and applied to every limit
- **Atomic window:** The refill window is claimed for ALL limits at once — one lock,
  one winner, no partial claims
- **Smaller expressions:** UpdateExpression shrinks from N ADD + N ADD + N SET + N
  conditions to N ADD + N ADD + 1 SET + 1 condition
- **Free requirement:** We already update all limits on every acquire (the lease
  commits all bucket states), so updating all buckets from a shared `rf` costs nothing

### Lazy Refill on Read

Refill is never stored in `tk`. Instead, effective tokens are computed at read time
using the shared `rf`:

```{.python .lint-only}
rf = item["rf"]
elapsed = now - rf

for name in limits:
    rate = item[f"b_{name}_ra"] / item[f"b_{name}_rp"]
    refill = elapsed * rate
    effective_tk = min(item[f"b_{name}_tk"] + refill, item[f"b_{name}_bx"])
```

Writers only ADD consumption (negative) to `tk`. The shared `rf` timestamp is SET
on successful writes, establishing the baseline for the next reader's refill
computation across all limits.

### Four Write Paths

#### 1. Create (item doesn't exist)

Used on first acquire for an entity+resource. Read returns no item.

```{.python .lint-only}
table.put_item(
    Item={
        "PK": pk_entity(entity_id),
        "SK": sk_bucket(resource),
        "entity_id": entity_id,
        "resource": resource,
        "rf": now,
        "b_rpm_tk": capacity - consumed,  # initial balance
        "b_rpm_cp": capacity,
        "b_rpm_bx": burst,
        "b_rpm_ra": refill_amount,
        "b_rpm_rp": refill_period,
        "b_rpm_tc": consumed,
        # ... same for each limit
        "GSI2PK": gsi2_pk, "GSI2SK": gsi2_sk, "ttl": ttl,
    },
    ConditionExpression="attribute_not_exists(PK)",
)
```

On `ConditionalCheckFailedException` (concurrent creation race): retry as a
Normal write.

#### 2. Normal (item exists, no contention)

The optimistic lock is the shared `rf` — if it matches our read value, no
concurrent writer has modified the item. The ADD includes both refill and
consumption for all limits.

```{.python .lint-only}
table.update_item(
    Key={"PK": pk, "SK": sk},
    UpdateExpression="""
        ADD b_rpm_tk :(rpm_refill - rpm_consumed),
            b_rpm_tc :rpm_consumed,
            b_tpm_tk :(tpm_refill - tpm_consumed),
            b_tpm_tc :tpm_consumed
        SET rf = :now
    """,
    ConditionExpression="rf = :expected_rf",
)
```

On `ConditionalCheckFailedException`: fall through to Retry path.

#### 3. Retry (lost optimistic lock)

Another writer claimed the refill window. Skip refill, only consume. Condition
ensures tokens don't go negative (negative only allowed via Adjust path).

```{.python .lint-only}
table.update_item(
    Key={"PK": pk, "SK": sk},
    UpdateExpression="""
        ADD b_rpm_tk :(-rpm_consumed),
            b_rpm_tc :rpm_consumed,
            b_tpm_tk :(-tpm_consumed),
            b_tpm_tc :tpm_consumed
    """,
    ConditionExpression="b_rpm_tk >= :rpm_consumed AND b_tpm_tk >= :tpm_consumed",
)
```

On `ConditionalCheckFailedException`: raise `RateLimitExceeded`. The request was
approved based on stale data, but another writer consumed the available tokens.

#### 4. Adjust (post-hoc correction)

`lease.adjust()` corrects consumption after the fact. Can go negative (by design).
No condition, no read needed.

```{.python .lint-only}
# Consumed more than estimated
table.update_item(
    Key={"PK": pk, "SK": sk},
    UpdateExpression="ADD b_rpm_tk :(-delta), b_rpm_tc :delta",
)

# Consumed less than estimated (return tokens)
table.update_item(
    Key={"PK": pk, "SK": sk},
    UpdateExpression="ADD b_rpm_tk :delta, b_rpm_tc :(-delta)",
)
```

### Cascade Support

With cascade, `acquire()` updates both child and parent composite items. The write
uses TransactWriteItems with two Update operations (one per entity). Each entity's
composite item has its own `rf`, and the transaction condition checks both:

```
Read:  BatchGetItem → 2-3 items
  - ENTITY#user-123  SK=#META           (cached, 0 RCU on hit)
  - ENTITY#user-123  SK=#BUCKET#gpt-4   (child composite)
  - ENTITY#project-1 SK=#BUCKET#gpt-4   (parent composite)

Write: TransactWriteItems → 2 Updates
  - Child: ADD + SET rf, CONDITION rf = :expected
  - Parent: ADD + SET rf, CONDITION rf = :expected
```

Cost comparison (2 limits, cascade):

| Metric        | Current     | Composite   |
|---------------|-------------|-------------|
| Read          | ~3 RCU      | ~2 RCU      |
| Write         | 8 WCU       | 4 WCU       |
| **Total**     | **11 CU**   | **6 CU**    |
| Items in call | 5 read, 4 write | 3 read, 2 write |

The composite cost is **constant regardless of limit count**: always 2 items to
read and 2 to write for cascade.

### Concurrency Model

| Aspect              | Current (PutItem)      | Composite (ADD + lock) |
|---------------------|------------------------|------------------------|
| Token balance       | Lost update            | Correct (ADD)          |
| `tc` counter        | Lost update            | Correct (ADD)          |
| Double refill       | N/A (refill in balance)| Prevented (single rf lock) |
| Negative on acquire | Possible               | Prevented (condition)  |
| Negative on adjust  | Possible (by design)   | Possible (by design)   |
| Retry cost          | N/A                    | 1 WCU, no re-read     |

**Concurrency trace:**

```
State: tk=90, rf=T0, capacity=100, rate=1.67/sec

Thread A at T1 (1sec later):
  read: rf=T0, rpm_tk=90
  effective = min(90 + 1.67, 100) = 91.67
  consume 3 → 91.67 - 3 = 88.67 ≥ 0 ✓
  write: ADD rpm_tk:(1.67-3), SET rf:T1, CONDITION rf=T0 → succeeds
  rpm_tk = 88.67, rf = T1

Thread B at T1 (concurrent):
  read: rf=T0, rpm_tk=90
  effective = min(90 + 1.67, 100) = 91.67
  consume 7 → 91.67 - 7 = 84.67 ≥ 0 ✓
  write: ADD rpm_tk:(1.67-7), SET rf:T1, CONDITION rf=T0 → FAILS

  retry: ADD rpm_tk:(-7), CONDITION rpm_tk >= 7
         rpm_tk = 88.67, 88.67 >= 7 → TRUE ✓
  rpm_tk = 81.67

Final: rpm_tk=81.67, rf=T1
Correct: 90 + 1.67(refill once) - 3 - 7 = 81.67 ✓
```

Under high contention (100+ concurrent writers), all consumptions are correctly
counted via atomic ADD. The single `rf` timestamp has at most ~500ms drift
(contention window), causing the limiter to be slightly more restrictive — the
safe direction.

### Aggregator Changes

The aggregator Lambda (`processor.py`) currently processes one stream event per
bucket item, extracting a single `ConsumptionDelta` per event.

With composite items, one stream event contains old/new images with ALL limits.
The `extract_delta` function becomes `extract_deltas` (returns a list):

```{.python .lint-only}
def extract_deltas(record: dict) -> list[ConsumptionDelta]:
    new_image = record["dynamodb"]["NewImage"]
    old_image = record["dynamodb"]["OldImage"]

    sk = new_image["SK"]["S"]
    if not sk.startswith(SK_BUCKET):
        return []

    resource = sk[len(SK_BUCKET):]  # No more #{limit_name} suffix
    entity_id = new_image["entity_id"]["S"]

    # Shared rf for timestamp
    new_rf = int(new_image.get("rf", {}).get("N", "0"))

    deltas = []
    # Enumerate b_{name}_tc attributes
    for key in new_image:
        if key.startswith("b_") and key.endswith("_tc"):
            limit_name = key[2:-3]  # extract name from b_{name}_tc
            new_tc = int(new_image[key]["N"])
            old_tc = int(old_image.get(key, {}).get("N", "0"))
            if new_tc != old_tc:
                deltas.append(ConsumptionDelta(
                    entity_id=entity_id,
                    resource=resource,
                    limit_name=limit_name,
                    tokens_delta=new_tc - old_tc,
                    timestamp_ms=new_rf,
                ))
    return deltas
```

Benefits:
- Fewer stream events (1 per acquire instead of N)
- Fewer Lambda invocations
- All limit deltas in a single atomic event (consistent snapshot)

### Edge Cases

**Negative buckets:** `tk` only decreases via consumption ADD during acquire.
Debt accumulates naturally and is recovered by lazy refill on read. Adjust path
allows negative (by design, for post-hoc reconciliation).

**Burst:** `burst_milli` (`bx`) is stored per-limit and applied as the cap in the
lazy refill computation: `effective = min(tk + refill, bx)`. Unchanged from
current behavior.

**Bucket creation race:** Two threads race to create the same composite item.
PutItem with `attribute_not_exists(PK)` ensures one wins. Loser retries as a
Normal write (item now exists).

**Adding limits:** New limit attributes are added via `if_not_exists` guards in
the same UpdateItem. Concurrent writers safely initialize via `if_not_exists`.
The shared `rf` applies to new limits immediately (refill computed from item
creation or last write).

**Removing limits:** Orphaned `b_{name}_*` attributes are cleaned up via REMOVE
in the next write for the entity+resource.

**High contention (100+ writers):** All consumptions counted correctly via ADD.
The single `rf` timestamp drift is bounded by the contention window (~500ms).
Limiter becomes slightly more restrictive (safe direction). Self-correcting once
contention drops.

**Lease adjust:** Maps directly to unconditional ADD. No read required. Concurrent
adjustments are all correctly applied. Cleaner than current read-modify-write-PUT
cycle.

### Cost Model

Per-limit storage: ~105 bytes (6 attributes with short names). Shared overhead:
~310 bytes (including `rf`). Sweet spot: 2-10 limits per entity+resource.

| Limits | Item size | Read (GetItem) | Write (UpdateItem) |
|--------|-----------|----------------|--------------------|
| 2      | ~520 B    | 1 RCU          | 1 WCU              |
| 5      | ~835 B    | 1 RCU          | 1 WCU              |
| 10     | ~1.4 KB   | 1 RCU          | 2 WCU              |

Non-cascade acquire cost: **1 RCU + 1 WCU = 2 CU** (vs current 5.5 CU).
Cascade acquire cost: **2 RCU + 4 WCU = 6 CU** (vs current 11 CU).

### Entity Metadata

The entity `#META` item is NOT merged into the composite bucket item. Reasons:

- META contains `parent_id`, `cascade`, and GSI1 keys for parent-child queries
- Merging would duplicate META across resources and break GSI1 deduplication
- The existing 60s TTL config cache already skips META reads on cache hits
- Cache miss adds 1 RCU (acceptable)

### Migration Strategy

This is a breaking schema change (SK format changes). Recommended approach:

1. **New schema version** in `version.py` / migration framework
2. **Read path:** Read composite format first, fall back to legacy separate items
3. **Write path:** Always write composite format
4. **Background migration:** Script to convert existing separate items to composite
5. **Cleanup:** Remove legacy items after migration, remove fallback code

This should be a **v2.0 schema milestone** item given the scope of changes:
repository, limiter, lease, aggregator, schema, and migration.

### Files Affected

| File | Changes |
|------|---------|
| `schema.py` | New `sk_bucket(resource)` (drop limit_name from SK) |
| `repository.py` | Composite read/write, four write paths, deserialization |
| `limiter.py` | Lazy refill computation, retry logic |
| `lease.py` | ADD-based commit, adjust via ADD |
| `bucket.py` | Refill computation changes (lazy, read-time only) |
| `models.py` | BucketState may need composite wrapper |
| `processor.py` | `extract_deltas` (multi-delta from single event) |
| `version.py` | New schema version |
| `migrations/` | Migration script for existing data |
