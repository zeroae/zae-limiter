"""Build Docker images and Lambda packages for load testing."""

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


def _get_orchestrator_path() -> Path:
    """Get path to orchestrator.py."""
    return Path(__file__).parent / "orchestrator.py"


def _generate_dockerfile(
    zae_limiter_source: Path | str,
    *,
    locustfile: str = "locustfile.py",
    has_user_requirements: bool = False,
) -> str:
    """Generate Dockerfile based on how zae-limiter is provided.

    Layer ordering optimized for caching:
    1. Install stable external deps (cached even when wheel changes)
    2. Install user requirements (cached separately)
    3. Copy and install wheel
    4. Copy Python files last (invalidates least)

    The LOCUSTFILE env var can be overridden at runtime via ECS task definition
    or container environment to select different locustfiles without rebuilding.
    """
    if isinstance(zae_limiter_source, Path):
        install_cmd = """\
# Install stable deps first (cached layer)
RUN pip install locust gevent boto3

# Install wheel (may change frequently)
COPY wheels/*.whl /tmp/
RUN pip install /tmp/*.whl"""
    else:
        # Version string - install from PyPI
        install_cmd = f"RUN pip install zae-limiter=={zae_limiter_source} locust gevent boto3"

    user_requirements_cmd = ""
    if has_user_requirements:
        user_requirements_cmd = """
# Install user dependencies (cached — only invalidated when requirements.txt changes)
COPY userfiles/requirements.txt /tmp/user-requirements.txt
RUN pip install -r /tmp/user-requirements.txt
"""

    return f"""\
FROM python:3.12-slim

{install_cmd}
{user_requirements_cmd}
# Copy all user files (locustfiles and supporting modules)
COPY userfiles/ /mnt/

# Copy orchestrator for sidecar container
COPY orchestrator.py /mnt/orchestrator.py

# Add /mnt to PYTHONPATH so locustfiles can import sibling packages (e.g., common/)
ENV PYTHONPATH=/mnt

# Default locustfile (can be overridden at runtime via LOCUSTFILE env var)
ENV LOCUSTFILE={locustfile}

# Master mode args (set to empty string for standalone mode via env override)
ENV LOCUST_MASTER_ARGS="--master --master-bind-port=5557 --enable-rebalancing --class-picker"

# Use shell form to enable variable substitution
ENTRYPOINT ["sh", "-c", "locust $LOCUST_MASTER_ARGS -f /mnt/$LOCUSTFILE"]
"""


def _create_build_context(
    zae_limiter_source: Path | str,
    locustfile_dir: Path,
    *,
    locustfile: str = "locustfile.py",
) -> io.BytesIO:
    """Create Docker build context as tar archive.

    Copies all files from locustfile_dir into the image to support examples
    with different file structures.
    """
    context = io.BytesIO()
    has_user_requirements = (locustfile_dir / "requirements.txt").exists()

    with tarfile.open(fileobj=context, mode="w:gz") as tar:
        # Add Dockerfile
        dockerfile_content = _generate_dockerfile(
            zae_limiter_source,
            locustfile=locustfile,
            has_user_requirements=has_user_requirements,
        )
        dockerfile_bytes = dockerfile_content.encode()
        dockerfile_info = tarfile.TarInfo(name="Dockerfile")
        dockerfile_info.size = len(dockerfile_bytes)
        tar.addfile(dockerfile_info, io.BytesIO(dockerfile_bytes))

        # Add wheel if provided
        if isinstance(zae_limiter_source, Path):
            tar.add(zae_limiter_source, arcname=f"wheels/{zae_limiter_source.name}")

        # Add all files and directories from locustfile_dir to userfiles/
        for item in locustfile_dir.iterdir():
            tar.add(item, arcname=f"userfiles/{item.name}")

        # Add orchestrator for sidecar container (always from load module)
        orchestrator_path = _get_orchestrator_path()
        if orchestrator_path.exists():
            tar.add(orchestrator_path, arcname="orchestrator.py")

    context.seek(0)
    return context


def build_and_push_locust_image(
    stack_name: str,
    region: str,
    locustfile_dir: Path,
    zae_limiter_source: Path | str | None = None,
    *,
    locustfile: str = "locustfile.py",
) -> str:
    """Build Locust master image with zae-limiter and push to ECR.

    Args:
        stack_name: Load test stack name (ECR repo name derived from this)
        region: AWS region
        locustfile_dir: Directory containing locustfile.py
        zae_limiter_source: Path to wheel, version string, or None to auto-detect
        locustfile: Locustfile path relative to locustfile_dir

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

    # Initialize Docker client
    client = docker.from_env()

    # Build image context
    context = _create_build_context(zae_limiter_source, locustfile_dir, locustfile=locustfile)

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
    auth_config = {"username": username, "password": password}
    for line in client.images.push(image_uri, stream=True, decode=True, auth_config=auth_config):
        if "error" in line:
            raise RuntimeError(f"Push failed: {line['error']}")
    print("  Push complete", file=sys.stderr)

    return image_uri
