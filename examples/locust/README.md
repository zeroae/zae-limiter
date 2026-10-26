# Locust Load Test Examples

Load test scenarios for zae-limiter using [Locust](https://locust.io/) with the
`RateLimiterUser` integration.

## Project Structure

Follows [Locust's recommended structure](https://docs.locust.io/en/stable/writing-a-locustfile.html):

```
examples/locust/
├── common/                     # Shared utilities
│   ├── config.py               # LoadConfig, LoadDistribution
│   └── distribution.py         # TrafficDistributor (whale/spike/powerlaw)
├── locustfiles/                # Scenario files
│   ├── simple.py               # Single resource, single limit
│   ├── max_rps.py              # Zero-wait max throughput benchmark
│   ├── multi_resource.py       # One multi-resource acquire vs two acquires
│   ├── llm_gateway.py          # LLM gateway with lease adjustments
│   ├── llm_production.py       # Production LLM traffic with load shapes
│   └── stress.py               # Whale/spike/power-law distribution
└── README.md
```

## Scenarios

| Scenario | Resources | Limits | Entities | Pattern |
|----------|-----------|--------|----------|---------|
| `simple` | 1 (`api`) | RPM | Many anonymous | `acquire` only |
| `max_rps` | 1 (`api`) | RPM | Many anonymous | Zero-wait back-to-back `acquire` |
| `multi_resource` | 2 (`search`, `budget`) | `rpm` + `budget_rpm` (both per minute) | Children of one shared parent | Zero-wait `acquire(..., also=...)` vs two nested `acquire` |
| `llm_gateway` | 8 LLM models | RPM + TPM | Many anonymous | `acquire` → `adjust` → `commit` |
| `llm_production` | 8 LLM models | RPM + TPM | Many anonymous | Weighted tasks + custom load shapes |
| `stress` | 8 APIs | RPM + TPM | 16K (whale/spike/powerlaw) | Hot partition + burst testing |

### User Classes

Each locustfile may define multiple user classes. Use positional arguments to select
specific classes (default: all classes in the file).

| File | Class | Cascade | Description |
|------|-------|---------|-------------|
| `simple.py` | `SimpleUser` | No | Standalone entities, system-level RPM limit |
| `simple.py` | `SimpleCascadeUser` | Yes | Child entities under a shared parent |
| `max_rps.py` | `MaxRpsUser` | No | Zero-wait standalone, max throughput |
| `max_rps.py` | `MaxRpsCascadeUser` | Yes | Zero-wait cascade, measures cascade overhead |
| `multi_resource.py` | `MultiResourceAlsoUser` | `search` only | One `acquire(..., also={"budget": ...})`: one lease, one round trip |
| `multi_resource.py` | `MultiResourceNestedUser` | `search` only | Two nested acquires (`search`, then `budget`): two leases, two round trips |

Cascade classes create child entities with `cascade=True` under a shared parent.
Every `acquire()` writes to both child and parent buckets, enabling direct
comparison of cascade overhead vs standalone operation.

The parent is **one entity for the whole run**, with a fixed id per locustfile
(`loadtest-cascade-parent-simple`, `loadtest-cascade-parent-max-rps`), so the
master, every worker and a headless run all debit the same one, as in
production, where a parent's bucket is the hot partition. Each process creates
it on its first cascade user's `on_start` and treats "already exists" as
success. Only the master (or a headless runner) deletes it in `on_test_stop`;
a worker never does. On a normal stop the master fires `on_test_stop` only after
its workers report no running users. It can fire earlier, while workers still
debit the parent, when the workers do not report within `stop_timeout` + 60 s,
or when the master quits instead of stopping (Ctrl-C, or the distributed CLI
stopping the Fargate task): `quit()` fires `test_stop` before it tells the
workers to quit. A worker's next slow path then recreates an orphan parent
bucket, which expires through its TTL (system defaults).

A run killed before `on_test_stop` leaves the parent behind, and the next run
reuses it with its old bucket state (possibly already sharded), so that run is
not comparable with a cold start.

The `multi_resource.py` classes share one setup: `search` cascades to the parent,
`budget` does not (resource-level cascade policy, which needs the stack's Lambdas
at v0.16.0 or later). **Compare the `ITERATION multi-resource` row**: both classes
fire it once per task, timing the whole iteration, so its latency and RPS are like
for like. The per-resource rows are informational only. A multi-resource acquire is
one lease, so it reports one `ACQUIRE` row named for the primary resource
(`search`); the nested class reports an `ACQUIRE` row for each resource, so its RPS
reads doubled, and its `search` row times only the outer acquire, so its p50 reads
halved.

Their parent is one entity for the whole run, with a fixed id
(`loadtest-cascade-parent-multi-resource`), whether the users run locally, on
distributed workers or on several Lambdas. Each process creates it if it is
missing (an existing one counts as success), and only the master or a local
runner deletes it when the test stops, never a worker.

## Running Locally

Run from this directory (`examples/locust/`):

```bash
# Simple: single resource, single limit
locust -f locustfiles/simple.py --host <stack-name>

# Max RPS: zero-wait throughput benchmark
locust -f locustfiles/max_rps.py --host <stack-name>

# Run only the cascade user class
locust -f locustfiles/max_rps.py --host <stack-name> MaxRpsCascadeUser

# Multi-resource: one acquire over two resources vs two acquires
locust -f locustfiles/multi_resource.py --host <stack-name> MultiResourceAlsoUser
locust -f locustfiles/multi_resource.py --host <stack-name> MultiResourceNestedUser

# LLM Gateway: multiple models with lease adjustments
locust -f locustfiles/llm_gateway.py --host <stack-name>

# LLM Production: realistic traffic with daily/spike load shapes
locust -f locustfiles/llm_production.py --host <stack-name>

# Stress: whale/spike/power-law traffic
locust -f locustfiles/stress.py --host <stack-name>

# All scenarios at once
locust -f locustfiles/ --host <stack-name>
```

Then open http://localhost:8089 to configure users and start the test.

## Running on AWS (Distributed)

The `zae-limiter loadtest` commands deploy a distributed Locust cluster using
ECS Fargate (master) + Lambda (workers).

### 1. Deploy Infrastructure

```bash
# Simple scenario
zae-limiter loadtest deploy -n <stack-name> -C examples/locust -f locustfiles/simple.py

# LLM Gateway scenario
zae-limiter loadtest deploy -n <stack-name> -C examples/locust -f locustfiles/llm_gateway.py

# Stress scenario (set up test data first — see step 2)
zae-limiter loadtest deploy -n <stack-name> -C examples/locust -f locustfiles/stress.py
```

The deploy command will interactively prompt for VPC and subnet selection if not
provided via `--vpc-id` and `--subnet-ids`.

Additional flags:

| Flag | Default | Description |
|------|---------|-------------|
| `--max-workers` | 100 | Maximum Lambda worker concurrency |
| `--lambda-timeout` | 5 | Lambda worker timeout in minutes |
| `--ssm-endpoint / --no-ssm-endpoint` | off | Create VPC endpoints for SSM (not needed if VPC has NAT gateway) |
| `--dynamodb-endpoint / --no-dynamodb-endpoint` | on | Create DynamoDB gateway endpoint (auto-discovers route tables) |

### 2. Set Up Test Data (stress scenario only)

The stress scenario expects pre-configured entities and limits:

```bash
zae-limiter loadtest deploy -n <stack-name> --apis 8 --custom-limits 300
```

The simple and llm_gateway scenarios configure their own limits via `on_start()`,
so no setup step is needed.

### 3. Connect to Locust UI

```bash
zae-limiter loadtest ui -n <stack-name> -f locustfiles/simple.py
```

This starts a Fargate task (if not running), waits for SSM agent readiness,
opens an SSM tunnel to http://localhost:8089, and auto-opens the browser
when the tunnel is ready.

| Flag | Default | Description |
|------|---------|-------------|
| `-f` / `--locustfile` | (required) | Locustfile path relative to `-C` directory |
| `--port` | 8089 | Local port for the Locust UI |
| `--destroy` | off | Stop Fargate task on disconnect even if already running |
| `--force` | off | Stop existing task and restart with new config |
| `--max-workers` | (from deploy) | Override max Lambda workers |
| `--min-workers` | (from deploy) | Override minimum workers |
| `--users-per-worker` | (from deploy) | Override users per worker ratio |

### 4. Run Headless Tests

```bash
# Lambda mode (default): single Lambda invocation
zae-limiter loadtest run -n <stack-name> -f locustfiles/max_rps.py --users 20 --duration 60

# Run only cascade user class (positional args)
zae-limiter loadtest run -n <stack-name> -f locustfiles/max_rps.py MaxRpsCascadeUser

# Distributed mode: Fargate master + Lambda workers
zae-limiter loadtest run -n <stack-name> -f locustfiles/max_rps.py --workers 10 --users 100

# Calibrate optimal per-worker concurrency
zae-limiter loadtest tune -n <stack-name> -f locustfiles/max_rps.py
zae-limiter loadtest tune -n <stack-name> -f locustfiles/max_rps.py MaxRpsCascadeUser
```

User class names are passed as positional arguments (Locust native style).
When omitted, all classes in the locustfile are used.

### 5. Delete Infrastructure

```bash
zae-limiter loadtest delete -n <stack-name> --yes
```
