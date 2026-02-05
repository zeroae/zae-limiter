"""Run Python code examples in documentation against moto."""

import pytest
from pytest_examples import CodeExample, EvalExample, find_examples

from tests.doctest.conftest import (
    DOCS_EXAMPLES_CONFIG,
    SKIP_TAGS,
    has_bare_async,
    should_skip,
    wrap_async_source,
)


@pytest.mark.parametrize(
    "example",
    [e for e in find_examples("docs/") if not str(e).startswith("docs/plans/")],
    ids=str,
)
def test_docs_run(
    example: CodeExample,
    eval_example: EvalExample,
    doctest_globals: dict,
):
    """Run doc examples that don't require special infrastructure."""
    reason = should_skip(example, SKIP_TAGS)
    if reason:
        pytest.skip(reason)

    eval_example.config = DOCS_EXAMPLES_CONFIG
    eval_example.lint_ruff(example)

    # Wrap bare async code (await, async with, async for) in async def
    original_source = example.source
    if has_bare_async(example.source):
        example.source = wrap_async_source(original_source)

    try:
        eval_example.run(example, module_globals=doctest_globals)
    except Exception as e:
        example.source = original_source
        pytest.fail(
            f"Example failed at {example.path}:{example.start_line}\n"
            f"Source:\n{original_source}\n"
            f"Error: {e}"
        )
    finally:
        example.source = original_source
