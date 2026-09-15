# Session Quotas (`reset_after`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `Limit.reset_after` — a duration-based quota window anchored to each entity's own
first use ("10,000 tokens, and your window resets 5 hours after you first used it") — alongside
the existing calendar `reset_schedule`, correct under write sharding.

**Architecture:** A rolling window is a quota (ADR-137: `refill_amount = 0`). The window **start**
`ws` is stored as per-limit bucket state (`b_{name}_ws`) and the window length is denormalised
beside it (`b_{name}_rsa`, from `reset_after`).
Each shard resets itself when it observes `ws > rf` — `RateLimiter._apply_reset_edge`'s existing
rule with the backwards cron scan replaced by an attribute read. Whoever first materialises a
shard past `ws + rsa` anchors a new window at its own clock reading and fans that scalar to the
entity's other shards under a monotonic `ws < :new` condition, exactly the shape
`Repository._propagate_shard_count()` already uses. **The fan-out moves a scalar and never `tk`**:
each sibling resets its own balance, under its own `rf` optimistic lock, in the write it was
going to make anyway. The speculative fast path is **byte-identical** — an elapsed window fails
the pre-existing `vu > :now` guard and routes to the slow path, which is already the only client
that writes `vu`.

**Tech Stack:** Python 3.11+/3.12, `aioboto3` (async client), `boto3` (Lambda packages), DynamoDB
single-table, `pytest` + `pytest-asyncio` + `moto`, LocalStack for integration/E2E, `uv` for the
venv, `hatch run generate-sync` for the AST-generated sync twins.

