# ADR-140: Shards of one entity share one duration window

**Status:** Accepted
**Date:** 2026-09-27
**Issue:** [#624](https://github.com/zeroae/zae-limiter/issues/624), [#625](https://github.com/zeroae/zae-limiter/issues/625), [#635](https://github.com/zeroae/zae-limiter/issues/635), [#640](https://github.com/zeroae/zae-limiter/issues/640)
**Related:** [ADR-139](139-duration-reset-windows.md), [ADR-142](142-hide-reset-after-config.md), ADR-133, ADR-134, [#597](https://github.com/zeroae/zae-limiter/issues/597)

## Context

A duration window ([ADR-139](139-duration-reset-windows.md)) is anchored to an entity, but a
sharded entity's balance lives on up to `MAX_SHARD_COUNT` bucket items, each refilled and reset
by whichever writer reaches it (ADR-133, ADR-134). A calendar reset gets shard coherence free,
because every shard reads the same clock and the same cron. A window start is state, so an
entity's shards either agree on it by propagation or each keep their own.

Per-shard windows would not over-admit — staggered shards redistribute *when* the allowance
arrives without loosening the ceiling — but they make the reset instant unanswerable: any single
value reported to a caller either over- or under-promises, and for a session cap shown to a
human that instant is the product. Carrying the balance across shards is not an option either:
token deltas are always additive so they commute with speculative writes, and a fan-out knows
no sibling's balance, so a blind set would either clobber committed consumption or double a
share (the #587 finding).

Two further hazards shape the rule. Two clients crossing a window boundary together each open a
window on their own shard, and a naive propagation lets each reset the other inside one window.
And a writer whose clock runs behind can move a shard's last-refill stamp `rf` backward, after
which the shard would believe a window it already applied is new (#635). Pre-v0.15 clients
writing windowed buckets ([ADR-142](142-hide-reset-after-config.md)) stamp `rf` from their own
clock and drop the fast path's `vu` gate, so `rf` cannot record a reset, and a shard can spend an
ended window's leftover inside the next one before its reset (mechanism: CLAUDE.md).

## Decision

Every shard of an entity must converge on one window start: the opener must propagate only the
start and a snapshot `wtc` of the sibling's consumption counter, never tokens, and only to
siblings whose own window had ended and that carry a window-applied marker `wa` or an `rf` older
than the new start; each shard must record in `wa` the start its balance reflects, must reset
when its start is newer than `wa` (than `rf` where no `wa` exists) to its current share,
recorded as its grant ([ADR-145](145-sharded-quota-conserves-allowance.md)), less the consumption
since `wtc` and never above that share, and no materialising writer may move `rf` backward. A
shard created mid-window must join shard 0's live window, read strongly consistently, and may
open and propagate its own only when shard 0's window has ended or it has none.

## Consequences

**Positive:**
- `check_availability` and `RateLimitExceeded` can report one honest reset instant per entity.
- A rollover never decrements one shard's balance to fund another's, so the #587 over-admission
  cannot recur through it. Funding a shard *created* mid-window is a separate, atomic move off the
  shard whose grant covers it ([ADR-145](145-sharded-quota-conserves-allowance.md)).
- The reset is a set, not an add: idempotent, one reset however many windows an idle shard slept
  through, and the consumption counter `tc` stays monotonic.
- Monotonic `rf` costs nothing on items without a window, since refill treats non-positive
  elapsed time as zero.
- Unsharded entities pay nothing extra.
- A shard resets exactly once per window whatever an old writer does to `rf`, and a late reset
  charges everything spent while it was pending, so a mixed fleet cannot over-admit through it.

**Negative:**
- A rollover costs one conditional write per (sibling shard, window limit), once per window.
- Shards can be staggered by milliseconds after concurrent openers, and a lost propagation write
  leaves one shard offset until the next rollover. Neither over-admits, but no reader may assume
  every shard carries an identical window start.
- Creating a windowed shard costs a strongly consistent read, because a stale pre-roll start
  looks ended and would mint a full share on top of the window just opened.
- The aggregator's refill condition grows one term per rolled window start, because two
  propagations are indistinguishable to its `rf` and `vu` pins alone.
- Two more small attributes per window limit per shard; an item without `wa` relies on `rf`.

## Alternatives Considered

### Each shard keeps its own window, with no propagation
Rejected because: the entity loses any single reset instant a caller can be told.

### Propagate the token balance with the window start
Rejected because: a fan-out cannot add without knowing each balance, and a blind set is unsafe
in both write orderings.

### Move a sibling whenever the new start is later than its own
Rejected because: concurrent openers then reset each other's shard inside one window, which
over-admits.

### Keep `rf` as the only record that a window was applied
Rejected because: a pre-v0.15 writer's clock then rolls a window twice or never.

### Zero the ended window's leftover when propagating the start
Rejected because: debt from `adjust()` after a zero-estimate lease is still forgiven by the
reset, and a clone of a shard with a pending reset is never propagated to.

### Stamp `rf` from the writer's own clock
Rejected because: a slow clock moves `rf` backward past the window start and re-applies the reset
on every request from that clock.

### Store the window end rather than the start
Rejected because: the start is the monotonic half, which is what makes the propagation condition
idempotent.

### Redistribute balance across shards instead of transferring it
Rejected because: admission gates on per-shard balance, and a quota's debt is never repaid. The
transfer that replaced it is ADR-145's move.
