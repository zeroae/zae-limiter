"""Run Python code examples that require LocalStack."""

import os

import pytest
from pytest_examples import CodeExample, EvalExample, find_examples

from tests.fixtures.doctest_helpers import (
    DOCS_EXAMPLES_CONFIG,
    has_bare_async,
    wrap_async_source,
)


@pytest.mark.integration
@pytest.mark.parametrize(
    "example",
    [e for e in find_examples("docs/") if not str(e).startswith("docs/plans/")],
    ids=str,
)
def test_docs_integration(
    example: CodeExample,
    eval_example: EvalExample,
    localstack_limiter,  # noqa: ARG001 - ensures stack is deployed
):
    """Run doc examples tagged with requires-localstack.

    The localstack_limiter fixture deploys a stack named 'limiter' before
    any tests run, matching the CLI deploy example in the documentation.
    """
    tags = example.prefix_tags()
    if "requires-localstack" not in tags:
        pytest.skip("not tagged requires-localstack")

    endpoint = os.environ.get("AWS_ENDPOINT_URL")
    if not endpoint:
        pytest.skip("AWS_ENDPOINT_URL not set (LocalStack not running)")

    eval_example.config = DOCS_EXAMPLES_CONFIG
    eval_example.lint_ruff(example)

    # Wrap bare async code (await, async with, async for) in async def
    original_source = example.source
    if has_bare_async(example.source):
        example.source = wrap_async_source(original_source)

    try:
        eval_example.run(example)
    finally:
        example.source = original_source
