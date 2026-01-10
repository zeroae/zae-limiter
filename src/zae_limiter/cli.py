"""Command-line interface for zae-limiter infrastructure management."""

import asyncio
import sys
from pathlib import Path

import click

from .infra.stack_manager import StackManager


@click.group()
@click.version_option()
def cli() -> None:
    """zae-limiter infrastructure management CLI."""
    pass


@cli.command()
@click.option(
    "--table-name",
    default="rate_limits",
    help="DynamoDB table name",
)
@click.option(
    "--stack-name",
    help="CloudFormation stack name (default: zae-limiter-{table-name})",
)
@click.option(
    "--region",
    help="AWS region (default: use boto3 defaults)",
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
    "--wait/--no-wait",
    default=True,
    help="Wait for stack creation to complete",
)
def deploy(
    table_name: str,
    stack_name: str | None,
    region: str | None,
    snapshot_windows: str,
    retention_days: int,
    enable_aggregator: bool,
    wait: bool,
) -> None:
    """Deploy CloudFormation stack with DynamoDB table and Lambda aggregator."""

    async def _deploy() -> None:
        async with StackManager(table_name, region, None) as manager:
            actual_stack_name = stack_name or manager.get_stack_name()

            click.echo(f"Deploying stack: {actual_stack_name}")
            click.echo(f"  Table name: {table_name}")
            click.echo(f"  Region: {region or 'default'}")
            click.echo(f"  Snapshot windows: {snapshot_windows}")
            click.echo(f"  Retention days: {retention_days}")
            click.echo(f"  Aggregator: {'enabled' if enable_aggregator else 'disabled'}")
            click.echo()

            parameters = {
                "snapshot_windows": snapshot_windows,
                "retention_days": str(retention_days),
                "enable_aggregator": "true" if enable_aggregator else "false",
            }

            try:
                # Step 1: Create CloudFormation stack
                result = await manager.create_stack(
                    stack_name=actual_stack_name,
                    parameters=parameters,
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
                if enable_aggregator and wait:
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
                    if enable_aggregator:
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
    "--stack-name",
    required=True,
    help="CloudFormation stack name to delete",
)
@click.option(
    "--region",
    help="AWS region (default: use boto3 defaults)",
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
    stack_name: str,
    region: str | None,
    wait: bool,
    yes: bool,
) -> None:
    """Delete CloudFormation stack."""

    if not yes:
        click.confirm(
            f"Are you sure you want to delete stack '{stack_name}'?",
            abort=True,
        )

    async def _delete() -> None:
        async with StackManager("dummy", region, None) as manager:
            click.echo(f"Deleting stack: {stack_name}")

            try:
                await manager.delete_stack(stack_name=stack_name, wait=wait)

                if wait:
                    click.echo(f"✓ Stack '{stack_name}' deleted successfully")
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


@cli.command()
@click.option(
    "--stack-name",
    required=True,
    help="CloudFormation stack name",
)
@click.option(
    "--region",
    help="AWS region (default: use boto3 defaults)",
)
def status(stack_name: str, region: str | None) -> None:
    """Get CloudFormation stack status."""

    async def _status() -> None:
        async with StackManager("dummy", region, None) as manager:
            try:
                stack_status = await manager.get_stack_status(stack_name)

                if stack_status is None:
                    click.echo(f"Stack '{stack_name}' not found")
                    sys.exit(1)

                click.echo(f"Stack: {stack_name}")
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


if __name__ == "__main__":
    cli()
