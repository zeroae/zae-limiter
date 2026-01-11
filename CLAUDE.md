# CLAUDE.md - Instructions for AI Assistants

This file provides context for AI assistants working on the zae-limiter codebase.

## Project Overview

zae-limiter is a rate limiting library backed by DynamoDB using the token bucket algorithm. It's designed for limiting LLM API calls where:
- Multiple limits are tracked per call (rpm, tpm)
- Token counts are unknown until after the call completes
- Hierarchical limits exist (API key → project)

## Build & Development

### Using uv (preferred)

```bash
# Setup (one-time)
uv sync --all-extras

# Deploy infrastructure (CloudFormation)
uv run zae-limiter deploy --table-name rate_limits --region us-east-1

# Run tests
uv run pytest

# Type check
uv run mypy src/zae_limiter

# Lint
uv run ruff check --fix .
uv run ruff format .
```

### Using conda

```bash
conda create -n zae-limiter python=3.12
conda activate zae-limiter
pip install -e ".[dev]"
pytest
```

## Development Workflow

### Pull Request Process

All changes to the codebase must go through pull requests. Direct commits to the `main` branch are not allowed.

**Workflow:**

1. Create a feature branch from main:
   ```bash
   git checkout main
   git pull origin main
   git checkout -b feat/your-feature-name
   ```

2. Make your changes following the project conventions
   - Follow commit message conventions (see Commit Messages section below)
   - Add tests for new functionality
   - Update documentation as needed

3. Push your branch and create a pull request:
   ```bash
   git push origin feat/your-feature-name
   ```

4. Wait for CI checks to pass:
   - **Lint**: Code style and formatting (ruff)
   - **Type Check**: Static type checking (mypy)
   - **Tests**: Unit tests with coverage (pytest on Python 3.11 & 3.12)

5. Address review feedback if needed

6. Once approved and CI passes, the PR will be merged to main

**Important:** Never force-push to main or bypass CI checks.

## Infrastructure Deployment

### CloudFormation Stack

The library uses CloudFormation for infrastructure deployment. The `deploy` command automatically:
1. Creates CloudFormation stack with DynamoDB table, streams, and Lambda function
2. Packages and deploys the Lambda aggregator code from the installed package

```bash
# Deploy stack with CLI (includes Lambda deployment)
zae-limiter deploy --table-name rate_limits --region us-east-1

# Deploy to LocalStack (for local development)
zae-limiter deploy --table-name rate_limits --endpoint-url http://localhost:4566 --region us-east-1

# Deploy without aggregator Lambda
zae-limiter deploy --table-name rate_limits --no-aggregator

# Export template for custom deployment
zae-limiter cfn-template > template.yaml

# Export Lambda package for custom deployment
zae-limiter lambda-export --output lambda.zip

# Show Lambda package info without building
zae-limiter lambda-export --info

# Check stack status
zae-limiter status --stack-name zae-limiter-rate_limits --region us-east-1

# Delete stack
zae-limiter delete --stack-name zae-limiter-rate_limits --yes
```

**Lambda Deployment Details:**
- The CLI automatically builds a deployment package from the installed `zae_limiter` package
- Lambda code is updated via AWS Lambda API after stack creation
- No S3 bucket required - deployment package (~30KB) is uploaded directly
- Lambda only depends on `boto3` (provided by AWS Lambda runtime)

### Auto-Creation in Code

For development/testing, stacks can be auto-created:

```python
limiter = RateLimiter(
    table_name="rate_limits",
    region="us-east-1",
    create_stack=True,  # Auto-creates CloudFormation stack
    stack_parameters={
        'snapshot_windows': 'hourly,daily',
        'retention_days': '90',
    }
)
```

**Note:** `create_table` parameter is deprecated. Use `create_stack` instead.

### Local Development

**LocalStack (Recommended):** For full feature testing including Lambda aggregator and DynamoDB Streams:

```bash
# Deploy to LocalStack using CLI
zae-limiter deploy --table-name rate_limits --endpoint-url http://localhost:4566 --region us-east-1
```

```python
# Or from code
limiter = RateLimiter(
    table_name="rate_limits",
    endpoint_url="http://localhost:4566",  # LocalStack
    region="us-east-1",
    create_stack=True,  # Uses CloudFormation (LocalStack supports it)
)
```

