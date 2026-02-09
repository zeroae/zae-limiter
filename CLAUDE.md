# CLAUDE.md - Instructions for AI Assistants

This file provides context for AI assistants working on the zae-limiter codebase.

## Project Overview

zae-limiter is a rate limiting library backed by DynamoDB using the token bucket algorithm. It excels at scenarios where:
- Multiple limits are tracked per call (rpm, tpm)
- Consumption is unknown upfront (adjust after the operation completes)
- Hierarchical limits exist (API key → project, tenant → user)
- Cost matters (~$0.75/1M requests)

**Project scopes:** `limiter`, `bucket`, `cli`, `infra`, `ci`, `aggregator`, `models`, `schema`, `repository`, `lease`, `exceptions`, `cache`, `test`, `benchmark`, `local`. See `release-planning.md` for area labels.

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

Native sync code is generated from async source via AST transformation (see ADR-121):

```bash
# Generate sync code after modifying async source
hatch run generate-sync

# Or directly
python scripts/generate_sync.py
```

**Generated source files (DO NOT EDIT):**
- `sync_repository_protocol.py` ← `repository_protocol.py`
- `sync_repository.py` ← `repository.py`
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
- The `zae_limiter_aggregator` package and a minimal `zae_limiter/schema.py` stub are copied into the zip
- Lambda code is updated via AWS Lambda API after stack creation
- No S3 bucket required - deployment package is uploaded directly
- No Docker required - `aws-lambda-builders` handles platform-specific wheels

### Declarative Infrastructure (Recommended)

Pass `StackOptions` to declare the desired state:

```python
from zae_limiter import RateLimiter, StackOptions

# Basic deployment
limiter = RateLimiter(
    name="my-app",
    region="us-east-1",
    stack_options=StackOptions(),  # CloudFormation ensures state matches
)

# Enterprise deployment with permission boundary and custom role naming
limiter = RateLimiter(
    name="my-app",
    region="us-east-1",
    stack_options=StackOptions(
        permission_boundary="arn:aws:iam::aws:policy/PowerUserAccess",
        role_name_format="pb-{}-PowerUser",
    ),
)
```

Other `StackOptions` fields: `lambda_memory`, `usage_retention_days`, `audit_retention_days`, `enable_alarms`, `alarm_sns_topic`, `enable_audit_archival`, `audit_archive_glacier_days`, `enable_tracing`, `create_iam_roles` (default: False), `role_name_format`, `policy_name_format`, `enable_deletion_protection`, `create_iam` (default: True), `aggregator_role_arn`.

**IAM Resource Defaults (ADR-117):**
- **Managed policies** are **created by default** (`AcquireOnlyPolicy`, `FullAccessPolicy`, `ReadOnlyPolicy`)
- **IAM roles** are **opt-in** (set `create_iam_roles=True` to create them)
- Users can attach managed policies to their own roles, users, or federated identities
- **Skip all IAM** with `create_iam=False` or `--no-iam` for restricted IAM environments
- **External Lambda role** with `aggregator_role_arn` or `--aggregator-role-arn` to use pre-existing role

**When to use `StackOptions` vs CLI:**
- **StackOptions**: Self-contained apps, serverless deployments, minimal onboarding friction
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

## Project Structure

```
src/zae_limiter/
├── __init__.py        # Public API exports
├── models.py          # Limit, Entity, LimitStatus, BucketState, StackOptions, AuditEvent, AuditAction, UsageSnapshot, UsageSummary, LimiterInfo, BackendCapabilities, Status, LimitName, ResourceCapacity, EntityCapacity
├── exceptions.py      # RateLimitExceeded, RateLimiterUnavailable, StackCreationError, VersionError, ValidationError, EntityNotFoundError, InfrastructureNotFoundError
├── naming.py          # Resource name validation (ZAEL- prefix retained for legacy discovery)
├── bucket.py          # Token bucket math (integer arithmetic)
├── schema.py          # DynamoDB key builders
├── repository_protocol.py # RepositoryProtocol for backend abstraction
├── repository.py      # DynamoDB operations
├── lease.py           # Lease context manager
├── limiter.py         # RateLimiter (async)
├── config_cache.py    # Client-side config caching with TTL (CacheStats)
├── sync_repository_protocol.py  # Generated: SyncRepositoryProtocol
├── sync_repository.py           # Generated: SyncRepository
├── sync_limiter.py              # Generated: SyncRateLimiter
├── sync_lease.py                # Generated: SyncLease
├── sync_config_cache.py         # Generated: SyncConfigCache
├── cli.py             # CLI commands (deploy, delete, status, list, cfn-template, lambda-export, version, upgrade, check, audit, usage, entity, resource, system, local)
├── version.py         # Version tracking and compatibility
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
├── __init__.py               # Re-exports handler, processor types
├── handler.py                # Lambda entry point
├── processor.py              # Stream processing logic for usage snapshots
└── archiver.py               # S3 audit archival (gzip JSONL)
```

