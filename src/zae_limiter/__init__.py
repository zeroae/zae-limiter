"""
zae-limiter: Rate limiting library backed by DynamoDB.

This library provides a token bucket rate limiter with:
- Multiple limits per entity/resource
- Two-level hierarchy (parent/child entities)
- Cascade mode (consume from entity + parent)
- Stored limit configs
- Usage analytics via Lambda aggregator
- Pluggable backends via RepositoryProtocol

Example (connect to existing infrastructure - recommended):
    from zae_limiter import RateLimiter, Repository, Limit

    repo = await Repository.connect("my-app", "us-east-1")
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

Example (provision infrastructure):
    from zae_limiter import RateLimiter, Repository

    repo = await Repository.builder("my-app", "us-east-1").build()
    limiter = RateLimiter(repository=repo)
"""

from .config_cache import CacheStats, ConfigSource
from .exceptions import (
    EntityError,
    EntityExistsError,
    EntityNotFoundError,
    IncompatibleSchemaError,
    InfrastructureError,
    InfrastructureNotFoundError,
    InvalidIdentifierError,
    InvalidNameError,
    NamespaceNotFoundError,
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

# Sync (generated from async via scripts/generate_sync.py)
from .infra.sync_stack_manager import SyncStackManager
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
from .repository_builder import RepositoryBuilder
from .repository_protocol import RepositoryProtocol
from .sync_config_cache import SyncConfigCache
from .sync_lease import SyncLease
from .sync_limiter import SyncRateLimiter
from .sync_repository import SyncRepository
from .sync_repository_builder import SyncRepositoryBuilder
from .sync_repository_protocol import SyncRepositoryProtocol

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
    "RepositoryBuilder",
    "RepositoryProtocol",
    "Lease",
    "StackManager",
    # Sync classes (generated from async via scripts/generate_sync.py)
    "SyncRateLimiter",
    "SyncRepository",
    "SyncRepositoryBuilder",
    "SyncRepositoryProtocol",
    "SyncLease",
    "SyncConfigCache",
    "SyncStackManager",
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
    "ConfigSource",
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
    "NamespaceNotFoundError",
    # Exceptions - Version
    "VersionMismatchError",
    "IncompatibleSchemaError",
    # Exceptions - Validation
    "ValidationError",
    "InvalidIdentifierError",
    "InvalidNameError",
]
