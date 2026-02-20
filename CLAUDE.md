# CLAUDE.md - Instructions for AI Assistants

This file provides context for AI assistants working on the zae-limiter codebase.

## Project Overview

zae-limiter is a rate limiting library backed by DynamoDB using the token bucket algorithm. It excels at scenarios where:
- Multiple limits are tracked per call (rpm, tpm)
- Consumption is unknown upfront (adjust after the operation completes)
- Hierarchical limits exist (API key → project, tenant → user)
- Cost matters (~$0.75/1M requests)

**Project scopes:** `limiter`, `bucket`, `cli`, `infra`, `ci`, `aggregator`, `models`, `schema`, `repository`, `lease`, `exceptions`, `cache`, `test`, `benchmark`, `local`, `loadtest`. See `release-planning.md` for area labels.

## Build & Development

### Using uv (preferred)

```bash
# Setup (one-time)
uv sync --all-extras
uv tool install hatch  # Install hatch for sync code generation
pre-commit install  # Install git hooks for linting

# Deploy infrastructure (CloudFormation)
uv run zae-limiter deploy --name limiter --region us-east-1

# Run tests
uv run pytest

# Type check
uv run mypy src/zae_limiter

# Lint (or let pre-commit run automatically on commit)
uv run ruff check --fix .
uv run ruff format .

# Run all pre-commit hooks manually
pre-commit run --all-files

# Lint CloudFormation template (after modifying cfn_template.yaml)
uv run cfn-lint src/zae_limiter/infra/cfn_template.yaml
```

### Sync Code Generation

Native sync code is generated from async source via AST transformation (see ADR-121). The transformer handles `asyncio.gather(a, b)` by converting it to `self._run_in_executor(lambda: a, lambda: b)`, with a configurable `parallel_mode` parameter on `SyncRepository` that controls the execution strategy:

| Mode | Behavior |
|------|----------|
| `"auto"` (default) | Silently picks the best strategy: gevent (if monkey-patched) -> serial (if single-CPU) -> threadpool (multi-CPU) |
| `"gevent"` | Forces gevent greenlets; **warns** if monkey-patching is not active (proceeds running like serial) |
| `"threadpool"` | Lazy `ThreadPoolExecutor(max_workers=2)`, created on first cascade request; **warns** on single-CPU hosts about GIL contention |
| `"serial"` | Sequential execution (no parallelism) |

All explicit modes warn (not error) when conditions are suboptimal. Auto mode silently selects the best strategy without warnings. Resolution happens once at `SyncRepository.__init__` time (not per-call). Usage:

```python
repo = SyncRepository.connect("my-app", "us-east-1", parallel_mode="gevent")
limiter = SyncRateLimiter(repository=repo)
```

```bash
# Generate sync code after modifying async source
hatch run generate-sync

# Or directly
python scripts/generate_sync.py
```

**Generated source files (DO NOT EDIT):**
- `sync_repository_protocol.py` ← `repository_protocol.py`
- `sync_repository.py` ← `repository.py`
- `sync_repository_builder.py` ← `repository_builder.py`
- `sync_limiter.py` ← `limiter.py`
- `sync_lease.py` ← `lease.py`
- `sync_config_cache.py` ← `config_cache.py`
- `infra/sync_stack_manager.py` ← `infra/stack_manager.py`
- `infra/sync_discovery.py` ← `infra/discovery.py`

**Generated test files (DO NOT EDIT):**
- `tests/unit/test_sync_limiter.py` ← `tests/unit/test_limiter.py`
- `tests/unit/test_sync_repository.py` ← `tests/unit/test_repository.py`
- `tests/unit/test_sync_stack_manager.py` ← `tests/unit/test_stack_manager.py`
- `tests/unit/test_sync_discovery.py` ← `tests/unit/test_discovery.py`
- `tests/unit/test_sync_config_cache.py` ← `tests/unit/test_config_cache.py`

Pre-commit hook verifies generated code is up-to-date. CI also verifies before running tests.

### Using conda

```bash
# Setup (one-time)
conda create -n zae-limiter python=3.12
conda activate zae-limiter
pip install -e ".[dev]"
pytest
```

## Infrastructure Deployment

### CloudFormation Stack

The library uses CloudFormation for infrastructure deployment. The `deploy` command automatically:
1. Creates CloudFormation stack with DynamoDB table, streams, and Lambda function
2. Packages and deploys the Lambda aggregator code from the installed package
3. Initializes the version record and registers the "default" namespace

```bash
# Basic deployment
zae-limiter deploy --name my-app --region us-east-1

# Deploy to LocalStack
zae-limiter deploy --name my-app --endpoint-url http://localhost:4566 --region us-east-1

# Deploy without aggregator Lambda
zae-limiter deploy --name my-app --no-aggregator

# Enterprise deployment (permission boundary + custom role naming)
zae-limiter deploy --name my-app \
  --permission-boundary arn:aws:iam::aws:policy/PowerUserAccess \
  --role-name-format "pb-{}-PowerUser"

# Other deploy flags: --lambda-timeout, --lambda-memory, --log-retention-days,
# --alarm-sns-topic, --no-alarms, --no-audit-archival, --enable-tracing,
# --create-iam-roles, --role-name-format, --policy-name-format,
# --iam/--no-iam, --aggregator-role-arn

# Stack management
zae-limiter status --name my-app --region us-east-1
zae-limiter delete --name my-app --yes

# Export for custom deployment
zae-limiter cfn-template > template.yaml
zae-limiter lambda-export --output lambda.zip
```

**Lambda Deployment Details:**
- The CLI automatically builds a deployment package using `aws-lambda-builders` for cross-platform compatibility
- Only `[lambda]` extra dependencies (aws-lambda-powertools) are pip-installed; `boto3` is provided by the Lambda runtime
- The `zae_limiter_aggregator` package and a minimal `zae_limiter` stub are copied into the zip: `schema.py`, `bucket.py` (refill math for aggregator-assisted refill), `models.py` (dataclasses used by bucket.py), and `exceptions.py` (exceptions used by models.py)
- Lambda code is updated via AWS Lambda API after stack creation
- No S3 bucket required - deployment package is uploaded directly
- No Docker required - `aws-lambda-builders` handles platform-specific wheels

### Declarative Infrastructure

Use `Repository.builder()` or CLI to provision infrastructure, then `Repository.connect()` in application code:

