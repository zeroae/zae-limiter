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
├── doctest/                     # Documentation code example tests
│   ├── conftest.py              # Ruff config, moto fixtures, skip tags
│   ├── test_docs_lint.py        # Lint all Python blocks with ruff
│   ├── test_docs_run.py         # Execute blocks against moto
│   └── test_docs_integration.py # Execute blocks against LocalStack
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
| **Doctest** | `tests/doctest/` | ruff / moto / LocalStack | Documentation code examples are valid and runnable | Fast |
| **Benchmark** | `tests/benchmark/` | moto or LocalStack | Latency (p50/p95/p99), throughput, cascade overhead | Variable |

## Pytest Markers

| Marker | Description | How to Run |
|--------|-------------|------------|
| (none) | Unit tests | `pytest tests/unit/` |
| `@pytest.mark.integration` | Requires LocalStack | `pytest -m integration` |
| `@pytest.mark.e2e` | End-to-end workflows | `pytest -m e2e` |
| `@pytest.mark.aws` | Real AWS (requires `--run-aws`) | `pytest -m aws --run-aws` |
| `@pytest.mark.benchmark` | Performance benchmarks | `pytest -m benchmark` |
| `@pytest.mark.doctest` | Documentation code examples | `pytest tests/doctest/` |
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
from zae_limiter import Repository, RateLimiter, StackOptions

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

    repo = Repository(
        name=name,
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=StackOptions(enable_aggregator=False),
    )
    limiter = RateLimiter(repository=repo)

    async with limiter:
        yield limiter

    # Cleanup: delete the CloudFormation stack
    await repo.delete_stack()


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

```{.python .lint-only}
@pytest.fixture(scope="session")
async def shared_limiter(localstack_endpoint):
    """
    Session-scoped limiter for faster test execution.

    Trade-off: Tests share state, less isolation.
    """
    repo = Repository(
        name="integration-test-shared",
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=StackOptions(enable_aggregator=False),
    )
    limiter = RateLimiter(repository=repo)

    async with limiter:
        yield limiter

    await repo.delete_stack()
```

### Sync Fixture Example

```{.python .lint-only}
@pytest.fixture(scope="function")
def sync_limiter(localstack_endpoint):
    """Synchronous rate limiter with cleanup."""
    from zae_limiter import SyncRepository, SyncRateLimiter, StackOptions
    import uuid

    name = f"test-sync-{uuid.uuid4().hex[:8]}"

    repo = SyncRepository(
        name=name,
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=StackOptions(enable_aggregator=False),
    )
    limiter = SyncRateLimiter(repository=repo)

    with limiter:
        yield limiter

    repo.delete_stack()
```

## Documentation Code Examples

