# Multi-resource acquire: design

**Decision record:** [ADR-148](../adr/148-multi-resource-acquire.md) (Proposed)
**Issue:** [#675](https://github.com/zeroae/zae-limiter/issues/675) · **Epic:** [#674](https://github.com/zeroae/zae-limiter/issues/674) · **Milestone:** v0.17.0
**Status:** design only — nothing here is implemented. The [Open decisions](#open-decisions)
need the owner's answer before an implementation plan is written.

## TLDR

- **One acquire, several resources, one lease.** The primary resource keeps today's
  arguments; extra resources are named in one more argument (recommended: `also=`).
- **All or none.** Any rejection, disable or outage refunds every debit the acquire wrote.
- **Fast path:** check the rejection cache for every resource first (0 writes if any is
  known short), then all speculative writes at once, one round trip.
- **Slow path:** only the resources the fast path could not settle, planned and committed
  in **one** `TransactWriteItems`. Fast-path wins are kept, refunded if that commit rejects.
- **Cost on success is unchanged** (one WCU per item, 3 WCU for the #674 shape). The win is
  **latency** (1 RT instead of 2), **one reconcile**, and **no hand-written refund**.
- **No new bucket write, no schema change.** Every write is an existing builder.

## 1. What exists today (verified against `main` at `f5d2ee23`)

| Fact | Where |
|------|-------|
| `acquire(entity_id, resource, consume, limits=None, use_stored_limits=False, on_unavailable=None)` — one resource | `limiter.py` `RateLimiter.acquire` |
| Fast path per resource: `_try_speculative_acquire` returns a pre-committed `Lease`, or `None` plus the shard hints the slow path needs, or raises `RateLimitExceeded` / `ResourceDisabled` | `limiter.py` |
| Cascade fast path already writes child + parent concurrently and refunds the one that landed (`_compensate_speculative`) | `limiter.py`, `repository.speculative_consume` |
| Cascade already has a "keep the fast win, slow-path only the rest" fallback: `_try_parent_only_acquire` | `limiter.py` |
| Slow path: `_slow_acquire` → `_do_acquire` (plan) → `Lease._commit_initial` (write), re-planned once on `QuotaMoveLostError`, then with `disable_moves=True` | `limiter.py`, `lease.py` |
| `LeaseEntry` already carries `entity_id` and `resource` | `lease.py` |
| `_commit_initial`, `_commit_adjustments` and `_rollback` already group entries by `(entity_id, resource, shard_id)`: one write per bucket item | `lease.py` |
| `_commit_initial` sends every item in **one** `transact_write`; one item becomes a plain `UpdateItem`/`PutItem` (1 WCU) | `repository.transact_write` |
| Per-index `CancellationReasons` already drive the consumption-only retry and the re-issued create `Put` | `lease._commit_initial` |
| ADR-145 donor debits are appended **after** every bucket item so reason indices line up | `lease._commit_initial` |
| `_commit_adjustments` writes **one item per round trip** (`write_each([item])` in a loop) so the items that landed are known (#682) | `lease.py` |
| `LimitStatus` carries `resource`; `RateLimitExceeded.as_dict()` already emits `"resource"` on every limit entry | `models.py`, `exceptions.py` |
| `Lease.adjust/consume/release(**amounts)` take **limit names as keyword arguments**; `Lease.consumed` is keyed by limit name only | `lease.py` |
| A limit may be named `also` (`NAME_PATTERN = ^[a-zA-Z][a-zA-Z0-9_.\-]*$`) | `models.py` |
| The sync generator supports `asyncio.gather(*[f(x) for x in xs])` and rejects keyword arguments to `gather` (#491, #666) | `scripts/generate_sync.py`, CLAUDE.md |

So the storage, commit and rollback layers are already multi-item and multi-resource. The
work is in the **orchestration** (`acquire`) and the **lease API**.

## 2. API shape

### 2.1 Naming the extra resources

| Option | Example | For | Against |
|--------|---------|-----|---------|
| **A1. `also=` mapping** (issue #675) | `acquire(user, "search", consume={"rpm": 1, "units": est}, also={"budget": {"weekly": c, "session": c}})` | Backward compatible; the primary resource stays the lease's identity (exception message, Locust event name, `on_unavailable` error) | Two shapes for "a resource and its amounts" |
| A2. Nested `consume` | `acquire(user, consume={"search": {...}, "budget": {...}})` | One shape | Overloads `consume`'s type; `resource` becomes optional; every type hint and doc changes |
| A3. New method | `acquire_many(user, {"search": {...}, "budget": {...}})` | No overloading; no "primary" | A second context manager to keep in parity (sync twin, Locust, docs) |

**Recommendation: A1.** It is what #674 and #675 already document, it adds one keyword
and changes nothing for existing callers.

### 2.2 Adjusting each resource on the lease

Today `lease.adjust(**amounts)` keys on limit name. With two resources two problems appear:
the issue's `lease.adjust(units=d, also={...})` **collides with a limit named `also`**, and
two resources can both declare `rpm`, so a name alone is ambiguous (as is `lease.consumed`).

| Option | Example | For | Against |
|--------|---------|-----|---------|
| B1. `also=` on `adjust/consume/release` (issue) | `lease.adjust(units=d, also={"budget": {"weekly": dc}})` | One call | Breaks a caller whose limit is named `also`; `**amounts` and a reserved keyword mix |
| **B2. Per-resource handle** | `lease.resource("budget").adjust(weekly=dc)`; `lease.adjust(units=d)` stays the primary | No collision; `consumed` per handle is unambiguous; existing methods keep their meaning exactly | Two calls to reconcile two resources (still **one** exit commit) |
| B3. Mapping method | `lease.adjust_resources({"search": {...}, "budget": {...}})` | One call, no collision | A third spelling of adjust |

**Recommendation: B2** (`lease.resource(name)`, the shape the owner discussed earlier).
Writes are deferred to the exit commit in every option, so "one reconcile" holds either way:
`adjust()` only changes memory, and `_commit_adjustments` writes one item per bucket on exit.
The handle:

- exposes `adjust`, `consume`, `release`, `consumed`, and the resource name;
- for the primary resource returns a view equal to the lease-level methods;
- raises `ValidationError` for a resource not in the lease (a typo must not be silent);
- on the degraded lease (`on_unavailable=ALLOW`) is a no-op view, never an error;
- applies #455's declared-scope warning per resource.

Lease-level `adjust/consume/release/consumed` are **scoped to the primary resource**
(child and parent entries, as today). This is the "no change in meaning" the ADR requires:
a single-resource lease behaves byte-for-byte as before.

### 2.3 Validation at the boundary (before any I/O)

- `also` keys pass `validate_resource`; the primary may not appear in `also`, and keys are
  unique by construction. (DynamoDB also rejects two operations on one item in a
  transaction, so this is a correctness rule, not style.)
- `1 + len(also) <= MAX_ACQUIRE_RESOURCES` (see decision D6).
- An empty `also` (or `None`) is today's single-resource acquire, same code path.
- `limits=` together with a non-empty `also` — see decision D8.

## 3. Fast path

```
acquire(user, "search", consume, also={"budget": ...})
  1. rejection-cache pre-check, every resource (and its cascading parent)   no I/O
       any resource known short  ->  RateLimitExceeded, 0 writes
  2. per resource, concurrently: _try_speculative_acquire(...)               1 RT
       -> Lease (debited) | None + shard hints (needs slow path) | raises
  3. resolve the outcomes (table below)
```

Step 2 reuses `_try_speculative_acquire` unchanged, so each resource keeps its own shard
retry, `wcu` doubling gate (#480), cascade parallel write and parent refund (#474, ADR-146)
and nested-parent handling. The concurrency is `asyncio.gather(*[_safe(r) for r in parts])`
with the `_safe` wrapper returning the exception (CLAUDE.md #491) — the generator turns it
into `_run_in_executor`, honouring `parallel_mode` in the sync twin.

Step 1 runs **before** step 2 for every resource, not inside each one, because inside
step 2 a cached rejection of `budget` would arrive after `search` was already written.

| Outcomes of step 2 | Action |
|--------------------|--------|
| All leases | Merge into one pre-committed lease. Done. |
| Any `ResourceDisabled` | Refund every lease that landed; raise `ResourceDisabled` (403 outranks 429, as for a cascade parent). |
| Any `RateLimitExceeded`, none disabled | Refund every lease that landed; raise one `RateLimitExceeded` (§6). |
| Any backend error | Refund what landed (best effort, logged); then `on_unavailable` as today: degraded lease under ALLOW, `RateLimiterUnavailable` under BLOCK. |
| Some `None`, no rejection | Keep the leases that landed; send only the `None` resources to the slow path (§4) with their shard hints. |

Refunds use the existing `Lease._rollback` on each landed part, which writes
`build_composite_adjust` via `write_each` and forgets the rejection-cache entry
(`_forget_written_bucket`). Refunds of different parts run concurrently (D7).

**Write-on-enter invariant 1** ("nothing a rejected request asked for is debited") holds at
the end of the acquire, not at every instant: like the cascade fast path today
("Speculative cascade fast rejection … child consumed, failed parent write, compensation",
CLAUDE.md pricing), a debit can be visible to concurrent callers for up to two round trips.
That can only **under**-admit them. The rule text in `.claude/rules/write-on-enter.md` should
name this case when the feature lands.

## 4. Slow path

Only the resources the fast path could not settle (all of them on a cold cache).

1. **Plan** each with `_do_acquire(...)` concurrently (each reads its own config, META,
   buckets, parent, quota siblings), producing uncommitted leases. Each part raises
   `RateLimitExceeded` / `ResourceDisabled` on its own; collect them all (`_safe` pattern).
2. **Rejected?** Refund the fast-path wins, commit any planned quota moves with nothing
   consumed (`_commit_rejected_moves`, per part, best effort — ADR-145 I5 and write-on-enter
   invariant 1's documented exception), raise the combined exception.
3. **Count items**: bucket items + donor debits. Over 100 → re-plan the parts without moves
   (`disable_moves=True`; donors vanish, the slots get 0 this period). See D6.
4. **Commit** one merged `Lease._commit_initial()`: one `TransactWriteItems` (a single item
   still degrades to `UpdateItem`, 1 WCU). The existing per-index cancellation handling,
   consumption-only retry and re-issued create `Put` work unchanged because they already key
   on `(entity, resource, shard)` groups.
5. **Commit rejects** (consumption-only retry fails) → refund the fast-path wins, raise.
   **`QuotaMoveLostError`** → re-plan **all** slow parts once, then once more with
   `disable_moves=True`, exactly like `_slow_acquire` today.
6. Merge the committed slow lease and the fast-path leases into the one lease yielded.

Why keep the fast-path wins (D3)? It mirrors `_try_parent_only_acquire`, and a resource that
needs a refill is the common slow-path cause: with refund-and-redo, one stale `budget`
would turn a 1-item `UpdateItem` into a 3-item transaction plus two refunds.

**Item budget.** Per resource: 1 child item, +1 parent item when it cascades, + one donor
debit per donor shard when a quota shard is created or seeded (ADR-145; at most one per
quota). `carriers` (`wcu`) share the bucket item and add nothing. With the recommended cap of
16 resources, bucket items are at most 32; donors only appear on shard creation or seeding.

## 5. Cascade, disable, sharding, quotas, rejection cache

| Concern | Per resource, as today? | Multi-resource specifics |
|---------|------------------------|--------------------------|
| Limits resolution (4 levels) | Yes | Config cache keys already include the resource |
| `disabled` (ADR-125) | Yes | Any disabled resource fails the whole acquire, refunds the rest |
| Cascade policy (ADR-146) | Yes — `_cascade_cache[(ns, entity, resource)]`, slow-path `resolve_cascade_from_fetched` | #674's shape works out of the box: `search` cascades, `budget` does not |
| Shards (ADR-133/134, #474, #480) | Yes | Each resource draws, doubles and probes its own shards |
| Quotas (ADR-145) | Yes | Donors are siblings of the **same** (entity, resource), so no item appears twice in the merged transaction; I1–I8 hold per resource |
| Session windows (ADR-139/140) | Yes | Fan-out runs after the merged commit, per bucket, as today |
| Rejection cache (ADR-147) | Yes | Pre-check of every resource before any write; still **only rejects** |

Nothing in the merged transaction is new to the condition terms the cache relies on; the
#674 epic's check — `tests/unit/test_rejection_cache.py::TestChangesElsewhere` — must keep
passing, and no new bucket writer is introduced.

## 6. Errors

- **`RateLimitExceeded.statuses`**: every declared limit of every resource the decision
  **evaluated**, each `LimitStatus` carrying its `resource` (already a field, already in
  `as_dict()`). Admitted resources contribute their passed statuses from the `ALL_NEW` image
  (fast) or the planned state (slow). A resource rejected locally by the cache reports from
  its cached state; a resource never evaluated is absent (D5).
- `retry_after_seconds` stays the max over violations: the wait until **every** resource can
  admit, which is the right answer for an all-or-none request.
- Message: `primary_violation`'s `entity/resource`, as today.
- **`ResourceDisabled`** from any resource outranks `RateLimitExceeded` from another.
- `ValidationError` (bad name, duplicate, over the cap, no limits configured for any
  resource) is raised before any write.
- `VersionMismatchError` (#638) only via `limits=` overrides; see D8.

## 7. Sync parity and other surfaces

- `SyncRateLimiter.acquire(..., also=...)`, `SyncLease.resource(...)`: generated (ADR-121).
  The `gather` uses the starred list-comprehension form, so `parallel_mode` applies.
- `RepositoryProtocol`: **no change** — every repository method is already per resource.
- Locust `RateLimiterSession.acquire`: pass `also` through; event name stays the primary
  resource. Optional for v0.17.0.
- CLI: none (runtime limiting needs no CLI, `.claude/rules/api-cli-parity.md`).
- `check_availability`: out of scope; one call per resource remains the display path.
- Docs: API reference, `docs/guide/` page from #678, CLAUDE.md (writer table unchanged,
  pricing rows added, Important Invariant 1 note).

## 8. Cost

Prices as in CLAUDE.md: WCU $0.625/M, transactional write $1.25/M per item (2 WCU), RCU
$0.125/M. Shape: **`search`** cascading to the org (2 items) + **`budget`** per user (1 item),
warm entity and config caches unless stated. "Today" = two sequential acquires.

| # | Case | RT | RCU | WCU | $/M | Today (2 acquires) |
|---|------|----|-----|-----|-----|--------------------|
| 1 | All admitted, fast path | **1** | 0 | 3 | **$1.88** | 2 RT, 0 RCU, 3 WCU, $1.88 |
| 2 | Exit reconcile, all three adjusted | 1 (D7 concurrent) or 3 (serial) | 0 | 3 | $1.88 | 3 RT, 3 WCU, $1.88 |
| 1+2 | **Typical request** | 2 or 4 | 0 | **6** | **$3.75** | 5 RT, 6 WCU, $3.75 |
| 3 | Rejected, rejection cache knows | 0 | 0 | 0 | $0 | $0 (if checked first) |
| 4 | `budget` rejected, cold cache | 2 | 0 | 3 + 2 refunds = 5 | $3.13 | `budget` first: 1 WCU, $0.63; `search` first: 5 WCU |
| 5 | `search` parent rejected, cold cache | 2 | 0 | 3 + 2 refunds = 5 | $3.13 | 3 WCU (cascade fast rejection), $1.88 |
| 6 | `budget` needs a refill (slow), `search` fast | 3 | 1 | 2 + 1 failed + 1 = 4 | $2.63 | 4 RT, 1 RCU, 4 WCU, $2.63 |
| 7 | All slow (first acquire of the entity) | ≈4 | ≈2.5 | 2 failed + 3×2 txn = 8 | ≈$5.31 | ≈2.5 RCU, 2 failed + 4 + 1 = 7 WCU, ≈$4.69 |

Notes:

- Case 1 is the issue's **3 WCU** acceptance target (`tests/benchmark/test_capacity.py`).
  The saving is one round trip, not money.
- Case 3 needs every resource to have a trusted cached state; the issue's "first
  rejection = 0 WCU" is only true here. A cold-cache rejection costs at least the failed
  write (1 WCU, ADR-147 / #695).
- Cases 4–5 are where concurrency costs: up to 2 extra refund WCU per rejection compared with
  a hand-ordered pair. With a 1 s rejection-cache TTL, a retry storm pays this once per
  second per process, then case 3.
- Case 7 pays **+1 WCU (~$0.63/M)** over today for atomicity: the merged 3-item transaction
  is 6 WCU where today's 2-item transaction + 1-item `UpdateItem` is 5. RCU values are
  estimated from CLAUDE.md's per-path costs, not measured.
- The #674 epic's "8 WCU with a real-time site budget cap" is case 1+2 with `budget`
  cascading too (4 items each way).

## 9. Test plan

**Unit (moto), async source, sync generated:**

- All-admitted fast path: one lease, entries for every resource, `lease.resource()` views,
  `consumed` per resource, lease-level methods scoped to the primary.
- Each outcome row of §3's table, with assertions on every bucket's `tk` after the acquire
  (nothing debited on any rejection) and on the exception type and precedence.
- Rejection cache: one resource known short ⇒ 0 DynamoDB calls (`capacity_counter`).
- Mixed fast/slow: `budget` stale, `search` fast; slow commit rejects ⇒ `search` refunded.
- Slow path merged transaction: per-index `CancellationReasons` with two resources (a lost
  create race on one, a lost `rf` lock on another), consumption-only retry, re-issued `Put`.
- Quota move lost on one resource ⇒ whole slow part re-planned; second loss ⇒ moves disabled.
- Item-count overflow ⇒ re-plan without moves (forced with a small patched cap).
- Cascade per resource (#674 shape): `search` debits the org, `budget` does not.
- `ResourceDisabled` on `budget` while `search` admitted ⇒ refunded, 403 raised.
- `on_unavailable=ALLOW` with a backend error on one resource ⇒ refunds, degraded lease,
  `lease.resource("budget")` no-op.
- Validation: duplicate resource, primary in `also`, over the cap, `limits=` + `also` (D8).
- Exceptions in user code ⇒ `_rollback` refunds every resource; adjust after exit raises
  `LeaseExpiredError` on every handle.
- `RateLimitExceeded.as_dict()` names each status's resource.
- **ADR-145 fuzz** (`tests/unit/test_quota_conservation_fuzz.py`): a mixed lease (a sharded
  quota resource plus a rate-limited resource) keeps I8.
- `tests/unit/test_bucket_writer_registry.py` / `test_expression_tokens.py`: **no new
  builder is expected**; if the implementation adds one it is registered there.
- Generated sync tests (`test_sync_limiter.py`) and the gevent/threadpool `parallel_mode`
  path for the concurrent writes.

**Integration (LocalStack):** a real multi-item `TransactWriteItems` across resources,
including a cancellation, and the 2 WCU/item accounting.

**Benchmark:** `test_capacity.py` — case 1 = 3 WCU, 0 RCU; case 3 = 0 calls;
`test_latency.py` — case 1 p50 ≈ one single-resource cascade acquire.

**Design validation:** run the `design-validator` agent on §4 step 2 (moves committed on a
rejected multi-resource pass) — "who funds this slot" is unchanged per resource, but the
rejection now comes from another resource.

## Open decisions

| # | Decision | Options | Recommendation | Why |
|---|----------|---------|----------------|-----|
| D1 | How extra resources are named | A1 `also=` mapping · A2 nested `consume` · A3 `acquire_many()` | **A1** | Backward compatible, one keyword, matches #674/#675 |
| D2 | Per-resource adjust/consume/release | B1 `also=` on each method · **B2 `lease.resource(name)` handle** · B3 `adjust_resources()` | **B2** | `also=` collides with a limit named `also`; a handle makes `consumed` unambiguous and keeps lease-level methods unchanged |
| D3 | Fast path partly admits, rest needs slow path | **Keep fast wins, slow-path the rest** · Refund all, redo all in one transaction | **Keep** | Same as the cascade's parent-only fallback; refund-and-redo costs ~3× WCU on a routine refill |
| D4 | Fast-path write order | **All concurrent** · Sequential, cheapest-to-reject first · Concurrent, with a caller-chosen "check first" resource | **All concurrent** | Admission is the common case; the rejection cache makes repeat rejections free |
| D5 | Statuses on a rejection | **Evaluated resources only (cached states count)** · Every resource, reading the unevaluated ones | **Evaluated only** | A read on the rejection path costs RCU and a round trip for display data; `retry_after_seconds` already covers the whole request |
| D6 | 100-item limit | **Cap resources at the boundary (16) + re-plan without moves if donors overflow** · Count after planning and raise `ValidationError` · Split into several transactions | **Cap + re-plan** | Predictable `ValidationError` before I/O; overflow can only under-admit, never fail; splitting loses atomicity |
| D7 | Exit reconcile and refunds: write order | Serial `write_each` per item (today, #682) · **Concurrent per item, each outcome tracked** | **Concurrent** | #682 needs to know *which* items landed, not their order; N RT → 1 RT. Applies to single-resource cascades too, so it could ship separately |
| D8 | `limits=` override with `also=` | **Reject the combination (`ValidationError`)** · Override applies to the primary only · Per-resource override mapping | **Reject** | A list of limits has no resource; guessing is how a budget gets the wrong limit. Revisit if a caller needs it |
