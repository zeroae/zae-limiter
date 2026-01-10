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
# Setup
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"

# Deploy infrastructure (CloudFormation)
zae-limiter deploy --table-name rate_limits --region us-east-1

# Run tests
pytest

# Type check
mypy src/zae_limiter

# Lint
ruff check --fix .
ruff format .
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

# Deploy without aggregator Lambda
zae-limiter deploy --table-name rate_limits --no-aggregator

# Export template for custom deployment
zae-limiter cfn-template > template.yaml

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

When using DynamoDB Local, CloudFormation is automatically skipped:

```python
limiter = RateLimiter(
    table_name="rate_limits",
    endpoint_url="http://localhost:8000",  # DynamoDB Local
    create_table=True,  # Uses direct table creation (not CloudFormation)
)
```

## Project Structure

```
src/zae_limiter/
├── __init__.py        # Public API exports
├── models.py          # Limit, Entity, LimitStatus, BucketState
├── exceptions.py      # RateLimitExceeded, RateLimiterUnavailable, StackCreationError
├── bucket.py          # Token bucket math (integer arithmetic)
├── schema.py          # DynamoDB key builders
├── repository.py      # DynamoDB operations
├── lease.py           # Lease context manager
├── limiter.py         # RateLimiter, SyncRateLimiter
├── cli.py             # CLI commands (deploy, delete, status, cfn-template)
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

### Test with Local DynamoDB
```bash
# Start DynamoDB Local
docker run -p 8000:8000 amazon/dynamodb-local

# Run integration tests
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
