---
title: ""
---

<div style="text-align: right; margin-bottom: 1rem;">
  <img src="assets/zae-limiter-white-bg.svg" alt="zae-limiter" width="400" class="light-only">
  <img src="assets/zae-limiter-dark-bg.svg" alt="zae-limiter" width="400" class="dark-only">
</div>

[![PyPI version](https://img.shields.io/pypi/v/zae-limiter?style=flat-square)](https://pypi.org/project/zae-limiter/)
[![Conda version](https://img.shields.io/conda/v/conda-forge/zae-limiter?style=flat-square)](https://anaconda.org/conda-forge/zae-limiter)
[![Python versions](https://img.shields.io/pypi/pyversions/zae-limiter?style=flat-square)](https://pypi.org/project/zae-limiter/)
[![License](https://img.shields.io/pypi/l/zae-limiter?style=flat-square)](https://github.com/zeroae/zae-limiter/blob/main/LICENSE)
[![Lint](https://img.shields.io/github/actions/workflow/status/zeroae/zae-limiter/ci-lint.yml?branch=main&style=flat-square&label=lint)](https://github.com/zeroae/zae-limiter/actions/workflows/ci-lint.yml)
[![Tests](https://img.shields.io/github/actions/workflow/status/zeroae/zae-limiter/ci-tests.yml?branch=main&style=flat-square&label=tests)](https://github.com/zeroae/zae-limiter/actions/workflows/ci-tests.yml)
[![codecov](https://img.shields.io/codecov/c/github/zeroae/zae-limiter?style=flat-square)](https://codecov.io/gh/zeroae/zae-limiter)

A rate limiting library backed by DynamoDB using the token bucket algorithm.

## Overview

**zae-limiter** excels at rate limiting scenarios where:

- **Multiple limits** are tracked per call (requests per minute, tokens per minute)
- **Consumption is unknown upfront** — adjust limits after the operation completes
- **Hierarchical limits** exist (API key → project, tenant → user)
- **Cost matters** — ~$0.75/1M requests ([details](performance.md#cost-optimization-strategies))

## Features

- **Token Bucket Algorithm** - Precise rate limiting with configurable burst capacity
- **Multiple Limits** - Track requests per minute, tokens per minute, etc. in a single call
- **Hierarchical Entities** - Two-level hierarchy (project → API keys) with cascade mode
- **Atomic Transactions** - Multi-key updates via DynamoDB TransactWriteItems
- **Rollback on Exception** - Automatic rollback if your code throws
- **Stored Limits** - Configure per-entity limits in DynamoDB
- **Usage Analytics** - Lambda aggregator for hourly/daily usage snapshots
- **Audit Logging** - Track entity and limit changes for compliance
- **Async + Sync APIs** - First-class async support with sync wrapper

## Quick Example

```python
from zae_limiter import RateLimiter, Limit, StackOptions

# Async rate limiter with declarative infrastructure
limiter = RateLimiter(
    name="my-app",
    region="us-east-1",
    stack_options=StackOptions(),  # Declare desired state - CloudFormation ensures it
)

# Define default limits (can be overridden per-entity)
default_limits = [
    Limit.per_minute("rpm", 100),
    Limit.per_minute("tpm", 10_000, burst=50_000),  # Token bucket with burst
]

async with limiter.acquire(
    entity_id="api-key-123",
    resource="gpt-4",
    limits=default_limits,
    consume={"rpm": 1, "tpm": 500},  # Estimate tokens upfront
) as lease:
    response = await call_llm()
    # Reconcile actual usage (can go negative for post-hoc adjustment)
    await lease.adjust(tpm=response.usage.total_tokens - 500)
    # On success: committed | On exception: rolled back automatically

# Hierarchical entities: project → API key
await limiter.create_entity(entity_id="proj-1", name="Production")
await limiter.set_limits("proj-1", [Limit.per_minute("tpm", 100_000)])
await limiter.create_entity(entity_id="api-key-456", parent_id="proj-1", cascade=True)

# cascade is an entity property — acquire() auto-cascades to parent
# limits=None auto-resolves from stored config (Entity > Resource > System)
async with limiter.acquire(
    entity_id="api-key-456",
    resource="gpt-4",
    limits=None,
    consume={"rpm": 1, "tpm": 500},
) as lease:
    response = await call_llm()
```

## Why DynamoDB?

- **Serverless** - No infrastructure to manage, 99.99% SLA
- **Regional** - Deploy independently per region with low latency
- **Scalable** - Handles millions of requests per second
- **Cost-effective** - Pay per request, no idle costs
- **Atomic** - TransactWriteItems for multi-key consistency

## Next Steps

| Guide | Description |
|-------|-------------|
| [Getting Started](getting-started.md) | Installation and first deployment |
| [Basic Usage](guide/basic-usage.md) | Rate limiting patterns and error handling |
| [Hierarchical Limits](guide/hierarchical.md) | Parent/child entities, cascade mode |
| [LLM Integration](guide/llm-integration.md) | Token estimation and reconciliation |
| [Production Guide](infra/production.md) | Security, monitoring, cost |
| [CLI Reference](cli.md) | Deploy, status, delete commands |