**Spec:** `docs/plans/2026-09-15-rolling-session-windows-analysis.md`, **merged on `main`** as
`6ce63053` (PR #585). Read it alongside this plan; every design argument below is its argument
and this plan does not re-derive them. Where the two once disagreed they have been reconciled:
the storage spelling is `rsa` at both levels (the analysis's reason — `ra` is already
`refill_amount` — is the better one and this plan adopts it), and the analysis has taken on the
plan's corrections to its §3.2 shard-create mechanism, its §3.4 redistribution and its §5.3 TTL
branch. The public field name `reset_after` and the type decision are the owner's and this
plan's; the analysis records the name as settled and the type as open.

## Global Constraints

- **Sharding is an invariant, not an option.** It is the mitigation for GHSA-76rv-2r9v-c5m6. No
  task may work by suppressing `bump_shard_count()`, by pinning a rolling bucket to shard 0, or by
  not sharding. The cross-shard fan-out is in the baseline.
- **ADR-137 holds unchanged**: a limit drips **or** resets, never both and never neither. A
  session quota has `refill_amount = 0`. `reset_after` and `reset_schedule` are therefore
  **mutually exclusive** — two spellings of the reset half, not two mechanisms that compose.
- **`Limit.is_quota` is the structural predicate.** Never key behaviour on `BucketState.accrues`
  or on `refill_amount == 0`.
- **The speculative fast path stays byte-identical.** No new condition term, no new expression
  value, no config read. Task 14 asserts this with `capacity_counter`.
- **Generated sync twins are never hand-edited.** Any task touching `limiter.py`, `lease.py`,
  `repository.py`, `repository_builder.py`, `config_cache.py`, `infra/stack_manager.py` or
  `infra/discovery.py` must finish with `hatch run generate-sync` and commit the regenerated
  files in the same commit.
- **Milestone v0.15.0** (milestone 25), tracked by epic **#597** (`🎯 Session quotas:
  Limit.reset_after`), which points at this plan rather than restating it. The analysis header
  says v1.0.0 and an earlier draft of this plan said v0.14.0; both are superseded. **The plan
  *document* ships in v0.14.0 (PR #596) and the *feature* ships in v0.15.0** — that split is
  deliberate and the v0.14.0 milestone description states it, so nothing here should contradict
  it.
- **#587 is a hard prerequisite and is not a task here.** It landed as PR #594 by
  **reclaim-then-grant**, not by the redistribution the analysis proposed. See "Dependency",
  below; confirm it is merged before Task 2.
- **Commit conventions** (`.claude/rules/commits.md`): gitmoji + conventional commits. A fix to
  pre-existing behaviour gets its **own** `fix(scope):` commit with a `Fixes #NNN` footer, never
  folded into the `feat:` commit that exposed it — so if this work uncovers a further defect in
  shipped behaviour, split it out rather than folding it in.
- **Lint rules are never suppressed** (`.claude/rules/lint-rules.md`). No `# noqa`, no
  `# type: ignore`, no config change to silence a rule, without asking the owner first.
- **No closing keywords** (`Closes`, `Fixes`, `Resolves` + `#NNN`) for **#222** anywhere — it is
  an epic and has been closed twice by accident, once from a PR body and once from a commit
  message quoting an example. Use `Refs #222`. Verify before every push:
  `git log origin/main..HEAD --format='%B' | grep -inE '(clos|fix|resolv)[a-z]*[[:space:]]+#[0-9]+'`

## Decisions taken in this plan (and why)

| Decision | Choice | Rationale |
|---|---|---|
| Public name | `Limit.reset_after` | Owner's decision. Reads as an alternative to `reset_schedule` at the call site, which is the relationship ADR-137 requires. |
| Python type | `datetime.timedelta \| None` | The name carries no unit suffix, so the **type** must supply it: `reset_after=timedelta(hours=5)` is self-documenting where `reset_after=18000` is not. The codebase is inconsistent here (`refill_period_seconds` and `usage_retention_days` name the unit and take `int`; `config_cache_ttl` takes bare seconds), so the precedent does not decide it. The rule adopted: **the unit lives in the name wherever the type cannot carry it** — hence `timedelta` in Python, and `reset_after_seconds` / `ResetAfterSeconds` at the YAML and CloudFormation boundaries, where every value is a bare scalar. |
| New export from `zae_limiter/__init__.py` | **None** | Apply the stated test in `CLAUDE.md`: *would a user writing application code ever type it?* They type `timedelta`, which is `datetime` stdlib, and `Limit`, already exported. `reset_after` is a keyword, not a name to import. This is the opposite of `ScheduleEntry` (#534), which had to be exported because the user **constructs** it. The frozen-at-v1.0.0 `__all__` is unchanged. |
| Storage encoding | Two plain numeric attributes, **not** a `sched`/`rsched` token | `schedule.decode_reset` deliberately *rejects* unknown modifier tags rather than ignoring them (#538's argument: a silently-misread reset is the worst available outcome), so a new token makes every entry conditionally-a-cron and grows a branch in `cycle_seconds`, `prev_reset_edge`, `next_reset_edge` and `to_cron`. A versioned encoding (#515) does not change this either way — see the #515 row below. Analysis §5.1. |
| Relationship to #515 (versioned encoding) | These attributes sit **outside** the versioned string | #515 has moved to **v0.14.0** and will therefore have shipped before this work starts — an earlier draft of this plan wrongly called it deferred to v1.0.0. It adds a version marker inside `encode()` / `encode_reset()`'s compact string; `b_{name}_ws` and `b_{name}_rsa` are plain DynamoDB `N` attributes and are never part of that string, so the marker neither covers them nor needs to. Two real interactions, both in Tasks 3 and 4. |
| Window semantics | **Idle-restarting**, not tiling | Owner's decision. Window ends at 14:00, entity quiet, calls at 20:00 ⇒ a fresh window starting 20:00. |
| What anchors a window | Only a **materialising pass that is persisted** | See "The anchoring rule", below — this is the one place this plan disagrees with a brief, and it says so rather than working around it. |
| Storage spelling | `b_{name}_ws` + `b_{name}_rsa`, `l_{name}_rsa` | Derived from `reset_after`, not from the analysis's provisional `window_seconds`. The obvious abbreviation `ra` is **taken** — `schema.BUCKET_FIELD_RA` and `schema.LIMIT_FIELD_RA` are `refill_amount` at both levels — so `rsa` is used, which also keeps the `r`-for-reset prefix `rsched` uses. Matches the merged analysis (`6ce63053`). The window **start** keeps `ws`. |
| Shard creation | `_quota_transfer` / `reclaim_quota_surplus` (PR #594), **never** redistribution | See "Dependency". Conserving the sum of balances is not conserving what can be spent. |
| Cascade | Parent and child anchor **independently** | Owner's decision, consistent with how cascade already treats limits, shards and `disabled` as per-entity state. Costed: the shard-create inheritance reads the **acquiring** entity's siblings, so a cascade slow path resolves the parent's `ws` separately (Task 8). |

### The anchoring rule, stated precisely

The brief gives two cases and this plan implements both, but the second one needs a correction
that the implementer must not gloss:

1. **Exhaustion inside the current window does not move `ws`.** This falls out of the existing
   code and costs nothing. On the fast path an exhausted quota returns
   `SpeculativeFailureReason.APP_LIMIT_EXHAUSTED`, `would_refill_satisfy` is false for a quota
   (rate 0), and the rejection is a 0-RCU/0-WCU fast rejection that writes nothing. On the slow
   path `_apply_window_roll` (Task 6) does not fire because `now < ws + rsa`, and
   `RateLimitExceeded` is raised **before** `_commit_initial()` per
   `.claude/rules/write-on-enter.md` invariant 1. `ws` is untouched in both.

2. **A pass that crosses a boundary and is then rejected for another reason** (e.g. the request
   exceeds `capacity`) leaves the item **unchanged on disk**, and this plan does not add a write
   to change that. The brief's concern — "`ws` stale and the balance restored, which is
   incoherent" — describes a state that cannot occur here: the restoration is in-memory only and
   is discarded along with the rejection, because write-on-enter raises before any write. There
   is no half-applied item. Persisting the roll on a rejection path would (a) violate
   write-on-enter invariant 1, which says no DynamoDB write happens on a rejection, and (b) cost
   1 WCU on **every** over-capacity request at a boundary, which an adversarial caller can drive.
   The observable consequence of not persisting is that the window anchors at the first request
   that actually writes, which is within one request of the boundary in every realistic case, and
   the rejection's own `LimitStatus` still reports the rolled view (Task 14) so the caller is told
   the truth about what it would have got.

   **If the owner prefers the write**, it is one task to flip: `_commit_initial()` is already the
   only writer, so the change is to call a `_commit_rollover_only()` before raising in
   `_do_acquire`'s rejection branch. Task 6 Step 8 pins the current behaviour with a test whose
   name says which reading it encodes, so flipping it is a visible, reviewed change rather than a
   silent one.

Task 6 Step 7 and Step 8 pin **both** cases as the brief asks.

### Why the fast path needs no change at all

Confirmed against `repository.py` and `limiter.py`, not taken on trust:

- `Repository._speculative_consume_single()` issues `ADD b_{n}_tk -consumed` with the condition
  `attribute_exists(PK) AND tk >= consumed AND attribute_not_exists(#disabled) AND
  (attribute_not_exists(#vu) OR #vu > :vu_now)`. It contains **no** `SET` of `vu` and no `SET` of
  any schedule attribute. It will contain no `SET` of `ws`.
- `vu` is the minimum of (next parameter change, next reset edge), and Task 8 adds `ws + rsa` as
  a third voting member. An elapsed window therefore makes `vu <= now`, the condition fails, and
  `_classify_speculative_failure` returns `SCHEDULE_BOUNDARY`, which `limiter.py` already routes
  to the slow path — never to a fast rejection and never to a shard retry.
- `CLAUDE.md` records that **the slow path is the only client that writes `vu`**, and Task 8 keeps
  that true of `ws`.

So anchoring lives entirely in the slow path's materialisation, and "only admitted use anchors"
falls out of the slow path being the thing that decides admission. Task 14 Step 3 asserts the
byte-identity with `capacity_counter`.

### Cost, re-derived

Per-acquire cost is **unchanged**: 0 RCU + 1 WCU on a speculative success ($0.625/M), 0 RCU + 0
WCU on a fast rejection. No new condition term, no new config read.

Marginal cost of a rolling window over a calendar one:

| Component | Cost | Frequency |
|---|---|---|
| `ws` rollover fan-out | (S − 1) × L WCU | once per window per (entity, resource); **0 at S = 1** |
| Sibling `ws` read on shard create | 0.5 RCU | once per shard ever, ≤ 31 per (entity, resource) |
| Per-shard materialisation | — | not marginal; the calendar quota pays exactly this at every edge |
| Doubling redistribution (Tasks 2–3) | — | rides writes `bump_shard_count` / `_propagate_shard_count` already issue |

where S is `shard_count` and L is the number of rolling limits that rolled in that pass (1 in the
motivating product). At S = 32, L = 1 that is **31 WCU per rollover**. A 5-hour window is 4.8
rollovers/day, so 10,000 entities **all** at S = 32 is 10,000 × 4.8 × 31 = **1.488 M WCU/day** =
$0.93/day ≈ **$28/month** at $0.625/M. At a realistic 1% maximally sharded it is 14,880 WCU/day ≈
**$0.28/month**. At S = 1 — most entities — it is **$0**.

The alternative to paying it is not $0. It is an entity pinned to one DynamoDB partition at
~1000 WCU/s with throttling beyond it, which is GHSA-76rv-2r9v-c5m6's condition and an outage
rather than a line item.

---

## File Structure

| File | Responsibility for this feature |
|---|---|
| `docs/adr/138-fixed-reset-windows-only.md` | loses the deferral clause (Task 1) |
| `docs/adr/139-duration-reset-windows.md` | **new** — the duration-window decision, Proposed (Task 1) |
| `src/zae_limiter/models.py` | `Limit.reset_after`; widened `__post_init__` / `is_quota` / `quota()` / `to_dict` / `from_dict` / `from_bucket_state` / `per_shard`; `BucketState.window_start_ms` + `reset_after_seconds`; `LimitStatus.resets_at_ms` |
| `src/zae_limiter/schema.py` | attribute-name constants; `_recovery_seconds` duration branch |
| `src/zae_limiter/repository.py` | config (de)serialise `l_{n}_rsa`; bucket stamp/read `b_{n}_ws` / `b_{n}_rsa`; `_propagate_window_start()`; `get_shard_window_starts()`; `_sync_bucket_params` stamps `rsa` and never `ws`; quota debit in `bump_shard_count` / `_propagate_shard_count` |
| `src/zae_limiter/limiter.py` | `_apply_window_roll()`; `_materialisation_stamps()` third member; rollover fan-out call site; shard-create seeding; `_readable_balance`; `check_availability` |
| `src/zae_limiter/lease.py` | `LeaseEntry._window_start_ms` / `_window_end_ms`; `_commit_initial` re-expression; `_build_retry_failure_statuses` |
| `src/zae_limiter/exceptions.py` | `_limit_shape` takes a `LimitStatus`, not a `Limit` |
| `src/zae_limiter/bucket.py` | thread `resets_at_ms` into `declared_statuses` |
| `src/zae_limiter/cli.py` | `_format_limit` duration branch |
| `src/zae_limiter_aggregator/processor.py` | parse `ws`/`rsa`; roll branch; `_item_next_boundary`; quota debit in `propagate_shard_count` + proactive Path 1 |
| `src/zae_limiter_provisioner/{manifest,differ,handler,bucket_sync}.py` | `reset_after_seconds` in the manifest; diff; CFN coercion; the sync mirror of the `rsa` stamp |
| `src/zae_limiter/sync_*.py`, `src/zae_limiter/infra/sync_*.py` | **generated** — `hatch run generate-sync`, never hand-edited |

---

### Task 1: The decision record

Two separate things, one commit. ADR-138 **is not superseded**: it decided what a
`reset_schedule` may name (fixed calendar windows, expressed as cron), and that stays true. It
only loses the clause deferring duration windows, which will simply be false once they exist.
ADR-138 is unreleased (added `0c6b42e1`, 2026-09-15; latest release tag is `v0.13.0` and no
release contains it), so that removal falls under the same unreleased-record exception already
granted for ADR-136, ADR-137 and ADR-138 itself. **This is the fifth grant**, and the commit body
must say so, with the standing observation that the real remedy is to stop marking an ADR
Accepted before the release it describes has shipped.

**Files:**
- Modify: `docs/adr/138-fixed-reset-windows-only.md`
- Create: `docs/adr/139-duration-reset-windows.md`

**Interfaces:**
- Consumes: nothing.
- Produces: ADR-139, referenced by name in the docstrings of `Limit.reset_after` (Task 2),
  `RateLimiter._apply_window_roll` (Task 6) and `Repository._propagate_window_start` (Task 9).

- [ ] **Step 1: Confirm 139 is free**

```bash
ls docs/adr/ | grep -oE '^[0-9]+' | sort -n | tr '\n' ' '
gh pr view 393 --json files --jq '.files[].path' | grep adr
```

Expected: existing numbers end at 138; PR #393 reserves **126–132** for multi-region ADRs. 139 is
free. If 139 has since been taken, use the next free number and update every reference in this
plan.

- [ ] **Step 2: Remove the deferral clause from ADR-138's Decision**

Replace the Decision section's second sentence. From:

```markdown
`reset_schedule` supports fixed calendar windows only. Duration-based windows anchored to an
entity's own activity are deferred to a later release on scope grounds, not excluded as
infeasible, and require their own decision record when taken up.
```

to:

```markdown
`reset_schedule` supports fixed calendar windows only. A window anchored to an entity's own
activity is a different mechanism rather than a competing answer to the same question, and is
recorded separately in [ADR-139](139-duration-reset-windows.md) as `Limit.reset_after`. A limit
carries one or the other and never both (ADR-137).
```

- [ ] **Step 3: Remove the two deferral assertions from Consequences**

In the **Negative** list, replace:

```markdown
- Every entity resets simultaneously, concentrating load at the boundary. For a large tenant
  population this is a thundering herd the current design does nothing to spread. The deferred
  duration form does not have this property at all, which is the strongest argument for taking
  it up.
```

with:

```markdown
- Every entity resets simultaneously, concentrating load at the boundary. For a large tenant
  population this is a thundering herd a calendar expression cannot spread. The duration form
  (ADR-139) does not have this property at all, which is the argument that carried it.
```

and replace:

```markdown
- A caller wanting per-entity windows has no partial path: the feature is absent rather than
  approximate.
```

with:

```markdown
- A caller wanting per-entity windows reaches for `reset_after` (ADR-139) rather than for an
  approximation of one in cron.
```

- [ ] **Step 4: Rewrite the two stale Alternatives entries**

Replace the whole "Duration-based windows anchored to the entity, in this release" entry with:

```markdown
### Duration-based windows anchored to the entity, expressed in cron
Rejected because: cron names instants on a wall clock, so it cannot express "five hours after
*you* started". The duration form is a second field on `Limit` rather than a second reading of
this one — see ADR-139.
```

Replace the "Support both and select per limit" entry with:

```markdown
### One field, interpreted as cron or as a duration depending on its content
Rejected because: it doubles the semantics of every reader — config, manifest, CLI, aggregator
and client — behind a value whose meaning is discovered by parsing it. Two fields that are
mutually exclusive at construction (ADR-137, ADR-139) give the same expressiveness and are
checked once, at the boundary.
```

- [ ] **Step 5: Leave ADR-138's Status and Context alone**

Do **not** change `**Status:** Accepted`. Do **not** run `/adr supersede`. The Context's closing
sentence — "The decision below therefore rests on scope, not on feasibility, although the
durability asymmetry recorded under Consequences is the argument that survives closest scrutiny
and is what a later record will have to answer" — is accurate history and ADR-139 is the later
record it names. Leave it.

- [ ] **Step 6: Write ADR-139**

Create `docs/adr/139-duration-reset-windows.md`:

```markdown
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
`ws_new` to the entity's other shards with concurrent conditional writes under
`attribute_not_exists(ws) OR ws < :new` — monotonic and idempotent, the shape
`_propagate_shard_count()` already uses. `ws` is monotonic because window *n+1* opens at a clock
reading strictly after window *n* closed.

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
- A lost fan-out write can cost one extra window's allowance on one shard. If a sibling misses
  the rollover write it keeps a stale `ws`, and when it is next drawn past that stale window's end
  it anchors a *later* window of its own and fans that out; shards that already rolled then see
  `ws > rf` a second time and restore again. It is bounded — one extra share per affected shard
  per lost write, and the system converges on the latest `ws` — and it requires a write to fail,
  since a sibling holding the *current* `ws` never re-anchors. The fan-out counts its writes and
  logs a shortfall rather than swallowing it.
- A `reset_after` limit is not backward-readable by a client predating it, which reads
  `refill_amount = 0` with no reset and raises. This is a property of ADR-137 rather than of this
  record — a pre-#222 client reading a *calendar* quota raises identically — and the
  fleet-upgrade requirement already exists.

## Alternatives Considered

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
lets the fan-out reuse `_propagate_shard_count()`'s condition verbatim. Storing both invites the
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
```

- [ ] **Step 7: Validate both records**

```bash
uv run python -c "import pathlib; [print(p, p.read_text().splitlines()[2]) for p in sorted(pathlib.Path('docs/adr').glob('13*.md'))]"
```

Expected: `139-duration-reset-windows.md` prints `**Status:** Proposed`; `138-…` still prints
`**Status:** Accepted`. Do **not** self-accept ADR-139 — the owner accepts ADRs.

- [ ] **Step 8: Commit**

```bash
git add docs/adr/138-fixed-reset-windows-only.md docs/adr/139-duration-reset-windows.md
git commit -m "$(cat <<'EOF'
📝 docs(adr): record duration reset windows as ADR-139

ADR-138 decided what a `reset_schedule` may name — fixed calendar windows,
expressed as cron — and that is unaffected. A duration window anchored to
first use is a different mechanism, so it gets its own record additively
rather than superseding one.

ADR-138 loses only the clause deferring duration windows, plus the two
Consequences and two Alternatives entries that asserted they were out of
scope. Its Status and its Decision's substance are unchanged.

That removal is the unreleased-record exception: ADR-138 was added in
0c6b42e1 today and no release contains it (latest tag v0.13.0). This is the
fifth grant of that exception, after ADR-136, ADR-137 and ADR-138 itself.
The standing observation stands with it: the real remedy is to stop marking
an ADR Accepted before the release it describes has shipped.

ADR-139 is Proposed, not Accepted.

Refs #222
EOF
)"
```

---
### Dependency: #587 must land first — and it did, as PR #594 (reclaim-then-grant)

**Not a task in this plan.** It is a hard prerequisite, it is a fix to shipped calendar-quota
behaviour independent of this feature, and it has its own PR. Confirm it is merged before
starting Task 2:

```bash
gh issue view 587 --json state,closedAt
gh pr view 594 --json state,mergedAt,title
git log --oneline origin/main | grep -i 'quota shard'
```

**Why a rolling window cannot ship without it.** A rolling window **is** a quota (ADR-137:
`refill_amount = 0`), so it inherits #587 exactly: a `wcu`-driven `shard_count` doubling
mid-window created new shards each holding a fresh `capacity // shard_count`, which nothing
trims, because a quota shard spent to zero never refills. #587 measured **3.49 × C** admitted in
a single frozen period after five doublings.

**What PR #594 actually landed, and it is not what the analysis proposed.** The analysis §3.4
recommended **redistribution** — `ADD −(old_share / 2)` to every existing shard, riding the
writes `bump_shard_count()` and `_propagate_shard_count()` already issue. That was **rejected**,
and the reason matters to this design:

> Blind `ADD −(old_share/2)` conserves the *sum of balances*, but admission gates on **per-shard
> balance ≥ 0**, and a quota's debt is never repaid — its rate is zero under ADR-137 and the
> reset *sets* the balance rather than adding to it, wiping the debt. On the measured case shard
> 0 goes to −498 while shard 1 gets +500, the entity spends 500 from shard 1, and the period
> admits 1498 against 1499 under the unfixed bug. **Conserving the sum of balances is not
> conserving what can be spent.**

The mechanism that landed is **reclaim-then-grant**: before a quota shard is created — on the
slow path and in the aggregator's Path 2 alike — every shard that already exists is **eagerly
clamped** to the ceiling the doubling just shrank it to (the same `min(capacity, tokens)` that
`refill_bucket` would apply on its next materialising pass), and the new shard starts with
exactly what that reclaimed, capped at one share. A **transfer, never a mint**. Keyed on
`Limit.is_quota`; the dripping path is byte-identical.

**Machinery this plan builds on rather than duplicating.** Read these before writing Task 8:

| Symbol | File |
|---|---|
| `Repository.reclaim_quota_surplus(entity_id, resource, shares_milli: dict[str,int]) -> tuple[int, dict[str,int]]` | `src/zae_limiter/repository.py` (+ protocol entry, + generated sync twin) |
| `RateLimiter._quota_transfer(entity_id, resource, limits, shard_count, any_existing, now_ms) -> dict[str,int]` | `src/zae_limiter/limiter.py` |
| `models.new_shard_starting_tokens_milli(share_milli, reclaimed_milli, *, is_quota) -> int` | `src/zae_limiter/models.py` |
| `processor._is_quota_limit(limit_name, image)` / `processor._reclaim_quota_surplus(...)` | `src/zae_limiter_aggregator/processor.py` |
| `test_doubling_conserves_the_entity_wide_quota` | `tests/unit/test_quota_shard_creation.py` |

**Three consequences this plan must respect, and does:**

1. **`reset_after` must widen `is_quota` before Task 8 can work.** `new_shard_starting_tokens_milli`
   and `_is_quota_limit` both branch on quota-ness; a duration quota that reads as a dripping
   limit there would be minted a fresh share and reintroduce #587 for exactly this feature. Task 2
   widens `Limit.is_quota`; Task 8 must additionally widen `processor._is_quota_limit`, which
   reads the **stream image** and therefore tests stored attributes rather than a `Limit` —
   `b_{name}_rsa` must join whatever it currently checks.

2. **This plan proposes redistribution nowhere.** Not for shard creation (Task 8 calls
   `_quota_transfer`) and not for rollover.

3. **The rollover is immune to the same argument, and here is why rather than by inheritance.**
   The rollover fan-out moves `ws` and never `tk`: it does not move value between shards at all.
   Each shard, on its own next materialising pass, **sets** its own balance to its own
   `effective_capacity_milli(now)` — `cp // shard_count`, its own share — under its own `rf`
   optimistic lock. No shard is decremented to fund another's, so no shard is driven negative and
   there is no per-shard debt to be wiped by the next reset. The per-shard test
   (`sum(max(0, tk))` across shards, debt excluded) holds by construction: every shard lands at
   exactly its share, so the sum is exactly `capacity`. Task 14 pins it with the same measurement
   `test_doubling_conserves_the_entity_wide_quota` uses.

4. **The eager-clamp hazard does transfer, and the fan-out already answers it.** PR #594's
   argument for clamping *eagerly* rather than at next materialisation is that the speculative
   fast path is a pure `ADD` with no ceiling arithmetic, so an untrimmed sibling spends the
   surplus the new shard was just granted. A rollover has the same shape — one shard's balance
   changes while siblings are unaware — and the answer is the `vu = 0` the fan-out stamps
   alongside `ws` (Task 5). That forces every sibling off the fast path and through exactly one
   materialising pass, which is where `ws > rf` is evaluated. Without it a sibling would keep
   spending its *old* window's remaining balance on the fast path while the entity had already
   rolled. The `vu = 0` is not a convenience here; it is the eager mechanism.

Also fold in when reading the codebase: `CLAUDE.md`'s "shard creation never multiplies total
capacity" **has been corrected** by #594 and now splits drip from quota, and the DynamoDB writer
table has a new reclaim row. Plan against the corrected text.

---

### Task 2: `Limit.reset_after` — the public surface

**Files:**
- Modify: `src/zae_limiter/models.py` — `Limit` (line 272), `__post_init__` (318), `quota()` (479),
  `is_quota` (550), `to_dict` (614), `from_dict` (635), `from_bucket_state` (652), `per_shard` (701)
- Test: `tests/unit/test_models.py`, `tests/unit/test_public_api.py`

**Interfaces:**
- Consumes: `Limit`, `ScheduleEntry`, `BucketState` from `src/zae_limiter/models.py`.
- Produces:
  ```python
  @dataclass(frozen=True)
  class Limit:
      name: str
      capacity: int
      refill_amount: int
      refill_period_seconds: int
      schedule: tuple[ScheduleEntry, ...] = ()
      reset_schedule: tuple[ScheduleEntry, ...] = ()
      reset_after: timedelta | None = None        # NEW

      @property
      def reset_after_seconds(self) -> int | None: ...   # NEW
      @property
      def is_quota(self) -> bool: ...                    # WIDENED

      @classmethod
      def quota(
          cls, name: str, amount: int, *,
          cron: str | None = None, tz: str = "UTC",
          reset_after: timedelta | None = None,
      ) -> "Limit": ...                                  # WIDENED
  ```
  `to_dict()` emits `"reset_after_seconds": int` when set; `from_dict()` reads it back.

- [ ] **Step 1: Write the failing validation tests**

Add to `tests/unit/test_models.py`:

```python
from datetime import timedelta
import pytest
from zae_limiter import Limit
from zae_limiter.schedule import ScheduleEntry


def test_quota_takes_a_duration():
    limit = Limit.quota("session", 10_000, reset_after=timedelta(hours=5))
    assert limit.capacity == 10_000
    assert limit.refill_amount == 0
    assert limit.reset_after == timedelta(hours=5)
    assert limit.reset_after_seconds == 18_000
    assert limit.reset_schedule == ()
    assert limit.is_quota is True


def test_quota_still_takes_a_cron():
    limit = Limit.quota("rpd", 10_000, cron="0 0 * * *")
    assert limit.reset_after is None
    assert limit.is_quota is True


def test_quota_requires_exactly_one_of_cron_and_reset_after():
    with pytest.raises(ValueError, match="exactly one of `cron` or `reset_after`"):
        Limit.quota("x", 10, cron="0 0 * * *", reset_after=timedelta(hours=5))
    with pytest.raises(ValueError, match="exactly one of `cron` or `reset_after`"):
        Limit.quota("x", 10)


def test_reset_after_and_reset_schedule_are_mutually_exclusive():
    # ADR-137/ADR-139: one recovery mechanism per limit. Two resets would
    # restore the allowance twice over some periods and once over others.
    with pytest.raises(ValueError, match="one recovery mechanism"):
        Limit(
            name="x", capacity=10, refill_amount=0, refill_period_seconds=1,
            reset_schedule=(ScheduleEntry.reset(cron="0 0 * * *"),),
            reset_after=timedelta(hours=5),
        )


def test_reset_after_beside_a_positive_rate_is_rejected():
    # The same ADR-137 pairing rule the cron form already enforces.
    with pytest.raises(ValueError, match="drips or resets"):
        Limit(
            name="x", capacity=10, refill_amount=10, refill_period_seconds=60,
            reset_after=timedelta(hours=5),
        )


@pytest.mark.parametrize(
    "bad",
    [
        timedelta(0),                      # zero
        timedelta(seconds=-1),             # negative
        timedelta(milliseconds=1500),      # not a whole number of seconds
    ],
)
def test_reset_after_must_be_a_positive_whole_number_of_seconds(bad):
    # #569's whole-number rule and #564's finiteness rule, restated for a
    # duration: sub-second windows are not expressible in storage (`rsa` is
    # seconds) and would truncate silently.
    with pytest.raises(ValueError, match="whole number of seconds"):
        Limit(
            name="x", capacity=10, refill_amount=0, refill_period_seconds=1,
            reset_after=bad,
        )


def test_a_duration_quota_round_trips_through_dict():
    limit = Limit.quota("session", 10_000, reset_after=timedelta(hours=5))
    assert limit.to_dict()["reset_after_seconds"] == 18_000
    assert Limit.from_dict(limit.to_dict()) == limit


def test_a_dripping_limit_omits_reset_after_from_its_dict():
    assert "reset_after_seconds" not in Limit.per_minute("rpm", 100).to_dict()
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/unit/test_models.py -k "reset_after or quota_takes or quota_requires or quota_still" -v`
Expected: FAIL — `TypeError: Limit.__init__() got an unexpected keyword argument 'reset_after'`.

- [ ] **Step 3: Add the field, the property and the validation**

In `src/zae_limiter/models.py`, add `from datetime import timedelta` to the imports, then:

```python
    reset_schedule: tuple[ScheduleEntry, ...] = ()
    # A window anchored to the entity's own first use, rather than to the wall
    # clock (ADR-139). The alternative spelling of `reset_schedule`, never a
    # companion to it: a limit has one recovery mechanism (ADR-137). A
    # `timedelta` rather than a bare integer because the field name carries no
    # unit, so the type has to — `reset_after=timedelta(hours=5)` is
    # self-documenting where `reset_after=18000` is a puzzle. Storage,
    # manifests and CloudFormation all spell it `..._seconds` and take an int,
    # because a bare scalar there cannot carry a type.
    reset_after: timedelta | None = None
```

and in `__post_init__`, **after** the two misplaced-entry checks and **before** the ADR-137
pairing checks:

```python
        # A duration has to be expressible in the storage unit, which is whole
        # seconds (`l_{name}_rsa` / `b_{name}_rsa`). Rejecting here rather
        # than truncating is the same call #569 made for the schedule
        # absolutes: a silently-truncated window is a limit that resets at a
        # time the operator never wrote.
        if self.reset_after is not None:
            total = self.reset_after.total_seconds()
            if total <= 0 or total != int(total):
                raise ValueError(
                    f"reset_after must be a positive whole number of seconds, got "
                    f"{self.reset_after!r} ({total}s). The window length is stored in "
                    f"seconds, so a fraction of one cannot be represented."
                )
        # ADR-139: `reset_after` and `reset_schedule` are two spellings of the
        # reset half, not two mechanisms that compose. A limit that both reset
        # at midnight and rolled five hours from first use would restore its
        # allowance twice over some periods and once over others, with no
        # reading of "the allowance" faithful to either.
        if self.reset_after is not None and self.reset_schedule:
            raise ValueError(
                "a limit has one recovery mechanism: `reset_after` names a window "
                "anchored to the entity's own first use and `reset_schedule` names "
                "fixed calendar instants, so they are alternatives rather than "
                "companions (ADR-137, ADR-139). Pass one of `cron=` or "
                "`reset_after=` to Limit.quota()."
            )
```

Widen the two ADR-137 pairing checks to consult both halves. Replace
`if self.refill_amount == 0 and not self.reset_schedule:` with:

```python
        if self.refill_amount == 0 and not self.reset_schedule and self.reset_after is None:
```

and its message's last line with:

```python
                "Use Limit.quota(name, amount, cron=...) or "
                "Limit.quota(name, amount, reset_after=...) (ADR-137, ADR-139)."
```

Replace `if self.refill_amount > 0 and self.reset_schedule:` with:

```python
        if self.refill_amount > 0 and (self.reset_schedule or self.reset_after is not None):
```

Add the accessor beside `is_quota`:

```python
    @property
    def reset_after_seconds(self) -> int | None:
        """:attr:`reset_after` in the unit everything below the API uses.

        Storage (``l_{name}_rsa``, ``b_{name}_rsa``), the manifest
        (``reset_after_seconds``) and CloudFormation (``ResetAfterSeconds``)
        all carry whole seconds, because a bare scalar cannot carry a type.
        ``__post_init__`` has already rejected anything that is not a positive
        whole number of them, so this cannot lose information.
        """
        return None if self.reset_after is None else int(self.reset_after.total_seconds())
```

Widen `is_quota`'s return to `return bool(self.reset_schedule) or self.reset_after is not None`
and extend its docstring: *"Since ADR-139 there are two spellings of the reset half — a cron and
a duration — and this is true of both. It stays structural: a property of the configuration and
of nothing else, so ``per_shard`` and ``from_bucket_state`` can rely on it without a clock."*

- [ ] **Step 4: Widen the `quota()` factory**

```python
    @classmethod
    def quota(
        cls,
        name: str,
        amount: int,
        *,
        cron: str | None = None,
        tz: str = "UTC",
        reset_after: timedelta | None = None,
    ) -> "Limit":
        """An allowance of ``amount`` per window, restored in one lump.

        Two window shapes, and **exactly one** of them per limit:

        ``cron`` gives a **fixed calendar window** — every entity on this
        schedule resets at the same wall-clock instant, in ``tz`` (ADR-138).
        That is what a billing period needs: "10,000 per calendar month" is a
        statement about the calendar, not about the caller.

        ``reset_after`` gives a **window anchored to the entity's own first
        use** (ADR-139): five hours from when *you* started, not from midnight.
        That is what a session cap needs. The window is idle-restarting — go
        quiet past its end and the next call opens a fresh one. ``tz`` is
        meaningless here and is ignored.

        Either way the limit does not drip: the balance is *set* to the
        capacity when the window opens and does not recover in between
        (ADR-137). The amount and the reset have to arrive together, which is
        why this factory exists — the intermediate value in any two-step
        spelling is either a drip with a reset or a zero rate with none, and
        ``Limit`` rejects both.

        Args:
            name: Limit name (e.g. "rpd", "session")
            amount: The whole allowance for one window (also the ceiling)
            cron: Standard 5-field cron naming the instant the window opens.
                Mutually exclusive with ``reset_after``.
            tz: IANA timezone ``cron`` is read in. Ignored with ``reset_after``.
            reset_after: Window length, anchored to first use. Mutually
                exclusive with ``cron``.

        Example: 10,000 a day, back to 10,000 at New York midnight
            Limit.quota("rpd", 10_000, cron="0 0 * * *",
                        tz="America/New_York")

        Example: 10,000 a session, five hours from your own first call
            Limit.quota("session", 10_000, reset_after=timedelta(hours=5))
        """
        if (cron is None) == (reset_after is None):
            raise ValueError(
                "Limit.quota() takes exactly one of `cron` or `reset_after`: a "
                "calendar window resets every entity at the same instant (ADR-138) "
                "and a duration window resets each entity relative to its own first "
                "use (ADR-139), and a limit has one recovery mechanism (ADR-137)."
            )
        return cls(
            name=name,
            capacity=amount,
            refill_amount=0,
            refill_period_seconds=_QUOTA_REFILL_PERIOD_SECONDS,
            reset_schedule=(
                (ScheduleEntry.reset(cron=cron, tz=tz),) if cron is not None else ()
            ),
            reset_after=reset_after,
        )
```

- [ ] **Step 5: Extend `to_dict` / `from_dict`**

In `to_dict`, after the `reset_schedule` block:

```python
        # Whole seconds at every boundary below the API, for the reason in
        # `reset_after_seconds`. Omitted when unset so existing payloads —
        # audit events included — are byte-identical.
        if self.reset_after is not None:
            result["reset_after_seconds"] = self.reset_after_seconds
```

In `from_dict`, add to the constructor call:

```python
            reset_after=(
                timedelta(seconds=data["reset_after_seconds"])
                if data.get("reset_after_seconds") is not None
                else None
            ),
```

- [ ] **Step 6: Carry it through `per_shard` and `from_bucket_state`**

In `per_shard`'s `replace(...)`, nothing changes — `replace` preserves `reset_after` by default,
which is correct for the same reason `reset_schedule` is carried through: nothing here applies it,
so there is nothing to apply twice, and dropping it would make the result unconstructible (a zero
`refill_amount` with neither reset). **But the early return must be checked**: `per_shard` returns
`self` unchanged when `shard_count <= 1 and not self.schedule`, which is still right. Add to the
docstring, beside the `reset_schedule` paragraph:

```
        ``reset_after`` is carried through for the identical reason, and the
        ``is_quota`` carve-out on the rate floor already covers it because that
        predicate is structural and asks both spellings.
```

In `from_bucket_state`, widen the stored-shape test so a duration quota reconstructs as a quota:

```python
        # The stored shape decides, not either half alone (see above). Since
        # ADR-139 a quota item can carry either spelling of the reset, so both
        # are consulted — a duration window read back as a dripping limit would
        # advertise a phantom one-token drip beside a `retry_after_seconds`
        # computed from a rate that does not exist.
        is_quota = state.refill_amount_milli == 0 and (
            bool(state.reset_sched) or state.reset_after_seconds is not None
        )
        return cls(
            name=state.limit_name,
            capacity=max(1, state.capacity_milli // 1000),
            refill_amount=0 if is_quota else max(1, state.refill_amount_milli // 1000),
            refill_period_seconds=max(1, state.refill_period_ms // 1000),
            schedule=state.sched,
            reset_schedule=state.reset_sched if is_quota else (),
            reset_after=(
                timedelta(seconds=state.reset_after_seconds)
                if is_quota and state.reset_after_seconds is not None
                else None
            ),
        )
```

`BucketState.reset_after_seconds` does not exist yet — Task 3 adds it. Implement Task 3 first if
this does not type-check, or add the field in this step and leave its wiring to Task 5.

- [ ] **Step 7: Pin the public API**

Add to `tests/unit/test_public_api.py`:

```python
def test_reset_after_needs_no_new_export():
    """ADR-139's surface adds no name to `__all__`.

    A user writes `Limit.quota("s", 10_000, reset_after=timedelta(hours=5))`.
    `Limit` is already exported and `timedelta` is stdlib, so there is nothing
    new to import — unlike `ScheduleEntry` (#534), which the user constructs.
    The `__all__` frozen at v1.0.0 is unchanged.
    """
    import zae_limiter

    assert "reset_after" not in zae_limiter.__all__
    assert "timedelta" not in zae_limiter.__all__
```

- [ ] **Step 8: Run the model tests**

```bash
uv run pytest tests/unit/test_models.py tests/unit/test_public_api.py -q
uv run mypy src/zae_limiter/models.py
```

Expected: both green.

- [ ] **Step 9: Commit**

```bash
git add src/zae_limiter/models.py tests/unit/test_models.py tests/unit/test_public_api.py
git commit -m "$(cat <<'EOF'
✨ feat(models): give a quota a duration window anchored to first use

`Limit.reset_after` is the duration spelling of the reset half: five hours
from the entity's own first use rather than from a wall-clock instant every
entity shares (ADR-139). `Limit.quota()` now takes exactly one of `cron` or
`reset_after` and says so when given both or neither.

A timedelta rather than a bare int because the field name carries no unit,
so the type has to. Storage, manifests and CloudFormation spell it
`..._seconds` and take an int, since a bare scalar cannot carry a type;
`reset_after_seconds` is the one conversion.

ADR-137 is unchanged in substance and widened in predicate: `is_quota` asks
both spellings, both pairing rules consult both, and `reset_after` beside a
`reset_schedule` is rejected — a limit has one recovery mechanism. A duration
that is not a positive whole number of seconds is rejected rather than
truncated, following #569's call on the schedule absolutes.

No new export: the user types `Limit` and `timedelta`, both already
available. The `__all__` frozen at v1.0.0 is untouched.

Refs #222
EOF
)"
```

---

### Task 3: `BucketState` window fields and the schema constants

**Files:**
- Modify: `src/zae_limiter/models.py` — `BucketState` (line 935), `from_limit` (1095)
- Modify: `src/zae_limiter/schema.py` — attribute-name constants beside `BUCKET_FIELD_RSCHED` (91)
  and `LIMIT_FIELD_RSCHED` (141)
- Test: `tests/unit/test_models.py`, `tests/unit/test_schema.py`

**Interfaces:**
- Consumes: `Limit.reset_after_seconds` (Task 2); `Limit.is_quota` (Task 2).
- Produces:
  ```python
  # src/zae_limiter/schema.py
  BUCKET_FIELD_WS = "ws"        # b_{name}_ws   — window start, epoch ms
  BUCKET_FIELD_RSA = "rsa"    # b_{name}_rsa — window length, seconds
  LIMIT_FIELD_RSA = "rsa"     # l_{name}_rsa — window length, seconds

  # src/zae_limiter/models.py, BucketState
  window_start_ms: int | None = None
  reset_after_seconds: int | None = None

  @property
  def window_end_ms(self) -> int | None: ...   # ws + reset_after*1000, or None
  ```

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_models.py`:

```python
def test_bucket_state_from_limit_stamps_the_window():
    limit = Limit.quota("session", 10_000, reset_after=timedelta(hours=5))
    now = 1_757_000_000_000
    state = BucketState.from_limit("e1", "gpt-4", limit, now_ms=now, shard_count=1)
    assert state.reset_after_seconds == 18_000
    assert state.window_start_ms == now          # a bucket is created BY a use
    assert state.window_end_ms == now + 18_000_000
    assert state.tokens_milli == 10_000_000


def test_bucket_state_window_end_is_none_without_a_window():
    state = BucketState.from_limit(
        "e1", "gpt-4", Limit.per_minute("rpm", 100), now_ms=0, shard_count=1
    )
    assert state.window_start_ms is None
    assert state.reset_after_seconds is None
    assert state.window_end_ms is None


def test_bucket_state_window_is_divided_by_shard_count_like_any_quota():
    # The window is entity-wide; only the BALANCE is per-shard. `ws` and
    # `rsa` are replicated verbatim to every shard.
    limit = Limit.quota("session", 10_000, reset_after=timedelta(hours=5))
    state = BucketState.from_limit("e1", "gpt-4", limit, now_ms=0, shard_count=4)
    assert state.reset_after_seconds == 18_000   # NOT divided
    assert state.tokens_milli == 2_500_000       # 10_000 // 4, in milli
```

and to `tests/unit/test_schema.py`:

```python
def test_window_attribute_names():
    assert schema.bucket_attr("session", schema.BUCKET_FIELD_WS) == "b_session_ws"
    assert schema.bucket_attr("session", schema.BUCKET_FIELD_RSA) == "b_session_rsa"
    assert schema.limit_attr("session", schema.LIMIT_FIELD_RSA) == "l_session_rsa"
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/unit/test_models.py tests/unit/test_schema.py -k "window" -v`
Expected: FAIL — `AttributeError: module 'zae_limiter.schema' has no attribute 'BUCKET_FIELD_WS'`.

- [ ] **Step 3: Add the schema constants**

In `src/zae_limiter/schema.py`, beside the existing schedule constants:

```python
# Duration reset windows (ADR-139). Two plain numbers rather than a token
# inside `rsched`: `decode_reset` rejects unknown modifier tags by design, so a
# token would make every stored entry conditionally-a-cron and grow a branch in
# `cycle_seconds`, `prev_reset_edge`, `next_reset_edge` and `to_cron`.
#
# Deliberately OUTSIDE the versioned compact encoding (#515). That marker lives
# inside the string `encode()` / `encode_reset()` produce; these are plain `N`
# attributes and are never part of it, so the marker neither covers them nor
# needs to — a number has no grammar to version. Staying outside is also what
# keeps them readable by the aggregator without a decoder.
#
# `ws` is the window START, per limit, epoch ms. Absent means the window has
# not started. It is an ENTITY-WIDE fact replicated verbatim to every shard —
# only the balance is per-shard — and it is monotonic, which is what lets the
# rollover fan-out reuse `_propagate_shard_count()`'s `< :new` condition.
#
# The window END is derived (`ws + rsa * 1000`) and never stored, so the pair
# cannot disagree after a partial write.
BUCKET_FIELD_WS = "ws"  # b_{name}_ws — window start, epoch ms
BUCKET_FIELD_RSA = "rsa"  # b_{name}_rsa — window length, seconds
```

and beside the `l_` constants:

```python
LIMIT_FIELD_RSA = "rsa"  # l_{name}_rsa — duration window length, seconds (ADR-139)
```

- [ ] **Step 4: Add the `BucketState` fields**

In `src/zae_limiter/models.py`, after `reset_sched`:

```python
    # Start of the current duration window, epoch ms (ADR-139). `None` means
    # the window has not started — which for a client-created bucket never
    # happens, because a bucket is created BY a use, but which a shard stamped
    # before the limit gained its window can carry until the next fan-out.
    #
    # Entity-wide, replicated verbatim to every shard: only the balance is
    # divided. Each shard resets itself when it observes `ws > rf`, the same
    # rule `RateLimiter._apply_reset_edge` uses for a cron, so the rollover
    # fan-out moves this scalar and never `tk`.
    window_start_ms: int | None = None
    # The window's length in seconds, denormalised from config so a
    # materialiser needs no config read — the aggregator reads the item and
    # nothing else. Never divided by `shard_count`.
    reset_after_seconds: int | None = None
```

and the derived accessor beside `accrues`:

```python
    @property
    def window_end_ms(self) -> int | None:
        """When the current duration window closes, or ``None`` (ADR-139).

        Derived rather than stored, so the start and the end cannot disagree
        after a partial write, and because ``reset_after_seconds`` has to be on
        the item anyway for the next window's length.
        """
        if self.window_start_ms is None or self.reset_after_seconds is None:
            return None
        return self.window_start_ms + self.reset_after_seconds * 1000
```

- [ ] **Step 5: Stamp them in `from_limit`**

In `BucketState.from_limit`'s constructor call, after `reset_sched=limit.reset_schedule,`:

```python
            # Stamped beside `rsched` and for the identical reason: both
            # refillers read the schedules off the item and nothing else, so a
            # bucket born carrying `vu` but no window is a bucket whose
            # `refill_amount` is 0 and which nothing ever resets.
            reset_after_seconds=limit.reset_after_seconds,
            # A bucket is created BY a use, so its window starts now. The one
            # caller that must override this is the shard-create path, which
            # inherits the entity's existing `ws` from a sibling (Task 8) —
            # a new shard joins the window in progress rather than opening one.
            window_start_ms=now_ms if limit.reset_after is not None else None,
```

- [ ] **Step 6: Check the item-size budget against #515**

These attributes and #515's version marker land on the **same item** and are charged against the
**same** 1 KB WCU boundary that design §4.2 exists to stay under — crossing it doubles the write
cost of every `acquire()` on that bucket. #515's acceptance criteria pin §4.2's worst shared case
(6 limits × 4 entries) under 1024 B *with the marker present*; this adds roughly 30 B per rolling
limit on top (~8 B name + 8 B value for `ws`, ~9 B + 3 B for `rsa`).

The two budgets compose and neither owner measured the other. Re-measure the worst case with
both:

```bash
uv run python -c "
import json
from datetime import timedelta
from zae_limiter import Limit
from zae_limiter.repository import Repository
# Build §4.2's worst shared case, then add one rolling limit, and size the item.
# Use whatever helper test_item_size / test_compact_shape uses — see tests/unit/.
"
```

If the combined worst case exceeds 1024 B, **stop and report it** rather than shaving either
feature: it is a shared-budget decision for the owner, and the cheapest lever (dropping the
marker, shortening `rsa`, or accepting the 2× on a rare shape) is not this plan's to pull.

- [ ] **Step 7: Run the tests**

```bash
uv run pytest tests/unit/test_models.py tests/unit/test_schema.py -q
uv run mypy src/zae_limiter/models.py src/zae_limiter/schema.py
```

Expected: green. This also un-breaks Task 2 Step 6's `from_bucket_state`.

- [ ] **Step 8: Commit**

```bash
git add src/zae_limiter/models.py src/zae_limiter/schema.py tests/unit/test_models.py tests/unit/test_schema.py
git commit -m "$(cat <<'EOF'
✨ feat(schema): carry a duration window on the bucket state

`b_{name}_ws` is the window start in epoch ms and `b_{name}_rsa` its length
in seconds, with `l_{name}_rsa` the config half. The window END is derived
rather than stored, so the two cannot disagree after a partial write.

Two plain numbers rather than a token inside `rsched`: `decode_reset`
rejects unknown modifier tags by design, so a token would make every stored
entry conditionally-a-cron and grow a branch in four scan functions.

`ws` is entity-wide and replicated verbatim to every shard — only the
balance is divided — and monotonic, which is what lets the rollover fan-out
reuse the shard-count propagation's `< :new` condition later.

Refs #222
EOF
)"
```

---
### Task 4: Persist `reset_after` in the config items

**Files:**
- Modify: `src/zae_limiter/repository.py` — the limit serialiser around line 5375 (where
  `LIMIT_FIELD_SCHED` / `LIMIT_FIELD_RSCHED` are written) and the deserialiser around 5439
- Test: `tests/unit/test_repository.py`, `tests/integration/test_config.py`

**Interfaces:**
- Consumes: `schema.LIMIT_FIELD_RSA`, `Limit.reset_after_seconds` (Tasks 2–3).
- Produces: config items carrying `l_{name}_rsa`; `Repository.resolve_limits()` returning
  `Limit`s with `reset_after` populated at all four levels.

- [ ] **Step 1: Write the failing round-trip test**

```python
@pytest.mark.asyncio
async def test_entity_limits_round_trip_a_duration_window(mock_dynamodb, unique_name):
    repo = await make_test_repo(...)
    limit = Limit.quota("session", 10_000, reset_after=timedelta(hours=5))
    await repo.set_limits("user-1", [limit], resource="gpt-4")
    repo.invalidate_config_cache()

    stored = await repo.get_limits("user-1", resource="gpt-4")
    assert stored == [limit]
    assert stored[0].reset_after == timedelta(hours=5)

    resolved, source = await repo.resolve_limits("user-1", "gpt-4")
    assert resolved[0].reset_after == timedelta(hours=5)
    assert source == "entity"


@pytest.mark.asyncio
async def test_rewriting_a_limit_without_a_window_drops_it(mock_dynamodb, unique_name):
    """Config storage is override-not-merge (full-replace PutItem), so this
    needs no explicit REMOVE — the same property `sched` relies on.
    """
    repo = await make_test_repo(...)
    await repo.set_limits(
        "user-1",
        [Limit.quota("session", 10_000, reset_after=timedelta(hours=5))],
        resource="gpt-4",
    )
    await repo.set_limits("user-1", [Limit.per_minute("session", 100)], resource="gpt-4")
    repo.invalidate_config_cache()

    stored = await repo.get_limits("user-1", resource="gpt-4")
    assert stored[0].reset_after is None
    assert stored[0].is_quota is False
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/unit/test_repository.py -k "duration_window or without_a_window" -v`
Expected: FAIL — `stored[0].reset_after` is `None`, or a `ValueError` from `Limit.__post_init__`
about a zero rate with no reset, because the deserialiser reconstructs `refill_amount=0` with
nothing to pair it with.

**That second failure mode is the important one** and mirrors #538 exactly: without the new
attribute a duration quota does not merely lose its window, it fails to reconstruct at all.

- [ ] **Step 3: Write the attribute**

In the serialiser, beside the `LIMIT_FIELD_RSCHED` write:

```python
            # Written only when the limit has one, exactly like `rsched`. The
            # full-replace PutItem is what makes removal free: a limit
            # re-written without a window loses the stored one with no explicit
            # REMOVE.
            if limit.reset_after_seconds is not None:
                base_item[schema.limit_attr(name, schema.LIMIT_FIELD_RSA)] = {
                    "N": str(limit.reset_after_seconds)
                }
```

- [ ] **Step 4: Read it back**

In the deserialiser, beside `rsched_name`:

```python
            rsa_name = schema.limit_attr(name, schema.LIMIT_FIELD_RSA)
            rsa_raw = item.get(rsa_name, {}).get("N")
            reset_after = timedelta(seconds=int(rsa_raw)) if rsa_raw is not None else None
```

and pass `reset_after=reset_after` to the `Limit(...)` construction.

**Failure handling:** a malformed `l_{name}_rsa` must fail the same way a malformed `sched`
does — #559 established that an unreadable stored schedule fails *safe* rather than poisoning
the read. Follow whatever `_decode_limit_schedules` does for a decode error at this site; do not
invent a second policy. A plain `int()` on a DynamoDB `N` cannot realistically fail, so the
realistic corruption is a **negative or zero** value, which `Limit.__post_init__` rejects — and
that raise must be caught by the same handler.

**#515 changes what "the same policy" means, and it will have shipped first.** Its acceptance
criteria give the schedule decoder a **three**-case taxonomy — `cannot parse from offset N`,
`invalid cron expression`, and a distinct greppable message for *a marker newer than this
reader* — and require the `RateLimiterUnavailable` raised from `get_limits()` / `resolve_limits()`
to name the attribute, the stored value, and which case applied. Match that shape: a bad `rsa`
names `l_{name}_rsa`, its stored value, and "not a positive whole number of seconds". Do **not**
add a fourth top-level case; `rsa` has no grammar and therefore no version, so it cannot produce
the newer-marker reading.

- [ ] **Step 5: Run the tests**

```bash
uv run pytest tests/unit/test_repository.py -k "duration_window or without_a_window" -v
uv run pytest tests/unit/test_repository.py tests/unit/test_config_cache.py -q
```

Expected: green.

- [ ] **Step 6: Regenerate and commit**

```bash
uv run hatch run generate-sync
git add src/zae_limiter/repository.py src/zae_limiter/sync_repository.py tests/unit/
git commit -m "$(cat <<'EOF'
✨ feat(repository): store a duration window on the config item

`l_{name}_rsa` carries the window length in seconds, written only when the
limit has one, at all four config levels. Storage is override-not-merge
(full-replace PutItem), so a limit re-written without a window loses the
stored one with no explicit REMOVE — the property `sched` already relies on.

Without the attribute a duration quota does not merely lose its window: it
reconstructs as `refill_amount=0` with nothing to pair it with and the read
raises, which is #538's shape exactly.

Refs #222
EOF
)"
```

---

### Task 5: Stamp and read the window on bucket items

**Files:**
- Modify: `src/zae_limiter/repository.py` — `_stamp_schedule` (2180) / `build_composite_create`
  (2205), `build_composite_normal` (2321), `_deserialize_composite_bucket` (5206)
- Test: `tests/unit/test_repository.py`

**Interfaces:**
- Consumes: `schema.BUCKET_FIELD_WS`, `schema.BUCKET_FIELD_RSA`, `BucketState.window_start_ms`,
  `BucketState.reset_after_seconds` (Task 3).
- Produces:
  ```python
  def build_composite_normal(
      self, entity_id: str, resource: str, consumed: dict[str, int],
      refill_amounts: dict[str, int], now_ms: int, expected_rf: int,
      ttl_seconds: int | None = None, shard_id: int = 0,
      vu: int | None = None, clear_vu: bool = False,
      window_starts: dict[str, int] | None = None,   # NEW: limit name -> new ws
  ) -> dict[str, Any]: ...
  ```
  `build_composite_create` needs no new parameter: it takes `states`, which already carry the
  window since Task 5.

- [ ] **Step 1: Write the failing tests**

```python
def test_create_stamps_the_window(repo):
    limit = Limit.quota("session", 10_000, reset_after=timedelta(hours=5))
    now = 1_757_000_000_000
    state = BucketState.from_limit("e1", "gpt-4", limit, now_ms=now, shard_count=1)
    item = repo.build_composite_create("e1", "gpt-4", [state], now_ms=now)["Put"]["Item"]

    assert item["b_session_ws"] == {"N": str(now)}
    assert item["b_session_rsa"] == {"N": "18000"}
    # `rsa` is NEVER divided by shard_count — only the balance is.
    sharded = BucketState.from_limit("e1", "gpt-4", limit, now_ms=now, shard_count=4)
    item4 = repo.build_composite_create(
        "e1", "gpt-4", [sharded], now_ms=now, shard_count=4
    )["Put"]["Item"]
    assert item4["b_session_rsa"] == {"N": "18000"}
    assert item4["b_session_tk"] == {"N": "2500000"}


def test_normal_write_sets_a_new_window_start(repo):
    upd = repo.build_composite_normal(
        "e1", "gpt-4",
        consumed={"session": 1_000},
        refill_amounts={"session": 0},
        now_ms=2_000,
        expected_rf=1_000,
        window_starts={"session": 2_000},
    )["Update"]
    assert "b_session_ws = :ws_session" in upd["UpdateExpression"]
    assert upd["ExpressionAttributeValues"][":ws_session"] == {"N": "2000"}


def test_normal_write_omits_ws_when_no_window_rolled(repo):
    upd = repo.build_composite_normal(
        "e1", "gpt-4",
        consumed={"session": 1_000},
        refill_amounts={"session": 0},
        now_ms=2_000,
        expected_rf=1_000,
    )["Update"]
    assert "_ws" not in upd["UpdateExpression"]


def test_deserialize_reads_the_window_back(repo):
    limit = Limit.quota("session", 10_000, reset_after=timedelta(hours=5))
    now = 1_757_000_000_000
    state = BucketState.from_limit("e1", "gpt-4", limit, now_ms=now, shard_count=1)
    item = repo.build_composite_create("e1", "gpt-4", [state], now_ms=now)["Put"]["Item"]

    back = {s.limit_name: s for s in repo._deserialize_composite_bucket(item)}
    assert back["session"].window_start_ms == now
    assert back["session"].reset_after_seconds == 18_000
    assert back["session"].window_end_ms == now + 18_000_000
    # `wcu` never carries a window — it is the per-partition write ceiling.
    assert back["wcu"].window_start_ms is None
    assert back["wcu"].reset_after_seconds is None
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/unit/test_repository.py -k "stamps_the_window or window_start or reads_the_window" -v`
Expected: FAIL — `KeyError: 'b_session_ws'`, and `TypeError` on the unknown `window_starts`
keyword.

- [ ] **Step 3: Stamp on create**

In `build_composite_create`'s per-state loop, after the `tc` write:

```python
            # `wcu` is auto-injected above and never reaches this loop, so it
            # can never carry a window — the structural exemption ADR-139
            # gets for free where `rsched` needed an explicit carve-out
            # (processor.py:799-804), because `ws` is per-limit and `rsched`
            # has an item-level default.
            if state.reset_after_seconds is not None:
                item[schema.bucket_attr(name, schema.BUCKET_FIELD_RSA)] = {
                    "N": str(state.reset_after_seconds)
                }
            if state.window_start_ms is not None:
                item[schema.bucket_attr(name, schema.BUCKET_FIELD_WS)] = {
                    "N": str(state.window_start_ms)
                }
```

- [ ] **Step 4: SET on the normal path**

Add the parameter and its docstring entry:

```python
        window_starts: Limit name -> the new window start to stamp, epoch ms
            (ADR-139). Only limits whose window rolled on **this** pass appear;
            an empty dict or ``None`` leaves every `ws` untouched. This is the
            only client write that moves a window start — the speculative fast
            path stays byte-identical — so `_commit_initial()` is where
            anchoring is decided, which is what makes "only admitted use
            anchors" fall out rather than being enforced.
```

and, in the expression builder, beside the `vu` SET:

```python
        for name, ws in sorted((window_starts or {}).items()):
            placeholder = f":ws_{i}"  # monotonic counter; limit names allow `-` and `.`
            set_parts.append(f"{schema.bucket_attr(name, schema.BUCKET_FIELD_WS)} = {placeholder}")
            expr_values[placeholder] = {"N": str(ws)}
```

using `enumerate` for `i`, for the reason #487's stale-limit aliases use `#stale{i}_{j}`:
`NAME_PATTERN` allows `-` and `.`, neither legal in an expression placeholder or an
`ExpressionAttributeNames` alias (`.` is a document-path separator).

`rsa` is **not** written here. The window's length changes only when the operator changes the
config, and that is `_sync_bucket_params`'s job (Task 9) — writing it on every acquire would be
a wasted attribute and would let a stale in-flight lease revert an operator's change.

- [ ] **Step 5: Read on deserialise**

In `_deserialize_composite_bucket`'s per-limit loop:

```python
            ws_raw = item.get(schema.bucket_attr(name, schema.BUCKET_FIELD_WS), {}).get("N")
            rsa_raw = item.get(schema.bucket_attr(name, schema.BUCKET_FIELD_RSA), {}).get("N")
```

and pass `window_start_ms=int(ws_raw) if ws_raw is not None else None` and
`reset_after_seconds=int(rsa_raw) if rsa_raw is not None else None` to the `BucketState(...)`
construction.

This matters for the same reason decoding `sched`/`rsched` here does: the `ALL_OLD` / `ALL_NEW`
images behind the speculative path go through this function, so without it every fast-path
`LimitStatus` would report a quota with no window and `Limit.from_bucket_state` would raise.

- [ ] **Step 6: Run and regenerate**

```bash
uv run pytest tests/unit/test_repository.py -q
uv run hatch run generate-sync
```

- [ ] **Step 7: Commit**

```bash
git add src/zae_limiter/repository.py src/zae_limiter/sync_repository.py tests/unit/test_repository.py
git commit -m "$(cat <<'EOF'
✨ feat(repository): stamp and read a duration window on bucket items

`build_composite_create` writes `b_{name}_ws` and `b_{name}_rsa` from the
states it is handed, `build_composite_normal` gains a `window_starts` map
that SETs `ws` for the limits whose window rolled on that pass, and the
deserialiser reads both back.

The deserialiser half is load-bearing beyond the slow path: the ALL_OLD and
ALL_NEW images behind the speculative path go through it, so without it
every fast-path status would report a quota with no window and
`Limit.from_bucket_state` would raise.

`rsa` is not written on the acquire path — a window's length changes only
when the operator changes the config, which is the param sync's job.
`window_starts` is the only client write that moves a window start, and it
lives on the slow path, so the speculative condition is untouched.

`wcu` never carries a window: it is auto-injected outside the per-state loop,
so the exemption is structural rather than the explicit carve-out `rsched`
needed.

Refs #222
EOF
)"
```

---

### Task 6: Roll the window on the slow path

The heart of the feature. `_apply_window_roll` is `_apply_reset_edge` with the backwards cron
scan replaced by an attribute read, and it runs in the same place for the same reasons.

**Files:**
- Modify: `src/zae_limiter/limiter.py` — new `_apply_window_roll` beside `_apply_reset_edge`
  (1425); `_materialisation_stamps` (1462); the two call sites at 1642/1653 (`_try_parent_only_acquire`)
  and 1892/1909 (`_do_acquire`)
- Modify: `src/zae_limiter/lease.py` — `LeaseEntry` (55-79); `_commit_initial` (400-460)
- Test: `tests/unit/test_limiter.py`, `tests/unit/test_lease.py`

**Interfaces:**
- Consumes: `BucketState.window_start_ms` / `window_end_ms` / `reset_after_seconds` (Task 3);
  `build_composite_normal(window_starts=...)` (Task 5).
- Produces:
  ```python
  # src/zae_limiter/limiter.py, RateLimiter
  @staticmethod
  def _apply_window_roll(limit: Limit, state: BucketState, now_ms: int) -> bool: ...
  @staticmethod
  def _materialisation_stamps(
      limit: Limit, state: BucketState, now_ms: int   # `state` is NEW
  ) -> tuple[int | None, int | None]: ...

  # src/zae_limiter/lease.py, LeaseEntry
  _window_start_ms: int | None = None   # the new ws to stamp, or None
  _window_end_ms: int | None = None     # the boundary the acquire path saw
  ```

- [ ] **Step 1: Write the failing roll tests**

```python
def test_window_roll_fires_when_ws_is_newer_than_rf():
    # `ws > rf` is the whole coherence rule: a shard whose window start is
    # newer than its own last materialisation has not applied that window yet.
    limit = Limit.quota("session", 1_000, reset_after=timedelta(hours=5))
    state = BucketState(
        entity_id="e1", resource="gpt-4", limit_name="session",
        tokens_milli=0, last_refill_ms=1_000,
        capacity_milli=1_000_000, refill_amount_milli=0, refill_period_ms=1_000,
        shard_count=1, window_start_ms=5_000, reset_after_seconds=18_000,
    )
    assert RateLimiter._apply_window_roll(limit, state, now_ms=6_000) is True
    assert state.tokens_milli == 1_000_000


def test_window_roll_is_strictly_greater_than():
    # `>=` would re-fire on every later request and refund everything spent
    # since — an unbounded quota. Same reasoning as `_apply_reset_edge`.
    limit = Limit.quota("session", 1_000, reset_after=timedelta(hours=5))
    state = BucketState(..., tokens_milli=0, last_refill_ms=5_000, window_start_ms=5_000, ...)
    assert RateLimiter._apply_window_roll(limit, state, now_ms=6_000) is False
    assert state.tokens_milli == 0


def test_window_roll_resets_to_the_shard_share():
    # Resetting every shard to the undivided capacity would multiply the
    # entity's quota by shard_count.
    limit = Limit.quota("session", 1_000, reset_after=timedelta(hours=5))
    state = BucketState(..., shard_count=4, tokens_milli=0, last_refill_ms=1_000,
                        capacity_milli=1_000_000, window_start_ms=5_000, ...)
    assert RateLimiter._apply_window_roll(limit, state, now_ms=6_000) is True
    assert state.tokens_milli == 250_000


def test_window_roll_does_nothing_for_a_cron_quota():
    limit = Limit.quota("rpd", 1_000, cron="0 0 * * *")
    state = BucketState(..., window_start_ms=None, reset_after_seconds=None, ...)
    assert RateLimiter._apply_window_roll(limit, state, now_ms=6_000) is False


def test_window_roll_leaves_tc_alone():
    # The consumption counter must stay monotonic
    # (.claude/rules/design-validation.md).
    ...
    before = state.total_consumed_milli
    RateLimiter._apply_window_roll(limit, state, now_ms=6_000)
    assert state.total_consumed_milli == before
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/unit/test_limiter.py -k window_roll -v`
Expected: FAIL — `AttributeError: type object 'RateLimiter' has no attribute '_apply_window_roll'`.

- [ ] **Step 3: Implement `_apply_window_roll`**

Immediately after `_apply_reset_edge` in `src/zae_limiter/limiter.py`:

```python
    @staticmethod
    def _apply_window_roll(limit: Limit, state: BucketState, now_ms: int) -> bool:
        """Restore the balance if a duration window has been rolled (ADR-139).

        :meth:`_apply_reset_edge` with the backwards cron scan replaced by an
        attribute read, and every property that one was designed for carries
        over verbatim:

        - **Idempotent.** It is a set, not an add, so two shards applying the
          same ``ws``, or one shard seeing it on two successive passes,
          converge.
        - **Idle buckets are correct for free.** A shard idle across three
          window boundaries applies one roll on wake, because ``ws`` holds only
          the *current* window's start.
        - **Strictly ``>``.** The pass that applies the roll stamps ``rf`` at or
          after ``ws``, so ``>=`` would re-fire on every later request and
          refund everything spent since — an unbounded quota.
        - **Per shard, to the shard's share.** ``effective_capacity_milli``
          applies the parameter schedule and then divides by ``shard_count``.
          Resetting every shard to the undivided capacity would multiply the
          entity's quota by ``shard_count``.
        - **``tc`` untouched**, so the consumption counter stays monotonic.

        The *anchoring* of a new window is **not** here. This applies a window
        another writer (or an earlier pass) already opened. Opening one is
        :meth:`_open_window_if_elapsed`, which runs immediately before this and
        mutates the same ``state``, so the two compose into one pass:
        ``_open_window_if_elapsed`` moves ``ws`` forward to ``now_ms``, and this
        then observes ``ws > rf`` and restores the balance.

        Must be called **before** :meth:`_admit_limit`, so the restored balance
        gates the request that crossed the boundary rather than the one after
        it. Mutates ``state`` in place and returns whether it did;
        ``_original_tokens_milli`` and ``_original_rf_ms`` must already have
        been captured, because they are the *stored* values the ``ADD`` delta
        and the ``rf`` lock are built from.
        """
        if state.window_start_ms is None:
            return False
        if state.window_start_ms <= state.last_refill_ms:
            return False
        state.tokens_milli = state.effective_capacity_milli(now_ms)
        return True

    @staticmethod
    def _open_window_if_elapsed(limit: Limit, state: BucketState, now_ms: int) -> int | None:
        """Anchor a new duration window when the current one has elapsed (ADR-139).

        Idle-restarting, not tiling: the new window starts at ``now_ms`` — the
        first use after expiry — rather than at ``ws_old + rsa``. Anchoring to
        the old end would be a fixed grid offset by the first-ever use, which
        cannot express "go idle long enough and your window restarts", the
        thing anchoring to the entity is *for*.

        A limit with no window, or one whose window has not elapsed, is left
        alone — which is how "exhaustion inside the current window does not
        move the anchor" is enforced: an exhausted quota is still inside its
        window, so nothing here fires and `_admit_limit` rejects against the
        balance on disk.

        A bucket carrying ``rsa`` but **no** ``ws`` (a shard stamped by the
        param sync before the limit gained its window, Task 11) opens its first
        window here. That is the only way a client-written bucket can lack one,
        since ``BucketState.from_limit`` stamps it at creation.

        Mutates ``state`` in place. Returns the new ``ws`` when it opened one,
        so the caller can stamp it and fan it out; ``None`` otherwise.
        """
        if limit.reset_after is None or state.reset_after_seconds is None:
            return None
        end = state.window_end_ms
        if end is not None and now_ms < end:
            return None
        state.window_start_ms = now_ms
        return now_ms
```

- [ ] **Step 4: Run the roll tests**

Run: `uv run pytest tests/unit/test_limiter.py -k "window_roll or open_window" -v`
Expected: PASS.

- [ ] **Step 5: Fold `ws + rsa` into `vu`**

Change `_materialisation_stamps`'s signature to take the state and add the third voting member:

```python
    @staticmethod
    def _materialisation_stamps(
        limit: Limit, state: BucketState, now_ms: int
    ) -> tuple[int | None, int | None]:
        """``(vu, next reset edge)`` for one limit at one clock reading (#222, ADR-139).

        ``vu`` is the minimum of the futures that invalidate the materialised
        ``tk``: the next parameter change, the next reset edge, and — since
        ADR-139 — the end of the current duration window. All three are
        boundaries past which the fast path must not spend tokens minted under
        conditions no longer in force.

        The window member is what keeps the speculative condition
        byte-identical. An elapsed window makes ``vu <= now``, the pre-existing
        ``(attribute_not_exists(vu) OR vu > :now)`` term fails, the failure
        classifies as ``SCHEDULE_BOUNDARY``, and the limiter routes it to the
        slow path — the only place that re-materialises, and therefore the only
        place that anchors. No new condition term, no new expression value, no
        config read on the fast path.

        Read the window end off ``state`` **after**
        :meth:`_open_window_if_elapsed` has run, so a pass that just anchored a
        new window stamps ``vu`` at the *new* window's end rather than the one
        that has already elapsed.
        """
        param_ms = next_boundary(limit.schedule, now_ms=now_ms) if limit.schedule else None
        reset_ms = (
            next_boundary((), limit.reset_schedule, now_ms=now_ms) if limit.reset_schedule else None
        )
        window_ms = state.window_end_ms if limit.reset_after is not None else None
        candidates = [c for c in (param_ms, reset_ms, window_ms) if c is not None]
        vu = min(candidates) if candidates else None
        return vu, reset_ms
```

Update both call sites (limiter.py:1653 and 1909) to pass the state, and place
`_open_window_if_elapsed` immediately before `_apply_reset_edge` / `_apply_window_roll` at
1642 and 1892:

```python
            new_ws = self._open_window_if_elapsed(limit, state, now_ms)
            self._apply_reset_edge(limit, state, now_ms)
            self._apply_window_roll(limit, state, now_ms)
```

and carry `new_ws` into the `LeaseEntry` as `_window_start_ms=new_ws`.

- [ ] **Step 6: Carry it to the commit**

In `src/zae_limiter/lease.py`, add to `LeaseEntry` beside `_reset_edge_ms`:

```python
    # The duration window this pass opened, epoch ms, or None when it opened
    # none (ADR-139). Set by `_open_window_if_elapsed` at the acquire path's
    # clock reading — never re-derived at commit time, for the same reason
    # `_boundary_ms` is not: the two readings are a round trip apart, and a
    # window that elapsed in between must not silently move the anchor forward
    # past the boundary the admission was gated on.
    _window_start_ms: int | None = None
    # The end of the window in force at that same reading, so `_commit_initial`
    # can detect one that elapsed **between** the two readings — the exact
    # analogue of `_reset_edge_ms`, and silent in the same way if unhandled.
    _window_end_ms: int | None = None
```

In `_commit_initial`'s `else:` branch, alongside the `_reset_edge_ms` re-expression:

```python
                window_starts: dict[str, int] = {}
                for entry in group_entries:
                    name = entry.limit.name
                    ...
                    # A window that elapsed between the acquire path's reading
                    # and this one is the mirror of the reset-edge case below,
                    # and silent in the same way: `_open_window_if_elapsed()`
                    # saw nothing, yet `rf` is stamped at this later reading,
                    # so the next pass compares a `ws` it never moved against
                    # an `rf` already past the boundary and never rolls either.
                    # A whole window's quota disappears.
                    #
                    # Re-expressing cannot double-apply: the acquire path
                    # covers every boundary at or before its own reading, and
                    # `_window_end_ms` is strictly after it. Admission was
                    # gated against the pre-roll balance, which is the
                    # conservative direction.
                    if entry._window_end_ms is not None and entry._window_end_ms <= now_ms:
                        entry._window_start_ms = now_ms
                        refill_amounts[name] = (
                            entry.state.effective_capacity_milli(now_ms)
                            - entry._original_tokens_milli
                        )
                    if entry._window_start_ms is not None:
                        window_starts[name] = entry._window_start_ms
```

and pass `window_starts=window_starts` to `build_composite_normal(...)`.

**Every entry in the group participates**, declared or not — `window_starts` is per-limit but the
write is one item, exactly as `vu` is, and an undeclared quota sharing the item must still have
its window stamped or it will never roll.

- [ ] **Step 7: Pin the exhaustion rule (brief case 1)**

```python
@pytest.mark.asyncio
async def test_exhaustion_inside_the_window_does_not_move_the_anchor(mock_dynamodb, unique_name):
    """ADR-139: only a persisted materialising pass anchors. An exhausted
    quota is still inside the window it already anchored, so a caller
    retrying against it must not keep restarting its own five hours.
    """
    repo = await make_test_repo(...)
    limiter = RateLimiter(repository=repo)
    await repo.set_limits(
        "user-1",
        [Limit.quota("session", 10, reset_after=timedelta(hours=5))],
        resource="gpt-4",
    )
    t0 = 1_757_000_000_000
    repo._now_ms = lambda: t0
    async with limiter.acquire("user-1", "gpt-4", consume={"session": 10}):
        pass
    ws_after_first = await _stored_ws(repo, "user-1", "gpt-4", "session")
    assert ws_after_first == t0

    # 1 hour later, still inside the window, and now exhausted.
    repo._now_ms = lambda: t0 + 3_600_000
    for _ in range(3):
        with pytest.raises(RateLimitExceeded):
            async with limiter.acquire("user-1", "gpt-4", consume={"session": 1}):
                pass
    assert await _stored_ws(repo, "user-1", "gpt-4", "session") == ws_after_first
```

Write `_stored_ws` as a module-level helper reading `b_{limit}_ws` off shard 0's raw item.

- [ ] **Step 8: Pin the boundary-rejection rule (brief case 2)**

```python
@pytest.mark.asyncio
async def test_a_rejection_at_a_boundary_writes_nothing(mock_dynamodb, unique_name):
    """A pass that crosses a boundary and is then rejected for another reason
    (here: asking for more than the whole allowance) leaves the item
    unchanged. See the plan's "anchoring rule": write-on-enter raises before
    any write, so the restored balance is in-memory only and there is no
    half-applied state. The window anchors at the next request that writes.

    **This test encodes a decision.** If the owner prefers the rollover to be
    persisted on a rejection path, this is the test to change, and the change
    is visible rather than silent.
    """
    ...  # spend the allowance, advance past ws + 5h
    repo._now_ms = lambda: t0 + 18_000_001
    with pytest.raises(RateLimitExceeded):
        async with limiter.acquire("user-1", "gpt-4", consume={"session": 999_999}):
            pass
    assert await _stored_ws(repo, "user-1", "gpt-4", "session") == t0   # unmoved

    # And the next admitted request anchors at its own now.
    repo._now_ms = lambda: t0 + 18_000_002
    async with limiter.acquire("user-1", "gpt-4", consume={"session": 1}):
        pass
    assert await _stored_ws(repo, "user-1", "gpt-4", "session") == t0 + 18_000_002


@pytest.mark.asyncio
async def test_a_rejection_at_a_boundary_reports_the_rolled_view(mock_dynamodb, unique_name):
    """Even though nothing is written, the rejection must tell the truth about
    what the caller would have got: the restored balance and the new window's
    end, not the burnt balance and an elapsed one.
    """
    ...
    with pytest.raises(RateLimitExceeded) as exc:
        async with limiter.acquire("user-1", "gpt-4", consume={"session": 999_999}):
            pass
    status = exc.value.status("session")
    assert status.available == 10
    assert status.resets_at_ms == t0 + 18_000_001 + 18_000_000
```

`LimitStatus.resets_at_ms` lands in Task 12; until then assert `status.available` only and add
the second assertion in that task.

- [ ] **Step 9: Pin the idle-restart and the cascade independence**

```python
@pytest.mark.asyncio
async def test_an_idle_entity_restarts_its_window(mock_dynamodb, unique_name):
    """Window ends at t0+5h, entity quiet, calls again at t0+11h -> a FRESH
    window starting t0+11h, not a grid tile at t0+10h.
    """
    ...
    repo._now_ms = lambda: t0 + 11 * 3_600_000
    async with limiter.acquire("user-1", "gpt-4", consume={"session": 1}):
        pass
    assert await _stored_ws(repo, "user-1", "gpt-4", "session") == t0 + 11 * 3_600_000


@pytest.mark.asyncio
async def test_cascade_parent_and_child_anchor_independently(mock_dynamodb, unique_name):
    """ADR-139: the parent's window does not track the child's, consistent
    with cascade already treating limits, shards and `disabled` as per-entity
    state.
    """
    await repo.create_entity("parent")
    await repo.create_entity("child", parent_id="parent", cascade=True)
    repo._now_ms = lambda: t0
    async with limiter.acquire("child", "gpt-4", consume={"session": 1}):
        pass
    # A second child, first seen an hour later, gives the parent nothing new:
    # the parent anchored at t0 with the first child's call.
    await repo.create_entity("child2", parent_id="parent", cascade=True)
    repo._now_ms = lambda: t0 + 3_600_000
    async with limiter.acquire("child2", "gpt-4", consume={"session": 1}):
        pass
    assert await _stored_ws(repo, "parent", "gpt-4", "session") == t0
    assert await _stored_ws(repo, "child2", "gpt-4", "session") == t0 + 3_600_000
```

- [ ] **Step 10: Run, regenerate, commit**

```bash
uv run pytest tests/unit/test_limiter.py tests/unit/test_lease.py -q
uv run hatch run generate-sync
uv run pytest tests/unit/ -q
git add src/zae_limiter/limiter.py src/zae_limiter/lease.py src/zae_limiter/sync_limiter.py \
        src/zae_limiter/sync_lease.py tests/unit/
git commit -m "$(cat <<'EOF'
✨ feat(limiter): roll a duration window on the slow path

`_open_window_if_elapsed` anchors a new window at the first use past the old
one's end — idle-restarting, so a quiet entity's next call opens a fresh
window rather than landing on a grid tile. `_apply_window_roll` then restores
the balance under `ws > rf`, which is `_apply_reset_edge`'s rule with the
backwards cron scan replaced by an attribute read: idempotent, correct for
idle buckets for free, strictly `>`, and to the shard's share rather than the
undivided capacity.

`vu` gains `ws + rsa` as a third voting member of its minimum. That is what
keeps the speculative condition byte-identical: an elapsed window makes
`vu <= now`, the pre-existing guard fails, the failure classifies as
SCHEDULE_BOUNDARY, and the limiter routes it to the slow path — the only
place that re-materialises and therefore the only place that anchors. So
"only admitted use anchors" falls out of the write model rather than being
enforced, and an exhausted quota's rejection (0 WCU on the fast path, raised
before any write on the slow one) cannot move the anchor.

A window elapsing between the acquire path's clock reading and the commit's
is re-expressed at commit time, the exact analogue of the reset-edge case
beside it and silent in the same way if left unhandled.

Refs #222
EOF
)"
```

---
### Task 7: The rollover fan-out

Without this, shard A drawn at 20:00 and shard B at 20:03 anchor different windows and the entity
gets staggered windows again — at which point `resets_at_ms` has no honest value and the
feature's headline number stops existing. This is the one genuinely new distributed-systems
surface in the plan.

**Files:**
- Modify: `src/zae_limiter/repository.py` — new `_propagate_window_start()` beside
  `_propagate_shard_count()` (3108)
- Modify: `src/zae_limiter/repository_protocol.py` — protocol entry
- Modify: `src/zae_limiter/lease.py` — call it from `_commit_initial` after the transaction commits
- Test: `tests/unit/test_repository.py`, `tests/integration/test_bucket_sharding.py`

**Interfaces:**
- Consumes: `schema.pk_bucket`, `schema.BUCKET_FIELD_WS`, `schema.BUCKET_FIELD_VU`;
  `LeaseEntry._window_start_ms` / `_shard_id` / `_shard_count` (Task 6).
- Produces:
  ```python
  async def _propagate_window_start(
      self,
      entity_id: str,
      resource: str,
      shard_id: int,
      shard_count: int,
      window_starts: dict[str, int],   # limit name -> new ws, epoch ms
  ) -> int: ...   # number of (shard, limit) writes that applied
  ```

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_propagate_window_start_writes_every_other_shard(repo):
    await _create_shards(repo, "e1", "gpt-4", count=4, ws=1_000, rsa=18_000)
    written = await repo._propagate_window_start(
        "e1", "gpt-4", shard_id=2, shard_count=4, window_starts={"session": 9_000}
    )
    assert written == 3                       # 0, 1, 3 — never the writer's own
    for s in (0, 1, 3):
        assert await _stored_ws_on(repo, "e1", "gpt-4", "session", shard=s) == 9_000
    # The writer's own shard was stamped by the transaction, not by this.


@pytest.mark.asyncio
async def test_propagate_window_start_is_monotonic(repo):
    """`ws` only ever increases: window n+1 opens at a clock reading strictly
    after window n closed. A delayed write carrying a stale `ws` must not drag
    every shard back a full window.
    """
    await _create_shards(repo, "e1", "gpt-4", count=2, ws=9_000, rsa=18_000)
    written = await repo._propagate_window_start(
        "e1", "gpt-4", shard_id=0, shard_count=2, window_starts={"session": 5_000}
    )
    assert written == 0
    assert await _stored_ws_on(repo, "e1", "gpt-4", "session", shard=1) == 9_000


@pytest.mark.asyncio
async def test_propagate_window_start_is_idempotent(repo):
    await _create_shards(repo, "e1", "gpt-4", count=2, ws=1_000, rsa=18_000)
    first = await repo._propagate_window_start(
        "e1", "gpt-4", shard_id=0, shard_count=2, window_starts={"session": 9_000}
    )
    second = await repo._propagate_window_start(
        "e1", "gpt-4", shard_id=0, shard_count=2, window_starts={"session": 9_000}
    )
    assert (first, second) == (1, 0)


@pytest.mark.asyncio
async def test_propagate_window_start_stamps_vu_zero(repo):
    """The `vu = 0` is the eager mechanism (see "Dependency" §4): it forces
    every sibling off the byte-identical fast path and through exactly one
    materialising pass, which is where `ws > rf` is evaluated. Without it a
    sibling keeps spending its OLD window's balance on a pure ADD with no
    ceiling arithmetic.
    """
    await _create_shards(repo, "e1", "gpt-4", count=2, ws=1_000, rsa=18_000)
    await repo._propagate_window_start(
        "e1", "gpt-4", shard_id=0, shard_count=2, window_starts={"session": 9_000}
    )
    assert await _stored_attr(repo, "e1", "gpt-4", shard=1, attr="vu") == 0


@pytest.mark.asyncio
async def test_propagate_window_start_is_a_noop_at_shard_count_one(repo):
    assert await repo._propagate_window_start(
        "e1", "gpt-4", shard_id=0, shard_count=1, window_starts={"session": 9_000}
    ) == 0


@pytest.mark.asyncio
async def test_propagate_window_start_never_touches_tk(repo):
    """The whole coherence argument rests on this. A fan-out cannot use ADD
    (it does not know each sibling's balance) and a blind SET races the
    sibling's own slow path in both orderings — clobbering its committed
    consumption, or landing under its still-held `rf` lock and leaving it at
    twice its share. Each sibling resets ITSELF, under its own lock.
    """
    await _create_shards(repo, "e1", "gpt-4", count=2, ws=1_000, rsa=18_000)
    await _spend(repo, "e1", "gpt-4", shard=1, limit="session", amount_milli=400_000)
    before = await _stored_attr(repo, "e1", "gpt-4", shard=1, attr="b_session_tk")
    await repo._propagate_window_start(
        "e1", "gpt-4", shard_id=0, shard_count=2, window_starts={"session": 9_000}
    )
    assert await _stored_attr(repo, "e1", "gpt-4", shard=1, attr="b_session_tk") == before
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/unit/test_repository.py -k propagate_window_start -v`
Expected: FAIL — `AttributeError: 'Repository' object has no attribute '_propagate_window_start'`.

- [ ] **Step 3: Implement it**

```python
    async def _propagate_window_start(
        self,
        entity_id: str,
        resource: str,
        shard_id: int,
        shard_count: int,
        window_starts: dict[str, int],
    ) -> int:
        """Stamp a newly anchored duration window on the entity's other shards (ADR-139).

        Mirrors :meth:`_propagate_shard_count` exactly, with ``ws`` substituted
        for ``shard_count``, because ``ws`` has the property that shape needs:
        it is **monotonic**. Window *n+1* opens at a clock reading strictly
        after window *n* closed, so ``ws₀ < ws₀+W ≤ ws₁ < …`` over the life of
        the bucket. What the ``ws < :new`` guard buys, in the same terms:

        - **Idempotent.** Re-running a rollover writes nothing the second time.
        - **Race-free against a concurrent roller.** Two clients crossing the
          boundary milliseconds apart produce two values; the later wins and the
          earlier no-ops. Both are within clock skew of the same instant and the
          window is at most ``reset_after`` long either way — never longer.
        - **Race-free against a delayed write.** A client whose rollover write
          is delayed past the *next* boundary carries a ``ws`` now smaller than
          the stored one, and the condition rejects it. Without monotonicity
          that write would drag every shard back a full window.
        - **Safe under ``--no-aggregator``.** The client owns this, exactly as
          :meth:`bump_shard_count` owns shard-count propagation for the same
          reason.

        **It writes ``ws`` and never ``tk``**, which is the whole coherence
        argument. A fan-out cannot use ``ADD`` — it does not know each sibling's
        balance — and the blind ``SET`` it would otherwise need races the
        sibling's own slow path in both orderings: landing after, it clobbers
        and refunds the sibling's committed consumption; landing before, the
        sibling's ``rf`` lock still holds and its own ``ADD`` applies on top,
        leaving it at twice its share. Both are over-admission. Each sibling
        resets itself, under its own lock, in the write it was going to make
        anyway.

        ``vu = 0`` rides along, for the reason the #468 fan-out writes it and
        for one more. It forces the sibling to take one materialising pass,
        which is where ``ws > rf`` is evaluated — and the fast path is a pure
        ``ADD`` with no ceiling arithmetic, so without it a sibling would keep
        spending its *old* window's balance against a bucket the entity has
        already rolled. A sibling's ``vu`` is usually already expired (its own
        window end was the minimum, and that is what just elapsed), but not
        always — one created before the limit gained its window has no ``ws``
        and a ``vu`` dominated by a cron boundary — so it is stamped
        unconditionally rather than conditionally reasoned about. The cost is
        one skipped aggregator refill per shard per rollover (#508's
        ``vu = :expected_vu`` pin sees the change), which is a missed top-up,
        self-healing on the next batch.

        One write per (sibling, limit) rather than one per sibling covering
        every rolled limit: two duration limits on one item can have different
        lengths and therefore roll at different instants, and an ANDed condition
        over both would no-op the whole write whenever one was already ahead —
        leaving the other staggered. With the single rolling limit the
        motivating product has, the two are the same count.

        Returns:
            The number of writes that applied. A shortfall against
            ``(shard_count - 1) * len(window_starts)`` is logged: a lost write
            leaves a sibling on a stale ``ws``, which costs at most one extra
            window's share on the shards that already rolled when that sibling
            later anchors a window of its own (ADR-139 Consequences).
        """
        if shard_count <= 1 or not window_starts:
            return 0
        client = await self._get_client()

        async def stamp(target_shard: int, name: str, new_ws: int) -> int:
            try:
                await client.update_item(
                    TableName=self.table_name,
                    Key={
                        "PK": {
                            "S": schema.pk_bucket(
                                self._namespace_id, entity_id, resource, target_shard
                            )
                        },
                        "SK": {"S": schema.sk_state()},
                    },
                    UpdateExpression="SET #ws = :new, #vu = :zero",
                    ConditionExpression=(
                        "attribute_exists(PK) AND "
                        "(attribute_not_exists(#ws) OR #ws < :new)"
                    ),
                    # An alias, not the bare name: `bucket_attr` interpolates a
                    # limit name, and `NAME_PATTERN` allows `-` and `.` — `.`
                    # is a document-path separator in an UpdateExpression.
                    ExpressionAttributeNames={
                        "#ws": schema.bucket_attr(name, schema.BUCKET_FIELD_WS),
                        "#vu": schema.BUCKET_FIELD_VU,
                    },
                    ExpressionAttributeValues={
                        ":new": {"N": str(new_ws)},
                        ":zero": {"N": "0"},
                    },
                )
                return 1
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code")
                if code == "ConditionalCheckFailedException":
                    return 0  # already at or ahead of this window, or gone
                raise

        # List comprehension, not a generator: the sync transformer rewrites
        # `gather(*[expr for x in it])` into `_run_in_executor(*[lambda ...])`,
        # which needs the call deferred into the lambda.
        targets = [
            (n, name, ws)
            for n in range(shard_count)
            if n != shard_id
            for name, ws in sorted(window_starts.items())
        ]
        results = await asyncio.gather(*[stamp(n, name, ws) for n, name, ws in targets])
        written = sum(results)
        return written
```

**No `asyncio.gather` keyword** — `return_exceptions=True` would abort sync generation
(`UnsupportedAsyncConstructError`, #491). A non-conditional `ClientError` propagates, which is
correct: the transaction has already committed, so the caller (Step 4) logs and continues rather
than failing the acquire.

- [ ] **Step 4: Call it from the commit**

In `src/zae_limiter/lease.py`, at the end of `_commit_initial()` — **after** the transaction has
succeeded, never before:

```python
        # After the commit, never inside it. The transaction is what makes the
        # roll durable on this shard; the fan-out is what stops the entity's
        # other shards anchoring windows of their own. A failure here is not a
        # failed acquire — the caller was admitted and the write landed — so it
        # is logged and swallowed, and the `ws > rf` rule converges the rest of
        # the shards on whichever `ws` is latest anyway (ADR-139).
        for group_key, starts in window_fanouts.items():
            entity_id, resource, shard_id, shard_count = group_key
            if shard_count <= 1 or not starts:
                continue
            try:
                written = await repo._propagate_window_start(
                    entity_id, resource, shard_id, shard_count, starts
                )
            except Exception:
                logger.warning(
                    "duration-window fan-out failed for resource=%s; siblings will "
                    "anchor their own windows until one converges them",
                    resource,
                    exc_info=True,
                )
                continue
            expected = (shard_count - 1) * len(starts)
            if written < expected:
                logger.info(
                    "duration-window fan-out wrote %d of %d for resource=%s",
                    written, expected, resource,
                )
```

Build `window_fanouts: dict[tuple[str, str, int, int], dict[str, int]]` in the same loop that
builds `window_starts` (Task 6 Step 6), keyed by the group's `(entity_id, resource, shard_id,
shard_count)`. A **cascade** commit has two groups — child and parent — and each fans out over
its **own** `shard_count`, which is exactly what makes parent and child windows independent
(ADR-139).

**The entity id is not logged.** Entity ids are routinely API keys and must not reach logs in
clear text (`py/clear-text-logging-sensitive-data`), the same rule `bump_shard_count`'s
`MAX_SHARD_COUNT` warning follows.

- [ ] **Step 5: Add the protocol entry**

`_propagate_window_start` is private and `_propagate_shard_count` is **not** on
`RepositoryProtocol`. Check before adding: `rg "_propagate_shard_count" src/zae_limiter/repository_protocol.py`.
If it is absent, do not add this one either — `lease.py` types `repo` as `RepositoryProtocol`, so
if the call does not type-check, add **both** or neither, and say which in the commit.

- [ ] **Step 6: Write the convergence integration test**

In `tests/integration/test_bucket_sharding.py`:

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_rollover_converges_every_shard_on_one_window(test_repo):
    """Four shards, one rollover, one window. This is the assertion that makes
    `resets_at_ms` meaningful; without it the entity has four windows drifting
    apart and no honest answer to "when does mine reset".
    """
    limiter = RateLimiter(repository=test_repo)
    await test_repo.set_limits(
        "user-1",
        [Limit.quota("session", 4_000, reset_after=timedelta(seconds=2))],
        resource="gpt-4",
    )
    await _force_shard_count(test_repo, "user-1", "gpt-4", 4)
    # Draw every shard once so all four items exist.
    for shard in range(4):
        await _acquire_on_shard(limiter, "user-1", "gpt-4", shard, {"session": 1})

    await asyncio.sleep(2.1)
    async with limiter.acquire("user-1", "gpt-4", consume={"session": 1}):
        pass

    starts = {
        await _stored_ws_on(test_repo, "user-1", "gpt-4", "session", shard=s)
        for s in range(4)
    }
    assert len(starts) == 1, f"shards anchored different windows: {starts}"
```

- [ ] **Step 7: Run, regenerate, commit**

```bash
uv run pytest tests/unit/test_repository.py -k propagate_window_start -q
uv run hatch run generate-sync
uv run pytest tests/unit/ -q
git add src/zae_limiter/repository.py src/zae_limiter/lease.py src/zae_limiter/sync_repository.py \
        src/zae_limiter/sync_lease.py src/zae_limiter/repository_protocol.py \
        src/zae_limiter/sync_repository_protocol.py tests/unit/ tests/integration/
git commit -m "$(cat <<'EOF'
✨ feat(repository): fan a newly anchored window out to an entity's shards

Without it, shard A drawn at 20:00 and shard B at 20:03 anchor different
windows and `resets_at_ms` has no honest value — `min(ws)+W` over-promises,
`max(ws)+W` under-promises, and for a session cap shown to a human that
number is the product.

`_propagate_window_start` is `_propagate_shard_count` with `ws` substituted,
because `ws` has the property that shape needs: it is monotonic, since window
n+1 opens strictly after window n closed. So `ws < :new` makes the write
idempotent, lets a later roller win over an earlier one, and rejects a
delayed write that would otherwise drag every shard back a full window.

It writes `ws` and never `tk`. A fan-out cannot use ADD — it does not know
each sibling's balance — and the blind SET it would need races the sibling's
slow path in both orderings, clobbering committed consumption or leaving the
shard at twice its share. Each sibling resets itself under its own rf lock.

`vu = 0` rides along and is the eager half: the fast path is a pure ADD with
no ceiling arithmetic, so without it a sibling keeps spending its old
window's balance against a bucket the entity has already rolled.

One write per (sibling, limit), not per sibling: two duration limits on one
item can roll at different instants, and an ANDed condition would no-op the
whole write and leave one staggered.

Refs #222
EOF
)"
```

---

### Task 8: A shard created mid-window joins the window in progress

A client creating shard N>0 may never have read another shard: it got there because the fast path
returned `BUCKET_MISSING` on a shard drawn from a `shard_count` it learned from the entity cache
or a failure image. It has seen the *entity*; `ws` lives on shard *items*.

**The analysis's proposed mechanism does not work.** §3.2 says to add shard 0's key to the
`BatchGetItem` the create path already issues. It cannot: `batch_get_entity_and_buckets` returns
`dict[tuple[str, str, str], BucketState]` keyed by `(entity_id, resource, limit_name)` with **no
shard component**, so shard 0 and shard N collide on every key and one silently overwrites the
other. A separate read is required. It costs 0.5 RCU (eventually consistent), **once per shard
ever**, on a path already priced at 2.5 RCU + 2 WCU.

**Files:**
- Modify: `src/zae_limiter/repository.py` — new `get_shard_window_starts()`
- Modify: `src/zae_limiter/limiter.py` — `_do_acquire`'s create branch (~1852) and
  `_try_parent_only_acquire`
- Test: `tests/unit/test_limiter.py`, `tests/integration/test_bucket_sharding.py`

**Interfaces:**
- Consumes: `schema.pk_bucket`, `schema.bucket_attr`, `BucketState.window_start_ms` (Task 3);
  `RateLimiter._quota_transfer` and `models.new_shard_starting_tokens_milli` (PR #594).
- Produces:
  ```python
  async def get_shard_window_starts(
      self, entity_id: str, resource: str, limit_names: list[str], shard_id: int = 0
  ) -> dict[str, int]: ...   # limit name -> ws, epoch ms; absent keys mean "no window there"
  ```

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_a_new_shard_inherits_the_window_in_progress(mock_dynamodb, unique_name):
    """A shard created mid-window joins it rather than opening its own.

    The created shard sets `rf = now` and inherits `ws`, so `ws > rf` is FALSE
    on the new item and it does not immediately re-roll itself.
    """
    ...  # entity at shard_count=1, window anchored at t0; force a doubling
    repo._now_ms = lambda: t0 + 60_000
    await _acquire_on_shard(limiter, "user-1", "gpt-4", shard=1, consume={"session": 1})
    assert await _stored_ws_on(repo, "user-1", "gpt-4", "session", shard=1) == t0


@pytest.mark.asyncio
async def test_a_new_shard_with_no_sibling_window_opens_one(mock_dynamodb, unique_name):
    """Shard 0 swept by TTL (possible only for resource- and system-level
    configs — ADR-136 gives entity-level buckets none). The degraded case, and
    the same durability asymmetry ADR-139 records.
    """
    ...
    assert await _stored_ws_on(repo, "user-1", "gpt-4", "session", shard=1) == t_now


@pytest.mark.asyncio
async def test_a_cascade_parent_shard_inherits_the_parents_window(mock_dynamodb, unique_name):
    """ADR-139: parent and child anchor independently, so the parent shard's
    `ws` is resolved with the PARENT's own read — never reused from the child,
    whose window may be hours out of step.
    """
    ...  # child anchored at t0, parent anchored at t_parent < t0
    await _acquire_on_shard(limiter, "child", "gpt-4", shard=1, consume={"session": 1})
    assert await _stored_ws_on(repo, "parent", "gpt-4", "session", shard=1) == t_parent
    assert await _stored_ws_on(repo, "child", "gpt-4", "session", shard=1) == t0


@pytest.mark.asyncio
async def test_a_new_quota_shard_is_still_filled_by_transfer(mock_dynamodb, unique_name):
    """A duration window is a quota, so PR #594's reclaim-then-grant must fire
    for it too — a mint here would be #587 again for this feature. Measured as
    #594 measures it: sum(max(0, tk)) across shards, debt excluded.
    """
    before = await _spendable_total(repo, "user-1", "gpt-4", "session")
    ...  # force a doubling and draw a new shard
    after = await _spendable_total(repo, "user-1", "gpt-4", "session")
    assert after == before
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/unit/test_limiter.py -k "new_shard" -v`
Expected: the first fails with the new shard's `ws` equal to `t0 + 60_000` (it opened its own
window); the cascade one fails with the parent inheriting the child's `t0`.

- [ ] **Step 3: Implement the sibling read**

```python
    async def get_shard_window_starts(
        self,
        entity_id: str,
        resource: str,
        limit_names: list[str],
        shard_id: int = 0,
    ) -> dict[str, int]:
        """Read one shard's duration-window starts, to seed a shard being created (ADR-139).

        Shard 0 by default, because :meth:`bump_shard_count` already treats it
        as the source of truth for ``shard_count``. A created shard inherits
        ``ws`` verbatim and sets ``rf = now``, so ``ws > rf`` is **false** on
        the new item and it does not immediately re-roll itself: it joins the
        window in progress rather than opening one.

        A **separate** read rather than an extra key in the create path's
        ``BatchGetItem``: that call returns a dict keyed by ``(entity_id,
        resource, limit_name)`` with no shard component, so shard 0 and shard N
        would collide on every key. 0.5 RCU, eventually consistent, **once per
        shard ever** (≤ 31 per (entity, resource), plus TTL recreations) on a
        path already priced at 2.5 RCU + 2 WCU.

        A limit absent from the result has no window on that shard — either it
        carries none, or the shard has been swept. The caller then opens a fresh
        window, which is the degraded case ADR-139 records under Consequences
        and which idle-restarting makes correct rather than merely tolerable.

        Args:
            entity_id: Entity owning the bucket. On a **cascade** create this is
                the entity whose shard is being created — the parent for a
                parent shard, never the child. Parent and child windows are
                independent (ADR-139).
            resource: Resource name.
            limit_names: The limits to look for; only these attributes are
                projected, so the read stays a fraction of the item.
            shard_id: The shard to read. Defaults to 0.
        """
        if not limit_names:
            return {}
        client = await self._get_client()
        # Aliases, not bare names: `bucket_attr` interpolates a limit name and
        # `NAME_PATTERN` allows `.`, a document-path separator.
        names = {
            f"#w{i}": schema.bucket_attr(n, schema.BUCKET_FIELD_WS)
            for i, n in enumerate(limit_names)
        }
        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(self._namespace_id, entity_id, resource, shard_id)},
                "SK": {"S": schema.sk_state()},
            },
            ProjectionExpression=", ".join(names),
            ExpressionAttributeNames=names,
        )
        item = response.get("Item") or {}
        out: dict[str, int] = {}
        for i, name in enumerate(limit_names):
            raw = item.get(names[f"#w{i}"], {}).get("N")
            if raw is not None:
                out[name] = int(raw)
        return out
