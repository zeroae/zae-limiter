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
partial write. Config carries `rsa` beside the limit's other fields, under the `w_` prefix that
pre-v0.15 readers do not scan (see [Hiding the configuration from pre-v0.15
readers](#hiding-the-configuration-from-pre-v015-readers-640)).

The anchor is **not** `vu`. Beyond gating the fast path, `vu` carries the marker a limit-change
fan-out stamps (`vu = 0`, #468/#487) and the staleness pin the aggregator adds to its refill
condition (#508). A window reading its expiry off `vu` would read every `set_limits()` fan-out as
an elapsed window and silently restore every caller's balance and restart every caller's clock.
`vu` gains `ws + rsa` as a third voting member of its minimum, which is all it needs.

**Each shard resets itself when it sees a window it has not applied** — `ws > wa`, where `wa` is
the per-limit window-applied marker described [below](#the-window-applied-marker-wa-640), and
`ws > rf` on an item written before that marker existed. This is `_apply_reset_edge`'s existing
rule with the backwards cron scan replaced by an attribute read, and every property that rule was
designed for carries over: it is a set rather than an add, so it is idempotent; an idle shard
applies one reset on wake however many windows it slept through, because `ws` holds only the
current window's start; the comparison is strictly `>` because the pass that applies the reset
records `ws` as applied; the target is the shard's share of the effective capacity, so
resetting does not multiply the entity's quota by `shard_count`; and `tc` is never touched, so the
consumption counter stays monotonic.

**`rf` is monotonic on every item, windowed or not.** Before #640 `rf` was the only record that
a shard had applied its window; it still is on an item without the `wa` marker, and so every
materialising write stamps
`rf = max(now, stored rf, every applied ws on the item)` (`lease._monotonic_rf`, mirrored by the
aggregator) — never `rf = now`. A writer whose clock runs behind the one that stamped the item
would otherwise move `rf` backward past `ws`, the next pass would read `ws > rf` and reset again,
and every request from the slow clock would refund the window's spend. The rule applies to items
with no window too — there it reduces to `max(now, stored rf)` — which costs nothing, because
`refill_bucket` treats a non-positive elapsed time as zero. A writer predating ADR-139 does not
follow this rule, which is why a marked item no longer relies on it for the roll.

**The rollover fan-out moves a scalar and never `tk`.** Whoever first materialises a shard past
`ws + rsa` anchors `ws_new = now`, applies its own reset under its own `rf` lock, and fans
`ws_new` (with `rsa`, and `vu = 0`) to the entity's other shards with concurrent conditional
writes under `(attribute_exists(wa) OR rf < ws_new) AND (attribute_not_exists(ws) OR ws <= ws_new
- rsa)` — a sibling moves only if its own window had already ended by `ws_new`, the same half-open
rule the opener applied to itself. The `rf` guard applies only to a sibling without the `wa`
marker, which applies the new window by reading `ws > rf`: one whose `rf` is already at or past
`ws_new` (an aggregator refill that landed after its old window ended, or a writer whose clock
runs ahead) would read the moved `ws` as already applied and keep its burnt balance for the whole
new window. Left alone, it opens its own window when next drawn. A marked sibling reads
`ws > wa`, which no `rf` can mask, so it takes the window whatever its `rf`.
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

A shard created mid-window inherits `ws` from shard 0, read once per shard on the create path,
and joins that window with the #587 transfer from its siblings. The read is **strongly
consistent** — 1 RCU for the small projected item, against 0.5 eventually consistent — because a
shard is usually created right after shard 0 was written, often by the write that rolled its
window: a stale pre-roll `ws` looks ended, and the new shard would take a fresh full share on top
of the window shard 0 had just opened (measured at 15 admitted against a quota of 10). If shard 0's
window **has** ended (or shard 0 carries none), the new shard opens its own window at `now` at its
full share — its siblings' balances belong to the window that ended — and fans that window out
like any rollover, so the entity keeps one window phase.
Cascade parents and children anchor **independently**: the parent's window does not track the
child's, consistent with cascade already treating limits, shards and `disabled` as per-entity
state. That independence is not free at shard-create time — the inheritance read is scoped to one
entity's shards, so a cascade slow path creating a parent shard resolves the *parent's* `ws` with
its own read rather than reusing the child's. It costs 1 RCU (strongly consistent, above) on the
cascade shard-create path, once per parent shard, and it is what stops a busy child's window from
silently becoming its parent's.

### Mixed-fleet version gate (#638)

A reader predating this record cannot read a `reset_after` limit (see Negatives), so storing one
is gated on the readers' versions. Decided 2026-09-26: options **A**, **C** and **D** now; **B**
deferred to #640.

- **A — writer gate on the aggregator.** Every writer of a `reset_after` limit —
  `Repository.set_limits`, `set_resource_defaults`, `set_system_defaults`, the provisioner
  applier, and `acquire(limits=[...])`, whose override the slow path writes onto the bucket
  item — refuses it with `VersionMismatchError` unless the version record's `lambda_version`
  is at least `0.15.0` (`version.MIN_READER_VERSION_FOR_RESET_AFTER`, release part only, so a
  `0.15.0rc1` counts), or is exactly the writer's own build. There is one aggregator per stack
  and its version is `lambda_version`, so this is the one reader a writer can check. Cost: the
  config writers issue one strongly consistent `GetItem` of `#VERSION` (1 RCU), and only when
  a limit in the call carries `reset_after`; the `acquire()` override trusts the version the
  repository read when opened and re-reads only on a refusal. A missing record, or an unknown
  `lambda_version`, fails closed.
- **The stamp must be earned.** `lambda_version` records this build only if the stack was
  created in this call, **or** both:
  - the aggregator is current: its code was pushed in this run, or it probes absent; **and**
  - the provisioner is current: its code was pushed in this run, or it probes absent.

  If either probe cannot tell, the stored stamp is kept (a new record is left unknown). The
  probe is `lambda:GetFunctionConfiguration` on `{stack}-aggregator` /
  `{stack}-limits-provisioner` — the functions themselves rather than the stack's
  `EnableAggregator` parameter, because the function is what does the reading and the template
  creates it only when a role is also available. The provisioner is in the rule because
  `deploy` on an existing stack pushes code but adds and removes no functions (`create_stack`
  never updates one): `--no-provisioner` or `--no-iam` leaves a live pre-v0.15 provisioner,
  which stores a `reset_after` manifest limit as a dripping one. One predicate,
  `stack_manager.stack_lambdas_current`, serves CLI `deploy`,
  `_ensure_infrastructure_internal` and the `open()` init path. An unknown version asks for no
  Lambda update, so `open(auto_update=True)` neither loops nor pushes code; the remedy is
  `zae-limiter upgrade`, which treats unknown as out of date. A `--no-aggregator` stack's remedy
  is re-running `zae-limiter deploy` from `0.15.0` with the provisioner enabled: the aggregator
  probes absent, the provisioner's code is pushed, and the stamp is earned — where `upgrade`
  would push code to an aggregator that does not exist.
- **C — `client_min_version` becomes a real gate.** Clients from v0.15.0 on raise
  `VersionMismatchError` when below it (before, `check_compatibility` returned an incompatible
  result with no flag set and both version checks fell through). A Lambda update, `deploy` and
  `upgrade` keep the stored minimum instead of resetting it to `0.0.0`. When A admits a write it
  also **ratchets** the minimum to `0.15.0` with a conditional `UpdateItem`, never lowering it,
  so the next feature is protected by the same field automatically.
- **D — documentation** of what remains: the session-quotas guide, CLAUDE.md and the Negatives
  below.
- **B — hide the configuration from pre-v0.15 readers** (store the limit under attributes they
  do not scan): deferred on 2026-09-26, then taken for v0.15.0 in #640 — see the next section.
  It needed #633 and a per-limit "window applied" marker first, and it turns
  `on_unavailable=block` on an old client from an outage into silent non-enforcement of the
  session limit.

Rejected: a writer gate on `client_min_version` alone (v0.14 ignores the field, so it stops
neither v0.14 clients nor the v0.14 aggregator — kept only as C's ratchet); bumping the schema
major so old clients raise `IncompatibleSchemaError` (breaks every v0.x client on the table and
needs a migration); a stored flag old clients already reject (fails as today, a `ValueError`
on read, so it still fails open under `allow`).

### Hiding the configuration from pre-v0.15 readers (#640)

**Decision (owner, 2026-09-27): take option B in v0.15.0**, accepting the `on_unavailable=block`
trade-off below. A pre-v0.15 client reading a level that holds a `reset_after` limit then keeps
enforcing every other limit on that level and ignores the session limit, instead of failing the
whole level.

**Storage mapping, config items only.** A limit with `reset_after` set stores its config
attributes as `w_{name}_{field}` — `cp`, `ra`, `rp`, `rsa`, and `sched` when it has a parameter
schedule — instead of `l_{name}_{field}`. The prefix is chosen by `limit.reset_after is not None`
when the item is written and recorded per name when it is read; nothing above the storage layer
sees it. `w_` must not start with `l_`, because that is what both pre-v0.15 discovery rules key
on, and it collides with no other top-level config attribute:

| Pre-v0.15 reader | Discovery rule | Result with `w_` |
|---|---|---|
| `Repository._deserialize_composite_limits` (behind `resolve_limits`, `get_limits`, the CLI `get-*` commands, `check_availability` and the stale-name diff of `set_limits` / `delete_limits`) | `startswith("l_") and endswith("_cp")` | ignored |
| Provisioner `bucket_sync._decode_limits` | every attribute through `schema.parse_limit_attr` (`startswith("l_")`) | ignored |
| Aggregator | never reads config | n/a |

**Bucket attributes stay `b_*`.** A v0.14 client already reads and rewrites a bucket carrying
`b_session_*` without error, and a v0.14 aggregator over-admits whichever prefix it sees (its
Path 2 clone copies shard 0's image verbatim); the version gate above is what keeps it away.
Hiding them would touch every bucket reader and writer and buy nothing.

**Readers accept both prefixes** — the client and the provisioner through one helper,
`schema.config_limit_names`. Two shapes are corrupt and take the whole item with them, exactly as
an undecodable schedule does: a name under **both** prefixes (the reader cannot know which one is
current), and a `w_` limit with no `rsa` (the prefix promises a window). The client raises
`RateLimiterUnavailable`, the provisioner a `ValueError` out of the item.

**Items written under `l_` with `l_{name}_rsa` are read forever, not migrated.** Only unreleased
v0.15 builds wrote them. Every config write is a full-replace `PutItem`, so the next write of that
level moves the limit to `w_` with no migration step, and the dual-prefix reader reads the same
fields under either prefix, so keeping them readable costs no code. Such an item is of course
still unreadable by a v0.14 client until it is rewritten.

**What an old client does under B** (verified against v0.14.0 from PyPI):

| Old-client action | Behaviour |
|---|---|
| Config read, level holds `rpm` + a session limit | sees and enforces `rpm` only; no error |
| Config read, level holds only a session limit | the level looks empty, so it falls through to the next level and enforces that |
| Bucket read (fast path images, slow path, `available`, `check_availability`, `get_buckets`) | reads `b_session_*` as a stray limit; no error |
| Bucket rewrite (normal, retry, adjust, rollback) | leaves `b_session_*` alone; stamps `rf`, `vu` and `ttl` from its own limits and clock |
| Bucket create (a new item or shard N>0) | the item has no session limit; the next v0.15 pass seeds it (#633), by #587 transfer on a sharded entity |

| Mode | Old client, no B | Old client under B |
|---|---|---|
| `on_unavailable=allow` | no limiting at all on that level | every other limit enforced; the session limit is not enforced by that client |
| `on_unavailable=block` | every acquire on that level raises (fail closed, all limits) | every other limit enforced; the session limit **silently** not enforced by that client |

Under `allow` B is strictly better. Under `block` an outage becomes silent non-enforcement of the
session limit, bounded by the old clients' share of traffic during the rollout.

**Old writers now touch buckets that carry a window.** That is the price of B, and it is why the
roll no longer trusts `rf` (next section) and why #633's seed had to land first. What remains is
accepted and documented:

- **TTL.** An old client stamps `ttl` from *its* limits, which can be shorter than
  `reset_after × multiplier`. If DynamoDB deletes the item mid-window, the next request opens a
  fresh window at full share: the window restarts mid-way, over-admitting at most one allowance.
  There is no storage-side fix, because the old writer re-stamps `ttl` on every write.
- **Old admins.** A v0.14 `set_limits` / `delete_limits` (or `set_resource_defaults`, or a v0.14
  `limits apply`) on a level holding a hidden limit silently drops it from config — every level
  is a full-replace write — and leaves `b_session_*` behind on the buckets, because the old
  stale-name diff cannot see it. Without B the same old admin fails instead. Admin tooling must be
  upgraded first.
- **`vu`.** An old client re-stamps or REMOVEs `vu` from its own limits, so the fast path is no
  longer gated at a window's end: an ended window stretches until its leftover balance runs out,
  and then the slow path opens the next. No over-admission per window; `resets_at_ms` and
  `retry_after_seconds` drift until a v0.15 slow pass re-stamps `vu`.
- **Item-level schedule defaults.** An old param sync SETs or REMOVEs the item-level `sched` /
  `rsched` / `sched_tz` from its own limits only. A session limit with no per-limit override on
  that item then inherits the new default; an inherited `rsched` makes the aggregator treat it as
  a calendar quota. Reaching this needs an old admin writing a level that the session limit's
  buckets are stamped from, which the rule above already forbids.
- **The #642 residual** applies to an old client's shard creation as to a stale config cache: a
  sibling granted a larger share earlier in the window that has already spent into it cannot be
  reclaimed from, so the seed of the new shard can over-grant by up to that spent part.

### The window-applied marker `wa` (#640)

Once B lets a pre-v0.15 client write a bucket carrying a window, `rf` can no longer be the record
that a shard has applied it. An old writer stamps `rf = :now` from its own clock under its `rf`
lock and knows nothing of `ws`:

- **backward `rf`** (its clock is behind the one that opened the window, and it writes within
  that skew of the roll): `rf` drops below `ws`, the next pass reads `ws > rf` and resets again —
  a **double roll**, one share over-admitted;
- **forward `rf`** (it writes a sibling after a rollover fan-out moved `ws` there, before any
  v0.15 pass): `rf` passes `ws`, and the sibling never applies the window — a **skipped roll**,
  a burnt shard for the whole window.

So each window limit carries its own marker, which no old writer touches.

- **Attribute.** `b_{name}_wa`, epoch ms, per limit and per shard: the `ws` whose allowance this
  shard's balance reflects. `wa <= ws` always.
- **Rule.** A shard has an unapplied window when `ws > wa` (`BucketState.window_rolled`); on an
  item that carries no `wa` for that limit, `ws > rf` exactly as before. That fallback is the
  whole migration: nothing is backfilled, and an unmarked item behaves as it did until its first
  v0.15 write marks it.
- **Who writes it.** Every v0.15 writer that brings a window's balance into being, and only
  those — each stamps `wa` to the `ws` its balance now reflects:

  | Writer | `wa` stamped |
  |---|---|
  | Client create (`build_composite_create`), including a shard N>0 joining a live window | the created `ws` (joined or opened) |
  | Normal path (`build_composite_normal`, rf-locked) | for every window limit on the write: the `ws` it opened, rolled or seeded, or the `ws` it read and carried unchanged — the last is what marks an old item |
  | Transfer-seed persist (#633) | the joined `ws` |
  | Aggregator refill (rf-locked) | for every window in force on the write: the `ws` it rolled or read |
  | Aggregator Path 2 clone | copies shard 0's `wa` verbatim beside its `ws` and `rf`, so a clone is marked exactly as shard 0 is |

  Never written by the fast path, the consumption-only retry, adjustments and rollbacks, the
  rollover fan-out, the param sync, or any pre-v0.15 writer.
- **A value, never a path.** A writer SETs `wa` to the `ws` *value* it read or opened, never
  `SET wa = ws` as a document path. The rollover fan-out moves `ws` without touching `rf`, so it
  can land between a pass's read and its rf-locked write; stamping the value read leaves the
  fanned-out window unapplied (`ws_new > wa`) and the next pass rolls it, where copying the path
  would record as applied a window whose share was never granted.
- **No new condition term.** `wa` moves only inside writes that are already serialised with the
  balance they describe: under the `rf` lock, at item creation (`attribute_not_exists(PK)`), or
  in the seed persist (`attribute_not_exists(tk)` for that limit). The aggregator's `rf`, `vu`
  and per-rolled-limit `ws` pins are unchanged.
- **The rollover fan-out** drops its `rf < :new` term for a marked sibling:
  `(attribute_exists(wa) OR rf < :new)`. The term existed only because an unmarked sibling applies
  the window by reading `ws > rf`. With it gone, the forward-`rf` case lands the new `ws` on the
  sibling and the sibling rolls on its next pass. The ended-window floor
  (`ws <= :new - rsa × 1000`) is unchanged — it is what stops concurrent openers resetting each
  other, and it is also what makes the relaxed term safe: a sibling moves only when its own
  window had ended by the new start, so it is owed exactly one fresh share.
- **The opener** still resets unconditionally under its own `rf` lock, and stamps
  `ws = wa = now`.
- **Monotonic `rf` stays** (#635). It is what makes the fallback correct on an unmarked item and
  keeps refill timing honest; on a marked item it is no longer what decides the roll.
- **Cost.** One number attribute per window limit per shard (under 20 bytes), written only inside
  writes that already happen: no extra request, no extra RCU/WCU unless an item crosses a 1 KB
  boundary. The speculative fast path is byte-identical.

Under an old writer, then: a backward `rf` on a marked item leaves `ws <= wa`, so nothing rolls
again; a forward `rf` leaves `ws > wa`, so the window rolls once and `wa` catches up. Either way a
shard rolls exactly once per window.

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
- A rollover costs (S − 1) × L writes to keep an entity's shards on one window, where S is
  `shard_count` and L the number of duration-window limits on the item — one conditional write
  per (sibling, limit), because two windows of different lengths roll at different instants.
  Zero for the unsharded majority; 31 × L at `MAX_SHARD_COUNT`, once per window. A
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
- A `reset_after` limit is not enforced by a client predating it. Stored under `l_`, such a
  client would ignore `l_{name}_rsa`, read `refill_amount = 0` with no reset, and raise
  `ValueError` under ADR-137 — under `block` every acquire against that level would raise
  `RateLimiterUnavailable`, under `allow` every acquire would be admitted with **no limiting at
  all on that level**. Stored under `w_` (#640) it simply does not see the limit, and enforces
  the level's others. An aggregator predating this record reads the quota as a dripping limit,
  so its proactive-sharding clone mints `cp // new_count` per new shard — the #587
  over-admission; the version gate above keeps it away. **Nothing makes a v0.14 client enforce
  the session limit**, so every client must still be upgraded before one is relied on, and every
  admin tool before one is stored.
- The gate holds only at write time. A v0.14 CLI can undo it afterwards: `deploy` or
  `upgrade --force` puts the v0.14 Lambdas back and stamps `lambda_version = 0.14.0`, and a
  v0.14 `upgrade` reads the ratcheted minimum as "not up to date", downgrades the Lambdas and
  resets the minimum to `0.0.0`. A later v0.15 `open()` re-upgrades the Lambdas, so they can
  flip back and forth. The rule is to never run a v0.14 CLI against a stack holding a
  `reset_after` limit.
- `client_min_version` is checked when a repository is opened, so a long-lived process opened
  before the minimum was raised is not refused until it restarts. For the same reason the
  `acquire(limits=...)` override gate trusts the `lambda_version` read at open, and does not
  see a later downgrade.
- A refused `Custom::ZaeLimiterLimits` update whose *previous* properties also carried a
  `reset_after` limit leaves the stack in `UPDATE_ROLLBACK_FAILED`: the rollback re-sends
  those properties and is refused the same way. The remedy is `zae-limiter upgrade` followed by
  `continue-update-rollback`, or `continue-update-rollback` skipping the resource — the refused
  update wrote nothing.
- A v0.15 `zae-limiter deploy` does no `client_min_version` check of its own; the other CLI
  commands that open the repository do.

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
