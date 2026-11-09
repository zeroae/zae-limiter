# ADR-154: Fill-instant fast path for dripping limits

**Status:** Proposed
**Date:** 2026-11-07
**Issue:** Refs [#808](https://github.com/zeroae/zae-limiter/issues/808)
**Partially supersedes:** [ADR-115](115-add-based-writes-lazy-refill.md) for dripping limits (refill materialised into `tk` under the shared `rf` lock, and its write paths)
**Amends:** [ADR-002](002-integer-arithmetic-millitokens.md) (a dripping limit's state is a scaled time, still an exact integer), [ADR-114](114-composite-bucket-items.md) (the fill instant replaces `tk` for dripping limits), [ADR-134](134-random-shard-selection.md) (a dripping limit's share on each of `S` shards is exactly `1/S` of its capacity and refill, never floored by `// shard_count`; random selection stands), [ADR-135](135-scheduled-limits.md) (the fast path evaluates the stored schedule on the client), [ADR-147](147-client-side-rejection-cache.md) (an image may choose which conditional write is sent), [ADR-151](151-non-enforcing-limits.md) (on the fast path a soft limit is debited by a separate write after admission, given up after a fixed number of failed writes)
**Related:** [ADR-146](146-per-resource-cascade-policy.md), [ADR-148](148-multi-resource-acquire.md), [ADR-153](153-admission-contract-for-dripping-limits.md), [ADR-155](155-fill-instant-writers.md), [ADR-156](156-dripping-shard-raises.md)
**Design:** [Fill-instant proofs](../contributing/fill-instant-proofs.md) (the bounds, and the behaviour with many consumers)

## Context

A dripping limit's state is equivalent to its fill instant `z`, the moment its share is back
at capacity: the balance at time `t` is `C − r × max(0, z − t)`. A strict admission of
`c ≤ C` tokens at `t` admits iff `z ≤ t + (C − c)/r` and records `max(z, t) + c/r`; the `max`
is how a full bucket forgets the time it sat full. Today's fast path adds `−c` to `tk` and
never moves `rf`, so on a full bucket it records the not-full case and the next materialising
pass credits the forgotten time (#808).

DynamoDB cannot record that `max` in one write: an update may only add or subtract two
operands, set constants, test attribute presence and `ADD`, and it cannot branch. With
attributes affine in the bucket state (`tk`/`rf`, `vu`, a stored fill instant), the recorded
fill instant is affine in the stored state and cannot follow the kink of `max(z, t)` across
`z = t`; a single write that never over-admits must record every admission on a full bucket as
emptying it. Each side alone is exact in one write (if `z ≥ t`, add `c/r`; if `z < t`, set
`t + c/r`), so one write suffices when the client knows the side. With several consumers it
cannot always know: admissions only move `z` later, so an image showing the bucket not full
stays right, but one showing it full can be overtaken.

## Decision

A dripping limit must store, on each bucket shard and in place of `tk`, its own fill instant
scaled by its undivided refill amount in force so that every quantity is an exact integer,
without using the shared `rf` as its refill baseline; every fast-path admission must use, per
hard limit, one of two writes whose condition proves which side of the write's clock reading
`t` that instant lies on (not full: add `c/r` under `t ≤ z ≤ t + (C − c)/r`; full: set
`t + c/r` under `z < t`), pinned as ADR-155 requires and kept beside today's guards, while a
soft limit takes no part and is debited afterwards by the debit pair ADR-155 gives a positive
adjustment, given up after a fixed number of failed writes. The client must not send the full
write when `c` exceeds the per-shard capacity in force (rejecting without a write), predicts each
side from its images and an estimate of the shard's utilisation, retries with the same `t` from
the image each failed write returns, answers a request still undecided after a fixed number of
writes as contended (#724), and never lets an image decide an admission.

## Consequences

**Positive:**
- Meets ADR-153's bound for any number of consumers, in one round trip whenever every limit's
  side is predicted and two when no other write lands in between; today's fallback after a
  failed fast-path write is three. Only a hard limit can reject (ADR-151).
- Nothing refills a dripping limit and no write can lift it above capacity, so the aggregator's
  refill and trim (#317), the self-trimming credit (#721) and #808 go away for these limits.
- No share is divided by the shard count (amending ADR-134's `// shard_count` shares), so a
  heavily sharded entity's per-shard rate can no longer floor to zero.

**Negative:**
- Every hard limit on the write must be predicted, the reserved `wcu` included, from a
  per-shard utilisation estimate kept beside ADR-147's cache. A bucket near full written by many
  processes mispredicts often, a cascade parent most of all; a parent still undecided after the
  cap refunds the child, as a failed parent write does today (ADR-146). In a multi-resource
  acquire (ADR-148) one wrong guess cancels the merged transaction, and the cap counts every
  resource's limits.
- A request whose writes keep being overtaken, which takes tokens returned between them, is
  answered as contended though tokens may remain: an under-admission. A request naming a soft
  limit costs one more write. A soft debit still failing after its fixed number of writes, or
  meeting a backend error, is logged and dropped, its debt and `tc` alike: only reporting (usage
  and the overdraw) is lost, since a soft limit never rejects.
- Every other writer changes with it (ADR-155, ADR-156), and `RepositoryProtocol`'s write
  builders change, which breaks custom backends (ADR-108). An item without a fill instant is
  converted from its `tk` and `rf` on its next slow pass. The schema version moves to 0.11.0
  for ADR-009's tracking, which refuses no 0.x client; with no gate (ADR-153), an older client
  or Lambda would seed `tk` beside a converted item and admit twice.
- At the #570 ceilings the refill amount in force reaches 1e24 millitokens, so a 2100 stamp
  scales to about 4.1e36 against DynamoDB's largest exact integer, just under 1e38. So amounts
  passed to `acquire()`, `consume()`, `adjust()` or `release()` above `MAX_TOKENS` are rejected
  with `ValidationError`, and a value that would still overflow is refused without ever taking
  the `on_unavailable=ALLOW` path, as `PermissionDenied` is (#751).
- Quotas keep `tk` and their resets (ADR-137, ADR-145), so one item can carry both forms.

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

### Store the fill instant as an offset from its expression instant
Rejected for now because: bounded amounts already keep it storable, while an offset needs an
image before any write and one more pin; it is the remedy if the #570 ceilings ever rise.

### A store with a server-side `max`, such as Redis
Rejected because: it replaces the DynamoDB backend rather than fixing it (#149).
