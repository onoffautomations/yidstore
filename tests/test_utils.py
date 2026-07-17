"""Tests for the pure helpers and the GitHub/HACS readers."""
import json
from pathlib import Path

import pytest
from aioresponses import aioresponses

from custom_components.yidstore import _utils
from custom_components.yidstore._utils import (
    async_github_latest_release,
    is_comparable_version,
    load_hacs_state,
    normalize_release,
    normalize_version,
    scan_custom_components_versions,
    version_is_newer,
)


def test_normalize_and_comparable():
    assert normalize_version("v3.2.0") == "3.2.0"
    assert normalize_version("3.2.0") == "3.2.0"
    assert normalize_version(None) == ""
    assert is_comparable_version("v1.2.3")
    for bad in ("main", "master", "dev", "unknown", "", "latest"):
        assert not is_comparable_version(bad)


def test_version_is_newer_leading_v():
    # Case 7: differ only by a leading "v" -> not an update.
    assert version_is_newer("v3.2.0", "3.2.0") is False
    assert version_is_newer("3.2.0", "v3.2.0") is False


def test_version_is_newer_ordering():
    assert version_is_newer("1.10.0", "1.9.0") is True
    # Case 8: installed newer than latest -> not an update.
    assert version_is_newer("1.0.0", "2.0.0") is False
    assert version_is_newer("2.0.0", "1.0.0") is True


def test_version_is_newer_branch_installs():
    for branch in ("main", "master", "dev", "unknown"):
        assert version_is_newer("1.0.0", branch) is False
        assert version_is_newer(branch, "1.0.0") is False


def test_normalize_release():
    rel = normalize_release(
        {
            "tag_name": "v2.0.0",
            "name": "Two",
            "body": "  notes  ",
            "html_url": "http://x/rel",
            "published_at": "2024",
            "prerelease": True,
            "draft": False,
        }
    )
    assert rel["release_tag"] == "v2.0.0"
    assert rel["release_summary"] == "Two"
    assert rel["release_notes"] == "notes"
    assert rel["release_url"] == "http://x/rel"
    assert rel["prerelease"] is True
    # Missing/empty inputs.
    assert normalize_release(None) is None
    assert normalize_release({}) is None
    empty_body = normalize_release({"tag_name": "v1", "body": "   "})
    assert empty_body["release_notes"] is None
    assert empty_body["release_summary"] == "v1"


async def test_github_release_ok(hass):
    with aioresponses() as m:
        m.get(
            "https://api.github.com/repos/o/r/releases?per_page=30",
            payload=[
                {"tag_name": "v1.2.0", "name": "1.2.0", "body": "notes",
                 "html_url": "http://gh/rel", "published_at": "2024",
                 "draft": False, "prerelease": False},
            ],
        )
        result = await async_github_latest_release(hass, "o", "r")
    assert result["status"] == "ok"
    assert result["release"]["release_tag"] == "v1.2.0"
    assert result["release"]["release_notes"] == "notes"


async def test_github_release_none(hass):
    # Case 5: repository has no releases.
    with aioresponses() as m:
        m.get("https://api.github.com/repos/o/r/releases?per_page=30", payload=[])
        result = await async_github_latest_release(hass, "o", "r")
    assert result["status"] == "none"
    assert result["release"] is None


async def test_github_release_prerelease_only(hass):
    # Case 6: only a prerelease exists -> not a stable update.
    with aioresponses() as m:
        m.get(
            "https://api.github.com/repos/o/r/releases?per_page=30",
            payload=[
                {"tag_name": "v2.0.0-beta", "name": "beta", "body": "b",
                 "draft": False, "prerelease": True},
            ],
        )
        result = await async_github_latest_release(hass, "o", "r")
    assert result["status"] == "none"

    # ...but honored when the package opts into prereleases.
    with aioresponses() as m:
        m.get(
            "https://api.github.com/repos/o/r/releases?per_page=30",
            payload=[
                {"tag_name": "v2.0.0-beta", "name": "beta", "body": "b",
                 "draft": False, "prerelease": True},
            ],
        )
        result = await async_github_latest_release(hass, "o", "r", allow_prerelease=True)
    assert result["status"] == "ok"
    assert result["release"]["release_tag"] == "v2.0.0-beta"


async def test_github_release_skips_drafts(hass):
    with aioresponses() as m:
        m.get(
            "https://api.github.com/repos/o/r/releases?per_page=30",
            payload=[
                {"tag_name": "v3.0.0", "draft": True, "prerelease": False},
                {"tag_name": "v2.9.0", "name": "stable", "body": "n",
                 "draft": False, "prerelease": False},
            ],
        )
        result = await async_github_latest_release(hass, "o", "r")
    assert result["status"] == "ok"
    assert result["release"]["release_tag"] == "v2.9.0"


async def test_github_release_rate_limited(hass):
    # Case 19: rate limit must be reported, not treated as "no release".
    with aioresponses() as m:
        m.get(
            "https://api.github.com/repos/o/r/releases?per_page=30",
            status=403,
            headers={"X-RateLimit-Remaining": "0"},
        )
        result = await async_github_latest_release(hass, "o", "r")
    assert result["status"] == "rate_limited"
    assert result["release"] is None


async def test_github_release_network_error(hass):
    with aioresponses() as m:
        m.get(
            "https://api.github.com/repos/o/r/releases?per_page=30",
            exception=ConnectionError("boom"),
        )
        result = await async_github_latest_release(hass, "o", "r")
    assert result["status"] == "error"


def test_scan_custom_components_versions(tmp_path):
    cc = tmp_path / "custom_components" / "cool"
    cc.mkdir(parents=True)
    (cc / "manifest.json").write_text(
        json.dumps({"domain": "cool", "version": "1.4.2"}), encoding="utf-8"
    )
    versions = scan_custom_components_versions(str(tmp_path))
    assert versions["cool"] == "1.4.2"
    # Missing directory is safe.
    assert scan_custom_components_versions(str(tmp_path / "nope")) == {}


def test_load_hacs_state(tmp_path):
    storage = tmp_path / ".storage"
    storage.mkdir()
    (storage / "hacs.repositories").write_text(
        json.dumps(
            {
                "data": {
                    "123": {
                        "full_name": "someuser/awesome",
                        "domain": "awesome",
                        "category": "integration",
                        "installed": True,
                        "installed_version": "3.1.0",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    state = load_hacs_state(str(tmp_path))
    assert "awesome" in state["domains"]
    assert state["versions"]["awesome"] == "3.1.0"
    assert "someuser/awesome" in state["repos"]
    # Missing file is safe.
    assert load_hacs_state(str(tmp_path / "nope"))["domains"] == set()
