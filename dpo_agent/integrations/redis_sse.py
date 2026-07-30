"""Redis pub/sub for SSE event fan-out across multiple
web workers.

PROBLEM: the FastAPI SSE endpoint runs the pipeline in a
thread + queue (see dpo_agent.examples.fastapi_server). When
uvicorn runs N workers, each worker has its own process and
its own thread-local queue. A request to worker A can't read
events from a pipeline running in worker B.

SOLUTION: when REDIS_URL is set, the pipeline thread publishes
SSE events to a Redis channel; each FastAPI worker's SSE
endpoint subscribes to that channel for the duration of a
streaming response. This is the standard pattern for
horizontal scaling of SSE.

This module is OPTIONAL. The existing per-request queue
pattern still works for single-worker deployments (the
default in docker-compose.yml). The Redis layer activates
only when REDIS_URL is set.

USAGE (in dpo_agent.examples.fastapi_server):

    from dpo_agent.integrations.redis_sse import (
        publish_event, subscribe_to_events, is_redis_enabled
    )

    if is_redis_enabled():
        # In the pipeline thread:
        publish_event(run_id, event_dict)

        # In the SSE endpoint:
        async for event in subscribe_to_events(run_id):
            yield f"data: {json.dumps(event)}\\n\\n"
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Optional

# Lazy import of redis — it's an optional dependency.
try:
    import redis.asyncio as aioredis
    _HAVE_REDIS = True
except ImportError:
    _HAVE_REDIS = False


# Channel prefix for SSE events. The full channel name is
# "dpo_agent:sse:<run_id>". Run IDs are unique per pipeline
# invocation.
_CHANNEL_PREFIX = "dpo_agent:sse:"


def is_redis_enabled() -> bool:
    """Whether the Redis SSE layer is enabled.

    Returns True only if BOTH the REDIS_URL env var is set
    AND the redis package is installed.
    """
    return bool(os.environ.get("REDIS_URL")) and _HAVE_REDIS


def _get_redis() -> "aioredis.Redis":
    """Get a Redis client from the REDIS_URL env var."""
    if not _HAVE_REDIS:
        raise ImportError(
            "redis is required for the SSE pub/sub layer. "
            "Install with: pip install redis"
        )
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    return aioredis.from_url(url, decode_responses=True)


async def publish_event(run_id: str, event: dict) -> None:
    """Publish an SSE event to Redis for the given run_id.

    Called by the pipeline thread after each stage. The event
    is JSON-serialized and published to a per-run channel.
    The channel is auto-cleaned when all subscribers disconnect.
    """
    if not is_redis_enabled():
        return
    client = _get_redis()
    channel = f"{_CHANNEL_PREFIX}{run_id}"
    try:
        await client.publish(channel, json.dumps(event))
    finally:
        await client.aclose()


async def subscribe_to_events(
    run_id: str,
    timeout: float = 600.0,
) -> AsyncIterator[dict]:
    """Subscribe to SSE events for the given run_id.

    Yields events as dicts. Auto-cancels after `timeout`
    seconds (default 10 minutes — generous for a 5-stage
    pipeline). The caller (the FastAPI SSE endpoint) iterates
    this and yields each event as an SSE message.
    """
    if not is_redis_enabled():
        return
    client = _get_redis()
    channel = f"{_CHANNEL_PREFIX}{run_id}"
    pubsub = client.pubsub()
    try:
        await pubsub.subscribe(channel)
        # Wait for the first event (the start signal) or timeout.
        # The first event is always a "stage_start" for stage 0;
        # this gives us a heartbeat to know the pipeline has
        # started.
        import asyncio
        start_time = asyncio.get_event_loop().time()
        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                break
            try:
                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
            except Exception:
                break
            if msg is None:
                continue
            if msg["type"] != "message":
                continue
            try:
                yield json.loads(msg["data"])
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()
        await client.aclose()


# ─── Synchronous wrapper for use in threads ────────────────────────
# The pipeline thread is sync (it runs the dpo-agent agents,
# which are sync). publish_event is async, which doesn't work
# in a sync context. We use asyncio.run() in a thread-safe way.

def publish_event_sync(run_id: str, event: dict) -> None:
    """Synchronous wrapper around publish_event for use in
    threads (the pipeline thread).
    """
    if not is_redis_enabled():
        return
    import asyncio
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(publish_event(run_id, event))
        finally:
            loop.close()
    except Exception:
        # Don't fail the pipeline just because Redis pub/sub failed.
        # The per-request queue is the fallback.
        pass
