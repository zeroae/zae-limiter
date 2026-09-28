# Quota Shard Grant Record — Design

**Issues:** Refs #637, Refs #642 (v0.15.0). Related: #587, #633, #475, #477.
**Status:** design approved in conversation on 2026-09-28; this document records it.
**ADR:** ADR-143 (to be written as the first plan task).

## 1. Problem

A quota (`Limit.quota()`, ADR-137) never drips. On a sharded entity each shard
holds a slice of the period's allowance, and today nothing records **how much
allowance each shard was handed this period**. Two bugs share that root cause:

| Issue | Direction | Measured (C = 1000, clock frozen in one period) |
|---|---|---|
| #637 | Under-admission | Full, unspent quota → **187** spendable after a `wcu` walk 1 → 32 with nobody spending |
| #642 | Over-admission | **1250** (aggregator Path 2 clones an unseeded shard 0); **1300** (a shard idle across the reset edge, transfer persist skipped) |

**Why #637 happens.** `Repository.reclaim_quota_surplus` clamps *every* sibling
holding more than the new share `C/S` down to it, and the new shard keeps at most
one share (`models.new_shard_starting_tokens_milli`). The rest is discarded. The
clamp could not be avoided before: `bucket.refill_bucket` trims every shard to
`C // shard_count` on every materialising pass, so a surplus left in place would
be trimmed anyway.

**Why #642 happens.** A new or seeded shard is granted a full `C/S` unless a
sibling visibly *holds* a surplus. A sibling granted a larger share at a lower
count earlier in the period, and which has already spent into it, holds no
visible surplus — so the new shard is granted a share that was already handed
out.

**Why a record is needed.** Two cases look identical from the balances alone:

- shard 0 reset at count 4 (250); shard 3 created later → shard 3 **must** get
  its own 250 (nobody received it);
