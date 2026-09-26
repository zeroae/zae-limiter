# CLAUDE.md - Instructions for AI Assistants

This file provides context for AI assistants working on the zae-limiter codebase.

## Project Overview

zae-limiter is a rate limiting library backed by DynamoDB using the token bucket algorithm. It excels at scenarios where:
- Multiple limits are tracked per call (rpm, tpm)
- Consumption is unknown upfront (adjust after the operation completes)
- Hierarchical limits exist (API key → project, tenant → user)
- Cost matters (~$0.75/1M requests)

**Project scopes:** `limiter`, `bucket`, `cli`, `infra`, `ci`, `aggregator`, `provisioner`, `models`, `schema`, `repository`, `lease`, `exceptions`, `cache`, `test`, `benchmark`, `local`, `loadtest`. See `release-planning.md` for area labels.

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
uv run mypy

# Lint (or let pre-commit run automatically on commit)
uv run ruff check --fix .
uv run ruff format .

# Run all pre-commit hooks manually
pre-commit run --all-files

# Lint CloudFormation template (after modifying cfn_template.yaml)
uv run cfn-lint src/zae_limiter/infra/cfn_template.yaml
```

**Run the lint commands bare — `.` is the whole tree, and that is correct (#486).** Ruff is
pinned to one exact version in five places (`[build-system] requires`, the `[dev]` extra, the
hatch default env, `.pre-commit-config.yaml`'s `rev`, and `ci-lint.yml`'s `pip install`), so
`uv run ruff format .` and the commit hook produce byte-identical output. On a clean tree the
bare command reformats nothing — the standing "never run bare `uv run ruff format .`"
workaround is retired.

Two ruff versions do not agree, and the damage is not limited to churn:
`scripts/generate_sync.py` formats its output with whatever `ruff` is on `PATH` while the
`verify-sync-generated` hook regenerates and checks with the pinned one, so a construct the two
disagree on leaves a generated sync twin reported permanently out of date with nothing in the
output naming the cause (observed on #513: a multi-line `lambda` with a conditional body). The
`check-ruff-pin` hook (`scripts/check_ruff_pin.py`) fails the commit if any of the five drifts —
including a dependabot bump that moves `pyproject.toml` without the hook `rev`, which is the
expected way it fires.

### Sync Code Generation

Native sync code is generated from async source via AST transformation (see ADR-121). The transformer handles `asyncio.gather(a, b)` by converting it to `self._run_in_executor(lambda: a, lambda: b)`, with a configurable `parallel_mode` parameter on `SyncRepository` that controls the execution strategy:

| Mode | Behavior |
|------|----------|
| `"auto"` (default) | Silently picks the best strategy: gevent (if monkey-patched) -> serial (if single-CPU) -> threadpool (multi-CPU) |
| `"gevent"` | Forces gevent greenlets; **warns** if monkey-patching is not active (proceeds running like serial) |
| `"threadpool"` | Lazy `ThreadPoolExecutor(max_workers=2)`, created on first cascade request; **warns** on single-CPU hosts about GIL contention |
| `"serial"` | Sequential execution (no parallelism) |

All explicit modes warn (not error) when conditions are suboptimal. Auto mode silently selects the best strategy without warnings. Resolution happens once at `SyncRepository.__init__` time (not per-call). Usage:

**`asyncio.gather` takes no keywords in generator-covered source (#491).** `_run_in_executor(*funcs)` accepts positional callables only, so a keyword would be dropped and the sync twin would silently diverge — `return_exceptions=True` would generate a twin that raises on the first sibling failure and abandons the rest. Generation now **aborts** (`UnsupportedAsyncConstructError`, exit 1) naming the file, line and keyword. Use the portable rewrite instead, which is faithful under all four strategies (a translated keyword would not be — serial abandons siblings):

```python
async def _safe(item):
    try:
        return await work(item)
    except Exception as exc:
        return exc


results = await asyncio.gather(*[_safe(i) for i in items])
```

`asyncio.wait_for` is guarded the same way: its `timeout` is discarded by design (sync has no cancellation), any other keyword aborts generation.

```python
repo = SyncRepository.open(parallel_mode="gevent")
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
- `tests/unit/test_sync_zero_estimate_lease.py` ← `tests/unit/test_zero_estimate_lease.py`

Pre-commit hook verifies generated code is up-to-date. CI also verifies before running tests.

### Worktrees need their own venv

`git worktree add` alone produces a checkout that cannot run the tests or the git hooks. `uv run`
does not rescue it: `pytest-asyncio` and the rest of the test stack live in the `dev` **extra**,
not a dependency group, so a plain `uv run pytest` installs neither. The failure lands at push
time as `ModuleNotFoundError: No module named 'pytest_asyncio'` out of the pre-push hook, which
reads like a broken branch rather than a missing environment.

```bash
scripts/new-worktree.sh <branch> [base]      # base defaults to origin/main
```

It creates the worktree under `.claude/worktrees/<branch-with-slashes-as-dashes>` and runs
`uv sync --all-extras` in it.