```

- [ ] **Step 4: Seed the created shard**

In `src/zae_limiter/limiter.py`'s shard-create branch, immediately before the states are built
(and beside the existing `_quota_transfer` call that PR #594 added):

```python
            # A shard created mid-window joins the window in progress rather
            # than opening its own (ADR-139). `BucketState.from_limit` stamps
            # `ws = now_ms`, which is right for the FIRST shard and wrong for
            # every later one: an entity whose shards each opened their own
            # window has no single `resets_at_ms`, which is the number the
            # feature exists to show.
            #
            # `entity_id` here is the entity whose shard is being created. On a
            # cascade create that is the parent for the parent's shard, read
            # separately — the child's window is independent and may be hours
            # out of step (ADR-139).
            window_limits = [lim.name for lim in limits if lim.reset_after is not None]
            if window_limits and shard_id != 0:
                inherited = await self._repository.get_shard_window_starts(
                    entity_id, resource, window_limits
                )
                for state in states:
                    ws = inherited.get(state.limit_name)
                    if ws is not None:
                        state.window_start_ms = ws
```

`shard_id != 0` because shard 0 has no sibling to inherit from: it *is* the source of truth, and
a shard 0 being created is either the entity's first bucket or a TTL recreation, both of which
correctly open a new window.

- [ ] **Step 5: Confirm the quota transfer still fires**

`RateLimiter._quota_transfer` (PR #594) branches on `Limit.is_quota`, which Task 2 widened to
include `reset_after`. **Read it and verify** that nothing narrower is in the path — in
particular that it does not test `limit.reset_schedule` directly anywhere:

```bash
rg -n "reset_schedule|is_quota" src/zae_limiter/limiter.py src/zae_limiter/models.py \
   | grep -n "quota_transfer\|new_shard_starting_tokens" -A 3 -B 3
