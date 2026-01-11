# zae-limiter

[![PyPI version](https://img.shields.io/pypi/v/zae-limiter)](https://pypi.org/project/zae-limiter/)
[![Python versions](https://img.shields.io/pypi/pyversions/zae-limiter)](https://pypi.org/project/zae-limiter/)
[![License](https://img.shields.io/pypi/l/zae-limiter)](https://github.com/zeroae/zae-limiter/blob/main/LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/zeroae/zae-limiter/ci.yml?branch=main)](https://github.com/zeroae/zae-limiter/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/zeroae/zae-limiter/graph/badge.svg)](https://codecov.io/gh/zeroae/zae-limiter)

A rate limiting library backed by DynamoDB using the token bucket algorithm.

## Features

- **Token Bucket Algorithm**: Precise rate limiting with configurable burst capacity
- **Multiple Limits**: Track requests per minute, tokens per minute, etc. in a single call
- **Hierarchical Entities**: Two-level hierarchy (project → API keys) with cascade mode
- **Atomic Transactions**: Multi-key updates via DynamoDB TransactWriteItems
- **Rollback on Exception**: Automatic rollback if your code throws
- **Stored Limits**: Configure per-entity limits in DynamoDB
- **Usage Analytics**: Lambda aggregator for hourly/daily usage snapshots
- **Async + Sync APIs**: First-class async support with sync wrapper

## Installation

```bash
pip install zae-limiter
```

Or using uv:

```bash
uv pip install zae-limiter
```

## Quick Start

### 1. Deploy Infrastructure

Using CLI (recommended):
```bash
zae-limiter deploy --table-name rate_limits --region us-east-1
```

Or get template for manual deployment:
```bash
zae-limiter cfn-template > template.yaml
aws cloudformation deploy --template-file template.yaml --stack-name zae-limiter
```

Or auto-create in code (development):
```python
from zae_limiter import RateLimiter

limiter = RateLimiter(
    table_name="rate_limits",
    region="us-east-1",
    create_stack=True,  # auto-create CloudFormation stack
)
```

### 2. Use in Code

```python
from zae_limiter import RateLimiter, Limit

# Initialize the limiter (stack must already exist)
limiter = RateLimiter(
    table_name="rate_limits",
    region="us-east-1",
)

# Acquire rate limit capacity
async with limiter.acquire(
    entity_id="api-key-123",
    resource="gpt-4",
    limits=[
        Limit.per_minute("rpm", 100),       # 100 requests/minute
        Limit.per_minute("tpm", 10_000),    # 10k tokens/minute
    ],
    consume={"rpm": 1, "tpm": 500},  # estimate 500 tokens
) as lease:
    response = await call_llm()

    # Reconcile actual token usage (can go negative)
    actual_tokens = response.usage.total_tokens
    await lease.adjust(tpm=actual_tokens - 500)

# On success: consumption is committed
# On exception: consumption is rolled back
```

### Local Development

For DynamoDB Local, auto-creation uses direct table creation (not CloudFormation):

```python
limiter = RateLimiter(
    table_name="rate_limits",
    endpoint_url="http://localhost:8000",
    create_table=True,  # Creates table directly (CloudFormation skipped)
)
```

## Usage

### Basic Rate Limiting

```python
from zae_limiter import RateLimiter, Limit, RateLimitExceeded

limiter = RateLimiter(table_name="rate_limits")

try:
    async with limiter.acquire(
        entity_id="user-123",
        resource="api",
        limits=[Limit.per_minute("requests", 100)],
        consume={"requests": 1},
    ) as lease:
        await do_work()
except RateLimitExceeded as e:
    # Exception includes ALL limit statuses (passed and failed)
    print(f"Retry after {e.retry_after_seconds:.1f}s")

    # For API responses
    return JSONResponse(
        status_code=429,
        content=e.as_dict(),
        headers={"Retry-After": e.retry_after_header},
    )
```

### Hierarchical Rate Limits (Cascade)

```python
# Create parent (project) and child (API key)
await limiter.create_entity(entity_id="proj-1", name="Production")
await limiter.create_entity(entity_id="key-abc", parent_id="proj-1")

# Cascade mode: consume from both key AND project
async with limiter.acquire(
    entity_id="key-abc",
    resource="gpt-4",
    limits=[
        Limit.per_minute("tpm", 10_000),  # per-key limit
    ],
    consume={"tpm": 500},
    cascade=True,  # also applies to parent
) as lease:
    await call_api()
```

### Burst Capacity

```python
# Allow burst of 15k tokens, but sustain only 10k/minute
limits = [
    Limit.per_minute("tpm", 10_000, burst=15_000),
]
```

### Stored Limits

```python
# Store custom limits for premium users
await limiter.set_limits(
    entity_id="user-premium",
    limits=[
        Limit.per_minute("rpm", 500),
        Limit.per_minute("tpm", 50_000, burst=75_000),
    ],
)

# Use stored limits (falls back to defaults if not stored)
async with limiter.acquire(
    entity_id="user-premium",
    resource="gpt-4",
    limits=[Limit.per_minute("rpm", 100)],  # default
    consume={"rpm": 1},
    use_stored_limits=True,
) as lease:
    ...
```

### LLM Token Estimation + Reconciliation

```python
async with limiter.acquire(
    entity_id="key-abc",
    resource="gpt-4",
    limits=[
        Limit.per_minute("rpm", 100),
        Limit.per_minute("tpm", 10_000),
    ],
    consume={"rpm": 1, "tpm": 500},  # estimate
) as lease:
    response = await llm.complete(prompt)
    actual = response.usage.total_tokens

    # Adjust without throwing (can go negative)
    await lease.adjust(tpm=actual - 500)
```

### Check Capacity Before Expensive Operations

```python
# Check available capacity
available = await limiter.available(
    entity_id="key-abc",
    resource="gpt-4",
    limits=[Limit.per_minute("tpm", 10_000)],
)
print(f"Available tokens: {available['tpm']}")

# Check when capacity will be available
if available["tpm"] < needed_tokens:
    wait = await limiter.time_until_available(
        entity_id="key-abc",
        resource="gpt-4",
        limits=[Limit.per_minute("tpm", 10_000)],
        needed={"tpm": needed_tokens},
    )
    raise RetryAfter(seconds=wait)
```

### Synchronous API

```python
from zae_limiter import SyncRateLimiter, Limit

limiter = SyncRateLimiter(table_name="rate_limits")

with limiter.acquire(
    entity_id="key-abc",
    resource="api",
    limits=[Limit.per_minute("rpm", 100)],
    consume={"rpm": 1},
) as lease:
    response = call_api()
    lease.adjust(tokens=response.token_count)
```

### Failure Modes

```python
from zae_limiter import RateLimiter, FailureMode

# Fail closed (default): reject requests if DynamoDB unavailable
limiter = RateLimiter(
    table_name="rate_limits",
    failure_mode=FailureMode.FAIL_CLOSED,
)

# Fail open: allow requests if DynamoDB unavailable
limiter = RateLimiter(
    table_name="rate_limits",
    failure_mode=FailureMode.FAIL_OPEN,
)

# Override per-call
async with limiter.acquire(
    ...,
    failure_mode=FailureMode.FAIL_OPEN,
):
    ...
```

## Exception Details

When a rate limit is exceeded, `RateLimitExceeded` includes full details:

```python
try:
    async with limiter.acquire(...):
        ...
except RateLimitExceeded as e:
    # All limits that were checked
    for status in e.statuses:
        print(f"{status.limit_name}: {status.available}/{status.limit.capacity}")
        print(f"  exceeded: {status.exceeded}")
        print(f"  retry_after: {status.retry_after_seconds}s")

    # Just the violations
    for v in e.violations:
        print(f"Exceeded: {v.limit_name}")

    # Just the passed limits
    for p in e.passed:
        print(f"Passed: {p.limit_name}")

    # Primary bottleneck
    print(f"Bottleneck: {e.primary_violation.limit_name}")
    print(f"Retry after: {e.retry_after_seconds}s")

    # For HTTP responses
    response_body = e.as_dict()
    retry_header = e.retry_after_header
```

## Infrastructure

### Deploy with CloudFormation

```bash
# Export the template from the installed package
zae-limiter cfn-template > template.yaml

# Deploy the DynamoDB table and Lambda aggregator
aws cloudformation deploy \
    --template-file template.yaml \
    --stack-name zae-limiter \
    --parameter-overrides \
        TableName=rate_limits \
        SnapshotRetentionDays=90 \
    --capabilities CAPABILITY_NAMED_IAM
```

### Automatic Lambda Deployment

The `zae-limiter deploy` CLI command automatically handles Lambda deployment:

```bash
# Deploy stack with Lambda aggregator (automatic)
zae-limiter deploy --table-name rate_limits --region us-east-1

# The CLI automatically:
# 1. Creates CloudFormation stack with DynamoDB table and Lambda function
# 2. Builds Lambda deployment package from installed library
# 3. Deploys Lambda code via AWS Lambda API (~30KB, no S3 required)
```

To deploy without the Lambda aggregator:

```bash
zae-limiter deploy --table-name rate_limits --no-aggregator
```

### Local Development with DynamoDB Local

```bash
# Start DynamoDB Local
docker run -p 8000:8000 amazon/dynamodb-local

# Use endpoint_url
limiter = RateLimiter(
    table_name="rate_limits",
    endpoint_url="http://localhost:8000",
    create_table=True,
)
```

## Development

### Setup

```bash
# Clone repository
git clone https://github.com/zeroae/zae-limiter.git
cd zae-limiter

# Using uv
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"

# Using conda
conda create -n zae-limiter python=3.12
conda activate zae-limiter
pip install -e ".[dev]"
```

### Run Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=zae_limiter --cov-report=html

# Run specific test file
pytest tests/test_limiter.py -v
```

### Code Quality

```bash
# Format and lint
ruff check --fix .
ruff format .

# Type checking
mypy src/zae_limiter
```

## Architecture

### DynamoDB Schema (Single Table)

| Record Type | PK | SK |
|-------------|----|----|
| Entity metadata | `ENTITY#{id}` | `#META` |
| Bucket | `ENTITY#{id}` | `#BUCKET#{resource}#{limit_name}` |
| Limit config | `ENTITY#{id}` | `#LIMIT#{resource}#{limit_name}` |
| Usage snapshot | `ENTITY#{id}` | `#USAGE#{resource}#{window_key}` |

**Indexes:**
- **GSI1**: Parent → Children lookup (`PARENT#{id}` → `CHILD#{id}`)
- **GSI2**: Resource aggregation (`RESOURCE#{name}` → buckets/usage)

### Token Bucket Implementation

- All values stored as **millitokens** (×1000) for precision
- Refill rate stored as **fraction** (amount/period) to avoid floating point
- Supports **negative buckets** for post-hoc reconciliation
- Uses DynamoDB **transactions** for multi-key atomicity

## License

MIT
