# ADR-150: Move an entity to a new parent

**Status:** Proposed
**Date:** 2026-10-09
**Issue:** [#677](https://github.com/zeroae/zae-limiter/issues/677)
**Amends:** [ADR-112](112-cascade-per-entity.md) (parent fixed at creation), CLAUDE.md invariant 9
**Related:** [ADR-125](125-resource-disable.md), [ADR-141](141-reset-after-version-gate.md), [ADR-145](145-sharded-quota-conserves-allowance.md), [ADR-146](146-per-resource-cascade-policy.md), [ADR-147](147-client-side-rejection-cache.md), [#674](https://github.com/zeroae/zae-limiter/issues/674), [#684](https://github.com/zeroae/zae-limiter/issues/684), [#686](https://github.com/zeroae/zae-limiter/issues/686)
**Design:** `docs/plans/2026-10-09-move-entity-design.md`

## Context

An entity's `parent_id` is written once, by `create_entity()`, onto its META record (with
the GSI1 parent→children keys). Since #684 every bucket item the entity owns carries the
same `parent_id` beside the resolved cascade policy (ADR-146), and the fast path debits
whichever parent the item, or the process's entity cache, names — with no META read.
CLAUDE.md invariant 9 states that this metadata never changes, and the entity cache has no
expiry. The use case in #674 needs a user moved between organisations, effective at once.

ADR-146 already built the machinery a move needs: an eager, two-pass fan-out over an
entity's buckets that stamps `cascade` and `parent_id` from strongly consistent reads, and
a warm path on which the child's returned item overrules the cache for `cascade`. Two gaps
remain. The warm path checks only `cascade`, so a process holding the old parent keeps
debiting it once the item has moved on. And a slow pass that read META before the move can
land its owner stamp after the fan-out, writing the old parent back onto a bucket that may
then take only the fast path; nothing would ever correct it. A move to no parent adds a
third: a stamp with no `parent_id` is today ignored as a pre-#684 artefact.

## Decision

An entity's parent must be changeable through one repository operation that updates the
META record and its GSI1 keys, then restamps every bucket the entity owns through the
ADR-146 cascade fan-out, which remains the only writer of `cascade` and `parent_id` on
bucket items outside the slow path's owner stamp. Each move must increment a parent
generation on META; every write of `parent_id` to a bucket item (owner stamp, create,
fan-out, provisioner fan-out) must carry the generation it read and must not overwrite a
higher one, so a stale writer can lose but never undo a move. A bucket item stamped with a
generation is authoritative for both `cascade` and `parent_id`, including when it names no
parent. On the warm path, when the child's returned item names a different parent from the
one the cache debited, the limiter must refund that debit, debit the item's parent, and
relearn the cache from the item. A move must refuse an entity as its own ancestor, must
write an audit event, and must pass the ADR-141 version gate at 0.17.0 and ratchet
`client_min_version` to 0.17.0. Consumption already debited to the old parent, including
by leases open across the move, stays where it was debited.

## Consequences

**Positive:**
- A move takes effect on the fast path of every process after at most one call per
  (process, entity), with no added cost on any acquire.
- One write path owns `parent_id` on bucket items, as ADR-146 item 10 asked.
- The generation also settles the "no `parent_id` = pre-#684" ambiguity for new stamps.
- Parents are separate entities with their own buckets: no quota allowance moves
  (ADR-145), and no lease is rewritten.

**Negative:**
- A move is O(buckets of the entity) writes, not O(1), like a cascade-policy change.
- Until the fan-out reaches a shard, acquires on it debit the old parent; a pre-0.17
  process that has not reopened the stack still debits it once per (process, entity).
- A rejection cached before the move can reject a child against the old parent's state
  for up to `rejection_cache_ttl` in another process (ADR-147); never an admission.
- The ancestor check costs one strongly consistent read per level of the new parent's
  chain.
- The rf-locked write gains one condition term; losing it costs that acquire the
  consumption-only retry (one extra write), only in the move's race window.

## Alternatives Considered

### Expire the entity cache on a TTL
Rejected: it bounds staleness by the TTL on every entity at the cost of periodic META
reads, where the item already returns the answer for free on every call.

### Make the slow path read META strongly consistently
Rejected: it doubles that read on every slow pass and still lets a pass that read before
the move write after the fan-out.

### Delay the fan-out's last pass past the replication window
Rejected: a guess about timing, not a guarantee; a slow pass can outlast any fixed delay.

### Delete and recreate the entity under the new parent
Rejected: it loses the entity's config, bucket balances and audit history.

### Move the old parent's consumption to the new parent
Rejected: usage is history; a parent's buckets belong to the parent, and moving tokens
between parents would breach ADR-145's conservation rules for quotas.
