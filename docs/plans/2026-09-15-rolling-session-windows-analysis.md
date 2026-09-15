# Rolling, per-entity session windows — feasibility analysis

**Issue:** #222 (epic #478) · **Status:** Analysis only — no implementation, no ADR
**Date:** 2026-09-15 · **Milestone:** v1.0.0
**Supersedes nothing.** ADR-138 remains Accepted; this document costs the thing it defers.

> **Revision note.** A first draft of this analysis recommended shipping with sharding
> suppressed on rolling-window buckets. That recommendation was **rejected on its premise and
> is withdrawn**. Write sharding is the mitigation for GHSA-76rv-2r9v-c5m6, not a performance
> optimisation a feature may opt out of; an entity that cannot escape a hot partition is the
> advisory's condition reintroduced, in the one use case — a busy account against a session cap
> — that guarantees it. Sharding is a hard invariant throughout what follows, and "it is
> cheaper" is not an argument against it.

---

## BLUF

**Build it, sharded, but not next and not as one piece.**

**The verdict.** A rolling per-entity window is fully compatible with write sharding, at a
marginal cost of **31 WCU per rollover per maximally-sharded entity** and **zero change to the
per-acquire cost** — the speculative fast path keeps a byte-identical condition expression and
its 1 WCU. The coherence problem is solved by copying the mechanism the calendar reset already
uses rather than inventing one: store the window **start** (`ws`) as an entity-wide scalar
fanned across shards, and let each shard apply its own reset when it sees `ws > rf`. That is
`prev_reset_edge(cron, now) > rf` with the cron replaced by a stored number. The fan-out never
touches `tk`, which is what makes it safe.

**The two facts that carry it.**

1. **`ws` is monotonic and `tk` never rides the fan-out.** Each new window starts at a fresh
   clock reading strictly after the previous window ended, so `ws` only increases and
   `_propagate_shard_count()`'s exact shape — concurrent conditional `UpdateItem`s under
   `ws < :new` — transfers unchanged. The thing that is *not* monotonic is the balance, and the
   `ws > rf` rule means the fan-out never has to carry it: every shard resets itself, under its
   own `rf` lock, on its own next materialisation. A fan-out that wrote `tk` would be a blind
   `SET` racing concurrent `ADD`s, which is the one shape this codebase never uses.

2. **The shard-doubling over-issue is in the design, and the design supplies its fix.** A quota
   shard spent to zero never refills, so a mid-period doubling that creates new shards at their
   full share grants ~`C/2` of net-new allowance that nothing trims. This is a **live defect in
   the calendar quota today**, and a rolling window is a quota, so it cannot be inherited. The
   fix is to **redistribute rather than issue**: on a doubling, `ADD −(old_share/2)` to every
   existing shard while new shards materialise lazily at the new share. Total preserved exactly,
   negative balances meaningful, commutative with concurrent consumption. It rides on the writes
   `bump_shard_count()` and `_propagate_shard_count()` already issue — `shard_count = :old` is
   exactly-once and `shard_count < :new` is at-most-once-per-value, which is precisely the
   guarantee an `ADD` needs — so it costs **zero extra round trips**.

**Why "not next".** Fact 2 is a correctness bug in shipped code that is independent of this
feature and currently unowned. It should be fixed first, on its own, where it is small and
independently testable. Rolling windows then land on top with the `ws` fan-out as the only
genuinely new distributed-systems surface.

**Cost, in the right budget.** Per-acquire: **unchanged** ($0.625/M). Per-rollover at S=32:
31 WCU ≈ $0.93/day at a deliberate worst case of 10k maximally-sharded entities, ~$0.28/month
at a realistic 1% sharded. The alternative to paying it is not $0 — it is an entity pinned to
one DynamoDB partition, which is a throttling outage, not a line item.

**Effort:** 9–11 PRs, ~3 weeks, across 12 source files and both Lambda packages. Shallow
relative to #222 — no cron, no timezones, no DST, no boundary scan, no encoding grammar.

**Recommended sequence:** (1) fix the quota doubling over-issue for calendar quotas; (2) the
`ws`/`rwin` storage and the per-shard `ws > rf` reset; (3) the `ws` fan-out and the shard-create
sibling read; (4) surface, manifest, CLI, docs, ADR.

---

## 1. The problem

> "10,000 tokens, and your window resets 5 hours after you first used it."

Two entities that first call at 09:00 and 14:30 have windows ending at 14:00 and 19:30. The
window is anchored to **each entity's own activity**, not to a wall clock shared by everyone.
ADR-138 defers exactly this, explicitly on scope grounds rather than feasibility.

Everything below assumes, as a hard invariant, that the feature works correctly under:

- `select_shard()` re-drawing `random.randrange(shard_count)` on **every** call (ADR-134);
- shards created lazily by whoever first draws them (ADR-133), possibly by a client that has
  never read another shard;
