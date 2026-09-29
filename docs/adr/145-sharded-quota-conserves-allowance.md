# ADR-145: A sharded quota conserves its allowance

**Status:** Proposed
**Date:** 2026-09-28
**Issue:** [#637](https://github.com/zeroae/zae-limiter/issues/637), [#642](https://github.com/zeroae/zae-limiter/issues/642)
**Related:** ADR-133, ADR-134, ADR-137, [ADR-140](140-duration-window-shard-coherence.md), [#587](https://github.com/zeroae/zae-limiter/issues/587), [#477](https://github.com/zeroae/zae-limiter/issues/477)

## Context

A quota (ADR-137) never drips, so on a sharded entity each shard holds a slice of the
period's allowance and nothing refills it before the next reset. When a doubling adds
shards, the allowance must be divided among them without being created or destroyed.

Until now the only record of a shard's slice was its balance. The #587 reclaim clamped
every sibling to the new share and kept one share for the new shard, discarding the
rest (#637: a full, unspent quota of 1000 fell to 187 spendable after a 1→32 walk). A
new or seeded shard was granted a full share unless a sibling visibly held a surplus,
so a share already granted at a lower count and spent was granted again (#642: 1250 and
1300 admitted against 1000). Both follow from one gap: nothing records how much of the
period's allowance each shard was handed.

Balances alone cannot close it. A shard created after a reset is owed its own share,
while a shard split off a sibling granted at a lower count is owed only what that
sibling still holds; the two look identical in the tokens.

## Decision

Every quota shard must record the shard count its current-period grant was sized at,
and a new or seeded quota shard must be funded by an atomic move from the
current-period sibling whose grant covers its slot, receiving fresh allowance only
when no such sibling exists.

## Consequences

**Positive:**
- A doubling neither creates nor destroys quota allowance: per period, admitted plus
  held plus still-grantable equals the configured capacity, apart from the bounded
  residuals below.
- #637 and #642 close together, including the aggregator's proactive clone.
- The speculative fast path is untouched.

**Negative:**
- A quota shard's ceiling is its grant, not `capacity // shard_count`, so balances
  across shards can be uneven, and a draw on an empty shard can reject while the
  entity holds tokens elsewhere, until the next reset.
- A shard creation that moves tokens is a transaction, and can conflict with writes
  on a busy donor.
- A create is not pinned to the count it planned at: a doubling that lands before its write
  cannot reach a shard that does not exist yet. The creator reads shard 0's count after the
  write and raises the new shard (1 RCU per quota shard creation), so it never resets at the
  stale count, but within that period one new share can be granted twice, once.
- A shard written before the record exists is read as granted at its stored count (or a lower
  one its balance implies), so #642's residual survives at most one period after upgrade. Within
  that period a count raise that does not also record the old grant size — the client's plain
  doubling propagation, the aggregator's when it cannot read the siblings, or any older writer —
  can make such a shard read as covering fewer slots than it was granted for, and a later shard
  can then be granted a slot it still holds tokens for: up to one old share, once, until its next
  reset records the grant.
- A rejected request that moves tokens onto a shard it creates or seeds commits the move with
  nothing consumed, so the tokens are never lost; if it also opened a session window
  ([ADR-139](139-duration-reset-windows.md)), that window is anchored by a rejected request.
- The aggregator does not pre-create a quota shard whose funding it cannot size safely from the
  record it holds; the client creates that shard on first use.
- The rule applies to the `divided` sharding regime; the choice of regime is #477.

## Alternatives Considered

### Full capacity per shard, refill divided
Rejected: burst after idle reaches `shard_count × capacity`, and a quota would grant that per period.

### Borrow tokens from a sibling on rejection
Rejected: a genuinely exhausted entity would lose its free fast rejection; it belongs to #477.

### A grant amount with no holding item
Rejected: two concurrent creators both claim the same unheld remainder.

### One per-entity allowance item
Rejected: a write per shard creation on one hot item reintroduces the partition limit sharding avoids.
