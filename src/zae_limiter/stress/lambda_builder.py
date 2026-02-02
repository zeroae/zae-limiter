"""Build Lambda deployment packages for stress testing."""

from __future__ import annotations

import shutil
import sys
import tempfile
import zipfile
from pathlib import Path


def _generate_requirements(zae_limiter_source: Path | str) -> str:
    """Generate requirements.txt for Lambda."""
    reqs = [
        "locust>=2.20",
        "gevent>=23.0",
        "nest-asyncio>=1.5.0",  # Allow nested event loops
    ]

    # Add zae-limiter
    if isinstance(zae_limiter_source, Path):
        # Local wheel
        reqs.append(str(zae_limiter_source))
    else:
        # Version string - install from PyPI
        reqs.append(f"zae-limiter=={zae_limiter_source}")

    return "\n".join(reqs) + "\n"


def build_stress_lambda_package(
    zae_limiter_source: Path | str,
    output_dir: Path | None = None,
) -> Path:
    """Build Lambda deployment package using aws-lambda-builders.

    Args:
        zae_limiter_source: Path to wheel or version string
        output_dir: Directory for output zip (default: build/)

    Returns:
        Path to the built zip file
    """
    from aws_lambda_builders.architecture import X86_64
    from aws_lambda_builders.builder import LambdaBuilder

    if output_dir is None:
        output_dir = Path("build")
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        source_dir = tmpdir / "source"
        build_dir = tmpdir / "build"
        artifacts_dir = tmpdir / "artifacts"

        source_dir.mkdir()
        build_dir.mkdir()
        artifacts_dir.mkdir()

        # Create requirements.txt
        requirements = source_dir / "requirements.txt"
        requirements.write_text(_generate_requirements(zae_limiter_source))

        # Create placeholder for aws-lambda-builders
        (source_dir / "__init__.py").touch()

        # Build using aws-lambda-builders
        runtime = f"python{sys.version_info.major}.{sys.version_info.minor}"
        builder = LambdaBuilder(
            language="python",
            dependency_manager="pip",
            application_framework=None,
        )

        builder.build(
            source_dir=str(source_dir),
            artifacts_dir=str(artifacts_dir),
            scratch_dir=str(build_dir),
            manifest_path=str(requirements),
            runtime=runtime,
            architecture=X86_64,
        )

        # Remove placeholder
        placeholder = artifacts_dir / "__init__.py"
        if placeholder.exists():
            placeholder.unlink()

        # Copy stress_lambda package
        stress_lambda_src = Path(__file__).parent / "lambda"
        if stress_lambda_src.exists():
            shutil.copytree(stress_lambda_src, artifacts_dir / "stress_lambda")

        # Copy locustfile
        locustfile_src = Path(__file__).parent / "locustfile.py"
        if locustfile_src.exists():
            shutil.copy(locustfile_src, artifacts_dir / "locustfile.py")

        # Copy distribution module
        distribution_src = Path(__file__).parent / "distribution.py"
        if distribution_src.exists():
            shutil.copy(distribution_src, artifacts_dir / "distribution.py")

        # Copy config module
        config_src = Path(__file__).parent / "config.py"
        if config_src.exists():
            shutil.copy(config_src, artifacts_dir / "config.py")

        # Create zip
        zip_path = output_dir / "stress-lambda.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in artifacts_dir.rglob("*"):
                if file.is_file():
                    zf.write(file, file.relative_to(artifacts_dir))

        return zip_path
