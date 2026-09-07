"""Per-config bandwidth throttling (token bucket). Re-exported for the relay
and XHTTP transports; a bucket is created lazily and removed when the config
is deleted or its speed limit changes.
"""
from __future__ import annotations

import asyncio
import time

from .. import registry, config

# Use the canonical config value (0 = unlimited).
MIN_RATE = 1024          # 1 KB/s floor to avoid div-by-zero / silly speeds
MIN_BURST = 16 * 1024    # 16 KB minimum burst so small chunks don't queue


class _Bucket:
    __slots__ = ("rate", "capacity", "tokens", "last")

    def __init__(self, rate_bytes_per_sec: float):
        self.rate = max(rate_bytes_per_sec, MIN_RATE)
        # burst capacity = 1 second worth of tokens (min 16 KB)
        self.capacity = max(self.rate, MIN_BURST)
        self.tokens = self.capacity
        self.last = time.monotonic()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self.last
        if elapsed > 0:
            self.last = now
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)

    async def consume(self, n: int):
        while True:
            self._refill()
            if self.tokens >= n:
                self.tokens -= n
                return
            deficit = n - self.tokens
            wait = deficit / self.rate
            # cap each sleep so live speed-limit edits are picked up quickly
            await asyncio.sleep(min(max(wait, 0.004), 0.5))


_buckets: dict[str, _Bucket] = {}


def _get_bucket(uuid: str, rate: int) -> _Bucket:
    b = _buckets.get(uuid)
    target = max(rate, MIN_RATE)
    if b is None or b.rate != target:
        b = _Bucket(target)
        _buckets[uuid] = b
    return b


async def throttle(uuid: str, nbytes: int):
    """Wait for permission to move `nbytes`. No-op when the config is unlimited."""
    if nbytes <= 0:
        return
    link = registry.LINKS.get(uuid)
    rate = int((link or {}).get("speed_limit_bytes", 0) or 0)
    if rate <= 0:
        return
    await _get_bucket(uuid, rate).consume(nbytes)


def reset_bucket(uuid: str):
    _buckets.pop(uuid, None)