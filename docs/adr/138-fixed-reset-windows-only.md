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
**duration** — reset the balance and set the valid-until stamp to `now + interval` — needs no
anchor field at all, because the valid-until stamp *is* the anchor. It is in fact cheaper than
the calendar form: no cron parsing, no timezone database, no daylight-saving handling, no
boundary scan. An earlier draft of this record rejected per-entity windows on the grounds that
they would require per-entity state the fast path could not compute from the item alone. That
reasoning was wrong: it described a design storing a fixed start instant and projecting
multiples of the interval from it, and did not hold for the sliding form. The decision below
therefore rests on scope, not on feasibility.

## Decision

`reset_schedule` supports fixed calendar windows only. Duration-based windows anchored to an
entity's own activity are deferred to a later release on scope grounds, not excluded as
infeasible, and require their own decision record when taken up.

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
  population this is a thundering herd the current design does nothing to spread. The deferred
  duration form does not have this property at all, which is the strongest argument for taking
  it up.
- The nearest common real-world behaviour is the one excluded, so the limitation must be stated
  explicitly in the user guide rather than left to be discovered.
- A caller wanting per-entity windows has no partial path: the feature is absent rather than
  approximate.

## Alternatives Considered

### Duration-based windows anchored to the entity, in this release
Deferred, not rejected: it is a second refill mechanism with its own configuration surface,
storage and documentation, and #222 is already long. It remains the answer to the thundering
herd noted above.

### Per-entity window storing a fixed start instant and projecting intervals from it
Rejected because: it needs an anchor field the sliding `now + interval` form does not, for no
behaviour the sliding form lacks.

### Derive an anchor from the entity identifier to stagger resets
Rejected because: it silently gives entities different allowance boundaries than their
configuration states, and the drift is undiscoverable from the config.

### Support both and select per limit
Rejected because: it doubles the semantics of every reader — config, manifest, CLI, aggregator
and client — for a behaviour that has not been asked for.
