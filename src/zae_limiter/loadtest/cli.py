"""CLI commands for load testing."""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from mypy_boto3_cloudformation.type_defs import ParameterTypeDef

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


def _build_task_overrides(
    standalone: bool,
    locustfile: str | None,
    max_workers: int | None,
    min_workers: int | None,
    users_per_worker: int | None,
    startup_lead_time: int | None,
    cpu: int | None = None,
    memory: int | None = None,
    pool_connections: int | None = None,
) -> dict[str, Any]:
    """Build container overrides dict for run_task().

    Args:
        standalone: Run Locust in single-process mode (no workers).
        locustfile: Override locustfile path.
        max_workers: Override max Lambda workers.
        min_workers: Override minimum workers.
        users_per_worker: Override auto-scaling ratio.
        startup_lead_time: Override predictive scaling lookahead.
        cpu: Override task CPU units (e.g. 1024 for 1 vCPU).
        memory: Override task memory in MB.
        pool_connections: Override boto3 connection pool size.

    Returns:
        Dict suitable for run_task(overrides=...), or empty dict if no overrides.
    """
    master_env: list[dict[str, str]] = []
    orchestrator_env: list[dict[str, str]] = []

    # Locustfile override applies to both containers
    if locustfile:
        master_env.append({"name": "LOCUSTFILE", "value": locustfile})
        orchestrator_env.append({"name": "LOCUSTFILE", "value": locustfile})

    # Scaling parameters apply to orchestrator
    if max_workers is not None:
        orchestrator_env.append({"name": "MAX_WORKERS", "value": str(max_workers)})
    if min_workers is not None:
        orchestrator_env.append({"name": "MIN_WORKERS", "value": str(min_workers)})
    if users_per_worker is not None:
        orchestrator_env.append({"name": "USERS_PER_WORKER", "value": str(users_per_worker)})
    if startup_lead_time is not None:
        orchestrator_env.append({"name": "STARTUP_LEAD_TIME", "value": str(startup_lead_time)})

    # Connection pool override
    if pool_connections is not None:
        master_env.append({"name": "BOTO3_MAX_POOL", "value": str(pool_connections)})

    # Standalone mode: clear master args to run without --master
    if standalone:
        master_env.append({"name": "LOCUST_MASTER_ARGS", "value": ""})
        # Make orchestrator idle (no workers)
        orchestrator_env.append({"name": "MAX_WORKERS", "value": "0"})
        orchestrator_env.append({"name": "MIN_WORKERS", "value": "0"})

    # Build overrides dict only if we have overrides
    if not master_env and not orchestrator_env and not cpu and not memory:
        return {}

    result: dict[str, Any] = {}

    container_overrides = []

    # Master container
    if master_env:
        container_overrides.append({"name": "locust-master", "environment": master_env})

    # Orchestrator container
    if orchestrator_env:
        container_overrides.append(
            {
                "name": "worker-orchestrator",
                "environment": orchestrator_env,
            }
        )

    if container_overrides:
        result["containerOverrides"] = container_overrides

    # Task-level CPU/memory overrides
    if cpu is not None:
        result["cpu"] = str(cpu)
    if memory is not None:
        result["memory"] = str(memory)

    return result


def _display_benchmark_results(stats: dict[str, Any]) -> None:
    """Display benchmark results in a standard format.

    Args:
        stats: Dict with keys: total_requests, total_failures, failure_rate,
               requests_per_second, avg_response_time, min_response_time,
               max_response_time, p50, p95, p99.
    """
    total_reqs = stats.get("total_requests", 0)
    total_fails = stats.get("total_failures", 0)
    fail_rate = stats.get("failure_rate", 0)
    rps = stats.get("requests_per_second", 0)
    p50 = stats.get("p50", 0)
    p95 = stats.get("p95", 0)
    p99 = stats.get("p99", 0)
    avg_rt = stats.get("avg_response_time", 0)
    min_rt = stats.get("min_response_time", 0)
    max_rt = stats.get("max_response_time", 0)

    click.echo("\nResults:")
    click.echo(f"  Requests: {total_reqs:,}")
    click.echo(f"  RPS: {rps:.1f}")
    click.echo(f"  Failures: {total_fails:,} ({fail_rate:.1%})")
    click.echo(f"  Avg: {avg_rt:.0f}ms")
    click.echo(f"  Min: {min_rt:.0f}ms / Max: {max_rt:.0f}ms")
    click.echo(f"  p50: {p50:.0f}ms")
    click.echo(f"  p95: {p95:.0f}ms")
    click.echo(f"  p99: {p99:.0f}ms")


def _map_locust_stats(locust_json: dict[str, Any]) -> dict[str, Any]:
    """Map Locust /stats/requests JSON to standard benchmark stats format.

    Args:
        locust_json: Response from Locust's /stats/requests endpoint.

    Returns:
        Dict compatible with _display_benchmark_results().
    """
    # Find the "Aggregated" entry in stats (the total)
    total_entry: dict[str, Any] = {}
    for entry in locust_json.get("stats", []):
        if entry.get("name") == "Aggregated":
            total_entry = entry
            break

    if not total_entry:
        return {}

    num_requests = total_entry.get("num_requests", 0)
    num_failures = total_entry.get("num_failures", 0)
    fail_ratio = num_failures / num_requests if num_requests > 0 else 0.0

    # Percentiles are flat keys on the stats entry: "response_time_percentile_0.95"
    # p50 uses "median_response_time" (always present on the entry)
    return {
        "total_requests": num_requests,
        "total_failures": num_failures,
        "failure_rate": fail_ratio,
        "requests_per_second": locust_json.get("total_rps", 0),
        "avg_response_time": total_entry.get("avg_response_time", 0),
        "min_response_time": total_entry.get("min_response_time", 0),
        "max_response_time": total_entry.get("max_response_time", 0),
        "p50": total_entry.get("median_response_time", 0),
        "p95": total_entry.get("response_time_percentile_0.95", 0),
        "p99": total_entry.get("response_time_percentile_0.99", 0),
    }


def _get_service_network_config(ecs_client: Any, cluster: str, service: str) -> dict[str, Any]:
    """Get network configuration from an ECS service.

    Args:
        ecs_client: boto3 ECS client.
        cluster: ECS cluster name.
        service: ECS service name.

    Returns:
        Network configuration dict suitable for run_task().

    Raises:
        click.ClickException: If service is not found.
    """
    response = ecs_client.describe_services(cluster=cluster, services=[service])
    services = response.get("services", [])
    if not services:
        raise click.ClickException(f"Service not found: {service} in cluster {cluster}")
    service_config = services[0]
    return cast(dict[str, Any], service_config["networkConfiguration"])


