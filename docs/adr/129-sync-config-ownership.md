# ADR-129: Sync Config Ownership via TTL Presence

**Status:** Proposed
**Date:** 2026-02-14

## Context

Both operators and the sync Lambda write entity-level config records to the same
DynamoDB item (`PK=ENTITY#{id}, SK=#CONFIG#{resource}`) via `set_limits()`. Without
an ownership mechanism, the sync Lambda overwrites operator-set limits on its next
cycle, and operators overwrite sync-computed quotas on manual updates.

The sync Lambda writes configs with a TTL attribute (ADR-128). Operator-written configs
have no TTL (they persist indefinitely per ADR-119). This difference in TTL presence
is a natural discriminator for config ownership.

## Decision

The sync Lambda must only write to an entity config record when the record is absent or
the existing record has a `ttl` attribute (indicating it was previously sync-written).
The sync Lambda's write must use the condition
`attribute_not_exists(PK) OR attribute_exists(ttl)`. This ownership check must be
implemented in the Repository layer (not in RateLimiter or the sync Lambda itself),
consistent with ADR-122's requirement that data access logic lives in the repository.

Operator-written configs (no `ttl` attribute) must never be overwritten by the sync
Lambda. When an operator writes entity config via `set_limits()`, the record must not
include a `ttl` attribute, signaling operator ownership. The sync Lambda must skip
that entity for all subsequent cycles.

To return an entity to sync-managed quotas, the operator must delete the entity config
via `delete_limits()`. The sync Lambda will then recreate it with TTL on the next
triggered cycle (ADR-126).

## Consequences

**Positive:**
- Operator configs always win: manual overrides are never clobbered by automated sync
- No new attributes: TTL presence is a sufficient ownership discriminator
- Reversible: `delete_limits()` returns the entity to sync management
- Condition check costs 0 extra RCU (evaluated server-side in the UpdateItem condition)

**Negative:**
- Operators must delete (not overwrite) entity configs to return to sync management;
  overwriting with `set_limits()` produces a record without TTL, taking operator ownership
- The sync Lambda's `ConditionalCheckFailedException` for operator-owned entities is
  silent (expected), but increases CloudWatch error metrics unless filtered
- If an operator accidentally creates entity config, the entity silently leaves sync
  management with no warning; observability tooling must surface this

## Alternatives Considered

### Explicit `origin` attribute ("sync" vs "operator") on config records
Rejected because: adds a new attribute that must be threaded through all config read/write
paths, requires migration for existing records, and provides no benefit over the TTL
presence check that ADR-128 already establishes.

### Sync Lambda always wins (overwrite operator configs)
Rejected because: operators set entity limits for business reasons (premium tiers, custom
SLAs); automated sync should not override intentional business decisions.

### Separate config level for sync (Entity > Sync > Resource)
Rejected because: breaks the four-level config hierarchy (ADR-118) and requires changes
to `resolve_limits()` in every backend implementation.
