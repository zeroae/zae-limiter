# ADR-138: Calendar reset covers fixed windows only

**Status:** Accepted
**Date:** 2026-09-15
**Issue:** [#222](https://github.com/zeroae/zae-limiter/issues/222)

## Context

`reset_schedule` (design §3.6) is expressed as a cron expression, which names instants on a
wall clock. Every entity sharing a schedule therefore resets at the same moments: an expression
meaning "every five hours" resets at 00:00, 05:00, 10:00 and so on, identically for all.

A second and superficially similar product exists: a window anchored to each entity's own first
use, so one caller's five hours begin when that caller begins and another's begin later. Widely
deployed allowance systems work this way, including the session limits that motivated
`reset_schedule` in the first place. A reader who sees "five-hour reset window" will reasonably
assume this behaviour.

The two express different products. A calendar window is what a billing period needs — "10,000
per calendar month" is a contractual statement about the calendar, not about the caller. A
per-entity window is what a session cap needs. Real services ship both, often together.

They are not, however, hard to build in the same way. A per-entity window expressed as a
**duration** is shallower than the calendar form on evaluation — no cron parsing, no timezone
database, no daylight-saving handling, no boundary scan, and a recovery horizon that is the
window length exactly — but it is not cheaper overall, because it costs more in storage, in
shard coherence (the calendar form gets that free, since every shard shares one clock and one
cron, whereas a window start has to be propagated across an entity's shards) and in durability.
It also needs an anchor of its own, and that anchor cannot be the valid-until stamp: beyond
gating the fast path, `vu` carries the marker a limit-change fan-out stamps and the staleness
pin the aggregator adds to its refill condition, and the calendar form escapes that collision
only because its reset decision is taken from the cron and the last-refill stamp, never from
`vu`. A duration window reading its expiry off `vu` would read every fan-out as an elapsed
window, so every `set_limits()` call would silently restore every caller's balance and restart
every caller's clock. Two earlier drafts of this record misplaced that anchor in opposite
directions — state the item could not carry, then no anchor at all — and both were wrong in the
same place: one per-limit attribute carries it, cheap but not free and not `vu`. The decision
below therefore rests on scope, not on feasibility, although the durability asymmetry recorded
under Consequences is the argument that survives closest scrutiny and is what a later record
will have to answer.

## Decision

`reset_schedule` supports fixed calendar windows only. A window anchored to an entity's own
activity is a different mechanism rather than a competing answer to the same question, and is
recorded separately in [ADR-139](139-duration-reset-windows.md) as `Limit.reset_after`. A limit
carries one or the other and never both (ADR-137).

## Consequences

**Positive:**
- The valid-until stamp stays computable from the bucket item alone, which is what keeps the
  fast path at one comparison and no extra read.
- A schedule means the same thing wherever it is read — config, manifest, CLI or item — with no
  per-entity interpretation.
- Missed edges remain idempotent, because a calendar edge is recoverable from the clock while a
  per-entity anchor would have to survive item expiry.

**Negative:**
- Every entity resets simultaneously, concentrating load at the boundary. For a large tenant
  population this is a thundering herd a calendar expression cannot spread. The duration form
  (ADR-139) does not have this property at all, which is the argument that carried it.
- The nearest common real-world behaviour is the one excluded, so the limitation must be stated
  explicitly in the user guide rather than left to be discovered.
- A caller wanting per-entity windows reaches for `reset_after` (ADR-139) rather than for an
  approximation of one in cron.

## Alternatives Considered

### Duration-based windows anchored to the entity, expressed in cron
Rejected because: cron names instants on a wall clock, so it cannot express "five hours after
*you* started". The duration form is a second field on `Limit` rather than a second reading of
this one — see ADR-139.

### Per-entity window storing a fixed start instant and projecting intervals from it
Rejected because: it needs the same anchor the duration form does, and still cannot express "go
idle long enough and your window restarts", which is what anchoring to the entity is for.

### Derive an anchor from the entity identifier to stagger resets
Rejected because: it silently gives entities different allowance boundaries than their
configuration states, and the drift is undiscoverable from the config.

### One field, interpreted as cron or as a duration depending on its content
Rejected because: it doubles the semantics of every reader — config, manifest, CLI, aggregator
and client — behind a value whose meaning is discovered by parsing it. Two fields that are
mutually exclusive at construction (ADR-137, ADR-139) give the same expressiveness and are
checked once, at the boundary.
