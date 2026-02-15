# Getting Started

This guide will help you install zae-limiter and set up rate limiting in your application.

## Installation

=== "pip"

    ```bash
    pip install zae-limiter
    ```

=== "uv"

    ```bash
    uv pip install zae-limiter
    ```

=== "poetry"

    ```bash
    poetry add zae-limiter
    ```

=== "conda"

    ```bash
    conda install -c conda-forge zae-limiter
    ```

## Quick Start

zae-limiter creates its own infrastructure automatically.

### Minimalist

For scripts and quick demos, pass limits inline:

```python
from zae_limiter import Repository, RateLimiter, Limit, RateLimitExceeded

repo = await Repository.builder("my-app", "us-east-1").build()
limiter = RateLimiter(repository=repo)

try:
    async with limiter.acquire(
        entity_id="user-123",
        resource="api",
        consume={"requests": 1},
        limits=[Limit.per_minute("requests", 100)],
    ) as lease:
        await do_work()
except RateLimitExceeded as e:
    print(f"Rate limited! Retry after {e.retry_after_seconds:.1f}s")

# Clean up when done
await repo.delete_stack()
```

### Stored Config (Recommended)

For production, configure limits once and keep application code simple.

**Step 1: Deploy and configure**

=== "CLI"

    ```bash
    # Deploy infrastructure
    zae-limiter deploy --name my-app --region us-east-1

    # Configure limits (apply to all entities)
    zae-limiter system set-defaults --name my-app -l rpm:1000 -l tpm:100000
    ```

=== "Python"

    ```python
    from zae_limiter import Repository, RateLimiter, Limit

    repo = await Repository.builder("my-app", "us-east-1").build()
    limiter = RateLimiter(repository=repo)

    await limiter.set_system_defaults(limits=[
        Limit.per_minute("rpm", 1000),
        Limit.per_minute("tpm", 100000),
    ])
    ```

**Step 2: Use in your application**

```python
from zae_limiter import Repository, RateLimiter, RateLimitExceeded

repo = await Repository.builder("my-app", "us-east-1").build()
limiter = RateLimiter(repository=repo)

try:
    async with limiter.acquire(
        entity_id="user-123",
        resource="api",
        consume={"rpm": 1, "tpm": 500},  # Limits resolved automatically
    ) as lease:
        await do_work()
except RateLimitExceeded as e:
    print(f"Rate limited! Retry after {e.retry_after_seconds:.1f}s")
```

## Infrastructure Persistence

When you use infrastructure builder methods, zae-limiter creates real AWS infrastructure via CloudFormation:

| Resource | Purpose | Persists? |
|----------|---------|-----------|
| DynamoDB Table | Rate limit state, entities, usage | Yes - until deleted |
| Lambda Function | Usage aggregation | Yes - until deleted |
| IAM Role | Lambda permissions | Yes - until deleted |
| CloudWatch Logs | Lambda logs | Yes - with retention |

!!! info "Infrastructure Outlives Your Python Session"
    This infrastructure persists beyond your Python session. Restarting your application
    reconnects to existing resources. Rate limit state is preserved across restarts.
    You only pay when the limiter is used (~$0.75/1M requests).

## Infrastructure Lifecycle

Both programmatic API and CLI are fully supported for managing infrastructure.

### Creating Infrastructure

=== "Programmatic"

    Use builder methods to declare the desired infrastructure state:

    ```python
    repo = await Repository.builder("my-app", "us-east-1").build()
    limiter = RateLimiter(repository=repo)
    ```

    CloudFormation ensures the infrastructure matches your declaration.

=== "CLI"

    ```bash
    zae-limiter deploy --name my-app --region us-east-1
    ```

    Useful for: CI/CD pipelines, GitOps workflows, infrastructure-as-code.

### Connecting to Existing Infrastructure

When no infrastructure builder methods are called, the builder connects to existing infrastructure without
attempting to create or modify it:

```python
# Connect only — no infra builder methods = no create/update
repo = await Repository.builder("my-app", "us-east-1").build()
limiter = RateLimiter(repository=repo)
```

This is useful when infrastructure is managed separately (e.g., via CLI or Terraform).

!!! warning "Declarative State Management"
    Builder methods declare the desired infrastructure state. If multiple applications
    use the same limiter name with **different** settings, CloudFormation will update
    the stack to match the most recent declaration—similar to how Terraform applies
    the last-written configuration.

    To maintain consistent state:

    - Use identical builder options across all clients sharing a limiter
    - Omit infrastructure builder methods in application code and manage infrastructure externally
    - Use different limiter names for different configurations

### Checking Status

=== "Programmatic"

    ```{.python .lint-only}
    available = await repo.ping()  # Async
    # or
    available = repo.ping()  # Sync

    if available:
        print("Stack is ready")
    else:
        print("DynamoDB not reachable")
    ```

    For comprehensive status including CloudFormation details, use the CLI command.

=== "CLI"

    ```bash
    zae-limiter status --name my-app --region us-east-1
    ```

### Deleting Infrastructure

=== "Programmatic"

    ```{.python .lint-only}
    # After you're done with the limiter
    await repo.delete_stack()  # Async
    # or
    repo.delete_stack()  # Sync
    ```

=== "CLI"

    ```bash
    zae-limiter delete --name my-app --region us-east-1 --yes
    ```

!!! warning "Data Loss"
    Deleting infrastructure permanently removes all rate limit data,
    entity configurations, and usage history. This cannot be undone.

