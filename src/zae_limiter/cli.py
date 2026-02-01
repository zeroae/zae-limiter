"""Command-line interface for zae-limiter infrastructure management."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

from .infra.lambda_builder import get_package_info, write_lambda_package
from .infra.stack_manager import StackManager
from .local import local
from .models import StackOptions

if TYPE_CHECKING:
    from .models import Limit


@click.group()
@click.version_option()
def cli() -> None:
    """zae-limiter infrastructure management CLI."""
    pass


@cli.command(
    epilog="""\b
Examples:
    \b
    # Basic deployment
    zae-limiter deploy --name my-app --region us-east-1
    \b
    # Production with deletion protection and tracing
    zae-limiter deploy --name prod --region us-east-1 \\
        --enable-deletion-protection --enable-tracing
    \b
    # Without Lambda aggregator (table only)
    zae-limiter deploy --name simple --no-aggregator
    \b
    # LocalStack development
    zae-limiter deploy --name dev \\
        --endpoint-url http://localhost:4566
    \b
    # Enterprise with permission boundary
    zae-limiter deploy --name prod \\
        --permission-boundary arn:aws:iam::aws:policy/PowerUserAccess \\
        --role-name-format "pb-{}-PowerUser"
"""
)
@click.option(
    "--name",
    "-n",
    default="limiter",
    help="Resource identifier used as the CloudFormation stack name. Default: limiter",
)
@click.option(
    "--region",
    help="AWS region (default: use boto3 defaults)",
)
@click.option(
    "--endpoint-url",
    help=(
        "AWS endpoint URL "
        "(e.g., http://localhost:4566 for LocalStack, or other AWS-compatible services)"
    ),
)
@click.option(
    "--snapshot-windows",
    default="hourly,daily",
    help="Comma-separated list of snapshot windows",
)
@click.option(
    "--retention-days",
    default=90,
    type=int,
    help="Number of days to retain usage snapshots",
)
@click.option(
    "--enable-aggregator/--no-aggregator",
    default=True,
    help="Deploy Lambda aggregator for usage snapshots",
)
@click.option(
    "--pitr-recovery-days",
    type=int,
    help="Point-in-Time Recovery period in days (1-35, default: AWS default of 35)",
)
@click.option(
    "--log-retention-days",
    default="30",
    type=click.Choice(
        [
            "1",
            "3",
            "5",
            "7",
            "14",
            "30",
            "60",
            "90",
            "120",
            "150",
            "180",
            "365",
            "400",
            "545",
            "731",
            "1096",
            "1827",
            "2192",
            "2557",
            "2922",
            "3288",
            "3653",
        ]
    ),
    help="Number of days to retain Lambda logs (CloudWatch standard retention periods)",
)
@click.option(
    "--lambda-timeout",
    type=click.IntRange(1, 900),
    default=60,
    help="Lambda timeout in seconds (1-900, default: 60)",
)
@click.option(
    "--lambda-memory",
    type=click.IntRange(128, 3008),
    default=256,
    help="Lambda memory size in MB (128-3008, default: 256)",
)
@click.option(
    "--enable-alarms/--no-alarms",
    default=True,
    help="Deploy CloudWatch alarms for monitoring (default: enabled)",
)
@click.option(
    "--alarm-sns-topic",
    type=str,
    default=None,
    help="SNS topic ARN for alarm notifications (optional)",
)
@click.option(
    "--lambda-duration-threshold-pct",
    type=click.IntRange(1, 100),
    default=80,
    help="Lambda duration alarm threshold as percentage of timeout (1-100, default: 80)",
)
@click.option(
    "--wait/--no-wait",
    default=True,
    help="Wait for stack creation to complete",
)
@click.option(
    "--permission-boundary",
    type=str,
    default=None,
    help="IAM permission boundary for Lambda role (policy name or full ARN)",
)
@click.option(
    "--role-name-format",
    type=str,
    default=None,
    help=(
        "Format template for Lambda role name. "
        "Use {} as placeholder for default name. "
        "Example: 'app-{}' produces 'app-mytable-aggregator-role'."
    ),
)
@click.option(
    "--enable-audit-archival/--no-audit-archival",
    default=True,
    help="Archive expired audit events to S3 (default: enabled)",
)
@click.option(
    "--audit-archive-glacier-days",
    type=click.IntRange(1, 3650),
    default=90,
    help="Days before transitioning audit archives to Glacier Instant Retrieval (default: 90)",
)
@click.option(
    "--enable-tracing/--no-tracing",
    default=False,
    help="Enable AWS X-Ray tracing for Lambda aggregator (default: disabled)",
)
@click.option(
    "--enable-iam-roles/--no-iam-roles",
    default=True,
    help="Create App/Admin/ReadOnly IAM roles for application access (default: enabled)",
)
@click.option(
    "--enable-deletion-protection/--no-deletion-protection",
    default=False,
    help="Enable DynamoDB table deletion protection (default: disabled)",
)
@click.option(
    "--tag",
    "-t",
    "tags",
    multiple=True,
    help="User-defined tag in KEY=VALUE format. Can be specified multiple times.",
)
def deploy(
    name: str,
    region: str | None,
    endpoint_url: str | None,
    snapshot_windows: str,
    retention_days: int,
    enable_aggregator: bool,
    pitr_recovery_days: int | None,
    log_retention_days: str,
    lambda_timeout: int,
    lambda_memory: int,
    enable_alarms: bool,
    alarm_sns_topic: str | None,
    lambda_duration_threshold_pct: int,
    wait: bool,
    permission_boundary: str | None,
    role_name_format: str | None,
    enable_audit_archival: bool,
    audit_archive_glacier_days: int,
    enable_tracing: bool,
    enable_iam_roles: bool,
    enable_deletion_protection: bool,
    tags: tuple[str, ...],
) -> None:
    """Deploy CloudFormation stack with DynamoDB table and Lambda aggregator.

    Creates or updates infrastructure including DynamoDB table, Lambda aggregator
    for usage snapshots, CloudWatch alarms, and IAM roles. The stack is idempotent -
    running deploy again updates existing resources.

    \f

    **Examples:**
        ```bash
        # Basic deployment
        zae-limiter deploy --name my-app --region us-east-1

        # Production with deletion protection and tracing
        zae-limiter deploy --name prod --region us-east-1 \\
            --enable-deletion-protection --enable-tracing

        # Without Lambda aggregator (table only)
        zae-limiter deploy --name simple --no-aggregator

        # LocalStack development
        zae-limiter deploy --name dev \\
            --endpoint-url http://localhost:4566

        # Enterprise with permission boundary
        zae-limiter deploy --name prod \\
            --permission-boundary arn:aws:iam::aws:policy/PowerUserAccess \\
            --role-name-format "pb-{}-PowerUser"
        ```
    """
    from .exceptions import ValidationError

    try:
        manager = StackManager(name, region, endpoint_url)
    except ValidationError as e:
        click.echo(f"Error: {e.reason}", err=True)
        sys.exit(1)

    async def _deploy() -> None:
        async with manager:
            # Parse user-defined tags
            user_tags: dict[str, str] | None = None
            if tags:
                user_tags = {}
                for tag_str in tags:
                    if "=" not in tag_str:
                        click.echo(
                            f"Error: Invalid tag format '{tag_str}'. Use KEY=VALUE.",
                            err=True,
                        )
                        sys.exit(1)
                    key, value = tag_str.split("=", 1)
                    user_tags[key] = value

            # Build StackOptions from CLI arguments
            stack_options = StackOptions(
                snapshot_windows=snapshot_windows,
                retention_days=retention_days,
                enable_aggregator=enable_aggregator,
                pitr_recovery_days=pitr_recovery_days,
                log_retention_days=int(log_retention_days),
                lambda_timeout=lambda_timeout,
                lambda_memory=lambda_memory,
                enable_alarms=enable_alarms,
                alarm_sns_topic=alarm_sns_topic,
                lambda_duration_threshold_pct=lambda_duration_threshold_pct,
                permission_boundary=permission_boundary,
                role_name_format=role_name_format,
                enable_audit_archival=enable_audit_archival,
                audit_archive_glacier_days=audit_archive_glacier_days,
                enable_tracing=enable_tracing,
                create_iam_roles=enable_iam_roles,
                enable_deletion_protection=enable_deletion_protection,
                tags=user_tags,
            )

            click.echo(f"Deploying stack: {manager.stack_name}")
            click.echo(f"  Table name: {manager.table_name}")
            click.echo(f"  Region: {region or 'default'}")
            click.echo(f"  Snapshot windows: {stack_options.snapshot_windows}")
            click.echo(f"  Retention days: {stack_options.retention_days}")
            click.echo(
                f"  Aggregator: {'enabled' if stack_options.enable_aggregator else 'disabled'}"
            )
            if stack_options.enable_aggregator:
                click.echo(f"  Lambda timeout: {stack_options.lambda_timeout}s")
                click.echo(f"  Lambda memory: {stack_options.lambda_memory}MB")
                click.echo(
                    f"  X-Ray tracing: {'enabled' if stack_options.enable_tracing else 'disabled'}"
                )
            click.echo(f"  Alarms: {'enabled' if stack_options.enable_alarms else 'disabled'}")
            if stack_options.enable_alarms and stack_options.alarm_sns_topic:
                click.echo(f"  Alarm SNS topic: {stack_options.alarm_sns_topic}")
            click.echo(
                f"  IAM roles: {'enabled' if stack_options.create_iam_roles else 'disabled'}"
            )
            deletion_protection_status = (
                "enabled" if stack_options.enable_deletion_protection else "disabled"
            )
            click.echo(f"  Deletion protection: {deletion_protection_status}")
            if stack_options.enable_aggregator:
                archival_status = "enabled" if stack_options.enable_audit_archival else "disabled"
                click.echo(f"  Audit archival: {archival_status}")
                if stack_options.enable_audit_archival:
                    click.echo(
                        f"  Glacier transition: {stack_options.audit_archive_glacier_days} days"
                    )
            if stack_options.tags:
                click.echo(f"  Tags: {len(stack_options.tags)} user-defined")
                for k, v in stack_options.tags.items():
                    click.echo(f"    {k}={v}")
            click.echo()

            try:
                # Step 1: Create CloudFormation stack
                result = await manager.create_stack(
                    stack_options=stack_options,
                    wait=wait,
                )

                status = result.get("status", "unknown")
                click.echo(f"✓ Stack {status.lower().replace('_', ' ')}")

                if result.get("stack_id"):
                    click.echo(f"  Stack ID: {result['stack_id']}")

                # Step 2: Deploy Lambda code if aggregator is enabled
                if stack_options.enable_aggregator and wait:
                    click.echo()
                    click.echo("Deploying Lambda function code...")

                    try:
                        lambda_result = await manager.deploy_lambda_code(wait=True)

                        if lambda_result.get("status") == "deployed":
                            size_kb = lambda_result.get("size_bytes", 0) / 1024
                            click.echo(f"✓ Lambda code deployed ({size_kb:.1f} KB)")
                            click.echo(f"  Function ARN: {lambda_result['function_arn']}")
                            click.echo(f"  Code SHA256: {lambda_result['code_sha256'][:16]}...")
                            if lambda_result.get("esm_ready"):
                                click.echo("✓ Event Source Mapping ready")
                    except Exception as e:
                        click.echo(f"⚠️  Lambda deployment failed: {e}", err=True)
                        click.echo(
                            "  Stack was created successfully, but Lambda code "
                            "needs manual deployment.",
                            err=True,
                        )
                        sys.exit(1)

                # Step 3: Initialize version record in DynamoDB
                if wait:
                    from . import __version__
                    from .repository import Repository
                    from .version import get_schema_version

                    click.echo()
                    click.echo("Initializing version record...")

                    repo = Repository(manager.table_name, region, endpoint_url)
                    await repo.set_version_record(
                        schema_version=get_schema_version(),
                        lambda_version=__version__,
                        client_min_version="0.0.0",
                        updated_by=f"cli:{__version__}",
                    )
                    click.echo(f"✓ Version record initialized (schema {get_schema_version()})")

                if not wait:
                    click.echo()
                    click.echo("Stack creation initiated. Use 'status' command to check progress.")
                    if stack_options.enable_aggregator:
                        click.echo(
                            "Note: Lambda code will not be deployed until stack is ready. "
                            "Run 'zae-limiter deploy' again with --wait to deploy Lambda."
                        )

            except Exception as e:
                click.echo(f"✗ Deployment failed: {e}", err=True)
                sys.exit(1)

    asyncio.run(_deploy())


@cli.command(
    epilog="""\b
