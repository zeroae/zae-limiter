# ADR-139: Duration reset windows anchored to first use

**Status:** Proposed
**Date:** 2026-09-15
**Issue:** [#597](https://github.com/zeroae/zae-limiter/issues/597)
**Related:** ADR-137, ADR-138, [ADR-140](140-duration-window-shard-coherence.md), [ADR-141](141-reset-after-version-gate.md)

## Context

ADR-137 gives a limit two ways to recover: a drip, or a reset that restores the balance in one
lump. ADR-138 decided that a reset is named by a cron expression, so every entity sharing a
schedule resets at the same wall-clock instant. That is what a billing period needs.

A session cap needs the other shape: "10,000 tokens, and your window resets five hours after you
first used it". Two entities that first call at 09:00 and 14:30 have windows ending at 14:00 and
19:30. This is the behaviour of widely deployed allowance systems, and a reader who sees
"five-hour window" will assume it. A duration cannot be written in cron, so it needs a second
field rather than a second reading of the first. Feasibility, costing and the full alternatives
are in `docs/plans/2026-09-15-rolling-session-windows-analysis.md` and
`docs/plans/2026-09-15-session-quotas-plan.md`.

This record lifts ADR-138's deferral of duration-based windows. ADR-138's own decision — that
`reset_schedule` names only fixed calendar windows — is unaffected: this is a new mechanism, not
a reinterpretation of `reset_schedule`. How an entity's shards agree on one window is
[ADR-140](140-duration-window-shard-coherence.md); when such a limit may be stored at all is
[ADR-141](141-reset-after-version-gate.md).

## Decision

A limit may carry `reset_after`, a duration quota (ADR-137) that must not also carry
`reset_schedule`, and its balance must be restored whole when its window ends. The window must
open at the entity's first admitted, committed request after the previous window ended
(idle-restarting, never tiling), must be anchored per entity so a cascade parent never follows
its child, and its start must be stored per limit on the bucket item, never derived from `vu`.

## Consequences

**Positive:**
- Entities do not reset simultaneously, which removes the thundering herd ADR-138 records as its
  strongest negative. The spread is a property of the anchor, not of added jitter.
- Evaluation is cheaper than the calendar form: no cron parse, no timezone database, no
  daylight-saving handling and no boundary scan. The reset instant is read off the item.
- The TTL recovery horizon is the window length exactly, with no rounding and no clock.
- A rejected request writes nothing, so hammering an exhausted quota never moves its anchor. The
  one exception is [ADR-145](145-sharded-quota-conserves-allowance.md)'s: a rejected request that
  also moves quota tokens onto a shard it creates or seeds commits that move with nothing consumed,
  and a window it opened is anchored with it — at most once per such shard.
- The per-acquire cost is unchanged; the speculative fast path reads no config.

**Negative:**
- The window is not recoverable from the clock, so losing every shard of a bucket loses it.
  ADR-138 records this as its strongest surviving objection, and idle-restarting answers it: an
  item swept by TTL means the entity was idle for a multiple of the window, so a fresh window on
  the next request is exactly the specified behaviour. Only a purge or manual delete remains,
  which loses the balance too and is not specific to this shape.
- A client or Lambda predating this record cannot read a `reset_after` limit; what that costs and
  how the stack is protected is [ADR-141](141-reset-after-version-gate.md).
- A cascade that creates a parent shard pays for the parent's own window read, because parent
  and child anchors are independent.

## Alternatives Considered

### Duration windows expressed in cron
Rejected because: cron names wall-clock instants and cannot express "five hours after *you*
started".

### One field, interpreted as cron or as a duration depending on its content
Rejected because: it doubles the semantics of every reader behind a value whose meaning is
discovered by parsing it.

### Read the window end off `vu`
Rejected because: `vu` is also the limit-change fan-out's marker and the aggregator's staleness
pin, so every `set_limits()` would read as an elapsed window and restore every balance.

### Tiling windows from a fixed start instant, projected forward in multiples
Rejected because: it needs the same anchor and cannot express "go idle long enough and your
window restarts".

### A per-entity offset into a fixed grid, derived from the entity id
Rejected because: a new entity's first window is whatever fragment of its grid cell remains, and
the boundaries are undiscoverable from the config.

### Anchor on the entity's `#META` record
Rejected because: window state is per (entity, resource, limit), so `#META` would grow unbounded
attributes and become a write hot spot at every rollover.

### Encode the duration as a token inside `rsched`
Rejected because: `decode_reset` rejects unknown tags by design, and a token would make every
stored reset conditionally a cron.
