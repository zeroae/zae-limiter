# Load Connect Parameter Overrides Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enable runtime parameter overrides at `load connect` time without redeploying CloudFormation.

**Architecture:** Replace ECS Service scaling with direct `run_task()` calls that support container environment overrides. The service remains at desiredCount=0 as a template for network configuration.

**Tech Stack:** Click CLI, boto3 ECS API, Python 3.12

---

## Task 1: Add Helper Function for Building Task Overrides

**Files:**
- Modify: `src/zae_limiter/load/cli.py` (add after line 89, before `@click.group()`)
- Test: `tests/unit/load/test_cli.py`

**Step 1: Write the failing test**

Add to `tests/unit/load/test_cli.py` after the existing `TestSelectSubnets` class:

```python
class TestBuildTaskOverrides:
    """Tests for _build_task_overrides helper."""

    def test_empty_overrides_returns_empty_dict(self):
        from zae_limiter.load.cli import _build_task_overrides

        result = _build_task_overrides(
            standalone=False,
            locustfile=None,
            max_workers=None,
            desired_workers=None,
            min_workers=None,
            users_per_worker=None,
            rps_per_worker=None,
            startup_lead_time=None,
        )
        assert result == {}

    def test_scaling_params_override_orchestrator_env(self):
        from zae_limiter.load.cli import _build_task_overrides

        result = _build_task_overrides(
            standalone=False,
            locustfile=None,
            max_workers=50,
            desired_workers=10,
            min_workers=2,
            users_per_worker=25,
            rps_per_worker=100,
            startup_lead_time=30,
        )

        # Find orchestrator container override
        orchestrator = next(
            c for c in result["containerOverrides"]
            if c["name"] == "worker-orchestrator"
        )
        env_dict = {e["name"]: e["value"] for e in orchestrator["environment"]}

        assert env_dict["MAX_WORKERS"] == "50"
        assert env_dict["DESIRED_WORKERS"] == "10"
        assert env_dict["MIN_WORKERS"] == "2"
        assert env_dict["USERS_PER_WORKER"] == "25"
        assert env_dict["RPS_PER_WORKER"] == "100"
        assert env_dict["STARTUP_LEAD_TIME"] == "30"

    def test_locustfile_overrides_both_containers(self):
        from zae_limiter.load.cli import _build_task_overrides

        result = _build_task_overrides(
            standalone=False,
            locustfile="locustfiles/llm_production.py",
            max_workers=None,
            desired_workers=None,
            min_workers=None,
            users_per_worker=None,
            rps_per_worker=None,
            startup_lead_time=None,
        )

        for container_name in ["locust-master", "worker-orchestrator"]:
            container = next(
                c for c in result["containerOverrides"]
                if c["name"] == container_name
            )
            env_dict = {e["name"]: e["value"] for e in container["environment"]}
            assert env_dict["LOCUSTFILE"] == "locustfiles/llm_production.py"

    def test_standalone_mode_overrides_master_command(self):
        from zae_limiter.load.cli import _build_task_overrides

        result = _build_task_overrides(
            standalone=True,
            locustfile="simple.py",
            max_workers=None,
            desired_workers=None,
            min_workers=None,
            users_per_worker=None,
            rps_per_worker=None,
            startup_lead_time=None,
        )

        master = next(
            c for c in result["containerOverrides"]
            if c["name"] == "locust-master"
        )
        assert "command" in master
        assert "--master" not in " ".join(master["command"])
        assert "simple.py" in " ".join(master["command"])

    def test_standalone_mode_sets_orchestrator_idle(self):
        from zae_limiter.load.cli import _build_task_overrides

        result = _build_task_overrides(
            standalone=True,
            locustfile=None,
            max_workers=None,
            desired_workers=None,
            min_workers=None,
            users_per_worker=None,
            rps_per_worker=None,
            startup_lead_time=None,
        )

        orchestrator = next(
            c for c in result["containerOverrides"]
            if c["name"] == "worker-orchestrator"
        )
        env_dict = {e["name"]: e["value"] for e in orchestrator["environment"]}
        assert env_dict["DESIRED_WORKERS"] == "0"
        assert env_dict["MIN_WORKERS"] == "0"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/load/test_cli.py::TestBuildTaskOverrides -v`
Expected: FAIL with "cannot import name '_build_task_overrides'"

**Step 3: Write minimal implementation**

Add to `src/zae_limiter/load/cli.py` after line 89 (after `_select_subnets` function, before `@click.group()`):

