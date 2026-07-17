"""Utility functions for internal use."""
from __future__ import annotations

import base64
import logging

_LOGGER = logging.getLogger(__name__)

# Ownership / management markers stored on each tracked package record.
MANAGED_BY_YIDSTORE = "yidstore"
MANAGED_BY_HACS = "hacs"
MANAGED_BY_MANUAL = "manual"

# Repository hosts we know how to fetch release metadata from.
SOURCE_GITHUB = "github"
SOURCE_GITEA = "gitea"

# Shown in the Home Assistant update dialog when a release carries no notes.
NO_RELEASE_NOTES = "No release notes were published for this version."

# Versions that are not real release tags and must never be diffed against a
# repository release (branch installs / unknown states).
_NON_RELEASE_VERSIONS = {"", "main", "master", "dev", "develop", "latest", "unknown", "none"}


def normalize_version(value: str | None) -> str:
    """Normalize a version/tag for comparison ("v3.2.0" -> "3.2.0")."""
    v = (value or "").strip()
    if v[:1].lower() == "v" and len(v) > 1 and v[1].isdigit():
        v = v[1:]
    return v


def is_comparable_version(value: str | None) -> bool:
    """Whether ``value`` is a real release version we can diff against.

    Branch installs ("main"/"master"/"dev") and unknown states can't be
    compared to a release tag — treating them as an update is always a guess
    and produces false positives.
    """
    return normalize_version(value).lower() not in _NON_RELEASE_VERSIONS


def version_is_newer(latest: str | None, installed: str | None) -> bool:
    """Return True when ``latest`` is a newer release than ``installed``.

    Uses Home Assistant's packaging version utility (AwesomeVersion) so the
    ordering is correct ("1.10.0" > "1.9.0") and the leading ``v`` is handled
    consistently. Never claims an update for branch/unknown installs, and
    fails safe (no update) when either version can't be parsed.
    """
    if not is_comparable_version(installed) or not is_comparable_version(latest):
        return False

    norm_latest = normalize_version(latest)
    norm_installed = normalize_version(installed)
    if norm_latest == norm_installed:
        return False

    try:
        from awesomeversion import AwesomeVersion
        from awesomeversion.exceptions import AwesomeVersionCompareException

        try:
            return AwesomeVersion(norm_latest) > AwesomeVersion(norm_installed)
        except AwesomeVersionCompareException:
            # Two versions we can't order (e.g. mixed schemes). Fall back to a
            # conservative string inequality — better to occasionally offer a
            # real update than to spam false ones, so only flag when clearly
            # different AND both look version-ish.
            return False
    except Exception:  # pragma: no cover - awesomeversion always present in HA
        return norm_latest != norm_installed


def normalize_release(raw: dict | None) -> dict | None:
    """Normalize a GitHub/Gitea release payload into shared package fields.

    GitHub and Gitea both expose the same release JSON keys (``tag_name``,
    ``name``, ``body``, ``html_url``, ``published_at``, ``draft``,
    ``prerelease``), so a single normalizer covers both sources. Returns
    ``None`` when there is no usable release.
    """
    if not isinstance(raw, dict):
        return None
    tag = (raw.get("tag_name") or raw.get("name") or "").strip()
    if not tag:
        return None
    name = (raw.get("name") or "").strip() or tag
    body = raw.get("body")
    if isinstance(body, str):
        body = body.strip() or None
    else:
        body = None
    return {
        "release_tag": tag,
        "release_summary": name,
        "release_notes": body,
        "release_url": raw.get("html_url") or None,
        "release_published_at": raw.get("published_at") or None,
        "prerelease": bool(raw.get("prerelease")),
        "draft": bool(raw.get("draft")),
    }


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


def scan_custom_components_versions(config_path: str) -> dict:
    """Map installed custom_components domains to their manifest versions.

    Keyed by both the folder name and (when different) the manifest ``domain``.
    This is a filesystem read — call it via ``hass.async_add_executor_job``.
    The on-disk manifest reflects the code currently installed; it proves the
    version, not who installed it.
    """
    import json
    from pathlib import Path

    root = Path(config_path) / "custom_components"
    versions: dict[str, str] = {}
    if not root.exists():
        return versions

    for domain_dir in root.iterdir():
        try:
            if not domain_dir.is_dir() or domain_dir.name.startswith("."):
                continue
            domain = domain_dir.name
            version = "unknown"
            manifest_path = domain_dir / "manifest.json"
            if manifest_path.is_file():
                try:
                    data = json.loads(manifest_path.read_text(encoding="utf-8"))
                    version = data.get("version") or version
                    manifest_domain = (data.get("domain") or "").strip()
                    if manifest_domain and manifest_domain.lower() != domain.lower():
                        versions.setdefault(manifest_domain.lower(), version)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Failed to read manifest for %s: %s", domain, err)
            versions[domain.lower()] = version
        except Exception:  # noqa: BLE001
            continue

    return versions


