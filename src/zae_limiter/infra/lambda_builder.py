"""Build Lambda deployment packages for zae-limiter aggregator.

Uses aws-lambda-builders to install the ``[lambda]`` extra pip dependencies
(aws-lambda-powertools) for the Lambda target platform (Linux x86_64), then
copies the ``zae_limiter_aggregator`` package and a minimal ``zae_limiter``
stub (``schema.py``, ``bucket.py``, ``models.py``, ``exceptions.py``) into the
artifact.  This ensures:

1. Cross-platform builds work (macOS/Windows host → Linux Lambda)
2. The deployed code matches what's installed locally (dev versions work)
3. The Lambda zip is small (~1-2 MB) — no aioboto3, aiohttp, click, etc.
"""

import importlib.metadata
import io
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path


def _get_runtime_requirements() -> list[str]:
    """Read ``[lambda]`` extra dependencies from installed zae-limiter metadata.

    Only returns the ``lambda`` extra dependencies (e.g. aws-lambda-powertools).
    Core dependencies like aioboto3, boto3, click are *not* needed inside the
    Lambda because the aggregator only uses boto3 (provided by the runtime)
    and aws-lambda-powertools.

    Returns:
        List of PEP 508 dependency strings (e.g. ["aws-lambda-powertools>=2.0.0"])
    """
    requires = importlib.metadata.requires("zae_limiter")
    if requires is None:
        return []

    result = []
    for r in requires:
        if "extra == 'lambda'" in r:
            # Lambda extra dependency — strip the marker
            dep = r.split(";")[0].strip()
            result.append(dep)
    return result


def build_lambda_package() -> bytes:
    """Build Lambda deployment package with cross-platform dependencies.

    Installs ``[lambda]`` extra dependencies via aws-lambda-builders, then
    copies:
    - ``zae_limiter_aggregator/`` (all .py files)
    - ``zae_limiter/__init__.py`` (empty stub — makes it a valid package)
    - ``zae_limiter/schema.py`` (full copy — no external deps)
    - ``zae_limiter/bucket.py`` (refill math for aggregator refill)
    - ``zae_limiter/models.py`` (dataclasses used by bucket.py)
    - ``zae_limiter/exceptions.py`` (exceptions used by models.py)

    The full ``zae_limiter`` package is *not* included (it depends on
    aioboto3 which is not needed by the aggregator).

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

        # Write requirements.txt with [lambda] extra deps only
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

        # Copy the zae_limiter_aggregator package into artifacts
        import zae_limiter_aggregator

        aggregator_path = Path(zae_limiter_aggregator.__file__).parent
        dest_aggregator = artifacts_dir / "zae_limiter_aggregator"

        # Remove any pip-installed copy to use local version
        if dest_aggregator.exists():
            shutil.rmtree(dest_aggregator)

        for src_file in aggregator_path.rglob("*.py"):
            rel = src_file.relative_to(aggregator_path)
            dst = dest_aggregator / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst)

        # Copy minimal zae_limiter stub (schema.py + empty __init__.py)
        import zae_limiter

        zae_limiter_path = Path(zae_limiter.__file__).parent
        dest_zae_limiter = artifacts_dir / "zae_limiter"

        # Remove any pip-installed zae_limiter (from deps resolution)
        if dest_zae_limiter.exists():
            shutil.rmtree(dest_zae_limiter)

        dest_zae_limiter.mkdir(parents=True, exist_ok=True)

        # Empty __init__.py — avoids importing aioboto3 when resolving
        # `from zae_limiter.schema import ...`
        (dest_zae_limiter / "__init__.py").write_text("")

        # Full copy of schema.py — no external deps (only imports typing)
        schema_src = zae_limiter_path / "schema.py"
        shutil.copy2(schema_src, dest_zae_limiter / "schema.py")

        # bucket.py — refill math for aggregator-assisted refill (Issue #317)
        shutil.copy2(zae_limiter_path / "bucket.py", dest_zae_limiter / "bucket.py")

        # models.py — dataclasses used by bucket.py (pure stdlib deps)
        shutil.copy2(zae_limiter_path / "models.py", dest_zae_limiter / "models.py")

        # exceptions.py — exceptions used by models.py (pure stdlib deps)
        shutil.copy2(zae_limiter_path / "exceptions.py", dest_zae_limiter / "exceptions.py")

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
    import zae_limiter_aggregator

    aggregator_path = Path(zae_limiter_aggregator.__file__).parent
    py_files = list(aggregator_path.rglob("*.py"))
    total_size = sum(f.stat().st_size for f in py_files)
    requirements = _get_runtime_requirements()

    return {
        "package_path": str(aggregator_path),
        "python_files": len(py_files),
        "uncompressed_size": total_size,
        "handler": "zae_limiter_aggregator.handler.handler",
        "runtime_dependencies": requirements,
    }
