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

zae-limiter creates its own infrastructure automatically. Here's a complete example:

### Async API (Recommended)

```python
from zae_limiter import RateLimiter, Limit, StackOptions, RateLimitExceeded

# Create rate limiter with declarative infrastructure
limiter = RateLimiter(
    name="my-app",           # ZAEL-my-app resources in AWS
    region="us-east-1",
    stack_options=StackOptions(),  # Declare desired state - CloudFormation ensures it
)

try:
    async with limiter.acquire(
        entity_id="user-123",
        resource="api",
        limits=[Limit.per_minute("requests", 100)],
        consume={"requests": 1},
    ) as lease:
        await do_work()
except RateLimitExceeded as e:
    print(f"Rate limited! Retry after {e.retry_after_seconds:.1f}s")
```

### Sync API

```python
from zae_limiter import SyncRateLimiter, Limit, StackOptions

limiter = SyncRateLimiter(
    name="my-app",
    region="us-east-1",
    stack_options=StackOptions(),
)

with limiter.acquire(
    entity_id="user-123",
    resource="api",
    limits=[Limit.per_minute("requests", 100)],
    consume={"requests": 1},
) as lease:
    response = call_api()
```

## Infrastructure Persistence

When you pass `stack_options=StackOptions()`, zae-limiter creates real AWS infrastructure via CloudFormation:

| Resource | Purpose | Persists? |
|----------|---------|-----------|
| DynamoDB Table | Rate limit state, entities, usage | Yes - until deleted |
| Lambda Function | Usage aggregation | Yes - until deleted |
| IAM Role | Lambda permissions | Yes - until deleted |
| CloudWatch Logs | Lambda logs | Yes - with retention |

!!! info "Infrastructure Outlives Your Python Session"
    This infrastructure persists beyond your Python session. Restarting your application
    reconnects to existing resources. Rate limit state is preserved across restarts.
    You only pay when the limiter is used (~$1/1M requests).

## Infrastructure Lifecycle

Both programmatic API and CLI are fully supported for managing infrastructure.

### Creating Infrastructure

=== "Programmatic"

    Pass `stack_options` to declare the desired infrastructure state:

    ```python
    limiter = RateLimiter(
        name="my-app",
        stack_options=StackOptions(),  # Desired state declaration
    )
    ```

    CloudFormation ensures the infrastructure matches your declaration.

=== "CLI"

    ```bash
    zae-limiter deploy --name my-app --region us-east-1
    ```

    Useful for: CI/CD pipelines, GitOps workflows, infrastructure-as-code.

### Connecting to Existing Infrastructure

If you omit `stack_options`, the limiter connects to existing infrastructure without
attempting to create or modify it:

```python
# Connect only - fails if ZAEL-my-app doesn't exist
limiter = RateLimiter(
    name="my-app",
    region="us-east-1",
    # No stack_options = connect only, no create/update
)
```

This is useful when infrastructure is managed separately (e.g., via CLI or Terraform).

!!! warning "Declarative State Management"
    `StackOptions` declares the desired infrastructure state. If multiple applications
    use the same limiter name with **different** settings, CloudFormation will update
    the stack to match the most recent declaration—similar to how Terraform applies
    the last-written configuration.

    To maintain consistent state:

    - Use identical `StackOptions` across all clients sharing a limiter
    - Omit `stack_options` in application code and manage infrastructure externally
    - Use different limiter names for different configurations

### Checking Status

```bash
zae-limiter status --name my-app --region us-east-1
```

### Deleting Infrastructure

=== "Programmatic"

    ```python
    # After you're done with the limiter
    await limiter.delete_stack()  # Async
    # or
    limiter.delete_stack()  # Sync
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
- **`limits`**: The rate limit rules to apply
- **`consume`**: How much capacity this request uses

Each entity has **separate buckets per resource**. A user rate limited on `"gpt-4"` can still access `"gpt-3.5-turbo"`:

```python
# User 123 accessing GPT-4 - tracked separately from GPT-3.5
async with limiter.acquire(
    entity_id="user-123",
    resource="gpt-4",        # Bucket: user-123 + gpt-4
    limits=[Limit.per_minute("rpm", 10)],
    consume={"rpm": 1},
) as lease:
    ...

# Same user, different resource - separate bucket
async with limiter.acquire(
    entity_id="user-123",
    resource="gpt-3.5-turbo",  # Bucket: user-123 + gpt-3.5-turbo
    limits=[Limit.per_minute("rpm", 100)],
    consume={"rpm": 1},
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
Limit.custom("requests", capacity=50, refill_period_seconds=30)
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
    async with limiter.acquire(...):
        await do_work()
except RateLimitExceeded as e:
    # Get retry delay
    print(f"Retry after: {e.retry_after_seconds}s")

    # For HTTP responses
    return JSONResponse(
        status_code=429,
        content=e.as_dict(),
        headers={"Retry-After": e.retry_after_header},
    )
```

## Next Steps

- [Basic Usage](guide/basic-usage.md) - Multiple limits, adjustments, capacity queries
- [Hierarchical Limits](guide/hierarchical.md) - Parent/child entities, cascade mode
- [LLM Integration](guide/llm-integration.md) - Token estimation and reconciliation
- [Deployment Guide](infra/deployment.md) - Production deployment options
- [CLI Reference](cli.md) - Full CLI command reference
