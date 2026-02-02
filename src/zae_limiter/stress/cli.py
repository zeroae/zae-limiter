"""CLI commands for stress testing."""

from __future__ import annotations

import sys
from pathlib import Path

import boto3
import click


@click.group()
def stress() -> None:
    """Stress testing commands for zae-limiter."""
    pass


@stress.command()
@click.option("--name", "-n", required=True, help="Stress test stack name")
@click.option("--target", "-t", required=True, help="Target zae-limiter stack name")
@click.option("--region", default=None, help="AWS region")
@click.option("--vpc-id", required=True, help="VPC ID for stress test resources")
@click.option("--subnet-ids", required=True, help="Comma-separated private subnet IDs")
@click.option("--max-workers", default=100, type=int, help="Maximum Lambda worker concurrency")
@click.option("--permission-boundary", default="", help="Permission boundary ARN for IAM roles")
def deploy(
    name: str,
    target: str,
    region: str | None,
    vpc_id: str,
    subnet_ids: str,
    max_workers: int,
    permission_boundary: str,
) -> None:
    """Deploy stress test infrastructure."""
    from .builder import build_and_push_locust_image, get_zae_limiter_source
    from .lambda_builder import build_stress_lambda_package

    click.echo(f"Deploying stress test stack: {name}")

    # Validate target stack
    cfn = boto3.client("cloudformation", region_name=region)
    try:
        response = cfn.describe_stacks(StackName=target)
        outputs = {
            o["OutputKey"]: o["OutputValue"] for o in response["Stacks"][0].get("Outputs", [])
        }
    except cfn.exceptions.ClientError:
        click.echo(f"Error: Target stack not found: {target}", err=True)
        sys.exit(1)

    required_outputs = ["AppPolicyArn", "AdminPolicyArn"]
    missing = [k for k in required_outputs if k not in outputs]
    if missing:
        click.echo(f"Error: Target stack missing outputs: {missing}", err=True)
        sys.exit(1)

    click.echo("  Target stack validated")

    # Get zae-limiter source
    zae_limiter_source = get_zae_limiter_source()
    click.echo(f"  zae-limiter source: {zae_limiter_source}")

    # Deploy CloudFormation stack
    template_path = Path(__file__).parent / "cfn_template.yaml"
    template_body = template_path.read_text()

    subnet_list = [s.strip() for s in subnet_ids.split(",")]

    try:
        cfn.create_stack(
            StackName=name,
            TemplateBody=template_body,
            Parameters=[
                {"ParameterKey": "TargetStackName", "ParameterValue": target},
                {"ParameterKey": "VpcId", "ParameterValue": vpc_id},
                {"ParameterKey": "PrivateSubnetIds", "ParameterValue": ",".join(subnet_list)},
                {"ParameterKey": "MaxWorkers", "ParameterValue": str(max_workers)},
                {"ParameterKey": "PermissionBoundary", "ParameterValue": permission_boundary},
            ],
            Capabilities=["CAPABILITY_NAMED_IAM"],
        )
        click.echo("  CloudFormation stack creation started")

        # Wait for stack
        waiter = cfn.get_waiter("stack_create_complete")
        click.echo("  Waiting for stack creation...")
        waiter.wait(StackName=name)
        click.echo("  CloudFormation stack created")

    except cfn.exceptions.AlreadyExistsException:
        click.echo("  Stack already exists, updating...")
        cfn.update_stack(
            StackName=name,
            TemplateBody=template_body,
            Parameters=[
                {"ParameterKey": "TargetStackName", "ParameterValue": target},
                {"ParameterKey": "VpcId", "ParameterValue": vpc_id},
                {"ParameterKey": "PrivateSubnetIds", "ParameterValue": ",".join(subnet_list)},
                {"ParameterKey": "MaxWorkers", "ParameterValue": str(max_workers)},
                {"ParameterKey": "PermissionBoundary", "ParameterValue": permission_boundary},
            ],
            Capabilities=["CAPABILITY_NAMED_IAM"],
        )
        update_waiter = cfn.get_waiter("stack_update_complete")
        update_waiter.wait(StackName=name)
        click.echo("  CloudFormation stack updated")

    # Build and push Docker image
    click.echo("  Building Locust image...")
    image_uri = build_and_push_locust_image(name, region or "us-east-1", zae_limiter_source)
    click.echo(f"  Locust image pushed: {image_uri}")

    # Build Lambda package
    click.echo("  Building Lambda package...")
    zip_path = build_stress_lambda_package(zae_limiter_source)
    click.echo(f"  Lambda package built: {zip_path}")

    # Upload Lambda code
    lambda_client = boto3.client("lambda", region_name=region)

    with open(zip_path, "rb") as f:
        zip_bytes = f.read()

    for func_suffix in ["worker", "setup"]:
        func_name = f"{name}-{func_suffix}"
        lambda_client.update_function_code(
            FunctionName=func_name,
            ZipFile=zip_bytes,
        )
        click.echo(f"  Lambda code uploaded: {func_name}")

    click.echo(f"\nStack ready: {name}")


