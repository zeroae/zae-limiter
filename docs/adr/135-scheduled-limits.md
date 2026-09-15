# ADR-135: Scheduled limits are resolved at read time

**Status:** Proposed
**Date:** 2026-09-15
**Issue:** [#222](https://github.com/zeroae/zae-limiter/issues/222)

## Context

A limit holds the value it was last given until someone calls `set_limits()`. A deployment that
wants capacity to vary by time of day, day of week or calendar period has one option today: an
out-of-tree cron job that swaps the numbers at each tick. That job is infrastructure the library
does not know about, it has no atomicity against concurrent traffic, and at every boundary there
is a window in which the superseded limit is still enforced. Moving the same job in-stack — an
EventBridge rule driving the provisioner to rewrite affected buckets at each tick — relocates the
problem rather than solving it: the cost is O(buckets) writes per tick per scheduled resource,
and boundary precision is bounded by scheduler and Lambda latency.

The constraint that decides the shape is the speculative fast path (ADR-115): the common
`acquire()` is a single conditional `UpdateItem` that reads no configuration and runs none of
this project's code. Any design requiring the client to know a schedule in order to admit a
request surrenders that, and with it the advertised per-request cost.

A schedule also has to answer a second question the token bucket cannot express at any
configuration — when a calendar allowance returns in one lump rather than dripping back. What
that reset means for the refill rate is settled by ADR-137, and what kind of window a reset may
name is settled by ADR-138. This record does not restate either.

## Decision

A limit carries its own cron schedule; the schedule is denormalized onto the bucket item, and
every refiller resolves the base parameters and the schedule to effective parameters at read
time, materializing only the token balance. The fast path evaluates no schedule and is gated
solely by an item-level valid-until stamp naming the earliest instant at which any limit on that
item changes effective parameters.

## Consequences

**Positive:**

- Steady-state cost is unchanged: the fast-path condition gains one comparison against an
  attribute that is absent on every unscheduled bucket, and still reads no configuration.
- Client and aggregator derive the same effective value from the item alone, so the two never
  need to agree on anything beyond the clock.
- Boundary cost is paid once per in-flight request at the boundary, not once per bucket, and an
  idle bucket costs nothing until it is next used.
- Base parameters stay on the item, so actor-written changes (`set_limits`, the manifest
  provisioner, future adaptive and webhook paths) and the schedule compose without ordering:
  an actor writes the base, the schedule transforms it.
- A calendar allowance is expressible without deleting bucket items, so the total-consumed
  counter stays monotonic and usage aggregation is unaffected.

**Negative:**

- A boundary takes effect on the first request after it, not at the instant itself. An idle
  bucket re-materializes when it wakes, so a display reading the item directly can be stale.
- A resource- or system-level schedule change reaches existing buckets only when their TTL
  expires and they are recreated, because re-materialization reads the item's own stamp and
  never configuration. Entity-level changes fan out immediately.
- At most one read-time function may apply to a bucket, which forecloses combining a schedule
  with a future ramp-up or utilization-adaptive limit on the same bucket. The rule is validated
  at configuration-write time only; a writer going directly to the table can defeat it.
- Timezone-correct evaluation obliges the Lambda packages to carry the `tzdata` wheel, since the
  runtime image is not guaranteed to ship a zoneinfo database.
- The valid-until stamp is capped at a bounded forward scan, so a schedule whose next boundary is
  beyond the cap costs one slow-path pass per active bucket per cap period even when nothing
  changed.

## Alternatives Considered

### EventBridge-driven materialization of limits at each boundary
Rejected because: it costs O(buckets) writes per tick per scheduled resource and bounds boundary
precision by scheduler latency, which is the out-of-tree cron job moved in-stack.

### Schedule stored as its own item, referenced by identifier from buckets
Rejected because: the aggregator would need an extra read per refill, and "which schedule
applies" becomes a second resolution walk beside the existing config hierarchy.

### Client resolves the schedule from cached config; the item stores no schedule
Rejected because: the aggregator keeps refilling from stale item parameters until a client slow
path syncs them, so a reduction over-refills on the fast path for up to a refill window.

### Materialize effective capacity and refill amount onto the bucket item
Rejected because: the fast path never reads them, so it buys nothing, and overwriting the base
destroys the only copy needed to compute the next window.

### Cron entries read as state transitions rather than as match patterns
Rejected because: the active entry then depends on firing history, so a clock skew across a tick
lets the client and the aggregator disagree about the limit in force.

### One namespace-level timezone, or UTC only
Rejected because: a multi-tenant namespace spans regions, and hand-converting local business
hours to UTC drifts by an hour twice a year.

### Clearing an allowance by deleting the bucket item (#471)
Rejected because: it discards the total-consumed counter with the item, and usage aggregation
derives consumption from that counter's deltas.
