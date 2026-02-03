"""CLI commands for stress testing."""

from __future__ import annotations

import sys
from pathlib import Path

import boto3
import click


def _select_name(region: str | None) -> str:
    """Interactively select a zae-limiter instance."""
    import asyncio

    import questionary

    from zae_limiter.infra.discovery import InfrastructureDiscovery

    async def list_targets() -> list[str]:
        async with InfrastructureDiscovery(region=region) as discovery:
            limiters = await discovery.list_limiters()
            # Exclude stress stacks, only include healthy stacks
            return [
                info.stack_name
                for info in limiters
                if not info.stack_name.endswith("-stress")
                and info.stack_status in ("CREATE_COMPLETE", "UPDATE_COMPLETE")
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


@click.group()
def stress() -> None:
    """Stress testing commands for zae-limiter."""
    pass


@stress.command()
@click.option("--name", "-n", default=None, help="zae-limiter name")
@click.option("--region", default=None, help="AWS region")
@click.option("--vpc-id", default=None, help="VPC ID for stress test resources")
@click.option("--subnet-ids", default=None, help="Comma-separated private subnet IDs")
@click.option("--max-workers", default=100, type=int, help="Maximum Lambda worker concurrency")
@click.option(
    "--lambda-timeout",
    default=5,
    type=int,
    help="Lambda worker timeout in minutes (default: 5)",
)
@click.option(
    "--create-vpc-endpoints",
    is_flag=True,
    help="Create VPC endpoints for SSM (not needed if VPC has NAT gateway)",
)
def deploy(
    name: str | None,
    region: str | None,
    vpc_id: str | None,
    subnet_ids: str | None,
    max_workers: int,
    lambda_timeout: int,
    create_vpc_endpoints: bool,
) -> None:
    """Deploy stress test infrastructure."""
    from .builder import build_and_push_locust_image, get_zae_limiter_source
    from .lambda_builder import build_stress_lambda_package

    # Interactive prompts for missing options
    if not name:
        name = _select_name(region)
    if not vpc_id:
        vpc_id = _select_vpc(region)
    if not subnet_ids:
        subnet_ids = _select_subnets(region, vpc_id)

    stack_name = f"{name}-stress"
    click.echo(f"Deploying stress test stack: {stack_name}")

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

    required_outputs = ["AppPolicyArn", "AdminPolicyArn"]
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

    # Build parameters list
    stack_params = [
        {"ParameterKey": "TargetStackName", "ParameterValue": name},
        {"ParameterKey": "VpcId", "ParameterValue": vpc_id},
        {"ParameterKey": "PrivateSubnetIds", "ParameterValue": ",".join(subnet_list)},
        {"ParameterKey": "MaxWorkers", "ParameterValue": str(max_workers)},
        {"ParameterKey": "LambdaTimeout", "ParameterValue": str(lambda_timeout * 60)},
        {"ParameterKey": "CreateVpcEndpoints", "ParameterValue": str(create_vpc_endpoints).lower()},
        {"ParameterKey": "PermissionBoundary", "ParameterValue": permission_boundary},
        {"ParameterKey": "RoleNameFormat", "ParameterValue": role_name_format},
    ]

    try:
        cfn.create_stack(
            StackName=stack_name,
            TemplateBody=template_body,
            Parameters=stack_params,  # type: ignore[arg-type]
            Capabilities=["CAPABILITY_NAMED_IAM"],
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
    image_uri = build_and_push_locust_image(stack_name, region or "us-east-1", zae_limiter_source)
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
        func_name = f"{stack_name}-{func_suffix}"
        lambda_client.update_function_code(
            FunctionName=func_name,
            ZipFile=zip_bytes,
        )
        click.echo(f"  Lambda code uploaded: {func_name}")

    click.echo(f"\nStack ready: {stack_name}")


@stress.command()
@click.option("--name", "-n", required=True, help="zae-limiter stack name")
@click.option("--region", default=None, help="AWS region")
@click.option("--endpoint-url", default=None, help="AWS endpoint URL (for LocalStack)")
@click.option(
    "--custom-limits", default=300, type=int, help="Number of entities with custom limits"
)
@click.option("--apis", default=8, type=int, help="Number of APIs to configure")
def setup(
    name: str,
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
            name=name,
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
@click.option("--name", "-n", required=True, help="zae-limiter name")
@click.option("--region", default=None, help="AWS region")
@click.option("--port", default=8089, type=int, help="Local port for Locust UI")
def connect(name: str, region: str | None, port: int) -> None:
    """Start Fargate master, open SSM tunnel, and block until interrupted."""
    import json
    import subprocess
    import time

    stack_name = f"{name}-stress"
    ecs = boto3.client("ecs", region_name=region)
    service_name = f"{stack_name}-master"

    def scale_service(count: int) -> None:
        ecs.update_service(cluster=stack_name, service=service_name, desiredCount=count)

    def get_running_task(require_ssm: bool = False) -> tuple[str, str] | None:
        """Get (task_id, runtime_id) for the running task, or None.

        Args:
            require_ssm: If True, also check that ExecuteCommandAgent is running.
        """
        tasks = ecs.list_tasks(cluster=stack_name, serviceName=service_name)
        if not tasks["taskArns"]:
            return None

        task_arn = tasks["taskArns"][0]
        task_id = task_arn.split("/")[-1]

        task_details = ecs.describe_tasks(cluster=stack_name, tasks=[task_arn])
        task = task_details["tasks"][0]

        # Check if task is running and has runtime ID
        if task.get("lastStatus") != "RUNNING":
            return None

        runtime_id = None
        ssm_ready = False

        for container in task.get("containers", []):
            if container.get("name") == "locust-master":
                runtime_id = container.get("runtimeId")
                # Check managed agents for SSM readiness
                for agent in container.get("managedAgents", []):
                    if agent.get("name") == "ExecuteCommandAgent":
                        ssm_ready = agent.get("lastStatus") == "RUNNING"

        if not runtime_id:
            return None
        if require_ssm and not ssm_ready:
            return None

        return task_id, runtime_id

    started_by_us = False

    try:
        # Check if task is already running
        result = get_running_task()
        if result:
            task_id, runtime_id = result
            click.echo(f"Found running Fargate task: {task_id}")
        else:
            # Start Fargate task
            started_by_us = True
            click.echo(f"Starting Fargate master: {stack_name}")
            scale_service(1)

            # Wait for task to be running
            click.echo("  Waiting for task to start...")
            for _ in range(60):  # 2 minute timeout
                result = get_running_task()
                if result:
                    task_id, runtime_id = result
                    break
                time.sleep(2)
            else:
                click.echo("Error: Task failed to start within 2 minutes", err=True)
                scale_service(0)
                sys.exit(1)

            click.echo(f"  Task running: {task_id}")

        # Wait for SSM agent to be ready
        click.echo("  Waiting for SSM agent...")
        for _ in range(30):  # 1 minute timeout
            result = get_running_task(require_ssm=True)
            if result:
                break
            time.sleep(2)
        else:
            click.echo("Error: SSM agent failed to start", err=True)
            scale_service(0)
            sys.exit(1)

        click.echo("  SSM agent ready")
        time.sleep(3)  # Give SSM agent a moment to fully initialize

        # Build SSM target
        ssm_target = f"ecs:{stack_name}_{task_id}_{runtime_id}"
        params = json.dumps(
            {
                "host": ["localhost"],
                "portNumber": ["8089"],
                "localPortNumber": [str(port)],
            }
        )

        click.echo(f"  Connecting to http://localhost:{port} ...")

        # Start SSM tunnel with retries
        region_args = ["--region", region] if region else []
        ssm_cmd = [
            "aws",
            "ssm",
            "start-session",
            "--target",
            ssm_target,
            "--document-name",
            "AWS-StartPortForwardingSessionToRemoteHost",
            "--parameters",
            params,
            *region_args,
        ]

        for attempt in range(5):
            proc = subprocess.run(ssm_cmd)
            if proc.returncode == 0:
                break
            if attempt < 4:
                click.echo(f"  SSM connection failed, retrying ({attempt + 1}/5)...")
                time.sleep(5)
        else:
            click.echo("SSM tunnel failed after 5 attempts", err=True)

    except KeyboardInterrupt:
        click.echo("\nInterrupted")

    finally:
        # Only scale down if we started the task
        if started_by_us:
            click.echo("Stopping Fargate master...")
            try:
                scale_service(0)
                click.echo("  Fargate master stopped")
            except Exception as e:
                click.echo(f"  Warning: Failed to stop service: {e}", err=True)
        else:
            click.echo("Disconnected (Fargate task still running)")


@stress.command()
@click.option("--name", "-n", required=True, help="zae-limiter name")
@click.option("--region", default=None, help="AWS region")
@click.option("--yes", is_flag=True, help="Skip confirmation")
def teardown(name: str, region: str | None, yes: bool) -> None:
    """Delete stress test infrastructure."""
    stack_name = f"{name}-stress"
    if not yes:
        click.confirm(f"Delete stress test stack '{stack_name}'?", abort=True)

    click.echo(f"Deleting stress test stack: {stack_name}")

    cfn = boto3.client("cloudformation", region_name=region)
    ecs = boto3.client("ecs", region_name=region)

    # Scale down service first
    try:
        ecs.update_service(cluster=stack_name, service=f"{stack_name}-master", desiredCount=0)
        click.echo("  Stopped Fargate tasks")
    except Exception:
        pass

    # Delete stack
    cfn.delete_stack(StackName=stack_name)
    waiter = cfn.get_waiter("stack_delete_complete")
    click.echo("  Waiting for stack deletion...")
    waiter.wait(StackName=stack_name)
    click.echo("  Stack deleted")
