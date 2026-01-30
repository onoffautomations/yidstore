"""Sensor platform for OnOff Integration Store."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    STORAGE_KEY_PACKAGES,
    STORAGE_VERSION,
    UPDATE_CHECK_INTERVAL,
    ATTR_REPO_NAME,
    ATTR_OWNER,
    ATTR_PACKAGE_TYPE,
    ATTR_INSTALLED_VERSION,
    ATTR_LATEST_VERSION,
    ATTR_UPDATE_AVAILABLE,
    ATTR_INSTALL_DATE,
    ATTR_LAST_CHECK,
    ATTR_RELEASE_SUMMARY,
    ATTR_RELEASE_NOTES,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OnOff Integration Store sensors."""
    coordinator = hass.data[DOMAIN][entry.entry_id].get("coordinator")

    if not coordinator:
        _LOGGER.error("Coordinator not found for entry %s", entry.entry_id)
        return

    # Store the callback in coordinator for dynamic sensor creation
    coordinator._add_entities_callback = async_add_entities
    _LOGGER.info("✓ Registered entity callback with coordinator")

    # Create sensors for all currently tracked packages
    sensors = []
    if coordinator.packages:
        for package_id, package_data in coordinator.packages.items():
            package_type = package_data.get('package_type', 'unknown')
            _LOGGER.info("Creating sensors for package: %s (type: %s)", package_id, package_type)

            sensors.extend([
                PackageVersionSensor(coordinator, package_id, package_data, entry.entry_id),
                PackageUpdateSensor(coordinator, package_id, package_data, entry.entry_id),
                PackageTypeSensor(coordinator, package_id, package_data, entry.entry_id),
            ])
            coordinator._created_entities.add(package_id)

            # Add restart sensor only for integrations
            if package_type == 'integration':
                sensors.append(WaitingRestartSensor(coordinator, package_id, package_data, hass, entry.entry_id))
                _LOGGER.info("✓ Added WaitingRestartSensor for integration: %s", package_id)
            else:
                _LOGGER.debug("Skipped WaitingRestartSensor for non-integration package: %s", package_id)

        _LOGGER.info("✓✓✓ Created %d total sensors for %d packages", len(sensors), len(coordinator.packages))
    else:
        _LOGGER.info("No packages tracked yet - sensors will be created when packages are installed")

    async_add_entities(sensors)

    # Start periodic update check (only start once per entry)
    if not hasattr(coordinator, '_update_checker_started'):
        async_track_time_interval(
            hass,
            coordinator.async_check_updates,
            timedelta(seconds=UPDATE_CHECK_INTERVAL)
        )
        coordinator._update_checker_started = True
        _LOGGER.info("Started update checker (every %d seconds)", UPDATE_CHECK_INTERVAL)


class PackageVersionSensor(SensorEntity):
    """Sensor for installed package version."""

    _attr_should_poll = False

    def __init__(self, coordinator, package_id: str, package_data: dict, entry_id: str) -> None:
        """Initialize the sensor."""
        self._coordinator = coordinator
        self._package_id = package_id
        self._entry_id = entry_id
        self._attr_name = f"{package_data['repo_name']} Version"
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{package_id}_version"
        self._attr_icon = "mdi:package-variant"

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

    @property
    def native_value(self) -> str:
        """Return the installed version."""
        package_data = self._coordinator.packages.get(self._package_id, {})
        version = package_data.get('installed_version', 'unknown')
        _LOGGER.debug("Version sensor for %s: %s", self._package_id, version)
        return version

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        package_data = self._coordinator.packages.get(self._package_id, {})
        return {
            ATTR_REPO_NAME: package_data.get('repo_name'),
            ATTR_OWNER: package_data.get('owner'),
            ATTR_PACKAGE_TYPE: package_data.get('package_type'),
            ATTR_INSTALL_DATE: package_data.get('install_date'),
            ATTR_LATEST_VERSION: package_data.get('latest_version', 'unknown'),
        }