- shards re-materialising independently, each under its own `rf` optimistic lock;
- `wcu`-driven doubling up to `MAX_SHARD_COUNT = 32`, triggered by either the client or the
  aggregator;
- `--no-aggregator` deployments, where the client is the only refiller.

---

## 2. The mechanism

### 2.1 `vu` is sufficient as a *gate*, insufficient as *state*

Today `vu` (valid-until, epoch ms) carries **three** meanings on one attribute:

| # | Meaning | Written by | Read by |
|---|---------|------------|---------|
| 1 | Fast-path gate: `vu > now` ⇒ `tk` was materialised under params still in force | slow path, aggregator | `_speculative_consume_single`'s condition |
| 2 | "The operator changed something here" — `vu = 0` unconditionally | `_sync_bucket_params` / `bucket_sync` fan-out | the forced materialising pass |
| 3 | Optimistic-lock pin, so a refill cannot ride a pre-fan-out stream image (#508) | — | `try_refill_bucket`'s `#vu = :expected_vu` |

For the **calendar** form these coexist because the reset decision is not taken from `vu`.
`_apply_reset_edge` asks `prev_reset_edge(limit.reset_schedule, now) > state.last_refill_ms` —
derived from the cron and `rf`, never from `vu`. A fan-out's `vu = 0` forces a pass, the pass
finds no missed cron edge, and nothing is refunded.

A rolling window has no cron. If its expiry state *is* `vu`, meaning 2 collides with meaning 1:

> `set_limits()` fans out `vu = 0` to **every shard of every bucket for that entity** (unscoped
> `_default_` widens it to every resource, #487). The next acquire on each would read
> `vu <= now` as "your 5-hour window ended", reset `tk` to capacity and open a fresh window.

An operator raising an unrelated `rpm` ceiling would hand every caller a free session quota and
restart every caller's clock — silently, with `tc` still climbing so usage aggregation looks
normal. Hence separate state, and hence the correction in §7.2.

### 2.2 What the bucket item carries

Two new per-limit attributes:

| Attribute | Type | Meaning |
|-----------|------|---------|
| `b_{name}_ws` | `N` (epoch ms) | Start of the current window. **Absent** = the window has not started. Entity-wide fact, replicated to every shard. |
| `b_{name}_rwin` | `N` (seconds) | Window length, denormalised from config so a materialiser needs no read. |

`we` (window end) is **derived**, not stored: `we = ws + rwin × 1000`. Storing one scalar rather
than two removes the possibility of the pair disagreeing after a partial write, and `rwin` has
to be on the item anyway so the aggregator can compute the next window without a config read.

Config gains `l_{name}_rwin`. `vu` stays exactly what it is — a derived minimum, gaining one
member:

```
vu = min(next param change, next reset edge, ws + rwin)
```

**Per-limit, not item-level-plus-override.** The `sched`/`rsched` machinery (`_encode_one_tuple`,
`BUCKET_SCHED_NONE`) exists because limits usually *share* a schedule. Window starts do not:
the motivating product is a 5-hour session cap **beside** a weekly cap, whose windows differ by
construction. The override table would degenerate to all-overrides.

**Item size:** ~8 B name + 8 B value for `ws`, ~10 B + 3 B for `rwin`. **~30 B per rolling
limit**, against §4.2's 479 B unscheduled baseline and the 1 KB WCU cliff.

### 2.3 The per-shard rule: `ws > rf`

This is the whole coherence mechanism, and it is not new — it is `_apply_reset_edge` with the
cron scan replaced by an attribute read:

```python
# calendar, today (limiter.py:1453-1459)
edge = prev_reset_edge(limit.reset_schedule, now_ms)
if edge is not None and edge > state.last_refill_ms:
    state.tokens_milli = state.effective_capacity_milli(now_ms)

# rolling, proposed — same shape, same idempotence, same idle-bucket handling
if state.window_start_ms is not None and state.window_start_ms > state.last_refill_ms:
    state.tokens_milli = state.effective_capacity_milli(now_ms)
```

Every property the calendar version was designed for carries over verbatim:

- **Idempotent.** It is a set, not an add. Two shards applying the same `ws` twice, or one shard
  seeing it on two successive passes, converge.
- **Idle buckets are correct for free.** A shard idle across three window boundaries applies one
  reset on wake, because `ws` holds only the current window's start.
- **Strictly `>`.** The pass that applies the reset stamps `rf` at or after `ws`, so `>=` would
  re-fire on every later request and refund everything spent since — an unbounded quota. Same
  reasoning as limiter.py:1435-1437.
- **Per shard, to the shard's share.** `effective_capacity_milli(now)` already applies the
  parameter schedule and then divides by `shard_count`. Unchanged.
- **`tc` untouched.** The counter stays monotonic (`.claude/rules/design-validation.md`).

The decisive consequence: **the fan-out only has to move a scalar.** It never writes `tk`.

### 2.4 Why the fan-out must not carry `tk`

Worth stating explicitly, because it is the trap. Suppose the fan-out reset every sibling's
balance directly. `tk` deltas in this codebase are always `ADD` — commutative with concurrent
speculative writes, which is the property the whole write model rests on. A fan-out cannot use
`ADD` because it does not know each sibling's current `tk`; it would have to `SET b_{n}_tk = :share`.
That blind `SET` races the sibling's own slow path:

- fan-out lands **after** the sibling's slow path ⇒ the sibling's consumption is clobbered and
  refunded;
- fan-out lands **before** ⇒ the sibling's `rf` lock still holds, its `ADD (share − stored + consumed)`
  applies on top of the already-set `share`, and the shard ends at `2×share − consumed`.

Both are over-admission. The `ws > rf` rule removes the question: the sibling resets itself,
under its own `rf` lock, in the same write that records its consumption — which is exactly the
write it was going to make anyway.

### 2.5 Mechanism options, costed

| # | Mechanism | Shard-coherent? | Fast path | Marginal cost | Verdict |
|---|-----------|-----------------|-----------|---------------|---------|
| A | Overload `vu` as the window end | — | 1 WCU, unchanged | — | **Broken** (§2.1) |
| B | `ws` + `rwin`, no fan-out; each shard windows independently | **No** | 1 WCU, unchanged | 0 | **Dropped.** Staggered per-shard windows; `resets_at_ms` has no single value (§3.1) |
| **C** | **B + `ws` fan-out + `ws > rf` + redistribution on doubling** | **Yes** | **1 WCU, unchanged** | **(S−1) WCU/rollover + 0.5 RCU once per shard** | **Recommended — the floor, not an upgrade** |
| D | Anchor on entity `META`, cached | Yes | unchanged | 0 extra RCU | **Rejected.** Window state is per-(entity, resource, limit); META would need unbounded flat attributes and becomes a write hot spot at every rollover — the hot-partition problem one level up |
| E | Per-entity offset into a fixed grid | Yes | unchanged | 0 | Rejected by ADR-138; also not first-use anchored (a new entity's first window can be one minute long) |
| F | Fixed start instant, projected multiples | Yes | unchanged | same as C | Rejected by ADR-138; strictly worse than C — cannot express "go idle long enough and your window restarts" |

Per the correction, **B is dropped rather than presented**: it is not viable, because §3.1 shows
its failure is in the feature's headline number, not in its cost.

---

## 3. Sharding, answered

### 3.1 Why a per-shard window is not an option

Under B each shard stamps its own `ws` at its own first post-expiry use, so an entity has S
window starts drifting apart by however long each shard waits to be drawn.

**Over-admission is not the reason to reject it.** A fixed window of length `W` admits up to
`2 × capacity` across any observation window of length `W`, by draining just before an edge and
again just after; that is inherent to fixed windows and is true of the calendar form today. With
S staggered shards each contributes at most `2 × (C/S)` over any `W`-length window, for the same
total of `2C`. Staggering redistributes *when* the allowance arrives; it does not loosen the
ceiling. Any claim that drift lets an entity exceed its quota is wrong, and it is worth saying so
because it is the obvious thing to assert.

**The reason to reject it is that `resets_at_ms` stops existing.** `check_availability()` sums
across every shard via GSI3 and returns one `Availability` stamped at one `checked_at_ms` (#472).
With staggered windows there is no honest answer to "when does my window reset": `min(ws) + W`
over-promises (the user is told 19:00 and gets back 1/32 of the quota), `max(ws) + W`
under-promises (told 20:30 while 31/32 came back at 19:00). For a *rate* limit that ambiguity is
invisible because a drip is continuous. For a **session cap shown to a human** — the entire
motivating product — it is the headline number. A feature whose headline number is undefined
under sharding is not shipped.

### 3.2 What sets `ws` on a shard created mid-window

The hard case from the correction: a client creating shard N>0 may never have read another shard.
It reached that state because the fast path returned `BUCKET_MISSING` on a shard it drew from a
`shard_count > 1` it learned from the entity cache or a failure image — so it has seen the
*entity*, but `ws` lives on shard *items*.

**Answer: the shard-create path reads a sibling in the `BatchGetItem` it already issues.**

The slow path for a shard create already does `batch_get_entity_and_buckets` (META + the target
bucket). Adding shard 0's key costs **+0.5 RCU** (eventually consistent, and this path is already
priced at 2.5 RCU + 2 WCU in CLAUDE.md), and it happens **once per shard** — at most 31 times per
(entity, resource) for the whole lifetime of the sharding, plus TTL recreations. It is not a hot
read: a hot entity creates each shard exactly once.

Shard 0 is the natural source because `bump_shard_count()` already treats it as the source of
truth for `shard_count` (repository.py:3030). The created shard inherits `ws` verbatim and sets
`rf = now`, so `ws > rf` is **false** on the new item and it does not immediately re-reset itself.
It starts at the new share — see §3.4 for why that is exactly balanced rather than an over-issue.

**When shard 0 is absent** (its TTL swept it; possible only for resource- and system-level
configs, since ADR-136 gives entity-level buckets no TTL) there is no `ws` to inherit and the
new shard starts a fresh window. That is the degraded case, and it is the same durability
asymmetry §5.4 records: a rolling window's state is not recoverable from the clock, so anything
that loses every item loses the window.

### 3.3 Does `_propagate_shard_count()`'s shape work? Yes — `ws` *is* monotonic

The correction asks whether a window end is monotonic across periods. **It is, and this is the
load-bearing detail.** Window *n+1* opens at a clock reading strictly after window *n* closed:

```
ws₀ < ws₀ + W ≤ ws₁ < ws₁ + W ≤ ws₂ < …
```

so `ws` is strictly increasing over the life of the bucket, exactly like `shard_count`. The
`_propagate_shard_count()` shape therefore transfers with one substitution:

```
UpdateExpression:    SET #ws = :new_ws, #vu = :zero
ConditionExpression: attribute_exists(PK)
                       AND (attribute_not_exists(#ws) OR #ws < :new_ws)
```

issued concurrently to shards `0..S-1` other than the writer's own, `asyncio.gather`-style, with
`ConditionalCheckFailedException` swallowed as a no-op — byte-for-byte the structure of
repository.py:3132-3158.

What the guard buys, in the same terms `_propagate_shard_count`'s docstring uses:

- **Idempotent.** Re-running a rollover writes nothing the second time.
- **Race-free against a concurrent roller.** Two clients crossing the boundary milliseconds apart
  produce two `ws` values; the later wins and the earlier no-ops. Both are within clock skew of
  the same instant, and the window is at most `W` long either way — never longer.
- **Race-free against a delayed write.** A client whose rollover write is delayed past the *next*
  boundary carries a stale `ws` that is now smaller than the stored one, and the condition
  rejects it. Without monotonicity that write would drag every shard back a full period.
- **Safe under `--no-aggregator`.** The client owns the fan-out, exactly as `bump_shard_count()`
  already owns shard-count propagation for that reason (repository.py:3085-3091).

**`vu = 0` rides along** for the same reason the #468 fan-out writes it: it forces the sibling to
take one materialising pass, which is where `ws > rf` is evaluated and the shard's own `tk` is
reset. Strictly speaking a sibling's `vu` is usually already expired — its own `ws + rwin` was the
`min`, and that is what just elapsed — but not always (a sibling created before the window was
configured has no `ws` and a `vu` dominated by a cron boundary), so stamping it unconditionally is
simpler than reasoning about which siblings need it. **Cost:** the aggregator's `#vu = :expected_vu`
pin (#508) sees the change and skips one refill per shard per rollover. That is a missed top-up,
self-healing on the next batch, not a correctness failure.

**Who fans out.** Whoever first materialises a shard with `now ≥ ws + rwin` computes
`ws_new = now`, applies its own reset under its own `rf` lock, and fans `ws_new` to the siblings.
The new window is anchored to `now` — the first use after expiry — which is what "rolling" means;
anchoring to `ws_old + W` would be a fixed grid offset by the first-ever use, which is option F.

### 3.4 A doubling landing mid-window: redistribute, do not issue

This is where the correction's item 4 lands, and the answer is the same for calendar quotas.

**The defect.** `BucketState.from_limit()` (models.py:1119-1146) creates a new shard at
`effective_capacity_milli(now)` = `cp // shard_count`. For a **dripping** limit that is correct —
the shard would have refilled to its share anyway, which is what CLAUDE.md's "shard creation never
multiplies total capacity" means. For a **quota** it is false: a quota shard spent to zero never
refills, so a doubling S → 2S creates S shards holding `C/2S` each and grants the entity `C/2` of
net-new allowance inside a period where the contract says it has none. Nothing trims it —
`refill_bucket`'s clamp is `min(cap, tk)` and each new shard sits exactly at its share, so the
surplus exists only in the entity-wide sum, which no writer computes.

**Neither obvious branch is acceptable.** New shards starting *fresh* is the over-issue above.
New shards starting at `tk = 0` converts write pressure into spurious rejections — the drawn shard
cannot admit, `_MAX_SHARD_RETRIES = 2` exhausts, and the doubling fails to relieve the partition
it was triggered to relieve. Deferring the doubling to the next rollover pins a hot entity to one
partition for up to `W` = 5 hours, which is the GHSA-76rv condition.

**The answer: a doubling redistributes the unspent balance rather than issuing new tokens.**

```
on doubling S → 2S, for every quota-shaped limit on the item:
    ADD b_{name}_tk  −(old_share / 2)      on each existing shard 0..S-1
    new shards S..2S-1 materialise lazily at the new share, C/2S
```

Total is preserved **exactly**: `S × (old_share/2)` is removed and `S × (C/2S)` is added, and
`old_share/2 = C/2S`. Worked through, S = 2 → 4, C = 1000:

| | shard 0 | shard 1 | shard 2 | shard 3 | total unspent |
|---|---|---|---|---|---|
| before, both untouched | 500 | 500 | — | — | **1000** |
| after debit of 250 each | 250 | 250 | — | — | 500 |
| after lazy create at 250 | 250 | 250 | 250 | 250 | **1000** ✓ |
| before, 0 spent out, 1 untouched | 0 | 500 | — | — | **500** |
| after debit | −250 | 250 | — | — | 0 |
| after lazy create | −250 | 250 | 250 | 250 | **500** ✓ |

Negative balances are meaningful and already supported: the entity really has spent that
allowance, and the shard correctly admits nothing until the next `ws`.

**Why it is safe, and why it is free:**

- **`ADD`, not `SET`.** Commutative with concurrent speculative consumption, for the identical
  reason the aggregator's negative clamp delta is (§3.3 of the design doc). No read required.
- **Exactly-once per shard.** `bump_shard_count()` writes shard 0 under `shard_count = :old` —
  exactly-once by construction, since only one racer can see `:old`. `_propagate_shard_count()`
  writes shards `1..S-1` under `shard_count < :new` — at-most-once per shard per count value.
  Together they cover precisely the set `0..S-1` that needs debiting, each exactly once. **This
  is the guarantee an `ADD` needs, and it already exists.**
- **Zero extra round trips.** The debit rides on writes those two functions already issue:
  `SET shard_count = :new ADD b_{q}_tk :debit`.
- **Quota-shaped limits only.** One `is_quota` test at the two doubling sites. `wcu` is never
  divided and so never debited; dripping limits keep today's behaviour exactly.
- **Mirrored in the aggregator.** `processor.propagate_shard_count` and the proactive Path 1
  doubling need the same debit, under the same `attribute_not_exists(shard_count) OR shard_count < :new`
  guard, which has the same at-most-once property. A client and the aggregator doubling
  concurrently to the same `:new` means one wins the condition per shard and the other no-ops.
- **A lagging lazy create is conservative.** The debit lands at doubling time; the credit lands
  when each new shard is first drawn. In between the entity's total is *under* its allowance,
  which is the safe direction, and it self-corrects as shards materialise.

**This fix is required for calendar quotas today, independent of rolling windows.** That is the
basis for the sequencing in the BLUF.

### 3.5 Cost, in the right budget

Marginal cost of rolling-vs-calendar, per rollover per entity, at S shards:

| Component | Cost | Notes |
|---|---|---|
| `ws` fan-out | **(S−1) WCU** | 31 WCU at S=32; **0 WCU at S=1**, which is most entities |
| Shard-create sibling read | +0.5 RCU | **once per shard**, ≤ 31 times per (entity, resource) ever |
| Per-shard materialisation | — | **not a marginal cost**; the calendar quota pays exactly this at every reset edge |
| Doubling debit | — | rides existing writes; ≤ 5 doublings per entity ever |
| **Per acquire** | **unchanged** | 0 RCU + 1 WCU on success, 0/0 on fast rejection |

Absolute figures, 5-hour window (4.8 rollovers/day):

| Scenario | WCU/day | $/month |
|---|---|---|
| 10k entities, **all** at S=32 (deliberate worst case) | 1.49M | **≈ $28** |
| 10k entities, 1% at S=32, rest at S=1 | 15k | **≈ $0.28** |
| 10k entities, all at S=1 | 0 | **$0** |

**The comparison that matters is not $28 against $0.** Per-acquire cost is what this project
defends, and it is untouched; a per-rollover cost is a different budget, on a path closer to
admin than to the hot path. $28/month is about 45M acquires' worth of WCU — an account running
10k entities against a 5-hour cap is doing far more than 45M acquires a month, so the fan-out is
a sub-percent line item. And the alternative is not $0: it is an entity that cannot leave a
single DynamoDB partition, capped at ~1000 WCU/s with throttling rather than graceful
degradation beyond it, which is the published advisory's condition and an outage rather than a
line item.

---

## 4. The aggregator

`try_refill_bucket` reads the item and nothing else, refills under an `rf` + `vu` optimistic
lock, and must never refill toward a stale ceiling. Three changes, all mirroring `rsched`:

1. **`_parse_bucket_record` reads `b_{name}_ws` / `b_{name}_rwin`** into `LimitRefillInfo`,
   beside the `sched` / `reset_sched` it already parses. No new failure mode — these are
   integers, not a compact grammar, so there is no `sched_error` analogue and nothing new can
   poison a batch.
2. **A roll branch beside the reset branch** (processor.py:839-847), evaluated *before* the
   `is_accrual_rate` guard for the identical reason: a rolling window is a quota, its stored rate
   is 0 by ADR-137, and that guard would skip exactly the limits the feature exists for. Same
   `ADD (effective_cp − tk_observed)` delta shape, same commutativity argument, with the
   `ws > state.rf_ms` test in place of `prev_reset_edge(...) > state.rf_ms`.
3. **`wcu` must be exempt.** `rsched` is item-level, so a user's midnight reset would otherwise
   hand `wcu` its per-partition write ceiling back at every edge — hence the explicit carve-out
   at processor.py:799-804. `ws` as specified is per-limit, so `wcu` simply never carries one and
   the exemption is **structural rather than a special case**. A small improvement over the
   calendar form.

**Fan-out ownership.** The aggregator should roll a shard it sees but **not** fan out, leaving
that to the client. It processes one bucket shard per stream record and would fan out once per
shard per batch — S² writes rather than S. The client's fan-out plus the `ws > rf` rule already
converges every shard, and the aggregator's roll is an optimisation on top.

**What it does not require.** The aggregator acts only on stream records, and an exhausted quota
bucket produces none (fast rejections are 0 WCU), so it cannot roll a window for an idle entity —
which is correct, since the window must be anchored to a *use*. When it does roll one it stamps
the instant it processes the record, a batch's lag behind the client's reading: single-digit
seconds against a window measured in hours. The two are allowed to disagree by that much, and
`ws < :new` resolves it in favour of the later.

`_item_next_boundary()` gains `ws + rwin` as a third voting member. The "item-level pair is a
member in its own right" reasoning (#541) does not apply — `ws` has no item-level default.

---

## 5. Everything else, point by point

### 5.1 Storage encoding — it does **not** fit `sched`/`rsched`, and should not try

| Option | Verdict |
|--------|---------|
| A new token inside `rsched`, e.g. `R18000` | **Rejected.** `decode_reset` deliberately *rejects* unknown modifier tags rather than ignoring them, on the argument that a silently-misread reset is the worst available outcome. A token makes every entry conditionally-a-cron, and `cycle_seconds` / `prev_reset_edge` / `next_reset_edge` / `to_cron` each grow a branch. |
| A versioned encoding (#515, deferred) | **Not required.** §4.1's argument holds: a marker cannot classify anything already written, so it only works forward. |
| Plain integer attributes (`l_{name}_rwin`, `b_{name}_rwin`, `b_{name}_ws`) | **Recommended.** Additive, unambiguous, no grammar change, no new decoder, ~30 B. |

**Compatibility is unchanged, and this is easy to overstate.** An old client reading a
rolling-window config item sees `l_{name}_ra = 0` with no `rsched`, constructs
`Limit(refill_amount=0, reset_schedule=())`, and `Limit.__post_init__` raises (#538's shape). So
rolling windows are not backward-readable — **and neither is the calendar form**: a pre-#222
client reading a calendar quota sees the same zero rate and raises identically. This is a
property of ADR-137, not of rolling windows, and the fleet-upgrade requirement already exists.

### 5.2 ADR-137 — the *rule* holds, the *predicate* widens

A rolling window is a quota: `refill_amount = 0`, recovery in a lump. "A limit recovers by drip or
by reset edge, never both and never neither" is unchanged in substance — a rolling window is a
third *spelling* of the reset half, not a mechanism running underneath a drip. What changes is
every structural test:

- `Limit.__post_init__`: `refill_amount == 0 and not reset_schedule` → raise, widens to
  `and window_seconds is None`;
- `Limit.is_quota`: `bool(self.reset_schedule)` → `or self.window_seconds is not None`;
- `Limit.from_bucket_state` and `Limit.per_shard`, which move the `max(1, …)` rate floor and
  `reset_schedule` together (models.py:667, 727) — `window_seconds` must join that pair or a
  reconstructed rolling limit becomes unconstructible;
- `schema._recovery_seconds` and `exceptions._limit_shape`, both branching on `is_quota`.

Mechanical, six sites, each load-bearing. §2.2's exclusion rule ("at most one *function* per
bucket") is unaffected: a rolling window is not a function of `now` applied to the base params,
so it composes with `schedule` exactly as `reset_schedule` does.

### 5.3 TTL — free, and sharper than the calendar form

`calculate_bucket_ttl_seconds(limits, multiplier)` deliberately holds no clock, and its three
production callers have none to pass. `_recovery_seconds` prices a quota by its reset **cycle**.
A rolling window's cycle is **`window_seconds`, exactly and by construction** — no cron parse, no
`cycle_seconds` ladder, no rounding-up approximation, no clock:

```python
if limit.is_quota:
    if limit.window_seconds is not None:
        return float(limit.window_seconds)
    return float(min(_reset_cycle_seconds(entry) for entry in limit.reset_schedule))
```

Strictly sharper than the calendar branch, which rounds a monthly pattern up to 31 days and an
annual one to 366.

At the default multiplier of 7, a 5-hour window gives a 35-hour TTL, so an entity idle 35 hours
has its bucket swept and recreated at full capacity with a fresh window — arguably *correct* for
a rolling window ("go idle long enough and your window resets"), unlike the calendar case where
ADR-136 has to argue the sweep is bounded. ADR-136 also means entity-level configs carry **no**
TTL, so a per-entity session cap — entity-level by nature — is never swept. Only resource- and
system-level rolling windows reach the formula.

### 5.4 `retry_after_seconds` and `resets_at_ms` — cheaper, but one signature is wrong

The computation is `ws + rwin − now`, read straight off the item; no scan.
`retry_after_with_schedule`'s reset-edge branch (schedule.py:961-976) currently calls
`next_reset_edge(reset_sched, now_ms=cursor)`, a bounded cron scan; for a rolling window the edge
is a constant that dominates on iteration one exactly as a quota's does today.

**The obstacle is that `resets_at_ms` is computed from the `Limit` alone.**
`RateLimitExceeded._limit_shape()` (exceptions.py:162-166) does:

```python
return {"kind": "quota", "capacity": limit.capacity,
        "resets_at_ms": next_reset_edge(limit.reset_schedule, now_ms=now_ms)}
```

A calendar edge is recoverable from the clock plus the config, so a `Limit` suffices. A rolling
window's `ws` lives on the bucket item. So `LimitStatus` must carry it — a new optional field
populated at all four construction sites (`bucket.declared_statuses`, `RateLimiter._admit_limit`,
`lease._build_retry_failure_statuses`, `RateLimiter.check_availability`) — and `_limit_shape` must
take the status rather than the limit. A change to the 429 body's plumbing, not to its JSON.

Under mechanism C, `check_availability` reports **one** value across shards, because every shard
carries the same `ws`. That is the whole point of §3.1.

### 5.5 Cold start and expiry — the honest asymmetry

| Situation | Calendar quota | Rolling window |
|-----------|----------------|----------------|
| Bucket never written | Full capacity; `resets_at_ms` = next cron edge, computable with no item | Full capacity; `resets_at_ms` = **`None`** — "resets `W` after you first use it" is the only honest answer |
| TTL swept mid-window | Recreated at full capacity; the next edge is still the same calendar instant | Recreated at full capacity **and** with a fresh window — balance and clock both restart |
| **All** shards swept | Same | Window lost; next use starts a new one |
| **Some** shards swept | Same | Survivor's `ws` is inherited on re-create (§3.2), so the window survives |
| Entity idle past `ws + W` | n/a | Next use opens a new window at that use. **Yes, an entity can reset its own window by going idle** — the intended semantics |
| Entity idle *within* the window | Nothing | Nothing; `ws` untouched |

ADR-138 already names the asymmetry in Consequences:

> "Missed edges remain idempotent, because a calendar edge is recoverable from the clock while a
> per-entity anchor would have to survive item expiry."

That sentence is **correct and is the strongest surviving argument in the record** — stronger than
the scope argument the Decision rests on. §3.2's sibling inheritance narrows it to "all shards
lost simultaneously", which for an entity-level config (no TTL) means a purge or a manual delete.
It does not eliminate it.

---

## 6. What would have to change, file by file

| File | Change | Size |
|------|--------|------|
| `src/zae_limiter/models.py` | `Limit.window_seconds`; widen `__post_init__`'s ADR-137 predicate; `is_quota`; `from_bucket_state`; `per_shard`; `to_dict`/`from_dict`; `BucketState.window_start_ms` + `window_seconds`; `LimitStatus.resets_at_ms` | M |
| `src/zae_limiter/schema.py` | attribute-name constants; `_recovery_seconds` quota branch (one line) | S |
| `src/zae_limiter/repository.py` | config serialise/deserialise (`l_{name}_rwin`); `build_composite_create` (stamp `rwin`, inherit `ws`); `build_composite_normal` (SET `ws`); `_deserialize_composite_bucket`; `_sync_bucket_params` (stamp `rwin`, **never** touch `ws`); **`_propagate_window_start()`**; **sibling key in the shard-create `BatchGetItem`**; **quota debit in `bump_shard_count` + `_propagate_shard_count`** | **L** |
| `src/zae_limiter/limiter.py` | `_apply_window_roll()`; fan-out call site; `_materialisation_stamps()` third member; `check_availability` | M |
| `src/zae_limiter/lease.py` | `LeaseEntry._window_start_ms`; `_commit_initial` re-expression for a window ending between the two clock readings (mirroring `_reset_edge_ms`, lease.py:434); `_build_retry_failure_statuses` | M |
| `src/zae_limiter/exceptions.py` | `_limit_shape` takes a status, not a limit | S |
| `src/zae_limiter/bucket.py` | thread `ws` into statuses | S |
| `src/zae_limiter_aggregator/processor.py` | `_parse_bucket_record`; roll branch; `_item_next_boundary`; **quota debit in `propagate_shard_count` and proactive Path 1** | M |
| `src/zae_limiter_provisioner/{manifest,differ,handler,bucket_sync}.py` | `window` in the manifest; diff; `_coerce_int`; the sync mirror of the `rwin` stamp | M |
| `limits_cli.py`, `cli.py` | `Window:` display; CFN `Window` property round trip | S |
| `sync_*.py` | generated — `hatch run generate-sync` | free |
| `tests/` | unit (roll arithmetic, monotonic fan-out guard, **the doubling redistribution table of §3.4**, `is_quota` widening, TTL horizon); integration (`vu` still gates with **zero config reads**, asserted with `capacity_counter` — the load-bearing claim; fan-out convergence across 4 shards; a doubling mid-window preserving the total); E2E with `window_seconds: 2`, real waiting, marked `slow`, **with and without the aggregator** | **L** |
| `docs/` + an ADR superseding ADR-138's deferral | — | M |

**Effort: 9–11 PRs, ~3 weeks.** Broad but *shallow* relative to #222 — no cron, no timezone
handling, no DST, no boundary scan, no encoding grammar, no oracle test.

---

## 7. Things I believe are now inaccurate

Reported, not fixed — this is an analysis and touches no source.

### 7.1 CLAUDE.md: "shard creation never multiplies total capacity" is false for a quota

> "A missing shard N>0 is created by the client with `capacity_milli // shard_count` tokens … so
> shard creation never multiplies total capacity."

True for a dripping limit, false for a quota since ADR-137, for the reason and with the fix in
§3.4. **This is a live defect in shipped calendar-quota behaviour, independent of this feature,
and it should be fixed on its own first.** Design §3.6 addresses the *reset* target ("resetting
every shard to the undivided capacity would multiply the entity's quota") but not the *create*
path or the doubling.

### 7.2 ADR-138: "needs no anchor field at all, because the valid-until stamp *is* the anchor"

Correct about anchoring; false about this codebase, because `vu` is also the fan-out marker
(`vu = 0`) and the aggregator's optimistic-lock pin (#508). A rolling window reading its expiry
off `vu` inherits meaning 2 and refunds every quota on every `set_limits()` (§2.1). I would not
re-open an Accepted ADR for this, but left uncontradicted the sentence reads as a design
instruction, and an implementing ADR must say so explicitly.

### 7.3 ADR-138: "It is in fact cheaper than the calendar form"

Half right. Cheaper on evaluation (no cron, no tzdata, no DST, no scan), on TTL (§5.3) and on
`retry_after` (§5.4). **More** expensive on storage, on shard coherence (§3 — the calendar form
gets this free because every shard shares one clock and one cron) and on durability (§5.5). Net:
comparable, differently distributed, not cheaper.

### 7.4 ADR-138 rests on the weaker of its two arguments

The Decision says scope; the Consequences say "a per-entity anchor would have to survive item
expiry". The second is the real design argument, survives this analysis (narrowed but not
eliminated by §3.2), and is what an implementing ADR will actually have to answer. Worth
promoting if the record is revisited.

### 7.5 This analysis's own first draft

It recommended suppressing `bump_shard_count()` on rolling-window buckets. That would have
reintroduced the GHSA-76rv-2r9v-c5m6 condition for exactly the traffic shape the feature targets.
Recorded here rather than silently removed, because the reasoning that produced it — treating
sharding as a cost to be traded away rather than as a security control — is the failure mode a
future reader is most likely to repeat.

---

## Related

#222, #478 (epic), **GHSA-76rv-2r9v-c5m6**, ADR-133, ADR-134, ADR-136, **ADR-137**, **ADR-138**,
#439 (client-side shard creation), #468/#481/#487 (fan-out infrastructure), #472/#473
(`check_availability`), #474 (independent parent sharding), #475 (per-shard statuses), #508 (the
`vu` pin), #515 (deferred version marker), #532/#557/#574 (TTL and scan horizons), #541
(`BUCKET_SCHED_NONE`), #545 (`resets_at_ms`).