@click.group()
def loadtest() -> None:
    """Load testing commands for zae-limiter."""
    pass


@loadtest.command()
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
    from .builder import get_zae_limiter_source

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
        resolved_region: str = ec2.meta.region_name
        route_table_ids = _discover_route_tables(ec2, subnet_list)
        if route_table_ids:
            # Check if a DynamoDB gateway endpoint already exists for this VPC
            existing = ec2.describe_vpc_endpoints(
                Filters=[
                    {"Name": "vpc-id", "Values": [vpc_id]},
                    {
                        "Name": "service-name",
                        "Values": [f"com.amazonaws.{resolved_region}.dynamodb"],
                    },
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
    stack_params: list[ParameterTypeDef] = [
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
            Parameters=stack_params,
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
                Parameters=stack_params,
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

    _push_code(stack_name, region, locustfile_dir, zae_limiter_source)

    click.echo(f"\nStack ready: {stack_name}")


def _push_code(
    stack_name: str,
    region: str | None,
    locustfile_dir: Path,
    zae_limiter_source: Path | str,
) -> None:
    """Build and push Locust image and Lambda code for a load test stack."""
    from .builder import build_and_push_locust_image
    from .lambda_builder import build_load_lambda_package

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


@loadtest.command()
@click.option("--name", "-n", default=None, help="zae-limiter name")
@click.option("--region", default=None, help="AWS region")
@click.option(
    "-C",
    "locustfile_dir",
    default=".",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Directory containing locustfiles (default: current directory)",
)
def push(
    name: str | None,
    region: str | None,
    locustfile_dir: Path,
) -> None:
    """Rebuild and push locustfiles and Lambda code."""
    from .builder import get_zae_limiter_source

    if not name:
        name = _select_name(region)

    stack_name = f"{name}-load"
    zae_limiter_source = get_zae_limiter_source()
    click.echo(f"Pushing code to {stack_name}")
    click.echo(f"  zae-limiter source: {zae_limiter_source}")

    _push_code(stack_name, region, locustfile_dir, zae_limiter_source)

    click.echo(f"\nCode pushed: {stack_name}")


@loadtest.command("ui")
@click.option("--name", "-n", required=True, help="zae-limiter name")
@click.option("--region", default=None, help="AWS region")
@click.option("--port", default=8089, type=int, help="Local port for Locust UI")
@click.option("--destroy", is_flag=True, help="Stop Fargate on disconnect even if already running")
@click.option("--force", is_flag=True, help="Stop existing task and restart with new config")
@click.option("--standalone", is_flag=True, help="Run Locust without workers (single-process mode)")
@click.option(
    "-f",
    "--locustfile",
    required=True,
    help="Locustfile path (relative to -C directory used in deploy)",
)
@click.option("--max-workers", type=int, default=None, help="Override max Lambda workers")
@click.option("--min-workers", type=int, default=None, help="Override minimum workers")
@click.option("--users-per-worker", type=int, default=None, help="Override users per worker ratio")
@click.option(
    "--startup-lead-time", type=int, default=None, help="Override predictive scaling lookahead"
)
@click.option(
    "--cpu", type=int, default=None, help="Override task CPU units (256, 512, 1024, 2048, 4096)"
)
@click.option("--memory", type=int, default=None, help="Override task memory in MB")
@click.option(
    "--pool-connections",
    type=int,
    default=None,
    help="Override boto3 connection pool size (default: 1000)",
)
def ui_cmd(
    name: str,
    region: str | None,
    port: int,
    destroy: bool,
    force: bool,
    standalone: bool,
    locustfile: str,
    max_workers: int | None,
    min_workers: int | None,
    users_per_worker: int | None,
    startup_lead_time: int | None,
    cpu: int | None,
    memory: int | None,
    pool_connections: int | None,
) -> None:
    """Open Locust web UI via SSM tunnel."""
    import json
    import subprocess
    import time

    stack_name = f"{name}-load"
    ecs = boto3.client("ecs", region_name=region)
    service_name = f"{stack_name}-master"
    task_definition = f"{stack_name}-master"

    # Build overrides from CLI options
    overrides = _build_task_overrides(
        standalone=standalone,
        locustfile=locustfile,
        max_workers=max_workers,
        min_workers=min_workers,
        users_per_worker=users_per_worker,
        startup_lead_time=startup_lead_time,
        cpu=cpu,
        memory=memory,
        pool_connections=pool_connections,
    )
    has_overrides = bool(overrides)

    def get_running_task(require_ssm: bool = False) -> tuple[str, str, str] | None:
        """Get (task_arn, task_id, runtime_id) for the running task, or None."""
        # Filter by task definition family (run_task tasks aren't service-associated)
        tasks = ecs.list_tasks(cluster=stack_name, family=task_definition)
        if not tasks["taskArns"]:
            return None

        task_arn = tasks["taskArns"][0]
        task_id = task_arn.split("/")[-1]

        task_details = ecs.describe_tasks(cluster=stack_name, tasks=[task_arn])
        task = task_details["tasks"][0]

        if task.get("lastStatus") != "RUNNING":
            return None

        runtime_id = None
        ssm_ready = False

        for container in task.get("containers", []):
            if container.get("name") == "locust-master":
                runtime_id = container.get("runtimeId")
                for agent in container.get("managedAgents", []):
                    if agent.get("name") == "ExecuteCommandAgent":
                        ssm_ready = agent.get("lastStatus") == "RUNNING"

        if not runtime_id:
            return None
        if require_ssm and not ssm_ready:
            return None

        return task_arn, task_id, runtime_id

    def start_task() -> str:
        """Start a new task with run_task(). Returns task ARN."""
        network_config = _get_service_network_config(ecs, stack_name, service_name)

        run_kwargs: dict[str, Any] = {
            "cluster": stack_name,
            "taskDefinition": task_definition,
            "networkConfiguration": network_config,
            "enableExecuteCommand": True,
            "count": 1,
        }
        if overrides:
            run_kwargs["overrides"] = overrides

        response = ecs.run_task(**run_kwargs)
        return cast(str, response["tasks"][0]["taskArn"])

    def stop_task(task_arn: str) -> None:
        """Stop a task."""
        ecs.stop_task(cluster=stack_name, task=task_arn)

    task_arn: str | None = None
    started_by_us = False

    try:
        # Check if task is already running
        result = get_running_task()
        if result:
            existing_task_arn, task_id, runtime_id = result

            if has_overrides and not force:
                click.echo(f"Found running Fargate task: {task_id}")
                click.echo("Warning: Task already running. Use --force to restart with new config.")
                click.echo("Connecting to existing task (overrides ignored)...")
                task_arn = existing_task_arn
            elif has_overrides and force:
                click.echo(f"Stopping existing task: {task_id}")
                stop_task(existing_task_arn)

                # Wait for old task to stop before starting new one
                click.echo("  Waiting for old task to stop...")
                for _ in range(30):
                    time.sleep(2)
                    if not get_running_task():
                        break
                else:
                    click.echo(
                        "Warning: Old task still running, starting new task anyway", err=True
                    )

                click.echo(f"Starting new Fargate task with overrides: {stack_name}")
                new_task_arn = start_task()
                started_by_us = True

                click.echo("  Waiting for task to start...")
                for _ in range(60):
                    result = get_running_task()
                    if result:
                        task_arn, task_id, runtime_id = result
                        # Verify we got the new task, not the old one still stopping
                        if task_arn == new_task_arn:
                            break
                    time.sleep(2)
                else:
                    click.echo("Error: Task failed to start within 2 minutes", err=True)
                    sys.exit(1)

                click.echo(f"  Task running: {task_id}")
            else:
                click.echo(f"Found running Fargate task: {task_id}")
                task_arn = existing_task_arn
        else:
            started_by_us = True
            click.echo(f"Starting Fargate master: {stack_name}")
            task_arn = start_task()

            click.echo("  Waiting for task to start...")
            for _ in range(60):
                result = get_running_task()
                if result:
                    task_arn, task_id, runtime_id = result
                    break
                time.sleep(2)
            else:
                click.echo("Error: Task failed to start within 2 minutes", err=True)
                if task_arn:
                    stop_task(task_arn)
                sys.exit(1)

            click.echo(f"  Task running: {task_id}")

        click.echo("  Waiting for SSM agent...")
        for _ in range(30):
            result = get_running_task(require_ssm=True)
            if result:
                break
            time.sleep(2)
        else:
            click.echo("Error: SSM agent failed to start", err=True)
            if started_by_us and task_arn:
                stop_task(task_arn)
            sys.exit(1)

        click.echo("  SSM agent ready")
        time.sleep(3)

        ssm_target = f"ecs:{stack_name}_{task_id}_{runtime_id}"
        params = json.dumps(
            {
                "host": ["localhost"],
                "portNumber": ["8089"],
                "localPortNumber": [str(port)],
            }
        )

        click.echo(f"  Connecting to http://localhost:{port} ...")

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
        if (started_by_us or destroy) and task_arn:
            click.echo("Stopping Fargate task...")
            try:
                stop_task(task_arn)
                click.echo("  Waiting for task to stop...")
                waiter = ecs.get_waiter("tasks_stopped")
                waiter.wait(cluster=stack_name, tasks=[task_arn])
                click.echo("  Fargate task stopped")
            except Exception as e:
                click.echo(f"  Warning: Failed to stop task: {e}", err=True)
        elif task_arn:
            click.echo("Disconnected (Fargate task still running, use --destroy to stop)")


@loadtest.command()
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


@loadtest.command("list")
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
            click.echo("  zae-limiter loadtest deploy --name my-app")
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


@loadtest.command("run")
@click.option("--name", "-n", required=True, help="zae-limiter name")
@click.option("--region", default=None, help="AWS region")
@click.option("--users", default=20, type=int, help="Number of simulated users (default: 20)")
@click.option("--duration", default=60, type=int, help="Test duration in seconds (default: 60)")
@click.option("--spawn-rate", default=10, type=int, help="User spawn rate per second (default: 10)")
@click.option(
    "-f",
    "--locustfile",
    required=True,
    help="Locustfile path (e.g. locustfiles/max_rps.py)",
)
@click.option(
    "--cpu", type=int, default=None, help="Override Fargate task CPU units (default from task def)"
)
@click.option(
    "--memory",
    type=int,
    default=None,
    help="Override Fargate task memory in MB (default from task def)",
)
@click.option("--port", default=8089, type=int, help="Local port for SSM tunnel (Fargate mode)")
@click.option(
    "--workers",
    default=None,
    type=int,
    help="Number of Lambda workers (implies distributed mode)",
)
@click.option(
    "--standalone",
    is_flag=True,
    hidden=True,
    help="Run Locust in single-process Fargate mode",
)
@click.option(
    "--user-classes",
    default=None,
    help="Comma-separated User class names to run (e.g. MaxRpsCascadeUser)",
)
def run_cmd(
    name: str,
    region: str | None,
    users: int,
    duration: int,
    spawn_rate: int,
    locustfile: str,
    cpu: int | None,
    memory: int | None,
    port: int,
    workers: int | None,
    standalone: bool,
    user_classes: str | None,
) -> None:
    """Run a single load test execution.

    Lambda mode (default): Invokes a single Lambda worker in headless mode.
    Use --workers N to run distributed (Fargate master + Lambda workers).
    """
    if workers is not None:
        _benchmark_distributed(
            name=name,
            region=region,
            users=users,
            duration=duration,
            spawn_rate=spawn_rate,
            locustfile=locustfile,
            cpu=cpu,
            memory=memory,
            port=port,
            workers=workers,
            user_classes=user_classes,
        )
    elif standalone:
        _benchmark_fargate(
            name=name,
            region=region,
            users=users,
            duration=duration,
            spawn_rate=spawn_rate,
            locustfile=locustfile,
            cpu=cpu,
            memory=memory,
            port=port,
            user_classes=user_classes,
        )
    else:
        _run_lambda(
            name=name,
            region=region,
            users=users,
            duration=duration,
            spawn_rate=spawn_rate,
            locustfile=locustfile,
            user_classes=user_classes,
        )


@loadtest.command("tune")
@click.option("--name", "-n", required=True, help="zae-limiter name")
@click.option("--region", default=None, help="AWS region")
@click.option(
    "-f",
    "--locustfile",
    required=True,
    help="Locustfile path (e.g. locustfiles/max_rps.py)",
)
@click.option(
    "--user-classes",
    default=None,
    help="Comma-separated User class names to run (e.g. MaxRpsCascadeUser)",
)
@click.option(
    "--max-users",
    default=40,
    type=int,
    help="Upper bound for binary search (default: 40)",
)
@click.option(
    "--threshold",
    default=0.80,
    type=float,
    help="Target efficiency ratio (default: 0.80)",
)
@click.option(
    "--step-duration",
    default=30,
    type=int,
    help="Seconds per tuning step (default: 30)",
)
@click.option(
    "--baseline-duration",
    default=60,
    type=int,
    help="Seconds for baseline phase (default: 60)",
)
@click.option(
    "--spawn-rate",
    default=10,
    type=int,
    help="User spawn rate per second (default: 10)",
)
def tune(
    name: str,
    region: str | None,
    locustfile: str,
    user_classes: str | None,
    max_users: int,
    threshold: float,
    step_duration: int,
    baseline_duration: int,
    spawn_rate: int,
) -> None:
    """Find optimal per-worker user count via binary search.

    Uses Little's Law to binary-search for the optimal per-worker user count
    by measuring efficiency (baseline_p50 / observed_p50) at different
    concurrency levels. Lambda-only.
    """
    _tune_lambda(
        name=name,
        region=region,
        locustfile=locustfile,
        user_classes=user_classes,
        max_users=max_users,
        threshold=threshold,
        step_duration=step_duration,
        baseline_duration=baseline_duration,
        spawn_rate=spawn_rate,
    )


def _invoke_lambda_headless(
    lambda_client: Any,
    func_name: str,
    users: int,
    duration: int,
    spawn_rate: int,
    locustfile: str,
    user_classes: str | None = None,
) -> dict[str, Any]:
    """Invoke Lambda in headless mode and return stats dict.

    Raises click.ClickException on Lambda error.
    """
    import json

    config: dict[str, Any] = {
        "mode": "headless",
        "users": users,
        "duration_seconds": duration,
        "spawn_rate": spawn_rate,
        "locustfile": locustfile,
    }
    if user_classes:
        config["user_classes"] = user_classes
    payload = {"config": config}

    response = lambda_client.invoke(
        FunctionName=func_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload),
    )

    response_payload = json.loads(response["Payload"].read())

    if "errorMessage" in response_payload:
        msg = response_payload["errorMessage"]
        trace = response_payload.get("stackTrace", [])
        detail = "\n  ".join(str(line) for line in trace)
        raise click.ClickException(f"Lambda execution failed: {msg}\n  {detail}".rstrip())

    return cast(dict[str, Any], response_payload)


def _get_lambda_client_and_config(name: str, region: str | None) -> tuple[Any, str, int, int]:
    """Get Lambda client (with extended timeout) and function config.

    Returns (lambda_client, func_name, memory_mb, timeout_seconds).
    """
    from botocore.config import Config

    stack_name = f"{name}-load"
    func_name = f"{stack_name}-worker"

    lambda_client = boto3.client("lambda", region_name=region)

    try:
        func_config = lambda_client.get_function_configuration(FunctionName=func_name)
    except lambda_client.exceptions.ResourceNotFoundException:
        click.echo(f"Error: Lambda function not found: {func_name}", err=True)
        click.echo("Deploy first with: zae-limiter loadtest deploy --name ...", err=True)
        sys.exit(1)

    memory_mb: int = func_config["MemorySize"]
    timeout_seconds: int = func_config["Timeout"]

    # Recreate client with read timeout matching Lambda timeout + buffer
    lambda_client = boto3.client(
        "lambda",
        region_name=region,
        config=Config(read_timeout=timeout_seconds + 30),
    )

    return lambda_client, func_name, memory_mb, timeout_seconds


def _run_lambda(
    name: str,
    region: str | None,
    users: int,
    duration: int,
    spawn_rate: int,
    locustfile: str,
    user_classes: str | None = None,
) -> None:
    """Run Lambda benchmark (single invocation)."""
    lambda_client, func_name, memory_mb, timeout_seconds = _get_lambda_client_and_config(
        name, region
    )

    vcpu_estimate = memory_mb / 1769

    # Warn if duration exceeds available time (with 10% buffer)
    effective_timeout = int(timeout_seconds * 0.9)
    if duration > effective_timeout:
        click.echo(
            f"Warning: duration ({duration}s) exceeds Lambda effective timeout "
            f"({effective_timeout}s with 10% buffer). Test will stop early.",
            err=True,
        )

    click.echo(f"\nLambda Run: {func_name}")
    click.echo(f"  Memory: {memory_mb} MB (~{vcpu_estimate:.2f} vCPU)")
    click.echo(f"  Timeout: {timeout_seconds}s")
    click.echo(f"  Locustfile: {locustfile}")
    click.echo(f"  Users: {users}, Duration: {duration}s, Spawn Rate: {spawn_rate}/s")
    click.echo()

    click.echo("Invoking Lambda (this may take a while)...")
    try:
        stats = _invoke_lambda_headless(
            lambda_client=lambda_client,
            func_name=func_name,
            users=users,
            duration=duration,
            spawn_rate=spawn_rate,
            locustfile=locustfile,
            user_classes=user_classes,
        )
    except click.ClickException as e:
        click.echo(f"\nError: {e.format_message()}", err=True)
        sys.exit(1)

    _display_benchmark_results(stats)


def _tune_lambda(
    name: str,
    region: str | None,
    locustfile: str,
    user_classes: str | None,
    max_users: int,
    threshold: float,
    step_duration: int,
    baseline_duration: int,
    spawn_rate: int,
) -> None:
    """Run binary search calibration to find optimal per-worker user count."""
    lambda_client, func_name, memory_mb, timeout_seconds = _get_lambda_client_and_config(
        name, region
    )

    vcpu_estimate = memory_mb / 1769

    click.echo(f"\nLambda Tune: {func_name}")
    click.echo(f"  Memory: {memory_mb} MB (~{vcpu_estimate:.2f} vCPU)")
    click.echo(f"  Timeout: {timeout_seconds}s")
    click.echo(f"  Locustfile: {locustfile}")
    click.echo(f"  Threshold: {threshold:.0%}")
    click.echo(f"  Search range: [1, {max_users}]")
    click.echo()

    # Minimum requests for stable percentiles
    min_requests = 100

    # Collect calibration data: list of {users, rps, p50, efficiency, requests}
    steps: list[dict[str, Any]] = []

    def run_step(users: int, duration: int, label: str) -> dict[str, Any]:
        click.echo(f"  [{label}] Invoking with {users} user(s) for {duration}s...")
        try:
            stats = _invoke_lambda_headless(
                lambda_client=lambda_client,
                func_name=func_name,
                users=users,
                duration=duration,
                spawn_rate=spawn_rate,
                locustfile=locustfile,
                user_classes=user_classes,
            )
        except click.ClickException as e:
            click.echo(f"\nError: {e.format_message()}", err=True)
            sys.exit(1)
        return stats

    # Step 1: Baseline with 1 user (auto-extend if too few requests)
    max_baseline_duration = timeout_seconds - 30  # leave headroom for Lambda overhead
    current_baseline_duration = baseline_duration
    while True:
        baseline_stats = run_step(1, current_baseline_duration, "baseline")
        baseline_reqs = int(baseline_stats.get("total_requests", 0))
        baseline_p50 = float(baseline_stats.get("p50", 0))
        baseline_rps = float(baseline_stats.get("requests_per_second", 0))

        if baseline_reqs >= min_requests:
            break

        if baseline_reqs > 0:
            # Extrapolate: how long to reach min_requests at observed rate
            multiplier = math.ceil(min_requests / baseline_reqs)
            new_duration = current_baseline_duration * multiplier
        else:
            new_duration = current_baseline_duration * 2
        new_duration = min(new_duration, max_baseline_duration)
        if new_duration <= current_baseline_duration:
            click.echo(
                f"  Baseline only {baseline_reqs} requests in {current_baseline_duration}s "
                f"(cannot extend further)."
            )
            break
        click.echo(
            f"  Baseline too few requests ({baseline_reqs} < {min_requests}), "
            f"retrying with {new_duration}s..."
        )
        current_baseline_duration = new_duration

    if baseline_p50 <= 0:
        click.echo("Error: Baseline p50 is zero — cannot compute efficiency.", err=True)
        sys.exit(1)

    baseline_p95 = float(baseline_stats.get("p95", 0))
    baseline_p99 = float(baseline_stats.get("p99", 0))
    steps.append(
        {
            "users": 1,
            "rps": baseline_rps,
            "p50": baseline_p50,
            "p95": baseline_p95,
            "p99": baseline_p99,
            "efficiency": 1.0,
            "requests": baseline_reqs,
        }
    )
    # Use the (possibly auto-extended) baseline duration for all steps
    step_duration = current_baseline_duration

    click.echo(
        f"  Baseline: p50={baseline_p50:.0f}ms, p95={baseline_p95:.0f}ms, "
        f"p99={baseline_p99:.0f}ms, RPS={baseline_rps:.1f}, "
        f"duration={step_duration}s, requests={baseline_reqs}"
    )

    # Step 2: Upper bound check
    upper_stats = run_step(max_users, step_duration, "upper")
    upper_reqs = int(upper_stats.get("total_requests", 0))
    upper_p50 = float(upper_stats.get("p50", 0))
    upper_p95 = float(upper_stats.get("p95", 0))
    upper_p99 = float(upper_stats.get("p99", 0))
    upper_rps = float(upper_stats.get("requests_per_second", 0))
    upper_eff = baseline_p50 / upper_p50 if upper_p50 > 0 else 0.0
    steps.append(
        {
            "users": max_users,
            "rps": upper_rps,
            "p50": upper_p50,
            "p95": upper_p95,
            "p99": upper_p99,
            "efficiency": upper_eff,
            "requests": upper_reqs,
        }
    )
    click.echo(
        f"  Upper ({max_users} users): p50={upper_p50:.0f}ms, p95={upper_p95:.0f}ms, "
        f"p99={upper_p99:.0f}ms, RPS={upper_rps:.1f}, efficiency={upper_eff:.0%}, "
        f"requests={upper_reqs}"
    )

    if upper_eff >= threshold:
        click.echo(f"\n  Efficiency >= {threshold:.0%} even at max_users={max_users}.")
        optimal_users = max_users
        optimal_rps = upper_rps
    else:
        # Step 3: Weighted bisection between [1, max_users]
        # Use linear interpolation of efficiency to pick the midpoint
        low = 1
        high = max_users
        eff_low = 1.0  # baseline efficiency
        eff_high = upper_eff
        optimal_users = 1
        optimal_rps = baseline_rps

        while high - low > 1:
            # Interpolate: where does threshold fall between eff_low and eff_high?
            # eff_low >= threshold > eff_high, so the optimal point is closer to low.
            # t=0 at low (eff_low), t=1 at high (eff_high).
            if eff_low != eff_high:
                t = (eff_low - threshold) / (eff_low - eff_high)
                mid = low + round(t * (high - low))
                mid = max(low + 1, min(mid, high - 1))
            else:
                mid = (low + high) // 2
            mid_stats = run_step(mid, step_duration, "search")
            mid_reqs = int(mid_stats.get("total_requests", 0))
            mid_p50 = float(mid_stats.get("p50", 0))
            mid_p95 = float(mid_stats.get("p95", 0))
            mid_p99 = float(mid_stats.get("p99", 0))
            mid_rps = float(mid_stats.get("requests_per_second", 0))
            mid_eff = baseline_p50 / mid_p50 if mid_p50 > 0 else 0.0
            steps.append(
                {
                    "users": mid,
                    "rps": mid_rps,
                    "p50": mid_p50,
                    "p95": mid_p95,
                    "p99": mid_p99,
                    "efficiency": mid_eff,
                    "requests": mid_reqs,
                }
            )
            click.echo(
                f"  Search ({mid} users): p50={mid_p50:.0f}ms, p95={mid_p95:.0f}ms, "
                f"p99={mid_p99:.0f}ms, RPS={mid_rps:.1f}, efficiency={mid_eff:.0%}, "
                f"requests={mid_reqs}"
            )

            if mid_eff >= threshold:
                low = mid
                eff_low = mid_eff
                optimal_users = mid
                optimal_rps = mid_rps
            else:
                high = mid
                eff_high = mid_eff

    click.echo()
    _display_calibration_results(
        steps=steps,
        baseline_p50=baseline_p50,
        optimal_users=optimal_users,
        optimal_rps=optimal_rps,
        threshold=threshold,
    )


def _display_calibration_results(
    steps: list[dict[str, Any]],
    baseline_p50: float,
    optimal_users: int,
    optimal_rps: float,
    threshold: float,
) -> None:
    """Display calibration table and distributed recommendations."""
    import math

    click.echo(f"Calibration Results (threshold: {threshold:.0%}):")
    click.echo(
        f"  {'Users':>5}  {'RPS':>7}  {'p50':>7}  {'p95':>7}  {'p99':>7}"
        f"  {'Reqs':>7}  {'Efficiency':>10}"
    )

    for step in steps:
        users = step["users"]
        rps = float(step.get("rps", 0))
        p50 = float(step.get("p50", 0))
        p95 = float(step.get("p95", 0))
        p99 = float(step.get("p99", 0))
        reqs = int(step.get("requests", 0))
        eff = float(step.get("efficiency", 0))
        marker = ""
        if users == 1:
            marker = " (baseline)"
        elif users == optimal_users and optimal_users != 1:
            marker = f" <- optimal (>= {threshold:.0%})"
        click.echo(
            f"  {users:>5}  {rps:>7.1f}  {p50:>5.0f}ms  {p95:>5.0f}ms  {p99:>5.0f}ms"
            f"  {reqs:>7,}  {eff:>9.0%}{marker}"
        )

    # Find baseline percentiles for the summary
    baseline_step = next(s for s in steps if s["users"] == 1)
    baseline_p95 = float(baseline_step.get("p95", 0))
    baseline_p99 = float(baseline_step.get("p99", 0))

    click.echo()
    click.echo(f"Optimal: {optimal_users} users per worker")
    click.echo(
        f"  Floor latency: p50={baseline_p50:.0f}ms, "
        f"p95={baseline_p95:.0f}ms, p99={baseline_p99:.0f}ms"
    )
    click.echo(f"  Throughput per worker: {optimal_rps:.1f} RPS")

    click.echo()
    click.echo("Distributed recommendations:")
    for target_users in [100, 500, 1000]:
        workers = math.ceil(target_users / optimal_users)
        click.echo(
            f"  {target_users} users:  loadtest run --workers {workers} --users {target_users}"
        )


def _benchmark_fargate(
    name: str,
    region: str | None,
    users: int,
    duration: int,
    spawn_rate: int,
    locustfile: str,
    cpu: int | None,
    memory: int | None,
    port: int,
    user_classes: str | None = None,
) -> None:
    """Run Fargate benchmark in standalone headless mode."""
    import json
    import subprocess
    import time
    import urllib.error
    import urllib.request

    stack_name = f"{name}-load"
    service_name = f"{stack_name}-master"
    task_definition = f"{stack_name}-master"

    ecs = boto3.client("ecs", region_name=region)

    # Build autostart args for Locust master container
    # Use --autostart (not --headless) to keep the web UI running on 8089
    # so we can poll /stats/requests via SSM tunnel. --autoquit 5 exits 5s
    # after the test finishes.
    headless_args = (
        f"--autostart --autoquit 5 --users {users} --run-time {duration}s --spawn-rate {spawn_rate}"
    )
    master_env: list[dict[str, str]] = [
        {"name": "LOCUST_MASTER_ARGS", "value": headless_args},
        {"name": "LOCUSTFILE", "value": locustfile},
    ]

    # Orchestrator should be idle (no workers in standalone mode)
    orchestrator_env: list[dict[str, str]] = [
        {"name": "MAX_WORKERS", "value": "0"},
        {"name": "MIN_WORKERS", "value": "0"},
    ]

    overrides: dict[str, Any] = {
        "containerOverrides": [
            {"name": "locust-master", "environment": master_env},
            {"name": "worker-orchestrator", "environment": orchestrator_env},
        ],
    }
    if cpu is not None:
        overrides["cpu"] = str(cpu)
    if memory is not None:
        overrides["memory"] = str(memory)

    # Resolve task def to get default CPU/memory for header display
    try:
        td_response = ecs.describe_task_definition(taskDefinition=task_definition)
        td = td_response["taskDefinition"]
        display_cpu = cpu or int(td.get("cpu", 1024))
        display_memory = memory or int(td.get("memory", 2048))
    except Exception:
        display_cpu = cpu or 1024
        display_memory = memory or 2048

    display_vcpu = display_cpu / 1024

    click.echo(f"\nFargate Benchmark: {service_name}")
    click.echo(f"  CPU: {display_cpu} units ({display_vcpu:.1f} vCPU)")
    click.echo(f"  Memory: {display_memory} MB")
    click.echo(f"  Locustfile: {locustfile}")
    click.echo(f"  Users: {users}, Duration: {duration}s, Spawn Rate: {spawn_rate}/s")
    click.echo()

    # Get network config from service
    network_config = _get_service_network_config(ecs, stack_name, service_name)

    # Start Fargate task
    click.echo("Starting Fargate task...")
    run_response = ecs.run_task(
        cluster=stack_name,
        taskDefinition=task_definition,
        networkConfiguration=network_config,
        enableExecuteCommand=True,
        count=1,
        overrides=overrides,
    )
    if not run_response.get("tasks"):
        failures = run_response.get("failures", [])
        reason = failures[0].get("reason", "unknown") if failures else "unknown"
        click.echo(f"Error: Failed to start Fargate task: {reason}", err=True)
        sys.exit(1)
    task_arn = run_response["tasks"][0]["taskArn"]
    task_id = task_arn.split("/")[-1]
    click.echo(f"  Task started: {task_id}")

    tunnel_proc: subprocess.Popen[bytes] | None = None
    last_good_stats: dict[str, Any] | None = None

    try:
        # Wait for task RUNNING + SSM agent
        click.echo("  Waiting for task to start...")
        runtime_id = None
        for _ in range(60):
            time.sleep(2)
            tasks = ecs.describe_tasks(cluster=stack_name, tasks=[task_arn])
            task = tasks["tasks"][0]
            if task.get("lastStatus") != "RUNNING":
                continue

            for container in task.get("containers", []):
                if container.get("name") == "locust-master":
                    runtime_id = container.get("runtimeId")
                    ssm_ready = False
                    for agent in container.get("managedAgents", []):
                        if agent.get("name") == "ExecuteCommandAgent":
                            ssm_ready = agent.get("lastStatus") == "RUNNING"
                    if runtime_id and ssm_ready:
                        break
            else:
                continue
            break
        else:
            click.echo("Error: Task failed to start within 2 minutes", err=True)
            ecs.stop_task(cluster=stack_name, task=task_arn)
            sys.exit(1)

        click.echo(f"  Task running: {task_id}")
        click.echo("  SSM agent ready")
        time.sleep(3)

        # Open SSM tunnel in background
        ssm_target = f"ecs:{stack_name}_{task_id}_{runtime_id}"
        params = json.dumps(
            {
                "host": ["localhost"],
                "portNumber": ["8089"],
                "localPortNumber": [str(port)],
            }
        )
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

        click.echo(f"  Opening SSM tunnel to localhost:{port}...")
        tunnel_proc = subprocess.Popen(
            ssm_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        # Wait for tunnel to be ready
        stats_url = f"http://localhost:{port}/stats/requests"
        click.echo("  Waiting for tunnel + Locust...")
        tunnel_ready = False
        for attempt in range(30):
            time.sleep(2)
            try:
                req = urllib.request.Request(stats_url)
                with urllib.request.urlopen(req, timeout=3) as resp:
                    json.loads(resp.read())
                tunnel_ready = True
                break
            except (urllib.error.URLError, ConnectionRefusedError, OSError):
                if tunnel_proc.poll() is not None:
                    # Tunnel process died, restart it
                    tunnel_proc = subprocess.Popen(
                        ssm_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                continue

        if not tunnel_ready:
            click.echo("Error: Could not connect to Locust via SSM tunnel", err=True)
            sys.exit(1)

        click.echo("  Connected. Polling stats...\n")

        # Poll stats until test completes
        poll_interval = 5
        consecutive_failures = 0
        max_consecutive_failures = 3
        # Extra buffer: wait up to duration + ramp-up time + buffer
        max_wait = duration + (users // max(spawn_rate, 1)) + 30

        elapsed = 0
        while elapsed < max_wait:
            time.sleep(poll_interval)
            elapsed += poll_interval

            try:
                req = urllib.request.Request(stats_url)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    stats_json = json.loads(resp.read())
                consecutive_failures = 0
                last_good_stats = stats_json

                state = stats_json.get("state", "unknown")
                total_rps = stats_json.get("total_rps", 0)
                user_count = stats_json.get("user_count", 0)
                click.echo(f"  [{elapsed}s] state={state}, users={user_count}, rps={total_rps:.1f}")

                if state == "stopped":
                    click.echo("  Test completed.")
                    break

            except (urllib.error.URLError, ConnectionRefusedError, OSError):
                consecutive_failures += 1
                if consecutive_failures >= max_consecutive_failures:
                    click.echo("  Locust stopped (connection lost).")
                    break
                click.echo(f"  [{elapsed}s] connection lost, retrying...")

        if last_good_stats:
            mapped = _map_locust_stats(last_good_stats)
            if mapped:
                _display_benchmark_results(mapped)
            else:
                click.echo("\nNo aggregated stats available.", err=True)
        else:
            click.echo("\nError: No stats collected.", err=True)
            sys.exit(1)

    except KeyboardInterrupt:
        click.echo("\nInterrupted")
        if last_good_stats:
            mapped = _map_locust_stats(last_good_stats)
            if mapped:
                click.echo("\nPartial results:")
                _display_benchmark_results(mapped)

    finally:
        # Clean up tunnel
        if tunnel_proc and tunnel_proc.poll() is None:
            tunnel_proc.terminate()
            try:
                tunnel_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tunnel_proc.kill()

        # Wait for task to stop (it should stop naturally after --run-time)
        click.echo("\nWaiting for Fargate task to stop...")
        try:
            waiter = ecs.get_waiter("tasks_stopped")
            waiter.wait(
                cluster=stack_name,
                tasks=[task_arn],
                WaiterConfig={"Delay": 5, "MaxAttempts": 24},
            )
            click.echo("  Fargate task stopped.")
        except Exception:
            # If it's taking too long, stop it explicitly
            click.echo("  Task still running, stopping...")
            try:
                ecs.stop_task(cluster=stack_name, task=task_arn)
                click.echo("  Fargate task stopped.")
            except Exception as e:
                click.echo(f"  Warning: Failed to stop task: {e}", err=True)


def _benchmark_distributed(
    name: str,
    region: str | None,
    users: int,
    duration: int,
    spawn_rate: int,
    locustfile: str,
    cpu: int | None,
    memory: int | None,
    port: int,
    workers: int,
    user_classes: str | None = None,
) -> None:
    """Run distributed benchmark: Fargate master + Lambda workers."""
    import json
    import subprocess
    import time
    import urllib.error
    import urllib.parse
    import urllib.request

    stack_name = f"{name}-load"
    service_name = f"{stack_name}-master"
    task_definition = f"{stack_name}-master"

    ecs = boto3.client("ecs", region_name=region)

    # Build overrides: master keeps default --master args, orchestrator gets worker params
    overrides = _build_task_overrides(
        standalone=False,
        locustfile=locustfile,
        max_workers=workers,
        min_workers=workers,
        users_per_worker=None,
        startup_lead_time=None,
        cpu=cpu,
        memory=memory,
    )

    # Resolve task def to get default CPU/memory for header display
    try:
        td_response = ecs.describe_task_definition(taskDefinition=task_definition)
        td = td_response["taskDefinition"]
        display_cpu = cpu or int(td.get("cpu", 1024))
        display_memory = memory or int(td.get("memory", 2048))
    except Exception:
        display_cpu = cpu or 1024
        display_memory = memory or 2048

    display_vcpu = display_cpu / 1024

    click.echo(f"\nDistributed Benchmark: {service_name}")
    click.echo(f"  CPU: {display_cpu} units ({display_vcpu:.1f} vCPU)")
    click.echo(f"  Memory: {display_memory} MB")
    click.echo(f"  Workers: {workers} Lambda")
    click.echo(f"  Locustfile: {locustfile}")
    click.echo(f"  Users: {users}, Duration: {duration}s, Spawn Rate: {spawn_rate}/s")
    click.echo()

    # Get network config from service
    network_config = _get_service_network_config(ecs, stack_name, service_name)

    # Start Fargate task
    click.echo("Starting Fargate task...")
    run_kwargs: dict[str, Any] = {
        "cluster": stack_name,
        "taskDefinition": task_definition,
        "networkConfiguration": network_config,
        "enableExecuteCommand": True,
        "count": 1,
    }
    if overrides:
        run_kwargs["overrides"] = overrides

    run_response = ecs.run_task(**run_kwargs)
    if not run_response.get("tasks"):
        failures = run_response.get("failures", [])
        reason = failures[0].get("reason", "unknown") if failures else "unknown"
        click.echo(f"Error: Failed to start Fargate task: {reason}", err=True)
        sys.exit(1)
    task_arn = run_response["tasks"][0]["taskArn"]
    task_id = task_arn.split("/")[-1]
    click.echo(f"  Task started: {task_id}")

    tunnel_proc: subprocess.Popen[bytes] | None = None
    last_good_stats: dict[str, Any] | None = None

    try:
        # Wait for task RUNNING + SSM agent
        click.echo("  Waiting for task to start...")
        runtime_id = None
        for _ in range(60):
            time.sleep(2)
            tasks = ecs.describe_tasks(cluster=stack_name, tasks=[task_arn])
            task = tasks["tasks"][0]
            if task.get("lastStatus") != "RUNNING":
                continue

            for container in task.get("containers", []):
                if container.get("name") == "locust-master":
                    runtime_id = container.get("runtimeId")
                    ssm_ready = False
                    for agent in container.get("managedAgents", []):
                        if agent.get("name") == "ExecuteCommandAgent":
                            ssm_ready = agent.get("lastStatus") == "RUNNING"
                    if runtime_id and ssm_ready:
                        break
            else:
                continue
            break
        else:
            click.echo("Error: Task failed to start within 2 minutes", err=True)
            ecs.stop_task(cluster=stack_name, task=task_arn)
            sys.exit(1)

        click.echo(f"  Task running: {task_id}")
        click.echo("  SSM agent ready")
        time.sleep(3)

        # Open SSM tunnel in background
        ssm_target = f"ecs:{stack_name}_{task_id}_{runtime_id}"
        params = json.dumps(
            {
                "host": ["localhost"],
                "portNumber": ["8089"],
                "localPortNumber": [str(port)],
            }
        )
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

        click.echo(f"  Opening SSM tunnel to localhost:{port}...")
        tunnel_proc = subprocess.Popen(
            ssm_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        # Wait for tunnel to be ready
        stats_url = f"http://localhost:{port}/stats/requests"
        click.echo("  Waiting for tunnel + Locust...")
        tunnel_ready = False
        for _ in range(30):
            time.sleep(2)
            try:
                req = urllib.request.Request(stats_url)
                with urllib.request.urlopen(req, timeout=3) as resp:
                    json.loads(resp.read())
                tunnel_ready = True
                break
            except (urllib.error.URLError, ConnectionRefusedError, OSError):
                if tunnel_proc.poll() is not None:
                    tunnel_proc = subprocess.Popen(
                        ssm_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                continue

        if not tunnel_ready:
            click.echo("Error: Could not connect to Locust via SSM tunnel", err=True)
            sys.exit(1)

        click.echo("  Connected.")

        # Wait for workers to connect
        click.echo(f"  Waiting for {workers} worker(s)...")
        workers_ready = False
        for i in range(60):
            time.sleep(5)
            elapsed_w = (i + 1) * 5
            try:
                req = urllib.request.Request(stats_url)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    stats_json = json.loads(resp.read())
                worker_list = stats_json.get("workers", [])
                worker_count = len(worker_list) if isinstance(worker_list, list) else 0
                click.echo(f"  [{elapsed_w}s] workers={worker_count}/{workers}")
                if worker_count >= workers:
                    workers_ready = True
                    break
            except (urllib.error.URLError, ConnectionRefusedError, OSError):
                click.echo(f"  [{elapsed_w}s] connection lost, retrying...")

        if not workers_ready:
            click.echo("Error: Workers failed to connect within 5 minutes", err=True)
            sys.exit(1)

        click.echo(f"  {workers} worker(s) connected.")

        # Start test via POST /swarm
        swarm_url = f"http://localhost:{port}/swarm"
        data = urllib.parse.urlencode(
            {
                "user_count": users,
                "spawn_rate": spawn_rate,
                "run_time": f"{duration}s",
            }
        ).encode()
        click.echo(f"\nStarting test: {users} users, {spawn_rate}/s spawn, {duration}s...")
        req = urllib.request.Request(swarm_url, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                swarm_response = json.loads(resp.read())
                if not swarm_response.get("success", True):
                    msg = swarm_response.get("message", "unknown error")
                    click.echo(f"Error: Failed to start swarm: {msg}", err=True)
                    sys.exit(1)
        except (urllib.error.URLError, OSError) as e:
            click.echo(f"Error: Failed to start swarm: {e}", err=True)
            sys.exit(1)

        click.echo("  Test started. Polling stats...\n")

        # Poll stats until test completes
        poll_interval = 5
        consecutive_failures = 0
        max_consecutive_failures = 3
        max_wait = duration + (users // max(spawn_rate, 1)) + 30

        elapsed = 0
        while elapsed < max_wait:
            time.sleep(poll_interval)
            elapsed += poll_interval

            try:
                req = urllib.request.Request(stats_url)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    stats_json = json.loads(resp.read())
                consecutive_failures = 0
                last_good_stats = stats_json

                state = stats_json.get("state", "unknown")
                total_rps = stats_json.get("total_rps", 0)
                user_count = stats_json.get("user_count", 0)
                worker_list = stats_json.get("workers", [])
                wc = len(worker_list) if isinstance(worker_list, list) else 0
                click.echo(
                    f"  [{elapsed}s] state={state}, users={user_count}, "
                    f"workers={wc}, rps={total_rps:.1f}"
                )

                if state == "stopped":
                    click.echo("  Test completed.")
                    break

            except (urllib.error.URLError, ConnectionRefusedError, OSError):
                consecutive_failures += 1
                if consecutive_failures >= max_consecutive_failures:
                    click.echo("  Locust stopped (connection lost).")
                    break
                click.echo(f"  [{elapsed}s] connection lost, retrying...")

        if last_good_stats:
            mapped = _map_locust_stats(last_good_stats)
            if mapped:
                _display_benchmark_results(mapped)
            else:
                click.echo("\nNo aggregated stats available.", err=True)
        else:
            click.echo("\nError: No stats collected.", err=True)
            sys.exit(1)

    except KeyboardInterrupt:
        click.echo("\nInterrupted")
        if last_good_stats:
            mapped = _map_locust_stats(last_good_stats)
            if mapped:
                click.echo("\nPartial results:")
                _display_benchmark_results(mapped)

    finally:
        # Clean up tunnel
        if tunnel_proc and tunnel_proc.poll() is None:
            tunnel_proc.terminate()
            try:
                tunnel_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tunnel_proc.kill()

        # Stop the Fargate task explicitly (no --autoquit in distributed mode)
        click.echo("\nStopping Fargate task...")
        try:
            ecs.stop_task(cluster=stack_name, task=task_arn)
            waiter = ecs.get_waiter("tasks_stopped")
            waiter.wait(
                cluster=stack_name,
                tasks=[task_arn],
                WaiterConfig={"Delay": 5, "MaxAttempts": 24},
            )
            click.echo("  Fargate task stopped.")
        except Exception as e:
            click.echo(f"  Warning: Failed to stop task: {e}", err=True)
