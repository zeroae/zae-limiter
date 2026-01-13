"""Command-line interface for zae-limiter infrastructure management."""

import asyncio
import sys
from pathlib import Path

import click

from .infra.lambda_builder import get_package_info, write_lambda_package
from .infra.stack_manager import StackManager
from .models import StackOptions


@click.group()
@click.version_option()
def cli() -> None:
    """zae-limiter infrastructure management CLI."""
    pass


@cli.command()
@click.option(
    "--name",
    "-n",
    default="limiter",
    help="Resource identifier (will be prefixed with 'ZAEL-'). Default: limiter",
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
) -> None:
    """Deploy CloudFormation stack with DynamoDB table and Lambda aggregator."""
    from .exceptions import ValidationError

    try:
        manager = StackManager(name, region, endpoint_url)
    except ValidationError as e:
        click.echo(f"Error: {e.reason}", err=True)
        sys.exit(1)

    async def _deploy() -> None:
        async with manager:
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
            click.echo(f"  Alarms: {'enabled' if stack_options.enable_alarms else 'disabled'}")
            if stack_options.enable_alarms and stack_options.alarm_sns_topic:
                click.echo(f"  Alarm SNS topic: {stack_options.alarm_sns_topic}")
            click.echo()

            try:
                # Step 1: Create CloudFormation stack
                result = await manager.create_stack(
                    stack_options=stack_options,
                    wait=wait,
                )

                status = result.get("status", "unknown")
                if status == "skipped_local":
                    click.echo("⚠️  CloudFormation deployment skipped (local DynamoDB detected)")
                    sys.exit(0)

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
                        elif lambda_result.get("status") == "skipped_local":
                            click.echo("  Lambda deployment skipped (local environment)")
                    except Exception as e:
                        click.echo(f"⚠️  Lambda deployment failed: {e}", err=True)
                        click.echo(
                            "  Stack was created successfully, but Lambda code "
                            "needs manual deployment.",
                            err=True,
                        )
                        sys.exit(1)

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


@cli.command()
@click.option(
    "--name",
    "-n",
    required=True,
    help="Resource identifier (will be prefixed with 'ZAEL-' if not already)",
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
    """Delete CloudFormation stack."""
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


@cli.command()
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Output file (default: stdout)",
)
def cfn_template(output: str | None) -> None:
    """Export CloudFormation template for custom deployment."""
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


@cli.command("lambda-export")
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
    """Export Lambda deployment package for custom deployment."""
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
            click.echo(f"Uncompressed size: {int(pkg_info['uncompressed_size']) / 1024:.1f} KB")
            click.echo(f"Handler:           {pkg_info['handler']}")
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