```python
def _build_task_overrides(
    standalone: bool,
    locustfile: str | None,
    max_workers: int | None,
    desired_workers: int | None,
    min_workers: int | None,
    users_per_worker: int | None,
    rps_per_worker: int | None,
    startup_lead_time: int | None,
) -> dict:
    """Build container overrides dict for run_task().

    Args:
        standalone: Run Locust in single-process mode (no workers).
        locustfile: Override locustfile path.
        max_workers: Override max Lambda workers.
        desired_workers: Override fixed worker count.
        min_workers: Override minimum workers.
        users_per_worker: Override auto-scaling ratio.
        rps_per_worker: Override auto-scaling ratio.
        startup_lead_time: Override predictive scaling lookahead.

    Returns:
        Dict suitable for run_task(overrides=...), or empty dict if no overrides.
    """
    master_env: list[dict[str, str]] = []
    orchestrator_env: list[dict[str, str]] = []
    master_command: list[str] | None = None

    # Locustfile override applies to both containers
    if locustfile:
        master_env.append({"name": "LOCUSTFILE", "value": locustfile})
        orchestrator_env.append({"name": "LOCUSTFILE", "value": locustfile})

    # Scaling parameters apply to orchestrator
    if max_workers is not None:
        orchestrator_env.append({"name": "MAX_WORKERS", "value": str(max_workers)})
    if desired_workers is not None:
        orchestrator_env.append({"name": "DESIRED_WORKERS", "value": str(desired_workers)})
    if min_workers is not None:
        orchestrator_env.append({"name": "MIN_WORKERS", "value": str(min_workers)})
    if users_per_worker is not None:
        orchestrator_env.append({"name": "USERS_PER_WORKER", "value": str(users_per_worker)})
    if rps_per_worker is not None:
        orchestrator_env.append({"name": "RPS_PER_WORKER", "value": str(rps_per_worker)})
    if startup_lead_time is not None:
        orchestrator_env.append({"name": "STARTUP_LEAD_TIME", "value": str(startup_lead_time)})

    # Standalone mode: override master command to run without --master
    if standalone:
        locustfile_path = locustfile or "$LOCUSTFILE"
        master_command = ["sh", "-c", f"locust -f /mnt/{locustfile_path}"]
        # Make orchestrator idle (no workers)
        orchestrator_env.append({"name": "DESIRED_WORKERS", "value": "0"})
        orchestrator_env.append({"name": "MIN_WORKERS", "value": "0"})

    # Build overrides dict only if we have overrides
    if not master_env and not orchestrator_env and not master_command:
        return {}

    container_overrides = []

    # Master container
    master_override: dict = {"name": "locust-master"}
    if master_env:
        master_override["environment"] = master_env
    if master_command:
        master_override["command"] = master_command
    if master_env or master_command:
        container_overrides.append(master_override)

    # Orchestrator container
    if orchestrator_env:
        container_overrides.append({
            "name": "worker-orchestrator",
            "environment": orchestrator_env,
        })

    return {"containerOverrides": container_overrides}
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/load/test_cli.py::TestBuildTaskOverrides -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/zae_limiter/load/cli.py tests/unit/load/test_cli.py
git commit -m "$(cat <<'EOF'
✨ feat(load): add _build_task_overrides helper

Helper function to build ECS run_task() container overrides for:
- Scaling parameters (max_workers, desired_workers, etc.)
- Locustfile selection
- Standalone mode (single-process Locust)
EOF
)"
```

---

## Task 2: Add Helper Function to Get Network Config from Service

**Files:**
- Modify: `src/zae_limiter/load/cli.py`
- Test: `tests/unit/load/test_cli.py`

**Step 1: Write the failing test**

Add to `tests/unit/load/test_cli.py`:

```python
class TestGetServiceNetworkConfig:
    """Tests for _get_service_network_config helper."""

    def test_extracts_network_config_from_service(self):
        from zae_limiter.load.cli import _get_service_network_config

        mock_ecs = MagicMock()
        mock_ecs.describe_services.return_value = {
            "services": [{
                "networkConfiguration": {
                    "awsvpcConfiguration": {
                        "subnets": ["subnet-a", "subnet-b"],
                        "securityGroups": ["sg-123"],
                        "assignPublicIp": "DISABLED",
                    }
                }
            }]
        }

        result = _get_service_network_config(mock_ecs, "my-cluster", "my-service")

        assert result == {
            "awsvpcConfiguration": {
                "subnets": ["subnet-a", "subnet-b"],
                "securityGroups": ["sg-123"],
                "assignPublicIp": "DISABLED",
            }
        }
        mock_ecs.describe_services.assert_called_once_with(
            cluster="my-cluster",
            services=["my-service"],
        )
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/load/test_cli.py::TestGetServiceNetworkConfig -v`
Expected: FAIL with "cannot import name '_get_service_network_config'"

