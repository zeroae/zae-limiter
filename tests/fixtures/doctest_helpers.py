"""Helper classes and functions for doctest execution."""

import re
import textwrap

from pytest_examples.config import ExamplesConfig

# Ruff config for doc examples: ignore rules that are inherent to documentation style.
DOCS_EXAMPLES_CONFIG = ExamplesConfig(
    line_length=100,
    target_version="py311",
    ruff_select=["E", "F", "I", "N", "W", "UP"],
    ruff_ignore=["F401", "F704", "F706", "F811", "F821", "F841", "E741", "I001", "N807"],
)

# Tags that cause an example to be skipped during execution tests.
SKIP_TAGS = {"requires-external", "lint-only", "requires-localstack"}


def should_skip(example, tags_to_skip: set[str]) -> str | None:
    """Return a skip reason if the example has any skip tags, else None."""
    tags = example.prefix_tags()
    matched = tags & tags_to_skip
    if matched:
        return f"tagged with {', '.join(sorted(matched))}"
    return None


# ---------------------------------------------------------------------------
# Async detection and wrapping helpers
# ---------------------------------------------------------------------------

_ASYNC_USAGE_RE = re.compile(
    r"(?:^|\s)(?:await |async with |async for )",
    re.MULTILINE,
)
_ASYNC_DEF_RE = re.compile(r"^\s*async\s+def\s+", re.MULTILINE)


def has_bare_async(source: str) -> bool:
    """Check if source contains async statements without an async def wrapper."""
    if not _ASYNC_USAGE_RE.search(source):
        return False
    if _ASYNC_DEF_RE.search(source):
        return False
    return True


def wrap_async_source(source: str) -> str:
    """Wrap bare-async source in async def _run() + asyncio.run(_run())."""
    lines = source.splitlines(keepends=True)
    import_lines: list[str] = []
    body_lines: list[str] = []
    in_imports = True
    for line in lines:
        stripped = line.strip()
        if in_imports and (
            stripped.startswith(("import ", "from ")) or stripped == "" or stripped.startswith("#")
        ):
            import_lines.append(line)
        else:
            in_imports = False
            body_lines.append(line)
    import_block = "".join(import_lines)
    indented_body = textwrap.indent("".join(body_lines), "    ")
    return (
        f"{import_block}import asyncio\n\nasync def _run():\n{indented_body}\nasyncio.run(_run())\n"
    )


# ---------------------------------------------------------------------------
# Mock classes for doc examples
# ---------------------------------------------------------------------------


class MockUsage:
    """Mock OpenAI-style usage object for doc examples."""

    prompt_tokens = 100
    completion_tokens = 50
    total_tokens = 150


class MockChoice:
    """Mock OpenAI-style choice object."""

    class Message:
        content = "Hello"

    message = Message()


class MockResponse:
    """Mock OpenAI-style response with .usage attribute access."""

    usage = MockUsage()
    choices = [MockChoice()]


class MockOpenAICompletions:
    """Mock openai.chat.completions for doc examples."""

    async def create(self, **kwargs):
        return MockResponse()


class MockOpenAIChat:
    completions = MockOpenAICompletions()


class MockOpenAI:
    chat = MockOpenAIChat()


class MockResult:
    """Mock result for execute_operation() stubs."""

    units_consumed = 12


# ---------------------------------------------------------------------------
# Stub functions for doc examples
# ---------------------------------------------------------------------------


async def stub_call_llm(*args, **kwargs):
    """Stub for call_llm() used in doc examples."""
    return MockResponse()


async def stub_call_api(*args, **kwargs):
    """Stub for call_api() used in doc examples."""
    return {"status": "ok"}


async def stub_do_work(*args, **kwargs):
    """Stub for do_work() used in doc examples."""
    pass


async def stub_premium_operation(*args, **kwargs):
    """Stub for premium_operation() used in doc examples."""
    return "premium result"


async def stub_basic_operation(*args, **kwargs):
    """Stub for basic_operation() used in doc examples."""
    return "basic result"


async def stub_execute_operation(*args, **kwargs):
    """Stub for execute_operation() used in doc examples."""
    return MockResult()


class JSONResponse:
    """Stub for starlette.responses.JSONResponse used in doc examples."""

    def __init__(self, status_code=200, content=None, headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


class HTTPError(Exception):
    """Stub for fastapi.HTTPException used in doc examples."""

    def __init__(self, status_code=500, detail=None):
        self.status_code = status_code
        self.detail = detail


# Entity IDs commonly referenced in documentation examples
COMMON_ENTITIES = [
    "user-123",
    "user-free",
    "user-premium",
    "api-key-123",
    "api-key-456",
    "key-123",
    "proj-1",
    "org-456",
    "tenant-acme",
    "test-entity",
    "entity",
    "entity-1",
    "entity-2",
    "project-prod",
    "project-1",
    "api-key",
]
