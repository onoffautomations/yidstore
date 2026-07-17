"""Install-time release-metadata capture routes to the correct host."""
from unittest.mock import AsyncMock

import pytest

from custom_components.yidstore import _fetch_release_metadata
import custom_components.yidstore as yidstore_pkg
from custom_components.yidstore._utils import normalize_release

from helpers import FakeGiteaClient, gitea_release


async def test_fetch_github_metadata(hass, monkeypatch):
    rel = normalize_release(gitea_release(tag="v2.0.0", body="gh notes",
                                          html_url="https://github.com/o/r/releases/tag/v2.0.0"))
    monkeypatch.setattr(
        yidstore_pkg, "async_github_latest_release",
        AsyncMock(return_value={"status": "ok", "release": rel}),
    )
    client = FakeGiteaClient()  # must not be touched for a GitHub package
    meta = await _fetch_release_metadata(hass, client, "o", "r", "github")
    assert meta["tag_name"] == "v2.0.0"
    assert meta["body"] == "gh notes"
    assert meta["html_url"].endswith("/releases/tag/v2.0.0")
    assert client.calls == []


async def test_fetch_gitea_metadata(hass, monkeypatch):
    gh = AsyncMock()
    monkeypatch.setattr(yidstore_pkg, "async_github_latest_release", gh)
    client = FakeGiteaClient(release=gitea_release(tag="v1.1.0", body="gitea notes"))
    meta = await _fetch_release_metadata(hass, client, "onoff", "cool", "gitea")
    assert meta["tag_name"] == "v1.1.0"
    assert meta["body"] == "gitea notes"
    assert client.calls == [("onoff", "cool")]
    gh.assert_not_called()


async def test_fetch_metadata_handles_failure(hass):
    client = FakeGiteaClient(raise_exc=RuntimeError("Latest release fetch failed (404)"))
    meta = await _fetch_release_metadata(hass, client, "onoff", "cool", "gitea")
    assert meta is None  # graceful, install continues
