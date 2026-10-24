# Repository

The `Repository` class owns all DynamoDB data access and infrastructure management.
`RateLimiter` delegates data operations to the repository while owning business logic.

## Preferred Usage Pattern

```python
from zae_limiter import RateLimiter, Repository

# Open repository (auto-provisions if needed, recommended)
repo = await Repository.open()
limiter = RateLimiter(repository=repo)

# For explicit infrastructure provisioning, use builder:
repo = await Repository.builder().build()
limiter = RateLimiter(repository=repo)

# Infrastructure managed elsewhere (your own CloudFormation/Terraform/CDK):
repo = await Repository.connect("my-app")
limiter = RateLimiter(repository=repo)
```

See [ADR-108](../adr/108-repository-protocol.md) for the design rationale.

## Choosing an Entry Point

| Entry point | Provisions infrastructure? | Use when |
|-------------|---------------------------|----------|
| `Repository.open()` | Yes — deploys the stack, registers namespaces, updates the Lambda | Application code, prototyping, LocalStack dev |
| `Repository.builder().build()` | Yes — with custom options (permission boundaries, IAM naming, Lambda config) | Enterprise deployments |
| `Repository.connect()` | **No** — reads only, raises when anything is missing | Infrastructure owned by your own CloudFormation, Terraform, or CDK |

### `Repository.connect()`

`connect()` binds to infrastructure that already exists and never writes to it.
It issues two reads — a namespace lookup and a version record read — and raises
rather than repairing anything it finds missing:

| Situation | `open()` | `connect()` |
|-----------|----------|-------------|
| Table missing | Deploys the stack | `InfrastructureNotFoundError` |
| Namespace unregistered | Registers it | `NamespaceNotFoundError` |
| Version record missing | Writes it | `InfrastructureNotFoundError` |
| Lambda version behind client | Updates the Lambda | `VersionMismatchError` |

```python
from zae_limiter import RateLimiter, Repository

# Stack, table, and namespaces are all deployed by your own template
repo = await Repository.connect("my-app")
limiter = RateLimiter(repository=repo)
```

Register namespaces ahead of time with `zae-limiter namespace register`, or
from a template, so `connect()` can resolve them. A `SyncRepository.connect()`
counterpart is available with the same signature.

## Repository

::: zae_limiter.repository.Repository
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

## RepositoryProtocol

The `RepositoryProtocol` defines the interface for pluggable backends.
Implement this protocol to use a different storage backend (e.g., for testing).

