# ADR-121: Usage Snapshot Sort Key Redesign with Rollup Resources

**Status:** Proposed
**Date:** 2026-02-02
**Related:** [ADR-006](006-flat-schema-snapshots.md) (flat schema for snapshots)

## Context

Usage snapshots track consumption per entity/resource/time window. The current sort key format is `#USAGE#{resource}#{window_key}`. This design has two limitations:

1. **Window type filtering is inefficient.** Queries like "all hourly snapshots for entity X" require a `FilterExpression` on the `window` attribute, reading all usage items and filtering client-side.

2. **Cross-resource time-range queries are inefficient.** Billing dashboards need "entity's total hourly consumption for January across all resources." This requires reading all snapshots and aggregating client-side.

Local Secondary Indexes (LSIs) were considered but rejected: LSIs cannot be added to existing tables, creating a migration barrier. Additionally, LSIs for consumption-based sorting (e.g., by `total_events`) would cause write amplification since counters change on every aggregation.

## Decision

Redesign the usage snapshot sort key to `#USAGE#{window_type}#{resource}#{window_key}` and introduce rollup snapshots with `resource="_all_"` that aggregate consumption across all resources.

The aggregator Lambda must write two snapshots per consumption delta per window: one for the specific resource and one for the `_all_` rollup. Both use atomic ADD operations.

## Consequences

**Positive:**

- Window type queries use efficient SK prefix (`begins_with #USAGE#hourly#`)
- Billing queries read a single rollup record instead of scanning all resources
- Resource + window + time range queries use efficient SK `between` conditions
- No LSI required; works with existing tables after backfill migration

**Negative:**

- 2x write amplification in aggregator (per-resource + rollup per window)
- ~1.5-2x storage for snapshot items
- Querying "all snapshots for gpt-4 regardless of window type" requires two queries or FilterExpression
- Requires backfill migration for existing snapshots

## Alternatives Considered

### LSI for window-type filtering
Rejected because: LSIs cannot be added to existing tables; would require table recreation and full data migration.

### Time-before-resource SK without rollup
Rejected because: Cross-resource time-range queries still require reading all resources; doesn't solve billing dashboard use case.

### Resource-before-time SK (current design)
Rejected because: Window type filtering requires FilterExpression; billing queries must aggregate client-side.

### Query-time aggregation
Rejected because: Higher read costs and latency for billing dashboards; pushes complexity to every consumer.
