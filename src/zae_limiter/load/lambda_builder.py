"""Build Lambda deployment packages for load testing."""

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
    ]

    # Add zae-limiter
    if isinstance(zae_limiter_source, Path):
        # Local wheel
        reqs.append(str(zae_limiter_source))
    else:
        # Version string - install from PyPI
        reqs.append(f"zae-limiter=={zae_limiter_source}")

    return "\n".join(reqs) + "\n"


def build_load_lambda_package(
    zae_limiter_source: Path | str,
    locustfile_dir: Path,
    output_dir: Path | None = None,
) -> Path:
    """Build Lambda deployment package using aws-lambda-builders.

    Args:
        zae_limiter_source: Path to wheel or version string
        locustfile_dir: Directory containing locustfile.py and supporting modules
        output_dir: Directory for output zip (default: build/)

    Returns:
        Path to the built zip file

    Note:
        All files in locustfile_dir are included in the package,
        supporting examples with different file structures.
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

        # Copy load_lambda package (handlers)
        load_lambda_src = Path(__file__).parent / "lambda"
        if load_lambda_src.exists():
            shutil.copytree(load_lambda_src, artifacts_dir / "load_lambda")

        # Copy all files from locustfile_dir
        for f in locustfile_dir.iterdir():
            if f.is_file():
                shutil.copy(f, artifacts_dir / f.name)

        # Create zip
        zip_path = output_dir / "load-lambda.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in artifacts_dir.rglob("*"):
                if file.is_file():
                    zf.write(file, file.relative_to(artifacts_dir))

        return zip_path
