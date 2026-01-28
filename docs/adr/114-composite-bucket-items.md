# ADR-114: Composite Bucket Items

**Status:** Proposed
**Date:** 2026-01-28
**Issue:** [#248](https://github.com/zeroae/zae-limiter/issues/248)

## Context

Each rate limit (rpm, tpm) for an entity+resource is stored as a separate DynamoDB
item with SK `#BUCKET#{resource}#{limit_name}`. For an entity with N limits,
`acquire()` reads N+1 items via BatchGetItem and writes N items via
TransactWriteItems. The transaction 2x WCU tax means 2 limits with cascade costs
11 CU per acquire, scaling linearly with limit count.

GSI2 produces N entries per entity+resource (one per limit), inflating resource
aggregation queries. The per-limit item design also prevents atomic operations
across limits — each item is updated independently within the transaction.

Entity metadata (`#META`) must remain a separate item because it carries GSI1 keys
for parent→children queries. Merging META into bucket items would duplicate it
across resources and break GSI1 deduplication. The existing 60s config cache
already skips META reads on cache hits.

## Decision

All limits for an entity+resource must be stored in a single composite DynamoDB
item with SK `#BUCKET#{resource}`. Per-limit attributes must use the prefix
`b_{limit_name}_{field}` with short field names: `tk` (tokens), `cp` (capacity),
`bx` (burst), `ra` (refill amount), `rp` (refill period), `tc` (total consumed).
GSI2SK must be per-entity (`BUCKET#{entity_id}`), not per-limit.

## Consequences

**Positive:**
- Acquire cost is constant regardless of limit count (1 item read, 1 item write)
- Non-cascade acquire drops from 5.5 CU to 2 CU; cascade drops from 11 CU to 6 CU
- GSI2 entries reduced from N per entity+resource to 1
- All limits for an entity+resource are atomically readable and writable

**Negative:**
- Breaking schema change requiring migration (new SK format)
- Per-limit attributes use short names (`tk`, `cp`) that are less readable than current names
- Deserialization must enumerate prefixed attributes to reconstruct BucketState objects
- Adding or removing limits requires updating a shared item rather than creating/deleting items

## Alternatives Considered

### Keep separate items, use UpdateItem instead of PutItem
Rejected because: fixes lost updates but does not reduce item count, CU cost, or
GSI2 inflation — the core scaling problem remains.

### Merge entity metadata into composite bucket item
Rejected because: duplicates META across resources, breaks GSI1 parent→children
queries, and requires fan-out writes on metadata changes.

### Nested map per limit (limits.rpm.tokens_milli)
Rejected because: DynamoDB cannot use atomic ADD on nested paths without
overlapping SET+ADD errors (ADR-111, issue #168).
