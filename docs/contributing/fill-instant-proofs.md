# Fill-instant fast path: proofs for many consumers

**Status:** Draft, companion to [ADR-154](../adr/154-fill-instant-fast-path.md) and
[ADR-153](../adr/153-admission-contract-for-dripping-limits.md)
**Date:** 2026-11-07

!!! note "A proposed design"
    This page proves properties of the design ADR-154 proposes for v0.18.0. Until that ships,
    the fast path still stores `tk` and `rf`, and #808 still applies.

ADR-153 makes the strict token bucket the admission contract for dripping limits. ADR-154
changes how a dripping limit is stored and written so the fast path can meet it. This document
proves the claims both ADRs rest on, for any number of consumers sharing one bucket shard:

- **§3 Safety.** The two writes never break the contract, whatever the interleaving, beyond a
  bound set by clocks and time in flight.
- **§4 Lower bounds.** One write cannot be exact for every state; other consumers are what
  make that bite; and when consumers return tokens, no exact protocol bounds its writes.
- **§5 Upper bound.** With other consumers only admitting, a request on `L` limits is decided
  within `L + 1` writes (`L + 2` if its first guess may be "not full").
- **§6 Expected cost** with many consumers.

## 1. Definitions

One bucket shard of one dripping limit has capacity `C > 0` and refill rate `r > 0` tokens per
second (its share). A request costs `c` tokens, `0 < c < C`; write `d = c / r`.

**Strict token bucket.** Between admissions the balance grows as
`b(t) = min(C, b(t0) + r × (t − t0))`. A request at `t` is admitted iff `b(t) ≥ c`, and then
`b ← b − c`. Returning `c` tokens sets `b ← min(C, b + c)`.

**Fill instant.** The state is one number `z`, the instant the balance is (or was) back at `C`:
`b(t) = C − r × max(0, z − t)`. Every `z ≤ t` reads as full at `t`. In this form:

- admitting `c` at `t` is allowed iff `z ≤ t + (C − c) / r`, and records `max(z, t) + d`;
- returning `c` records `z − d`, which is exactly `b ← min(C, b + c)`.

The `max` is the clamp: a full bucket forgets how long it has been full. Today's fast path
records `z + d` in every case (it adds `−c` to `tk` and leaves `rf`), which on a full bucket is
earlier than `t + d`. That is #808.

## 2. Model

- **M0. Consumers.** Any number of processes share the item and nothing else; each reads its
  own clock. A request reads its clock once, its **stamp** `t`, and sends writes one after
  another until it is admitted or rejected. Other consumers' writes may land between them.
  DynamoDB applies the writes to one item one at a time, at real instants `T_1 < T_2 < …`, and
  evaluates each condition against the item as the previous writes left it.
- **M1. One write.** Each write is one request (`UpdateItem`, `TransactWriteItems` or a PartiQL
  statement) and costs one round trip. Its condition, update and constants are a function of
  what its consumer knows: the stamp, the cost, its configuration and the images returned to
  it. They are not a function of the item's current values.
- **M2. What a write computes.** The server evaluates the condition `φ` on the item as stored.
  If it holds, it applies the update `U`, computed from the item as it was before the update;
  otherwise it changes nothing and can return the item. A transaction applies all of its
  writes or none, which adds atomicity across items but no choice between updates. A `SET`
  assigns one operand, or the sum or difference of two, where an operand is an attribute, a
  constant or `if_not_exists(attribute, constant)`; `ADD` adds a constant. There is no `max`,
  `min`, multiplication, division or conditional expression. So on the items that share one
  **presence pattern** (one set of attributes present), `U` acts as an affine map
  `x ↦ A x + b` with integer coefficients, fixed by the write.
- **M3. Encoding.** The limit's state is stored in numeric attributes `x` from which the fill
  instant is affine, `z = w · x + w0`, and presence does not depend on the numeric values.
  This holds for `tk`/`rf` (`z = rf + (C − tk) / r`), for a stored fill instant (`z = x`) and
  for every encoding the library has used or proposed.
- **M4. Reachability.** For any interval of fill instants, some line `x0 + λ v` of reachable
  items has fill instants covering it. With `tk`/`rf`, fix `rf` and vary `tk` (debits of
  different sizes); with a stored fill instant, vary it.

ADR-154 stores `z` directly and uses these writes, each with the request's stamp `t`:

| Write | Condition | Update |
|---|---|---|
| Not full, `N_t` | `t ≤ z ≤ t + (C − c) / r` | `z ← z + d` |
| Full, `F_t` | `z < t` | `z ← t + d` |
| Return `c` | none | `z ← z − d` |
| Adjust by `+c` (debt) | none | `z ← z + d` |

**Lemma 1 (exactness).** On an admissible item (`z ≤ t + (C − c) / r`) exactly one of `N_t`,
`F_t` applies, and it records `max(z, t) + d`. On any other item both fail. *Immediate from
the table.*

**Lemma 2 (admissions move `z` later).** `N_t` adds `d > 0`. `F_t` applies only when `z < t`
and sets `t + d > z`. Only a return moves `z` earlier.

Parameters (`C`, `r`, the schedule, the shard count) are fixed throughout §§3–6; §8 covers
changes to them.

## 3. Safety with many consumers

**Theorem 1.** For write `i`, let `o_i = t_i − T_i`: its stamp minus the instant it lands
(clock offset plus time in flight, usually negative). Let `s = max |o_i|`. Then over any real
time window of length `w`, tokens admitted minus tokens returned is at most
`C + r × w + 2 r × s`. With `s = 0` this is ADR-153's bound.

**Proof.** Compare the item with a reference bucket `Y`: a strict bucket fed every applied
write at the instant it landed, allowed to go negative (fill instant `y`, updated by
`y ← max(y, T_i) + d` for an admission and `y ← y ∓ d` for a return or adjustment). Let
`e = z − y`. We show `|e| ≤ s` by induction over applied writes.

- The write that creates the item records `t + d` where `Y` records `T + d`, so `e = o`.
- A return or adjustment moves `z` and `y` by the same amount: `e` is unchanged.
- An admission with offset `o`, by which side each view is on:
  - both not full (`z ≥ t`, `y ≥ T`): both add `d`, so `e` is unchanged;
  - both full: `z' = t + d`, `y' = T + d`, so `e' = o`;
  - item full, `Y` not (`z < t`, `y ≥ T`): `e' = t − y`. From `y ≥ T`, `e' ≤ o`; from
    `y = z − e < t − e`, `e' > e`. So `e'` lies between `e` and `o`;
  - item not full, `Y` full (`z ≥ t`, `y < T`): `e' = z − T`. From `z ≥ t`, `e' ≥ o`; from
    `z = y + e < T + e`, `e' < e`. So again `e'` lies between `e` and `o`.

In every case `|e'| ≤ max(|e|, |o|) ≤ s`.

An admission at `T` passed `z ≤ t + (C − c) / r`, so `y = z − e ≤ T + o − e + (C − c) / r
≤ T + 2s + (C − c) / r`. `Y`'s balance before it is then at least `c − 2 r s`, and after it at
least `−2 r s`. Take any window and the last admission in it. From the window's start to just
after that admission, `Y`'s balance changes by its refill (at most `r × w`), plus what returns
added (at most the tokens returned), minus what admissions and adjustments took. It starts at
most `C` and ends at least `−2 r s`, so admitted minus returned is at most
`C + r × w + 2 r × s`; returns after the last admission only lower the count. ∎

The interleaving does not appear in the bound: every applied write is an exact step on the
item as it finds it (Lemma 1), so there is no lock to lose and no read to go stale. If every
write has the same offset, `e` stays constant, `Y`'s admissions are the strict bucket's shifted
in time, and the excess is zero. Today's design stamps `rf` from the client clock and carries
the same exposure.

## 4. Lower bounds

### 4.1 Theorem 2: one write cannot be exact for every state

Fix `t` and `c`, and let `W = (φ, U)` be any write satisfying M1 and M2. Suppose the items its
consumer cannot rule out include a line whose fill instants cover `[t − a, t + e]`, with
`a > 0` and `0 < e ≤ (C − c) / r`. Then `W` is not exact on that line: either `φ` rejects an
item on it whose request the strict bucket admits, or for some item on it the item `W` leaves
does not encode `max(z, t) + d`.

**Proof.** Suppose `φ` accepts every item on the line and the result is exact. Along the line
`z(λ) = α + β λ` with `β = w · v ≠ 0`. After the write the fill instant is
`z'(λ) = w · (A (x0 + λ v) + b) + w0 = α' + β' λ`, affine in `λ`. Exactness requires
`α' + β' λ = max(α + β λ, t) + d` on the line. The right side has slope `0` where
`z(λ) < t` and slope `β` where `z(λ) > t`, and each part contains an interval. An affine
function has one slope, so `β' = 0` and `β' = β ≠ 0`. Contradiction. ∎

