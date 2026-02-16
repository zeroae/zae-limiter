# Testing

## Test Directory Structure

```
tests/
├── conftest.py                  # Shared config (--run-aws flag, gevent skip)
├── fixtures/                    # Shared test fixtures package
│   ├── __init__.py
│   ├── moto.py                  # aws_credentials, mock_dynamodb, _patch_aiobotocore_response
│   ├── names.py                 # unique_name, unique_name_class, unique_namespace
│   ├── stacks.py                # SharedStack dataclass, create/destroy helpers, localstack_endpoint
│   ├── repositories.py          # make_test_repo, make_test_limiter, make_sync_test_repo
│   ├── aws_clients.py           # boto3 client factories (cloudwatch, sqs, lambda, s3, dynamodb)
│   ├── polling.py               # poll_for_snapshots, wait_for_event_source_mapping
│   ├── capacity.py              # CapacityCounter, _counting_client, capacity_counter
│   └── doctest_helpers.py       # Mock classes, stubs, DOCS_EXAMPLES_CONFIG, COMMON_ENTITIES
├── unit/                        # Fast tests with mocked AWS (moto)
├── integration/                 # LocalStack tests (repository-level)
├── e2e/                         # Full workflow tests (LocalStack + AWS)
├── benchmark/                   # Performance benchmarks (pytest-benchmark)
└── doctest/                     # Documentation example tests
```

## Fixture Architecture

### Session-scoped shared stacks

Integration, E2E, and benchmark tests share session-scoped CloudFormation stacks instead of creating per-test stacks. Each test gets its own namespace for data isolation within the shared stack.

```
Session event loop (loop_scope="session")
  └─ shared_minimal_stack → SharedStack(name, region, endpoint_url)
  └─ shared_aggregator_stack → same with aggregator Lambda

Function event loop (default)
  └─ test_repo → Repository() on function loop → register_namespace → namespace()

Class event loop (E2E workflow tests, loop_scope="class")
  └─ e2e_limiter → Repository() on class loop → register_namespace → RateLimiter
```

### SharedStack dataclass

`SharedStack` is a frozen dataclass with no active connections. Each consumer creates its own `Repository` on its own event loop, avoiding cross-event-loop async resource sharing.

### Key patterns

- **Session fixtures** use `@pytest_asyncio.fixture(scope="session", loop_scope="session")` with `Repository.builder().build()`
- **Function fixtures** use `make_test_repo(stack, namespace)` to create namespace-scoped repos
- **Class fixtures** (E2E workflows) use `@pytest_asyncio.fixture(scope="class", loop_scope="class")`
- **CLI tests** deploy their own stacks — CLI commands operate on default namespace
- **No cross-module conftest imports** — all shared code lives in `tests/fixtures/`

### Fixture scope selection

| Scope | Use When | Example |
|-------|----------|---------|
| `function` | Test mutates state, needs isolation | `sync_limiter` (each test gets clean state) |
| `class` | Expensive setup shared by class | `e2e_limiter` (CloudFormation stack) |
| `module` | Expensive setup shared by file | `benchmark_entities` (100 pre-warmed entities) |
| `session` | Immutable configuration | `localstack_endpoint` (env var read) |

**Rule**: If fixture setup takes >100ms and is used by multiple tests in the same file, consider `scope="module"`.

**Module-scoped moto fixtures**: Can't use `monkeypatch` (function-scoped). Use `os.environ` directly with manual cleanup in teardown. The `mock_aws()` context manager scopes the mock to the module.

## Test Categories

| Category | Directory | Backend | What to Test | Speed |
|----------|-----------|---------|--------------|-------|
| **Unit** | `tests/unit/` | moto (mocked) | Business logic, bucket math, schema, exceptions | Fast (~seconds) |
| **Integration** | `tests/integration/` | LocalStack | Repository operations, transactions, GSI queries, optimistic locking | Medium |
| **E2E** | `tests/e2e/` | LocalStack or AWS | Full workflows: CLI, rate limiting, hierarchical limits, aggregator | Slow |
| **Benchmark** | `tests/benchmark/` | moto or LocalStack | Latency (p50/p95/p99), throughput, cascade overhead | Variable |