```python
from zae_limiter import RateLimiter, Repository

# Application code — connect to existing infrastructure
repo = await Repository.connect("my-app", "us-east-1")
limiter = RateLimiter(repository=repo)

# Infrastructure provisioning — builder handles infra + namespace in one call
repo = await (
    Repository.builder("my-app", "us-east-1")
    .namespace("default")
    .build()
)

# Enterprise deployment with permission boundary and custom role naming
repo = await (
    Repository.builder("my-app", "us-east-1")
    .permission_boundary("arn:aws:iam::aws:policy/PowerUserAccess")
    .role_name_format("pb-{}-PowerUser")
    .policy_name_format("pb-{}-PowerUser")
    .build()
)
```

Other builder methods: `.lambda_memory()`, `.usage_retention_days()`, `.audit_retention_days()`, `.enable_alarms()`, `.alarm_sns_topic()`, `.enable_audit_archival()`, `.audit_archive_glacier_days()`, `.enable_tracing()`, `.create_iam_roles()`, `.create_iam()`, `.aggregator_role_arn()`, `.enable_deletion_protection()`, `.tags()`.

**IAM Resource Defaults (ADR-117):**
- **Managed policies** are **created by default** — both table-level (`acq`, `full`, `read`) and namespace-scoped (`ns-acq`, `ns-full`, `ns-read`)
- **IAM roles** are **opt-in** (set `create_iam_roles=True` to create them)
- Users can attach managed policies to their own roles, users, or federated identities
- **Skip all IAM** with `create_iam=False` or `--no-iam` for restricted IAM environments
- **External Lambda role** with `aggregator_role_arn` or `--aggregator-role-arn` to use pre-existing role

**When to use `connect()` vs `builder()` vs CLI:**
- **`connect()`**: Application code at startup, connecting to pre-existing infrastructure
- **`builder().build()`**: Self-contained apps, serverless deployments, minimal onboarding friction
- **CLI**: Strict infra/app separation, audit requirements, Terraform/CDK integration

### Local Development with LocalStack

LocalStack provides full AWS service emulation (CloudFormation, DynamoDB, Streams, Lambda). Use the `zae-limiter local` CLI commands (preferred) or `docker-compose.yml`:

```bash
# Start LocalStack with CLI (preferred — source of truth for container config)
zae-limiter local up

# Start and deploy a stack in one step
zae-limiter local up --name my-app

# Check status / stream logs / stop
zae-limiter local status
zae-limiter local logs --follow
zae-limiter local down
```

**Important:** The Docker socket mount is required for LocalStack to spawn Lambda functions as Docker containers. Without it, CloudFormation stack creation will fail when the aggregator Lambda is enabled.

**Note:** CloudFormation is used for all deployments, including LocalStack. The `endpoint_url` parameter configures the AWS endpoint for all services. See `localstack-parity.md` for keeping CLI, `docker-compose.yml`, and CI in sync.

### Load Testing

The `zae-limiter loadtest` commands deploy a distributed Locust cluster using ECS Fargate (master) + Lambda (workers). See `examples/locust/` for locustfile scenarios.

```bash
# Deploy load test infrastructure
zae-limiter loadtest deploy -n my-app -C examples/locust

# Push updated locustfiles and Lambda code
zae-limiter loadtest push -n my-app -C examples/locust

# Open Locust web UI via SSM tunnel (auto-opens browser when ready)
zae-limiter loadtest ui -n my-app -f locustfiles/simple.py

# Run a headless load test (Lambda mode, default)
zae-limiter loadtest run -n my-app -f locustfiles/max_rps.py --users 20 --duration 60

# Run specific user classes (positional args, Locust native style)
zae-limiter loadtest run -n my-app -f locustfiles/max_rps.py MaxRpsCascadeUser

# Run distributed (Fargate master + Lambda workers)
zae-limiter loadtest run -n my-app -f locustfiles/max_rps.py --workers 10 --users 100

# Calibrate optimal per-worker concurrency
zae-limiter loadtest tune -n my-app -f locustfiles/max_rps.py
zae-limiter loadtest tune -n my-app -f locustfiles/max_rps.py MaxRpsCascadeUser

# List / delete
zae-limiter loadtest list
zae-limiter loadtest delete -n my-app --yes
```

**User class selection:** The `loadtest run` and `loadtest tune` commands accept user class names as positional arguments to select which Locust user classes to run. When omitted, all classes in the locustfile are used.

**Locustfile user classes:**

| File | Class | Description |
|------|-------|-------------|
| `simple.py` | `SimpleUser` | Standalone entities, no cascade |
| `simple.py` | `SimpleCascadeUser` | Child entities with `cascade=True` to shared parent |
| `max_rps.py` | `MaxRpsUser` | Zero-wait standalone, max throughput |
| `max_rps.py` | `MaxRpsCascadeUser` | Zero-wait cascade, measures cascade overhead |

Cascade classes create child entities under a shared parent and set `cascade=True`, so every `acquire()` writes to both child and parent buckets. This is used to benchmark cascade overhead against standalone operation.

## Project Structure

