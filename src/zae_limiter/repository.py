"""DynamoDB repository for rate limiter data."""

import asyncio
import functools
import logging
import random
import time
import warnings
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from aiobotocore.session import AioSession, get_session
from botocore.exceptions import ClientError
from ulid import ULID

from . import schedule, schema
from .config_cache import CacheStats, ConfigCache, ConfigSource
from .exceptions import (
    EntityExistsError,
    FanoutIncomplete,
    NamespaceStateError,
    RateLimiterUnavailable,
    ValidationError,
)
from .models import (
    AuditAction,
    AuditEvent,
    BackendCapabilities,
    BucketState,
    Entity,
    Limit,
    OnUnavailableAction,
    StackOptions,
    UsageSnapshot,
    UsageSummary,
    hoisted_schedule_timezone,
    validate_identifier,
    validate_resource,
)
from .naming import normalize_stack_name
from .repository_protocol import (
    PRESERVE_DISABLED as _PRESERVE_DISABLED,
)
from .repository_protocol import SpeculativeFailureReason, SpeculativeResult

if TYPE_CHECKING:
    from .repository_builder import RepositoryBuilder

logger = logging.getLogger(__name__)

#: Sentinel meaning "keep whatever `disabled` value is already stored" (ADR-125).
#: Distinct from None, which explicitly means "inherit from the level above".
# _PRESERVE_DISABLED is imported from repository_protocol so the contract and
# this implementation compare against the same object (ADR-108/ADR-125).

# A partial BatchGetItem must be retried rather than treated as "these items
# do not exist" (issue #444). Three retries with exponential backoff from 50ms
# covers a transient throttle without stalling the slow path; beyond that the
# caller is told the answer is unknown rather than given a short list it
# cannot tell apart from a complete one.
_BATCH_GET_MAX_RETRIES = 3
_BATCH_GET_RETRY_BASE_DELAY = 0.05


