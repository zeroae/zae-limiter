# ADR-001: Single-Table DynamoDB Design

**Status:** Accepted
**Date:** 2026-01-09
**Commit:** [3902c8c](https://github.com/zeroae/zae-limiter/commit/3902c8c)
**Milestone:** v0.1.0

## Context

zae-limiter needs to store multiple entity types: entities (users/API keys), token buckets, limits, audit events, usage snapshots, and version metadata. Traditional relational design would use separate tables, but DynamoDB pricing and access patterns favor different approaches.

Key requirements:
- Atomic multi-item transactions (entity + buckets in single transaction)
- Efficient parent-child lookups (hierarchical entities)
- Resource-level aggregation (capacity across all entities)
- Pay-per-request pricing optimization

## Decision

Use single-table design with composite keys and GSIs for all access patterns.

**Key structure:**
- `PK`: Entity/resource identifier (e.g., `ENTITY#user-1`, `RESOURCE#gpt-4`, `SYSTEM#`)
- `SK`: Record type and identifiers (e.g., `#META`, `#BUCKET#gpt-4#tpm`, `#AUDIT#2026-01-01T00:00:00Z`)

**Global Secondary Indexes:**
- `GSI1`: Parent → Children lookups (`GSI1PK=PARENT#{id}`)
- `GSI2`: Resource aggregation (`GSI2PK=RESOURCE#{name}`)

## Consequences

**Positive:**
- Single table = single provisioning decision, simpler cost management
- TransactWriteItems works across all record types (max 100 items)
- Efficient queries: entity + all buckets in single query
- Natural fit for hierarchical data (parent/child via GSI1)

**Negative:**
- More complex key design requires careful documentation
- Hot partition risk if single entity has extreme traffic
- GSI costs for every write (mitigated by sparse indexes)

## Alternatives Considered

- **Multi-table design**: Rejected due to cross-table transaction limitations and higher operational complexity
- **Adjacency list only**: Rejected; GSIs provide cleaner access patterns for resource aggregation