The admission decision is not the obstacle: a condition can test `z ≤ t + (C − c) / r`
exactly. What one write cannot do is record `max(z, t)`.

### 4.2 Corollary 1: a single write that never over-admits wastes the bucket

Relax exactness to safety: the write must record a fill instant no earlier than the true one.
Assume the consumer cannot rule out an idle bucket, so a reachable line covers
`[t − L, t + (C − c) / r]` for every `L` (with `tk`/`rf`, fix `tk` and let `rf` be arbitrarily
old). Along it, write the recorded fill instant as `z' = s z + k`.

- On `[t − L, t]` safety needs `s z + k ≥ t + d`; as `L → ∞` this forces `s ≤ 0`.
- At `z_max = t + (C − c) / r` safety needs `s z_max + k ≥ z_max + d = t + C / r`.
- So at `z = t`: `s t + k = s (t − z_max) + (s z_max + k) ≥ t + C / r`.

A recorded fill instant of `t + C / r` is a balance of `0`, where the strict bucket leaves
`C − c`: every admission on a full bucket is recorded as emptying it. ∎

### 4.3 Theorem 3: what other consumers do to a consumer's knowledge

Let consumer `A` hold an exact image of the item as of some instant, and let another consumer
`B` be able to send writes between that instant and `A`'s write with stamp `t`.

**(a) A "full" image can be overtaken.** If `A`'s image shows `z_A < t`, the items `A` cannot
rule out include a line crossing `t`, so by Theorem 2 no write of `A` is exact on all of them.

*Proof.* `B` may do nothing (the item keeps `z_A < t`), or send one request with stamp
`t_B ∈ (max(z_A, t − d), t)` and cost `c_B ∈ (0, C)`. Its `F_{t_B}` applies, since
`z_A < t_B`, and leaves `z = t_B + c_B / r`. Fixing `t_B` and varying `c_B` gives a line of
reachable items whose fill instants cover `(t_B, t_B + C / r)`, which contains
`[t_B + ε, t + (C − c) / r]` because `t − t_B < d`. That interval crosses `t`. ∎

**(b) A "not full" image cannot be overtaken by admissions.** If `A`'s image shows `z_A ≥ t`
and no consumer returns tokens, then `z ≥ t` when `A`'s write lands (Lemma 2), so `A` knows its
side.

**(c) Returns make the number of writes unbounded.** If other consumers may return tokens
between a request's writes, then for every `k` there is an execution in which an exact
protocol has not decided some request after `k` writes.

*Proof.* Each write `A` sends is fixed before it lands (M1). An exact write never records a
wrong state, so by Theorem 2 it rejects some admissible item on any line crossing `t`. Between
`A`'s writes, `B` can move the item to such an item: an admission moves `z` later by any amount
up to `C / r`, and a return moves it earlier by any amount the returning lease holds, so `B`
can place it at any admissible fill instant near `t`. Repeating this before each of `A`'s
writes leaves `A` undecided after `k` of them. ∎

