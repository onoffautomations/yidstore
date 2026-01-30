"""Button platform for OnOff Integration Store."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OnOff Integration Store buttons."""
    coordinator = hass.data[DOMAIN][entry.entry_id].get("coordinator")

    if not coordinator:
        _LOGGER.error("Coordinator not found for entry %s", entry.entry_id)
        return

    # Store the callback in coordinator for dynamic button creation
    coordinator._add_button_entities_callback = async_add_entities
    _LOGGER.info("✓ Registered button entity callback with coordinator")

    # Create buttons for all currently tracked packages
    buttons = []
    if coordinator.packages:
        for package_id, package_data in coordinator.packages.items():
            buttons.append(PackageUpdateButton(coordinator, package_id, package_data, entry))
            buttons.append(PackageCheckUpdateButton(coordinator, package_id, package_data, entry))
        _LOGGER.info("Created %d buttons for %d existing packages", len(buttons), len(coordinator.packages))
    else:
        _LOGGER.info("No packages tracked yet - buttons will be created when packages are installed")

    async_add_entities(buttons)


class PackageUpdateButton(ButtonEntity):
    """Button to update a package."""

    _attr_should_poll = False

    def __init__(self, coordinator, package_id: str, package_data: dict, entry: ConfigEntry) -> None:
        """Initialize the button."""
        self._coordinator = coordinator
        self._package_id = package_id
        self._entry = entry
        self._attr_name = f"{package_data['repo_name']} Update"
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{package_id}_update_button"
        self._attr_icon = "mdi:package-up"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info - dynamically updated."""
        package_data = self._coordinator.packages.get(self._package_id, {})
        return DeviceInfo(
            identifiers={(DOMAIN, self._package_id)},
            name=package_data.get('repo_name', 'Unknown'),
            manufacturer="OnOff Integration Store",
            model=package_data.get('package_type', 'unknown').title(),
            sw_version=package_data.get('installed_version', 'unknown'),
        )

    async def async_press(self) -> None:
        """Handle the button press."""
        package_data = self._coordinator.packages.get(self._package_id)

        if not package_data:
            _LOGGER.error("Package data not found for %s", self._package_id)
            return

        repo_name = package_data.get('repo_name')
        owner = package_data.get('owner')
        package_type = package_data.get('package_type')
        mode = package_data.get('mode')
        asset_name = package_data.get('asset_name')

        _LOGGER.info("Update button pressed for %s/%s (type: %s)", owner, repo_name, package_type)

        # Determine which service to call
        if package_type == "integration":
            service_name = "install_integration"
        elif package_type == "lovelace":
            service_name = "install_lovelace"
        elif package_type == "blueprints":
            service_name = "install_blueprints"
        else:
            _LOGGER.error("Unknown package type: %s", package_type)
            return

        # Build service data
        service_data = {
            "owner": owner,
            "repo": repo_name,
        }

        if mode:
            service_data["mode"] = mode
        if asset_name:
            service_data["asset_name"] = asset_name

        try:
            # Call the install service
            await self.hass.services.async_call(
                DOMAIN,
                service_name,
                service_data,
                blocking=True
            )

            _LOGGER.info("✓ Update triggered for %s/%s", owner, repo_name)

            # Send notification
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "YidStore - Update Started",
                    "message": f"Updating **{repo_name}**...\n\nCheck logs for progress.",
                    "notification_id": f"onoff_store_update_button_{self._package_id}"
                },
                blocking=False
            )

        except Exception as e:
            _LOGGER.error("Failed to update %s/%s: %s", owner, repo_name, e, exc_info=True)

            # Send error notification
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "YidStore - Update Failed",
                    "message": f"Failed to update **{repo_name}**\n\nError: {str(e)}",
                    "notification_id": f"onoff_store_update_error_{self._package_id}"
                },
                blocking=False
            )

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()


class PackageCheckUpdateButton(ButtonEntity):
    """Button to check for updates."""

    _attr_should_poll = False

    def __init__(self, coordinator, package_id: str, package_data: dict, entry: ConfigEntry) -> None:
        """Initialize the button."""
        self._coordinator = coordinator
        self._package_id = package_id
        self._entry = entry
        self._attr_name = f"{package_data['repo_name']} Check for Updates"
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{package_id}_check_update_button"
        self._attr_icon = "mdi:refresh"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info - dynamically updated."""
        package_data = self._coordinator.packages.get(self._package_id, {})
        return DeviceInfo(
            identifiers={(DOMAIN, self._package_id)},
            name=package_data.get('repo_name', 'Unknown'),
            manufacturer="OnOff Integration Store",
            model=package_data.get('package_type', 'unknown').title(),
            sw_version=package_data.get('installed_version', 'unknown'),
        )

    async def async_press(self) -> None:
        """Handle the button press."""
        _LOGGER.info("Check for updates button pressed for package %s", self._package_id)
        await self._coordinator.async_check_updates()

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()