rg -n "def new_shard_starting_tokens_milli" -A 40 src/zae_limiter/models.py
```

If any site tests `reset_schedule` where it means "is a quota", change it to `is_quota` and note
it in the commit. That is the structural-predicate rule, and it is exactly the drift #489
records for `_is_custom_config`.

- [ ] **Step 6: Run and commit**

```bash
uv run pytest tests/unit/test_limiter.py -k "new_shard or quota_shard" -q
uv run hatch run generate-sync
uv run pytest tests/unit/ -q
git add src/zae_limiter/repository.py src/zae_limiter/limiter.py src/zae_limiter/sync_*.py tests/
git commit -m "$(cat <<'EOF'
✨ feat(limiter): let a new shard join the window already in progress

`BucketState.from_limit` stamps `ws = now`, which is right for an entity's
first shard and wrong for every later one: shards that each opened their own
window leave the entity with no single `resets_at_ms`, the number this
feature exists to show.

`get_shard_window_starts` reads shard 0 — already the source of truth for
shard_count — and the created shard inherits `ws` verbatim while setting
`rf = now`, so `ws > rf` is false on the new item and it does not
immediately re-roll itself.

A separate projected GetItem rather than an extra key in the create path's
BatchGetItem, which the analysis proposed: that call's result is keyed
(entity, resource, limit_name) with no shard component, so shard 0 and shard
N collide. 0.5 RCU, eventually consistent, once per shard ever, on a path
already priced at 2.5 RCU + 2 WCU.

