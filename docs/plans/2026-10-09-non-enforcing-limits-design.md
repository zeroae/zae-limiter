# Soft limits and bypass: design

**Decision record:** [ADR-151](../adr/151-non-enforcing-limits.md) (Proposed)
**Issues:** [#467](https://github.com/zeroae/zae-limiter/issues/467) (soft limits),
[#311](https://github.com/zeroae/zae-limiter/issues/311) (bypass; BLOCKED shipped as ADR-125)
**Epic:** [#674](https://github.com/zeroae/zae-limiter/issues/674), milestone v0.17.0
**Status:** design only — no production code. Every "today" claim below was checked against
`origin/main` at `f5d2ee230` (v0.16.0 + #711); line numbers are from that commit.

## TLDR

| | Soft limit (#467) | Bypass (#311) |
|---|---|---|
| What it is | A property of **one limit** | An access mode of an **(entity, resource)** |
| Admits? | Always, for that limit (other hard limits still gate) | Always (only `wcu` still gates) |
| `tk` | Debited — may go into debt | **Untouched** |
| `tc` | Incremented | Incremented |
| Config | `l_{name}_soft` beside `cp`/`ra`/`rp` (travels with the limit) | Fourth value of the ADR-125 `disabled` attribute |
| Bucket stamp | `b_{name}_soft` per limit | `bypass` (item-level, separate from `disabled`) |
| Fast path knows it by | **Nothing** — the server decides (`attribute_exists(soft) OR tk >= c`) | A per-process cache learned from returned item images |
| Fast-path cost | 0 RCU + 1 WCU (unchanged) | 0 RCU + 1 WCU (warm) |
| Change reaches buckets by | Entity: existing param sync. Resource/system: new change-only fan-out | ADR-125 fan-out (GSI2 / GSI3), unchanged mechanism |

## 1. Why two mechanisms, not one

The issues ask for things that look alike ("admit, keep counting") but sit at different layers.

- **Soft-ness modifies one limit.** `tpm` soft, `rpm` hard on the same resource is the headline
  use case. Limits resolve **override, not merge** (an entity-level `tpm` replaces the
  resource's `tpm` entirely), so the flag that says how `tpm` behaves must ride with the `tpm`
  definition it modifies. A separate walk would let an entity's own hard `tpm` inherit a
  resource's soft flag — the entity author never wrote it. (#467 Q7: **travels with the limit**.)
- **Bypass is an access decision**, identical in shape to `disabled` and the cascade policy:
  "this entity, on this resource, is treated specially". ADR-125 already resolves that by one
  walk with carve-outs, and #311 Design Constraint 1 requires bypass to be the same walk.
- **Their write shapes differ.** Soft still debits `tk` (debt is the overdraw signal). Bypass
  must **not** debit `tk`, so that restoring enforcement needs no reset (#311's key decision,
  kept). DynamoDB cannot make an `ADD` term conditional, so bypass needs a different
  `UpdateExpression`, and the client must know the mode before it writes. Soft needs no such
  knowledge (§3).

Bypass is **not** "every limit soft": a soft limit leaves debt behind, which a bypassed
privileged entity must not carry into enforcement when the bypass ends.

## 2. Storage

### 2.1 Soft

| Item | Attribute | Meaning |
|---|---|---|
| Config (system, resource, entity) | `l_{name}_soft` = `true` (or `w_{name}_soft` for a `reset_after` limit, ADR-142) | This limit is soft. Absent = hard. Two-state, not tri-state: it is part of the limit definition, and a level that redefines the limit redefines its soft-ness |
| Bucket | `b_{name}_soft` = `true` | Denormalised copy; absent = hard |

- `Limit` gains `soft: bool = False` (all factories pass it through; `to_dict()` / `from_dict`
  round-trip it). Not allowed on the reserved `wcu` limit.
- Verified invisible to old readers: config limit discovery keys on `l_*_cp` / `w_*_cp`
  (`schema.config_limit_names`), bucket discovery on `b_{n}_tk` **with** `b_{n}_cp`
  (`repository.py:6512-6522`); a `b_{n}_soft` or `l_{n}_soft` is ignored by v0.16.
- Limit-name safety: `NAME_PATTERN` allows `_`, but `b_{n}_soft` cannot be mistaken for a
  limit because discovery requires the `_tk`/`_cp` pair; `parse_bucket_attr` splits on the last
  `_`, so `b_x_soft` parses as `(x, soft)`, as `b_x_ws` already does.

### 2.2 Bypass

| Item | Attribute | Values |
|---|---|---|
| Config (resource, entity(resource), entity(`_default_`)) | `disabled` (ADR-125) | absent = inherit; `BOOL true` = disabled; `BOOL false` = explicit enforce (carve-out); **`S "bypass"` = bypass** |
| Bucket | **`bypass`** = `BOOL true` | present only while effectively bypassed |

- **One attribute, one walk, one precedence** (#311 DC1). `resolve_access` (`repository.py:7073`)
  already reads the three walk items in one uncached `BatchGetItem`; it returns an effective
  mode `enforce | disabled | bypass` instead of a bool. A resource `disabled: true` with an
  entity `bypass` admits that entity (carve-out); a resource `bypass` with an entity
  `disabled: true` blocks it.
- **Why the bucket stamp is a separate attribute.** Every client since v0.12 enforces
  `attribute_not_exists(disabled)` on the fast path (`repository.py:3631-3635`). Writing
  `disabled = "bypass"` on a bucket would turn every pre-0.17 client's view of a bypassed
  entity into a 403 outage. A separate `bypass` attribute is ignored by old clients, which then
  simply enforce (§10).
- **Old config readers.** `schema.decode_disabled` returns `bool(attr.get("BOOL", False))`
  (`schema.py:255-258`), so a v0.16 reader sees `S "bypass"` as explicit `false`: it stops the
  walk and enforces. That is the safe reading (never a 403, never an unlimited admit), and an
  entity bypass under a disabled resource still admits on v0.16, as on v0.17.

## 3. The fast path

Today's speculative write (`_speculative_consume_single`, `repository.py:3536`) is
`ADD tk -c, tc +c` per declared limit plus `wcu`, with condition
`attribute_exists(PK) AND tk_i >= c_i … AND wcu_tk >= 1000 AND ttl-guard AND
attribute_not_exists(disabled) AND (attribute_not_exists(vu) OR vu > now)`.

### 3.1 Soft: decided by the server

Each declared limit's term becomes

```
(attribute_exists(#o{i}) OR #t{i} >= :h{i})        -- #o{i} -> b_{name}_soft
```

The `ADD` set is unchanged, so a soft limit is still debited and may go negative. The client
needs **no** knowledge of which limits are soft; the item decides, so a stamp change made
elsewhere takes effect on the very next write (#467 Q2: yes, 0 extra RCU / WCU — and better
than "omit from the condition", which would need the client to know). `#o{i}` is a new
positional token family (#634); it collides with none in `test_expression_tokens.py` (the seed
family is `#s{code}{j}`, codes `t c p a r h x y w g k`).

A soft limit missing from the item (a seed, #633) still fails the condition — `attribute_exists`
and `tk >= c` are both false — and goes to the slow path, as today.

### 3.2 Bypass: a different write, guarded by its own stamp

Bypass shape (warm process, stamp known):

```
ADD tc_i +c_i (declared limits), wcu_tk -1000, wcu_tc +1000   [SET ttl as today]
CONDITION attribute_exists(PK) AND attribute_exists(#byp)
          AND wcu_tk >= 1000 AND ttl-guard AND attribute_not_exists(#disabled)
```

- No `tk` term and no `vu` guard: nothing is spent, so a passed schedule boundary is
  irrelevant; a bypassed entity stays on the fast path across boundaries.
- `wcu` stays hard: it protects the partition, not the tenant. Shard doubling under bypass
  works exactly as today.
- **Learning the mode.** A per-process map `(ns, entity, resource) -> bypassed`, holding only
  positive entries (bypassed pairs are few), learned from the `bypass` stamp on every image a
  write returns (`ALL_NEW` on success, `ALL_OLD` on failure) — the ADR-146 `_cascade_cache`
  pattern. The item overrules the cache:

| Client believes | Item says | What happens | Extra cost |
|---|---|---|---|
| enforce | enforce | today's write | 0 |
| bypass | bypass | bypass write | 0 |
| enforce | **bypass** | enforce write with `(attribute_exists(#byp) OR …)` in every term ⇒ admitted; `ALL_NEW` shows the stamp; **refund** `ADD tk +c` (the existing `build_composite_adjust`, `tc` net 0 not touched); cache learns bypass | +1 WCU once per process per (entity, resource) per transition |
| bypass | **enforce** (cleared) | bypass write fails on `attribute_exists(#byp)`; `ALL_OLD` shows no stamp; cache forgets; retry with the enforce write | +1 WCU (failed) once |

  The enforce write therefore carries `attribute_exists(#byp)` as a third alternative in each
  limit's term, so an enforce-shaped write on a bypassed bucket never produces a 429.
  Rejected alternative: leave that out and let the failure image route the retry — it costs
  the same write but surfaces a spurious failure path through the rejection classifier.
- A refund that lifts `tk` above the ceiling is handled by the existing #679 clamp
  (`build_vu_reset`), unchanged.

### 3.3 Failure classification and fast rejection

`_check_speculative_failure` and `would_refill_satisfy` read the `ALL_OLD` image. They must
skip every limit stamped soft (it cannot have failed), and a `bypass` stamp on the image is
the "learn bypass, retry bypass-shaped" branch above, never a rejection.
`SpeculativeFailureReason` precedence becomes `DISABLED` > bypass-learned > `SCHEDULE_BOUNDARY`
> exhausted reasons.

## 4. The slow path

- **Admission.** `try_consume` / `_admit_limit` skip soft limits when deciding admission but keep
  them in the write set (debit, `tc`). `build_composite_retry`'s `tk >= consumed` condition is
  emitted only for hard limits (#467 Q3); its seed term (`attribute_not_exists(tk) OR tk >= c`)
  likewise.
- **Bypass on the slow path** (bucket missing, seed, cold process after a `BUCKET_MISSING`):
  `resolve_access` already returns the mode uncached on every slow path that needs it, **0 extra
  RCU**. A **create** writes the full item exactly as an enforced create (ADR-145 move or fresh
  grant for quotas, `tk` at the starting share) **without** subtracting this request's
  consumption, adds `tc`, and stamps `bypass`. A normal rf-locked write under bypass is the
  bypass shape plus nothing else: it does **not** advance `rf`, apply a reset, or roll a window.
- **Soft stamps on the slow path.** A create or seed stamps `b_{n}_soft` from config read
  **uncached in that pass** (§6.2). The rf-locked normal write re-stamps (SET or REMOVE)
  `b_{n}_soft` for every limit on the item only when that pass's limits were read uncached —
  ADR-146's "stamp only when the config fetch read fresh" rule — so a wrong stamp left by an old
  client self-heals on the next fresh slow pass.

## 5. Leases (#455 declared scope)

- Declared scope is unchanged by both. A soft limit named in `consume` is adjustable; an
  undeclared soft limit is write-only, as any undeclared limit is.
- `adjust()` / `consume()` / `release()` / rollback on a **soft** limit: today's
  `build_composite_adjust` (`ADD tk ∓d, tc ±d`), unchanged — debt allowed by design.
- Under **bypass** the lease's mode is fixed at admission. Adjust, release and rollback write
  `tc` only (a `build_composite_adjust` variant with no `tk` term). If bypass is cleared
  mid-lease the lease still writes `tc` only: the tokens it never debited are not owed.
- New read-only `Lease` surface: `bypassed: bool`, and `overdrawn` — the declared soft limits
  whose balance, on the shard written, is below zero after the admission write (from `ALL_NEW`
  or the slow path's in-memory state, **0 cost**).

## 6. Propagating a change

### 6.1 Bypass

Identical to ADR-125 (#311 DC3): config write, then the two-pass fan-out (GSI2 for a resource,
GSI3 for an entity) SETs or REMOVEs `bypass` on each bucket from the mode resolved for that
bucket's own (entity, resource); a bucket going from bypass to disabled gets both changes in
one write. `delete_limits()` / `delete_resource_defaults()` re-run it against the newly resolved
mode when the deleted item carried a value. The ADR-125 in-flight race and its second pass
apply unchanged. Provisioner mirror: `zae_limiter_provisioner/fanout.py` (#311 DC6), with the
existing rule that re-asserting an unchanged resource value never clobbers an entity carve-out.

### 6.2 Soft

| Level changed | Today's propagation of a limit change | Soft-ness propagation |
|---|---|---|
| Entity (`set_limits` / `delete_limits`) | `_sync_bucket_params` fans out to every shard (#468, #487) | Same write gains `SET`/`REMOVE b_{n}_soft` per limit, resolved per bucket. **0 extra writes** |
| Resource (`set_resource_defaults`) | none — bucket TTL (#271, #296) | **New change-only fan-out**: the config `PutItem` asks for `ALL_OLD` (as ADR-146 does in the provisioner); only if some limit's soft-ness changed, discover by GSI2 and SET/REMOVE `b_{n}_soft` on each bucket whose resolved limit is the resource's (an entity override of that limit is left alone) |
| System (`set_system_defaults`) | none — bucket TTL | Same, discovering by GSI4 (`GSI4PK={ns}`, `GSI4SK begins_with BUCKET#`) |

TTL propagation is not enough for soft: a **soft → hard** change left to TTL keeps admitting
without limit for `max_recovery × 7` — seven days for a daily quota. Hard → soft lagging is
only under-admission, but the same fan-out serves both. A partial failure raises
`FanoutIncomplete`, like every fan-out. The writes are `SET`/`REMOVE` of one attribute under
`attribute_exists(PK)`; they move no `tk`, `rf`, `gc` or `shard_count`.

**Stale config cache on create.** Bucket `cp`/`ra` on create come from the config cache today,
and a stale cached capacity persists on an entity bucket (no TTL) until the next sync — an
accepted, bounded staleness. A stale **soft** stamp is unbounded over-admission, so a create or
seed must take soft-ness from config read uncached that pass. The slow path already does an
uncached `resolve_access` read of the three walk items whenever the cache served a level; it
must also include the system config item when any limit on the bucket resolved from the system
level (+0.5 RCU, creates and seeds only).

## 7. Quotas, session windows, sharding (ADR-137/139/145)

- **Soft quota.** Debited and may go into debt within the period; the reset or roll is a `SET`
  to the share, so it **forgives the debt** and restores the allowance. That is the metering
  semantics ("you went 3,000 over this week"); the overdraw signal (§9) is where the overage is
  recorded. ADR-145 grant logic is unchanged: a donor in debt holds `tk ≤ 0`, so it moves
  `min(C // S, max(tk, 0))` = 0, as today. I8 reads "allowance created per period = C";
  admissions may exceed it by the overdraw, which is the point.
- **Soft session window.** A soft admission is an admitted pass and anchors a window exactly as
  a hard one.
- **Bypass.** Never opens, joins or rolls a session window and never applies a calendar reset
  (it does not move `rf`, `ws` or `wa`). When bypass is cleared, the first enforced pass
  applies any pending reset and opens a window, as for an idle entity. A bucket **created**
  under bypass is funded by the ADR-145 move or fresh grant like any create, and records `gc`;
  it leaves `ws` absent. Funding on create keeps I1–I8 intact without a special case.
- **Debt is per shard.** A sharded entity's soft limit can be overdrawn on one shard while
  siblings hold tokens; `overdrawn` and the snapshot counter (§9) are per shard. Accepted.
- **Aggregator.** Refill of a soft limit in debt is the ordinary `refill_bucket` arithmetic.
  Under bypass `tk` never drops, so the refill computes a zero delta and writes nothing — it
  does not fight the stamp. Path 2 clones copy shard 0's item (`base_item`, `processor.py:1881`),
  stamps included; v0.16 aggregators do the same. Proactive sharding is unchanged (`wcu` is
  still debited).

## 8. Cascade

Each entity resolves both flags for itself (#467 Q6, #311 DC5), mirroring ADR-125's "carve-outs
do not extend to parents":

| Child | Parent | Result |
|---|---|---|
| soft `tpm` | hard `tpm` | Child never rejects on `tpm`; the parent can. Mark the parent's limit soft (entity or resource level) for non-gating org metering. A resource-level soft applies to both, since both resolve it |
| bypass | enforce | Child bypass-shaped write, parent's normal write. Parent rejects ⇒ child's `tc` is compensated (`tc` only), no admission |
| bypass | disabled | `ResourceDisabled(entity_id=parent)` |
| enforce | bypass | Child enforced; parent bypass-shaped |

The warm parallel path issues the child and parent writes in their own shapes, each from its
own cache entry.

## 9. Reporting and the overdraw signal

**Statuses** (#467 Q4). `LimitStatus` gains `soft: bool`. For a soft limit `exceeded` is always
`False` and `retry_after_seconds` 0.0; `available` may be negative. A derived
`overdrawn = soft and available < 0`. In `RateLimitExceeded` a soft limit appears in `passed`,
never in `violations`, and the bottleneck `retry_after_seconds` ignores it. `as_dict()` emits
`"soft": true|false` on every entry (additive key, like `kind` in #545). `Availability.allowed`
and `deficit` ignore soft limits; `Availability` gains `bypassed: bool` from the stamp it
already reads.

**Overdraw signal** (#467 Q5). Two, both free:

| Signal | Where | Cost |
|---|---|---|
| `Lease.overdrawn` | Client, synchronous, from the admission image | 0 |
| Usage snapshot counter `{limit}#od` | Aggregator: per stream record where a soft limit's `tc` rose and its NewImage `tk` < 0, `ADD {limit}#od :1` in the **same** snapshot `UpdateItem` | 0 extra WCU (one more `ADD` term) |

`#` cannot occur in a limit name (`NAME_PATTERN`, `models.py:33`), so the counter cannot collide.
New readers expose it as `UsageSnapshot.overdrawn: dict[str, int]`; a v0.16 reader's
`_deserialize_usage_snapshot` (`repository.py:6166-6186`) shows it as one extra counter —
cosmetic. A CloudWatch EMF metric from the aggregator is deferred (the aggregator emits no
metrics today; EMF needs no IAM change and can be added later). An `AuditEvent` per overdraw is
rejected (a write per overdrawn request).

## 10. Rejection cache (ADR-147)

- `_known_short_shards` / `_shortfall` must skip soft limits (from the cached image's
  `b_{n}_soft`), and must never reject from an image stamped `bypass`.
- All new admin writes are `@clears_rejection_cache`.
- A change made **elsewhere** is seen at most `rejection_cache_ttl` late: hard → soft or a new
  bypass can be locally rejected for up to 1 s (under-admission, the ADR-147 trade); the cache
  never admits, so soft → hard and a cleared bypass are enforced immediately.
- `tests/unit/test_rejection_cache.py::TestChangesElsewhere` gains cases for both.

## 11. Surfaces

| Surface | Soft | Bypass |
|---|---|---|
| Python | `Limit.*(…, soft=True)`; existing setters | `bypass_resource(r)`, `bypass_entity(id, resource=None)` on `Repository` (return bucket-write count); `clear_*_disabled()` clears it; `disabled="bypass"` on `set_resource_defaults` / `set_limits` |
| Getters | `Limit.soft` in `get_*` results | `get_resource_defaults` / `get_limits` expose the explicit value |
| CLI | `--soft NAME` (repeatable) on `system set-defaults`, `resource set-defaults`, `entity set-limits`; `get-*` prints `(soft)` | `resource bypass NAME`, `entity bypass ID [--resource R]`; `get-*` prints `Status: BYPASSED` |
| Manifest | `soft: true` under any `limits.<name>`, every level | `disabled: bypass` on `resources.<name>` and `entities.<id>.resources.<name>`; rejected on `system` (#693) |
| CloudFormation | `Soft` in `_CFN_LIMIT_OPTIONAL_KEYS` (string-coerced, #554) | `Disabled: bypass`, through a widened `_coerce_bool` |
| Audit | limit change events carry the flag | bypass set/clear events (ADR-106 entity ids) |
| `SyncRepository` / `SyncRateLimiter` | generated | generated |

`-l` cannot express soft and a set is a full replace: `entity set-limits … -l tpm:…` without
`--soft tpm` turns a soft limit hard — the same sharp edge as schedules today, documented.

## 12. Version gate (ADR-141 machinery)

Who misreads what:

| Old component | Soft | Bypass | Direction |
|---|---|---|---|
| Client v0.15–0.16 | ignores `b_{n}_soft` ⇒ enforces; creates buckets with no stamp ⇒ new fast path enforces until healed | ignores `bypass` ⇒ enforces and debits `tk` (debt carried into enforcement) | under-admission |
| Client v0.14 | same; ignores `client_min_version` | same | under-admission |
| Admin full-replace write (old) | drops `l_{n}_soft` ⇒ hard | `disabled` unaffected unless rewritten | under-admission |
| Provisioner < 0.17 | full-replace apply erases `soft`; cannot parse `disabled: bypass` | erases or rejects bypass | under-admission / failed apply |
| Aggregator < 0.17 | refill unaffected; clone copies stamps | unaffected | none |

Every old-reader failure **enforces**. The gate is therefore not about safety but about the
provisioner erasing the flag on its next apply (ADR-146's reason) and about old clients
silently stamping un-soft buckets. Recommendation: `_require_readers` at
`MIN_READER_VERSION_FOR_NON_ENFORCING = 0.17.0` for any write that sets a soft limit or a bypass
value, and ratchet `client_min_version` to 0.17.0 — free when neither is declared, one strongly
consistent `GetItem` (1 RCU) otherwise.

## 13. Cost

$0.625/M WCU, $0.125/M RCU (on-demand, us-east-1).

| Operation | Today (hard, enforce) | Soft | Bypass |
|---|---|---|---|
| Speculative success | 0 RCU + 1 WCU = $0.625/M | 0 + 1 = **$0.625/M** | 0 + 1 = **$0.625/M** |
| Exhausted (would 429) | 1 WCU ($0.625/M); repeat in TTL $0 | admitted: 1 WCU | admitted: 1 WCU |
| Cascade, both succeed | 0 + 2 WCU = $1.25/M | same | same |
| First request per process after bypass set | — | — | +1 WCU refund, once per (process, entity, resource) |
| First request after bypass cleared | — | — | +1 failed WCU, once |
| Slow-path create | 2.5 RCU + 2 WCU (ADR-133) | +0.5 RCU when a limit resolves from system | same as today |
| Adjust / release / rollback | 1 WCU each | same | same (`tc` only) |
| Overdraw signal | — | 0 client; 0 extra aggregator WCU | — |
| Bypass set/clear | — | — | like disable: 2 KEYS_ONLY discovery passes + 1 WCU per bucket. 10,000 single-shard buckets ≈ 10,000 WCU ≈ **$0.006** |
| Soft change, entity level | param sync (O(buckets) WCU) | **0 extra** | — |
| Soft change, resource level | none | GSI2 discovery + per distinct entity ≤ 2 consistent config reads + 1 WCU per changed bucket. 10,000 entities ≈ 20,000 RCU + 10,000 WCU ≈ **$0.009** | — |
| Soft change, system level | none | GSI4 discovery over the whole namespace, then as above | — |

Item size: `b_{n}_soft` ≈ 15 B per soft limit; `bypass` ≈ 10 B. No WCU change unless an item
crosses 1 KB (ADR-135 §4.2 headroom: 178 B in the worst shared case).

## 14. Writers (registry and token tests)

New or changed bucket writers, each to be declared in `tests/unit/test_bucket_writer_registry.py`
and checked in `tests/unit/test_expression_tokens.py`:

| Writer | Writes `tk`? | Writes `gc`? |
|---|---|---|
| Speculative consume, enforce shape (+ `#o{i}`, `#byp` terms) | yes (unchanged) | no |
| Speculative consume, bypass shape | **no** | no |
| Bypass adjust / rollback (`tc` only) | no | no |
| Bypass-learned refund (existing adjust) | yes (credit) | no |
| Slow path create / seed / normal (+ `b_{n}_soft`, `bypass`) | as today | as today |
| Bypass fan-out (`SET`/`REMOVE bypass`) | no | no |
| Soft fan-out, resource/system (`SET`/`REMOVE b_{n}_soft`) | no | no |
| Param sync (+ `b_{n}_soft`) | no | no |

## 15. Test plan

- **Unit, soft:** fast path admits an exhausted soft limit and debits it; a hard sibling still
  rejects; failure classification ignores soft; slow path and retry admit; statuses (`soft`,
  `exceeded=False`, `overdrawn`, `passed` not `violations`, `retry_after_seconds` ignores it);
  `as_dict()`; rejection cache never rejects on soft; soft quota reset forgives debt; ADR-145
  fuzz (`test_quota_conservation_fuzz.py`) with soft quotas, I8 restated as allowance created.
- **Unit, bypass:** walk matrix (bypass at each level, carve-outs both ways); warm path leaves
  `tk` untouched and adds `tc`; the four rows of the §3.2 table, including the refund; `wcu`
  exhaustion under bypass doubles shards; adjust / release / rollback touch `tc` only; no window
  anchored, no reset applied; create funded per ADR-145; cascade matrix (§8).
- **Unit, propagation:** entity sync stamps soft; resource and system change-only fan-out
  (fires only on a soft change; leaves entity overrides alone; `FanoutIncomplete`); bypass
  fan-out and delete re-resolution; uncached soft stamp on create when the cache is stale
  (another `Repository` changes it).
- **Unit, version gate:** refusal below 0.17.0, pass on equal dev build, ratchet; free when
  undeclared.
- **Provisioner:** manifest `soft` and `disabled: bypass` parse, round-trip through
  `Custom::ZaeLimiterLimits`, `system.disabled: bypass` rejected, fan-out preserves carve-outs.
- **Aggregator:** overdraw counter written in the same snapshot update; no refill write for a
  bypassed bucket; Path 2 clone carries stamps.
- **Capacity:** `tests/benchmark/test_capacity.py` — soft success 0 RCU + 1 WCU; bypass warm
  0 RCU + 1 WCU; transition costs as in §13.
- **Integration (LocalStack):** stamps on every shard; usage snapshots for a bypassed entity
  equal real consumption (#179); `attribute_exists` OR terms accepted by LocalStack and moto.
- **AWS e2e (release run):** one soft and one bypass acquire on real DynamoDB.
- Generated sync tests follow.

## 16. Issue questions, answered or carried

| Question | Answer |
|---|---|
| #467 Q1 Where the flag lives | `l_{n}_soft` on config, `b_{n}_soft` denormalised (§2.1) |
| #467 Q2 Fast-path condition | Server-side `attribute_exists(soft) OR tk >= c`, `ADD` unchanged, `wcu` hard (§3.1) |
| #467 Q3 Slow path / retry | Admission skips soft; retry condition only on hard limits (§4) |
| #467 Q4 Reporting | `LimitStatus.soft`, derived `overdrawn`, in `passed` (§9) — Open decision D7 |
| #467 Q5 Overdraw signal | `Lease.overdrawn` + snapshot counter; EMF deferred (§9) — D6 |
| #467 Q6 Cascade | Independent per entity (§8) — D5 |
| #467 Q7 Resolution | Travels with the limit (§1) — D1 |
| #311 DC1 One tri-state | Widen `disabled`; separate bucket stamp (§2.2) — D2 |
| #311 DC2 Fast path | Learned stamp + guarded bypass write + refund (§3.2) — D3 |
| #311 DC3 Fan-out | ADR-125 fan-out unchanged (§6.1) |
| #311 DC4 #455 | Declared scope unchanged; `tc` only (§5) |
| #311 DC5 Cascade | Independent (§8) |
| #311 DC6 Provisioner | Mirrored; carve-outs preserved (§6.1, §11) |
| #311 DC7 System-level block | Out of scope — D9 |

## Open decisions

**D1. How soft-ness resolves.**
(a) A property of the limit, travelling with it (override-not-merge). (b) An independent
tri-state walk like `disabled`. (c) Per-call only (rejected in #455).
**Recommend (a):** the flag modifies one limit's definition; under (b) an entity's own hard
`tpm` would inherit a resource's soft flag it never wrote.

**D2. How bypass is stored.**
(a) Widen the `disabled` config value (`S "bypass"`) and stamp buckets with a separate
`bypass` attribute. (b) A sibling `bypass` config attribute read in the same walk. (c) Reuse the
bucket `disabled` stamp.
**Recommend (a):** one value per level means no precedence question (#311 DC1); a v0.16 reader
reads it as explicit enforce, which is safe; (c) would turn bypass into a 403 on every old client.

**D3. How the fast path knows it is bypassing.**
(a) Per-process cache learned from returned images; bypass write guarded on the stamp; refund on
a stale enforce guess. (b) Always debit `tk`, admit by condition, reset `tk` when bypass is
cleared. (c) Read config on the fast path.
**Recommend (a):** warm cost 0 RCU + 1 WCU and `tk` exact; (b) puts a `SET tk` in the clear
fan-out, which breaks ADR-145 conservation for quotas; (c) adds a read to every call.

**D4. How a resource- or system-level soft change reaches buckets.**
(a) Change-only fan-out (GSI2 / GSI4). (b) Bucket TTL, as other resource/system changes.
(c) Allow soft only at entity level.
**Recommend (a):** (b) admits without limit for up to seven reset periods after soft → hard;
(c) loses the shadow-rollout use case, which is a resource-level change by nature.

**D5. Cascade.**
(a) Each entity resolves for itself; parents are never relaxed by a child. (b) A child's bypass
also bypasses its parent write.
**Recommend (a):** matches ADR-125 and ADR-146; a privileged child that must also skip the org
cap can be given `cascade: false` on that resource (ADR-146) or the parent can be bypassed.

**D6. Overdraw signal.**
(a) `Lease.overdrawn` plus a usage-snapshot counter, both free. (b) Add a CloudWatch EMF metric
from the aggregator now. (c) An `AuditEvent` per overdraw.
**Recommend (a), with (b) as a follow-up:** no new infrastructure; (c) is a hot-path write per
overdrawn request.

**D7. How statuses report soft limits.**
(a) `LimitStatus.soft` + derived `overdrawn`, soft limits in `passed`, `"soft"` on every
`as_dict()` entry. (b) A separate `overdrawn` list on the exception and lease.
**Recommend (a):** one model, additive keys, consistent with `kind` (#545).

**D8. Version gate.**
(a) Gate at 0.17.0 and ratchet `client_min_version` (ADR-146). (b) Gate the Lambdas only, no
ratchet, since every old client fails toward enforcement. (c) No gate.
**Recommend (a):** the provisioner must not erase the flag, and v0.15–0.16 clients would keep
creating un-stamped buckets; consistent with the last two gates. (b) is defensible if the owner
prefers not to lock out v0.15–0.16 clients for a fail-safe feature.

**D9. System-level block and system-level bypass (#311 DC7).**
(a) Out of scope; file a separate issue if a namespace kill switch is wanted. (b) Add both here
with GSI4 fan-out.
**Recommend (a):** ADR-125 and #693 rejected system-level `disabled`; reversing that is its own
decision with its own blast radius.

**D10. Public names for bypass.**
(a) `bypass_resource()` / `bypass_entity()`, `disabled="bypass"`, manifest `disabled: bypass`.
(b) A new `access` keyword / enum (`enforce | disabled | bypass`) everywhere, deprecating
`disabled=`.
**Recommend (a) for v0.17.0:** matches ADR-125's surface and #311's acceptance criteria; (b) is a
cleaner name but a rename across API, CLI, manifest and CFN for no behavioural gain.

**D11. CLI spelling of soft.**
(a) A repeatable `--soft NAME` flag. (b) A suffix in the `-l` grammar (`-l tpm:50000:soft`).
**Recommend (a):** `-l`'s third field is already burst; overloading it is ambiguous.

**D12. A soft quota's debt at reset.**
(a) Forgiven: the reset `SET`s the share (today's mechanics). (b) Carried into the next period.
**Recommend (a):** (b) needs a new reset write shape and makes a soft quota gate nothing yet
penalise the next period; the overdraw counter keeps the record.