All Python code blocks in `docs/` are automatically tested using [pytest-examples](https://github.com/pydantic/pytest-examples). Three test passes validate documentation quality:

| Pass | File | What it checks |
|------|------|----------------|
| **Lint** | `test_docs_lint.py` | All blocks pass ruff linting |
| **Run (moto)** | `test_docs_run.py` | Runnable blocks execute against moto |
| **Integration** | `test_docs_integration.py` | LocalStack-tagged blocks execute against LocalStack |

### Running Doc Tests

```bash
# Lint all Python code blocks
uv run pytest tests/doctest/test_docs_lint.py -v -p no:xdist -o "addopts="

# Run executable blocks against moto
uv run pytest tests/doctest/test_docs_run.py -v -p no:xdist -o "addopts="

# Run LocalStack blocks (requires LocalStack running)
uv run pytest tests/doctest/test_docs_integration.py -v -m integration -p no:xdist -o "addopts="
```

### Code Fence Tags

Use tags to classify code blocks that can't run in the default (moto) environment:

| Tag | Effect | When to use |
|-----|--------|-------------|
| (none) | Lint + run with moto | Default for most blocks |
| `{.python .lint-only}` | Lint only, skip execution | Fragments, pseudo-code, undefined helpers |
| `{.python .requires-external}` | Skip lint + execution | Needs packages not in project deps |
| `{.python .requires-localstack}` | Run with LocalStack only | Needs real CloudFormation/Lambda |

The `{.python .tag}` syntax is compatible with MkDocs Material — the extra classes are silently ignored during rendering.

### Adding New Code Blocks

When adding Python code blocks to documentation:

1. Write the block as standard `` ```python ``
2. Run `uv run pytest tests/doctest/ -v -p no:xdist -o "addopts="` to check
3. If the block can't run standalone (uses undefined variables, bare `await`, etc.), change the fence to `` ```{.python .lint-only} ``
4. If the block needs external packages, use `` ```{.python .requires-external} ``

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

Performance benchmarks measure operation latency, throughput, and DynamoDB capacity. Benchmarks are essential for detecting performance regressions when optimizing operations like config caching and cascade resolution.

**Quick Start:**

```bash
# Mocked benchmarks (fast - no Docker needed)
uv run pytest tests/benchmark/test_operations.py -v

# LocalStack benchmarks (realistic latency - requires Docker)
docker compose up -d
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION=us-east-1
uv run pytest tests/benchmark/test_localstack.py -v
docker compose down

# Export results to JSON for comparison
uv run pytest tests/benchmark/ -v --benchmark-json=benchmark.json
```

**Benchmark Categories:**

| Test File | Backend | Purpose | Speed |
|-----------|---------|---------|-------|
| `test_operations.py` | moto (mocked) | Fast iteration, baseline measurements | < 10s |
| `test_localstack.py` | DynamoDB emulation | Realistic network latency, real-world metrics | 30-60s |
| `test_latency.py` | moto | p50/p95/p99 latency breakdown | < 10s |
| `test_throughput.py` | moto | Sequential/concurrent throughput | < 30s |
| `test_capacity.py` | moto | RCU/WCU tracking | < 10s |
| `test_aws.py` | Real AWS | Production metrics (optional) | 60-120s |

**Performance Workflow:**

1. **Establish baseline before optimization:**
   ```bash
   uv run pytest tests/benchmark/test_operations.py -v \
     --benchmark-json=baseline.json
   ```

2. **Implement optimization (e.g., config caching, BatchGetItem)**

3. **Compare against baseline:**
   ```bash
   uv run pytest tests/benchmark/test_operations.py -v \
     --benchmark-compare=baseline.json
   ```

**Key Benchmarks:**

| Benchmark | Purpose | Typical Overhead |
|-----------|---------|------------------|
| `test_acquire_release_single_limit` | Baseline operation | ~1ms (mocked) |
| `test_acquire_with_cached_config` | Config cache hit | < 5% overhead |
| `test_acquire_cold_config` | Config cache miss | < 15% overhead |
| `test_cascade_with_batchgetitem_optimization` | Cascade optimization | 10-20% improvement |
| `test_cascade_with_config_cache_optimization` | Combined optimizations | 20-30% improvement |

**Interpreting Results:**

```
test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit
  mean ± std dev: 1.23 ± 0.15 ms [min: 0.98 ms, max: 1.65 ms] (PASS)
```

- `PASS`: Performance stable (no regression)
- `FAIL`: Regression detected (< -5% typical threshold)
- Positive/negative % = improvement/degradation vs baseline

**Storing Baselines:**

After establishing a good baseline, save it for future comparison:

```bash
# Save baseline with version
cp baseline.json docs/benchmark-v0.11.0.json
git add docs/benchmark-v0.11.0.json

# Compare future runs
pytest tests/benchmark/ -v --benchmark-compare=docs/benchmark-v0.11.0.json
```

**Adding New Benchmarks:**

When adding performance-sensitive code:

1. Create test in appropriate benchmark file
2. Include clear docstring explaining what's measured
3. Compare against related baseline test
4. Use `@pytest.mark.benchmark` marker for filtering
5. Run locally and verify results
6. Document expected performance targets

Example:

```{.python .lint-only}
@pytest.mark.benchmark
def test_acquire_with_new_optimization(self, benchmark, sync_limiter):
    """Measure acquire with new optimization.

    Expected: 10% improvement over baseline due to [reason].
    """
    limits = [Limit.per_minute("rpm", 1_000_000)]

    def operation():
        with sync_limiter.acquire(
            entity_id="bench-opt",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

    benchmark(operation)
```

## CI Configuration

Example GitHub Actions workflow for integration tests:

```yaml
# .github/workflows/ci-tests.yml
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
