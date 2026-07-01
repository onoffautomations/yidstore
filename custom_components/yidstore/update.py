"""Update entities for OnOff Integration Store."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.update import (
    UpdateEntity,
    UpdateEntityFeature,
    UpdateDeviceClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, SERVICE_INSTALL

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up update entities for tracked packages."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    # Create update entities for all tracked packages
    entities = []
    for package_id, package_data in coordinator.packages.items():
        entities.append(PackageUpdateEntity(coordinator, package_id, package_data, entry))

    async_add_entities(entities)

    # Store callback for dynamic entity creation
    coordinator._add_update_entities_callback = async_add_entities


class PackageUpdateEntity(UpdateEntity):
    """Update entity for a tracked package."""

    _attr_has_entity_name = True
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL
        | UpdateEntityFeature.RELEASE_NOTES
    )

    def __init__(
        self,
        coordinator,
        package_id: str,
        package_data: dict[str, Any],
        entry: ConfigEntry,
    ) -> None:
        """Initialize the update entity."""
        self.coordinator = coordinator
        self.package_id = package_id
        self._package_data = package_data
        self._entry = entry
        self._attr_unique_id = f"{package_id}_update"
        self._attr_name = "Update"

        # Set device info
        repo_name = package_data.get("repo_name", package_id)
        owner = package_data.get("owner", "Unknown")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, package_id)},
            name=self._format_name(repo_name),
            manufacturer=owner,
            model=package_data.get("package_type", "integration").title(),
            sw_version=package_data.get("installed_version"),
        )

    def _format_name(self, name: str) -> str:
        """Format repo name for display."""
        s = name[2:] if name.startswith('x-') else name
        s = s.replace('_', ' ').replace('-', ' ')
        return ' '.join(word.capitalize() for word in s.split())

    @property
    def installed_version(self) -> str | None:
        """Return the installed version."""
        pkg = self.coordinator.packages.get(self.package_id, {})
        return pkg.get("installed_version")

    @property
    def latest_version(self) -> str | None:
        """Return the latest version.

        HA's update entity flags an update whenever this string differs
        from installed_version, so follow the coordinator's (normalized)
        decision — otherwise "v3.2.0" vs "3.2.0" shows a phantom update.
        """
        pkg = self.coordinator.packages.get(self.package_id, {})
        if not pkg.get("update_available"):
            return pkg.get("installed_version")
        return pkg.get("latest_version")

    @property
    def release_summary(self) -> str | None:
        """Return the release summary."""
        pkg = self.coordinator.packages.get(self.package_id, {})
        return pkg.get("release_summary")

    @property
    def title(self) -> str | None:
        """Return the title of the update."""
        pkg = self.coordinator.packages.get(self.package_id, {})
        repo_name = pkg.get("repo_name", self.package_id)
        return self._format_name(repo_name)

    @property
    def entity_picture(self) -> str | None:
        """Return the installed integration's own brand icon.

        Served by YidStore's brands endpoint, which prefers the icon
        shipped inside custom_components/<domain>/brand/ and falls back to
        the official HA brands site — so update entities show the
        integration's icon instead of YidStore's.
        """
        pkg = self.coordinator.packages.get(self.package_id, {})
        if pkg.get("package_type", "integration") != "integration":
            return None
        domain = pkg.get("domain") or pkg.get("repo_name", "").lower().replace("-", "_")
        if not domain:
            return None
        return f"/api/yidstore/brands/{domain}/icon.png"

    async def async_release_notes(self) -> str | None:
        """Return the release notes."""
        pkg = self.coordinator.packages.get(self.package_id, {})
        notes = pkg.get("release_notes")

        if notes:
            return notes

        # Try to fetch release notes from the latest release
        try:
            owner = pkg.get("owner")
            repo = pkg.get("repo_name")
            if owner and repo:
                release = await self.coordinator.client.get_latest_release(owner, repo)
                if release:
                    return release.get("body", "No release notes available.")
        except Exception as e:
            _LOGGER.debug("Failed to fetch release notes: %s", e)

        return "No release notes available."

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Install the update."""
        pkg = self.coordinator.packages.get(self.package_id, {})
        owner = pkg.get("owner")
        repo = pkg.get("repo_name")
        pkg_type = pkg.get("package_type", "integration")
        mode = pkg.get("mode")
        asset_name = pkg.get("asset_name")
        source = pkg.get("source")
        repo_url = pkg.get("repo_url")

        if not owner or not repo:
            _LOGGER.error("Cannot install update: missing owner or repo")
            return

        _LOGGER.info("Installing update for %s/%s to version %s", owner, repo, version or "latest")

        # Build service data
        service_data = {
            "owner": owner,
            "repo": repo,
            "type": pkg_type,
        }
        # Carry the source so GitHub-installed repos update via GitHub, not the
        # Gitea store (otherwise the latest-release lookup hits the wrong server
        # and 404s — and would leak the store URL in the error).
        if source:
            service_data["source"] = source
        if repo_url:
            service_data["repo_url"] = repo_url
        if mode:
            service_data["mode"] = mode
        if asset_name:
            service_data["asset_name"] = asset_name
        if version:
            service_data["tag"] = version

        # Call the install service
        await self.hass.services.async_call(
            DOMAIN,
            SERVICE_INSTALL,
            service_data,
            blocking=True,
        )

        _LOGGER.info("Update installed for %s", repo)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )
