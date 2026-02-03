"""
zae-limiter: Rate limiting library backed by DynamoDB.

This library provides a token bucket rate limiter with:
- Multiple limits per entity/resource
- Two-level hierarchy (parent/child entities)
- Cascade mode (consume from entity + parent)
- Stored limit configs
- Usage analytics via Lambda aggregator
- Pluggable backends via RepositoryProtocol

Example (new API - preferred):
    from zae_limiter import RateLimiter, Repository, Limit, StackOptions

    repo = Repository(
        name="my-app",
        region="us-east-1",
        stack_options=StackOptions(),  # Declare desired infrastructure state
    )
    limiter = RateLimiter(repository=repo)

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

Example (old API - deprecated):
    limiter = RateLimiter(
        name="my-app",
        region="us-east-1",
        stack_options=StackOptions(),
    )
"""

from .config_cache import CacheStats
from .exceptions import (
    EntityError,
    EntityExistsError,
    EntityNotFoundError,
    IncompatibleSchemaError,
    InfrastructureError,
    InfrastructureNotFoundError,
    InvalidIdentifierError,
    InvalidNameError,
    RateLimitError,
    RateLimiterUnavailable,
    RateLimitExceeded,
    StackAlreadyExistsError,
    StackCreationError,
    ValidationError,
    VersionError,
    VersionMismatchError,
    ZAELimiterError,
)
from .infra.stack_manager import StackManager
from .lease import Lease
from .limiter import OnUnavailable, RateLimiter
from .models import (
    AuditAction,
    AuditEvent,
    BackendCapabilities,
    BucketState,
    Entity,
    EntityCapacity,
    Limit,
    LimiterInfo,
    LimitName,
    LimitStatus,
    ResourceCapacity,
    StackOptions,
    Status,
    UsageSnapshot,
    UsageSummary,
)
from .repository import Repository
from .repository_protocol import RepositoryProtocol

try:
    from ._version import __version__
except ImportError:
    __version__ = "0.0.0+unknown"

__all__ = [
    # Version
    "__version__",
    # Main classes
    "RateLimiter",
    "Repository",
    "RepositoryProtocol",
    "Lease",
    "StackManager",
    # Models
    "Limit",
    "LimiterInfo",
    "LimitName",
    "Entity",
    "LimitStatus",
    "BucketState",
    "UsageSnapshot",
    "UsageSummary",
    "ResourceCapacity",
    "EntityCapacity",
    "StackOptions",
    "BackendCapabilities",
    "Status",
    "CacheStats",
    # Audit
    "AuditEvent",
    "AuditAction",
    # Enums
    "OnUnavailable",
    # Exceptions - Base
    "ZAELimiterError",
    # Exceptions - Categories
    "RateLimitError",
    "InfrastructureError",
    "EntityError",
    "VersionError",
    # Exceptions - Rate Limit
    "RateLimitExceeded",
    # Exceptions - Entity
    "EntityNotFoundError",
    "EntityExistsError",
    # Exceptions - Infrastructure
    "RateLimiterUnavailable",
    "StackCreationError",
    "StackAlreadyExistsError",
    "InfrastructureNotFoundError",
    # Exceptions - Version
    "VersionMismatchError",
    "IncompatibleSchemaError",
    # Exceptions - Validation
    "ValidationError",
    "InvalidIdentifierError",
    "InvalidNameError",
]
