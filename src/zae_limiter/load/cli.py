"""CLI commands for load testing."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import boto3
import click


def _select_name(region: str | None) -> str:
    """Interactively select a zae-limiter instance."""
    import asyncio

    import questionary

    from zae_limiter.infra.discovery import InfrastructureDiscovery

    async def list_targets() -> list[str]:
        async with InfrastructureDiscovery(region=region) as discovery:
            limiters = await discovery.list_limiters(stack_type="limiter")
            return [
                info.stack_name
                for info in limiters
                if info.stack_status in ("CREATE_COMPLETE", "UPDATE_COMPLETE")
            ]

    targets = asyncio.run(list_targets())

    if not targets:
        click.echo("Error: No zae-limiter stacks found", err=True)
        sys.exit(1)

    result: str = questionary.select("Select zae-limiter stack:", choices=targets).ask()
    return result


def _select_vpc(region: str | None) -> str:
    """Interactively select a VPC."""
    import questionary

    ec2 = boto3.client("ec2", region_name=region)
    vpcs = ec2.describe_vpcs()["Vpcs"]

    if not vpcs:
        click.echo("Error: No VPCs found", err=True)
        sys.exit(1)

    choices = []
    for vpc in vpcs:
        vid = vpc["VpcId"]
        name = next((t["Value"] for t in vpc.get("Tags", []) if t["Key"] == "Name"), "")
        label = f"{vid} ({name})" if name else vid
        choices.append(questionary.Choice(title=label, value=vid))

    result: str = questionary.select("Select VPC:", choices=choices).ask()
    return result


def _select_subnets(region: str | None, vpc_id: str) -> str:
    """Interactively select subnets (multi-select)."""
    import questionary

    ec2 = boto3.client("ec2", region_name=region)
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]

    if not subnets:
        click.echo(f"Error: No subnets found in VPC {vpc_id}", err=True)
        sys.exit(1)

    choices = []
    for subnet in subnets:
        subnet_id = subnet["SubnetId"]
        az = subnet["AvailabilityZone"]
        name = next((t["Value"] for t in subnet.get("Tags", []) if t["Key"] == "Name"), "")
        label = f"{subnet_id} ({az}, {name})" if name else f"{subnet_id} ({az})"
        choices.append(questionary.Choice(title=label, value=subnet_id))

    selected = questionary.checkbox("Select subnets (at least 2):", choices=choices).ask()

    if not selected or len(selected) < 2:
        click.echo("Error: Select at least 2 subnets", err=True)
        sys.exit(1)

    return ",".join(selected)


def _discover_route_tables(ec2: Any, subnet_ids: list[str]) -> str:
    """Discover route table IDs associated with the given subnets."""
    response = ec2.describe_route_tables(
        Filters=[{"Name": "association.subnet-id", "Values": subnet_ids}]
    )
    rt_ids = {rt["RouteTableId"] for rt in response["RouteTables"]}
    return ",".join(sorted(rt_ids))


@click.group()
def load() -> None:
    """Load testing commands for zae-limiter."""
    pass


@load.command()
@click.option("--name", "-n", default=None, help="zae-limiter name")
@click.option("--region", default=None, help="AWS region")
@click.option("--vpc-id", default=None, help="VPC ID for load test resources")
@click.option("--subnet-ids", default=None, help="Comma-separated private subnet IDs")
@click.option(
    "--max-workers", default=100, type=int, help="Maximum Lambda workers (auto-scaling cap)"
)
@click.option(
    "--users-per-worker",
    default=10,
    type=int,
    help="Max users per Lambda worker for auto-scaling (default: 10)",
)
@click.option(
    "--min-workers",
    default=1,
    type=int,
    help="Minimum workers to maintain (default: 1)",
)
@click.option(
    "--startup-lead-time",
    default=20,
    type=int,
    help="Seconds to predict ahead for proactive scaling (default: 20)",
)
@click.option(
    "--lambda-timeout",
    default=5,
    type=int,
    help="Lambda worker timeout in minutes (default: 5)",
)
@click.option(
    "--lambda-memory",
    default=1769,
    type=int,
    help="Lambda worker memory in MB (CPU scales with memory; 1769 MB = 1 vCPU, default: 1769)",
)
@click.option(
    "--capacity-provider",
    type=click.Choice(["FARGATE_SPOT", "FARGATE"], case_sensitive=False),
    default="FARGATE_SPOT",
    help="ECS capacity provider (default: FARGATE_SPOT)",
)
@click.option(
    "--ssm-endpoint/--no-ssm-endpoint",
    default=False,
    help="Create VPC endpoints for SSM (not needed if VPC has NAT gateway)",
)
@click.option(
    "--dynamodb-endpoint/--no-dynamodb-endpoint",
    default=True,
    help="Create DynamoDB gateway endpoint (auto-discovers route tables)",
)
@click.option(
    "-C",
    "locustfile_dir",
    default=".",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Directory containing locustfiles (default: current directory)",
)
def deploy(
    name: str | None,
    region: str | None,
    vpc_id: str | None,
    subnet_ids: str | None,
    max_workers: int,
    users_per_worker: int,
    min_workers: int,
    startup_lead_time: int,
    lambda_timeout: int,
    lambda_memory: int,
    capacity_provider: str,
    ssm_endpoint: bool,
    dynamodb_endpoint: bool,
    locustfile_dir: Path,
) -> None:
    """Deploy load test infrastructure."""
    from .builder import build_and_push_locust_image, get_zae_limiter_source
    from .lambda_builder import build_load_lambda_package

    # Interactive prompts for missing options
    if not name:
        name = _select_name(region)
    if not vpc_id:
        vpc_id = _select_vpc(region)
    if not subnet_ids:
        subnet_ids = _select_subnets(region, vpc_id)

    stack_name = f"{name}-load"
    click.echo(f"Deploying load test stack: {stack_name}")

    # Validate target stack
    cfn = boto3.client("cloudformation", region_name=region)
    try:
        response = cfn.describe_stacks(StackName=name)
        outputs = {
            o["OutputKey"]: o["OutputValue"] for o in response["Stacks"][0].get("Outputs", [])
        }
    except cfn.exceptions.ClientError:
        click.echo(f"Error: Stack not found: {name}", err=True)
        sys.exit(1)

    required_outputs = ["AcquireOnlyPolicyArn", "FullAccessPolicyArn"]
    missing = [k for k in required_outputs if k not in outputs]
    if missing:
        click.echo(f"Error: Stack missing outputs: {missing}", err=True)
        sys.exit(1)

    # Get IAM configuration from target stack
    permission_boundary = outputs.get("PermissionBoundaryArn", "")
    role_name_format = outputs.get("RoleNameFormat", "{}")
    if permission_boundary:
        click.echo(f"  Using permission boundary: {permission_boundary}")
    if role_name_format and role_name_format != "{}":
        click.echo(f"  Using role name format: {role_name_format}")

    click.echo("  Stack validated")

    # Get zae-limiter source
    zae_limiter_source = get_zae_limiter_source()
    click.echo(f"  zae-limiter source: {zae_limiter_source}")

    # Deploy CloudFormation stack
    template_path = Path(__file__).parent / "cfn_template.yaml"
    template_body = template_path.read_text()

    subnet_list = [s.strip() for s in subnet_ids.split(",")]

    # Auto-discover route tables for DynamoDB gateway endpoint
    route_table_ids = ""
    if dynamodb_endpoint:
        ec2 = boto3.client("ec2", region_name=region)
        route_table_ids = _discover_route_tables(ec2, subnet_list)
        if route_table_ids:
            # Check if a DynamoDB gateway endpoint already exists for this VPC
            existing = ec2.describe_vpc_endpoints(
                Filters=[
                    {"Name": "vpc-id", "Values": [vpc_id]},
                    {"Name": "service-name", "Values": [f"com.amazonaws.{region}.dynamodb"]},
                    {"Name": "vpc-endpoint-state", "Values": ["available"]},
                ]
            )
            if existing["VpcEndpoints"]:
                ep_id = existing["VpcEndpoints"][0]["VpcEndpointId"]
                click.echo(f"  DynamoDB endpoint already exists: {ep_id}")
                route_table_ids = ""
            else:
                click.echo(f"  Route tables for DynamoDB endpoint: {route_table_ids}")

    # Build parameters list
    stack_params = [
        {"ParameterKey": "TargetStackName", "ParameterValue": name},
        {"ParameterKey": "VpcId", "ParameterValue": vpc_id},
        {"ParameterKey": "PrivateSubnetIds", "ParameterValue": ",".join(subnet_list)},
        {"ParameterKey": "MaxWorkers", "ParameterValue": str(max_workers)},
        {"ParameterKey": "UsersPerWorker", "ParameterValue": str(users_per_worker)},
        {"ParameterKey": "MinWorkers", "ParameterValue": str(min_workers)},
        {"ParameterKey": "StartupLeadTime", "ParameterValue": str(startup_lead_time)},
        {"ParameterKey": "LambdaTimeout", "ParameterValue": str(lambda_timeout * 60)},
        {"ParameterKey": "LambdaMemory", "ParameterValue": str(lambda_memory)},
        {"ParameterKey": "CapacityProvider", "ParameterValue": capacity_provider.upper()},
        {"ParameterKey": "CreateSsmEndpoint", "ParameterValue": str(ssm_endpoint).lower()},
        {"ParameterKey": "PrivateRouteTableIds", "ParameterValue": route_table_ids},
        {"ParameterKey": "PermissionBoundary", "ParameterValue": permission_boundary},
        {"ParameterKey": "RoleNameFormat", "ParameterValue": role_name_format},
    ]

    stack_tags: Any = [
        {"Key": "ManagedBy", "Value": "zae-limiter"},
        {"Key": "zae-limiter:name", "Value": name},
        {"Key": "zae-limiter:type", "Value": "load-test"},
    ]

    try:
        cfn.create_stack(
            StackName=stack_name,
            TemplateBody=template_body,
            Parameters=stack_params,  # type: ignore[arg-type]
            Capabilities=["CAPABILITY_NAMED_IAM"],
            Tags=stack_tags,
        )
        click.echo("  CloudFormation stack creation started")

        # Wait for stack
        waiter = cfn.get_waiter("stack_create_complete")
        click.echo("  Waiting for stack creation...")
        waiter.wait(StackName=stack_name)
        click.echo("  CloudFormation stack created")

    except cfn.exceptions.AlreadyExistsException:
        click.echo("  Stack already exists, updating...")
        try:
            cfn.update_stack(
                StackName=stack_name,
                TemplateBody=template_body,
                Parameters=stack_params,  # type: ignore[arg-type]
                Capabilities=["CAPABILITY_NAMED_IAM"],
                Tags=stack_tags,
            )
            update_waiter = cfn.get_waiter("stack_update_complete")
            update_waiter.wait(StackName=stack_name)
            click.echo("  CloudFormation stack updated")
        except cfn.exceptions.ClientError as e:
            if "No updates are to be performed" in str(e):
                click.echo("  Stack is up to date")
            else:
                raise

    # Build and push Docker image
    click.echo("  Building Locust image...")
    image_uri = build_and_push_locust_image(
        stack_name,
        region or "us-east-1",
        locustfile_dir,
        zae_limiter_source,
    )
    click.echo(f"  Locust image pushed: {image_uri}")

    # Build Lambda package
    click.echo("  Building Lambda package...")
    zip_path = build_load_lambda_package(
        zae_limiter_source,
        locustfile_dir,
    )
    click.echo(f"  Lambda package built: {zip_path}")

    # Upload Lambda code
    lambda_client = boto3.client("lambda", region_name=region)

    with open(zip_path, "rb") as f:
        zip_bytes = f.read()

    func_name = f"{stack_name}-worker"
    lambda_client.update_function_code(
        FunctionName=func_name,
        ZipFile=zip_bytes,
    )
    click.echo(f"  Lambda code uploaded: {func_name}")

    click.echo(f"\nStack ready: {stack_name}")


@load.command("delete")
@click.option("--name", "-n", required=True, help="zae-limiter name")
@click.option("--region", default=None, help="AWS region")
@click.option("--yes", is_flag=True, help="Skip confirmation")
def delete(name: str, region: str | None, yes: bool) -> None:
    """Delete load test infrastructure."""
    stack_name = f"{name}-load"
    if not yes:
        click.confirm(f"Delete load test stack '{stack_name}'?", abort=True)

    click.echo(f"Deleting load test stack: {stack_name}")

    cfn = boto3.client("cloudformation", region_name=region)
    ecs = boto3.client("ecs", region_name=region)

    # Scale down service first
    try:
        ecs.update_service(cluster=stack_name, service=f"{stack_name}-master", desiredCount=0)
        click.echo("  Stopped Fargate tasks")
    except Exception as e:
        click.echo(f"  Warning: Failed to scale down service: {e}", err=True)

    # Delete stack
    cfn.delete_stack(StackName=stack_name)
    waiter = cfn.get_waiter("stack_delete_complete")
    click.echo("  Waiting for stack deletion...")
    waiter.wait(StackName=stack_name)
    click.echo("  Stack deleted")


@load.command("list")
@click.option("--region", default=None, help="AWS region")
def list_cmd(region: str | None) -> None:
    """List deployed load test stacks."""
    import asyncio
    from datetime import datetime

    from zae_limiter.infra.discovery import InfrastructureDiscovery

    async def _list() -> None:
        async with InfrastructureDiscovery(region=region) as discovery:
            limiters = await discovery.list_limiters(stack_type="load-test")

        if not limiters:
            click.echo()
            click.echo("No load test stacks found.")
            click.echo(f"  Region: {region or 'default'}")
            click.echo()
            click.echo("Deploy a load test with:")
            click.echo("  zae-limiter load deploy --name my-app")
            return

        click.echo()
        region_display = region or "default"
        click.echo(f"Load Test Stacks ({region_display})")
        click.echo()

        headers = ["Name", "Target", "Status", "Created"]
        rows: list[list[str]] = []
        for limiter in limiters:
            try:
                created = datetime.fromisoformat(limiter.creation_time)
                created_display = created.strftime("%Y-%m-%d")
            except Exception:
                created_display = "unknown"

            # Target is the user_name (limiter stack name)
            rows.append(
                [
                    limiter.stack_name,
                    limiter.user_name,
                    limiter.stack_status,
                    created_display,
                ]
            )

        from zae_limiter.visualization import TableRenderer

        renderer = TableRenderer()
        click.echo(renderer.render(headers, rows))

        click.echo()
        click.echo(f"Total: {len(limiters)} stack(s)")

    asyncio.run(_list())