Examples:
    \b
    # Delete with confirmation prompt
    zae-limiter delete --name my-app --region us-east-1
    \b
    # Skip confirmation (for scripts)
    zae-limiter delete --name my-app --yes
    \b
    # Delete without waiting
    zae-limiter delete --name my-app --no-wait
"""
)
@click.option(
    "--name",
    "-n",
    required=True,
    help="Resource identifier used as the CloudFormation stack name",
)
@click.option(
    "--region",
    help="AWS region (default: use boto3 defaults)",
)
@click.option(
    "--endpoint-url",
    help=(
        "AWS endpoint URL "
        "(e.g., http://localhost:4566 for LocalStack, or other AWS-compatible services)"
    ),
)
@click.option(
    "--wait/--no-wait",
    default=True,
    help="Wait for stack deletion to complete",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip confirmation prompt",
)
def delete(
    name: str,
    region: str | None,
    endpoint_url: str | None,
    wait: bool,
    yes: bool,
) -> None:
    """Delete CloudFormation stack and all resources.

    Removes the DynamoDB table, Lambda function, IAM roles, and all associated
    resources. This action cannot be undone - all data will be permanently lost.

    \f

    **Examples:**
        ```bash
        # Delete with confirmation prompt
        zae-limiter delete --name my-app --region us-east-1

        # Skip confirmation (for scripts)
        zae-limiter delete --name my-app --yes

        # Delete without waiting
        zae-limiter delete --name my-app --no-wait
        ```

    !!! warning "Data Loss"
        Deleting a stack removes the DynamoDB table and all its data.
        This action cannot be undone.
    """
    from .exceptions import ValidationError
    from .naming import normalize_name

    try:
        normalized_name = normalize_name(name)
    except ValidationError as e:
        click.echo(f"Error: {e.reason}", err=True)
        sys.exit(1)

    if not yes:
        click.confirm(
            f"Are you sure you want to delete stack '{normalized_name}'?",
            abort=True,
        )

    async def _delete() -> None:
        async with StackManager(name, region, endpoint_url) as manager:
            click.echo(f"Deleting stack: {manager.stack_name}")

            try:
                await manager.delete_stack(stack_name=manager.stack_name, wait=wait)

                if wait:
                    click.echo(f"✓ Stack '{manager.stack_name}' deleted successfully")
                else:
                    click.echo("Stack deletion initiated. Use 'status' command to check progress.")

            except Exception as e:
                click.echo(f"✗ Deletion failed: {e}", err=True)
                sys.exit(1)

    asyncio.run(_delete())


@cli.command(
    epilog="""\b
Examples:
    \b
    # Export to file
    zae-limiter cfn-template --output template.yaml
    \b
    # Pipe to stdout
    zae-limiter cfn-template > template.yaml
    \b
    # View in pager
    zae-limiter cfn-template | less
"""
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Output file (default: stdout)",
)
def cfn_template(output: str | None) -> None:
    """Export CloudFormation template for custom deployment.

    Outputs the raw CloudFormation YAML template for manual deployment,
    integration with CDK/Terraform, or customization.

    \f

    **Examples:**
        ```bash
        # Export to file
        zae-limiter cfn-template --output template.yaml

        # Pipe to stdout
        zae-limiter cfn-template > template.yaml

        # View in pager
        zae-limiter cfn-template | less
        ```
    """
    try:
        template_path = Path(__file__).parent / "infra" / "cfn_template.yaml"

        if not template_path.exists():
            click.echo(f"✗ Template not found: {template_path}", err=True)
            sys.exit(1)

        content = template_path.read_text()

        if output:
            output_path = Path(output)
            output_path.write_text(content)
            click.echo(f"✓ Template exported to: {output}")
        else:
            click.echo(content)

    except Exception as e:
        click.echo(f"✗ Failed to export template: {e}", err=True)
        sys.exit(1)


@cli.command(
    "lambda-export",
    epilog="""\b
Examples:
    \b
    # Export Lambda package
    zae-limiter lambda-export --output lambda.zip
    \b
    # Show package info without building
    zae-limiter lambda-export --info
    \b
    # Overwrite existing file
    zae-limiter lambda-export --force
""",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    default="lambda.zip",
    help="Output file path (default: lambda.zip)",
)
@click.option(
    "--info",
    is_flag=True,
    help="Show package information without building",
)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="Overwrite existing file without prompting",
)
def lambda_export(output: str, info: bool, force: bool) -> None:
    """Export Lambda deployment package for custom deployment.

    Creates a ZIP file containing the Lambda aggregator code for manual
    deployment or inspection. Useful for custom deployment pipelines.

    \f

    **Examples:**
        ```bash
        # Export Lambda package
        zae-limiter lambda-export --output lambda.zip

        # Show package info without building
        zae-limiter lambda-export --info

        # Overwrite existing file
        zae-limiter lambda-export --force
        ```

    **Sample Output (--info):**
        ```
        Lambda Package Information
        ==========================

        Package path:      /path/to/zae_limiter_aggregator
        Python files:      4
        Uncompressed size: 24.5 KB
        Handler:           zae_limiter_aggregator.handler.handler
        Dependencies:      1
          - aws-lambda-powertools
        ```
    """
    try:
        if info:
            # Show package info without building
            pkg_info = get_package_info()
            click.echo()
            click.echo("Lambda Package Information")
            click.echo("=" * 26)
            click.echo()
            click.echo(f"Package path:      {pkg_info['package_path']}")
            click.echo(f"Python files:      {pkg_info['python_files']}")
            size_bytes = pkg_info["uncompressed_size"]
            assert isinstance(size_bytes, int)
            click.echo(f"Uncompressed size: {size_bytes / 1024:.1f} KB")
            click.echo(f"Handler:           {pkg_info['handler']}")
            deps = pkg_info.get("runtime_dependencies", [])
            assert isinstance(deps, list)
            if deps:
                click.echo(f"Dependencies:      {len(deps)}")
                for dep in deps:
                    click.echo(f"  - {dep}")
            click.echo()
            return

        output_path = Path(output)

        # Check if file exists
        if output_path.exists() and not force:
            click.echo(f"File already exists: {output_path}", err=True)
            click.echo("Use --force to overwrite.", err=True)
            sys.exit(1)

        # Build and write the package
        size_bytes = write_lambda_package(output_path)
        size_kb = size_bytes / 1024

        click.echo(f"✓ Exported Lambda package to: {output_path} ({size_kb:.1f} KB)")

    except Exception as e:
        click.echo(f"✗ Failed to export Lambda package: {e}", err=True)
        sys.exit(1)


def _format_size(size_bytes: int | None) -> str:
    """Format size in bytes to human-readable format."""
    if size_bytes is None:
        return "N/A"
    if size_bytes < 1024:
        return f"~{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"~{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"~{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"~{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def _format_count(count: int | None) -> str:
    """Format item count to human-readable format."""
    if count is None:
        return "N/A"
    if count < 1000:
        return f"~{count:,}"
    elif count < 1000000:
        return f"~{count / 1000:.1f}K"
    else:
        return f"~{count / 1000000:.1f}M"


@cli.command(
    epilog="""\b
Examples:
    \b
    zae-limiter status --name my-app --region us-east-1
    \b
    zae-limiter status --name dev --endpoint-url http://localhost:4566
