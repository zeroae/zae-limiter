# ADR-149: Reset and top-up are in-place, rf-locked passes over every shard

**Status:** Proposed
**Date:** 2026-10-09
**Issue:** [#470](https://github.com/zeroae/zae-limiter/issues/470)
**Related:** [ADR-125](125-resource-disable.md), [ADR-137](137-quota-limits-reset-not-drip.md), [ADR-139](139-duration-reset-windows.md), [ADR-140](140-duration-window-shard-coherence.md), [ADR-141](141-reset-after-version-gate.md), [ADR-145](145-sharded-quota-conserves-allowance.md), [ADR-147](147-client-side-rejection-cache.md), [#471](https://github.com/zeroae/zae-limiter/pull/471), [#674](https://github.com/zeroae/zae-limiter/issues/674)

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

A reset or top-up of an (entity, resource) must be written in place, never by deleting items,
as one transaction over every existing shard of that pair. Each shard write must be a
zero-consumption slow-path pass: it materialises every limit on the item as an acquire would,
applies the operation to the named limits as a token delta (never a set), never changes the
consumption counter, `disabled` or the parent's buckets, is conditioned on the shard's `rf`
and `shard_count` as read, and forces the shard's next acquire through one materialising pass.
Each call must carry one operation id, stamped on every shard it writes, so that a write whose
response was lost is recognised as landed rather than applied a second time, and nothing may
fail the call after the write commits.

A reset restores each named limit to its full effective share and starts a new period: a quota
records the grant at the planned shard count; a session quota's current window is marked
ended and applied, never removed, so the next admitted request opens one and no in-flight
rollover can re-apply the old one, and its balance is left for that opener to restore — a share
written into an ended window can be spent twice. A top-up of a quota adds exactly N across the shards and must
be recorded per shard as allowance above the ceiling for the current period, cleared by the next
reset or window roll; a top-up of a dripping limit is bounded by its ceiling. A top-up above the
ceiling must pass the ADR-141 version gate at 0.17.0 and ratchet `client_min_version`, because
an older client or aggregator clamps the balance and destroys it.

The operations are imperative and have no manifest or CloudFormation surface. They clear this
process's rejection cache (ADR-147) and are audited. Details, costs and open decisions are in
`docs/plans/2026-10-09-reset-and-top-up-design.md`.

## Consequences

**Positive:**
- Plan upgrades, purchases and session resets take effect on the next request, on every shard.
- Nothing the item carries is lost; the fast path stays 0 RCU + 1 WCU and reads nothing new.
- ADR-145 conservation extends by one explicit term: per period, admitted plus held plus
  still-grantable equals the capacity plus the top-ups granted.
- The aggregator and concurrent slow paths are excluded by the `rf` lock they already honour.

**Negative:**
- Cost is O(shards) reads and two write units per shard, against one delete per shard in #471.
- A conflicting writer on any shard aborts the whole transaction; the operation retries a
  bounded number of times and then fails.
- A topped-up balance can sit on shards a request does not draw, so a draw can reject while
  the entity holds purchased tokens elsewhere, as ADR-145 already accepts for quotas.
- Another process's rejection cache can refuse for up to its TTL after a reset elsewhere.
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
