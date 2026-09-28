# ADR-143: A config level may merge its limits over the level below

**Status:** Proposed
**Date:** 2026-09-28
**Related:** [ADR-118](118-four-level-config-hierarchy.md), [ADR-135](135-scheduled-limits.md), [ADR-137](137-reset-replaces-drip.md), [ADR-141](141-reset-after-version-gate.md), [ADR-142](142-hide-reset-after-config.md), [ADR-144](144-fan-out-to-merged-entity-buckets.md)
**Design:** `docs/plans/2026-09-28-limit-inheritance-merge-plan.md`

## Context

[ADR-118](118-four-level-config-hierarchy.md) resolves limits by taking the first level that has
any: entity (resource) → entity `_default_` → resource → system. The winning level supplies the
whole set, and nothing below it is consulted. A resource that overrides only `rpm` therefore
silently drops the system `tpm`, and an entity that overrides only `rpm` drops the resource's
`tpm`. Operators have to restate every limit at every level. A forgotten one is not an error; the
limit simply stops being enforced.

Merging must not become implicit. Every existing manifest was written against override
semantics, and flipping the default would change enforcement for every deployed override in one
release. Merging must not mix fields within one limit either. A capacity from one level beside a
schedule or refill rate from another is a limit no operator wrote, and under
[ADR-137](137-reset-replaces-drip.md) such a combination can be unconstructible (a reset beside a
positive rate). The one legitimate field-level need is an entity or resource that inherits a
limit's numbers but runs it on its own time-of-day schedule. Restating the numbers to add a
schedule pins them and cuts that limit off from inheritance.

A client that predates this feature reads a merging level as override. It enforces fewer limits
than configured, and nothing reports it.

## Decision

A resource, entity `_default_` or entity (resource) config level carrying `inherit_limits: true`
must resolve to its own limits merged by limit name over what the next level down resolves to,
where each name takes the **whole** `Limit` from the highest level that declares it. The only
exceptions are that level's `exclude_limits`, which remove inherited names, and its
`patch_limits`, which replace only the `schedule` of an inherited limit. A write storing any of
the three must raise `client_min_version` to the introducing release; a timezone conflict in the
merged set must fail closed as `RateLimiterUnavailable`; and a system level must reject all three.

## Consequences

**Positive:**
- A level states only what it changes. Inherited limits keep tracking the level that owns them
- Opt-in per level: an override-only deployment resolves exactly as before, and adoption is one
  field on the configs that want it
- Whole-`Limit` resolution keeps every resolved limit one that some operator wrote. Patches are
  confined to `schedule`, the one field whose replacement cannot change how a limit recovers
- No extra reads: the batched config fetch already reads all four levels on a cache miss
- An old client is refused when it opens the repository rather than silently enforcing fewer
  limits, from the introducing release on

**Negative:**
- One limit's definition can span two levels (numbers below, schedule above), so the stored
  config alone no longer shows what is enforced. An effective view in the CLI and in `limits plan`
  becomes necessary rather than convenient
- Validity now depends on other levels: a timezone conflict or an invalid patched limit can come
  from a write at a level the patch writer does not control. Writes must be validated against
  their dependents, and a system write can be refused because of an entity's patch
- A system or resource change now silently adds or changes limits on every merging level, the
  mirror image of today's silent drop. Only the effective view makes it visible
- Inherits [ADR-141](141-reset-after-version-gate.md)'s holes: `client_min_version` is checked
  only when a repository is opened, and v0.14 clients ignore it
- Merged entity buckets carry limits from levels that do not fan out, which is why
  [ADR-144](144-fan-out-to-merged-entity-buckets.md) is required alongside this ADR

## Alternatives Considered

### Make merge the default
Rejected because: it changes enforcement for every existing override in one release, with no way
to adopt it incrementally.

### One namespace-wide switch on the system config item
Rejected because: it forces one semantics on every level of every tenant and turns adoption into
an all-or-nothing migration.

### Merge individual fields across levels
Rejected because: it builds limits no operator wrote, and a reset beside an inherited positive
rate violates ADR-137.

### Allow patching every field, not only `schedule`
Rejected because: a patched `capacity` whose `refill_amount` still inherits recreates field-level
mixing one field at a time, and a patched `reset_schedule` or `reset_after` changes how the limit
recovers.

### Tombstone limits (`tpm: null`) instead of `exclude_limits`
Rejected because: every `l_`/`w_` reader, both Lambdas included, would have to learn a limit that
is not a limit.

### On a timezone conflict, drop the lower level's limit
Rejected because: it silently stops enforcing a limit an operator configured, and guessing a zone
instead moves a daily reset by hours with no error.
