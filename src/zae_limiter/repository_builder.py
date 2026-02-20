"""Builder pattern for Repository construction with async initialization.

The RepositoryBuilder separates sync configuration from async I/O operations
(infrastructure creation, namespace resolution). Configuration is done via
fluent method chaining, then ``build()`` performs all async work and returns
a fully initialized Repository.

Use ``Repository.builder()`` when you need custom infrastructure options
that ``Repository.open()`` doesn't expose. For most use cases, prefer
``Repository.open()``.

Example:
    repo = await (
        Repository.builder()
        .permission_boundary("arn:aws:iam::aws:policy/PowerUserAccess")
        .role_name_format("PowerUserPB-{}")
        .policy_name_format("PowerUserPB-{}")
        .lambda_memory(512)
        .build()
    )
    limiter = RateLimiter(repository=repo)
"""

import warnings
from typing import TYPE_CHECKING, Any

from .exceptions import NamespaceNotFoundError
from .naming import resolve_stack_name

if TYPE_CHECKING:
    from .models import OnUnavailableAction, StackOptions
    from .repository import Repository


class RepositoryBuilder:
    """Fluent builder for constructing a fully initialized Repository.

    All configuration methods return ``self`` for chaining. Call ``build()``
    to perform async initialization and get a ``Repository``.

    Stack defaults mirror ``Repository.open()``: ``ZAEL_STACK`` env
    var or ``"zae-limiter"``, namespace ``"default"``.
    """

    def __init__(self) -> None:
        self._stack: str | None = None
        self._region: str | None = None
        self._endpoint_url: str | None = None
        self._namespace_name: str | None = None
        self._config_cache_ttl = 60
        self._auto_update = True
        self._bucket_ttl_multiplier = 7
        self._on_unavailable: OnUnavailableAction | None = None
        self._infra_options: dict[str, Any] = {}

    # -------------------------------------------------------------------------
    # Connection configuration
    # -------------------------------------------------------------------------

    def stack(self, name: str) -> "RepositoryBuilder":
        """Set the stack name (default: ``ZAEL_STACK`` env var or ``"zae-limiter"``)."""
        self._stack = name
        return self

    def region(self, name: str) -> "RepositoryBuilder":
        """Set the AWS region (e.g., ``"us-east-1"``)."""
        self._region = name
        return self

    def endpoint_url(self, url: str) -> "RepositoryBuilder":
        """Set a custom endpoint URL (e.g., ``"http://localhost:4566"`` for LocalStack)."""
        self._endpoint_url = url
        return self

    # -------------------------------------------------------------------------
    # Behavioral configuration
    # -------------------------------------------------------------------------

    def namespace(self, name: str) -> "RepositoryBuilder":
        """Set the namespace to resolve during build.

        Defaults to ``ZAEL_NAMESPACE`` env var or ``"default"``.
        """
        self._namespace_name = name
        return self

    def config_cache_ttl(self, seconds: int) -> "RepositoryBuilder":
        """Set config cache TTL in seconds (default: 60, 0 to disable)."""
        self._config_cache_ttl = seconds
        return self

    def auto_update(self, enabled: bool) -> "RepositoryBuilder":
        """Enable/disable auto-update of Lambda on version mismatch (default: True)."""
        self._auto_update = enabled
        return self

    def bucket_ttl_multiplier(self, value: int) -> "RepositoryBuilder":
        """Set bucket TTL multiplier (default: 7, 0 to disable)."""
        self._bucket_ttl_multiplier = value
        return self

    def on_unavailable(self, value: "OnUnavailableAction") -> "RepositoryBuilder":
        """Set on_unavailable behavior for the namespace ("allow" or "block").

        This is persisted as the system-level default via set_system_defaults().
        """
        self._on_unavailable = value
        return self

    # -------------------------------------------------------------------------
    # Infrastructure configuration (maps to StackOptions fields)
    # -------------------------------------------------------------------------

    def snapshot_windows(self, value: str) -> "RepositoryBuilder":
        """Set snapshot windows (e.g., "hourly,daily")."""
        self._infra_options["snapshot_windows"] = value
        return self

    def usage_retention_days(self, value: int) -> "RepositoryBuilder":
        """Set usage snapshot retention in days."""
        self._infra_options["usage_retention_days"] = value
        return self

    def audit_retention_days(self, value: int) -> "RepositoryBuilder":
        """Set audit record retention in days."""
        self._infra_options["audit_retention_days"] = value
        return self

    def enable_aggregator(self, value: bool = True) -> "RepositoryBuilder":
        """Enable/disable Lambda aggregator."""
        self._infra_options["enable_aggregator"] = value
        return self

    def pitr_recovery_days(self, value: int | None) -> "RepositoryBuilder":
        """Set Point-in-Time Recovery period (1-35, None for AWS default)."""
        self._infra_options["pitr_recovery_days"] = value
        return self

    def log_retention_days(self, value: int) -> "RepositoryBuilder":
        """Set CloudWatch log retention in days."""
        self._infra_options["log_retention_days"] = value
        return self

    def lambda_timeout(self, value: int) -> "RepositoryBuilder":
        """Set Lambda timeout in seconds (1-900)."""
        self._infra_options["lambda_timeout"] = value
        return self

    def lambda_memory(self, value: int) -> "RepositoryBuilder":
        """Set Lambda memory in MB (128-3008)."""
        self._infra_options["lambda_memory"] = value
        return self

    def enable_alarms(self, value: bool = True) -> "RepositoryBuilder":
        """Enable/disable CloudWatch alarms."""
        self._infra_options["enable_alarms"] = value
        return self

    def alarm_sns_topic(self, value: str | None) -> "RepositoryBuilder":
        """Set SNS topic ARN for alarm notifications."""
        self._infra_options["alarm_sns_topic"] = value
        return self

    def lambda_duration_threshold_pct(self, value: int) -> "RepositoryBuilder":
        """Set duration alarm threshold as percentage of timeout (1-100)."""
        self._infra_options["lambda_duration_threshold_pct"] = value
        return self

    def permission_boundary(self, value: str | None) -> "RepositoryBuilder":
        """Set IAM permission boundary (policy name or full ARN)."""
        self._infra_options["permission_boundary"] = value
        return self

    def role_name_format(self, value: str | None) -> "RepositoryBuilder":
        """Set format template for IAM role names."""
        self._infra_options["role_name_format"] = value
        return self

    def policy_name_format(self, value: str | None) -> "RepositoryBuilder":
        """Set format template for managed policy names."""
        self._infra_options["policy_name_format"] = value
        return self

    def enable_audit_archival(self, value: bool = True) -> "RepositoryBuilder":
        """Enable/disable S3 audit archival."""
        self._infra_options["enable_audit_archival"] = value
        return self

    def audit_archive_glacier_days(self, value: int) -> "RepositoryBuilder":
        """Set days before transitioning archives to Glacier IR."""
        self._infra_options["audit_archive_glacier_days"] = value
        return self

    def enable_tracing(self, value: bool = True) -> "RepositoryBuilder":
        """Enable/disable X-Ray tracing."""
        self._infra_options["enable_tracing"] = value
        return self

    def create_iam_roles(self, value: bool = True) -> "RepositoryBuilder":
        """Create App/Admin/ReadOnly IAM roles (default: False)."""
        self._infra_options["create_iam_roles"] = value
        return self

    def create_iam(self, value: bool = True) -> "RepositoryBuilder":
        """Create IAM resources (policies and roles)."""
        self._infra_options["create_iam"] = value
        return self

    def aggregator_role_arn(self, value: str | None) -> "RepositoryBuilder":
        """Set existing IAM role ARN for the Lambda aggregator."""
        self._infra_options["aggregator_role_arn"] = value
        return self

    def enable_deletion_protection(self, value: bool = True) -> "RepositoryBuilder":
        """Enable DynamoDB table deletion protection."""
        self._infra_options["enable_deletion_protection"] = value
        return self

    def tags(self, value: dict[str, str] | None) -> "RepositoryBuilder":
        """Set user-defined tags for the CloudFormation stack."""
        self._infra_options["tags"] = value
        return self

    # -------------------------------------------------------------------------
    # Migration helper
    # -------------------------------------------------------------------------

    def stack_options(self, opts: "StackOptions") -> "RepositoryBuilder":
        """Copy all fields from a StackOptions into the builder.

        .. deprecated::
            Use individual builder methods instead. This method exists
            to ease migration from the old ``Repository(stack_options=...)``
            pattern.
        """
        warnings.warn(
            "RepositoryBuilder.stack_options() is deprecated. "
            "Use individual builder methods (e.g., .lambda_memory(512)) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        import dataclasses

        for f in dataclasses.fields(opts):
            self._infra_options[f.name] = getattr(opts, f.name)
        return self

    # -------------------------------------------------------------------------
    # Build
    # -------------------------------------------------------------------------

    async def build(self) -> "Repository":
        """Perform async initialization and return a fully initialized Repository.

        Steps:
            1. Construct Repository with materialized StackOptions (if any infra options set)
            2. Ensure infrastructure exists (no-op if no infra options)
            3. Register the "default" namespace (conditional PutItem, no-op if exists)
            4. Resolve the requested namespace name to an opaque ID
            5. Reinitialize config cache with resolved namespace ID
            6. Version check and Lambda auto-update
            7. Return fully initialized Repository

        Raises:
            NamespaceNotFoundError: If the requested namespace doesn't exist
            IncompatibleSchemaError: If schema migration is required
            VersionMismatchError: If auto_update is False and versions differ
        """
        from .models import StackOptions as StackOptionsModel
        from .naming import resolve_namespace_name
        from .repository import Repository

        # 1. Construct Repository
        name = resolve_stack_name(self._stack)
        ns_name = resolve_namespace_name(self._namespace_name)
        stack_opts = StackOptionsModel(**self._infra_options) if self._infra_options else None
        repo = Repository(
            name=name,
            region=self._region,
            endpoint_url=self._endpoint_url,
            stack_options=stack_opts,
            config_cache_ttl=self._config_cache_ttl,
            _skip_deprecation_warning=True,
        )
        repo._bucket_ttl_refill_multiplier = self._bucket_ttl_multiplier
        repo._auto_update = self._auto_update

        # 2. Ensure infrastructure exists
        await repo._ensure_infrastructure_internal()

        # 3. Register the "default" namespace (idempotent)
        await repo._register_namespace("default")

        # 4. Resolve the requested namespace
        namespace_id = await repo._resolve_namespace(ns_name)
        if namespace_id is None:
            raise NamespaceNotFoundError(ns_name)

        # 5. Set resolved namespace and reinitialize config cache
        repo._namespace_id = namespace_id
        repo._namespace_name = ns_name
        repo._reinitialize_config_cache(namespace_id)

        # 5b. Persist on_unavailable as system config if set
        if self._on_unavailable is not None:
            existing_limits, _ = await repo.get_system_defaults()
            await repo.set_system_defaults(
                limits=existing_limits,
                on_unavailable=self._on_unavailable,
            )

        # 6. Version check + Lambda auto-update (always, no endpoint_url guard)
        if self._auto_update:
            await repo._check_and_update_version_auto()
        else:
            await repo._check_version_strict()

        # 7. Mark as builder-initialized
        repo._builder_initialized = True

        return repo