@stress.command()
@click.option("--target", "-t", required=True, help="Target zae-limiter stack name")
@click.option("--region", default=None, help="AWS region")
@click.option("--endpoint-url", default=None, help="AWS endpoint URL (for LocalStack)")
@click.option(
    "--custom-limits", default=300, type=int, help="Number of entities with custom limits"
)
@click.option("--apis", default=8, type=int, help="Number of APIs to configure")
def setup(
    target: str,
    region: str | None,
    endpoint_url: str | None,
    custom_limits: int,
    apis: int,
) -> None:
    """Setup test entities and limits."""
    import asyncio

    from zae_limiter import Limit, RateLimiter

    click.echo("Setting up test data...")

    async def run_setup() -> None:
        limiter = RateLimiter(
            name=target,
            region=region or "us-east-1",
            endpoint_url=endpoint_url,
        )

        async with limiter:
            # System defaults
            await limiter.set_system_defaults(
                limits=[
                    Limit.per_minute("rpm", 1000),
                    Limit.per_minute("tpm", 100_000),
                ]
            )
            click.echo("  System defaults configured")

            # Resource defaults
            api_configs = {
                "api-0": {"rpm": 500, "tpm": 50_000},
                "api-1": {"rpm": 2000, "tpm": 200_000},
                "api-2": {"rpm": 1000, "tpm": 100_000},
                "api-3": {"rpm": 1500, "tpm": 150_000},
                "api-4": {"rpm": 800, "tpm": 80_000},
                "api-5": {"rpm": 1200, "tpm": 120_000},
                "api-6": {"rpm": 600, "tpm": 60_000},
                "api-7": {"rpm": 1800, "tpm": 180_000},
            }

            for api_name, limits in list(api_configs.items())[:apis]:
                await limiter.set_resource_defaults(
                    resource=api_name,
                    limits=[
                        Limit.per_minute("rpm", limits["rpm"]),
                        Limit.per_minute("tpm", limits["tpm"]),
                    ],
                )
            click.echo(f"  {apis} API resource configs")

            # Whale entity
            await limiter.create_entity("entity-whale", name="Whale Entity")
            await limiter.set_limits(
                entity_id="entity-whale",
                limits=[
                    Limit.per_minute("rpm", 10_000),
                    Limit.per_minute("tpm", 1_000_000),
                ],
            )

            # Spike entity
            await limiter.create_entity("entity-spiker", name="Spike Entity")
            await limiter.set_limits(
                entity_id="entity-spiker",
                limits=[
                    Limit.per_minute("rpm", 5_000),
                    Limit.per_minute("tpm", 500_000),
                ],
            )
            click.echo("  Whale and spike entities created")

            # Custom limit entities
            for i in range(custom_limits):
                entity_id = f"entity-{i:05d}"
                await limiter.create_entity(entity_id, name=f"Custom Entity {i}")
                multiplier = 1 + (i % 5) * 0.5
                await limiter.set_limits(
                    entity_id=entity_id,
                    limits=[
                        Limit.per_minute("rpm", int(1000 * multiplier)),
                        Limit.per_minute("tpm", int(100_000 * multiplier)),
                    ],
                )
            click.echo(f"  {custom_limits} entities with custom limits")

    asyncio.run(run_setup())
    click.echo("\nReady for testing")


@stress.command()
@click.option("--name", "-n", required=True, help="Stress test stack name")
@click.option("--region", default=None, help="AWS region")
def connect(name: str, region: str | None) -> None:
    """Print SSM port-forward command for Locust UI."""
    ecs = boto3.client("ecs", region_name=region)

    # Get task ARN
    tasks = ecs.list_tasks(cluster=name, serviceName=f"{name}-master")
    if not tasks["taskArns"]:
        click.echo("Error: No running tasks. Run 'stress start' first.", err=True)
        sys.exit(1)

    task_arn = tasks["taskArns"][0]
    task_id = task_arn.split("/")[-1]

    click.echo("Run this command to port-forward:\n")
    click.echo("  aws ssm start-session \\")
    click.echo(f"    --target ecs:{name}_{task_id}_locust-master \\")
    click.echo("    --document-name AWS-StartPortForwardingSessionToRemoteHost \\")
    click.echo(
        '    --parameters \'host=["localhost"],portNumber=["8089"],localPortNumber=["8089"]\''
    )
    click.echo("\nThen open: http://localhost:8089")


@stress.command()
@click.option("--name", "-n", required=True, help="Stress test stack name")
@click.option("--region", default=None, help="AWS region")
@click.option("--yes", is_flag=True, help="Skip confirmation")
def teardown(name: str, region: str | None, yes: bool) -> None:
    """Delete stress test infrastructure."""
    if not yes:
        click.confirm(f"Delete stress test stack '{name}'?", abort=True)

    click.echo(f"Deleting stress test stack: {name}")

    cfn = boto3.client("cloudformation", region_name=region)
    ecs = boto3.client("ecs", region_name=region)

    # Scale down service first
    try:
        ecs.update_service(cluster=name, service=f"{name}-master", desiredCount=0)
        click.echo("  Stopped Fargate tasks")
    except Exception:
        pass

    # Delete stack
    cfn.delete_stack(StackName=name)
    waiter = cfn.get_waiter("stack_delete_complete")
    click.echo("  Waiting for stack deletion...")
    waiter.wait(StackName=name)
    click.echo("  Stack deleted")
