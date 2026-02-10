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
| `llm_gateway` | 8 LLM models | RPM + TPM | Many anonymous | `acquire` → `adjust` → `commit` |
| `llm_production` | 8 LLM models | RPM + TPM | Many anonymous | Weighted tasks + custom load shapes |
| `stress` | 8 APIs | RPM + TPM | 16K (whale/spike/powerlaw) | Hot partition + burst testing |

## Running Locally

Run from this directory (`examples/locust/`):

```bash
# Simple: single resource, single limit
locust -f locustfiles/simple.py --host <stack-name>

# Max RPS: zero-wait throughput benchmark
locust -f locustfiles/max_rps.py --host <stack-name>

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

The `zae-limiter load` commands deploy a distributed Locust cluster using
ECS Fargate (master) + Lambda (workers).

### 1. Deploy Infrastructure

```bash
# Simple scenario
zae-limiter load deploy -n <stack-name> -C examples/locust -f locustfiles/simple.py

# LLM Gateway scenario
zae-limiter load deploy -n <stack-name> -C examples/locust -f locustfiles/llm_gateway.py

# Stress scenario (set up test data first — see step 2)
zae-limiter load deploy -n <stack-name> -C examples/locust -f locustfiles/stress.py
```

The deploy command will interactively prompt for VPC and subnet selection if not
provided via `--vpc-id` and `--subnet-ids`.

Additional flags:

| Flag | Default | Description |
|------|---------|-------------|
| `--max-workers` | 100 | Maximum Lambda worker concurrency |
| `--lambda-timeout` | 5 | Lambda worker timeout in minutes |
| `--create-vpc-endpoints` | off | Create VPC endpoints for SSM (skip if VPC has NAT) |

### 2. Set Up Test Data (stress scenario only)

The stress scenario expects pre-configured entities and limits:

```bash
zae-limiter load setup -n <stack-name> --apis 8 --custom-limits 300
```

The simple and llm_gateway scenarios configure their own limits via `on_start()`,
so no setup step is needed.

### 3. Connect to Locust UI

```bash
zae-limiter load connect -n <stack-name>
```

This starts a Fargate task (if not running), waits for SSM agent readiness,
and opens an SSM tunnel to http://localhost:8089.

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | 8089 | Local port for the Locust UI |
| `--destroy` | off | Stop Fargate task on disconnect |

### 4. Tear Down

```bash
zae-limiter load teardown -n <stack-name> --yes
```