**Step 3: Write minimal implementation**

Add to `src/zae_limiter/load/cli.py` after `_build_task_overrides`:

```python
def _get_service_network_config(ecs_client, cluster: str, service: str) -> dict:
    """Get network configuration from an ECS service.

    Args:
        ecs_client: boto3 ECS client.
        cluster: ECS cluster name.
        service: ECS service name.

    Returns:
        Network configuration dict suitable for run_task().
    """
    response = ecs_client.describe_services(cluster=cluster, services=[service])
    service_config = response["services"][0]
    return service_config["networkConfiguration"]
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/load/test_cli.py::TestGetServiceNetworkConfig -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/zae_limiter/load/cli.py tests/unit/load/test_cli.py
git commit -m "$(cat <<'EOF'
✨ feat(load): add _get_service_network_config helper

Extracts network configuration from ECS service for use with run_task().
EOF
)"
```

---

## Task 3: Add CLI Options to Connect Command

**Files:**
- Modify: `src/zae_limiter/load/cli.py` (lines 417-422)

**Step 1: Add new CLI options**

Modify the `connect` command decorator chain (starting at line 417) to add new options:

```python
@load.command()
@click.option("--name", "-n", required=True, help="zae-limiter name")
@click.option("--region", default=None, help="AWS region")
@click.option("--port", default=8089, type=int, help="Local port for Locust UI")
@click.option("--destroy", is_flag=True, help="Stop Fargate on disconnect even if already running")
@click.option("--force", is_flag=True, help="Stop existing task and restart with new config")
@click.option("--standalone", is_flag=True, help="Run Locust without workers (single-process mode)")
@click.option(
    "-f",
    "--locustfile",
    default=None,
    help="Override locustfile path (relative to -C directory used in deploy)",
)
@click.option("--max-workers", type=int, default=None, help="Override max Lambda workers")
@click.option("--desired-workers", type=int, default=None, help="Override fixed worker count")
@click.option("--min-workers", type=int, default=None, help="Override minimum workers")
@click.option("--users-per-worker", type=int, default=None, help="Override users per worker ratio")
@click.option("--rps-per-worker", type=int, default=None, help="Override RPS per worker ratio")
@click.option("--startup-lead-time", type=int, default=None, help="Override predictive scaling lookahead")
def connect(
    name: str,
    region: str | None,
    port: int,
    destroy: bool,
    force: bool,
    standalone: bool,
    locustfile: str | None,
    max_workers: int | None,
    desired_workers: int | None,
    min_workers: int | None,
    users_per_worker: int | None,
    rps_per_worker: int | None,
    startup_lead_time: int | None,
) -> None:
    """Connect to Fargate master via SSM tunnel."""
```

**Step 2: Run existing tests to verify no regression**

Run: `uv run pytest tests/unit/load/test_cli.py::TestConnectCommand -v`
Expected: PASS (existing tests should still work)

**Step 3: Commit**

```bash
git add src/zae_limiter/load/cli.py
git commit -m "$(cat <<'EOF'
✨ feat(load): add override options to connect command

New CLI options for runtime parameter overrides:
- --standalone: Run Locust without workers
- -f/--locustfile: Override locustfile path
- --max-workers, --desired-workers, --min-workers
- --users-per-worker, --rps-per-worker, --startup-lead-time
- --force: Restart task with new config
EOF
)"
```

---

## Task 4: Replace Service Scaling with run_task()

**Files:**
- Modify: `src/zae_limiter/load/cli.py` (connect command body)
- Test: `tests/unit/load/test_cli.py`

**Step 1: Write the failing test for run_task behavior**

Add to `tests/unit/load/test_cli.py` in `TestConnectCommand`:

