# ADR-149: Reset and top-up are in-place, rf-locked passes over every shard

**Status:** Accepted
**Date:** 2026-10-09
**Issue:** [#470](https://github.com/zeroae/zae-limiter/issues/470)
**Amends:** [ADR-145](145-sharded-quota-conserves-allowance.md) (I1, I7, I8: a top-up is an operator grant above the plan), [ADR-139](139-duration-reset-windows.md) (a session top-up with no live window opens one at the top-up instant: an operator action, not an admitted request, anchors it)
**Related:** [ADR-125](125-resource-disable.md), [ADR-137](137-reset-replaces-drip.md), [ADR-139](139-duration-reset-windows.md), [ADR-140](140-duration-window-shard-coherence.md), [ADR-141](141-reset-after-version-gate.md), [ADR-145](145-sharded-quota-conserves-allowance.md), [ADR-147](147-client-side-rejection-cache.md), [#471](https://github.com/zeroae/zae-limiter/pull/471), [#674](https://github.com/zeroae/zae-limiter/issues/674)

## Context

The #674 use case needs a plan upgrade, a purchase of more allowance, or a session reset to
take effect **immediately** for one (entity, resource). Today nothing does that: `set_limits()`
changes a bucket's parameters but never adds tokens, so an upgraded quota (which does not drip,
ADR-137) keeps its old remaining balance until the next reset, and a purchase has no
representation at all.

PR #471 resets by deleting the bucket items. That loses the consumption counter the aggregator
diffs (#179), the `disabled` stamp, `shard_count` and the ADR-145 grant record, and an in-flight
`adjust()` (an unconditional `ADD`) recreates a skeleton item with no parameters.

Three facts about the current code constrain an in-place write. Every refill clamps a balance to
its ceiling, which for a quota is `C // gc`, so a credit above the ceiling is trimmed by the next
pass of any writer. `rf` is one attribute per item, so a write that moves it without materialising
every limit on the item skips their refill and any pending reset edge. And a sharded quota holds
its allowance in slices whose sum ADR-145 conserves, so a credit to one shard is a mint unless it
is counted.

## Decision

Reset and top-up of an (entity, resource) must be in-place, `rf`-locked passes over every
existing shard in one transaction, recorded by one operation record per call. A quota may be
topped up above its ceiling only through `b_{q}_tu`, which ends with the period and is gated at
0.17.0.

## Consequences

**Positive:**
- Plan upgrades, purchases and session resets take effect on the next request, on every shard.
- Each shard write is a zero-consumption slow-path pass: every limit materialised, the named
  ones changed by a token delta (never a set), `tc`, `disabled` and the parent untouched, and
  the shard's next acquire forced through one materialising pass. Nothing the item carries is
  lost; the fast path stays 0 RCU + 1 WCU and reads nothing new.
- The operation record (put only if absent, in the same transaction) lets exactly one write per
  id commit and resolves an unclear failure; a caller may supply the id as an idempotency key.
- A reset starts a new period: a quota's grant is recorded at the planned count; a session
  window is marked ended and applied, its balance left for the next opener to restore.
- A quota top-up adds exactly N. A dripping limit's is capped at its ceiling: it has no period
  to bound extra headroom, so raising its rate is `set_limits`' job.
- ADR-145 is amended, not replaced: a top-up is an operator grant above the plan, so I1 gains
  it as a source, I7's ceiling becomes `C // gc + tu`, and I8 reads admitted plus held plus
  still-grantable equals the capacity plus the top-ups granted.
- The aggregator and concurrent slow paths are excluded by the `rf` lock they already honour;
  older clients and aggregators, which would clamp `tu` away, by the ADR-141 gate.
- Imperative only (no manifest or CloudFormation surface); audited; this process's rejection
  cache (ADR-147) is cleared. Details and costs: `docs/plans/2026-10-09-reset-and-top-up-design.md`.

**Negative:**
- Cost is O(shards) reads and two write units per shard, plus two for the operation record
  and two for the audit event, which ride in the same transaction so a grant and its audit
  commit together: even a one-shard operation is a three-item transaction (6 WRU where one
  `UpdateItem` and a separate audit `PutItem` were 2), against one delete per shard in #471.
- Without an idempotency key, an error from a reset or top-up leaves its outcome unknown to the
  caller; the operation record lives three days.
- `reset_bucket` / `top_up` are required `RepositoryProtocol` members, not capability-gated (ADR-109), and their audit event rides in the same transaction, so a backend that implements them must also write audit events (ADR-108 lists audit as optional elsewhere). A top-up that creates the bucket takes its bypass and soft stamps from config read uncached (ADR-151 decision 4).
- A record is tied to the entity's incarnation (#716's `pinc`), read before the write rather
  than checked inside it: an entity deleted and recreated during one call can see a key retry
  applied once more. Closing it would put a `ConditionCheck` on META in every reset and
  top-up (+2 WCU a call); v0.17.0 grants `dynamodb:ConditionCheckItem` (ADR-150), but its
  check also pins the parent generation, so every `set_parent` would cancel resets in flight.
  Left open. A top-up that creates the bucket already carries ADR-150's check.
- After a plan upgrade a quota's ceiling is at most one top-up wider than the entitlement until
  the next reset; it under-clamps refunds, never mints.
- A conflicting writer on any shard aborts the whole transaction; the operation retries a
  bounded number of times and then fails.
- A topped-up balance can sit on shards a request does not draw, so a draw can reject while
  the entity holds purchased tokens elsewhere, as ADR-145 already accepts for quotas.
- Another process's rejection cache can refuse for up to its TTL after a reset elsewhere.
- Reset and top-up are operator actions by contract. IAM cannot stop a principal that may
  acquire from writing its own buckets, so an application can grant itself allowance; a policy
  can only keep it out of the library's calls, not off the items.
- Every reset and roll writer, client and aggregator, gains one attribute to clear, and ADR-145's
  conservation fuzz test and writer registry gain a term.
- A credit landing between the read and the write can lift a balance above its target until the
  forced materialising pass clamps it.

## Alternatives Considered

- **Delete the bucket items (#471):** loses the consumption counter, `disabled`, `shard_count`
  and the grant record, and in-flight adjustments recreate a corrupt item.
- **Set the balance without materialising the item:** moving `rf` skips other limits' refill
  and pending reset edges; not moving it lets a stale aggregator refill land on top.
- **Top-up capped at the ceiling only:** cannot express a purchase beyond the plan.
- **Purchase as a raised configured capacity:** persists into later periods and needs a second
  admin write to undo.
- **Independent per-shard writes:** a partial reset leaves overlapping quota grants, which
  over-admits.