**DynamoDB Local:** For basic rate limiting only (no Lambda/Streams support):

```python
limiter = RateLimiter(
    table_name="rate_limits",
    endpoint_url="http://localhost:8000",  # DynamoDB Local
    create_table=True,  # Uses direct table creation (bypasses CloudFormation)
)
```

**Note:** The `--endpoint-url` CLI flag assumes CloudFormation support (LocalStack). For DynamoDB Local, use `create_table=True` in code instead of the CLI deploy command.

## Project Structure

```
src/zae_limiter/
├── __init__.py        # Public API exports
├── models.py          # Limit, Entity, LimitStatus, BucketState
├── exceptions.py      # RateLimitExceeded, RateLimiterUnavailable, StackCreationError, VersionError
├── bucket.py          # Token bucket math (integer arithmetic)
├── schema.py          # DynamoDB key builders
├── repository.py      # DynamoDB operations
├── lease.py           # Lease context manager
├── limiter.py         # RateLimiter, SyncRateLimiter
├── cli.py             # CLI commands (deploy, delete, status, cfn-template, version, upgrade, check)
├── version.py         # Version tracking and compatibility
├── migrations/        # Schema migration framework
│   ├── __init__.py    # Migration registry and runner
│   └── v1_0_0.py      # Initial schema baseline
├── aggregator/        # Lambda for usage snapshots
│   ├── handler.py     # Lambda entry point
│   └── processor.py   # Stream processing logic
└── infra/
    ├── stack_manager.py    # CloudFormation stack operations
    ├── lambda_builder.py   # Lambda deployment package builder
    └── cfn_template.yaml   # CloudFormation template
```

## Key Design Decisions

### Integer Arithmetic for Precision
- All token values stored as **millitokens** (×1000)
- Refill rates stored as fraction: `refill_amount / refill_period_seconds`
- Avoids floating point precision issues in distributed systems

### Token Bucket Algorithm
- Buckets can go **negative** for post-hoc reconciliation
- Refill is calculated lazily on each access
- `burst >= capacity` allows controlled bursting

### DynamoDB Single Table Design
- All entities, buckets, limits, usage in one table
- GSI1: Parent → Children lookups
- GSI2: Resource aggregation (capacity tracking)
- Uses TransactWriteItems for atomicity

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

## Testing

### Run Tests with Moto (DynamoDB Mock)
```bash
pytest tests/ -v
```

### Test with LocalStack (Recommended)
```bash
# Start LocalStack (includes DynamoDB, Lambda, CloudFormation)
docker run -d -p 4566:4566 localstack/localstack

# Deploy infrastructure and run integration tests
zae-limiter deploy --table-name test_limits --endpoint-url http://localhost:4566 --region us-east-1
DYNAMODB_ENDPOINT=http://localhost:4566 pytest tests/integration/ -v
```

### Test with DynamoDB Local (Basic Only)
```bash
# Start DynamoDB Local (no Lambda/Streams support)
docker run -p 8000:8000 amazon/dynamodb-local

# Run integration tests (aggregator features won't work)
DYNAMODB_ENDPOINT=http://localhost:8000 pytest tests/integration/ -v
```

### Test Coverage
```bash
pytest --cov=zae_limiter --cov-report=html
open htmlcov/index.html
```

## Code Style

- Use `ruff` for linting and formatting
- Use `mypy` for type checking (strict mode)
- All public APIs must have docstrings
- Async is primary, sync is wrapper

## Code Review Guidelines

When reviewing PRs, check the following based on files changed:

### Test Coverage (changes to src/)
- Verify corresponding tests exist in tests/
- Check edge cases: negative values, empty collections, None
- Ensure async tests have sync counterparts
- Flag if new public methods lack tests

### Async/Sync Parity (changes to limiter.py, lease.py, repository.py)
- Verify SyncRateLimiter has matching sync methods
- Check SyncLease matches AsyncLease functionality
- Ensure error handling is consistent
- Confirm both test files updated

### Infrastructure (changes to infra/, aggregator/)
- Validate CloudFormation template syntax
- Check IAM follows least privilege
- Verify Lambda handler signature
- **Prefer fixes that preserve existing schema** (see Schema Preservation below)
- Only update version.py if schema change is unavoidable