```python
    def test_uses_run_task_with_overrides(self, runner):
        """Connect uses run_task with overrides instead of service scaling."""
        with (
            patch("boto3.client") as mock_client,
            patch("subprocess.run") as mock_subprocess_run,
            patch("time.sleep"),
        ):
            mock_ecs = MagicMock()
            mock_client.return_value = mock_ecs

            # No tasks running initially
            mock_ecs.list_tasks.side_effect = [
                {"taskArns": []},  # initial check
                {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/task-id"]},  # after run_task
                {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/task-id"]},  # ssm check
            ]
            mock_ecs.describe_tasks.return_value = self._make_task_response()
            mock_ecs.describe_services.return_value = {
                "services": [{
                    "networkConfiguration": {
                        "awsvpcConfiguration": {
                            "subnets": ["subnet-a"],
                            "securityGroups": ["sg-123"],
                            "assignPublicIp": "DISABLED",
                        }
                    }
                }]
            }
            mock_ecs.run_task.return_value = {
                "tasks": [{"taskArn": "arn:aws:ecs:us-east-1:123:task/cluster/task-id"}]
            }
            mock_subprocess_run.return_value = MagicMock(returncode=0)

            result = runner.invoke(
                load,
                ["connect", "--name", "my-app", "--max-workers", "50", "--destroy"],
            )
            assert result.exit_code == 0, result.output

            # Should have called run_task, not update_service for scaling up
            mock_ecs.run_task.assert_called_once()
            call_kwargs = mock_ecs.run_task.call_args[1]
            assert call_kwargs["cluster"] == "my-app-load"
            assert "overrides" in call_kwargs

            # Should use stop_task on cleanup, not update_service
            mock_ecs.stop_task.assert_called()

    def test_force_restarts_running_task(self, runner):
        """Connect --force stops existing task and starts new one."""
        with (
            patch("boto3.client") as mock_client,
            patch("subprocess.run") as mock_subprocess_run,
            patch("time.sleep"),
        ):
            mock_ecs = MagicMock()
            mock_client.return_value = mock_ecs

            # Task already running
            mock_ecs.list_tasks.side_effect = [
                {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/old-task"]},  # initial
                {"taskArns": []},  # after stop
                {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/new-task"]},  # after run
                {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/new-task"]},  # ssm
            ]
            mock_ecs.describe_tasks.return_value = self._make_task_response()
            mock_ecs.describe_services.return_value = {
                "services": [{
                    "networkConfiguration": {
                        "awsvpcConfiguration": {
                            "subnets": ["subnet-a"],
                            "securityGroups": ["sg-123"],
                            "assignPublicIp": "DISABLED",
                        }
                    }
                }]
            }
            mock_ecs.run_task.return_value = {
                "tasks": [{"taskArn": "arn:aws:ecs:us-east-1:123:task/cluster/new-task"}]
            }
            mock_subprocess_run.return_value = MagicMock(returncode=0)

            result = runner.invoke(
                load,
                ["connect", "--name", "my-app", "--max-workers", "50", "--force", "--destroy"],
            )
            assert result.exit_code == 0, result.output

            # Should have stopped the old task
            mock_ecs.stop_task.assert_any_call(
                cluster="my-app-load",
                task="arn:aws:ecs:us-east-1:123:task/cluster/old-task",
            )

    def test_warns_when_task_running_with_overrides_no_force(self, runner):
        """Connect warns when task exists and overrides provided without --force."""
        with (
            patch("boto3.client") as mock_client,
            patch("subprocess.run") as mock_subprocess_run,
            patch("time.sleep"),
        ):
            mock_ecs = MagicMock()
            mock_client.return_value = mock_ecs

            # Task already running
            mock_ecs.list_tasks.return_value = {
                "taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/task-id"]
            }
            mock_ecs.describe_tasks.return_value = self._make_task_response()
            mock_subprocess_run.return_value = MagicMock(returncode=0)

            result = runner.invoke(
                load,
                ["connect", "--name", "my-app", "--max-workers", "50", "--destroy"],
            )
            assert result.exit_code == 0, result.output
            assert "already running" in result.output.lower()
            assert "--force" in result.output

    def test_standalone_mode_overrides_command(self, runner):
        """Connect --standalone overrides master command."""
        with (
            patch("boto3.client") as mock_client,
            patch("subprocess.run") as mock_subprocess_run,
            patch("time.sleep"),
        ):
            mock_ecs = MagicMock()
            mock_client.return_value = mock_ecs

            mock_ecs.list_tasks.side_effect = [
                {"taskArns": []},
                {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/task-id"]},
                {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/task-id"]},
            ]
            mock_ecs.describe_tasks.return_value = self._make_task_response()
            mock_ecs.describe_services.return_value = {
                "services": [{
                    "networkConfiguration": {
                        "awsvpcConfiguration": {
                            "subnets": ["subnet-a"],
                            "securityGroups": ["sg-123"],
                            "assignPublicIp": "DISABLED",
                        }
                    }
                }]
            }
            mock_ecs.run_task.return_value = {
                "tasks": [{"taskArn": "arn:aws:ecs:us-east-1:123:task/cluster/task-id"}]
            }
            mock_subprocess_run.return_value = MagicMock(returncode=0)

            result = runner.invoke(
                load,
                ["connect", "--name", "my-app", "--standalone", "--destroy"],
            )
            assert result.exit_code == 0, result.output

            # Verify overrides include standalone command
            call_kwargs = mock_ecs.run_task.call_args[1]
            overrides = call_kwargs["overrides"]
            master = next(
                c for c in overrides["containerOverrides"]
                if c["name"] == "locust-master"
            )
            assert "command" in master
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/load/test_cli.py::TestConnectCommand::test_uses_run_task_with_overrides -v`
Expected: FAIL

