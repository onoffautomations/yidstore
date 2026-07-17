"""Coordinator ownership + update-check tests."""
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.yidstore import coordinator as coord_mod
from custom_components.yidstore._utils import normalize_release

from helpers import (
    FakeGiteaClient,
    gitea_release,
    make_coordinator,
    write_hacs_repositories,
    write_manifest,
    yidstore_package,
)


def _patch_github(monkeypatch, status="ok", release=None):
    mock = AsyncMock(return_value={"status": status, "release": release})
    monkeypatch.setattr(coord_mod, "async_github_latest_release", mock)
    return mock


# 1. YidStore-managed Gitea integration has an update with release notes.
async def test_gitea_managed_update_with_notes(hass):
    client = FakeGiteaClient(release=gitea_release(tag="v1.1.0", body="rel notes"))
    coord = make_coordinator(hass, client)
    coord.packages = {"onoff_cool_integration": yidstore_package(installed_version="1.0.0")}

    await coord.async_check_updates()

    pkg = coord.packages["onoff_cool_integration"]
    assert pkg["update_available"] is True
    assert pkg["latest_version"] == "v1.1.0"
    assert pkg["release_notes"] == "rel notes"
    assert pkg["release_summary"] == "Release 1.1.0"
    assert pkg["release_url"].endswith("/releases/tag/v1.1.0")
    assert client.calls == [("onoff", "cool_integration")]


# 2. YidStore-managed GitHub integration has an update with release notes.
async def test_github_managed_update_with_notes(hass, monkeypatch):
    rel = normalize_release(gitea_release(tag="v2.0.0", body="gh notes",
                                          html_url="https://github.com/o/r/releases/tag/v2.0.0"))
    _patch_github(monkeypatch, "ok", rel)
    client = FakeGiteaClient()
    coord = make_coordinator(hass, client)
    coord.packages = {"o_r": yidstore_package(owner="o", repo="r", domain="r",
                                              source="github", installed_version="1.0.0")}

    await coord.async_check_updates()

    pkg = coord.packages["o_r"]
    assert pkg["update_available"] is True
    assert pkg["latest_version"] == "v2.0.0"
    assert pkg["release_notes"] == "gh notes"
    # 3. GitHub check must not touch the Gitea client.
    assert client.calls == []


# 4. Gitea release check must not call GitHub.
async def test_gitea_check_does_not_call_github(hass, monkeypatch):
    gh = _patch_github(monkeypatch, "ok", normalize_release(gitea_release()))
    client = FakeGiteaClient(release=gitea_release(tag="v1.1.0"))
    coord = make_coordinator(hass, client)
    coord.packages = {"onoff_cool_integration": yidstore_package(source="gitea")}

    await coord.async_check_updates()

    gh.assert_not_called()
    assert client.calls  # gitea client was used


# 5. GitHub repository has no releases -> no update, record intact.
async def test_github_no_releases(hass, monkeypatch):
    _patch_github(monkeypatch, "none", None)
    coord = make_coordinator(hass)
    coord.packages = {"o_r": yidstore_package(owner="o", repo="r", domain="r",
                                              source="github", installed_version="1.0.0")}

    await coord.async_check_updates()

    pkg = coord.packages["o_r"]
    assert pkg["update_available"] is False
    assert pkg["installed_version"] == "1.0.0"


# 6. GitHub latest release is a prerelease -> helper returns none -> no update.
async def test_github_prerelease_only_no_update(hass, monkeypatch):
    _patch_github(monkeypatch, "none", None)
    coord = make_coordinator(hass)
    coord.packages = {"o_r": yidstore_package(owner="o", repo="r", domain="r",
                                              source="github", installed_version="1.0.0")}
    await coord.async_check_updates()
    assert coord.packages["o_r"]["update_available"] is False


# 7. Installed and latest differ only by a leading "v".
async def test_leading_v_no_update(hass):
    client = FakeGiteaClient(release=gitea_release(tag="1.2.3"))
    coord = make_coordinator(hass, client)
    coord.packages = {"onoff_cool_integration": yidstore_package(installed_version="v1.2.3")}
    await coord.async_check_updates()
    assert coord.packages["onoff_cool_integration"]["update_available"] is False


# 8. Installed version is newer than the latest repository release.
async def test_installed_newer_than_release(hass):
    client = FakeGiteaClient(release=gitea_release(tag="v1.0.0"))
    coord = make_coordinator(hass, client)
    coord.packages = {"onoff_cool_integration": yidstore_package(installed_version="2.0.0")}
    await coord.async_check_updates()
    assert coord.packages["onoff_cool_integration"]["update_available"] is False


