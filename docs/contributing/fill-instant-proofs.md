# Fill-instant fast path: proofs for many consumers

**Status:** Draft, companion to [ADR-153](../adr/153-admission-contract-for-dripping-limits.md),
[ADR-154](../adr/154-fill-instant-fast-path.md), [ADR-155](../adr/155-fill-instant-writers.md)
and [ADR-156](../adr/156-dripping-shard-raises.md)
**Date:** 2026-11-07

!!! note "A proposed design"
    This page proves properties of the design ADR-154, ADR-155 and ADR-156 propose for v0.18.0.
    Until that ships, the fast path still stores `tk` and `rf`, and #808 still applies.

ADR-153 makes the strict token bucket the admission contract for dripping limits. ADR-154
changes how a dripping limit is stored and written so the fast path can meet it, ADR-155 holds
every other writer to exact steps, and ADR-156 governs shard raises. This document proves the
claims those ADRs rest on, for any number of consumers sharing a bucket:

- **§3 Safety.** The two writes never break the contract, whatever the interleaving, beyond a
  bound set by clocks and time in flight.
- **§4 Lower bounds.** One write cannot be exact for every state; other consumers are what
  make that bite; and when consumers return tokens, no exact protocol bounds its writes.
- **§5 Upper bound.** With other consumers only admitting, a request on `L` limits is decided
  within `L + 1` writes (`L + 2` if its first guess may be "not full").
- **§6 Expected cost** with many consumers.
- **§8 Other writers.** Returns, adjustments, resets, top-ups, re-expression across schedule
  windows and shard splits keep the bound, summed over shards.

An independent event-level model (branch `docs/808-fill-instant-review`) checked Theorems 1,
3(c) and 4 and the shard split. It also found three defects in an earlier version of this page
and the ADRs, corrected here: a positive adjustment written as a plain add (§7), a full write
admitting `c > C` (§2), and the ceiling read at the window's start under clock skew (§8).

## 1. Definitions

One bucket shard of one dripping limit has capacity `C > 0` and refill rate `r > 0` tokens per
second (its share). A request costs `c` tokens, `0 < c ≤ C`; write `d = c / r`. A request for
a hard limit with `c > C` can never be admitted, and ADR-154's client rejects it without a
write (§2 says why it must).

**Strict token bucket.** Between admissions the balance grows as
`b(t) = min(C, b(t0) + r × (t − t0))`. A request at `t` is admitted iff `b(t) ≥ c`, and then
`b ← b − c`. Returning `c` tokens sets `b ← min(C, b + c)`. Debiting `c` after the fact (a
positive adjustment) sets `b ← b − c` with no test, so the balance may go negative.

**Fill instant.** The state is one number `z`, the instant the balance is (or was) back at `C`:
`b(t) = C − r × max(0, z − t)`. Every `z ≤ t` reads as full at `t`. In this form:

- admitting `c` at `t` is allowed iff `z ≤ t + (C − c) / r`, and records `max(z, t) + d`;
- debiting `c` records `max(z, t) + d` with no test: the same clamp as an admission;
- returning `c` records `z − d`, which is exactly `b ← min(C, b + c)`.

The `max` is the clamp: a full bucket forgets how long it has been full. Today's fast path
records `z + d` in every case (it adds `−c` to `tk` and leaves `rf`), which on a full bucket is
earlier than `t + d`. That is #808.

