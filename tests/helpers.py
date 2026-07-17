"""Shared test helpers."""
from __future__ import annotations

import json
from pathlib import Path

from custom_components.yidstore.coordinator import OnOffGiteaStoreCoordinator


class FakeGiteaClient:
    """Minimal stand-in for GiteaClient used in coordinator/update tests."""

    def __init__(self, release=None, raise_exc=None, token=None):
        self._release = release
        self._raise_exc = raise_exc
        self.token = token
        self.calls: list[tuple[str, str]] = []

    async def get_latest_release(self, owner: str, repo: str) -> dict:
        self.calls.append((owner, repo))
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._release


def make_coordinator(hass, client=None) -> OnOffGiteaStoreCoordinator:
    """Build a coordinator backed by the real hass fixture."""
    return OnOffGiteaStoreCoordinator(hass, "test_entry", client or FakeGiteaClient())


def gitea_release(
    tag="v1.1.0",
    name="Release 1.1.0",
    body="## What's new\n- Fixed bugs",
    html_url="https://git.example.com/o/r/releases/tag/v1.1.0",
    published_at="2024-06-01T00:00:00Z",
    draft=False,
    prerelease=False,
):
    return {
        "tag_name": tag,
        "name": name,
        "body": body,
        "html_url": html_url,
        "published_at": published_at,
        "draft": draft,
        "prerelease": prerelease,
    }


def write_hacs_repositories(hass, entries: dict) -> None:
    """Write a .storage/hacs.repositories file with the given repo records."""
    storage = Path(hass.config.config_dir) / ".storage"
    storage.mkdir(parents=True, exist_ok=True)
    (storage / "hacs.repositories").write_text(
        json.dumps({"version": 1, "data": entries}), encoding="utf-8"
    )


def write_manifest(hass, domain: str, version: str) -> None:
    """Write custom_components/<domain>/manifest.json on the test config dir."""
    d = Path(hass.config.config_dir) / "custom_components" / domain
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(
        json.dumps({"domain": domain, "name": domain, "version": version}),
        encoding="utf-8",
    )


def yidstore_package(
    *,
    owner="onoff",
    repo="cool_integration",
    domain="cool_integration",
    source="gitea",
    installed_version="1.0.0",
    managed_by="yidstore",
    installed_by_yidstore=True,
    **extra,
) -> dict:
    pkg = {
        "repo_name": repo,
        "owner": owner,
        "package_type": "integration",
        "installed_version": installed_version,
        "latest_version": installed_version,
        "update_available": False,
        "source": source,
        "domain": domain,
        "managed_by": managed_by,
        "installed_by_yidstore": installed_by_yidstore,
        "installation_id": "abc123",
    }
    pkg.update(extra)
    return pkg
