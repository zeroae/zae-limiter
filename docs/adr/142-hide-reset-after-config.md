# ADR-142: A reset_after limit's config is hidden from pre-v0.15 readers

**Status:** Proposed
**Date:** 2026-09-27
**Issue:** [#640](https://github.com/zeroae/zae-limiter/issues/640)
**Related:** [ADR-139](139-duration-reset-windows.md), [ADR-140](140-duration-window-shard-coherence.md), [ADR-141](141-reset-after-version-gate.md), ADR-137, ADR-114, [#638](https://github.com/zeroae/zae-limiter/issues/638)

## Context

A client predating [ADR-139](139-duration-reset-windows.md) cannot reconstruct a `reset_after`
limit: it reads a zero rate with no reset, which ADR-137 rejects, and loses the whole config
level with it. Under `on_unavailable=block` every acquire on that level fails; under `allow` every
acquire is admitted with no limiting at all, including the level's other limits.
[ADR-141](141-reset-after-version-gate.md) keeps the old aggregator and provisioner away, but
nothing a newer build does can make an old client fail closed.

Every pre-v0.15 config reader discovers limits by the `l_` attribute prefix alone, and none of
them rejects an attribute it does not recognise. A limit stored under another prefix is therefore
invisible to them, and they go on enforcing the rest of the level. Bucket attributes need no such
treatment: old clients already read and rewrite a bucket carrying the session limit's attributes
without error, and only the old aggregator mishandles them, which ADR-141 prevents.

Hiding the limit lets old clients write buckets that carry a window, whose shared `rf` and `vu`
they stamp from their own limits and clock. [ADR-140](140-duration-window-shard-coherence.md)
makes the window mechanism independent of both, and the seed funds a shard an old client creates
by [ADR-145](145-sharded-quota-conserves-allowance.md)'s move off the sibling whose grant covers
it. The storage mapping, readers and verification against
v0.14.0 are in #640 and CLAUDE.md "Hidden config (#640)".

## Decision

A `reset_after` limit's config attributes must be stored under the `w_` prefix (`w_{name}_*`),
which pre-v0.15 readers do not read, and every config reader from v0.15 on must read both prefixes and
must treat a name stored under both, or a `w_` limit without its window length, as a corrupt item.

**Owner decision (2026-09-27):** accepted that under `on_unavailable="block"` a pre-v0.15 client
enforces every other limit and skips only the session limit, rather than refusing the level.

## Consequences

**Positive:**
- A pre-v0.15 client keeps enforcing every other limit on a level that holds a session limit,
  under either `on_unavailable` mode, instead of failing the level or admitting it unlimited.
- A level holding only the session limit reads as empty to it, so it falls through to the next
  level's limits rather than to none.
- The prefix is a storage mapping only: nothing above the serialiser sees it, and a level written
  under `l_` by an unreleased build is read as before and moves to `w_` on its next write.

**Negative:**
- Old clients do not enforce the session limit for the requests they serve; under `block` that
  replaces an outage with silent non-enforcement, bounded by their share of traffic.
- An old client stamps a bucket's TTL from the limits it resolves, which can be shorter than the
  window's recovery horizon; a bucket swept mid-window restarts its window part-way, admitting at
  most one extra allowance.
- An old admin tool rewrites a whole level from the limits it can see, so it silently drops the
  hidden limit from config and leaves its balance on the buckets. Admin tooling must be upgraded
  first.
- An old param sync can change the item-level schedule a session limit without its own override
  inherits. An old client's shard creation, funded on the next v0.15 pass by ADR-145's move, can
  only under-admit.

## Alternatives Considered

### Hide the bucket attributes as well
Rejected because: old clients already handle them, and only the old aggregator misreads them,
which ADR-141 already keeps away.

### A hidden field suffix under the `l_` prefix
Rejected because: any suffix ending in the capacity field stays visible to the old discovery rule.

### A separate config item per level for session limits
Rejected because: it breaks the atomic full replace of a level and adds a read to every cache
miss.

### One opaque map attribute per session limit
Rejected because: it violates the flat-schema rule of ADR-111.

### Leave the configuration visible and upgrade every client first
Rejected because: an old client under `allow` then admits a whole level with no limiting at all.
