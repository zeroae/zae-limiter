# ADR-147 phase 3 withdrawn: refill from the cached state

**Date:** 2026-10-07 · **Decision:** owner · **Shipped in:** never (removed before v0.16.0)
**Code as it stood:** `main` at `b4bf36a3d` (merged by #700, removed by the PR that adds this file)

## TLDR

Phase 3 admitted a request through **one conditional write built from a cached bucket
state**, instead of today's failed speculative write, read and locked write. It was
removed before release because:

1. **Ten over-admissions** were found in it across four reviews; nine were fixed, the
   tenth was found by the pre-release review. Each came from a different writer or
   input the condition did not pin. There is no argument that the list is complete.
2. **Its saving is small and narrow.** Phase 1 (local rejection) delivers 88–99% of
   ADR-147's saving at 20x over-demand and above. Phase 3 matters mainly at ~2x
   over-demand and on sharded entities, and it **costs 9% more** than v0.15.1 with 50
   processes on one hot entity.
3. It imposed a standing rule on every future bucket writer (CLAUDE.md invariant 11):
   change a pinned attribute, or phase 3 admits against a state it cannot see.

Phases 1 and 2 stay. They can only reject (or steer a write to another shard); no
review found an over-admission in either.

## What phase 3 did

When a cached shard's **stored** balance could not cover a request but its projection
to now could, the client sent `build_composite_normal`'s rf-locked refill-and-debit,
computed from the cached state, with extra condition terms meant to make it fail if
anything had changed since the state was seen. By the end the condition carried:
`rf = :cached_rf`, `vu` absent, not `disabled`, TTL unexpired, `cascade` absent or
false, `shard_count = :cached`, and per limit `tk <= :cached`, the floor
`tk >= consumed − refill` at any sign, and `cp`/`ra`/`rp` equal to the cached ones.
It also ran only when: the bucket stamp named a parent or an **existing** META record
said there was none; a real slow pass had re-read config for that bucket within
`config_cache_ttl`; the warm config cache showed no schedule, reset or window and every
configured limit on the item.

Every one of those terms and gates exists because of a reproduced over-admission.

## The over-admissions

All reproduced on moto with a second `Repository` (or a raw `update_item`) standing in
for another process, comparing phase 3 on against the same scenario with the cache off.

| # | Writer or input the condition could not see | Reproduction | Result with phase 3 | Fixed by |
|---|---|---|---|---|
| 1 | Another process turns the child's **cascade policy** on (ADR-146): stamps `cascade` without moving `rf` or `vu` | Child cached; `set_entity_cascade` elsewhere; child acquires | Child admitted, **parent never debited** | `148b5a11c` (pin `cascade` off) |
| 2 | A **pre-#684 stamp** (`cascade=False`, no `parent_id`) on a cascading child | Child created by an older client; warm acquires | Phase 3 kept pre-empting the slow pass that repairs the stamp; parent never debited | `148b5a11c` (trust "no parent" only from META) |
| 3 | A **refund / release / rollback / compensation** in another process: `ADD` tokens, `rf` unchanged | Cached at tk 0; refund elsewhere; refill clamped against the lower cached balance | **3 admitted against capacity 2**, item above its ceiling | `7fa70d1e5` (pin `tk <= cached`) |
| 4 | A **shard doubling** elsewhere (client bump, propagation, aggregator Path 1): `shard_count` set, `rf`/`vu` unchanged | Cached at count 1; doubled to 2 elsewhere | Shard 0 refilled to its full **new** share instead of empty | `9922e5d37` (pin `shard_count`) |
| 5 | A **resource- or system-level schedule**: never fans out to buckets (#271/#296), so the item carries no `vu` | Resource schedule halves capacity; entity bucket unscheduled | **4 admitted against a scheduled capacity of 2** | `1dd694621` (ask the warm config cache, `peek_limits`) |
| 6 | An entity **created later under a parent** (here or elsewhere) after its bucket existed | Acquire before `create_entity`; then `create_entity(parent_id=...)` elsewhere | **4 admitted where the parent allowed 2**, parent never debited | `d9bb0f79a` (trust "no parent" only from an existing record) |
| 7 | A **disable stamp the fan-out missed** (ADR-125 race, `FanoutIncomplete`, pending provisioner retry) | Resource disabled elsewhere; one bucket's stamp removed | **Admitted on a disabled resource, indefinitely** (phase 3 replaced every slow pass) | `7f230e529` (only within one config window of a real slow pass) |
| 8 | A **debit elsewhere smaller than the refill**: the normal write's floor is emitted only when the debit exceeds the refill | Drained; another process `ADD -1000`; 0.9 s later request 1 (refill 9) | **1 admitted with the item at −1000** (−992 after) | `7cd4cdc6c` (floor at any sign) |
| 9 | A **limit cut** (param sync: `cp`/`ra`/`rp` + `vu = 0`, no `rf`), then a slow pass whose clock is behind the stored `rf` clears `vu`; or the cut lands inside a slow pass and the recorded state keeps the old settings | 1000/min cut to 10/min; skewed slow pass; request 400 | **400 admitted against 10/min** | `e26f74512` (pin `cp`/`ra`/`rp`) |
| 10 | A **declared limit consumed at 0**: the floor loop skipped `c <= 0`, so another process's debt on it was invisible. This is the zero-estimate LLM pattern (`consume={"rpm": 1, "tpm": 0}`, `adjust(tpm=...)` later) | rpm 600/min, tpm 600k/min; slow pass; fast path drains rpm; +900 ms; other process `ADD b_tpm_tk -10e9`; acquire `{"rpm":1,"tpm":0}` | **Admitted with tpm at −9.4e9**; cache off rejects | **Not fixed** — found by the pre-release review and by its randomized fuzzer (seed 19) |

Related, not over-admissions: the slow path's own write erasing a concurrent `vu = 0`
(#701, a bug on `main` before ADR-147, **kept fixed**); phase 3's lost writes under the
aggregator (119 of 597 at 2x, net saving still positive); and under-crediting near
capacity after a concurrent debit (accepted, never over-admits).

## The cost case

From the pre-release cost review (moto, request units converted at on-demand prices;
admissions equal across configurations unless noted):

| Scenario ($ per million requests) | v0.15.1 | Phase 1 only | Phases 1–3 |
|---|---|---|---|
| Steady under the limit | .719 | .719 | .628 (capacity-10 artefact; ~0.1% at capacity 1,000) |
| Over-demand 2x | 1.093 | .783 | .316 |
| Over-demand 20x | .672 | .079 | .033 |
| Over-demand 200x | .630 | .037 | .033 |
| Sharded 8, 1.2x | 2.307 (547 admitted) | 1.255 (924 admitted) | .515 |
| 50 processes, 20x | .674 | .674 | **.733 (+9%)** |
| Entities that never call `create_entity`, 2x | 1.093 | 1.093 | 1.093 |

Phase 3's marginal saving was about $0.94 per million phase-3 admissions. Its lost
writes with many processes come from the same cause as finding 9's guard: each
process's cached state, up to 60 s old, carries a stale `rf` (112 of 112 lost at 50
processes; 0 of 59 at 10).

"Phase 1 only" in the table **includes** two fixes that shipped inside the phase-3 PR
and are kept, because phase 1 depends on them:

- `866f72cd3` — the slow path records the state its rf-locked write left. Without it
  the cache held the failed write's image with the pre-write `rf`, and phase 1 saved
  **nothing** at 2x.
- `868f2c34c` — it does not record a state a refund in this process overtook (a mark
  taken before the read; under-admission otherwise).

## What a phase 3 would need to come back

Write a new ADR. At minimum:

1. **An argument that the condition is complete**, not a list grown by review: every
   input to the slow path's admission decision (each limit's balance, settings,
   schedule, reset, window, the entity's parent and cascade policy, `disabled`, TTL,
   shard count, every declared limit including those consumed at 0) mapped to a
   condition term or to a gate with a stated bound — and every bucket writer in the
   CLAUDE.md writer table checked against it.
2. **A randomized differential fuzzer in CI** comparing admissions with the feature on
   and off across several `Repository` objects on one table, with raw "other process"
   credits, debits, admin changes and clock skew. The pre-release review's fuzzer found
   finding 10 independently; findings 1–9 were all found by hand.
3. **An answer to the many-processes cost regression** (stop after a lost write, or
   bound the state's age by more than the slow-pass window).
4. A measured saving that justifies it against phase 1 alone.

## Sources

- Reviews of #700: the phase-3 review (findings 1–5), its verification (6–7), the
  option-A test (8), the final review of #700 (9), the pre-release reviews A–D on
  2026-10-07 (10, and the cost table).
- Commits: `git log 845a8c3a1..2422a0642^2` (the #700 branch).