On a cascade create the parent's window is resolved with the parent's own
read. Parent and child anchor independently (ADR-139), and reusing the
child's would silently make a busy child's window its parent's.

Refs #222
EOF
)"
```

---
### Task 9: The param sync stamps `rsa` and never touches `ws`

`_sync_bucket_params` is how a `set_limits()` reaches existing buckets (#468/#481/#487). It must
carry the new window **length** to every shard of every affected resource, and it must **never**
write `ws` — a config change is not a rollover, and stamping `ws = now` there would restart every
caller's window on an unrelated `rpm` edit, which is the failure mode ADR-138 warned about and
ADR-139 avoids by not reading expiry off `vu`.

**Files:**
- Modify: `src/zae_limiter/repository.py` — `_build_bucket_param_update` /
  `_resolved_bucket_param_update` (3421) and `_sync_bucket_params` (3297)
- Modify: `src/zae_limiter_provisioner/bucket_sync.py` — the sync boto3 mirror
- Test: `tests/unit/test_repository.py`, `tests/unit/test_bucket_sync.py`

**Interfaces:**
- Consumes: `schema.BUCKET_FIELD_RSA`, `Limit.reset_after_seconds` (Tasks 2–3).
- Produces: no new public signature. `_build_bucket_param_update` SETs `b_{name}_rsa` where a
  resolved limit has one and REMOVEs it where it does not.

- [ ] **Step 1: Write the failing tests**

```python
def test_param_sync_stamps_the_window_length(repo):
    upd = repo._build_bucket_param_update(
        [Limit.quota("session", 10_000, reset_after=timedelta(hours=5))],
        stale_limit_names=frozenset(),
        ttl_seconds=None,
    )
    assert "b_session_rsa = :" in upd["UpdateExpression"]
    assert "b_session_ws" not in upd["UpdateExpression"]