"""
)
@click.option(
    "--name",
    "-n",
    required=True,
    help="Resource identifier used as the CloudFormation stack name",
)
@click.option(
    "--region",
    help="AWS region (default: use boto3 defaults)",
)
@click.option(
    "--endpoint-url",
    help=(
        "AWS endpoint URL "
        "(e.g., http://localhost:4566 for LocalStack, or other AWS-compatible services)"
    ),
)
def status(name: str, region: str | None, endpoint_url: str | None) -> None:
    """Get comprehensive status of rate limiter infrastructure.

    Shows connectivity, stack status, version compatibility, table metrics,
    and IAM role ARNs. Read-only operation - does not modify any resources.

    \f

    **Examples:**
        ```bash
        zae-limiter status --name my-app --region us-east-1
        zae-limiter status --name dev --endpoint-url http://localhost:4566
        ```

    **Sample Output:**
        ```
        Status: my-app
        ==================================================

        Connectivity
          Available:     ✓ Yes
          Latency:       42ms
          Region:        us-east-1

        Infrastructure
          Stack:         CREATE_COMPLETE
          Table:         ACTIVE
          Aggregator:    Enabled

        Versions
          Client:        0.6.0
          Schema:        0.6.0
          Lambda:        0.6.0

        Table Metrics
          Items:         1,234
          Size:          256 KB

        ✓ Infrastructure is ready
        ```
    """
    import time

    from . import __version__
    from .exceptions import ValidationError
    from .naming import normalize_name
    from .repository import Repository

    try:
        stack_name = normalize_name(name)
    except ValidationError as e:
        click.echo(f"Error: {e.reason}", err=True)
        sys.exit(1)

    async def _status() -> None:
        # Initialize status values
        available = False
        latency_ms: float | None = None
        cfn_status: str | None = None
        table_status: str | None = None
        aggregator_enabled = False
        schema_version: str | None = None
        lambda_version: str | None = None
        table_item_count: int | None = None
        table_size_bytes: int | None = None
        role_arns: dict[str, str] = {}

        # Get CloudFormation stack status and outputs (read-only)
        try:
            async with StackManager(stack_name, region, endpoint_url) as manager:
                cfn_status = await manager.get_stack_status(stack_name)
                # Get stack outputs for role ARNs if stack exists and is complete
                if cfn_status and "COMPLETE" in cfn_status:
                    try:
                        client = await manager._get_client()
                        response = await client.describe_stacks(StackName=stack_name)
                        if response.get("Stacks"):
                            outputs = response["Stacks"][0].get("Outputs", [])
                            for output in outputs:
                                key = output.get("OutputKey", "")
                                value = output.get("OutputValue", "")
                                if key in ("AppRoleArn", "AdminRoleArn", "ReadOnlyRoleArn"):
                                    role_arns[key] = value
                    except Exception:
                        pass  # Stack outputs unavailable
        except Exception:
            pass  # Stack status unavailable

        # Create repository for read-only DynamoDB access
        repository = Repository(stack_name, region, endpoint_url)

        try:
            # Ping DynamoDB and measure latency
            try:
                start_time = time.time()
                client = await repository._get_client()

                # Use DescribeTable to check connectivity and get table info
                response = await client.describe_table(TableName=stack_name)
                latency_ms = (time.time() - start_time) * 1000
                available = True

                # Extract table information
                table = response.get("Table", {})
                table_status = table.get("TableStatus")
                table_item_count = table.get("ItemCount")
                table_size_bytes = table.get("TableSizeInBytes")

                # Check if aggregator is enabled by looking for stream specification
                stream_spec = table.get("StreamSpecification", {})
                aggregator_enabled = stream_spec.get("StreamEnabled", False)

            except Exception:
                pass  # DynamoDB unavailable

            # Get version information from DynamoDB
            if available:
                try:
                    version_record = await repository.get_version_record()
                    if version_record:
                        schema_version = version_record.get("schema_version")
                        lambda_version = version_record.get("lambda_version")
                except Exception:
                    pass  # Version record unavailable

            # Header
            click.echo()
            click.echo(f"Status: {stack_name}")
            click.echo("=" * 50)
            click.echo()

            # Connectivity section
            click.echo("Connectivity")
            available_str = "✓ Yes" if available else "✗ No"
            click.echo(f"  Available:     {available_str}")
            if latency_ms is not None:
                click.echo(f"  Latency:       {latency_ms:.0f}ms")
            else:
                click.echo("  Latency:       N/A")
            click.echo(f"  Region:        {region or 'default'}")
            click.echo()

            # Infrastructure section
            click.echo("Infrastructure")
            click.echo(f"  Stack:         {cfn_status or 'Not found'}")
            click.echo(f"  Table:         {table_status or 'Not found'}")
            aggregator_str = "Enabled" if aggregator_enabled else "Disabled"
            click.echo(f"  Aggregator:    {aggregator_str}")
            click.echo()

            # Versions section
            click.echo("Versions")
            click.echo(f"  Client:        {__version__}")
            click.echo(f"  Schema:        {schema_version or 'N/A'}")
            click.echo(f"  Lambda:        {lambda_version or 'N/A'}")
            click.echo()

            # Table Metrics section
            click.echo("Table Metrics")
            click.echo(f"  Items:         {_format_count(table_item_count)}")
            click.echo(f"  Size:          {_format_size(table_size_bytes)}")
            click.echo()

            # IAM Roles section (only if roles exist)
            if role_arns:
                click.echo("IAM Roles")
                if "AppRoleArn" in role_arns:
                    click.echo(f"  App:           {role_arns['AppRoleArn']}")
                if "AdminRoleArn" in role_arns:
                    click.echo(f"  Admin:         {role_arns['AdminRoleArn']}")
                if "ReadOnlyRoleArn" in role_arns:
                    click.echo(f"  ReadOnly:      {role_arns['ReadOnlyRoleArn']}")
                click.echo()

            # Exit with appropriate status code
            if not available:
                click.echo("✗ Infrastructure is not available", err=True)
                sys.exit(1)
            elif cfn_status and ("FAILED" in cfn_status or "ROLLBACK" in cfn_status):
                click.echo("✗ Stack is in failed state", err=True)
                sys.exit(1)
            elif cfn_status and "IN_PROGRESS" in cfn_status:
                click.echo("⏳ Operation in progress...")
            elif cfn_status == "CREATE_COMPLETE":
                click.echo("✓ Infrastructure is ready")

        except Exception as e:
            click.echo(f"✗ Failed to get status: {e}", err=True)
            sys.exit(1)
        finally:
            await repository.close()

    asyncio.run(_status())


@cli.command(
    "list",
    epilog="""\b
Examples:
    \b
    zae-limiter list --region us-east-1
    \b
    zae-limiter list --endpoint-url http://localhost:4566
""",
)
@click.option(
    "--region",
    help="AWS region (default: use boto3 defaults)",
)
@click.option(
    "--endpoint-url",
    help=(
        "AWS endpoint URL "
        "(e.g., http://localhost:4566 for LocalStack, or other AWS-compatible services)"
    ),
)
def list_limiters(region: str | None, endpoint_url: str | None) -> None:
    """List all deployed rate limiter instances in the region.

    Discovers stacks by CloudFormation tags. Shows name, status, version, and
    creation date for each instance.

    \f

    **Examples:**
        ```bash
        zae-limiter list --region us-east-1
        zae-limiter list --endpoint-url http://localhost:4566
        ```

    **Sample Output:**
        ```
        Rate Limiter Instances (us-east-1)

        Name        Status             Version   Created
        ──────────  ─────────────────  ────────  ──────────
        my-app      CREATE_COMPLETE    0.6.0     2026-01-15
        prod-api    UPDATE_COMPLETE    0.6.0     2026-01-10
        ```
    """
    from datetime import datetime

    from .infra.discovery import InfrastructureDiscovery

    async def _list() -> None:
        try:
            async with InfrastructureDiscovery(
                region=region, endpoint_url=endpoint_url
            ) as discovery:
                limiters = await discovery.list_limiters()

            if not limiters:
                click.echo()
                click.echo("No rate limiter instances found in region.")
                click.echo(f"  Region: {region or 'default'}")
                click.echo()
                click.echo("Deploy a new instance with:")
                click.echo("  zae-limiter deploy --name my-app")
                return

            # Build table data
            click.echo()
            region_display = region or "default"
            click.echo(f"Rate Limiter Instances ({region_display})")
            click.echo()

            headers = ["Name", "Status", "Version", "Created"]
            rows: list[list[str]] = []
            for limiter in limiters:
                # Parse and format creation time (ISO 8601 -> readable)
                try:
                    created = datetime.fromisoformat(limiter.creation_time)
                    created_display = created.strftime("%Y-%m-%d")
                except Exception:
                    created_display = "unknown"

                rows.append(
                    [
                        limiter.user_name,
                        limiter.stack_status,
                        limiter.version or "-",
                        created_display,
                    ]
                )

            from .visualization import TableRenderer

            renderer = TableRenderer()
            click.echo(renderer.render(headers, rows))

            # Summary
            click.echo()
            click.echo(f"Total: {len(limiters)} instance(s)")

            # Show problem summary if any
            failed = [lim for lim in limiters if lim.is_failed]
            in_progress = [lim for lim in limiters if lim.is_in_progress]
            if failed:
                click.echo(f"  {len(failed)} failed")
            if in_progress:
                click.echo(f"  {len(in_progress)} in progress")
            click.echo()

        except Exception as e:
            click.echo(f"✗ Failed to list limiters: {e}", err=True)
            sys.exit(1)

    asyncio.run(_list())


@cli.command(
    "version",
    epilog="""\b
Examples:
    \b
    zae-limiter version --name my-app --region us-east-1
    \b
    zae-limiter version --endpoint-url http://localhost:4566