@cli.command()
@click.option(
    "--name",
    "-n",
    required=True,
    help="Resource identifier (will be prefixed with 'ZAEL-' if not already)",
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
    """Get CloudFormation stack status."""
    from .exceptions import ValidationError

    try:
        manager = StackManager(name, region, endpoint_url)
    except ValidationError as e:
        click.echo(f"Error: {e.reason}", err=True)
        sys.exit(1)

    async def _status() -> None:
        async with manager:
            try:
                stack_status = await manager.get_stack_status(manager.stack_name)

                if stack_status is None:
                    click.echo(f"Stack '{manager.stack_name}' not found")
                    sys.exit(1)

                click.echo(f"Stack: {manager.stack_name}")
                click.echo(f"Status: {stack_status}")

                # Interpret status
                if stack_status == "CREATE_COMPLETE":
                    click.echo("✓ Stack is ready")
                elif stack_status == "DELETE_COMPLETE":
                    click.echo("✓ Stack has been deleted")
                elif "IN_PROGRESS" in stack_status:
                    click.echo("⏳ Operation in progress...")
                elif "FAILED" in stack_status or "ROLLBACK" in stack_status:
                    click.echo("✗ Stack operation failed", err=True)
                    sys.exit(1)

            except Exception as e:
                click.echo(f"✗ Failed to get status: {e}", err=True)
                sys.exit(1)

    asyncio.run(_status())


@cli.command("version")
@click.option(
    "--name",
    "-n",
    default="limiter",
    help="Resource identifier (will be prefixed with 'ZAEL-'). Default: limiter",
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
    """Show infrastructure version information."""
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


@cli.command()
@click.option(
    "--name",
    "-n",
    default="limiter",
    help="Resource identifier (will be prefixed with 'ZAEL-'). Default: limiter",
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
    """Upgrade infrastructure to match client version."""
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
                click.echo("[1/2] Deploying Lambda code...")
                try:
                    result = await manager.deploy_lambda_code(wait=True)

                    if result.get("status") == "deployed":
                        size_kb = result.get("size_bytes", 0) / 1024
                        click.echo(f"      Lambda code deployed ({size_kb:.1f} KB)")
                    elif result.get("status") == "skipped_local":
                        click.echo("      Skipped (local environment)")

                except Exception as e:
                    click.echo(f"✗ Lambda deployment failed: {e}", err=True)
                    sys.exit(1)

                # Step 2: Update version record
                click.echo("[2/2] Updating version record...")
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


@cli.command()
@click.option(
    "--name",
    "-n",
    default="limiter",
    help="Resource identifier (will be prefixed with 'ZAEL-'). Default: limiter",
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
    """Check infrastructure compatibility without modifying."""
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


@cli.command("deploy-admin")
@click.option(
    "--name",
    "-n",
    required=True,
    help="Admin stack identifier (will be prefixed with 'ZAEL-' and suffixed with '-admin')",
)
@click.option(
    "--core-stack",
    required=True,
    help="Name of the core ZAEL stack (for DynamoDB table reference)",
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
    "--auth-type",
    type=click.Choice(["IAM", "NONE"]),
    default="IAM",
    help="API authorization type (IAM or NONE for development)",
)
@click.option(
    "--lambda-timeout",
    default=30,
    type=int,
    help="Lambda function timeout in seconds",
)
@click.option(
    "--lambda-memory",
    default=256,
    type=int,
    help="Lambda function memory in MB",
)
@click.option(
    "--log-retention-days",
    default="14",
    type=click.Choice(
        ["1", "3", "5", "7", "14", "30", "60", "90", "120", "150", "180", "365"]
    ),
    help="CloudWatch log retention period",
)
@click.option(
    "--permission-boundary",
    type=str,
    default=None,
    help="ARN of IAM permission boundary to attach to Lambda execution role",
)
@click.option(
    "--role-name-format",
    type=str,
    default=None,
    help="Format template for Lambda role name. Use {} as placeholder for default name.",
)
@click.option(
    "--wait/--no-wait",
    default=True,
    help="Wait for stack creation to complete",
)
def deploy_admin(
    name: str,
    core_stack: str,
    region: str | None,
    endpoint_url: str | None,
    auth_type: str,
    lambda_timeout: int,
    lambda_memory: int,
    log_retention_days: str,
    permission_boundary: str | None,
    role_name_format: str | None,
    wait: bool,
) -> None:
    """Deploy Admin API stack (API Gateway + Lambda) for rate limiter administration."""
    from .exceptions import ValidationError
    from .infra.admin_stack_manager import AdminStackManager

    try:
        manager = AdminStackManager(name, core_stack, region, endpoint_url)
    except ValidationError as e:
        click.echo(f"Error: {e.reason}", err=True)
        sys.exit(1)

    async def _deploy() -> None:
        async with manager:
            click.echo(f"Deploying Admin API stack: {manager.stack_name}")
            click.echo(f"  Core stack: {manager.core_stack_name}")
            click.echo(f"  Region: {region or 'default'}")
            click.echo(f"  Auth type: {auth_type}")
            click.echo(f"  Lambda timeout: {lambda_timeout}s")
            click.echo(f"  Lambda memory: {lambda_memory}MB")
            click.echo()

            try:
                result = await manager.create_stack(
                    auth_type=auth_type,
                    lambda_timeout=lambda_timeout,
                    lambda_memory=lambda_memory,
                    log_retention_days=int(log_retention_days),
                    permission_boundary=permission_boundary,
                    role_name_format=role_name_format,
                    wait=wait,
                )

                status = result.get("status", "unknown")
                click.echo(f"✓ Stack {status.lower().replace('_', ' ')}")

                if result.get("stack_id"):
                    click.echo(f"  Stack ID: {result['stack_id']}")

                # Deploy Lambda code if wait is True
                if wait:
                    click.echo()
                    click.echo("Deploying Lambda function code...")

                    try:
                        lambda_result = await manager.deploy_lambda_code(wait=True)

                        if lambda_result.get("status") == "deployed":
                            size_kb = lambda_result.get("size_bytes", 0) / 1024
                            click.echo(f"✓ Lambda code deployed ({size_kb:.1f} KB)")
                            click.echo(f"  Function ARN: {lambda_result['function_arn']}")
                        elif lambda_result.get("status") == "skipped_local":
                            click.echo("  Lambda deployment skipped (local environment)")
                    except Exception as e:
                        click.echo(f"⚠️  Lambda deployment failed: {e}", err=True)
                        sys.exit(1)

                # Show API endpoint
                if result.get("api_endpoint"):
                    click.echo()
                    click.echo("Admin API Endpoint:")
                    click.echo(f"  {result['api_endpoint']}")

            except Exception as e:
                click.echo(f"✗ Deployment failed: {e}", err=True)
                sys.exit(1)

    asyncio.run(_deploy())


if __name__ == "__main__":
    cli()