def test_param_sync_removes_a_window_a_limit_no_longer_has(repo):
    """A quota converted to a dripping limit must lose `rsa`, or the item
    keeps reconstructing as a quota forever. Absence means "no window", so
    this is a REMOVE — unlike `sched`, where absence means "inherit the item
    default" and #541 needs the explicit BUCKET_SCHED_NONE marker.
    """
    upd = repo._build_bucket_param_update(
        [Limit.per_minute("session", 100)],
        stale_limit_names=frozenset(),
        ttl_seconds=None,
    )
    assert "b_session_rsa" in upd["UpdateExpression"]
    assert upd["UpdateExpression"].index("REMOVE") < upd["UpdateExpression"].index(
        "b_session_rsa"
    )


def test_param_sync_never_writes_ws(repo):
    """A config change is not a rollover. Stamping `ws` here would restart
    every caller's window on an unrelated `rpm` edit — the exact failure
    ADR-138 warned about for a window read off `vu`, and the reason ADR-139
    keeps the anchor in its own attribute.
    """
    for limits in (
        [Limit.quota("session", 10_000, reset_after=timedelta(hours=5))],
        [Limit.per_minute("rpm", 100)],
        [Limit.quota("rpd", 10_000, cron="0 0 * * *")],
    ):
        upd = repo._build_bucket_param_update(limits, frozenset(), None)
        assert "_ws" not in upd["UpdateExpression"], limits
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/unit/test_repository.py -k "param_sync" -v`
Expected: the first two fail (`b_session_rsa` never appears); the third passes already and is
a **regression guard** — it must keep passing after Steps 3–4.

- [ ] **Step 3: SET and REMOVE `rsa`**

In `_build_bucket_param_update`, beside where the per-limit `sched`/`rsched` are handled:

```python
            # SET where the resolved limit has a window, REMOVE where it does
            # not. Absence means "this limit has no duration window", full
            # stop — there is no item-level default to inherit, so this needs
            # no `BUCKET_SCHED_NONE` analogue (#541). A `rsa` left behind on
            # a limit converted back to a drip would keep the item
            # reconstructing as a quota forever.
            rsa_attr = schema.bucket_attr(limit.name, schema.BUCKET_FIELD_RSA)
            if limit.reset_after_seconds is not None:
                alias = f":rsa{i}"
                set_parts.append(f"{rsa_attr} = {alias}")
                expr_values[alias] = {"N": str(limit.reset_after_seconds)}
            else:
                remove_parts.append(rsa_attr)
```

`i` is the existing monotonic counter over the resolved limits. Do **not** use the limit name in
an alias (#487's `#stale{i}_{j}` rule).

**`ws` is not written here, and the `vu = 0` the sync already stamps unconditionally is what
makes that safe.** The next acquire on each shard takes one materialising pass; if the window has
elapsed it anchors a new one there, and if it has not, `_open_window_if_elapsed` returns `None`
and the existing `ws` stands. A shard that gains a window for the first time (the limit was a
drip before) has `rsa` and no `ws`; `_open_window_if_elapsed`'s `end is None` branch opens its
first one on that same pass.

- [ ] **Step 4: Mirror it in the provisioner**

`src/zae_limiter_provisioner/bucket_sync.py` is the sync boto3 mirror and has the identical
exposure on every manifest apply. Apply the same SET/REMOVE, and add the same three tests to
`tests/unit/test_bucket_sync.py`. The two cannot share a module — the provisioner zip carries
only the four-file `zae_limiter` stub (ADR-113), which is why `_encode_one_tuple` is duplicated
there already.

- [ ] **Step 5: Run and commit**

```bash
uv run pytest tests/unit/test_repository.py tests/unit/test_bucket_sync.py -q
uv run hatch run generate-sync
git add src/zae_limiter/repository.py src/zae_limiter/sync_repository.py \
        src/zae_limiter_provisioner/bucket_sync.py tests/unit/
git commit -m "$(cat <<'EOF'
✨ feat(repository): carry a window length through the param sync

`set_limits()` fans a limit change out to every shard of every affected
resource (#468/#481/#487), and a duration window's length has to ride along
or a shard keeps enforcing the window it was born with forever — bucket
items are the only thing the aggregator reads.

SET where the resolved limit has a window, REMOVE where it does not. Absence
means "no window", full stop, so this needs no BUCKET_SCHED_NONE analogue
(#541) — there is no item-level default to inherit. A `rsa` left behind on
a limit converted back to a drip would keep the item reconstructing as a
quota forever.

`ws` is deliberately NOT written. A config change is not a rollover, and
stamping it here would restart every caller's window on an unrelated `rpm`
edit — the failure ADR-138 warned about for a window read off `vu`. The
`vu = 0` this write already stamps forces the materialising pass that
anchors, or leaves the window alone if it has not elapsed.

Mirrored in the provisioner's bucket_sync, which has the identical exposure
on every manifest apply.

Refs #222
EOF
)"
```

---

### Task 10: The aggregator rolls a window it sees

The aggregator reads the item and nothing else. Without this it refills a duration quota toward a
ceiling it never restores, and (worse) `_is_quota_limit` misreads one as a dripping limit at
shard-create time and mints it a fresh share — #587 again, for this feature.

**Files:**
- Modify: `src/zae_limiter_aggregator/processor.py` — `_parse_bucket_record` /
  `ParsedBucketLimit` / `LimitRefillInfo`; the roll branch beside the reset branch (~839);
  `_item_next_boundary`; `_is_quota_limit`
- Test: `tests/unit/test_processor.py`

**Interfaces:**
- Consumes: the stream image's `b_{name}_ws` / `b_{name}_rsa` (Tasks 5, 9).
- Produces:
  ```python
  @dataclass(frozen=True)
  class ParsedBucketLimit:
      ...
      window_start_ms: int | None = None      # NEW
      reset_after_seconds: int | None = None  # NEW
  ```
  `LimitRefillInfo` gains the same two fields.

- [ ] **Step 1: Write the failing tests**

```python
def test_parse_reads_the_window_off_the_image():
    image = _bucket_image(limits={"session": {"cp": 10_000_000, "ra": 0}},
                          extra={"b_session_ws": {"N": "5000"},
                                 "b_session_rsa": {"N": "18000"}})
    parsed = _parse_bucket_record(image)
    assert parsed.limits["session"].window_start_ms == 5_000
    assert parsed.limits["session"].reset_after_seconds == 18_000


def test_is_quota_limit_recognises_a_duration_window():
    """Reads the STREAM IMAGE, not a `Limit`, so it tests stored attributes.
    A duration quota misread as a dripping limit here would be minted a fresh
    share at shard-create time — #587 again, for this feature.
    """
    image = _bucket_image(limits={"session": {"cp": 10_000_000, "ra": 0}},
                          extra={"b_session_rsa": {"N": "18000"}})
    assert _is_quota_limit("session", image) is True


def test_aggregator_rolls_an_elapsed_window():
    """Evaluated BEFORE the accrual-rate guard, for the reason the reset
    branch is: a duration quota's stored rate is 0 (ADR-137) and that guard
    would skip exactly the limits the feature exists for.
    """
    # rf = 1000, ws = 5000 -> ws > rf, so this shard has not applied the
    # window yet. tk = 0, effective cp = 10_000_000 -> ADD +10_000_000.
    ...
    assert writes[0]["ExpressionAttributeValues"][":rd_session"] == 10_000_000


def test_wcu_never_carries_a_window():
    """`rsched` is item-level and needed an explicit carve-out
    (processor.py:799-804) or a user's midnight reset would hand `wcu` its
    per-partition write ceiling back at every edge. `ws` is per-limit, so the
    exemption is STRUCTURAL: `wcu` simply never carries one.
    """
    image = _bucket_image(limits={"wcu": {"cp": 1_000_000, "ra": 1_000_000}})
    assert _parse_bucket_record(image).limits["wcu"].window_start_ms is None


def test_the_aggregator_does_not_fan_out():
    """It processes one bucket shard per stream record and would fan out once
    per shard per batch — S² writes rather than S. The client's fan-out plus
    the `ws > rf` rule already converges every shard; this is an optimisation
    on top.
    """
    ...
    assert not any("_ws = " in w["UpdateExpression"] for w in writes)
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/unit/test_processor.py -k "window or is_quota_limit" -v`
Expected: FAIL — `AttributeError: 'ParsedBucketLimit' object has no attribute 'window_start_ms'`.

- [ ] **Step 3: Parse the two attributes**

Add `window_start_ms: int | None = None` and `reset_after_seconds: int | None = None` to
`ParsedBucketLimit` and `LimitRefillInfo`, and read them in `_parse_bucket_record` beside the
`sched` / `reset_sched` it already reads:

```python
        ws_raw = image.get(bucket_attr(name, BUCKET_FIELD_WS), {}).get("N")
        rsa_raw = image.get(bucket_attr(name, BUCKET_FIELD_RSA), {}).get("N")
```

**No new failure mode.** These are integers, not a compact grammar, so there is no `sched_error`
analogue and nothing new can poison a batch — which is a real difference from `rsched` and worth
the comment.

- [ ] **Step 4: Widen `_is_quota_limit`**

```python
def _is_quota_limit(limit_name: str, image: dict[str, Any]) -> bool:
    """Does this limit on this stream image recover by reset rather than drip?

    The stored twin of `Limit.is_quota`, and since ADR-139 there are **two**
    spellings of the reset half on an item: `b_{name}_rsched` (a calendar
    cron) and `b_{name}_rsa` (a duration window). Both must be recognised,
    because the caller uses this to decide whether a shard coming into
    existence is filled by transfer or minted a fresh share (#587) — and a
    duration quota misread as a dripping limit is #587 reintroduced for
    exactly the limit shape this feature adds.
    """
    ...  # existing rsched test, OR'd with:
    return existing or bucket_attr(limit_name, BUCKET_FIELD_RSA) in image
```

Read the landed implementation before editing; the `ra == 0` half of the test may already be
there, in which case only the reset half widens.

- [ ] **Step 5: Add the roll branch**

Immediately before the existing `if reset_sched:` branch (so a limit carrying both — which
`Limit` rejects, so only a corrupt item — takes the calendar branch and is left as it was):

```python
        # A duration window rolled since this item was last refilled sets the
        # balance to the effective capacity, which as an `ADD` is
        # `eff_cp - tk_observed` — the identical delta shape the reset branch
        # below and the unconditional clamp use, and safe for the identical
        # commutativity reason. It is the same `> rf` comparison, against the
        # same stored `rf`, that `RateLimiter._apply_window_roll()` makes on
        # the client, so whichever writer gets there first stamps `rf` past
        # the window start and the other one skips.
        #
        # Evaluated **before** the accrual-rate guard further down, not after
        # it: a duration quota's stored rate is 0 since ADR-137, so that guard
        # would skip exactly the limits this exists for.
        #
        # It applies a window the CLIENT anchored; it never anchors one. The
        # aggregator acts only on stream records, and an exhausted quota
        # produces none (a fast rejection is 0 WCU), so it could not anchor for
        # an idle entity even if it tried — which is correct, since the window
        # must be anchored to a *use*. And it does not fan out: it processes
        # one shard per record and would issue S² writes per batch rather than
        # S. The client's fan-out plus `ws > rf` already converges every shard.
        if info.window_start_ms is not None and info.window_start_ms > state.rf_ms:
            roll_delta = effective_cp - info.tk_milli
            if roll_delta != 0:
                any_needs_refill = True
                add_parts.append(f"{bucket_attr(limit_name, BUCKET_FIELD_TK)} :wd_{limit_name}")
                expr_values[f":wd_{limit_name}"] = roll_delta
            continue
```

- [ ] **Step 6: Add `ws + rsa` to `_item_next_boundary`**

```python
        # The third voting member of `vu`, exactly as on the client
        # (`RateLimiter._materialisation_stamps`). The "an item-level pair is a
        # member in its own right" reasoning (#541) does **not** apply: `ws`
        # has no item-level default, so only the per-limit values vote.
        if info.window_start_ms is not None and info.reset_after_seconds is not None:
            candidates.append(info.window_start_ms + info.reset_after_seconds * 1000)
```

- [ ] **Step 7: Run and commit**

```bash
uv run pytest tests/unit/test_processor.py -q
uv run pytest tests/unit/ -k lambda_builder -q     # import closure unchanged
git add src/zae_limiter_aggregator/processor.py tests/unit/test_processor.py
git commit -m "$(cat <<'EOF'
✨ feat(aggregator): roll a duration window it sees on the stream

`_parse_bucket_record` reads `b_{name}_ws` and `b_{name}_rsa` beside the
schedules it already parses — integers rather than a compact grammar, so
there is no sched_error analogue and nothing new can poison a batch.

The roll branch sits beside the reset branch and before the accrual-rate
guard, for the reason the reset branch does: a duration quota's stored rate
is 0 under ADR-137, so that guard would skip exactly the limits this exists
for. Same `ADD (eff_cp - tk_observed)` delta, same commutativity argument,
same `> rf` comparison against the same stored `rf` the client makes.

It applies windows and never anchors one, and it does not fan out: it
processes one shard per record and would issue S^2 writes per batch rather
than S. The client's fan-out plus `ws > rf` already converges every shard.

`_is_quota_limit` widens to recognise the duration spelling. It reads the
stream image rather than a `Limit`, and a duration quota misread there as a
dripping limit would be minted a fresh share at shard-create time — #587
reintroduced for exactly the shape this feature adds.

`wcu` gets its exemption structurally: `ws` is per-limit, so `wcu` simply
never carries one, where `rsched` needed an explicit carve-out.

Refs #222
EOF
)"
```

---

### Task 11: The TTL recovery horizon

`reset_after` is the recovery cycle **exactly** — no cron parse, no `cycle_seconds` ladder, no
rounding up, no clock. Sharper than the calendar branch, which rounds a monthly pattern to 31
days. It is also what makes ADR-138's durability objection moot, so the arithmetic must be
verified rather than asserted.

**Files:**
- Modify: `src/zae_limiter/schema.py` — `_recovery_seconds` (705), the branch at 753-754
- Test: `tests/unit/test_schema.py`

**Interfaces:**
- Consumes: `Limit.is_quota`, `Limit.reset_after_seconds` (Task 2).
- Produces: no signature change. `calculate_bucket_ttl_seconds(limits, multiplier)` keeps taking
  no clock, which is what its three production callers need.

- [ ] **Step 1: Write the failing tests**

```python
def test_recovery_horizon_of_a_duration_quota_is_its_window():
    limit = Limit.quota("session", 10_000, reset_after=timedelta(hours=5))
    assert schema._recovery_seconds(limit) == 18_000.0


def test_bucket_ttl_of_a_duration_quota():
    limit = Limit.quota("session", 10_000, reset_after=timedelta(hours=5))
    # 5h x the default multiplier of 7 = 35h. An item swept by this means the
    # entity has been idle ~7 windows, at which point idle-restarting makes a
    # fresh window the SPECIFIED behaviour rather than data loss — which is
    # ADR-138's durability objection answered (ADR-139 Consequences).
    assert schema.calculate_bucket_ttl_seconds([limit], multiplier=7) == 126_000


def test_a_duration_quota_does_not_divide_by_its_zero_rate():
    """#532's crash shape, restated. `_recovery_seconds`'s quota branch is
    `min(_reset_cycle_seconds(e) for e in limit.reset_schedule)`, and a
    duration quota's `reset_schedule` is EMPTY — so without its own branch
    this raises `ValueError: min() arg is an empty sequence` rather than
    falling through to the time-to-fill division. Either way it is a crash on
    an admin path, and either way the answer is a branch of its own.
    """
    limit = Limit.quota("session", 10_000, reset_after=timedelta(hours=5))
    schema.calculate_bucket_ttl_seconds([limit], multiplier=7)  # must not raise


def test_mixed_item_takes_the_max_across_both_quota_spellings():
    slow = Limit.custom("slow", capacity=1000, refill_amount=10, refill_period_seconds=60)
    session = Limit.quota("session", 10_000, reset_after=timedelta(hours=5))
    daily = Limit.quota("rpd", 10_000, cron="0 0 * * *")
    # 6000s, 18000s, 86400s -> the daily wins.
    assert schema.calculate_bucket_ttl_seconds([slow, session, daily], multiplier=1) == 86_400
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/unit/test_schema.py -k "duration_quota or both_quota_spellings" -v`
Expected: `ValueError: min() arg is an empty sequence` from `_recovery_seconds`.

