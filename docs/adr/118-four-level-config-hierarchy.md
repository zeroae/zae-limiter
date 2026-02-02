# ADR-118: Four-Level Configuration Hierarchy

**Status:** Accepted
**Date:** 2026-02-02
**Issue:** [#297](https://github.com/zeroae/zae-limiter/issues/297)
**Supersedes:** ADR-102

## Context

ADR-102 established a three-level configuration hierarchy (Entity > Resource > System). However, this design lacks a way to set entity-wide defaults that apply to all resources for that entity without duplicating configuration for each resource.

Operators need to set per-entity defaults (e.g., premium tier limits) that apply across all resources, while still allowing resource-specific entity overrides for edge cases.

## Decision

Extend the hierarchy to **four levels**: Entity (resource-specific) > Entity (`_default_`) > Resource > System > Constructor defaults.

When resolving limits for `acquire(entity_id, resource)`:
1. Check entity config for the specific resource
2. Check entity config for `_default_` (fallback for all resources)
3. Check resource-level defaults
4. Check system-level defaults
5. Use constructor override parameter

Entity `_default_` config is treated as a default for TTL purposes (TTL applied), distinguishing it from resource-specific entity config (no TTL).

## Consequences

**Positive:**
- Premium users can have elevated limits on all resources with a single config entry
- Resource-specific entity overrides still take precedence when needed
- Backward compatible—existing three-level configs work unchanged

**Negative:**
- One additional cache lookup per resolution when entity+resource config misses
- Slightly more complex resolution logic

## Alternatives Considered

### Keep three-level hierarchy
Rejected because: Operators would need to duplicate entity config for every resource, which is error-prone and tedious.

### Treat entity `_default_` as custom config (no TTL)
Rejected because: Entity `_default_` is semantically a default, not a resource-specific customization; TTL should apply.
