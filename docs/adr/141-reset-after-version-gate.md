# ADR-141: Storing a reset_after limit is gated on reader versions

**Status:** Proposed
**Date:** 2026-09-27
**Issue:** [#638](https://github.com/zeroae/zae-limiter/issues/638)
**Related:** [ADR-139](139-duration-reset-windows.md), ADR-137, ADR-009, [#640](https://github.com/zeroae/zae-limiter/issues/640), [#644](https://github.com/zeroae/zae-limiter/issues/644)

## Context

A reader predating [ADR-139](139-duration-reset-windows.md) cannot read a `reset_after` limit.
An old client sees a zero rate with no reset, which ADR-137 rejects: under `on_unavailable=block`
every acquire on that level fails, and under `allow` every acquire is admitted with no limiting at
all on that level. An old aggregator reads the quota as a dripping limit, so its
proactive-sharding clone mints a fresh share per new shard — the #587 over-admission, where a
v0.15 aggregator funds the clone by [ADR-145](145-sharded-quota-conserves-allowance.md)'s move. An old
provisioner stores a `reset_after` manifest limit as a dripping one.

The version record already carries `lambda_version` and `client_min_version`, but neither could
be trusted. `lambda_version` was stamped by writers that had not necessarily updated both Lambdas
(a `deploy` on an existing stack pushes code but never adds or removes a function), and
`client_min_version` was reset to `0.0.0` by every Lambda update and ignored by clients, which
fell through an incompatible check with no flag set. There is one aggregator per stack, so its
version is the one reader a writer can verify; old clients cannot be made to fail closed from a
newer build. The options, the owner's 2026-09-26 decision, and the mechanics are in #638 and
CLAUDE.md "Version gate (#638)".

## Decision

Every writer of a `reset_after` limit — config setters, the provisioner, and an `acquire()`
limits override — must refuse it unless `lambda_version` is at least 0.15.0 or equals the
writer's own build, and a config write it admits must raise `client_min_version` to 0.15.0
(capped at a development writer's own build), which no writer may lower and below which clients
from v0.15.0 on must refuse to run. A writer may stamp `lambda_version` with its build only when
it created the stack in that call, or when the aggregator and the provisioner each either had
code pushed in that run or do not exist.

## Consequences

**Positive:**
- A pre-v0.15 aggregator or provisioner can no longer be handed a limit it would misread.
- One predicate decides the stamp for CLI `deploy`, CLI `upgrade`, infrastructure provisioning,
  `open()`'s record initialization and its Lambda auto-update, so the rule cannot drift between
  them.
- A missing record or unknown Lambda version fails closed and names its remedy.
- `client_min_version` becomes a working field, so the next incompatible feature is protected by
  the same ratchet without new machinery.
- Writes of every other limit pay nothing.

**Negative:**
- Nothing makes a v0.14 client fail closed; it does not enforce the session limit, and every
  client must still be upgraded before one is relied on (see [ADR-142](142-hide-reset-after-config.md)).
- The gate holds only at write time: a v0.14 CLI can later put the old Lambdas back, and a v0.14
  `upgrade` also resets the minimum. Never run a v0.14 CLI against such a stack.
- The minimum, and the override gate's cached Lambda version, are checked when a repository
  opens, so a long-lived process is not refused until it restarts.
- A refused CloudFormation update whose previous properties also carried `reset_after` leaves the
  stack in `UPDATE_ROLLBACK_FAILED`, needing a manual rollback continuation.
- `deploy` performs no minimum check of its own, and `--no-aggregator` stacks get no exemption
  in the gate; `zae-limiter upgrade` and `open(auto_update=True)` push only to the Lambdas a
  stack has, so they are the remedy on every stack shape (#644).

## Alternatives Considered

### Gate writers on `client_min_version` alone
Rejected because: v0.14 ignores that field, so it stops neither v0.14 clients nor the v0.14
aggregator.

### Bump the schema major so old clients raise `IncompatibleSchemaError`
Rejected because: it breaks every v0.x client on the table and needs a migration.

### A stored flag old clients already reject
Rejected because: it fails exactly as today, and so still fails open under `allow`.

### Trust the stack's `EnableAggregator` parameter to earn the stamp
Rejected because: the function does the reading, and the template creates it only when a role is
also available.

### Hide the configuration from pre-v0.15 readers
Decided separately in [ADR-142](142-hide-reset-after-config.md).
