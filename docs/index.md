# zae-limiter

A rate limiting library backed by DynamoDB using the token bucket algorithm.

[![PyPI version](https://img.shields.io/pypi/v/zae-limiter)](https://pypi.org/project/zae-limiter/)
[![Python versions](https://img.shields.io/pypi/pyversions/zae-limiter)](https://pypi.org/project/zae-limiter/)
[![License](https://img.shields.io/pypi/l/zae-limiter)](https://github.com/zeroae/zae-limiter/blob/main/LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/zeroae/zae-limiter/ci.yml?branch=main)](https://github.com/zeroae/zae-limiter/actions/workflows/ci.yml)

## Overview

**zae-limiter** is designed for limiting LLM API calls where:

- **Multiple limits** are tracked per call (requests per minute, tokens per minute)
- **Token counts** are unknown until after the call completes
- **Hierarchical limits** exist (API key → project)

## Features

- **Token Bucket Algorithm** - Precise rate limiting with configurable burst capacity
- **Multiple Limits** - Track requests per minute, tokens per minute, etc. in a single call
- **Hierarchical Entities** - Two-level hierarchy (project → API keys) with cascade mode
- **Atomic Transactions** - Multi-key updates via DynamoDB TransactWriteItems
- **Rollback on Exception** - Automatic rollback if your code throws
- **Stored Limits** - Configure per-entity limits in DynamoDB
- **Usage Analytics** - Lambda aggregator for hourly/daily usage snapshots
- **Async + Sync APIs** - First-class async support with sync wrapper

## Quick Example

```python
from zae_limiter import RateLimiter, Limit

limiter = RateLimiter(
    name="limiter",  # Connects to ZAEL-limiter
    region="us-east-1",
)

async with limiter.acquire(
    entity_id="api-key-123",
    resource="gpt-4",
    limits=[
        Limit.per_minute("rpm", 100),       # 100 requests/minute
        Limit.per_minute("tpm", 10_000),    # 10k tokens/minute
    ],
    consume={"rpm": 1, "tpm": 500},  # estimate 500 tokens
) as lease:
    response = await call_llm()

    # Reconcile actual token usage
    actual_tokens = response.usage.total_tokens
    await lease.adjust(tpm=actual_tokens - 500)
```

## Why DynamoDB?

- **Serverless** - No infrastructure to manage
- **Global** - Multi-region replication for low latency
- **Scalable** - Handles millions of requests per second
- **Cost-effective** - Pay per request, no idle costs
- **Atomic** - TransactWriteItems for multi-key consistency

## Next Steps

- [Getting Started](getting-started.md) - Installation and quick start guide
- [User Guide](guide/basic-usage.md) - Detailed usage patterns
- [API Reference](api/index.md) - Complete API documentation
- [Migrations](migrations.md) - Schema migration strategy and versioning
