# ADR-124: S3-Based Cross-Region Sync Exchange

**Status:** Proposed
**Date:** 2026-02-14

## Context

With independent DynamoDB tables per region (ADR-123), a sync mechanism must exchange
consumption data between regions. The exchange payload is a snapshot of all active
entities' bucket states: `total_consumed_milli`, `tokens_milli`, and `capacity_milli`
per entity, resource, and limit.

Using DynamoDB as the exchange medium means writing one item per active (entity, resource)
pair per sync cycle. At 2,000 active entities with 10 resources and a 10-second sync
window, this costs ~$3,900/month in WCU alone — roughly 3x the entire acquire budget.

The sync payload is a batch snapshot: all active entities' state at a point in time.
This is a bulk data transfer problem, not an item-level access problem.

## Decision

Each region's sync Lambda must write its consumption snapshot as a single S3 object
(JSON) to a shared sync bucket, keyed by `{region}/snapshot.json`. Remote regions must
read these objects via cross-region S3 GET. DynamoDB must not be used for publishing
sync reports.

The snapshot must include, per active (entity, resource) pair: the per-limit
`total_consumed_milli` counter, the current `tokens_milli`, and the configured
`capacity_milli`. Snapshot objects must have a TTL (S3 lifecycle) of 5 minutes.

## Consequences

**Positive:**
- Publishing cost drops to ~$1.30/month regardless of entity count (1 S3 PUT per cycle)
- Reading cost is ~$0.10/month per remote region (1 S3 GET per cycle)
- Snapshot size is bounded: 2,000 entities x 10 resources x 60 bytes = ~1.2 MB per PUT
- S3 is highly available and durable; no capacity planning needed

**Negative:**
- Introduces S3 dependency for cross-region coordination (new failure mode)
- S3 eventual consistency means a GET may return a slightly stale snapshot (~1s)
- Requires a shared S3 bucket accessible from all regions (cross-region GET latency
  ~100ms, acceptable for background sync)
- Snapshot format must be versioned to handle schema evolution

## Alternatives Considered

### DynamoDB items for sync reports (one per entity per resource)
Rejected because: WCU cost scales linearly with entity count, reaching $3,900/month
at 2,000 active entities with 10-second sync — 3x the acquire budget.

### DynamoDB items for sync reports (one batch item per resource)
Rejected because: 400KB item size limit caps at ~4,000 entities per item, and large
item writes consume proportionally more WCUs, offering no cost advantage over
individual items.

### SQS/SNS for event-driven sync
Rejected because: requires per-event cross-region message delivery, adding complexity
and cost proportional to acquire volume rather than sync frequency.