```
src/zae_limiter/
├── __init__.py        # Public API exports
├── models.py          # Limit, Entity, LimitStatus, BucketState, StackOptions, AuditEvent, AuditAction, UsageSnapshot, UsageSummary, LimiterInfo, BackendCapabilities, Status, LimitName, ResourceCapacity, EntityCapacity
├── exceptions.py      # RateLimitExceeded, RateLimiterUnavailable, StackCreationError, VersionError, ValidationError, EntityNotFoundError, InfrastructureNotFoundError, NamespaceNotFoundError
├── naming.py          # Resource name validation (ZAEL- prefix retained for legacy discovery)
├── bucket.py          # Token bucket math (integer arithmetic)
├── schema.py          # DynamoDB key builders (namespace-prefixed)
├── repository_protocol.py  # RepositoryProtocol for backend abstraction
├── repository.py      # DynamoDB operations (namespace-aware)
├── repository_builder.py   # RepositoryBuilder (fluent async construction)
├── lease.py           # Lease context manager
├── limiter.py         # RateLimiter (async)
├── config_cache.py    # Client-side config caching with TTL (CacheStats)
├── sync_repository_protocol.py  # Generated: SyncRepositoryProtocol
├── sync_repository.py           # Generated: SyncRepository
├── sync_repository_builder.py   # Generated: SyncRepositoryBuilder
├── sync_limiter.py              # Generated: SyncRateLimiter
├── sync_lease.py                # Generated: SyncLease
├── sync_config_cache.py         # Generated: SyncConfigCache
├── locust.py          # Locust load testing integration (RateLimiterUser, RateLimiterSession)
├── cli.py             # CLI commands (deploy, delete, status, list, cfn-template, lambda-export, version, upgrade, check, audit, usage, entity, resource, system, namespace, local, loadtest)
├── version.py         # Version tracking and compatibility
├── loadtest/          # Load testing infrastructure (deploy, push, ui, run, tune, delete, list)
│   ├── __init__.py
│   ├── cli.py             # CLI commands for load test lifecycle
│   ├── builder.py         # Docker image builder for Locust master
│   ├── lambda_builder.py  # Lambda deployment package for load workers
│   ├── orchestrator.py    # ECS orchestrator for auto-scaling Lambda workers
│   ├── lambda/
│   │   ├── __init__.py
│   │   └── worker.py      # Lambda worker handler (headless and distributed modes)
│   └── cfn_template.yaml  # CloudFormation template for load test stack
├── migrations/        # Schema migration framework
│   └── __init__.py    # Migration registry and runner
├── visualization/     # Usage snapshot formatting and display
│   ├── __init__.py    # UsageFormatter enum, format_usage_snapshots()
│   ├── factory.py     # Formatter factory
│   ├── formatters.py  # PlotFormatter (ASCII charts)
│   └── table.py       # TableFormatter for tabular output
└── infra/
    ├── stack_manager.py         # CloudFormation stack operations
    ├── sync_stack_manager.py    # Generated: SyncStackManager
    ├── discovery.py             # Multi-stack discovery and listing
    ├── sync_discovery.py        # Generated: SyncInfrastructureDiscovery
    ├── lambda_builder.py        # Lambda deployment package builder
    └── cfn_template.yaml        # CloudFormation template

src/zae_limiter_aggregator/   # Lambda aggregator (top-level package)
├── __init__.py               # Re-exports handler, processor types (ProcessResult, ConsumptionDelta, BucketRefillState, LimitRefillInfo, ParsedBucketRecord, ParsedBucketLimit)
├── handler.py                # Lambda entry point (returns refills_written count)
├── processor.py              # Stream processing: usage snapshots + bucket refill (Issue #317)
└── archiver.py               # S3 audit archival (gzip JSONL)
```

### Repository Pattern (v0.5.0+)

The `Repository` class owns data access and infrastructure management. `RateLimiter` owns business logic.

#### Repository.connect() (Recommended)

Use `Repository.connect()` for application code connecting to existing infrastructure:

```python
from zae_limiter import RateLimiter, Repository

# Basic usage — connect to existing infrastructure
repo = await Repository.connect("my-app", "us-east-1")
limiter = RateLimiter(repository=repo)

# Multi-tenant — each tenant gets an isolated namespace
repo_alpha = await Repository.connect("my-app", "us-east-1", namespace="tenant-alpha")
limiter_alpha = RateLimiter(repository=repo_alpha)

# With custom config cache TTL
repo = await Repository.connect("my-app", "us-east-1", config_cache_ttl=120)

# LocalStack development
repo = await Repository.connect(
    "my-app", "us-east-1",
    endpoint_url="http://localhost:4566",
)
```

**`connect()` steps:**
1. Create Repository instance (no deprecation warning)
2. Resolve namespace name to opaque ID (must already exist)
3. Reinitialize config cache with resolved namespace ID
4. Version check and Lambda auto-update (skip for local endpoints)

**`connect()` does NOT:** create tables, register namespaces, or provision infrastructure. Raises `NamespaceNotFoundError` if the namespace is not registered.

#### RepositoryBuilder (Infrastructure Provisioning)

Use `Repository.builder()` for infrastructure provisioning (like `terraform deploy`):

```python
from zae_limiter import RateLimiter, Repository

# Provision infrastructure + register namespace
repo = await (
    Repository.builder("my-app", "us-east-1")
    .namespace("default")       # Resolve namespace (default: "default")
    .config_cache_ttl(120)      # Config cache TTL in seconds
    .build()                    # Async: creates infra, registers default ns, resolves namespace
)
limiter = RateLimiter(repository=repo)

# With infrastructure options
repo = await (
    Repository.builder("my-app", "us-east-1")
    .lambda_memory(512)
    .enable_alarms(False)
    .permission_boundary("arn:aws:iam::aws:policy/PowerUserAccess")
    .role_name_format("PowerUserPB-{}")
    .policy_name_format("PowerUserPB-{}")
    .build()
)

# LocalStack development
repo = await (
    Repository.builder("my-app", "us-east-1", endpoint_url="http://localhost:4566")
    .enable_aggregator(False)
    .enable_alarms(False)
    .build()
)
```

**Builder `build()` steps:**
1. Construct Repository with materialized StackOptions (if any infra options set)
2. Ensure infrastructure exists (no-op if no infra options)
3. Register the "default" namespace (conditional PutItem, no-op if exists)
4. Resolve the requested namespace name to an opaque ID
5. Reinitialize config cache with resolved namespace ID
6. Version check and Lambda auto-update (skip for local endpoints)

**When to use `connect()` vs `builder()`:**
- **`connect()`**: Application code at startup, connecting to pre-existing infrastructure
- **`builder().build()`**: Infrastructure provisioning, first-time setup, CI/CD pipelines

**Config ownership (connect/builder vs deprecated RateLimiter params):**

| Parameter | `connect()` / `builder()` | RateLimiter (deprecated) |
|-----------|---------------------------|--------------------------|
| `namespace` | `connect(namespace=...)` / `.namespace("tenant-a")` | N/A |
| `config_cache_ttl` | `connect(config_cache_ttl=...)` / `.config_cache_ttl(120)` | N/A (was on Repository constructor) |
| `auto_update` | `connect(auto_update=...)` / `.auto_update(True)` | `auto_update=True` (deprecated) |
| `bucket_ttl_multiplier` | `.bucket_ttl_multiplier(7)` | `bucket_ttl_refill_multiplier=7` (deprecated) |
| `on_unavailable` | `.on_unavailable("allow")` | `on_unavailable="allow"` (deprecated) |
| `name/region/endpoint_url` | `connect(name, region)` / `builder(name, region)` | `RateLimiter(name=..., region=...)` (deprecated) |
| `stack_options` | Individual builder methods | `RateLimiter(stack_options=...)` (deprecated) |
| Infrastructure options | `.lambda_memory()`, `.enable_alarms()`, etc. | Via `StackOptions` dataclass |

