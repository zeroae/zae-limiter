# Rolling, per-entity session windows — feasibility analysis

**Issue:** #222 (epic #478) · **Status:** Analysis only — no implementation, no ADR
**Date:** 2026-09-15 · **Milestone:** v1.0.0
**Supersedes nothing.** ADR-138 remains Accepted; this document costs the thing it defers.

---

## 0. The question, and the short answer

> "10,000 tokens, and your window resets 5 hours after you first used it."

Two entities that first call at 09:00 and 14:30 have windows ending at 14:00 and 19:30. The
window is anchored to **each entity's own activity**, not to a wall clock shared by everyone.
ADR-138 defers exactly this, explicitly on scope grounds rather than feasibility.

**The short answer, in three sentences.** A rolling window is buildable and the expensive
property survives intact: the speculative fast path stays at 1 WCU with **the condition
expression completely unchanged**, because `vu` already means "the materialised balance has
expired, go to the slow path" and a window end is one more thing that can make that true. The
corrected paragraph in ADR-138 is right that no *anchor* field is needed but wrong that no
field is needed at all — `vu` is already overloaded as the `set_limits` fan-out marker
(`vu = 0`, #222 Task 13), so a rolling window that reads its own expiry off `vu` turns every
admin `set_limits()` call into a fleet-wide quota refund. The single new attribute that fixes
that (`b_{name}_we`, window end, epoch ms) also makes `resets_at_ms`, `retry_after_seconds`
and the TTL horizon **cheaper** than the calendar form, because none of them needs a cron
scan.

**Recommendation (§6):** build it, but in a **deliberately unsharded** form scoped by one
validation rule. That removes the only genuinely new infrastructure (a cross-shard window
fan-out) and about half the effort, at a documented cost that the session-cap shape mostly
does not pay.

---

## 1. The mechanism, and where the anchor lives

### 1.1 `vu` is sufficient as a *gate*, insufficient as *state*

Today `vu` (valid-until, epoch ms) carries **three** meanings on one attribute:

| # | Meaning | Written by | Read by |
|---|---------|------------|---------|
| 1 | Fast-path gate: `vu > now` ⇒ `tk` was materialised under params still in force | slow path, aggregator | `_speculative_consume_single`'s condition |
| 2 | "The operator changed something here" — `vu = 0` unconditionally | `_sync_bucket_params` / `bucket_sync` fan-out | the forced materialising pass |
| 3 | Optimistic-lock pin, so a refill cannot ride a pre-fan-out stream image (#508) | — | `try_refill_bucket`'s `#vu = :expected_vu` |

For the **calendar** form these three coexist because the reset decision is *not* taken from
`vu`. `_apply_reset_edge` asks `prev_reset_edge(limit.reset_schedule, now) > state.last_refill_ms`
— derived from the cron and `rf`, never from `vu`. So a fan-out's `vu = 0` forces a pass, the
pass finds no missed cron edge, and nothing is refunded.

A rolling window has no cron. Its entire state is "when does this window end". If that state
*is* `vu`, then meaning 2 collides with meaning 1 head-on:

> `set_limits()` on any limit fans out `vu = 0` to **every shard of every bucket for that
> entity** (unscoped `_default_` widens it to every resource, #487). The next acquire on each
> would see `vu <= now`, read that as "your 5-hour window ended", reset `tk` to capacity and
> open a fresh window.

An operator raising an unrelated `rpm` ceiling would hand every caller a free session quota
and restart every caller's clock. Silently — there is no error, no log, and `tc` keeps
climbing so usage aggregation looks normal. **This is the single biggest obstacle in the
analysis**, and it is invisible from ADR-138's framing because ADR-138 reasons about `vu` as
a stamp rather than about what else already writes it.

### 1.2 What the bucket item has to carry

Two new attributes, both per-limit (not item-level with overrides — see below):

| Attribute | Type | Meaning |
|-----------|------|---------|
| `b_{name}_we` | `N` (epoch ms) | This limit's current window end. **Absent** means the window has not started. |
| `b_{name}_rwin` | `N` (seconds) | Window length, denormalised from config so a materialiser needs no read. |

And `vu` stays exactly what it is — a **derived** minimum:

```
vu = min(next param change, next reset edge, b_{name}_we across every limit on the item)
```

`_materialisation_stamps()` and `_item_next_boundary()` each gain one member in that `min`.
Nothing about the fast path changes.

The rolling-window decision then becomes, per limit, on the materialising pass:

```python
if we is None or we <= now_ms:          # window not started, or ended
    state.tokens_milli = state.effective_capacity_milli(now_ms)
    new_we = now_ms + window_seconds * 1000
```

`we is None` on a fresh item is what makes "anchored to first use" fall out for free: the
bucket is created at full capacity with **no** `we`, and the first acquire that materialises
it stamps the window. A bucket created by the fast path never exists (the fast path cannot
create), so the first write is always a slow-path write and always sets `we`.

**Per-limit, not item-level-plus-override.** The `sched`/`rsched` machinery (`_encode_one_tuple`,
`BUCKET_SCHED_NONE`) exists because limits usually *share* a schedule. Window ends do not
share by construction: the motivating product is a 5-hour session cap **beside** a weekly cap,
whose ends differ by definition. The override table would degenerate to all-overrides, so skip
it. `rwin` could share, but pairing it with `we` is clearer than splitting the two.

**Item size:** `b_rpd_we` ≈ 8 B name + 8 B value; `b_rpd_rwin` ≈ 10 B + 3 B. ~30 B per rolling
limit, against §4.2's 479 B unscheduled baseline and the 1 KB WCU cliff. Two rolling limits on
one item cost ~60 B. Not a concern.

### 1.3 Mechanism options, costed

| # | Mechanism | New item state | Fast path | Rollover cost (1 shard) | Verdict |
|---|-----------|----------------|-----------|-------------------------|---------|
| A | Overload `vu` as the window end | none | 1 WCU, unchanged | 1 WCU (failed cond.) + ~1 RCU + 1 WCU | **Broken** — §1.1, `set_limits` refunds every quota |
| B | `we` + `rwin` per limit, `vu = min(…, we)` | 2 attrs | 1 WCU, unchanged | 1 WCU + ~1 RCU + 1 WCU ≈ **$1.375/M rollovers** | **Recommended** |
| C | B + cross-shard window fan-out | 2 attrs | 1 WCU, unchanged | B + (S−1) WCU | Needed only if sharded; see §3 |
| D | Anchor on the entity `META` item, cached | 1 attr on META | 1 WCU, but the cache never invalidates | +1 RCU on cold cache | Rejected — `_entity_cache` is documented immutable; a window end moves every interval |
| E | Per-entity offset into a fixed grid (`hash(id) % W`) | none | 1 WCU, unchanged | calendar cost | Rejected by ADR-138 already, and it is not first-use anchored: a new entity's first window can be one minute long |
| F | Store a fixed start instant, project multiples of `W` from it | 1 attr | 1 WCU, unchanged | same as B | Rejected by ADR-138; strictly worse than B — it cannot express "go idle long enough and your window restarts" |

Only B and C are live. The rest of this document costs B, and §3 costs the C delta.

**Rollover volume.** At 10k active entities on a 5-hour window: 10k × 4.8 = 48k rollovers/day.
At $1.375/M that is **$0.066/day ≈ $2/month**, on a path nobody triggers by hand. Negligible,
and the same order as §2.1's own boundary estimate ("~40k extra slow-path passes/day, roughly
$0.05").

---

## 2. The fast path survives — unchanged

This is the load-bearing claim and it is the strongest result in the analysis.

`_speculative_consume_single` (repository.py:2725–2851) builds a condition of five parts:

```
attribute_exists(PK)
  AND #tk_{name} >= :thresh_{name}          (per declared limit)
  AND #wcu_tk >= :thresh_wcu
  AND (attribute_not_exists(#ttl) OR #ttl > :now_epoch)
  AND attribute_not_exists(#disabled)
  AND (attribute_not_exists(#vu) OR #vu > :vu_now)
```

Under mechanism B, **not one character of this changes.** The window end reaches the fast path
only through `vu`, which the slow path and the aggregator already compute and stamp. The fast
path continues to:

- read the clock exactly once (`now_ms`, #430) — `:vu_now` is the same bound value;
- read no config;
- evaluate no schedule and no window arithmetic;
- cost 1 WCU on success, 0 RCU + 0 WCU on a fast rejection.

The failure classification is likewise unchanged: `vu <= now` already returns
`SpeculativeFailureReason.SCHEDULE_BOUNDARY` (repository.py:2907–2917), ranked **after**
`DISABLED` and **ahead of** every exhausted reason, and the limiter already routes it to the
slow path rather than to a rejection or a shard retry. Every reason that ordering exists
applies verbatim to a window end: a closed window is not a rejection; reading it as
`WCU_EXHAUSTED` would double `shard_count` at every rollover; every shard crosses its own
window end so a shard retry cannot help.

The one place a *new* branch is needed is the slow path, which is the only client that
materialises. `_do_acquire` gains one call beside `_apply_reset_edge`:

```python
self._apply_reset_edge(limit, existing, now_ms)      # calendar, today
self._apply_window_roll(limit, existing, now_ms)     # rolling, new
```

with the same contract — called **before** `_admit_limit`, mutating `state` in place, never
touching `tc`. And `_commit_initial` gains the same "edge crossed between the two clock
readings" re-expression that `_reset_edge_ms` already carries (lease.py:434), for the same
reason: the acquire path reads the clock, the commit reads it again a round trip later, and a
window that ended in between would otherwise be stamped past by `rf` and lost for a whole
period.

**Cost summary, mechanism B, per acquire:**

| Path | Today | With a rolling window |
|------|-------|----------------------|
| Speculative success | 0 RCU + 1 WCU = $0.625/M | **identical** |
| Speculative fast rejection | 0 RCU + 0 WCU = $0/M | **identical** |
| Slow path (rollover) | — | 1 WCU + ~1 RCU + 1 WCU ≈ $1.375/M, once per window |
| Aggregator refill | 1 WCU | identical (one extra condition term already present) |

---

## 3. Sharding — the part the brief expects to sink a naive design

`select_shard` draws `random.randrange(shard_count)` on **every** call (ADR-134), shards
re-materialise independently, and each holds `capacity // shard_count`. Under mechanism B
each shard would stamp its own `we = now + W` at its own first post-expiry use. So an entity
with S shards has S window ends, drifting apart by however long each shard waits to be drawn.

Three things are worth separating here, because two of them are smaller than they look and
the third is the real one.

### 3.1 Over-admission is *not* worsened. (Counter-intuitive, and load-bearing.)

A fixed window of length `W` admits up to `2 × capacity` across any observation window of
length `W`, by draining just before an edge and again just after. That is inherent to fixed
windows and is true of the **calendar** form today, aligned shards or not.

With S staggered shards, each shard contributes at most `2 × (C/S)` over any `W`-length
observation window, for a total of `2C` — **the same bound**. Staggering redistributes when
the allowance arrives; it does not loosen the ceiling. Any claim that drift lets an entity
exceed its quota is wrong, and I want that on record because it is the obvious thing to
assert.

### 3.2 What drift actually breaks is the *reported reset instant*

`check_availability()` sums across every shard via GSI3 (#472) and reports one `Availability`
stamped at one `checked_at_ms`. With staggered windows there is no single honest answer to
"when does my window reset":

- `min(we)` over-promises — the user is told 19:00 and gets back 1/32 of the quota;
- `max(we)` under-promises — the user is told 20:30 while 31/32 came back at 19:00.

For a *rate* limit that ambiguity is invisible, because a drip is continuous. For a **session
cap shown to a human** — which is the entire motivating product — `resets_at_ms` is the
headline number. A feature whose headline number is undefined under sharding is not shipped.

### 3.3 The shard-count doubling refund, which is a pre-existing bug

`BucketState.from_limit()` (models.py:1119–1146) creates a new shard at
`effective_capacity_milli(now)` = `cp // shard_count`. For a **dripping** limit that is
correct — the shard would have refilled to its share anyway — and CLAUDE.md says so:

> "A missing shard N>0 is created by the client with `capacity_milli // shard_count` tokens …
> identical to the aggregator's Path 2 clone, **so shard creation never multiplies total
> capacity**."

That sentence is **false for a quota**, and has been since ADR-137 landed. A quota shard spent
to zero does not refill. A `wcu`-driven doubling from S to 2S mid-period creates S new shards
each holding `C/2S`, so the entity gains `C/2` of net-new allowance inside a period where the
contract says it has none. Nothing trims it: `refill_bucket`'s clamp is `min(cap, tk)` and
each new shard is exactly at its share, so per-shard there is no surplus to clamp — the
surplus exists only in the entity-wide sum, which no writer computes.

**This is an existing defect in the calendar quota, not something rolling windows introduce.**
It is reported in §7 rather than fixed here, since this is an analysis.

### 3.4 Mechanism C: the cross-shard window fan-out

The fix for §3.2 is to make the window one value for the entity. The shape already exists:
`Repository._propagate_shard_count()` fans a monotonic value from the winner to shards
`1..old_count-1` with concurrent conditional `UpdateItem`s (`shard_count < :new`), idempotent
and race-free. A window end is also monotonic, so the identical shape works with
`b_{name}_we < :new`:

- whoever crosses the window end first writes its own `we = now + W` **and** fans the same
  value to every sibling shard;
- a shard that materialises later adopts the fanned value rather than computing its own,
  because `we > now` already;
- racing writers converge on the earliest roller, which is the conservative direction (the
  window is at most `W` long, never longer).

**Cost:** (S−1) WCU per rollover per entity, on top of mechanism B. At S=32, 10k entities and
a 5-hour window: 48k × 31 = 1.49M WCU/day ≈ **$0.93/day ≈ $28/month** — 14× mechanism B, still
small in absolute terms, and paid only by entities that actually shard.

**Two frictions it introduces**, neither fatal:

1. The aggregator's `#vu = :expected_vu` pin (#508) treats *any* change to `vu` as "the
   operator changed something here" and skips the refill. A sibling's window fan-out would
   change `vu` without touching `rf`, so the aggregator would skip one refill per shard per
   rollover. That is a missed top-up, not a correctness failure, and it self-heals on the next
   batch. Worth a comment, not a redesign.
2. Shard 0 is not privileged in this scheme (deliberately — making it the anchor source would
   recreate the hot partition sharding exists to remove), so two shards can roll
   near-simultaneously and fan out two values one millisecond apart. The `<` condition makes
   the *later* one win, and both are within a millisecond of the same instant.

---

## 4. The aggregator

`try_refill_bucket` reads the item and nothing else, refills under an `rf` + `vu` optimistic
lock, and must never refill toward a stale ceiling. A rolling window requires three changes,
all mirroring what `rsched` already does:

1. **`_parse_bucket_record` reads `b_{name}_we` / `b_{name}_rwin`** into `LimitRefillInfo`,
   beside the `sched` / `reset_sched` it already parses. No new failure mode: these are
   integers, not a compact grammar, so there is no `sched_error` analogue and nothing new can
   poison a batch.
2. **A roll branch beside the reset branch** (processor.py:839–847), evaluated *before* the
   `is_accrual_rate` guard for the identical reason — a rolling window is a quota, its stored
   rate is 0 by ADR-137, and that guard would skip exactly the limits the feature exists for.
   Expressed as the same `ADD (effective_cp − tk_observed)` delta, safe for the same
   commutativity reason, with `SET b_{name}_we = :new_we` in the same write.
3. **`wcu` must be exempt**, exactly as it is exempt from `rsched` (processor.py:799–804).
   `rsched` is item-level so a user's midnight reset would otherwise hand `wcu` its
   per-partition write ceiling back at every edge. `we` as specified is per-limit, so `wcu`
   simply never carries one and the exemption is structural rather than a special case —
   a small improvement over the calendar form.

**What it does *not* require.** The aggregator only acts on stream records, and an exhausted
quota bucket produces none (fast rejections are 0 WCU). So it cannot roll a window for an
idle entity, which is correct: the window must be anchored to a *use*. When it does roll one,
it rolls at the instant it processes the record — a batch's worth of lag, single-digit seconds,
against a window measured in hours. Irrelevant, but worth stating, because the client would
have stamped a slightly earlier instant and the two must be allowed to disagree by that much.

`_item_next_boundary()` gains `we` as a third voting member. The existing "the item-level pair
is a member in its own right" reasoning (#541) does not apply — `we` has no item-level default
to fall back to.

---

## 5. Everything else, point by point

### 5.1 Storage encoding — it does **not** fit `sched`/`rsched`, and should not try

`sched` and `rsched` are compact tag-grammar cron strings (§4.1). A duration is not a cron and
there is no honest way to spell one in that grammar. Three options were considered:

| Option | Verdict |
|--------|---------|
| A new token inside `rsched`, e.g. `R18000` | **Rejected.** `decode_reset` deliberately *rejects* unknown modifier tags rather than ignoring them, on the argument that a silently-misread reset is the worst available outcome. Adding a token makes every entry conditionally-a-cron, and `cycle_seconds` / `prev_reset_edge` / `next_reset_edge` / `to_cron` each grow a branch. |
| A versioned encoding (#515, deferred to v1.0.0) | **Not required.** §4.1's argument against a marker holds: it cannot classify anything already written, so the distinction only works forward. A rolling window does not need one. |
| A plain integer attribute pair (`l_{name}_rwin` on config, `b_{name}_rwin` + `b_{name}_we` on the bucket) | **Recommended.** Additive, unambiguous, no grammar change, no new decoder, ~30 B. |

**Forward/backward compatibility is unchanged, and this is worth stating precisely because it
is easy to overstate.** An old client reading a rolling-window config item sees
`l_{name}_ra = 0` with no `rsched`, constructs `Limit(refill_amount=0, reset_schedule=())`, and
`Limit.__post_init__` raises (#538's shape). So rolling windows are not backward-readable.
**Neither is the calendar form**: a pre-#222 client reading a calendar quota sees the same zero
rate and raises identically. This is a property of ADR-137, not of rolling windows, and the
fleet-upgrade requirement already exists.

### 5.2 ADR-137 (drip or reset, never both) — the *rule* holds, the *predicate* widens

A rolling window is a quota: `refill_amount = 0`, recovery in a lump. The rule "a limit
recovers by drip or by reset edge, never both and never neither" is unchanged in substance —
a rolling window is a third *spelling* of the reset half, not a third mechanism running
underneath a drip.

What changes is every place that tests for it structurally:

- `Limit.__post_init__`: `refill_amount == 0 and not reset_schedule` → raise, must widen to
  `and window_seconds is None`;
- `Limit.is_quota`: `bool(self.reset_schedule)` → `or self.window_seconds is not None`;
- `Limit.from_bucket_state` and `Limit.per_shard`, which currently move the `max(1, …)` rate
  floor and `reset_schedule` together (models.py:667, 727) — `window_seconds` must join that
  pair or a reconstructed rolling limit becomes unconstructible;
- `schema._recovery_seconds`, which branches on `limit.is_quota`;
- `exceptions._limit_shape`, which branches on the same predicate.

Mechanical, but it touches six sites and each one is load-bearing. The exclusion rule of §2.2
("at most one *function* per bucket") is unaffected — a rolling window is not a function of
`now` applied to the base params; it is the same reset mechanism with a different edge source,
so it composes with `schedule` exactly as `reset_schedule` does.

### 5.3 TTL — this one is **free**, and cheaper than the calendar form

The brief flags this as having forced design twice (#532, #557), so it was checked rather than
assumed. It is fine.

`calculate_bucket_ttl_seconds(limits, multiplier)` deliberately holds no clock, and its three
production callers (`lease._commit_initial`, `Repository._sync_bucket_params`,
`zae_limiter_provisioner.bucket_sync`) have none to pass. `_recovery_seconds` prices a quota
by its reset **cycle** — `min(_reset_cycle_seconds(entry))`, which walks the coarsest
constrained cron field.

A rolling window's cycle is **`window_seconds`, exactly and by construction.** No cron parse,
no `cycle_seconds` ladder, no rounding-up approximation, no clock. One line:

```python
if limit.is_quota:
    if limit.window_seconds is not None:
        return float(limit.window_seconds)
    return float(min(_reset_cycle_seconds(entry) for entry in limit.reset_schedule))
```

It is strictly sharper than the calendar branch, which rounds a monthly pattern up to 31 days
and an annual one to 366. And `schema.py`'s import of `schedule.py` (added for `parse_cron`)
is not needed for this path at all.

**One consequence worth naming.** At the default multiplier of 7, a 5-hour window gives a
35-hour TTL. An entity idle 35 hours has its bucket swept and recreated at full capacity with
a fresh window — which for a rolling window is arguably *correct* ("go idle long enough and
your window resets"), unlike the calendar case where ADR-136 has to argue the sweep is
bounded. And ADR-136 means entity-level configs carry **no** TTL at all, so a per-entity
session cap — which is entity-level by nature — is never swept in the first place. Only
resource- and system-level rolling windows reach the formula.

### 5.4 `retry_after_seconds` and `resets_at_ms` — cheaper, but one signature is wrong

The computation is trivial and needs no scan: the answer is `we − now`, read straight off the
item. `retry_after_with_schedule`'s reset-edge branch (schedule.py:961–976) currently calls
`next_reset_edge(reset_sched, now_ms=cursor)`, a bounded cron scan; for a rolling window the
edge is a constant that dominates on iteration one exactly as a quota's does today.

**The obstacle is that `resets_at_ms` is currently computed from the `Limit` alone.**
`RateLimitExceeded._limit_shape()` (exceptions.py:162–166) does:

```python
return {"kind": "quota", "capacity": limit.capacity,
        "resets_at_ms": next_reset_edge(limit.reset_schedule, now_ms=now_ms)}
```

A calendar edge is recoverable from the clock plus the config, so a `Limit` suffices. A
rolling window's end lives on the **bucket item** and is not derivable from anything else. So
`LimitStatus` must carry it — a new optional field, populated at all four construction sites
(`bucket.declared_statuses`, `RateLimiter._admit_limit`, `lease._build_retry_failure_statuses`,
`RateLimiter.check_availability`), and `_limit_shape` must take the status rather than the
limit. That is a genuine public-shape change to the 429 body's plumbing, though not to its
JSON. Moderate, and unavoidable under any mechanism.

### 5.5 Cold start and expiry — the honest asymmetry

| Situation | Calendar quota | Rolling window |
|-----------|----------------|----------------|
| Bucket never written | Full capacity; `resets_at_ms` = next cron edge, computable with no item | Full capacity; `resets_at_ms` = **`None`** — the window has not started, and "resets `W` after you first use it" is the only honest answer |
| TTL swept mid-window | Recreated at full capacity; next edge is still the same calendar instant, so at most one extra allowance in the period | Recreated at full capacity **and** with a fresh window — both the balance and the clock restart |
| Entity goes idle past `we` | n/a | Next use opens a new window at that use. **Yes, an entity can reset its own window by going idle** — and this is the intended semantics, matching the motivating product |
| Entity goes idle *within* the window | Nothing | Nothing; `we` is untouched |

The middle row is the real asymmetry, and ADR-138 already names it in Consequences:

> "Missed edges remain idempotent, because a calendar edge is recoverable from the clock while
> a per-entity anchor would have to survive item expiry."

That sentence is **correct and is the strongest surviving argument in the record** — stronger
than the scope argument the Decision rests on. A rolling window's state is not recoverable
from the clock, so anything that loses the item loses the window: TTL sweep, a manual
`reset_bucket`-shaped operation, a namespace purge. The mitigation is ADR-136 (entity-level
configs carry no TTL), which covers the common case but not resource- or system-level rolling
windows.

---

## 6. What would have to change, file by file

| File | Change | Size |
|------|--------|------|
| `src/zae_limiter/models.py` | `Limit.window_seconds` field; widen `__post_init__`'s ADR-137 predicate; `is_quota`; `from_bucket_state`; `per_shard`; `to_dict`/`from_dict`; `BucketState.window_end_ms` + `window_seconds`; `LimitStatus.resets_at_ms` | M |
| `src/zae_limiter/schema.py` | attribute-name constants; `_recovery_seconds` quota branch (one line) | S |
| `src/zae_limiter/repository.py` | config serialise/deserialise (`l_{name}_rwin`); `build_composite_create` (stamp `rwin`, omit `we`); `build_composite_normal` (SET `we`); `_deserialize_composite_bucket`; `_sync_bucket_params` (stamp `rwin`, **never** touch `we`); mechanism C only: `_propagate_window_end()` | **L** |
| `src/zae_limiter/limiter.py` | `_apply_window_roll()`; `_materialisation_stamps()` third member; `check_availability` (`resets_at_ms`, and under C a single value across shards) | M |
| `src/zae_limiter/lease.py` | `LeaseEntry._window_end_ms`; `_commit_initial` re-expression for a window that ended between the two clock readings; `_build_retry_failure_statuses` | M |
| `src/zae_limiter/exceptions.py` | `_limit_shape` takes a status, not a limit | S |
| `src/zae_limiter/bucket.py` | `try_consume` / `declared_statuses` thread `we` into statuses | S |
| `src/zae_limiter_aggregator/processor.py` | `_parse_bucket_record`; roll branch in `try_refill_bucket`; `_item_next_boundary` | M |
| `src/zae_limiter_provisioner/{manifest,differ,handler,bucket_sync}.py` | `window` in the manifest; diff; `_coerce_int`; the sync mirror of the `rwin` stamp | M |
| `src/zae_limiter/limits_cli.py`, `cli.py` | `Window:` display line; CFN `Window` property round trip | S |
| `src/zae_limiter/sync_*.py`, `infra/sync_*.py` | generated — `hatch run generate-sync` | free |
| `tests/` | unit (roll arithmetic, `is_quota` widening, TTL horizon, ADR-137 validation); integration (`vu` still gates with zero config reads — the load-bearing claim, asserted with `capacity_counter`); E2E with `window_seconds: 2` and real waiting, marked `slow`; the manifest round trip | **L** |
| `docs/` + a new ADR superseding ADR-138's deferral | — | M |

**Effort.** Twelve source files plus two Lambda packages. Broad, but *shallow* relative to
#222: there is no cron, no timezone handling, no DST, no boundary scan, no encoding grammar,
no oracle test. I would estimate **6–8 PRs** on this project's cadence for mechanism B, and
**9–11** for B + C. Call it 2 weeks of focused work for B, 3 for B + C.

---

## 7. Recommendation

**Build it, as mechanism B, with sharding excluded by validation. Defer mechanism C until
someone hits the ceiling it removes.**

The reasoning:

1. **The expensive property is free.** The fast path stays at 1 WCU with a byte-identical
   condition expression. That is the one thing that could have killed this, and it does not.
   Everything else is bookkeeping in code that already exists for `reset_schedule`.

2. **Three of the nine concerns come out *cheaper* than the calendar form.** TTL
   (`window_seconds` is the cycle, exactly, no clock, no rounding), `retry_after_seconds`
   (a constant, not a bounded scan) and the `wcu` exemption (structural rather than a special
   case). The calendar form paid for a cron scanner, a timezone database, DST handling,
   `_reset_scan`'s two-ended horizon derivation (#574) and a compact grammar. A rolling window
   pays for none of it.

3. **§3 is solved by scoping, not by engineering.** The sharding problem is real, but it is
   real only for entities that shard, and a rolling session cap mostly cannot. `wcu` is 1000
   writes/minute/partition; an entity whose 5-hour allowance is under 300,000 requests can
   never reach it. So make a rolling window **refuse to shard**: suppress the `wcu`-driven
   `bump_shard_count()` when the bucket carries one, and let the entity take a `wcu` rejection
   instead of a shard. That is one guard in `limiter.py`, it is honest ("this limit shape
   cannot shard, here is the write ceiling"), and it makes `resets_at_ms` exact by
   construction. A config-time rejection is the wrong tool — sharding is reactive, so the
   constraint has to be enforced where the doubling happens.

   **The cost, stated plainly:** composite bucket items carry every limit for
   (entity, resource), so suppressing the shard for the quota suppresses it for any dripping
   limit sharing the item. A resource with a session cap *and* a hot per-minute rate limit on
   the same bucket would lose write sharding. That is the one configuration this scoping
   breaks, it is detectable at config-write time, and mechanism C is the upgrade path when
   someone needs it.

4. **The `vu = 0` collision must be fixed first, whatever is built.** If the eventual
   implementer is tempted by mechanism A — and ADR-138's corrected paragraph invites exactly
   that temptation by saying no field is needed — the result is a silent fleet-wide quota
   refund on every `set_limits()`. That should be the first sentence of the implementing ADR.

**If the answer is "not worth it": the cheaper thing that gets 80%.** There is no partial path
that is genuinely cheaper *and* delivers first-use anchoring — §1.3's options D, E and F are
each either rejected already or strictly worse than B. The honest 80% is not a different
mechanism; it is **mechanism B with sharding scoped out**, which is what is recommended above
and costs roughly half of the full design. The remaining 20% (a single reset instant for an
entity hot enough to shard) is mechanism C, and it is cleanly deferrable because `we` is
already per-item state that a fan-out can converge later without a migration.

---

## 8. Things I believe are now inaccurate

Reported rather than fixed — this is an analysis and touches no source.

### 8.1 CLAUDE.md: "shard creation never multiplies total capacity" is false for a quota

The Pre-Shard Buckets section states:

> "A missing shard N>0 is created by the client with `capacity_milli // shard_count` tokens,
> `wcu` undivided, stored `cp`/`ra` undivided, and the observed `shard_count` stamped —
> identical to the aggregator's Path 2 clone, **so shard creation never multiplies total
> capacity.**"

True for a dripping limit, where the new shard would have refilled to its share anyway. False
for a quota since ADR-137: a quota shard spent to zero does not refill, so a doubling from S
to 2S mid-period creates S shards holding `C/2S` each and grants the entity `C/2` of net-new
allowance inside a period where the contract says it has none. Nothing trims it —
`refill_bucket`'s clamp is `min(cap, tk)` and each new shard sits exactly at its share, so the
surplus exists only in the entity-wide sum, which no writer computes. **This is a live defect
in the calendar quota, independent of anything in this document, and it deserves its own
issue.** Note §3.6 of the design doc addresses the *reset* target ("resetting every shard to
the undivided capacity would multiply the entity's quota") but not the *create* path.

### 8.2 ADR-138: "needs no anchor field at all, because the valid-until stamp *is* the anchor"

Correct as a statement about anchoring, and the correction that produced it was right to
withdraw the earlier reasoning. But it is **false as a statement about this codebase**, because
`vu` is not only a valid-until stamp: since #222 Task 13 it is also the fan-out marker
(`vu = 0`) and the aggregator's optimistic-lock pin (#508). A rolling window reading its expiry
off `vu` inherits meaning 2 and refunds every quota on every `set_limits()` (§1.1). One extra
attribute is required. I would not re-open the ADR for this — it is Accepted and the Decision
does not rest on the claim — but the implementing ADR must contradict it explicitly, or the
sentence will be read as a design instruction.

### 8.3 ADR-138: "It is in fact cheaper than the calendar form"

Half right, and the half that is right is the more interesting half. Cheaper on evaluation (no
cron parse, no timezone database, no DST, no boundary scan), cheaper on TTL (§5.3), cheaper on
`retry_after` (§5.4). **More** expensive on storage (two new attributes plus a config field
against zero), on shard coherence (§3, which the calendar form gets free because every shard
shares one clock) and on durability (§5.5 — the state is not recoverable from the clock, which
the ADR's own Consequences section already says). Net: comparable, not cheaper, and the
distribution of the cost is different rather than smaller.

### 8.4 ADR-138's Decision rests on the weaker of its two arguments

The Decision says scope. The Consequences say "a per-entity anchor would have to survive item
expiry", which is the one property a rolling window genuinely cannot have and the calendar form
genuinely does. That is a *design* argument, it survives this analysis intact, and it is what an
implementing ADR will actually have to answer. Worth promoting if the record is ever revisited.

---

## Related

#222, #478 (epic), ADR-133, ADR-134, ADR-136, **ADR-137**, **ADR-138**, #468/#481/#487
(fan-out infrastructure), #472/#473 (`check_availability`), #475 (per-shard statuses), #508
(the `vu` pin), #515 (deferred version marker), #532/#557/#574 (TTL and scan horizons), #541
(`BUCKET_SCHED_NONE`), #545 (`resets_at_ms`).
