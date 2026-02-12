"""Integration tests for Lambda package builder.

Tests the aws-lambda-builders pipeline (real pip install) and Docker-based
Lambda runtime validation.
"""

import io
import json
import subprocess
import zipfile
from pathlib import Path

import pytest


@pytest.mark.integration
class TestBuildLambdaPackageIntegration:
    """Integration tests for build_lambda_package using real aws-lambda-builders.

    These tests exercise the full build pipeline (no mocks) to verify that
    aws-lambda-builders correctly installs cross-platform dependencies and
    the zae_limiter_aggregator package + schema stub are included.
    """

    def test_build_produces_valid_zip(self) -> None:
        """Build produces a valid zip file with real dependencies."""
        from zae_limiter.infra.lambda_builder import build_lambda_package

        zip_bytes = build_lambda_package()
        assert isinstance(zip_bytes, bytes)
        assert len(zip_bytes) > 0

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            assert zf.testzip() is None

    def test_zip_contains_lambda_extra_dependencies(self) -> None:
        """Built zip includes [lambda] extra dependencies."""
        from zae_limiter.infra.lambda_builder import build_lambda_package

        zip_bytes = build_lambda_package()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            files = set(zf.namelist())

            # aws-lambda-powertools is in the [lambda] extra
            powertools_files = [f for f in files if f.startswith("aws_lambda_powertools/")]
            assert len(powertools_files) > 0, "aws-lambda-powertools not found in zip"

    def test_zip_does_not_contain_core_deps(self) -> None:
        """Built zip does NOT include core deps (aioboto3, aiohttp, click, etc.)."""
        from zae_limiter.infra.lambda_builder import build_lambda_package

        zip_bytes = build_lambda_package()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            files = set(zf.namelist())

            # These should NOT be in the zip
            aioboto3_files = [f for f in files if f.startswith("aioboto3/")]
            assert len(aioboto3_files) == 0, "aioboto3 should not be in zip"

            aiohttp_files = [f for f in files if f.startswith("aiohttp/")]
            assert len(aiohttp_files) == 0, "aiohttp should not be in zip"

            click_files = [f for f in files if f.startswith("click/")]
            assert len(click_files) == 0, "click should not be in zip"

    def test_zip_contains_aggregator_and_schema_stub(self) -> None:
        """Built zip includes aggregator package and schema stub."""
        from zae_limiter.infra.lambda_builder import build_lambda_package

        zip_bytes = build_lambda_package()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            files = set(zf.namelist())

            # Aggregator package (top-level)
            assert "zae_limiter_aggregator/__init__.py" in files
            assert "zae_limiter_aggregator/handler.py" in files
            assert "zae_limiter_aggregator/processor.py" in files

            # Schema stub
            assert "zae_limiter/__init__.py" in files
            assert "zae_limiter/schema.py" in files

            # zae_limiter/__init__.py should be empty stub
            content = zf.read("zae_limiter/__init__.py").decode()
            assert content == ""

    def test_write_lambda_package_to_file(self, tmp_path: Path) -> None:
        """write_lambda_package writes a valid zip to disk."""
        from zae_limiter.infra.lambda_builder import write_lambda_package

        output_path = tmp_path / "lambda.zip"
        size = write_lambda_package(output_path)

        assert output_path.exists()
        assert output_path.stat().st_size == size

        with zipfile.ZipFile(output_path) as zf:
            assert zf.testzip() is None
            assert "zae_limiter_aggregator/handler.py" in zf.namelist()


