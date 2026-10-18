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

!!! warning "Changed in v0.17.0: the adjust write's contract (#721)"
    A custom backend must accept `ceilings`, `trim` and `pin` on
    `build_composite_adjust` from v0.17.0. The lease and the limiter pass them on
    every refund — a rollback, a `release()`, a negative `adjust()` or a
    speculative compensation — so a backend that does not accept them raises
    `TypeError` there.

    **Required of every adjust write**, whatever the keywords:

    - **The item must exist.** Fail the write (raise
      `ConditionalCheckFailedException`) when the bucket is gone. An
      unconditional credit onto a swept or deleted item creates a stub that
      rejects the entity forever.
    - **A quota credit stays in its debit's period.** For each credited limit
      whose `CreditCeiling.quota_period` is set, fail the write unless the period
      still holds — calendar: the item's `rf` is before `reset_edge_ms`; session:
      the limit's window start and applied marker equal `window`. An
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

    **`transact_write` now returns** the item's attributes after a single-item
    update (`ALL_NEW`), or `None`. A refund after the consumption-only retry
    takes its quota period from that image; a backend that returns `None`
    makes it fall back to the period the lease read, which can drop a refund
    after a reset raced the retry but never lands one in the wrong period.

::: zae_limiter.repository_protocol.RepositoryProtocol
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3
