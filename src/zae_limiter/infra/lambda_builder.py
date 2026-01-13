"""Build Lambda deployment packages for zae-limiter aggregator."""

import io
import zipfile
from pathlib import Path


def build_lambda_package() -> bytes:
    """
    Build Lambda deployment package from installed zae_limiter package.

    Creates a zip file containing the entire zae_limiter package, which
    includes all necessary code for the aggregator Lambda function.

    The package only depends on boto3, which is provided by the Lambda
    runtime, so no external dependencies need to be bundled.

    Returns:
        Zip file contents as bytes
    """
    import zae_limiter

    # Find installed package location
    package_path = Path(zae_limiter.__file__).parent

    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        # Add all Python files from the package
        for py_file in package_path.rglob("*.py"):
            # Create archive name: zae_limiter/...
            arcname = py_file.relative_to(package_path.parent)
            zf.write(py_file, arcname)

        # Also include the CloudFormation templates for reference
        # (not used by Lambda, but useful for debugging)
        for template_name in ("cfn_template.yaml", "cfn_admin_template.yaml"):
            cfn_template = package_path / "infra" / template_name
            if cfn_template.exists():
                arcname = cfn_template.relative_to(package_path.parent)
                zf.write(cfn_template, arcname)

    zip_buffer.seek(0)
    return zip_buffer.getvalue()


def build_admin_lambda_package() -> bytes:
    """
    Build Lambda deployment package for Admin API.

    This is an alias for build_lambda_package() since the admin handler
    is part of the same package.

    Returns:
        Zip file contents as bytes
    """
    return build_lambda_package()


def write_lambda_package(output_path: str | Path) -> int:
    """
    Build and write Lambda package to a file.

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


def get_package_info() -> dict[str, str | int]:
    """
    Get information about the Lambda package without building it.

    Returns:
        Dict with package metadata
    """
    import zae_limiter

    package_path = Path(zae_limiter.__file__).parent

    # Count files
    py_files = list(package_path.rglob("*.py"))
    total_size = sum(f.stat().st_size for f in py_files)

    return {
        "package_path": str(package_path),
        "python_files": len(py_files),
        "uncompressed_size": total_size,
        "handler": "zae_limiter.aggregator.handler.handler",
    }
