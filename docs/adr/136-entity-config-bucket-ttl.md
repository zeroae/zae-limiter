# ADR-136: Entity Configuration Determines Bucket TTL

**Status:** Proposed
**Supersedes:** ADR-119
**Date:** 2026-09-14
**Issue:** [#487](https://github.com/zeroae/zae-limiter/issues/487), [#489](https://github.com/zeroae/zae-limiter/issues/489)

## Context

ADR-119 established that buckets using entity custom limits persist indefinitely, while buckets using entity `_default_`, resource, or system defaults carry a TTL. That `_default_` clause classified a resolution level which was hours old: #271 was designed and implemented while entity `_default_` did not resolve at all, its enabling fix #297 closed the following morning, and ADR-119 was written that same day. #271's own TTL table has two rows — system/resource defaults, and entity-level custom limits — with no row for `_default_`, because the level did not yet exist.

ADR-119 scopes the TTL to "ephemeral entities (anonymous users, one-time callers)" and exempts custom limits because they "signal explicit intent". An entity carrying an explicit `_default_` configuration satisfies that exemption — an operator configured it deliberately. Expiring its bucket inflicts the harm ADR-119 itself names, an unintended rate limit reset, and additionally breaks the continuity of the total-consumed counter that usage snapshots derive from. The storage reclaimed is negligible at realistic scale (measured in #489).

TTL also serves a second purpose ADR-119 did not record: it is the propagation mechanism for default-derived buckets. `set_resource_defaults()` and `set_system_defaults()` do not fan out, so those buckets pick up changed parameters by expiring and being recreated. Entity `_default_` relied on the same mechanism only because `_sync_bucket_params` could not reach those buckets, which #487 fixes.

## Decision

A bucket carries a TTL only when its limits resolve from the resource or system level; entity configuration at either the per-resource or the entity-wide `_default_` level is custom, and those buckets must persist indefinitely. ADR-119's time-to-fill TTL formula is unchanged.

## Consequences

**Positive:**
- Explicitly configured entities never lose rate limit state, matching ADR-119's own stated intent
- Total-consumed counter continuity is preserved, so usage reporting has no gaps for those entities
- TTL keeps only the roles its rationale describes: reclaiming ephemeral buckets, and propagating resource and system default changes

**Negative:**
- Behaviour change — buckets created from entity-wide `_default_` limits currently expire and will now persist; requires a changelog entry
- Storage for entity-wide default buckets is no longer reclaimed automatically, and must be managed by deleting the entity or its configuration
- Two code paths and one characterization test encode the old rule and must change together (#489)

## Alternatives Considered

### Keep ADR-119 and fix `set_limits` to apply the TTL for `_default_`
Rejected because: it re-arms on every sync the unintended rate limit reset that ADR-119 identifies as unacceptable, for negligible storage savings.

### Fan out resource and system default changes and drop TTL entirely
Rejected because: those levels apply to every entity in a namespace, making the fan-out O(all buckets) per administrative operation.

### Treat `_default_` as custom only when no per-resource entity config exists
Rejected because: it makes a bucket's TTL depend on configuration for an unrelated resource, which no reader could predict.
