"""Stars and download counts for YidStore items.

The counts are built every few hours by a GitHub Action in the YidStore
repository (.github/workflows/stats.yml) from public data on the store
server and GitHub, and published as one file on the repository's "stats"
branch. YidStore reads that file over plain HTTPS: no token, no account, and
no per-item API calls.

Items are keyed by a short hash of "<source>:<owner>/<repo>" (lower-case), the
same hash .github/scripts/build_stats.py writes, so the public file never
names private repositories.
"""
from __future__ import annotations

import hashlib
import logging
import time

from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

STATS_URL = "https://raw.githubusercontent.com/onoffautomations/yidstore/stats/stats.json"
_STATS_TTL = 3600.0       # re-read at most once an hour
_STATS_RETRY = 300.0      # after a failed read, try again in 5 minutes

_STATE: dict = {"fetched": 0.0, "failed": 0.0, "items": {}}


def item_key(source: str | None, owner: str, repo: str) -> str:
    src = "github" if (source or "").lower() == "github" else "gitea"
    return hashlib.sha256(f"{src}:{owner}/{repo}".lower().encode()).hexdigest()[:16]


async def async_get_stats(hass) -> dict:
    """The hash -> {"stars", "downloads"} map; cached, never raises."""
    now = time.time()
    if now - _STATE["fetched"] < _STATS_TTL or now - _STATE["failed"] < _STATS_RETRY:
        return _STATE["items"]
    try:
        sess = async_get_clientsession(hass)
        async with sess.get(STATS_URL, timeout=15, headers={"User-Agent": "YidStore"}) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            data = await resp.json(content_type=None)
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, dict):
            raise RuntimeError("unexpected format")
        _STATE["items"] = items
        _STATE["fetched"] = now
    except Exception as exc:
        # Keep the last good counts; the store works the same without them.
        _STATE["failed"] = now
        _LOGGER.debug("Could not read YidStore stats: %s", exc)
    return _STATE["items"]


def stats_for(items: dict, source: str | None, owner: str, repo: str) -> dict | None:
    entry = items.get(item_key(source, owner, repo)) if owner and repo else None
    if not isinstance(entry, dict):
        return None
    return {
        "stars": _count(entry.get("stars")),
        "downloads": _count(entry.get("downloads")),
    }


def _count(value) -> int:
    """A non-negative whole number from the stats file, 0 if it is not one."""
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0
