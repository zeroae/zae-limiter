"""Lambda handler for stress test data setup."""

from __future__ import annotations

import os
from typing import Any


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Create entities and configure limits before stress test.

    Args:
        event: Lambda event with setup config
        context: Lambda context

    Returns:
        Setup results
    """
    import asyncio

    from zae_limiter import Limit, RateLimiter

    config = event.get("config", {})

    stack_name = config.get("stack_name") or os.environ["TARGET_STACK_NAME"]
    region = config.get("region") or os.environ.get("TARGET_REGION", "us-east-1")
    num_custom_limits = config.get("num_custom_limit_entities", 300)
    num_apis = config.get("num_apis", 8)

    async def setup() -> dict[str, Any]:
        limiter = RateLimiter(name=stack_name, region=region)

        async with limiter:
            # 1. Set system defaults
            await limiter.set_system_defaults(
                limits=[
                    Limit.per_minute("rpm", 1000),
                    Limit.per_minute("tpm", 100_000),
                ]
            )

            # 2. Set per-API resource defaults
            api_configs = {
                "api-0": {"rpm": 500, "tpm": 50_000},
                "api-1": {"rpm": 2000, "tpm": 200_000},
                "api-2": {"rpm": 1000, "tpm": 100_000},
                "api-3": {"rpm": 1500, "tpm": 150_000},
                "api-4": {"rpm": 800, "tpm": 80_000},
                "api-5": {"rpm": 1200, "tpm": 120_000},
                "api-6": {"rpm": 600, "tpm": 60_000},
                "api-7": {"rpm": 1800, "tpm": 180_000},
            }

            for api_name, limits in list(api_configs.items())[:num_apis]:
                await limiter.set_resource_defaults(
                    resource=api_name,
                    limits=[
                        Limit.per_minute("rpm", limits["rpm"]),
                        Limit.per_minute("tpm", limits["tpm"]),
                    ],
                )

            # 3. Create whale entity with high limits
            await limiter.create_entity("entity-whale", name="Whale Entity")
            await limiter.set_limits(
                entity_id="entity-whale",
                limits=[
                    Limit.per_minute("rpm", 10_000),
                    Limit.per_minute("tpm", 1_000_000),
                ],
            )

            # 4. Create spike entity with high limits
            await limiter.create_entity("entity-spiker", name="Spike Entity")
            await limiter.set_limits(
                entity_id="entity-spiker",
                limits=[
                    Limit.per_minute("rpm", 5_000),
                    Limit.per_minute("tpm", 500_000),
                ],
            )

            # 5. Create entities with custom limits
            for i in range(num_custom_limits):
                entity_id = f"entity-{i:05d}"
                await limiter.create_entity(entity_id, name=f"Custom Entity {i}")

                # Vary limits by entity
                multiplier = 1 + (i % 5) * 0.5  # 1x, 1.5x, 2x, 2.5x, 3x
                await limiter.set_limits(
                    entity_id=entity_id,
                    limits=[
                        Limit.per_minute("rpm", int(1000 * multiplier)),
                        Limit.per_minute("tpm", int(100_000 * multiplier)),
                    ],
                )

        return {
            "status": "ready",
            "system_defaults": True,
            "resource_configs": min(num_apis, 8),
            "whale_entity": "entity-whale",
            "spike_entity": "entity-spiker",
            "custom_limit_entities": num_custom_limits,
        }

    return asyncio.get_event_loop().run_until_complete(setup())