**Step 3: Rewrite connect command body**

Replace the `connect` function body in `src/zae_limiter/load/cli.py`:

```python
def connect(
    name: str,
    region: str | None,
    port: int,
    destroy: bool,
    force: bool,
    standalone: bool,
    locustfile: str | None,
    max_workers: int | None,
    desired_workers: int | None,
    min_workers: int | None,
    users_per_worker: int | None,
    rps_per_worker: int | None,
    startup_lead_time: int | None,
) -> None:
    """Connect to Fargate master via SSM tunnel."""
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
        desired_workers=desired_workers,
        min_workers=min_workers,
        users_per_worker=users_per_worker,
        rps_per_worker=rps_per_worker,
        startup_lead_time=startup_lead_time,
    )
    has_overrides = bool(overrides)

    def get_running_task(require_ssm: bool = False) -> tuple[str, str, str] | None:
        """Get (task_arn, task_id, runtime_id) for the running task, or None.

        Args:
            require_ssm: If True, also check that ExecuteCommandAgent is running.
        """
        tasks = ecs.list_tasks(cluster=stack_name, serviceName=service_name)
        if not tasks["taskArns"]:
            # Also check for tasks not associated with service (from run_task)
            tasks = ecs.list_tasks(cluster=stack_name)
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

        return task_arn, task_id, runtime_id

    def start_task() -> str:
        """Start a new task with run_task(). Returns task ARN."""
        network_config = _get_service_network_config(ecs, stack_name, service_name)

        run_kwargs: dict = {
            "cluster": stack_name,
            "taskDefinition": task_definition,
            "networkConfiguration": network_config,
            "enableExecuteCommand": True,
            "count": 1,
        }
        if overrides:
            run_kwargs["overrides"] = overrides

        response = ecs.run_task(**run_kwargs)
        return response["tasks"][0]["taskArn"]

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
                # Warn user about existing task
                click.echo(f"Found running Fargate task: {task_id}")
                click.echo("Warning: Task already running. Use --force to restart with new config.")
                click.echo("Connecting to existing task (overrides ignored)...")
                task_arn = existing_task_arn
            elif has_overrides and force:
                # Stop existing task and start new one
                click.echo(f"Stopping existing task: {task_id}")
                stop_task(existing_task_arn)
                time.sleep(2)  # Wait for task to stop

                click.echo(f"Starting new Fargate task with overrides: {stack_name}")
                task_arn = start_task()
                started_by_us = True

                # Wait for new task to be running
                click.echo("  Waiting for task to start...")
                for _ in range(60):
                    result = get_running_task()
                    if result:
                        task_arn, task_id, runtime_id = result
                        break
                    time.sleep(2)
                else:
                    click.echo("Error: Task failed to start within 2 minutes", err=True)
                    sys.exit(1)

                click.echo(f"  Task running: {task_id}")
            else:
                # No overrides, just reuse existing task
                click.echo(f"Found running Fargate task: {task_id}")
                task_arn = existing_task_arn
        else:
            # No task running, start a new one
            started_by_us = True
            click.echo(f"Starting Fargate master: {stack_name}")
            task_arn = start_task()

            # Wait for task to be running
            click.echo("  Waiting for task to start...")
            for _ in range(60):  # 2 minute timeout
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

        # Wait for SSM agent to be ready
        click.echo("  Waiting for SSM agent...")
        for _ in range(30):  # 1 minute timeout
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
        # Stop task if we started it or --destroy was passed
        if (started_by_us or destroy) and task_arn:
            click.echo("Stopping Fargate task...")
            try:
                stop_task(task_arn)
                click.echo("  Fargate task stopped")
            except Exception as e:
                click.echo(f"  Warning: Failed to stop task: {e}", err=True)
        elif task_arn:
            click.echo("Disconnected (Fargate task still running, use --destroy to stop)")
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/load/test_cli.py::TestConnectCommand -v`
Expected: Most tests PASS (some may need adjustment)

