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

## Deploy Infrastructure

Before using zae-limiter, you need to deploy the DynamoDB table and optional Lambda aggregator.

### Option 1: CLI (Recommended)

```bash
zae-limiter deploy --name limiter --region us-east-1
```

This creates a CloudFormation stack with:

- DynamoDB table with streams enabled
- Lambda function for usage aggregation
- Required IAM roles and permissions

### Option 2: Export Template

```bash
# Export CloudFormation template
zae-limiter cfn-template > template.yaml

# Deploy with AWS CLI
aws cloudformation deploy \
    --template-file template.yaml \
    --stack-name zae-limiter \
    --capabilities CAPABILITY_NAMED_IAM
```

### Option 3: Auto-Create in Code (Development)

```python
from zae_limiter import RateLimiter, StackOptions

limiter = RateLimiter(
    name="limiter",  # Creates ZAEL-limiter resources
    region="us-east-1",
    stack_options=StackOptions(),  # Auto-creates CloudFormation stack
)
```

!!! warning "Production Use"
    Auto-creation is convenient for development but not recommended for production.
    Use the CLI or CloudFormation for production deployments.

## Basic Usage

### Async API (Recommended)

```python
from zae_limiter import RateLimiter, Limit, RateLimitExceeded

limiter = RateLimiter(
    name="limiter",  # Connects to ZAEL-limiter resources
    region="us-east-1",
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
from zae_limiter import SyncRateLimiter, Limit

limiter = SyncRateLimiter(name="limiter")

with limiter.acquire(
    entity_id="user-123",
    resource="api",
    limits=[Limit.per_minute("requests", 100)],
    consume={"requests": 1},
) as lease:
    response = call_api()
```

## Understanding Limits

A `Limit` defines a rate limit using the token bucket algorithm:

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

- [Basic Usage](guide/basic-usage.md) - More usage patterns
- [Hierarchical Limits](guide/hierarchical.md) - Parent/child entities
- [LLM Integration](guide/llm-integration.md) - Token estimation and reconciliation
- [CLI Reference](cli.md) - Command-line interface