The proofs work over the reals. ADR-154 stores `z` scaled by the limit's undivided refill
amount, which makes `d`, the slack and every stamp exact integers. For one of `S` shards of a
bucket whose undivided capacity is `C_b` (so the shard's share is `C = C_b / S`), `d` becomes
`c × S × rp` and the slack `(C_b − c × S) × rp`, with capacities and the refill amount in
millitokens and the period `rp` in milliseconds. Nothing is divided, so no rounding enters and
the proofs hold as stated. At the ceilings #570 sets (`MAX_TOKENS`, `MAX_SCALE`) the refill
amount in force reaches 1e24 millitokens, so a stamp in 2100 scales to about 4.1e36, just under
DynamoDB's largest exact integer near 1e38; ADR-154 records how little headroom that leaves.

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
| Return `c`, or adjust by `−c` | none | `z ← z − d` |
| Adjust by `+c` (debit) | `N_t` or `F_t` without the upper bound | as `N_t` or `F_t` |
| Top up `N` (ADR-155) | none | `z ← z − N / r` |
| Reset (ADR-155) | none | `z ← t` |

Every write is also pinned on the parameters, schedule and shard count it was computed against
and on the item's recorded expression instant, with its stamp before the next schedule boundary
after that instant (ADR-155), so `C` and `r` above are the ones `z` is expressed in. A soft
limit takes no part in the admission write; it is debited afterwards like a positive
adjustment (ADR-154).

**Lemma 1 (exactness).** For `c ≤ C`, on an admissible item (`z ≤ t + (C − c) / r`) exactly
one of `N_t`, `F_t` applies, and it records `max(z, t) + d`. On any other item both fail.
*Immediate from the table.* `F_t`'s condition does not mention `c`: with `c > C` it would apply
to a full item and record a negative balance (an independent model measured 36 times the
`2 r s` term), so ADR-154's client never sends it for a hard limit. Without the upper bound the
pair applies to every item, which is the debit a positive adjustment and a soft limit need.

**Lemma 2 (admissions and debits move `z` later).** `N_t` adds `d > 0`. `F_t` applies only when
`z < t` and sets `t + d > z`. A positive adjustment does one or the other. Only returns,
negative adjustments, top-ups, resets and re-expressions (§8) move `z` earlier.

Parameters (`C`, `r`, the schedule, the shard count) are fixed throughout §§3–6; §8 covers
changes to them.

## 3. Safety with many consumers

**Theorem 1.** For write `i`, let `o_i = t_i − T_i`: its stamp minus the instant it lands
(clock offset plus time in flight, usually negative; an admission's retries keep its stamp, so
this includes their time, while a debit takes a fresh stamp on each retry). Let
`s = max |o_i|`. Then over any real
time window of length `w`, tokens debited (admissions and positive adjustments) minus tokens
returned, counted from the window's start to its last admission, is at most
`C + r × w + 2 r × s`, plus what resets and top-ups grant in the window. This is ADR-153's
bound for one shard with fixed parameters.

**Proof.** Compare the item with a reference bucket `Y`: a strict bucket fed every applied
write at the instant it landed, allowed to go negative (fill instant `y`, updated by
`y ← max(y, T_i) + d` for an admission or a positive adjustment, and `y ← y − d` for a return
or a negative adjustment). Let `e = z − y`. We show `|e| ≤ s` by induction over applied writes.

- The write that creates the item records `t + d` where `Y` records `T + d`, so `e = o`.
- A return, negative adjustment or top-up moves `z` and `y` by the same amount: `e` is
  unchanged.
- A reset sets `z` to `t` and `y` to `T`, so `e' = o`.
- An admission or a positive adjustment with offset `o`, by which side each view is on:
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
added (at most the tokens returned) and what resets and top-ups granted, minus what admissions
and positive adjustments took. It starts at most `C` and ends at least `−2 r s`, so debited
minus returned up to that admission is at most `C + r × w + 2 r × s` plus the grants. ∎

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
and nothing that moves `z` earlier lands in between (a return, a top-up, a reset or a
re-expression), then `z ≥ t` when `A`'s write lands (Lemma 2), so `A` knows its side.

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

**Theorem 4.** If only admissions and positive adjustments land between a request's writes,
the request is admitted or
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

With one consumer per shard and nothing moving `z` earlier, its image is exact and it never
guesses wrong. With several, Theorem 3(b) means an image showing "not full" is always right,
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

An adjustment by `+c` is a debit, not an add. On a full bucket it must record `max(z, t) + d`;
adding `d` instead forgives the debt whenever `z + d < t`, which is the usual case for a lease
reconciling after a long call on an otherwise idle bucket (the independent model measured 17
times the `2 r s` term). It therefore uses `N_t` or `F_t` without the upper bound: it is never
rejected, may push `z` past `t + C / r` (debt), and is retried until one lands, because
dropping it would forgive the debt. Each retry takes a fresh stamp: with no admission test it
needs no fixed one, every attempt is exact at its own stamp, and its lateness never enters `s`.
Returns landing between its writes can delay it, as Theorem 3(c) delays an admission. A soft
limit is debited the same way, by a separate write after the admission (ADR-154), so only a
hard limit can reject. Neither returns nor debits are idempotent (#731), and a reset (§8)
forgives debt.

Nothing refills a dripping limit, so the aggregator writes no refill or trim for one. It still
counts usage, and doubles a bucket that holds a dripping limit only through ADR-156's raise
(§8).

## 8. Other writers (ADR-155)

ADR-155 allows every other write of a fill instant one of three forms, each pinned on the
parameters, schedule and shard count it was computed against and on the item's recorded
expression instant, with its stamp before the next schedule boundary after it. Under that pin
the item's `z` is expressed in exactly the `C` and `r` the write used. The pin does not use
`vu`, which quota writers also stamp.

**Lemma 3 (writes exact on every item).** Adding `−d` returns `c` tokens or adjusts by `−c`
(§1). Adding `−N / r` tops up `N` tokens, `b ← min(C, b + N)`, capped at capacity because every
`z` before now reads as full. Setting `z ← t` resets the balance to `C`, forgiving any debt.
None depends on `z`, so none needs a read. A positive adjustment is not among them (§7). ADR-155
still sends a top-up pinned on a read, only so that ADR-149's record knows what each shard
gained; the write itself is exact on every item.

**Lemma 4 (writes pinned on a read).** A write pinned on the fill instants it read applies only
to an item still holding them, so a result it computes from the read lands on the state it was
computed from. If another write lands in between, the pin fails and nothing changes.

**Re-expression.** A fill instant `z` expressed against `(C, r)` implies at instant `τ` the
balance `b = C − r × max(0, z − τ)`. Re-expressing it against `(C', r')` at `τ` records
`z' = τ + (C' − min(b, C')) / r'`: the balance is kept, or trimmed to the new ceiling, and
never raised. It needs `max(z, τ)`, so it is a Lemma 4 write; ADR-155 requires one wherever a
write changes the parameters, schedule or shard count.

**Schedule windows.** Let `e` be the instant the item's fill instants were last expressed
(ADR-155 records it) and `τ_1 < … < τ_k` the schedule boundaries between `e` and now.
Re-expressing at `τ_1`, then at `τ_2`, and so on, drips each window at its own rate and caps it
at its own ceiling: the strict bucket with time-varying parameters. Crediting the whole
interval at the rate in force at its end, as the code does today, admitted 1000 against 117
across a boundary from 0.1x to 1x.

**Shard split (ADR-156).** A raise from `S` to `2S` shards first stamps a short-lived marker on
every shard, which every write that moves a fill instant, the expression instant or the shard
count refuses while it is live, so their writes cannot break its pins; one it does not hold off
(a stamp fan-out) cancels it, since it is pinned on what it copies.
It then reads every shard and, in one transaction pinned on what it read, re-expresses each
shard `i` (holding `b_i ≤ C / S`) at the new share `C / 2S`, keeping `min(b_i, C / 2S)`, and
creates shard `i + S` holding `max(0, b_i − C / 2S)`, which is at most `C / 2S`. Every shard
holds at most its share, and the total balance is exactly what it was before the raise. A shard
missing while others of its bucket exist is created empty, which adds nothing. The reserved
`wcu` budget is not under ADR-153's bound: each partition has its own, so a new shard's starts
full.

**Theorem 5 (ADR-153's bound for every writer).** If every write of a dripping limit takes one
of ADR-155's forms, then on one (entity, resource), summed over its shards, tokens debited minus
tokens returned, counted from the start of a window to its last admission, is at most
`C + R(w) + 2 r s` plus what resets and top-ups grant in the window, where `C` is the largest
ceiling in force from `s` before the window's start to its start, `R(w)` the refill the
schedule grants across the window, and `r` the largest rate in force across it.

*Proof sketch.* Apply Theorem 1's window argument to the total balance over shards. It starts
the window at most `C`: every shard holds at most its share and the shares sum to the ceiling
in force, and a write stamped up to `s` before a boundary that lowers capacity can still land
after it and spend the old ceiling until a re-expression trims it. That is why `C` is taken
over the `s` before the window; read at the window's start alone, the independent model
exceeded the bound by 3 to 54 times the `2 r s` term.
It gains at most `R(w)` of refill: within a schedule window the shards' rates sum to the rate
in force, and across boundaries each shard drips at each window's own rate. A split conserves
it, a shard created empty adds nothing, a re-expression keeps or lowers it, returns add at most
what they return, and resets and top-ups add what they grant. Theorem 1's clock-offset induction holds shard by shard, each
reference bucket falling at most `2 r_S s` below zero, and the shards' rates `r_S` sum to at
most `r`. ∎

## 9. What this does not cover

- **An encoding where attribute presence tracks time.** `if_not_exists` is the only update that
  depends on anything but arithmetic, presence changes only on writes, and the client chooses
  attribute names before it sees the item. M3 excludes such encodings; none is known that works
  with a bounded number of attributes.
- **An encoding where `z` is the max of several attributes.** A condition can test each one, so
  `max(a, b) ≤ K` is expressible, and recording `max(z, t) + d` as one more candidate keeps a
  write exact. But every full-side write adds a candidate, and no single write can tell which
  are dominated, so the count grows unless something prunes it. M3 excludes such encodings; no
  bounded one is known.
- **A store with a server-side `max` or branching**, such as GCRA in Redis/Lua or SQL
  `GREATEST`. One round trip is exact there; the bound is a property of DynamoDB's update
  language.
- **Quotas.** A quota does not drip; it keeps `tk` and its resets (ADR-137, ADR-145).
