# ADR-144: Resource and system changes fan out to merged entity buckets

**Status:** Proposed
**Date:** 2026-09-28
**Related:** [ADR-136](136-entity-config-bucket-ttl.md), [ADR-143](143-merge-limits-through-hierarchy.md), [#468](https://github.com/zeroae/zae-limiter/issues/468), [#487](https://github.com/zeroae/zae-limiter/issues/487)
**Design:** `docs/plans/2026-09-28-limit-inheritance-merge-plan.md`

## Context

Bucket items store each limit's base parameters, and nothing on the acquire path compares them
with config: the speculative fast path reads no config, and the normal path writes parameters only
when it seeds a limit missing from the item. A config change reaches existing buckets in one of
two ways. Entity-level changes are fanned out to every shard (#468, #487). Resource- and
system-level changes are not fanned out; their buckets carry a TTL and are recreated with the
current parameters when it expires. [ADR-136](136-entity-config-bucket-ttl.md) exempts any bucket
whose limits come from an entity level from that TTL, so that deliberately configured entities
never lose rate-limit state. It rejected fanning out resource and system changes because those
levels reach every bucket in a namespace.

[ADR-143](143-merge-limits-through-hierarchy.md) breaks the assumption that joins these rules. A
merged entity bucket holds limits from the entity level **and** from resource or system. It is
custom, so under ADR-136 it carries no TTL. Its inherited limits come from levels that do not fan
out. A later `set_system_defaults()` would therefore never reach it, and nothing detects the
drift.

## Decision

A change to resource- or system-level limits (set or delete, via the API or the provisioner) must
be fanned out to the buckets of every entity whose resolution reaches the changed level through
`inherit_limits: true`, and to no other bucket. Each such bucket must be stamped from its own
merged resolution by the existing limit-change sync, and TTL keeps [ADR-136](136-entity-config-bucket-ttl.md)'s rule: a
bucket with any entity-level limit persists.

## Consequences

**Positive:**
- A merged entity's inherited limits track the level that owns them without waiting for any
  expiry, and ADR-136's guarantee that configured entities keep their state is untouched
- Scope is bounded by entities that opted into merge, not by all buckets. An override-only
  deployment pays only the discovery reads and writes nothing
- Reuses the entity fan-out's machinery (per-bucket resolution, shard coverage, `vu = 0`
  re-materialisation, partial-progress reporting), so no new bucket write shape exists
- Buckets running purely on resource or system defaults keep propagating by TTL, unchanged

**Negative:**
- A resource or system write becomes O(merged dependent buckets) writes and can fail part-way.
  Callers get a progress count and must re-run, as with the entity fan-out
- A system write must discover every entity carrying any entity config before it can filter to
  merged ones, which grows with the number of configured entities
- The provisioner's Lambda-side sync must mirror the discovery and the merged resolution exactly,
  a second implementation to keep in parity
- The narrow race the entity fan-out already has applies here too: a bucket created between the
  config write and the discovery query can be missed until its next param sync

## Alternatives Considered

### Give a merged bucket a TTL whenever any limit comes from resource or system
Rejected because: it expires exactly the deliberately configured buckets ADR-136 protects,
resetting their rate-limit state and breaking consumption-counter continuity.

### Fan out resource and system changes to every bucket
Rejected because: ADR-136 already rejected O(all buckets) per administrative write, and buckets
without entity config already propagate by TTL.

### Detect stale parameters on the acquire path
Rejected because: the speculative fast path reads no config by design, so detection would cost a
read on every acquire.

### Forbid merging for levels that do not fan out
Rejected because: inheriting from system and resource is the use case ADR-143 exists for.