# 9. HACS-managed integration is fully updated.
async def test_hacs_managed_fully_updated(hass, monkeypatch):
    gh = _patch_github(monkeypatch, "ok", normalize_release(gitea_release()))
    write_hacs_repositories(hass, {"1": {"full_name": "o/awesome", "domain": "awesome",
                                         "category": "integration", "installed": True,
                                         "installed_version": "3.1.0"}})
    client = FakeGiteaClient(release=gitea_release(tag="v9.9.9"))
    coord = make_coordinator(hass, client)
    coord.packages = {"o_awesome": yidstore_package(owner="o", repo="awesome", domain="awesome",
                                                     managed_by="hacs", installed_by_yidstore=False,
                                                     installed_version="3.0.0")}
    await coord.async_check_updates()
    pkg = coord.packages["o_awesome"]
    assert pkg["update_available"] is False          # HACS owns updates
    assert pkg["installed_version"] == "3.1.0"        # synced from HACS metadata
    gh.assert_not_called()
    assert client.calls == []


# 10 & 12. HACS-managed integration is outdated -> still no actionable YidStore update.
async def test_hacs_managed_outdated_no_yidstore_update(hass):
    write_hacs_repositories(hass, {"1": {"full_name": "o/awesome", "domain": "awesome",
                                         "category": "integration", "installed": True,
                                         "installed_version": "3.1.0"}})
    client = FakeGiteaClient(release=gitea_release(tag="v4.0.0"))
    coord = make_coordinator(hass, client)
    coord.packages = {"o_awesome": yidstore_package(owner="o", repo="awesome", domain="awesome",
                                                     managed_by="hacs", installed_by_yidstore=False,
                                                     installed_version="3.1.0")}
    await coord.async_check_updates()
    assert coord.packages["o_awesome"]["update_available"] is False
    assert client.calls == []


# 11. HACS updates an integration and YidStore synchronizes the installed version.
async def test_hacs_update_syncs_installed_version(hass):
    write_hacs_repositories(hass, {"1": {"full_name": "o/awesome", "domain": "awesome",
                                         "category": "integration", "installed": True,
                                         "installed_version": "3.2.0"}})
    coord = make_coordinator(hass)
    coord.packages = {"o_awesome": yidstore_package(owner="o", repo="awesome", domain="awesome",
                                                     managed_by="hacs", installed_by_yidstore=False,
                                                     installed_version="3.1.0")}
    await coord.async_check_updates()
    assert coord.packages["o_awesome"]["installed_version"] == "3.2.0"
    assert coord.packages["o_awesome"]["update_available"] is False


# 13. Manually installed integration does not expose an actionable YidStore update.
async def test_manual_no_actionable_update(hass, monkeypatch):
    gh = _patch_github(monkeypatch, "ok", normalize_release(gitea_release()))
    client = FakeGiteaClient(release=gitea_release(tag="v5.0.0"))
    coord = make_coordinator(hass, client)
    coord.packages = {"onoff_cool_integration": yidstore_package(managed_by="manual",
                                                      installed_by_yidstore=False)}
    await coord.async_check_updates()
    assert coord.packages["onoff_cool_integration"]["update_available"] is False
    assert client.calls == []
    gh.assert_not_called()


# 14. is_yidstore_managed: a mere disk/HACS match never proves ownership.
def test_ownership_helper():
    assert coord_mod.OnOffGiteaStoreCoordinator.is_yidstore_managed(
        {"managed_by": "yidstore"}) is True
    assert coord_mod.OnOffGiteaStoreCoordinator.is_yidstore_managed(
        {"managed_by": "hacs"}) is False
    assert coord_mod.OnOffGiteaStoreCoordinator.is_yidstore_managed(
        {"managed_by": "manual"}) is False
    assert coord_mod.OnOffGiteaStoreCoordinator.is_yidstore_managed(None) is False
    # Legacy record (pre-migration) defaults to managed.
    assert coord_mod.OnOffGiteaStoreCoordinator.is_yidstore_managed(
        {"installed_by_yidstore": True}) is True


# 15. YidStore install stays YidStore-managed even when HACS recognizes the domain.
async def test_yidstore_ownership_not_overwritten_by_hacs(hass):
    coord = make_coordinator(hass)
    # Existing proven YidStore install.
    coord.packages["onoff_cool_integration"] = yidstore_package(installed_version="1.0.0")
    # Re-track (e.g. a version bump) while HACS also lists the domain.
    await coord.async_add_or_update_package(
        repo_name="cool_integration", owner="onoff", package_type="integration",
        installed_version="1.1.0", source="gitea", domain="cool_integration",
        managed_by="yidstore", installed_by_yidstore=True,
    )
    pkg = coord.packages["onoff_cool_integration"]
    assert pkg["managed_by"] == "yidstore"
    assert pkg["installed_by_yidstore"] is True
    assert pkg["installation_id"]  # durable marker preserved


