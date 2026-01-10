#!/usr/bin/env python3
"""
Basic Rate Limiting Example

Demonstrates the core zae-limiter API with DynamoDB Local.

Setup:
    # Start DynamoDB Local
    docker run -p 8000:8000 amazon/dynamodb-local

    # Run this example
    uv run python examples/basic_rate_limiting.py

For real AWS DynamoDB, set environment variables:
    export AWS_ACCESS_KEY_ID=...
    export AWS_SECRET_ACCESS_KEY=...
    export AWS_DEFAULT_REGION=us-east-1

Then update ENDPOINT_URL to None below.
"""

import asyncio

from zae_limiter import (
    Limit,
    RateLimiter,
    RateLimitExceeded,
    SyncRateLimiter,
)

# Configuration for DynamoDB Local
ENDPOINT_URL = "http://localhost:8000"
TABLE_NAME = "rate_limits_example"


async def async_main() -> None:
    """Demonstrate async rate limiting."""
    print("=== Async Rate Limiting Example ===\n")

    # Create rate limiter with DynamoDB Local
    limiter = RateLimiter(
        table_name=TABLE_NAME,
        endpoint_url=ENDPOINT_URL,
        create_table=True,  # Auto-create table for local dev
        skip_version_check=True,  # Skip version check for local dev
    )

    # Define limits: 5 requests per minute, 100 tokens per minute
    limits = [
        Limit.per_minute("rpm", 5),
        Limit.per_minute("tpm", 100),
    ]

    # Make 7 requests to demonstrate rate limiting
    for i in range(7):
        try:
            async with limiter.acquire(
                entity_id="user-123",
                resource="api",
                limits=limits,
                consume={"rpm": 1, "tpm": 10},
            ) as lease:
                # Your API call goes here
                print(f"Request {i + 1}: Success! Consumed: {lease.consumed}")

        except RateLimitExceeded as e:
            # Rate limit exceeded - show how to handle it
            print(f"Request {i + 1}: Rate limit exceeded!")
            print(f"  Message: {e}")
            print(f"  Retry after: {e.retry_after_seconds:.1f}s")
            print(f"  HTTP header: Retry-After: {e.retry_after_header}")

            # For API responses, use as_dict() for JSON serialization
            error_response = e.as_dict()
            print(f"  JSON response: error={error_response['error']}")
            violations = [v["limit_name"] for v in error_response["limits"] if v["exceeded"]]
            print(f"  Violations: {violations}")

    await limiter.close()


def sync_main() -> None:
    """Demonstrate sync rate limiting (same API, without async/await)."""
    print("\n=== Sync Rate Limiting Example ===\n")

    # Create sync rate limiter
    limiter = SyncRateLimiter(
        table_name=TABLE_NAME + "_sync",
        endpoint_url=ENDPOINT_URL,
        create_table=True,
        skip_version_check=True,
    )

    limits = [Limit.per_minute("rpm", 3)]

    for i in range(5):
        try:
            # Same API as async, just without 'async with'
            with limiter.acquire(
                entity_id="user-456",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ) as lease:
                print(f"Request {i + 1}: Success! Consumed: {lease.consumed}")

        except RateLimitExceeded as e:
            print(f"Request {i + 1}: Exceeded! Retry in {e.retry_after_seconds:.1f}s")

    limiter.close()


if __name__ == "__main__":
    # Run async example
    asyncio.run(async_main())

    # Run sync example
    sync_main()

    print("\nDone! Both async and sync APIs work the same way.")