- shard 1 granted 500 at count 2 and spent it; count doubles to 4; shard 3
  created → shard 3 **must** get 0 (shard 1's grant paid for it).

## 2. Scope

**In:** the `divided` sharding regime (today's only regime), calendar and session
quotas, client and aggregator.

**Out:** dripping (rate) limits — unchanged. User-selectable sharding regimes
(`unsharded`, `divided`, `replicated`, `pooled`) — deferred to #477 (v1.0.0).

Considered and rejected on the way (owner decision, 2026-09-28):

| Idea | Why not |
|---|---|
| Full capacity per shard, rate `R/S`, no borrowing | Burst after idle up to `S × C` (32×); a quota would grant `S × C` per period |
| Borrow on reject (move tokens on demand) | A truly exhausted entity loses its free fast rejection (~1.5–2 RCU each) — belongs in #477 as `pooled` |
| A per-shard grant *amount* with no holder (option A) | The unhanded remainder lives on no item, so two concurrent creators both claim it |
| One per-entity allowance item (option B) | A write per shard creation on one hot item — reintroduces the partition limit sharding exists to avoid |

## 3. The rule

Every leftover token lives on some shard item, and every move is a conditional
write on the item that holds it. Fresh tokens are granted only for a **slot**
that no current-period sibling covers.

- **Slot.** At shard count `S`, slot `j` owns `C // S`.
- **Coverage.** A shard `i` whose current-period grant was sized at count `gc_i`
  covers every slot `j` with `j mod gc_i == i mod gc_i` — its own slot and every
  slot later split off it.
- **Reset / window roll** of shard `i` at count `S`: `tk = C // S`, `gc = S`.
- **Create or seed** of slot `j` at count `S`: among current-period siblings that
  carry the quota and a `gc`, keep those covering `j`; pick the largest `gc`
  (lowest shard id on a tie).
  - **Found** → move `x = min(C // S, donor tk)` off the donor.
  - **None** → grant a fresh `C // S`.
  - The new or seeded shard gets `gc = S`.
- **Ceiling.** A quota shard is trimmed to `C // gc`, not `C // S`.

### 3.1 Validation (throwaway model)

A pure-Python model of shards, resets, doublings and lazy creation (not product
code; no DynamoDB, no concurrency):

| Check | Today's rule | New rule |
|---|---|---|
| Nobody spends, walk 1 → 32, 200 random creation orders | 192 / 1024 | **1024 / 1024** |
| 1250 repro | 1250 (measured) | **1000** |
| 1300 repro | 1300 (measured) | **1000** |
| Fuzz, 300 runs: spend / double / late seed / reset | — | worst **1006 / 1024** per period; `admitted + held + still-mintable` **= 1024 at every step** |

The last line is the invariant that proves both directions: nothing minted,
nothing lost. Lazy creation needed no special case — shard 5 created before its
parent (shard 1) is covered by shard 0 through `j mod gc`.

## 4. Storage and writers

**New bucket attribute `b_{q}_gc`** (number): the shard count this shard's
current-period grant was sized at. Quotas only (calendar and session); a rate
limit never carries it. +~15 B per quota per shard.

| Writer | Writes `gc`? | Value |
|---|---|---|
| Fast path (`speculative_consume`) | No — unchanged | — |
| Slow path applying a reset or roll (`_apply_reset_edge`, `_apply_window_roll`, opener) | Yes, on the same rf-locked write | item's `shard_count` |
| Client shard create (`build_composite_create`) | Yes | creation count |
| Seed of a missing quota (`build_composite_normal(seeds=…)`) | Yes | seed count |
| Aggregator refill that applies a reset or roll | Yes, same write | item's `shard_count` |
| Aggregator Path 2 clone | Yes | new count |
| Retry, adjust / rollback, window fan-out, param sync, shard-count propagation | No | — |

`BucketState.effective_capacity_milli` splits into two roles:

- **reset target** — `cp // shard_count`, as today;
- **ceiling** — `cp // gc` for a quota, `cp // shard_count` otherwise.

Expression tokens are positional (`#gc{i}` / `:gc{i}`, #634) and every new write
builder is added to `tests/unit/test_expression_tokens.py`.

## 5. Current period and donor selection

A sibling may donate only if its grant belongs to the current period. The test is
the exact negation of "a reset is pending", sharing the existing helpers:

| Quota | Grant is current when | Same rule as |
|---|---|---|
| Calendar | `prev_reset_edge(rsched, now) <= rf`, or no edge within reach | `RateLimiter._apply_reset_edge` |
| Session | window live (`ws + rsa > now`) **and** applied (`wa >= ws`; unmarked item: `ws <= rf`) | `BucketState.window_rolled`, `processor._window_applied` |

Session windows may be staggered by milliseconds across shards (ADR-140). A
sibling in a different live window still counts as current: neither window
resets before it ends. A new shard still joins shard 0's live window (ADR-140).

## 6. Create / seed flow

One repository method replaces `reclaim_quota_surplus`, `reclaim_quota_seed` and
`persist_seed`:

1. **Read** — GSI3 KEYS_ONLY query + `BatchGetItem` of the siblings (as today).
2. **Decide** per quota — donor or mint (§3). The count is the largest of the
   caller's and every sibling's stored `shard_count`.
3. **Propagate** — before minting, raise `shard_count` on any sibling still below
   `S` (the existing `_propagate_shard_count`; a write only when a lag is seen).
   See R5.
4. **Write atomically** — one `TransactWriteItems`:
   - donor: `ADD tk −x` under `tk >= :x AND gc = :gc` and its period still current
     (calendar `rf >= :edge`; session `wa = :wa`);
   - the new shard `Put` under `attribute_not_exists(PK)`, or the seed's rf-locked
     `UpdateItem`, with `tk = x − consumed` and `gc = S`. A seed (an existing
     item) also carries the count pin
     `attribute_not_exists(shard_count) OR shard_count <= :gc`.
5. **Rejected acquire** — the same transaction runs with nothing consumed, so a
   move is never lost (replaces `persist_seed`; the documented pre-rejection
   write of `write-on-enter.md`).
6. **Condition failure** — re-read and retry once; if it fails again the shard
   gets 0 this time (under-admission, never over).

**Aggregator Path 2** does the same per pre-created clone, the donor being its
parent `j mod old_count`, one transaction per clone. The greedy distribution of
`processor._reclaim_quota_surplus` is removed.

### 6.1 Cost (quotas only)

| | Today | New |
|---|---|---|
| Reads | Query + BatchGet | same |
| Mint | 1 WCU `Put` | same |
| Move | 1 WCU `Put` + 1 WCU per clamped sibling | 4 WCU (2-item transaction) |
| Rejected acquire | clamps + `persist_seed` | the same transaction |
| Fast path | 0 RCU + 1 WCU | **unchanged** |

At most 31 creations per entity per period.

## 7. Ceiling and reporting

- `refill_bucket` trims a quota shard to `cp // gc`.
- A mid-period **capacity decrease** still takes effect: the param sync sets
  `vu = 0`, the next pass trims each shard to `C_new // gc_i`, and because
  coverage classes are disjoint `Σ 1/gc_i ≤ 1`, so the total stays ≤ `C_new`. An
  increase applies at the next reset, as today.
- `Limit.per_shard` reports a quota's per-shard capacity as `C // gc` at all four
  `LimitStatus` sites (`gc` is on the item image already; no extra read), so a 429
  never shows `available > capacity`. Rate limits, `retry_after_seconds`,
  `resets_at_ms` and `check_availability` are unchanged.
- Side effect: a quota request above `C // S` can be admitted by a shard holding
  enough. #475 stays open.

## 8. Races

| # | Race | Outcome |
|---|---|---|
| R1 | Two creators of different shards share a donor | Donor debited under `tk >= x`; one fails and retries. No over |
| R2 | Two seeders of the same shard | rf lock + `attribute_not_exists` guard; the loser's whole transaction fails, donor untouched |
| R3 | Donor resets between read and write | Conditioned on `gc` and period; tokens from a new-period grant move into a new-period shard, each slot ≤ its share. No over |
| R4 | Aggregator doubles during a create / seed | Count pin, now on every write that sets `gc` |
| R5 | A sibling whose stored `shard_count` lags a doubling resets at the lower count, covering a slot someone else just minted | Propagate before minting (§6 step 3) **and** pin every reset/roll write on `shard_count <= :gc`; a stale reset fails its pin once and retries |
| R6 | Client whose config cache predates the quota creates a shard without it (#642 source a) | Seeded later by the rule |

No known over-admission remains; the `C·(1/S′ − 1/S)` residual documented for
#642 is removed. Race tests step interleavings deterministically with stubs —
moto is not thread-safe (#656).

## 9. Compatibility

**Item with the quota but no `gc`** → read as `gc = its shard_count` (owner
decision, option 1). Exactly today's meaning; the #642 residual survives at most
one period after upgrade, on a quota sharded and spent across a doubling, and the
next reset writes `gc`.

**v0.14 writers** (calendar quotas shipped in v0.14; session quotas are hidden
from v0.14 by ADR-142) can only under-admit:

| v0.14 writer | Effect |
|---|---|
| Slow pass trims to `C // S` | discards a v0.15 shard's held surplus |
| Reset without writing `gc` | stale, lower `gc` → broader coverage → moves instead of mints |
| Create (#587 clamp) or Path 2 clone | clamps siblings; new shard without `gc`, ≤ `C // S` |

No version gate is needed.

## 10. Acceptance tests

Quota C = 1000, clock frozen in one period; moto, async and sync.

| # | Test | Must be |
|---|---|---|
| 1 | Nobody spends, `wcu` walk 1 → 32; sum spendable | **exactly 1000** (today 187) |
| 2 | Aggregator Path 2 clones an unseeded shard 0 | ≤ 1000 (today 1250) |
| 3 | Shards idle across the reset edge, then a seed | ≤ 1000 (today 1300) |
| 4 | #587 walk, spending while splitting | ≤ 1000 |
| 5 | Seed racing a doubling (R3 of #633) | ≤ 1000 |
| 6 | Concurrent creators of different shards (stepped) | ≤ 1000; Σ tokens never grows |
| 7 | Any split | Σ tokens unchanged |
| 8 | Fast path capacity | 0 RCU + 1 WCU |
| 9 | Shard creation capacity | Query + BatchGet + ≤ one 2-item transaction |
| 10 | Tests 1–3 with a session quota | same numbers |

Plus race tests for R2–R5 and a v0.14-shape item (no `gc`) test.

Homes: `tests/unit/test_quota_shard_creation.py`,
`tests/unit/test_window_shard_creation.py`, the aggregator processor tests, and
`tests/unit/test_expression_tokens.py`.

## 11. Documentation

ADR-143 ("A sharded quota conserves its allowance", Proposed, accepted at the
v0.15.0 release); CLAUDE.md (Pre-Shard Buckets #587 paragraph, #633 seed
paragraph, writer table, pricing); the sharding notes in
`docs/guide/session-quotas.md` and `docs/guide/scheduled-limits.md`; the docstrings of the replaced repository methods.