## When to Add Tests

- **New business logic** (bucket calculations, limit validation) → `unit/`
- **New DynamoDB operations** (queries, transactions, GSI) → `integration/`
- **New user-facing features** (CLI commands, rate limiting workflows) → `e2e/`
- **AWS-specific behavior** (alarms, DLQ, CloudWatch metrics) → `e2e/test_aws.py`
- **Performance-sensitive code** (new operations, optimizations) → `benchmark/`

## Pytest Markers

| Marker | Description | How to Run |
|--------|-------------|------------|
| (none) | Unit tests | `pytest tests/unit/` |
| `@pytest.mark.gevent` | Gevent monkey-patching (auto-skipped under xdist) | `pytest tests/unit/ -m gevent -n 0` |
| `@pytest.mark.integration` | Requires LocalStack | `pytest -m integration` (with LocalStack env vars) |
| `@pytest.mark.e2e` | End-to-end workflows | `pytest -m e2e` (with LocalStack env vars) |
| `@pytest.mark.aws` | Real AWS (requires `--run-aws`) | `pytest -m aws --run-aws` |
| `@pytest.mark.benchmark` | Performance benchmarks | `pytest -m benchmark` |
| `@pytest.mark.slow` | Tests with >30s waits | Skip with `-m "not slow"` |
| `@pytest.mark.monitoring` | CloudWatch/DLQ verification | Skip with `-m "not monitoring"` |
| `@pytest.mark.snapshots` | Usage snapshot verification | Skip with `-m "not snapshots"` |

## Running Tests

```bash
# Unit tests only (fast, no Docker)
uv run pytest tests/unit/ -v
# Gevent tests require xdist disabled (monkey-patching incompatible with xdist workers)
uv run pytest tests/unit/ -m gevent -n 0 -v

# Start LocalStack
zae-limiter local up

# Set environment variables for LocalStack
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION=us-east-1

# Integration / E2E / Benchmarks
uv run pytest tests/integration/ -v
uv run pytest tests/e2e/test_localstack.py -v
uv run pytest tests/e2e/test_aws.py --run-aws -v     # Real AWS (costs money!)
uv run pytest tests/benchmark/test_operations.py -v   # Mocked (fast)
uv run pytest tests/benchmark/test_localstack.py -v   # LocalStack (realistic)

# Coverage
pytest --cov=zae_limiter --cov-report=html

# Stop LocalStack
zae-limiter local down
```

## Benchmark Workflow

Benchmarks detect performance regressions. Files in `tests/benchmark/` track latency, throughput, and DynamoDB capacity.

**Important:** `-o "addopts="` disables xdist by overriding `pyproject.toml`. Only use it for benchmarks and gevent tests — all other test runs (unit, integration, E2E) must keep xdist enabled for parallel execution:

```bash
# Run benchmarks (disable xdist with -o "addopts=")
uv run pytest tests/benchmark/ -o "addopts=" -v --benchmark-only

# 1. Baseline before optimization
uv run pytest tests/benchmark/test_operations.py -o "addopts=" -v --benchmark-json=baseline.json

# 2. Make changes, then compare
uv run pytest tests/benchmark/test_operations.py -o "addopts=" -v --benchmark-compare=baseline.json

# 3. Export results for JSON
uv run pytest tests/benchmark/ -o "addopts=" -v --benchmark-json=benchmark.json
```

| Type | File | Backend | Use Case |
|------|------|---------|----------|
| **Operations** | `test_operations.py` | moto | Fast local iteration |
| **LocalStack** | `test_localstack.py` | DynamoDB emulation | Realistic network latency |
| **Latency** | `test_latency.py` | moto | p50/p95/p99 breakdown |
| **Throughput** | `test_throughput.py` | moto | Sequential/concurrent ops |
| **Capacity** | `test_capacity.py` | moto | RCU/WCU tracking |
| **AWS** | `test_aws.py` | Real AWS | Production metrics |
