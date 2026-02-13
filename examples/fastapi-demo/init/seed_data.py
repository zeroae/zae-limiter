#!/usr/bin/env python3
"""Seed demo data for the FastAPI example."""

import asyncio
import os

from zae_limiter import EntityExistsError, Limit, RateLimiter


async def seed_data() -> None:
    """Create demo entities and limits."""
    endpoint_url = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")
    name = os.environ.get("NAME", "demo")

    print(f"Connecting to {endpoint_url}, name: {name} (table: ZAEL-{name})")

    limiter = RateLimiter(
        name=name,  # Creates ZAEL-demo resources
        endpoint_url=endpoint_url,
    )

    # Create project (parent entity)
    try:
        await limiter.create_entity(
            entity_id="proj-demo",
            name="Demo Project",
            metadata={"tier": "enterprise"},
        )
        print("  Created project: proj-demo")
    except EntityExistsError:
        print("  Project proj-demo already exists")

    # Create API keys (child entities)
    api_keys = [
        ("key-alice", "Alice's API Key"),
        ("key-bob", "Bob's API Key"),
        ("key-charlie", "Charlie's API Key"),
    ]

    for key_id, name in api_keys:
        try:
            await limiter.create_entity(
                entity_id=key_id,
                name=name,
                parent_id="proj-demo",
                cascade=True,
            )
            print(f"  Created API key: {key_id}")
        except EntityExistsError:
            print(f"  API key {key_id} already exists")

    # Set project-level limits (shared across all keys)
    # Balanced limits: small requests hit RPM, large requests hit TPM
    await limiter.set_limits(
        entity_id="proj-demo",
        resource="gpt-4",
        limits=[
            Limit.per_minute("rpm", 50),  # 50 rpm for entire project
            Limit.per_minute("tpm", 10_000),  # 10k tpm for entire project
        ],
    )
    print("  Set project limits: 50 rpm, 10k tpm")

    # Set individual key limits - Alice (premium)
    await limiter.set_limits(
        entity_id="key-alice",
        resource="gpt-4",
        limits=[
            Limit.per_minute("rpm", 30),  # Alice: 30 rpm
            Limit.per_minute("tpm", 3_000),  # Alice: 3k tpm (~100 tokens/req threshold)
        ],
    )
    print("  Set Alice limits: 30 rpm, 3k tpm")

    # Set individual key limits - Bob (standard)
    # 10 RPM, 1000 TPM = 100 tokens/request threshold
    # < 100 tokens: hit RPM first, > 100 tokens: hit TPM first
    await limiter.set_limits(
        entity_id="key-bob",
        resource="gpt-4",
        limits=[
            Limit.per_minute("rpm", 10),  # Bob: 10 rpm (easy to hit!)
            Limit.per_minute("tpm", 1_000),  # Bob: 1k tpm
        ],
    )
    print("  Set Bob limits: 10 rpm, 1k tpm")

    # Charlie uses default limits (no stored limits)
    print("  Charlie uses default limits (20 rpm, 2k tpm)")

    await limiter.close()
    print("\nDemo data seeded successfully!")


if __name__ == "__main__":
    asyncio.run(seed_data())