class PackageUpdateSensor(SensorEntity):
    """Binary sensor for update availability."""

    _attr_should_poll = False

    def __init__(self, coordinator, package_id: str, package_data: dict, entry_id: str) -> None:
        """Initialize the sensor."""
        self._coordinator = coordinator
        self._package_id = package_id
        self._entry_id = entry_id
        self._attr_name = f"{package_data['repo_name']} Update Available"
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{package_id}_update"
        self._attr_icon = "mdi:update"

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

    @property
    def native_value(self) -> str:
        """Return Yes/No for update availability."""
        package_data = self._coordinator.packages.get(self._package_id, {})
        update_available = package_data.get('update_available', False)
        result = "Yes" if update_available else "No"
        _LOGGER.debug("Update sensor for %s: %s (installed=%s, latest=%s)",
                     self._package_id, result,
                     package_data.get('installed_version'),
                     package_data.get('latest_version'))
        return result

    @property
    def icon(self) -> str:
        """Return icon based on update status."""
        package_data = self._coordinator.packages.get(self._package_id, {})
        update_available = package_data.get('update_available', False)
        return "mdi:alert-circle" if update_available else "mdi:check-circle"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        package_data = self._coordinator.packages.get(self._package_id, {})
        return {
            ATTR_INSTALLED_VERSION: package_data.get('installed_version'),
            ATTR_LATEST_VERSION: package_data.get('latest_version', 'unknown'),
            ATTR_LAST_CHECK: package_data.get('last_check'),
            ATTR_UPDATE_AVAILABLE: package_data.get('update_available', False),
            ATTR_RELEASE_SUMMARY: package_data.get('release_summary'),
            ATTR_RELEASE_NOTES: package_data.get('release_notes'),
        }


class PackageTypeSensor(SensorEntity):
    """Diagnostic sensor for package type."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, package_id: str, package_data: dict, entry_id: str) -> None:
        """Initialize the sensor."""
        self._coordinator = coordinator
        self._package_id = package_id
        self._entry_id = entry_id
        self._attr_name = f"{package_data['repo_name']} Type"
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{package_id}_type"
        self._attr_icon = "mdi:package-variant-closed"

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

    @property
    def native_value(self) -> str:
        """Return the package type."""
        package_data = self._coordinator.packages.get(self._package_id, {})
        package_type = package_data.get('package_type', 'unknown')
        # Capitalize properly: integration -> Integration
        return package_type.title()

    @property
    def icon(self) -> str:
        """Return icon based on package type."""
        package_data = self._coordinator.packages.get(self._package_id, {})
        package_type = package_data.get('package_type', 'unknown')

        icon_map = {
            'integration': 'mdi:puzzle',
            'lovelace': 'mdi:view-dashboard',
            'blueprints': 'mdi:file-document-outline',
        }
        return icon_map.get(package_type, 'mdi:package-variant-closed')


class WaitingRestartSensor(SensorEntity):
    """Sensor to show if integration needs restart after update."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, package_id: str, package_data: dict, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize the sensor."""
        self._coordinator = coordinator
        self._package_id = package_id
        self._hass = hass
        self._entry_id = entry_id
        self._attr_name = f"{package_data['repo_name']} Waiting Restart"
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{package_id}_waiting_restart"
        self._attr_icon = "mdi:restart"

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

    @property
    def native_value(self) -> str:
        """Return Yes/No based on whether restart is needed."""
        package_data = self._coordinator.packages.get(self._package_id, {})

        # Get last_update timestamp (when package was last installed/updated)
        last_update_str = package_data.get('last_update')
        if not last_update_str:
            return "No"

        try:
            # Parse last_update timestamp
            last_update = datetime.fromisoformat(last_update_str)

            # Get HA start time
            ha_start_time = self._hass.data.get('homeassistant_start_time')
            if not ha_start_time:
                # Fallback: check if component was recently updated (within last 5 minutes of HA runtime)
                # If we can't determine start time, assume no restart needed
                return "No"

            # If last_update is AFTER HA started, restart is needed
            if last_update > ha_start_time:
                _LOGGER.debug(
                    "Package %s needs restart: last_update=%s > ha_start=%s",
                    self._package_id, last_update, ha_start_time
                )
                return "Yes"
            else:
                return "No"

        except Exception as e:
            _LOGGER.debug("Error checking restart status for %s: %s", self._package_id, e)
            return "No"

    @property
    def icon(self) -> str:
        """Return icon based on restart status."""
        needs_restart = self.native_value == "Yes"
        return "mdi:restart-alert" if needs_restart else "mdi:check-circle"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        package_data = self._coordinator.packages.get(self._package_id, {})
        return {
            'last_update': package_data.get('last_update'),
            'installed_version': package_data.get('installed_version'),
        }