**Note the failure text.** It is not a `ZeroDivisionError`, because the `is_quota` branch is
taken and short-circuits before the division — so this is #532's *shape* on a different
exception. Both are a crash on the admin path that `set_limits()` runs.

- [ ] **Step 3: Add the branch**

Replace `_recovery_seconds`'s quota branch:

```python
    if limit.is_quota:
        # A duration window's cycle is `reset_after`, exactly and by
        # construction (ADR-139): no cron parse, no `cycle_seconds` ladder, no
        # rounding-up approximation, and — decisively — no clock, which
        # `calculate_bucket_ttl_seconds` does not have and none of its three
        # production callers can supply. Strictly sharper than the calendar
        # branch below, which rounds a monthly pattern up to 31 days.
        #
        # The two spellings are mutually exclusive (ADR-137/ADR-139), so this
        # is an either/or rather than a `max` — and it must come first, because
        # a duration quota's `reset_schedule` is empty and `min()` over an
        # empty sequence raises.
        if limit.reset_after_seconds is not None:
            return float(limit.reset_after_seconds)
        return float(min(_reset_cycle_seconds(entry) for entry in limit.reset_schedule))
```

- [ ] **Step 4: Verify the ADR-139 claim with real arithmetic**

```bash
uv run python -c "
from datetime import timedelta
from zae_limiter import Limit
from zae_limiter import schema
lim = Limit.quota('session', 10_000, reset_after=timedelta(hours=5))
ttl = schema.calculate_bucket_ttl_seconds([lim], multiplier=7)
print('window  ', lim.reset_after_seconds, 's')
print('ttl     ', ttl, 's =', ttl / 3600, 'h')
print('windows ', ttl / lim.reset_after_seconds)
"
```

Expected: `window 18000 s`, `ttl 126000 s = 35.0 h`, `windows 7.0`. **If it does not print 7.0,
ADR-139's Consequences claim is wrong and must be corrected before this task is committed** —
the claim is precisely that an item swept by TTL means idleness of `reset_after × multiplier`,
so a swept item starting a fresh window is the specified behaviour rather than loss.

Also confirm ADR-136's other half: an entity-level config carries **no** TTL at all, so a
per-entity session cap — entity-level by nature — is never swept, and only resource- and
system-level duration windows reach this formula.

```bash
rg -n "_is_custom_config" -A 20 src/zae_limiter/limiter.py | head -30
```

- [ ] **Step 5: Run and commit**

```bash
uv run pytest tests/unit/test_schema.py -q
git add src/zae_limiter/schema.py tests/unit/test_schema.py
git commit -m "$(cat <<'EOF'
✨ feat(schema): price a duration quota's TTL at its own window

`reset_after` IS the recovery cycle — no cron parse, no cycle_seconds ladder,
no rounding up, and no clock, which `calculate_bucket_ttl_seconds` does not
have and none of its three callers can supply. Strictly sharper than the
calendar branch, which rounds a monthly pattern up to 31 days.

Without its own branch this is #532's shape on a different exception: the
`is_quota` branch is taken, `reset_schedule` is empty, and `min()` over an
empty sequence raises on the admin path `set_limits()` runs.

At the default multiplier of 7 a 5-hour window gives a 35-hour TTL, so an
item swept by it means the entity has been idle about seven windows — at
which point idle-restarting makes a fresh window the specified behaviour
rather than data loss. That is ADR-138's durability objection answered, and
the test asserts the arithmetic rather than the prose.

Refs #222
EOF
)"
```

---

### Task 12: `resets_at_ms`, `retry_after_seconds` and the CLI

Both become constants read off the item rather than scans. But `resets_at_ms` is currently
computed from the `Limit` alone, and a duration window's `ws` lives on the **bucket**, so
`LimitStatus` has to carry it.

**Files:**
- Modify: `src/zae_limiter/models.py` — `LimitStatus` (828)
- Modify: `src/zae_limiter/exceptions.py` — `_limit_shape` (~156-166), `as_dict`
- Modify: `src/zae_limiter/bucket.py` — `declared_statuses` / `try_consume`
- Modify: `src/zae_limiter/limiter.py` — `_admit_limit`, `_readable_balance`, `check_availability`
- Modify: `src/zae_limiter/lease.py` — `_build_retry_failure_statuses`
- Modify: `src/zae_limiter/cli.py` — `_format_limit` (2284)
- Test: `tests/unit/test_exceptions.py`, `tests/unit/test_limiter.py`, `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: `BucketState.window_end_ms` (Task 3); `Limit.is_quota` / `reset_after` (Task 2).
- Produces:
  ```python
  @dataclass(frozen=True)
  class LimitStatus:
      ...
      resets_at_ms: int | None = None   # NEW — absolute instant the allowance returns

  # exceptions.py — takes the STATUS, not the limit
  def _limit_shape(self, status: LimitStatus, now_ms: int) -> dict[str, Any]: ...
  ```

- [ ] **Step 1: Write the failing tests**

```python
def test_a_duration_quota_reports_a_constant_reset_instant():
    """`ws + reset_after`, read straight off the item — no scan. The calendar
    form needs `next_reset_edge`'s bounded cron walk; this one does not.
    """
    status = LimitStatus(
        entity_id="e1", resource="gpt-4", limit_name="session",
        limit=Limit.quota("session", 10_000, reset_after=timedelta(hours=5)),
        available=0, requested=1, exceeded=True, retry_after_seconds=3600.0,
        resets_at_ms=1_757_018_000_000,
    )
    exc = RateLimitExceeded(statuses=[status], retry_after_seconds=3600.0)
    body = exc.as_dict()["limits"][0]
    assert body["kind"] == "quota"
    assert body["capacity"] == 10_000
    assert body["resets_at_ms"] == 1_757_018_000_000
    # #545: a quota omits the drip fields entirely.
    assert "refill_amount" not in body
    assert "refill_period_seconds" not in body


def test_a_calendar_quota_still_scans_for_its_edge():
    """Regression guard: `_limit_shape` now takes a status, and a status with
    `resets_at_ms=None` for a CALENDAR quota must still fall back to
    `next_reset_edge`, or #545's whole surface goes null.
    """
    status = LimitStatus(..., limit=Limit.quota("rpd", 10_000, cron="0 0 * * *"),
                         resets_at_ms=None, ...)
    body = RateLimitExceeded(statuses=[status], retry_after_seconds=1.0).as_dict()["limits"][0]
    assert body["resets_at_ms"] is not None


def test_a_rate_limit_carries_no_reset_instant():
    status = LimitStatus(..., limit=Limit.per_minute("rpm", 100), resets_at_ms=None, ...)
    body = RateLimitExceeded(statuses=[status], retry_after_seconds=1.0).as_dict()["limits"][0]
    assert body["kind"] == "rate"
    assert "resets_at_ms" not in body


def test_retry_after_for_a_duration_quota_is_the_wait_to_its_window_end():
    """A quota has no drip at all under ADR-137, so the window end is the only
    finite answer — the same call the calendar form's reset branch makes.
    """
    ...
    assert status.retry_after_seconds == pytest.approx(3600.0, abs=1.0)


def test_format_limit_renders_a_duration_window():
    limit = Limit.quota("session", 10_000, reset_after=timedelta(hours=5))
    assert cli._format_limit(limit) == "session: 10,000 quota (resets 5h after first use)"


def test_format_limit_still_renders_a_cron_quota():
    limit = Limit.quota("rpd", 10_000, cron="0 0 * * *")
    assert "resets" in cli._format_limit(limit)
    assert "after first use" not in cli._format_limit(limit)
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/unit/test_exceptions.py tests/unit/test_cli.py -k "duration or reset_instant or format_limit" -v`
Expected: FAIL — `TypeError: LimitStatus.__init__() got an unexpected keyword argument 'resets_at_ms'`.

- [ ] **Step 3: Add the field**

```python
    retry_after_seconds: float  # time until `requested` is available (0 if not exceeded)
    # The absolute instant this limit's allowance returns, epoch ms, or None
    # for a rate limit and for a quota whose edge is out of scan reach.
    #
    # It has to live here rather than being derived from `limit` because a
    # duration window's anchor (`ws`) is on the BUCKET, not in the config
    # (ADR-139). A calendar edge is recoverable from the clock plus the cron,
    # which is why `_limit_shape` could take a `Limit` until now; a per-entity
    # window is not recoverable from anything but the item.
    #
    # Populated at all four construction sites — `bucket.declared_statuses`,
    # `RateLimiter._admit_limit`, `lease._build_retry_failure_statuses` and
    # `RateLimiter.check_availability` — the same four #222 §7 wired the
    # boundary-aware `retry_after_seconds` at.
    resets_at_ms: int | None = None
```

- [ ] **Step 4: Make `_limit_shape` take the status**

```python
    def _limit_shape(self, status: "LimitStatus", now_ms: int) -> dict[str, Any]:
        ...
        limit = status.limit
        if not limit.is_quota:
            return {
                "kind": "rate",
                "capacity": limit.capacity,
                "refill_amount": limit.refill_amount,
                "refill_period_seconds": limit.refill_period_seconds,
            }
        # A duration window's instant is `ws + reset_after`, computed where the
        # bucket was read and carried on the status — no scan, and no way to
        # recompute it here, since `ws` is not in the config (ADR-139). A
        # calendar edge still falls back to the cron walk, which is the only
        # source for it and which #574 widened to the reset's own cycle.
        resets_at_ms = status.resets_at_ms
        if resets_at_ms is None and limit.reset_schedule:
            resets_at_ms = next_reset_edge(limit.reset_schedule, now_ms=now_ms)
        return {"kind": "quota", "capacity": limit.capacity, "resets_at_ms": resets_at_ms}
```

and in `as_dict`, change `**self._limit_shape(s.limit, now_ms)` to `**self._limit_shape(s, now_ms)`.

- [ ] **Step 5: Populate it at the four sites**

At each, the value is `state.window_end_ms` when the limit carries a `reset_after`, else `None`:

| Site | File | Source of the `BucketState` |
|---|---|---|
| `declared_statuses` / `would_refill_satisfy` | `src/zae_limiter/bucket.py` | the `ALL_OLD` image (Task 5 decodes it) |
| `_admit_limit` | `src/zae_limiter/limiter.py` | the slow path's state, **after** `_open_window_if_elapsed` |
| `_build_retry_failure_statuses` | `src/zae_limiter/lease.py` | `entry.state` |
| `check_availability` | `src/zae_limiter/limiter.py` | the summed read — see Step 6 |

**`_admit_limit` must read it after the roll**, so a rejection at a boundary reports the *new*
window's end rather than the one that just elapsed (Task 6 Step 8's second test).

For `retry_after_seconds`: a duration quota's wait is `max(0, window_end_ms - now_ms) / 1000`.
Add it as a branch in `schedule.retry_after_with_schedule`'s caller rather than inside
`schedule.py` — **`schedule.py` may import nothing from `models.py`**, and that one-way
dependency is what lets it be vendored into both Lambda packages. Compute it at the four sites
and pass it in, the same way the per-shard floored-rate rule is re-derived there.

- [ ] **Step 6: `check_availability` reports one window across shards**

```python
        # Every shard carries the same `ws` (the fan-out is what guarantees it,
        # Task 7), so there is ONE window and one honest `resets_at_ms`. That
        # is the whole reason the fan-out is in the baseline rather than an
        # optimisation: with per-shard windows `min(ws)+W` over-promises and
        # `max(ws)+W` under-promises, and for a session cap shown to a human
        # that number is the product.
        #
        # `max` across shards rather than `min`, so a shard lagging the fan-out
        # cannot report the window ending sooner than it does.
        window_ends = [b.window_end_ms for b in shards if b.window_end_ms is not None]
        resets_at_ms = max(window_ends) if window_ends else None
```

and extend `_readable_balance` so a shard past its window end reports the balance the next
`acquire()` will restore, decided **per shard** exactly as the reset branch already is:

```python
        # Same reasoning as the reset branch above, and the same per-shard
        # granularity: a sharded entity can have some shards past the boundary
        # and some not, and collapsing that would report the whole entity
        # restored on the strength of one stale shard. Reads `ws`/`rsa` off
        # the item rather than from config, because the `rf` it is compared
        # against lives on the item — the same pairing `_apply_window_roll`
        # uses on the slow path, so the two agree by construction.
        if limit is not None and limit.reset_after is not None:
            end = bucket.window_end_ms
            if end is not None and now_ms >= end:
                return bucket.effective_capacity_milli(now_ms) // 1000
            if bucket.window_start_ms is not None and bucket.window_start_ms > bucket.last_refill_ms:
                return bucket.effective_capacity_milli(now_ms) // 1000
```

The **second** branch is the one a reader would omit: a shard that has received the fan-out but
has not been drawn since holds a `ws` newer than its `rf` and a burnt balance, and the next
`acquire()` on it restores the quota immediately.

- [ ] **Step 7: Render it in the CLI**

```python
        # The reset stays on the headline, beside the allowance, for the reason
        # #542 put the cron there: this line renders what a limit allows and
        # how it recovers. A duration window's recovery is a duration, and
        # "after first use" is the part an operator will otherwise get wrong —
        # the obvious misreading is "every 5 hours on the clock", which is the
        # calendar form.
        if limit.reset_after is not None:
            return (
                f"{limit.name}: {limit.capacity:,} quota "
                f"(resets {_format_duration(limit.reset_after)} after first use)"
            )
        resets = ", ".join(_format_cron_entry(entry) for entry in limit.reset_schedule)
        return f"{limit.name}: {limit.capacity:,} quota (resets {resets})"
```

with a `_format_duration(td: timedelta) -> str` helper rendering `5h`, `30m`, `90s`, `1d`, and
`5h30m` for a mixed one — largest unit first, zero components omitted, never `0s` for a
non-zero duration.

- [ ] **Step 8: Run, regenerate, commit**

```bash
uv run pytest tests/unit/ -q
uv run hatch run generate-sync
git add src/zae_limiter/models.py src/zae_limiter/exceptions.py src/zae_limiter/bucket.py \
        src/zae_limiter/limiter.py src/zae_limiter/lease.py src/zae_limiter/cli.py \
        src/zae_limiter/sync_*.py tests/unit/
git commit -m "$(cat <<'EOF'
✨ feat(models): report when a duration window resets

`LimitStatus.resets_at_ms` carries the instant a quota's allowance returns,
and `_limit_shape` now takes the status rather than the limit. It has to: a
calendar edge is recoverable from the clock plus the cron, which is why a
`Limit` sufficed, but a duration window's anchor lives on the bucket item
(ADR-139) and nothing in the config can reconstruct it.

The 429 body's JSON is unchanged — #545's `kind` plus `resets_at_ms`, drip
fields omitted for a quota. Only the plumbing moved. A calendar quota with no
carried instant still falls back to the cron walk, which is the regression
this could silently have caused.

For a duration window both `resets_at_ms` and `retry_after_seconds` are
constants read off the item — `ws + reset_after` — rather than scans. The
arithmetic is computed at the four status sites and passed in rather than
added to `schedule.py`, which may import nothing from `models.py`; that
one-way dependency is what lets it be vendored into both Lambda packages.

`check_availability` reports ONE window across shards, which is what the
rollover fan-out exists to guarantee, and `_readable_balance` gains the
duration branch per shard — including the case a reader would omit, a shard
that has received the fan-out but has not been drawn since.

Refs #222
EOF
)"
```

---
### Task 13: The manifest and the CloudFormation round trip

**Files:**
- Modify: `src/zae_limiter_provisioner/manifest.py` — `LimitDecl` (109), `from_dict` (127)
- Modify: `src/zae_limiter_provisioner/handler.py` — `_CFN_LIMIT_OPTIONAL_KEYS` (616)
- Modify: `src/zae_limiter/limits_cli.py` — the inverse map
- Test: `tests/unit/test_manifest.py`, `tests/unit/test_handler.py`, `tests/unit/test_limits_cli.py`

**Interfaces:**
- Consumes: `Limit.reset_after` (Task 2).
- Produces: manifest key `reset_after_seconds` (int); CFN property `ResetAfterSeconds` (string,
  coerced by `_coerce_int`); `LimitDecl.reset_after_seconds: int | None = None`.

The manifest and CFN spell it `..._seconds` and take an integer, while Python takes a
`timedelta`. That is the rule stated in the header, not an inconsistency: **the unit lives in the
name wherever the type cannot carry it**, and a YAML scalar and a CloudFormation string carry no
type at all.

- [ ] **Step 1: Write the failing tests**

```python
def test_manifest_parses_a_duration_window():
    decl = LimitDecl.from_dict({"capacity": 10_000, "reset_after_seconds": 18_000})
    assert decl.capacity == 10_000
    assert decl.reset_after_seconds == 18_000
    # ADR-137: a reset flips the refill_amount shorthand default to 0, so the
    # natural manifest — allowance plus window, nothing else — is the valid one.
    assert decl.refill_amount == 0
    assert decl.to_limit("session").reset_after == timedelta(hours=5)


def test_manifest_rejects_both_reset_spellings():
    with pytest.raises(ValueError, match="one recovery mechanism"):
        LimitDecl.from_dict({
            "capacity": 10_000,
            "reset_after_seconds": 18_000,
            "reset_schedule": [{"cron": "0 0 * * *"}],
        })


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "5h"])
def test_manifest_rejects_a_non_integral_window(bad):
    """#569's whole-number rule and #564's finiteness rule, both of which the
    schedule absolutes already carry. `True` is rejected even though bool is
    an int subclass: a window of `true` is a mistake, not one second.
    """
    with pytest.raises(ValueError):
        LimitDecl.from_dict({"capacity": 10_000, "reset_after_seconds": bad})


def test_cfn_round_trip_of_a_duration_window():
    """CloudFormation delivers EVERY property as a string (#554), so the
    coercer is not optional here.
    """
    manifest_key, coercer = handler._CFN_LIMIT_OPTIONAL_KEYS["ResetAfterSeconds"]
    assert manifest_key == "reset_after_seconds"
    assert coercer("18000", "ResetAfterSeconds") == 18_000
    with pytest.raises(ValueError, match="whole number"):
        coercer("5h", "ResetAfterSeconds")


def test_cfn_keys_are_exact_inverses():
    """`limits_cli._SCHEDULE_KEYS` and `handler._CFN_SCHEDULE_KEYS` cannot
    share a module — the provisioner zip carries only the four-file
    `zae_limiter` stub (ADR-113) — so a test is what keeps them inverse.
    Extend the existing one rather than adding a second.
    """
    ...
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/unit/test_manifest.py tests/unit/test_handler.py -k "duration or reset_after" -v`
Expected: FAIL — `LimitDecl` has no `reset_after_seconds`.

- [ ] **Step 3: Extend `LimitDecl`**

```python
    reset_schedule: tuple[ScheduleEntry, ...] = ()
    # A window anchored to the entity's own first use (ADR-139). Spelled
    # `..._seconds` and typed `int` here because a YAML scalar carries no type,
    # where the Python API takes a `timedelta` whose type carries the unit.
    reset_after_seconds: int | None = None
```

and in `from_dict`, after the schedule parsing:

```python
        reset_after_seconds = d.get("reset_after_seconds")
        if reset_after_seconds is not None:
            # `bool` is an `int` subclass in Python; a window of `true` is a
            # mistake, not one second. Same call `_coerce_int` makes (#569).
            if isinstance(reset_after_seconds, bool) or not isinstance(
                reset_after_seconds, int
            ):
                raise ValueError(
                    f"reset_after_seconds must be a whole number of seconds, got "
                    f"{reset_after_seconds!r}. Limits are rejected at parse time so "
                    f"`limits plan` surfaces the problem before anything is written."
                )
            if reset_after_seconds <= 0:
                raise ValueError(
                    f"reset_after_seconds must be positive, got {reset_after_seconds}."
                )
            if reset_schedule:
                raise ValueError(
                    "a limit has one recovery mechanism: `reset_after_seconds` names a "
                    "window anchored to the entity's own first use and `reset_schedule` "
                    "names fixed calendar instants (ADR-137, ADR-139). Use one."
                )
```

and widen the shorthand discriminator — this is the one-line change that makes the natural
manifest valid:

```python
        # A reset of EITHER spelling flips the shorthand default from
        # `capacity` to 0, so a manifest naming only the allowance and the
        # window is the ADR-137-valid one rather than failing with a message
        # about a field the author never wrote. That single boolean is the
        # whole discriminator.
        resets = bool(reset_schedule) or reset_after_seconds is not None
