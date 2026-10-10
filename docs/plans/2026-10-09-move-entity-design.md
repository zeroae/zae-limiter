# Move an entity to a new parent: design

**Decision record:** [ADR-150](../adr/150-move-entity-to-new-parent.md) (Proposed)
**Issue:** [#677](https://github.com/zeroae/zae-limiter/issues/677), part of epic #674
**Milestone:** v0.17.0

## TLDR

- **One call** (`Repository.set_parent`, CLI `entity set-parent`) rewrites the entity's META
  record and GSI1 keys, then **reuses the ADR-146 cascade fan-out** to restamp `parent_id`
  and the effective `cascade` on every bucket the entity owns.
- **A parent generation (`pgen`)** on META and on bucket items stops a stale writer from
  putting the old parent back. Without it the move is not safe (§4).
- **The warm path checks `parent_id`, not just `cascade`**: a process with a stale cache
  refunds the old parent and debits the new one, once per (process, entity).
- **Nothing moves between parents.** Old-parent usage, open leases and quota grants stay
  where they were debited.
- Cost per move ≈ **1 + R·S WCU** writes and **~3 + 3R RCU** reads (R resources, S shards);
  **0 change** on every acquire.

## 1. What the code does today (verified 2026-10-09, `origin/main` f5d2ee2)

| Fact | Where |
|------|-------|
| META holds `parent_id` (`S` or `NULL`), `cascade`, and GSI1 keys `GSI1PK={ns}/PARENT#{p}`, `GSI1SK=CHILD#{id}` only when there is a parent | `Repository.create_entity` |
| `create_entity` does **not** check that the parent exists, so a cycle A→B, B→A can already be built today | same |
| `get_children` reads GSI1 (eventually consistent) | `Repository.get_children` |
| `_entity_cache[(ns, id)] = (cascade, parent_id, shard_counts)`, no expiry; overwritten by every META read (`get_entity`, `batch_get_entity_and_buckets`) and by `_learn_shard_count(meta=...)` from every speculative **success** image | `repository.py` |
| `_cascade_cache[(ns, id, resource)]` is learned only from a stamp that carries `parent_id` | `_learn_shard_count` |
| Warm parallel path debits `parent_id_cached`. On child success the result keeps the **item's** `parent_id`; the limiter refunds the parent only when the item says `cascade=False` with a `parent_id`. It does **not** compare parent ids | `speculative_consume`, `limiter.py` ~L1047 |
| Slow path reads META with an **eventually consistent** `BatchGetItem` and the rf-locked write re-stamps `cascade`/`parent_id` (`#ocs`/`#opid`) from it | `batch_get_entity_and_buckets`, `build_composite_normal(owner=...)` |
| `_fanout_cascade(entity_id=...)` discovers the entity's buckets by GSI3 (two passes), reads META and `resolve_access` **strongly consistently**, and stamps `SET cascade, parent_id` (or `REMOVE parent_id`) under `attribute_exists(PK)` | `_fanout_cascade`, `_stamp_bucket_cascade` |
| Provisioner mirror stamps the same pair | `zae_limiter_provisioner/fanout.py` `stamp_bucket_cascade` |
| The aggregator never reads `cascade` or `parent_id`; Path 2 clones copy them verbatim | `zae_limiter_aggregator/` (no match) |
| The manifest does **not** declare entities' parents (no `parent` key in `manifest.py`); entity META is not provisioner-managed | `zae_limiter_provisioner/manifest.py` |
| Rejection cache entries carry `cascades` + `parent_id` from the item; `parent_of` trusts them only inside `rejection_cache_ttl` | `rejection_cache.py` |
| No `AuditAction` exists for an entity update (`ENTITY_CREATED`, `ENTITY_DELETED`, `LIMITS_SET`, …) | `models.py` |

## 2. The operation

`set_parent(entity_id, parent_id | None, *, principal=None) -> int` (buckets stamped).

1. **Validate.** `validate_identifier` on both ids; `parent_id == entity_id` → `ValidationError`.
2. **Gate.** `_require_readers("0.17.0", …)` — the ADR-141 machinery ADR-146 reuses (§9).
3. **Cycle check** (§6): walk the new parent's ancestors with strongly consistent META reads.
4. **META write** — one `UpdateItem` on `{ns}/ENTITY#{id}`, `#META`:
   - to a parent: `SET parent_id = :p, GSI1PK = :g1pk, GSI1SK = :g1sk, pgen = if_not_exists(pgen, :zero) + :one`
   - to no parent: `SET parent_id = :null, pgen = …  REMOVE GSI1PK, GSI1SK`
   - condition `attribute_exists(PK)` (missing entity → `EntityNotFoundError`), `ReturnValues=ALL_OLD`
     so the audit event records the old parent and the new `pgen` is `old + 1` with no read.
   - META `cascade` is **not** touched (decision D3).
5. **Evict** this process's caches: `_entity_cache` entry rewritten from the new META,
   `_cascade_cache` entries for the entity dropped, rejection cache cleared
   (`@clears_rejection_cache`, as every admin write).
6. **Fan-out** — `_fanout_cascade(entity_id=..., resource=None)` unchanged in discovery and
   resolution, with one change to the stamp (§4): it writes `pgen` and is conditioned on it.
   The effective cascade for each bucket is re-resolved by the ADR-146 walk, so a move to no
   parent stamps `cascade=False` (no parent never cascades) and a move from root to a parent
   stamps whatever the policy/META says.
7. **Audit** — `AuditAction.ENTITY_PARENT_CHANGED`, details
   `{old_parent_id, parent_id, pgen, buckets_stamped}`.

Idempotent: a repeat call with the same parent bumps `pgen` and re-runs the fan-out, which is
also the repair path after `FanoutIncomplete`.

**GSI1.** The update moves the META item between GSI1 partitions; `get_children(old)` stops
listing it and `get_children(new)` lists it once the index propagates (eventually
consistent, typically sub-second). No other item carries GSI1 keys.

## 3. Where staleness lives, and for how long

| Holder | Stale value | How it is corrected | Old parent debited meanwhile |
|--------|-------------|---------------------|------------------------------|
| Bucket item, before the fan-out reaches it | old `parent_id` | fan-out stamp | every acquire on that shard, for the fan-out's duration (ms per bucket) |
| Bucket item stamped by a stale slow pass | old `parent_id` | **cannot happen** with `pgen` (§4); without it, never corrected for a fast-path-only bucket | — |
| `_entity_cache` in another process (v0.17) | old `parent_id` | the child's returned item (§5) | **0 net**: one debit, refunded on the same call |
| `_entity_cache` in another process (v0.16) | old `parent_id` | the child's returned item, after the call | **1 debit** per (process, entity), kept |
| `_cascade_cache` | per-resource policy | not keyed on the parent; stays valid unless the move changes the effective cascade (to/from no parent), which the item's stamp corrects (§5) | as above |
| Rejection cache (`parent_of`) | old parent's shards | age cap | none: it only rejects (ADR-147). A child can get a 429 judged on the old parent for ≤ `rejection_cache_ttl` (default 1 s) |
| Slow pass in flight across the move | old META | `pgen` pin makes its owner stamp fail → consumption-only retry | that one acquire (in flight, §7) |

The issue's three options for cache staleness (refresh from the item, a TTL, both): **refresh
from the item**. Every speculative response already returns the stamp at no cost; a TTL would
add periodic META reads for a value that changes almost never (decision D2).

## 4. The race the generation closes

Without a generation, this interleaving leaves the old parent on a bucket forever:

| t | Slow pass S (process A) | Move (process B) |
|---|-------------------------|------------------|
| 0 | `BatchGetItem` reads META: parent P1 (eventually consistent, or simply before t1) | |
| 1 | | META → P2 |
| 2 | | fan-out stamps bucket P2 |
| 3 | rf-locked write: owner stamp **P1** — the `rf` lock passes, the fan-out never moves `rf` | |

From t3 the bucket says P1. If it then only takes the fast path (aggregator refill keeps it
topped up), every process re-learns P1 from its image and debits P1 indefinitely. A strongly
consistent META read does not help: S can read before t1 and write after t2.

**Fix:** `pgen` (number) on META and on bucket items.

- META: set to the creation instant (epoch ms) by `create_entity`, incremented by every
  move; absent = 0 (an entity created before v0.17). Not 0 at creation: a bucket can
  outlive its entity (a delete whose GSI3 query missed it, an acquire in flight across
  the delete) and keep the generation the old entity reached; a recreated entity
  starting at 0 would sit below it, and the owner-stamp pin would fail on that bucket
  forever, leaving the old parent on it (review of #716, finding 4).
- Every writer of `parent_id`/`cascade` onto a bucket writes the `pgen` it read beside them and
  adds `attribute_not_exists(pgen) OR pgen <= :pgen` to its condition:
  - **owner stamp** in `build_composite_normal` — a lost pin fails the transaction like a lost
    `rf` lock and goes down the existing consumption-only retry, which stamps no owner;
  - **create `Put`** — a `Put` cannot condition on attributes of an item that does not exist.
    The two-pass fan-out does **not** cover it (review of #716, finding 2): a create that read
    META before the move can land after both passes, on a resource that had no bucket when
    the move ran. So after the write, each created bucket's owner META is read once, strongly
    consistent (`Lease._repair_created_owner_stamps` →
    `Repository.repair_created_owner_stamp`, +1 RCU per bucket created), and when its
    generation moved the bucket is restamped through the fan-out's pinned write. A move whose
    META write follows that read fans out after the bucket exists and finds it (the ADR-125
    GSI3 lag residual, nothing new). Not a `ConditionCheck` on META inside the create's
    transaction: no shipped policy grants `dynamodb:ConditionCheckItem`, so an application
    role on the acquire-only policy would be refused on every create, and a lone `Put` would
    become a 2-item transaction (4 WCU instead of 1);
  - **aggregator Path 2 clone** — copies shard 0's stamp from a stream image that can predate
    the move, again after the fan-out ran (finding 3). After the clones land the aggregator
    reads the owner's META once per record (strongly consistent, 1 RCU) and, when the
    generation differs from the image's, restamps each clone with META's parent and the
    ADR-146 policy walk (consistent reads), through the same pinned write
    (`processor._repair_clone_owner_stamps`). Same reason as above for a read rather than a
    `ConditionCheck`: the aggregator role has no `ConditionCheckItem` either;
  - **fan-out stamp** (`_stamp_bucket_cascade`, provisioner `stamp_bucket_cascade`) — a lost
    pin means a newer move already stamped this bucket: skip, as for a vanished bucket. Two
    concurrent moves therefore converge on the later one on every bucket.
- A bucket stamp **carrying `pgen`** is authoritative for both attributes, with or without
  `parent_id`. That settles the ADR-146 rule "only a stamp carrying `parent_id` is a policy",
  which otherwise ignores a v0.17 move-to-root stamp (`cascade=False`, no `parent_id`) as a
  pre-#684 artefact. Items without `pgen` keep today's rule.

Cost: one number attribute (~8 B) and one condition term; 0 RCU, 0 WCU on any path that does
not lose the race. Expression tokens are fixed names (`#pg`/`:pg`), declared in
`tests/unit/test_expression_tokens.py`.

## 5. Warm path: the item overrules the cache on `parent_id` too

In `RateLimiter` after the parallel write, extend the ADR-146 mismatch handling:

| Child item says | Cache debited | Action | Extra cost |
|-----------------|---------------|--------|-----------|
| cascade, parent P2 | P1 (`P1 != P2`) | compensate P1 on the shard written; issue the sequential parent write to P2 (the cold path's code); learn P2 | +1 WCU refund, +1 WCU parent write (instead of P1's) — once per (process, entity) |
| no cascade, `pgen` present, no parent | P1 | compensate P1; child-only lease; learn (False, None) | +1 WCU refund, once |
| no cascade, parent P2 | P1 | existing ADR-146 refund | unchanged |

If the P2 write is rejected the lease fails exactly as a cold-path parent rejection does,
with the child compensated (existing `_handle_nested_parent_failure` route). After the refund
the result no longer names P1 as the debited parent, so every later step — the parent-only
slow path when a refill would help P2, a disabled parent's 403, a `wcu` doubling — acts on P2
(review of #716, finding 1: it used to run the parent-only acquire against P1).

A child **failure** image compensates the write that actually landed (the cached parent,
`debited_parent_id`) and learns the item's parent for the next call. A disabled cached parent
outranks the child's own failure only when the item names that same parent; a parent the
child has been moved away from cannot 403 it (finding 5).

Sequential path and slow path need no change: both act on the item's or META's parent.

## 6. Cycles

The cascade only ever covers child + parent (#686), so a cycle cannot loop an acquire. It
still corrupts hierarchy reports and makes "the org" meaningless. `set_parent` walks the new
parent's chain — strongly consistent `GetItem` of each ancestor's META — and refuses
(`ValidationError`) if it meets `entity_id`. Bounded at 32 hops (refuse with a clear message
beyond). Cost = depth of the new parent, typically 1–2 RCU. Concurrent moves can still build
a cycle (A under B while B moves under A); that window is accepted and the ancestor check is
a guard against operator error, not a lock (decision D4). `create_entity` is unchanged.

Whether the new parent must exist is decision D5.

## 7. In-flight leases

A lease records its entries by entity id. A lease opened before the move holds the old
parent's entries; its `adjust()`, `release()` and rollback land on the old parent. This is
correct — they reconcile what was debited there — and is the issue's "usage already debited
is not moved". A lease opened after the stamp holds the new parent. Leases are not rewritten.

## 8. Cascade policy, quotas, aggregator

- **Cascade policy (ADR-146):** keyed per (entity, resource) on config items, unaffected by a
  move. The effective value is re-resolved by the fan-out; with no parent it is always off.
  Moving a root entity under a parent cascades only where META `cascade` or a policy says so.
- **Quotas (ADR-145):** the old and new parents' buckets are their own entities' items; a
  quota's grants (`gc`), donors and periods are per (entity, resource). Nothing in a child's
  bucket refers to a parent's grant, so a move moves no allowance. The new parent's session
  windows (ADR-139/140) are the new parent's own. **Confirmed: nothing moves.**
- **Aggregator:** its refill reads neither attribute. Path 2 clones copy `cascade`,
  `parent_id` and `pgen` from the stream image, which can predate a move, so the clone path
  re-checks the owner's META after the clones land and restamps them (§4). Not "no
  change", as this design first said: the review of #716 measured 9 of 20 debits going
  to the old parent from clones of a lagging record.
- **Rejection cache (ADR-147):** a move is an admin write and clears this process's cache.
  `TestChangesElsewhere` gets a case: a move made by a second `Repository` → the next acquire
  debits the new parent and admits exactly what DynamoDB alone would.

## 9. Version gate

| Old writer | What it can do after a move | Closed by |
|------------|-----------------------------|-----------|
| v0.16 client slow path | owner-stamp the old parent in the race window, with no `pgen` pin — the §4 failure, permanent on a fast-path-only bucket | ratchet `client_min_version` → 0.17.0 (v0.16 refuses to open the stack) |
| v0.16 client warm path | one kept debit of the old parent per (process, entity) | same ratchet; processes already open are the ADR-141 documented hole |
| v0.16 provisioner `fanout_cascade` | stamp a stale parent if it races a move | writer gate: `lambda_version >= 0.17.0` |
| v0.14/v0.15 | ignore `client_min_version` / already gated out by ADR-146's 0.16 ratchet if a policy was set | documented, as ADR-141/146 |

Free when it passes (gate read only, 1 consistent RCU). Proposed constant
`MIN_READER_VERSION_FOR_PARENT_MOVE = "0.17.0"` beside the ADR-146 one.

## 10. Surfaces

- **API:** `Repository.set_parent(entity_id, parent_id, *, principal=None) -> int`; required
  member of `RepositoryProtocol` (ADR-146 precedent, ADR-109); `RateLimiter.set_parent`
  delegating, as `create_entity` / `get_children` do; sync twins generated.
- **CLI:** `zae-limiter entity set-parent ENTITY_ID (--parent P | --none) [-N ns]`, printing
  old → new parent and buckets stamped; exit 1 on `FanoutIncomplete` with the count and
  "re-run to finish". `entity show` already prints the parent.
- **Manifest:** not today (entities' parents are not in the manifest; decision D6).
- **Exceptions:** `EntityNotFoundError`, `ValidationError` (self/cycle), `VersionMismatchError`,
  `FanoutIncomplete`.
- **Docs:** `docs/guide/hierarchical.md` (moving), `docs/cli.md`, CLAUDE.md invariant 9 and the
  writer table (`pgen` on the owner stamp and fan-out rows), entity metadata cache section.

## 11. Cost

Per move, entity with R resources × S shards each, ancestor depth d, on-demand us-east-1:

| Step | RCU | WCU |
|------|-----|-----|
| Gate read (+ first-time ratchet) | 1 | 0 (+1 once) |
| Ancestor walk | d | 0 |
| META `UpdateItem` | 0 | 1 |
| Fan-out discovery (2 GSI3 KEYS_ONLY queries) | ~1 | 0 |
| Fan-out META read (consistent) | 1 | 0 |
| `resolve_access` per resource (3 consistent keys) | 3R | 0 |
| Stamps | 0 | R·S |
| Audit event | 0 | 1 |
| **Total** | **~3 + d + 3R** | **2 + R·S** |

#674's shape (R = 30, S = 1, d = 1): **~94 RCU + 32 WCU ≈ $31.75 per million moves**
(94 × $0.125 + 32 × $0.625). Admin path; a move per user per month is negligible.

Per acquire: **unchanged** (fast path 0 RCU + 1 WCU; `pgen` is one condition term on the
rf-locked write). Per bucket **created** (first acquire of an entity on a resource, or a new
shard): **+1 RCU**, the strongly consistent owner check (§4), ~$0.125/M creates; a restamp
(+1 WCU, + 4 RCU for the policy) only when a move overtook the create. Per aggregator record
that clones shards: +1 RCU, the same check. After a move, once per (process, entity): **+2 WCU** (refund + new-parent
write replace a plain parent write: +1 WCU net over a normal cascade call, ~$0.625/M of those
calls). A lost `pgen` pin: +1 WCU (the retry), only inside a move's race window.

## 12. Test plan

- **Unit (moto):** META + GSI1 to parent / to root / root → parent; `get_children` both sides;
  `pgen` increments; missing entity; self-parent and 2-hop cycle refused; audit event.
- **Fan-out:** every shard of every resource stamped; effective cascade re-resolved (to root
  ⇒ off); `FanoutIncomplete` count and re-run.
- **The race (§4):** a slow pass whose META read predates the move, owner stamp after the
  fan-out → stamp refused, acquire completes via retry, bucket keeps the new parent. Same for
  a stale provisioner stamp and two concurrent moves (later wins everywhere).
- **Another process (acceptance):** limiter B warm on P1; A moves to P2; B's next acquire
  debits P2 only (P1 net 0); B's second acquire uses the parallel path to P2. Move to root:
  P1 net 0 and no cascade afterwards.
- **In-flight lease:** open on P1, move, `adjust(+n)` lands on P1; rollback lands on P1.
- **Rejection cache:** `TestChangesElsewhere` case (above); local 429 against the old parent
  bounded by the TTL, never an admission.
- **Quota:** child and both parents' quota conservation fuzz unchanged (I8); bucket-writer
  registry declares the fan-out stamp "no `tk`/`gc`".
- **Version gate:** refused below 0.17.0; ratchet; dev build passes.
- **Expression tokens:** owner stamp + both fan-out stamps.
- **CLI:** `--parent`, `--none`, mutually exclusive, exit codes.
- **Integration (LocalStack):** GSI1 propagation; end-to-end move under load with two limiters.

## Open decisions

**D1 — Close the stale owner-stamp race (§4)?**
- (a) `pgen` generation pinned on every `parent_id` writer. Closes it; one attribute + one
  term; needs the 0.17 ratchet.
- (b) Delay the fan-out's second pass (e.g. 5 s) past replication + a slow pass. No schema;
  a timing guess.
- (c) Accept it; self-heals on the bucket's next slow pass (unbounded for fast-path-only).
- **Recommend (a):** the only option that is a guarantee, and it also settles the
  move-to-root stamp ambiguity for free.

**D2 — How other processes learn the move.**
- (a) From the child's returned item (the warm-path parent check, §5).
- (b) An entity-cache TTL (e.g. 60 s, like the config cache).
- (c) Both.
- **Recommend (a):** zero added reads, corrects within one call; a TTL costs META reads on
  every hot entity forever for a rare event and still debits the old parent for the TTL.

**D3 — Does `set_parent` also take `cascade`?**
- (a) No; use ADR-146 `set_entity_cascade` (policy) — META `cascade` stays as created.
- (b) Optional `cascade=` that rewrites META `cascade` too.
- **Recommend (a):** ADR-146 rejected a mutable entity-wide flag; a policy on the entity's
  `_default_` config already does it, gated and fanned out. Document "moving a root entity
  under a parent: set a policy if it was created with `cascade=False`".

**D4 — Cycle protection.**
- (a) Refuse self only.
- (b) Bounded consistent ancestor walk (depth ≤ 32), best effort under concurrent moves.
- (c) (b) plus a conditional write on each ancestor's `pgen` in one transaction (race-free).
- **Recommend (b):** cascade is one hop, so a cycle cannot loop an acquire; (b) stops the
  operator mistake for ~1–2 RCU; (c) costs a transaction across d items for a race nobody hits.

**D5 — Must the new parent exist?**
- (a) Yes (`EntityNotFoundError`); the ancestor walk reads it anyway.
- (b) No, matching `create_entity`.
- **Recommend (a):** the read is already paid, and moving users into a mistyped org is the
  likeliest operator error; `create_entity` can be tightened separately.

**D6 — Manifest support.**
- (a) Out of scope: the manifest declares config items, not entities.
- (b) Add `entities.<id>.parent` and let the provisioner move entities.
- **Recommend (a):** the provisioner manages config only; entity lifecycle (create/delete)
  is not in the manifest either, and a declarative parent would need create semantics first.

**D7 — Name.**
- (a) `set_parent` / `entity set-parent` (the issue's).
- (b) `move_entity` / `entity move`.
- **Recommend (a):** matches the `set_*` admin family and reads correctly for "to no parent".
