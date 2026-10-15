"""Shared pacing and metadata filters for Mini App packet catalogues."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass


@dataclass
class _BatchLimiter:
    lock: asyncio.Lock
    finished_at: float = -1


@asynccontextmanager
async def background_packet_batch():
    loop = asyncio.get_running_loop()
    # A contended lock retains its loop; keep that cycle local to the loop.
    limiter = getattr(loop, "_sitg_packet_batch_limiter", None)
    if limiter is None:
        limiter = _BatchLimiter(asyncio.Lock())
        loop._sitg_packet_batch_limiter = limiter
    async with limiter.lock:
        await asyncio.sleep(max(0, limiter.finished_at + 0.2 - loop.time()))
        try:
            yield
        finally:
            limiter.finished_at = loop.time()


def matches_packet(version, authors, filters: dict[str, object]) -> bool:
    def normalize(value):
        return str(value or "").strip().lower()
    if normalize(filters.get("name")) not in normalize(version.name):
        return False
    if normalize(filters.get("author")) not in normalize(" ".join(authors)):
        return False
    for value, prefix in (
        (version.year, "year"),
        (version.published_at.year if version.published_at else None, "publication"),
    ):
        start, end = filters.get(f"{prefix}_from"), filters.get(f"{prefix}_to")
        if (start is not None or end is not None) and (
            value is None or (start is not None and value < int(start))
            or (end is not None and value > int(end))
        ):
            return False
    return True
