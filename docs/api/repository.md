# Repository

The `Repository` class owns all DynamoDB data access and infrastructure management.
`RateLimiter` delegates data operations to the repository while owning business logic.

## Preferred Usage Pattern

```python
from zae_limiter import RateLimiter, Repository, StackOptions

# Create repository with infrastructure config
repo = Repository(
    name="my-app",
    region="us-east-1",
    stack_options=StackOptions(lambda_memory=512),
)

# Infrastructure is created/updated automatically on first use
limiter = RateLimiter(repository=repo)
```

See [ADR-108](../adr/108-repository-protocol.md) for the design rationale.

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
