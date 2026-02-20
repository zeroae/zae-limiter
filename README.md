<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/zeroae/zae-limiter/main/docs/assets/zae-limiter-dark-bg.svg">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/zeroae/zae-limiter/main/docs/assets/zae-limiter-white-bg.svg">
    <img alt="zae-limiter" src="https://raw.githubusercontent.com/zeroae/zae-limiter/main/docs/assets/zae-limiter-white-bg.svg" width="50%">
  </picture>
</p>

[![PyPI version](https://img.shields.io/pypi/v/zae-limiter?style=flat-square)](https://pypi.org/project/zae-limiter/)
[![Conda version](https://img.shields.io/conda/v/conda-forge/zae-limiter?style=flat-square)](https://anaconda.org/conda-forge/zae-limiter)
[![Python versions](https://img.shields.io/pypi/pyversions/zae-limiter?style=flat-square)](https://pypi.org/project/zae-limiter/)
[![License](https://img.shields.io/pypi/l/zae-limiter?style=flat-square)](https://github.com/zeroae/zae-limiter/blob/main/LICENSE)
[![Lint](https://img.shields.io/github/actions/workflow/status/zeroae/zae-limiter/ci-lint.yml?branch=main&style=flat-square&label=lint)](https://github.com/zeroae/zae-limiter/actions/workflows/ci-lint.yml)
[![Tests](https://img.shields.io/github/actions/workflow/status/zeroae/zae-limiter/ci-tests.yml?branch=main&style=flat-square&label=tests)](https://github.com/zeroae/zae-limiter/actions/workflows/ci-tests.yml)
[![codecov](https://img.shields.io/codecov/c/github/zeroae/zae-limiter?style=flat-square)](https://codecov.io/gh/zeroae/zae-limiter)
[![Docs](https://img.shields.io/badge/docs-latest-blue.svg?style=flat-square)](https://zeroae.github.io/zae-limiter/)

A rate limiting library backed by DynamoDB using the token bucket algorithm.

## Installation

```bash
pip install zae-limiter
# or
conda install -c conda-forge zae-limiter
```

## Usage

```python
from zae_limiter import Repository, RateLimiter, SyncRepository, SyncRateLimiter, Limit

# Auto-provisions infrastructure if needed
repo = await Repository.open()
limiter = RateLimiter(repository=repo)

# Sync
sync_repo = SyncRepository.open()
sync_limiter = SyncRateLimiter(repository=sync_repo)

# Define default limits (can be overridden per-entity)
default_limits = [
    Limit.per_minute("rpm", 100),
    # Token bucket with burst capacity
    Limit.per_minute("tpm", 10_000, burst=50_000),
]

async with limiter.acquire(
    entity_id="api-key-123",
    resource="gpt-4",
    limits=default_limits,  # Multiple limits in a single atomic transaction
    consume={"rpm": 1, "tpm": 500},  # Estimate tokens upfront
) as lease:
    response = await call_llm()
    # Reconcile actual usage (can go negative for post-hoc adjustment)
    await lease.adjust(tpm=response.usage.total_tokens - 500)
    # Tokens written to DynamoDB on enter | Rolled back on exception

# Hierarchical entities: create project with stored limits, then API key under it
await limiter.create_entity(entity_id="proj-1", name="Production")
await limiter.set_limits("proj-1", [Limit.per_minute("tpm", 100_000)])  # Project-level
await limiter.create_entity(entity_id="api-key-456", parent_id="proj-1", cascade=True)

# cascade is an entity property — acquire() auto-cascades to parent
with sync_limiter.acquire(
    entity_id="api-key-456",
    resource="gpt-4",
    limits=default_limits,
    consume={"rpm": 1, "tpm": 500},
):
    call_api()

# Multi-tenant: each tenant gets an isolated namespace
tenant_repo = await Repository.open("tenant-alpha")
tenant_limiter = RateLimiter(repository=tenant_repo)

# Cleanup (removes all data)
await repo.delete_stack()
```

## Documentation

**[Full Documentation](https://zeroae.github.io/zae-limiter/)**

| Guide | Description |
|-------|-------------|
| [Getting Started](https://zeroae.github.io/zae-limiter/getting-started/) | Installation, first deployment |
| [Basic Usage](https://zeroae.github.io/zae-limiter/guide/basic-usage/) | Rate limiting patterns, error handling |
| [Hierarchical Limits](https://zeroae.github.io/zae-limiter/guide/hierarchical/) | Parent/child entities, cascade mode |
| [LLM Integration](https://zeroae.github.io/zae-limiter/guide/llm-integration/) | Token estimation and reconciliation |
| [CLI Reference](https://zeroae.github.io/zae-limiter/cli/) | Deploy, status, delete commands |
| [Multi-Tenant Guide](https://zeroae.github.io/zae-limiter/infra/production/#multi-tenant-deployments) | Namespace isolation, per-tenant IAM |
| [Production Guide](https://zeroae.github.io/zae-limiter/infra/production/) | Security, monitoring, cost |

## Production Deployment

The default deployment includes CloudWatch alarms and usage aggregation. For production, add data recovery and alert routing:

```bash
zae-limiter deploy --name my-app --region us-east-1 \
    --pitr-recovery-days 7 \
    --alarm-sns-topic arn:aws:sns:us-east-1:123456789012:alerts
```

For security best practices, multi-region considerations, and cost estimation, see the [Production Guide](https://zeroae.github.io/zae-limiter/infra/production/).

## Contributing

```bash
git clone https://github.com/zeroae/zae-limiter.git && cd zae-limiter
uv sync --all-extras
pytest
```

See the [Contributing Guide](https://zeroae.github.io/zae-limiter/contributing/) for development setup, testing, and architecture details.

## License

MIT
