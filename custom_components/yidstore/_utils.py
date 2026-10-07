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


async def _async_github_newest_release(hass, owner: str, repo: str) -> dict | None:
    """Newest non-draft GitHub release, pre-releases included (REST API)."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    sess = async_get_clientsession(hass)
    try:
        async with sess.get(
            f"https://api.github.com/repos/{owner}/{repo}/releases?per_page=15",
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "YidStore",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=20,
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception:
        return None
    for rel in data if isinstance(data, list) else []:
        if isinstance(rel, dict) and not rel.get("draft") and rel.get("tag_name"):
            return rel
    return None


async def async_github_latest_tag(hass, owner: str, repo: str, include_prereleases: bool = False) -> str | None:
    """Resolve the latest GitHub release tag WITHOUT the REST API.

    https://github.com/<owner>/<repo>/releases/latest redirects to
    /releases/tag/<tag>. Unlike api.github.com, plain github.com is not
    subject to the 60 requests/hour unauthenticated API rate limit, so
    this works reliably (it's the same reason HACS-style downloads keep
    working when the REST API returns 403).
    """
    from urllib.parse import unquote

    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    if include_prereleases:
        # The redirect below never points at a pre-release.
        rel = await _async_github_newest_release(hass, owner, repo)
        if rel:
            return rel["tag_name"]

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


async def async_github_latest_release(hass, owner: str, repo: str, include_prereleases: bool = False) -> dict | None:
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

    if include_prereleases:
        rel = await _async_github_newest_release(hass, owner, repo)
        if rel:
            return {
                "tag_name": rel.get("tag_name"),
                "name": rel.get("name"),
                "body": rel.get("body"),
                "html_url": rel.get("html_url"),
                "published_at": rel.get("published_at"),
            }

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


# ---------------------------------------------------------------------------
# Community store detection (HACS custom integration, and the built-in
# Home Assistant "Marketplace" that replaces it from HA 2026.11).
#
# The Marketplace adopts the HACS data on first start: `.storage/hacs.*`
# becomes `.storage/marketplace.*` and `/hacsfiles/` dashboard resources are
# rewritten to `/local/community/`. YidStore reads whichever is present so
# packages managed by either one are never touched by YidStore.
# ---------------------------------------------------------------------------

# Newest first: once the Marketplace has adopted the data, the legacy
# files are removed, but an un-migrated install may still only have these.
_COMMUNITY_STORE_FILES = (
    "marketplace.repositories",
    "hacs.repositories",
    "hacs.data",
    "hacs",
)


def _community_store_entries(raw) -> list[dict]:
    """Flatten the shapes the HACS / Marketplace storage files have used."""
    data = raw.get("data", raw) if isinstance(raw, dict) else raw
    out: list[dict] = []
    if isinstance(data, dict) and isinstance(data.get("repositories"), (list, dict)):
        data = data["repositories"]
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, dict):
                # {id: repo} or, in hacs.data, {category: [repo, ...]}
                out.append(value)
            elif isinstance(value, list):
                for repo in value:
                    if isinstance(repo, dict):
                        out.append({"category": key, **repo})
    elif isinstance(data, list):
        out.extend(r for r in data if isinstance(r, dict))
    # Very old HACS nested the fields under "data".
    return [
        {**r.get("data", {}), **r} if isinstance(r.get("data"), dict) else r
        for r in out
    ]


def read_community_store_repositories(config_path: str) -> list[dict]:
    """Return the repositories HACS or the Marketplace knows about.

    Blocking (filesystem) — call through async_add_executor_job.
    """
    from pathlib import Path

    storage = Path(config_path) / ".storage"
    for name in _COMMUNITY_STORE_FILES:
        path = storage / name
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        entries = _community_store_entries(raw)
        if entries:
            return entries
    return []


def community_store_installed(config_path: str) -> tuple[set[str], set[str]]:
    """Return (integration domains, lowercase full_names) installed by HACS/Marketplace."""
    domains: set[str] = set()
    full_names: set[str] = set()
    for repo in read_community_store_repositories(config_path):
        if repo.get("installed") is False:
            continue
        full_name = repo.get("full_name")
        if isinstance(full_name, str) and full_name:
            full_names.add(full_name.lower())
        if repo.get("category") not in (None, "integration"):
            continue
        domain = repo.get("domain")
        if isinstance(domain, str) and domain:
            domains.add(domain.lower())
        for d in repo.get("domains") or []:
            if isinstance(d, str) and d:
                domains.add(d.lower())
    return domains, full_names


def community_store_serves_hacsfiles(hass) -> bool:
    """True only when the legacy HACS custom integration serves /hacsfiles/.

    The built-in Marketplace no longer serves that path; it uses
    /local/community/ instead, which always works.
    """
    components = getattr(hass.config, "components", set())
    if "marketplace" in components:
        return False
    return "hacs" in hass.data or "hacs" in components
