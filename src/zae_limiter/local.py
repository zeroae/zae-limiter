"""Local development commands for managing LocalStack containers.

This module provides CLI commands to manage a LocalStack container
for local development and testing. It uses the Docker SDK (optional
dependency) to manage the container lifecycle.

Install the optional dependency:
    pip install 'zae-limiter[local]'
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from typing import Any

import click

# Lazy import: docker is an optional dependency.
# All functions reference this module-level name so that tests can patch it.
# When docker is not installed, this is set to None and _get_docker_client()
# will exit with a helpful message before any docker.errors usage.


def _import_docker() -> Any:
    try:
        import docker as _mod

        return _mod
    except ImportError:
        return None


docker: Any = _import_docker()

# ---------------------------------------------------------------------------
# Container configuration constants (source of truth)
#
# docker-compose.yml and .github/workflows/ci.yml must stay in sync
# with these values. See CLAUDE.md "LocalStack Configuration Parity".
# ---------------------------------------------------------------------------

CONTAINER_NAME = "zae-limiter-localstack"
DEFAULT_IMAGE = "localstack/localstack:4"
DEFAULT_PORT = 4566
LOCALSTACK_SERVICES = (
    "dynamodb,dynamodbstreams,lambda,cloudformation,"
    "logs,iam,cloudwatch,sqs,s3,sts,resourcegroupstaggingapi"
)
PERSISTENCE_DIR = os.path.join(tempfile.gettempdir(), "localstack")

# Health check configuration
HEALTH_CHECK_INTERVAL = 5  # seconds between polls
HEALTH_CHECK_RETRIES = 30  # max retries (total wait: 150s)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _get_docker_client(docker_host: str | None = None) -> Any:
    """Get a Docker client, with a helpful error if the package is missing.

    Args:
        docker_host: Optional Docker daemon URL override.

    Returns:
        docker.DockerClient instance.

    Raises:
        SystemExit: If docker is not installed or Docker daemon is not reachable.
    """
    if docker is None:
        click.echo(
            "Error: docker package is required for local commands.\n"
            "Install with: pip install 'zae-limiter[local]'",
            err=True,
        )
        sys.exit(1)

    try:
        if docker_host:
            return docker.DockerClient(base_url=docker_host)
        return docker.from_env()
    except docker.errors.DockerException as e:
        click.echo(f"Error: Could not connect to Docker: {e}", err=True)
        sys.exit(1)


def _find_container(client: Any) -> Any | None:
    """Find the LocalStack container by name.

    Returns:
        Container object if found, None otherwise.
    """
    try:
        return client.containers.get(CONTAINER_NAME)
    except docker.errors.NotFound:
        return None


def _ensure_image(client: Any, image: str) -> None:
    """Pull the Docker image if not present locally.

    Shows progress feedback during pull to avoid the appearance of a hang.

    Args:
        client: Docker client.
        image: Image name and tag.
    """
    try:
        client.images.get(image)
    except docker.errors.ImageNotFound:
        click.echo(f"Pulling image {image} (this may take a few minutes)...")
        try:
            client.images.pull(image)
            click.echo(f"Image {image} pulled successfully")
        except docker.errors.APIError as e:
            click.echo(f"Error: Failed to pull image {image}: {e}", err=True)
            sys.exit(1)


def _wait_for_health(container: Any) -> bool:
    """Wait for the container to become healthy by polling Docker health status.

    Uses Docker's built-in healthcheck mechanism. The container runs
    ``curl -f http://localhost:4566/_localstack/health`` internally;
    this function polls the health status via the Docker API.

    Fails fast if the container exits or crashes instead of waiting
    for the full timeout.

    Args:
        container: Docker container object.

    Returns:
        True if healthy, False if timeout exceeded or container failed.
    """
    click.echo("Waiting for LocalStack to become healthy...", nl=False)

    for _ in range(HEALTH_CHECK_RETRIES):
        container.reload()

        # Fail fast if container exited or crashed
        container_status = container.status
        if container_status in ("exited", "dead"):
            click.echo(f" container {container_status}")
            # Show last few log lines for debugging
            try:
                log_tail = container.logs(tail=10).decode("utf-8", errors="replace")
                if log_tail.strip():
                    click.echo("Container logs:", err=True)
                    click.echo(log_tail, err=True)
            except Exception:
                pass
            return False

        health = container.attrs.get("State", {}).get("Health", {}).get("Status")
        if health == "healthy":
            click.echo(" ready")
            return True
        if health == "unhealthy":
            click.echo(" failed")
            return False
        time.sleep(HEALTH_CHECK_INTERVAL)
        click.echo(".", nl=False)

    click.echo(" timeout")
    return False


# ---------------------------------------------------------------------------
# Environment variable helpers
# ---------------------------------------------------------------------------


def _localstack_env_vars(port: int = DEFAULT_PORT) -> dict[str, str]:
    """Return the LocalStack environment variables as a dict.

    Args:
        port: Host port for LocalStack endpoint.

    Returns:
        Dict mapping env var names to their values.
    """
    return {
        "AWS_ENDPOINT_URL": f"http://localhost:{port}",
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_DEFAULT_REGION": "us-east-1",
    }


def _format_env_vars(port: int = DEFAULT_PORT, fmt: str = "eval") -> str:
    """Format LocalStack environment variables for shell output.

    Args:
        port: Host port for LocalStack endpoint.
        fmt: Output format - ``eval``, ``direnv``, or ``powershell``.

    Returns:
        Formatted string with one variable per line.
    """
    env_vars = _localstack_env_vars(port)
    lines: list[str] = []
    for key, value in env_vars.items():
        if fmt == "eval":
            lines.append(f"export {key}={value}")
        elif fmt == "direnv":
            lines.append(f"{key}={value}")
        elif fmt == "powershell":
            lines.append(f'$env:{key} = "{value}"')
    return "\n".join(lines)


def _echo_env_hint() -> None:
    """Print shell configuration hints pointing to ``local env``."""
    click.echo()
    click.echo("To configure your shell:")
    click.echo('  eval "$(zae-limiter local env)"                 # bash/zsh')
    click.echo("  zae-limiter local env --format direnv > .envrc  # direnv")
    click.echo("  zae-limiter local env --format powershell       # PowerShell")


# ---------------------------------------------------------------------------
# Click command group
# ---------------------------------------------------------------------------

local = click.Group(
    name="local",
    help="Local development with LocalStack.\n\n"
    "Requires the [local] extra:\n\n"
    "    pip install 'zae-limiter[local]'",
)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@local.command()
@click.option(
    "--format",
    "-f",
    "fmt",
    type=click.Choice(["eval", "direnv", "powershell"], case_sensitive=False),
    default="eval",
    show_default=True,
    help="Output format",
)
@click.option(
    "--port",
    default=DEFAULT_PORT,
    type=int,
    show_default=True,
    help="Host port for LocalStack endpoint",
)
def env(fmt: str, port: int) -> None:
    """Output LocalStack environment variables.

    Prints the environment variables needed to connect to LocalStack.
    Does not require Docker or a running container.

    \b
    Examples:
      eval "$(zae-limiter local env)"                 # bash/zsh
      zae-limiter local env --format direnv > .envrc  # direnv
      zae-limiter local env --format powershell       # PowerShell
    """
    click.echo(_format_env_vars(port=port, fmt=fmt))


@local.command()
@click.option(
    "--docker-host",
    envvar="DOCKER_HOST",
    help="Docker daemon URL (e.g., unix:///path/to/docker.sock)",
)
@click.option(
    "--image",
    default=DEFAULT_IMAGE,
    show_default=True,
    help="LocalStack Docker image",
)
@click.option(
    "--name",
    "-n",
    "stack_name",
    default=None,
    help="Stack name to include in deploy instructions",
)
@click.option(
    "--port",
    default=DEFAULT_PORT,
    type=int,
    show_default=True,
    help="Host port to bind LocalStack to",
)
def up(
    docker_host: str | None,
    image: str,
    stack_name: str | None,
    port: int,
) -> None:
    """Start LocalStack for local development."""
    client = _get_docker_client(docker_host)

    # Check if already running
    existing = _find_container(client)
    if existing is not None:
        if existing.status == "running":
            click.echo(f"LocalStack is already running at http://localhost:{port}")
            return
        # Remove stopped container so we can recreate with current config
        click.echo("Removing stopped container...")
        existing.remove()

    # Ensure image is available (shows progress if pulling)
    _ensure_image(client, image)

    click.echo(f"Starting LocalStack ({image})...")

    try:
        container = client.containers.run(
            image,
            detach=True,
            name=CONTAINER_NAME,
            ports={f"{DEFAULT_PORT}/tcp": port},
            environment={
                "SERVICES": LOCALSTACK_SERVICES,
                "DEBUG": "0",
                "DYNAMODB_REMOVE_EXPIRED_ITEMS": "1",
            },
            volumes={
                "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
                PERSISTENCE_DIR: {"bind": "/var/lib/localstack", "mode": "rw"},
            },
            healthcheck={
                "test": ["CMD", "curl", "-f", "http://localhost:4566/_localstack/health"],
                "interval": HEALTH_CHECK_INTERVAL * 1_000_000_000,  # nanoseconds
                "timeout": HEALTH_CHECK_INTERVAL * 1_000_000_000,
                "retries": HEALTH_CHECK_RETRIES,
            },
        )
    except docker.errors.APIError as e:
        error_msg = str(e)
        if "port is already allocated" in error_msg or "address already in use" in error_msg:
            click.echo(
                f"Error: Port {port} is already in use. Stop existing LocalStack or use --port.",
                err=True,
            )
        else:
            click.echo(f"Error: Failed to start container: {e}", err=True)
        sys.exit(1)

    if not _wait_for_health(container):
        click.echo("Error: LocalStack did not become healthy", err=True)
        sys.exit(1)

    endpoint = f"http://localhost:{port}"
    click.echo(f"LocalStack is ready at {endpoint}")
    _echo_env_hint()

    if stack_name:
        click.echo()
        click.echo("Or deploy directly:")
        click.echo(
            f"  zae-limiter deploy --name {stack_name} --endpoint-url {endpoint} --region us-east-1"
        )


@local.command()
@click.option(
    "--docker-host",
    envvar="DOCKER_HOST",
    help="Docker daemon URL (e.g., unix:///path/to/docker.sock)",
)
def down(docker_host: str | None) -> None:
    """Stop LocalStack."""
    client = _get_docker_client(docker_host)
    container = _find_container(client)

    if container is None:
        click.echo("LocalStack is not running.")
        return

    click.echo("Stopping LocalStack...")
    try:
        container.stop(timeout=10)
        container.remove()
    except docker.errors.NotFound:
        pass  # Already removed
    except docker.errors.APIError as e:
        click.echo(f"Error: Failed to stop container: {e}", err=True)
        sys.exit(1)
    click.echo("LocalStack stopped.")


@local.command()
@click.option(
    "--docker-host",
    envvar="DOCKER_HOST",
    help="Docker daemon URL (e.g., unix:///path/to/docker.sock)",
)
def status(docker_host: str | None) -> None:
    """Show LocalStack container status."""
    client = _get_docker_client(docker_host)
    container = _find_container(client)

    if container is None:
        click.echo("LocalStack: not running")
        return

    try:
        container.reload()
    except docker.errors.NotFound:
        click.echo("LocalStack: not running")
        return

    state = container.status
    health = container.attrs.get("State", {}).get("Health", {}).get("Status", "unknown")

    # Extract bound port
    ports = container.attrs.get("NetworkSettings", {}).get("Ports", {})
    port_bindings = ports.get("4566/tcp", [])
    host_port = port_bindings[0]["HostPort"] if port_bindings else str(DEFAULT_PORT)

    endpoint = f"http://localhost:{host_port}"
    image = container.attrs.get("Config", {}).get("Image", "unknown")

    click.echo(f"LocalStack: {state}")
    click.echo(f"Endpoint:   {endpoint}")
    click.echo(f"Health:     {health}")
    click.echo(f"Image:      {image}")
    click.echo(f"Services:   {LOCALSTACK_SERVICES}")

    if state == "running" and health == "healthy":
        _echo_env_hint()


@local.command()
@click.option(
    "--docker-host",
    envvar="DOCKER_HOST",
    help="Docker daemon URL (e.g., unix:///path/to/docker.sock)",
)
@click.option(
    "--follow",
    "-f",
    is_flag=True,
    help="Follow log output",
)
@click.option(
    "--tail",
    default=100,
    type=int,
    help="Number of lines to show from end of logs",
)
def logs(docker_host: str | None, follow: bool, tail: int) -> None:
    """Show LocalStack container logs."""
    client = _get_docker_client(docker_host)
    container = _find_container(client)

    if container is None:
        click.echo("Error: LocalStack is not running. Run 'zae-limiter local up' first.", err=True)
        sys.exit(1)

    try:
        if follow:
            for line in container.logs(stream=True, follow=True, tail=tail):
                click.echo(line.decode("utf-8", errors="replace"), nl=False)
        else:
            output = container.logs(tail=tail)
            click.echo(output.decode("utf-8", errors="replace"), nl=False)
    except KeyboardInterrupt:
        pass
    except docker.errors.NotFound:
        click.echo("\nContainer stopped.", err=True)
    except docker.errors.APIError as e:
        click.echo(f"\nError: Lost connection to container: {e}", err=True)
        sys.exit(1)
