"""CloudFormation stack management for zae-limiter infrastructure."""

import asyncio
import time
from importlib.resources import files
from typing import Any, cast

import aioboto3
from botocore.exceptions import ClientError

from ..exceptions import StackAlreadyExistsError, StackCreationError
from ..models import StackOptions
from ..naming import normalize_stack_name
from .lambda_builder import build_lambda_package

# Version tag keys for infrastructure
VERSION_TAG_PREFIX = "zae-limiter:"
VERSION_TAG_KEY = f"{VERSION_TAG_PREFIX}version"
LAMBDA_VERSION_TAG_KEY = f"{VERSION_TAG_PREFIX}lambda-version"
SCHEMA_VERSION_TAG_KEY = f"{VERSION_TAG_PREFIX}schema-version"

# Discovery tag keys
MANAGED_BY_TAG_KEY = "ManagedBy"
MANAGED_BY_TAG_VALUE = "zae-limiter"
NAME_TAG_KEY = f"{VERSION_TAG_PREFIX}name"


class StackManager:
    """
    Manages CloudFormation stack lifecycle for rate limiter infrastructure.

    Supports both AWS and LocalStack environments. When endpoint_url is provided,
    CloudFormation operations are performed against that endpoint.

    The stack_name is validated and used as-is (no prefix added).
    The table_name is always identical to the stack_name.
    """

    def __init__(
        self,
        stack_name: str,
        region: str | None = None,
        endpoint_url: str | None = None,
    ) -> None:
        """
        Initialize stack manager.

        Args:
            stack_name: Stack identifier (e.g., 'rate-limits').
                Used as-is for the CloudFormation stack name.
            region: AWS region (default: use boto3 defaults)
            endpoint_url: Optional endpoint URL (for LocalStack or other AWS-compatible services)
        """
        # Validate and normalize stack name
        self.stack_name = normalize_stack_name(stack_name)
        # Table name is always identical to stack name
        self.table_name = self.stack_name
        self.region = region
        self.endpoint_url = endpoint_url
        self._session: aioboto3.Session | None = None
        self._client: Any = None

    async def _get_client(self) -> Any:
        """Get or create CloudFormation client."""
        if self._client is not None:
            return self._client

        if self._session is None:
            self._session = aioboto3.Session()

        kwargs: dict[str, Any] = {}
        if self.region:
            kwargs["region_name"] = self.region
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url

        # Type checker doesn't know _session is not None after the check above
        session = self._session
        self._client = await session.client("cloudformation", **kwargs).__aenter__()
        return self._client

    def _load_template(self) -> str:
        """Load CloudFormation template from package resources."""
        try:
            # Python 3.9+ importlib.resources API
            template_data = files("zae_limiter.infra").joinpath("cfn_template.yaml").read_text()
            return template_data
        except Exception as e:
            raise StackCreationError(
                stack_name="unknown",
                reason=f"Failed to load CloudFormation template: {e}",
            ) from e

    def _format_parameters(self, parameters: dict[str, str] | None) -> list[dict[str, str]]:
        """
        Convert parameter dict to CloudFormation format.

        Args:
            parameters: Dict of parameter key-value pairs

        Returns:
            List of CloudFormation parameter dicts
        """
        # Get schema version
        try:
            from ..version import get_schema_version

            schema_version = get_schema_version()
        except ImportError:
            schema_version = "1.0.0"

        if not parameters:
            # Use defaults from template with schema version
            return [
                {"ParameterKey": "SchemaVersion", "ParameterValue": schema_version},
            ]

        result = []
        # Always include SchemaVersion
        result.append({"ParameterKey": "SchemaVersion", "ParameterValue": schema_version})

        # Map common parameter names
        param_mapping = {
            "snapshot_windows": "SnapshotWindows",
            "retention_days": "SnapshotRetentionDays",
            "lambda_memory_size": "LambdaMemorySize",
            "lambda_timeout": "LambdaTimeout",
            "enable_aggregator": "EnableAggregator",
            "schema_version": "SchemaVersion",
            "pitr_recovery_days": "PITRRecoveryPeriodDays",
            "log_retention_days": "LogRetentionDays",
            "enable_alarms": "EnableAlarms",
            "alarm_sns_topic_arn": "AlarmSNSTopicArn",
            "lambda_duration_threshold": "LambdaDurationThreshold",
            "permission_boundary": "PermissionBoundary",
            "role_name": "RoleName",
            "enable_audit_archival": "EnableAuditArchival",
            "audit_archive_glacier_days": "AuditArchiveGlacierTransitionDays",
            "enable_tracing": "EnableTracing",
            "enable_iam_roles": "EnableIAMRoles",
        }

        for key, value in parameters.items():
            # Try mapped name first, fallback to key as-is
            param_key = param_mapping.get(key, key)
            result.append({"ParameterKey": param_key, "ParameterValue": str(value)})

        return result

    def _get_version_tags(self) -> list[dict[str, str]]:
        """
        Get version tags for CloudFormation stack.

        Returns:
            List of CloudFormation tag dicts
        """
        from .. import __version__

        try:
            from ..version import get_schema_version

            schema_version = get_schema_version()
        except ImportError:
            schema_version = "1.0.0"

        return [
            {"Key": VERSION_TAG_KEY, "Value": __version__},
            {"Key": SCHEMA_VERSION_TAG_KEY, "Value": schema_version},
            {"Key": LAMBDA_VERSION_TAG_KEY, "Value": __version__},
        ]

    def _get_all_tags(self, user_tags: dict[str, str] | None = None) -> list[dict[str, str]]:
        """
        Build complete tag list for CloudFormation stack.

        Combines discovery tags, version tags, and user-defined tags.
        Managed tags (discovery + version) take precedence over user tags
        with the same key.

        Args:
            user_tags: Optional user-defined tags

        Returns:
            List of CloudFormation tag dicts
        """
        from ..naming import PREFIX

        # Derive user_name: strip legacy ZAEL- prefix if present
        user_name = self.stack_name
        if user_name.startswith(PREFIX):
            user_name = user_name[len(PREFIX) :]

        # Start with user tags (lowest precedence)
        tag_dict: dict[str, str] = {}
        if user_tags:
            tag_dict.update(user_tags)

        # Discovery tags (override user tags on collision)
        tag_dict[MANAGED_BY_TAG_KEY] = MANAGED_BY_TAG_VALUE
        tag_dict[NAME_TAG_KEY] = user_name

        # Version tags (override user tags on collision)
        for tag in self._get_version_tags():
            tag_dict[tag["Key"]] = tag["Value"]

        return [{"Key": k, "Value": v} for k, v in tag_dict.items()]

    async def ensure_tags(self, user_tags: dict[str, str] | None = None) -> bool:
        """
        Ensure stack has discovery tags. Auto-tags existing stacks.

        Checks if the stack has the ``ManagedBy`` and ``zae-limiter:name``
        tags. If missing, updates the stack tags via CloudFormation.

        Args:
            user_tags: Optional user-defined tags to include

        Returns:
            True if tags were added/updated, False if already present
        """
        client = await self._get_client()

        try:
            response = await client.describe_stacks(StackName=self.stack_name)
            stacks = response.get("Stacks", [])
            if not stacks:
                return False

            current_tags = {tag["Key"]: tag["Value"] for tag in stacks[0].get("Tags", [])}
        except ClientError:
            return False

        # Check if discovery tags exist
        from ..naming import PREFIX

        # Derive user_name: strip legacy ZAEL- prefix if present
        user_name = self.stack_name
        if user_name.startswith(PREFIX):
            user_name = user_name[len(PREFIX) :]
        has_managed_by = current_tags.get(MANAGED_BY_TAG_KEY) == MANAGED_BY_TAG_VALUE
        has_name_tag = current_tags.get(NAME_TAG_KEY) == user_name

        if has_managed_by and has_name_tag:
            return False

        # Apply tags via stack update (UsePreviousTemplate preserves resources)
        new_tags = self._get_all_tags(user_tags)
        try:
            await client.update_stack(
                StackName=self.stack_name,
                UsePreviousTemplate=True,
                Tags=new_tags,
                Capabilities=["CAPABILITY_NAMED_IAM"],
            )
        except ClientError as e:
            # "No updates are to be performed" is not an error
            if "No updates" in str(e):
                return False
            raise

        return True

    async def stack_exists(self, stack_name: str) -> bool:
        """
        Check if a CloudFormation stack exists.

        Args:
            stack_name: Name of the stack

        Returns:
            True if stack exists and is not in DELETE_COMPLETE state
        """
        client = await self._get_client()
        try:
            response = await client.describe_stacks(StackName=stack_name)
            stacks = response.get("Stacks", [])
            if not stacks:
                return False

            # Stack exists if it's not in DELETE_COMPLETE state
            status = cast(str, stacks[0]["StackStatus"])
            return status != "DELETE_COMPLETE"
        except ClientError as e:
            if e.response["Error"]["Code"] == "ValidationError":
                # Stack doesn't exist
                return False
            raise

    async def get_stack_status(self, stack_name: str) -> str | None:
        """
        Get current status of a CloudFormation stack.

        Args:
            stack_name: Name of the stack

        Returns:
            Stack status string or None if stack doesn't exist
        """
        client = await self._get_client()
        try:
            response = await client.describe_stacks(StackName=stack_name)
            stacks = response.get("Stacks", [])
            if not stacks:
                return None
            return cast(str, stacks[0]["StackStatus"])
        except ClientError as e:
            if e.response["Error"]["Code"] == "ValidationError":
                return None
            raise

    async def create_stack(
        self,
        stack_options: StackOptions | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        """
        Create CloudFormation stack.

        Handles stack already exists gracefully.

        Args:
            stack_options: Stack configuration
            wait: Wait for stack to be CREATE_COMPLETE

        Returns:
            Dict with stack_id, stack_name, and status

        Raises:
            StackCreationError: If stack creation fails
            StackAlreadyExistsError: If stack already exists
        """
        # Use the normalized stack name from constructor
        stack_name = self.stack_name

        # Convert stack_options to parameters
        parameters = stack_options.to_parameters(self.stack_name) if stack_options else None
        client = await self._get_client()

        # Check if stack already exists
        existing_status = await self.get_stack_status(stack_name)
        if existing_status:
            if wait and existing_status in ("CREATE_IN_PROGRESS", "UPDATE_IN_PROGRESS"):
                # Wait for in-progress operation
                waiter = client.get_waiter("stack_create_complete")
                try:
                    await waiter.wait(StackName=stack_name)
                except Exception as e:
                    raise StackCreationError(
                        stack_name=stack_name,
                        reason=f"Waiting for existing stack failed: {e}",
                    ) from e

                return {
                    "stack_id": stack_name,
                    "stack_name": stack_name,
                    "status": "already_exists_and_ready",
                }

            # Auto-tag existing stacks that may lack discovery tags
            user_tags = stack_options.tags if stack_options else None
            await self.ensure_tags(user_tags)

            # Stack exists and is stable
            return {
                "stack_id": stack_name,
                "stack_name": stack_name,
                "status": existing_status,
                "message": f"Stack already exists with status: {existing_status}",
            }

        # Load template and format parameters
        template_body = self._load_template()
        cfn_parameters = self._format_parameters(parameters)
        user_tags = stack_options.tags if stack_options else None
        tags = self._get_all_tags(user_tags)

        # Create stack
        try:
            response = await client.create_stack(
                StackName=stack_name,
                TemplateBody=template_body,
                Parameters=cfn_parameters,
                Capabilities=["CAPABILITY_NAMED_IAM"],
                Tags=tags,
            )

            stack_id = response["StackId"]

            if wait:
                # Wait for stack creation to complete
                waiter = client.get_waiter("stack_create_complete")
                try:
                    await waiter.wait(StackName=stack_name)
                except Exception as e:
                    # Fetch stack events for debugging
                    events = await self._get_stack_events(client, stack_name)
                    raise StackCreationError(
                        stack_name=stack_name,
                        reason=f"Stack creation failed: {e}",
                        events=events,
                    ) from e

            return {
                "stack_id": stack_id,
                "stack_name": stack_name,
                "status": "CREATE_COMPLETE" if wait else "CREATE_IN_PROGRESS",
            }

        except ClientError as e:
            error_code = e.response["Error"]["Code"]

            if error_code == "AlreadyExistsException":
                # Race condition - stack was just created
                if wait:
                    waiter = client.get_waiter("stack_create_complete")
                    try:
                        await waiter.wait(StackName=stack_name)
                    except Exception:
                        pass  # Best effort

                raise StackAlreadyExistsError(
                    stack_name=stack_name,
                    reason="Stack already exists",
                )

            # Other error
            raise StackCreationError(
                stack_name=stack_name,
                reason=f"CloudFormation API error: {e.response['Error']['Message']}",
            ) from e

    async def delete_stack(self, stack_name: str, wait: bool = True) -> None:
        """
        Delete CloudFormation stack.

        Args:
            stack_name: Name of the stack to delete
            wait: Wait for stack to be DELETE_COMPLETE

        Raises:
            StackCreationError: If deletion fails
        """
        client = await self._get_client()

        try:
            await client.delete_stack(StackName=stack_name)

            if wait:
                waiter = client.get_waiter("stack_delete_complete")
                await waiter.wait(StackName=stack_name)

        except ClientError as e:
            error_code = e.response["Error"]["Code"]

            # Ignore if stack doesn't exist
            if error_code == "ValidationError" and "does not exist" in str(e):
                return

            raise StackCreationError(
                stack_name=stack_name,
                reason=f"Stack deletion failed: {e.response['Error']['Message']}",
            ) from e

    async def _get_stack_events(
        self, client: Any, stack_name: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """
        Fetch recent stack events for debugging.

        Args:
            client: CloudFormation client
            stack_name: Stack name
            limit: Max number of events to fetch

        Returns:
            List of stack event dicts
        """
        try:
            response = await client.describe_stack_events(StackName=stack_name)
            events = response.get("StackEvents", [])[:limit]

            return [
                {
                    "timestamp": e.get("Timestamp"),
                    "resource_type": e.get("ResourceType"),
                    "logical_id": e.get("LogicalResourceId"),
                    "status": e.get("ResourceStatus"),
                    "reason": e.get("ResourceStatusReason"),
                }
                for e in events
            ]
        except Exception:
            return []

    async def deploy_lambda_code(
        self,
        function_name: str | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        """
        Deploy Lambda function code after stack creation.

        Builds the Lambda deployment package from the installed zae_limiter
        package and updates the Lambda function code via the AWS API.

        This is called after CloudFormation stack creation to replace the
        placeholder code with the actual aggregator implementation.

        Args:
            function_name: Lambda function name (default: {table_name}-aggregator)
            wait: Wait for function update to complete

        Returns:
            Dict with function_arn, code_sha256, and status

        Raises:
            StackCreationError: If Lambda deployment fails
        """
        function_name = function_name or f"{self.table_name}-aggregator"

        # Build Lambda package
        try:
            zip_bytes = build_lambda_package()
        except Exception as e:
            raise StackCreationError(
                stack_name=self.stack_name,
                reason=f"Failed to build Lambda package: {e}",
            ) from e

        # Get Lambda client
        if self._session is None:
            self._session = aioboto3.Session()

        kwargs: dict[str, Any] = {}
        if self.region:
            kwargs["region_name"] = self.region
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url

        session = self._session
        async with session.client("lambda", **kwargs) as lambda_client:
            try:
                # Update function code
                response = await lambda_client.update_function_code(
                    FunctionName=function_name,
                    ZipFile=zip_bytes,
                )

                if wait:
                    # Wait for update to complete
                    waiter = lambda_client.get_waiter("function_updated")
                    try:
                        await waiter.wait(FunctionName=function_name)
                    except Exception as e:
                        raise StackCreationError(
                            stack_name=self.stack_name,
                            reason=f"Waiting for Lambda update failed: {e}",
                        ) from e

                if wait:
                    # Wait for Lambda to be Active before returning
                    # This ensures the function is ready to process events
                    waiter = lambda_client.get_waiter("function_active")
                    try:
                        await waiter.wait(FunctionName=function_name)
                    except Exception as e:
                        raise StackCreationError(
                            stack_name=self.stack_name,
                            reason=f"Waiting for Lambda to be active failed: {e}",
                        ) from e

                # Update Lambda tags to reflect new version
                from .. import __version__

                await lambda_client.tag_resource(
                    Resource=response["FunctionArn"],
                    Tags={
                        "zae-limiter:lambda-version": __version__,
                    },
                )

                result = {
                    "function_arn": response["FunctionArn"],
                    "code_sha256": response["CodeSha256"],
                    "status": "deployed",
                    "size_bytes": len(zip_bytes),
                    "version": __version__,
                }

            except ClientError as e:
                error_code = e.response["Error"]["Code"]
                error_msg = e.response["Error"]["Message"]

                raise StackCreationError(
                    stack_name=self.stack_name,
                    reason=f"Lambda deployment failed ({error_code}): {error_msg}",
                ) from e

        # Wait for ESM to be ready outside Lambda client context
        # ESM needs ~45s after stack creation to establish stream position
        if wait:
            esm_ready = await self.wait_for_esm_ready(function_name)
            result["esm_ready"] = esm_ready

        return result

    async def wait_for_esm_ready(
        self,
        function_name: str,
        max_seconds: int = 120,
        min_stabilization: float = 45.0,
    ) -> bool:
        """
        Wait for Lambda Event Source Mapping to be ready to process events.

        After CloudFormation stack creation and Lambda deployment, the ESM needs
        time to fully initialize and establish its position in the DynamoDB stream.
        Even after reporting State="Enabled" and LastProcessingResult="No records
        processed", ESM may not reliably capture events for ~45 seconds.

        This is because ESM with StartingPosition: LATEST must establish its
        position in the stream. Events written before ESM establishes its position
        are missed (see issue #249).

        Args:
            function_name: Lambda function name
            max_seconds: Maximum time to wait for ESM to be fully ready
            min_stabilization: Minimum seconds to wait after ESM shows enabled

        Returns:
            True if ESM is ready, False if timeout or no ESM found
        """
        if self._session is None:
            self._session = aioboto3.Session()

        kwargs: dict[str, Any] = {}
        if self.region:
            kwargs["region_name"] = self.region
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url

        start_time = time.time()
        enabled_at: float | None = None
        interval = 5.0

        async with self._session.client("lambda", **kwargs) as lambda_client:
            while time.time() - start_time < max_seconds:
                try:
                    response = await lambda_client.list_event_source_mappings(
                        FunctionName=function_name,
                    )
                    mappings = response.get("EventSourceMappings", [])

                    for mapping in mappings:
                        state = mapping.get("State", "")
                        last_result = mapping.get("LastProcessingResult")

                        if state in ("Disabled", "Disabling"):
                            return False

                        if state in ("Creating", "Enabling", "Updating"):
                            # Still transitioning, keep waiting
                            break

                        if state == "Enabled":
                            # Track when ESM first became enabled
                            if enabled_at is None:
                                enabled_at = time.time()

                            # Check if ESM has polled at least once
                            if last_result is None:
                                # ESM hasn't polled yet, keep waiting
                                break

                            if last_result not in ("OK", "No records processed"):
                                # Error state, keep waiting
                                break

                            # ESM is enabled and has polled - check stabilization
                            time_since_enabled = time.time() - enabled_at
                            if time_since_enabled >= min_stabilization:
                                return True
                            # Still stabilizing, keep waiting
                            break

                    await asyncio.sleep(interval)
                except Exception:
                    await asyncio.sleep(interval)

        return False

    async def close(self) -> None:
        """Close the underlying session and client."""
        if self._client is not None:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:
                pass  # Best effort cleanup
            finally:
                self._client = None
        self._session = None

    async def __aenter__(self) -> "StackManager":
        """Enter async context manager."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Exit async context manager."""
        await self.close()
