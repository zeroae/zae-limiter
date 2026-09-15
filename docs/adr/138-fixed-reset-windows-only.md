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

The two are not variants of one feature. A per-entity window requires an anchor timestamp
stored on each bucket and compared per entity, whereas a calendar window is a pure function of
the clock. The distinction matters for cost as well as storage: the fast path gates on a single
valid-until stamp that any writer can compute from the bucket item alone, and per-entity
anchoring would make that stamp depend on state the aggregator would have to read and maintain
separately.

## Decision

`reset_schedule` supports fixed calendar windows only. Windows anchored to an entity's own
activity are out of scope for this feature, and adding them requires a separate decision record
and a separate mechanism.

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
  population this is a thundering herd the current design does nothing to spread.
- The nearest common real-world behaviour is the one excluded, so the limitation must be stated
  explicitly in the user guide rather than left to be discovered.
- A caller wanting per-entity windows has no partial path: the feature is absent rather than
  approximate.

## Alternatives Considered

### Per-entity anchor timestamp on the bucket item
Rejected because: it makes the valid-until stamp depend on per-entity state, which removes the
property that any writer can compute it from the item alone.

### Derive an anchor from the entity identifier to stagger resets
Rejected because: it silently gives entities different allowance boundaries than their
configuration states, and the drift is undiscoverable from the config.

### Support both and select per limit
Rejected because: it doubles the semantics of every reader — config, manifest, CLI, aggregator
and client — for a behaviour that has not been asked for.
