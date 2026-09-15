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

### Shared stacks are named per pytest session (#577)

The CloudFormation stack is `shared-minimal-<session key>`, never the bare `shared-minimal`, where the key is 8 hex characters derived from the **controller basetemp** (`session_root()` strips the `popen-gwN` suffix an xdist worker gets, so every worker computes the same key). The `FileLock` and the metadata JSON keep their plain names — they already live in a directory private to the session.

The fixed global name was a data-loss bug, not a tidiness problem. `ensure_infrastructure()` is idempotent, so a second concurrent `pytest` invocation silently **adopted** the first one's live stack, and whichever session finished first deleted the table the other was still writing to. The victim saw `ResourceNotFoundException ... non-existent table` surfacing as `RateLimiterUnavailable`, with no assertion ever reached. CI cannot reproduce it — one session per runner — but under this repo's worktree workflow concurrent local sessions are the normal case.

Two rules follow, and both matter:

- **Never delete a stack by a name you did not derive from your own session root.** `_delete_recorded_stack()` refuses any record whose stack name lacks the session key of the directory holding it, which also makes it safe alongside a peer still running the pre-#577 revision.
- **Orphans are reclaimed by pid liveness, never by age.** A run killed before `pytest_sessionfinish` leaks its stack; `pytest_sessionstart` writes `zae-session-owner.pid` into the session root and reaps peer roots whose pid is gone. Sweeping by age would reintroduce the same failure with a longer fuse.

The intended stack name is written to `<base>.pending` **before** the stack is created and removed once `<base>.json` lands. `builder().build()` does several things after `CREATE_COMPLETE` — namespace registration, version record, Lambda update — and a session killed inside that window leaves a live stack that no record names. Only the `.json` means "ready", so a surviving worker still falls through to the idempotent create rather than adopting a half-built stack.

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
pytest --cov --cov-report=html

# Stop LocalStack
zae-limiter local down
```

## Benchmark Workflow

Benchmarks detect performance regressions. Files in `tests/benchmark/` track latency, throughput, and DynamoDB capacity.

**Important:** `-o "addopts="` disables xdist by overriding `pyproject.toml`. Only use it for benchmarks and gevent tests — all other test runs (unit, integration, E2E) must keep xdist enabled for parallel execution:

> **`-o "addopts="` on a full `tests/unit/` run deadlocks.** The `@pytest.mark.gevent` tests are auto-skipped under xdist; disabling xdist un-skips them, so `gevent.monkey.patch_all()` runs in the same process as the asyncio/moto tests and the run hangs indefinitely — no output, no failure, no timeout. A normal full unit run is ~3 minutes, so anything past that is this. Run the two sets separately instead:
>
> ```bash
> uv run pytest tests/unit/ -q          # xdist on, gevent auto-skipped (~3 min)
> uv run pytest tests/unit/ -m gevent -n 0 -q   # gevent only (~2 sec)
> ```
>
> If a run is already hung, `pkill -f "pytest tests/unit"` — a backgrounded pytest that reported "timed out and moved to background" keeps running and will not exit on its own.

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