class Repository:
    """Async DynamoDB repository for rate limiter data.

    Handles all DynamoDB operations including entities, buckets,
    limit configs, and transactions.

    Use :meth:`open` to open a repository (recommended), or :meth:`builder`
    for custom infrastructure options (permission boundaries, IAM config).

    .. deprecated::
        Direct construction via ``Repository(...)`` is deprecated and will be
        removed in v1.0.0. Use ``Repository.open(...)`` or
        ``Repository.builder().build()`` instead.

    Args:
        name: Resource identifier (e.g., "my-app"). Used as the
            CloudFormation stack_name and DynamoDB table_name.
        region: AWS region (e.g., "us-east-1").
        endpoint_url: Custom endpoint URL (e.g., LocalStack).
        stack_options: Configuration for CloudFormation infrastructure.
            Pass StackOptions to enable declarative infrastructure management.
        config_cache_ttl: TTL in seconds for config cache (default: 60, 0 to disable).
            Controls caching of resolved limit configs in resolve_limits().

    Example::

        # Most users (auto-provisions if needed)
        repo = await Repository.open("my-app")

        # Custom infrastructure options
        repo = await Repository.builder().namespace("my-app").build()
    """

    def __init__(
        self,
        name: str,
        region: str | None = None,
        endpoint_url: str | None = None,
        stack_options: StackOptions | None = None,
        config_cache_ttl: int = 60,
        *,
        _skip_deprecation_warning: bool = False,
    ) -> None:
        if not _skip_deprecation_warning:
            warnings.warn(
                "Directly calling Repository(...) is deprecated. "
                "Use Repository.open(...) for most use cases "
                "or Repository.builder().build() for custom infrastructure options. "
                "This will be removed in v1.0.0.",
                DeprecationWarning,
                stacklevel=2,
            )
        # Validate and normalize name
        self.stack_name = normalize_stack_name(name)
        # Table name is always identical to stack name
        self.table_name = self.stack_name
        self.region = region
        self.endpoint_url = endpoint_url
        self._namespace_id = schema.DEFAULT_NAMESPACE
        self._namespace_name = "default"
        self._bucket_ttl_refill_multiplier = 7
        self._stack_options = stack_options
        self._session: AioSession | None = None
        self._client: Any = None
        self._caller_identity_arn: str | None = None
        self._caller_identity_fetched = False
        self._audit_retention_days_cache: int | None = None

        # Builder-initialized flag: set True by RepositoryBuilder.build()
        self._builder_initialized = False
        # Auto-update Lambda on version mismatch (set by builder)
        self._auto_update = True
        # Scoped repo flag: prevents close() from closing shared client
        self._is_scoped = False

        # DynamoDB supports all extended features
        self._capabilities = BackendCapabilities(
            supports_audit_logging=True,
            supports_usage_snapshots=True,
            supports_infrastructure_management=True,
            supports_change_streams=True,
            supports_batch_operations=True,
        )

        # Config cache for resolve_limits() (ADR-122)
        self._config_cache = ConfigCache(
            ttl_seconds=config_cache_ttl, namespace_id=self._namespace_id
        )
        self._config_cache_ttl = config_cache_ttl

        # Entity metadata cache for parallel cascade writes (issue #318)
        # (entity_id, resource) pairs already warned about MAX_SHARD_COUNT
        self._shard_cap_warned: set[tuple[str, str]] = set()
        # Value: (cascade, parent_id, {resource: shard_count})
        # cascade/parent_id are immutable; shard_count updated on doubling
        self._entity_cache: dict[tuple[str, str], tuple[bool, str | None, dict[str, int]]] = {}

        # Cached on_unavailable from system config (issue #366)
        # Once loaded, used as fallback when DynamoDB is unreachable
        self._on_unavailable_cache: OnUnavailableAction | None = None

        # Namespace resolution cache: shared across scoped repos
        self._namespace_cache: dict[str, str] = {}

    @classmethod
    def builder(cls) -> "RepositoryBuilder":
        """Create a RepositoryBuilder for fluent configuration.

        For most use cases, prefer :meth:`open` instead. Use ``builder()``
        when you need custom infrastructure options (permission boundaries,
        Lambda config, IAM role naming).

        Stack defaults mirror :meth:`open`: ``ZAEL_STACK`` env var
        or ``"zae-limiter"``, namespace ``"default"``.

        Example:
            repo = await (
                Repository.builder()
                .namespace("my-app")
                .lambda_memory(512)
                .build()
            )
        """
        from .repository_builder import RepositoryBuilder

        return RepositoryBuilder()

    @classmethod
    async def open(
        cls,
        namespace: str | None = None,
        *,
        stack: str | None = None,
        region: str | None = None,
        endpoint_url: str | None = None,
        config_cache_ttl: int = 60,
        auto_update: bool = True,
    ) -> "Repository":
        """Open a repository, auto-provisioning infrastructure if needed.

        This is the recommended entry point for most applications.
        Namespace is the primary parameter — stack name defaults to
        ``"zae-limiter"`` and is rarely needed.

        **Auto-provision behavior:**

        - If the DynamoDB table doesn't exist, deploys a new stack with
          default options (aggregator enabled).
        - If the table exists but the namespace isn't registered, registers it.
        - The ``"default"`` namespace is always registered.
        - Version check and Lambda auto-update run on every call.

        For custom infrastructure options (permission boundaries, Lambda
        config, IAM role naming), use ``Repository.builder()`` instead.

        **Stack resolution:** ``stack`` arg → ``ZAEL_STACK`` env var
        → ``"zae-limiter"``.

        **Namespace resolution:** ``namespace`` arg → ``ZAEL_NAMESPACE``
        env var → ``"default"``.

        Args:
            namespace: Namespace to open. Defaults to ``ZAEL_NAMESPACE``
                env var or ``"default"``.
            stack: Stack name. Defaults to ``ZAEL_STACK`` env var
                or ``"zae-limiter"``.
            region: AWS region (e.g., ``"us-east-1"``).
            endpoint_url: Custom endpoint URL (e.g., LocalStack).
            config_cache_ttl: Config cache TTL in seconds (default: 60,
                0 to disable).
            auto_update: Auto-update Lambda on version mismatch
                (default: True).

        Returns:
            Fully initialized Repository ready for use.

        Raises:
            IncompatibleSchemaError: If schema migration is required.
            VersionMismatchError: If auto_update is False and versions differ.

        Example::

            # Most users
            repo = await Repository.open("my-app")

            # Multi-tenant
            repo_alpha = await Repository.open("tenant-alpha")

            # Explicit stack
            repo = await Repository.open("my-app", stack="custom-stack")

            # Simplest (stack="zae-limiter", namespace="default")
            repo = await Repository.open()
        """
        from .naming import resolve_namespace_name, resolve_stack_name

        name = resolve_stack_name(stack)
        ns_name = resolve_namespace_name(namespace)

        repo = cls(
            name=name,
            region=region,
            endpoint_url=endpoint_url,
            config_cache_ttl=config_cache_ttl,
            _skip_deprecation_warning=True,
        )
        repo._auto_update = auto_update

        # Try resolve namespace — auto-provision if needed
        try:
            namespace_id = await repo._resolve_namespace(ns_name)
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                # Table doesn't exist — deploy stack with defaults
                repo._stack_options = StackOptions()
                await repo._ensure_infrastructure_internal()
                # Always register "default" namespace
                await repo._register_namespace("default")
                # Register requested namespace (no-op if "default")
                namespace_id = await repo._register_namespace(ns_name)
            else:
                raise
        else:
            if namespace_id is None:
                # Table exists but namespace not found — register it
                namespace_id = await repo._register_namespace(ns_name)

        repo._namespace_id = namespace_id
        repo._namespace_name = ns_name
        repo._reinitialize_config_cache(namespace_id)

        # Version check + Lambda auto-update (always, no endpoint_url guard)
        if auto_update:
            await repo._check_and_update_version_auto()
        else:
            await repo._check_version_strict()

        repo._builder_initialized = True
        return repo

    @classmethod
    async def connect(
        cls,
        namespace: str | None = None,
        *,
        stack: str | None = None,
        region: str | None = None,
        endpoint_url: str | None = None,
        config_cache_ttl: int = 60,
    ) -> "Repository":
        """Connect to existing infrastructure without provisioning anything.

        Use this when the CloudFormation stack, the DynamoDB table, and the
        namespace registry are managed outside the library — a packaged
        CloudFormation template, Terraform, or CDK. Unlike :meth:`open`,
        ``connect()`` never creates or mutates infrastructure: it issues
        reads only and raises when something it needs is absent.

        **Differences from :meth:`open`:**

        ============================  ===============  ==========================
        Situation                     ``open()``       ``connect()``
        ============================  ===============  ==========================
        Table missing                 Deploys stack    ``InfrastructureNotFound``
        Namespace unregistered        Registers it     ``NamespaceNotFoundError``
        Version record missing        Writes it        ``InfrastructureNotFound``
        Lambda version behind client  Updates Lambda   ``VersionMismatchError``
        ============================  ===============  ==========================

        **Stack resolution:** ``stack`` arg → ``ZAEL_STACK`` env var
        → ``"zae-limiter"``.

        **Namespace resolution:** ``namespace`` arg → ``ZAEL_NAMESPACE``
        env var → ``"default"``.

        Args:
            namespace: Namespace to connect to. Defaults to
                ``ZAEL_NAMESPACE`` env var or ``"default"``.
            stack: Stack name. Defaults to ``ZAEL_STACK`` env var
                or ``"zae-limiter"``.
            region: AWS region (e.g., ``"us-east-1"``).
            endpoint_url: Custom endpoint URL (e.g., LocalStack).
            config_cache_ttl: Config cache TTL in seconds (default: 60,
                0 to disable).

        Returns:
            Repository bound to the existing infrastructure.

        Raises:
            InfrastructureNotFoundError: If the table doesn't exist, or
                exists but was never initialized by ``zae-limiter deploy``.
            NamespaceNotFoundError: If the namespace isn't registered.
            VersionMismatchError: If the deployed Lambda version is behind
                the client version. Redeploy your stack to resolve.
            IncompatibleSchemaError: If schema migration is required.

        Example::

            # Infrastructure deployed by your own CloudFormation
            repo = await Repository.connect("my-app")
            limiter = RateLimiter(repository=repo)
        """
        from .exceptions import InfrastructureNotFoundError, NamespaceNotFoundError
        from .naming import resolve_namespace_name, resolve_stack_name

        name = resolve_stack_name(stack)
        ns_name = resolve_namespace_name(namespace)

        # stack_options stays None, so infrastructure provisioning is
        # structurally impossible for this Repository.
        repo = cls(
            name=name,
            region=region,
            endpoint_url=endpoint_url,
            config_cache_ttl=config_cache_ttl,
            _skip_deprecation_warning=True,
        )
        repo._auto_update = False

        try:
            namespace_id = await repo._resolve_namespace(ns_name)
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                raise InfrastructureNotFoundError(name) from e
            raise

        if namespace_id is None:
            raise NamespaceNotFoundError(ns_name)

        repo._namespace_id = namespace_id
        repo._namespace_name = ns_name
        repo._reinitialize_config_cache(namespace_id)

        # Strict version check — never writes, never updates the Lambda
        await repo._check_version_strict(initialize_if_missing=False)

        repo._builder_initialized = True
        return repo

    @property
    def namespace_name(self) -> str:
        """The human-readable namespace name."""
        return self._namespace_name

    @property
    def namespace_id(self) -> str:
        """The opaque namespace ID used in DynamoDB keys."""
        return self._namespace_id

    @property
    def capabilities(self) -> BackendCapabilities:
        """Declare which extended features this backend supports."""
        return self._capabilities

    async def _get_client(self) -> Any:
        """Get or create the DynamoDB client."""
        if self._client is None:
            self._session = get_session()
            self._client = await self._session.create_client(
                "dynamodb",
                region_name=self.region,
                endpoint_url=self.endpoint_url,
            ).__aenter__()
        return self._client

    async def namespace(
        self,
        name: str,
        *,
        on_unavailable: "OnUnavailableAction | None" = None,
        bucket_ttl_multiplier: int | None = None,
    ) -> "Repository":
        """Return a scoped Repository for the given namespace.

        The scoped repo shares the DynamoDB client, entity cache, and
        namespace cache with the parent, but has its own ``ConfigCache``
        and namespace identity.  Calling ``close()`` on a scoped repo
        is a no-op (it does not close the shared client).

        Args:
            name: Namespace name to resolve.
            on_unavailable: Override on_unavailable behavior for this
                namespace ("allow" or "block").  Persisted via
                ``set_system_defaults()``.
            bucket_ttl_multiplier: Override bucket TTL multiplier for
                this scoped repo.  Defaults to the parent's value.

        Returns:
            A new Repository scoped to the resolved namespace.

        Raises:
            NamespaceNotFoundError: If the namespace is not registered.
        """
        from .exceptions import NamespaceNotFoundError

        # Resolve namespace (uses cache if available)
        namespace_id = await self._resolve_namespace(name)
        if namespace_id is None:
            raise NamespaceNotFoundError(name)

        # Create scoped repo (shallow copy sharing client resources)
        scoped = Repository.__new__(Repository)
        scoped.stack_name = self.stack_name
        scoped.table_name = self.table_name
        scoped.region = self.region
        scoped.endpoint_url = self.endpoint_url
        scoped._namespace_id = namespace_id
        scoped._namespace_name = name
        scoped._bucket_ttl_refill_multiplier = (
            bucket_ttl_multiplier
            if bucket_ttl_multiplier is not None
            else self._bucket_ttl_refill_multiplier
        )
        scoped._stack_options = None  # scoped repos don't manage infrastructure
        scoped._session = self._session
        scoped._client = self._client
        scoped._caller_identity_arn = self._caller_identity_arn
        scoped._caller_identity_fetched = self._caller_identity_fetched
        scoped._audit_retention_days_cache = self._audit_retention_days_cache
        scoped._builder_initialized = self._builder_initialized
        scoped._auto_update = self._auto_update
        scoped._is_scoped = True
        scoped._capabilities = self._capabilities
        scoped._config_cache_ttl = self._config_cache_ttl
        scoped._config_cache = ConfigCache(
            ttl_seconds=self._config_cache_ttl, namespace_id=namespace_id
        )
        # Share mutable caches
        scoped._entity_cache = self._entity_cache
        scoped._namespace_cache = self._namespace_cache
        # Scoped repos start with no on_unavailable cache (each namespace
        # has its own system config)
        scoped._on_unavailable_cache = None

        # Persist on_unavailable as system config if set
        if on_unavailable is not None:
            existing_limits, _ = await scoped.get_system_defaults()
            await scoped.set_system_defaults(
                limits=existing_limits,
                on_unavailable=on_unavailable,
            )

        return scoped

    async def close(self) -> None:
        """Close the DynamoDB client.

        No-op for scoped repos (created via ``namespace()``).
        """
        if self._is_scoped:
            return
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None
            self._session = None

    async def _get_item(self, pk: str, sk: str) -> dict[str, Any] | None:
        """Get a raw DynamoDB item by primary key (testing helper).

        Args:
            pk: Partition key value (already formatted, e.g., "ENTITY#user-1")
            sk: Sort key value (already formatted, e.g., "#BUCKET#api")

        Returns:
            Deserialized item dict or None if not found.
        """
        client = await self._get_client()
        response = await client.get_item(
            TableName=self.table_name,
            Key={"PK": {"S": pk}, "SK": {"S": sk}},
        )
        item = response.get("Item")
        if not item:
            return None
        # Deserialize DynamoDB types to Python types (S and N only for buckets)
        result: dict[str, Any] = {}
        for key, value in item.items():
            if "S" in value:
                result[key] = value["S"]
            elif "N" in value:
                result[key] = int(value["N"])
        return result

    async def _get_caller_identity_arn(self) -> str | None:
        """
        Get the ARN of the AWS caller identity (lazy cached).

        Returns the full ARN of the IAM user/role making API calls.
        Returns None if the identity cannot be determined (e.g., local testing).
        """
        if self._caller_identity_fetched:
            return self._caller_identity_arn

        self._caller_identity_fetched = True
        try:
            if self._session is None:
                self._session = get_session()

            async with self._session.create_client(
                "sts",
                region_name=self.region,
                endpoint_url=self.endpoint_url,
            ) as sts_client:
                response = await sts_client.get_caller_identity()
                self._caller_identity_arn = response.get("Arn")
        except Exception:
            # Silently fail - caller identity is optional
            self._caller_identity_arn = None

        return self._caller_identity_arn

    def _now_ms(self) -> int:
        """Current time in epoch milliseconds — the token-bucket clock (#430).

        Declared on ``RepositoryProtocol`` so the limiter and the lease read
        the clock through here too. Patching this one method controls refill
        math, ``rf`` stamps and bucket TTLs across a whole ``acquire()``
        without sleeping and without touching the global ``time`` module
        (which moto and botocore also read).

        It does **not** control the config cache, whose TTL is in seconds
        against ``time.time()``. See ``RepositoryProtocol._now_ms`` for what
        that means for a test that jumps the clock, and for how many readings
        each ``acquire()`` path takes.
        """
        return int(time.time() * 1000)

    # -------------------------------------------------------------------------
    # Table operations
    # -------------------------------------------------------------------------

    async def create_table(self) -> None:
        """Create the DynamoDB table if it doesn't exist."""
        client = await self._get_client()
        definition = schema.get_table_definition(self.table_name)

        try:
            await client.create_table(**definition)
            # Wait for table to be active
            waiter = client.get_waiter("table_exists")
            await waiter.wait(TableName=self.table_name)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceInUseException":
                raise

    async def delete_table(self) -> None:
        """Delete the DynamoDB table."""
        client = await self._get_client()
        try:
            await client.delete_table(TableName=self.table_name)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                raise

    async def delete_stack(self) -> None:
        """Delete the CloudFormation stack and all associated resources.

        Permanently removes the stack including DynamoDB table, Lambda
        aggregator, IAM roles, and CloudWatch log groups. Waits for
        deletion to complete. No-op if stack doesn't exist.

        Raises:
            StackOperationError: If deletion fails.
        """
        from .infra.stack_manager import StackManager

        async with StackManager(self.stack_name, self.region, self.endpoint_url) as manager:
            await manager.delete_stack(self.stack_name)

    async def ensure_infrastructure(self) -> None:
        """
        Ensure DynamoDB infrastructure exists.

        .. deprecated::
            Use ``Repository.builder(...).build()`` instead, which handles
            infrastructure creation during the build step.

        Creates CloudFormation stack using stack_options passed to the constructor.
        No-op if stack_options was not provided.

        Raises:
            StackOperationError: If CloudFormation stack creation fails
        """
        import warnings

        warnings.warn(
            "ensure_infrastructure() is deprecated. Use Repository.builder(...).build() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        await self._ensure_infrastructure_internal()

    async def _ensure_infrastructure_internal(self) -> None:
        """Internal: ensure infrastructure exists (no deprecation warning)."""
        if self._stack_options is None:
            return

        from .infra.stack_manager import StackManager

        async with StackManager(self.stack_name, self.region, self.endpoint_url) as manager:
            await manager.create_stack(stack_options=self._stack_options)

            # Deploy Lambda code only for functions CloudFormation actually created.
            # Both gates mirror the template conditions: with create_iam=False the
            # functions have no execution role and are never created, so pushing
            # code to them would fail with ResourceNotFoundException.
            if self._stack_options.deploys_aggregator_lambda:
                await manager.deploy_lambda_code()

            if self._stack_options.deploys_provisioner_lambda:
                await manager.deploy_provisioner_code()

        # Write retention config to system config item
        await self._write_audit_retention_config()

    async def create_stack(
        self,
        stack_options: StackOptions | None = None,
    ) -> None:
        """
        Create DynamoDB infrastructure via CloudFormation.

        .. deprecated:: 0.6.0
            Use :meth:`ensure_infrastructure` instead. Pass stack_options
            to the Repository constructor. Will be removed in v1.0.0.

        Args:
            stack_options: Configuration for CloudFormation stack.
                If None, uses the stack_options passed to the constructor.

        Raises:
            StackOperationError: If CloudFormation stack creation fails
        """
        import warnings

        warnings.warn(
            "create_stack() is deprecated. Use ensure_infrastructure() instead. "
            "Pass stack_options to the Repository constructor. "
            "This will be removed in v1.0.0.",
            DeprecationWarning,
            stacklevel=2,
        )

        if stack_options is not None:
            # Temporarily override stack_options for this call
            saved = self._stack_options
            self._stack_options = stack_options
            try:
                await self._ensure_infrastructure_internal()
            finally:
                self._stack_options = saved
        else:
            await self._ensure_infrastructure_internal()

    # -------------------------------------------------------------------------
    # Namespace registry (minimal, inline — #369 adds full CRUD + CLI)
    # -------------------------------------------------------------------------

    async def _register_namespace(self, name: str) -> str:
        """Register a namespace (idempotent).

        Creates two records under RESERVED_NAMESPACE:
        - ``PK=_/SYSTEM#, SK=#NAMESPACE#{name}`` (name → ID lookup)
        - ``PK=_/SYSTEM#, SK=#NSID#{id}`` (ID → name lookup)

        Uses TransactWriteItems with ConditionExpression to ensure atomicity.
        On TransactionCanceledException (namespace exists), resolves and returns
        the existing ID.

        Returns:
            The namespace_id (either newly created or existing).
        """
        import secrets

        client = await self._get_client()
        namespace_id = secrets.token_urlsafe(8)
        while namespace_id.startswith("-"):
            namespace_id = secrets.token_urlsafe(8)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        pk = schema.pk_system(schema.RESERVED_NAMESPACE)

        try:
            await client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.table_name,
                            "Item": {
                                "PK": {"S": pk},
                                "SK": {"S": schema.sk_namespace(name)},
                                "namespace_id": {"S": namespace_id},
                                "namespace_name": {"S": name},
                                "status": {"S": "active"},
                                "created_at": {"S": now},
                                # GSI4: co-located under reserved namespace
                                "GSI4PK": {"S": schema.RESERVED_NAMESPACE},
                                "GSI4SK": {"S": pk},
                            },
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.table_name,
                            "Item": {
                                "PK": {"S": pk},
                                "SK": {"S": schema.sk_nsid(namespace_id)},
                                "namespace_id": {"S": namespace_id},
                                "namespace_name": {"S": name},
                                "status": {"S": "active"},
                                "created_at": {"S": now},
                                # GSI4: co-located under reserved namespace
                                "GSI4PK": {"S": schema.RESERVED_NAMESPACE},
                                "GSI4SK": {"S": pk},
                            },
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                ]
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "TransactionCanceledException":
                # Namespace already exists — resolve the existing ID
                existing_id = await self._resolve_namespace(name)
                if existing_id is not None:
                    return existing_id
                # Should not happen: transaction failed but record not found
                raise  # pragma: no cover
            raise

        # Cache the resolved namespace
        self._namespace_cache[name] = namespace_id
        return namespace_id

    async def _resolve_namespace(self, name: str) -> str | None:
        """Resolve a namespace name to its opaque ID.

        Returns None if the namespace doesn't exist or has status "deleted".
        """
        # Check cache first
        if name in self._namespace_cache:
            return self._namespace_cache[name]

        client = await self._get_client()
        pk = schema.pk_system(schema.RESERVED_NAMESPACE)

        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": pk},
                "SK": {"S": schema.sk_namespace(name)},
            },
        )

        item = response.get("Item")
        if not item:
            return None

        status = item.get("status", {}).get("S", "")
        if status in ("deleted", "purging"):
            return None

        namespace_id: str = item["namespace_id"]["S"]
        self._namespace_cache[name] = namespace_id
        return namespace_id

    # -------------------------------------------------------------------------
    # Namespace registry — public API (#369)
    # -------------------------------------------------------------------------

    async def register_namespace(self, namespace: str) -> str:
        """Register a namespace in the registry (idempotent).

        Creates forward (name -> ID) and reverse (ID -> name) mappings.
        Idempotent: returns existing ID if namespace already registered.

        Args:
            namespace: Namespace name to register.

        Returns:
            The namespace_id (either newly created or existing).

        Raises:
            ValidationError: If namespace is the reserved namespace ``"_"``.
        """
        if namespace == schema.RESERVED_NAMESPACE:
            raise ValidationError("namespace", namespace, "reserved for system use")
        return await self._register_namespace(namespace)

    async def register_namespaces(self, namespaces: list[str]) -> dict[str, str]:
        """Bulk-register multiple namespaces.

        Registers all namespaces in DynamoDB (forward + reverse records each).

        Args:
            namespaces: List of namespace names to register.

        Returns:
            Mapping of ``{name: namespace_id}`` for all namespaces.

        Raises:
            ValidationError: If any namespace is the reserved namespace ``"_"``.
        """
        # Validate all names upfront (fail fast)
        for ns in namespaces:
            if ns == schema.RESERVED_NAMESPACE:
                raise ValidationError("namespace", ns, "reserved for system use")

        ids = await asyncio.gather(*[self._register_namespace(ns) for ns in namespaces])
        return dict(zip(namespaces, ids))

    async def get_namespace(self, namespace: str) -> dict[str, str] | None:
        """Get details for a single namespace by name.

        Args:
            namespace: Namespace name to look up.

        Returns:
            Dict with ``{name, namespace_id, status, created_at}``
            or ``None`` if not found.
        """
        client = await self._get_client()
        pk = schema.pk_system(schema.RESERVED_NAMESPACE)

        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": pk},
                "SK": {"S": schema.sk_namespace(namespace)},
            },
        )
        item = response.get("Item")
        if not item:
            return None

        return {
            "name": namespace,
            "namespace_id": item["namespace_id"]["S"],
            "status": item.get("status", {}).get("S", "unknown"),
            "created_at": item.get("created_at", {}).get("S", "unknown"),
        }

    async def list_namespaces(self) -> list[dict[str, str]]:
        """List all active namespaces with their IDs.

        Performs a Query on ``PK = "_/SYSTEM#"`` with
        ``SK begins_with "#NAMESPACE#"`` (forward records only).

        Returns:
            List of ``{name, namespace_id, created_at}`` dicts.
        """
        client = await self._get_client()
        pk = schema.pk_system(schema.RESERVED_NAMESPACE)

        results: list[dict[str, str]] = []
        exclusive_start_key = None

        while True:
            query_params: dict[str, Any] = {
                "TableName": self.table_name,
                "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk_prefix)",
                "ExpressionAttributeValues": {
                    ":pk": {"S": pk},
                    ":sk_prefix": {"S": schema.sk_namespace_prefix()},
                },
            }
            if exclusive_start_key is not None:
                query_params["ExclusiveStartKey"] = exclusive_start_key

            response = await client.query(**query_params)

            for item in response.get("Items", []):
                results.append(
                    {
                        "name": item["namespace_name"]["S"],
                        "namespace_id": item["namespace_id"]["S"],
                        "created_at": item.get("created_at", {}).get("S", ""),
                    }
                )

            if "LastEvaluatedKey" not in response:
                break
            exclusive_start_key = response["LastEvaluatedKey"]

        return results

    async def delete_namespace(self, namespace: str) -> None:
        """Soft-delete a namespace. O(1) for data plane.

        Removes the forward record and marks the reverse record as
        ``status="deleted"``.  Data items are NOT deleted — they remain
        orphaned under the namespace's random ID prefix.

        No-op if the namespace does not exist.

        Args:
            namespace: Namespace name to delete.

        Raises:
            ValidationError: If namespace is the reserved namespace ``"_"``.
        """
        if namespace == schema.RESERVED_NAMESPACE:
            raise ValidationError("namespace", namespace, "reserved for system use")

        client = await self._get_client()
        pk = schema.pk_system(schema.RESERVED_NAMESPACE)

        # Step 1: Read the forward record to get the namespace_id
        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": pk},
                "SK": {"S": schema.sk_namespace(namespace)},
            },
        )
        item = response.get("Item")
        if not item:
            return  # No-op if namespace does not exist

        namespace_id: str = item["namespace_id"]["S"]
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Step 2: Delete the forward record
        await client.delete_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": pk},
                "SK": {"S": schema.sk_namespace(namespace)},
            },
        )

        # Step 3: Update the reverse record — status="deleted", deleted_at
        await client.update_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": pk},
                "SK": {"S": schema.sk_nsid(namespace_id)},
            },
            UpdateExpression="SET #status = :deleted, deleted_at = :now",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":deleted": {"S": "deleted"},
                ":now": {"S": now},
            },
        )

        # Invalidate cache
        self._namespace_cache.pop(namespace, None)

    async def recover_namespace(self, namespace_id: str) -> str:
        """Recover a deleted namespace by its ID.

        Reads the reverse record to find the original name, re-creates
        the forward record, and marks the reverse record as active.

        Args:
            namespace_id: Opaque namespace ID to recover.

        Returns:
            The recovered namespace name.

        Raises:
            EntityNotFoundError: If the reverse record does not exist.
            ValidationError: If the namespace name is reserved.
            NamespaceStateError: If the namespace is active or being purged.
        """
        from .exceptions import EntityNotFoundError

        client = await self._get_client()
        pk = schema.pk_system(schema.RESERVED_NAMESPACE)

        # Step 1: Read the reverse record
        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": pk},
                "SK": {"S": schema.sk_nsid(namespace_id)},
            },
        )
        item = response.get("Item")
        if not item:
            raise EntityNotFoundError(namespace_id)

        status = item.get("status", {}).get("S", "")
        namespace_name: str = item["namespace_name"]["S"]

        if namespace_name == schema.RESERVED_NAMESPACE:
            raise ValidationError("namespace", namespace_name, "reserved for system use")

        if status == "purging":
            raise NamespaceStateError(
                f"Cannot recover namespace '{namespace_name}' — purge is in progress "
                f"and is terminal",
                namespace_name=namespace_name,
                state=status,
            )

        if status == "active":
            raise NamespaceStateError(
                f"Namespace '{namespace_name}' is already active (not deleted)",
                namespace_name=namespace_name,
                state=status,
            )

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        created_at = item.get("created_at", {}).get("S", now)

        # Step 2: Re-create the forward record
        try:
            await client.put_item(
                TableName=self.table_name,
                Item={
                    "PK": {"S": pk},
                    "SK": {"S": schema.sk_namespace(namespace_name)},
                    "namespace_id": {"S": namespace_id},
                    "namespace_name": {"S": namespace_name},
                    "status": {"S": "active"},
                    "created_at": {"S": created_at},
                    "GSI4PK": {"S": schema.RESERVED_NAMESPACE},
                    "GSI4SK": {"S": pk},
                },
                ConditionExpression="attribute_not_exists(PK)",
            )
        except client.exceptions.ConditionalCheckFailedException:
            raise NamespaceStateError(
                f"Cannot recover namespace '{namespace_name}' — "
                f"the name has been re-registered by another caller",
                namespace_name=namespace_name,
                state="re-registered",
            ) from None

        # Step 3: Update the reverse record — status="active", remove deleted_at
        await client.update_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": pk},
                "SK": {"S": schema.sk_nsid(namespace_id)},
            },
            UpdateExpression="SET #status = :active REMOVE deleted_at",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":active": {"S": "active"},
            },
        )

        # Update cache
        self._namespace_cache[namespace_name] = namespace_id
        return namespace_name

    async def list_orphan_namespaces(self) -> list[dict[str, str]]:
        """List deleted namespaces with orphaned data.

        Queries reverse records (``SK begins_with "#NSID#"``) and
        filters for ``status="deleted"``.

        Returns:
            List of ``{namespace_id, namespace, deleted_at}`` dicts.
        """
        client = await self._get_client()
        pk = schema.pk_system(schema.RESERVED_NAMESPACE)

        results: list[dict[str, str]] = []
        exclusive_start_key = None

        while True:
            query_params: dict[str, Any] = {
                "TableName": self.table_name,
                "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk_prefix)",
                "ExpressionAttributeValues": {
                    ":pk": {"S": pk},
                    ":sk_prefix": {"S": schema.sk_nsid_prefix()},
                },
            }
            if exclusive_start_key is not None:
                query_params["ExclusiveStartKey"] = exclusive_start_key

            response = await client.query(**query_params)

            for item in response.get("Items", []):
                status = item.get("status", {}).get("S", "")
                if status == "deleted":
                    results.append(
                        {
                            "namespace_id": item["namespace_id"]["S"],
                            "namespace": item["namespace_name"]["S"],
                            "deleted_at": item.get("deleted_at", {}).get("S", ""),
                        }
                    )

            if "LastEvaluatedKey" not in response:
                break
            exclusive_start_key = response["LastEvaluatedKey"]

        return results

    async def purge_namespace(self, namespace_id: str) -> None:
        """Purge all orphaned data for a deleted namespace.

        Verifies the namespace is in ``"deleted"`` status, transitions to
        ``"purging"``, queries GSI4 to find all items, deletes them in
        batches, then removes the reverse record.

        Safe to call on a non-existent namespace_id (no-op).

        Args:
            namespace_id: Opaque namespace ID to purge.

        Raises:
            NamespaceStateError: If the namespace is ``"active"`` (cannot purge
                an active namespace).
        """
        client = await self._get_client()
        pk = schema.pk_system(schema.RESERVED_NAMESPACE)

        # Step 1: Read the reverse record
        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": pk},
                "SK": {"S": schema.sk_nsid(namespace_id)},
            },
        )
        item = response.get("Item")
        if not item:
            return  # No-op for non-existent namespace_id

        status = item.get("status", {}).get("S", "")
        if status == "active":
            ns_name = item["namespace_name"]["S"]
            raise NamespaceStateError(
                f"Cannot purge active namespace '{ns_name}'. "
                f"Delete it first with delete_namespace().",
                namespace_name=ns_name,
                state=status,
            )

        # Step 2: Set status="purging" (prevents concurrent recovery)
        if status != "purging":
            await client.update_item(
                TableName=self.table_name,
                Key={
                    "PK": {"S": pk},
                    "SK": {"S": schema.sk_nsid(namespace_id)},
                },
                UpdateExpression="SET #status = :purging",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":purging": {"S": "purging"},
                },
            )

        # Step 3: Query GSI4 for all items belonging to the namespace
        exclusive_start_key = None
        while True:
            query_params: dict[str, Any] = {
                "TableName": self.table_name,
                "IndexName": schema.GSI4_NAME,
                "KeyConditionExpression": "GSI4PK = :pk",
                "ExpressionAttributeValues": {
                    ":pk": {"S": namespace_id},
                },
            }
            if exclusive_start_key is not None:
                query_params["ExclusiveStartKey"] = exclusive_start_key

            response = await client.query(**query_params)
            items = response.get("Items", [])

            # Step 4: Batch delete items (25 per batch), retrying unprocessed
            if items:
                delete_requests = [
                    {"DeleteRequest": {"Key": {"PK": i["PK"], "SK": i["SK"]}}} for i in items
                ]
                for batch_start in range(0, len(delete_requests), 25):
                    chunk = delete_requests[batch_start : batch_start + 25]
                    unprocessed: list[Any] = chunk
                    while unprocessed:
                        resp = await client.batch_write_item(
                            RequestItems={self.table_name: unprocessed}
                        )
                        unprocessed = resp.get("UnprocessedItems", {}).get(self.table_name, [])

            if "LastEvaluatedKey" not in response:
                break
            exclusive_start_key = response["LastEvaluatedKey"]

        # Step 5: Delete the reverse record
        await client.delete_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": pk},
                "SK": {"S": schema.sk_nsid(namespace_id)},
            },
        )

    def _reinitialize_config_cache(self, namespace_id: str) -> None:
        """Reinitialize the config cache with a new namespace_id."""
        self._config_cache = ConfigCache(
            ttl_seconds=self._config_cache_ttl, namespace_id=namespace_id
        )

    # -------------------------------------------------------------------------
    # Version management (used by builder; replaces limiter-level version check)
    # -------------------------------------------------------------------------

    async def _check_and_update_version_auto(self) -> None:
        """Check version compatibility and auto-update Lambda if needed.

        Used when ``auto_update=True``. On version mismatch, deploys
        updated Lambda code and updates the version record. On schema
        migration needed, raises ``IncompatibleSchemaError``.
        """
        from . import __version__
        from .version import InfrastructureVersion, check_compatibility

        version_record = await self.get_version_record()

        if version_record is None:
            await self._initialize_version_record()
            return

        infra_version = InfrastructureVersion.from_record(version_record)
        compatibility = check_compatibility(__version__, infra_version)

        if compatibility.is_compatible and not compatibility.requires_lambda_update:
            return

        if compatibility.requires_schema_migration:
            from .exceptions import IncompatibleSchemaError

            raise IncompatibleSchemaError(
                client_version=__version__,
                schema_version=infra_version.schema_version,
                message=compatibility.message,
            )

        if compatibility.requires_lambda_update:
            await self._perform_lambda_update()

    async def _check_version_strict(self, *, initialize_if_missing: bool = True) -> None:
        """Check version compatibility in strict mode (no auto-update).

        Raises ``VersionMismatchError`` if the Lambda version differs
        from the client version.

        Args:
            initialize_if_missing: When True (default), write the version
                record if it is absent. When False, raise
                ``InfrastructureNotFoundError`` instead — used by
                ``connect()``, which must not write to externally
                managed infrastructure.
        """
        from . import __version__
        from .exceptions import InfrastructureNotFoundError, VersionMismatchError
        from .version import InfrastructureVersion, check_compatibility

        version_record = await self.get_version_record()

        if version_record is None:
            if not initialize_if_missing:
                raise InfrastructureNotFoundError(self.stack_name)
            await self._initialize_version_record()
            return

        infra_version = InfrastructureVersion.from_record(version_record)
        compatibility = check_compatibility(__version__, infra_version)

        if compatibility.is_compatible and not compatibility.requires_lambda_update:
            return

        if compatibility.requires_schema_migration:
            from .exceptions import IncompatibleSchemaError

            raise IncompatibleSchemaError(
                client_version=__version__,
                schema_version=infra_version.schema_version,
                message=compatibility.message,
            )

        if compatibility.requires_lambda_update:
            raise VersionMismatchError(
                client_version=__version__,
                schema_version=infra_version.schema_version,
                lambda_version=infra_version.lambda_version,
                message=compatibility.message,
                can_auto_update=True,
            )

    async def _initialize_version_record(self) -> None:
        """Initialize the version record for first-time setup."""
        from . import __version__
        from .version import get_schema_version

        await self.set_version_record(
            schema_version=get_schema_version(),
            lambda_version=__version__,
            client_min_version="0.0.0",
            updated_by=f"client:{__version__}",
        )

    async def _perform_lambda_update(self) -> None:
        """Update Lambda code to match client version."""
        from . import __version__
        from .infra.stack_manager import StackManager
        from .version import get_schema_version

        async with StackManager(
            self.stack_name,
            self.region,
            self.endpoint_url,
        ) as manager:
            await manager.deploy_lambda_code()
            await manager.deploy_provisioner_code()

            await self.set_version_record(
                schema_version=get_schema_version(),
                lambda_version=__version__,
                client_min_version="0.0.0",
                updated_by=f"client:{__version__}",
            )

    # -------------------------------------------------------------------------
    # Entity operations
    # -------------------------------------------------------------------------

    async def create_entity(
        self,
        entity_id: str,
        name: str | None = None,
        parent_id: str | None = None,
        cascade: bool = False,
        metadata: dict[str, str] | None = None,
        principal: str | None = None,
    ) -> Entity:
        """
        Create a new entity.

        Args:
            entity_id: Unique identifier for the entity
            name: Optional display name (defaults to entity_id)
            parent_id: Optional parent entity ID (for hierarchical limits)
            cascade: If True, acquire() will also consume from parent entity
            metadata: Optional key-value metadata
            principal: Caller identity for audit logging

        Returns:
            The created Entity

        Raises:
            InvalidIdentifierError: If entity_id or parent_id is invalid
            EntityExistsError: If entity already exists
        """
        # Validate inputs at API boundary
        validate_identifier(entity_id, "entity_id")
        if parent_id is not None:
            validate_identifier(parent_id, "parent_id")

        client = await self._get_client()
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        item: dict[str, Any] = {
            "PK": {"S": schema.pk_entity(self._namespace_id, entity_id)},
            "SK": {"S": schema.sk_meta()},
            "entity_id": {"S": entity_id},
            "name": {"S": name or entity_id},
            "parent_id": {"S": parent_id} if parent_id else {"NULL": True},
            "cascade": {"BOOL": cascade},
            "metadata": {"M": self._serialize_map(metadata or {})},
            "created_at": {"S": now},
            # GSI4: namespace-scoped item discovery
            "GSI4PK": {"S": self._namespace_id},
            "GSI4SK": {"S": schema.pk_entity(self._namespace_id, entity_id)},
        }

        # Add GSI1 keys for parent lookup if this is a child
        if parent_id:
            item["GSI1PK"] = {"S": schema.gsi1_pk_parent(self._namespace_id, parent_id)}
            item["GSI1SK"] = {"S": schema.gsi1_sk_child(entity_id)}

        try:
            await client.put_item(
                TableName=self.table_name,
                Item=item,
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise EntityExistsError(entity_id)
            raise

        # Log audit event
        await self._log_audit_event(
            action=AuditAction.ENTITY_CREATED,
            entity_id=entity_id,
            principal=principal,
            details={
                "name": name or entity_id,
                "parent_id": parent_id,
                "cascade": cascade,
                "metadata": metadata or {},
            },
        )

        return Entity(
            id=entity_id,
            name=name or entity_id,
            parent_id=parent_id,
            cascade=cascade,
            metadata=metadata or {},
            created_at=now,
        )

    async def get_entity(self, entity_id: str) -> Entity | None:
        """Get an entity by ID."""
        client = await self._get_client()

        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_entity(self._namespace_id, entity_id)},
                "SK": {"S": schema.sk_meta()},
            },
        )

        item = response.get("Item")
        cache_key = (self._namespace_id, entity_id)
        existing_shards = self._entity_cache.get(cache_key, (False, None, {}))[2]
        if not item:
            self._entity_cache[cache_key] = (False, None, existing_shards)
            return None

        entity = self._deserialize_entity(item)
        self._entity_cache[cache_key] = (entity.cascade, entity.parent_id, existing_shards)
        return entity

    async def delete_entity(
        self,
        entity_id: str,
        principal: str | None = None,
    ) -> None:
        """
        Delete an entity and all its related records.

        Args:
            entity_id: ID of the entity to delete
            principal: Caller identity for audit logging
        """
        client = await self._get_client()

        # Query entity items (metadata, config, usage, audit)
        response = await client.query(
            TableName=self.table_name,
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={
                ":pk": {"S": schema.pk_entity(self._namespace_id, entity_id)}
            },
        )
        items = response.get("Items", [])

        # Query bucket items via GSI3 (pre-shard buckets have separate PKs)
        gsi3_response = await client.query(
            TableName=self.table_name,
            IndexName="GSI3",
            KeyConditionExpression="GSI3PK = :gsi3pk",
            ExpressionAttributeValues={
                ":gsi3pk": {"S": schema.gsi3_pk_entity(self._namespace_id, entity_id)},
            },
        )
        bucket_items = gsi3_response.get("Items", [])

        all_items = items + bucket_items
        if not all_items:
            return

        # Build delete requests
        delete_requests = [
            {"DeleteRequest": {"Key": {"PK": item["PK"], "SK": item["SK"]}}} for item in all_items
        ]

        # BatchWriteItem in chunks of 25
        for i in range(0, len(delete_requests), 25):
            chunk = delete_requests[i : i + 25]
            await client.batch_write_item(RequestItems={self.table_name: chunk})

        # Log audit event
        await self._log_audit_event(
            action=AuditAction.ENTITY_DELETED,
            entity_id=entity_id,
            principal=principal,
            details={"records_deleted": len(all_items)},
        )

    async def get_children(self, parent_id: str) -> list[Entity]:
        """Get all children of a parent entity."""
        client = await self._get_client()

        response = await client.query(
            TableName=self.table_name,
            IndexName=schema.GSI1_NAME,
            KeyConditionExpression="GSI1PK = :pk",
            ExpressionAttributeValues={
                ":pk": {"S": schema.gsi1_pk_parent(self._namespace_id, parent_id)}
            },
        )

        entities = []
        for item in response.get("Items", []):
            entity = self._deserialize_entity(item)
            if entity:
                entities.append(entity)

        return entities

    # -------------------------------------------------------------------------
    # Bucket operations
    # -------------------------------------------------------------------------

    async def get_bucket(
        self,
        entity_id: str,
        resource: str,
        limit_name: str,
        shard_id: int = 0,
    ) -> BucketState | None:
        """Get a single limit's bucket from the composite item."""
        client = await self._get_client()

        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(self._namespace_id, entity_id, resource, shard_id)},
                "SK": {"S": schema.sk_state()},
            },
        )

        item = response.get("Item")
        if not item:
            return None

        buckets = self._deserialize_composite_bucket(item)
        for b in buckets:
            if b.limit_name == limit_name:
                return b
        return None

    async def _batch_get_all(
        self,
        keys: list[dict[str, Any]],
        *,
        consistent_read: bool = False,
        context: str,
        entity_id: str | None = None,
        resource: str | None = None,
    ) -> list[dict[str, Any]]:
        """BatchGetItem that retries UnprocessedKeys and never returns a partial read.

        DynamoDB may answer a BatchGetItem partially and report the remainder
        in ``UnprocessedKeys`` -- documented behaviour under throttling or
        when the response would exceed 16 MB, not an error, and boto3 does not
        retry it for you. A withheld item is indistinguishable from an absent
        one, so accepting a partial response is silently wrong at every call
        site: the config precedence walk falls through to a more permissive
        level, the ADR-125 disable walk admits a disabled entity, and the
        ``acquire()`` slow path treats a live bucket as new.

        Raises rather than returning a short list when the retry budget runs
        out. For ``acquire()`` paths that routes the decision into the
        existing ``on_unavailable`` handling, which is what it is for.

        Chunking stays with the caller: this handles the unprocessed remainder
        *within* one request, so ``keys`` must already be <= 100 items.

        Args:
            keys: DynamoDB key dicts for a single request (at most 100).
            consistent_read: Passed through to the request.
            context: Short description of the read, used in the error message.
            entity_id: Attached to the raised exception when applicable.
            resource: Attached to the raised exception when applicable.

        Returns:
            Every item that exists among ``keys``, in no particular order.

        Raises:
            RateLimiterUnavailable: Keys remained unprocessed after
                ``_BATCH_GET_MAX_RETRIES`` retries.
        """
        client = await self._get_client()
        items: list[dict[str, Any]] = []
        pending = keys
        attempts = 0
        while pending:
            response = await client.batch_get_item(
                RequestItems={self.table_name: {"Keys": pending, "ConsistentRead": consistent_read}}
            )
            items.extend(response.get("Responses", {}).get(self.table_name, []))

            unprocessed = response.get("UnprocessedKeys", {}).get(self.table_name) or {}
            pending = unprocessed.get("Keys", [])
            if not pending:
                break

            attempts += 1
            if attempts > _BATCH_GET_MAX_RETRIES:
                raise RateLimiterUnavailable(
                    f"Could not read {context}: DynamoDB left {len(pending)} key(s) "
                    f"unprocessed after {_BATCH_GET_MAX_RETRIES} retries",
                    stack_name=self.stack_name,
                    entity_id=entity_id,
                    resource=resource,
                )
            await asyncio.sleep(_BATCH_GET_RETRY_BASE_DELAY * 2 ** (attempts - 1))

        return items

    async def get_buckets(
        self,
        entity_id: str,
        resource: str | None = None,
        shard_id: int = 0,
    ) -> list[BucketState]:
        """Get all buckets for an entity, optionally filtered by resource.

        With pre-shard buckets (v0.9.0+), each item lives on its own partition
        key ``PK={ns}/BUCKET#{id}#{resource}#{shard}``. When resource is
        specified, fetches the single bucket at the given shard_id. When
        resource is None, uses GSI3 (KEYS_ONLY) to discover all bucket PKs,
        then BatchGetItem to fetch full items.

        The internal ``wcu`` infrastructure limit is filtered from the
        returned bucket states.

        Args:
            entity_id: Entity to query buckets for.
            resource: Resource name filter, or None for all resources.
            shard_id: Shard index (used only when resource is specified).

        Returns:
            List of BucketState objects (one per application limit).
        """
        client = await self._get_client()

        if resource:
            # Single composite item for this entity+resource+shard
            response = await client.get_item(
                TableName=self.table_name,
                Key={
                    "PK": {
                        "S": schema.pk_bucket(self._namespace_id, entity_id, resource, shard_id)
                    },
                    "SK": {"S": schema.sk_state()},
                },
            )
            item = response.get("Item")
            if not item:
                return []
            return [
                b
                for b in self._deserialize_composite_bucket(item)
                if b.limit_name != schema.WCU_LIMIT_NAME
            ]

        # Step 1: GSI3 query to discover bucket PKs (KEYS_ONLY projection)
        key_condition = "GSI3PK = :gsi3pk"
        expression_values: dict[str, Any] = {
            ":gsi3pk": {"S": schema.gsi3_pk_entity(self._namespace_id, entity_id)},
        }

        response = await client.query(
            TableName=self.table_name,
            IndexName="GSI3",
            KeyConditionExpression=key_condition,
            ExpressionAttributeValues=expression_values,
        )

        gsi3_items = response.get("Items", [])
        if not gsi3_items:
            return []

        # Step 2: BatchGetItem to fetch full items from main table
        request_keys = [{"PK": item["PK"], "SK": item["SK"]} for item in gsi3_items]

        buckets: list[BucketState] = []
        for i in range(0, len(request_keys), 100):
            chunk = request_keys[i : i + 100]
            full_items = await self._batch_get_all(
                chunk,
                context=f"buckets for entity {entity_id!r}",
                entity_id=entity_id,
            )
            for full_item in full_items:
                buckets.extend(self._deserialize_composite_bucket(full_item))
        return [b for b in buckets if b.limit_name != schema.WCU_LIMIT_NAME]

    async def batch_get_buckets(
        self,
        keys: list[tuple[str, str, int]],
    ) -> dict[tuple[str, str, str], BucketState]:
        """
        Batch get composite buckets in a single DynamoDB call.

        With composite items, each (entity_id, resource, shard) is a single
        DynamoDB item containing all limits. Returns individual BucketStates
        keyed by (entity_id, resource, limit_name) for backward compatibility.

        Args:
            keys: List of (entity_id, resource, shard_id) tuples. The shard
                is the one the acquire selected (issue #439); it is never
                assumed to be 0.

        Returns:
            Dict mapping (entity_id, resource, limit_name) to BucketState.
            Missing composite items are not included in the result.

        Note:
            DynamoDB BatchGetItem supports up to 100 items per request.
            For larger batches, this method automatically chunks the requests.
        """
        if not keys:
            return {}

        result: dict[tuple[str, str, str], BucketState] = {}

        # Build request keys (deduplicate)
        unique_keys = list(set(keys))

        # BatchGetItem supports max 100 items per request
        for i in range(0, len(unique_keys), 100):
            chunk = unique_keys[i : i + 100]

            request_keys = [
                {
                    "PK": {
                        "S": schema.pk_bucket(self._namespace_id, entity_id, resource, shard_id)
                    },
                    "SK": {"S": schema.sk_state()},
                }
                for entity_id, resource, shard_id in chunk
            ]

            items = await self._batch_get_all(request_keys, context="rate limit buckets")

            # Process responses — each item is a composite bucket
            for item in items:
                buckets = self._deserialize_composite_bucket(item)
                for bucket in buckets:
                    key = (bucket.entity_id, bucket.resource, bucket.limit_name)
                    result[key] = bucket

        return result

    async def batch_get_entity_and_buckets(
        self,
        entity_id: str,
        bucket_keys: list[tuple[str, str, int]],
    ) -> tuple[Entity | None, dict[tuple[str, str, str], BucketState]]:
        """
        Fetch entity metadata and composite buckets in a single BatchGetItem.

        With composite items, each (entity_id, resource, shard) is a single
        DynamoDB item. Includes the entity's #META record alongside bucket
        records to avoid a separate get_entity() round trip.

        Args:
            entity_id: Entity whose META record to include
            bucket_keys: List of (entity_id, resource, shard_id) for composite
                buckets — the shard the acquire selected (issue #439)

        Returns:
            Tuple of (entity_or_none, bucket_dict) where bucket_dict maps
            (entity_id, resource, limit_name) to BucketState.

        Note:
            DynamoDB BatchGetItem supports up to 100 items per request.
            The META key counts toward that limit.
        """
        # Build all keys: META key + composite bucket keys
        meta_key = {
            "PK": {"S": schema.pk_entity(self._namespace_id, entity_id)},
            "SK": {"S": schema.sk_meta()},
        }

        request_keys = [meta_key]
        unique_bucket_keys = list(set(bucket_keys))
        for eid, resource, shard_id in unique_bucket_keys:
            request_keys.append(
                {
                    "PK": {"S": schema.pk_bucket(self._namespace_id, eid, resource, shard_id)},
                    "SK": {"S": schema.sk_state()},
                }
            )

        entity: Entity | None = None
        buckets: dict[tuple[str, str, str], BucketState] = {}

        # BatchGetItem in chunks of 100
        for i in range(0, len(request_keys), 100):
            chunk = request_keys[i : i + 100]

            items = await self._batch_get_all(
                chunk,
                context=f"entity {entity_id!r} and its buckets",
                entity_id=entity_id,
            )
            for item in items:
                sk = item.get("SK", {}).get("S", "")
                if sk == schema.sk_meta():
                    entity = self._deserialize_entity(item)
                elif sk == schema.sk_state():
                    for bucket in self._deserialize_composite_bucket(item):
                        key = (bucket.entity_id, bucket.resource, bucket.limit_name)
                        buckets[key] = bucket

        # Populate entity cache transparently (issue #318)
        cache_key = (self._namespace_id, entity_id)
        existing_shards = self._entity_cache.get(cache_key, (False, None, {}))[2]
        if entity is not None:
            self._entity_cache[cache_key] = (entity.cascade, entity.parent_id, existing_shards)
        else:
            self._entity_cache[cache_key] = (False, None, existing_shards)

        return entity, buckets

    async def batch_get_configs(
        self,
        keys: list[tuple[str, str]],
        disabled_out: dict[tuple[str, str], bool | None] | None = None,
    ) -> dict[tuple[str, str], tuple[list[Limit], OnUnavailableAction | None]]:
        """
        Batch get config items in a single DynamoDB call.

        Fetches config records (entity, resource, system level) in a single
        BatchGetItem request and returns deserialized limits.

        Args:
            keys: List of (PK, SK) tuples identifying config items
            disabled_out: Optional dict to receive the tri-state `disabled`
                value of every key actually requested here, so a caller that
                also needs the disable walk can reuse this read instead of
                issuing an identical second BatchGetItem (ADR-125). Keys that
                resolve to no item are recorded as None ("no explicit value"),
                which is exactly what the walk needs — a requested-but-absent
                level is still a *fresh* answer. Only keys present in this dict
                may be reused; anything served from the config cache must not
                be, since caching the gate would let a first acquire with no
                bucket yet be admitted to a disabled resource permanently.

        Returns:
            Dict mapping (PK, SK) to (limits, on_unavailable) tuples.
            on_unavailable is extracted from system config items (None for others).
            Missing items are not included in the result.

        Note:
            DynamoDB BatchGetItem supports up to 100 items per request.
            For larger batches, this method automatically chunks the requests.
            Uses eventually consistent reads (0.5 RCU per item).
        """
        if not keys:
            return {}

        result: dict[tuple[str, str], tuple[list[Limit], OnUnavailableAction | None]] = {}

        # Deduplicate keys
        unique_keys = list(set(keys))

        # BatchGetItem supports max 100 items per request
        for i in range(0, len(unique_keys), 100):
            chunk = unique_keys[i : i + 100]

            request_keys = [
                {
                    "PK": {"S": pk},
                    "SK": {"S": sk},
                }
                for pk, sk in chunk
            ]

            items = await self._batch_get_all(request_keys, context="limit configuration")

            # _batch_get_all never returns a partial read, so every key in this
            # chunk was genuinely read and each one gets a fresh `disabled`
            # answer — absent item included (None = no explicit value at that
            # level).
            if disabled_out is not None:
                for pk, sk in chunk:
                    disabled_out[(pk, sk)] = None

            # Process responses: deserialize each item
            for item in items:
                pk = item.get("PK", {}).get("S", "")
                sk = item.get("SK", {}).get("S", "")
                if pk and sk:
                    limits = self._deserialize_composite_limits(item)
                    ou_attr = item.get("on_unavailable", {})
                    ou_str = ou_attr.get("S") if ou_attr else None
                    on_unavailable: OnUnavailableAction | None = (
                        cast(OnUnavailableAction, ou_str) if ou_str else None
                    )
                    result[(pk, sk)] = (limits, on_unavailable)
                    if disabled_out is not None:
                        disabled_out[(pk, sk)] = schema.decode_disabled(item)

        return result

    async def get_or_create_bucket(
        self,
        entity_id: str,
        resource: str,
        limit: Limit,
    ) -> BucketState:
        """
        Get an existing bucket or create a new one with the given limit.

        If the bucket exists, it is returned. If not, a new bucket is created
        with capacity set to the limit's capacity.

        Args:
            entity_id: Entity owning the bucket
            resource: Resource name (e.g., "gpt-4")
            limit: Limit configuration for the bucket

        Returns:
            Existing or newly created BucketState
        """
        existing = await self.get_bucket(entity_id, resource, limit.name)
        if existing is not None:
            return existing

        # Create new bucket at full capacity
        now_ms = self._now_ms()
        state = BucketState(
            entity_id=entity_id,
            resource=resource,
            limit_name=limit.name,
            tokens_milli=limit.capacity * 1000,
            last_refill_ms=now_ms,
            capacity_milli=limit.capacity * 1000,
            refill_amount_milli=limit.refill_amount * 1000,
            refill_period_ms=limit.refill_period_seconds * 1000,
            total_consumed_milli=0,
        )

        # Write bucket to DynamoDB
        put_item = self.build_bucket_put_item(state)
        await self.transact_write([put_item])

        return state

    def build_bucket_put_item(
        self,
        state: BucketState,
        ttl_seconds: int = 86400,
    ) -> dict[str, Any]:
        """Build a PutItem for a composite bucket (for use in transactions).

        Wraps build_composite_create for backward compatibility with protocol.
        """
        now_ms = self._now_ms()
        return self.build_composite_create(
            entity_id=state.entity_id,
            resource=state.resource,
            states=[state],
            now_ms=now_ms,
            ttl_seconds=ttl_seconds,
        )

    def build_bucket_update_item(
        self,
        entity_id: str,
        resource: str,
        limit_name: str,
        new_tokens_milli: int,
        new_last_refill_ms: int,
        expected_tokens_milli: int | None = None,
        shard_id: int = 0,
    ) -> dict[str, Any]:
        """Build an UpdateItem for a single limit in a composite bucket.

        Legacy method — prefer build_composite_normal/retry/adjust for
        composite writes. This updates one limit's tk within the composite item.
        """
        tk_attr = schema.bucket_attr(limit_name, schema.BUCKET_FIELD_TK)
        update: dict[str, dict[str, Any]] = {
            "Update": {
                "TableName": self.table_name,
                "Key": {
                    "PK": {
                        "S": schema.pk_bucket(self._namespace_id, entity_id, resource, shard_id)
                    },
                    "SK": {"S": schema.sk_state()},
                },
                "UpdateExpression": "SET #tokens = :tokens, #refill = :refill",
                "ExpressionAttributeNames": {
                    "#tokens": tk_attr,
                    "#refill": schema.BUCKET_FIELD_RF,
                },
                "ExpressionAttributeValues": {
                    ":tokens": {"N": str(new_tokens_milli)},
                    ":refill": {"N": str(new_last_refill_ms)},
                },
            }
        }

        # Add optimistic locking condition if provided
        if expected_tokens_milli is not None:
            update["Update"]["ConditionExpression"] = "#tokens = :expected"
            update["Update"]["ExpressionAttributeValues"][":expected"] = {
                "N": str(expected_tokens_milli)
            }

        return update

    # -------------------------------------------------------------------------
    # Composite bucket write paths (ADR-114, ADR-115)
    # -------------------------------------------------------------------------

    @staticmethod
    def _encode_one_tuple(
        named_schedules: list[tuple[str, tuple[schedule.ScheduleEntry, ...]]],
        encoder: Callable[[tuple[schedule.ScheduleEntry, ...]], tuple[str, str | None]],
    ) -> tuple[str, dict[str, str]] | None:
        """§4.1's item-level default plus per-limit overrides, for one tuple.

        Returns ``(default_compact, overrides)``, where ``overrides`` names
        only the limits whose encoding differs from the default, or ``None``
        when no limit on the item carries this kind of schedule. The timezone
        is resolved once for the whole item by the caller, not here.

        A limit with **no** schedule of this kind is an override too, spelled
        ``schema.BUCKET_SCHED_NONE`` (#541). Absence still means "inherit the
        item default", which is what keeps a shared schedule down to one
        attribute — but it can no longer *also* mean "unscheduled", because
        both readings applied to the same byte pattern and every reader picked
        the wrong one. The contamination ran both ways on a mixed item: a rate
        limit acquired the quota's midnight reset, and the quota acquired the
        rate limit's ``0.5x`` window.
        """
        scheduled = [(name, encoder(sched)[0]) for name, sched in named_schedules if sched]
        if not scheduled:
            return None
        # Order decides the item-level default, as it always has: a different
        # pick would relabel which limits need an override.
        default_compact = scheduled[0][1]
        encodings = dict(scheduled)
        overrides = {}
        for name, _sched in named_schedules:
            compact = encodings.get(name, schema.BUCKET_SCHED_NONE)
            if compact != default_compact:
                overrides[name] = compact
        return default_compact, overrides

    @classmethod
    def _encode_item_schedules(
        cls,
        named: list[
            tuple[str, tuple[schedule.ScheduleEntry, ...], tuple[schedule.ScheduleEntry, ...]]
        ],
    ) -> tuple[str, tuple[str, dict[str, str]] | None, tuple[str, dict[str, str]] | None] | None:
        """Encode **both** schedule tuples for one bucket item, under one zone.

        One encoder for both writers of these attributes — the bucket-create
        stamp (`_stamp_schedule`) and the `set_limits` fan-out
        (`_build_bucket_param_update`). They write into different shapes (a
        PutItem's item map vs an UpdateExpression's SET parts), but the
        *semantics* must not diverge: which schedule becomes the item-level
        default, which limits get an override, and what counts as a timezone
        conflict.

        The two tuples are resolved **together** rather than by two independent
        calls, because `sched_tz` is a single item-level attribute they share
        (§4.1). Two calls would each be internally consistent and jointly
        wrong — and on the fan-out, one of them would `SET` the attribute while
        the other `REMOVE`d it in the same expression, which is the #488
        `ValidationException`.

        Args:
            named: ``(limit_name, schedule, reset_schedule)`` for every limit
                on the item, scheduled or not. Order decides the item-level
                default, independently per tuple.

        Returns:
            ``(tz, param, reset)`` where each of ``param``/``reset`` is
            ``(default_compact, overrides)`` or ``None`` when no limit carries
            that kind. ``None`` overall when nothing on the item is scheduled
            at all — in which case there is no timezone to report either.

        Raises:
            ValueError: The scheduled limits disagree on a timezone. It is
                hoisted to a single item-level ``sched_tz``, so an item cannot
                carry two. ``set_limits()`` rejects this at config-write time
                (``hoisted_schedule_timezone``); the check is repeated here
                because an ``acquire(limits=[...])`` override reaches a bucket
                create without ever passing through a config write, and
                silently keeping the first limit's zone would reinterpret the
                second limit's cron in the wrong one.
        """
        zones = {
            entry.tz for _name, sched, reset in named for entry in (*(sched or ()), *(reset or ()))
        }
        if len(zones) > 1:
            raise ValueError(
                f"all scheduled limits on one bucket item must share a timezone, got "
                f"{sorted(zones)}. The timezone is stored once per item as "
                f"`sched_tz`, not per limit."
            )
        if not zones:
            return None

        param = cls._encode_one_tuple(
            [(name, sched or ()) for name, sched, _reset in named], schedule.encode
        )
        reset = cls._encode_one_tuple(
            [(name, reset or ()) for name, _sched, reset in named], schedule.encode_reset
        )
        return zones.pop(), param, reset

    def _stamp_schedule(self, item: dict[str, Any], states: list[BucketState]) -> None:
        """Write ``sched`` / ``rsched`` / ``sched_tz`` / overrides onto a new item.

        A fresh item carries no stale override to strip, so this is the SET
        half of what `_build_bucket_param_update` does; both go through
        `_encode_item_schedules` so the two cannot drift.
        """
        encoded = self._encode_item_schedules(
            [(s.limit_name, s.sched, s.reset_sched) for s in states]
        )
        if encoded is None:
            return
        tz, param, reset = encoded
        item[schema.BUCKET_FIELD_SCHED_TZ] = {"S": tz}
        for field, part in (
            (schema.BUCKET_FIELD_SCHED, param),
            (schema.BUCKET_FIELD_RSCHED, reset),
        ):
            if part is None:
                continue
            default_compact, overrides = part
            item[field] = {"S": default_compact}
            for name, compact in overrides.items():
                item[schema.bucket_attr(name, field)] = {"S": compact}

    def build_composite_create(
        self,
        entity_id: str,
        resource: str,
        states: list[BucketState],
        now_ms: int,
        ttl_seconds: int | None = 86400,
        cascade: bool = False,
        parent_id: str | None = None,
        shard_id: int = 0,
        shard_count: int = 1,
        vu: int | None = None,
        rf_ms: int | None = None,
    ) -> dict[str, Any]:
        """Build a PutItem for creating a new composite bucket.

        Used on first acquire for an entity+resource. Condition ensures no
        concurrent creation race (attribute_not_exists).

        Args:
            entity_id: Entity owning the bucket
            resource: Resource name
            states: BucketState objects for each limit
            now_ms: Current timestamp in milliseconds
            ttl_seconds: TTL in seconds from now, or None to omit TTL
            cascade: Whether the entity has cascade enabled
            parent_id: The entity's parent_id (if any)
            shard_id: Shard index for this bucket (default 0)
            shard_count: Total number of shards (default 1)
            vu: Valid-until stamp in epoch ms (#222 §2.1) — the earliest
                instant at which any limit on this item changes effective
                params. ``None`` omits the attribute, which the fast path
                reads as "no schedule, never expires".
            rf_ms: The ``rf`` to stamp, when the caller has clamped it above
                ``now_ms`` so that ``rf`` never sits below a window start the
                item carries (ADR-139). ``None`` stamps ``now_ms``.
        """
        item: dict[str, Any] = {
            "PK": {"S": schema.pk_bucket(self._namespace_id, entity_id, resource, shard_id)},
            "SK": {"S": schema.sk_state()},
            "entity_id": {"S": entity_id},
            "resource": {"S": resource},
            schema.BUCKET_FIELD_RF: {"N": str(now_ms if rf_ms is None else rf_ms)},
            "GSI2PK": {"S": schema.gsi2_pk_resource(self._namespace_id, resource)},
            "GSI2SK": {"S": schema.gsi2_sk_bucket(entity_id, shard_id)},
            "cascade": {"BOOL": cascade},
            # GSI3: bucket discovery by entity
            "GSI3PK": {"S": schema.gsi3_pk_entity(self._namespace_id, entity_id)},
            "GSI3SK": {"S": schema.gsi3_sk_bucket(resource, shard_id)},
            # GSI4: namespace-scoped item discovery
            "GSI4PK": {"S": self._namespace_id},
            "GSI4SK": {"S": schema.gsi4_sk_bucket(entity_id, resource, shard_id)},
            "shard_count": {"N": str(shard_count)},
        }
        if parent_id is not None:
            item["parent_id"] = {"S": parent_id}
        # Only add TTL if specified (None means no TTL for entity-level config)
        if ttl_seconds is not None:
            item["ttl"] = {"N": str(schema.calculate_ttl(now_ms, ttl_seconds))}

        if vu is not None:
            item[schema.BUCKET_FIELD_VU] = {"N": str(vu)}

        # Both schedules are stamped at bucket creation and re-stamped by the
        # `set_limits` fan-out (§2.2). Without it here, a bucket first seen by
        # the slow path inside a `0.5x` window would reach the aggregator
        # carrying `vu` but no `sched`: the aggregator reads the item and
        # nothing else, so it would refill toward the *base* ceiling and the
        # fast path would spend the surplus — the scheduled limit silently not
        # enforced until the next admin fan-out. `rsched` is the same argument
        # one step further: a quota bucket born without it is a bucket whose
        # `refill_amount` is 0 and which nothing ever refills.
        self._stamp_schedule(item, states)

        # Auto-inject wcu infrastructure limit
        wcu_cp_milli = schema.WCU_LIMIT_CAPACITY * 1000
        wcu_ra_milli = schema.WCU_LIMIT_REFILL_AMOUNT * 1000
        wcu_rp_ms = schema.WCU_LIMIT_REFILL_PERIOD_SECONDS * 1000
        wcu_name = schema.WCU_LIMIT_NAME
        item[schema.bucket_attr(wcu_name, schema.BUCKET_FIELD_TK)] = {
            "N": str(wcu_cp_milli),
        }
        item[schema.bucket_attr(wcu_name, schema.BUCKET_FIELD_CP)] = {
            "N": str(wcu_cp_milli),
        }
        item[schema.bucket_attr(wcu_name, schema.BUCKET_FIELD_RA)] = {
            "N": str(wcu_ra_milli),
        }
        item[schema.bucket_attr(wcu_name, schema.BUCKET_FIELD_RP)] = {
            "N": str(wcu_rp_ms),
        }
        item[schema.bucket_attr(wcu_name, schema.BUCKET_FIELD_TC)] = {"N": "0"}

        for state in states:
            name = state.limit_name
            item[schema.bucket_attr(name, schema.BUCKET_FIELD_TK)] = {
                "N": str(state.tokens_milli),
            }
            item[schema.bucket_attr(name, schema.BUCKET_FIELD_CP)] = {
                "N": str(state.capacity_milli),
            }
            item[schema.bucket_attr(name, schema.BUCKET_FIELD_RA)] = {
                "N": str(state.refill_amount_milli),
            }
            item[schema.bucket_attr(name, schema.BUCKET_FIELD_RP)] = {
                "N": str(state.refill_period_ms),
            }
            tc = state.total_consumed_milli if state.total_consumed_milli is not None else 0
            item[schema.bucket_attr(name, schema.BUCKET_FIELD_TC)] = {
                "N": str(tc),
            }
            # ADR-139 duration window. `wcu` is auto-injected above and never
            # reaches this loop, so it can never carry a window -- the
            # structural exemption ADR-139 gets for free where `rsched`
            # needed an explicit carve-out (processor.py). `rsa` is entity-
            # wide and never divided by shard_count; only the balance above
            # is.
            if state.reset_after_seconds is not None:
                item[schema.bucket_attr(name, schema.BUCKET_FIELD_RSA)] = {
                    "N": str(state.reset_after_seconds),
                }
            if state.window_start_ms is not None:
                item[schema.bucket_attr(name, schema.BUCKET_FIELD_WS)] = {
                    "N": str(state.window_start_ms),
                }

        return {
            "Put": {
                "TableName": self.table_name,
                "Item": item,
                "ConditionExpression": "attribute_not_exists(PK)",
            }
        }

    def build_composite_normal(
        self,
        entity_id: str,
        resource: str,
        consumed: dict[str, int],
        refill_amounts: dict[str, int],
        now_ms: int,
        expected_rf: int,
        ttl_seconds: int | None = None,
        shard_id: int = 0,
        vu: int | None = None,
        clear_vu: bool = False,
        windows: dict[str, tuple[int, int]] | None = None,
        rf_ms: int | None = None,
    ) -> dict[str, Any]:
        """Build an UpdateItem for the normal write path (ADR-115 path 2).

        ADD tk:(refill - consumed), tc:consumed for each limit.
        SET rf:now. CONDITION rf = :expected.

        Args:
            entity_id: Entity owning the bucket
            resource: Resource name
            consumed: Amount consumed per limit (millitokens)
            refill_amounts: Refill amount per limit (millitokens)
            now_ms: Current timestamp in milliseconds
            expected_rf: Expected refill timestamp for optimistic lock
            ttl_seconds: TTL behavior:
                - None: Don't change TTL
                - 0: REMOVE ttl (entity has custom limits)
                - >0: SET ttl to (now + ttl_seconds)
            shard_id: Shard index for this bucket (default 0)
            vu: Valid-until stamp in epoch ms (#222 §2.1), or ``None`` to
                leave the attribute untouched. ``None`` is not "no schedule":
                it means this pass has nothing to say about the boundary, so a
                `vu` already on the item survives.
            clear_vu: REMOVE `vu` instead of leaving it. Only meaningful with
                ``vu=None``, and only correct when the caller knows nothing on
                the item is scheduled — `_commit_initial()` does, because its
                group covers every limit sharing the item. This is the half of
                the #468 fan-out's `vu = 0` that makes it self-clearing rather
                than a permanent fast-path demotion.
            windows: Limit name -> ``(window_start_ms, reset_after_seconds)``
                to stamp as ``b_{name}_ws`` and ``b_{name}_rsa`` (ADR-139).
                Only limits whose window rolled on **this** pass appear; an
                empty dict or ``None`` leaves every `ws` untouched. This is the
                only client write that moves a window start — the speculative
                fast path stays byte-identical — so `_commit_initial()` is
                where anchoring is decided, which is what makes "only admitted
                use anchors" fall out rather than being enforced.

                The pair is one argument so that `ws` can never land without
                `rsa`. The aggregator and a new shard's inheritance read only
                the item, and a resource- or system-level `reset_after` never
                reaches an existing bucket through the param sync (#271/#296)
                — so an item holding `ws` alone would carry a window whose end
                nothing but a config-resolving client could compute.
            rf_ms: The ``rf`` to stamp, when the caller has clamped it so that
                it never moves backward and never sits below a window start
                the item carries (ADR-139). ``None`` stamps ``now_ms``. The
                lock still compares against ``expected_rf``, the stored value.
        """
        add_parts: list[str] = []
        set_parts: list[str] = ["#rf = :now"]
        remove_parts: list[str] = []
        attr_names: dict[str, str] = {"#rf": schema.BUCKET_FIELD_RF}
        attr_values: dict[str, Any] = {
            ":now": {"N": str(now_ms if rf_ms is None else rf_ms)},
            ":expected_rf": {"N": str(expected_rf)},
        }

        # Handle TTL
        if ttl_seconds is not None:
            attr_names["#ttl"] = "ttl"
            if ttl_seconds > 0:
                # SET ttl to new value
                set_parts.append("#ttl = :ttl_val")
                attr_values[":ttl_val"] = {"N": str(schema.calculate_ttl(now_ms, ttl_seconds))}
            else:
                # REMOVE ttl (entity has custom limits, should persist)
                remove_parts.append("#ttl")

        # This write is the materialisation the fast path's `vu` gate waits
        # on, so the same pass that moves `tk` and `rf` restamps the boundary
        # (#222 §2.1). `vu` is SET here and must therefore never join
        # `remove_parts`: SET and REMOVE on one attribute in a single
        # UpdateExpression is the ValidationException #488 hit.
        if vu is not None:
            set_parts.append("#vu = :vu")
            attr_names["#vu"] = schema.BUCKET_FIELD_VU
            attr_values[":vu"] = {"N": str(vu)}
        elif clear_vu:
            # Nothing on the item is scheduled, so there is no boundary to
            # gate on and the stamp must go. Without this an unscheduled
            # bucket that the #468 fan-out stamped `vu = 0` would fail the
            # fast-path condition on *every* future acquire — permanently
            # demoted to the 3-round-trip slow path, since the fan-out's
            # forced pass is the only thing that can clear it and this is that
            # pass. SET and REMOVE are mutually exclusive here by
            # construction, never both in one expression (#488).
            remove_parts.append("#vu")
            attr_names["#vu"] = schema.BUCKET_FIELD_VU

        # ADR-139 duration window rollover. Monotonic counters, not the limit
        # name, mirroring the #487 stale-limit REMOVE aliases (`#stale{i}_{j}`):
        # `NAME_PATTERN` allows `-` and `.` in a limit name, and an
        # `ExpressionAttributeNames` *value* may legally contain either --
        # what cannot is a raw name appearing as a path segment directly in
        # the `UpdateExpression` *text*, where `.` parses as a document-path
        # separator and `-` as subtraction. The alias (`#ws{i}`) sidesteps
        # that by keeping the expression text itself free of the raw name;
        # only the alias *value*, substituted by DynamoDB, carries it.
        # `sorted` only to keep the expression deterministic for tests.
        for i, (name, (ws, rsa)) in enumerate(sorted((windows or {}).items())):
            attr_names[f"#ws{i}"] = schema.bucket_attr(name, schema.BUCKET_FIELD_WS)
            attr_names[f"#rsa{i}"] = schema.bucket_attr(name, schema.BUCKET_FIELD_RSA)
            set_parts.append(f"#ws{i} = :ws{i}")
            set_parts.append(f"#rsa{i} = :rsa{i}")
            attr_values[f":ws{i}"] = {"N": str(ws)}
            attr_values[f":rsa{i}"] = {"N": str(rsa)}

        condition_parts: list[str] = ["#rf = :expected_rf"]

        for name in consumed:
            c = consumed[name]
            r = refill_amounts.get(name, 0)
            tk_delta = r - c  # refill minus consumption

            tk_alias = f"#b_{name}_tk"
            tc_alias = f"#b_{name}_tc"
            tk_val = f":b_{name}_tk_delta"
            tc_val = f":b_{name}_tc_delta"

            attr_names[tk_alias] = schema.bucket_attr(name, schema.BUCKET_FIELD_TK)
            attr_names[tc_alias] = schema.bucket_attr(name, schema.BUCKET_FIELD_TC)
            attr_values[tk_val] = {"N": str(tk_delta)}
            attr_values[tc_val] = {"N": str(c)}

            add_parts.append(f"{tk_alias} {tk_val}")
            add_parts.append(f"{tc_alias} {tc_val}")

            # Guard against concurrent speculative consumption draining tk.
            # Speculative writes modify tk without touching rf, so the rf lock
            # alone can't detect them. Ensure tk can absorb the net decrease.
            floor = max(0, c - r)
            if floor > 0:
                floor_val = f":b_{name}_tk_floor"
                attr_values[floor_val] = {"N": str(floor)}
                condition_parts.append(f"{tk_alias} >= {floor_val}")

        # Build update expression
        update_expr = f"SET {', '.join(set_parts)} ADD {', '.join(add_parts)}"
        if remove_parts:
            update_expr += f" REMOVE {', '.join(remove_parts)}"

        return {
            "Update": {
                "TableName": self.table_name,
                "Key": {
                    "PK": {
                        "S": schema.pk_bucket(self._namespace_id, entity_id, resource, shard_id)
                    },
                    "SK": {"S": schema.sk_state()},
                },
                "UpdateExpression": update_expr,
                "ConditionExpression": " AND ".join(condition_parts),
                "ExpressionAttributeNames": attr_names,
                "ExpressionAttributeValues": attr_values,
            }
        }

    def build_composite_retry(
        self,
        entity_id: str,
        resource: str,
        consumed: dict[str, int],
        shard_id: int = 0,
    ) -> dict[str, Any]:
        """Build an UpdateItem for the retry write path (ADR-115 path 3).

        Lost optimistic lock — skip refill, only consume.
        ADD tk:(-consumed), tc:consumed for each limit.
        CONDITION: tk >= consumed per limit (prevent negative on acquire).
        """
        add_parts: list[str] = []
        condition_parts: list[str] = []
        attr_names: dict[str, str] = {}
        attr_values: dict[str, Any] = {}

        for name in consumed:
            c = consumed[name]
            tk_alias = f"#b_{name}_tk"
            tc_alias = f"#b_{name}_tc"
            tk_neg_val = f":b_{name}_tk_neg"
            tc_val = f":b_{name}_tc_delta"
            tk_threshold = f":b_{name}_tk_min"

            attr_names[tk_alias] = schema.bucket_attr(name, schema.BUCKET_FIELD_TK)
            attr_names[tc_alias] = schema.bucket_attr(name, schema.BUCKET_FIELD_TC)
            attr_values[tk_neg_val] = {"N": str(-c)}
            attr_values[tc_val] = {"N": str(c)}
            attr_values[tk_threshold] = {"N": str(c)}

            add_parts.append(f"{tk_alias} {tk_neg_val}")
            add_parts.append(f"{tc_alias} {tc_val}")
            condition_parts.append(f"{tk_alias} >= {tk_threshold}")

        update_expr = f"ADD {', '.join(add_parts)}"
        condition_expr = " AND ".join(condition_parts)

        return {
            "Update": {
                "TableName": self.table_name,
                "Key": {
                    "PK": {
                        "S": schema.pk_bucket(self._namespace_id, entity_id, resource, shard_id)
                    },
                    "SK": {"S": schema.sk_state()},
                },
                "UpdateExpression": update_expr,
                "ConditionExpression": condition_expr,
                "ExpressionAttributeNames": attr_names,
                "ExpressionAttributeValues": attr_values,
            }
        }

    def build_composite_adjust(
        self,
        entity_id: str,
        resource: str,
        deltas: dict[str, int],
        shard_id: int = 0,
    ) -> dict[str, Any]:
        """Build an UpdateItem for the adjust write path (ADR-115 path 4).

        Unconditional ADD for post-hoc correction. Can go negative by design.
        Positive delta = consumed more (subtract tokens, add to counter).
        Negative delta = consumed less (add tokens, subtract from counter).
        """
        add_parts: list[str] = []
        attr_names: dict[str, str] = {}
        attr_values: dict[str, Any] = {}

        for name, delta in deltas.items():
            if delta == 0:
                continue
            tk_alias = f"#b_{name}_tk"
            tc_alias = f"#b_{name}_tc"
            tk_val = f":b_{name}_tk_delta"
            tc_val = f":b_{name}_tc_delta"

            attr_names[tk_alias] = schema.bucket_attr(name, schema.BUCKET_FIELD_TK)
            attr_names[tc_alias] = schema.bucket_attr(name, schema.BUCKET_FIELD_TC)
            # delta > 0 means consumed more: subtract from tk, add to tc
            attr_values[tk_val] = {"N": str(-delta)}
            attr_values[tc_val] = {"N": str(delta)}

            add_parts.append(f"{tk_alias} {tk_val}")
            add_parts.append(f"{tc_alias} {tc_val}")

        if not add_parts:
            # Nothing to adjust
            return {}

        update_expr = f"ADD {', '.join(add_parts)}"

        return {
            "Update": {
                "TableName": self.table_name,
                "Key": {
                    "PK": {
                        "S": schema.pk_bucket(self._namespace_id, entity_id, resource, shard_id)
                    },
                    "SK": {"S": schema.sk_state()},
                },
                "UpdateExpression": update_expr,
                "ExpressionAttributeNames": attr_names,
                "ExpressionAttributeValues": attr_values,
            }
        }

    async def transact_write(self, items: list[dict[str, Any]]) -> None:
        """Execute a write, using single-item API when possible to halve WCU cost."""
        if not items:
            return

        client = await self._get_client()

        if len(items) == 1:
            item = items[0]
            if "Put" in item:
                await client.put_item(**item["Put"])
            elif "Update" in item:
                await client.update_item(**item["Update"])
            elif "Delete" in item:
                await client.delete_item(**item["Delete"])
            else:
                await client.transact_write_items(TransactItems=items)
        else:
            await client.transact_write_items(TransactItems=items)

    async def write_each(self, items: list[dict[str, Any]]) -> None:
        """Write items independently without cross-item atomicity (1 WCU each).

        Each item is dispatched as a single PutItem, UpdateItem, or DeleteItem
        call. Use for unconditional writes (e.g., ADD adjustments) where partial
        success is acceptable.
        """
        if not items:
            return

        client = await self._get_client()

        for item in items:
            if "Put" in item:
                await client.put_item(**item["Put"])
            elif "Update" in item:
                await client.update_item(**item["Update"])
            elif "Delete" in item:
                await client.delete_item(**item["Delete"])

    async def speculative_consume(
        self,
        entity_id: str,
        resource: str,
        consume: dict[str, int],
        ttl_seconds: int | None = None,
        shard_id: int | None = None,
        now_ms: int | None = None,
    ) -> SpeculativeResult:
        """Attempt speculative UpdateItem with condition check.

        Checks entity cache for cascade metadata. If cache hit + cascade,
        issues child+parent UpdateItems concurrently via asyncio.gather
        and returns nested parent_result.

        When ``shard_id`` is explicitly provided, targets that shard directly
        without cascade logic (used for shard retry).

        Args:
            entity_id: Entity owning the bucket
            resource: Resource name
            consume: Amount per limit (tokens, not milli)
            ttl_seconds: TTL in seconds from now, or None for no TTL change
            shard_id: Explicit shard to target (skips random selection and
                cascade logic). None means auto-select from entity cache.
            now_ms: The caller's "now" (issue #430). The limiter passes the
                same reading it used for its own refill math, so one logical
                ``acquire()`` observes one instant: the ``ttl`` stamp, the
                TTL-expiry guard and the caller's decision all agree. None
                reads the clock once here, for callers outside an acquire.

        Returns:
            SpeculativeResult with:
            - On cache hit + cascade + both succeed: parent_result populated
            - On cache miss or non-cascade: parent_result is None
            - On failure: old_buckets from ALL_OLD (or None if bucket missing)
        """
        if now_ms is None:
            now_ms = self._now_ms()

        # Explicit shard_id: direct single-shard consume (shard retry path)
        if shard_id is not None:
            return await self._speculative_consume_single(
                entity_id, resource, consume, ttl_seconds, shard_id=shard_id, now_ms=now_ms
            )

        # Check entity cache for parallel cascade opportunity (issue #318)
        cache_key = (self._namespace_id, entity_id)
        cache_entry = self._entity_cache.get(cache_key)

        effective_shard_id, _shard_count = self.select_shard(entity_id, resource)

        if cache_entry is not None:
            cascade_cached, parent_id_cached, shards_cached = cache_entry
            if cascade_cached and parent_id_cached:
                child_result: SpeculativeResult
                parent_result: SpeculativeResult
                # The parent shards independently of the child (issue #474):
                # its shard comes from the parent's own cached shard_count.
                # Defaulting to shard 0 kept every warm-path cascade write on
                # parent shard 0 — the unmitigated hot partition write
                # sharding exists to protect (GHSA-76rv, issue #116) — and
                # disagreed with the slow path, which draws from that count.
                parent_shard_id, _parent_count = self.select_shard(parent_id_cached, resource)
                child_result, parent_result = await asyncio.gather(
                    self._speculative_consume_single(
                        entity_id,
                        resource,
                        consume,
                        ttl_seconds,
                        shard_id=effective_shard_id,
                        now_ms=now_ms,
                    ),
                    self._speculative_consume_single(
                        parent_id_cached,
                        resource,
                        consume,
                        ttl_seconds,
                        shard_id=parent_shard_id,
                        now_ms=now_ms,
                    ),
                )
                if parent_result.success:
                    # A failure learns inside _speculative_consume_single; a
                    # success must too, or the parent's cached count never
                    # grows and every later draw lands back on shard 0.
                    #
                    # No `meta`: the parent's shard N>0 was most likely created
                    # by a *child's* cascade slow path, which denormalizes only
                    # the acquiring entity's own flags and so stamps the parent
                    # cascade=False / parent_id=None. Passing that as meta
                    # would downgrade the parent's cache entry, and the next
                    # acquire(parent) would silently stop debiting the
                    # grandparent (the cache has no TTL). Without meta this
                    # still grows the count on an entry that already exists.
                    self._learn_shard_count(parent_id_cached, resource, parent_result.shard_count)
                if child_result.success:
                    self._learn_shard_count(
                        entity_id,
                        resource,
                        child_result.shard_count,
                        meta=(child_result.cascade, child_result.parent_id),
                    )
                else:
                    # On failure, _speculative_consume_single doesn't return
                    # cascade/parent_id (only in ALL_NEW). Use cached values
                    # so the caller can compensate the parent.
                    child_result.cascade = cascade_cached
                    child_result.parent_id = parent_id_cached
                child_result.parent_result = parent_result
                return child_result

        # Cache miss or non-cascade: single UpdateItem
        result = await self._speculative_consume_single(
            entity_id, resource, consume, ttl_seconds, shard_id=effective_shard_id, now_ms=now_ms
        )
        if result.success:
            self._learn_shard_count(
                entity_id, resource, result.shard_count, meta=(result.cascade, result.parent_id)
            )
        return result

    async def _speculative_consume_single(
        self,
        entity_id: str,
        resource: str,
        consume: dict[str, int],
        ttl_seconds: int | None = None,
        shard_id: int = 0,
        now_ms: int | None = None,
    ) -> SpeculativeResult:
        """Issue a single speculative UpdateItem on a bucket shard.

        In addition to consuming the application-level limits, this method
        auto-consumes 1 WCU (1000 millitokens) from the ``wcu`` infrastructure
        limit and includes ``wcu tk >= 1000`` in the condition expression.

        Args:
            entity_id: Entity owning the bucket.
            resource: Resource name.
            consume: Amount per limit (tokens, not milli).
            ttl_seconds: TTL in seconds, or None for no TTL change.
            shard_id: Target shard index (default 0).
            now_ms: The caller's "now" (issue #430). Every clock-derived part
                of the write — the ``ttl`` stamp, the ``#ttl > :now_epoch``
                expiry guard and the ``#vu > :vu_now`` schedule-window guard
                (#222) — is derived from this one value. None reads the clock
                once here.

        Returns:
            SpeculativeResult with shard_id and shard_count populated.
        """
        if now_ms is None:
            now_ms = self._now_ms()

        client = await self._get_client()

        # Build ADD expression for each limit
        add_parts: list[str] = []
        condition_parts: list[str] = ["attribute_exists(PK)"]
        attr_names: dict[str, str] = {}
        attr_values: dict[str, Any] = {}

        for limit_name, amount in consume.items():
            amount_milli = amount * 1000
            tk_attr = schema.bucket_attr(limit_name, schema.BUCKET_FIELD_TK)
            tc_attr = schema.bucket_attr(limit_name, schema.BUCKET_FIELD_TC)
            tk_alias = f"#tk_{limit_name}"
            tc_alias = f"#tc_{limit_name}"
            neg_val = f":neg_{limit_name}"
            pos_val = f":pos_{limit_name}"
            thresh_val = f":thresh_{limit_name}"

            attr_names[tk_alias] = tk_attr
            attr_names[tc_alias] = tc_attr
            attr_values[neg_val] = {"N": str(-amount_milli)}
            attr_values[pos_val] = {"N": str(amount_milli)}
            attr_values[thresh_val] = {"N": str(amount_milli)}

            add_parts.append(f"{tk_alias} {neg_val}")
            add_parts.append(f"{tc_alias} {pos_val}")
            condition_parts.append(f"{tk_alias} >= {thresh_val}")

        # Add wcu infrastructure limit consumption (1 WCU = 1000 millitokens per write)
        wcu_tk_attr = schema.bucket_attr(schema.WCU_LIMIT_NAME, schema.BUCKET_FIELD_TK)
        wcu_tc_attr = schema.bucket_attr(schema.WCU_LIMIT_NAME, schema.BUCKET_FIELD_TC)
        attr_names["#wcu_tk"] = wcu_tk_attr
        attr_names["#wcu_tc"] = wcu_tc_attr
        wcu_milli = 1000  # 1 WCU = 1000 millitokens
        attr_values[":neg_wcu"] = {"N": str(-wcu_milli)}
        attr_values[":pos_wcu"] = {"N": str(wcu_milli)}
        attr_values[":thresh_wcu"] = {"N": str(wcu_milli)}
        add_parts.append("#wcu_tk :neg_wcu")
        add_parts.append("#wcu_tc :pos_wcu")
        condition_parts.append("#wcu_tk >= :thresh_wcu")

        update_expr = "ADD " + ", ".join(add_parts)

        # Handle TTL
        if ttl_seconds is not None:
            ttl_epoch = schema.calculate_ttl(now_ms, ttl_seconds)
            update_expr = f"SET #ttl = :ttl {update_expr}"
            attr_names["#ttl"] = "ttl"
            attr_values[":ttl"] = {"N": str(ttl_epoch)}

        # Reject expired-but-not-yet-deleted buckets (DynamoDB TTL is eventual)
        now_epoch = now_ms // 1000
        attr_names["#ttl"] = "ttl"
        attr_values[":now_epoch"] = {"N": str(now_epoch)}
        condition_parts.append("(attribute_not_exists(#ttl) OR #ttl > :now_epoch)")

        # Reject buckets stamped as disabled (ADR-125). The attribute is present
        # only when the bucket is effectively disabled, so this costs nothing on
        # the enabled path.
        attr_names["#disabled"] = schema.BUCKET_FIELD_DISABLED
        condition_parts.append("attribute_not_exists(#disabled)")

        # Reject a bucket whose schedule window has closed (#222 §2.1). The
        # fast path cannot evaluate a schedule, so `vu` is a precomputed
        # instant: past it, `tk` was materialised under parameters that no
        # longer apply and only the slow path may spend it. Absent means "no
        # schedule, never expires", which is every bucket written before
        # scheduling existed — so this costs nothing on the unscheduled path.
        # Uses the bound `now_ms`; a fresh read here would re-introduce the
        # second clock reading #430 removed, and could straddle the boundary
        # the comparison is about.
        attr_names["#vu"] = schema.BUCKET_FIELD_VU
        attr_values[":vu_now"] = {"N": str(now_ms)}
        condition_parts.append("(attribute_not_exists(#vu) OR #vu > :vu_now)")

        condition_expr = " AND ".join(condition_parts)

        try:
            response = await client.update_item(
                TableName=self.table_name,
                Key={
                    "PK": {
                        "S": schema.pk_bucket(self._namespace_id, entity_id, resource, shard_id)
                    },
                    "SK": {"S": schema.sk_state()},
                },
                UpdateExpression=update_expr,
                ConditionExpression=condition_expr,
                ExpressionAttributeNames=attr_names,
                ExpressionAttributeValues=attr_values,
                ReturnValues="ALL_NEW",
                ReturnValuesOnConditionCheckFailure="ALL_OLD",
            )

            # Success: deserialize ALL_NEW
            item = response["Attributes"]
            buckets = self._deserialize_composite_bucket(item)
            cascade = item.get("cascade", {}).get("BOOL", False)
            parent_id = item.get("parent_id", {}).get("S")
            shard_count = int(item.get("shard_count", {}).get("N", "1"))
            return SpeculativeResult(
                success=True,
                buckets=buckets,
                cascade=cascade,
                parent_id=parent_id,
                shard_id=shard_id,
                shard_count=shard_count,
            )

        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                old_item = cast(dict[str, Any] | None, e.response.get("Item"))
                if old_item:
                    old_buckets = self._deserialize_composite_bucket(old_item)
                    old_shard_count = int(old_item.get("shard_count", {}).get("N", "1"))
                    # Denormalized on the item, so a failure knows whether this
                    # entity cascades even with a cold cache — a cascading child
                    # must never be admitted by a child-only shard retry.
                    old_cascade = old_item.get("cascade", {}).get("BOOL", False)
                    old_parent_id = old_item.get("parent_id", {}).get("S")
                    # Keep the cached shard_count current from the failure
                    # image so later draws cover every shard (issue #439); the
                    # image also carries cascade/parent_id, so a cold cache
                    # can be populated correctly rather than skipped.
                    self._learn_shard_count(
                        entity_id, resource, old_shard_count, meta=(old_cascade, old_parent_id)
                    )

                    # Disabled wins over every other classification: retrying on
                    # another shard or doubling shards cannot help (ADR-125).
                    if old_item.get(schema.BUCKET_FIELD_DISABLED, {}).get("BOOL", False):
                        return SpeculativeResult(
                            success=False,
                            old_buckets=old_buckets,
                            cascade=old_cascade,
                            parent_id=old_parent_id,
                            shard_id=shard_id,
                            shard_count=old_shard_count,
                            failure_reason=SpeculativeFailureReason.DISABLED,
                        )

                    # A closed schedule window outranks exhaustion (#222 §2.1):
                    # the limits that rejected this write are stale, and the
                    # new window may admit it. Must precede the exhausted
                    # checks, or a boundary reads as a rejection and the caller
                    # sees RateLimitExceeded against limits no longer in force
                    # — and a stale `wcu` reading would double shard_count at
                    # every boundary. Mirrors the condition exactly (`vu > now`
                    # passes), against the same bound `now_ms`.
                    vu_raw = old_item.get(schema.BUCKET_FIELD_VU, {}).get("N")
                    if vu_raw is not None and int(vu_raw) <= now_ms:
                        return SpeculativeResult(
                            success=False,
                            old_buckets=old_buckets,
                            cascade=old_cascade,
                            parent_id=old_parent_id,
                            shard_id=shard_id,
                            shard_count=old_shard_count,
                            failure_reason=SpeculativeFailureReason.SCHEDULE_BOUNDARY,
                        )

                    # Classify failure reason (GHSA-76rv)
                    wcu_exhausted = any(
                        b.limit_name == schema.WCU_LIMIT_NAME and b.tokens_milli < 1000
                        for b in old_buckets
                    )
                    app_exhausted = any(
                        b.limit_name != schema.WCU_LIMIT_NAME
                        and b.tokens_milli < consume.get(b.limit_name, 0) * 1000
                        for b in old_buckets
                    )
                    if wcu_exhausted and app_exhausted:
                        reason = SpeculativeFailureReason.BOTH_EXHAUSTED
                    elif wcu_exhausted:
                        reason = SpeculativeFailureReason.WCU_EXHAUSTED
                    else:
                        reason = SpeculativeFailureReason.APP_LIMIT_EXHAUSTED

                    return SpeculativeResult(
                        success=False,
                        old_buckets=old_buckets,
                        cascade=old_cascade,
                        parent_id=old_parent_id,
                        shard_id=shard_id,
                        shard_count=old_shard_count,
                        failure_reason=reason,
                    )
                else:
                    return SpeculativeResult(
                        success=False,
                        shard_id=shard_id,
                        failure_reason=SpeculativeFailureReason.BUCKET_MISSING,
                    )
            raise

    def _learn_shard_count(
        self,
        entity_id: str,
        resource: str,
        observed: int,
        *,
        meta: tuple[bool, str | None] | None = None,
    ) -> int:
        """Record an observed shard_count in the entity cache, monotonically.

        The cache never shrinks: a shard N>0 item can carry a stale, lower
        ``shard_count`` (propagation lag), and shard 0 itself can lag its
        siblings after a TTL re-create. Adopting a lower value would narrow
        the draw range and stamp the next client-created shard with the
        lowered count (issue #439). ``meta`` supplies ``(cascade, parent_id)``
        for a new entry; without it an unknown entity is left uncached.

        Returns:
            The count now cached (``observed`` when nothing was cached).
        """
        cache_key = (self._namespace_id, entity_id)
        entry = self._entity_cache.get(cache_key)
        if entry is None:
            if meta is None:
                return observed
            cascade, parent_id = meta
            shards: dict[str, int] = {}
        else:
            cascade, parent_id = meta if meta is not None else (entry[0], entry[1])
            shards = dict(entry[2])
        count = max(observed, shards.get(resource, 1))
        shards[resource] = count
        self._entity_cache[cache_key] = (cascade, parent_id, shards)
        return count

    def select_shard(
        self,
        entity_id: str,
        resource: str,
        shard_id: int | None = None,
        shard_count: int | None = None,
    ) -> tuple[int, int]:
        """Pick the bucket shard an acquire should target (GHSA-76rv, issue #439).

        This is the only place a shard is drawn. The speculative fast path
        draws here, and the slow path reuses whatever shard the fast path
        already selected (passing it back as ``shard_id``) so a
        ``BUCKET_MISSING`` on shard N reads and creates shard N — never a
        second random draw that lands on shard 0 again.

        Selection is ``random.randrange(shard_count)`` from the cached
        shard_count, not a hash of the entity id: every call re-picks, which
        is what spreads one hot entity's writes across all of its shards.

        Args:
            entity_id: Entity owning the bucket.
            resource: Resource name.
            shard_id: Explicit shard to honour verbatim, or None to draw one.
            shard_count: Count observed by the caller (e.g. on a speculative
                failure image, which never updates the cache); None reads
                the entity cache.

        Returns:
            ``(shard_id, shard_count)`` with shard_count from the argument or
            the entity cache (1 when unknown).
        """
        if shard_count is None:
            cache_key = (self._namespace_id, entity_id)
            cache_entry = self._entity_cache.get(cache_key)
            shard_count = cache_entry[2].get(resource, 1) if cache_entry is not None else 1
        if shard_id is None:
            shard_id = random.randrange(shard_count) if shard_count > 1 else 0
        return shard_id, shard_count

    async def bump_shard_count(self, entity_id: str, resource: str, current_count: int) -> int:
        """Double shard_count on shard 0 via conditional write.

        Shard 0 is the source of truth for shard_count. The conditional
        expression ``shard_count = :old`` prevents double-bumping when
        multiple clients race to double concurrently. Also updates the
        entity cache with the new shard_count.

        Args:
            entity_id: Entity owning the bucket.
            resource: Resource name.
            current_count: Current shard_count to double.

        Returns:
            The new shard_count (doubled), or — if another client already
            doubled (ConditionalCheckFailedException) — the winner's count
            read from the failed write's ALL_OLD image, so the loser draws
            from the new shard range instead of caching its stale count and
            landing back on the exhausted shard (issue #439).
        """
        cache_key = (self._namespace_id, entity_id)
        meta = None if cache_key in self._entity_cache else (False, None)
        if current_count >= schema.MAX_SHARD_COUNT:
            # Refused: the share per shard would fall below what a request
            # can use, and an exhausted shard must not drive doubling forever
            # (ADR-133). Warn once per (entity, resource); #475 adds a metric.
            if (entity_id, resource) not in self._shard_cap_warned:
                self._shard_cap_warned.add((entity_id, resource))
                # Deduplicated per (entity, resource), but the entity id is not
                # logged: entity ids are routinely API keys and must not be
                # written to logs in clear text (py/clear-text-logging-sensitive
                # -data). Per-entity attribution belongs on the metric in #475.
                logger.warning(
                    "shard_count for resource=%s is at MAX_SHARD_COUNT=%d; "
                    "refusing to double further",
                    resource,
                    schema.MAX_SHARD_COUNT,
                )
            return self._learn_shard_count(entity_id, resource, current_count, meta=meta)

        new_count = current_count * 2
        client = await self._get_client()
        try:
            await client.update_item(
                TableName=self.table_name,
                Key={
                    "PK": {"S": schema.pk_bucket(self._namespace_id, entity_id, resource, 0)},
                    "SK": {"S": schema.sk_state()},
                },
                UpdateExpression="SET shard_count = :new",
                ConditionExpression="shard_count = :old",
                ExpressionAttributeValues={
                    ":old": {"N": str(current_count)},
                    ":new": {"N": str(new_count)},
                },
                ReturnValuesOnConditionCheckFailure="ALL_OLD",
            )
            effective_count = new_count
            # We won the bump, so we own propagating the new count to the
            # shards that already exist (1..current_count-1). The aggregator's
            # Path 1 does this from the stream, but with --no-aggregator
            # nothing would, and a shard left on a stale lower count refills
            # to cp // stale_count — the shares then sum to more than the
            # limit (issue #439). Same conditional write, so whichever of us
            # gets there first wins and the other is a no-op.
            await self._propagate_shard_count(entity_id, resource, current_count, new_count)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                # Another client already doubled: adopt the winner's count —
                # but never a lower one. Shard 0 can lag its siblings (a TTL
                # re-create starts it at 1); adopting that would pin the
                # client to shard 0. Without an image keep what we knew.
                winner = cast(dict[str, Any] | None, e.response.get("Item")) or {}
                winner_count = int(winner.get("shard_count", {}).get("N", str(current_count)))
                effective_count = max(current_count, winner_count)
            else:
                raise

        # Update entity cache with new shard_count (monotonic)
        return self._learn_shard_count(entity_id, resource, effective_count, meta=meta)

    async def _propagate_shard_count(
        self, entity_id: str, resource: str, old_count: int, new_count: int
    ) -> int:
        """Stamp ``new_count`` on the shards that already exist.

        Mirrors the aggregator's Path 1 (``processor.propagate_shard_count``)
        so write sharding is self-consistent without the aggregator: every
        shard must agree on ``shard_count`` because each refills toward
        ``capacity_milli // shard_count`` (issue #439). Shards
        ``old_count..new_count-1`` are not written here — they do not exist
        yet and are created with the current count by whoever draws them.

        The writes are independent single-item conditional updates issued
        concurrently. ``shard_count < :new`` makes each one idempotent and
        monotonic, so racing with the aggregator or another client is a no-op
        rather than a conflict.

        Returns:
            The number of shards actually updated.
        """
        if old_count <= 1:
            return 0
        client = await self._get_client()

        async def stamp(target_shard: int) -> int:
            try:
                await client.update_item(
                    TableName=self.table_name,
                    Key={
                        "PK": {
                            "S": schema.pk_bucket(
                                self._namespace_id, entity_id, resource, target_shard
                            )
                        },
                        "SK": {"S": schema.sk_state()},
                    },
                    UpdateExpression="SET shard_count = :new",
                    ConditionExpression="shard_count < :new",
                    ExpressionAttributeValues={":new": {"N": str(new_count)}},
                )
                return 1
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code")
                if code == "ConditionalCheckFailedException":
                    return 0  # Already at or above the new count
                raise

        # List comprehension, not a generator: the sync transformer rewrites
        # `gather(*[expr for x in it])` into `_run_in_executor(*[lambda x=x:
        # expr for x in it])`, which needs the call deferred into the lambda.
        results = await asyncio.gather(*[stamp(n) for n in range(1, old_count)])
        return sum(results)

    async def _propagate_window_start(
        self,
        entity_id: str,
        resource: str,
        shard_id: int,
        shard_count: int,
        windows: dict[str, tuple[int, int]],
    ) -> int:
        """Stamp a newly anchored duration window on the entity's other shards (ADR-139).

        Without it, shard A drawn at 20:00 and shard B at 20:03 anchor
        different windows and the entity's windows stagger — at which point
        "when does mine reset" has no honest answer: ``min(ws) + W``
        over-promises and ``max(ws) + W`` under-promises.

        Shaped like :meth:`_propagate_shard_count`: one conditional write per
        target, idempotent and monotonic. The condition is **not** "the
        sibling's ``ws`` is older" but "the sibling's window had already
        *ended* by the new start"::

            attribute_not_exists(ws) OR ws <= :open_floor
            :open_floor = new_ws - rsa * 1000

        which is exactly :meth:`RateLimiter._open_window_if_elapsed`'s rule
        (the window is half-open, ``[ws, ws + rsa)``, so it has elapsed at
        ``now`` iff ``ws + rsa <= now``) evaluated at ``new_ws``. A sibling
        moves only when it would itself have opened a window at that instant.
        What that buys:

        - **Idempotent.** Once stamped, ``new_ws <= new_ws - W`` is false.
        - **No double reset between concurrent openers.** Two clients crossing
          the boundary milliseconds apart each open a window on their own
          shard and each fan out. A plain ``ws < :new`` let the later value
          overwrite the earlier opener's own shard, whose ``rf`` is its own
          ``now`` — so ``ws > rf`` held and that shard reset a *second* time
          in one window (a moto repro admitted 15 against a quota of 10).
          Under the floor, both fan-outs no-op on the other's shard: the two
          shards stay staggered by the openers' few milliseconds until the
          next window, which is the whole residual cost.
        - **No drag-back from a delayed write.** A write carrying a stale
          ``ws`` has a floor below the stored start, and is rejected.
        - **Safe under ``--no-aggregator``.** The client owns this, exactly as
          :meth:`bump_shard_count` owns shard-count propagation.

        A sibling whose window is *longer* than ``rsa`` (the length was just
        raised) may not have elapsed by the floor and no-ops; it opens its own
        window when it does elapse, which is the pre-fan-out behaviour.

        **It writes ``ws`` and never ``tk``**, which is the coherence argument.
        A fan-out cannot use ``ADD`` — it does not know each sibling's
        balance — and the blind ``SET`` it would otherwise need races the
        sibling's own slow path in both orderings: landing after, it clobbers
        the sibling's committed consumption; landing before, the sibling's
        ``rf`` lock still holds and its own ``ADD`` applies on top, leaving it
        at twice its share. Each sibling resets itself, under its own ``rf``
        lock, in the write it was going to make anyway: it reads ``ws > rf``
        (``BucketState.window_rolled``).

        ``rsa`` rides with ``ws``, as on every acquire-path write: a sibling
        created before its limit gained a window carries none, and the
        aggregator and a new shard's inheritance read only the item, so a
        ``ws`` alone would leave the window's end unknowable to them.

        ``vu = 0`` rides along too. The fast path is a pure ``ADD`` with no
        ceiling arithmetic, so without it a sibling whose ``vu`` still lies in
        the future (gated by a cron boundary, say) would keep spending its
        *old* window's balance against a bucket the entity has already
        rolled. The cost is one skipped aggregator refill per shard per
        rollover (#508's ``vu = :expected_vu`` pin sees the change).

        ``attribute_exists(PK)`` keeps a sibling that does not exist yet from
        being conjured as a half-item; whoever draws it creates it.

        One write per (sibling, limit) rather than one per sibling: two
        duration limits on one item can have different lengths and so roll at
        different instants, and an ANDed condition would no-op the whole
        write whenever one was not due — leaving the other staggered.

        A write that fails for any reason other than its condition is logged
        and counted as not landed rather than raised: under the serial and
        gevent sync strategies a raise would abandon the siblings not yet
        written (the portable ``_safe`` shape, #491). The caller was already
        admitted; a sibling left behind opens its own window later.

        Args:
            windows: Limit name -> ``(new_ws_ms, reset_after_seconds)``.

        Returns:
            The number of writes that applied. Zero, with no request issued,
            at ``shard_count == 1``: the cost is ``(S - 1) × L`` WCU per
            rollover, and nothing at all for an unsharded entity.
        """
        if shard_count <= 1 or not windows:
            return 0
        client = await self._get_client()

        async def stamp(target_shard: int, name: str, window: tuple[int, int]) -> int:
            new_ws, rsa = window
            try:
                await client.update_item(
                    TableName=self.table_name,
                    Key={
                        "PK": {
                            "S": schema.pk_bucket(
                                self._namespace_id, entity_id, resource, target_shard
                            )
                        },
                        "SK": {"S": schema.sk_state()},
                    },
                    UpdateExpression="SET #ws = :new, #rsa = :rsa, #vu = :zero",
                    ConditionExpression=(
                        "attribute_exists(PK) AND (attribute_not_exists(#ws) OR #ws <= :open_floor)"
                    ),
                    # Aliases, not bare names: `NAME_PATTERN` allows `-` and
                    # `.` in a limit name, and `.` is a document-path
                    # separator in expression text.
                    ExpressionAttributeNames={
                        "#ws": schema.bucket_attr(name, schema.BUCKET_FIELD_WS),
                        "#rsa": schema.bucket_attr(name, schema.BUCKET_FIELD_RSA),
                        "#vu": schema.BUCKET_FIELD_VU,
                    },
                    ExpressionAttributeValues={
                        ":new": {"N": str(new_ws)},
                        ":rsa": {"N": str(rsa)},
                        ":open_floor": {"N": str(new_ws - rsa * 1000)},
                        ":zero": {"N": "0"},
                    },
                )
                return 1
            except Exception as e:
                code = (
                    e.response.get("Error", {}).get("Code") if isinstance(e, ClientError) else None
                )
                if code != "ConditionalCheckFailedException":
                    # The entity id is routinely an API key; never log it.
                    logger.warning(
                        "duration-window fan-out write failed for resource=%s shard=%d",
                        resource,
                        target_shard,
                        exc_info=True,
                    )
                return 0  # not due, absent, or failed: the sibling opens its own

        targets = [
            (n, name, window)
            for n in range(shard_count)
            if n != shard_id
            for name, window in sorted(windows.items())
        ]
        # One bare-name comprehension target, not `for n, name, w in ...`:
        # the sync transformer defers the call into a `lambda t=t:` only for a
        # plain Name target. A tuple target falls through to its generic
        # branch, which calls `stamp(...)` eagerly and then calls the int.
        results = await asyncio.gather(*[stamp(*t) for t in targets])
        return sum(results)

    # -------------------------------------------------------------------------
    # Limit config operations
    # -------------------------------------------------------------------------

    async def set_limits(
        self,
        entity_id: str,
        limits: list[Limit],
        resource: str = schema.DEFAULT_RESOURCE,
        principal: str | None = None,
        *,
        disabled: bool | None = _PRESERVE_DISABLED,
    ) -> None:
        """
        Store limit configs for an entity (composite format, ADR-114).

        All limits for an entity+resource are stored in a single composite item
        with SK '#CONFIG#{resource}'. This reduces cache-miss cost from N GetItem
        calls to 1 GetItem call.

        Args:
            entity_id: ID of the entity
            limits: List of Limit configurations to store
            resource: Resource name (defaults to "_default_")
            principal: Caller identity for audit logging
            disabled: Tri-state disabled flag. Defaults to preserving whatever
                value is already stored (this is a full-replace PutItem, so an
                explicit value must be passed to change it; see ADR-125).
                Passing an explicit value also fans out to existing buckets,
                exactly as `disable_entity()`/`enable_entity()` do.
        """
        client = await self._get_client()

        # Full-replace PutItem would drop `disabled`; preserve it unless the
        # caller passed an explicit value (ADR-125).
        disabled_explicit = disabled is not _PRESERVE_DISABLED
        if not disabled_explicit:
            disabled = await self.get_entity_disabled(entity_id, resource)

        # Build composite config item with all limits
        item: dict[str, Any] = {
            "PK": {"S": schema.pk_entity(self._namespace_id, entity_id)},
            "SK": {"S": schema.sk_config(resource)},
            "entity_id": {"S": entity_id},
            "resource": {"S": resource},
            "config_version": {"N": "1"},
            # GSI3 attributes for sparse indexing (entity config queries)
            "GSI3PK": {"S": schema.gsi3_pk_entity_config(self._namespace_id, resource)},
            "GSI3SK": {"S": schema.gsi3_sk_entity(entity_id)},
            # GSI4: namespace-scoped item discovery
            "GSI4PK": {"S": self._namespace_id},
            "GSI4SK": {"S": schema.pk_entity(self._namespace_id, entity_id)},
        }

        # Add l_* attributes for each limit
        self._serialize_composite_limits(limits, item)

        disabled_attr = schema.encode_disabled(disabled)
        if disabled_attr is not None:
            item[schema.CONFIG_FIELD_DISABLED] = disabled_attr

        # Use transaction to atomically create config + increment registry (issue #288)
        # This prevents race conditions where concurrent creates both increment
        try:
            await client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.table_name,
                            "Item": item,
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.table_name,
                            "Key": {
                                "PK": {"S": schema.pk_system(self._namespace_id)},
                                "SK": {"S": schema.sk_entity_config_resources()},
                            },
                            "UpdateExpression": (
                                "SET GSI4PK = if_not_exists(GSI4PK, :gsi4pk),"
                                " GSI4SK = if_not_exists(GSI4SK, :gsi4sk)"
                                " ADD #resource :one"
                            ),
                            "ExpressionAttributeNames": {"#resource": resource},
                            "ExpressionAttributeValues": {
                                ":one": {"N": "1"},
                                ":gsi4pk": {"S": self._namespace_id},
                                ":gsi4sk": {"S": schema.pk_system(self._namespace_id)},
                            },
                        }
                    },
                ]
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "TransactionCanceledException":
                # Check if cancellation was due to condition failure (UPDATE case)
                reasons = e.response.get("CancellationReasons", [])
                if reasons and reasons[0].get("Code") == "ConditionalCheckFailed":
                    # Config already exists - UPDATE case, just overwrite
                    await client.put_item(TableName=self.table_name, Item=item)
                else:
                    raise
            else:
                raise

        # Auto-evict from config cache so next resolve reads fresh config
        # (ADR-122). Strictly BEFORE the bucket sync: under the entity-wide
        # `_default_` scope that sync re-resolves per bucket resource, and a
        # stale cached `_default_` level would make it write the limits this
        # call just replaced (#487).
        self._config_cache.evict_entity(entity_id, resource)

        # Sync bucket static params if bucket exists (issue #294, #327)
        # Entity config = no TTL (bucket_ttl_refill_multiplier=0 → REMOVE ttl)
        await self._sync_bucket_params(entity_id, resource, limits, bucket_ttl_refill_multiplier=0)

        # An explicit `disabled` changes what resolve_disabled() answers, so the
        # denormalized bucket stamp has to follow it eagerly — same contract as
        # disable_entity()/enable_entity() (ADR-125). With the preserve sentinel
        # the stored value is unchanged, so no fan-out is needed.
        if disabled_explicit:
            effective, _level = await self.resolve_disabled(entity_id, resource)
            fanout_resource = None if resource == schema.DEFAULT_RESOURCE else resource
            await self._fanout_entity(entity_id, fanout_resource, disabled=effective)

        # Log audit event
        await self._log_audit_event(
            action=AuditAction.LIMITS_SET,
            entity_id=entity_id,
            principal=principal,
            resource=resource,
            details={"limits": [limit.to_dict() for limit in limits]},
        )

    async def _sync_bucket_params(
        self,
        entity_id: str,
        resource: str,
        limits: list[Limit],
        bucket_ttl_refill_multiplier: int | None = None,
        stale_limit_names: set[str] | None = None,
    ) -> None:
        """Sync bucket static params when limits change (issue #294, #327).

        Updates capacity, refill_amount, refill_period, and TTL for
        existing buckets. Optionally removes stale limit attributes.
        Uses conditional update with attribute_exists(PK) to skip if
        bucket doesn't exist yet.

        Fans out to **every** shard of the (entity, resource), discovered via
        GSI3 like the ADR-125 disable fan-out. Keying only shard 0 left shards
        1..N-1 — created by the aggregator's Path 2 propagation or by the client
        slow path with ``--no-aggregator`` — enforcing the limits they were born
        with forever, and nothing detects the drift (issue #468, named residual
        of GHSA-w6c2-33wf-qfwf). The stored ``cp``/``ra``/``rp`` stay
        **undivided** on every shard: the per-shard share is derived at read
        time by ``BucketState.effective_*``, exactly as the aggregator's Path 2
        clone writes it. Cost is O(shards) WCU plus the KEYS_ONLY GSI3 queries,
        and the same narrow race ADR-125 documents applies: a bucket created by
        an acquire already in flight can be missed (mitigated, not eliminated,
        by the second discovery pass).

        Writes are issued **serially**, exactly like `_fanout_entity`. Under
        the entity-wide scope below the write set is O(resources x shards), not
        the <= MAX_SHARD_COUNT of one resource, so a single `asyncio.gather`
        over it would put thousands of concurrent `UpdateItem`s in flight on an
        admin call. Serial also makes an exact partial-progress count possible:
        the config item is already committed by the time this runs, so a
        failure part-way leaves the table half-applied and the operator needs
        to know how far it got. That is `FanoutIncomplete`, raised here for the
        same reason and with the same re-run-to-reconcile remedy as the ADR-125
        fan-out — each write is idempotent. The cost is up to MAX_SHARD_COUNT
        sequential round trips where #468 issued one concurrent batch; the
        disable fan-out already pays exactly that over exactly these buckets.

        ``_default_`` is the entity-WIDE config scope, not a resource, and a
        caller passing it means "every resource this entity has a bucket for".
        No bucket item can ever carry the GSI3SK ``BUCKET#_default_#``, so
        forwarding it to discovery matched zero items and the whole sync was a
        silent no-op — the exact ``_default_`` -> unscoped translation the
        ADR-125 disable fan-out performs a few lines below (issue #487).
        Discovery therefore widens, and because Entity(resource) outranks
        Entity(``_default_``), each discovered bucket is stamped from the
        limits resolved for its OWN resource rather than from the caller's,
        exactly as `_fanout_entity` re-resolves `disabled` per bucket. See
        `_resolved_bucket_param_update` for what that implies for TTL and for
        the caller's stale-name set.

        Args:
            entity_id: ID of the entity
            resource: Resource name, or `schema.DEFAULT_RESOURCE` for the
                entity-wide scope (every resource the entity has a bucket for)
            limits: New limit configurations. Under the entity-wide scope these
                are the caller's *directive*, not what gets written: each
                bucket is written from its own resolved limits.
            bucket_ttl_refill_multiplier: TTL behavior (issue #327):
                - None: Don't change TTL
                - 0: REMOVE ttl (entity has custom limits)
                - >0: SET ttl to (now + calculated_seconds)
                Ignored under the entity-wide scope, where the resolved level
                decides per bucket.
            stale_limit_names: Limit names to REMOVE from bucket (issue #327).
                Used when downgrading from entity config to defaults where
                the old config had limits not present in the new defaults.

        Raises:
            FanoutIncomplete: A write failed part-way through. Carries the
                number of bucket items already written and the underlying
                error; the config item is committed either way, so re-running
                the same call reconciles the remainder.
        """
        if not limits:
            return

        # `_default_` names no bucket: widen discovery and re-resolve per
        # bucket resource (#487). A real resource is an unambiguous directive.
        unscoped = resource == schema.DEFAULT_RESOURCE
        plans: dict[str, tuple[str, dict[str, str], dict[str, dict[str, str]]]] = {}
        if not unscoped:
            plans[resource] = self._build_bucket_param_update(
                limits, bucket_ttl_refill_multiplier, stale_limit_names
            )

        # Two discovery passes, exactly like the ADR-125 disable fan-out: the
        # second catches a bucket created by an acquire that was already in
        # flight during the first.
        synced: set[str] = set()
        written = 0
        for _pass in range(2):
            for pk in await self._discover_entity_bucket_pks(
                entity_id, None if unscoped else resource
            ):
                if pk in synced:
                    continue
                if unscoped:
                    # Memoized per distinct resource, like `_fanout_entity`'s
                    # `effective_by_resource`: a 500-bucket entity spread over
                    # three resources resolves three times.
                    _ns, _eid, bucket_resource, _shard = schema.parse_bucket_pk(pk)
                    if bucket_resource not in plans:
                        plans[bucket_resource] = await self._resolved_bucket_param_update(
                            entity_id, bucket_resource, limits, stale_limit_names
                        )
                try:
                    if await self._sync_one_bucket_shard_from_plans(pk, plans):
                        written += 1
                except Exception as e:
                    # Config is already committed, so this is half-applied and
                    # nothing self-heals it. Report how far it got (ADR-125).
                    raise FanoutIncomplete(
                        written,
                        e,
                        resource=None if unscoped else resource,
                        entity_id=entity_id,
                        action="syncing",
                    ) from e
                synced.add(pk)

    async def _resolved_bucket_param_update(
        self,
        entity_id: str,
        bucket_resource: str,
        directive_limits: list[Limit],
        stale_limit_names: set[str] | None,
    ) -> tuple[str, dict[str, str], dict[str, dict[str, str]]]:
        """Build one bucket's update from the limits resolved for ITS resource.

        Used only under the entity-wide (`_default_`) scope. The caller's
        limits are a directive for the level it wrote, and a resource with its
        own entity config outranks that level; writing the caller's limits
        everywhere would clobber the more specific config with the less
        specific one (issue #487).

        Two things follow from resolving per bucket:

        * **TTL follows the level that answered.** Entity-level limits mean the
          bucket persists, resource/system defaults mean it expires and is
          recreated with current params (#271, #296). One `set_limits` call can
          touch buckets resolving at different levels, so the multiplier cannot
          be fixed at the call site.
        * **The caller's stale names are intersected with the resolution.**
          `delete_limits` computes them against `_default_`'s own fallback, so
          a limit the *deleted* config declared may still be declared by a
          resource's own entity config. Removing it there would strip a
          configured limit — and SET and REMOVE on one attribute in a single
          expression is a DynamoDB ValidationException. Conversely a directive
          limit absent from this resource's resolution is stale *here* even
          though the caller did not name it.
        """
        resolved, _on_unavailable, source = await self.resolve_limits(entity_id, bucket_resource)
        if not resolved:
            # Nothing applies anywhere any more. There is no correct value to
            # write, so leave the bucket alone — the same choice the delete
            # path makes when no fallback config exists at all.
            return "", {}, {}

        resolved_names = {limit.name for limit in resolved}
        stale = (set(stale_limit_names or ()) | {limit.name for limit in directive_limits}) - (
            resolved_names
        )
        entity_level = source in ("entity", "entity_default")
        multiplier = 0 if entity_level else self._bucket_ttl_refill_multiplier
        return self._build_bucket_param_update(resolved, multiplier, stale or None)

    def _build_bucket_param_update(
        self,
        limits: list[Limit],
        bucket_ttl_refill_multiplier: int | None,
        stale_limit_names: set[str] | None,
    ) -> tuple[str, dict[str, str], dict[str, dict[str, str]]]:
        """Build the SET/REMOVE UpdateExpression for one bucket item.

        Args:
            limits: Limits to stamp (stored undivided; the per-shard share is
                derived at read time by `BucketState.effective_*`)
            bucket_ttl_refill_multiplier: None leaves `ttl` alone, 0 REMOVEs it,
                >0 SETs it from the limits' max time-to-fill
            stale_limit_names: Limit names to strip from the bucket entirely

        Returns:
            `(update_expr, expr_names, expr_values)`
        """
        # Build SET expression for static bucket params
        # Use numeric index for expression names since limit names can contain hyphens
        set_parts: list[str] = []
        remove_parts: list[str] = []
        expr_names: dict[str, str] = {}
        expr_values: dict[str, dict[str, str]] = {}

        for i, limit in enumerate(limits):
            name = limit.name
            # Capacity (millitokens)
            cp_attr = schema.bucket_attr(name, schema.BUCKET_FIELD_CP)
            set_parts.append(f"#cp{i} = :cp{i}")
            expr_names[f"#cp{i}"] = cp_attr
            expr_values[f":cp{i}"] = {"N": str(limit.capacity * 1000)}

            # Refill amount (millitokens)
            ra_attr = schema.bucket_attr(name, schema.BUCKET_FIELD_RA)
            set_parts.append(f"#ra{i} = :ra{i}")
            expr_names[f"#ra{i}"] = ra_attr
            expr_values[f":ra{i}"] = {"N": str(limit.refill_amount * 1000)}

            # Refill period (milliseconds)
            rp_attr = schema.bucket_attr(name, schema.BUCKET_FIELD_RP)
            set_parts.append(f"#rp{i} = :rp{i}")
            expr_names[f"#rp{i}"] = rp_attr
            expr_values[f":rp{i}"] = {"N": str(limit.refill_period_seconds * 1000)}

            # Duration window length in seconds (ADR-139). SET where the
            # resolved limit has one, REMOVE where it does not. Absence means
            # "this limit has no duration window", full stop — there is no
            # item-level default to inherit, so this needs no
            # BUCKET_SCHED_NONE analogue (#541). A `rsa` left behind on a
            # limit converted back to a drip would keep the item
            # reconstructing as a quota forever.
            #
            # `ws` (window start) is deliberately NOT written here. A config
            # change is not a rollover, and stamping it would restart every
            # caller's window on an unrelated edit — the failure ADR-138
            # warned about for a window read off `vu`, and the reason
            # ADR-139 keeps the anchor in its own attribute rather than
            # deriving it there. The `vu = 0` this write already stamps
            # unconditionally (below) forces exactly one materialising pass,
            # which anchors a first window if none exists yet
            # (`_open_window_if_elapsed`'s `end is None` branch) or leaves an
            # existing one alone if it has not elapsed.
            rsa_attr = schema.bucket_attr(name, schema.BUCKET_FIELD_RSA)
            expr_names[f"#rsa{i}"] = rsa_attr
            if limit.reset_after_seconds is not None:
                set_parts.append(f"#rsa{i} = :rsa{i}")
                expr_values[f":rsa{i}"] = {"N": str(limit.reset_after_seconds)}
            else:
                remove_parts.append(f"#rsa{i}")

        # Re-stamp both schedules (#222 §2.2, §3.6). The aggregator reads the
        # item and nothing else, so a bucket left holding a superseded `sched`
        # is refilled toward a ceiling the operator has already changed, and
        # one left holding a superseded `rsched` keeps resetting on a calendar
        # nobody asked for any more.
        encoded = self._encode_item_schedules(
            [(limit.name, limit.schedule, limit.reset_schedule) for limit in limits]
        )
        # `sched_tz` is shared by both tuples, so it is decided once, from
        # whether *anything* on the item is scheduled. Deciding it inside the
        # parameter branch would REMOVE it for a quota carrying only a reset —
        # and the stored `rsched` would then decode as UTC forever — or, worse,
        # SET and REMOVE it in one expression (#488).
        expr_names["#sched_tz"] = schema.BUCKET_FIELD_SCHED_TZ
        if encoded is None:
            tz, param, reset = None, None, None
            remove_parts.append("#sched_tz")
        else:
            tz, param, reset = encoded
            set_parts.append("#sched_tz = :sched_tz")
            expr_values[":sched_tz"] = {"S": tz}

        # Per-limit overrides are SET where a limit differs from the item
        # default and REMOVEd everywhere else — including on the scheduled
        # branch. Absence means "inherit the item default", so a limit that
        # used to carry its own schedule and now shares the default keeps
        # enforcing the superseded one forever unless its override is
        # stripped. A limit that now has *no* schedule is not in that class:
        # it gets `BUCKET_SCHED_NONE` SET rather than its override REMOVEd
        # (#541), because removing it would make it inherit the default
        # instead. Each alias lands in exactly one of the two lists, never
        # both (#488).
        for prefix, field, part in (
            ("sched", schema.BUCKET_FIELD_SCHED, param),
            ("rsched", schema.BUCKET_FIELD_RSCHED, reset),
        ):
            item_alias = f"#{prefix}"
            expr_names[item_alias] = field
            if part is None:
                overrides: dict[str, str] = {}
                remove_parts.append(item_alias)
            else:
                default_compact, overrides = part
                set_parts.append(f"{item_alias} = :{prefix}")
                expr_values[f":{prefix}"] = {"S": default_compact}
            for i, limit in enumerate(limits):
                alias = f"#l{prefix}{i}"
                expr_names[alias] = schema.bucket_attr(limit.name, field)
                compact = overrides.get(limit.name)
                if compact is None:
                    remove_parts.append(alias)
                else:
                    set_parts.append(f"{alias} = :l{prefix}{i}")
                    expr_values[f":l{prefix}{i}"] = {"S": compact}

        # Outside both branches, so it runs on EVERY fan-out, scheduled or
        # not: force exactly one materialising pass, which clamps any surplus
        # over a lowered ceiling before the fast path can spend it. #496 made
        # `refill_bucket` clamp on its early-return paths, but the speculative
        # fast path is a pure ADD with no cap maths, so nothing else trims a
        # bucket after a `set_limits` capacity shrink — this is what lets #222
        # subsume #469 completely rather than partially. Nesting it inside the
        # `if` above would leave every *unscheduled* entity exposed, which is
        # most of them. `#vu` is SET here and must therefore never join
        # `remove_parts` above: SET and REMOVE on one attribute in a single
        # UpdateExpression is the ValidationException #488 hit.
        set_parts.append("#vu = :vu_zero")
        expr_names["#vu"] = schema.BUCKET_FIELD_VU
        expr_values[":vu_zero"] = {"N": "0"}

        # Handle TTL update (issue #327)
        if bucket_ttl_refill_multiplier is not None:
            expr_names["#ttl"] = "ttl"
            if bucket_ttl_refill_multiplier > 0:
                ttl_seconds = schema.calculate_bucket_ttl_seconds(
                    limits, bucket_ttl_refill_multiplier
                )
                if ttl_seconds is not None:
                    now_ms = self._now_ms()
                    set_parts.append("#ttl = :ttl_val")
                    expr_values[":ttl_val"] = {"N": str(schema.calculate_ttl(now_ms, ttl_seconds))}
            else:
                # REMOVE ttl (entity has custom limits, bucket should persist)
                remove_parts.append("#ttl")

        # Handle stale limit attribute removal (issue #327)
        # Note: RF (last_refill_ms) is shared across all limits in a composite
        # bucket and must NOT be removed when individual limits are stale.
        #
        # Monotonic counters, NOT the stale name: `NAME_PATTERN` allows `-` and
        # `.` in a limit name and neither is legal in an expression attribute
        # alias (`.` is parsed as a document-path separator), so interpolating
        # the name builds an expression DynamoDB rejects with a
        # ValidationException — after the config item has already been written.
        # `sorted` only to keep the expression deterministic for tests.
        # The provisioner mirror does the same (`bucket_sync.py`).
        for i, stale_name in enumerate(sorted(stale_limit_names or ())):
            for j, field in enumerate(
                (
                    schema.BUCKET_FIELD_TK,
                    schema.BUCKET_FIELD_CP,
                    schema.BUCKET_FIELD_RA,
                    schema.BUCKET_FIELD_RP,
                    schema.BUCKET_FIELD_TC,
                    # A dropped limit's own schedule overrides go with it.
                    # Left behind they are orphan state that re-attaches the
                    # moment a limit of that name is configured again.
                    schema.BUCKET_FIELD_SCHED,
                    schema.BUCKET_FIELD_RSCHED,
                )
            ):
                alias = f"#stale{i}_{j}"
                expr_names[alias] = schema.bucket_attr(stale_name, field)
                remove_parts.append(alias)

        # Build update expression
        update_expr = f"SET {', '.join(set_parts)}"
        if remove_parts:
            update_expr += f" REMOVE {', '.join(remove_parts)}"
        return update_expr, expr_names, expr_values

    async def _sync_one_bucket_shard_from_plans(
        self,
        pk: str,
        plans: dict[str, tuple[str, dict[str, str], dict[str, dict[str, str]]]],
    ) -> bool:
        """Apply the plan built for this bucket's own resource.

        Args:
            pk: Full bucket partition key (namespace- and shard-qualified)
            plans: Prepared updates keyed by resource; every discovered bucket's
                resource has an entry. An entry with an empty expression is
                skipped — nothing resolved for that resource, so there is no
                correct value to write.

        Returns:
            True if a bucket item was written.
        """
        _ns, _eid, bucket_resource, _shard = schema.parse_bucket_pk(pk)
        update_expr, expr_names, expr_values = plans[bucket_resource]
        if not update_expr:
            return False
        return await self._sync_one_bucket_shard(pk, update_expr, expr_names, expr_values)

    async def _sync_one_bucket_shard(
        self,
        pk: str,
        update_expr: str,
        expr_names: dict[str, str],
        expr_values: dict[str, dict[str, str]],
    ) -> bool:
        """Apply one shard's static-param update, tolerating a vanished shard.

        Args:
            pk: Full bucket partition key (namespace- and shard-qualified)
            update_expr: SET/REMOVE expression built by `_sync_bucket_params`
            expr_names: Expression attribute name aliases
            expr_values: Expression attribute values

        Returns:
            True if the item was written, False if the shard had vanished.
        """
        client = await self._get_client()
        try:
            await client.update_item(
                TableName=self.table_name,
                Key={"PK": {"S": pk}, "SK": {"S": schema.sk_state()}},
                UpdateExpression=update_expr,
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeNames=expr_names,
                ExpressionAttributeValues=expr_values,
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # Bucket does not exist (yet, or any more: TTL can expire a
                # shard between discovery and this write). Either way there is
                # nothing to reconcile — a bucket created later is created with
                # the current params.
                return False
            raise
        return True

    async def reconcile_bucket_to_defaults(
        self,
        entity_id: str,
        resource: str,
        effective_limits: list[Limit],
        stale_limit_names: set[str] | None = None,
    ) -> None:
        """Reconcile bucket to effective defaults after config deletion (issue #327).

        Updates limit fields (cp, ra, rp) to match the new effective
        limits, sets TTL (since entity is now on defaults), and removes
        stale limit attributes that no longer exist in the effective config.

        No-op if bucket doesn't exist (uses attribute_exists(PK) condition).

        Args:
            entity_id: ID of the entity
            resource: Resource name
            effective_limits: The new effective limits (resource/system defaults)
            stale_limit_names: Limit names to REMOVE from bucket (limits that
                were in the deleted entity config but not in effective defaults)
        """
        await self._sync_bucket_params(
            entity_id,
            resource,
            effective_limits,
            bucket_ttl_refill_multiplier=self._bucket_ttl_refill_multiplier,
            stale_limit_names=stale_limit_names,
        )

    async def _cleanup_entity_config_registry(self, resource: str) -> None:
        """Remove resource from entity config registry if count <= 0.

        Called after decrementing to clean up zero/negative counts.
        Uses conditional REMOVE to avoid race conditions.
        """
        client = await self._get_client()

        try:
            await client.update_item(
                TableName=self.table_name,
                Key={
                    "PK": {"S": schema.pk_system(self._namespace_id)},
                    "SK": {"S": schema.sk_entity_config_resources()},
                },
                UpdateExpression="REMOVE #resource",
                ConditionExpression="#resource <= :zero",
                ExpressionAttributeNames={"#resource": resource},
                ExpressionAttributeValues={":zero": {"N": "0"}},
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            # Count > 0, which is expected - nothing to clean up

    async def get_limits(
        self,
        entity_id: str,
        resource: str = schema.DEFAULT_RESOURCE,
    ) -> list[Limit]:
        """Get stored limit configs for an entity (composite format, ADR-114).

        Reads a single composite item with SK '#CONFIG#{resource}' containing
        all limits for the entity+resource pair.
        """
        client = await self._get_client()

        # ADR-105: Use eventually consistent reads for config (0.5 RCU vs 1 RCU)
        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_entity(self._namespace_id, entity_id)},
                "SK": {"S": schema.sk_config(resource)},
            },
            ConsistentRead=False,
        )

        item = response.get("Item")
        if not item:
            return []

        return self._deserialize_composite_limits(item)

    async def get_entity_disabled(self, entity_id: str, resource: str) -> bool | None:
        """Read the tri-state disabled flag from an entity config item.

        Returns:
            True or False when explicitly set, None when unset (inherit).
        """
        client = await self._get_client()
        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_entity(self._namespace_id, entity_id)},
                "SK": {"S": schema.sk_config(resource)},
            },
            ConsistentRead=False,
        )
        item = response.get("Item")
        if not item:
            return None
        return schema.decode_disabled(item)

    async def delete_limits(
        self,
        entity_id: str,
        resource: str = schema.DEFAULT_RESOURCE,
        principal: str | None = None,
    ) -> None:
        """
        Delete stored limit configs for an entity (composite format, ADR-114).

        Deletes the single composite config item for this entity+resource.

        Args:
            entity_id: ID of the entity
            resource: Resource name (defaults to "_default_")
            principal: Caller identity for audit logging
        """
        client = await self._get_client()

        # Does this config actually decide `disabled`? If not, deleting it
        # cannot change resolve_disabled()'s answer and the fan-out below is
        # pure waste (two GSI3 discovery passes, a resolve per entity and an
        # UpdateItem per bucket, all serial).
        #
        # delete_resource_defaults gets this for free from its DeleteItem's
        # ALL_OLD image, but this path deletes inside a TransactWriteItems,
        # and transactions return no old image on success — the per-item
        # Delete shape only offers ReturnValuesOnConditionCheckFailure. So it
        # costs one projected GetItem (~0.5 RCU) to avoid a fan-out
        # proportional to the entity's bucket count. `disabled` is a DynamoDB
        # reserved word, hence the alias.
        existing = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_entity(self._namespace_id, entity_id)},
                "SK": {"S": schema.sk_config(resource)},
            },
            ProjectionExpression="#disabled",
            ExpressionAttributeNames={"#disabled": schema.CONFIG_FIELD_DISABLED},
        )
        had_disabled = schema.CONFIG_FIELD_DISABLED in (existing.get("Item") or {})

        # Use transaction to atomically delete config + decrement registry (issue #288)
        # This prevents double-decrement if delete_limits is called twice
        try:
            await client.transact_write_items(
                TransactItems=[
                    {
                        "Delete": {
                            "TableName": self.table_name,
                            "Key": {
                                "PK": {"S": schema.pk_entity(self._namespace_id, entity_id)},
                                "SK": {"S": schema.sk_config(resource)},
                            },
                            "ConditionExpression": "attribute_exists(PK)",
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.table_name,
                            "Key": {
                                "PK": {"S": schema.pk_system(self._namespace_id)},
                                "SK": {"S": schema.sk_entity_config_resources()},
                            },
                            "UpdateExpression": "ADD #resource :minus_one",
                            "ExpressionAttributeNames": {"#resource": resource},
                            "ExpressionAttributeValues": {":minus_one": {"N": "-1"}},
                        }
                    },
                ]
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "TransactionCanceledException":
                # Check if cancellation was due to condition failure (item doesn't exist)
                reasons = e.response.get("CancellationReasons", [])
                if reasons and reasons[0].get("Code") == "ConditionalCheckFailed":
                    # Config doesn't exist - nothing to delete, skip audit
                    return
                else:
                    raise
            else:
                raise

        # Cleanup: remove registry attribute if count <= 0
        await self._cleanup_entity_config_registry(resource)

        # Auto-evict from config cache so next resolve reads fresh config (ADR-122)
        self._config_cache.evict_entity(entity_id, resource)

        # The deleted item is where this level's `disabled` lived, so removing
        # it can change resolve_disabled()'s answer in either direction. Re-stamp
        # so the denormalized bucket flag never contradicts the resolution
        # (ADR-125). Deleting the `_default_` config is an entity-wide change, so
        # it fans out unscoped and each bucket's own resource is re-resolved.
        # Only restamp when the deleted config was actually carrying a
        # `disabled` value; otherwise the resolution is unchanged and every
        # bucket already holds the right stamp. An explicit `disabled: false`
        # counts as present — dropping a carve-out re-disables the entity.
        if had_disabled:
            if resource == schema.DEFAULT_RESOURCE:
                await self._fanout_entity(entity_id, None, disabled=False)
            else:
                effective, _level = await self.resolve_disabled(entity_id, resource)
                await self._fanout_entity(entity_id, resource, disabled=effective)

        # Log audit event
        await self._log_audit_event(
            action=AuditAction.LIMITS_DELETED,
            entity_id=entity_id,
            principal=principal,
            resource=resource,
        )

    async def list_entities_with_custom_limits(
        self,
        resource: str,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> tuple[list[str], str | None]:
        """
        List all entities that have custom limit configurations for a resource.

        Uses GSI3 sparse index for efficient queries. Only entity-level configs
        have GSI3 attributes, so this query returns only entities with custom
        limits (not system or resource defaults).

        Args:
            resource: Resource to filter by (required).
            limit: Maximum number of entities to return. None for all.
            cursor: Pagination cursor from previous call. None for first page.

        Returns:
            Tuple of (entity_ids, next_cursor). next_cursor is None if no more results.
        """
        import base64
        import json

        client = await self._get_client()

        query_params: dict[str, Any] = {
            "TableName": self.table_name,
            "IndexName": schema.GSI3_NAME,
            "KeyConditionExpression": "GSI3PK = :pk",
            "ExpressionAttributeValues": {
                ":pk": {"S": schema.gsi3_pk_entity_config(self._namespace_id, resource)}
            },
        }

        if limit is not None:
            query_params["Limit"] = limit
        if cursor is not None:
            # Decode cursor (base64 encoded LastEvaluatedKey)
            query_params["ExclusiveStartKey"] = json.loads(base64.b64decode(cursor))

        response = await client.query(**query_params)

        entity_ids: list[str] = []
        for item in response.get("Items", []):
            entity_id = item.get("GSI3SK", {}).get("S")
            if entity_id:
                entity_ids.append(entity_id)

        # Encode next cursor if more results
        next_cursor: str | None = None
        if "LastEvaluatedKey" in response:
            next_cursor = base64.b64encode(
                json.dumps(response["LastEvaluatedKey"]).encode()
            ).decode()

        return entity_ids, next_cursor

    async def list_resources_with_entity_configs(self) -> list[str]:
        """
        List all resources that have entity-level custom limit configs.

        Uses the entity config resources registry (wide column with ref counts)
        for efficient O(1) lookup. Returns resources with count > 0.

        Returns:
            Sorted list of resource names with at least one entity having custom limits
        """
        client = await self._get_client()

        # Read from entity config resources registry (single GetItem: 1 RCU)
        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_system(self._namespace_id)},
                "SK": {"S": schema.sk_entity_config_resources()},
            },
            ConsistentRead=False,
        )

        # Extract resource names from numeric attributes (wide column pattern)
        item = response.get("Item", {})
        resources = []
        for attr_name, attr_value in item.items():
            # Skip key attributes
            if attr_name in ("PK", "SK"):
                continue
            # Include resources with count > 0
            count_str = attr_value.get("N")
            if count_str is not None and int(count_str) > 0:
                resources.append(attr_name)

        return sorted(resources)

    # -------------------------------------------------------------------------
    # Resource-level limit config operations (composite format, ADR-114)
    # -------------------------------------------------------------------------

    async def set_resource_defaults(
        self,
        resource: str,
        limits: list[Limit],
        principal: str | None = None,
        *,
        disabled: bool | None = _PRESERVE_DISABLED,
    ) -> None:
        """
        Store default limit configs for a resource (composite format, ADR-114).

        All limits for a resource are stored in a single composite item
        with SK '#CONFIG'. This reduces cache-miss cost.

        Args:
            resource: Resource name
            limits: List of Limit configurations to store
            principal: Caller identity for audit logging
            disabled: Tri-state disabled flag. Defaults to preserving whatever
                value is already stored (this is a full-replace PutItem, so an
                explicit value must be passed to change it; see ADR-125).
                Passing an explicit value also fans out to existing buckets,
                exactly as `disable_resource()`/`enable_resource()` do.
        """
        validate_resource(resource)
        client = await self._get_client()

        # Full-replace PutItem would drop `disabled`; preserve it unless the
        # caller passed an explicit value (ADR-125).
        disabled_explicit = disabled is not _PRESERVE_DISABLED
        if not disabled_explicit:
            disabled = await self.get_resource_disabled(resource)

        # Build composite config item with all limits
        item: dict[str, Any] = {
            "PK": {"S": schema.pk_resource(self._namespace_id, resource)},
            "SK": {"S": schema.sk_config()},
            "resource": {"S": resource},
            "config_version": {"N": "1"},
            # GSI4: namespace-scoped item discovery
            "GSI4PK": {"S": self._namespace_id},
            "GSI4SK": {"S": schema.pk_resource(self._namespace_id, resource)},
        }

        # Add l_* attributes for each limit
        self._serialize_composite_limits(limits, item)

        disabled_attr = schema.encode_disabled(disabled)
        if disabled_attr is not None:
            item[schema.CONFIG_FIELD_DISABLED] = disabled_attr

        # Single PutItem replaces any existing config for this resource
        await client.put_item(TableName=self.table_name, Item=item)

        # Add resource to the registry using atomic ADD operation
        await client.update_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_system(self._namespace_id)},
                "SK": {"S": schema.sk_resources()},
            },
            UpdateExpression=(
                "SET GSI4PK = if_not_exists(GSI4PK, :gsi4pk),"
                " GSI4SK = if_not_exists(GSI4SK, :gsi4sk)"
                " ADD resources :resource"
            ),
            ExpressionAttributeValues={
                ":resource": {"SS": [resource]},
                ":gsi4pk": {"S": self._namespace_id},
                ":gsi4sk": {"S": schema.pk_system(self._namespace_id)},
            },
        )

        # An explicit `disabled` changes what resolve_disabled() answers, so the
        # denormalized bucket stamp has to follow it eagerly — same contract as
        # disable_resource()/enable_resource() (ADR-125). With the preserve
        # sentinel the stored value is unchanged, so no fan-out is needed.
        if disabled_explicit:
            await self._fanout_resource(resource, disabled=bool(disabled))

        # Log audit event with special prefix
        await self._log_audit_event(
            action=AuditAction.LIMITS_SET,
            entity_id=f"$RESOURCE:{resource}",
            principal=principal,
            resource=resource,
            details={"limits": [limit.to_dict() for limit in limits]},
        )

    async def get_resource_defaults(
        self,
        resource: str,
    ) -> list[Limit]:
        """Get stored default limit configs for a resource (composite format, ADR-114).

        Reads a single composite item with SK '#CONFIG' containing all limits
        for the resource.
        """
        validate_resource(resource)
        client = await self._get_client()

        # ADR-105: Use eventually consistent reads for config (0.5 RCU vs 1 RCU)
        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_resource(self._namespace_id, resource)},
                "SK": {"S": schema.sk_config()},
            },
            ConsistentRead=False,
        )

        item = response.get("Item")
        if not item:
            return []

        return self._deserialize_composite_limits(item)

    async def get_resource_disabled(self, resource: str) -> bool | None:
        """Read the tri-state disabled flag from a resource config item.

        Returns:
            True or False when explicitly set, None when unset (inherit).
        """
        validate_resource(resource)
        client = await self._get_client()
        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_resource(self._namespace_id, resource)},
                "SK": {"S": schema.sk_config()},
            },
            ConsistentRead=False,
        )
        item = response.get("Item")
        if not item:
            return None
        return schema.decode_disabled(item)

    async def delete_resource_defaults(
        self,
        resource: str,
        principal: str | None = None,
    ) -> None:
        """
        Delete stored default limit configs for a resource (composite format, ADR-114).

        Deletes the single composite config item for this resource.

        Args:
            resource: Resource name
            principal: Caller identity for audit logging
        """
        validate_resource(resource)
        client = await self._get_client()

        # Single DeleteItem removes the composite config. ALL_OLD returns the
        # deleted image at no extra capacity charge, which is what decides
        # whether the fan-out below is needed at all.
        deleted = await client.delete_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_resource(self._namespace_id, resource)},
                "SK": {"S": schema.sk_config()},
            },
            ReturnValues="ALL_OLD",
        )
        had_disabled = schema.CONFIG_FIELD_DISABLED in (deleted.get("Attributes") or {})

        # Remove resource from the registry using atomic DELETE operation
        await client.update_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_system(self._namespace_id)},
                "SK": {"S": schema.sk_resources()},
            },
            UpdateExpression="DELETE resources :resource",
            ExpressionAttributeValues={
                ":resource": {"SS": [resource]},
            },
        )

        # The deleted item is where the resource's `disabled` lived, so removing
        # it can change resolve_disabled()'s answer. With no level above resource
        # in the walk, the resource now resolves to "not disabled"; entities with
        # their own override are skipped by _fanout_resource and keep their stamp
        # (ADR-125).
        #
        # If the deleted item carried no `disabled` attribute at all, it was
        # never the deciding level, so the resolution is unchanged and every
        # bucket's stamp is already correct. Skipping is not an optimisation
        # guess: `had_disabled` comes from the image of the item actually
        # deleted, so there is no window in which it could be stale. An
        # explicit `disabled: false` counts as present — removing a resource
        # level re-enable does change the resolution.
        if had_disabled:
            await self._fanout_resource(resource, disabled=False)

        # Log audit event
        await self._log_audit_event(
            action=AuditAction.LIMITS_DELETED,
            entity_id=f"$RESOURCE:{resource}",
            principal=principal,
            resource=resource,
        )

    async def list_resources_with_defaults(self) -> list[str]:
        """List all resources that have default limit configs from the resource registry."""
        client = await self._get_client()

        # Read from resource registry (single GetItem: 1 RCU)
        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_system(self._namespace_id)},
                "SK": {"S": schema.sk_resources()},
            },
            ConsistentRead=False,
        )

        # Extract resources from the string set, or return empty list if registry doesn't exist
        item = response.get("Item", {})
        resources_set = item.get("resources", {}).get("SS", [])
        return sorted(resources_set)

    # -------------------------------------------------------------------------
    # System-level default config operations (composite format, ADR-114)
    # -------------------------------------------------------------------------

    async def set_system_defaults(
        self,
        limits: list[Limit],
        on_unavailable: OnUnavailableAction | None = None,
        principal: str | None = None,
    ) -> None:
        """
        Store system-wide default limits and config (composite format, ADR-114).

        All system limits and config (on_unavailable) are stored in a single
        composite item with SK '#CONFIG'. This reduces cache-miss cost.

        Args:
            limits: List of Limit configurations (apply to all resources)
            on_unavailable: Behavior when DynamoDB unavailable ("allow" or "block")
            principal: Caller identity for audit logging
        """
        client = await self._get_client()

        # Build composite config item with all limits + on_unavailable
        item: dict[str, Any] = {
            "PK": {"S": schema.pk_system(self._namespace_id)},
            "SK": {"S": schema.sk_config()},
            "config_version": {"N": "1"},
        }

        # Add on_unavailable if provided
        if on_unavailable is not None:
            item["on_unavailable"] = {"S": on_unavailable}

        # Add l_* attributes for each limit
        self._serialize_composite_limits(limits, item)

        # GSI4: namespace-scoped item discovery
        item["GSI4PK"] = {"S": self._namespace_id}
        item["GSI4SK"] = {"S": schema.pk_system(self._namespace_id)}

        # Single PutItem replaces any existing system config
        await client.put_item(TableName=self.table_name, Item=item)

        # Log audit event (ADR-106: use $SYSTEM for all system-level events)
        await self._log_audit_event(
            action=AuditAction.LIMITS_SET,
            entity_id="$SYSTEM",
            principal=principal,
            details={
                "limits": [limit.to_dict() for limit in limits],
                "on_unavailable": on_unavailable,
            },
        )

    async def get_system_defaults(self) -> tuple[list[Limit], OnUnavailableAction | None]:
        """
        Get system-wide default limits and config (composite format, ADR-114).

        Reads a single composite item with SK '#CONFIG' containing all limits
        and on_unavailable setting.

        Returns:
            Tuple of (limits, on_unavailable). on_unavailable may be None if not set.
        """
        client = await self._get_client()

        # ADR-105: Use eventually consistent reads for config (0.5 RCU vs 1 RCU)
        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_system(self._namespace_id)},
                "SK": {"S": schema.sk_config()},
            },
            ConsistentRead=False,
        )

        item = response.get("Item")
        if not item:
            return [], None

        # Extract limits from composite attributes
        limits = self._deserialize_composite_limits(item)

        # Extract on_unavailable
        on_unavailable_attr = item.get("on_unavailable", {})
        on_unavailable: OnUnavailableAction | None = (
            cast(OnUnavailableAction, on_unavailable_attr.get("S"))
            if on_unavailable_attr and on_unavailable_attr.get("S")
            else None
        )

        return limits, on_unavailable

    async def delete_system_defaults(
        self,
        principal: str | None = None,
    ) -> None:
        """
        Delete all system-wide default limits and config (composite format, ADR-114).

        Deletes the single composite config item for system defaults.

        Args:
            principal: Caller identity for audit logging
        """
        client = await self._get_client()

        # Get existing limits for audit logging before deleting
        limits, on_unavailable = await self.get_system_defaults()

        # Single DeleteItem removes the composite config
        await client.delete_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_system(self._namespace_id)},
                "SK": {"S": schema.sk_config()},
            },
        )

        # Log audit event (ADR-106: use $SYSTEM for all system-level events)
        await self._log_audit_event(
            action=AuditAction.LIMITS_DELETED,
            entity_id="$SYSTEM",
            principal=principal,
            details={
                "limits": [limit.name for limit in limits],
                "on_unavailable": on_unavailable,
            },
        )

    async def get_system_limits(self) -> list[Limit]:
        """Get system-wide default limits (without config).

        This is a convenience method that returns only the limits.
        Use get_system_defaults() to also get on_unavailable config.
        """
        limits, _ = await self.get_system_defaults()
        return limits

    # -------------------------------------------------------------------------
    # Provisioner state (declarative limits management, Issue #405)
    # -------------------------------------------------------------------------

    async def get_provisioner_state(self) -> dict[str, Any]:
        """Get the provisioner state record for this namespace.

        Returns:
            Dict with keys: managed_system, managed_resources, managed_entities,
            last_applied, applied_hash. Returns empty state if no record exists.
        """
        client = await self._get_client()
        result = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_system(self._namespace_id)},
                "SK": {"S": schema.sk_provisioner()},
            },
        )
        item = result.get("Item")
        if not item:
            return {
                "managed_system": False,
                "managed_resources": [],
                "managed_entities": {},
                "last_applied": None,
                "applied_hash": None,
            }

        managed_entities: dict[str, list[str]] = {}
        raw_entities = item.get("managed_entities", {}).get("M", {})
        for entity_id, resources_attr in raw_entities.items():
            managed_entities[entity_id] = [r["S"] for r in resources_attr.get("L", [])]

        return {
            "managed_system": item.get("managed_system", {}).get("BOOL", False),
            "managed_resources": [r["S"] for r in item.get("managed_resources", {}).get("L", [])],
            "managed_entities": managed_entities,
            "last_applied": item.get("last_applied", {}).get("S"),
            "applied_hash": item.get("applied_hash", {}).get("S"),
        }

    async def put_provisioner_state(self, state: dict[str, Any]) -> None:
        """Write the provisioner state record for this namespace.

        Args:
            state: Dict with keys: managed_system, managed_resources,
                   managed_entities, last_applied, applied_hash.
        """
        client = await self._get_client()
        item: dict[str, Any] = {
            "PK": {"S": schema.pk_system(self._namespace_id)},
            "SK": {"S": schema.sk_provisioner()},
            "GSI4PK": {"S": self._namespace_id},
            "managed_system": {"BOOL": state["managed_system"]},
            "managed_resources": {"L": [{"S": r} for r in state["managed_resources"]]},
            "managed_entities": {
                "M": {
                    entity_id: {"L": [{"S": r} for r in resources]}
                    for entity_id, resources in state["managed_entities"].items()
                }
            },
            "last_applied": {"S": state["last_applied"]},
            "applied_hash": {"S": state["applied_hash"]},
        }
        await client.put_item(TableName=self.table_name, Item=item)

    # -------------------------------------------------------------------------
    # Audit retention configuration
    # -------------------------------------------------------------------------

    async def _write_audit_retention_config(self) -> None:
        """
        Write audit_retention_days to system config item.

        Uses atomic UpdateItem to avoid overwriting other system config fields.
        Called from ensure_infrastructure() after stack creation.
        """
        if self._stack_options is None:
            return
        client = await self._get_client()
        await client.update_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_system(self._namespace_id)},
                "SK": {"S": schema.sk_config()},
            },
            UpdateExpression="SET audit_retention_days = :ard",
            ExpressionAttributeValues={
                ":ard": {"N": str(self._stack_options.audit_retention_days)},
            },
        )
        # Update cache
        self._audit_retention_days_cache = self._stack_options.audit_retention_days

    async def _get_audit_retention_days(self) -> int:
        """
        Get audit retention days from system config or default.

        Returns cached value if available, otherwise reads from DynamoDB.
        Falls back to stack_options if available (saves DynamoDB call).
        Default is 90 days if not configured anywhere.
        """
        if self._audit_retention_days_cache is not None:
            return self._audit_retention_days_cache

        # Try to read from stack_options first (saves DynamoDB call)
        if self._stack_options is not None:
            self._audit_retention_days_cache = self._stack_options.audit_retention_days
            return self._audit_retention_days_cache

        # Read from DynamoDB system config
        client = await self._get_client()
        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_system(self._namespace_id)},
                "SK": {"S": schema.sk_config()},
            },
            ConsistentRead=False,
        )

        item = response.get("Item", {})
        ard = item.get("audit_retention_days", {}).get("N")
        self._audit_retention_days_cache = int(ard) if ard else 90  # Default 90 days
        return self._audit_retention_days_cache

    # -------------------------------------------------------------------------
    # Version record operations
    # -------------------------------------------------------------------------

    async def get_version_record(self) -> dict[str, Any] | None:
        """
        Get the infrastructure version record.

        Returns:
            Version record with schema_version, lambda_version, etc.
            None if no version record exists.
        """
        client = await self._get_client()

        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_system(schema.RESERVED_NAMESPACE)},
                "SK": {"S": schema.sk_version()},
            },
        )

        item = response.get("Item")
        if not item:
            return None

        result: dict[str, Any] = {}
        for key in (
            "schema_version",
            "lambda_version",
            "client_min_version",
            "updated_at",
            "updated_by",
        ):
            if key in item:
                result[key] = self._deserialize_value(item[key])
        return result

    async def ping(self) -> bool:
        """
        Check if the DynamoDB table is reachable.

        Performs a lightweight GetItem operation to verify connectivity.
        Does not verify the table is initialized or has valid data.

        Returns:
            True if the table is reachable, False otherwise.
        """
        try:
            client = await self._get_client()
            await client.get_item(
                TableName=self.table_name,
                Key={
                    "PK": {"S": schema.pk_system(schema.RESERVED_NAMESPACE)},
                    "SK": {"S": schema.sk_version()},
                },
            )
            return True
        except Exception:
            return False

    async def set_version_record(
        self,
        schema_version: str,
        lambda_version: str | None = None,
        client_min_version: str = "0.0.0",
        updated_by: str | None = None,
    ) -> None:
        """
        Set the infrastructure version record.

        Args:
            schema_version: Current schema version (e.g., "1.0.0")
            lambda_version: Currently deployed Lambda version
            client_min_version: Minimum compatible client version
            updated_by: Identifier of what performed the update
        """
        client = await self._get_client()
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Flat schema (v0.6.0+)
        # Version record is table-level (one per table), uses RESERVED_NAMESPACE
        item: dict[str, Any] = {
            "PK": {"S": schema.pk_system(schema.RESERVED_NAMESPACE)},
            "SK": {"S": schema.sk_version()},
            "schema_version": {"S": schema_version},
            "client_min_version": {"S": client_min_version},
            "updated_at": {"S": now},
            "lambda_version": {"S": lambda_version} if lambda_version else {"NULL": True},
            "updated_by": {"S": updated_by} if updated_by else {"NULL": True},
            # GSI4: table-level item discovery via RESERVED_NAMESPACE
            "GSI4PK": {"S": schema.RESERVED_NAMESPACE},
            "GSI4SK": {"S": schema.pk_system(schema.RESERVED_NAMESPACE)},
        }

        await client.put_item(TableName=self.table_name, Item=item)

    # -------------------------------------------------------------------------
    # Audit logging operations
    # -------------------------------------------------------------------------

    def _generate_event_id(self) -> str:
        """Generate a unique event ID using ULID (monotonic, collision-free)."""
        return str(ULID())

    async def _log_audit_event(
        self,
        action: str,
        entity_id: str,
        principal: str | None = None,
        resource: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """
        Log an audit event to DynamoDB.

        Args:
            action: Type of action (see AuditAction constants)
            entity_id: ID of the entity affected
            principal: Caller identity who performed the action. If None,
                auto-detects from AWS STS caller identity (lazy cached).
            resource: Resource name for limit-related actions
            details: Additional action-specific details

        Returns:
            The created AuditEvent

        Raises:
            InvalidIdentifierError: If principal is invalid
        """
        # Auto-detect principal from AWS caller identity if not provided
        if principal is None:
            principal = await self._get_caller_identity_arn()

        # Validate principal if provided (skip validation for ARNs from STS)
        # ARNs contain colons which would fail identifier validation
        # Only validate user-provided principals that aren't ARNs
        if principal is not None and not principal.startswith("arn:"):
            validate_identifier(principal, "principal")

        # Get TTL from system config (cached)
        audit_retention_days = await self._get_audit_retention_days()
        ttl_seconds = audit_retention_days * 86400

        client = await self._get_client()
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        event_id = self._generate_event_id()

        event = AuditEvent(
            event_id=event_id,
            timestamp=now,
            action=action,
            entity_id=entity_id,
            principal=principal,
            resource=resource,
            details=details or {},
        )

        # Build DynamoDB item (flat schema v0.6.0+)
        item: dict[str, Any] = {
            "PK": {"S": schema.pk_audit(self._namespace_id, entity_id)},
            "SK": {"S": schema.sk_audit(event_id)},
            "entity_id": {"S": entity_id},
            "event_id": {"S": event_id},
            "timestamp": {"S": now},
            "action": {"S": action},
            "principal": {"S": principal} if principal else {"NULL": True},
            "resource": {"S": resource} if resource else {"NULL": True},
            "details": {"M": self._serialize_map(details or {})},
            "ttl": {"N": str(schema.calculate_ttl(self._now_ms(), ttl_seconds))},
            # GSI4: namespace-scoped item discovery
            "GSI4PK": {"S": self._namespace_id},
            "GSI4SK": {"S": schema.pk_audit(self._namespace_id, entity_id)},
        }

        await client.put_item(TableName=self.table_name, Item=item)
        return event

    async def get_audit_events(
        self,
        entity_id: str,
        limit: int = 100,
        start_event_id: str | None = None,
    ) -> list[AuditEvent]:
        """
        Get audit events for an entity.

        Args:
            entity_id: ID of the entity to query
            limit: Maximum number of events to return
            start_event_id: Event ID to start after (for pagination)

        Returns:
            List of AuditEvent objects, ordered by most recent first
        """
        client = await self._get_client()

        query_args: dict[str, Any] = {
            "TableName": self.table_name,
            "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk_prefix)",
            "ExpressionAttributeValues": {
                ":pk": {"S": schema.pk_audit(self._namespace_id, entity_id)},
                ":sk_prefix": {"S": schema.SK_AUDIT},
            },
            "ScanIndexForward": False,  # Most recent first
            "Limit": limit,
        }

        if start_event_id:
            query_args["ExclusiveStartKey"] = {
                "PK": {"S": schema.pk_audit(self._namespace_id, entity_id)},
                "SK": {"S": schema.sk_audit(start_event_id)},
            }

        response = await client.query(**query_args)

        events = []
        for item in response.get("Items", []):
            event = self._deserialize_audit_event(item)
            if event:
                events.append(event)

        return events

    def _deserialize_audit_event(self, item: dict[str, Any]) -> AuditEvent | None:
        """Deserialize a DynamoDB item to AuditEvent (flat format only)."""
        if "action" not in item or "S" not in item.get("action", {}):
            return None

        details_raw = item.get("details", {})
        details = self._deserialize_map(details_raw.get("M", {})) if "M" in details_raw else {}
        principal = self._deserialize_value(item["principal"]) if "principal" in item else None
        resource = self._deserialize_value(item["resource"]) if "resource" in item else None
        return AuditEvent(
            event_id=item.get("event_id", {}).get("S", ""),
            timestamp=item.get("timestamp", {}).get("S", ""),
            action=item["action"]["S"],
            entity_id=item.get("entity_id", {}).get("S", ""),
            principal=principal,
            resource=resource,
            details=details,
        )

    # -------------------------------------------------------------------------
    # Usage snapshot operations
    # -------------------------------------------------------------------------

    async def get_usage_snapshots(
        self,
        entity_id: str | None = None,
        resource: str | None = None,
        window_type: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        limit: int = 100,
        next_key: dict[str, Any] | None = None,
    ) -> tuple[list[UsageSnapshot], dict[str, Any] | None]:
        """
        Query usage snapshots with filtering and pagination.

        Supports two query modes:
        1. Entity-scoped: Query by entity_id (uses primary key)
        2. Resource-scoped: Query by resource across all entities (uses GSI2)

        Args:
            entity_id: Entity to query (mutually exclusive for efficient queries)
            resource: Resource name filter (required if entity_id is None)
            window_type: Filter by window type ("hourly", "daily")
            start_time: Filter snapshots >= this timestamp (ISO format)
            end_time: Filter snapshots <= this timestamp (ISO format)
            limit: Maximum items to fetch from DynamoDB per page (default: 100)
            next_key: Pagination cursor from previous call

        Returns:
            Tuple of (snapshots, next_key). next_key is None if no more results.

        Raises:
            ValueError: If neither entity_id nor resource is provided

        Note:
            The ``limit`` parameter controls the DynamoDB query batch size.
            Client-side filters (window_type, start_time, end_time) are applied
            after fetching, so the returned count may be less than ``limit``.
            Use ``next_key`` to paginate through all matching results.
        """
        if entity_id is None and resource is None:
            raise ValueError("Either entity_id or resource must be provided")

        client = await self._get_client()
        snapshots: list[UsageSnapshot] = []

        if entity_id is not None:
            # Query by entity (primary key)
            key_condition = "PK = :pk AND begins_with(SK, :sk_prefix)"
            expression_values: dict[str, Any] = {
                ":pk": {"S": schema.pk_entity(self._namespace_id, entity_id)},
                ":sk_prefix": {"S": schema.SK_USAGE},
            }

            # If resource is also provided, narrow the SK prefix
            if resource:
                expression_values[":sk_prefix"] = {"S": f"{schema.SK_USAGE}{resource}#"}

            query_args: dict[str, Any] = {
                "TableName": self.table_name,
                "KeyConditionExpression": key_condition,
                "ExpressionAttributeValues": expression_values,
                "ScanIndexForward": False,  # Most recent first
                "Limit": limit,
            }

            if next_key:
                query_args["ExclusiveStartKey"] = next_key

            response = await client.query(**query_args)

        elif resource is not None:
            # Query by resource across entities (GSI2)
            key_condition = "GSI2PK = :pk AND begins_with(GSI2SK, :sk_prefix)"
            expression_values = {
                ":pk": {"S": schema.gsi2_pk_resource(self._namespace_id, resource)},
                ":sk_prefix": {"S": "USAGE#"},
            }

            query_args = {
                "TableName": self.table_name,
                "IndexName": schema.GSI2_NAME,
                "KeyConditionExpression": key_condition,
                "ExpressionAttributeValues": expression_values,
                "ScanIndexForward": False,  # Most recent first
                "Limit": limit,
            }

            if next_key:
                query_args["ExclusiveStartKey"] = next_key

            response = await client.query(**query_args)

        else:
            raise ValueError("Either entity_id or resource must be provided")

        # Deserialize and filter results
        for item in response.get("Items", []):
            snapshot = self._deserialize_usage_snapshot(item)
            if snapshot is None:
                continue

            # Apply filters
            if window_type and snapshot.window_type != window_type:
                continue
            if start_time and snapshot.window_start < start_time:
                continue
            if end_time and snapshot.window_start > end_time:
                continue

            snapshots.append(snapshot)

        # Get next pagination key
        returned_next_key = response.get("LastEvaluatedKey")

        return snapshots, returned_next_key

    async def get_usage_summary(
        self,
        entity_id: str | None = None,
        resource: str | None = None,
        window_type: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> UsageSummary:
        """
        Aggregate usage across snapshots into a summary.

        Fetches all matching snapshots (auto-paginates internally) and computes:
        - Total consumption per limit type
        - Average consumption per snapshot per limit type
        - Time range of aggregated data

        Args:
            entity_id: Entity to query
            resource: Resource name filter
            window_type: Filter by window type ("hourly", "daily")
            start_time: Filter snapshots >= this timestamp (ISO format)
            end_time: Filter snapshots <= this timestamp (ISO format)

        Returns:
            UsageSummary with aggregated statistics

        Raises:
            ValueError: If neither entity_id nor resource is provided
        """
        # Collect all matching snapshots with auto-pagination
        all_snapshots: list[UsageSnapshot] = []
        next_key: dict[str, Any] | None = None

        while True:
            snapshots, next_key = await self.get_usage_snapshots(
                entity_id=entity_id,
                resource=resource,
                window_type=window_type,
                start_time=start_time,
                end_time=end_time,
                limit=1000,  # Larger batch for efficiency
                next_key=next_key,
            )
            all_snapshots.extend(snapshots)

            if next_key is None:
                break

            # Safety limit to prevent unbounded memory usage
            if len(all_snapshots) >= 10000:
                break

        # Aggregate statistics
        total: dict[str, int] = {}
        counts: dict[str, int] = {}
        min_window: str | None = None
        max_window: str | None = None

        for snapshot in all_snapshots:
            # Track time range
            if min_window is None or snapshot.window_start < min_window:
                min_window = snapshot.window_start
            if max_window is None or snapshot.window_start > max_window:
                max_window = snapshot.window_start

            # Sum counters
            for limit_name, value in snapshot.counters.items():
                total[limit_name] = total.get(limit_name, 0) + value
                counts[limit_name] = counts.get(limit_name, 0) + 1

        # Calculate averages
        average: dict[str, float] = {}
        for limit_name, sum_value in total.items():
            count = counts.get(limit_name, 1)
            average[limit_name] = sum_value / count if count > 0 else 0.0

        return UsageSummary(
            snapshot_count=len(all_snapshots),
            total=total,
            average=average,
            min_window_start=min_window,
            max_window_start=max_window,
        )

    def _deserialize_usage_snapshot(self, item: dict[str, Any]) -> UsageSnapshot | None:
        """
        Deserialize a DynamoDB item to UsageSnapshot.

        Snapshots use FLAT schema (no nested data.M) to support atomic ADD
        operations. See issue #168.
        """
        # Extract from flat schema (not nested data.M)
        entity_id = item.get("entity_id", {}).get("S", "")
        resource = item.get("resource", {}).get("S", "")
        window_type = item.get("window", {}).get("S", "")
        window_start = item.get("window_start", {}).get("S", "")

        if not entity_id or not resource or not window_start:
            return None

        # Calculate window_end based on window_type
        window_end = self._calculate_window_end(window_start, window_type)

        # Extract total_events
        total_events = int(item.get("total_events", {}).get("N", "0"))

        # Extract counters (dynamic limit names stored as top-level attributes)
        # Known non-counter fields to exclude
        excluded_keys = {
            "PK",
            "SK",
            "entity_id",
            "resource",
            "window",
            "window_start",
            "total_events",
            "GSI2PK",
            "GSI2SK",
            "ttl",
        }

        counters: dict[str, int] = {}
        for key, value in item.items():
            if key in excluded_keys:
                continue
            # Counter values are stored as numbers
            if "N" in value:
                counters[key] = int(value["N"])

        return UsageSnapshot(
            entity_id=entity_id,
            resource=resource,
            window_start=window_start,
            window_end=window_end,
            window_type=window_type,
            counters=counters,
            total_events=total_events,
        )

    def _calculate_window_end(self, window_start: str, window_type: str) -> str:
        """Calculate window end timestamp from start and type."""
        from datetime import datetime, timedelta

        try:
            # Parse ISO timestamp
            dt = datetime.fromisoformat(window_start.replace("Z", "+00:00"))

            if window_type == "hourly":
                end_dt = dt.replace(minute=59, second=59, microsecond=999999)
            elif window_type == "daily":
                end_dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
            elif window_type == "monthly":
                # Last day of month
                if dt.month == 12:
                    end_dt = dt.replace(year=dt.year + 1, month=1, day=1) - timedelta(seconds=1)
                else:
                    end_dt = dt.replace(month=dt.month + 1, day=1) - timedelta(seconds=1)
            else:
                # Unknown window type - return window_start as window_end
                end_dt = dt

            return end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, AttributeError):
            return window_start

    # -------------------------------------------------------------------------
    # Resource aggregation
    # -------------------------------------------------------------------------

    async def get_resource_buckets(
        self,
        resource: str,
        limit_name: str | None = None,
    ) -> list[BucketState]:
        """Get all buckets for a resource across all entities.

        With composite items, each GSI2 entry is one composite item per
        entity. Returns individual BucketStates, optionally filtered by limit_name.
        """
        client = await self._get_client()

        key_condition = "GSI2PK = :pk AND begins_with(GSI2SK, :sk_prefix)"
        expression_values: dict[str, Any] = {
            ":pk": {"S": schema.gsi2_pk_resource(self._namespace_id, resource)},
            ":sk_prefix": {"S": "BUCKET#"},
        }

        response = await client.query(
            TableName=self.table_name,
            IndexName=schema.GSI2_NAME,
            KeyConditionExpression=key_condition,
            ExpressionAttributeValues=expression_values,
        )

        buckets: list[BucketState] = []
        for item in response.get("Items", []):
            for bucket in self._deserialize_composite_bucket(item):
                if limit_name is None or bucket.limit_name == limit_name:
                    buckets.append(bucket)

        return buckets

    # -------------------------------------------------------------------------
    # Serialization helpers
    # -------------------------------------------------------------------------

    def _serialize_map(self, data: dict[str, Any]) -> dict[str, Any]:
        """Serialize a Python dict to DynamoDB map format."""
        result: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, str):
                result[key] = {"S": value}
            elif isinstance(value, bool):
                result[key] = {"BOOL": value}
            elif isinstance(value, int | float):
                result[key] = {"N": str(value)}
            elif isinstance(value, dict):
                result[key] = {"M": self._serialize_map(value)}
            elif isinstance(value, list):
                result[key] = {"L": [self._serialize_value(v) for v in value]}
            elif value is None:
                result[key] = {"NULL": True}
        return result

    def _serialize_value(self, value: Any) -> dict[str, Any]:
        """Serialize a single value to DynamoDB format."""
        if isinstance(value, str):
            return {"S": value}
        elif isinstance(value, bool):
            return {"BOOL": value}
        elif isinstance(value, int | float):
            return {"N": str(value)}
        elif isinstance(value, dict):
            return {"M": self._serialize_map(value)}
        elif isinstance(value, list):
            return {"L": [self._serialize_value(v) for v in value]}
        elif value is None:
            return {"NULL": True}
        return {"S": str(value)}

    def _deserialize_map(self, data: dict[str, Any]) -> dict[str, Any]:
        """Deserialize a DynamoDB map to Python dict."""
        result = {}
        for key, value in data.items():
            result[key] = self._deserialize_value(value)
        return result

    def _deserialize_value(self, value: dict[str, Any]) -> Any:
        """Deserialize a single DynamoDB value."""
        if "S" in value:
            return value["S"]
        elif "N" in value:
            num_str = value["N"]
            return int(num_str) if "." not in num_str else float(num_str)
        elif "BOOL" in value:
            return value["BOOL"]
        elif "M" in value:
            return self._deserialize_map(value["M"])
        elif "L" in value:
            return [self._deserialize_value(v) for v in value["L"]]
        elif "NULL" in value:
            return None
        return None

    def _deserialize_entity(self, item: dict[str, Any]) -> Entity:
        """Deserialize a DynamoDB item to Entity (flat format only)."""
        entity_id = item.get("entity_id", {}).get("S", "")
        name_val = item["name"].get("S") if "name" in item else None
        parent_val = self._deserialize_value(item["parent_id"]) if "parent_id" in item else None
        cascade_val = item.get("cascade", {}).get("BOOL", False)
        metadata_val = (
            self._deserialize_map(item["metadata"].get("M", {}))
            if "metadata" in item and "M" in item.get("metadata", {})
            else {}
        )
        created_val = item.get("created_at", {}).get("S")

        return Entity(
            id=entity_id,
            name=name_val,
            parent_id=parent_val,
            cascade=cascade_val,
            metadata=metadata_val,
            created_at=created_val,
        )

    def _deserialize_bucket(self, item: dict[str, Any]) -> BucketState:
        """Deserialize a DynamoDB item to BucketState (flat format only)."""
        # Counter is stored as FLAT top-level attribute.
        # None if not present (old bucket without counter). See issue #179.
        counter_attr = item.get("total_consumed_milli", {})
        total_consumed_milli = int(counter_attr["N"]) if "N" in counter_attr else None

        return BucketState(
            entity_id=item.get("entity_id", {}).get("S", ""),
            resource=item.get("resource", {}).get("S", ""),
            limit_name=item.get("limit_name", {}).get("S", ""),
            tokens_milli=int(item["tokens_milli"]["N"]),
            last_refill_ms=int(item.get("last_refill_ms", {}).get("N", "0")),
            capacity_milli=int(item.get("capacity_milli", {}).get("N", "0")),
            refill_amount_milli=int(item.get("refill_amount_milli", {}).get("N", "0")),
            refill_period_ms=int(item.get("refill_period_ms", {}).get("N", "0")),
            total_consumed_milli=total_consumed_milli,
        )

    def _decode_stored_schedule(
        self, attr_name: str, compact: str, tz: str, *, reset: bool = False
    ) -> tuple[schedule.ScheduleEntry, ...]:
        """Decode a stored schedule, or declare the limiter unavailable (#222 §6).

        A limiter that cannot determine which limit is in force is definitionally
        unavailable, and ``on_unavailable`` is the knob that already exists for
        that — under ``allow`` it degrades exactly the way the operator asked,
        under ``block`` it raises. The alternative, treating an unreadable
        schedule as *no* schedule, runs at the **base** limit: a parse error
        would then double a customer's limit when the schedule said ``0.5x``,
        and with ``vu`` left expired the bucket would be pinned to the slow path
        permanently.

        The parser keeps raising ``ValueError`` and is deliberately not touched.
        ``schedule.py`` is pure stdlib plus cronsim with no ``zae_limiter``
        imports, so that ``models`` can import it without a cycle and both
        Lambdas can vendor it; and the aggregator's ``_decode_schedule`` catches
        ``ValueError`` *specifically*, so raising an ``InfrastructureError``
        there would slip through that catch and re-arm the poison-pill failure
        core plan Task 14 fixed. Each boundary converts instead: the aggregator
        skips the bucket, and this is the client's conversion.

        The message carries the attribute name, the stored value, the timezone
        it was read in, and the parser's own message — which since #515 names
        which of three things happened: the value carries no version marker, it
        carries one this build cannot read (a newer client wrote it), or it is
        a same-version failure. Only the last is ambiguous between corruption
        and a mistake; ``_tokenise``'s structural discrimination applies there
        and remains a heuristic rather than a proof.
        """
        try:
            if reset:
                return schedule.decode_reset(compact, tz)
            return schedule.decode(compact, tz)
        except ValueError as exc:
            raise RateLimiterUnavailable(
                f"stored schedule in {attr_name} could not be decoded: {compact!r} ({tz}): {exc}",
                cause=exc,
                stack_name=self.stack_name,
            ) from exc

    def _decode_stored_window_int(self, attr_name: str, raw: str | None) -> int | None:
        """Parse a `b_{name}_ws` / `b_{name}_rsa` value, or declare the limiter
        unavailable (ADR-139).

        Same shape and reasoning as `_decode_stored_schedule`: unlike `sched`/
        `rsched`, `ws`/`rsa` have no grammar of their own -- they are bare `N`
        attributes, so DynamoDB legally stores a non-integral value like
        `"18000.5"` and `int()` itself can raise. Converting that to
        `RateLimiterUnavailable` (rather than letting a bare `ValueError`
        escape, or silently reading it as `None`) matters beyond the slow
        path: the ALL_OLD / ALL_NEW images behind the speculative path go
        through `_deserialize_composite_bucket`, which calls this, so an
        unguarded corruption there would raise out of a hot path with no
        indication of which attribute or item was at fault. `None` (the
        attribute is simply absent) is not corruption -- it is every bucket
        written before this attribute existed, and every `wcu` limit, which
        never carries one -- so it is returned, not raised.
        """
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError as exc:
            raise RateLimiterUnavailable(
                f"stored duration window in {attr_name} could not be decoded: {raw!r}: {exc}",
                cause=exc,
                stack_name=self.stack_name,
            ) from exc

    def _deserialize_composite_bucket(self, item: dict[str, Any]) -> list[BucketState]:
        """Deserialize a composite DynamoDB item to a list of BucketStates.

        A composite bucket item stores all limits for an entity+resource in a
        single DynamoDB item. Per-limit attributes use the prefix b_{name}_{field}
        with a shared rf (refill timestamp). See ADR-114.

        Both schedule tuples are decoded off the item (#222 §4.1) under the
        same item-level-default-plus-per-limit-override rule the aggregator
        applies, so the ``ALL_OLD`` / ``ALL_NEW`` images behind the speculative
        path carry the schedule that was in force for the write. Without them
        every fast-path ``LimitStatus`` reports the base capacity, flat, inside
        a window that has already halved it. The slow path overwrites
        ``state.sched`` from the config it just resolved (``_do_acquire``),
        which is the fresher of the two — that ordering is deliberate and this
        does not change it.
        """
        entity_id = item.get("entity_id", {}).get("S", "")
        resource = item.get("resource", {}).get("S", "")
        rf = int(item.get(schema.BUCKET_FIELD_RF, {}).get("N", "0"))
        # Carried so refill math uses this shard's effective share (ADR-133).
        # The reserved wcu limit is per-partition and stays undivided.
        shard_count = int(item.get("shard_count", {}).get("N", "1"))

        # One hoisted zone for the whole item, covering both tuples (#222
        # §4.1). Absent on every item written before scheduling existed, where
        # UTC is harmless: it is only ever consulted alongside a compact
        # string, and those items carry none.
        sched_tz = item.get(schema.BUCKET_FIELD_SCHED_TZ, {}).get("S") or "UTC"
        item_sched = item.get(schema.BUCKET_FIELD_SCHED, {}).get("S")
        item_rsched = item.get(schema.BUCKET_FIELD_RSCHED, {}).get("S")
        # Keyed by (compact, reset) rather than by limit: the item-level
        # default is shared by every limit that has no override of its own, so
        # a 20-limit item decodes it once.
        decoded: dict[tuple[str, bool], tuple[schedule.ScheduleEntry, ...]] = {}

        def _schedule_for(
            name: str, field: str, item_compact: str | None, reset: bool
        ) -> tuple[schedule.ScheduleEntry, ...]:
            """One limit's tuple: its own override if it has one, else the item default.

            Absence means "inherit the default" — the write side only emits
            `b_{name}_{field}` where a limit's encoding *differs* from it — so
            this is the exact inverse of `_encode_one_tuple`, and the same rule
            `processor._parse_bucket_record` applies.

            `BUCKET_SCHED_NONE` is the third reading (#541): an override that
            says "this limit has none of this kind", written for every
            unscheduled limit on an item that carries a default. Without it
            "unscheduled" and "same as the default" are one byte pattern, and
            an unscheduled limit sharing an item with a scheduled one inherits
            a window it never declared.
            """
            attr = schema.bucket_attr(name, field)
            override = item.get(attr, {}).get("S")
            if override == schema.BUCKET_SCHED_NONE:
                return ()
            compact = override or item_compact
            if not compact:
                return ()
            key = (compact, reset)
            if key not in decoded:
                # The attribute that actually carried the string, not the one
                # this limit would have used: an operator reading the message
                # has to know whether to repair the item default or one
                # limit's override.
                decoded[key] = self._decode_stored_schedule(
                    attr if override else field, compact, sched_tz, reset=reset
                )
            return decoded[key]

        # Discover limit names by scanning for b_{name}_tk attributes
        limit_names: list[str] = []
        suffix = f"_{schema.BUCKET_FIELD_TK}"
        for attr_name in item:
            if attr_name.startswith(schema.BUCKET_ATTR_PREFIX) and attr_name.endswith(suffix):
                name = attr_name[len(schema.BUCKET_ATTR_PREFIX) : -len(suffix)]
                if name:
                    limit_names.append(name)

        buckets: list[BucketState] = []
        for name in limit_names:

            def _get(field: str) -> int:
                attr = schema.bucket_attr(name, field)
                return int(item.get(attr, {}).get("N", "0"))

            tc_attr = item.get(schema.bucket_attr(name, schema.BUCKET_FIELD_TC), {})
            total_consumed = int(tc_attr["N"]) if "N" in tc_attr else None

            # ADR-139 duration window. A missing attribute (absent on every
            # item written before the window existed, and always absent on
            # `wcu`, which never carries one) decodes to `None`, exactly like
            # `total_consumed_milli` above for the same reason -- predating
            # the attribute is not corruption. A *present but non-integral*
            # value is corruption and is converted to `RateLimiterUnavailable`
            # by `_decode_stored_window_int`, the same treatment
            # `_decode_stored_schedule` gives a corrupt `sched`/`rsched`. This
            # matters beyond the slow path: the ALL_OLD / ALL_NEW images
            # behind the speculative path go through this function too, so
            # without it every fast-path status would report a quota with no
            # window, and with an unguarded `int()` a corrupt value would
            # raise a bare, undiagnosable `ValueError` from that hot path.
            ws_name = schema.bucket_attr(name, schema.BUCKET_FIELD_WS)
            window_start_ms = self._decode_stored_window_int(
                ws_name, item.get(ws_name, {}).get("N")
            )
            rsa_name = schema.bucket_attr(name, schema.BUCKET_FIELD_RSA)
            reset_after_seconds = self._decode_stored_window_int(
                rsa_name, item.get(rsa_name, {}).get("N")
            )

            # `wcu` is never scheduled — it tracks partition write pressure,
            # not a user limit, and is the one limit `effective_params` must
            # not scale (a 0.5x window would halve the write ceiling on
            # exactly the hot buckets sharding exists to protect). The
            # aggregator reaches the same place by exempting it at each
            # consumer instead; doing it here keeps every client consumer of
            # `state.sched` — refill, ceiling, retry estimate — covered at
            # once. Mirrors `Limit._carrier` and `BucketState.for_wcu`, which
            # both set the tuples to `()` explicitly on the write side.
            is_wcu = name == schema.WCU_LIMIT_NAME
            sched = (
                () if is_wcu else _schedule_for(name, schema.BUCKET_FIELD_SCHED, item_sched, False)
            )
            reset_sched = (
                () if is_wcu else _schedule_for(name, schema.BUCKET_FIELD_RSCHED, item_rsched, True)
            )

            buckets.append(
                BucketState(
                    entity_id=entity_id,
                    resource=resource,
                    limit_name=name,
                    tokens_milli=_get(schema.BUCKET_FIELD_TK),
                    last_refill_ms=rf,
                    capacity_milli=_get(schema.BUCKET_FIELD_CP),
                    refill_amount_milli=_get(schema.BUCKET_FIELD_RA),
                    refill_period_ms=_get(schema.BUCKET_FIELD_RP),
                    total_consumed_milli=total_consumed,
                    shard_count=1 if is_wcu else shard_count,
                    sched=sched,
                    reset_sched=reset_sched,
                    window_start_ms=window_start_ms,
                    reset_after_seconds=reset_after_seconds,
                )
            )

        return buckets

    # -------------------------------------------------------------------------
    # Composite limit config serialization (ADR-114 for configs)
    # -------------------------------------------------------------------------

    def _serialize_composite_limits(
        self,
        limits: list[Limit],
        base_item: dict[str, Any],
    ) -> dict[str, Any]:
        """Add l_* attributes to a DynamoDB item for composite limit storage.

        Every config level is written with a full-replace ``PutItem``, so an
        attribute this method omits (an unscheduled limit's ``l_{name}_sched``,
        or ``sched_tz`` when nothing is scheduled) disappears from the stored
        item on its own — storage is override-not-merge and no explicit REMOVE
        is needed.

        Args:
            limits: List of Limit objects to serialize
            base_item: Base DynamoDB item to add attributes to (mutated in place)

        Returns:
            The modified base_item with l_{name}_{field} attributes added

        Raises:
            ValueError: if two scheduled limits disagree on timezone; it is
                hoisted to one item-level attribute (#222 §4.1).
        """
        # Raises before anything is written, so a rejected item is never
        # half-serialized into base_item.
        hoisted_tz = hoisted_schedule_timezone(limits)

        for limit in limits:
            name = limit.name
            base_item[schema.limit_attr(name, schema.LIMIT_FIELD_CP)] = {"N": str(limit.capacity)}
            base_item[schema.limit_attr(name, schema.LIMIT_FIELD_RA)] = {
                "N": str(limit.refill_amount)
            }
            base_item[schema.limit_attr(name, schema.LIMIT_FIELD_RP)] = {
                "N": str(limit.refill_period_seconds)
            }
            if limit.schedule:
                compact, _tz = schedule.encode(limit.schedule)
                base_item[schema.limit_attr(name, schema.LIMIT_FIELD_SCHED)] = {"S": compact}
            # Without this leg a quota round-trips to `refill_amount=0` with no
            # reset, which `Limit.__post_init__` rejects — so the write poisons
            # the config item and every later read raises (#538).
            if limit.reset_schedule:
                compact, _tz = schedule.encode_reset(limit.reset_schedule)
                base_item[schema.limit_attr(name, schema.LIMIT_FIELD_RSCHED)] = {"S": compact}
            # ADR-139: the alternative spelling of the reset half — a window
            # anchored to the entity's own first use rather than a calendar
            # instant. Written only when the limit has one, exactly like
            # `rsched`. The full-replace PutItem is what makes removal free: a
            # limit re-written without a window loses the stored one with no
            # explicit REMOVE.
            if limit.reset_after_seconds is not None:
                base_item[schema.limit_attr(name, schema.LIMIT_FIELD_RSA)] = {
                    "N": str(limit.reset_after_seconds)
                }

        if hoisted_tz is not None:
            base_item[schema.CONFIG_FIELD_SCHED_TZ] = {"S": hoisted_tz}
        return base_item

    def _deserialize_composite_limits(self, item: dict[str, Any]) -> list[Limit]:
        """Deserialize l_* attributes from a DynamoDB item to Limit objects.

        Discovers limit names by scanning for l_{name}_cp attributes.

        A stored schedule that will not decode raises ``RateLimiterUnavailable``
        (#222 §6) and takes **the whole item** with it, not just its own limit.
        Returning the other limits would drop the unreadable one from the level
        entirely, and config precedence is per *level*, not per limit — a level
        that still defines anything wins outright — so the dropped limit would
        not fall back to the resource or system value, it would go unenforced.
        That is strictly worse than the over-admission this guard exists to
        prevent. It would also amplify: ``_sync_bucket_params`` resolves limits
        and stamps them onto every bucket, so a partial read would erase the
        limit from the items enforcing it. Both other readers of these
        attributes already fail at item granularity — the aggregator skips the
        whole bucket on one bad limit (``processor.try_refill_bucket``), and the
        provisioner's ``_decode_limits`` raises out of the whole item.

        Args:
            item: DynamoDB item with l_{name}_{field} attributes

        Returns:
            List of Limit objects reconstructed from composite attributes

        Raises:
            RateLimiterUnavailable: A stored schedule on this item cannot be
                decoded, or a limit carrying one — or a stored `rsa` duration
                window (ADR-139) — cannot be reconstructed from what is
                stored.
        """
        from datetime import timedelta

        # Discover limit names by scanning for l_{name}_cp attributes
        limit_names: list[str] = []
        suffix = f"_{schema.LIMIT_FIELD_CP}"
        for attr_name in item:
            if attr_name.startswith(schema.LIMIT_ATTR_PREFIX) and attr_name.endswith(suffix):
                name = attr_name[len(schema.LIMIT_ATTR_PREFIX) : -len(suffix)]
                if name:
                    limit_names.append(name)

        # One hoisted timezone for the whole item (#222 §4.1). Absent on items
        # written before schedules existed, and on items where nothing is
        # scheduled; UTC is then the harmless default, since it is only ever
        # consulted alongside a `sched` attribute.
        sched_tz = item.get(schema.CONFIG_FIELD_SCHED_TZ, {}).get("S") or "UTC"

        limits: list[Limit] = []
        for name in limit_names:

            def _get(field: str) -> int:
                attr = schema.limit_attr(name, field)
                return int(item.get(attr, {}).get("N", "0"))

            sched_name = schema.limit_attr(name, schema.LIMIT_FIELD_SCHED)
            rsched_name = schema.limit_attr(name, schema.LIMIT_FIELD_RSCHED)
            rsa_name = schema.limit_attr(name, schema.LIMIT_FIELD_RSA)
            sched_attr = item.get(sched_name, {}).get("S")
            rsched_attr = item.get(rsched_name, {}).get("S")
            rsa_attr = item.get(rsa_name, {}).get("N")
            # All three decode independently — any one can be corrupt on its
            # own, and `rsched` / `rsa` are the only reset spellings a quota
            # carries (ADR-139: never both on the same limit).
            sched = (
                self._decode_stored_schedule(sched_name, sched_attr, sched_tz) if sched_attr else ()
            )
            reset_sched = (
                self._decode_stored_schedule(rsched_name, rsched_attr, sched_tz, reset=True)
                if rsched_attr
                else ()
            )
            # Unlike `sched`/`rsched`, `rsa` has no grammar of its own — it is
            # a bare `N`, so a DynamoDB attribute legally carries a
            # non-integral value like `"1.5"` and `int()` itself can raise.
            # The parse therefore has to sit *inside* the guarded region below
            # alongside the value-range failures `Limit.__post_init__` raises
            # (zero, negative, or over MAX_PERIOD_SECONDS) — pulling it out
            # would let a non-integral `rsa` escape as a bare `ValueError`
            # from get_limits()/resolve_limits() (#621).
            try:
                reset_after = timedelta(seconds=int(rsa_attr)) if rsa_attr is not None else None
                limits.append(
                    Limit(
                        name=name,
                        capacity=_get(schema.LIMIT_FIELD_CP),
                        refill_amount=_get(schema.LIMIT_FIELD_RA),
                        refill_period_seconds=_get(schema.LIMIT_FIELD_RP),
                        schedule=sched,
                        reset_schedule=reset_sched,
                        reset_after=reset_after,
                    )
                )
            except ValueError as exc:
                # A schedule (or a duration window) can also defeat
                # reconstruction *after* it parses: a stored `rsched` beside a
                # positive stored rate, or a stored `rsa` <= 0, is rejected by
                # `Limit.__post_init__` (ADR-137: never both; ADR-139: a
                # duration must be a positive whole number of seconds). And a
                # duration window can fail to parse at all — a non-integral
                # `rsa` (e.g. `"1.5"`) raises out of `int()` itself, inside
                # this same guarded region, before `Limit.__post_init__` is
                # ever reached. Same class as a schedule decode failure — the
                # stored value leaves the limit undeterminable — so all of it
                # converts the same way, and for the same reason: "no
                # schedule" would silently run at the base limit. Culprit
                # membership for `rsa` is decided by `rsa_attr is not None`
                # (the attribute was present on the item), not by whether
                # `reset_after` parsed — that name may be unbound here if the
                # `int()` conversion above is what raised. Scoped to limits
                # that actually carry one of the three; an unscheduled limit
                # that will not reconstruct (a stored zero rate with no
                # reset, #538's shape) still surfaces as the ValueError it
                # has always been, since none of the three is involved in
                # deciding it.
                culprits = []
                if sched:
                    culprits.append(sched_name)
                if reset_sched:
                    culprits.append(rsched_name)
                if rsa_attr is not None:
                    culprits.append(f"{rsa_name}={rsa_attr!r}")
                if not culprits:
                    raise
                raise RateLimiterUnavailable(
                    f"stored limit {name!r} carries {', '.join(culprits)} but cannot be "
                    f"reconstructed: {exc}",
                    cause=exc,
                    stack_name=self.stack_name,
                ) from exc

        return limits

    # -------------------------------------------------------------------------
    # Config resolution (ADR-122)
    # -------------------------------------------------------------------------

    async def resolve_limits(
        self,
        entity_id: str,
        resource: str,
        disabled_out: dict[tuple[str, str], bool | None] | None = None,
    ) -> tuple[list[Limit] | None, OnUnavailableAction | None, ConfigSource | None]:
        """Resolve effective limits using the four-level config hierarchy.

        Uses ConfigCache with batched fetch optimization. Falls back to
        sequential individual GetItem calls if batch resolution fails.

        Args:
            entity_id: Entity to resolve limits for
            resource: Resource being accessed
            disabled_out: Optional dict to receive the tri-state `disabled`
                value of each config level this call actually read from
                DynamoDB. The disable walk's levels are a subset of these, so
                a caller needing both can reuse this read rather than issuing
                an identical second BatchGetItem — see
                `resolve_disabled_from_fetched`. Levels served from the config
                cache are deliberately absent from the dict: they are not
                fresh, and the gate must never be answered from cache.

        Returns:
            Tuple of (limits, on_unavailable, config_source)
        """
        # Try batched resolution (1 BatchGetItem instead of up to 4 GetItem calls)
        if self.capabilities.supports_batch_operations:
            try:
                fetch_fn = self.batch_get_configs
                if disabled_out is not None:
                    fetch_fn = functools.partial(self.batch_get_configs, disabled_out=disabled_out)
                return await self._config_cache.resolve_limits(
                    entity_id,
                    resource,
                    fetch_fn,
                )
            except Exception:
                logger.debug("Batched config resolution failed, falling back to sequential")

        # Sequential fallback (or non-batch backend)
        return await self._resolve_limits_sequential(entity_id, resource)

    async def _resolve_limits_sequential(
        self,
        entity_id: str,
        resource: str,
    ) -> tuple[list[Limit] | None, OnUnavailableAction | None, ConfigSource | None]:
        """Sequential fallback for resolve_limits().

        Queries each config level individually with caching.
        """
        # Entity-level config for specific resource
        entity_limits = await self._config_cache.get_entity_limits(
            entity_id,
            resource,
            self.get_limits,
        )
        if entity_limits:
            return entity_limits, None, "entity"

        # Entity-level _default_ config
        if resource != schema.DEFAULT_RESOURCE:
            entity_default_limits = await self._config_cache.get_entity_limits(
                entity_id,
                schema.DEFAULT_RESOURCE,
                self.get_limits,
            )
            if entity_default_limits:
                return entity_default_limits, None, "entity_default"

        # Resource-level defaults
        resource_limits = await self._config_cache.get_resource_defaults(
            resource,
            self.get_resource_defaults,
        )
        if resource_limits:
            return resource_limits, None, "resource"

        # System-level defaults (includes on_unavailable)
        system_limits, on_unavailable = await self._config_cache.get_system_defaults(
            self.get_system_defaults,
        )
        if system_limits:
            return system_limits, on_unavailable, "system"

        # Nothing found
        return None, on_unavailable, None

    async def resolve_on_unavailable(self) -> OnUnavailableAction:
        """Resolve on_unavailable from system config, with caching fallback.

        Returns the on_unavailable action from system config. Caches the
        value after first successful load so it's available as fallback
        when DynamoDB is unreachable. Defaults to "block" if no system
        config exists and no cached value is available.
        """
        try:
            _, on_unavailable = await self._config_cache.get_system_defaults(
                self.get_system_defaults,
            )
            if on_unavailable is not None:
                self._on_unavailable_cache = on_unavailable
                return on_unavailable
            # System config exists but on_unavailable not set — use cache or default
            if self._on_unavailable_cache is not None:
                return self._on_unavailable_cache
            return "block"
        except Exception:
            # DynamoDB unreachable — use cached value or default
            if self._on_unavailable_cache is not None:
                logger.warning(
                    "DynamoDB unavailable, using cached on_unavailable=%s",
                    self._on_unavailable_cache,
                )
                return self._on_unavailable_cache
            logger.warning("DynamoDB unavailable, defaulting on_unavailable=block")
            return "block"

    def resolve_disabled_from_fetched(
        self,
        entity_id: str,
        resource: str,
        fetched: dict[tuple[str, str], bool | None],
    ) -> tuple[bool, str | None] | None:
        """Answer the disable walk from a config fetch, or decline (ADR-125).

        `resolve_limits(disabled_out=...)` records the tri-state `disabled` of
        every level it actually read. The disable walk's levels are a subset of
        those, so when all of them were read in that same call, the walk can be
        evaluated here for free instead of issuing an identical second
        BatchGetItem.

        Returns None — meaning "the caller must call `resolve_disabled`" — if
        even one level is missing from `fetched`. A missing level is one the
        config cache served, and a cached value must never answer this gate:
        a first `acquire()` for an entity with no bucket yet would then be
        admitted on a stale `false`, and would go on to create an unstamped
        bucket that the already-finished fan-out will never stamp. That is
        permanent admission to a disabled resource, which is why
        `resolve_disabled` is uncached in the first place.

        Note the distinction this relies on: a level present in `fetched` with
        value None was genuinely read and has no explicit value (so the walk
        moves on); a level absent from `fetched` was not read at all.
        """
        ns = self._namespace_id
        levels: list[tuple[str, tuple[str, str]]] = [
            ("entity", (schema.pk_entity(ns, entity_id), schema.sk_config(resource))),
        ]
        if resource != schema.DEFAULT_RESOURCE:
            levels.append(
                (
                    "entity_default",
                    (schema.pk_entity(ns, entity_id), schema.sk_config(schema.DEFAULT_RESOURCE)),
                )
            )
        levels.append(("resource", (schema.pk_resource(ns, resource), schema.sk_config())))

        if any(key not in fetched for _level, key in levels):
            return None

        for level, key in levels:
            value = fetched[key]
            if value is not None:
                return value, level
        return False, None

    async def resolve_disabled(
        self,
        entity_id: str,
        resource: str,
    ) -> tuple[bool, str | None]:
        """Resolve the effective disabled state for an entity+resource (ADR-125).

        Walks entity(resource) -> entity(_default_) -> resource and returns the
        first level that sets `disabled` explicitly. This walk is independent of
        the limits walk in resolve_limits(): a level that sets `disabled` but
        defines no limits still decides the outcome, which is what lets an
        entity-level `disabled: false` re-admit one entity to a disabled resource.

        Deliberately uncached — see ADR-125. Called only on the slow path and by
        the eager fan-out, never on the speculative fast path.

        Args:
            entity_id: Entity to resolve for
            resource: Resource being accessed

        Returns:
            (effective_disabled, deciding_level) where deciding_level is
            "entity", "entity_default", "resource", or None if nothing set it.
        """
        ns = self._namespace_id
        levels: list[tuple[str, str, str]] = [
            ("entity", schema.pk_entity(ns, entity_id), schema.sk_config(resource)),
        ]
        if resource != schema.DEFAULT_RESOURCE:
            levels.append(
                (
                    "entity_default",
                    schema.pk_entity(ns, entity_id),
                    schema.sk_config(schema.DEFAULT_RESOURCE),
                )
            )
        levels.append(("resource", schema.pk_resource(ns, resource), schema.sk_config()))

        # A withheld item is indistinguishable in the walk below from a level
        # that sets no value, so a partial BatchGetItem would let an entity
        # marked `disabled: true` fall through and be ADMITTED. _batch_get_all
        # retries the remainder and refuses to answer rather than guess:
        # returning (False, None) here cannot be told apart by the caller from
        # "nothing is disabled".
        items = await self._batch_get_all(
            [{"PK": {"S": pk}, "SK": {"S": sk}} for _, pk, sk in levels],
            context=f"disabled state for {entity_id!r}/{resource!r}",
            entity_id=entity_id,
            resource=resource,
        )
        by_key = {(i.get("PK", {}).get("S", ""), i.get("SK", {}).get("S", "")): i for i in items}

        for level, pk, sk in levels:
            item = by_key.get((pk, sk))
            if item is None:
                continue
            value = schema.decode_disabled(item)
            if value is not None:
                return value, level

        return False, None

    async def _stamp_bucket_disabled(self, pk: str, disabled: bool) -> None:
        """Set or remove the `disabled` attribute on one bucket item (ADR-125).

        The attribute is present only when the bucket is effectively disabled,
        which keeps the speculative guard as a cheap attribute_not_exists check.

        Args:
            pk: Full bucket partition key (already namespace- and shard-qualified)
            disabled: True to stamp the bucket, False to clear the stamp
        """
        client = await self._get_client()
        kwargs: dict[str, Any] = {
            "TableName": self.table_name,
            "Key": {"PK": {"S": pk}, "SK": {"S": schema.sk_state()}},
            "ExpressionAttributeNames": {"#disabled": schema.BUCKET_FIELD_DISABLED},
            # Never resurrect a bucket that TTL or a delete removed.
            "ConditionExpression": "attribute_exists(PK)",
        }
        if disabled:
            kwargs["UpdateExpression"] = "SET #disabled = :true"
            kwargs["ExpressionAttributeValues"] = {":true": {"BOOL": True}}
        else:
            kwargs["UpdateExpression"] = "REMOVE #disabled"

        try:
            await client.update_item(**kwargs)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            # Bucket vanished between discovery and stamp — nothing to disable.

    async def _discover_resource_bucket_pks(self, resource: str) -> list[tuple[str, str]]:
        """Find every bucket PK for a resource, across entities and shards.

        Uses GSI2 (GSI2PK={ns}/RESOURCE#{name}, GSI2SK begins_with BUCKET#),
        the same access pattern used for resource capacity aggregation.

        Returns:
            List of (bucket_pk, entity_id) tuples.
        """
        client = await self._get_client()
        results: list[tuple[str, str]] = []
        start_key: dict[str, Any] | None = None

        while True:
            params: dict[str, Any] = {
                "TableName": self.table_name,
                "IndexName": schema.GSI2_NAME,
                "KeyConditionExpression": "GSI2PK = :pk AND begins_with(GSI2SK, :sk)",
                "ExpressionAttributeValues": {
                    ":pk": {"S": schema.gsi2_pk_resource(self._namespace_id, resource)},
                    ":sk": {"S": "BUCKET#"},
                },
            }
            if start_key:
                params["ExclusiveStartKey"] = start_key
            response = await client.query(**params)
            for item in response.get("Items", []):
                pk = item.get("PK", {}).get("S", "")
                if not pk:
                    continue
                _ns, entity_id, _res, _shard = schema.parse_bucket_pk(pk)
                results.append((pk, entity_id))
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                break

        return results

    async def _discover_entity_bucket_pks(self, entity_id: str, resource: str | None) -> list[str]:
        """Find every bucket PK for an entity, optionally scoped to one resource.

        Uses GSI3 (GSI3PK={ns}/ENTITY#{id}, GSI3SK begins_with BUCKET#{resource}#),
        the KEYS_ONLY discovery index added for GHSA-76rv.

        Returns:
            List of bucket PKs.
        """
        client = await self._get_client()
        pks: list[str] = []
        start_key: dict[str, Any] | None = None
        sk_prefix = f"BUCKET#{resource}#" if resource else "BUCKET#"

        while True:
            params: dict[str, Any] = {
                "TableName": self.table_name,
                "IndexName": schema.GSI3_NAME,
                "KeyConditionExpression": "GSI3PK = :pk AND begins_with(GSI3SK, :sk)",
                "ExpressionAttributeValues": {
                    ":pk": {"S": schema.gsi3_pk_entity(self._namespace_id, entity_id)},
                    ":sk": {"S": sk_prefix},
                },
            }
            if start_key:
                params["ExclusiveStartKey"] = start_key
            response = await client.query(**params)
            for item in response.get("Items", []):
                pk = item.get("PK", {}).get("S", "")
                if pk:
                    pks.append(pk)
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                break

        return pks

    async def reclaim_quota_surplus(
        self,
        entity_id: str,
        resource: str,
        shares_milli: dict[str, int],
    ) -> tuple[int, dict[str, int]]:
        """Clamp a quota's existing shards to their new share, and report the take (#587).

        Called once, just before the slow path creates a shard that does not
        exist yet, and only for limits that are quotas. A doubling shrinks every
        shard's ceiling from ``cp // old_count`` to ``cp // new_count``, and
        ``bucket.refill_bucket`` would trim each shard to the new one on its
        next materialising pass anyway (``min(capacity, tokens)``, #496 / #222
        §3.3). Doing it here instead makes the trim and the new shard's grant a
        single conserving **transfer**: what comes off the siblings is exactly
        what the new shard is created with, so the entity's spendable total does
        not move across a doubling.

        Eager rather than lazy because the speculative fast path is a pure
        ``ADD`` with no ceiling arithmetic (#469 / #222 §3.3). An unclamped
        sibling can spend its surplus at full speed while the new shard holds a
        grant made from that same surplus — the over-admission of #587 in
        transient form. Nothing is destroyed that was not already doomed, so a
        reclaim followed by a rejected acquire costs the entity nothing.

        A **dripping** limit must never be passed here. Its stored ``ra`` is
        undivided, so its shards' ceilings still sum to the configured capacity
        and a new shard starting full costs at most one ``time_to_fill`` of
        burst, which token-bucket semantics allow; clamping it early would only
        throw away tokens the refill is about to re-add.

        Cost: 1 GSI3 KEYS_ONLY query + 1 ``BatchGetItem`` + one conditional
        ``UpdateItem`` per shard that actually holds a surplus — none at all in
        the common case of an entity that has already spent down. Paid once per
        shard creation, bounded by ``MAX_SHARD_COUNT`` over the life of an
        (entity, resource).

        Args:
            entity_id: Entity owning the shards.
            resource: Resource the shards belong to.
            shares_milli: ``{limit_name: capacity_milli // shard_count}`` for
                the quota limits only — the ceiling each shard is clamped to.

        Returns:
            ``(shards_found, {limit_name: reclaimed_milli})``. ``shards_found``
            is 0 when nothing has been materialised for this (entity, resource)
            at all, which is **not** the same as reclaiming nothing: the caller
            grants a full share in that case and a capped transfer otherwise.
        """
        reclaimed: dict[str, int] = dict.fromkeys(shares_milli, 0)
        if not shares_milli:
            return 0, reclaimed

        pks = await self._discover_entity_bucket_pks(entity_id, resource)
        if not pks:
            return 0, reclaimed

        client = await self._get_client()
        keys = [{"PK": {"S": pk}, "SK": {"S": schema.sk_state()}} for pk in pks]
        items: list[dict[str, Any]] = []
        for start in range(0, len(keys), 100):
            items.extend(
                await self._batch_get_all(
                    keys[start : start + 100],
                    context=f"quota shards for entity {entity_id!r}",
                    entity_id=entity_id,
                )
            )

        for item in items:
            pk = item["PK"]["S"]
            for name, share in shares_milli.items():
                attr = schema.bucket_attr(name, schema.BUCKET_FIELD_TK)
                raw = item.get(attr, {}).get("N")
                if raw is None or int(raw) <= share:
                    continue
                try:
                    response = await client.update_item(
                        TableName=self.table_name,
                        Key={"PK": {"S": pk}, "SK": {"S": schema.sk_state()}},
                        UpdateExpression="SET #tk = :share",
                        ConditionExpression="#tk > :share",
                        ExpressionAttributeNames={"#tk": attr},
                        ExpressionAttributeValues={":share": {"N": str(share)}},
                        ReturnValues="UPDATED_OLD",
                    )
                except ClientError as e:
                    if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                        # Spent below the new ceiling since the read. There is
                        # no surplus left to move, which is the right answer.
                        continue
                    raise
                previous = int(response["Attributes"][attr]["N"])
                reclaimed[name] += previous - share

        return len(pks), reclaimed

    async def get_shard_window_starts(
        self,
        entity_id: str,
        resource: str,
        limit_names: list[str],
        shard_id: int = 0,
    ) -> dict[str, int]:
        """Read one shard's duration-window starts, to seed a shard being created (ADR-139).

        Shard 0 by default, because :meth:`bump_shard_count` already treats it
        as the source of truth for ``shard_count``. A created shard inherits
        ``ws`` verbatim and sets ``rf = now``, so ``ws > rf`` is **false** on
        the new item and it does not immediately re-roll itself: it joins the
        window in progress rather than opening one. Whether the window read
        here is still *live* is the caller's decision, against the ``rsa`` of
        the config it resolved — this returns the stored start and nothing
        more.

        A **separate** read rather than an extra key in the create path's
        ``BatchGetItem``: that call returns a dict keyed by ``(entity_id,
        resource, limit_name)`` with no shard component, so shard 0 and shard N
        would collide on every key. 0.5 RCU, eventually consistent, **once per
        shard ever** (≤ 31 per (entity, resource), plus TTL recreations) on a
        path already priced at 2.5 RCU + 2 WCU.

        A limit absent from the result has no window on that shard — either it
        carries none, or the shard has been swept. The caller then opens a fresh
        window, which is the degraded case ADR-139 records under Consequences
        and which idle-restarting makes correct rather than merely tolerable.

        Args:
            entity_id: Entity owning the bucket. On a **cascade** create this is
                the entity whose shard is being created — the parent for a
                parent shard, never the child. Parent and child windows are
                independent (ADR-139).
            resource: Resource name.
            limit_names: The limits to look for; only these attributes are
                projected, so the read stays a fraction of the item.
            shard_id: The shard to read. Defaults to 0.

        Returns:
            ``{limit_name: window_start_ms}`` for the limits whose ``ws`` is on
            the item.

        Raises:
            RateLimiterUnavailable: A stored ``ws`` is not an integer, the same
                treatment every other reader gives a corrupt window.
        """
        if not limit_names:
            return {}
        client = await self._get_client()
        # Aliases, not bare names: `bucket_attr` interpolates a limit name and
        # `NAME_PATTERN` allows `.`, a document-path separator.
        names = {
            f"#w{i}": schema.bucket_attr(name, schema.BUCKET_FIELD_WS)
            for i, name in enumerate(limit_names)
        }
        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(self._namespace_id, entity_id, resource, shard_id)},
                "SK": {"S": schema.sk_state()},
            },
            ProjectionExpression=", ".join(names),
            ExpressionAttributeNames=names,
        )
        item = response.get("Item") or {}
        out: dict[str, int] = {}
        for i, name in enumerate(limit_names):
            attr = names[f"#w{i}"]
            ws = self._decode_stored_window_int(attr, item.get(attr, {}).get("N"))
            if ws is not None:
                out[name] = ws
        return out

    async def _fanout_resource(self, resource: str, disabled: bool) -> int:
        """Stamp every bucket for a resource, honoring per-entity overrides.

        An entity whose own config resolves to a different value than the
        resource-level one is skipped — that is what makes an entity-level
        `disabled: false` a carve-out from a disabled resource (ADR-125).

        Runs the discovery query twice: the second pass catches buckets created
        by an acquire that was already in flight during the first pass.

        Returns:
            Number of bucket items stamped.
        """
        stamped: set[str] = set()
        effective_by_entity: dict[str, bool] = {}

        for _pass in range(2):
            for pk, entity_id in await self._discover_resource_bucket_pks(resource):
                if pk in stamped:
                    continue
                if entity_id not in effective_by_entity:
                    effective, _level = await self.resolve_disabled(entity_id, resource)
                    effective_by_entity[entity_id] = effective
                if effective_by_entity[entity_id] != disabled:
                    # This entity overrides the resource-level value; leave it alone.
                    continue
                try:
                    await self._stamp_bucket_disabled(pk, disabled)
                except Exception as e:
                    # The config write already landed, so the change is half
                    # applied and nothing self-heals it (ADR-125). Report how
                    # far this got so the operator knows to re-run.
                    raise FanoutIncomplete(len(stamped), e, resource=resource) from e
                stamped.add(pk)

        return len(stamped)

    async def _fanout_entity(self, entity_id: str, resource: str | None, disabled: bool) -> int:
        """Stamp every bucket for an entity (optionally scoped to one resource).

        When unscoped (`resource is None`), this is applying the entity's
        `_default_` directive across every resource the entity has a bucket
        for. A resource-specific override for this same entity (its own
        `set_limits(..., resource=<res>, disabled=...)`, or the resource's
        own `disabled` config) can outrank that `_default_` in
        `resolve_disabled`'s walk, exactly as an entity's own override
        outranks a resource-level fan-out in `_fanout_resource`. Each
        discovered bucket's own resource is therefore **re-resolved** and
        stamped with its OWN resolved value, and the `disabled` argument is
        ignored. Skipping the buckets whose resolution disagrees with the
        directive would be wrong for a *clear*: once the entity's
        `_default_` value is gone, a resource whose own config says the
        opposite becomes the deciding level, and its buckets must be
        restamped to that new value rather than left holding a stale one
        (ADR-125). When scoped to one resource, the caller's directive is
        unambiguous for every discovered bucket, so all buckets are stamped
        with `disabled` directly.

        Returns:
            Number of bucket items written.
        """
        stamped: set[str] = set()
        effective_by_resource: dict[str, bool] = {}

        for _pass in range(2):
            for pk in await self._discover_entity_bucket_pks(entity_id, resource):
                if pk in stamped:
                    continue
                target = disabled
                if resource is None:
                    _ns, _eid, bucket_resource, _shard = schema.parse_bucket_pk(pk)
                    if bucket_resource not in effective_by_resource:
                        effective, _level = await self.resolve_disabled(entity_id, bucket_resource)
                        effective_by_resource[bucket_resource] = effective
                    target = effective_by_resource[bucket_resource]
                try:
                    await self._stamp_bucket_disabled(pk, target)
                except Exception as e:
                    # Half-applied: config written, only some buckets stamped.
                    # Nothing reconciles this on its own (ADR-125), so surface
                    # the count rather than leaving the operator guessing.
                    raise FanoutIncomplete(
                        len(stamped), e, resource=resource, entity_id=entity_id
                    ) from e
                stamped.add(pk)
        return len(stamped)

    async def disable_resource(self, resource: str, principal: str | None = None) -> int:
        """Disable a resource for all entities without an explicit override (ADR-125).

        Writes config first, then eagerly stamps every existing bucket so the
        change takes effect on the speculative fast path immediately.

        Args:
            resource: Resource to disable
            principal: Caller identity for audit logging

        Returns:
            Number of bucket items stamped.
        """
        return await self._set_resource_disabled(resource, True, principal)

    async def enable_resource(self, resource: str, principal: str | None = None) -> int:
        """Explicitly enable a resource (stores `disabled: false`).

        Returns:
            Number of bucket items unstamped.
        """
        return await self._set_resource_disabled(resource, False, principal)

    async def clear_resource_disabled(self, resource: str, principal: str | None = None) -> int:
        """Remove the resource's explicit disabled value, reverting to inherit.

        Returns:
            Number of bucket items unstamped.
        """
        return await self._set_resource_disabled(resource, None, principal)

    async def _set_resource_disabled(
        self, resource: str, value: bool | None, principal: str | None
    ) -> int:
        """Write the resource-level `disabled` config, then fan out to buckets.

        The config write is an UPSERT (no `ConditionExpression`) when setting an
        explicit value, mirroring `set_resource_defaults`'s attributes exactly
        (`resource`, `GSI4PK`, `GSI4SK`) via `if_not_exists` so a resource with no
        prior config item — the common case when running purely on system
        defaults — can still be disabled. Clearing back to "inherit" only makes
        sense against an item that already exists, so that branch keeps the
        `attribute_exists(PK)` guard and treats a missing item as a no-op rather
        than fabricating a stub with a bare REMOVE (ADR-125).
        """
        validate_resource(resource)
        client = await self._get_client()

        # 1. Write config first, so any acquire starting from now resolves the
        #    new value on the slow path.
        key = {
            "PK": {"S": schema.pk_resource(self._namespace_id, resource)},
            "SK": {"S": schema.sk_config()},
        }
        if value is None:
            try:
                await client.update_item(
                    TableName=self.table_name,
                    Key=key,
                    UpdateExpression="REMOVE #disabled",
                    ExpressionAttributeNames={"#disabled": schema.CONFIG_FIELD_DISABLED},
                    ConditionExpression="attribute_exists(PK)",
                )
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    raise
                # No config item to clear — already effectively "inherit".
        else:
            # Upsert: create a minimal config item if none exists yet, mirroring
            # set_resource_defaults's attributes so the item is well-formed even
            # with no limits of its own (falls through to system defaults).
            await client.update_item(
                TableName=self.table_name,
                Key=key,
                UpdateExpression=(
                    "SET #disabled = :v,"
                    " #resource = if_not_exists(#resource, :res),"
                    " GSI4PK = if_not_exists(GSI4PK, :gsi4pk),"
                    " GSI4SK = if_not_exists(GSI4SK, :gsi4sk)"
                ),
                ExpressionAttributeNames={
                    "#disabled": schema.CONFIG_FIELD_DISABLED,
                    "#resource": "resource",
                },
                ExpressionAttributeValues={
                    ":v": {"BOOL": value},
                    ":res": {"S": resource},
                    ":gsi4pk": {"S": self._namespace_id},
                    ":gsi4sk": {"S": schema.pk_resource(self._namespace_id, resource)},
                },
            )

            # Writing a resource config item means registering it, exactly as
            # set_resource_defaults does — #RESOURCES is what
            # list_resources_with_defaults() reads, so without this a resource
            # disabled while running on system defaults is invisible to the
            # only listing an operator has. `resources` is a String Set, so
            # re-adding an existing member is a no-op and this needs no
            # create-vs-update guard.
            await client.update_item(
                TableName=self.table_name,
                Key={
                    "PK": {"S": schema.pk_system(self._namespace_id)},
                    "SK": {"S": schema.sk_resources()},
                },
                UpdateExpression=(
                    "SET GSI4PK = if_not_exists(GSI4PK, :reg_gsi4pk),"
                    " GSI4SK = if_not_exists(GSI4SK, :reg_gsi4sk)"
                    " ADD resources :reg_resource"
                ),
                ExpressionAttributeValues={
                    ":reg_resource": {"SS": [resource]},
                    ":reg_gsi4pk": {"S": self._namespace_id},
                    ":reg_gsi4sk": {"S": schema.pk_system(self._namespace_id)},
                },
            )

        await self.invalidate_config_cache()

        # 2. Fan out to existing buckets. For a clear, the effective value is
        #    whatever the resource now inherits, which with no system-level
        #    disable is always False.
        count = await self._fanout_resource(resource, disabled=bool(value))

        await self._log_audit_event(
            action=AuditAction.LIMITS_SET,
            entity_id=f"$RESOURCE:{resource}",
            principal=principal,
            resource=resource,
            details={"disabled": value, "buckets_stamped": count},
        )
        return count

    async def disable_entity(
        self,
        entity_id: str,
        resource: str | None = None,
        principal: str | None = None,
    ) -> int:
        """Disable an entity, for one resource or across all of them (ADR-125).

        Args:
            entity_id: Entity to disable
            resource: Resource to scope to. None targets the entity's
                `_default_` config, disabling it for every resource.
            principal: Caller identity for audit logging

        Returns:
            Number of bucket items written. When scoped to one resource,
            every written bucket is stamped `disabled=True`. When unscoped,
            `_fanout_entity` re-resolves and (re)writes each of the entity's
            buckets to its OWN resolved value (which may disagree with this
            call for a resource with its own override) — see
            `_fanout_entity` for why.
        """
        return await self._set_entity_disabled(entity_id, resource, True, principal)

    async def enable_entity(
        self,
        entity_id: str,
        resource: str | None = None,
        principal: str | None = None,
    ) -> int:
        """Explicitly enable an entity, overriding a disabled resource.

        Returns:
            Number of bucket items written. When scoped to one resource,
            every written bucket is unstamped (`disabled=False` removes the
            attribute). When unscoped, `_fanout_entity` re-resolves and
            (re)writes each of the entity's buckets to its OWN resolved
            value, which may disagree with this call — see `_fanout_entity`.
        """
        return await self._set_entity_disabled(entity_id, resource, False, principal)

    async def clear_entity_disabled(
        self,
        entity_id: str,
        resource: str | None = None,
        principal: str | None = None,
    ) -> int:
        """Remove the entity's explicit disabled value, reverting to inherit.

        Returns:
            Number of bucket items restamped to match the inherited value.
        """
        return await self._set_entity_disabled(entity_id, resource, None, principal)

    async def _set_entity_disabled(
        self,
        entity_id: str,
        resource: str | None,
        value: bool | None,
        principal: str | None,
    ) -> int:
        target_resource = resource if resource is not None else schema.DEFAULT_RESOURCE
        client = await self._get_client()

        key = {
            "PK": {"S": schema.pk_entity(self._namespace_id, entity_id)},
            "SK": {"S": schema.sk_config(target_resource)},
        }
        if value is None:
            try:
                await client.update_item(
                    TableName=self.table_name,
                    Key=key,
                    UpdateExpression="REMOVE #disabled",
                    ExpressionAttributeNames={"#disabled": schema.CONFIG_FIELD_DISABLED},
                    ConditionExpression="attribute_exists(PK)",
                )
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    raise
                # No config item to clear — already effectively "inherit".
        else:
            # The entity may have no config item yet — create a minimal one so
            # the override is durable even with no entity-level limits.
            #
            # A created item has to be registered exactly the way set_limits
            # registers the equivalent one: GSI3 attributes so the sparse
            # index can see it, and +1 on #ENTITY_CONFIG_RESOURCES so the ref
            # count matches the number of live entity config items. Skipping
            # the increment leaves the count low, and a later delete_limits on
            # *any* entity drives it to zero and unregisters the resource
            # while other entities still hold configs for it.
            #
            # Updating an item that already exists must not increment again,
            # so the create is attempted under attribute_not_exists(PK) inside
            # a transaction and falls back to a plain update — the same
            # create-vs-update split set_limits uses.
            update_expression = (
                "SET #disabled = :v,"
                " entity_id = if_not_exists(entity_id, :eid),"
                " #resource = if_not_exists(#resource, :res),"
                " GSI3PK = if_not_exists(GSI3PK, :gsi3pk),"
                " GSI3SK = if_not_exists(GSI3SK, :gsi3sk),"
                " GSI4PK = if_not_exists(GSI4PK, :ns),"
                " GSI4SK = if_not_exists(GSI4SK, :gsi4sk)"
            )
            names = {
                "#disabled": schema.CONFIG_FIELD_DISABLED,
                "#resource": "resource",
            }
            values = {
                ":v": {"BOOL": value},
                ":eid": {"S": entity_id},
                ":res": {"S": target_resource},
                ":gsi3pk": {"S": schema.gsi3_pk_entity_config(self._namespace_id, target_resource)},
                ":gsi3sk": {"S": schema.gsi3_sk_entity(entity_id)},
                ":ns": {"S": self._namespace_id},
                ":gsi4sk": {"S": schema.pk_entity(self._namespace_id, entity_id)},
            }
            try:
                await client.transact_write_items(
                    TransactItems=[
                        {
                            "Update": {
                                "TableName": self.table_name,
                                "Key": key,
                                "UpdateExpression": update_expression,
                                "ConditionExpression": "attribute_not_exists(PK)",
                                "ExpressionAttributeNames": names,
                                "ExpressionAttributeValues": values,
                            }
                        },
                        {
                            "Update": {
                                "TableName": self.table_name,
                                "Key": {
                                    "PK": {"S": schema.pk_system(self._namespace_id)},
                                    "SK": {"S": schema.sk_entity_config_resources()},
                                },
                                "UpdateExpression": (
                                    "SET GSI4PK = if_not_exists(GSI4PK, :reg_gsi4pk),"
                                    " GSI4SK = if_not_exists(GSI4SK, :reg_gsi4sk)"
                                    " ADD #reg_resource :one"
                                ),
                                "ExpressionAttributeNames": {"#reg_resource": target_resource},
                                "ExpressionAttributeValues": {
                                    ":one": {"N": "1"},
                                    ":reg_gsi4pk": {"S": self._namespace_id},
                                    ":reg_gsi4sk": {"S": schema.pk_system(self._namespace_id)},
                                },
                            }
                        },
                    ]
                )
            except ClientError as e:
                if e.response["Error"]["Code"] != "TransactionCanceledException":
                    raise
                reasons = e.response.get("CancellationReasons", [])
                if not (reasons and reasons[0].get("Code") == "ConditionalCheckFailed"):
                    raise
                # Config item already exists — update it without double-counting.
                await client.update_item(
                    TableName=self.table_name,
                    Key=key,
                    UpdateExpression=update_expression,
                    ExpressionAttributeNames=names,
                    ExpressionAttributeValues=values,
                )

        self._config_cache.evict_entity(entity_id, target_resource)

        # For an explicit value the effective state is that value. For a clear,
        # recompute what the entity now inherits.
        if value is None:
            effective, _level = await self.resolve_disabled(entity_id, target_resource)
        else:
            effective = value

        count = await self._fanout_entity(entity_id, resource, disabled=effective)

        await self._log_audit_event(
            action=AuditAction.LIMITS_SET,
            entity_id=entity_id,
            principal=principal,
            resource=target_resource,
            details={"disabled": value, "buckets_stamped": count},
        )
        return count

    async def invalidate_config_cache(self) -> None:
        """Invalidate all cached config entries (ADR-122)."""
        await self._config_cache.invalidate_async()
        self._on_unavailable_cache = None

    def get_cache_stats(self) -> CacheStats:
        """Get config cache performance statistics (ADR-122)."""
        return self._config_cache.get_stats()


# Type assertion: Repository implements RepositoryProtocol
# This is verified at type-check time by mypy, not at runtime
if TYPE_CHECKING:
    from .repository_protocol import RepositoryProtocol

    _: RepositoryProtocol = cast(Repository, None)
