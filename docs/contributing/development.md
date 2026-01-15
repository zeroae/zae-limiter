# Development Setup

This guide covers setting up a local development environment for zae-limiter.

## Prerequisites

- Python 3.11 or 3.12
- [uv](https://docs.astral.sh/uv/) (recommended) or conda
- Docker (for LocalStack integration tests)

## Setup

### Using uv (Recommended)

```bash
git clone --recurse-submodules https://github.com/zeroae/zae-limiter.git
cd zae-limiter
uv sync --all-extras
```

### Using conda

```bash
git clone --recurse-submodules https://github.com/zeroae/zae-limiter.git
cd zae-limiter
conda create -n zae-limiter python=3.12
conda activate zae-limiter
pip install -e ".[dev]"
```

!!! tip "Already cloned?"
    If you cloned without `--recurse-submodules`, run:
    ```bash
    git submodule update --init --recursive
    ```

## Running Tests

```bash
# Run all unit tests
pytest tests/unit/ -v

# Run with coverage
pytest --cov=zae_limiter --cov-report=html

# Run specific test file
pytest tests/unit/test_limiter.py -v
```

For integration tests with LocalStack, see the [Testing Guide](testing.md).

## Code Quality

```bash
# Format code
ruff format .

# Lint and auto-fix
ruff check --fix .

# Type checking
mypy src/zae_limiter
```

## Commit Messages

Follow the ZeroAE [commit conventions](https://github.com/zeroae/.claude/blob/main/commits.md):

```bash
# Examples
feat(limiter): add hierarchical rate limiting support
fix(bucket): prevent integer overflow in refill calculation
docs(readme): add CloudFormation deployment guide
refactor(schema): simplify DynamoDB key structure
```

**Project scopes:** `limiter`, `bucket`, `cli`, `infra`, `ci`, `aggregator`, `models`, `schema`, `repository`, `lease`, `exceptions`, `test`, `benchmark`

## Next Steps

- [LocalStack](localstack.md) - Local AWS development
- [Testing](testing.md) - Test organization and fixtures
- [Architecture](architecture.md) - DynamoDB schema and design decisions
