"""CloudFormation stack management for Admin API infrastructure."""

from importlib.resources import files
from typing import Any, cast

import aioboto3  # type: ignore
from botocore.exceptions import ClientError

from ..exceptions import StackCreationError
from ..naming import normalize_stack_name
from .lambda_builder import build_admin_lambda_package


class AdminStackManager:
    """
    Manages CloudFormation stack lifecycle for Admin API infrastructure.

    This creates a separate stack for the admin REST API (API Gateway + Lambda).
    """

    def __init__(
        self,
        name: str,
        core_stack_name: str,
        region: str | None = None,
        endpoint_url: str | None = None,
    ) -> None:
        """
        Initialize admin stack manager.

        Args:
            name: Admin stack identifier (will be prefixed with 'ZAEL-' and suffixed with '-admin')
            core_stack_name: Name of the core ZAEL stack (for DynamoDB table reference)
            region: AWS region (default: use boto3 defaults)
            endpoint_url: Optional endpoint URL (for LocalStack)
        """
        base_name = normalize_stack_name(name)
        self.stack_name = f"{base_name}-admin"
        self.core_stack_name = normalize_stack_name(core_stack_name)
        self.region = region
        self.endpoint_url = endpoint_url
        self._session: aioboto3.Session | None = None
        self._cfn_client: Any = None
        self._lambda_client: Any = None

    async def __aenter__(self) -> "AdminStackManager":
        """Async context manager entry."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Async context manager exit."""
        await self.close()

    async def close(self) -> None:
        """Close clients."""
        if self._cfn_client is not None:
            await self._cfn_client.__aexit__(None, None, None)
            self._cfn_client = None
        if self._lambda_client is not None:
            await self._lambda_client.__aexit__(None, None, None)
            self._lambda_client = None

    async def _get_cfn_client(self) -> Any:
        """Get or create CloudFormation client."""
        if self._cfn_client is not None:
            return self._cfn_client

        if self._session is None:
            self._session = aioboto3.Session()

        kwargs: dict[str, Any] = {}
        if self.region:
            kwargs["region_name"] = self.region
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url

        session = self._session
        self._cfn_client = await session.client("cloudformation", **kwargs).__aenter__()
        return self._cfn_client

    async def _get_lambda_client(self) -> Any:
        """Get or create Lambda client."""
        if self._lambda_client is not None:
            return self._lambda_client

        if self._session is None:
            self._session = aioboto3.Session()

        kwargs: dict[str, Any] = {}
        if self.region:
            kwargs["region_name"] = self.region
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url

        session = self._session
        self._lambda_client = await session.client("lambda", **kwargs).__aenter__()
        return self._lambda_client

    def _load_template(self) -> str:
        """Load CloudFormation template from package resources."""
        try:
            template_data = (
                files("zae_limiter.infra")
                .joinpath("cfn_admin_template.yaml")
                .read_text()
            )
            return template_data
        except Exception as e:
            raise StackCreationError(
                stack_name="unknown",
                reason=f"Failed to load Admin CloudFormation template: {e}",
            ) from e

    def _format_parameters(
        self,
        auth_type: str,
        lambda_timeout: int,
        lambda_memory: int,
        log_retention_days: int,
        permission_boundary: str | None,
        role_name_format: str | None,
    ) -> list[dict[str, str]]:
        """Format CloudFormation parameters."""
        params = [
            {"ParameterKey": "StackName", "ParameterValue": self.stack_name},
            {"ParameterKey": "CoreStackName", "ParameterValue": self.core_stack_name},
            {"ParameterKey": "AuthType", "ParameterValue": auth_type},
            {"ParameterKey": "LambdaTimeout", "ParameterValue": str(lambda_timeout)},
            {"ParameterKey": "LambdaMemory", "ParameterValue": str(lambda_memory)},
            {"ParameterKey": "LogRetentionDays", "ParameterValue": str(log_retention_days)},
        ]

        if permission_boundary:
            params.append(
                {"ParameterKey": "PermissionBoundary", "ParameterValue": permission_boundary}
            )

        if role_name_format:
            params.append(
                {"ParameterKey": "RoleNameFormat", "ParameterValue": role_name_format}
            )

        return params

    async def get_stack_status(self) -> str | None:
        """Get current status of the stack."""
        client = await self._get_cfn_client()
        try:
            response = await client.describe_stacks(StackName=self.stack_name)
            stacks = response.get("Stacks", [])
            if not stacks:
                return None
            return cast(str, stacks[0]["StackStatus"])
        except ClientError as e:
            if e.response["Error"]["Code"] == "ValidationError":
                return None
            raise

    async def get_stack_outputs(self) -> dict[str, str]:
        """Get stack outputs."""
        client = await self._get_cfn_client()
        try:
            response = await client.describe_stacks(StackName=self.stack_name)
            stacks = response.get("Stacks", [])
            if not stacks:
                return {}

            outputs = {}
            for output in stacks[0].get("Outputs", []):
                outputs[output["OutputKey"]] = output["OutputValue"]
            return outputs
        except ClientError:
            return {}

    async def create_stack(
        self,
        auth_type: str = "IAM",
        lambda_timeout: int = 30,
        lambda_memory: int = 256,
        log_retention_days: int = 14,
        permission_boundary: str | None = None,
        role_name_format: str | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        """
        Create Admin API CloudFormation stack.

        Args:
            auth_type: API authorization type (IAM or NONE)
            lambda_timeout: Lambda function timeout in seconds
            lambda_memory: Lambda function memory in MB
            log_retention_days: CloudWatch log retention period
            permission_boundary: Optional IAM permission boundary ARN
            role_name_format: Optional role name format
            wait: Wait for stack to be CREATE_COMPLETE

        Returns:
            Dict with stack_id, stack_name, status, and api_endpoint
        """
        client = await self._get_cfn_client()

        # Check if stack already exists
        existing_status = await self.get_stack_status()
        if existing_status:
            if existing_status in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
                outputs = await self.get_stack_outputs()
                return {
                    "status": "already_exists",
                    "stack_name": self.stack_name,
                    "api_endpoint": outputs.get("ApiEndpoint"),
                }
            elif existing_status.endswith("_IN_PROGRESS"):
                if wait:
                    waiter = client.get_waiter("stack_create_complete")
                    await waiter.wait(StackName=self.stack_name)
                    outputs = await self.get_stack_outputs()
                    return {
                        "status": "CREATE_COMPLETE",
                        "stack_name": self.stack_name,
                        "api_endpoint": outputs.get("ApiEndpoint"),
                    }

        # Load template
        template = self._load_template()

        # Format parameters
        parameters = self._format_parameters(
            auth_type=auth_type,
            lambda_timeout=lambda_timeout,
            lambda_memory=lambda_memory,
            log_retention_days=log_retention_days,
            permission_boundary=permission_boundary,
            role_name_format=role_name_format,
        )

        # Create stack
        try:
            response = await client.create_stack(
                StackName=self.stack_name,
                TemplateBody=template,
                Parameters=parameters,
                Capabilities=["CAPABILITY_NAMED_IAM"],
                Tags=[
                    {"Key": "Application", "Value": "zae-limiter"},
                    {"Key": "Component", "Value": "admin-api"},
                ],
            )
            stack_id = response.get("StackId", "")
        except ClientError as e:
            if "AlreadyExistsException" in str(e):
                outputs = await self.get_stack_outputs()
                return {
                    "status": "already_exists",
                    "stack_name": self.stack_name,
                    "api_endpoint": outputs.get("ApiEndpoint"),
                }
            raise StackCreationError(
                stack_name=self.stack_name,
                reason=str(e),
            ) from e

        # Wait for stack creation if requested
        if wait:
            waiter = client.get_waiter("stack_create_complete")
            try:
                await waiter.wait(StackName=self.stack_name)
            except Exception as e:
                raise StackCreationError(
                    stack_name=self.stack_name,
                    reason=f"Stack creation failed: {e}",
                ) from e

        outputs = await self.get_stack_outputs()
        return {
            "status": "CREATE_COMPLETE",
            "stack_id": stack_id,
            "stack_name": self.stack_name,
            "api_endpoint": outputs.get("ApiEndpoint"),
        }

    async def deploy_lambda_code(self, wait: bool = True) -> dict[str, Any]:
        """
        Deploy Lambda function code.

        Args:
            wait: Wait for update to complete

        Returns:
            Dict with deployment status
        """
        # Check for local environment
        if self.endpoint_url and "localhost" in self.endpoint_url:
            # For LocalStack, we need to deploy differently
            return {"status": "skipped_local"}

        # Get Lambda function name from stack outputs
        outputs = await self.get_stack_outputs()
        function_name = outputs.get("LambdaFunctionName")

        if not function_name:
            # Try constructing it
            function_name = f"{self.stack_name}-admin-api"

        # Build Lambda package
        package_bytes = build_admin_lambda_package()

        # Update Lambda function code
        lambda_client = await self._get_lambda_client()

        response = await lambda_client.update_function_code(
            FunctionName=function_name,
            ZipFile=package_bytes,
        )

        return {
            "status": "deployed",
            "function_arn": response.get("FunctionArn"),
            "code_sha256": response.get("CodeSha256"),
            "size_bytes": len(package_bytes),
        }

    async def delete_stack(self, wait: bool = True) -> dict[str, Any]:
        """Delete the Admin API stack."""
        client = await self._get_cfn_client()

        try:
            await client.delete_stack(StackName=self.stack_name)

            if wait:
                waiter = client.get_waiter("stack_delete_complete")
                await waiter.wait(StackName=self.stack_name)

            return {"status": "DELETE_COMPLETE", "stack_name": self.stack_name}
        except ClientError as e:
            return {"status": "error", "error": str(e)}
