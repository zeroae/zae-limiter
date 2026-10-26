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
  **0 change** on an acquire that touches existing buckets.
- **A bucket create is pinned to the owner's META** inside its own transaction (a
  `ConditionCheck`), and an aggregator clone likewise; see
  [Review decisions (2026-10-20)](#review-decisions-2026-10-20), which supersedes the
  post-create owner check described in §4, §8 and §11.

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
| Slow pass in flight across the move, or reading a stale META replica | old META | `pgen` pin makes its owner stamp fail; the image shows the same `pinc` at a higher `pgen`, so the acquire re-plans once from a strongly consistent read (`OwnerStampLostError`, +1 RCU) | **0**: the new parent is debited. Was: the consumption-only retry debited the old parent on every such pass while META reads lagged (review P1) |

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
- **Incarnation** (`pinc`, second review of #716): a creation instant is only ordered on one
  clock, so a leftover bucket from a process whose clock ran ahead could still carry a
  higher `pgen` than the recreated entity, and then win every pin forever (old parent
  debited, `rf` frozen, `set_parent` counting a stamp it never wrote). `create_entity` writes
  a random token on META; every owner stamp copies it beside `pgen`: a generation compares
  only within one incarnation. META without `pinc` (created before this) is the legacy
  incarnation `-` (amended: its `created_at`, `-` only if that is missing too; see the
  review decisions below). A fan-out counts only buckets it actually stamped. Two pins, by how the
  writer read META (third review of #716):
  - **strongly consistent** writers — the fan-out, the provisioner fan-out (and, until the
    2026-10-20 review removed them, the post-create owner check and the aggregator's clone
    repair) — use `attribute_not_exists(pinc) OR pinc <>
    :inc OR attribute_not_exists(pgen) OR pgen <= :pg`: they read the current record, so
    another incarnation's stamp is a predecessor's and loses;
  - the **slow path's rf-locked owner stamp** read META eventually consistently, and after a
    delete and recreate that read can be the predecessor's. It uses `attribute_not_exists(pinc)
    OR (pinc = :inc AND (attribute_not_exists(pgen) OR pgen <= :pg))` and asks for the failure
    image. Losing to another `pinc`, the lease reads META once, strongly consistent
    (`Repository.restamp_owner_if_current`): if its incarnation is current the bucket is a
    leftover, restamped through the fan-out's write, and the original write is re-sent once
    (the claim moves no `rf`); if not, the bucket is left alone and the acquire commits on the
    consumption-only retry, debiting the parent it planned (the predecessor's) once — the
    same bound as a lease opened before a move. Without this split, a stale replica put the
    old parent back over the current stamp (org-a debited 10 of 10 afterwards).
- **A stamped shard is cleared where it is** (third review, finding 3): a trimming `vu = 0`
  (the owner-check fallback, or #679's credit clamp) fails a fast-path write whose balances
  fit, which the repository classifies as exhausted. On a sharded bucket the shard retry then
  sent the slow path to another shard, so the stamped one was never cleared. When the image
  shows the request would have fit, the slow path now goes to that same shard. main's #679
  stamp has the same problem on any sharded bucket.
- Every writer of `parent_id`/`cascade` onto a bucket writes the `pgen` it read beside them and
  adds `attribute_not_exists(pgen) OR pgen <= :pgen` to its condition:
  - **owner stamp** in `build_composite_normal` — a lost pin fails the transaction like a lost
    `rf` lock and goes down the existing consumption-only retry, which stamps no owner;
  - **create `Put`** — **Superseded by [Review decisions (2026-10-20)](#review-decisions-2026-10-20).** A `Put` cannot condition on attributes of an item that does not exist.
    The two-pass fan-out does **not** cover it (review of #716, finding 2): a create that read
    META before the move can land after both passes, on a resource that had no bucket when
    the move ran. So after the write, each created bucket's owner META is read once, strongly
    consistent (`Lease._repair_created_owner_stamps` →
    `Repository.repair_created_owner_stamp`, +1 RCU per bucket created), and when its
    generation or incarnation moved the bucket is restamped through the fan-out's pinned
    write. The check runs once the acquire's caller block has finished (the lease is handed
    back first: the check matters only to the next acquire). If it fails, the bucket gets
    `vu = 0`, so the next acquire takes the slow path, whose pinned owner stamp restamps it;
    if that write fails too, it is logged. A process that dies between create and check
    leaves the stamp to the bucket's next slow pass. A move whose
    META write follows that read fans out after the bucket exists and finds it, unless GSI3
    still lags the create: then both passes miss it, exactly ADR-125's residual for
    `disabled`, and the bucket keeps the old parent until its next slow pass or a repeat of
    the move. Not a `ConditionCheck` on META inside the create's
    transaction: no shipped policy grants `dynamodb:ConditionCheckItem`, so an application
    role on the acquire-only policy would be refused on every create, and a lone `Put` would
    become a 2-item transaction (4 WCU instead of 1);
  - **aggregator Path 2 clone** — **Superseded by [Review decisions (2026-10-20)](#review-decisions-2026-10-20).** Copies shard 0's stamp from a stream image that can predate
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
- **Aggregator:** **Superseded by [Review decisions (2026-10-20)](#review-decisions-2026-10-20).** Its refill reads neither attribute. Path 2 clones copy `cascade`,
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
rf-locked write). **Superseded by [Review decisions (2026-10-20)](#review-decisions-2026-10-20).** Per bucket **created** (first acquire of an entity on a resource, or a new
shard): **+1 RCU**, the strongly consistent owner check (§4), ~$0.125/M creates, started when
the commit lands and joined on lease exit, so it adds latency only when the caller's block is
shorter than the read; a restamp
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

## Decisions (ratified 2026-10-26)

All seven were ratified as built, on the recommended option, at v0.17.0 release prep. The
"Recommend" lines below are the chosen options; D4 and D5 carry follow-up notes.

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
- **Follow-up:** the hierarchical guide says "two levels only", but neither `create_entity` nor
  `set_parent` enforces it, which is why a cycle check is needed at all. Whether to enforce two
  levels (which makes a cycle impossible) or document deeper chains is #784 (v1.0.0).

**D5 — Must the new parent exist?**
- (a) Yes (`EntityNotFoundError`); the ancestor walk reads it anyway.
- (b) No, matching `create_entity`.
- **Recommend (a):** the read is already paid, and moving users into a mistyped org is the
  likeliest operator error; `create_entity` can be tightened separately.
- **Follow-up:** `create_entity` still accepts a missing parent; tightening it is part of #784.

**D6 — Manifest support.**
- (a) Out of scope: the manifest declares config items, not entities.
- (b) Add `entities.<id>.parent` and let the provisioner move entities.
- **Recommend (a):** the provisioner manages config only; entity lifecycle (create/delete)
  is not in the manifest either, and a declarative parent would need create semantics first.

**D7 — Name.**
- (a) `set_parent` / `entity set-parent` (the issue's).
- (b) `move_entity` / `entity move`.
- **Recommend (a):** matches the `set_*` admin family and reads correctly for "to no parent".

## Review decisions (2026-10-20)

The owner-approved fixes from the review of Stack B (#712, #716, #722). This section
supersedes the post-create owner check in §4, §8 and §11; the text there is kept as
history. ADR-150 records the decision; the rules and costs live here.

**Incarnations.** `create_entity` writes a random token, `pinc`, on META, and every owner
stamp copies it beside `pgen`; a generation compares only within one incarnation. An entity
created before v0.17 has no `pinc`: its incarnation is its `created_at`, which a delete and
recreate changes, and `-` only if that is missing too (`schema.entity_incarnation`, the one
rule every reader shares). Writers that read META strongly consistently (the fan-out, the
provisioner fan-out) may overwrite a stamp of another incarnation; the slow path's rf-locked
owner stamp read META eventually consistently, so it may overwrite only its own incarnation's
stamp. When it loses that pin to another incarnation it reads META once, strongly
consistently (`Repository.restamp_owner_if_current`, a required member of
`RepositoryProtocol`): if its incarnation is current the bucket is a predecessor's leftover
and is claimed, and the write is re-sent once; otherwise the bucket is left alone.

**A bucket create is pinned to the owner's META.** Every client create — the first create,
an ADR-133 shard create, a cascade parent's create, each part of a multi-resource `also=`
commit, and a create re-issued on the consumption-only retry — carries one `ConditionCheck`
per distinct owner, on `PK={ns}/ENTITY#{id}, SK=#META`, asserting the `pgen` and `pinc` the
pass read by the incarnation rule above (`attribute_not_exists` terms for what was absent),
or `attribute_not_exists(PK)` when the pass found no META (`schema.owner_meta_condition`,
`Repository.build_owner_check`). The checks follow the bucket items and donor debits so the
per-index cancellation reasons still line up, and an item is never touched twice. A
cancellation on a check raises `OwnerStampLostError`, a contended `RateLimitExceeded`; the
slow path (single- and multi-resource) re-plans once with every owner's META read strongly
consistently, which also cascades to the new parent, and a second loss stands as the
contended 429. Two parts of one multi-resource commit that read different META for one owner
re-plan before sending. The parent-only fallback never creates (it returns None when the
parent bucket is missing), so it has nothing to pin. The background owner check, its join on
lease exit, its cancellation and `KeyboardInterrupt` handling, its `vu = 0` fallback and the
sync generator's `create_task` translation are removed.

**Aggregator clones.** Every Path 2 clone is one `TransactWriteItems`: the `Put`, any ADR-145
donor debit, and a `ConditionCheck` on the owner's META asserting the stream image's `pgen`
and `pinc` (an image with neither asserts META has neither). A clone the check cancels is
skipped and left to the client, exactly like the R13 and R15 skips, and logged without the
entity id. The post-write clone repair is removed.

**IAM.** `dynamodb:ConditionCheckItem` is granted to the `acq`, `full`, `ns-acq` (namespace
statement only) and `ns-full` managed policies and to the aggregator role. Breaking for
custom IAM: every principal that acquires, and a custom aggregator role, needs it.

**Version gate.** `set_parent` passes the ADR-141 gate at 0.17.0 and ratchets
`client_min_version` to 0.17.0 (§9). The ratchet refuses only a pre-0.17 process's next open:
until it reopens the stack, it debits the old parent on every warm-path call, and its
unpinned slow-pass stamp can restore the old parent from a stale read. Restart every client
after upgrading, before the first move.

**Ancestor check.** One strongly consistent `GetItem` per level of the new parent's chain,
bounded at 32 levels (§6).

**Audit.** `ENTITY_PARENT_CHANGED` with `{old_parent_id, parent_id, pgen, buckets_stamped}` on
every path: on success (a failed audit raises, like every admin method), after
`FanoutIncomplete` with `buckets_stamped` = what was stamped, and after any other fan-out
error with 0. On the two error paths a failed audit write is logged at WARNING (action and
error type, no entity id) and the original exception is re-raised, so the caller still learns
to re-run the move.

**Warm path.** When the child's item names another parent than the one the cache debited,
the debit is refunded and the item's parent debited (§5). An item stamped with the same
incarnation and a lower generation than the newest this process has seen (a shard the fan-out
has not reached) is older, not a policy: it neither moves the cached owner nor feeds
`_cascade_cache`, and nothing is refunded (`Repository._owner_stamp_cache`, shared with every
`namespace()` scope). A missing-shard hint from the fast path names the parent it came from
and is used only for that parent; and the slow path never honours an explicit shard at or
past the count it would stamp on a create. Every send of the commit transaction retries a
write conflict and turns sustained contention into the contended 429. `_find_disabled` and
the CLI read META strongly consistently.

**Consumption stays where it was debited**, including by leases open across the move (§7).

**Costs.**

| Path | Before the review | Now |
|------|-------------------|-----|
| Bucket create (once per shard) | `PutItem` + 1 RCU consistent owner check after it | a 2-item transaction: the `Put` and the META `ConditionCheck`; no read after it |
| Aggregator clone | `PutItem` (or a 2-item move) + 1 RCU per record | one transaction per clone with one more item; no read |
| Slow-path claim of a predecessor's bucket | — | 1 RCU, + up to 3 RCU (strongly consistent policy walk) only when it claims |
| Lost META check | — | one re-plan (+1 RCU per owner, consistent), then the commit |

A transactional write bills 2 WCU per KB, and AWS bills a transaction
[2 units per item](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis.html#transaction-capacity-handling).
A `ConditionCheck` item bills as **write** capacity, 2 WCU like the `Put` (measured on DynamoDB
2026-10-21 with `ReturnConsumedCapacity`: the `Put` alone in a transaction 2 WCU, `Put` + check
4 WCU whether the check reads a META item or asserts one absent, a plain `PutItem` 1 WCU, 0 RCU
in every case; a cancelled transaction returns no `ConsumedCapacity`). A bucket create is
therefore 4 WCU where it was 1, including for an entity with no META and on every TTL
recreation of a resource- or system-default bucket.
