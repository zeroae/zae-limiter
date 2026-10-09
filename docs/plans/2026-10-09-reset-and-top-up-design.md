# Reset and top-up for an (entity, resource): design

**Decision record:** [ADR-149](../adr/149-reset-and-top-up.md) (Proposed)
**Issue:** [#470](https://github.com/zeroae/zae-limiter/issues/470) (reshaped section, 2026-10-04)
**Epic:** [#674](https://github.com/zeroae/zae-limiter/issues/674), row "Upgrade / buy more / reset session, immediately"
**Prior work:** PR [#471](https://github.com/zeroae/zae-limiter/pull/471) (@mrohr, delete-based)

## TLDR

- **Two operations** on one (entity, resource), optionally narrowed to named limits:
  **reset** (balance back to the full share, new period) and **top-up** (add N now).
- **In place, never delete.** Each shard gets one rf-locked, zero-consumption slow-path pass
  that applies a token **delta**; all shards of the pair go in **one transaction**.
- **Quota top-up above the plan** needs a new per-shard attribute, `b_{q}_tu` (allowance
  topped up this period), which raises that shard's ceiling until the next reset or roll.
  That one piece needs the ADR-141 version gate at **0.17.0**. Reset and dripping top-up need
  no gate.
- **Cost:** about **$1.6/M** operations at one shard, **$45/M** at 32 shards, plus one
  slow pass per shard afterwards. The fast path is unchanged.
- `design-validator` was run on this design: **sound with changes**. All its changes are in
  here (see [Validation](#validation)).

## 1. What the code does today (verified)

| Fact | Where |
|------|-------|
| `set_limits()` rewrites `cp/ra/rp` on every shard and stamps `vu = 0`, but **never adds tokens** | `Repository._sync_bucket_params` |
| Every refill clamps the balance to the ceiling with `min()`, on every path | `bucket.refill_bucket` |
| A quota's ceiling is `C // gc`; a dripping limit's is `C // shard_count` | `BucketState.ceiling_milli` |
| A reset or roll sets `tk = C_eff // shard_count` and stamps `gc` | `BucketState.reset_target_milli`, `RateLimiter._apply_reset_edge` |
| A calendar quota's period is current iff no reset edge falls after `rf` | `models.quota_period_is_current` |
| `rsa` with no `ws` is a legal state; the next pass opens a window | `RateLimiter._open_window_if_elapsed`, `processor._window_in_force` |
| A credit above the ceiling is clamped by stamping `vu = 0` | `Lease._clamps_for_credits_above_ceiling`, `Repository.build_vu_reset` (#679) |
| Resource-scoped shard discovery already exists | `Repository._discover_entity_bucket_pks(entity_id, resource)`, `_entity_bucket_items` |
| Audit actions today: `entity_created`, `entity_deleted`, `limits_set`, `limits_deleted` | `models.AuditAction` |
| Version gate helper, shared by `reset_after` and cascade policy | `Repository._require_readers(minimum, refusal)` |

So after a plan upgrade a **quota** keeps its old remaining balance until the next reset (it never
drips, ADR-137), and a dripping limit climbs to the new ceiling at the new rate. Neither is
"immediately". A purchase beyond the plan has no representation at all.

## 2. Semantics

"Share" below is the shard's effective share at `now`: `C_eff // S` for a reset, where `C_eff`
is the schedule-effective capacity (#222) and `S` the planned shard count (§4).

### 2.1 Reset

| Limit shape | Per shard |
|-------------|-----------|
| Dripping | `tk` → ceiling (`C_eff // S`). Debt is forgiven. |
| Calendar quota (`reset_schedule`) | `tk` → share, `gc = S`, `tu` removed. `rf` moves to `now`, so the period counts as current. The next calendar edge still fires as usual. |
| Session quota (`reset_after`) | `tk` → share, `tu` removed, and the window is **marked ended and applied**: `ws = wa = now − rsa·1000`, `wtc` removed. The next admitted request opens a fresh window (idle-restart, ADR-139). |
| `wcu` | Never touched. Not a nameable limit. |

Why "ended and applied" and not `REMOVE ws`: a rollover fan-out already in flight is guarded by
`attribute_not_exists(ws) OR ws <= :open_floor` (ADR-140). With `ws` removed, a late fan-out from
a window opened **before** the reset would land, the shard would read `ws > wa`, and roll — one
extra share, once (validator finding 3). With `ws = now − rsa·1000`, that fan-out's floor
(`its ws − rsa·1000`, an earlier instant) is below the stored `ws`, so it no-ops. A fan-out from a
window opened **after** the reset passes, as it should.

### 2.2 Top-up (add N)

| Limit shape | Effect |
|-------------|--------|
| Dripping | Adds up to N, **bounded by the ceiling** (a refund of consumption). The amount actually granted is returned. No new attribute. |
| Calendar or session quota | Adds **exactly N** for the current period, spread over the shards (§4.3), recorded in `b_{q}_tu`. Each shard's ceiling becomes `C // gc + tu`. Cleared by the next reset or roll. |

- **Upgrade:** `set_limits(new plan)` then `top_up(N = new C − old C)`. The result is within the
  new ceiling, so no `tu` is needed once the new `cp` is on the item. Open decision D3 is whether
  to wrap these two calls into one.
- **Purchase:** `top_up(N)` alone. The allowance is above the plan for this period only.
- **A pending reset is applied first, in the same write.** A shard whose calendar edge has passed
  gets `tk = share + portion` and `gc = S`. Older `tu` is wiped, because it belonged to the
  previous period. This is stated in the API docstring.
- **Session quota with no live window:** see D4.

### 2.3 What neither operation touches

- **`tc`.** The aggregator derives usage from it (#179). A top-up is not negative consumption.
- **`disabled`.** A disabled bucket stays disabled.
- **`shard_count`.** It is only ever raised (§4.1), never collapsed.
- **Config.**
- **The parent's buckets.** A cascading child's reset leaves its parent alone, the same rule as
  #471 and ADR-125. If the parent is also exhausted, the operator calls the operation on the
  parent too. In #674 the `budget` resource does not cascade (ADR-146), so this is rare.
- **Other resources.**

## 3. Why in place, and what of #471 survives

| #471 piece | Fate | Why |
|------------|------|-----|
| `DeleteItem` per shard | **Dropped** | Deleting loses `tc`, `disabled`, `shard_count`, `gc` and `tu`. An in-flight `adjust()` `ADD` recreates a skeleton with no `cp`/`ra`/`rp`. |
| Resource-scoped GSI3 discovery | **Kept**: `_discover_entity_bucket_pks(…, resource)` is already on main | Used as is. |
| `Repository` placement, `principal`, `validate_resource`, generated sync twin | **Kept** | Same admin-action shape as ADR-125. |
| `AuditAction.BUCKET_RESET` | **Kept**, plus `BUCKET_TOPPED_UP` | Details change: per-limit amounts and shard count, not `buckets_deleted`. |
| Return value `int` (items deleted) | **Changed** | Returns a small result: shards written plus per-limit granted amounts (dripping top-ups can be truncated). |
| CLI `entity reset-bucket` | **Changed** to `entity reset` / `entity top-up` (D2) | Matches the reshaped issue. |
| `RepositoryProtocol` member | **Kept**, as a required member (as in ADR-146) | |
| Tests "no-op on missing", "all shards", "parent untouched", "audit", "invalid name" | **Kept**, re-asserted against in-place state | |
| Test "items deleted" / "shard_count collapsed to 1" | **Dropped** | That is the behaviour being rejected. |
| Docs (`docs/operations/rate-limits.md` runbook rewrite, auditing, CLI) | **Kept in spirit**, rewritten | |

The rework happens on #471's branch, as the epic plans.

## 4. Mechanism

### 4.1 Read and plan

1. Resolve the limits for (entity, resource) from config **uncached**, with the same read
   `resolve_access` uses. Every named limit must be one of them. `wcu` and unknown names raise
   `ValidationError`.
2. Discover the shards (GSI3, KEYS_ONLY, resource-scoped). Read them with a **strongly
   consistent** `BatchGetItem`.
3. Plan at `S = max(stored shard_count)`. A shard that lags is raised to `S` in its own write.
   Its legacy grant size is frozen first, as R5 does (`gc = if_not_exists(gc, inferred)`, with
   R7 inference). Without the freeze, the coverage weights in §4.3 are wrong (validator finding 6).
4. For each shard, run the slow path's **materialisation**: refill every dripping limit, apply
   any pending calendar edge or window roll. This is the same code `_do_acquire` uses. Then apply
   the operation to the named limits.

Step 4 is the reason this is a "zero-consumption slow pass" and not a bare `SET tk`. `rf` is
one attribute per item. Moving it without materialising would:

- drop the un-credited refill of every other limit, and
- skip a pending reset edge of an un-named quota, because `rf` past the edge reads as
  "period current" — one period of under-admission.

Not moving `rf` would let a stale aggregator refill, pinned on the old `rf`, land on top.

### 4.2 Write

Per shard, one `Update` with the same shape as `build_composite_normal`, at consumed = 0:

- `ADD b_{n}_tk :delta` for every limit, where delta = target − read balance. **Never SET.**
  A fast-path debit that lands between the read and the write is kept, and counts against the
  new balance. A SET would erase it.
- `SET rf = max(now, stored rf, applied ws)` (ADR-140 rule), plus `gc`, `wa`, `tu` for the
  limits this pass resets, rolls or tops up. `REMOVE tu` / `wtc` where §2 says.
- `SET vu = 0`. This forces one materialising pass per shard on the next acquire. That pass
  clamps any credit that slipped in between the read and the write, and re-stamps the correct
  `vu` (validator finding 4; the same mechanism as #679).
- **Condition:** `rf = :read_rf AND shard_count = :read_sc`, plus the existing I4 pin when `gc`
  is written.
- **S = 1:** a single `UpdateItem`. `transact_write` already uses the single-item API, so this
  costs 1 WCU.
- **S > 1:** one `TransactWriteItems`, at most 32 items (well under the 100-item limit).
  **Not independent writes:** a partial quota reset leaves an un-reset shard holding an old
  grant whose coverage overlaps the reset shards' `gc = S` (I2 broken), and that over-admits by
  up to `C // g − C // S`.
- **On conflict** (the condition fails, or `TransactionConflict`): re-read and re-plan. Retry up
  to 3 times with full jitter, then raise. Anything that moves `rf` conflicts: a slow path, the
  aggregator, a doubling. The fast path does not move `rf`, so a hot bucket served by the fast
  path does not starve the retries.
- **Must verify during implementation:** how the speculative fast path handles a
  `TransactionConflictException` against an item in this transaction. ADR-145 donor moves have
  the same exposure today. A test is required (§9).

### 4.3 Who funds which shard (quota top-up, ADR-145)

Distribute N over the **existing current-period** shards, weighted by coverage `1/gc_i`:

- `portion_i = floor(N · (1/gc_i) / Σ_j (1/gc_j))`.
- The remainder goes to the lowest shard id.
- **The total is exactly N.**

Each shard gets `tk += portion_i` and `tu += portion_i`.

- **Missing slots get no portion.** They are later granted a fresh `C // S` or moved by
  `plan_quota_grant`, exactly as today.
- **Donation is unchanged:** `min(C // S, donor tk)`. The recipient's ceiling is `C // S`, so a
  donation never exceeds what the recipient can hold. Top-up surplus stays with the donor, whose
  ceiling still includes its `tu` (validator finding 2, resolved by design).

**Conservation.** I1 gains one source: an operator top-up, exactly N per call. I8 becomes
**admitted + held + still-grantable = C + Σ top-ups this period**. I7 becomes
`ceiling = C // gc + tu`. I2 and I4 are unchanged, and a reset re-establishes I2 with `gc = S`
on every shard.

**Option D1** (below) is the simpler alternative: all of N on one shard.

### 4.4 `tu` across every writer (the version-gated part)

| Writer | Change |
|--------|--------|
| Client reset edge / window roll / opener | `REMOVE b_{q}_tu` |
| Aggregator refill reset / roll | `REMOVE b_{q}_tu`; clamp uses `C // gc + tu` |
| Aggregator Path 2 clone | Does not copy `tu` (the clone is funded by a move, at most `C // S`) |
| `BucketState.ceiling_milli` (vendored stub) | `+ tu` for a quota |
| `check_availability` `min(total, capacity)` clamp (`limiter.py`) | Capacity + Σ `tu`, or the purchase is hidden from the display |
| `Limit.per_shard` / 429 statuses | Report `C // gc + tu` on the drawn shard |
| `#679` credit clamp | Uses `ceiling_milli`, so it inherits the change |
| Fast path | **Unchanged.** Pure `ADD`; never reads `tu` |

A pre-0.17 client or aggregator clamps a quota to `C // gc` on its next pass. That destroys
purchased allowance: under-admission of paid credit, never over-admission. So a top-up that
writes `tu` must pass `_require_readers("0.17.0", …)` and ratchet `client_min_version`.

- `lambda_version` is stamped only when **both** Lambdas are proven current
  (`stack_lambdas_current`), so the gate covers the aggregator too (validator finding 1).
- Reset and dripping top-up write only attributes that 0.15+ writers already understand, so they
  need **no gate**.
- A stale `tu` left behind by an old writer's reset is benign: nothing fills tokens above share
  except a top-up, and the next 0.17 reset clears it.

### 4.5 Missing bucket

- **Reset:** no-op, returns 0 shards. There is nothing to restore, and the first acquire creates
  the bucket at full share.
- **Top-up:** D5.

## 5. Concurrency

| Concurrent writer | Outcome |
|-------------------|---------|
| Fast-path `ADD` (no `rf` change) | Commutes with our `ADD`; kept and counted against the new balance |
| Client slow path / aggregator refill | Both move `rf` → one of the two writes fails its lock; we retry, the aggregator skips |
| Aggregator proactive doubling / propagation | Changes `shard_count` → our pin fails → re-plan at the new `S` |
| Quota shard create/seed (ADR-145 move) | Donor debit pinned on `gc` and period: if we changed them it fails and re-plans; else it moves post-reset tokens (a move, not a mint). A create landing after our commit sees `gc = S` siblings and plans correctly (validator finding 3, interleaving 3) |
| Window rollover fan-out | §2.1 ended-and-applied `ws` blocks a pre-reset fan-out |
| `adjust()` / rollback credit | `ADD`; kept; a credit above the ceiling is clamped by the forced pass (`vu = 0`) |
| Param sync (`set_limits`) | Does not move `rf`; it SETs `cp`/`vu = 0`. Ordering either way ends in one materialising pass at the newest params |
| Disable stamp | Independent attribute; untouched |

**Rejection cache (ADR-147).** A reset or top-up is a credit.

- **This process:** the methods are `@clears_rejection_cache`, so the effect is visible at once.
- **Other processes:** they may still reject from a cached short state for up to
  `rejection_cache_ttl` (default 1 s). That is the documented trade-off: under-admission only,
  because the cache never admits.

## 6. Surfaces

- **API** (on `Repository` and `RepositoryProtocol`, generated sync twin; shape per D2):
  `reset_bucket(entity_id, resource, *, limits=None, principal=None)` and
  `top_up(entity_id, resource, amounts, *, principal=None)`, where `amounts` is
  `{limit: N}` in tokens, mirroring `consume=`. Both return a result with shards written and
  per-limit granted amounts.
- **CLI:** `zae-limiter entity reset ID -r R [--limit L ...]` and
  `zae-limiter entity top-up ID -r R --add L:N [--add L:N ...]`. Both are write commands, so
  they go through `cli._connect()`.
- **Manifest / provisioner / CFN:** **none.** These are one-off imperative events, not desired
  state. A manifest entry would re-apply on every `limits apply`.
- **Audit:** `AuditAction.BUCKET_RESET` and `BUCKET_TOPPED_UP`, on `entity_id`, with
  `resource`, the limits, the per-limit amounts, `shards`, and `principal`. Written after the
  transaction commits, and only when at least one shard was written.
- **Docs:** user guide section in the #678 guide (plans, purchases, sessions);
  `docs/operations/rate-limits.md` runbook; CLI and API references; CLAUDE.md writer table.

## 7. Cost

Assumptions:

- Bucket items are ≤ 1 KB.
- Strongly consistent `BatchGetItem` costs 1 RCU per item.
- KEYS_ONLY query: 0.5 RCU.
- Uncached config read: 1.5 RCU.
- Transactional writes cost 2 WRU per item.
- Prices are us-east-1 on-demand: $0.125/M RRU, $0.625/M WRU.

| Shards | Round trips | RCU | WCU (incl. 1 audit) | $/M operations | Afterwards (one slow pass per shard) |
|-------:|------------:|----:|-----:|----:|----:|
| 1 | 4 (config, query, get, update) + audit | 3 | 2 | **$1.63** | +1 RCU +1 WCU ≈ +$0.75 |
| 4 | 4 + audit | 6 | 9 | **$6.38** | ≈ +$3.00 |
| 32 | 4 + audit | 34 | 65 | **$44.88** | ≈ +$24.00 |

- **Gated top-up:** +1 RCU on the first call per repository (the version read, strongly
  consistent; cached after that).
- **Conflict retry:** repeats the read and write.
- **#471 for comparison:** 0.5 RCU + S WCU of deletes, but every shard then pays a full create
  on its next acquire, and it loses state (§3).
- **Hot path:** unchanged. The fast path stays at 0 RCU + 1 WCU and reads no new attribute.

Even at 32 shards, a million operations cost about $70 in total. At #674's scale (one operation
per plan change or purchase) that is negligible.

## 8. Open decisions

| # | Decision | Options | Recommendation | Why |
|---|----------|---------|----------------|-----|
| D1 | How a quota top-up is spread over shards | (a) weighted by coverage `1/gc`; (b) all on one shard (shard 0 or most headroom); (c) equal split | **(a)** | Puts tokens where draws land; (b) conserves trivially but a draw on any other shard rejects while the entity holds the purchase. |
| D2 | API shape | (a) two methods `reset_bucket` + `top_up(amounts={…})`; (b) one `reset_bucket(…, add=N)` as in the reshaped issue; (c) one `adjust_bucket(op=…)` | **(a)** | Different semantics, gates and return values; `amounts` dict matches `consume=` and allows several limits with different N. CLI `--add L:N` likewise. |
| D3 | Plan upgrade as one call | (a) app calls `set_limits` then `top_up(Δ)`; (b) `set_limits(…, top_up_difference=True)`; (c) `upgrade_plan()` helper | **(a)** for v0.17.0 | No atomicity that matters is lost (the gap between the two calls only under-admits), and it keeps `set_limits` free of bucket token writes. Revisit after the #678 guide. |
| D4 | Session top-up when no window is live | (a) open a window at the top-up instant and credit it; (b) refuse with an error; (c) carry the credit to the next window | **(a)** | A credit to an ended window is wiped by the next opener (silently lost); (c) changes opener semantics; (a) reads naturally as "the purchase starts the session". |
| D5 | Top-up when no bucket exists | (a) create shard 0 from resolved config with `tk = share + N`, `tu = N` (`attribute_not_exists(PK)`); (b) refuse; (c) no-op | **(a)** | A purchase must not be lost because the user had not called yet; reuses the slow-path create builder. The TTL horizon is ≥ 7 periods, longer than the period the credit lives in. |
| D6 | Ship quota top-up above the ceiling in v0.17.0 | (a) yes, with `tu` and the 0.17 gate; (b) reset + bounded top-up now, `tu` later (validator's suggestion) | **(a)**, (b) as fallback if the milestone slips | #674 needs purchases; (b) ships with no new invariant term if time runs short. |
| D7 | Reset of a quota: what `S` | (a) max stored count, raise lagging shards; (b) collapse to 1 (as #471) | **(a)** | Lowering `shard_count` breaks the "all shards agree, monotonic" rule and the `shard_count < :new` conditions everywhere. |

## 9. Test plan

- **Unit (moto), async + generated sync:**
  - reset dripping / calendar / session, one shard and 4 shards;
  - top-up dripping (bounded) and quota (exactly N, coverage split, remainder);
  - pending edge applied in the same write;
  - un-named limits keep their refill and pending edge;
  - `tc`, `disabled`, parent untouched;
  - missing bucket;
  - `wcu` / unknown limit rejected;
  - audit events;
  - conflict retry;
  - lagging `shard_count` raised with frozen `gc`.
- **ADR-145:** extend `test_quota_conservation_fuzz.py` with reset and top-up steps and the
  `C + Σ top-ups` term. Declare the writer in `test_bucket_writer_registry.py` (writes `tk`,
  `gc`, `tu`). Add the builder to `test_expression_tokens.py`.
- **Session (ADR-140):** a pre-reset fan-out arriving after the reset no-ops; the next acquire
  opens a fresh window; a sharded rollover after reset restores the share once.
- **Rejection cache:** a `TestChangesElsewhere` case — reset from a second `Repository` →
  this process admits after at most the TTL, and never over-admits; an in-process reset admits
  at once.
- **Aggregator:** a refill against a pre-reset image is skipped (`rf` lock); reset and roll
  clear `tu`; Path 2 does not copy it; the clamp includes `tu`.
- **Version gate:** a top-up writing `tu` against `lambda_version` 0.16 is refused with
  `VersionMismatchError`; reset is not gated.
- **Transaction conflict:** the fast path against an item inside the reset transaction (§4.2).
- **Integration (LocalStack):** acquire after reset sees full capacity; acquire after a purchase
  admits beyond the plan exactly N; real transaction semantics.
- **Benchmark:** `TestQuotaGrantCapacity` unchanged (fast path 0 RCU + 1 WCU).
- **CLI:** `entity reset`, `entity top-up` (parsing of `--add L:N`, exit codes).
- **Before implementation:** re-run `design-validator` on the final planner code for §4.3.

## Validation

`design-validator` was run on 2026-10-09 against this design. **Verdict: sound with changes.**

| # | Finding | Resolution here |
|---|---------|-----------------|
| 1 | Pre-0.17 client or aggregator clamps `tu` away | 0.17 gate on `lambda_version`, which covers both Lambdas (§4.4) |
| 2 | Donated top-up surplus clamped at the recipient | Donation stays capped at `C // S`; surplus stays with the donor (§4.3) |
| 3 | `REMOVE wa/ws` on session reset lets a late fan-out re-roll (one share, once) | Ended-and-applied `ws = wa = now − rsa` (§2.1) |
| 4 | A credit between the read and the write leaves `tk` above target | `vu = 0` forces a clamping pass (§4.2) |
| 5 | Top-up with no live window or a pending reset loses the credit | Pending reset applied first (§2.2); D4 |
| 6 | Legacy items without `gc` | R5 freeze / R7 inference before weighting (§4.1) |
| 7 | I8 gains a term | Fuzz test and writer registry updated (§9) |