def load_hacs_state(config_path: str) -> dict:
    """Read HACS storage and return what HACS currently owns.

    Returns ``{"domains": set[str], "repos": set[str], "versions": {domain: version}}``.
    This is a filesystem read — call it via ``hass.async_add_executor_job``.
    HACS has used a couple of storage layouts over the years; we read both
    ``.storage/hacs.repositories`` (current) and ``.storage/hacs`` (legacy) and
    merge whatever we find. Never raises — a missing/garbled file just yields
    empty sets, which keeps YidStore from over-claiming HACS ownership.
    """
    import json
    from pathlib import Path

    domains: set[str] = set()
    repos: set[str] = set()
    versions: dict[str, str] = {}

    def _record(repo_info: dict) -> None:
        if not isinstance(repo_info, dict):
            return
        data = repo_info.get("data") if isinstance(repo_info.get("data"), dict) else repo_info
        category = data.get("category") or repo_info.get("category")
        installed = data.get("installed")
        if installed is None:
            installed = repo_info.get("installed")
        # ``hacs.repositories`` doesn't always carry an explicit installed flag;
        # presence of an installed version is a reliable proxy.
        version = (
            data.get("installed_version")
            or data.get("version_installed")
            or repo_info.get("installed_version")
            or repo_info.get("version_installed")
        )
        if installed is False and not version:
            return
        full_name = data.get("full_name") or repo_info.get("full_name")
        if isinstance(full_name, str) and full_name:
            repos.add(full_name.lower())
        dom_values = []
        for key in ("domain", "name"):
            val = data.get(key) or repo_info.get(key)
            if isinstance(val, str) and val:
                dom_values.append(val)
        for val in data.get("domains") or repo_info.get("domains") or []:
            if isinstance(val, str) and val:
                dom_values.append(val)
        # Only integration repositories participate in update-ownership; other
        # categories (plugin/theme) are still recorded as installed but we don't
        # need their per-domain version.
        is_integration = category in (None, "integration")
        for dom in dom_values:
            slug = dom.strip().lower().replace("-", "_")
            if not slug:
                continue
            domains.add(slug)
            if version and is_integration:
                versions.setdefault(slug, str(version))

    for filename in ("hacs.repositories", "hacs"):
        path = Path(config_path) / ".storage" / filename
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed to read HACS storage %s: %s", filename, err)
            continue
        data = raw.get("data", raw)
        if isinstance(data, dict):
            iterable = data.values()
        elif isinstance(data, list):
            iterable = data
        else:
            iterable = []
        for repo_info in iterable:
            try:
                _record(repo_info)
            except Exception:  # noqa: BLE001
                continue

    return {"domains": domains, "repos": repos, "versions": versions}


async def async_github_latest_release(
    hass, owner: str, repo: str, *, allow_prerelease: bool = False
) -> dict:
    """Fetch the latest GitHub release metadata (not just the redirect tag).

    Returns a dict::

        {"status": "ok"|"none"|"rate_limited"|"error", "release": <dict>|None}

    - ``ok``          a stable (non-draft, non-prerelease unless allowed)
                      release was found; ``release`` is the normalized payload.
    - ``none``        the repository has no matching release.
    - ``rate_limited``the unauthenticated REST API is rate limited (HTTP 403
                      with ``X-RateLimit-Remaining: 0``). Callers should reuse
                      cached data and must not treat this as "no update".
    - ``error``       any other failure (network, unexpected payload).

    This is the ONLY place that touches api.github.com. It's called at most
    once per repo per update-check interval, and its result is cached in the
    package record, so it doesn't spam the 60 req/hour unauthenticated limit.
    """
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    sess = async_get_clientsession(hass)
    url = f"https://api.github.com/repos/{owner}/{repo}/releases?per_page=30"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "YidStore",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with sess.get(url, headers=headers, timeout=30) as resp:
            if resp.status == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
                _LOGGER.debug("GitHub API rate limited for %s/%s", owner, repo)
                return {"status": "rate_limited", "release": None}
            if resp.status == 404:
                return {"status": "none", "release": None}
            if resp.status != 200:
                _LOGGER.debug(
                    "GitHub releases fetch failed for %s/%s (%s)", owner, repo, resp.status
                )
                return {"status": "error", "release": None}
            releases = await resp.json()
    except Exception as err:  # noqa: BLE001 - network errors handled gracefully
        _LOGGER.debug("GitHub releases fetch error for %s/%s: %s", owner, repo, err)
        return {"status": "error", "release": None}

    if not isinstance(releases, list) or not releases:
        return {"status": "none", "release": None}

    # The API returns releases newest-first. Skip drafts always; skip
    # prereleases unless the package opted in.
    for raw in releases:
        if not isinstance(raw, dict):
            continue
        if raw.get("draft"):
            continue
        if raw.get("prerelease") and not allow_prerelease:
            continue
        normalized = normalize_release(raw)
        if normalized:
            return {"status": "ok", "release": normalized}

    # Only drafts/prereleases exist and none qualify — no stable update.
    return {"status": "none", "release": None}


def github_archive_url(owner: str, repo: str, ref: str) -> str:
    """Zip download URL for a GitHub ref (tag or branch) WITHOUT the REST API.

    github.com/<o>/<r>/archive/<ref>.zip redirects to codeload.github.com,
    which is not API rate-limited (api.github.com/.../zipball is).
    """
    return f"https://github.com/{owner}/{repo}/archive/{ref}.zip"
