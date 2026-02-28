# ADR-123: Local Secondary Indexes

**Status:** Proposed
**Date:** 2026-02-27

## Context

DynamoDB Local Secondary Indexes (LSIs) can only be defined at table creation time — they cannot be added to existing tables. DynamoDB allows a maximum of 5 LSIs per table. Each LSI shares the table's partition key (`PK`) and provides an alternative sort key within the same partition.

The project has not yet reached v1.0. Once we release v1.0, adding an LSI becomes a breaking change requiring table recreation and full data migration. Pre-1.0 is the only window to make this decision.

An LSI slot that is defined but has no items with the sort key attribute populated costs nothing — zero storage, zero write amplification, zero RCU/WCU. The cost of defining unused slots is zero; the cost of not defining them is permanent.

## Decision

Define all 5 Local Secondary Indexes on the DynamoDB table before v1.0.

### Overloading

LSI sort keys are overloaded following the same single-table design pattern used for GSIs. Different item types in different partitions use different value formats for the same LSI sort key attribute. Since queries always specify a PK, the formats never collide. This maximizes the value of each slot — a single LSI can serve multiple unrelated access patterns across different partition key spaces.

### Projection pattern

Odd-numbered LSIs use ALL projection; even-numbered use KEYS_ONLY:

| Slot | Sort Key Attribute | Projection |
|------|-------------------|------------|
| LSI1 | `LSI1SK` (String) | ALL |
| LSI2 | `LSI2SK` (String) | KEYS_ONLY |
| LSI3 | `LSI3SK` (String) | ALL |
| LSI4 | `LSI4SK` (String) | KEYS_ONLY |
| LSI5 | `LSI5SK` (String) | ALL |

Projection types cannot be changed after table creation. The alternating pattern provides a permanent mix of:

- **ALL** (3 slots) — single-query data retrieval, no follow-up reads required
- **KEYS_ONLY** (2 slots) — cheap discovery and filtering, follow up with BatchGetItem when full data is needed

Which slots are populated, and with what value formats, are separate decisions that can be made independently as access patterns are implemented.

## Consequences

**Positive:**

- Preserves all 5 LSI slots before the v1.0 schema freeze at zero cost
- Overloaded design enables multiple access patterns per slot
- Alternating projection types provide flexibility for both data-retrieval and discovery use cases
- Individual LSI slots can be populated incrementally without schema changes
- No impact on existing access patterns — primary key and GSIs are unchanged

**Negative:**

- Pre-1.0 deployments require table recreation to adopt the new schema
- 10GB per-partition-key limit applies across base table and all LSI items sharing a PK
- Projection types are permanent — if the alternating pattern proves suboptimal for a specific use case, that slot's projection cannot be changed
- Each populated LSI slot adds write cost (one additional WCU per indexed write for items under 1KB) and storage for items with the sort key attribute set

## Alternatives Considered

### Add LSIs post-1.0

LSIs cannot be added to existing tables. Post-1.0, this requires table recreation and data migration — a breaking change requiring a major version bump.

### Define only slots with immediate use cases

Unused LSI slots cost nothing. Defining fewer than 5 saves nothing and permanently forecloses future options. Defining all 5 is strictly better given the irreversibility of the decision.

### Use GSIs instead of LSIs

GSIs can be added at any time and are not subject to the pre-1.0 urgency. However, GSIs have separate throughput costs and are better suited for cross-partition queries. LSIs are the optimal choice for queries already scoped to a single partition key, which describes several current and planned access patterns. The two index types are complementary, not interchangeable.

### Uniform projection (all ALL or all KEYS_ONLY)

All-ALL maximizes read efficiency but commits every future slot to full storage duplication. All-KEYS_ONLY minimizes storage but forces follow-up reads on every query. The alternating pattern avoids committing entirely to either trade-off.