""",
)
@click.option(
    "--name",
    "-n",
    default="limiter",
    help="Resource identifier used as the CloudFormation stack name. Default: limiter",
)
@click.option(
    "--region",
    help="AWS region (default: use boto3 defaults)",
)
@click.option(
    "--endpoint-url",
    help=(
        "AWS endpoint URL "
        "(e.g., http://localhost:4566 for LocalStack, or other AWS-compatible services)"
    ),
)
def version_cmd(
    name: str,
    region: str | None,
    endpoint_url: str | None,
) -> None:
    """Show infrastructure version information.

    Displays client version, schema version, and deployed infrastructure
    versions. Checks compatibility between client and infrastructure.

    \f

    **Examples:**
        ```bash
        zae-limiter version --name my-app --region us-east-1
        zae-limiter version --endpoint-url http://localhost:4566
        ```

    **Sample Output:**
        ```
        zae-limiter Infrastructure Version
        ====================================

        Client Version:     0.6.0
        Schema Version:     0.6.0

        Infra Schema:       0.6.0
        Lambda Version:     0.6.0
        Min Client Version: 0.5.0

        Status: COMPATIBLE
        ```
    """
    from . import __version__
    from .exceptions import ValidationError
    from .version import (
        InfrastructureVersion,
        check_compatibility,
        get_schema_version,
    )

    async def _version() -> None:
        # Import here to avoid loading aioboto3 at CLI startup
        from .repository import Repository

        try:
            repo = Repository(name, region, endpoint_url)
        except ValidationError as e:
            click.echo(f"Error: {e.reason}", err=True)
            sys.exit(1)

        try:
            click.echo()
            click.echo("zae-limiter Infrastructure Version")
            click.echo("=" * 36)
            click.echo()
            click.echo(f"Client Version:     {__version__}")
            click.echo(f"Schema Version:     {get_schema_version()}")
            click.echo()

            # Get version from DynamoDB
            version_record = await repo.get_version_record()

            if version_record is None:
                click.echo("Infrastructure:     Not initialized")
                click.echo()
                click.echo("Run 'zae-limiter deploy' to initialize infrastructure.")
                return

            infra_version = InfrastructureVersion.from_record(version_record)

            click.echo(f"Infra Schema:       {infra_version.schema_version}")
            click.echo(f"Lambda Version:     {infra_version.lambda_version or 'unknown'}")
            click.echo(f"Min Client Version: {infra_version.client_min_version}")
            click.echo()

            # Check compatibility
            compat = check_compatibility(__version__, infra_version)

            if compat.is_compatible and not compat.requires_lambda_update:
                click.echo("Status: COMPATIBLE")
            elif compat.requires_lambda_update:
                click.echo("Status: COMPATIBLE (Lambda update available)")
                click.echo()
                click.echo(f"  {compat.message}")
                click.echo()
                click.echo("Run 'zae-limiter upgrade' to update Lambda.")
            elif compat.requires_schema_migration:
                click.echo("Status: INCOMPATIBLE (Schema migration required)", err=True)
                click.echo()
                click.echo(f"  {compat.message}")
                sys.exit(1)
            else:
                click.echo("Status: INCOMPATIBLE", err=True)
                click.echo()
                click.echo(f"  {compat.message}")
                sys.exit(1)

        except Exception as e:
            click.echo(f"✗ Failed to get version info: {e}", err=True)
            sys.exit(1)
        finally:
            await repo.close()

    asyncio.run(_version())


@cli.command(
    epilog="""\b
Examples:
    \b
    # Standard upgrade
    zae-limiter upgrade --name my-app --region us-east-1
    \b
    # Force Lambda update
    zae-limiter upgrade --name my-app --force
"""
)
@click.option(
    "--name",
    "-n",
    default="limiter",
    help="Resource identifier used as the CloudFormation stack name. Default: limiter",
)
@click.option(
    "--region",
    help="AWS region (default: use boto3 defaults)",
)
@click.option(
    "--endpoint-url",
    help=(
        "AWS endpoint URL "
        "(e.g., http://localhost:4566 for LocalStack, or other AWS-compatible services)"
    ),
)
@click.option(
    "--lambda-only",
    is_flag=True,
    help="Only update Lambda code",
)
@click.option(
    "--force",
    is_flag=True,
    help="Force update even if version matches",
)
def upgrade(
    name: str,
    region: str | None,
    endpoint_url: str | None,
    lambda_only: bool,
    force: bool,
) -> None:
    """Upgrade infrastructure to match client version.

    Updates Lambda code and version records to match the current client.
    Use --force to update even when versions already match.

    \f

    **Examples:**
        ```bash
        # Standard upgrade
        zae-limiter upgrade --name my-app --region us-east-1

        # Force Lambda update
        zae-limiter upgrade --name my-app --force
        ```
    """
    from . import __version__
    from .version import (
        InfrastructureVersion,
        check_compatibility,
        get_schema_version,
    )

    async def _upgrade() -> None:
        from .exceptions import ValidationError
        from .repository import Repository

        try:
            repo = Repository(name, region, endpoint_url)
        except ValidationError as e:
            click.echo(f"Error: {e.reason}", err=True)
            sys.exit(1)

        try:
            click.echo()
            click.echo("Checking infrastructure version...")

            version_record = await repo.get_version_record()

            if version_record is None:
                click.echo("Infrastructure not initialized.")
                click.echo("Run 'zae-limiter deploy' first.")
                sys.exit(1)

            infra_version = InfrastructureVersion.from_record(version_record)
            compat = check_compatibility(__version__, infra_version)

            if not force and compat.is_compatible and not compat.requires_lambda_update:
                click.echo()
                click.echo("Infrastructure is already up to date.")
                click.echo(f"  Client:   {__version__}")
                click.echo(f"  Lambda:   {infra_version.lambda_version}")
                return

            if compat.requires_schema_migration:
                click.echo()
                click.echo("✗ Schema migration required - cannot auto-upgrade", err=True)
                click.echo(f"  {compat.message}")
                sys.exit(1)

            # Perform upgrade
            click.echo()
            click.echo(f"Current: Lambda {infra_version.lambda_version or 'unknown'}")
            click.echo(f"Target:  Lambda {__version__}")
            click.echo()

            async with StackManager(name, region, endpoint_url) as manager:
                # Step 1: Update Lambda code
                click.echo("[1/3] Deploying Lambda code...")
                try:
                    result = await manager.deploy_lambda_code(wait=True)

                    if result.get("status") == "deployed":
                        size_kb = result.get("size_bytes", 0) / 1024
                        click.echo(f"      Lambda code deployed ({size_kb:.1f} KB)")

                except Exception as e:
                    click.echo(f"✗ Lambda deployment failed: {e}", err=True)
                    sys.exit(1)

                # Step 2: Ensure discovery tags
                click.echo("[2/3] Ensuring discovery tags...")
                try:
                    tags_added = await manager.ensure_tags()
                    if tags_added:
                        click.echo("      Discovery tags added")
                    else:
                        click.echo("      Tags already present")
                except Exception as e:
                    click.echo(f"⚠️  Tag update failed: {e}", err=True)
                    # Non-fatal — continue with upgrade

                # Step 3: Update version record
                click.echo("[3/3] Updating version record...")
                await repo.set_version_record(
                    schema_version=get_schema_version(),
                    lambda_version=__version__,
                    client_min_version="0.0.0",
                    updated_by=f"cli:{__version__}",
                )
                click.echo("      Version record updated")

            click.echo()
            click.echo("✓ Upgrade complete!")

        except Exception as e:
            click.echo(f"✗ Upgrade failed: {e}", err=True)
            sys.exit(1)
        finally:
            await repo.close()

    asyncio.run(_upgrade())


@cli.command(
    epilog="""\b
Examples:
    \b
    zae-limiter check --name my-app --region us-east-1
    \b
    zae-limiter check --endpoint-url http://localhost:4566
"""
)
@click.option(
    "--name",
    "-n",
    default="limiter",
    help="Resource identifier used as the CloudFormation stack name. Default: limiter",
)
@click.option(
    "--region",
    help="AWS region (default: use boto3 defaults)",
)
@click.option(
    "--endpoint-url",
    help=(
        "AWS endpoint URL "
        "(e.g., http://localhost:4566 for LocalStack, or other AWS-compatible services)"
    ),
)
def check(
    name: str,
    region: str | None,
    endpoint_url: str | None,
) -> None:
    """Check infrastructure compatibility without modifying.

    Verifies that the client version is compatible with the deployed
    infrastructure. Read-only operation - does not change anything.

    \f

    **Examples:**
        ```bash
        zae-limiter check --name my-app --region us-east-1
        zae-limiter check --endpoint-url http://localhost:4566
        ```

    **Sample Output:**
        ```
        Compatibility Check
        ====================

        Client:      0.6.0
        Schema:      0.6.0
        Lambda:      0.6.0

        Result: COMPATIBLE

        Client and infrastructure are fully compatible.
        ```
    """
    from . import __version__
    from .exceptions import ValidationError
    from .version import (
        InfrastructureVersion,
        check_compatibility,
    )

    async def _check() -> None:
        from .repository import Repository

        try:
            repo = Repository(name, region, endpoint_url)
        except ValidationError as e:
            click.echo(f"Error: {e.reason}", err=True)
            sys.exit(1)

        try:
            click.echo()
            click.echo("Compatibility Check")
            click.echo("=" * 20)
            click.echo()

            version_record = await repo.get_version_record()

            if version_record is None:
                click.echo("Result: NOT INITIALIZED")
                click.echo()
                click.echo("Infrastructure has not been deployed yet.")
                click.echo("Run 'zae-limiter deploy' to initialize.")
                sys.exit(1)

            infra_version = InfrastructureVersion.from_record(version_record)
            compat = check_compatibility(__version__, infra_version)

            click.echo(f"Client:      {__version__}")
            click.echo(f"Schema:      {infra_version.schema_version}")
            click.echo(f"Lambda:      {infra_version.lambda_version or 'unknown'}")
            click.echo()

            if compat.is_compatible and not compat.requires_lambda_update:
                click.echo("Result: COMPATIBLE")
                click.echo()
                click.echo("Client and infrastructure are fully compatible.")
            elif compat.requires_lambda_update:
                click.echo("Result: COMPATIBLE (update available)")
                click.echo()
                click.echo(compat.message)
                click.echo()
                click.echo("Run 'zae-limiter upgrade' to update.")
            elif compat.requires_schema_migration:
                click.echo("Result: INCOMPATIBLE", err=True)
                click.echo()
                click.echo(compat.message)
                sys.exit(1)
            else:
                click.echo("Result: INCOMPATIBLE", err=True)
                click.echo()
                click.echo(compat.message)
                sys.exit(1)

        except Exception as e:
            click.echo(f"✗ Check failed: {e}", err=True)
            sys.exit(1)
        finally:
            await repo.close()

    asyncio.run(_check())


# -------------------------------------------------------------------------
# Audit commands
# -------------------------------------------------------------------------


@cli.group()
def audit() -> None:
    """Audit log commands.

    Query audit events for entities. Events track configuration changes
    like limits_set, entity_created, and entity_deleted.
    """
    pass


@audit.command(
    "list",
    epilog="""\b
Examples:
    \b
    zae-limiter audit list --entity-id user-123
    \b
    zae-limiter audit list --entity-id user-123 --limit 10
