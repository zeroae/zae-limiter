"""Tests for Lambda package builder."""

import io
import tempfile
import zipfile
from pathlib import Path


class TestLambdaBuilder:
    """Tests for building Lambda deployment packages."""

    def test_build_lambda_package(self) -> None:
        """Test that Lambda package builds successfully."""
        from zae_limiter.infra.lambda_builder import build_lambda_package

        zip_bytes = build_lambda_package()

        # Should return bytes
        assert isinstance(zip_bytes, bytes)
        assert len(zip_bytes) > 0

        # Should be a valid zip file
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            # Should not have errors
            assert zf.testzip() is None

    def test_package_contains_required_files(self) -> None:
        """Test that package contains all required files."""
        from zae_limiter.infra.lambda_builder import build_lambda_package

        zip_bytes = build_lambda_package()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            files = set(zf.namelist())

            # Must contain aggregator handler
            assert "zae_limiter/aggregator/handler.py" in files
            assert "zae_limiter/aggregator/processor.py" in files

            # Must contain schema (dependency of processor)
            assert "zae_limiter/schema.py" in files

            # Should contain package init
            assert "zae_limiter/__init__.py" in files

    def test_package_size_reasonable(self) -> None:
        """Test that package size is reasonable."""
        from zae_limiter.infra.lambda_builder import build_lambda_package

        zip_bytes = build_lambda_package()

        # Should be less than 100KB (currently ~30KB)
        assert len(zip_bytes) < 100 * 1024, f"Package too large: {len(zip_bytes)} bytes"

        # Should be more than 1KB (sanity check)
        assert len(zip_bytes) > 1024, f"Package suspiciously small: {len(zip_bytes)} bytes"

    def test_write_lambda_package(self) -> None:
        """Test writing package to file."""
        from zae_limiter.infra.lambda_builder import write_lambda_package

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "lambda.zip"

            size = write_lambda_package(output_path)

            # File should exist
            assert output_path.exists()

            # Size should match
            assert output_path.stat().st_size == size

            # Should be valid zip
            with zipfile.ZipFile(output_path) as zf:
                assert zf.testzip() is None

    def test_get_package_info(self) -> None:
        """Test getting package metadata."""
        from zae_limiter.infra.lambda_builder import get_package_info

        info = get_package_info()

        # Should contain expected keys
        assert "package_path" in info
        assert "python_files" in info
        assert "uncompressed_size" in info
        assert "handler" in info

        # Handler should be correct
        assert info["handler"] == "zae_limiter.aggregator.handler.handler"

        # Should have reasonable number of files
        assert isinstance(info["python_files"], int)
        assert info["python_files"] > 5  # At least a few modules


class TestLambdaHandlerUnit:
    """Unit tests for Lambda handler (no Docker)."""

    def test_handler_with_empty_records(self) -> None:
        """Test handler with empty Records list."""
        from zae_limiter.aggregator.handler import handler

        event = {"Records": []}
        result = handler(event, None)

        assert result["statusCode"] == 200
        assert result["body"]["processed"] == 0
        assert result["body"]["snapshots_updated"] == 0

    def test_handler_with_no_records_key(self) -> None:
        """Test handler with missing Records key."""
        from zae_limiter.aggregator.handler import handler

        event = {}
        result = handler(event, None)

        assert result["statusCode"] == 200
        assert result["body"]["processed"] == 0
