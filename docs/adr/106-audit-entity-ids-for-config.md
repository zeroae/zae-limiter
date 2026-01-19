# ADR-106: Audit Entity IDs for Config Levels

**Status:** Proposed
**Date:** 2026-01-19
**Issue:** [#130](https://github.com/zeroae/zae-limiter/issues/130)

## Context

The audit logging system requires an `entity_id` for all audit events. This works naturally for entity-level operations where the entity_id is the actual entity being modified.

However, system-level and resource-level config changes (introduced in #130) have no natural entity. System config applies globally, and resource config applies to a resource name rather than a specific entity.

Additionally, operators need to query audit trails for config changes separately from entity changes. Mixing config audits with entity audits under arbitrary entity IDs would make compliance queries difficult.

## Decision

Use special prefixes for config-level audit entity IDs:
- **System config**: `$SYSTEM`
- **Resource config**: `$RESOURCE:{resource_name}` (e.g., `$RESOURCE:gpt-4`)

The `$` prefix is chosen because it cannot appear in valid entity IDs (which must start with alphanumeric characters per validation rules).

## Consequences

**Positive:**
- Clear distinction between entity and config audit events
- Operators can query all system config changes via `entity_id=$SYSTEM`
- Resource config audits are grouped by resource name
- No collision risk with real entity IDs

**Negative:**
- Introduces a reserved character convention that must be documented
- Audit queries for "all changes" must now include both entity and `$`-prefixed patterns

## Alternatives Considered

### Use literal strings without prefix (e.g., "SYSTEM")
Rejected: Could collide with actual entity IDs if a user creates an entity named "SYSTEM".

### Skip audit logging for config changes
Rejected: Config changes are security-sensitive operations that require audit trails for compliance.