### Schema Preservation (DynamoDB changes)
When fixing DynamoDB-related bugs, prefer solutions that preserve the existing schema:
- Use `if_not_exists()` to initialize nested maps instead of flattening structure
- Use conditional expressions to handle missing attributes
- Avoid changing attribute names or moving data between top-level and nested paths
- Schema changes require version bumps, migrations, and careful rollout planning
- Only change schema when there's no viable alternative

### Migrations (changes to migrations/)
- Verify migration follows protocol (async, Repository param)
- Check backward compatibility
- Validate CURRENT_VERSION in version.py matches
- Flag breaking changes needing major version bump

### DynamoDB Schema (changes to schema.py, repository.py)
- Verify key builders follow single-table patterns
- Check GSI usage matches access patterns
- Validate transaction limits (max 100 items)
- Ensure patterns documented in CLAUDE.md

### API Documentation (changes to __init__.py, models.py)
- Verify docstrings exist and are accurate
- Check type hints match descriptions
- Flag public API changes without changelog entry

## Commit Messages

Follow the ZeroAE [commit conventions](https://github.com/zeroae/.github/blob/main/docs/commits.md).

**Project scopes:** `limiter`, `bucket`, `cli`, `infra`, `aggregator`, `models`, `schema`, `repository`, `lease`, `exceptions`

**Examples:**
```bash
✨ feat(limiter): add hierarchical rate limiting support
🐛 fix(bucket): prevent integer overflow in refill calculation
📝 docs(readme): add CloudFormation deployment guide
♻️ refactor(schema): simplify DynamoDB key structure
```

## Important Invariants

1. **Lease commits only on success**: If any exception occurs in the context, changes are rolled back
2. **Bucket can go negative**: `lease.adjust()` never throws, allows debt
3. **Cascade is optional**: Parent is only checked if `cascade=True`
4. **Stored limits override defaults**: When `use_stored_limits=True`
5. **Transactions are atomic**: Multi-entity updates succeed or fail together

## DynamoDB Access Patterns

| Pattern | Query |
|---------|-------|
| Get entity | `PK=ENTITY#{id}, SK=#META` |
| Get buckets | `PK=ENTITY#{id}, SK begins_with #BUCKET#` |
| Get children | GSI1: `GSI1PK=PARENT#{id}` |
| Resource capacity | GSI2: `GSI2PK=RESOURCE#{name}, SK begins_with BUCKET#` |
| Get version | `PK=SYSTEM#, SK=#VERSION` |

## Dependencies

- `aioboto3`: Async DynamoDB client
- `boto3`: Sync DynamoDB (for Lambda aggregator)
- `moto`: DynamoDB mocking for tests

## Releasing

Releases are fully automated via GitHub Actions. No manual build or publish steps are required.

### Release Process

1. **Ensure main is ready**: All PRs merged, CI passing, CHANGELOG expectations met

2. **Create and push a version tag**:
   ```bash
   git checkout main
   git pull origin main
   git tag v0.1.0
   git push origin v0.1.0
   ```

3. **GitHub Actions automatically**:
   - Builds the package (wheel + sdist)
   - Generates changelog using git-cliff from conventional commits
   - Creates GitHub Release with changelog and distribution files
   - Publishes to PyPI using OIDC authentication

4. **Verify the release**:
   - Check GitHub Releases page for the new release
   - Verify PyPI: https://pypi.org/project/zae-limiter/
   - Confirm changelog accuracy

### Version Management

- Versions are **automatically generated** from git tags using `hatch-vcs`
- No manual version updates needed in `pyproject.toml` or `__init__.py`
- Tag format: `v{major}.{minor}.{patch}` (e.g., `v0.1.0`, `v1.2.3`)

### Changelog Generation

- Uses `git-cliff` with configuration in `cliff.toml`
- Parses conventional commits since the last tag
- Groups by: Features, Bug Fixes, Documentation, Performance, Refactoring, etc.
- Removes emoji prefixes automatically (e.g., `✨ feat(scope):` → `feat(scope):`)

### Release Workflow Details

See `.github/workflows/release.yml` for the complete automation:
- **Build job**: Creates distribution packages
- **Publish job**: Uploads to PyPI (requires `pypi` environment approval if configured)
- **Release job**: Creates GitHub release with generated changelog

**Note:** The PyPI publish step uses OpenID Connect (OIDC) for authentication, eliminating the need for API tokens in secrets.