""",
)
@click.option(
    "--name",
    "-n",
    default="limiter",
    help="Resource identifier used as the CloudFormation stack name. Default: limiter",
)
@click.option(
    "--region",
    help="AWS region (default: use boto3 defaults)",
)
@click.option(
    "--endpoint-url",
    help=(
        "AWS endpoint URL "
        "(e.g., http://localhost:4566 for LocalStack, or other AWS-compatible services)"
    ),
)
@click.option(
    "--entity-id",
    "-e",
    required=True,
    help="Entity ID to query audit events for",
)
@click.option(
    "--limit",
    "-l",
    default=100,
    type=int,
    help="Maximum number of events to return (default: 100)",
)
@click.option(
    "--start-event-id",
    help="Event ID to start after (for pagination)",
)
def audit_list(
    name: str,
    region: str | None,
    endpoint_url: str | None,
    entity_id: str,
    limit: int,
    start_event_id: str | None,
) -> None:
    """List audit events for an entity.

    Shows configuration changes like limits_set, entity_created, entity_deleted.
    Results are ordered by timestamp (newest first).

    \f

    **Examples:**
        ```bash
        zae-limiter audit list --entity-id user-123
        zae-limiter audit list --entity-id user-123 --limit 10
        ```

    **Sample Output:**
        ```
        Audit Events for: user-123

        Timestamp                Action         Principal   Resource
        ───────────────────────  ─────────────  ──────────  ────────
        2026-01-15T10:30:00Z     limits_set     admin       gpt-4
        2026-01-15T10:25:00Z     entity_created admin       -

        Total: 2 events
        ```
    """
    from .exceptions import ValidationError
    from .repository import Repository

    async def _list() -> None:
        try:
            repo = Repository(name, region, endpoint_url)
        except ValidationError as e:
            click.echo(f"Error: {e.reason}", err=True)
            sys.exit(1)

        try:
            events = await repo.get_audit_events(
                entity_id=entity_id,
                limit=limit,
                start_event_id=start_event_id,
            )

            if not events:
                click.echo(f"No audit events found for entity: {entity_id}")
                return

            # Build table data
            click.echo()
            click.echo(f"Audit Events for: {entity_id}")
            click.echo()

            headers = ["Timestamp", "Action", "Principal", "Resource"]
            rows: list[list[str]] = []
            for event in events:
                rows.append(
                    [
                        event.timestamp,
                        event.action,
                        event.principal or "-",
                        event.resource or "-",
                    ]
                )

            from .visualization import TableRenderer

            renderer = TableRenderer()
            click.echo(renderer.render(headers, rows))
            click.echo()
            click.echo(f"Total: {len(events)} events")

            if len(events) == limit:
                last_id = events[-1].event_id
                click.echo(f"More events may exist. Use --start-event-id {last_id}")

        except Exception as e:
            click.echo(f"Error: Failed to list audit events: {e}", err=True)
            sys.exit(1)
        finally:
            await repo.close()

    asyncio.run(_list())


# -------------------------------------------------------------------------
# Usage commands
# -------------------------------------------------------------------------


@cli.group()
def usage() -> None:
    """Usage snapshot commands.

    Query historical usage data aggregated by the Lambda aggregator.
    Snapshots track token consumption per entity/resource in hourly and daily windows.
    """
    pass


@usage.command(
    "list",
    epilog="""\b
Examples:
    \b
    zae-limiter usage list --entity-id user-123
    \b
    zae-limiter usage list --resource gpt-4 --window hourly
    \b
    zae-limiter usage list --entity-id user-123 --plot
""",
)
@click.option(
    "--name",
    "-n",
    default="limiter",
    help="Resource identifier used as the CloudFormation stack name. Default: limiter",
)
@click.option(
    "--region",
    help="AWS region (default: use boto3 defaults)",
)
@click.option(
    "--endpoint-url",
    help=(
        "AWS endpoint URL "
        "(e.g., http://localhost:4566 for LocalStack, or other AWS-compatible services)"
    ),
)
@click.option(
    "--entity-id",
    "-e",
    help="Entity ID to query (required unless --resource is provided)",
)
@click.option(
    "--resource",
    "-r",
    help="Resource name filter (required if --entity-id is not provided)",
)
@click.option(
    "--window",
    "-w",
    type=click.Choice(["hourly", "daily"]),
    help="Filter by window type",
)
@click.option(
    "--start",
    help="Start time (ISO format, e.g., 2024-01-01T00:00:00Z)",
)
@click.option(
    "--end",
    help="End time (ISO format, e.g., 2024-01-31T23:59:59Z)",
)
@click.option(
    "--limit",
    "-l",
    default=100,
    type=int,
    help="Maximum number of snapshots to return (default: 100)",
)
@click.option(
    "--plot",
    "-p",
    is_flag=True,
    help="Display as ASCII charts instead of table (requires: pip install 'zae-limiter[plot]')",
)
def usage_list(
    name: str,
    region: str | None,
    endpoint_url: str | None,
    entity_id: str | None,
    resource: str | None,
    window: str | None,
    start: str | None,
    end: str | None,
    limit: int,
    plot: bool,
) -> None:
    """List usage snapshots.

    Query historical token consumption data. Requires either --entity-id or
    --resource. Use --plot for ASCII chart visualization.

    \f

    **Examples:**
        ```bash
        zae-limiter usage list --entity-id user-123
        zae-limiter usage list --resource gpt-4 --window hourly
        zae-limiter usage list --entity-id user-123 --plot
        ```

    !!! note
        Either `--entity-id` or `--resource` must be provided.

    !!! tip "ASCII Charts"
        The `--plot` flag requires the optional `plot` extra:
        `pip install 'zae-limiter[plot]'`

    **Sample Output:**
        ```
        Usage Snapshots

        Window Start          Type    Resource  Entity    Events  Counters
        ────────────────────  ──────  ────────  ────────  ──────  ────────────────
        2026-01-15T10:00:00Z  hourly  gpt-4     user-123  42      tpm=15,000
        2026-01-15T09:00:00Z  hourly  gpt-4     user-123  38      tpm=12,500

        Total: 2 snapshots
        ```
    """
    from .exceptions import ValidationError
    from .repository import Repository

    if entity_id is None and resource is None:
        click.echo("Error: Either --entity-id or --resource must be provided", err=True)
        sys.exit(1)

    async def _list() -> None:
        try:
            repo = Repository(name, region, endpoint_url)
        except ValidationError as e:
            click.echo(f"Error: {e.reason}", err=True)
            sys.exit(1)

        try:
            snapshots, next_key = await repo.get_usage_snapshots(
                entity_id=entity_id,
                resource=resource,
                window_type=window,
                start_time=start,
                end_time=end,
                limit=limit,
            )

            if not snapshots:
                click.echo("No usage snapshots found")
                return

            use_table = not plot
            if plot:
                # Use PlotFormatter for ASCII charts
                from .visualization import UsageFormatter, format_usage_snapshots

                try:
                    output = format_usage_snapshots(snapshots, formatter=UsageFormatter.PLOT)
                    click.echo(output)
                except ImportError as e:
                    # Graceful fallback for missing asciichartpy
                    click.echo(f"Warning: {e}", err=True)
                    click.echo("Falling back to table format...", err=True)
                    use_table = True

            if use_table:
                # Use TableRenderer for box-drawing table (auto-sized columns)
                from .visualization import TableRenderer

                click.echo()
                click.echo("Usage Snapshots")
                click.echo()

                headers = ["Window Start", "Type", "Resource", "Entity", "Events", "Counters"]
                rows: list[list[str]] = []
                for snap in snapshots:
                    counters_str = ", ".join(f"{k}={v:,}" for k, v in sorted(snap.counters.items()))
                    rows.append(
                        [
                            snap.window_start,
                            snap.window_type,
                            snap.resource,
                            snap.entity_id,
                            str(snap.total_events),
                            counters_str,
                        ]
                    )

                renderer = TableRenderer()
                click.echo(renderer.render(headers, rows))

            click.echo()
            click.echo(f"Total: {len(snapshots)} snapshots")

            if next_key:
                click.echo()
                click.echo("More snapshots exist. Use pagination to see more.")

        except ValueError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except Exception as e:
            click.echo(f"Error: Failed to list usage snapshots: {e}", err=True)
            sys.exit(1)
        finally:
            await repo.close()

    asyncio.run(_list())


@usage.command(
    "summary",
    epilog="""\b
Examples:
    \b
    zae-limiter usage summary --entity-id user-123
    \b
    zae-limiter usage summary --resource gpt-4 --window daily
