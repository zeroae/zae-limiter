# Load Connect Parameter Overrides Design

**Date:** 2026-02-05
**Status:** Approved
**Branch:** perf/stress-test

## Problem

Currently, changing load test parameters (max workers, desired workers, locustfile, etc.) requires redeploying the entire CloudFormation stack via `zae-limiter load deploy`. This is slow and cumbersome during iterative testing.

## Solution

Enable parameter overrides at `load connect` time by switching from ECS Service scaling to direct `run_task()` calls with container overrides.

## CLI Interface

```bash
# Current (no overrides)
zae-limiter load connect --name stress-target --region us-east-1

# With scaling overrides
zae-limiter load connect --name stress-target --region us-east-1 \
  --max-workers 50 \
  --desired-workers 10 \
  --min-workers 2

# With locustfile override
zae-limiter load connect --name stress-target --region us-east-1 \
  -f locustfiles/llm_production.py

# Standalone mode (no workers, single-process Locust)
zae-limiter load connect --name stress-target --region us-east-1 \
  --standalone \
  -f locustfiles/simple.py

# Force restart with new config (when task already running)
zae-limiter load connect --name stress-target --region us-east-1 \
  --max-workers 50 --force
```

### New Options

| Option | Description |
|--------|-------------|
| `--max-workers` | Override max Lambda workers |
| `--desired-workers` | Override fixed worker count (disables auto-scaling) |
| `--min-workers` | Override minimum workers |
| `--users-per-worker` | Override auto-scaling ratio |
| `--rps-per-worker` | Override auto-scaling ratio |
| `--startup-lead-time` | Override predictive scaling lookahead |
| `-f, --locustfile` | Override locustfile path |
| `--standalone` | Run Locust without workers (single-process mode) |
| `--force` | Stop existing task and restart with new config |

## Implementation

### Task Lifecycle Changes

**Current approach (ECS Service scaling):**
```python
ecs.update_service(cluster=stack_name, service=service_name, desiredCount=1)
# ... wait for task ...
ecs.update_service(cluster=stack_name, service=service_name, desiredCount=0)
```

**New approach (direct run_task):**
```python
response = ecs.run_task(
    cluster=stack_name,
    taskDefinition=f"{stack_name}-master",
    networkConfiguration={...},  # Fetched from service config
    enableExecuteCommand=True,
    overrides={...},  # Parameter overrides
)
task_arn = response["tasks"][0]["taskArn"]

# On disconnect:
ecs.stop_task(cluster=stack_name, task=task_arn)
```

The ECS Service remains at `desiredCount=0` and serves as a "template" holding network config and defaults.

### Container Overrides Structure

**Scaling parameters** (orchestrator container):
```python
orchestrator_env = []
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
```

**Locustfile override** (both containers):
```python
if locustfile:
    master_env.append({"name": "LOCUSTFILE", "value": locustfile})
    orchestrator_env.append({"name": "LOCUSTFILE", "value": locustfile})
```

**Standalone mode** (master command override + orchestrator idle):
```python
if standalone:
    # Override command to run without --master flag
    master_override["command"] = [
        "sh", "-c",
        f"locust -f /mnt/{locustfile or '$LOCUSTFILE'}"
    ]
    # Let orchestrator run but idle (no workers)
    orchestrator_env.append({"name": "DESIRED_WORKERS", "value": "0"})
    orchestrator_env.append({"name": "MIN_WORKERS", "value": "0"})
```

### Handling Existing Tasks

When connecting with overrides and a task is already running:

1. **No overrides provided** → Reuse existing task (current behavior)
2. **Overrides provided + task running**:
   - Default: Warn and reuse existing task
   - With `--force`: Stop existing task and start new one with overrides

```
Found running Fargate task: abc123
Warning: Task already running. Use --force to restart with new config.
Connecting to existing task...
```

## Files Changed

| File | Changes |
|------|---------|
| `src/zae_limiter/load/cli.py` | Add CLI options, replace service scaling with run_task/stop_task, add override building logic |

**No changes to:**
- `orchestrator.py` - Already reads from environment variables
- `cfn_template.yaml` - Service remains as template
- `builder.py` - Docker image unchanged

## Helper Function

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
    """Build container overrides dict for run_task()."""
```

## Notes

- `lambda_timeout` is NOT overridable at connect time (requires Lambda configuration change via redeploy)
- The ECS Service is retained for its network configuration; we query it to get subnets and security groups for `run_task()`
- Standalone mode runs Locust in single-process mode (no `--master` flag), suitable for quick tests without Lambda worker overhead
