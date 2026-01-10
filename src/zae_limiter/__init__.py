"""
zae-limiter: Rate limiting library backed by DynamoDB.

This library provides a token bucket rate limiter with:
- Multiple limits per entity/resource
- Two-level hierarchy (parent/child entities)
- Cascade mode (consume from entity + parent)
- Stored limit configs
- Usage analytics via Lambda aggregator

Example:
    from zae_limiter import RateLimiter, Limit, FailureMode

    limiter = RateLimiter(
        table_name="rate_limits",
        region="us-east-1",
        create_table=True,
    )

    async with limiter.acquire(
        entity_id="key-abc",
        resource="gpt-4",
        limits=[
            Limit.per_minute("rpm", 100),
            Limit.per_minute("tpm", 10_000),
        ],
        consume={"rpm": 1, "tpm": 500},
    ) as lease:
        response = await llm_call()
        await lease.adjust(tpm=response.usage.total_tokens - 500)
"""

from .exceptions import (
    EntityExistsError,
    EntityNotFoundError,
    RateLimitError,
    RateLimiterUnavailable,
    RateLimitExceeded,
    StackAlreadyExistsError,
    StackCreationError,
)
from .infra.stack_manager import StackManager
from .lease import Lease, SyncLease
from .limiter import FailureMode, RateLimiter, SyncRateLimiter
from .models import (
    BucketState,
    Entity,
    EntityCapacity,
    Limit,
    LimitName,
    LimitStatus,
    ResourceCapacity,
    UsageSnapshot,
)

__version__ = "0.1.0"

__all__ = [
    # Main classes
    "RateLimiter",
    "SyncRateLimiter",
    "Lease",
    "SyncLease",
    "StackManager",
    # Models
    "Limit",
    "LimitName",
    "Entity",
    "LimitStatus",
    "BucketState",
    "UsageSnapshot",
    "ResourceCapacity",
    "EntityCapacity",
    # Enums
    "FailureMode",
    # Exceptions
    "RateLimitError",
    "RateLimitExceeded",
    "RateLimiterUnavailable",
    "EntityNotFoundError",
    "EntityExistsError",
    "StackCreationError",
    "StackAlreadyExistsError",
]
