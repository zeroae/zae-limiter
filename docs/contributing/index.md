# Contributing

Thank you for your interest in contributing to zae-limiter!

## Quick Start

```bash
git clone https://github.com/zeroae/zae-limiter.git
cd zae-limiter
uv sync --all-extras
uv tool install hatch  # Required for sync code generation
pytest
```

## Development Guides

| Guide | Description |
|-------|-------------|
| [Development Setup](development.md) | Environment setup, running tests, code quality |
| [LocalStack](localstack.md) | Local AWS development environment |
| [Testing](testing.md) | Test organization, pytest fixtures, CI |
| [Architecture](architecture.md) | DynamoDB schema, token bucket algorithm |

## Detailed Reference

For comprehensive development instructions including:

- Build commands and linting
- Commit message conventions
- Code review guidelines
- Release process

See [CLAUDE.md](https://github.com/zeroae/zae-limiter/blob/main/CLAUDE.md) in the repository root.

## Pull Request Process

1. Create a feature branch from `main`
2. Make changes following project conventions
3. Ensure CI passes (lint, type check, tests)
4. Submit PR for review

All changes must go through pull requests. Direct commits to `main` are not allowed.
