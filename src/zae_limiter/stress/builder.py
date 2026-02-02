"""Build Docker images and Lambda packages for stress testing."""

from __future__ import annotations

import base64
import importlib.metadata
import io
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def get_zae_limiter_source() -> Path | str:
    """Get zae-limiter source for packaging.

    Returns:
        Path to wheel file (development mode) or version string (installed).
    """
    # Check if we're in development mode (pyproject.toml exists)
    # Look for pyproject.toml relative to this file's location
    package_dir = Path(__file__).parent.parent.parent.parent
    pyproject = package_dir / "pyproject.toml"

    if pyproject.exists():
        return _build_wheel(package_dir)

    # Installed mode - return version string for PyPI install
    return importlib.metadata.version("zae_limiter")


def _build_wheel(repo_root: Path) -> Path:
    """Build wheel in development mode."""
    import subprocess

    dist_dir = repo_root / "dist"

    # Clean old wheels
    if dist_dir.exists():
        for old_wheel in dist_dir.glob("zae_limiter-*.whl"):
            old_wheel.unlink()

    # Build new wheel
    subprocess.run(
        ["uv", "build", "--wheel"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )

    # Return the built wheel
    wheels = list(dist_dir.glob("zae_limiter-*.whl"))
    if not wheels:
        raise RuntimeError("Wheel build failed - no wheel found in dist/")

    return wheels[0]


def _get_locustfile_path() -> Path:
    """Get path to locustfile.py."""
    return Path(__file__).parent / "locustfile.py"


def _get_orchestrator_path() -> Path:
    """Get path to orchestrator.py."""
    return Path(__file__).parent / "orchestrator.py"


def _generate_dockerfile(zae_limiter_source: Path | str) -> str:
    """Generate Dockerfile based on how zae-limiter is provided.

    Layer ordering optimized for caching:
    1. Install stable external deps (cached even when wheel changes)
    2. Copy and install wheel
    3. Copy Python files last (invalidates least)
    """
    if isinstance(zae_limiter_source, Path):
        install_cmd = """\
# Install stable deps first (cached layer)
RUN pip install locust gevent asyncio-gevent

# Install wheel (may change frequently)
COPY wheels/*.whl /tmp/
RUN pip install /tmp/*.whl"""
    else:
        # Version string - install from PyPI
        install_cmd = (
            f"RUN pip install zae-limiter=={zae_limiter_source} locust gevent asyncio-gevent"
        )

    return f"""\
FROM python:3.12-slim

{install_cmd}

COPY locustfile.py /mnt/locustfile.py
COPY orchestrator.py /mnt/orchestrator.py

ENTRYPOINT ["locust"]
CMD ["--master", "--master-bind-port=5557", "-f", "/mnt/locustfile.py"]
"""


def _create_build_context(zae_limiter_source: Path | str) -> io.BytesIO:
    """Create Docker build context as tar archive."""
    context = io.BytesIO()

    with tarfile.open(fileobj=context, mode="w:gz") as tar:
        # Add Dockerfile
        dockerfile_content = _generate_dockerfile(zae_limiter_source)
        dockerfile_bytes = dockerfile_content.encode()
        dockerfile_info = tarfile.TarInfo(name="Dockerfile")
        dockerfile_info.size = len(dockerfile_bytes)
        tar.addfile(dockerfile_info, io.BytesIO(dockerfile_bytes))

        # Add wheel if provided
        if isinstance(zae_limiter_source, Path):
            tar.add(zae_limiter_source, arcname=f"wheels/{zae_limiter_source.name}")

        # Add locustfile
        locustfile_path = _get_locustfile_path()
        if locustfile_path.exists():
            tar.add(locustfile_path, arcname="locustfile.py")

        # Add orchestrator for sidecar container
        orchestrator_path = _get_orchestrator_path()
        if orchestrator_path.exists():
            tar.add(orchestrator_path, arcname="orchestrator.py")

    context.seek(0)
    return context


def build_and_push_locust_image(
    stack_name: str,
    region: str,
    zae_limiter_source: Path | str | None = None,
) -> str:
    """Build Locust master image with zae-limiter and push to ECR.

    Args:
        stack_name: Stress test stack name (ECR repo name derived from this)
        region: AWS region
        zae_limiter_source: Path to wheel, version string, or None to auto-detect

    Returns:
        ECR image URI
    """
    try:
        import docker
    except ImportError as e:
        raise RuntimeError(
            "docker package required for building images. "
            "Install with: pip install 'zae-limiter[local]'"
        ) from e

    import boto3

    if zae_limiter_source is None:
        zae_limiter_source = get_zae_limiter_source()

    ecr = boto3.client("ecr", region_name=region)
    sts = boto3.client("sts", region_name=region)

    account_id = sts.get_caller_identity()["Account"]
    repo_name = f"{stack_name}-locust"
    image_uri = f"{account_id}.dkr.ecr.{region}.amazonaws.com/{repo_name}:latest"

    # Get ECR auth token
    auth = ecr.get_authorization_token()
    token = auth["authorizationData"][0]["authorizationToken"]
    username, password = base64.b64decode(token).decode().split(":")
    registry = auth["authorizationData"][0]["proxyEndpoint"]

    # Initialize Docker client
    client = docker.from_env()

    # Login to ECR
    client.login(
        username=username,
        password=password,
        registry=registry,
    )

    # Build image context
    context = _create_build_context(zae_limiter_source)

    # Build image (this can take 15-30s due to pip install)
    import sys

    print("  Building Docker image (this may take 15-30s)...", file=sys.stderr)
    sys.stderr.flush()
    image, build_logs = client.images.build(
        fileobj=context,
        custom_context=True,
        tag=image_uri,
        rm=True,
        platform="linux/amd64",
    )
    print("  Docker image built", file=sys.stderr)

    # Push to ECR
    print("  Pushing to ECR...", file=sys.stderr)
    sys.stderr.flush()
    for line in client.images.push(image_uri, stream=True, decode=True):
        if "error" in line:
            raise RuntimeError(f"Push failed: {line['error']}")
    print("  Push complete", file=sys.stderr)

    return image_uri