**Step 5: Fix any failing tests**

Some existing tests may fail because they expect `update_service` instead of `run_task`. Update them to match the new behavior.

**Step 6: Commit**

```bash
git add src/zae_limiter/load/cli.py tests/unit/load/test_cli.py
git commit -m "$(cat <<'EOF'
✨ feat(load): switch connect to run_task with overrides

Replace ECS service scaling with direct run_task() calls:
- Supports container environment overrides for scaling params
- Supports standalone mode via command override
- --force flag to restart task with new config
- Service remains at desiredCount=0 as network config template
EOF
)"
```

---

## Task 5: Update Existing Tests for New Behavior

**Files:**
- Modify: `tests/unit/load/test_cli.py`

**Step 1: Update tests that expect service scaling**

Update tests that call `update_service` for starting tasks to expect `run_task` instead. Key tests to update:

- `test_starts_fargate_and_connects` - should verify `run_task` called
- `test_task_start_timeout` - should verify `stop_task` called on failure
- `test_ssm_agent_timeout` - should verify `stop_task` called on failure

**Step 2: Run all tests**

Run: `uv run pytest tests/unit/load/test_cli.py -v`
Expected: All PASS

**Step 3: Commit**

```bash
git add tests/unit/load/test_cli.py
git commit -m "$(cat <<'EOF'
✅ test(load): update connect tests for run_task behavior

Update tests to expect run_task/stop_task instead of service scaling.
EOF
)"
```

---

## Task 6: Update CLAUDE.md Documentation

**Files:**
- Modify: `CLAUDE.md`

**Step 1: Update the Load Testing section**

Find the "Load Testing with Locust" section and update to document new options:

```markdown
### Load Testing with Locust

Distributed load testing using Fargate Spot master + Lambda workers:

```bash
# 1. Deploy load test infrastructure (one-time setup)
zae-limiter load deploy --name stress-target --region us-east-1 \
  --vpc-id vpc-09fa0359f30c6efe4 \
  --subnet-ids "subnet-0441a9342c2d605cf,subnet-0d607c058fe28230e" \
  -C examples/locust/ -f locustfiles/simple.py \
  --desired-workers 10

# 2. Connect to Fargate master (starts task if not running)
zae-limiter load connect --name stress-target --region us-east-1
# Opens SSM tunnel to http://localhost:8089 (Locust UI)

# 2b. Connect with runtime overrides (no redeploy needed)
zae-limiter load connect --name stress-target --region us-east-1 \
  --max-workers 50 \
  -f locustfiles/llm_production.py

# 2c. Standalone mode (no Lambda workers, single-process Locust)
zae-limiter load connect --name stress-target --region us-east-1 \
  --standalone -f locustfiles/simple.py

# 2d. Force restart with new config (when task already running)
zae-limiter load connect --name stress-target --region us-east-1 \
  --max-workers 100 --force

# 3. Start test via curl (or use Locust UI)
curl -X POST http://localhost:8089/swarm -d "user_count=100&spawn_rate=10"

# 4. Monitor / Stop / Disconnect (unchanged)
```

**Connect runtime overrides:**
- `--max-workers`, `--desired-workers`, `--min-workers`: Scaling parameters
- `--users-per-worker`, `--rps-per-worker`, `--startup-lead-time`: Auto-scaling tuning
- `-f, --locustfile`: Switch locustfile without redeploy
- `--standalone`: Run Locust in single-process mode (no Lambda workers)
- `--force`: Stop existing task and restart with new config
```

**Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
📝 docs(load): document connect runtime overrides

Document new load connect options:
- Scaling parameter overrides
- Locustfile override
- Standalone mode
- --force flag
EOF
)"
```

---

## Summary

| Task | Description | Estimated Lines |
|------|-------------|-----------------|
| 1 | Add `_build_task_overrides` helper | ~80 |
| 2 | Add `_get_service_network_config` helper | ~15 |
| 3 | Add CLI options to connect command | ~30 |
| 4 | Replace service scaling with run_task | ~150 |
| 5 | Update existing tests | ~50 |
| 6 | Update CLAUDE.md | ~30 |

**Total:** ~355 lines changed
