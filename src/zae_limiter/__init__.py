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
        create_stack=True,
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
    EntityError,
    EntityExistsError,
    EntityNotFoundError,
    IncompatibleSchemaError,
    InfrastructureError,
    InfrastructureNotFoundError,
    RateLimitError,
    RateLimiterUnavailable,
    RateLimitExceeded,
    StackAlreadyExistsError,
    StackCreationError,
    VersionError,
    VersionMismatchError,
    ZAELimiterError,
)
from .lease import Lease, SyncLease
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

# RateLimiter, SyncRateLimiter, FailureMode, and StackManager are imported
# lazily via __getattr__ to avoid loading aioboto3 for Lambda functions
# that only need boto3

try:
    from ._version import __version__  # type: ignore[import-untyped]
except ImportError:
    __version__ = "0.0.0+unknown"

__all__ = [
    # Version
    "__version__",
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
    # Exceptions - Base
    "ZAELimiterError",
    # Exceptions - Categories
    "RateLimitError",
    "InfrastructureError",
    "EntityError",
    "VersionError",
    # Exceptions - Rate Limit
    "RateLimitExceeded",
    "RateLimiterUnavailable",
    # Exceptions - Entity
    "EntityNotFoundError",
    "EntityExistsError",
    # Exceptions - Infrastructure
    "StackCreationError",
    "StackAlreadyExistsError",
    "InfrastructureNotFoundError",
    # Exceptions - Version
    "VersionMismatchError",
    "IncompatibleSchemaError",
]


def __getattr__(name: str) -> type:
    """
    Lazy import for modules with heavy dependencies.

    This allows the package to be imported without loading aioboto3,
    which is critical for Lambda functions that only need boto3.

    The aggregator Lambda function imports the handler which would normally
    trigger loading of the entire package. By making RateLimiter and
    StackManager lazy imports, we avoid loading aioboto3 (not available in
    Lambda runtime) while maintaining backward compatibility for regular usage.
    """
    if name == "RateLimiter":
        from .limiter import RateLimiter

        return RateLimiter
    if name == "SyncRateLimiter":
        from .limiter import SyncRateLimiter

        return SyncRateLimiter
    if name == "FailureMode":
        from .limiter import FailureMode

        return FailureMode
    if name == "StackManager":
        from .infra.stack_manager import StackManager

        return StackManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
