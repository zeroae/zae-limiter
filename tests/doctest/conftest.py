"""Configuration for documentation example tests."""

import asyncio
from collections.abc import Awaitable
from unittest.mock import patch

import pytest
from moto import mock_aws
from pytest_examples import CodeExample
from pytest_examples.config import ExamplesConfig

# Ruff config for doc examples: ignore rules that are inherent to documentation style.
# - F704: yield/await outside function (async examples use bare await)
# - F706: return outside function (snippets showing return values)
# - F811: redefinition of unused name (multiple examples redefine same var)
# - F821: undefined name (snippets reference variables from prose context)
# - F841: local variable assigned but never used (illustrative assignments)
# - E741: ambiguous variable name (e.g., single-letter vars in examples)
# - I001: import not sorted (cosmetic, doc examples prioritize readability)
# - F401: imported but unused (doc examples import for illustration)
# - N807: function name should not start/end with __ (e.g., __version__)
DOCS_EXAMPLES_CONFIG = ExamplesConfig(
    line_length=100,
    target_version="py311",
    ruff_select=["E", "F", "I", "N", "W", "UP"],
    ruff_ignore=["F401", "F704", "F706", "F811", "F821", "F841", "E741", "I001", "N807"],
)

# Tags that cause an example to be skipped during execution tests.
# - lint-only: fragments, pseudo-code, or blocks that reference undefined helpers
# - requires-external: needs packages not in project dependencies
# - requires-localstack: needs real CloudFormation/Lambda (LocalStack or AWS)
SKIP_TAGS = {"requires-external", "lint-only", "requires-localstack"}


def should_skip(example: CodeExample, tags_to_skip: set[str]) -> str | None:
    """Return a skip reason if the example has any skip tags, else None."""
    tags = example.prefix_tags()
    matched = tags & tags_to_skip
    if matched:
        return f"tagged with {', '.join(sorted(matched))}"
    return None


def _patch_aiobotocore_response():
    """
    Patch aiobotocore to work with moto's sync responses.

    Moto returns botocore.awsrequest.AWSResponse which has sync content,
    but aiobotocore expects async content. This patch wraps the response
    handling to convert sync content to async.

    See: https://github.com/aio-libs/aiobotocore/discussions/1300
    """
    from aiobotocore import endpoint

    original_convert = endpoint.convert_to_response_dict

    async def patched_convert(http_response, operation_model):
        # If content is not awaitable (moto's sync response), wrap it
        if hasattr(http_response, "_content") and not isinstance(http_response._content, Awaitable):
            fut: asyncio.Future[bytes] = asyncio.Future()
            fut.set_result(http_response.content)
            http_response._content = fut
        return await original_convert(http_response, operation_model)

    return patch.object(endpoint, "convert_to_response_dict", patched_convert)


@pytest.fixture
def moto_env(monkeypatch):
    """Provide a moto-backed AWS mock environment for doc example execution."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)

    with mock_aws(), _patch_aiobotocore_response():
        yield


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    """Add doctest marker to all items in this directory."""
    for item in items:
        if "doctest" in str(item.fspath):
            item.add_marker(pytest.mark.doctest)
