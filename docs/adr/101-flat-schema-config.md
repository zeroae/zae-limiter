# ADR-101: Flat Schema for Config Records

**Status:** Proposed
**Date:** 2026-01-18
**Issue:** [#129](https://github.com/zeroae/zae-limiter/issues/129)

## Context

The codebase has three DynamoDB schema patterns:

| Pattern | Records | Use Case |
|---------|---------|----------|
| Nested `data.M` | Entities, Limits, Audit, Version | No atomic counters needed |
| Hybrid | Buckets (`total_consumed_milli` flat) | Mostly nested + one atomic counter |
| Flat | Snapshots | Atomic upsert with ADD counters |

DynamoDB rejects UpdateExpressions that combine `SET #data = if_not_exists(#data, :map)` with `ADD #data.counter :delta` due to "overlapping document paths" (issues [#168](https://github.com/zeroae/zae-limiter/issues/168), [#179](https://github.com/zeroae/zae-limiter/issues/179)).

New config records need atomic `config_version` counter increments for cache invalidation.

## Decision

Use **flat schema** (no nested `data.M`) for all new config records at System, Resource, and Entity levels. This matches the snapshot pattern established in v0.4.0.

**v0.6.0 recommendation:** Flatten all existing record types (entities, limits, audit, version) for consistency. See [#180](https://github.com/zeroae/zae-limiter/issues/180).

## Consequences

**Positive:**
- Enables atomic `config_version` counter increments
- Consistent with snapshot pattern (v0.4.0)
- Sets standard: flat schema for all new records
- Forward compatible with v0.6.0 full schema migration

**Negative:**
- v0.6.0 will require migration work to flatten existing records
- Two schema patterns coexist until v0.6.0

## Alternatives Considered

### Nested `data.M` Schema
Rejected: Inconsistent with flat snapshot pattern; DynamoDB prevents atomic counters with nested paths.

### Hybrid Schema (like buckets)
Rejected: Adds complexity; better to standardize on flat for new records.