""",
)
@click.option(
    "--name",
    "-n",
    default="limiter",
    help="Resource identifier used as the CloudFormation stack name. Default: limiter",
)
@click.option(
    "--region",
    help="AWS region (default: use boto3 defaults)",
)
@click.option(
    "--endpoint-url",
    help=(
        "AWS endpoint URL "
        "(e.g., http://localhost:4566 for LocalStack, or other AWS-compatible services)"
    ),
)
@click.option(
    "--entity-id",
    "-e",
    help="Entity ID to query (required unless --resource is provided)",
)
@click.option(
    "--resource",
    "-r",
    help="Resource name filter (required if --entity-id is not provided)",
)
@click.option(
    "--window",
    "-w",
    type=click.Choice(["hourly", "daily"]),
    help="Filter by window type",
)
@click.option(
    "--start",
    help="Start time (ISO format, e.g., 2024-01-01T00:00:00Z)",
)
@click.option(
    "--end",
    help="End time (ISO format, e.g., 2024-01-31T23:59:59Z)",
)
def usage_summary(
    name: str,
    region: str | None,
    endpoint_url: str | None,
    entity_id: str | None,
    resource: str | None,
    window: str | None,
    start: str | None,
    end: str | None,
) -> None:
    """Show aggregated usage summary.

    Computes total and average consumption across matching snapshots.
    Useful for billing, reporting, and capacity planning.

    \f

    **Examples:**
        ```bash
        zae-limiter usage summary --entity-id user-123
        zae-limiter usage summary --resource gpt-4 --window daily
        ```

    !!! note
        Either `--entity-id` or `--resource` must be provided.

    **Sample Output:**
        ```
        Usage Summary

        Entity:     user-123
        Resource:   gpt-4
        Snapshots:  24
        Time Range: 2026-01-14T00:00:00Z to 2026-01-15T23:00:00Z

        Limit  Total     Average
        ─────  ────────  ─────────
        rpm        950      39.58
        tpm    450,000  18,750.00
        ```
    """
    from .exceptions import ValidationError
    from .repository import Repository

    if entity_id is None and resource is None:
        click.echo("Error: Either --entity-id or --resource must be provided", err=True)
        sys.exit(1)

    async def _summary() -> None:
        try:
            repo = Repository(name, region, endpoint_url)
        except ValidationError as e:
            click.echo(f"Error: {e.reason}", err=True)
            sys.exit(1)

        try:
            summary = await repo.get_usage_summary(
                entity_id=entity_id,
                resource=resource,
                window_type=window,
                start_time=start,
                end_time=end,
            )

            if summary.snapshot_count == 0:
                click.echo("No usage data found matching the criteria")
                return

            # Display summary header
            click.echo()
            click.echo("Usage Summary")
            click.echo()
            if entity_id:
                click.echo(f"Entity:     {entity_id}")
            if resource:
                click.echo(f"Resource:   {resource}")
            if window:
                click.echo(f"Window:     {window}")
            click.echo(f"Snapshots:  {summary.snapshot_count}")
            if summary.min_window_start and summary.max_window_start:
                click.echo(f"Time Range: {summary.min_window_start} to {summary.max_window_start}")
            click.echo()

            # Table of counters
            headers = ["Limit", "Total", "Average"]
            rows: list[list[str]] = []
            for limit_name in sorted(summary.total.keys()):
                total = summary.total[limit_name]
                avg = summary.average.get(limit_name, 0.0)
                rows.append([limit_name, f"{total:,}", f"{avg:,.2f}"])

            from .visualization import TableRenderer

            renderer = TableRenderer(alignments=["l", "r", "r"])
            click.echo(renderer.render(headers, rows))
            click.echo()

        except ValueError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except Exception as e:
            click.echo(f"Error: Failed to get usage summary: {e}", err=True)
            sys.exit(1)
        finally:
            await repo.close()

    asyncio.run(_summary())


# -------------------------------------------------------------------------
# Resource config commands
# -------------------------------------------------------------------------


def _parse_limit(limit_str: str) -> Limit:
    """Parse a limit string in format 'name:capacity:burst' or 'name:capacity'."""
    from .models import Limit as LimitModel

    parts = limit_str.split(":")
    if len(parts) < 2:
        raise click.BadParameter(
            f"Invalid limit format: {limit_str}. Expected 'name:capacity[:burst]'"
        )

    name = parts[0]
    try:
        capacity = int(parts[1])
        burst = int(parts[2]) if len(parts) > 2 else capacity
    except ValueError as e:
        raise click.BadParameter(f"Invalid limit values in '{limit_str}': {e}") from e

    # Default to per-minute refill
    return LimitModel(
        name=name,
        capacity=capacity,
        burst=burst,
        refill_amount=capacity,
        refill_period_seconds=60,
    )


def _format_limit(limit: Limit) -> str:
    """Format a limit for display."""
    return f"{limit.name}: {limit.capacity:,}/min (burst: {limit.burst:,})"


@cli.group()
def resource() -> None:
    """Resource-level default limit configuration.

    Configure default limits for specific resources (e.g., gpt-4, claude-3).
    Resource defaults override system defaults but are overridden by entity limits.
    """
    pass


@resource.command(
    "set-defaults",
    epilog="""\b
Examples:
    \b
    # Set TPM and RPM defaults for gpt-4
    zae-limiter resource set-defaults gpt-4 -l tpm:100000 -l rpm:1000
    \b
    # Set limits with burst capacity
    zae-limiter resource set-defaults claude-3 -l tpm:50000:75000
""",
)
@click.argument("resource_name")
@click.option(
    "--name",
    "-n",
    default="limiter",
    help="Stack identifier used as the CloudFormation stack name. Default: limiter",
)
@click.option(
    "--region",
    help="AWS region (default: use boto3 defaults)",
)
@click.option(
    "--endpoint-url",
    help="AWS endpoint URL (e.g., http://localhost:4566 for LocalStack)",
)
@click.option(
    "--limit",
    "-l",
    "limits",
    multiple=True,
    required=True,
    help="Limit: 'name:capacity[:burst]' (repeatable). Example: -l tpm:10000 -l rpm:500",
)
def resource_set_defaults(
    resource_name: str,
    name: str,
    region: str | None,
    endpoint_url: str | None,
    limits: tuple[str, ...],
) -> None:
    """Set default limits for a resource.

    RESOURCE_NAME is the resource to configure (e.g., 'gpt-4', 'claude-3').
    Resource defaults override system defaults for this specific resource.

    \f

    **Examples:**
        ```bash
        # Set TPM and RPM defaults for gpt-4
        zae-limiter resource set-defaults gpt-4 -l tpm:100000 -l rpm:1000

        # Set limits with burst capacity
        zae-limiter resource set-defaults claude-3 -l tpm:50000:75000
        ```
    """
    from .exceptions import ValidationError
    from .models import Limit as LimitModel
    from .repository import Repository

    # Parse limits
    parsed_limits: list[LimitModel] = []
    for limit_str in limits:
        try:
            parsed_limits.append(_parse_limit(limit_str))
        except click.BadParameter as e:
            click.echo(f"Error: {e.message}", err=True)
            sys.exit(1)

    async def _set() -> None:
        try:
            repo = Repository(name, region, endpoint_url)
        except ValidationError as e:
            click.echo(f"Error: {e.reason}", err=True)
            sys.exit(1)

        try:
            await repo.set_resource_defaults(resource_name, parsed_limits)
            click.echo(f"Set {len(parsed_limits)} default(s) for resource '{resource_name}':")
            for limit in parsed_limits:
                click.echo(f"  {_format_limit(limit)}")
        except ValidationError as e:
            click.echo(f"Error: {e.reason}", err=True)
            sys.exit(1)
        except Exception as e:
            click.echo(f"Error: Failed to set resource defaults: {e}", err=True)
            sys.exit(1)
        finally:
            await repo.close()

    asyncio.run(_set())


@resource.command(
    "get-defaults",
    epilog="""\b
Examples:
    \b
    zae-limiter resource get-defaults gpt-4
    \b
    zae-limiter resource get-defaults claude-3 --name prod
""",
)
@click.argument("resource_name")
@click.option(
    "--name",
    "-n",
    default="limiter",
    help="Stack identifier used as the CloudFormation stack name. Default: limiter",
)
@click.option(
    "--region",
    help="AWS region (default: use boto3 defaults)",
)
@click.option(
    "--endpoint-url",
    help="AWS endpoint URL (e.g., http://localhost:4566 for LocalStack)",
)
def resource_get_defaults(
    resource_name: str,
    name: str,
    region: str | None,
    endpoint_url: str | None,
) -> None:
    """Get default limits for a resource.

    RESOURCE_NAME is the resource to query (e.g., 'gpt-4', 'claude-3').

    \f

    **Examples:**
        ```bash
        zae-limiter resource get-defaults gpt-4
        zae-limiter resource get-defaults claude-3 --name prod
        ```

    **Sample Output:**
        ```
        Defaults for resource 'gpt-4':
          rpm: 500/min (burst: 500)
          tpm: 50000/min (burst: 50000)
        ```
    """
    from .exceptions import ValidationError
    from .repository import Repository

    async def _get() -> None:
        try:
            repo = Repository(name, region, endpoint_url)
        except ValidationError as e:
            click.echo(f"Error: {e.reason}", err=True)
            sys.exit(1)

        try:
            limits = await repo.get_resource_defaults(resource_name)
            if not limits:
                click.echo(f"No defaults configured for resource '{resource_name}'")
                return

            click.echo(f"Defaults for resource '{resource_name}':")
            for limit in limits:
                click.echo(f"  {_format_limit(limit)}")
        except ValidationError as e:
            click.echo(f"Error: {e.reason}", err=True)
            sys.exit(1)
        except Exception as e:
            click.echo(f"Error: Failed to get resource defaults: {e}", err=True)
            sys.exit(1)
        finally:
            await repo.close()

    asyncio.run(_get())


@resource.command(
    "delete-defaults",
    epilog="""\b
Examples:
    \b
    # Delete with confirmation prompt
    zae-limiter resource delete-defaults gpt-4
    \b
    # Skip confirmation
    zae-limiter resource delete-defaults gpt-4 --yes
""",
)
@click.argument("resource_name")
@click.option(
    "--name",
    "-n",
    default="limiter",
    help="Stack identifier used as the CloudFormation stack name. Default: limiter",
)
@click.option(
    "--region",
    help="AWS region (default: use boto3 defaults)",
)
@click.option(
    "--endpoint-url",
    help="AWS endpoint URL (e.g., http://localhost:4566 for LocalStack)",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip confirmation prompt",
)
def resource_delete_defaults(
    resource_name: str,
    name: str,
    region: str | None,
    endpoint_url: str | None,
    yes: bool,
) -> None:
    """Delete default limits for a resource.

    RESOURCE_NAME is the resource to delete defaults from (e.g., 'gpt-4', 'claude-3').

    \f

    **Examples:**
        ```bash
        # Delete with confirmation prompt
        zae-limiter resource delete-defaults gpt-4

        # Skip confirmation
        zae-limiter resource delete-defaults gpt-4 --yes
        ```
    """
    from .exceptions import ValidationError
    from .repository import Repository

    if not yes:
        if not click.confirm(f"Delete all defaults for resource '{resource_name}'?"):
            click.echo("Cancelled")
            return

    async def _delete() -> None:
        try:
            repo = Repository(name, region, endpoint_url)
        except ValidationError as e:
            click.echo(f"Error: {e.reason}", err=True)
            sys.exit(1)

        try:
            await repo.delete_resource_defaults(resource_name)
            click.echo(f"Deleted defaults for resource '{resource_name}'")
        except ValidationError as e:
            click.echo(f"Error: {e.reason}", err=True)
            sys.exit(1)
        except Exception as e:
            click.echo(f"Error: Failed to delete resource defaults: {e}", err=True)
            sys.exit(1)
        finally:
            await repo.close()

    asyncio.run(_delete())


@resource.command(
    "list",
    epilog="""\b
Examples:
    \b
    zae-limiter resource list
    \b
    zae-limiter resource list --name prod