### Repository Pattern (v0.5.0+)

The `Repository` class owns data access and infrastructure management. `RateLimiter` owns business logic.

```python
from zae_limiter import RateLimiter, Repository, StackOptions

# New pattern (preferred): Repository with stack_options
repo = Repository(
    name="my-app",
    region="us-east-1",
    stack_options=StackOptions(lambda_memory=512),  # Pass to constructor
)
await repo.ensure_infrastructure()  # Creates stack using stored options
limiter = RateLimiter(repository=repo)

# Old pattern (deprecated, emits DeprecationWarning):
# repo.create_stack(stack_options=StackOptions())  # Don't use

# RateLimiter._ensure_initialized() calls repo.ensure_infrastructure() automatically
```

**Key API methods:**
- `Repository(stack_options=...)` - Pass infrastructure config to constructor
- `repo.ensure_infrastructure()` - Create/update stack using stored options (no-op if None)
- `repo.create_stack()` - **Deprecated**. Will be removed in v2.0.0

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
- Components: `acq` (AcquireOnlyPolicy), `full` (FullAccessPolicy), `read` (ReadOnlyPolicy)
- Default names: `{stack}-acq`, `{stack}-full`, `{stack}-read`
- Maximum policy name length: 128 characters (IAM limit)
- Policies are **always created** regardless of `create_iam_roles` setting

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
- `burst >= capacity` allows controlled bursting

### DynamoDB Single Table Design
- All entities, buckets, limits, usage in one table
- GSI1: Parent -> Children lookups
- GSI2: Resource aggregation (capacity tracking)
- GSI3: Entity config queries (sparse - only entity configs indexed)
- Uses TransactWriteItems for atomic multi-entity writes (initial consumption)
- Uses independent single-item writes (`write_each`) for adjustments and rollbacks (1 WCU each)

### Speculative Writes (Issue #315)
- Enabled by default (`speculative_writes=True`); disable with `speculative_writes=False`
- Skips the read round trip (BatchGetItem) by issuing a conditional UpdateItem directly
- Uses `ReturnValuesOnConditionCheckFailure=ALL_OLD` to inspect bucket state on failure
- Falls back to the normal read-write path when the bucket is missing, config changed, or refill would help
- Fast rejection: if refill would not help, raises `RateLimitExceeded` immediately (0 RCU, 0 WCU)
- Cascade/parent_id denormalized into bucket items to avoid entity metadata lookup on the fast path

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

## DynamoDB Pricing Reference

On-demand pricing (us-east-1, post-Nov 2023 50% reduction):
- Write Request Units: **$0.625/M** ($1.25/M for transactional writes)
- Read Request Units: **$0.125/M** ($0.25/M for transactional reads)