!!! note "Deployment Options"
    For organizations requiring strict infrastructure/application separation,
    see [CLI deployment](infra/deployment.md) or [CloudFormation template export](infra/deployment.md#cloudformation-template).

## Understanding Limits

Rate limiting in zae-limiter tracks **who** is making requests, **what** they're accessing, and **how much** they can use.

### The Core Concepts

When you call `acquire()`, you specify:

- **`entity_id`**: Who is being rate limited (e.g., `"user-123"`, `"api-key-abc"`, `"tenant-xyz"`)
- **`resource`**: What they're accessing (e.g., `"gpt-4"`, `"api"`, `"embeddings"`)
- **`consume`**: How much capacity this request uses
- **`limits`**: The rate limit rules to apply (optional if using stored config)

Each entity has **separate buckets per resource**. A user rate limited on `"gpt-4"` can still access `"gpt-3.5-turbo"`:

```python
# User 123 accessing GPT-4 - tracked separately from GPT-3.5
async with limiter.acquire(
    entity_id="user-123",
    resource="gpt-4",        # Bucket: user-123 + gpt-4
    consume={"rpm": 1},
    limits=[Limit.per_minute("rpm", 10)],
) as lease:
    ...

# Same user, different resource - separate bucket
async with limiter.acquire(
    entity_id="user-123",
    resource="gpt-3.5-turbo",  # Bucket: user-123 + gpt-3.5-turbo
    consume={"rpm": 1},
    limits=[Limit.per_minute("rpm", 100)],
) as lease:
    ...
```

### Defining Limits

A `Limit` defines a rate limit using the [token bucket algorithm](guide/token-bucket.md):

```python
# 100 requests per minute
Limit.per_minute("rpm", 100)

# 10,000 tokens per minute with 15,000 burst capacity
Limit.per_minute("tpm", 10_000, burst=15_000)

# 1,000 requests per hour
Limit.per_hour("rph", 1_000)

# Custom: 50 requests per 30 seconds
Limit.custom("requests", capacity=50, refill_amount=50, refill_period_seconds=30)
```

| Parameter | Description |
|-----------|-------------|
| `name` | Unique identifier (e.g., "rpm", "tpm") |
| `capacity` | Tokens that refill per period (sustained rate) |
| `burst` | Maximum bucket size (defaults to capacity) |

See [Token Bucket Algorithm](guide/token-bucket.md) for details on how capacity, burst, and refill work together.

## Handling Rate Limit Errors

When a rate limit is exceeded, `RateLimitExceeded` is raised with full details:

```python
from zae_limiter import RateLimitExceeded

try:
    async with limiter.acquire(
        entity_id="user-123",
        resource="gpt-4",
        consume={"rpm": 2},  # Exceeds capacity to trigger error
        limits=[Limit.per_minute("rpm", 1)],
    ):
        await do_work()
except RateLimitExceeded as e:
    # Get retry delay
    print(f"Retry after: {e.retry_after_seconds}s")

    # For HTTP responses
    response = JSONResponse(
        status_code=429,
        content=e.as_dict(),
        headers={"Retry-After": e.retry_after_header},
    )
```

## Centralized Configuration (v0.5.0+)

zae-limiter supports storing rate limit configurations in DynamoDB, eliminating the need to hardcode limits in application code.

### Setting Up Defaults

Configure limits at system and resource levels (typically done by admins during deployment):

```bash
# Set system-wide defaults (applies to ALL resources)
zae-limiter system set-defaults -l rpm:100 -l tpm:10000

# Set resource-specific defaults (override system for this resource)
zae-limiter resource set-defaults gpt-4 -l rpm:50 -l tpm:100000
zae-limiter resource set-defaults gpt-3.5-turbo -l rpm:200 -l tpm:500000

# Set entity-specific limits (premium users)
zae-limiter entity set-limits user-premium --resource gpt-4 -l rpm:500 -l tpm:500000
```

### Automatic Resolution

With limits configured, application code becomes simpler—no need to pass limits:

```python
# Limits are resolved automatically from stored config
async with limiter.acquire(
    entity_id="user-123",
    resource="gpt-4",
    consume={"rpm": 1},  # No limits parameter needed
) as lease:
    await call_api()
```

**Resolution order (highest to lowest precedence):**

1. **Entity level** - Specific limits for entity+resource
2. **Resource level** - Default limits for a resource
3. **System level** - Global defaults for all resources
4. **Override parameter** - Fallback if no stored config

See [Configuration Hierarchy](guide/config-hierarchy.md) for full details.

## Multi-Tenant Namespaces

For multi-tenant applications, namespaces provide logical isolation within a single DynamoDB table:

```python
from zae_limiter import Repository, RateLimiter

# Each tenant gets an isolated namespace
repo = await (
    Repository.builder("my-app", "us-east-1")
    .namespace("tenant-alpha")
    .build()
)
limiter = RateLimiter(repository=repo)

# All operations are scoped to tenant-alpha's namespace
async with limiter.acquire(
    entity_id="user-123",
    resource="api",
    consume={"rpm": 1},
) as lease:
    await do_work()
```

For namespace lifecycle management and per-tenant IAM access control, see the [Production Guide](infra/production.md#multi-tenant-deployments).

## Next Steps

- [Basic Usage](guide/basic-usage.md) - Multiple limits, adjustments, capacity queries
- [Configuration Hierarchy](guide/config-hierarchy.md) - Three-tier limit resolution
- [Hierarchical Limits](guide/hierarchical.md) - Parent/child entities, cascade mode
- [LLM Integration](guide/llm-integration.md) - Token estimation and reconciliation
- [Deployment Guide](infra/deployment.md) - Production deployment options
- [CLI Reference](cli.md) - Full CLI command reference
- [Namespace Keys Migration](migrations/namespace-keys.md) - Migrating from v0.9.x to v0.10.0
