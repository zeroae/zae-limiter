# ADR-139: Duration reset windows anchored to first use

**Status:** Proposed
**Date:** 2026-09-15
**Issue:** [#222](https://github.com/zeroae/zae-limiter/issues/222)

## Context

ADR-137 gives a limit two ways to recover: a drip, or a reset that restores the balance in one
lump. ADR-138 decided that a reset is named by a cron expression, so every entity sharing a
schedule resets at the same wall-clock instant. That is what a billing period needs.

A session cap needs the other shape: "10,000 tokens, and your window resets five hours after you
first used it". Two entities that first call at 09:00 and 14:30 have windows ending at 14:00 and
19:30. This is the behaviour of widely deployed allowance systems, including the one that
motivated `reset_schedule`, and a reader who sees "five-hour window" will assume it.

A duration cannot be written in cron, so this is a second field rather than a second reading of
the first. Feasibility, costing and the alternatives considered are in
`docs/plans/2026-09-15-rolling-session-windows-analysis.md`.

This record **lifts ADR-138's deferral** of duration-based windows to a later release.
ADR-138's own decision — that `reset_schedule` itself names only fixed calendar windows,
expressed as cron — is unaffected and stays exactly as written there: this is a new mechanism,
`Limit.reset_after`, not a reinterpretation of `reset_schedule`. A limit carries one or the
other and never both (ADR-137).

## Decision

`Limit` gains **`reset_after: timedelta | None`**. A limit with `reset_after` set is a quota
(`refill_amount = 0`, ADR-137): its balance is restored in one lump when the window elapses, and
does not drip in between.

**One recovery mechanism per limit.** `reset_after` and `reset_schedule` are mutually exclusive,
rejected together in `Limit.__post_init__` by the same cross-field rule ADR-137 already uses for
the drip/reset pairing. They are two spellings of the reset half, not two mechanisms that
compose: a limit that both reset at midnight and rolled five hours from first use would restore
its allowance twice over some periods and once over others, with no reading of "the allowance" to
which either is faithful.

**The window is idle-restarting, not tiling.** Windows do not run forward from the first-ever use
regardless of activity. A window that elapses while the entity is quiet is simply over; the next
use anchors a fresh one at that instant. That is what "anchored to your own activity" means, and
it is also what makes ADR-138's durability objection moot — see Consequences.

**Only a persisted materialising pass anchors a window.** A request rejected because the quota is
exhausted inside the current window does not move the anchor; the entity is still in the window
it already anchored. This falls out of the write model rather than being enforced: an exhausted
quota's rejection is a 0-WCU fast rejection on the speculative path, and on the slow path
`RateLimitExceeded` is raised before any write (`.claude/rules/write-on-enter.md`).

### Storage and shard coherence

The window **start** `ws` is stored as per-limit bucket state (`b_{name}_ws`, epoch ms) beside the
window length `rsa` (`b_{name}_rsa`, seconds), denormalised from config so a materialiser needs
no config read. The window *end* is derived, never stored, so the pair cannot disagree after a
partial write. Config carries `l_{name}_rsa`.

The anchor is **not** `vu`. Beyond gating the fast path, `vu` carries the marker a limit-change
fan-out stamps (`vu = 0`, #468/#487) and the staleness pin the aggregator adds to its refill
condition (#508). A window reading its expiry off `vu` would read every `set_limits()` fan-out as
an elapsed window and silently restore every caller's balance and restart every caller's clock.
`vu` gains `ws + rsa` as a third voting member of its minimum, which is all it needs.

**Each shard resets itself when it sees `ws > rf`.** This is `_apply_reset_edge`'s existing rule
with the backwards cron scan replaced by an attribute read, and every property that rule was
designed for carries over: it is a set rather than an add, so it is idempotent; an idle shard
applies one reset on wake however many windows it slept through, because `ws` holds only the
current window's start; the comparison is strictly `>` because the pass that applies the reset
stamps `rf` at or after `ws`; the target is the shard's share of the effective capacity, so
resetting does not multiply the entity's quota by `shard_count`; and `tc` is never touched, so the
consumption counter stays monotonic.

**The rollover fan-out moves a scalar and never `tk`.** Whoever first materialises a shard past
`ws + rsa` anchors `ws_new = now`, applies its own reset under its own `rf` lock, and fans
`ws_new` (with `rsa`, and `vu = 0`) to the entity's other shards with concurrent conditional
writes under `attribute_not_exists(ws) OR ws <= ws_new - rsa` — a sibling moves only if its own
window had already ended by `ws_new`, the same half-open rule the opener applied to itself.
Idempotent and monotonic, in the shape `_propagate_shard_count()` uses; `ws` is monotonic because
window *n+1* opens at a clock reading strictly after window *n* closed.

The floor, rather than a plain `ws < :new`, is what keeps **concurrent openers** from resetting
each other. Two clients crossing the boundary milliseconds apart each open a window on their own
shard and each fan out. Under `ws < :new` the later value overwrote the earlier opener's own
shard, whose `rf` is its own `now`, so `ws > rf` held and that shard reset a second time inside
one window — over-admission. Under the floor both fan-outs no-op on the other's shard. The
residual cost is that those shards stay staggered by the openers' few milliseconds until the
next window; the entity still admits at most one allowance per window.

A fan-out that carried `tk` would be unsafe in both orderings and cannot be made safe: token
deltas in this codebase are always `ADD`, which is what makes them commutative with concurrent
speculative writes, and a fan-out cannot use `ADD` because it does not know each sibling's
balance. The blind `SET` it would need either clobbers a sibling's committed consumption or
lands under the sibling's still-held `rf` lock and leaves it at twice its share. The `ws > rf`
rule removes the question.

A shard created mid-window inherits `ws` from shard 0, read once per shard on the create path.
Cascade parents and children anchor **independently**: the parent's window does not track the
child's, consistent with cascade already treating limits, shards and `disabled` as per-entity
state. That independence is not free at shard-create time — the inheritance read is scoped to one
entity's shards, so a cascade slow path creating a parent shard resolves the *parent's* `ws` with
its own read rather than reusing the child's. It costs 0.5 RCU on the cascade shard-create path,
once per parent shard, and it is what stops a busy child's window from silently becoming its
parent's.

## Consequences

**Positive:**
- Entities do not reset simultaneously, which removes the thundering herd ADR-138 records as its
  strongest negative. The spread is a property of the anchor, not of added jitter.
- Evaluation is cheaper than the calendar form: no cron parse, no timezone database, no
  daylight-saving handling and no boundary scan. `retry_after_seconds` and `resets_at_ms` are
  `ws + rsa` read off the item, a constant rather than a scan.
- The TTL recovery horizon is the window length **exactly**, with no rounding up and no clock —
  sharper than the calendar branch, which rounds a monthly pattern to 31 days.
- The per-acquire cost is unchanged. The speculative condition is byte-identical and reads no
  config.

**Negative:**
- A rollover costs (S − 1) writes to keep an entity's shards on one window, where S is
  `shard_count`. Zero for the unsharded majority; 31 at `MAX_SHARD_COUNT`, once per window. A
  per-shard window would cost nothing and is not available: `check_availability` would then have
  no honest answer to "when does my window reset", which is the feature's headline number.
- The window state is not recoverable from the clock, so losing every shard of a bucket loses the
  window. **ADR-138 records this as the argument that survives closest scrutiny, and
  idle-restarting answers it.** A bucket's TTL is `recovery_horizon × multiplier` and this
  shape's horizon **is** the window length, so an item swept by TTL means the entity has been idle
  for roughly `reset_after × multiplier` — 35 hours for a 5-hour window at the default multiplier
  of 7. Under idle-restarting, a swept item starting a fresh window on the next request is
  **exactly the specified behaviour**, not data loss. ADR-136 also gives entity-level configs no
  TTL at all, and a per-entity session cap is entity-level by nature, so the formula is reached
  only by resource- and system-level rolling windows. What remains is a purge or a manual delete,
  which loses the balance as well as the window and is not specific to this shape.
- A lost fan-out write leaves one shard staggered, not over-admitting. A sibling that misses the
  rollover write keeps its stale `ws`, and when it is next drawn past that stale window's end it
  anchors a window of its own and fans it out. The shards that already rolled no-op that write —
  their current windows have not ended by the new start — so none restores twice; the entity
  runs with that shard offset until the windows next line up at a rollover. The same holds for
  concurrent openers (above), which is the common case, not a failure. A lengthened `rsa` can
  likewise no-op on a sibling still inside a longer window; it opens its own when that ends.
  The fan-out counts its writes and logs a shortfall at debug level rather than swallowing it.
- A `reset_after` limit is not backward-readable by a client predating it, which reads
  `refill_amount = 0` with no reset and raises. This is a property of ADR-137 rather than of this
  record — a pre-#222 client reading a *calendar* quota raises identically — and the
  fleet-upgrade requirement already exists.

## Alternatives Considered

### Duration-based windows anchored to the entity, expressed in cron
Rejected because: cron names instants on a wall clock, so it cannot express "five hours after
*you* started". The duration form needed a second field on `Limit` rather than a second reading
of `reset_schedule` — which is this record.

### One field, interpreted as cron or as a duration depending on its content
Rejected because: it doubles the semantics of every reader — config, manifest, CLI, aggregator
and client — behind a value whose meaning is discovered by parsing it. Two fields that are
mutually exclusive at construction (ADR-137, this record) give the same expressiveness and are
checked once, at the boundary.

### Read the window end off `vu`
Rejected because: `vu` is also the limit-change fan-out's marker and the aggregator's staleness
pin, so every `set_limits()` would read as an elapsed window and restore every caller's balance.

### Let each shard keep its own window, with no fan-out
Rejected because: it costs nothing and is still wrong. Over-admission is *not* the reason — S
staggered shards each contribute at most `2 × (C/S)` over any window of length `W`, for the same
`2C` total a single fixed window admits, so drift redistributes when the allowance arrives
without loosening the ceiling. The reason is that `resets_at_ms` stops existing: `min(ws) + W`
over-promises and `max(ws) + W` under-promises, and for a session cap shown to a human that
number *is* the product.

### Store the window end rather than the start
Rejected because: it is the same information and `ws` is the half that is monotonic, which is what
lets the fan-out use a `_propagate_shard_count()`-shaped monotonic condition. Storing both invites the
pair disagreeing after a partial write.

### A per-entity offset into a fixed grid, derived from the entity id
Rejected on its own terms, independently of ADR-138 having excluded it: it is not first-use
anchored. A new entity's first window is however long remains of whatever grid cell it appears
in, so an entity created a minute before its cell boundary gets a one-minute window and its whole
allowance a minute later. It also gives entities boundaries their configuration does not state,
and the drift is undiscoverable from the config.

### A fixed start instant per entity, with the window projected forward in multiples
Rejected on its own terms: it needs exactly the anchor the chosen design needs, so it saves
nothing, and it cannot express "go idle long enough and your window restarts" — it tiles forward
from the first-ever use regardless of activity, which is the semantics the owner did not choose.

### Redistribute the balance across shards instead of transferring it
Rejected because: admission gates on **per-shard** balance, not on the entity-wide sum. A blind
`ADD −(share/2)` conserves the sum while driving a spent shard negative, and a quota's debt is
never repaid — its rate is zero and its reset *sets* rather than adds, wiping the debt. The
measured case admits 1498 against 1499 under the unfixed bug. This is #587's finding and it
applies here because a duration window is a quota; the rollover avoids it by moving `ws` and
never `tk`, so no shard is ever decremented to fund another's.

### Anchor on the entity's `#META` record, cached
Rejected because: window state is per-(entity, resource, limit), so `META` would need unbounded
flat attributes and would become a write hot spot at every rollover — the hot-partition problem
one level up.

### Encode the duration as a token inside `rsched`
Rejected because: `decode_reset` deliberately rejects unknown modifier tags rather than ignoring
them, on the argument that a silently-misread reset is the worst available outcome. A token makes
every stored entry conditionally-a-cron and grows a branch in `cycle_seconds`, `prev_reset_edge`,
`next_reset_edge` and `to_cron`.
