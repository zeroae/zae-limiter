# ADR-111: Flatten All DynamoDB Records to Top-Level Attributes

**Status:** Proposed
**Date:** 2026-01-25
**Issue:** [#180](https://github.com/zeroae/zae-limiter/issues/180)

## Context

The codebase accumulated three DynamoDB schema patterns: nested `data.M` maps (entities, limits, audit events, version records), hybrid (buckets with one flat counter), and flat (usage snapshots and config records). ADR-006 and ADR-010 introduced flat attributes to solve DynamoDB's "overlapping document paths" limitation. ADR-101 standardized flat schema for all new config records and explicitly recommended flattening existing record types in v0.6.0.

The mixed patterns create inconsistency in serialization/deserialization code, complicate the aggregator Lambda (which must navigate nested paths), and make the codebase harder to maintain. Every new feature must decide which pattern to use, adding cognitive overhead.

## Decision

All DynamoDB record types must use flat schema (top-level attributes, no nested `data.M` wrapper). Deserialization reads flat format only. Pre-1.0.0 semver allows breaking changes without migration — existing nested records are not supported. Serialization produces only flat format.

## Consequences

**Positive:**
- Uniform schema across all record types eliminates pattern inconsistency
- Simpler serialization code without `data.M` wrapper construction
- Aggregator Lambda reads flat attributes directly instead of navigating nested paths
- Enables atomic operations on any attribute without "overlapping paths" errors
- No branching logic in deserializers

**Negative:**
- Existing nested `data.M` records from pre-0.6.0 deployments must be recreated (no migration path provided pre-1.0.0)
- DynamoDB reserved words (`name`, `resource`, `action`, `timestamp`) require `ExpressionAttributeNames` aliases in all expressions

## Alternatives Considered

### Keep nested `data.M` for existing records
Rejected because: Perpetuates schema inconsistency; ADR-101 already committed to flattening in v0.6.0.

### Big-bang migration (write stops until all records migrated)
Rejected because: Requires downtime; pre-1.0.0 semver makes migration unnecessary — breaking changes are expected.

### Flatten only on read (no serialization changes)
Rejected because: Leaves old-format records indefinitely; new writes would still produce nested format, preventing convergence.