#### Scoped Repositories (Namespace Switching)

After connecting or building, use `repo.namespace()` to get a scoped Repository for a different namespace:

```python
# Register additional namespaces (requires builder or admin access)
await repo.register_namespace("tenant-beta")

# Get scoped repo (shares client, entity cache, namespace cache)
repo_beta = await repo.namespace("tenant-beta")
limiter_beta = RateLimiter(repository=repo_beta)
```

#### Legacy API (Deprecated)

```python
# Old pattern (deprecated, emits DeprecationWarning):
repo = Repository(
    name="my-app",
    region="us-east-1",
    stack_options=StackOptions(lambda_memory=512),
)
await repo.ensure_infrastructure()
limiter = RateLimiter(repository=repo)
```

See [ADR-108](docs/adr/108-repository-protocol.md) and [ADR-110](docs/adr/110-deprecation-constructor.md) for details.

## Naming Convention

### Resource Naming

Users provide a short identifier (e.g., `my-app`), and the system uses it directly as the stack and resource name:

| User Provides | AWS Resources |
|---------------|---------------|
| `limiter` | `limiter` (stack, table, Lambda, etc.) |
| `my-app` | `my-app` (stack, table, Lambda, etc.) |

**Key points:**
- Stack name = user-provided name directly
- Table name = stack name
- Lambda function name: `{name}-aggregator`
- DLQ name: `{name}-aggregator-dlq`
- IAM roles: `{name}-aggr`, `{name}-app`, `{name}-admin`, `{name}-read` (ADR-116)
- Log group: `/aws/lambda/{name}-aggregator`
- S3 audit archive bucket name is auto-generated by CloudFormation
- The `name` parameter is cloud-agnostic (not tied to CloudFormation terminology)
- Names must use **hyphens** (not underscores) due to CloudFormation rules
- Names must start with a letter and contain only alphanumeric characters and hyphens
- Maximum identifier length: 55 characters (IAM role name constraints)

**IAM Role Naming (ADR-116):**
- Pattern: `{role_name_format}.replace("{}", f"{stack_name}-{component}")`
- Components: `aggr` (Lambda), `app`, `admin`, `read`
- All components ≤ 8 characters (invariant for upgrade safety)
- Default names: `{stack}-aggr`, `{stack}-app`, `{stack}-admin`, `{stack}-read`
- Roles are **opt-in** (set `create_iam_roles=True`)

**IAM Managed Policy Naming (ADR-117):**
- Pattern: `{policy_name_format}.replace("{}", f"{stack_name}-{component}")`
- **Table-level components:** `acq` (AcquireOnlyPolicy), `full` (FullAccessPolicy), `read` (ReadOnlyPolicy)
- **Namespace-scoped components:** `ns-acq` (NamespaceAcquirePolicy), `ns-full` (NamespaceFullAccessPolicy), `ns-read` (NamespaceReadOnlyPolicy)
- Default names: `{stack}-acq`, `{stack}-full`, `{stack}-read`, `{stack}-ns-acq`, `{stack}-ns-full`, `{stack}-ns-read`
- Maximum policy name length: 120 characters (`policy_name_format` max length)
- Policies are **always created** regardless of `create_iam_roles` setting

**Two-tier IAM Policy Model:**

| Tier | Policies | Scope | Use Case |
|------|----------|-------|----------|
| **Table-level** (admin) | `acq`, `full`, `read` | Full table access | Platform admins, cross-namespace operations |
| **Namespace-scoped** (tenant) | `ns-acq`, `ns-full`, `ns-read` | Single namespace via TBAC | Tenant applications, isolated access |

**Namespace-scoped policies** use Tag-Based Access Control (TBAC) with `dynamodb:LeadingKeys` condition:
- Restrict DynamoDB access to items prefixed with the caller's `zael_namespace_id` principal tag
- Also grant read access to the reserved namespace `_/*` (namespace registry, shared config)
- Attach the `zael_namespace_id` tag to IAM roles/users to scope their access

```bash
# Tag an IAM role for namespace access
aws iam tag-role --role-name my-app-role \
  --tags Key=zael_namespace_id,Value=<opaque-namespace-id>
```

**Invalid names (rejected by validation):**
- `rate_limits` (underscores not allowed)
- `my.app` (periods not allowed)
- `123app` (must start with letter)

### Rate Limiting Resource Names

Resource names (used in `acquire()`, `set_resource_defaults()`, etc.) have different rules than stack names:

| Character | Allowed |
|-----------|---------|
| Letters | ✅ |
| Numbers | ✅ (not first char) |
| Underscore `_` | ✅ |
| Hyphen `-` | ✅ |
| Dot `.` | ✅ |
| Slash `/` | ✅ (for provider/model grouping) |
| Hash `#` | ❌ (DynamoDB delimiter) |

**Valid resource names:**
- `api`, `gpt-4`, `gpt-3.5-turbo`
- `openai/gpt-4`, `anthropic/claude-3` (provider/model grouping)
- `anthropic/claude-3/opus` (nested paths)

**Note:** Limit names (e.g., `rpm`, `tpm`) do NOT allow slashes.

### Hot Partition Risk Mitigation (Issue #116)

Cascade (`cascade=True`) causes parent entities to receive traffic proportional to child count. High-fanout parents (1000+ children) risk exceeding per-partition throughput (~3,000 RCU / 1,000 WCU).