```

The two existing ADR-137 checks (`refill_amount == 0 and not resets`, `refill_amount > 0 and
resets`) then cover both spellings with no further edit — verify that by reading them.

- [ ] **Step 4: Wire CloudFormation**

In `src/zae_limiter_provisioner/handler.py`:

```python
_CFN_LIMIT_OPTIONAL_KEYS: dict[str, tuple[str, Any]] = {
    "RefillAmount": ("refill_amount", _coerce_int),
    "RefillPeriod": ("refill_period", _coerce_int),
    # CloudFormation delivers every property as a string (#554); `_coerce_int`
    # parses it back and rejects a bool and a non-integral value (#569).
    "ResetAfterSeconds": ("reset_after_seconds", _coerce_int),
}
```

and add the inverse entry in `src/zae_limiter/limits_cli.py`, so
`zae-limiter limits cfn-template` emits `ResetAfterSeconds` for a manifest carrying
`reset_after_seconds`. The existing inverse-map test is what keeps the two from drifting.

- [ ] **Step 5: Extend `differ.py` if it enumerates limit fields**

```bash
rg -n "refill_period|reset_schedule" src/zae_limiter_provisioner/differ.py
```

If the differ compares `LimitDecl` values structurally (dataclass equality), nothing changes —
`reset_after_seconds` is a field and participates automatically. If it enumerates field names,
add the new one. **Read before editing**; do not assume either.

- [ ] **Step 6: Update the manifest documentation**

In `CLAUDE.md`'s "YAML manifest format" section, add `reset_after_seconds` beside
`schedule` / `reset_schedule` with the one-line rule and the ADR-139 pointer, and add an example:

```yaml
resources:
  claude-sonnet:
    limits:
      session:
        capacity: 10000
        reset_after_seconds: 18000   # 5h, from each entity's own first use
```

- [ ] **Step 7: Run and commit**

```bash
uv run pytest tests/unit/test_manifest.py tests/unit/test_handler.py tests/unit/test_limits_cli.py -q
uv run pytest tests/unit/ -k lambda_builder -q
git add src/zae_limiter_provisioner/ src/zae_limiter/limits_cli.py tests/unit/ CLAUDE.md
git commit -m "$(cat <<'EOF'
✨ feat(provisioner): declare a duration window in the limits manifest

`reset_after_seconds` on any `limits.<name>` mapping, at every level, round-
tripped through the CloudFormation `Custom::ZaeLimiterLimits` resource as a
`ResetAfterSeconds` property. Spelled `..._seconds` and typed int because a
YAML scalar and a CloudFormation string carry no type, where the Python API's
`timedelta` does.

A reset of EITHER spelling now flips the `refill_amount` shorthand default
from `capacity` to 0, so a manifest naming only the allowance and the window
is the ADR-137-valid one rather than failing with a message about a field the
author never wrote. That is a one-line widening of the existing discriminator.

Both spellings on one limit is rejected at parse time, so `limits plan`
surfaces it before anything is written. `_coerce_int` carries #554's
every-property-is-a-string rule and #569's whole-number rule, bool included.

Refs #222
EOF
)"
```

---

### Task 14: Integration and E2E

Unit tests with a frozen clock cover the arithmetic; these cover what only a real backend shows —
conditional-write races, cross-shard convergence, real elapsed time, and the aggregator.

**Files:**
- Modify: `tests/integration/test_bucket_sharding.py`
- Create: `tests/e2e/test_session_quotas.py`
- Modify: `tests/benchmark/test_capacity.py`

- [ ] **Step 1: Assert the fast path is byte-identical (the load-bearing claim)**

In `tests/integration/test_bucket_sharding.py`, using the `capacity_counter` fixture:

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_duration_quota_costs_the_same_on_the_fast_path(test_repo, capacity_counter):
    """The claim the whole design rests on: 0 RCU + 1 WCU inside a window, and
    ZERO config reads. If this regresses, `reset_after` has quietly become a
    per-acquire cost rather than a per-rollover one.
    """
    limiter = RateLimiter(repository=test_repo)
    await test_repo.set_limits(
        "user-1",
        [Limit.quota("session", 10_000, reset_after=timedelta(hours=5))],
        resource="gpt-4",
    )
    async with limiter.acquire("user-1", "gpt-4", consume={"session": 1}):
        pass  # warms the config cache and creates the bucket

    capacity_counter.reset()
    for _ in range(10):
        async with limiter.acquire("user-1", "gpt-4", consume={"session": 1}):
            pass
    assert capacity_counter.read_units == 0
    assert capacity_counter.write_units == 10
    assert capacity_counter.get_item_calls == 0
    assert capacity_counter.batch_get_item_calls == 0
```

Read `tests/fixtures/capacity.py` for the counter's real attribute names before writing this;
the names above are illustrative and the fixture is authoritative.

- [ ] **Step 2: Assert the rollover's WCU cost**

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_rollover_costs_one_write_per_sibling(test_repo, capacity_counter):
    """(S-1) WCU per rollover per entity, zero at S=1. The number the plan's
    cost section quotes, measured rather than asserted.
    """
    ...  # 4 shards, all drawn, window of 2 seconds
    await asyncio.sleep(2.1)
    capacity_counter.reset()
    async with limiter.acquire("user-1", "gpt-4", consume={"session": 1}):
        pass
    # 1 failed conditional (0 WCU) + slow-path reads + 1 transaction write
    # + 3 fan-out writes.
    assert capacity_counter.write_units >= 4
    fanout = capacity_counter.write_units - _baseline_slow_path_writes
    assert fanout == 3
```

- [ ] **Step 3: Assert the doubling conserves what can be spent**

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_duration_quota_doubling_conserves_the_spendable_total(test_repo):
    """PR #594's invariant, restated for this limit shape. Measured as #594
    measures it — sum(max(0, tk)) across shards, DEBT EXCLUDED, because debt
    is dead weight for a quota: its rate is zero and its reset SETS the
    balance rather than adding to it, wiping the debt.
    """
    before = await _spendable_total(test_repo, "user-1", "gpt-4", "session")
    await _force_wcu_doubling(test_repo, "user-1", "gpt-4")
    await _draw_every_shard(limiter, "user-1", "gpt-4")
    after = await _spendable_total(test_repo, "user-1", "gpt-4", "session")
    assert after == before
```

- [ ] **Step 4: Write the E2E cases that need real elapsed time**

Create `tests/e2e/test_session_quotas.py`. These use `reset_after=timedelta(seconds=2)` and real
`asyncio.sleep`, and are marked `@pytest.mark.slow` where the wait exceeds 30 s.

```python
@pytest.mark.e2e
@pytest.mark.asyncio
async def test_two_entities_get_independent_windows(e2e_limiter):
    """The feature, in one test. Entity A first calls at T, entity B at T+1s;
    A's window ends at T+2s and B's at T+3s. A calendar quota would reset both
    at the same instant, which is the difference ADR-138 and ADR-139 draw.
    """


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_an_idle_entity_restarts_its_window(e2e_limiter):
    """Spend the allowance, wait past the window, call again -> full
    allowance and a window anchored at THAT call, not at a grid tile."""


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_a_sharded_entity_has_one_window(e2e_limiter):
    """Four shards, real rollover, one `resets_at_ms` from
    `check_availability`. This is what the fan-out buys and the reason it is
    in the baseline rather than an optimisation."""


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_it_works_without_the_aggregator(e2e_minimal_stack):
    """`--no-aggregator`. The client owns the fan-out and the roll, exactly
    as it owns shard-count propagation for the same reason, so this must be
    byte-identical in outcome to the aggregator stack."""


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_the_aggregator_rolls_a_window_it_sees(e2e_aggregator_stack):
    """With the aggregator, a shard it processes is rolled from the stream
    rather than waiting for a client to draw it — an optimisation on top of
    convergence, so assert it HAPPENS rather than that it is required."""


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_a_manifest_applies_a_duration_window(e2e_limiter, tmp_path):
    """`limits apply` with `reset_after_seconds`, then acquire against it.
    Covers the provisioner's bucket_sync mirror against a live table."""
```

Use `shared_minimal_stack` / `shared_aggregator_stack` per `.claude/rules/testing.md` — do not
create per-test stacks.

- [ ] **Step 5: Run the suites**

```bash
zae-limiter local up
export AWS_ENDPOINT_URL=http://localhost:4566 AWS_ACCESS_KEY_ID=test \
       AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1
uv run pytest tests/integration/ -q
uv run pytest tests/e2e/test_session_quotas.py -q
```

- [ ] **Step 6: Commit**

```bash
git add tests/integration/ tests/e2e/ tests/benchmark/
git commit -m "$(cat <<'EOF'
✅ test(test): cover duration windows across shards and real elapsed time

The load-bearing assertion is the capacity one: a duration quota costs
0 RCU + 1 WCU on the fast path with zero config reads, so `reset_after` is a
per-rollover cost and not a per-acquire one. If that regresses, the design's
central claim has quietly stopped being true.

Cross-shard convergence, the (S-1) fan-out cost, and PR #594's
spendable-total invariant restated for this limit shape — measured as #594
measures it, sum(max(0, tk)) with debt excluded, because a quota's debt is
never repaid and its reset sets rather than adds.

E2E uses two-second windows and real waiting, with and without the
aggregator, because the client owns the fan-out and the roll under
--no-aggregator exactly as it owns shard-count propagation.

Refs #222
EOF
)"
```

---

### Task 15: Documentation and the parity checklist

**Files:**
- Modify: `CLAUDE.md`, `docs/guide/basic-usage.md`, `docs/api/`, `docs/cli.md`,
  `docs/performance.md`
- Create: `docs/guide/session-quotas.md`
- Modify: `mkdocs.yml`

Run the `docs-updater` agent per `.claude/rules/docs-parity.md` and review its changes, then fill
the gaps it cannot know about.

- [ ] **Step 1: Work the parity table explicitly**

| Layer | What must change |
|---|---|
| Python API | `Limit.quota(..., reset_after=...)` docstring (done in Task 2) |
| CLI | `_format_limit` renders it (Task 12). **`-l` cannot express it** — say so, beside the existing note that `-l` cannot express a schedule and that a set is a full replace |
| API docs | `docs/api/` — `Limit.reset_after`, `LimitStatus.resets_at_ms` |
| CLI docs | `docs/cli.md` — the rendering, and the `-l` limitation |
| User guide | new `docs/guide/session-quotas.md` |
| CLAUDE.md | Scheduled Limits section, the writer table, the config/bucket attribute tables, the TTL table, the access-pattern notes |

- [ ] **Step 2: Write the user-guide page**

`docs/guide/session-quotas.md` must answer, in this order:

1. **The one-liner.** `Limit.quota("session", 10_000, reset_after=timedelta(hours=5))`.
2. **Which one do I want?** A table: a billing period is `cron` (ADR-138); a session cap is
   `reset_after` (ADR-139); you cannot have both on one limit and why (ADR-137).
3. **Idle-restarting.** Go quiet past the window's end and the next call opens a fresh one. An
   entity *can* reset its own window by going idle, and that is intended.
4. **What anchors it.** Only admitted use. A caller hammering an exhausted quota does not keep
   restarting its own five hours.
5. **Cascade.** Parent and child anchor independently.
6. **What it costs.** Nothing per acquire. (S − 1) writes per rollover for a sharded entity, zero
   for an unsharded one.
7. **What a 429 tells the caller.** `kind: "quota"`, `capacity`, `resets_at_ms` — an absolute
   instant, so no cron parser is needed to schedule a retry.
8. **The limitation.** A single request above `capacity // shard_count` is unadmittable on every
   shard while the entity is under its configured limit (#475, inherited).

Every code block must be doctest-covered — add it to `tests/doctest/` per the existing pattern.

- [ ] **Step 3: Update `CLAUDE.md`'s reference tables**

| Table | Row to add |
|---|---|
| DynamoDB writer table | `Window rollover fan-out` — `SET b_{n}_ws = :new, vu = :zero` / `attribute_exists(PK) AND (attribute_not_exists(b_{n}_ws) OR b_{n}_ws < :new)` / touches `rf`? **No** |
| Limit attribute format | `rwin`… no — **`rsa`** (number, ADR-139): the duration window's length in seconds. Written only when the limit has one. |
| Bucket attributes | `b_{name}_ws` (number): window start, epoch ms. Entity-wide, replicated to every shard, monotonic |
| TTL recovery horizon | a third row: duration quota ⇒ `reset_after`, exactly |
| Access patterns | `Read one shard's window starts` ⇒ projected `GetItem` on `PK={ns}/BUCKET#{id}#{res}#0` |
| Pricing reference | rollover = (S − 1) WCU, once per window; shard-create sibling read = +0.5 RCU once per shard |

- [ ] **Step 4: Update the Scheduled Limits section**

The existing paragraph ends *"windows are fixed calendar windows, never anchored to an entity's
own first use (ADR-138)"* — which becomes false. Replace with a statement of the two spellings
and their mutual exclusion, pointing at ADR-138 and ADR-139.

Also correct `Limit`'s class docstring, which currently ends *"The reset window is a **fixed
calendar window** — every entity on one schedule resets at the same wall-clock instant
(ADR-138)."* That sentence stays true **of `reset_schedule`** and must be scoped to it.

- [ ] **Step 5: Correct the stale docstring found while reading**

`Limit.from_bucket_state`'s docstring says `state.reset_sched` is *"**not yet** [populated] by
`_deserialize_composite_bucket`"*. That is **stale on `main`** — that deserialiser's own
docstring says "Both schedule tuples are decoded off the item (#222 §4.1)". Fix it in this task,
in its own `📝 docs(models):` commit, not folded into the feature commit.

- [ ] **Step 6: Run the docs build and the doctests**

```bash
uv run pytest tests/doctest/ -q
uv run mkdocs build --strict
```

- [ ] **Step 7: Commit**

```bash
git add docs/ mkdocs.yml CLAUDE.md tests/doctest/
git commit -m "$(cat <<'EOF'
📝 docs(guide): document session quotas

A new guide page, the reference tables in CLAUDE.md (writer table, attribute
formats, TTL horizon, access patterns, pricing), and the CLI note that `-l`
cannot express a window any more than it can express a schedule.

Corrects two sentences that this work makes false: CLAUDE.md's "windows are
fixed calendar windows, never anchored to an entity's own first use", and
`Limit`'s class docstring making the same claim unscoped — true of
`reset_schedule`, not of every reset.

Refs #222
EOF
)"
```

---

## Risks

Named in the order most likely to sink it.

**1. The milestone — resolved, and recorded because the reasoning still governs the sequencing.**
The first draft of this plan flagged v0.14.0 as its largest risk: that milestone was one bug from
shipping, and this is 15 tasks across 12 source files and both Lambda packages whose central
mechanism — a cross-shard fan-out of a monotonic scalar — does not exist in the codebase today.
The analysis's own estimate was *"9–11 PRs, ~3 weeks"*.

**The owner accepted the recommendation.** v0.14.0 ships now with scheduled limits complete, and
this feature is **v0.15.0** (milestone 25), tracked by epic **#597**. The plan document itself
still lands in v0.14.0, via PR #596 — the document and the feature are deliberately in different
releases.

What survives is the sequencing consequence: Tasks 1–5 are **inert** (a field nobody reads, an
attribute nobody writes), so they can land ahead of the rest without changing any behaviour. If
v0.15.0 needs to be split, that is where the seam is.

**2. A lost fan-out write costs one extra window's share.** Recorded in ADR-139's Consequences
and above. A sibling that misses the rollover keeps a stale `ws`, later anchors a window of its
own, fans *that* out, and shards which already rolled see `ws > rf` a second time. Bounded (one
extra share per affected shard per lost write) and self-converging, but real. The mitigation is
the shortfall log in Task 7 Step 4, which makes it observable rather than silent. **If the owner
wants it eliminated rather than bounded, the only clean answer is for the roll to be applied from
shard 0's `ws` only — which costs a read per rollover per shard and is a materially different
design.**

**3. `resets_at_ms` plumbing touches four status sites and one is easy to miss.** `_limit_shape`
changing from taking a `Limit` to taking a `LimitStatus` is a quiet behaviour change for
**calendar** quotas: if the fallback to `next_reset_edge` is dropped, every calendar quota's
`resets_at_ms` silently becomes `null` in 429 bodies and #545's whole surface regresses. Task 12
Step 1 has a regression guard specifically for this, and it must not be deleted as redundant.

**4. `processor._is_quota_limit` reads the image, not the config.** Task 2 widens
`Limit.is_quota`, which is a different function. If Task 10 Step 4 is skipped, a duration quota is
minted a fresh share at shard-create time and #587 is reintroduced for exactly the limit shape
this feature adds — silently, visible only in the entity-wide sum, which is what made #587 hard
to find in the first place.

**5. Two duration limits on one item with different lengths.** They roll at different instants,
which is why Task 7 issues one fan-out write per (sibling, limit) rather than one per sibling. It
is correct but it multiplies the rollover cost by the number of rolling limits. The motivating
product has one. If a user puts three on an item, a maximally-sharded rollover is 93 writes.

**6. Clock skew across clients.** Two clients crossing a boundary milliseconds apart produce two
`ws` values and the later wins, which is fine. A client with a badly wrong clock that runs *fast*
wins every race and pulls every window forward — bounded by `ws < :new` being monotonic, so it
can shorten a window but never lengthen it past `reset_after`. Worth a sentence in the guide; not
worth machinery.

**7. `timedelta` at the serialization boundaries.** Three of them (`to_dict`, YAML, CFN) and each
converts. The mitigation is that only one direction is lossy-capable and `__post_init__` rejects
it up front, but a fourth boundary added later by someone who does not know the rule will take an
`int` and be wrong by a factor of 1000. The stated rule — *the unit lives in the name wherever
the type cannot carry it* — belongs in `CLAUDE.md`, not only in this plan.

**8. The E2E tests use real elapsed time.** Two-second windows keep them fast but make them
timing-sensitive on a loaded CI runner. Mark generously and prefer asserting *ordering* (window B
ends after window A) over absolute instants.

## Effort

| Task | Rough size | Notes |
|---|---|---|
| 1 ADR-138 edit + ADR-139 | S | Docs only. Can land immediately. |
| 2 `Limit.reset_after` | M | Six structural sites; mechanical but each load-bearing. |
| 3 `BucketState` + schema constants | S | |
| 4 Config persistence | S | |
| 5 Bucket item stamp/read | M | |
| 6 Slow-path roll | **L** | The feature. Two new helpers, a signature change with two call sites, the lease's between-readings re-expression, and five behavioural tests. |
| 7 Rollover fan-out | **L** | The only new distributed-systems surface. |
| 8 Shard creation | M | Depends on PR #594's landed machinery. |
| 9 Param sync + provisioner mirror | M | Two implementations that cannot share code. |
| 10 Aggregator | M | |
| 11 TTL horizon | S | One branch, but verify the arithmetic (Step 4). |
| 12 `resets_at_ms` + CLI | **L** | Four status sites plus a regression risk for calendar quotas. |
| 13 Manifest + CFN | M | |
| 14 Integration + E2E | **L** | |
| 15 Docs | M | |

**Totals: 15 tasks — 4 S, 6 M, 5 L. Estimate 11–13 PRs, ~3 weeks of focused work**, against the
analysis's 9–11 and ~3 weeks. The extra two PRs are the cascade-independent shard-create read
(absent from the analysis entirely) and the `resets_at_ms` plumbing being larger than §5.4
allowed for, since it turns out to touch a calendar-quota regression path.

**Sequencing.** 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 are strictly ordered. From there, 9, 10, 11, 12 and
13 are independent of each other and can run in parallel; 14 needs all of them; 15 needs 14.
Tasks 1–5 are inert — they add a field nobody reads and an attribute nobody writes — so they can
land early without changing any behaviour, which is where the seam is if v0.15.0 has to be split.

## Self-review

**Spec coverage.** Every section of the analysis has a task: §2.2 storage → 3/4/5; §2.3 the
`ws > rf` rule → 6; §3.2 shard-create inheritance → 8 (with the analysis's mechanism corrected);
§3.3 the fan-out → 7; §3.4 the doubling → the Dependency section (with the analysis's mechanism
**replaced**, per PR #594); §4 aggregator → 10; §5.1 encoding → 3; §5.2 ADR-137 predicates → 2;
§5.3 TTL → 11; §5.4 `retry_after`/`resets_at_ms` → 12; §5.5 cold start → 11 + ADR-139; §6 file
list → the File Structure table. The analysis's §7 corrections are folded into Task 15 Step 5 and
the Dependency section.

**Owner decisions not in the analysis**, all covered: `reset_after` as the name (2), idle-
restarting (6), what anchors a window (6 Steps 7–9), cascade independence (8), ADR-138 edited
rather than superseded (1), and the v0.15.0 milestone with the plan document staying in v0.14.0
(Global Constraints, Risks §1).

**Placeholders.** None. Every step names exact files and shows the code. Four steps deliberately
say "read this before editing" rather than showing code — Task 8 Step 5 (`_quota_transfer`, which
lands in PR #594 and cannot be quoted before it merges), Task 13 Step 5 (`differ.py`, whose
comparison strategy must be read not assumed), Task 7 Step 5 (whether `_propagate_shard_count` is
on the protocol), and Task 14 Step 1 (`capacity_counter`'s attribute names). Each says exactly
what to look for and what to do with either answer.

**Type consistency.** `reset_after: timedelta | None` on `Limit`; `reset_after_seconds: int | None`
on `Limit` (property), `BucketState`, `ParsedBucketLimit`, `LimitRefillInfo` and `LimitDecl`;
`window_start_ms: int | None` on `BucketState` and `LeaseEntry`; `resets_at_ms: int | None` on
`LimitStatus`. Storage is `b_{name}_ws` / `b_{name}_rsa` / `l_{name}_rsa` throughout;
manifest `reset_after_seconds`; CFN `ResetAfterSeconds`. `window_starts: dict[str, int]` is the
one map shape, used by `build_composite_normal` and `_propagate_window_start` alike.
