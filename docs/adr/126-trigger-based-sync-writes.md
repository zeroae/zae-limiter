# ADR-126: Trigger-Based Sync Config Writes

**Status:** Proposed
**Date:** 2026-02-14

## Context

With quota enforcement via entity config overrides (ADR-125), the sync Lambda writes
a `set_limits()` call for every active (entity, resource) pair each cycle. At 2,000
active entities with 10 resources and a 10-second sync window, this produces 20,000
WCU per cycle — ~$486/month. Most of these writes are wasted: 80% of entities are well
under their limits with stable allocations.

The sync window already defines the worst-case over-admission bound (entities can
double-consume for one sync window regardless of write frequency). Config writes do
not improve the worst case; they only tighten steady-state accuracy.

## Decision

The sync Lambda must only write entity config overrides when one of two triggers fires:

1. **Exhaustion trigger:** The entity's projected time-to-exhaustion (remaining tokens
   divided by recent consumption rate) is less than twice the sync window. This prevents
   the entity from running out of tokens before the next sync cycle can react.

2. **Drift trigger:** The computed allocation differs from the currently configured
   capacity by more than 15%. This corrects stale quotas for entities whose traffic
   pattern has shifted significantly.

All other entities must be skipped (no config write). Trigger evaluation must be
computed from S3 snapshot data (ADR-124) without additional DynamoDB reads.

## Consequences

**Positive:**
- Config writes drop from ~20,000 to ~600 per cycle at steady state (~97% reduction)
- Monthly sync cost drops from ~$486 to ~$40 at 2,000 active entities
- DynamoDB write throughput spikes are smoothed (fewer concurrent writes)
- Worst-case over-admission is unchanged (bounded by sync window, not write frequency)

**Negative:**
- Entities with slowly drifting traffic (<15% per cycle) may have stale quotas for
  multiple sync cycles before the drift threshold triggers
- Exhaustion prediction depends on consumption rate estimation, which may be noisy for
  bursty workloads
- Two tunable parameters (exhaustion horizon = 2x sync window, drift threshold = 15%)
  require validation under production traffic patterns

## Alternatives Considered

### Write every entity every cycle (no filtering)
Rejected because: 97% of writes are redundant, costing ~$450/month in unnecessary WCU
without improving the over-admission bound set by the sync window.

### Write only on exhaustion (drop drift trigger)
Rejected because: entities with shifting traffic patterns would keep stale allocations
indefinitely, wasting regional quota until they approach exhaustion.

### Event-driven writes via DynamoDB Streams (write on every bucket change)
Rejected because: couples sync frequency to acquire volume rather than a fixed window,
producing more writes than periodic polling for high-throughput entities.
