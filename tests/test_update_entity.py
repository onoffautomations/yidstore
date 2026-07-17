"""Update entity: release-notes routing, features, and yidstore-only creation."""
from unittest.mock import AsyncMock, Mock

import pytest

from homeassistant.components.update import UpdateEntityFeature

from custom_components.yidstore import update as update_mod
from custom_components.yidstore.const import DOMAIN
from custom_components.yidstore._utils import NO_RELEASE_NOTES, normalize_release

from helpers import FakeGiteaClient, gitea_release, make_coordinator, yidstore_package


def _make_entity(hass, coord, package_id):
    pkg = coord.packages[package_id]
    ent = update_mod.PackageUpdateEntity(coord, package_id, pkg, Mock(entry_id="e"))
    ent.hass = hass
    return ent


def _patch_github(monkeypatch, status="ok", release=None):
    mock = AsyncMock(return_value={"status": status, "release": release})
    monkeypatch.setattr(update_mod, "async_github_latest_release", mock)
    return mock


async def test_release_notes_prefers_stored(hass):
    coord = make_coordinator(hass, FakeGiteaClient())
    coord.packages = {"onoff_cool_integration": yidstore_package(release_notes="stored notes")}
    ent = _make_entity(hass, coord, "onoff_cool_integration")
    assert await ent.async_release_notes() == "stored notes"


# 3. GitHub release notes must not call the Gitea client.
async def test_github_notes_do_not_call_gitea(hass, monkeypatch):
    rel = normalize_release(gitea_release(tag="v2.0.0", body="gh body"))
    gh = _patch_github(monkeypatch, "ok", rel)
    client = FakeGiteaClient()  # must stay untouched
    coord = make_coordinator(hass, client)
    coord.packages = {"o_r": yidstore_package(owner="o", repo="r", domain="r",
                                              source="github", release_notes=None)}
    ent = _make_entity(hass, coord, "o_r")

    notes = await ent.async_release_notes()

    assert notes == "gh body"
    gh.assert_awaited_once()
    assert client.calls == []


# 4. Gitea release notes must not call GitHub.
async def test_gitea_notes_do_not_call_github(hass, monkeypatch):
    gh = _patch_github(monkeypatch, "ok", normalize_release(gitea_release()))
    client = FakeGiteaClient(release=gitea_release(body="gitea body"))
    coord = make_coordinator(hass, client)
    coord.packages = {"onoff_cool_integration": yidstore_package(source="gitea",
                                                                 release_notes=None)}
    ent = _make_entity(hass, coord, "onoff_cool_integration")

    notes = await ent.async_release_notes()

    assert notes == "gitea body"
    gh.assert_not_called()
    assert client.calls  # gitea client was used


async def test_release_notes_fallback(hass, monkeypatch):
    # No stored notes and the host returns nothing -> useful fallback message.
    _patch_github(monkeypatch, "none", None)
    coord = make_coordinator(hass, FakeGiteaClient())
    coord.packages = {"o_r": yidstore_package(owner="o", repo="r", domain="r",
                                              source="github", release_notes=None)}
    ent = _make_entity(hass, coord, "o_r")
    assert await ent.async_release_notes() == NO_RELEASE_NOTES


def test_supported_features_advertise_release_notes(hass):
    coord = make_coordinator(hass, FakeGiteaClient())
    coord.packages = {"onoff_cool_integration": yidstore_package(source="gitea")}
    ent = _make_entity(hass, coord, "onoff_cool_integration")
    feats = ent.supported_features
    assert UpdateEntityFeature.INSTALL in feats
    assert UpdateEntityFeature.RELEASE_NOTES in feats


def test_supported_features_no_release_notes_when_unretrievable(hass):
    # No owner/repo and no stored notes -> RELEASE_NOTES not advertised.
    coord = make_coordinator(hass, FakeGiteaClient())
    pkg = yidstore_package(source="unknown")
    pkg["owner"] = ""
    pkg["repo_name"] = ""
    pkg["source"] = "somethingelse"
    coord.packages = {"onoff_cool_integration": pkg}
    ent = _make_entity(hass, coord, "onoff_cool_integration")
    feats = ent.supported_features
    assert UpdateEntityFeature.INSTALL in feats
    assert UpdateEntityFeature.RELEASE_NOTES not in feats


def test_release_url_and_versions(hass):
    coord = make_coordinator(hass, FakeGiteaClient())
    coord.packages = {
        "onoff_cool_integration": yidstore_package(
            installed_version="1.0.0", latest_version="1.1.0", update_available=True,
            release_url="https://git.example.com/o/r/releases/tag/v1.1.0",
            release_summary="Cool 1.1.0",
        )
    }
    ent = _make_entity(hass, coord, "onoff_cool_integration")
    assert ent.installed_version == "1.0.0"
    assert ent.latest_version == "1.1.0"           # update available -> real latest
    assert ent.release_url.endswith("/releases/tag/v1.1.0")
    assert ent.release_summary == "Cool 1.1.0"


def test_latest_version_masks_phantom_update(hass):
    # No update -> latest mirrors installed so HA shows no phantom update.
    coord = make_coordinator(hass, FakeGiteaClient())
    coord.packages = {
        "onoff_cool_integration": yidstore_package(
            installed_version="v3.2.0", latest_version="3.2.0", update_available=False
        )
    }
    ent = _make_entity(hass, coord, "onoff_cool_integration")
    assert ent.latest_version == ent.installed_version == "v3.2.0"


# 12. HACS/manual packages get no YidStore update entity.
async def test_setup_creates_entities_only_for_yidstore(hass):
    coord = make_coordinator(hass, FakeGiteaClient())
    coord.packages = {
        "onoff_cool_integration": yidstore_package(),
        "o_hacsint": yidstore_package(owner="o", repo="hacsint", domain="hacsint",
                                      managed_by="hacs", installed_by_yidstore=False),
        "o_manualint": yidstore_package(owner="o", repo="manualint", domain="manualint",
                                        managed_by="manual", installed_by_yidstore=False),
    }
    hass.data.setdefault(DOMAIN, {})["e"] = {"coordinator": coord}
    added = []

    def _add(entities):
        added.extend(entities)

    await update_mod.async_setup_entry(hass, Mock(entry_id="e"), _add)

    ids = {e.package_id for e in added}
    assert ids == {"onoff_cool_integration"}