!!! warning "Changed in v0.17.0: the adjust, normal and retry writes' contracts (#721)"
    A custom backend must accept `ceilings`, `trim` and `pin` on
    `build_composite_adjust` from v0.17.0. The lease and the limiter pass them on
    every refund — a rollback, a `release()`, a negative `adjust()` or a
    speculative compensation — so a backend that does not accept them raises
    `TypeError` there. It must also accept `read_tokens` on
    `build_composite_normal` and `period_guards` on `build_composite_retry`,
    described below.

    **Required of every adjust write**, whatever the keywords:

    - **The item must exist.** Fail the write (raise
      `ConditionalCheckFailedException`) when the bucket is gone. An
      unconditional credit onto a swept or deleted item creates a stub that
      rejects the entity forever.
    - **A quota credit stays in its debit's period.** For each credited limit
      whose `CreditCeiling.quota_period` is set, fail the write unless the period
      still holds — calendar: the item's `rf` is before `reset_edge_ms` and the
      reset-schedule strings in `reset_sched_raw` (item `rsched`, `sched_tz`, the
      limit's `rsched`) are still the item's; session: the limit's window start
      and applied marker equal `window`. An
      unconditional credit lands a refund of one period's debit in the next,
      which then admits more than its allowance. Applies to the `trim=True`
      write too.

    Expected for the protection a refund relies on:

    - `ceilings`: limit name → `CreditCeiling`. Condition each credited limit on
      its balance staying at or below `ceiling_milli` after the credit, and on the
      stored capacity, grant count and schedule overrides it was computed from.
      Raise `ConditionalCheckFailedException` when the condition fails.
    - `trim=True`: the fallback the caller sends after that failure. Apply the
      same credit (still subject to the two requirements above) and, in the same
      write, mark the bucket so the next acquire re-materialises it
      (`SET vu = 0` on DynamoDB).
    - `pin`: the item's (`shard_count`, `vu`, item-level schedule strings) as the
      ceiling saw them, held beside the ceilings.

    **`build_composite_normal(read_tokens=...)`**: limit name → the `tk` the
    slow pass read (millitokens). For every limit whose refill delta is
    positive — a drip, a reset, a roll, a window opened — fail the write
    unless the stored balance is still at or below that read. A refund or
    release landing between the read and the write moves neither `rf` nor
    `vu`, and an unpinned raise stacks on top of it. A trim (delta <= 0) is
    not pinned. A failure falls to the consumption-only retry, like a lost
    `rf` lock.

    **`build_composite_retry(period_guards=...)`**: limit name →
    `(reset_edge_ms, window_start_ms)`, one of the two set, for each quota the
    lost write was to reset, roll or open. Fail the retry unless that period
    has been applied on the item — calendar: `rf >= reset_edge_ms`; session:
    the limit's window-applied marker equals `window_start_ms` — and return the
    old image on that failure (`ALL_OLD`). The lease then re-plans once from a
    fresh read; a second loss is a rejection. Without the guard the retry
    spends the previous period's balance and leaves the reset pending, so the
    next pass grants a full allowance again.

    **`transact_write` now returns** the item's attributes after a single-item
    update (`ALL_NEW`), or `None`. A refund after the consumption-only retry
    takes its quota period from that image; a backend that returns `None`
    makes it fall back to the period the lease read, which can drop a refund
    after a reset raced the retry but never lands one in the wrong period.

    **Known gaps (not a backend's to close).** A resource- or system-level
    change never reaches the bucket item (it propagates through the TTL). A
    lowered schedule there lets a refund land above the new ceiling until the
    next slow pass clamps it; a reset-schedule change there leaves the period
    guard following the lease's cron, so a reset under the new cron before
    the old edge can let the old period's refund land. An entity-level change
    reaches the item, and any reset-schedule change on the item drops the
    in-flight quota refunds (one per open lease per change).

!!! warning "Changed in v0.17.0: moving an entity (ADR-150)"
    A custom backend must implement `set_parent`, `build_owner_check` and
    `restamp_owner_if_current`. The lease appends one `build_owner_check` item
    (a condition-only transaction item that writes nothing) per owner whose bucket
    a commit creates, after the bucket items and donor debits: it must fail the
    transaction, with a per-item cancellation reason, when the owner's META no
    longer holds the parent generation and incarnation the pass read (or now
    exists when the pass read none). The lease calls `restamp_owner_if_current`
    when the slow path's owner stamp lost its pin to another incarnation.

!!! warning "Changed in v0.17.0: soft limits and bypass (ADR-151)"
    A custom backend must implement `build_bypass_consume`,
    `resolve_bypass_from_fetched`, `resolve_soft_limits`, `limits_read_fresh`,
    `_stamp_deadline`, `bypass_resource` and `bypass_entity`, and accept the new
    keywords below; the limiter and the lease pass them on every slow pass, so a
    backend that does not raises `TypeError` there. `fix_bucket_stamps` is
    optional: the limiter skips it on a backend without it.

    - **Stamps are deadlines.** A bucket's `bypass` and `b_{name}_soft` hold an
      epoch-ms deadline. `speculative_consume` must admit a declared limit
      without testing its balance only while `b_{name}_soft` is live **and** the
      item carries the limit, or while `bypass` is live; fail an expired stamp as
      `SCHEDULE_BOUNDARY` (never a rejection); and report `bypassed` on its
      result. A bypassed bucket is debited nothing but still counts `tc` and
      `wcu`.
    - **`build_composite_create(bypass_until_ms=...)`** stamps `bypass` with that
      deadline; each state's `soft` / `soft_until_ms` stamps `b_{name}_soft`.
    - **`build_composite_normal(soft_stamps=, soft_reads=, soft_trusted=,
      bypass_stamp=)`**: SET each stamp to its deadline or REMOVE it, each pinned
      on the value read (`soft_reads`, the second element of `bypass_stamp`);
      waive a balance floor on a live soft stamp, and omit it for a
      `soft_trusted` limit.
    - **`build_composite_retry(soft_trusted=, now_ms=)`**: the same waivers on the
      consumption-only retry, for its balance floors and its quota period guards;
      it never writes a stamp except a seed's.
    - **`build_composite_adjust(tokens=False)`** writes `tc` only (a bypassed
      lease's adjustment); **`counter=False`** writes `tk` only (the bypass
      refund), still subject to the ceilings and period guards above.
    - **`resolve_access(include_limits=, include_system=)`** returns each limit's
      soft-ness, read uncached, in `ConfigAccess.limit_soft`; `bypass` is set
      when the deciding `disabled` value is `"bypass"`.
    - `get_resource_disabled` / `get_entity_disabled` may return `"bypass"`, and
      the setters accept `disabled="bypass"`: test `disabled is True`, never its
      truthiness.

::: zae_limiter.repository_protocol.RepositoryProtocol
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3
