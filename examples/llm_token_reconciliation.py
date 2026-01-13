#!/usr/bin/env python3
"""
LLM Token Reconciliation Example

Demonstrates rate limiting for LLM APIs where token counts are unknown
until after the call completes. This is the primary use case for zae-limiter.

Key patterns:
- Multiple limits per call (requests + tokens per minute)
- Token estimation before the call
- Post-hoc reconciliation with lease.adjust()
- Checking available capacity before expensive operations
- Handling negative bucket states (actual > estimate)

Setup:
    # Start LocalStack (from project root)
    docker compose up -d
    uv run python examples/llm_token_reconciliation.py
"""

import asyncio
import random
from dataclasses import dataclass

from zae_limiter import (
    Limit,
    RateLimiter,
    RateLimitExceeded,
    StackOptions,
)

# Configuration
ENDPOINT_URL = "http://localhost:4566"
NAME = "llm"  # Creates ZAEL-llm resources


@dataclass
class LLMUsage:
    """Simulates LLM API response usage."""

    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class LLMResponse:
    """Simulates an LLM API response."""

    content: str
    usage: LLMUsage


async def mock_llm_call(prompt: str) -> LLMResponse:
    """Simulate an LLM API call with realistic token usage."""
    # Simulate API latency
    await asyncio.sleep(0.1)

    # Simulate varying token usage (actual often differs from estimate)
    prompt_tokens = len(prompt.split()) * 2  # rough approximation
    completion_tokens = random.randint(50, 200)

    return LLMResponse(
        content=f"Response to: {prompt[:50]}...",
        usage=LLMUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        ),
    )


def estimate_tokens(prompt: str) -> int:
    """Estimate tokens for a prompt (before API call)."""
    # Simple heuristic: ~1.3 tokens per word + buffer for completion
    return int(len(prompt.split()) * 1.3) + 100


async def main() -> None:
    """Demonstrate LLM rate limiting with token reconciliation."""
    print("=== LLM Token Reconciliation Example ===\n")

    limiter = RateLimiter(
        name=NAME,  # Creates ZAEL-llm resources
        endpoint_url=ENDPOINT_URL,
        stack_options=StackOptions(),
        skip_version_check=True,
    )

    # Define limits for a GPT-4 style API
    limits = [
        Limit.per_minute("rpm", 10),  # 10 requests per minute
        Limit.per_minute("tpm", 1000),  # 1000 tokens per minute
    ]

    prompts = [
        "Explain quantum computing in simple terms.",
        "Write a haiku about Python programming.",
        "What are the benefits of using rate limiting?",
        "Describe the token bucket algorithm.",
        "How does DynamoDB handle distributed locking?",
    ]

    print("Making LLM calls with token reconciliation...\n")

    for i, prompt in enumerate(prompts):
        # Estimate tokens before the call
        estimated_tokens = estimate_tokens(prompt)

        try:
            async with limiter.acquire(
                entity_id="api-key-abc",
                resource="gpt-4",
                limits=limits,
                consume={"rpm": 1, "tpm": estimated_tokens},  # consume estimate upfront
            ) as lease:
                # Make the actual API call
                response = await mock_llm_call(prompt)
                actual_tokens = response.usage.total_tokens

                # Reconcile: adjust by the difference (actual - estimate)
                # This NEVER throws, even if it pushes the bucket negative
                token_delta = actual_tokens - estimated_tokens
                await lease.adjust(tpm=token_delta)

                print(f"Call {i + 1}:")
                print(f"  Prompt: {prompt[:40]}...")
                print(f"  Estimated: {estimated_tokens} tokens")
                print(f"  Actual: {actual_tokens} tokens (delta: {token_delta:+d})")
                print(f"  Total consumed: {lease.consumed}")
                print()

        except RateLimitExceeded as e:
            print(f"Call {i + 1}: Rate limited!")
            print(f"  Exceeded: {[v.limit_name for v in e.violations]}")
            print(f"  Retry after: {e.retry_after_seconds:.1f}s")
            print()

    # Demonstrate capacity checking before expensive operations
    print("=== Capacity Checking ===\n")

    # Check current availability
    available = await limiter.available(
        entity_id="api-key-abc",
        resource="gpt-4",
        limits=limits,
    )
    print(f"Currently available: {available}")

    # Calculate wait time for a large request
    needed_tokens = 500
    wait_time = await limiter.time_until_available(
        entity_id="api-key-abc",
        resource="gpt-4",
        limits=limits,
        needed={"rpm": 1, "tpm": needed_tokens},
    )

    if wait_time > 0:
        print(f"Need {needed_tokens} tokens, must wait {wait_time:.1f}s")
    else:
        print(f"Have enough capacity for {needed_tokens} tokens right now!")

    await limiter.close()
    print("\nDone!")


if __name__ == "__main__":
    asyncio.run(main())
