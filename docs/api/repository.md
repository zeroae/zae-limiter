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

::: zae_limiter.repository_protocol.RepositoryProtocol
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3
