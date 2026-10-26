# ADR-150: Move an entity to a new parent

**Status:** Accepted
**Date:** 2026-10-09
**Issue:** [#677](https://github.com/zeroae/zae-limiter/issues/677)
**Amends:** CLAUDE.md invariant 9 (`parent_id` set once at `create_entity()` and never
changed); [ADR-115](115-add-based-writes-lazy-refill.md) and
[ADR-133](133-client-shard-creation.md) (a bucket create is a transaction with a
`ConditionCheck` on the owner's META, 4 WCU, not a single `PutItem`, and a cancelled check
is a second cause of a lost create); [ADR-146](146-per-resource-cascade-policy.md) (a
slow-path owner stamp older than the stored generation is not written)
**Related:** [ADR-112](112-cascade-per-entity.md), [ADR-125](125-resource-disable.md), [ADR-141](141-reset-after-version-gate.md), [ADR-145](145-sharded-quota-conserves-allowance.md), [ADR-146](146-per-resource-cascade-policy.md), [ADR-147](147-client-side-rejection-cache.md), [#674](https://github.com/zeroae/zae-limiter/issues/674), [#684](https://github.com/zeroae/zae-limiter/issues/684), [#686](https://github.com/zeroae/zae-limiter/issues/686)
**Design:** `docs/plans/2026-10-09-move-entity-design.md`

## Context

An entity's `parent_id` is written once, by `create_entity()`, onto its META record. Since
#684 every bucket item the entity owns carries the same `parent_id` beside the resolved
cascade policy (ADR-146), and the fast path debits whichever parent the item, or the
process's entity cache, names — with no META read. CLAUDE.md invariant 9 states that this
metadata never changes, and the entity cache has no expiry. The use case in #674 needs a user
moved between organisations, effective at once.

ADR-146 already built most of what a move needs: an eager, two-pass fan-out that restamps an
entity's buckets from strongly consistent reads. What it lacks is protection against stale
writers: a slow pass, a bucket create or an aggregator shard clone that read META before the
move can land after the fan-out and write the old parent back onto a bucket that may then
take only the fast path, where nothing would ever correct it.

## Decision

An entity's parent must be changeable through one repository operation that updates its META record and restamps every bucket it owns through the ADR-146 cascade fan-out. Every write of `parent_id` to a bucket item must carry the parent generation and entity incarnation it read and must not overwrite a newer stamp, and a write that creates a bucket item must be pinned to the owner's META in the same transaction, so a stale writer can lose but never undo a move.

## Consequences

**Positive:**
- A move takes effect on the fast path of every process within one call per (process, entity): the child's item overrules a stale cache, and the old parent's debit is refunded.
- No consumption or quota allowance moves between parents (ADR-145), and no lease is rewritten; details and costs are in the design doc.

**Negative:**
- A move is O(buckets of the entity) writes, not O(1), like a cascade-policy change. ADR-114
  rejected merging META into buckets partly to avoid fan-out on metadata changes; denormalising
  `parent_id` for a 0-RCU fast path makes this one metadata change pay that fan-out.
- `set_parent` is a required `RepositoryProtocol` member, not capability-gated (ADR-109).
- Until the fan-out reaches a shard, acquires on it debit the old parent.
- Every bucket create becomes a transaction with one extra item, a `ConditionCheck` on the owner's META, which needs `dynamodb:ConditionCheckItem`: breaking for custom IAM.
- Residual (GSI3 lag): a create that lands before the move's META write but is missed by both discovery passes keeps the old parent until its next slow pass or a repeat of the move.
- Residual (recreate race): a bucket left over from a deleted entity, written by a slow pass whose eventually consistent META read still returns the predecessor within about a second of the entity's re-creation, keeps the predecessor's parent until the next slow pass reads the new incarnation.
- Residual (leftover after a delete): a bucket the delete's GSI3 discovery missed keeps the deleted entity's parent stamp, and `create_entity` does not fan out, so the recreated entity's fast-path writes on that bucket debit the old parent until its next slow pass claims the bucket for the new incarnation.
- Residual (fan-out across a recreate): a move's fan-out reads META once, then may overwrite another incarnation's stamp; a fan-out still running when the entity is deleted and recreated can stamp the old incarnation's parent on a new bucket, until that bucket's next slow pass.
- Residual (ADR-147): a rejection cached before the move can reject a child against the old parent's state for up to `rejection_cache_ttl` in another process; never an admission.
- Until it reopens the stack, a pre-0.17 process debits the old parent on every warm-path call, and its unpinned slow-pass stamp can restore the old parent from a stale read; the version ratchet refuses only its next open. Restart every client after upgrading, before the first move.

## Alternatives Considered

### Expire the entity cache on a TTL
Rejected: periodic META reads on every hot entity for a value the item already returns for free.

### Make the slow path read META strongly consistently
Rejected: it doubles that read on every slow pass and still lets a pass that read before the move write after the fan-out.

### Delay the fan-out's last pass past the replication window
Rejected: a guess about timing, not a guarantee; a slow pass can outlast any fixed delay.

### Delete and recreate the entity under the new parent
Rejected: it loses the entity's config, bucket balances and audit history.

### Move the old parent's consumption to the new parent
Rejected: usage is history, and moving tokens between parents would breach ADR-145's conservation rules for quotas.

### Re-check each create against META after it lands
Rejected: a background check per create needs joining, cancellation handling and a `vu = 0` fallback, and still leaves a window in which other processes debit the old parent.
