"""LLM completion simulator."""

import asyncio
import random

RESPONSES = [
    "I'd be happy to help you with that question. Let me provide a detailed explanation.",
    "That's an interesting topic. Here's what I know about it.",
    "Based on my knowledge, I can offer the following insights.",
    "Let me break this down for you step by step.",
    "Great question! Here's a comprehensive answer.",
    "I understand your query. Here's my response based on the context provided.",
    "Thank you for asking. Let me address your question thoroughly.",
    "This is a nuanced topic that deserves careful consideration.",
]


async def simulate_completion(
    messages: list[dict],
    model: str,
    max_tokens: int | None = None,
) -> tuple[str, int, int]:
    """
    Simulate an LLM completion with realistic token usage.

    Args:
        messages: List of chat messages
        model: Model name (affects simulated latency)
        max_tokens: Maximum tokens to generate

    Returns:
        Tuple of (response_content, prompt_tokens, completion_tokens)
    """
    # Simulate API latency (50-300ms depending on model)
    if "gpt-4" in model:
        latency = random.uniform(0.1, 0.3)
    else:
        latency = random.uniform(0.05, 0.15)
    await asyncio.sleep(latency)

    # Calculate input tokens (rough estimate: ~4 chars per token)
    input_chars = sum(len(m.get("content", "")) for m in messages)
    prompt_tokens = max(10, input_chars // 4)

    # Generate response
    base_response = random.choice(RESPONSES)

    # Add some random padding to vary token count
    padding_sentences = random.randint(1, 5)
    padding = " ".join(
        [
            "This additional context helps provide a more complete answer."
            for _ in range(padding_sentences)
        ]
    )
    response = f"{base_response}\n\n{padding}"

    # Calculate completion tokens
    if max_tokens:
        completion_tokens = min(max_tokens, len(response.split()) * 2)
        # Truncate response if needed
        words = response.split()
        max_words = max_tokens // 2
        if len(words) > max_words:
            response = " ".join(words[:max_words]) + "..."
    else:
        completion_tokens = len(response.split()) * 2  # ~2 tokens per word

    # Add some randomness to completion tokens
    completion_tokens = int(completion_tokens * random.uniform(0.8, 1.2))
    completion_tokens = max(10, completion_tokens)

    return response, prompt_tokens, completion_tokens