""",
)
@click.option(
    "--name",
    "-n",
    default="limiter",
    help="Stack identifier used as the CloudFormation stack name. Default: limiter",
)
@click.option(
    "--region",
    help="AWS region (default: use boto3 defaults)",
)
@click.option(
    "--endpoint-url",
    help="AWS endpoint URL (e.g., http://localhost:4566 for LocalStack)",
)
def resource_list(
    name: str,
    region: str | None,
    endpoint_url: str | None,
) -> None:
    """List all resources with configured defaults.

    \f

    **Examples:**
        ```bash
        zae-limiter resource list
        zae-limiter resource list --name prod
        ```

    **Sample Output:**
        ```
        Resources with configured defaults:
          gpt-4
          gpt-3.5-turbo
          claude-3
        ```
    """
    from .exceptions import ValidationError
    from .repository import Repository

    async def _list() -> None:
        try:
            repo = Repository(name, region, endpoint_url)
        except ValidationError as e:
            click.echo(f"Error: {e.reason}", err=True)
            sys.exit(1)

        try:
            resources = await repo.list_resources_with_defaults()
            if not resources:
                click.echo("No resources with configured defaults")
                return

            click.echo("Resources with configured defaults:")
            for res in resources:
                click.echo(f"  {res}")
        except Exception as e:
            click.echo(f"Error: Failed to list resources: {e}", err=True)
            sys.exit(1)
        finally:
            await repo.close()

    asyncio.run(_list())


# -------------------------------------------------------------------------
# System config commands
# -------------------------------------------------------------------------


@cli.group()
def system() -> None:
    """System-level default limit configuration.

    Configure global defaults that apply to ALL resources unless overridden.
    System defaults are the lowest priority in the hierarchy.
    """
    pass


@system.command(
    "set-defaults",
    epilog="""\b
Examples:
    \b
    # Set global defaults
    zae-limiter system set-defaults -l tpm:10000 -l rpm:100
    \b
    # Set defaults with unavailability behavior
    zae-limiter system set-defaults -l tpm:10000 --on-unavailable allow
""",
)
@click.option(
    "--name",
    "-n",
    default="limiter",
    help="Stack identifier used as the CloudFormation stack name. Default: limiter",
)
@click.option(
    "--region",
    help="AWS region (default: use boto3 defaults)",
)
@click.option(
    "--endpoint-url",
    help="AWS endpoint URL (e.g., http://localhost:4566 for LocalStack)",
)
@click.option(
    "--limit",
    "-l",
    "limits",
    multiple=True,
    required=True,
    help="Limit: 'name:capacity[:burst]' (repeatable). Example: -l tpm:10000 -l rpm:500",
)
@click.option(
    "--on-unavailable",
    type=click.Choice(["allow", "block"]),
    help="Behavior when DynamoDB is unavailable",
)
def system_set_defaults(
    name: str,
    region: str | None,
    endpoint_url: str | None,
    limits: tuple[str, ...],
    on_unavailable: str | None,
) -> None:
    """Set system-wide default limits.

    System defaults apply to ALL resources unless overridden at resource or entity level.

    \f

    **Examples:**
        ```bash
        # Set global defaults
        zae-limiter system set-defaults -l tpm:10000 -l rpm:100

        # Set defaults with unavailability behavior
        zae-limiter system set-defaults -l tpm:10000 --on-unavailable allow
        ```
    """
    from .exceptions import ValidationError
    from .models import Limit as LimitModel
    from .repository import Repository

    # Parse limits
    parsed_limits: list[LimitModel] = []
    for limit_str in limits:
        try:
            parsed_limits.append(_parse_limit(limit_str))
        except click.BadParameter as e:
            click.echo(f"Error: {e.message}", err=True)
            sys.exit(1)

    async def _set() -> None:
        try:
            repo = Repository(name, region, endpoint_url)
        except ValidationError as e:
            click.echo(f"Error: {e.reason}", err=True)
            sys.exit(1)

        try:
            await repo.set_system_defaults(parsed_limits, on_unavailable=on_unavailable)
            n_limits = len(parsed_limits)
            click.echo(f"Set {n_limits} system-wide default(s):")
            for limit in parsed_limits:
                click.echo(f"  {_format_limit(limit)}")
            if on_unavailable:
                click.echo(f"  on_unavailable: {on_unavailable}")
        except ValidationError as e:
            click.echo(f"Error: {e.reason}", err=True)
            sys.exit(1)
        except Exception as e:
            click.echo(f"Error: Failed to set system defaults: {e}", err=True)
            sys.exit(1)
        finally:
            await repo.close()

    asyncio.run(_set())


@system.command(
    "get-defaults",
    epilog="""\b
Examples:
    \b
    zae-limiter system get-defaults
    \b
    zae-limiter system get-defaults --name prod
""",
)
@click.option(
    "--name",
    "-n",
    default="limiter",
    help="Stack identifier used as the CloudFormation stack name. Default: limiter",
)
@click.option(
    "--region",
    help="AWS region (default: use boto3 defaults)",
)
@click.option(
    "--endpoint-url",
    help="AWS endpoint URL (e.g., http://localhost:4566 for LocalStack)",
)
def system_get_defaults(
    name: str,
    region: str | None,
    endpoint_url: str | None,
) -> None:
    """Get system-wide default limits and config.

    \f

    **Examples:**
        ```bash
        zae-limiter system get-defaults
        zae-limiter system get-defaults --name prod
        ```

    **Sample Output:**
        ```
        System-wide defaults:
          Limits:
            rpm: 1000/min (burst: 1000)
            tpm: 100000/min (burst: 100000)
          on_unavailable: allow
        ```
    """
    from .exceptions import ValidationError
    from .repository import Repository

    async def _get() -> None:
        try:
            repo = Repository(name, region, endpoint_url)
        except ValidationError as e:
            click.echo(f"Error: {e.reason}", err=True)
            sys.exit(1)

        try:
            limits, on_unavailable = await repo.get_system_defaults()
            if not limits and not on_unavailable:
                click.echo("No system defaults configured")
                return

            click.echo("System-wide defaults:")
            if limits:
                click.echo("  Limits:")
                for limit in limits:
                    click.echo(f"    {_format_limit(limit)}")
            if on_unavailable:
                click.echo(f"  on_unavailable: {on_unavailable}")
        except ValidationError as e:
            click.echo(f"Error: {e.reason}", err=True)
            sys.exit(1)
        except Exception as e:
            click.echo(f"Error: Failed to get system defaults: {e}", err=True)
            sys.exit(1)
        finally:
            await repo.close()

    asyncio.run(_get())


@system.command(
    "delete-defaults",
    epilog="""\b
Examples:
    \b
    # Delete with confirmation prompt
    zae-limiter system delete-defaults
    \b
    # Skip confirmation
    zae-limiter system delete-defaults --yes
""",
)
@click.option(
    "--name",
    "-n",
    default="limiter",
    help="Stack identifier used as the CloudFormation stack name. Default: limiter",
)
@click.option(
    "--region",
    help="AWS region (default: use boto3 defaults)",
)
@click.option(
    "--endpoint-url",
    help="AWS endpoint URL (e.g., http://localhost:4566 for LocalStack)",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip confirmation prompt",
)
def system_delete_defaults(
    name: str,
    region: str | None,
    endpoint_url: str | None,
    yes: bool,
) -> None:
    """Delete all system-wide default limits and config.

    \f

    **Examples:**
        ```bash
        # Delete with confirmation prompt
        zae-limiter system delete-defaults

        # Skip confirmation
        zae-limiter system delete-defaults --yes
        ```
    """
    from .exceptions import ValidationError
    from .repository import Repository

    if not yes:
        if not click.confirm("Delete all system-wide defaults?"):
            click.echo("Cancelled")
            return

    async def _delete() -> None:
        try:
            repo = Repository(name, region, endpoint_url)
        except ValidationError as e:
            click.echo(f"Error: {e.reason}", err=True)
            sys.exit(1)

        try:
            await repo.delete_system_defaults()
            click.echo("Deleted all system-wide defaults")
        except ValidationError as e:
            click.echo(f"Error: {e.reason}", err=True)
            sys.exit(1)
        except Exception as e:
            click.echo(f"Error: Failed to delete system defaults: {e}", err=True)
            sys.exit(1)
        finally:
            await repo.close()

    asyncio.run(_delete())


# -------------------------------------------------------------------------
# Entity config commands
# -------------------------------------------------------------------------


@cli.group()
def entity() -> None:
    """Entity-level limit configuration.

    Manage entities (users, API keys, projects) and their custom limits.
    Entity limits have highest priority, overriding resource and system defaults.
    """
    pass


@entity.command(
    "create",
    epilog="""\b
Examples:
    \b
    # Create a standalone entity
    zae-limiter entity create user-123
    \b
    # Create with display name
    zae-limiter entity create api-key-abc --display-name "Production API"
    \b
    # Create with parent and cascade
    zae-limiter entity create user-123 --parent org-456 --cascade
""",
)
@click.argument("entity_id")
@click.option(
    "--display-name",
    default=None,
    help="Human-readable name (defaults to entity_id)",
)
@click.option(
    "--parent",
    default=None,
    help="Parent entity ID (for hierarchical limits)",
)
@click.option(
    "--cascade/--no-cascade",
    default=False,
    help="Enable cascade: acquire() also consumes from parent entity",
)
@click.option(
    "--name",
    "-n",
    default="limiter",
    help="Stack identifier used as the CloudFormation stack name. Default: limiter",
)
@click.option(
    "--region",
    help="AWS region (default: use boto3 defaults)",
)
@click.option(
    "--endpoint-url",
    help="AWS endpoint URL (e.g., http://localhost:4566 for LocalStack)",
)
def entity_create(
    entity_id: str,
    display_name: str | None,
    parent: str | None,
    cascade: bool,
    name: str,
    region: str | None,
    endpoint_url: str | None,
) -> None:
    """Create a new entity.

    ENTITY_ID is the unique identifier for the entity (e.g., 'user-123', 'api-key-abc').

    \f

    **Examples:**
        ```bash
        # Create a standalone entity
        zae-limiter entity create user-123

        # Create with display name
        zae-limiter entity create api-key-abc --display-name "Production API"

        # Create with parent and cascade
        zae-limiter entity create user-123 --parent org-456 --cascade
        ```
    """
    from .exceptions import ValidationError
    from .repository import Repository

    async def _create() -> None:
        try:
            repo = Repository(name, region, endpoint_url)
        except ValidationError as e:
            click.echo(f"Error: {e.reason}", err=True)
            sys.exit(1)

        try:
            entity = await repo.create_entity(
                entity_id=entity_id,
                name=display_name,
                parent_id=parent,
                cascade=cascade,
            )
            click.echo(f"Created entity '{entity.id}'")
            if entity.name and entity.name != entity.id:
                click.echo(f"  Name:    {entity.name}")
            if entity.parent_id:
                click.echo(f"  Parent:  {entity.parent_id}")
            click.echo(f"  Cascade: {entity.cascade}")
        except Exception as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        finally:
            await repo.close()

    asyncio.run(_create())


@entity.command(
    "show",
    epilog="""\b
