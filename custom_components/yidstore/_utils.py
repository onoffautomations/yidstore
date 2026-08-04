"""Utility functions for internal use."""
from __future__ import annotations

import base64
import json
from urllib.parse import quote


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


async def async_github_latest_release(hass, owner: str, repo: str) -> dict | None:
    """Fetch the FULL latest GitHub release via the REST API.

    Returns a dict with ``tag_name``, ``name``, ``body``, ``html_url`` and
    ``published_at`` — everything needed to show release notes the way HACS
    does — or ``None`` when the repo has no release or the API is
    unavailable (e.g. the unauthenticated 60 req/hour rate limit).

    This is the ONLY place YidStore uses api.github.com, and only for
    GitHub-sourced packages: release notes require the release *body*, which
    the /releases/latest redirect (async_github_latest_tag) cannot provide.
    Callers should fall back to async_github_latest_tag() for the version
    when this returns None, so a rate-limited API only costs the notes, not
    update detection. The Gitea client is never used for GitHub repos.
    """
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    sess = async_get_clientsession(hass)
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "YidStore",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with sess.get(url, headers=headers, timeout=20) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception:
        return None

    if not isinstance(data, dict):
        return None
    tag = data.get("tag_name") or data.get("name")
    if not tag:
        return None
    return {
        "tag_name": data.get("tag_name") or data.get("name"),
        "name": data.get("name"),
        "body": data.get("body"),
        "html_url": data.get("html_url"),
        "published_at": data.get("published_at"),
    }


def github_archive_url(owner: str, repo: str, ref: str) -> str:
    """Zip download URL for a GitHub ref (tag or branch) WITHOUT the REST API.

    github.com/<o>/<r>/archive/<ref>.zip redirects to codeload.github.com,
    which is not API rate-limited (api.github.com/.../zipball is).
    """
    return f"https://github.com/{owner}/{repo}/archive/{ref}.zip"


async def async_github_hacs_manifest(hass, owner: str, repo: str, ref: str | None = None) -> dict | None:
    """Fetch hacs.json from GitHub without using the REST API.

    HACS repository metadata is read from main/master first, with the selected
    release ref as a fallback. This lets GitHub integrations honor HACS
    ``zip_release`` + ``filename`` while avoiding api.github.com rate limits.
    """
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    # HACS treats hacs.json as repository metadata, not as release-asset
    # contents. Read the normal repository branches first (main/master), then
    # fall back to the selected ref. This matters for repositories that added
    # zip_release metadata after an older tag was cut: HACS will still use the
    # configured release asset, and YidStore should do the same.
    refs: list[str] = ["main", "master"]
    if ref and ref not in refs:
        refs.append(ref)

    sess = async_get_clientsession(hass)
    headers = {"User-Agent": "YidStore"}

    for candidate in refs:
        encoded_ref = quote(candidate, safe="")
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{encoded_ref}/hacs.json"
        try:
            async with sess.get(url, headers=headers, timeout=20) as resp:
                if resp.status != 200:
                    continue
                text = await resp.text()
            data = json.loads(text)
        except Exception:
            continue

        if isinstance(data, dict):
            return data

    return None


def github_release_asset_url(owner: str, repo: str, tag: str, filename: str) -> str:
    """Direct download URL for a GitHub Release asset.

    Downloads through this URL are GitHub release-asset downloads, so GitHub's
    asset ``download_count`` (and Shields github/downloads badges) increments.
    """
    return (
        f"https://github.com/{owner}/{repo}/releases/download/"
        f"{quote(tag, safe='')}/{quote(filename, safe='')}"
    )
