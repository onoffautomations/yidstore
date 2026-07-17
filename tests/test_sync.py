"""`_sync_preinstalled_integrations`: detection is never ownership."""
from unittest.mock import Mock

import pytest

from custom_components.yidstore import _sync_preinstalled_integrations

from helpers import make_coordinator, write_hacs_repositories, write_manifest, yidstore_package


def _entry():
    return Mock(data={"owner": "onoff"})


# 14. A matching custom_components folder does not prove YidStore ownership:
#     an untracked on-disk integration is never auto-adopted as YidStore-managed.
async def test_sync_does_not_auto_adopt_disk_integration(hass):
    write_manifest(hass, "randomint", "1.0.0")
    write_hacs_repositories(hass, {"1": {"full_name": "x/randomint", "domain": "randomint",
                                         "category": "integration", "installed": True,
                                         "installed_version": "1.0.0"}})
    coord = make_coordinator(hass)
    coord.packages = {}
    await _sync_preinstalled_integrations(hass, coord, _entry())
    assert coord.packages == {}  # nothing adopted


# 15. A proven YidStore install stays YidStore-managed even when HACS lists the domain.
async def test_sync_keeps_yidstore_ownership_when_hacs_present(hass):
    write_manifest(hass, "cool_integration", "1.0.0")
    write_hacs_repositories(hass, {"1": {"full_name": "onoff/cool_integration",
                                         "domain": "cool_integration",
                                         "category": "integration", "installed": True,
                                         "installed_version": "1.0.0"}})
    coord = make_coordinator(hass)
    coord.packages = {"onoff_cool_integration": yidstore_package(installed_version="1.0.0")}
    await _sync_preinstalled_integrations(hass, coord, _entry())
    pkg = coord.packages["onoff_cool_integration"]
    assert pkg["managed_by"] == "yidstore"
    assert pkg["installed_by_yidstore"] is True


async def test_sync_reconciles_installed_version_from_disk(hass):
    write_manifest(hass, "cool_integration", "1.3.0")
    coord = make_coordinator(hass)
    coord.packages = {"onoff_cool_integration": yidstore_package(installed_version="1.0.0")}
    await _sync_preinstalled_integrations(hass, coord, _entry())
    assert coord.packages["onoff_cool_integration"]["installed_version"] == "1.3.0"


async def test_sync_reclaims_hacs_for_non_yidstore_package(hass):
    # A previously manual/tracked package that HACS now owns should flip to HACS.
    write_manifest(hass, "cool_integration", "2.0.0")
    write_hacs_repositories(hass, {"1": {"full_name": "onoff/cool_integration",
                                         "domain": "cool_integration",
                                         "category": "integration", "installed": True,
                                         "installed_version": "2.0.0"}})
    coord = make_coordinator(hass)
    coord.packages = {"onoff_cool_integration": yidstore_package(
        managed_by="manual", installed_by_yidstore=False, installed_version="1.0.0")}
    await _sync_preinstalled_integrations(hass, coord, _entry())
    pkg = coord.packages["onoff_cool_integration"]
    assert pkg["managed_by"] == "hacs"
    assert pkg["installed_version"] == "2.0.0"