Non-cascade `acquire()` = 1 RCU + 1 WCU = $0.125 + $0.625 = **$0.75/M** (the project's advertised cost).

Speculative non-cascade `acquire()` (success) = 0 RCU + 1 WCU = **$0.625/M** (~17% savings).
Speculative fast rejection (exhausted) = 0 RCU + 0 WCU = **$0/M** (free).
Speculative fallback (refill helps) = 1 RCU + 2 WCU = $0.125 + $1.25 = **$1.375/M** (worse than normal).
Speculative cascade (both succeed) = 0 RCU + 2 WCU = **$1.25/M** (vs $1.75/M normal cascade).
Speculative cascade fallback (parent refill helps) = 0.5 RCU + 3 WCU = **$1.94/M** (deferred compensation).
Speculative cascade fast rejection (parent exhausted) = 0 RCU + 2 WCU = **$1.25/M** (child consumed + compensated).

## DynamoDB Access Patterns

| Pattern | Query |
|---------|-------|
| Get entity | `PK=ENTITY#{id}, SK=#META` |
| Get buckets | `PK=ENTITY#{id}, SK begins_with #BUCKET#` |
| Batch get buckets | `BatchGetItem` with multiple PK/SK pairs (issue #133) |
| Batch get configs | `BatchGetItem` with entity/resource/system config keys (issue #298) |
| Get children | GSI1: `GSI1PK=PARENT#{id}` |
| Resource capacity | GSI2: `GSI2PK=RESOURCE#{name}, SK begins_with BUCKET#` |
| List resources with defaults | `PK=SYSTEM#, SK=#RESOURCES` (single GetItem: 1 RCU, issue #233) |
| Get version | `PK=SYSTEM#, SK=#VERSION` |
| Get audit events | `PK=AUDIT#{entity_id}, SK begins_with #AUDIT#` |
| Get usage snapshots (by entity) | `PK=ENTITY#{id}, SK begins_with #USAGE#` |
| Get usage snapshots (by resource) | GSI2: `GSI2PK=RESOURCE#{name}, GSI2SK begins_with USAGE#` |
| Get system config (limits + on_unavailable) | `PK=SYSTEM#, SK=#CONFIG` |
| Get resource config (limits) | `PK=RESOURCE#{resource}, SK=#CONFIG` |
| Get entity config (limits) | `PK=ENTITY#{id}, SK=#CONFIG#{resource}` |
| List entities with custom limits | GSI3: `GSI3PK=ENTITY_CONFIG#{resource}` |
| List resources with entity configs | `PK=SYSTEM#, SK=#ENTITY_CONFIG_RESOURCES` (wide column, issue #288) |

**Optimized read patterns (issue #133):**
- `acquire()` uses `BatchGetItem` to fetch all buckets for entity + parent in a single round trip
- This reduces cascade scenarios from N sequential GetItem calls to 1 BatchGetItem call

**Speculative write pattern (issue #315):**
- `speculative_consume()` issues a conditional `UpdateItem` with `ADD -consumed` and condition `attribute_exists(PK) AND tk >= consumed`
- Uses `ReturnValuesOnConditionCheckFailure=ALL_OLD` to return bucket state on failure without a separate read
- Uses `ReturnValues=ALL_NEW` on success to reconstruct `BucketState`, `cascade`, and `parent_id` from the response
- Cascade and parent_id are denormalized into bucket items (via `build_composite_create`) to avoid entity metadata lookup

**Hot partition risk with cascade (issue #116):** See [Hot Partition Risk Mitigation](#hot-partition-risk-mitigation-issue-116) above.

**Key builders for config records:**
- `pk_system()` - Returns `SYSTEM#`
- `pk_resource(resource)` - Returns `RESOURCE#{resource}`
- `pk_entity(entity_id)` - Returns `ENTITY#{entity_id}`
- `sk_config()` - Returns `#CONFIG` (for system/resource level)
- `sk_config(resource)` - Returns `#CONFIG#{resource}` (for entity level)
- `sk_entity_config_resources()` - Returns `#ENTITY_CONFIG_RESOURCES` (registry with ref counts)

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

Limit configs use composite items (v0.8.0+, ADR-114 for configs). All limits for a config level are stored in a single item:

| Level | PK | SK | Attributes |
|-------|----|----|------------|
| System | `SYSTEM#` | `#CONFIG` | `on_unavailable`, `l_rpm_cp`, `l_rpm_bx`, `l_rpm_ra`, `l_rpm_rp`, ... |
| Resource | `RESOURCE#{res}` | `#CONFIG` | `resource`, `l_rpm_cp`, ... |
| Entity | `ENTITY#{id}` | `#CONFIG#{resource}` | `entity_id`, `resource`, `l_rpm_cp`, ... |

**Limit attribute format:** `l_{limit_name}_{field}` where field is one of:
- `cp` (capacity), `bx` (burst), `ra` (refill_amount), `rp` (refill_period_seconds)

**Config fields:**
- `config_version` (int): Atomic counter for cache invalidation
- `on_unavailable` (string): "allow" or "block" (system level only)

**Caching:** 60s TTL in-memory cache per Repository instance (configurable via `config_cache_ttl` parameter on Repository constructor, 0 to disable). Use `repo.invalidate_config_cache()` for immediate refresh. Use `repo.get_cache_stats()` for monitoring. `set_limits()` and `delete_limits()` auto-evict relevant cache entries. Negative caching for entities without custom config. Config resolution is handled by `repo.resolve_limits()` (ADR-122).

**Cost impact:** 1.5 RCU per cache miss (one GetItem per level, reduced from 2 RCU with per-limit items). With caching, `acquire()` costs 1-2 RCU per request regardless of limit count (O(1) via composite items, ADR-114/115).

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

**Optional extras:**
- `[plot]`: `asciichartpy` for ASCII chart visualization of usage snapshots
- `[dev]`: Testing and development tools (pytest, moto, ruff, mypy, pre-commit)
- `[docs]`: MkDocs documentation generation
- `[cdk]`: AWS CDK constructs
- `[lambda]`: Lambda Powertools (aws-lambda-powertools)

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
