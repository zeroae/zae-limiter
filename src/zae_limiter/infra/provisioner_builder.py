"""Build Lambda deployment packages for zae-limiter provisioner.

Uses aws-lambda-builders to install dependencies (pyyaml,
aws-lambda-powertools) for the Lambda target platform, then copies the
``zae_limiter_provisioner`` package and a minimal ``zae_limiter`` stub
(``schema.py``, ``models.py``, ``exceptions.py``) into the artifact.
"""

import importlib.metadata
import io
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path


def _get_runtime_requirements() -> list[str]:
    """Read ``[lambda]`` extra dependencies plus pyyaml.

    The provisioner needs:
    - pyyaml (YAML manifest parsing)
    - aws-lambda-powertools (Lambda utilities, shared with aggregator)

    Returns:
        List of PEP 508 dependency strings.
    """
    requires = importlib.metadata.requires("zae_limiter")
    if requires is None:
        return ["pyyaml>=6.0"]

    result = []
    for r in requires:
        if "extra == 'lambda'" in r:
            dep = r.split(";")[0].strip()
            result.append(dep)

    # pyyaml is a core dep but needed explicitly in Lambda
    dep_names = [r.split(">=")[0].split(">")[0].split("==")[0].lower() for r in result]
    if "pyyaml" not in [n.lower() for n in dep_names]:
        result.append("pyyaml>=6.0")

    return result


def build_provisioner_package() -> bytes:
    """Build Lambda deployment package for the provisioner.

    Installs dependencies via aws-lambda-builders, then copies:
    - ``zae_limiter_provisioner/`` (all .py files)
    - ``zae_limiter/__init__.py`` (empty stub)
    - ``zae_limiter/schema.py`` (key builders, no external deps)
    - ``zae_limiter/models.py`` (dataclasses used by schema)
    - ``zae_limiter/exceptions.py`` (exceptions used by models)

    Returns:
        Zip file contents as bytes.
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

        # Write requirements.txt
        requirements = _get_runtime_requirements()
        requirements_txt = source_dir / "requirements.txt"
        requirements_txt.write_text("\n".join(requirements) + "\n")

        # Placeholder for aws-lambda-builders
        (source_dir / "__init__.py").touch()

        # Install dependencies
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

        # Remove placeholder
        placeholder = artifacts_dir / "__init__.py"
        if placeholder.exists():
            placeholder.unlink()

        # Copy zae_limiter_provisioner package
        import zae_limiter_provisioner

        provisioner_path = Path(zae_limiter_provisioner.__file__).parent
        dest_provisioner = artifacts_dir / "zae_limiter_provisioner"

        if dest_provisioner.exists():
            shutil.rmtree(dest_provisioner)

        for src_file in provisioner_path.rglob("*.py"):
            rel = src_file.relative_to(provisioner_path)
            dst = dest_provisioner / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst)

        # Copy minimal zae_limiter stub
        import zae_limiter

        zae_limiter_path = Path(zae_limiter.__file__).parent
        dest_zae_limiter = artifacts_dir / "zae_limiter"

        if dest_zae_limiter.exists():
            shutil.rmtree(dest_zae_limiter)

        dest_zae_limiter.mkdir(parents=True, exist_ok=True)

        # Empty __init__.py
        (dest_zae_limiter / "__init__.py").write_text("")

        # schema.py — key builders (no external deps)
        shutil.copy2(zae_limiter_path / "schema.py", dest_zae_limiter / "schema.py")

        # models.py — dataclasses used by schema
        shutil.copy2(zae_limiter_path / "models.py", dest_zae_limiter / "models.py")

        # exceptions.py — exceptions used by models
        shutil.copy2(
            zae_limiter_path / "exceptions.py",
            dest_zae_limiter / "exceptions.py",
        )

        # Create zip
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in artifacts_dir.rglob("*"):
                if file_path.is_file():
                    arcname = file_path.relative_to(artifacts_dir)
                    zf.write(file_path, arcname)

        zip_buffer.seek(0)
        return zip_buffer.getvalue()


def get_provisioner_info() -> dict[str, str | int | list[str]]:
    """Get information about the provisioner Lambda package.

    Returns:
        Dict with package metadata.
    """
    import zae_limiter_provisioner

    provisioner_path = Path(zae_limiter_provisioner.__file__).parent
    py_files = list(provisioner_path.rglob("*.py"))
    total_size = sum(f.stat().st_size for f in py_files)
    requirements = _get_runtime_requirements()

    return {
        "package_path": str(provisioner_path),
        "python_files": len(py_files),
        "uncompressed_size": total_size,
        "handler": "zae_limiter_provisioner.handler.on_event",
        "runtime_dependencies": requirements,
    }
