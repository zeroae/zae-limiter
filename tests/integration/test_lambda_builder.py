"""Integration tests for Lambda package builder (require Docker)."""

import json
import subprocess
from pathlib import Path

import pytest


@pytest.mark.integration
class TestLambdaInDocker:
    """
    Integration tests running Lambda in Docker container.

    Tests the actual Lambda package in the Python 3.12 Lambda runtime to ensure
    the aggregator can import and execute without aioboto3 (which is not available
    in Lambda - only boto3 is provided).

    This validates that the lazy import mechanism in __init__.py works correctly.
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

# Import handler - note aggregator.__init__ re-exports handler function
from zae_limiter.aggregator.handler import handler
from zae_limiter.aggregator.processor import process_stream_records

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

from zae_limiter.aggregator.handler import handler

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

from zae_limiter.aggregator.handler import handler

# Mock DynamoDB stream event (MODIFY on BUCKET)
event = {
    "Records": [
        {
            "eventName": "MODIFY",
            "dynamodb": {
                "NewImage": {
                    "PK": {"S": "ENTITY#test-entity"},
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
                    "PK": {"S": "ENTITY#test-entity"},
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