**Each worktree gets its own venv — do not share or symlink the main checkout's.** The editable
install must resolve `zae_limiter` to *that worktree's* `src/`. Share one venv and every
measurement taken in a worktree silently describes `main` instead of the branch under test: a
session lost a day to this, reporting a docs page green while its own branch was red, because the
run resolved `conftest.py` and `src/` from `main`. uv hardlinks from a shared cache, so a
per-worktree venv costs seconds and little disk.

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
# --iam/--no-iam, --aggregator-role-arn, --enable-provisioner/--no-provisioner

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
- The `zae_limiter_aggregator` package and a minimal `zae_limiter` stub are copied into the zip: `schema.py`, `bucket.py` (refill math for aggregator-assisted refill), `models.py` (dataclasses used by bucket.py), `exceptions.py` (exceptions used by models.py), and `schedule.py` (cron evaluation for scheduled limits, #222). The provisioner package vendors the same stub minus `bucket.py`. An unvendored module is an `ImportError` at cold start that no test importing the *installed* package can see, so both builder test modules assert the import closure of the built zip
- Lambda code is updated via AWS Lambda API after stack creation
- No S3 bucket required - deployment package is uploaded directly
- No Docker required - `aws-lambda-builders` handles platform-specific wheels

### Declarative Infrastructure

Use `Repository.open()` for application code (auto-provisions if needed), or `Repository.builder()` / CLI for enterprise deployments:

```python
from zae_limiter import RateLimiter, Repository

# Application code — open handles everything (auto-provisions if needed)
repo = await Repository.open("my-app")
limiter = RateLimiter(repository=repo)

# Enterprise deployment — builder for permission boundaries and custom config
repo = await (
    Repository.builder()
    .permission_boundary("arn:aws:iam::aws:policy/PowerUserAccess")
    .role_name_format("pb-{}-PowerUser")
    .policy_name_format("pb-{}-PowerUser")
    .build()
)
```

Other builder methods: `.stack()`, `.region()`, `.endpoint_url()`, `.namespace()`, `.lambda_memory()`, `.enable_provisioner()`, `.usage_retention_days()`, `.audit_retention_days()`, `.enable_alarms()`, `.alarm_sns_topic()`, `.enable_audit_archival()`, `.audit_archive_glacier_days()`, `.enable_tracing()`, `.create_iam_roles()`, `.create_iam()`, `.aggregator_role_arn()`, `.enable_deletion_protection()`, `.tags()`.

**IAM Resource Defaults (ADR-117):**
- **Managed policies** are **created by default** — both table-level (`acq`, `full`, `read`) and namespace-scoped (`ns-acq`, `ns-full`, `ns-read`)
- **IAM roles** are **opt-in** (set `create_iam_roles=True` to create them)
- Users can attach managed policies to their own roles, users, or federated identities
- **Skip all IAM** with `create_iam=False` or `--no-iam` for restricted IAM environments
- **External Lambda role** with `aggregator_role_arn` or `--aggregator-role-arn` to use pre-existing role

**When to use `open()` vs `connect()` vs `builder()` vs CLI:**
- **`open()`**: 90% of users. Application code, prototyping, LocalStack dev. Auto-provisions infrastructure if missing
- **`connect()`**: Infrastructure managed externally (packaged CloudFormation, Terraform, CDK). Reads only — never provisions, registers, or updates anything
- **`builder().build()`**: Enterprise deployments needing permission boundaries, custom Lambda config, IAM role naming
- **CLI**: Strict infra/app separation, audit requirements, Terraform/CDK integration

### Declarative Limits Management (Issue #405)

Define rate limits as YAML manifests and apply them via a Lambda provisioner. The provisioner tracks managed state in a `#PROVISIONER` record and computes diffs to create, update, or delete limit configs.

```bash
# Preview changes (like terraform plan)
zae-limiter limits plan -n my-app -f limits.yaml

# Apply limits from YAML file
zae-limiter limits apply -n my-app -f limits.yaml

# Show drift between YAML and live DynamoDB state
zae-limiter limits diff -n my-app -f limits.yaml

# Generate CloudFormation template for a Custom::ZaeLimiterLimits resource
zae-limiter limits cfn-template -n my-app -f limits.yaml
```

**YAML manifest format:**

```yaml
namespace: default
system:
  on_unavailable: block
  limits:
    rpm:
      capacity: 1000
    tpm:
      capacity: 100000
resources:
  gpt-4:
    limits:
      rpm:
        capacity: 500
      tpm:
        capacity: 50000
  legacy-model:
    disabled: true
    limits:
      rpm:
        capacity: 500
entities:
  user-premium:
    resources:
      gpt-4:
        limits:
          rpm:
            capacity: 1000
      legacy-model:
        disabled: false   # carve-out: re-admit this entity to a disabled resource
        limits:
          rpm:
            capacity: 100
```

**Limit shorthand defaults:** Only `capacity` is required. When omitted: `burst` defaults to `capacity`, `refill_amount` defaults to `capacity`, `refill_period` defaults to `60` (seconds).

**`schedule` / `reset_schedule` (#222, ADR-135):** Optional lists of entries on any
`limits.<name>` mapping, at every level. `schedule` entries carry `cron`, `tz` and exactly one
of `scale` or the absolute fields (`capacity`, `refill_amount`, `refill_period_seconds`);
`reset_schedule` entries carry `cron` and `tz` only. Standard 5-field cron at the manifest
boundary — the compact form is storage only. A `reset_schedule` flips the `refill_amount`
shorthand default from `capacity` to **0** (`manifest.LimitDecl.from_dict`), so the natural
manifest is the ADR-137-valid one and a positive rate beside a reset is rejected with a message
about the pairing rather than about a field the author never wrote. Both round-trip through the
CloudFormation `Custom::ZaeLimiterLimits` resource as `Schedule` and `ResetSchedule`
properties; `limits_cli._SCHEDULE_KEYS` and `handler._CFN_SCHEDULE_KEYS` are exact inverses,
pinned by a unit test, and cannot share a module because the provisioner zip carries only the
four-file `zae_limiter` stub.

**`reset_after_seconds` (ADR-139):** A third recovery spelling on any `limits.<name>` mapping,
at every level — a window anchored to the entity's own **first use** rather than to fixed
calendar instants. Mutually exclusive with `reset_schedule` (a limit has one recovery
mechanism); like `reset_schedule`, it flips the `refill_amount` shorthand default from
`capacity` to **0**, so the natural manifest names only the allowance and the window:

```yaml
resources:
  claude-sonnet:
    limits:
      session:
        capacity: 10000
        reset_after_seconds: 18000   # 5h, from each entity's own first use
```

Round-trips through the CloudFormation `Custom::ZaeLimiterLimits` resource as a
`ResetAfterSeconds` property (`handler._CFN_LIMIT_OPTIONAL_KEYS`); `limits_cli._limits_to_cfn`
emits it from the same key. Spelled `..._seconds` and typed `int` in the manifest and in
CloudFormation because neither carries a type, matching `Limit.reset_after_seconds` rather than
the Python API's `Limit.reset_after: timedelta`.

**`disabled` (ADR-125):** Optional tri-state boolean on `resources.<name>` and
`entities.<id>.resources.<name>` entries (omit to inherit; `true`/`false` to set explicitly).
Not supported on `system`. Round-trips through the generated CloudFormation
`Custom::ZaeLimiterLimits` resource as a `Disabled` property.

**Provisioner Lambda:**
- Function name: `{stack}-limits-provisioner`
- Deployed by default; disable with `--no-provisioner` (CLI) or `.enable_provisioner(False)` (builder). `--no-iam` also disables it (it needs an IAM role, and unlike the aggregator it has no external-role escape hatch)
- When it is not deployed, `zae-limiter limits plan|apply|diff` exits 1 with an explanation, and a `limits cfn-template` stack fails to resolve the `{stack}-ProvisionerArn` export
- Handles CLI invocations (action + manifest payload) and CloudFormation custom resource events (`Custom::ZaeLimiterLimits`)
- Tracks managed items in `PK={ns}/SYSTEM#, SK=#PROVISIONER` with `managed_system`, `managed_resources`, `managed_entities` fields
- Computes diff between manifest and previous state, then applies create/update/delete via PutItem/DeleteItem
- On delete (CFN Delete), uses an empty manifest to remove all managed items

**CloudFormation integration:** The `cfn-template` subcommand generates a CFN template with a `Custom::ZaeLimiterLimits` resource that uses `Fn::ImportValue` to reference the provisioner Lambda ARN from the main stack.

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
├── models.py          # Limit, Entity, LimitStatus, Availability, BucketState, StackOptions, AuditEvent, AuditAction, UsageSnapshot, UsageSummary, LimiterInfo, BackendCapabilities, Status, LimitName, ResourceCapacity, EntityCapacity
├── exceptions.py      # RateLimitExceeded, LeaseExpiredError, RateLimiterUnavailable, StackOperationError, StackAlreadyExistsError, InfrastructureNotFoundError, NamespaceNotFoundError, NamespaceStateError, EntityNotFoundError, EntityExistsError, VersionError, ValidationError, ResourceDisabled
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
├── cli.py             # CLI commands (deploy, delete, status, list, cfn-template, lambda-export, version, upgrade, check, audit, usage, entity, resource, system, namespace, limits, local, loadtest)
├── limits_cli.py      # CLI commands for declarative limits (plan, apply, diff, cfn-template)
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
├── processor.py              # Stream processing: usage snapshots + bucket refill (Issue #317) + proactive sharding + shard propagation (GHSA-76rv)
└── archiver.py               # S3 audit archival (gzip JSONL)

src/zae_limiter_provisioner/   # Lambda provisioner for declarative limits (#405)
├── __init__.py               # Re-exports (ApplyResult, Change, LimitsManifest, compute_diff, on_event)
├── handler.py                # Lambda entry point (CLI + CFN custom resource events)
├── manifest.py               # LimitsManifest YAML parsing (LimitDecl, SystemDecl, ResourceDecl, EntityDecl)
├── differ.py                 # Diff engine (manifest vs #PROVISIONER state → list of Change)
├── applier.py                # Applies changes via boto3 DynamoDB (PutItem/DeleteItem)
├── fanout.py                 # Sync boto3 mirror of Repository._fanout_resource/_fanout_entity for disable/enable (ADR-125)
└── bucket_sync.py            # Sync boto3 mirror of Repository._sync_bucket_params (#481, #487)
```

### Repository Pattern (v0.5.0+)

The `Repository` class owns data access and infrastructure management. `RateLimiter` owns business logic.

#### Repository.open() (Recommended)

Use `Repository.open()` for application code. It auto-provisions infrastructure and registers namespaces as needed:

```python
from zae_limiter import RateLimiter, Repository

# Basic usage — namespace defaults via ZAEL_NAMESPACE env var or "default"
# Stack defaults via ZAEL_STACK env var or "zae-limiter"
repo = await Repository.open()
limiter = RateLimiter(repository=repo)

# Explicit namespace (positional arg)
repo = await Repository.open("my-app")
limiter = RateLimiter(repository=repo)

# Multi-tenant — each tenant gets an isolated namespace
repo_alpha = await Repository.open("tenant-alpha")
limiter_alpha = RateLimiter(repository=repo_alpha)

# With custom config cache TTL
repo = await Repository.open(config_cache_ttl=120)

# LocalStack development
repo = await Repository.open(endpoint_url="http://localhost:4566")
```

**`open()` signature:** `Repository.open(namespace, *, stack=..., region=..., endpoint_url=..., config_cache_ttl=...)`
- `namespace`: positional arg, defaults via `ZAEL_NAMESPACE` env var or `"default"`
- `stack`: defaults via `ZAEL_STACK` env var or `"zae-limiter"`

**`open()` steps:**
1. Try to resolve namespace name to opaque ID
2. If table is missing, deploy stack with defaults
3. If namespace is missing, register it (always registers "default" on new stack)
4. Reinitialize config cache with resolved namespace ID
5. Version check and Lambda auto-update

#### Repository.connect() (Externally Managed Infrastructure)

Use `Repository.connect()` when the stack, table, and namespace registry are
deployed by your own CloudFormation, Terraform, or CDK. It issues reads only —
it never creates infrastructure, registers namespaces, writes the version
record, or updates the Lambda:

```python
from zae_limiter import RateLimiter, Repository

repo = await Repository.connect("my-app")
limiter = RateLimiter(repository=repo)
```

**`connect()` signature:** `Repository.connect(namespace, *, stack=..., region=..., endpoint_url=..., config_cache_ttl=...)`
- Same `ZAEL_NAMESPACE` / `ZAEL_STACK` resolution as `open()`
- No `auto_update` parameter — Lambda updates are always off

**`connect()` steps:**
1. Resolve namespace name to opaque ID (`InfrastructureNotFoundError` if the table is missing)
2. Raise `NamespaceNotFoundError` if the namespace is not registered
3. Reinitialize config cache with resolved namespace ID
4. Strict version check with `initialize_if_missing=False`

**`connect()` vs `open()` on missing state:**

| Situation | `open()` | `connect()` |
|-----------|----------|-------------|
| Table missing | Deploys stack | `InfrastructureNotFoundError` |
| Namespace unregistered | Registers it | `NamespaceNotFoundError` |
| Version record missing | Writes it | `InfrastructureNotFoundError` |
| Lambda version behind client | Updates Lambda | `VersionMismatchError` |

`SyncRepository.connect()` is generated from the async source with the same signature.

#### RepositoryBuilder (Infrastructure Provisioning)

Use `Repository.builder()` for enterprise infrastructure provisioning (like `terraform deploy`):

```python
from zae_limiter import RateLimiter, Repository

# Provision infrastructure + register namespace
repo = await (
    Repository.builder()
    .namespace("default")  # Resolve namespace (default: "default")
    .config_cache_ttl(120)  # Config cache TTL in seconds
    .build()  # Async: creates infra, registers default ns, resolves namespace
)
limiter = RateLimiter(repository=repo)

# With infrastructure options
repo = await (
    Repository.builder()
    .lambda_memory(512)
    .enable_alarms(False)
    .permission_boundary("arn:aws:iam::aws:policy/PowerUserAccess")
    .role_name_format("PowerUserPB-{}")
    .policy_name_format("PowerUserPB-{}")
    .build()
)

# LocalStack development
repo = await Repository.builder().endpoint_url("http://localhost:4566").build()
```

**Builder `build()` steps:**
1. Construct Repository with materialized StackOptions (if any infra options set)
2. Ensure infrastructure exists (no-op if no infra options)
3. Register the "default" namespace (conditional PutItem, no-op if exists)
4. Resolve the requested namespace name to an opaque ID
5. Reinitialize config cache with resolved namespace ID
6. Version check and Lambda auto-update

**When to use `open()` vs `connect()` vs `builder()`:**
- **`open()`**: 90% of users. Application code, prototyping, LocalStack dev. Auto-provisions infrastructure
- **`connect()`**: Infrastructure managed by your own CloudFormation/Terraform/CDK. Reads only, raises on anything missing
- **`builder().build()`**: Enterprise deployments needing permission boundaries, custom Lambda config, IAM role naming

**Config ownership (open/builder vs deprecated RateLimiter params):**

| Parameter | `open()` / `builder()` | RateLimiter (deprecated) |
|-----------|------------------------|--------------------------|
| `namespace` | `open("tenant-a")` or `ZAEL_NAMESPACE` env var / `.namespace("tenant-a")` | N/A |
| `stack` | `open(stack=...)` or `ZAEL_STACK` env var / `.stack("my-app")` | N/A |
| `config_cache_ttl` | `open(config_cache_ttl=...)` / `.config_cache_ttl(120)` | N/A (was on Repository constructor) |
| `auto_update` | `open(auto_update=...)` / `.auto_update(True)` | `auto_update=True` (deprecated) |
| `bucket_ttl_multiplier` | `.bucket_ttl_multiplier(7)` | `bucket_ttl_refill_multiplier=7` (deprecated) |
| `on_unavailable` | `.on_unavailable("allow")` | `on_unavailable="allow"` (deprecated) |
| `region/endpoint_url` | `open(region=..., endpoint_url=...)` / `.region()`, `.endpoint_url()` | `RateLimiter(name=..., region=...)` (deprecated) |
| `stack_options` | Individual builder methods | `RateLimiter(stack_options=...)` (deprecated) |
| Infrastructure options | `.lambda_memory()`, `.enable_alarms()`, etc. | Via `StackOptions` dataclass |

#### Scoped Repositories (Namespace Switching)

After opening or building, use `repo.namespace()` to get a scoped Repository for a different namespace:

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

## Public API

`src/zae_limiter/__init__.py`'s `__all__` **is** the public contract — the set frozen at
v1.0.0. A module's own `__all__` governs `from .module import *`, not what the package
promises: a name is public only if `__init__.py` re-exports it.

The test for admitting a name: **would a user writing application code ever type it?** If it
only appears inside the limiter's own machinery, it stays module-scoped and reachable as
`zae_limiter.<module>.<name>` for tests and internal callers. `tests/unit/test_public_api.py`
pins both directions.

### `schedule.py` (#222, #534)

Exported: **`ScheduleEntry`** only — it is the argument to `Limit.with_schedule()` and
`Limit.reset_schedule`, so every scheduled-limits example begins with
`from zae_limiter import Limit, ScheduleEntry`.

Deliberately not exported, and why:

| Name | Why it stays in `zae_limiter.schedule` |
|------|----------------------------------------|
| `parse_cron`, `ParsedCron` | Parse artifact. `ScheduleEntry.__post_init__` already parses and raises `ValueError`, so a user never needs to validate a cron string separately. |
| `matches` | Takes a `ParsedCron`, so it is unusable without exporting the parse artifact too. Evaluation internal. |
| `effective_params` | The evaluation engine, in milli-units. Its result is what `acquire()` enforces; callers read limits through `LimitStatus`, not by re-running it. |
| `entry_params` | `effective_params` with the matching taken out: the §1.1/§1.2 override arithmetic for **one** entry, in milli-units. Split out so `schema._recovery_seconds` can price every window without a clock (#557) instead of restating the scale floor and the three absolute overrides. Internal to the same engine. |
| `next_boundary` | Computes a bucket's `vu` (valid-until) as the earlier of the next parameter change and the next reset edge. Purely a materialisation concern. |
| `prev_reset_edge` | The reset half of the same concern, scanning *backwards*: "was an edge missed since `rf`?" (§3.6). Only the materialising pass asks. |
| `next_reset_edge` | The forward twin, used to answer "when does this quota come back?" in a `retry_after_seconds` and serialised as `resets_at_ms` (#545). Shares one scanner with the internal `_next_reset_edge` (`_forward_reset_scan`); they differ only in how they report "nothing in reach" — `None` for a wait, `now + cap` for a `vu` stamp. **Both scan to the entry's own cycle, not to its probe step's cap (#574)** — see `_reset_scan` below. Callers read the answer off `LimitStatus`, not by re-scanning. |
| `cycle_seconds` | How long until a cron pattern's firing set repeats, read off the **coarsest** field it constrains: 31 days for `0 0 1 * *`, 366 for `0 0 1 1 *`, rounding up at every branch. Two unrelated callers ask it — `schedule._reset_scan` for a scan horizon, `schema._reset_cycle_seconds` for a quota bucket's TTL recovery horizon (#532) — and #574 is what letting the two answers drift looks like, so it lives here and `schema` delegates. Not exported for the same reason `entry_params` is not: it is engine-internal, and a caller wanting a limit's reset instant reads `LimitStatus` or `next_reset_edge`. |
| `_reset_scan` | Step **and** horizon for one reset entry, read off opposite ends of the expression (#574): the step from `_granularity` and the finest constrained field, so the match state is constant across it; the horizon from `cycle_seconds` and the coarsest, so it can actually reach the next edge. The `max` of the two, so the horizon can only grow — `* * * * *` constrains nothing and must keep its day-step 366-day cap. Not applied to `_next_param_change`, whose cap is a re-materialisation interval rather than a search horizon: a coarse *parameter* window is honoured exactly either way, because `effective_params` is evaluated at read time. Only an **edge** can be missed by being out of reach. |
| `retry_after_with_schedule` | The boundary-aware retry estimate (§7). Takes milli-units and a raw `shard_count`; every user-facing consumer reaches it through `LimitStatus.retry_after_seconds`. |
| `encode`, `decode` | The compact storage encoding (§4.1). Repository-internal; exporting it would freeze the on-item format as public API. |
| `to_cron` | Renders a *compact entry string* back to cron — and that string only comes from `encode()` or a raw DynamoDB attribute, neither of which is public. A user holding a `ScheduleEntry` reads `entry.cron`. Display helper for tooling that reads stored items. |

`schedule.py` imports nothing from `models.py`, and that one-way dependency is load-bearing:
it is what lets `schedule.py` be vendored into both Lambda packages. Re-exporting from
`__init__.py` does not disturb it — do not "tidy" the direction.

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
- Lambda function name (aggregator): `{name}-aggregator`
- Lambda function name (provisioner): `{name}-limits-provisioner`
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
- Components: `aggr` (Lambda aggregator), `prov` (Lambda provisioner), `app`, `admin`, `read`
- All components ≤ 8 characters (invariant for upgrade safety)
- Default names: `{stack}-aggr`, `{stack}-prov`, `{stack}-app`, `{stack}-admin`, `{stack}-read`
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
| Colon `:` | ✅ (for tag/version suffixes) |
| Hash `#` | ❌ (DynamoDB delimiter) |

**Valid resource names:**
- `api`, `gpt-4`, `gpt-3.5-turbo`
- `openai/gpt-4`, `anthropic/claude-3` (provider/model grouping)
- `anthropic/claude-3/opus` (nested paths)
- `llama3:8b`, `anthropic.claude-v2:1` (colon-separated tags, e.g. Ollama/Bedrock model IDs)

**Note:** Limit names (e.g., `rpm`, `tpm`) do NOT allow slashes or colons.

### Hot Partition Risk Mitigation (Issue #116)

Cascade (`cascade=True`) causes parent entities to receive traffic proportional to child count. High-fanout parents (1000+ children) risk exceeding per-partition throughput (~3,000 RCU / 1,000 WCU).

**Decision tree:**
- 0-500 children with cascade: Safe, no action needed
- 500-1000 children with cascade: Monitor with Contributor Insights
- 1000+ children with cascade: Implement write sharding (see [Performance Guide](docs/performance.md#write-sharding-for-high-fanout-parents)) or disable cascade

Primary mitigation: cascade defaults to `False`.

### Pre-Shard Buckets (GHSA-76rv-2r9v-c5m6, v0.9.0+)

Bucket items use per-(entity, resource, shard) partition keys: `PK={ns}/BUCKET#{id}#{resource}#{shard}, SK=#STATE`. This distributes write traffic across DynamoDB partitions.

**Write sharding mechanism:**
- A reserved `wcu` (write capacity unit) infrastructure limit is auto-injected on every bucket (capacity=1000, 1 per write = 1000 milli consumed)
- When `wcu` is exhausted on a shard, the client doubles `shard_count` and retries on a new shard, chosen at random from the shards it has not yet tried (`random.choice(untried)`, up to `_MAX_SHARD_RETRIES = 2` retries in `limiter.py`)
- Shard selection: `Repository.select_shard()` — `random.randrange(shard_count)` when `shard_count > 1`, else shard 0 — **random, not a hash of the entity id**. Every call re-picks, so one hot entity's writes spread across all of its shards; which shard holds which portion of its tokens is not predictable from the entity id. See [ADR-134](docs/adr/134-random-shard-selection.md) for why a hash (as GHSA-w6c2-33wf-qfwf suggested) would leave the hot entity on one shard
- Tests that need a bucket on a specific shard must pass an explicit `shard_id` to `speculative_consume()` (the parameter exists to skip random selection). Assuming a given `acquire()` lands on a particular shard is flaky by construction
- Effective per-shard limits: `capacity_milli // shard_count`, `refill_amount_milli // shard_count`. `BucketState.shard_count` is read from the item and `bucket.py` refills through `effective_capacity_milli` / `effective_refill_amount_milli`, so the client slow path caps each shard at its share exactly like the aggregator; `wcu` is never divided
- **Every shard must agree on `shard_count`**, because each refills toward its own `cp // shard_count`: a shard left on a stale lower count claims a *larger* share and the shares sum to more than the configured limit. A client that wins a bump propagates it (`Repository._propagate_shard_count()`, mirroring the aggregator's Path 1) with concurrent conditional `UpdateItem`s (`shard_count < :new`) to shards `1..old_count-1` — monotonic and idempotent, so racing the aggregator or another client is a no-op. Shards `old_count..new_count-1` are not written: they do not exist yet and are created with the current count by whoever draws them
- **Statuses report the share, not the config (#475):** `Limit.per_shard(shard_count, now_ms)` is the **single** place a reported `Limit` is narrowed, and it narrows on both axes at once — the schedule in force at `now_ms` scales the undivided base (#222 §3.5), then the shard takes `capacity // shard_count` and `refill_amount // shard_count`. It is applied wherever a `LimitStatus` is built: `bucket.declared_statuses()` on the fast path, `RateLimiter._admit_limit()` and the lease statuses on the slow path — so `RateLimitExceeded` never promises a capacity no shard can serve, nor a base capacity a `0.5x` window has already halved. It is identity only for an unscheduled limit at `shard_count == 1`, the result carries no schedule (it is a point-in-time value), and `refill_period_seconds` follows the window but is **never** divided — shards split the numerator and all refill on the same clock. `Limit.from_bucket_state(state)` deliberately does **not** narrow: it returns the item's undivided base plus its `sched`, which is what `LeaseEntry.limit` carries on both paths, so the lease's own `per_shard` call divides exactly once. Pre-dividing there made a 4-shard fast-path lease report `capacity // 16`. Sub-token shares clamp to 1 (`Limit` validates `capacity > 0`). Retry estimates use `BucketState.retry_refill_amount_milli`, which falls back to the **undivided** rate when the share floors to 0 (1 token/min at `shard_count=1024`), and `calculate_retry_after` guards a stored rate of 0 instead of raising `ZeroDivisionError`
- **A limit change fans out to every shard (#468):** stored `cp`/`ra`/`rp` are **undivided** on each shard item, so `Repository._sync_bucket_params()` — reached by `set_limits()` and by `delete_limits()`'s `reconcile_bucket_to_defaults()` — discovers all shards via GSI3 (`GSI3PK={ns}/ENTITY#{id}`, `GSI3SK begins_with BUCKET#{resource}#`) and writes the new params to each concurrently, two discovery passes like the ADR-125 disable fan-out. Keying only shard 0 left shards 1..N-1 enforcing the limits they were born with forever, and nothing detects the drift: `SpeculativeFailureReason` has no `CONFIG_CHANGED`, the speculative success branch never compares the item's `cp`/`ra` against config, and the slow path refills from the stored values. Cost is O(shards) WCU + the KEYS_ONLY queries, on an admin path. The same write also re-stamps `sched`/`sched_tz` and the per-limit `b_{name}_sched` overrides (SET where a limit differs from the item default, REMOVE everywhere else — absence means "inherit the default", so a superseded override left behind keeps that limit on the old schedule forever; a limit with **no** schedule is SET to `BUCKET_SCHED_NONE` rather than REMOVEd, see #541 below), and SETs **`vu = 0` unconditionally, scheduled or not** (#222 Task 13). That last one is what lets #222 subsume #469: since #496 `refill_bucket` clamps on every path, but the speculative fast path is a pure `ADD` with no cap maths, so after a capacity shrink nothing else trims the surplus before it is spent. `vu = 0` forces exactly one materialising pass, which clamps and then clears the stamp again (`build_composite_normal(clear_vu=True)`) so the fast path is restored after one demoted acquire rather than lost permanently. Mirrored in `zae_limiter_provisioner/bucket_sync.py`, which has the identical exposure on every manifest apply. Named residual of GHSA-w6c2-33wf-qfwf. `set_resource_defaults()` / `set_system_defaults()` do **not** touch buckets at all — buckets running on defaults carry a TTL and are recreated with the current params when it expires (#271, #296)
- **An entity-wide change fans out across every resource (#487):** `_default_` is the entity-WIDE config scope, not a resource, so `_sync_bucket_params()` translates it to an **unscoped** GSI3 discovery (`BUCKET#` prefix) exactly as the ADR-125 disable fan-out does. Forwarding the sentinel built `BUCKET#_default_#`, which no bucket item can carry, so `set_limits(entity_id, limits)` (the no-resource default) and `delete_limits(entity_id)` matched zero items and wrote nothing, silently, forever — entity configs carry no TTL, so nothing recreated the drifted buckets. Because Entity(resource) outranks Entity(`_default_`), each discovered bucket is then stamped from the limits **resolved for its own resource** (`_resolved_bucket_param_update()`, memoized per distinct resource), never from the caller's — stamping the caller's would clobber a resource that has its own entity config. Two consequences: the **TTL multiplier is decided per bucket** from the resolved level (entity/entity_default ⇒ REMOVE `ttl`, resource/system ⇒ SET it), not fixed at the call site; and the caller's `stale_limit_names` are **intersected** with each bucket's resolution, since a name still declared by that bucket's own level would otherwise be SET and REMOVEd in one expression (a DynamoDB `ValidationException`), while a directive limit absent from that resolution is stale there. `set_limits()` evicts the config cache **before** the sync so the per-resource resolution cannot read the level it just replaced. Mirrored on the Lambda side by `zae_limiter_provisioner/bucket_sync.py` (`resolve_bucket_limits()`, `_resolved_plan()`). Cost is O(distinct resources) config walks + O(buckets) WCU, on an admin path. Third instance of the #468 / #481 class
- **The param sync is serial and reports partial progress:** unscoped, the write set is O(resources × shards) rather than the ≤ `MAX_SHARD_COUNT` of one resource, so `_sync_bucket_params()` writes **serially** like `_fanout_entity` instead of one unbounded `asyncio.gather`. A write that fails part-way raises **`FanoutIncomplete`** (not the raw `ClientError`) carrying the exact number of bucket items already written — the config item is committed before the fan-out runs, so a half-applied table needs a progress count, and every write is idempotent so re-running the same call reconciles the rest. Same contract the ADR-125 disable fan-out already used on this same method. Stale-limit `REMOVE` aliases use monotonic counters (`#stale{i}_{j}`), never the limit name: `NAME_PATTERN` allows `-` and `.`, neither legal in an `ExpressionAttributeNames` alias (`.` is a document-path separator), and the resulting `ValidationException` would land *after* the config write
- **Known limitation:** a single request above `capacity // shard_count` is unadmittable on *every* shard while the entity is under its configured limit; `MAX_SHARD_COUNT = 32` bounds how small a share gets. Surfacing an occurrence as an event/metric is #475
- `wcu` is filtered from user-facing output (`get_buckets`, `RateLimitExceeded`, usage snapshots)

**Client-side shard creation (ADR-133, issue #439) — no aggregator dependency:**
- The slow path targets the **same shard** the speculative attempt selected, sized by the `shard_count` on its failure image: `_try_speculative_acquire()` hands both to `_do_acquire(shard_id=..., shard_count=...)`, which passes them through `Repository.select_shard()`, `batch_get_entity_and_buckets` / `batch_get_buckets` (keys are `(entity_id, resource, shard_id)`) and into `LeaseEntry._shard_id` / `_shard_count` for `_commit_initial()`. A failed speculative write never updates the entity cache, so the cache is not consulted for a shard the fast path already observed
- A missing shard N>0 is created by the client with `wcu` undivided, stored `cp`/`ra` undivided, and the observed `shard_count` stamped — identical to the aggregator's Path 2 clone. A **dripping** limit's new shard starts at `capacity_milli // shard_count`, and that does not multiply total capacity: the stored `ra` is undivided, so every shard refills at `ra // shard_count`, the ceilings still sum to `capacity`, and a new shard starting full is a one-off burst of at most one `time_to_fill` — which token-bucket semantics already permit. A doubling does not rewrite shard 0's balance, but it no longer survives either: `refill_bucket()` clamps to `min(capacity_milli, tokens_milli)` on **every** path, including the two that add no tokens, so shard 0 is trimmed to its new share on the next refill pass by whichever refiller gets there first — the client slow path or the aggregator, which writes the negative delta as an `ADD` (#222 §3.3, replaces #469). Steady state and the transient are both `capacity`. Debt is untouched: the clamp is `min()`, not a clamp into range
- **A quota's new shard is filled by transfer, never minted (#587).** A quota has no drip at all (ADR-137), so for it the paragraph above does not hold: a fresh `capacity // shard_count` is net-new allowance, and nothing reclaims it before the next reset edge. Measured on `Limit.quota("rpd", 1000, cron="0 0 * * *")` with the clock frozen inside one period, a `wcu` walk 1→32 admitted **3496 against a configured 1000 — 3.5x**; the single clearest step is an entity that had spent 998 of 1000 and held 2 tokens finding itself holding **501** after one `acquire()` tripped `wcu`. So before the shard is created, `Repository.reclaim_quota_surplus()` (mirrored in the aggregator by `processor._reclaim_quota_surplus`) **eagerly applies the clamp** to every shard that already exists — one conditional `UpdateItem` per shard holding a surplus, `SET tk = :share` under `tk > :share` with `ReturnValues=UPDATED_OLD` — and the new shard starts with exactly what that took, capped at one share (`models.new_shard_starting_tokens_milli`). A full entity therefore still gets a full new share, paid for by the clamp on the shard it split from (the pre-#587 behaviour, which was right for that case and is why the bug hid); a spent entity gets nothing. The clamp is eager rather than left to the siblings' next pass because the speculative fast path is a pure `ADD` with no ceiling arithmetic, so an untrimmed sibling would spend the same surplus the new shard was just granted. No token is destroyed that was not already doomed — the clamp takes exactly that much whenever it next runs — so a reclaim followed by a rejected acquire costs the entity nothing. Keyed on `Limit.is_quota`, the **structural** predicate, never `BucketState.accrues`, which is also true of a dripping limit whose share has floored to zero (#475). Cost: 1 GSI3 KEYS_ONLY query + 1 `BatchGetItem` + at most one write per surplus-holding shard, once per shard creation, and only for a resource that carries a quota
  - **Zero-filling and blind redistribution were both considered and rejected.** Zero-filling never over-admits but runs away: a shard that can admit nothing still takes its share of ADR-134's random draws and rejects them, every successful write piles back onto the one shard with tokens, that shard trips `wcu` again, and the count walks to `MAX_SHARD_COUNT` with the whole balance clamped onto a single `C // 32` — 3% of the quota reachable and ~91% spurious rejections for the rest of the period. Redistribution (deduct a blind `old_share / 2` from each existing shard at the doubling, create the new ones at the new share) does not fix the over-admission at all: on a spent quota the deduction lands as **debt that nothing ever repays**, while the new shard's share is immediately spendable, so the entity still gains `C/2` per doubling — arithmetically identical to the bug. Conserving the *sum* of the balances is not the same as conserving what can be **spent**
- After wcu-driven doubling the slow path draws from the **newly added** shards (`random.randrange(old_count, new_count)`); `bump_shard_count()` returns the winner's count (via `ReturnValuesOnConditionCheckFailure=ALL_OLD`) when another client doubled first, so a losing client still draws from the new range. A non-cascade shard-retry that hits `BUCKET_MISSING` stops probing and sends the slow path to create that shard instead of fast-rejecting
- **A doubling is gated on the acquire proceeding, on the child path too (#480).** `_try_speculative_acquire` runs `_check_speculative_failure` *before* `_shard_after_wcu_exhaustion`, the same ordering #474 established for the cascade parent. A `BOTH_EXHAUSTED` failure — reserved `wcu` drained **and** a declared limit drained — is on its way to a `RateLimitExceeded`, and doubling there is a pure side effect: `_learn_shard_count` is monotonic, so one doubling per rejection walks an entity at its limit to `MAX_SHARD_COUNT` and pins every shard at `capacity // 32` forever (#475). Worse before the gate: the doubling handed the slow path a shard it then *created* holding `capacity // 2`, so the rejection became an admission. A plain `WCU_EXHAUSTED` passes the gate by construction — `consume` never names `wcu`, so every declared limit is satisfiable — which is what keeps the GHSA-76rv mitigation and ADR-133's wcu-refill route (slow path on the same shard) intact
- Cascade: a cascading child never accepts a child-only retry lease (it would bypass the parent); on `APP_LIMIT_EXHAUSTED` it hands an untried shard to the slow path, which commits child + parent in one transaction. Speculative compensation credits the shard that was debited (`result.shard_id` / `parent_result.shard_id`), and the parent-only slow path reuses `parent_result.shard_id`
- Race with the aggregator: both create under `attribute_not_exists(PK)`; a lost race routes `_commit_initial()` to the consumption-only retry (`tk >= consumed`) on that same shard — one extra WCU, no over-admission. If a *sibling* item cancelled the transaction instead (parent rf lock), the innocent Put is re-issued from its per-index `CancellationReasons` entry
- Write sharding therefore engages with `--no-aggregator`; the aggregator's proactive sharding and propagation below are an optimization, not a requirement

**Aggregator proactive sharding (optional):**
- Monitors `wcu` consumption ratio per bucket in each stream batch
- When consumption >= 80% of capacity (`WCU_PROACTIVE_THRESHOLD = 0.8`), doubles `shard_count` on shard 0
- Propagates `shard_count` changes from shard 0 to all other shards via conditional writes, pre-creating new shard items so clients skip the one-time create slow path
- Path 2 pre-creates **all** the new shards in one pass, so an unguarded quota there grants the full `C/2` immediately rather than lazily. It runs the same reclaim-then-grant as the client (`_reclaim_quota_surplus`, #587): clamp shards `0..old_count-1` to their new share, then hand the pool out **greedily** across the new shards — the first usable one gets a full share rather than every clone getting a slice too small to admit anything. Dripping limits keep `scaled_cp // new_count` unchanged

**GSI3 bucket discovery:**
- Bucket items set `GSI3PK={ns}/ENTITY#{id}, GSI3SK=BUCKET#{resource}#{shard}`
- `get_buckets(entity_id)` uses GSI3 (KEYS_ONLY) to discover all bucket PKs, then `BatchGetItem` to fetch full items

## Key Design Decisions

### Integer Arithmetic for Precision
- All token values stored as **millitokens** (x1000)
- Refill rates stored as fraction: `refill_amount / refill_period_seconds`
- Avoids floating point precision issues in distributed systems

### Token Bucket Algorithm
- Buckets can go **negative** for post-hoc reconciliation
- Refill is calculated lazily on each access
- `capacity` is the bucket ceiling; factory methods accept `burst` to set `capacity > refill_amount`
- A **quota** (`Limit.quota()`, ADR-137) does not drip at all: `refill_amount` is 0 and the balance is set to the effective capacity at each `reset_schedule` edge. It takes no `burst` — `capacity` *is* the allowance, so headroom above a sustained rate is meaningless. A limit drips or resets, never both and never neither

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

### Scheduled Limits (#222, ADR-135)

A `Limit` carries two independent tuples of `ScheduleEntry`. `schedule` is **level**-triggered
and answers "what is the limit right now?" — an entry is active while `now` matches all five
cron fields as sets, first match wins, no match means the base. `reset_schedule` is
**edge**-triggered and answers "when does the balance go back to full, in one lump?" — it fires
on the transition *into* matching, which is why `0 0 * * *` is correct there and would be a
one-minute window in `schedule`. `Limit.quota()` is the only constructor for the second
(ADR-137: a limit drips or resets, never both, so the allowance and the reset must arrive
together); windows are fixed calendar windows, never anchored to an entity's own first use
(ADR-138).

Both ride on the `Limit` through the existing four-level resolution, so no setter signature
changed and inheritance is **override, not merge** — an entity-level limit with no schedule
removes the resource-level schedule for that entity exactly as it already replaces the numbers.

The mechanism is read-time resolution: `cp`/`ra`/`rp` on the item stay the undivided **base**
forever, every refiller computes `effective = f(base, sched, now)` as local variables, and the
only materialised quantity is `tk`. The fast path evaluates nothing and is gated by `vu` alone.
The pieces are documented where they live — `vu` and `SCHEDULE_BOUNDARY` under the speculative
write pattern, `sched`/`rsched`/`sched_tz` under the config attribute format and the writer
table, the quota TTL horizon under ADR-136, and the boundary walk under Exception Design.

**The compact encoding is versioned (#515).** Every stored `sched` / `rsched` /
`b_{name}_sched` / `l_{name}_sched` value opens with its encoding version as decimal digits —
`1h9-17w1-5s500;h0-6c2000`, `1m0h0` — emitted once per attribute by `schedule.encode` /
`encode_reset` and checked first by `decode` / `decode_reset` / `to_cron`. **One byte**: no
legal entry can begin with a digit (`_encode_cron` always emits `tag + spec`, every tag a
letter), so no delimiter is needed, and the 1 KB WCU boundary §4.2 defends is shared with
everything else on the item.

**An unversioned string is rejected, not read as v1.** The usual tolerant-reader concession
would serve an empty population: the encoding is unreleased, so no unmarked item exists.
Rejecting keeps the invariant checkable and is what lets a newer-client value be diagnosed at
offset 0. Three distinct greppable messages — `carries no version marker`, `is encoding version
N; this build reads up to version M`, and the pre-existing `cannot parse from offset N` /
`invalid cron expression` — all still plain `ValueError` (§6.1), so the aggregator's
`except ValueError` still catches them and `_decode_stored_schedule` still converts to
`RateLimiterUnavailable`.

What this buys is a **log line, not behaviour**, which is why it was deferrable at all: design
§6.5's table had a newer client's appended modifier tag (`h9-17s500q42`) failing as
`invalid literal for int()`, indistinguishable from corruption, because `_tokenise`'s value
pattern absorbs an unknown tag that does not sit at an entry boundary. The version is now read
before the body, so that diagnosis no longer depends on where the unknown token sits. The
tokeniser heuristic survives as a statement about corruption *within* a version.

Measured: the §4.2 worst shared case (6 limits x 4 entries, one shared schedule) is **846 B**,
**178 B** under the boundary, pinned exactly by
`TestSizeBudget.test_the_worst_shared_case_stays_under_one_kb`. That headroom is shared with
unlanded work — see the design doc's headroom note.

**Magnitude bounds on every numeric modifier (#570).** `schedule.py` names three ceilings —
`MAX_TOKENS = 10**15`, `MAX_PERIOD_SECONDS = 10**9`, `MAX_SCALE = 10**6` — enforced by
`ScheduleEntry.__post_init__` on `scale` / `capacity` / `refill_amount` /
`refill_period_seconds` and by `Limit.__post_init__` on the base `capacity` /
`refill_amount` / `refill_period_seconds`. Each raises a plain `ValueError` naming the field,
the value and the bound, so `manifest._parse_entries` reports it as `schedule[i]: …` and
`zae-limiter limits plan` fails before anything is written.

Only one number here is derived: `MAX_STORED_MILLI = 10**38 - 1`, DynamoDB's largest exactly
storable integer (38 significant digits — boto3's serializer raises `decimal.Rounded` at
`10**38`, before the request is sent). The three ceilings are judgement calls chosen so that no
product of them can reach it: `10**15 × 1000 × 10**6 = 10**24`, fourteen orders inside.
**Bounding `scale` alone would not have worked**, which is why `Limit` is narrowed too: the
quantity that must stay storable is the product `capacity × 1000 × scale`, so with an unbounded
base the overflow threshold stays data-dependent — the same `ScheduleEntry` raising
`OverflowError` on a `tpm` of 10,000,000 and returning cleanly on an `rpm` of 10, on the acquire
slow path, as a 500. The check is ordered **after** #564's finiteness and #569's integrality
guards (so a NaN or a `1.5` still reports its own message) and after the positivity test, whose
upper half it is.

See [ADR-135](docs/adr/135-scheduled-limits.md) for the decision and the alternatives
considered, and `docs/plans/2026-09-13-scheduled-limits-design.md` for the full design.

### Combined Capacity Check (Issue #472)

`RateLimiter.check_availability(entity_id, resource, needed=None, limits=None) -> Availability`
is the **one** non-consuming read path. `available()` and `time_until_available()` are thin
wrappers over it (both keep their signatures, return types and the deprecated
`use_stored_limits`), so there is one implementation rather than three.

**Why a combined call, and why it is not "convenience".** The driving consumer is a UI that
displays "47 remaining · resets in 3m 12s", per limit. Calling the two methods separately is
two reads at two *instants*: tokens refill in between and each discovers the entity's shards
independently, so the pair can render "0 remaining · available now" or "47 remaining · 0s".
Everything on `Availability` is derived from one snapshot stamped `checked_at_ms`, so the
numbers cannot contradict each other.

`Availability` is a frozen dataclass carrying `entity_id`, `resource`, `checked_at_ms`, and
`statuses: list[LimitStatus]` — one per resolved limit, reusing the model
`RateLimitExceeded` already carries. `limits`, `available`, `needed`, `retry_after_seconds`
(max over statuses), `allowed`, `exceeded` and `deficit` are **derived** properties, plus
`status(limit_name)`. A `needed` key naming no resolved limit is ignored.

**Two bugs this fixed on main, which is most of its value:**
- `time_until_available()` issued one `get_bucket()` **per limit** against a single ADR-114
  composite item — a pure N+1.
- `get_bucket()` defaults to `shard_id=0`, so that wait estimate was computed from one
  shard's balance *and* one shard's share of the refill, while `available()` summed across
  shards (#466). On a 2-shard entity holding 40 tokens at 100/min the pair reported "40
  available" and "47.97s until 60" where the truth is ~12s.

**Sharding:** sums across every shard (GSI3), matching `available()` and
`get_resource_capacity()`, and computes the wait from the **summed** refill rate against the
same `available` it reports. Shares that floor to 0 fall back to the undivided rate, as
`BucketState.retry_refill_amount_milli` does. The reported `limit` is therefore the
**undivided** config, unlike the per-shard statuses in `RateLimitExceeded` (`Limit.per_shard()`,
#475). Known limitation inherited from #475: a single request above `capacity // shard_count`
is unadmittable on every shard, so `acquire()` can reject an amount this reports as available.

**Schedule-aware (#222 §7).** Everything reported is the value in force at `checked_at_ms`,
not the stored base. The ceiling comes from `effective_params`, so a `scale: 0.5` window reports
500 rather than 1000 — this covers *both* base-capacity sites, the `min(total, capacity)` clamp
and the missing-bucket branch, neither of which any `BucketState` conversion reaches because
both work from the config-resolved `Limit`. The wait walks forward across boundaries
(`schedule.retry_after_with_schedule`) instead of dividing by the rate that happens to apply
right now, and a limit with a `reset_schedule` reports the wait to its next edge — for a daily
quota, whose `refill_amount` is 0 by ADR-137, the only finite answer there is. A bucket that
crossed a reset edge and has not been written to since reports the balance the next `acquire()`
will restore (`RateLimiter._readable_balance`), decided **per shard** rather than per limit
name, so one stale shard cannot report the whole entity restored. Without that, the display
reads "0 remaining, resets at midnight tomorrow" while the very next `acquire()` restores the
quota immediately.

Non-consuming and write-free, and **not** a pre-flight gate for `acquire()`: check-then-acquire
is TOCTOU and costs an extra read, where `acquire()` answers the same question in 1 WCU (0 RCU +
0 WCU on a fast rejection) via `RateLimitExceeded.retry_after_seconds`. It is for *display*.

Cost: 1 GSI3 KEYS_ONLY query + 1 `BatchGetItem` + 1 config resolution, regardless of limit
count or shard count. A missing bucket means full capacity and no wait.

### Exception Design
- `RateLimitExceeded` includes a status for **every limit declared in `consume`** — both the ones that were exceeded and the ones that passed. Limits the caller did not name (and the reserved `wcu`) never appear (Issue #455), on the fast path, the slow path, and the consumption-only retry path alike
- Each status reports the **effective per-shard, in-window** capacity and refill, not the undivided config (`Limit.per_shard(shard_count, now_ms)`, #475 / #222 §3.5) — see [Pre-Shard Buckets](#pre-shard-buckets-ghsa-76rv-2r9v-c5m6-v090)
- `retry_after_seconds` **walks schedule boundaries** rather than dividing by the rate in force now (`schedule.retry_after_with_schedule`, #222 §7). The flat estimate over-reports when a boundary raises the limit and under-reports when one lowers it, which is the headline use case: empty bucket, 500 needed, 1000/min now, a boundary in 10 s dropping to 500/min is **50 s**, not 30. A `reset_schedule` edge landing before the deficit clears **is** the answer — a quota has no drip at all under ADR-137, so "at midnight" is the only finite answer. Capped at eight windows, then the flat estimate (which still carries #530's reset branch). Wired at all **four** `LimitStatus` sites, not the three the plan named: `bucket.try_consume` covers the speculative fast rejection (`declared_statuses` / `would_refill_satisfy`) and slow-path admission (`_admit_limit`) at once, and `lease._build_retry_failure_statuses` and `RateLimiter.check_availability` convert individually
- `Limit.from_bucket_state()` reconstructs a **quota** as a quota: the `max(1, …)` rate floor and `reset_schedule` move together (a floored `refill_amount=1` beside a reset is what ADR-137 rejects), keyed on the stored shape `refill_amount_milli == 0 and reset_sched`. A corrupt item carrying a reset beside a positive rate keeps the floor and drops the tuple rather than raising from inside a rejection path
- `bucket.calculate_retry_after` and `BucketState.retry_refill_amount_milli` have **no production callers** since #222 §7; both remain as the definitions the walk is pinned against. The walk cannot call either — `bucket` imports `models` imports `schedule`, and `schedule` may import neither (that one-way dependency is what lets both Lambdas vendor it), so the arithmetic and the #475 floored-share rule are re-derived there and held identical by test
- **`as_dict()` shapes each limit by how it recovers (#545).** Every entry carries a `kind`, and the recovery fields follow it: `"rate"` keeps `refill_amount` / `refill_period_seconds`, `"quota"` **omits both** and carries `resets_at_ms` instead — the absolute epoch-ms instant of the next reset edge (`schedule.next_reset_edge`), `null` when no edge is inside the forward-scan horizon. A quota's `refill_amount` is 0 by ADR-137 and its `refill_period_seconds` is the inert `_QUOTA_REFILL_PERIOD_SECONDS`, so serializing them put "refills 0 tokens every 1 second" into 429 bodies — false about a limit that returns whole at a calendar instant, and acted on programmatically rather than merely squinted at. `kind` is on **both** shapes so no consumer infers a quota from `refill_amount == 0`, which is unsafe in both directions (a dripping limit's share can floor to zero, #475; #556 gives a scaled quota a phantom 1-milli drip). Derived from `Limit.is_quota`, the structural predicate. `as_dict()` reads the clock **once** for the whole body, so two quotas on one rejection cannot report edges scanned from different instants. An absolute instant rather than the cron string the CLI shows (`cli._format_limit`): an operator reading a terminal wants the recurrence, an HTTP client wants a timestamp it can schedule against without a cron parser — and being absolute it needs no companion `checked_at_ms`. `resets_at_ms: null` is genuinely rare since #574 — see below
- **The reset scan reaches the reset's own cycle (#574).** `schedule._reset_scan` takes the probe *step* from the finest constrained cron field and the *horizon* from the coarsest (`cycle_seconds`); conflating the two gave every reset a 7-day horizon, because every practical reset pattern pins the minute. Both forward surfaces were wrong past that: `next_reset_edge` returned `None` for a monthly `0 0 1 * *` for ~24 days out of every 30 (so `resets_at_ms` was `null` exactly where a client most needs it), and an **annual quota's `retry_after_seconds` was `0.0`** — "retry immediately" against a limit that could not admit anything for months, i.e. a hot retry loop driven by the 429 itself. The old reach on that surface was 56 days, the product of the 7-day cap and `retry_after_with_schedule`'s `max_windows = 8`; `max_windows` is **unchanged** and was never the fix, because a quota's rate is zero in every window and the zero-rate branch returns the edge on iteration *one* as soon as the scan can see it. The backwards twin `prev_reset_edge` is widened by the same change, which is what stops a bucket idle across a monthly edge from never resetting at all. Cost went **down**: skipping whole local days and hours that the date and hour fields rule out (`_unreachable_block`) bounds a search at (days in horizon) + 24 + 60 ≈ 450 probes, against the 10,080 a flat 7-day minute walk cost — an annual quota's rejection path measured 171,377 `matches` calls and 413 ms before, 222 calls and 1.8 ms after. Residual: a pattern that skips whole years (`0 0 29 2 *`) is still out of reach three years in four, and still reports `None`, which is the documented reading
- Both `violations` (exceeded) and `passed` (ok) are available
- `retry_after_seconds` calculated from primary bottleneck

## Common Tasks

### Adding a New Limit Type
1. No code changes needed for any **dripping** shape - `Limit.custom()` covers it. A calendar allowance needs `Limit.quota()` (it takes `cron`/`tz`, and `refill_amount=0` without a `reset_schedule` is rejected by `__post_init__` under ADR-137); a cron-varied limit needs `.with_schedule()`
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

### Planning Artifacts

Store superpowers artifacts (brainstorming design specs and writing-plans implementation plans) under `docs/plans/`, named `<YYYY-MM-DD>-<topic>-design.md` / `<YYYY-MM-DD>-<topic>-plan.md`. This overrides the skill default of `docs/superpowers/specs/`.

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
   - **`consume` is the declared scope of a lease (Issue #455)**: only limits named in `acquire(consume=...)` are adjustable through `adjust()`/`consume()`/`release()` and reported by `lease.consumed`, on both the fast and slow paths. The slow path still builds a `LeaseEntry` for every resolved limit because `_commit_initial()` needs them (`build_composite_create` writes only the states it is handed; `build_composite_normal` advances the shared `rf` and credits refill only to the limits it is handed), but those carry `_declared=False` and are write-only. A key that names no declared limit (a typo, or the reserved `wcu`) is ignored with a `FutureWarning` (not `DeprecationWarning`, which Python hides by default outside `__main__` and so would never surface from application code) naming the keys and the declared limits; it becomes a `ValidationError` at v1.0.0. The `on_unavailable=ALLOW` no-op lease is constructed with `degraded=True` and is exempt — never infer degradation from `entries == []`
3. **Cascade is per-entity config**: Set `cascade=True` on `create_entity()` to auto-cascade to parent on every `acquire()`
4. **Stored limits are the default (v0.5.0+)**: Limits resolved from System/Resource/Entity config automatically. Pass `limits` parameter to override.
5. **Initial writes are atomic + optimistic lock on refill**: `_commit_initial` uses `transact_write` for cross-item atomicity. `build_composite_normal` locks on `last_refill_ms` (`ConditionExpression: #rf = :expected_rf`) to prevent stale refill overwrites. On lock failure, `build_composite_retry` skips refill and uses `tk >= consumed` condition to prevent over-admission
6. **Adjustments and rollbacks use independent writes**: `_commit_adjustments()` and `_rollback()` use `write_each()` (1 WCU each) since they produce unconditional ADD operations that do not require cross-item atomicity
7. **Transaction item limit**: DynamoDB `TransactWriteItems` supports max 100 items per transaction. Cascade operations with many buckets (entity + parent, multiple resources x limits) must stay within this limit
8. **Speculative writes are pre-committed**: When the speculative path succeeds, `_commit_initial()` is a no-op because the UpdateItem already persisted the consumption. Rollback compensates with `build_composite_adjust` + `write_each`
9. **Entity metadata cache is immutable**: `Repository._entity_cache` stores `{entity_id: (cascade, parent_id, shard_counts)}` where `shard_counts` is `dict[str, int]` mapping resource to shard_count. Entity metadata (cascade, parent_id) is set once at `create_entity()` and never changes; shard_counts are updated when shard doubling occurs. Populated from speculative result (ALL_NEW) or slow path (entity META record)

## DynamoDB Pricing Reference

On-demand pricing (us-east-1, post-Nov 2023 50% reduction):
- Write Request Units: **$0.625/M** ($1.25/M for transactional writes)
- Read Request Units: **$0.125/M** ($0.25/M for transactional reads)

Non-cascade `acquire()` = 1 RCU + 1 WCU = $0.125 + $0.625 = **$0.75/M** (the project's advertised cost).

Speculative non-cascade `acquire()` (success) = 0 RCU + 1 WCU = **$0.625/M** (~17% savings).
Speculative fast rejection (exhausted) = 0 RCU + 0 WCU = **$0/M** (free).
Speculative fallback (refill helps) = 1 RCU + 2 WCU = $0.125 + $1.25 = **$1.375/M** (worse than normal).
Client shard create (`BUCKET_MISSING` on shard N, ADR-133, warm config cache) = 2.5 RCU + 2 WCU (1 failed conditional + disable-walk BatchGet 1.5 RCU + META/bucket BatchGet 1 RCU + single-item `PutItem`) = $0.3125 + $1.25 = **$1.56/M**, paid **once per shard** (+1 WCU when a wcu bump precedes it: **$2.19/M**); the previous broken fallback cost the same on every acquire that drew a missing shard.
Speculative cascade (both succeed, sequential) = 0 RCU + 2 WCU = **$1.25/M** (vs $1.75/M normal cascade).
Speculative cascade (both succeed, parallel, issue #318) = 0 RCU + 2 WCU = **$1.25/M** (same cost, lower latency).
Speculative cascade fallback (parent refill helps) = 0.5 RCU + 3 WCU = **$1.94/M** (deferred compensation).
Speculative cascade fast rejection (parent exhausted) = 0 RCU + 2 WCU = **$1.25/M** (child consumed + compensated).

`resolve_disabled()` (ADR-125) is deliberately uncached and only runs on the slow path — never
on the speculative fast path — but when it does run it costs an extra up-to-3-key
`BatchGetItem` (entity(resource), entity(`_default_`), resource), and a second one for the
parent on a cascade slow path (`limiter.py` resolves child and parent independently).

## DynamoDB Access Patterns

All PK and GSI PK values are prefixed with `{ns}/` where `{ns}` is the opaque namespace ID (e.g., `a7x3kq`). The reserved namespace `_` is used for namespace registry records.

| Pattern | Query |
|---------|-------|
| Get entity | `PK={ns}/ENTITY#{id}, SK=#META` |
| Get bucket (single shard) | `PK={ns}/BUCKET#{id}#{resource}#{shard}, SK=#STATE` |
| Get buckets (all for entity) | GSI3: `GSI3PK={ns}/ENTITY#{id}` → BatchGetItem (GHSA-76rv) |
| Batch get buckets | `BatchGetItem` with `PK={ns}/BUCKET#{id}#{resource}#{shard}, SK=#STATE` pairs |
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
| Discover buckets for entity | GSI3: `GSI3PK={ns}/ENTITY#{id}` (KEYS_ONLY, GHSA-76rv) |
| List resources with entity configs | `PK={ns}/SYSTEM#, SK=#ENTITY_CONFIG_RESOURCES` (wide column, issue #288) |
| Namespace forward lookup | `PK=_/SYSTEM#, SK=#NAMESPACE#{name}` |
| Namespace reverse lookup | `PK=_/SYSTEM#, SK=#NSID#{id}` |
| List all items in namespace | GSI4: `GSI4PK={ns}` |
| Get provisioner state | `PK={ns}/SYSTEM#, SK=#PROVISIONER` |

**Optimized read patterns (issue #133):**
- `acquire()` uses `BatchGetItem` to fetch all buckets for entity + parent in a single round trip
- This reduces cascade scenarios from N sequential GetItem calls to 1 BatchGetItem call

**Speculative write pattern (issue #315, GHSA-76rv):**
- `speculative_consume()` targets `PK={ns}/BUCKET#{id}#{resource}#{shard}, SK=#STATE`
- Issues a conditional `UpdateItem` with `ADD -consumed` for all user limits + wcu, condition `attribute_exists(PK) AND tk >= consumed`
- Uses `ReturnValuesOnConditionCheckFailure=ALL_OLD` to return bucket state on failure without a separate read
- Uses `ReturnValues=ALL_NEW` on success to reconstruct `BucketState`, `cascade`, `parent_id`, and `shard_count` from the response
- Cascade, parent_id, and shard_count are denormalized into bucket items (via `build_composite_create`) to avoid entity metadata lookup
- On `wcu` exhaustion, doubles `shard_count` on the current shard and retries on a new shard
- Condition includes `attribute_not_exists(#disabled)` alongside the TTL guard (ADR-125), rejecting buckets stamped disabled without any config read
- Condition also includes `(attribute_not_exists(#vu) OR #vu > :vu_now)` (#222 §2.1). `vu` (valid-until, epoch ms) is the earliest instant at which any limit on the item changes effective params, precomputed by whoever last materialised `tk`; past it the fast path must not spend tokens that were minted under parameters no longer in force. Absent means "no schedule, never expires", so every unscheduled bucket — which is every bucket written before scheduling existed — passes unchanged. `:vu_now` is the **bound** `now_ms` (#430), the same instant the `ttl` stamp and the TTL guard use: the fast path still reads the clock exactly once and still evaluates no schedule and reads no config
- A failure tripping `vu` classifies as `SpeculativeFailureReason.SCHEDULE_BOUNDARY`, **ahead of `DISABLED`'s successor checks and every exhausted reason** — a closed window is not a rejection, and reading it as one would raise `RateLimitExceeded` against limits the new window may have just raised, or (worse) read as `WCU_EXHAUSTED` and double `shard_count` at every boundary. `DISABLED` still outranks it: no re-materialisation admits a disabled bucket. The limiter routes `SCHEDULE_BOUNDARY` to the slow path — the only place that re-materialises — never to a fast rejection and never to a shard retry, since every shard crosses the same boundary. A *probed* shard that reports it (the transient where shards re-materialise one at a time) is handed to the slow path exactly as a `BUCKET_MISSING` probe is. On the cascade path a boundary-expired **parent** refunds the child's speculative debit and falls back to the full slow path rather than the parent-only one, costing one extra compensating write per crossing
- **The slow path is the only client that writes `vu` (#222 §2.1).** `_commit_initial()` takes the minimum `next_boundary()` across **every** limit sharing the bucket item — including the ones the caller did not name in `consume`, since the same write materialises them and `vu` is one item-level attribute — and passes it to `build_composite_normal(vu=...)` / `build_composite_create(vu=...)`. `None` (the unscheduled majority) omits the attribute on a create and leaves an existing one untouched on an update — **except** when the group resolved no boundary at all, where it is `REMOVE`d (`clear_vu`): that group covers every limit sharing the item, so no boundary means nothing on the item is scheduled, and leaving the `vu = 0` the fan-out stamps on every call would fail the fast-path guard forever rather than once; `vu` is only ever `SET`, never `REMOVE`d in the same expression (#488). Each boundary is computed by the acquire path at the **same clock reading that drove `effective_params` for that entry's refill**, not at `_commit_initial()`'s own later reading: the two are a round trip apart, and a boundary crossed in between must land `vu` at or before `rf` so the next acquire re-materialises — deriving it from the commit instant would skip the boundary just crossed and leave the fast path spending pre-boundary tokens for a whole window
- **A bucket created by the slow path carries its schedules** (`sched` / `rsched` / `sched_tz` / `b_{name}_sched` / `b_{name}_rsched`, design §2.2, §3.6), and its balance starts at the *scheduled* per-shard share rather than the base ceiling. The aggregator reads the item and nothing else, so an item with `vu` but no `sched` would be refilled toward the base ceiling — the schedule silently unenforced until the next admin fan-out. All scheduled limits on one item must agree on a timezone (`sched_tz` is one attribute per item), the same rule `set_limits()` enforces at config-write time
- **An unscheduled limit is recorded as unscheduled, not left to inherit (#541).** A bucket item carries one hoisted `sched` / `rsched` pair plus the per-limit `b_{name}_*` overrides, and absence of an override means "inherit the item default" — which is what keeps a shared schedule down to one attribute. It cannot *also* mean "this limit has none", so every unscheduled limit on an item that carries a default is stamped with the reserved marker `schema.BUCKET_SCHED_NONE` (`"-"`, not a legal compact encoding: it carries no version marker, so the decoders reject it before the tokeniser, #515). Without it the contamination ran **both** ways on a mixed item — a `0.5x` rate limit took the quota's midnight reset (its balance hard-`SET` at the edge, its drip skipped that pass) and the quota took the rate limit's `0.5x` window (half its allowance, silently) — and it reached the aggregator's refill and shard clone, the slow path's `BucketState`, and every fast-path `RateLimitExceeded` status through `Limit.per_shard`. One encoder (`Repository._encode_one_tuple`) serves both writers, mirrored in `zae_limiter_provisioner/bucket_sync.py`; the readers are `Repository._deserialize_composite_bucket`, `processor._parse_bucket_record` and `processor.propagate_shard_count`. The reserved `wcu` limit never carries either tuple and so is never marked
- **The slow path's `BucketState.sched` comes from the resolved config, not from the item.** `_deserialize_composite_bucket()` reads the base params only; `_do_acquire()` and `_try_parent_only_acquire()` attach `limit.schedule` to each state before admission, so `effective_params` applies to the refill, the ceiling and the `Limit.per_shard()` in any rejection. Config is also the fresher of the two — an item stamped before the last `set_limits()` still holds the old schedule

**Entity metadata cache (issue #318, GHSA-76rv):**
- `Repository._entity_cache` stores `{entity_id: (cascade, parent_id, shard_counts)}` where `shard_counts` is `dict[str, int]` (resource → shard_count)
- Populated from speculative result (ALL_NEW on success) or slow path (entity META record)
- `shard_counts` updated when shard doubling occurs (wcu exhaustion triggers `shard_count *= 2`)
- Shard selection: `random.randrange(shard_count)` using the cached `shard_count`, re-picked on every call (not derived from the entity id)
- On cache hit with `cascade=True`, `speculative_consume()` issues child + parent speculative writes concurrently via `asyncio.gather` (async) or `self._run_in_executor` (sync, strategy controlled by `parallel_mode`)
- **The parent shards independently of the child (#474):** its shard is drawn from the **parent's own** cached `shard_count` (`select_shard(parent_id, resource)`), never hardcoded to 0 — that left the one partition every cascading child writes to as an unmitigated hot partition. Compensation and the parent-only fallback reuse `parent_result.shard_id` (the shard whose image the decision was made on), but the slow path is handed a parent shard only when it *needs that shard*: `BUCKET_MISSING` (create it where the fast path looked) or the brand-new shard a `wcu` doubling added. An **exhausted** parent shard is never handed over — every shard holds its own share and ADR-134 re-picks on every call, so `_do_acquire` re-drawing can admit where the drawn shard could not
- **Parent `wcu` exhaustion doubles the parent's count** via `_shard_after_wcu_exhaustion` (shared with the child path), but **only after** the `would_refill_satisfy` gate: doubling on the way to a `RateLimitExceeded` never creates or reads the shard it picks, and one doubling per rejection walks a parent sitting at its limit to `MAX_SHARD_COUNT`, shrinking every shard's share permanently. The shard a doubling adds skips the parent-only attempt (it cannot exist yet) and goes straight to the full slow path. The warm parallel path and the cold-cache sequential path share this handling (`_handle_nested_parent_failure`)
- A *successful* parent write grows the parent's cached `shard_count` but deliberately passes no `meta`: a parent shard N>0 is usually created by a **child's** cascade slow path, which denormalizes only the acquiring entity's flags and so stamps the parent `cascade=False`/`parent_id=None`. Learning that as metadata would downgrade the parent's cache entry and silently stop a three-level hierarchy from debiting the grandparent. The consequence is that a parent with no cache entry learns its count lazily, from the first failure image (`_speculative_consume_single`), rather than on success
- Reduces cascade latency from 2 sequential round trips to 1 parallel round trip (same WCU cost)
- First acquire for an entity always uses sequential path (populates cache); subsequent acquires use parallel path
- **Sync parallel modes:** `"auto"` (default: gevent if patched, serial if single-CPU, threadpool otherwise), `"gevent"` (greenlets, warns if unpatched), `"threadpool"` (lazy ThreadPoolExecutor, warns on single-CPU), `"serial"` (sequential). Explicit modes warn on suboptimal conditions. Resolved once at `SyncRepository.__init__`

**Aggregator refill write pattern (issue #317, GHSA-76rv):**
- `try_refill_bucket()` targets `PK={ns}/BUCKET#{id}#{resource}#{shard}, SK=#STATE`
- Issues `UpdateItem` with `ADD b_{limit}_tk +refill_delta SET rf = :now` and condition `rf = :expected_rf`
- Uses effective per-shard limits: `capacity_milli // shard_count`, `refill_amount_milli // shard_count`
- Uses `ADD` for token deltas so it commutes with concurrent speculative writes (no read required)
- Optimistic lock on `rf` prevents double-refill with the client slow path or another aggregator invocation
- **`vu` is pinned in the same condition (#508).** The `rf` lock alone cannot see a `_sync_bucket_params` fan-out, which rewrites `cp`/`ra`/`rp`/`sched` on every shard and never touches `rf` — so a stream image captured before it passes the lock and refills toward the **old, larger** capacity. Since Task 13 the fan-out SETs `vu = 0` on every call, making `vu` the marker for "the operator changed something here": pinning it costs one condition term, covers every attribute that write touches (present and future), and forfeits none of the refill accrued since the last stamp the way bumping `rf` in the fan-out would. PR #506's `#sched` pin is kept, and still rides only with the `vu` re-stamp — it makes the *boundary* trustworthy, a different claim from "the item has not moved"
- On `ConditionalCheckFailedException`, the refill is silently skipped (another writer updated `rf` first)
- **Proactive sharding:** When `wcu` consumption >= 80% capacity in a batch, doubles `shard_count` on shard 0 via conditional write (`shard_count = :old`)
- **Shard propagation:** On `MODIFY` records showing `shard_count` change on shard 0, propagates new count to shards 1..N via conditional writes (`attribute_not_exists(shard_count) OR shard_count < :new`)

**DynamoDB writer table:**

| Writer | UpdateExpression | Condition | Touches `rf`? |
|--------|-----------------|-----------|---------------|
| Speculative consume | `ADD tk -consumed` | `attribute_exists(PK) AND tk >= consumed AND (attribute_not_exists(vu) OR vu > :now)` | No |
| Normal path (initial) | `SET rf = :new_rf (+ vu) ADD tk -consumed` (`REMOVE vu` when nothing on the item is scheduled) | `rf = :expected_rf` | Yes (optimistic lock) |
| Normal path (retry) | `ADD tk -consumed` | `tk >= consumed` | No (skips refill) |
| Client shard create (ADR-133) | `Put` full item, `tk = effective cp // shard_count` (a quota: what the reclaim below took, #587), `wcu` undivided, `sched`/`rsched`/`sched_tz`/`vu` when scheduled | `attribute_not_exists(PK)` | Sets `rf = now` |
| Quota surplus reclaim, per existing shard (#587) | `SET tk = :share`, `ReturnValues=UPDATED_OLD` | `tk > :share` (client) / `attribute_exists(PK) AND tk > :share` (aggregator) | No |
| Adjustment / rollback | `ADD tk +/-delta` | (unconditional) | No |
| Aggregator refill | `ADD tk +refill SET rf = :now` | `rf = :expected_rf AND vu = :expected_vu` (#508) | Yes (optimistic lock) |
| Aggregator proactive shard | `SET shard_count = :new` | `shard_count = :old` | No |
| Aggregator shard propagation | `SET shard_count = :new` | `attribute_not_exists(shard_count) OR shard_count < :new` | No |
| Client shard propagation (#439) | `SET shard_count = :new` | `shard_count < :new` | No |
| Limit-change sync, per shard (#468), per resource under `_default_` (#487) | `SET cp/ra/rp, sched/rsched/sched_tz, per-limit sched/rsched (compact, or "-" for unscheduled), vu = 0 (+ ttl) REMOVE stale, per-limit overrides that now match the item default` | `attribute_exists(PK)` | No |
| Disable stamp (ADR-125) | `SET disabled = :true` / `REMOVE disabled` | `attribute_exists(PK)` | No |

**Hot partition risk with cascade (issue #116):** See [Hot Partition Risk Mitigation](#hot-partition-risk-mitigation-issue-116) above.

**Key builders for bucket records (v0.9.0+, GHSA-76rv):**
- `pk_bucket(namespace_id, entity_id, resource, shard_id)` - Returns `{ns}/BUCKET#{id}#{resource}#{shard}`
- `sk_state()` - Returns `#STATE`
- `parse_bucket_pk(pk)` - Returns `(namespace_id, entity_id, resource, shard_id)`
- `gsi3_pk_entity(namespace_id, entity_id)` - Returns `{ns}/ENTITY#{entity_id}` (bucket discovery)
- `gsi3_sk_bucket(resource, shard_id)` - Returns `BUCKET#{resource}#{shard}`

**Key builders for config records:**
- `pk_system(namespace_id)` - Returns `{ns}/SYSTEM#`
- `pk_resource(namespace_id, resource)` - Returns `{ns}/RESOURCE#{resource}`
- `pk_entity(namespace_id, entity_id)` - Returns `{ns}/ENTITY#{entity_id}`
- `sk_config()` - Returns `#CONFIG` (for system/resource level)
- `sk_config(resource)` - Returns `#CONFIG#{resource}` (for entity level)
- `sk_entity_config_resources()` - Returns `#ENTITY_CONFIG_RESOURCES` (registry with ref counts)
- `sk_namespace(name)` - Returns `#NAMESPACE#{name}` (forward lookup)
- `sk_nsid(id)` - Returns `#NSID#{id}` (reverse lookup)
- `sk_provisioner()` - Returns `#PROVISIONER` (declarative limits managed state)

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

**`-l` flag format:** `name:rate[/period][:burst]` where `period` defaults to `/min`. Supported periods: `/sec`, `/min`, `/hour`, `/day`.

**`-l` cannot express a schedule, and a set is a full replace.** `cli._parse_limit()` builds a `Limit` with `schedule=()` / `reset_schedule=()`, and every config level is written with a full-replace `PutItem`, so `entity set-limits user-123 -r gpt-4 -l rpm:1000` against a level whose stored `rpm` is scheduled silently drops the schedule — and turns a stored quota into a dripping limit. Nothing in the output signals it: `_echo_limit` renders the *new* limit. Deliberate per #222 §1.5 (the flag was not grown a cron mini-syntax; `limits apply` is the CLI path), but the erasure is the sharp edge, not the absence.

```bash
# Equivalent: 1000 per minute
-l rpm:1000
-l rpm:1000/min

# Other periods
-l rps:10/sec
-l rph:5000/hour
-l rpd:100000/day

# With burst
-l rpm:1000:1500
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
- `sched` (string, #222): the limit's schedule in the compact storage encoding (`schedule.encode()`), written only when that limit has one. `cp`/`ra`/`rp` stay the **base** parameters; the schedule is applied on top of them at read time, never materialised onto the item.
- `rsched` (string, #222 §3.6): the limit's **reset** schedule in the compact storage encoding (`schedule.encode_reset()`), written only when that limit has one. Same grammar as `sched` minus the modifier tokens, because a reset overrides no parameters — `0 0 * * *` is `m0h0`, four bytes. A separate attribute rather than a tag inside `sched`, mirroring the separate tuple on `Limit` and keeping the decoder from partitioning one list into two meanings (§4.1). `decode_reset` **rejects** a modifier token found here rather than ignoring it: it means corruption, or a parameter schedule stored under the wrong key, and an entry that silently reset a balance on a schedule meant only to scale it is the worst available reading. Without this attribute a quota (`refill_amount = 0`, ADR-137) does not merely lose its reset — it fails to reconstruct at all, and the read raises (#538).

**Config fields:**
- `config_version` (int): Atomic counter for cache invalidation
- `on_unavailable` (string): "allow" or "block" (system level only)
- `sched_tz` (string, #222): the IANA timezone shared by every schedule on the item, hoisted out of the individual entries — **one attribute per item, not per limit** (§4.1), which is why all scheduled limits written to one config item must agree on a zone (`models.hoisted_schedule_timezone()` raises otherwise). Absent when nothing on the item is scheduled; a `sched` with no `sched_tz` decodes as UTC. It covers **both** tuples: `hoisted_schedule_timezone()` and `Limit.__post_init__` both vote over `schedule` *and* `reset_schedule`, because a quota carries no parameter schedule unless one is chained on — a `schedule[0].tz`-only vote would leave `sched_tz` unwritten and decode a New York daily quota as UTC, resetting at 19:00 local forever with no error anywhere.

Config items are written with full-replace `PutItem` at every level, so storage is override-not-merge: a limit re-written without a schedule loses the stored one, no explicit REMOVE needed.

**Caching:** 60s TTL in-memory cache per Repository instance (configurable via `config_cache_ttl` parameter on Repository constructor, 0 to disable). Use `repo.invalidate_config_cache()` for immediate refresh. Use `repo.get_cache_stats()` for monitoring. `set_limits()` and `delete_limits()` auto-evict relevant cache entries. Negative caching for entities without custom config. Config resolution is handled by `repo.resolve_limits()` (ADR-122).

**Cost impact:** 1.5 RCU per cache miss (one GetItem per level, reduced from 2 RCU with per-limit items). With caching, `acquire()` costs 1-2 RCU per request regardless of limit count (O(1) via composite items, ADR-114/115).

### Disabling Resources and Entities (ADR-125)

`disabled` is a **tri-state** flag stored beside `limits` on resource and entity config items:
`true`, `false`, or absent meaning "inherit from the level above". It resolves by an
**independent** walk over entity(resource) → entity(`_default_`) → resource, where the first
level with an explicit value wins, regardless of which level supplies the limits. That
independence is what lets an entity-level `disabled: false` re-admit one entity to a resource
disabled for everyone else. **System-level disable is not supported** (ADR-125 scopes it out).

Disabling is **eager**: the call writes config, then stamps every existing bucket item with a
`disabled` attribute. This matters because `acquire()`'s default fast path
(`speculative_writes=True`) is a conditional `UpdateItem` that never reads config —
enforcement is `attribute_not_exists(disabled)` in that condition, alongside the pre-existing
TTL guard. `acquire()` raises `ResourceDisabled`, a direct `ZAELimiterError` subclass and
deliberately **not** a `RateLimitError`, carrying no retry hint: map it to 403, not 429.
`ResourceDisabled` always propagates regardless of `on_unavailable` mode, same as
`RateLimitExceeded` and `ValidationError`.

`Repository.resolve_disabled(entity_id, resource) -> tuple[bool, str | None]` performs the
walk directly and is deliberately **uncached**: it is called only on the slow path and by the
eager fan-out, never on the speculative fast path.

**API (each returns an `int` count of bucket items written), on `Repository` (not `RateLimiter`):**

| Level | Disable / Enable | Clear |
|-------|------------------|-------|
| Resource | `disable_resource(resource)` / `enable_resource(resource)` | `clear_resource_disabled(resource)` |
| Entity | `disable_entity(entity_id, resource=...)` / `enable_entity(entity_id, resource=...)` | `clear_entity_disabled(entity_id, resource=...)` |

`resource=None` on the entity-level methods means "all resources for that entity" (targets the
entity's `_default_` config). `set_resource_defaults()` and `set_limits()` also accept a
`disabled` keyword; passing it **explicitly** (not the default "preserve stored value"
sentinel) fans out immediately, exactly like `disable_resource()`/`disable_entity()` — a setter
call is not just a config write. `delete_limits()` and `delete_resource_defaults()` delete the
config item that was holding `disabled`, which can change what `resolve_disabled()` returns in
either direction, so both re-run the fan-out against the newly resolved value rather than
leaving the deleted level's stamp in place.

Cascade is out of scope for entity-level `disabled`: an entity-level `disabled: false` carve-out
on a cascading **child** does not extend to its **parent**. `acquire()` checks the child's own
bucket stamp only; if the parent's bucket is separately stamped disabled, the cascade still
raises `ResourceDisabled` with `entity_id` set to the **parent**, not the child.

**CLI:**

```bash
zae-limiter resource disable|enable|clear-disabled RESOURCE_NAME
zae-limiter entity disable|enable|clear-disabled ENTITY_ID [--resource R]
```

`resource get-defaults` and `entity get-limits` show `Status: DISABLED` or
`Status: enabled (explicit override)` when the level has an explicit value.

**Declarative limits (Issue #405):** `disabled` is supported on `resources.<name>` and
`entities.<id>.resources.<name>` in the YAML manifest (not on `system`), and carried through
the CloudFormation `Custom::ZaeLimiterLimits` round trip in both directions via a `Disabled`
property. The Lambda-side provisioner fan-out (`src/zae_limiter_provisioner/fanout.py`) mirrors
the async `Repository` fan-out and consults per-entity overrides the same way, so a manifest
apply that merely re-asserts an unchanged resource-level `disabled` (as every apply does —
`differ.py` emits a change for every manifest resource regardless of whether anything changed)
never clobbers a carve-out made out of band directly against the table.

**Known limitations:**
- Disabling is O(buckets for the resource) writes, not O(1).
- A narrow race exists between the config write and the fan-out query: an in-flight
  `acquire()` can create a bucket the fan-out's GSI query misses. Mitigated by a two-pass
  discovery pass, not eliminated.

See [ADR-125](docs/adr/125-resource-disable.md) for the full design and alternatives considered.

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

### Bucket TTL for Default Limits (Issue #271, #296, ADR-136)

Buckets using system/resource default limits have TTL for auto-expiration:

| Config Source (`ConfigSource`) | TTL Behavior |
|--------------------------------|--------------|
| Entity limits, resource-specific (`entity`) | No TTL (persist indefinitely) |
| Entity limits, entity-wide `_default_` (`entity_default`) | No TTL (persist indefinitely) |
| Resource defaults (`resource`) | TTL = now + max_recovery × multiplier |
| System defaults (`system`) | TTL = now + max_recovery × multiplier |
| Override parameter | TTL = now + max_recovery × multiplier |

**[ADR-136](docs/adr/136-entity-config-bucket-ttl.md) (supersedes ADR-119):** entity configuration is custom at **either** entity level, so an entity-wide `_default_` bucket persists like a resource-specific one. The test lives in `limiter.py`'s `_is_custom_config()` — a single helper, because the three call sites that consume it (`_try_parent_only_acquire`, `_do_acquire`'s per-entity entries, and the `_wcu_carrier` argument) had drifted to a two-way `== "entity"` test that silently excluded `entity_default` (#489). TTL is also the **propagation mechanism** for resource/system buckets, which do not fan out on change, so widening this test any further would stop them picking up new parameters.

**Recovery horizon, per limit shape (#532).** `max_recovery` is a single `max` across every limit on the composite item, and the horizon each one contributes depends on how it recovers:

| Limit shape | Recovery horizon |
|-------------|------------------|
| Drips (`refill_amount > 0`) | `time_to_fill = (capacity / refill_amount) × refill_period_seconds`, at its **slowest** over the base parameters and every `schedule` window (#557) |
| Quota (`Limit.is_quota`, ADR-137) | the **reset period** — the cycle over which its `reset_schedule` cron repeats |

The dripping formula is unchanged, and still ensures slow-refill limits (where `capacity >> refill_amount`) have time to fully refill before expiring. A quota needs its own horizon because ADR-137 fixes `refill_amount = 0` for every one of them, and time-to-fill divides by exactly that field — the `ZeroDivisionError` of #532. "No TTL for a quota" is not available as an answer: ADR-136 makes TTL the **propagation mechanism** for resource- and system-level limits, so a quota with no TTL would enforce its original allowance forever.

**A dripping limit is priced in every window, not just at its base (#557).** An *absolute* `ScheduleEntry` — one overriding `capacity`, `refill_amount` or `refill_period_seconds` rather than scaling — changes time-to-fill inside its window, so `_recovery_seconds` takes the `max` over the base parameters **and** each entry's `schedule.entry_params()`. `per_minute("rpm", 60).with_schedule((ScheduleEntry(cron="0 0-6 * * *", refill_amount=1),))` needs 3600 s overnight against the base's 60 s; reading the base alone gave it a 420 s TTL, so a bucket idle seven minutes overnight was swept while still in debt and recreated at full capacity — the over-admission ADR-136 exempts custom-configured buckets from TTL to avoid. `scale` entries come out unchanged by construction (§1.1 moves capacity and refill together). The base is always in the set: it applies outside every window, and including it rounds up. Worst case rather than the current window because the function holds no clock and cannot know which window the expiry lands in. A **quota** never reaches the walk — `is_quota` is structural, so the reset period governs however its parameter schedule moves the ceiling, and the walk never divides by the zero rate ADR-137 gives it (that would be #532 again).

The reset *period* rather than the wait to the next edge, because `schema.calculate_bucket_ttl_seconds(limits, multiplier)` holds no clock and none of its three production callers (`lease._commit_initial`, `Repository._sync_bucket_params`, `zae_limiter_provisioner.bucket_sync`) has one to pass — the period bounds that wait from above at every instant. The period is read off the **coarsest** cron field the reset pattern constrains (`schedule.cycle_seconds`, which `schema._reset_cycle_seconds` delegates to; the opposite end from `schedule._granularity`, which picks a scan step from the finest), and every approximation rounds **up**: 31 days for a monthly pattern, 366 for an annual one, the tightest cycle where several reset entries share a limit. The ladder lives in `schedule.py` since #574, where a second caller — `_reset_scan`, sizing a reset edge's *scan* horizon — needs the identical answer; keeping two copies is exactly what #574 was. Too long only delays propagation; too short expires a bucket still carrying debt, and a bucket recreated in debt comes back at full capacity — for a quota, an unscheduled reset.

A limit that neither drips nor resets is unconstructible (`Limit.__post_init__`, ADR-137); `schema._recovery_seconds` raises a `ValueError` naming the limit rather than dividing, so a future validation bypass surfaces as that sentence and not as #532 again.

`schema.py` therefore imports `schedule.py` (for `parse_cron`). Both Lambda stubs already vendor `schedule.py` and both install `cronsim`, so the import closure is unchanged.

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
- `Limit.quota("rpd", 10000, cron="0 0 * * *")`: reset period = 86400s, TTL = 86400×7 = 604800s (7 days)
- `Limit.quota("rpmo", 10000, cron="0 0 1 * *")`: reset period = 31 days, TTL = 217 days
- Mixed item, slow refill (42000s) beside an hourly quota (3600×7 = 25200s): TTL = 42000s — the `max` spans both shapes

**TTL behavior on upgrade/downgrade:**
- Entity with custom limits → TTL removed on next acquire
- Entity downgrades to defaults → TTL set on next acquire

## Dependencies

**Required:**
- `aioboto3`: Async DynamoDB client
- `aws-lambda-builders`: Cross-platform Lambda packaging (see ADR-113)
- `boto3`: Sync DynamoDB (for Lambda aggregator)
- `cronsim`: Cron parsing for scheduled limits (#222). Parsing only — matching, boundary scanning and the compact encoding are ours (`schedule.py`), because no cron library computes when a *window* closes
- `pip`: Required by `aws-lambda-builders` for dependency resolution
- `questionary`: Interactive prompts for CLI workflows
- `tzdata`: IANA timezone database for `zoneinfo`. A runtime dependency, not a platform assumption — the Lambda runtime image is not guaranteed to ship `/usr/share/zoneinfo`

**Optional extras:**
- `[plot]`: `asciichartpy` for ASCII chart visualization of usage snapshots
- `[dev]`: Testing and development tools (pytest, moto, ruff, mypy, pre-commit, types-gevent) plus `croniter`, the **test-only** oracle the cron matcher is pinned against (#222 §3.1) — never a runtime dependency
- `[docs]`: MkDocs documentation generation
- `[cdk]`: AWS CDK constructs
- `[lambda]`: Lambda Powertools (aws-lambda-powertools), plus `cronsim` and `tzdata` — both Lambda packages vendor `schedule.py` and evaluate schedules from the item
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
