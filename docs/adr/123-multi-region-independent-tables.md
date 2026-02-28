# ADR-123: Multi-Region via Independent Regional Tables

**Status:** Proposed
**Date:** 2026-02-14

## Context

Users need to enforce rate limits across multiple AWS regions (e.g., an organization's
global RPM limit must be shared by clients in us-east-1 and eu-west-1). DynamoDB Global
Tables is the obvious candidate, but it has fundamental conflicts with zae-limiter's
write patterns:

- **ADD counter loss:** Global Tables uses last-writer-wins at the item level. Concurrent
  `ADD tk -consumed` from two regions results in one write overwriting the other, silently
  losing consumption data and causing over-admission.
- **Transaction non-atomicity:** `TransactWriteItems` is ACID only in the originating
  region. Cascade child+parent writes appear as partial updates in other regions.
- **Double refill:** Each region's aggregator processes its own stream. Replicated writes
  appear in all streams, requiring filtering to avoid double-counting and double-refilling.

The namespace feature (issue #376) already provides write isolation: each namespace has
its own partition key prefix, so records in different namespaces never collide.

## Decision

Multi-region must use **independent DynamoDB tables per region**, one per deployed stack.
Each region must use a dedicated namespace for its rate-limiting data. Cross-region
coordination must be handled by a periodic sync mechanism (see ADR-124, ADR-127), not by
DynamoDB replication. Global Tables must not be used for the rate-limiting table.

## Consequences

**Positive:**
- Write cost stays at 1x (no replicated WCU tax)
- All existing write patterns (speculative, optimistic lock, transactions) work unchanged
- Aggregator Lambda processes only local events, no stream filtering needed
- Each region is fully independent; one region's failure does not affect others

**Negative:**
- No automatic data replication; regional data is lost if a region fails permanently
- Cross-region coordination requires a new sync component (ADR-124, ADR-127)
- Rate limiter state is ephemeral; region loss causes temporary over-admission until
  sync catches up, bounded by one sync window

## Alternatives Considered

### DynamoDB Global Tables with namespace-per-region isolation
Rejected because: replicated WCUs double write cost, ADD operations lose data under
concurrent cross-region writes, and transactions are not atomic across regions.

### DynamoDB Global Tables with counter sharding (per-region SET attributes)
Rejected because: requires reworking the composite bucket schema (ADR-114), breaks
speculative writes, and the 2x write cost is not justified when a sync mechanism is
needed regardless.

### Centralized single-region table with cross-region API calls
Rejected because: adds 50-150ms latency to every acquire() call for remote-region
clients, creating a single point of failure with no local fallback.