Examples:
    \b
    zae-limiter entity show user-123
    \b
    zae-limiter entity show api-key-abc --name prod
""",
)
@click.argument("entity_id")
@click.option(
    "--name",
    "-n",
    default="limiter",
    help="Stack identifier used as the CloudFormation stack name. Default: limiter",
)
@click.option(
    "--region",
    help="AWS region (default: use boto3 defaults)",
)
@click.option(
    "--endpoint-url",
    help="AWS endpoint URL (e.g., http://localhost:4566 for LocalStack)",
)
def entity_show(
    entity_id: str,
    name: str,
    region: str | None,
    endpoint_url: str | None,
) -> None:
    """Show details for an entity.

    ENTITY_ID is the entity to query (e.g., 'user-123', 'api-key-abc').

    \f

    **Examples:**
        ```bash
        zae-limiter entity show user-123
        zae-limiter entity show api-key-abc --name prod
        ```

    **Sample Output:**
        ```
        Entity: user-123
          Name:       Alice Smith
          Parent:     org-456
          Cascade:    True
          Created:    2026-01-15T10:30:00Z
          Metadata:   {'tier': 'premium'}
        ```
    """
    from .exceptions import ValidationError
    from .repository import Repository

    async def _show() -> None:
        try:
            repo = Repository(name, region, endpoint_url)
        except ValidationError as e:
            click.echo(f"Error: {e.reason}", err=True)
            sys.exit(1)

        try:
            entity = await repo.get_entity(entity_id)
            if entity is None:
                click.echo(f"Entity '{entity_id}' not found", err=True)
                sys.exit(1)

            click.echo(f"Entity: {entity.id}")
            if entity.name and entity.name != entity.id:
                click.echo(f"  Name:       {entity.name}")
            click.echo(f"  Parent:     {entity.parent_id or '(none)'}")
            click.echo(f"  Cascade:    {entity.cascade}")
            if entity.created_at:
                click.echo(f"  Created:    {entity.created_at}")
            if entity.metadata:
                click.echo(f"  Metadata:   {entity.metadata}")
        except Exception as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        finally:
            await repo.close()

    asyncio.run(_show())


@entity.command(
    "set-limits",
    epilog="""\b
Examples:
    \b
    # Set premium user limits for gpt-4
    zae-limiter entity set-limits user-premium -r gpt-4 -l tpm:100000 -l rpm:1000
    \b
    # Set limits with burst
    zae-limiter entity set-limits api-key-123 -r claude-3 -l tpm:50000:75000
""",
)
@click.argument("entity_id")
@click.option(
    "--resource",
    "-r",
    "resource_name",
    required=True,
    help="Resource name (e.g., 'gpt-4', 'claude-3')",
)
@click.option(
    "--name",
    "-n",
    default="limiter",
    help="Stack identifier used as the CloudFormation stack name. Default: limiter",
)
@click.option(
    "--region",
    help="AWS region (default: use boto3 defaults)",
)
@click.option(
    "--endpoint-url",
    help="AWS endpoint URL (e.g., http://localhost:4566 for LocalStack)",
)
@click.option(
    "--limit",
    "-l",
    "limits",
    multiple=True,
    required=True,
    help="Limit: 'name:capacity[:burst]' (repeatable). Example: -l tpm:10000 -l rpm:500",
)
def entity_set_limits(
    entity_id: str,
    resource_name: str,
    name: str,
    region: str | None,
    endpoint_url: str | None,
    limits: tuple[str, ...],
) -> None:
    """Set limits for a specific entity and resource.

    ENTITY_ID is the entity to configure (e.g., 'user-123', 'api-key-abc').
    Entity limits override resource and system defaults.

    \f

    **Examples:**
        ```bash
        # Set premium user limits for gpt-4
        zae-limiter entity set-limits user-premium -r gpt-4 -l tpm:100000 -l rpm:1000

        # Set limits with burst
        zae-limiter entity set-limits api-key-123 -r claude-3 -l tpm:50000:75000
        ```
    """
    from .exceptions import ValidationError
    from .models import Limit as LimitModel
    from .repository import Repository

    # Parse limits
    parsed_limits: list[LimitModel] = []
    for limit_str in limits:
        try:
            parsed_limits.append(_parse_limit(limit_str))
        except click.BadParameter as e:
            click.echo(f"Error: {e.message}", err=True)
            sys.exit(1)

    async def _set() -> None:
        try:
            repo = Repository(name, region, endpoint_url)
        except ValidationError as e:
            click.echo(f"Error: {e.reason}", err=True)
            sys.exit(1)

        try:
            await repo.set_limits(entity_id, parsed_limits, resource=resource_name)
            click.echo(
                f"Set {len(parsed_limits)} limit(s) for entity '{entity_id}' "
                f"on resource '{resource_name}':"
            )
            for limit in parsed_limits:
                click.echo(f"  {_format_limit(limit)}")
        except ValidationError as e:
            click.echo(f"Error: {e.reason}", err=True)
            sys.exit(1)
        except Exception as e:
            click.echo(f"Error: Failed to set entity limits: {e}", err=True)
            sys.exit(1)
        finally:
            await repo.close()

    asyncio.run(_set())


@entity.command(
    "get-limits",
    epilog="""\b
Examples:
    \b
    zae-limiter entity get-limits user-premium --resource gpt-4
    \b
    zae-limiter entity get-limits api-key-123 -r claude-3
""",
)
@click.argument("entity_id")
@click.option(
    "--resource",
    "-r",
    "resource_name",
    required=True,
    help="Resource name (e.g., 'gpt-4', 'claude-3')",
)
@click.option(
    "--name",
    "-n",
    default="limiter",
    help="Stack identifier used as the CloudFormation stack name. Default: limiter",
)
@click.option(
    "--region",
    help="AWS region (default: use boto3 defaults)",
)
@click.option(
    "--endpoint-url",
    help="AWS endpoint URL (e.g., http://localhost:4566 for LocalStack)",
)
def entity_get_limits(
    entity_id: str,
    resource_name: str,
    name: str,
    region: str | None,
    endpoint_url: str | None,
) -> None:
    """Get limits for a specific entity and resource.

    ENTITY_ID is the entity to query (e.g., 'user-123', 'api-key-abc').

    \f

    **Examples:**
        ```bash
        zae-limiter entity get-limits user-premium --resource gpt-4
        zae-limiter entity get-limits api-key-123 -r claude-3
        ```

    **Sample Output:**
        ```
        Limits for entity 'user-premium' on resource 'gpt-4':
          rpm: 1000/min (burst: 1000)
          tpm: 100000/min (burst: 100000)
        ```
    """
    from .exceptions import ValidationError
    from .repository import Repository

    async def _get() -> None:
        try:
            repo = Repository(name, region, endpoint_url)
        except ValidationError as e:
            click.echo(f"Error: {e.reason}", err=True)
            sys.exit(1)

        try:
            limits = await repo.get_limits(entity_id, resource=resource_name)
            if not limits:
                click.echo(
                    f"No limits configured for entity '{entity_id}' on resource '{resource_name}'"
                )
                return

            click.echo(f"Limits for entity '{entity_id}' on resource '{resource_name}':")
            for limit in limits:
                click.echo(f"  {_format_limit(limit)}")
        except ValidationError as e:
            click.echo(f"Error: {e.reason}", err=True)
            sys.exit(1)
        except Exception as e:
            click.echo(f"Error: Failed to get entity limits: {e}", err=True)
            sys.exit(1)
        finally:
            await repo.close()

    asyncio.run(_get())


@entity.command(
    "delete-limits",
    epilog="""\b
Examples:
    \b
    # Delete with confirmation
    zae-limiter entity delete-limits user-premium --resource gpt-4
    \b
    # Skip confirmation
    zae-limiter entity delete-limits user-premium -r gpt-4 --yes
""",
)
@click.argument("entity_id")
@click.option(
    "--resource",
    "-r",
    "resource_name",
    required=True,
    help="Resource name (e.g., 'gpt-4', 'claude-3')",
)
@click.option(
    "--name",
    "-n",
    default="limiter",
    help="Stack identifier used as the CloudFormation stack name. Default: limiter",
)
@click.option(
    "--region",
    help="AWS region (default: use boto3 defaults)",
)
@click.option(
    "--endpoint-url",
    help="AWS endpoint URL (e.g., http://localhost:4566 for LocalStack)",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip confirmation prompt",
)
def entity_delete_limits(
    entity_id: str,
    resource_name: str,
    name: str,
    region: str | None,
    endpoint_url: str | None,
    yes: bool,
) -> None:
    """Delete limits for a specific entity and resource.

    ENTITY_ID is the entity to delete limits from (e.g., 'user-123', 'api-key-abc').

    \f

    **Examples:**
        ```bash
        # Delete with confirmation
        zae-limiter entity delete-limits user-premium --resource gpt-4

        # Skip confirmation
        zae-limiter entity delete-limits user-premium -r gpt-4 --yes
        ```
    """
    from .exceptions import ValidationError
    from .repository import Repository

    if not yes:
        if not click.confirm(
            f"Delete limits for entity '{entity_id}' on resource '{resource_name}'?"
        ):
            click.echo("Cancelled")
            return

    async def _delete() -> None:
        try:
            repo = Repository(name, region, endpoint_url)
        except ValidationError as e:
            click.echo(f"Error: {e.reason}", err=True)
            sys.exit(1)

        try:
            await repo.delete_limits(entity_id, resource=resource_name)
            click.echo(f"Deleted limits for entity '{entity_id}' on resource '{resource_name}'")
        except ValidationError as e:
            click.echo(f"Error: {e.reason}", err=True)
            sys.exit(1)
        except Exception as e:
            click.echo(f"Error: Failed to delete entity limits: {e}", err=True)
            sys.exit(1)
        finally:
            await repo.close()

    asyncio.run(_delete())


# ---------------------------------------------------------------------------
# Local development commands
# ---------------------------------------------------------------------------

cli.add_command(local)


if __name__ == "__main__":
    cli()