# 16. Existing storage records migrate without losing package information.
async def test_migration_preserves_data(hass):
    write_hacs_repositories(hass, {"1": {"full_name": "o/hacsint", "domain": "hacsint",
                                         "category": "integration", "installed": True,
                                         "installed_version": "2.0.0"}})
    coord = make_coordinator(hass)
    coord.packages = {
        "onoff_cool_integration": {  # legacy YidStore install (no ownership fields)
            "repo_name": "cool_integration", "owner": "onoff",
            "package_type": "integration", "installed_version": "1.0.0",
            "source": "gitea", "domain": "cool_integration", "mode": "zipball",
            "install_date": "2023-01-01T00:00:00", "asset_name": "x.zip",
        },
        "o_hacsint": {  # legacy record for something HACS now owns
            "repo_name": "hacsint", "owner": "o", "package_type": "integration",
            "installed_version": "2.0.0", "source": "gitea", "domain": "hacsint",
        },
    }
    await coord._async_migrate_packages()

    cool = coord.packages["onoff_cool_integration"]
    assert cool["managed_by"] == "yidstore"
    assert cool["installed_by_yidstore"] is True
    # No data was lost.
    assert cool["mode"] == "zipball"
    assert cool["asset_name"] == "x.zip"
    assert cool["install_date"] == "2023-01-01T00:00:00"

    hacsint = coord.packages["o_hacsint"]
    assert hacsint["managed_by"] == "hacs"
    assert hacsint["installed_by_yidstore"] is False


# 17. Update entity/sensors refresh after a check.
async def test_listeners_notified_after_check(hass):
    client = FakeGiteaClient(release=gitea_release())
    coord = make_coordinator(hass, client)
    coord.packages = {"onoff_cool_integration": yidstore_package()}
    coord.async_update_listeners = Mock()
    await coord.async_check_updates()
    coord.async_update_listeners.assert_called()


# 17b. State refreshes after install/registration.
async def test_listeners_notified_after_install(hass):
    coord = make_coordinator(hass)
    coord.packages["onoff_cool_integration"] = yidstore_package()
    coord.async_update_listeners = Mock()
    await coord.async_add_or_update_package(
        repo_name="cool_integration", owner="onoff", package_type="integration",
        installed_version="1.1.0", source="gitea", domain="cool_integration",
        managed_by="yidstore", installed_by_yidstore=True,
    )
    coord.async_update_listeners.assert_called()


# 18. Release notes/summary/URL survive a Home Assistant restart via storage.
async def test_release_fields_survive_restart(hass):
    coord = make_coordinator(hass)
    await coord.async_add_or_update_package(
        repo_name="cool_integration", owner="onoff", package_type="integration",
        installed_version="1.1.0", source="gitea", domain="cool_integration",
        managed_by="yidstore", installed_by_yidstore=True,
        release=gitea_release(tag="v1.1.0", name="Cool 1.1.0", body="persisted notes"),
    )
    # Simulate a restart: brand new coordinator loading from the same store.
    coord2 = make_coordinator(hass)
    await coord2.async_load_packages()
    pkg = coord2.packages["onoff_cool_integration"]
    assert pkg["release_notes"] == "persisted notes"
    assert pkg["release_summary"] == "Cool 1.1.0"
    assert pkg["release_url"].endswith("/releases/tag/v1.1.0")


# 19. GitHub API failure/rate limit does not create a false update.
async def test_github_rate_limit_no_false_update(hass, monkeypatch):
    _patch_github(monkeypatch, "rate_limited", None)
    coord = make_coordinator(hass)
    coord.packages = {"o_r": yidstore_package(owner="o", repo="r", domain="r",
                                              source="github", installed_version="1.0.0",
                                              latest_version="1.0.0")}
    await coord.async_check_updates()
    pkg = coord.packages["o_r"]
    assert pkg["update_available"] is False
    assert pkg["installed_version"] == "1.0.0"


# 20. An unavailable repo does not remove or corrupt the existing package record.
async def test_unavailable_repo_keeps_record(hass):
    client = FakeGiteaClient(raise_exc=RuntimeError("Latest release fetch failed (404)"))
    coord = make_coordinator(hass, client)
    original = yidstore_package(installed_version="1.0.0", latest_version="1.0.0",
                                release_notes="old notes")
    coord.packages = {"onoff_cool_integration": dict(original)}
    await coord.async_check_updates()
    pkg = coord.packages["onoff_cool_integration"]
    assert pkg["installed_version"] == "1.0.0"
    assert pkg["update_available"] is False
    assert pkg["release_notes"] == "old notes"  # not corrupted
    assert "last_check" in pkg
