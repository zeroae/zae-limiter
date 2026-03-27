# ADR-128: TTL on Sync-Written Entity Config Records

**Status:** Proposed
**Date:** 2026-02-14

## Context

The sync Lambda enforces regional quotas by writing entity-level config overrides via
`set_limits()` (ADR-125). Per ADR-119, buckets using entity custom limits persist
indefinitely (no TTL), while buckets using resource/system defaults have TTL and
auto-expire.

When the sync Lambda writes an entity config, the bucket transitions from
"default-limit" (has TTL) to "custom-limit" (no TTL). If the entity later goes idle
and the sync Lambda stops writing configs (no trigger fires per ADR-126), both the
config record and its bucket persist indefinitely. For high-churn entity populations
(anonymous users, ephemeral API keys), this causes unbounded storage growth.

The fix must not affect operator-written entity configs, which intentionally persist
indefinitely per ADR-119.

## Decision

Sync-written entity config records must include a DynamoDB TTL attribute set to
`now + 3 × sync_window`. The sync Lambda must refresh the TTL on each config write.

This extends ADR-119's bucket TTL rule. The updated bucket TTL logic is:

- Bucket has **no TTL** if: entity config exists **without** a `ttl` attribute
  (operator-written, persists indefinitely — unchanged from ADR-119)
- Bucket **has TTL** if: entity config exists **with** a `ttl` attribute
  (sync-written, treated as default-like for TTL calculation)
- Bucket **has TTL** if: no entity config exists
  (using resource/system defaults — unchanged from ADR-119)

When the entity goes idle (no trigger fires for 3 sync windows), the config record
auto-expires via DynamoDB TTL. The entity reverts to resource/system defaults, and
bucket TTL behavior per ADR-119 resumes.

## Consequences

**Positive:**
- Idle entities auto-cleanup: sync config expires, bucket regains TTL, storage bounded
- No new attributes needed: the DynamoDB `ttl` attribute already exists in the table schema
- Self-healing: if a sync Lambda fails permanently, all its configs expire within 3 windows

**Negative:**
- Bucket TTL logic (ADR-119) must check whether the entity config has a `ttl` attribute
  to distinguish sync-written from operator-written configs
- Config records gain a new write pattern: conditional refresh of TTL alongside limits
- DynamoDB TTL deletion is asynchronous (up to 48 hours), so expired configs may linger
  in scans; queries using strong conditions are unaffected

## Alternatives Considered

### Explicit cleanup pass in the sync Lambda (delete stale configs)
Rejected because: requires the sync Lambda to maintain a "previously synced" entity set
across invocations, adding state management complexity to a stateless Lambda function.

### Separate DynamoDB sort key for sync configs (#SYNC_CONFIG#{resource})
Rejected because: adds a new config level to the resolution hierarchy (ADR-118), breaking
the existing four-level precedence model and requiring changes to `resolve_limits()`.

### No TTL on sync configs (rely on operator cleanup)
Rejected because: operators should not need to manually clean up configs created by an
automated sync process, especially for ephemeral entities at scale.
