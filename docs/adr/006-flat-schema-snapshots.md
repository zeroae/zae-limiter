# ADR-006: Flat Schema for Usage Snapshots

**Status:** Accepted
**Date:** 2026-01-11 (initial), 2026-01-16 (confirmed)
**PRs:** [#20](https://github.com/zeroae/zae-limiter/pull/20), [#172](https://github.com/zeroae/zae-limiter/pull/172)
**Issue:** [#168](https://github.com/zeroae/zae-limiter/issues/168)
**Milestone:** v0.1.0, v0.4.0

## Context

Usage snapshots aggregate consumption data (tpm, rpm, total_events) per entity/resource/time window. The Lambda aggregator uses `UpdateItem` with `ADD` for atomic counter increments. Initial implementation used nested `data.M` maps like other record types.

DynamoDB limitation discovered: You cannot SET a map path (`#data = if_not_exists(#data, :map)`) AND ADD to paths within it (`#data.counter`) in the same UpdateExpression. It fails with "overlapping document paths" error.

## Decision

Use flat schema (top-level attributes) for usage snapshot records, diverging from the nested `data.M` pattern used elsewhere.

**Snapshot structure:**
```python
{
    "PK": "ENTITY#user-1",
    "SK": "#USAGE#gpt-4#2024-01-01T14:00:00Z",
    "resource": "gpt-4",      # Top-level
    "window": "hourly",       # Top-level
    "tpm": 5000,              # Counter at top-level
    "total_events": 10,       # Counter at top-level
}
```

## Consequences

**Positive:**
- Atomic upsert with ADD counters works correctly
- Single UpdateItem call creates or updates record
- No pre-existence check required

**Negative:**
- Schema inconsistency with other record types (entities, buckets use nested `data.M`)
- Established pattern for v0.6.0 full schema flattening (see [#180](https://github.com/zeroae/zae-limiter/issues/180))

## Alternatives Considered

- **Two-step create-then-update**: Rejected; race conditions, higher latency, more RCU/WCU
- **Conditional expressions with retries**: Rejected; complex error handling, still potential races
- **Store in separate table**: Rejected; loses single-table benefits, complicates transactions
