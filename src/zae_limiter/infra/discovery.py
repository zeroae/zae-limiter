"""Infrastructure discovery for zae-limiter deployments.

This module provides multi-stack discovery operations across a region,
separate from the single-stack lifecycle management in StackManager.
"""

from typing import Any

import aioboto3  # type: ignore
from botocore.exceptions import ClientError

from ..models import LimiterInfo
from ..naming import PREFIX
from .stack_manager import (
    LAMBDA_VERSION_TAG_KEY,
    SCHEMA_VERSION_TAG_KEY,
    VERSION_TAG_KEY,
)

# CloudFormation stack statuses to include (exclude DELETE_COMPLETE)
_ACTIVE_STACK_STATUSES = [
    "CREATE_IN_PROGRESS",
    "CREATE_COMPLETE",
    "CREATE_FAILED",
    "ROLLBACK_IN_PROGRESS",
    "ROLLBACK_COMPLETE",
    "ROLLBACK_FAILED",
    "DELETE_IN_PROGRESS",
    "DELETE_FAILED",
    "UPDATE_IN_PROGRESS",
    "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
    "UPDATE_COMPLETE",
    "UPDATE_FAILED",
    "UPDATE_ROLLBACK_IN_PROGRESS",
    "UPDATE_ROLLBACK_FAILED",
    "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS",
    "UPDATE_ROLLBACK_COMPLETE",
    "REVIEW_IN_PROGRESS",
    "IMPORT_IN_PROGRESS",
    "IMPORT_COMPLETE",
    "IMPORT_ROLLBACK_IN_PROGRESS",
    "IMPORT_ROLLBACK_FAILED",
    "IMPORT_ROLLBACK_COMPLETE",
]


class InfrastructureDiscovery:
    """
    Discovers deployed zae-limiter instances across a region.

    Provides read-only discovery of CloudFormation stacks with ZAEL- prefix.
    Does NOT manage stack lifecycle (use StackManager for that).

    Example:
        async with InfrastructureDiscovery(region="us-east-1") as discovery:
            limiters = await discovery.list_limiters()
            for limiter in limiters:
                print(f"{limiter.user_name}: {limiter.stack_status}")

    Attributes:
        region: AWS region to discover stacks in
        endpoint_url: Optional CloudFormation endpoint (for LocalStack)
    """

    def __init__(
        self,
        region: str | None = None,
        endpoint_url: str | None = None,
    ) -> None:
        """
        Initialize infrastructure discovery.

        Args:
            region: AWS region (default: use boto3 defaults)
            endpoint_url: CloudFormation endpoint (for LocalStack)
        """
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

        session = self._session
        self._client = await session.client("cloudformation", **kwargs).__aenter__()
        return self._client

    async def list_limiters(self) -> list[LimiterInfo]:
        """
        List all zae-limiter stacks in the region.

        Queries CloudFormation for stacks with ZAEL- prefix and extracts
        version information from tags.

        Returns:
            List of LimiterInfo objects, sorted by user name.
            Excludes DELETE_COMPLETE stacks.

        Raises:
            ClientError: If CloudFormation API call fails
        """
        client = await self._get_client()
        region_display = self.region or "default"

        limiters: list[LimiterInfo] = []
        next_token: str | None = None

        # Paginate through all stacks in the region
        while True:
            try:
                kwargs: dict[str, Any] = {
                    "StackStatusFilter": _ACTIVE_STACK_STATUSES,
                }
                if next_token:
                    kwargs["NextToken"] = next_token

                response = await client.list_stacks(**kwargs)

                # Filter for ZAEL- prefix
                for summary in response.get("StackSummaries", []):
                    stack_name = summary["StackName"]
                    if not stack_name.startswith(PREFIX):
                        continue

                    # Get version tags for this stack
                    version_info = await self._get_version_tags(client, stack_name)

                    # Format timestamps
                    creation_time = summary["CreationTime"].isoformat()
                    last_updated = summary.get("LastUpdatedTime")
                    last_updated_time = last_updated.isoformat() if last_updated else None

                    limiters.append(
                        LimiterInfo(
                            stack_name=stack_name,
                            user_name=stack_name[len(PREFIX) :],  # Strip prefix
                            region=region_display,
                            stack_status=summary["StackStatus"],
                            creation_time=creation_time,
                            last_updated_time=last_updated_time,
                            version=version_info.get("version"),
                            lambda_version=version_info.get("lambda_version"),
                            schema_version=version_info.get("schema_version"),
                        )
                    )

                next_token = response.get("NextToken")
                if not next_token:
                    break

            except ClientError:
                # Re-raise CloudFormation API errors
                raise

        # Sort by user name for consistent output
        limiters.sort(key=lambda x: x.user_name)
        return limiters

    async def _get_version_tags(self, client: Any, stack_name: str) -> dict[str, str | None]:
        """
        Extract version information from stack tags.

        Args:
            client: CloudFormation client
            stack_name: Stack name to query

        Returns:
            Dict with version, lambda_version, schema_version keys.
            Values are None if tags don't exist.
        """
        try:
            response = await client.describe_stacks(StackName=stack_name)
            stacks = response.get("Stacks", [])
            if not stacks:
                return {
                    "version": None,
                    "lambda_version": None,
                    "schema_version": None,
                }

            tags = {tag["Key"]: tag["Value"] for tag in stacks[0].get("Tags", [])}

            return {
                "version": tags.get(VERSION_TAG_KEY),
                "lambda_version": tags.get(LAMBDA_VERSION_TAG_KEY),
                "schema_version": tags.get(SCHEMA_VERSION_TAG_KEY),
            }
        except ClientError:
            # If describe_stacks fails (e.g., stack deleted between list and describe),
            # return None for all versions
            return {
                "version": None,
                "lambda_version": None,
                "schema_version": None,
            }

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

    async def __aenter__(self) -> "InfrastructureDiscovery":
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