**Decision tree:**
- 0-500 children with cascade: Safe, no action needed
- 500-1000 children with cascade: Monitor with Contributor Insights
- 1000+ children with cascade: Implement write sharding (see [Performance Guide](docs/performance.md#write-sharding-for-high-fanout-parents)) or disable cascade

Primary mitigation: cascade defaults to `False`.

## Key Design Decisions

### Integer Arithmetic for Precision
- All token values stored as **millitokens** (x1000)
- Refill rates stored as fraction: `refill_amount / refill_period_seconds`
- Avoids floating point precision issues in distributed systems

### Token Bucket Algorithm
- Buckets can go **negative** for post-hoc reconciliation
- Refill is calculated lazily on each access
- `capacity` is the bucket ceiling (maximum tokens)

### DynamoDB Single Table Design
- All entities, buckets, limits, usage in one table
- All PK and GSI PK values are namespace-prefixed: `{namespace_id}/PREFIX#value`
- GSI1: Parent -> Children lookups
- GSI2: Resource aggregation (capacity tracking)
- GSI3: Entity config queries (sparse - only entity configs indexed)
- GSI4: Namespace-scoped item discovery (KEYS_ONLY projection, used by `purge_namespace()`)
- Uses TransactWriteItems for atomic multi-entity writes (initial consumption)
- Uses independent single-item writes (`write_each`) for adjustments and rollbacks (1 WCU each)

### Speculative Writes (Issue #315)
- Enabled by default (`speculative_writes=True`); disable with `speculative_writes=False`
- Skips the read round trip (BatchGetItem) by issuing a conditional UpdateItem directly
- Uses `ReturnValuesOnConditionCheckFailure=ALL_OLD` to inspect bucket state on failure
- Falls back to the normal read-write path when the bucket is missing, config changed, or refill would help
- Fast rejection: if refill would not help, raises `RateLimitExceeded` immediately (0 RCU, 0 WCU)
- Cascade/parent_id denormalized into bucket items to avoid entity metadata lookup on the fast path
- **Parallel cascade writes (Issue #318):** After the first acquire populates the entity cache, subsequent cascade acquires issue child + parent speculative writes concurrently via `asyncio.gather` (async) or `SyncRepository._run_in_executor` (sync, strategy controlled by `parallel_mode` parameter), reducing cascade latency from 2 sequential round trips to 1 parallel round trip

### Aggregator-Assisted Bucket Refill (Issue #317)
- The Lambda aggregator proactively refills token buckets for active entities via DynamoDB Streams
- Keeps speculative writes on the fast path (1 RT) by ensuring buckets have sufficient tokens, avoiding fallback to the slow path (3 RT)
- Uses `aggregate_bucket_states()` to accumulate `tc` deltas and last NewImage per (entity_id, resource) across stream records in a batch
- `try_refill_bucket()` computes refill via `refill_bucket()` from `bucket.py`, only writes if projected tokens are insufficient to cover the observed consumption rate
- Uses `ADD` for token deltas (commutative with concurrent speculative writes) and an optimistic lock on the shared `rf` timestamp (`ConditionExpression: rf = :expected_rf`) to prevent double-refill with the client slow path
- `ConditionalCheckFailedException` is silently skipped (another writer updated `rf` first)
- New types: `ParsedBucketRecord`, `ParsedBucketLimit` (shared stream record parsing), `BucketRefillState`, `LimitRefillInfo` (per-bucket aggregated state for refill decisions)
- `ProcessResult` includes `refills_written` field; handler response body includes the count

### Exception Design
- `RateLimitExceeded` includes **ALL** limit statuses
- Both `violations` (exceeded) and `passed` (ok) are available
- `retry_after_seconds` calculated from primary bottleneck

## Common Tasks

### Adding a New Limit Type
1. No code changes needed - `Limit.custom()` supports any configuration
2. For convenience, add factory method to `Limit` class in `models.py`

### Modifying the Schema
1. Update key builders in `schema.py`
2. Update serialization in `repository.py`
3. Update CloudFormation template in `infra/cfn_template.yaml`
4. Be careful with backwards compatibility

### Adding New Exception Fields
1. Update `LimitStatus` in `models.py`
2. Update `RateLimitExceeded.as_dict()` in `exceptions.py`
3. Update tests in `test_limiter.py`

## Documentation

### Docs Framework

The project uses **MkDocs Material** for documentation. Configuration is in `mkdocs.yml`.

```bash
# Preview docs locally (--livereload required due to Click 8.3.x bug)
uv run mkdocs serve --livereload --dirty
```

Use **Mermaid** for all diagrams (MkDocs Material has built-in support).

### Docs Structure

Documentation is organized by **audience** with 4 top-level sections:

```
docs/
├── index.md                 # Landing page
├── getting-started.md       # Installation, first deployment
│
├── guide/                   # User Guide (library users)
│   ├── basic-usage.md       # Rate limiting patterns, error handling
│   ├── hierarchical.md      # Parent/child entities, cascade mode
│   ├── llm-integration.md   # Token estimation and reconciliation
│   └── unavailability.md    # Error handling strategies
│
├── infra/                   # Operator Guide (ops/platform teams)
│   ├── deployment.md        # CLI deployment, declarative infrastructure
│   ├── production.md        # Security, multi-region, cost
│   ├── cloudformation.md    # Template customization
│   └── auditing.md          # Audit logging and compliance
├── operations/              # Troubleshooting runbooks
├── monitoring.md            # Dashboards, alerts, Logs Insights
├── performance.md           # Capacity planning, optimization
├── migrations.md            # Schema migrations
│
├── cli.md                   # Reference: CLI commands
├── api/                     # Reference: API documentation
│
└── contributing/            # Contributors (developers)
    ├── index.md             # Quick start, links to CLAUDE.md
    ├── development.md       # Environment setup, code quality
    ├── localstack.md        # Local AWS development (developer-only)
    ├── testing.md           # Test organization, pytest fixtures
    └── architecture.md      # DynamoDB schema, token bucket
```

**Key organization decisions:**
- **LocalStack is developer-only** - lives in `contributing/`, not `infra/`
- **User Guide** = how to use the library (rate limiting, hierarchies, LLM integration)
- **Operator Guide** = how to run in production (deployment, monitoring, performance)
- **Contributing** = how to develop the library (setup, testing, architecture)
- **CLAUDE.md remains the authoritative dev reference** - Contributing docs are lightweight entry points

## Important Invariants

1. **Write-on-enter**: `acquire()` writes initial consumption to DynamoDB before yielding the lease, making tokens immediately visible to concurrent callers. On exception, a compensating write restores the consumed tokens (see `.claude/rules/write-on-enter.md`)
2. **Bucket can go negative (adjust only)**: `lease.adjust()` never throws, allows debt. The initial admission path (`try_consume` + `_commit_initial`) is a gate that MUST NOT over-admit — do not use "bucket can go negative" to justify skipping admission checks
3. **Cascade is per-entity config**: Set `cascade=True` on `create_entity()` to auto-cascade to parent on every `acquire()`
4. **Stored limits are the default (v0.5.0+)**: Limits resolved from System/Resource/Entity config automatically. Pass `limits` parameter to override.
5. **Initial writes are atomic + optimistic lock on refill**: `_commit_initial` uses `transact_write` for cross-item atomicity. `build_composite_normal` locks on `last_refill_ms` (`ConditionExpression: #rf = :expected_rf`) to prevent stale refill overwrites. On lock failure, `build_composite_retry` skips refill and uses `tk >= consumed` condition to prevent over-admission
6. **Adjustments and rollbacks use independent writes**: `_commit_adjustments()` and `_rollback()` use `write_each()` (1 WCU each) since they produce unconditional ADD operations that do not require cross-item atomicity
7. **Transaction item limit**: DynamoDB `TransactWriteItems` supports max 100 items per transaction. Cascade operations with many buckets (entity + parent, multiple resources x limits) must stay within this limit
8. **Speculative writes are pre-committed**: When the speculative path succeeds, `_commit_initial()` is a no-op because the UpdateItem already persisted the consumption. Rollback compensates with `build_composite_adjust` + `write_each`
9. **Entity metadata cache is immutable**: `Repository._entity_cache` stores `{entity_id: (cascade, parent_id)}` with no TTL. Entity metadata (cascade, parent_id) is set once at `create_entity()` and never changes, so the cache never goes stale. Populated from speculative result (ALL_NEW) or slow path (entity META record)

## DynamoDB Pricing Reference

On-demand pricing (us-east-1, post-Nov 2023 50% reduction):
- Write Request Units: **$0.625/M** ($1.25/M for transactional writes)
- Read Request Units: **$0.125/M** ($0.25/M for transactional reads)

Non-cascade `acquire()` = 1 RCU + 1 WCU = $0.125 + $0.625 = **$0.75/M** (the project's advertised cost).

Speculative non-cascade `acquire()` (success) = 0 RCU + 1 WCU = **$0.625/M** (~17% savings).
Speculative fast rejection (exhausted) = 0 RCU + 0 WCU = **$0/M** (free).
Speculative fallback (refill helps) = 1 RCU + 2 WCU = $0.125 + $1.25 = **$1.375/M** (worse than normal).
Speculative cascade (both succeed, sequential) = 0 RCU + 2 WCU = **$1.25/M** (vs $1.75/M normal cascade).
Speculative cascade (both succeed, parallel, issue #318) = 0 RCU + 2 WCU = **$1.25/M** (same cost, lower latency).
Speculative cascade fallback (parent refill helps) = 0.5 RCU + 3 WCU = **$1.94/M** (deferred compensation).
Speculative cascade fast rejection (parent exhausted) = 0 RCU + 2 WCU = **$1.25/M** (child consumed + compensated).

## DynamoDB Access Patterns

All PK and GSI PK values are prefixed with `{ns}/` where `{ns}` is the opaque namespace ID (e.g., `a7x3kq`). The reserved namespace `_` is used for namespace registry records.

| Pattern | Query |
|---------|-------|
| Get entity | `PK={ns}/ENTITY#{id}, SK=#META` |
| Get buckets | `PK={ns}/ENTITY#{id}, SK begins_with #BUCKET#` |
| Batch get buckets | `BatchGetItem` with multiple PK/SK pairs (issue #133) |
| Batch get configs | `BatchGetItem` with entity/resource/system config keys (issue #298) |
| Get children | GSI1: `GSI1PK={ns}/PARENT#{id}` |
| Resource capacity | GSI2: `GSI2PK={ns}/RESOURCE#{name}, SK begins_with BUCKET#` |
| List resources with defaults | `PK={ns}/SYSTEM#, SK=#RESOURCES` (single GetItem: 1 RCU, issue #233) |
| Get version | `PK={ns}/SYSTEM#, SK=#VERSION` |
| Get audit events | `PK={ns}/AUDIT#{entity_id}, SK begins_with #AUDIT#` |
| Get usage snapshots (by entity) | `PK={ns}/ENTITY#{id}, SK begins_with #USAGE#` |
| Get usage snapshots (by resource) | GSI2: `GSI2PK={ns}/RESOURCE#{name}, GSI2SK begins_with USAGE#` |
| Get system config (limits + on_unavailable) | `PK={ns}/SYSTEM#, SK=#CONFIG` |
| Get resource config (limits) | `PK={ns}/RESOURCE#{resource}, SK=#CONFIG` |
| Get entity config (limits) | `PK={ns}/ENTITY#{id}, SK=#CONFIG#{resource}` |
| List entities with custom limits | GSI3: `GSI3PK={ns}/ENTITY_CONFIG#{resource}` |
| List resources with entity configs | `PK={ns}/SYSTEM#, SK=#ENTITY_CONFIG_RESOURCES` (wide column, issue #288) |
| Namespace forward lookup | `PK=_/SYSTEM#, SK=#NAMESPACE#{name}` |
| Namespace reverse lookup | `PK=_/SYSTEM#, SK=#NSID#{id}` |
| List all items in namespace | GSI4: `GSI4PK={ns}` |

**Optimized read patterns (issue #133):**
- `acquire()` uses `BatchGetItem` to fetch all buckets for entity + parent in a single round trip
- This reduces cascade scenarios from N sequential GetItem calls to 1 BatchGetItem call

**Speculative write pattern (issue #315):**
- `speculative_consume()` issues a conditional `UpdateItem` with `ADD -consumed` and condition `attribute_exists(PK) AND tk >= consumed`
- Uses `ReturnValuesOnConditionCheckFailure=ALL_OLD` to return bucket state on failure without a separate read
- Uses `ReturnValues=ALL_NEW` on success to reconstruct `BucketState`, `cascade`, and `parent_id` from the response
- Cascade and parent_id are denormalized into bucket items (via `build_composite_create`) to avoid entity metadata lookup

**Entity metadata cache (issue #318):**
- `Repository._entity_cache` stores `{entity_id: (cascade, parent_id)}` -- immutable metadata, no TTL needed
- Populated from speculative result (ALL_NEW on success) or slow path (entity META record)
- On cache hit with `cascade=True`, `speculative_consume()` issues child + parent speculative writes concurrently via `asyncio.gather` (async) or `self._run_in_executor` (sync, strategy controlled by `parallel_mode`)
- Reduces cascade latency from 2 sequential round trips to 1 parallel round trip (same WCU cost)
- First acquire for an entity always uses sequential path (populates cache); subsequent acquires use parallel path
- **Sync parallel modes:** `"auto"` (default: gevent if patched, serial if single-CPU, threadpool otherwise), `"gevent"` (greenlets, warns if unpatched), `"threadpool"` (lazy ThreadPoolExecutor, warns on single-CPU), `"serial"` (sequential). Explicit modes warn on suboptimal conditions. Resolved once at `SyncRepository.__init__`

**Aggregator refill write pattern (issue #317):**
- `try_refill_bucket()` issues an `UpdateItem` with `ADD b_{limit}_tk +refill_delta SET rf = :now` and condition `rf = :expected_rf`
- Uses `ADD` for token deltas so it commutes with concurrent speculative writes (no read required)
- Optimistic lock on `rf` prevents double-refill with the client slow path or another aggregator invocation
- On `ConditionalCheckFailedException`, the refill is silently skipped (another writer updated `rf` first)

**DynamoDB writer table:**

| Writer | UpdateExpression | Condition | Touches `rf`? |
|--------|-----------------|-----------|---------------|
| Speculative consume | `ADD tk -consumed` | `attribute_exists(PK) AND tk >= consumed` | No |
| Normal path (initial) | `SET rf = :new_rf ADD tk -consumed` | `rf = :expected_rf` | Yes (optimistic lock) |
| Normal path (retry) | `ADD tk -consumed` | `tk >= consumed` | No (skips refill) |
| Adjustment / rollback | `ADD tk +/-delta` | (unconditional) | No |
| Aggregator refill | `ADD tk +refill SET rf = :now` | `rf = :expected_rf` | Yes (optimistic lock) |

**Hot partition risk with cascade (issue #116):** See [Hot Partition Risk Mitigation](#hot-partition-risk-mitigation-issue-116) above.

**Key builders for config records:**
- `pk_system(namespace_id)` - Returns `{ns}/SYSTEM#`
- `pk_resource(namespace_id, resource)` - Returns `{ns}/RESOURCE#{resource}`
- `pk_entity(namespace_id, entity_id)` - Returns `{ns}/ENTITY#{entity_id}`
- `sk_config()` - Returns `#CONFIG` (for system/resource level)
- `sk_config(resource)` - Returns `#CONFIG#{resource}` (for entity level)
- `sk_entity_config_resources()` - Returns `#ENTITY_CONFIG_RESOURCES` (registry with ref counts)
- `sk_namespace(name)` - Returns `#NAMESPACE#{name}` (forward lookup)
- `sk_nsid(id)` - Returns `#NSID#{id}` (reverse lookup)

**Audit entity IDs for config levels** (ADR-106):
- System config: Audit events use `$SYSTEM` as entity_id
- Resource config: Audit events use `$RESOURCE:{resource_name}` (e.g., `$RESOURCE:gpt-4`)

### Centralized Configuration (v0.5.0+)

Limit configs use a four-level hierarchy with precedence: **Entity (resource-specific) > Entity (_default_) > Resource > System > Constructor defaults**.

**API methods for managing stored limits:**

| Level | Set | Get | Delete | List |
|-------|-----|-----|--------|------|
| System | `set_system_defaults(limits, on_unavailable)` | `get_system_defaults()` | `delete_system_defaults()` | - |
| Resource | `set_resource_defaults(resource, limits)` | `get_resource_defaults(resource)` | `delete_resource_defaults(resource)` | `list_resources_with_defaults()` |
| Entity | `set_limits(entity_id, limits, resource)` | `get_limits(entity_id, resource)` | `delete_limits(entity_id, resource)` | `list_entities_with_custom_limits(resource)` |

**Cross-level queries:**
- `list_resources_with_entity_configs()` - Returns which resources have entity-level custom limits (useful for discovery and cleanup)

**CLI commands for managing stored limits:**

```bash
# System-level defaults
zae-limiter system set-defaults -l tpm:100000 -l rpm:1000 --on-unavailable allow

# Resource-level limits
zae-limiter resource set-defaults gpt-4 -l tpm:50000 -l rpm:500

# Entity-level limits (highest precedence)
zae-limiter entity set-limits user-123 --resource gpt-4 -l rpm:1000
```

Each level also has `get-*` and `delete-*` subcommands. Use `zae-limiter resource list` to list resources with defaults. Use `zae-limiter entity list-resources` to list all resources with entity-level configs. Use `zae-limiter entity list --with-custom-limits <resource>` to list entities with custom limits for a specific resource.

**Namespace CLI commands:**

```bash
# Register namespaces
zae-limiter namespace register tenant-alpha tenant-beta

# List active namespaces
zae-limiter namespace list

# Show namespace details
zae-limiter namespace show tenant-alpha

# Soft delete a namespace
zae-limiter namespace delete tenant-alpha

# Recover a deleted namespace (by ID)
zae-limiter namespace recover <namespace-id>

# List deleted namespaces (candidates for purge)
zae-limiter namespace orphans

# Hard delete all data in a namespace (irreversible)
zae-limiter namespace purge <namespace-id> --yes
```

**`--namespace` / `-N` flag on data-access commands:**

Most data-access commands accept `--namespace` to scope operations to a specific namespace:

```bash
# Entity operations in a specific namespace
zae-limiter entity set-limits user-123 --namespace tenant-alpha -l rpm:1000

# Audit events for a namespace
zae-limiter audit list --namespace tenant-alpha

# Usage snapshots for a namespace
zae-limiter usage list --namespace tenant-alpha

# System defaults for a namespace
zae-limiter system set-defaults --namespace tenant-alpha -l rpm:5000
```

Limit configs use composite items (v0.8.0+, ADR-114 for configs). All limits for a config level are stored in a single item:

| Level | PK | SK | Attributes |
|-------|----|----|------------|
| System | `{ns}/SYSTEM#` | `#CONFIG` | `on_unavailable`, `l_rpm_cp`, `l_rpm_ra`, `l_rpm_rp`, ... |
| Resource | `{ns}/RESOURCE#{res}` | `#CONFIG` | `resource`, `l_rpm_cp`, ... |
| Entity | `{ns}/ENTITY#{id}` | `#CONFIG#{resource}` | `entity_id`, `resource`, `l_rpm_cp`, ... |

**Limit attribute format:** `l_{limit_name}_{field}` where field is one of:
- `cp` (capacity), `ra` (refill_amount), `rp` (refill_period_seconds)

**Config fields:**
- `config_version` (int): Atomic counter for cache invalidation
- `on_unavailable` (string): "allow" or "block" (system level only)

**Caching:** 60s TTL in-memory cache per Repository instance (configurable via `config_cache_ttl` parameter on Repository constructor, 0 to disable). Use `repo.invalidate_config_cache()` for immediate refresh. Use `repo.get_cache_stats()` for monitoring. `set_limits()` and `delete_limits()` auto-evict relevant cache entries. Negative caching for entities without custom config. Config resolution is handled by `repo.resolve_limits()` (ADR-122).

**Cost impact:** 1.5 RCU per cache miss (one GetItem per level, reduced from 2 RCU with per-limit items). With caching, `acquire()` costs 1-2 RCU per request regardless of limit count (O(1) via composite items, ADR-114/115).

### Namespace Registry

The namespace registry stores bidirectional records under the reserved namespace `_` (constant: `RESERVED_NAMESPACE`):

| Record | PK | SK | Key Attributes |
|--------|----|----|----------------|
| Forward (name→ID) | `_/SYSTEM#` | `#NAMESPACE#{name}` | `namespace_id`, `status`, `created_at` |
| Reverse (ID→name) | `_/SYSTEM#` | `#NSID#{id}` | `namespace`, `status`, `created_at`, `deleted_at` |

**Status lifecycle:** `active` → `deleted` (soft delete, forward record removed) → `purging` (hard delete in progress) → removed

**Namespace ID format:** 11-character opaque string generated via `secrets.token_urlsafe(8)`, never starts with `-` (regenerated if so, to avoid CLI argument parsing issues)

**API methods:**

| Method | Description |
|--------|-------------|
| `register_namespace(name)` | Register a new namespace (idempotent, returns ID) |
| `register_namespaces(names)` | Bulk register multiple namespaces |
| `list_namespaces()` | List active namespaces (excludes deleted) |
| `get_namespace(name)` | Get namespace details |
| `delete_namespace(name)` | Soft delete (removes forward record, marks reverse as deleted) |
| `recover_namespace(id)` | Restore a soft-deleted namespace |
| `list_orphan_namespaces()` | List deleted namespaces (candidates for purge) |
| `purge_namespace(id)` | Hard delete all data items + reverse record (uses GSI4) |

### Schema Design Notes

All records use flat schema (v0.6.0+, top-level attributes, no nested `data.M`). See `dynamodb-patterns.md` rules and [ADR-111](docs/adr/111-flatten-all-records.md).

See [ADR-100](docs/adr/100-centralized-config.md) for full config design details.

### Bucket TTL for Default Limits (Issue #271, #296)

Buckets using system/resource default limits have TTL for auto-expiration:

| Config Source | TTL Behavior |
|---------------|--------------|
| Entity custom limits | No TTL (persist indefinitely) |
| Resource defaults | TTL = now + max_time_to_fill × multiplier |
| System defaults | TTL = now + max_time_to_fill × multiplier |
| Override parameter | TTL = now + max_time_to_fill × multiplier |

Where `time_to_fill = (capacity / refill_amount) × refill_period_seconds`. This ensures slow-refill limits (where `capacity >> refill_amount`) have enough time to fully refill before expiring.

Configure via `bucket_ttl_refill_multiplier` parameter (default: 7). Set to 0 to disable.

**Example:**
```python
# Custom multiplier (14 days for a limit that takes 24h to refill)
limiter = RateLimiter(
    name="my-app",
    bucket_ttl_refill_multiplier=14,
)

# Disable TTL (all buckets persist indefinitely)
limiter = RateLimiter(
    name="my-app",
    bucket_ttl_refill_multiplier=0,
)
```

**TTL calculation examples:**
- `Limit.per_minute("rpm", 100)`: time_to_fill = (100/100)×60 = 60s, TTL = 60×7 = 420s (7 min)
- Slow refill: capacity=1000, refill_amount=10, period=60s → time_to_fill = 6000s, TTL = 42000s (11.7 hours)

**TTL behavior on upgrade/downgrade:**
- Entity with custom limits → TTL removed on next acquire
- Entity downgrades to defaults → TTL set on next acquire

## Dependencies

**Required:**
- `aioboto3`: Async DynamoDB client
- `aws-lambda-builders`: Cross-platform Lambda packaging (see ADR-113)
- `boto3`: Sync DynamoDB (for Lambda aggregator)
- `pip`: Required by `aws-lambda-builders` for dependency resolution
- `questionary`: Interactive prompts for CLI workflows

**Optional extras:**
- `[plot]`: `asciichartpy` for ASCII chart visualization of usage snapshots
- `[dev]`: Testing and development tools (pytest, moto, ruff, mypy, pre-commit, types-gevent)
- `[docs]`: MkDocs documentation generation
- `[cdk]`: AWS CDK constructs
- `[lambda]`: Lambda Powertools (aws-lambda-powertools)
- `[local]`: `docker` for LocalStack container management
- `[bench]`: `docker`, `locust`, `gevent` for load testing and benchmarks

## Releasing

Releases are fully automated via GitHub Actions (`release.yml`). No manual build or publish steps required.

**Process:** Tag a version on main (`git tag v0.1.0 && git push origin v0.1.0`), and GitHub Actions builds, generates changelog (git-cliff), creates a GitHub Release, and publishes to PyPI via OIDC.

**Version management:** Versions are automatically generated from git tags using `hatch-vcs`. No manual version updates needed. Tag format: `v{major}.{minor}.{patch}`.

**Changelog:** Uses `git-cliff` with `cliff.toml` config. Parses conventional commits since the last tag.

### Replying to PR Review Comments

```bash
gh api repos/{owner}/{repo}/pulls/{pr}/comments \
  -X POST \
  -f body="Your reply" \
  -F in_reply_to={comment_id}
```

- Field is `in_reply_to` (not `in_reply_to_id`)
- Use `-F` for numeric fields, `-f` for strings
- Do NOT pass `commit_id`, `path`, or `position` when replying
