# ADR-154: Fill-instant fast path for dripping limits

**Status:** Proposed
**Date:** 2026-11-07
**Issue:** Refs [#808](https://github.com/zeroae/zae-limiter/issues/808)
**Related:** ADR-114, ADR-115, ADR-133, ADR-135, ADR-147, [ADR-153](153-admission-contract-for-dripping-limits.md)
**Design:** [Fill-instant proofs](../contributing/fill-instant-proofs.md) (the bounds, and the behaviour with many consumers)

## Context

A dripping limit's state is equivalent to its fill instant `z`, the moment its share is back
at capacity: the balance at time `t` is `C − r × max(0, z − t)`. A strict admission of `c`
tokens at `t` admits iff `z ≤ t + (C − c)/r` and records `max(z, t) + c/r`; the `max` is how a
full bucket forgets the time it sat full. Today's fast path adds `−c` to `tk` under `tk ≥ c`
and never moves `rf`, so on a full bucket it records the not-full case, and the next
materialising pass credits the forgotten time (#808).

DynamoDB cannot record that `max` in one write. An update may only add or subtract two
operands, set constants, test attribute presence and `ADD`, and it cannot branch between
updates. With attributes affine in the bucket state, which covers `tk`/`rf`, `vu` and a stored
fill instant, the recorded fill instant is then affine in the stored state, so it cannot follow
the kink of `max(z, t)` across states on both sides of `z = t`. A single write that never
over-admits must therefore record every admission on a full bucket as emptying it.

Each side alone is exact in one write: if `z ≥ t`, add `c/r` to `z`; if `z < t`, set `z` to
`t + c/r`. One write therefore suffices whenever the client knows which side of now `z` lies
on. With several consumers it cannot always know: admissions only move `z` later, so an image
showing the bucket not full stays right, but one showing it full can be overtaken. A failed
conditional write returns the item, so while others only admit a request is decided within a
bounded number of writes; tokens returned between its writes can delay it indefinitely.
Today's attributes cannot express the full-side write: `rf` is shared by every limit on the
item, and a condition can only compare stored values, so it cannot test
`tk + r × (t − rf) ≥ C`.

## Decision

A dripping limit must store its own fill instant on each bucket shard in place of `tk`, without
using the shared `rf` as its refill baseline, and every fast-path admission must use, per limit,
one of two writes whose condition proves which side of now that instant lies on: the not-full
write (add `c/r` under `t ≤ z ≤ t + (C − c)/r`) or the full write (set `t + c/r` under
`z < t`), each pinned on the stored parameters, schedule and shard count it was computed from.
The client chooses each limit's write from its last image of the shard, retries with the same
clock reading from the image each failed write returns, and answers a request still undecided
after a fixed number of writes as contended (#724); no image may decide an admission.

## Consequences

**Positive:**
- Meets ADR-153's strict bound for any number of consumers, in one round trip whenever every
  limit's side is predicted and two when no other write lands in between; today's fallback
  after a failed fast-path write is three.
- Nothing refills a dripping limit, and no write can lift it above capacity: the aggregator's
  refill and trim (#317), the self-trimming credit (#721) and #808 go away for these limits.
- Every admission is still decided by a condition on the item; an image only chooses the write.

**Negative:**
- Every limit on the write must be predicted, the reserved `wcu` included. A caller whose
  bucket hovers at full and is served from many processes mispredicts often and pays the second
  write often.
- A change to a limit's capacity or rate (configuration, a schedule boundary, a shard-count
  raise) must re-express its fill instant in a materialising pass before the fast path resumes,
  as `vu` already forces.
- The fast path computes `c/r` and its slack from the shard's parameters, so a stale image or
  configuration costs one retry per process and shard.
- The schema changes: partially supersedes ADR-115 (refill materialised into `tk` under the
  shared `rf` lock, and its four write paths) for dripping limits, and amends ADR-114's field
  set. An item without a fill instant is converted from its `tk` and `rf` on its next slow
  pass. There is no version gate: every client and Lambda sharing a table must run v0.18.0 or
  later, since an older one would seed `tk` beside a converted fill instant and admit twice.
- A request whose writes keep being overtaken, which takes tokens returned between them, is
  answered as contended though tokens may remain: an under-admission.
- Quotas keep `tk` and their resets (ADR-137, ADR-145), so one bucket item can carry both
  representations.
- Amends ADR-147: an image may also choose which conditional write is sent.

## Alternatives Considered

### Gate today's fast path on the instant the bucket could be full
Rejected because: a caller below its limit refills to full between requests, so it would take
the slow path on every request.

### Keep `tk` and `rf` and add a full-side write
Rejected because: `rf` is shared by every limit on the item, and a condition cannot test
whether `tk + r × (t − rf)` has reached capacity.

### Read before every write
Rejected because: it costs two round trips on every request, where this design costs two only
on a misprediction.

### One write per admission that records conservatively
Rejected because: it must record every admission on a full bucket as emptying it.

### A store with a server-side `max`, such as Redis
Rejected because: it replaces the DynamoDB backend rather than fixing it; other backends are
separate work (#149).
