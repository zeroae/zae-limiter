# ADR-127: Per-Region Sync Lambda

**Status:** Proposed
**Date:** 2026-02-14

## Context

Cross-region sync (ADR-123, ADR-124) requires a Lambda function that reads consumption
snapshots, computes quota allocations, and writes config overrides. Two topologies were
evaluated:

- **Single coordinator:** One Lambda in a designated region reads all snapshots, computes
  all quotas, and writes configs to all regions. Total cross-region calls: `2(N-1)` per
  cycle (reads + writes). Single point of failure.
- **Per-region:** Each region runs its own Lambda that reads remote snapshots, computes
  its own quota locally, and writes only to its local table. Total cross-region calls
  per Lambda: `N-1` reads, 0 writes.

Both topologies produce the same quota allocation when given the same inputs. The
per-region Lambda runs a deterministic function: given the same S3 snapshots, every
region independently computes the same allocation. No distributed consensus is needed.

## Decision

Each region must run its own sync Lambda, triggered by EventBridge on a fixed schedule
(configurable sync window). Each Lambda must read its local bucket states, write its
snapshot to S3 (ADR-124), read all remote snapshots from S3, compute quotas using a
deterministic allocation function, and write triggered config overrides (ADR-126) to
its local DynamoDB table only. No Lambda may write to a remote region's DynamoDB table.

## Consequences

**Positive:**
- Symmetric architecture: every region deploys the same CloudFormation stack
- No single point of failure: one region's Lambda failure does not affect other regions
- Zero cross-region DynamoDB writes (only cross-region S3 reads, ~100ms latency)
- Scales naturally: adding a region means deploying the same stack, no coordinator changes
- Each region can independently tune its sync window

**Negative:**
- N Lambdas compute the same allocation independently (redundant CPU, negligible cost)
- Slight snapshot staleness between Lambdas reading at different moments within a cycle
  (sub-second divergence, converges on next cycle)
- More infrastructure per region (EventBridge rule + Lambda + IAM), though identical
  across regions and part of the standard stack deployment

## Alternatives Considered

### Single coordinator Lambda in a designated region
Rejected because: introduces an asymmetric "special" region, creates a single point of
failure for all global quota allocation, and requires cross-region DynamoDB writes for
config overrides in remote regions.

### Peer-to-peer gossip between regional Lambdas
Rejected because: adds network coordination complexity (discovery, message ordering)
without improving on the deterministic-computation-from-shared-S3 approach.