So with more than one consumer the bound of Theorem 2 is met in practice, not only in
principle: the only uncertainty consumers create is whether a "full" image is still full, and
returns can sustain it indefinitely. An implementation must cap a request's writes and answer
the rest as contended (#724), which under-admits and never over-admits.

## 5. Upper bound

A request covers `L` limits, the reserved `wcu` included. Each write chooses `N` or `F` per
limit from the latest image it holds, reusing the request's stamp `t` on every retry. A fresh
stamp would undo Theorem 3(b): `z ≥ t` does not imply `z ≥ t'` for a later `t'`.

**Theorem 4.** If only admissions land between a request's writes, the request is admitted or
rejected within `L + 2` writes, and within `L + 1` if its first write guesses "full" for every
limit. With no write landing in between, within 2.

**Proof.** A failed write returns the item, so the next write is chosen from the truth at that
failure. If that image shows a limit past its slack (`z > t + (C − c) / r`), Lemma 2 keeps it
there and the request is rejected. Otherwise the next write fails only if some limit the image
showed full has since become not full, or some limit has since passed its slack, which rejects
the request. By Lemma 2 a limit becomes not full at most once, so
after the first write there are at most `L` such failures, and the write after them decides:
`L + 2` in all. If the first write guessed "full" everywhere and failed, every limit it got
wrong was already not full and cannot flip, so at most `L − 1` flips remain: `L + 1`. With no
write in between, the second write is chosen from an exact image and decides. ∎

With `L = 1` the bounds are 2 for full-first and 3 otherwise, and Theorems 2 and 3(a) show 2
cannot be improved. Full-first is optimal in the worst case but guesses wrong whenever the
bucket is not full; §6 is why a predictor that may guess "not full" is usually cheaper.

## 6. Expected cost with many consumers

With one consumer per shard and no returns, its image is exact and it never guesses wrong.
With several and no returns, Theorem 3(b) means an image showing "not full" is always right,
so every wrong guess comes from an image showing "full" that another consumer has overtaken.

With Poisson arrivals at aggregate utilization `u = λ × c / r < 1`, `z − t` behaves like the
work in a single-server queue whose jobs each take `d`. Such a server is busy a fraction `u` of
the time, so a request finds the bucket **not full with probability `u`**; for `u ≥ 1` the
bucket is never full in steady state. A consumer whose image tells it nothing does best by
guessing the likelier side and is wrong with probability `min(u, 1 − u) ≤ 1/2`.

Measured on a model of a 300 / 4 h bucket with 1-token requests: Poisson arrivals, each from a
consumer drawn at random, each consumer predicting from the item its own last write returned,
200,000 requests per row. Every row admitted exactly what the strict bucket admits.

| `u` | Consumers | Image says full iff `z < t` | Always "not full" | Image extrapolated by `u` |
|---|---|---|---|---|
| 0.1 | 4 | 7.4% | 90.0% | 7.7% |
| 0.1 | 32 | 9.7% | 90.0% | 9.7% |
| 0.5 | 4 | 33.2% | 50.2% | 33.3% |
| 0.5 | 32 | 47.5% | 50.2% | 47.1% |
| 0.9 | 4 | 31.9% | 10.3% | 10.9% |
| 0.9 | 32 | 76.6% | 10.3% | 36.0% |

The plain image rule is the worst choice for a busy bucket with many consumers (77% at
`u = 0.9`, 32 consumers): a stale image almost always shows a full bucket. The predictor must
estimate `u` per shard and, when its image shows "full", pick the likelier side.

**Cost.** A wrong guess is one failed conditional write and one round trip. A failed write
consumes 1 WCU of the partition's throughput but applies nothing, so it does not drain the
reserved `wcu` budget. A write fails if any of its `L` limits is guessed wrong, so the rates
compound; `wcu` is usually full (its `u` is a shard's writes per second divided by 1000) and
adds little. With failure rate `p` per write, an admission costs about `1 + p` round trips and
`1 + p` WCU and no RCU. At `p ≈ 1/2` that is 1.5 round trips and 1.5 WCU: fewer round trips
than reading before every write (2), but more write capacity than the read costs. One consumer
per shard, or a better predictor, moves the cost toward 1.

## 7. Returns, debt and the aggregator

A return of `c` adds `−d` to `z` with no condition and gives exactly `b ← min(C, b + c)`: on a
full bucket it moves `z` further into the past, which still reads as full. No write can lift a
balance above `C`, so the ceiling checks of #721 have nothing left to do for dripping limits.
An adjustment by `+c` adds `d` with no condition and may push `z` past `t + C / r`, which is
debt. Neither is idempotent (#731).

Nothing refills a dripping limit, so the aggregator writes no refill or trim for one. It still
counts usage and shards hot entities.

## 8. Changes of capacity or rate

`z` is expressed against the shard's `C` and `r`. A configuration change, a schedule boundary
or a shard-count raise changes them, so `z` must be re-expressed, from the balance it implies,
in a write that pins the old parameters, before any fast write uses the new ones. Fast writes
pin the parameters they were computed from, so one computed before a change fails after it.

## 9. What this does not cover

- **An encoding where attribute presence tracks time.** `if_not_exists` is the only update that
  depends on anything but arithmetic, presence changes only on writes, and the client chooses
  attribute names before it sees the item. M3 excludes such encodings; none is known that works
  with a bounded number of attributes.
- **A store with a server-side `max` or branching**, such as GCRA in Redis/Lua or SQL
  `GREATEST`. One round trip is exact there; the bound is a property of DynamoDB's update
  language.
- **Quotas.** A quota does not drip; it keeps `tk` and its resets (ADR-137, ADR-145).
