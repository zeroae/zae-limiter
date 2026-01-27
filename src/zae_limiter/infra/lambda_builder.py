"""Build Lambda deployment packages for zae-limiter aggregator.

Uses aws-lambda-builders to install runtime dependencies for the Lambda
target platform (Linux x86_64), then copies the locally installed
zae_limiter package into the artifact. This ensures:

1. Cross-platform builds work (macOS/Windows host → Linux Lambda)
2. The deployed code matches what's installed locally (dev versions work)
3. Native extensions (aiohttp, etc.) are compiled for the correct platform
"""

import importlib.metadata
import io
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path


def _get_runtime_requirements() -> list[str]:
    """Read runtime dependencies from installed zae-limiter metadata.

    Includes core dependencies and the ``lambda`` extra dependencies
    (e.g. aws-lambda-powertools).

    Returns:
        List of PEP 508 dependency strings (e.g. ["aioboto3>=12.0.0", ...])
    """
    requires = importlib.metadata.requires("zae_limiter")
    if requires is None:
        return []

    result = []
    for r in requires:
        if "extra ==" not in r:
            # Core dependency (no extra marker)
            result.append(r)
        elif "extra == 'lambda'" in r:
            # Lambda extra dependency — strip the marker
            dep = r.split(";")[0].strip()
            result.append(dep)
    return result


def build_lambda_package() -> bytes:
    """Build Lambda deployment package with cross-platform dependencies.

    Uses aws-lambda-builders to install runtime dependencies (aioboto3,
    boto3, etc.) for the Lambda target platform, then copies the locally
    installed zae_limiter package on top.

    Returns:
        Zip file contents as bytes
    """

    from aws_lambda_builders.architecture import X86_64
    from aws_lambda_builders.builder import LambdaBuilder

    with tempfile.TemporaryDirectory() as temp_root:
        temp_path = Path(temp_root)
        source_dir = temp_path / "source"
        artifacts_dir = temp_path / "artifacts"
        scratch_dir = temp_path / "scratch"

        source_dir.mkdir()
        artifacts_dir.mkdir()
        scratch_dir.mkdir()

        # Write requirements.txt with runtime deps only (not zae-limiter itself)
        requirements = _get_runtime_requirements()
        requirements_txt = source_dir / "requirements.txt"
        requirements_txt.write_text("\n".join(requirements) + "\n")

        # aws-lambda-builders requires a source directory; create minimal placeholder
        (source_dir / "__init__.py").touch()

        # Install dependencies for the Lambda target platform
        runtime = f"python{sys.version_info.major}.{sys.version_info.minor}"
        builder = LambdaBuilder(
            language="python",
            dependency_manager="pip",
            application_framework=None,
        )
        builder.build(
            source_dir=str(source_dir),
            artifacts_dir=str(artifacts_dir),
            scratch_dir=str(scratch_dir),
            manifest_path=str(requirements_txt),
            runtime=runtime,
            architecture=X86_64,
        )

        # Remove the placeholder __init__.py from artifacts
        placeholder = artifacts_dir / "__init__.py"
        if placeholder.exists():
            placeholder.unlink()

        # Copy the locally installed zae_limiter package into artifacts
        import zae_limiter

        package_path = Path(zae_limiter.__file__).parent
        dest_path = artifacts_dir / "zae_limiter"

        # Remove any pip-installed zae_limiter (from deps resolution) to use local copy
        if dest_path.exists():
            shutil.rmtree(dest_path)

        for src_file in package_path.rglob("*.py"):
            rel = src_file.relative_to(package_path)
            dst = dest_path / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst)

        # Include CloudFormation template (useful for debugging)
        cfn_template = package_path / "infra" / "cfn_template.yaml"
        if cfn_template.exists():
            dst = dest_path / "infra" / "cfn_template.yaml"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cfn_template, dst)

        # Create zip from artifacts
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in artifacts_dir.rglob("*"):
                if file_path.is_file():
                    arcname = file_path.relative_to(artifacts_dir)
                    zf.write(file_path, arcname)

        zip_buffer.seek(0)
        return zip_buffer.getvalue()


def write_lambda_package(output_path: str | Path) -> int:
    """Build and write Lambda package to a file.

    Args:
        output_path: Path where to write the zip file

    Returns:
        Size of the written file in bytes
    """
    zip_bytes = build_lambda_package()
    output_path = Path(output_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(zip_bytes)

    return len(zip_bytes)


def get_package_info() -> dict[str, str | int | list[str]]:
    """Get information about the Lambda package without building it.

    Returns:
        Dict with package metadata
    """
    import zae_limiter

    package_path = Path(zae_limiter.__file__).parent
    py_files = list(package_path.rglob("*.py"))
    total_size = sum(f.stat().st_size for f in py_files)
    requirements = _get_runtime_requirements()

    return {
        "package_path": str(package_path),
        "python_files": len(py_files),
        "uncompressed_size": total_size,
        "handler": "zae_limiter.aggregator.handler.handler",
        "runtime_dependencies": requirements,
    }
