# Testing

This guide covers test organization, pytest fixtures, and CI configuration for zae-limiter.

## Test Organization

Tests are organized by execution environment and scope:

```
tests/
├── conftest.py                  # Shared config (--run-aws flag)
├── unit/                        # Fast tests with mocked AWS (moto)
│   ├── test_limiter.py
│   ├── test_repository.py
│   └── test_sync_limiter.py
├── integration/                 # LocalStack tests (repository-level)
│   └── test_repository.py
├── e2e/                         # Full workflow tests (LocalStack + AWS)
│   ├── test_localstack.py
│   └── test_aws.py
└── benchmark/                   # Performance benchmarks (pytest-benchmark)
    ├── test_operations.py       # Mocked benchmarks
    └── test_localstack.py       # LocalStack benchmarks
```

## Test Categories

| Category | Directory | Backend | What to Test | Speed |
|----------|-----------|---------|--------------|-------|
| **Unit** | `tests/unit/` | moto (mocked) | Business logic, bucket math, schema, exceptions | Fast (~seconds) |
| **Integration** | `tests/integration/` | LocalStack | Repository operations, transactions, GSI queries | Medium |
| **E2E** | `tests/e2e/` | LocalStack or AWS | Full workflows: CLI, rate limiting, hierarchical limits | Slow |
| **Benchmark** | `tests/benchmark/` | moto or LocalStack | Latency (p50/p95/p99), throughput, cascade overhead | Variable |

## Pytest Markers

| Marker | Description | How to Run |
|--------|-------------|------------|
| (none) | Unit tests | `pytest tests/unit/` |
| `@pytest.mark.integration` | Requires LocalStack | `pytest -m integration` |
| `@pytest.mark.e2e` | End-to-end workflows | `pytest -m e2e` |
| `@pytest.mark.aws` | Real AWS (requires `--run-aws`) | `pytest -m aws --run-aws` |
| `@pytest.mark.benchmark` | Performance benchmarks | `pytest -m benchmark` |
| `@pytest.mark.slow` | Tests with >30s waits | Skip with `-m "not slow"` |

## pytest Fixtures

### LocalStack Endpoint Fixture

```python
import os
import pytest

@pytest.fixture
def localstack_endpoint():
    """Get LocalStack endpoint from environment."""
    return os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566")
```

### Function-Scoped Limiter (Isolated)

```python
import uuid
import pytest
from zae_limiter import RateLimiter, StackOptions

@pytest.fixture(scope="function")
async def limiter(localstack_endpoint):
    """
    Create a rate limiter connected to LocalStack with automatic cleanup.

    This fixture:
    1. Creates a unique stack for test isolation
    2. Yields the limiter for test use
    3. Deletes the stack in teardown
    """
    # Unique name prevents test interference
    name = f"test-{uuid.uuid4().hex[:8]}"

    limiter = RateLimiter(
        name=name,  # Creates ZAEL-test-{uuid} resources
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=StackOptions(enable_aggregator=False),
    )

    async with limiter:
        yield limiter

    # Cleanup: delete the CloudFormation stack
    await limiter.delete_stack()


@pytest.mark.integration
async def test_rate_limiting(limiter):
    async with limiter.acquire(
        entity_id="test-user",
        resource="api",
        limits=[Limit.per_minute("requests", 10)],
        consume={"requests": 1},
    ):
        pass  # Success
```

### Session-Scoped Limiter (Faster)

For test suites where stack creation overhead is significant:

```python
@pytest.fixture(scope="session")
async def shared_limiter(localstack_endpoint):
    """
    Session-scoped limiter for faster test execution.

    Trade-off: Tests share state, less isolation.
    """
    limiter = RateLimiter(
        name="integration-test-shared",
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=StackOptions(enable_aggregator=False),
    )

    async with limiter:
        yield limiter

    await limiter.delete_stack()
```

### Sync Fixture Example

```python
@pytest.fixture(scope="function")
def sync_limiter(localstack_endpoint):
    """Synchronous rate limiter with cleanup."""
    from zae_limiter import SyncRateLimiter, StackOptions
    import uuid

    name = f"test-sync-{uuid.uuid4().hex[:8]}"

    limiter = SyncRateLimiter(
        name=name,
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=StackOptions(enable_aggregator=False),
    )

    with limiter:
        yield limiter

    limiter.delete_stack()
```

## Running Tests

### Unit Tests Only (No Docker)

```bash
pytest tests/unit/ -v
```

### Integration Tests (Requires LocalStack)

```bash
# Start LocalStack
docker compose up -d

# Set environment variables
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION=us-east-1

# Run integration tests
pytest tests/integration/ -v

# Stop LocalStack
docker compose down
```

### E2E Tests

```bash
# LocalStack E2E
pytest tests/e2e/test_localstack.py -v

# Real AWS E2E (costs money!)
pytest tests/e2e/test_aws.py --run-aws -v
```

### Benchmarks

```bash
# Mocked benchmarks (fast)
pytest tests/benchmark/test_operations.py -v

# LocalStack benchmarks (realistic latency)
pytest tests/benchmark/test_localstack.py -v

# Export results to JSON
pytest tests/benchmark/ -v --benchmark-json=benchmark.json
```

## CI Configuration

Example GitHub Actions workflow for integration tests:

```yaml
# .github/workflows/ci.yml
jobs:
  integration:
    runs-on: ubuntu-latest
    services:
      localstack:
        image: localstack/localstack
        ports:
          - 4566:4566
        env:
          SERVICES: dynamodb,dynamodbstreams,lambda,cloudformation,logs,iam,cloudwatch,sqs
        options: >-
          --mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock
    steps:
      - uses: actions/checkout@v4
      - run: pip install -e ".[dev]"
      - run: pytest -m integration
        env:
          AWS_ENDPOINT_URL: http://localhost:4566
          AWS_ACCESS_KEY_ID: test
          AWS_SECRET_ACCESS_KEY: test
```

## When to Add Tests

- **New business logic** (bucket calculations, limit validation) → `unit/`
- **New DynamoDB operations** (queries, transactions, GSI) → `integration/`
- **New user-facing features** (CLI commands, rate limiting workflows) → `e2e/`
- **AWS-specific behavior** (alarms, DLQ, CloudWatch metrics) → `e2e/test_aws.py`
- **Performance-sensitive code** (new operations, optimizations) → `benchmark/`

## Test Coverage

```bash
pytest --cov=zae_limiter --cov-report=html
open htmlcov/index.html
```

## Next Steps

- [LocalStack](localstack.md) - Local AWS development environment
- [Architecture](architecture.md) - Understanding the codebase
