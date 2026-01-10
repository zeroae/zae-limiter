"""CloudFormation stack management for zae-limiter infrastructure."""

from importlib.resources import files
from typing import Any, cast

import aioboto3  # type: ignore
from botocore.exceptions import ClientError

from ..exceptions import StackAlreadyExistsError, StackCreationError


class StackManager:
    """
    Manages CloudFormation stack lifecycle for rate limiter infrastructure.

    Auto-detects local DynamoDB environments (via endpoint_url) and
    gracefully skips CloudFormation operations in those cases.
    """

    def __init__(
        self,
        table_name: str,
        region: str | None = None,
        endpoint_url: str | None = None,
    ) -> None:
        """
        Initialize stack manager.

        Args:
            table_name: Name of the DynamoDB table
            region: AWS region (default: use boto3 defaults)
            endpoint_url: Optional CloudFormation endpoint URL
        """
        self.table_name = table_name
        self.region = region
        self.endpoint_url = endpoint_url
        self._is_local = endpoint_url is not None
        self._session: aioboto3.Session | None = None
        self._client: Any = None

    def _should_use_cloudformation(self) -> bool:
        """
        Determine if CloudFormation should be used.

        Returns False for local DynamoDB environments (endpoint_url is set).
        """
        return not self._is_local

    def get_stack_name(self, table_name: str | None = None) -> str:
        """
        Generate stack name from table name.

        Args:
            table_name: Table name (default: use self.table_name)

        Returns:
            CloudFormation stack name
        """
        name = table_name or self.table_name
        return f"zae-limiter-{name}"

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
        if not parameters:
            # Use defaults from template
            return [{"ParameterKey": "TableName", "ParameterValue": self.table_name}]

        result = []
        # Always include TableName
        result.append({"ParameterKey": "TableName", "ParameterValue": self.table_name})

        # Map common parameter names
        param_mapping = {
            "snapshot_windows": "SnapshotWindows",
            "retention_days": "SnapshotRetentionDays",
            "lambda_memory_size": "LambdaMemorySize",
            "lambda_timeout": "LambdaTimeout",
            "enable_aggregator": "EnableAggregator",
        }

        for key, value in parameters.items():
            # Try mapped name first, fallback to key as-is
            param_key = param_mapping.get(key, key)
            result.append({"ParameterKey": param_key, "ParameterValue": str(value)})

        return result

    async def stack_exists(self, stack_name: str) -> bool:
        """
        Check if a CloudFormation stack exists.

        Args:
            stack_name: Name of the stack

        Returns:
            True if stack exists and is not in DELETE_COMPLETE state
        """
        if not self._should_use_cloudformation():
            return False

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
        if not self._should_use_cloudformation():
            return None

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
        stack_name: str | None = None,
        parameters: dict[str, str] | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        """
        Create CloudFormation stack.

        Auto-skips for local DynamoDB environments. Handles stack already
        exists gracefully.

        Args:
            stack_name: Override stack name (default: auto-generated)
            parameters: Stack parameters dict (keys: snake_case or PascalCase)
            wait: Wait for stack to be CREATE_COMPLETE

        Returns:
            Dict with stack_id, stack_name, and status

        Raises:
            StackCreationError: If stack creation fails
            StackAlreadyExistsError: If stack already exists
        """
        if not self._should_use_cloudformation():
            # Local environment - skip CloudFormation
            return {
                "stack_id": None,
                "stack_name": None,
                "status": "skipped_local",
                "message": "CloudFormation skipped for local DynamoDB",
            }

        stack_name = stack_name or self.get_stack_name()
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

        # Create stack
        try:
            response = await client.create_stack(
                StackName=stack_name,
                TemplateBody=template_body,
                Parameters=cfn_parameters,
                Capabilities=["CAPABILITY_NAMED_IAM"],
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

        Auto-skips for local DynamoDB environments.

        Args:
            stack_name: Name of the stack to delete
            wait: Wait for stack to be DELETE_COMPLETE

        Raises:
            StackCreationError: If deletion fails
        """
        if not self._should_use_cloudformation():
            # Local environment - skip CloudFormation
            return

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