@pytest.mark.integration
class TestLambdaInDocker:
    """Integration tests running Lambda in Docker container.

    Tests the actual Lambda package in the Python 3.12 Lambda runtime to ensure
    the aggregator can import and execute with all dependencies bundled.
    """

    @pytest.fixture(autouse=True)
    def check_docker_available(self) -> None:
        """Check if Docker is available, skip if not."""
        try:
            subprocess.run(
                ["docker", "info"],
                capture_output=True,
                check=True,
                timeout=5,
            )
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            pytest.skip("Docker not available")

    @pytest.fixture
    def lambda_zip_path(self, tmp_path: Path) -> Path:
        """Build Lambda package and return path."""
        from zae_limiter.infra.lambda_builder import write_lambda_package

        zip_path = tmp_path / "lambda.zip"
        write_lambda_package(zip_path)
        return zip_path

    def test_lambda_imports_in_docker(self, lambda_zip_path: Path) -> None:
        """Test that Lambda package can import successfully in Docker."""
        # Create a test script that tries to import
        test_script = """
import sys
import zipfile

# Extract the zip
with zipfile.ZipFile('/var/task/function.zip', 'r') as zf:
    zf.extractall('/tmp/lambda')

sys.path.insert(0, '/tmp/lambda')

# Import handler from new top-level package
from zae_limiter_aggregator.handler import handler
from zae_limiter_aggregator.processor import process_stream_records

print("SUCCESS: All imports worked")
"""

        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "python",
                "-v",
                f"{lambda_zip_path}:/var/task/function.zip:ro",
                "public.ecr.aws/lambda/python:3.12",
                "-c",
                test_script,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, f"Docker test failed:\n{result.stderr}"
        assert "SUCCESS" in result.stdout

    def test_lambda_handler_executes_in_docker(self, lambda_zip_path: Path) -> None:
        """Test that Lambda handler executes successfully in Docker."""
        test_script = """
import sys
import zipfile
import json

# Extract the zip
with zipfile.ZipFile('/var/task/function.zip', 'r') as zf:
    zf.extractall('/tmp/lambda')

sys.path.insert(0, '/tmp/lambda')

from zae_limiter_aggregator.handler import handler

# Test with empty event
event = {"Records": []}
result = handler(event, None)

print(json.dumps(result))
"""

        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "python",
                "-v",
                f"{lambda_zip_path}:/var/task/function.zip:ro",
                "public.ecr.aws/lambda/python:3.12",
                "-c",
                test_script,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, f"Docker test failed:\n{result.stderr}"

        # Parse output
        output_lines = result.stdout.strip().split("\n")
        result_json = json.loads(output_lines[-1])

        assert result_json["statusCode"] == 200
        assert result_json["body"]["processed"] == 0

    def test_lambda_with_mock_event_in_docker(self, lambda_zip_path: Path) -> None:
        """Test Lambda handler with a mock DynamoDB stream event."""
        test_script = """
import sys
import zipfile
import json

# Extract the zip
with zipfile.ZipFile('/var/task/function.zip', 'r') as zf:
    zf.extractall('/tmp/lambda')

sys.path.insert(0, '/tmp/lambda')

from zae_limiter_aggregator.handler import handler

# Mock DynamoDB stream event (MODIFY on BUCKET)
event = {
    "Records": [
        {
            "eventName": "MODIFY",
            "dynamodb": {
                "NewImage": {
                    "PK": {"S": "default/ENTITY#test-entity"},
                    "SK": {"S": "#BUCKET#test-resource#rpm"},
                    "entity_id": {"S": "test-entity"},
                    "data": {
                        "M": {
                            "tokens_milli": {"N": "5000"},
                            "last_refill_ms": {"N": "1704067200000"}
                        }
                    }
                },
                "OldImage": {
                    "PK": {"S": "default/ENTITY#test-entity"},
                    "SK": {"S": "#BUCKET#test-resource#rpm"},
                    "entity_id": {"S": "test-entity"},
                    "data": {
                        "M": {
                            "tokens_milli": {"N": "10000"},
                            "last_refill_ms": {"N": "1704067200000"}
                        }
                    }
                }
            }
        }
    ]
}

# Handler should process this without DynamoDB access
# (it will fail when trying to write, but should parse the event)
try:
    result = handler(event, None)
    print("Handler returned:", json.dumps(result))
except Exception as e:
    # Expected to fail on DynamoDB write, but should get past parsing
    print(f"Expected failure on DynamoDB write: {type(e).__name__}")
    error_msg = str(e).lower()
    if "endpoint" in error_msg or "credentials" in error_msg or "region" in error_msg:
        print("SUCCESS: Event parsing worked, failed on AWS access as expected")
    else:
        raise
"""

        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "python",
                "-v",
                f"{lambda_zip_path}:/var/task/function.zip:ro",
                "public.ecr.aws/lambda/python:3.12",
                "-c",
                test_script,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        # Should either succeed or fail on AWS access
        assert result.returncode == 0 or "SUCCESS:" in result.stdout, (
            f"Unexpected failure:\n{result.stderr}\n{result.stdout}"
        )
