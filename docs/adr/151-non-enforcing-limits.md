# ADR-151: Soft limits and bypass, decided on the bucket item

**Status:** Proposed
**Date:** 2026-10-09
**Issue:** [#467](https://github.com/zeroae/zae-limiter/issues/467), [#311](https://github.com/zeroae/zae-limiter/issues/311)
**Related:** [ADR-125](125-resource-disable.md), [ADR-141](141-reset-after-version-gate.md), [ADR-145](145-sharded-quota-conserves-allowance.md), [ADR-146](146-per-resource-cascade-policy.md), [ADR-147](147-client-side-rejection-cache.md), [#674](https://github.com/zeroae/zae-limiter/issues/674)
**Design:** `docs/plans/2026-10-09-non-enforcing-limits-design.md`

## Context

Operators need two ways to run a limit without enforcing it. A **soft** limit (#467) is metered
exactly like a hard one — tokens are debited, the bucket may go into debt — but it never causes
`RateLimitExceeded`; billing-style `tpm` beside a hard `rpm`, and shadow rollout of a new limit,
both need it. **Bypass** (#311) is an administrative waiver for one entity or resource: every
request is admitted, no token is debited, and consumption is still counted. ADR-125 shipped the
deny half of #311 (`disabled`); bypass is what remains.

The two differ in kind. Soft-ness belongs to one limit and must change with the limit's own
definition, which resolves override-not-merge through the four config levels. Bypass is an
access decision about an (entity, resource), exactly like `disabled` and the cascade policy,
which already resolve by one walk and are stamped on bucket items so the fast path decides with
no config read (ADR-125, ADR-146).

The fast path cannot read config, and a bypass write has a different update shape (no `tk`
debit), so the client must know it is bypassing before it builds the write. Soft-ness, by
contrast, can be decided by the server: the write may admit a limit when the item says it is
soft. Either flag left stale on a bucket is dangerous in one direction only — a stale soft or
bypass stamp after enforcement is restored admits without limit.

## Decision

1. Soft-ness must be a property of a limit: stored beside the limit's other config fields,
   resolved with the limit, and denormalised per limit onto every bucket item that carries it.
   The speculative write must admit a limit whose item marks it soft without testing its
   balance, while still debiting it; no admission path, local rejection (ADR-147) or reported
   retry hint may treat a soft limit as a reason to reject.
2. Bypass must be a fourth value of the ADR-125 `disabled` config attribute, resolved by the
   same single walk (first explicit value wins; no system level), and stamped on bucket items
   as a separate attribute that pre-0.17 clients ignore. A bypassed write must debit no
   limit's balance, must still add every declared limit's consumption to its counter, and must
   still debit and enforce the reserved `wcu` limit.
3. The client must learn bypass from the stamp on the items its own writes return, and every
   bypass-shaped write must be conditioned on the stamp being present, so a cleared bypass is
   detected by the server and never admits without enforcement. A request whose write debited
   a bucket that turns out to be bypassed must be refunded.
4. Every change of either flag must reach existing buckets eagerly: bypass by the ADR-125
   fan-out, soft-ness by the entity-level parameter sync and, for resource- and system-level
   changes, by a change-only fan-out. A bucket created or seeded by the slow path must take
   its soft stamps from config read uncached in that pass, never from the config cache.
5. Each entity resolves both flags independently; a child's soft limit or bypass never
   extends to its cascade parent.
6. A bypassed pass must not open, join or roll a session window, and must not reset a quota;
   a soft quota's reset restores its allowance and forgives its debt. ADR-145's grant rules
   are unchanged: a bucket created under bypass is funded exactly as an enforced one.
7. Storing a soft limit or a bypass value must pass the ADR-141 version gate at 0.17.0 and
   ratchet `client_min_version` to 0.17.0, as ADR-146 does.

The write shapes, fan-out paths, reporting fields, overdraw signal, surfaces and costs are in
the design document.

## Consequences

**Positive:**
- Soft limits cost nothing extra on the fast path: one more condition term per declared limit,
  0 RCU, 1 WCU, and no client knowledge of which limits are soft.
- Bypass on a warm process costs 0 RCU + 1 WCU and leaves `tk` untouched, so restoring
  enforcement needs no reset and leaves no debt.
- Usage snapshots stay correct under both, because they are built from `tc` (#179).
- Bypass and `disabled` cannot disagree: one attribute, one walk, one precedence.

**Negative:**
- The first bypassed request per process per (entity, resource) costs one refund write, and the
  first request after bypass is cleared costs one failed write.
- A soft-ness change at resource or system level is O(buckets) writes, and a system-level one
  discovers every bucket in the namespace.
- A bucket an old client created carries no soft stamp, so the new fast path enforces that limit
  until the next fan-out or fresh slow pass: under-admission, never over-admission.
- A soft limit's debt is per shard: one shard can be overdrawn while siblings hold tokens.
- Clients older than v0.15 ignore `client_min_version` and enforce soft and bypassed limits.

## Alternatives Considered

- **Per-call non-gating (`consume={"tpm": None}`)**: rejected in #455; the debt gates the next caller.
- **Soft as an independent tri-state walk**: it would detach soft-ness from the limit it modifies, so an entity override of `tpm` would silently inherit a resource's soft flag.
- **Bypass as a sibling attribute to `disabled`**: two values per level with no defined precedence, which #311 rules out.
- **Reuse the bucket `disabled` stamp for bypass**: every pre-0.17 client would read a bypassed bucket as disabled and return 403.
- **Debit `tk` under bypass and reset on clear**: a reset is a `SET` that breaks ADR-145 conservation for quotas.
- **Decide bypass by server condition only**: the update expression cannot omit the `tk` debit conditionally.
- **Propagate resource-level soft changes by bucket TTL**: a soft-to-hard change would admit without limit for up to seven reset periods.
- **AuditEvent per overdraw**: one extra write per overdrawn request, on the hot path.
