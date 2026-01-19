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
        name="my-app",  # ZAEL-my-app resources
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

# ---------------------------------------------------------------------------
# Lazy imports for Lambda compatibility
# ---------------------------------------------------------------------------
# RateLimiter, SyncRateLimiter, OnUnavailable, and StackManager are imported
# lazily via __getattr__ below. This is REQUIRED because:
#
# 1. These modules depend on aioboto3, which is NOT available in the AWS
#    Lambda runtime (only boto3 is provided).
# 2. The Lambda aggregator function (zae_limiter.aggregator.handler) imports
#    this package. Without lazy imports, it would fail with:
#    ImportError: No module named 'aioboto3'
# 3. By deferring the import until the attribute is accessed, Lambda can
#    import the package and use boto3-only code paths successfully.
#
# WARNING: Do not add eager imports of limiter.py or infra/stack_manager.py
# here. Doing so will break the Lambda aggregator function.
# ---------------------------------------------------------------------------
from typing import TYPE_CHECKING

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
from .lease import Lease, SyncLease
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

if TYPE_CHECKING:
    # Type-checking imports for static analysis and IDE support.
    # These are never executed at runtime.
    from .infra.stack_manager import StackManager as StackManager
    from .limiter import OnUnavailable as OnUnavailable
    from .limiter import RateLimiter as RateLimiter
    from .limiter import SyncRateLimiter as SyncRateLimiter
    from .repository import Repository as Repository
    from .repository_protocol import RepositoryProtocol as RepositoryProtocol

try:
    from ._version import __version__  # type: ignore[import-not-found]
except ImportError:
    __version__ = "0.0.0+unknown"

__all__ = [
    # Version
    "__version__",
    # Main classes
    "RateLimiter",
    "SyncRateLimiter",
    "Repository",
    "RepositoryProtocol",
    "Lease",
    "SyncLease",
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


def __getattr__(name: str) -> type:
    """Lazy import for modules that require aioboto3.

    This function enables the package to be imported in the AWS Lambda runtime,
    which only provides boto3 (not aioboto3). Without lazy imports, importing
    this package would fail with ``ImportError: No module named 'aioboto3'``.

    The Lambda aggregator function (``zae_limiter.aggregator.handler``) uses
    only boto3 for DynamoDB stream processing. By deferring imports of
    ``RateLimiter``, ``SyncRateLimiter``, ``OnUnavailable``, and ``StackManager``
    until they are actually accessed, we allow the Lambda handler to import
    the package successfully.

    For static type checking and IDE support, these classes are also imported
    in the ``TYPE_CHECKING`` block above, which is only evaluated by type
    checkers, not at runtime.

    See Also:
        PEP 562 -- Module __getattr__ and __dir__
    """
    if name == "RateLimiter":
        from .limiter import RateLimiter

        return RateLimiter
    if name == "SyncRateLimiter":
        from .limiter import SyncRateLimiter

        return SyncRateLimiter
    if name == "OnUnavailable":
        from .limiter import OnUnavailable

        return OnUnavailable
    if name == "StackManager":
        from .infra.stack_manager import StackManager

        return StackManager
    if name == "Repository":
        from .repository import Repository

        return Repository
    if name == "RepositoryProtocol":
        from .repository_protocol import RepositoryProtocol

        return RepositoryProtocol
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
