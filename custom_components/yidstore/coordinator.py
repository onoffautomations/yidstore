"""Coordinator for OnOff Integration Store package tracking."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DOMAIN,
    STORAGE_KEY_PACKAGES,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


class OnOffGiteaStoreCoordinator(DataUpdateCoordinator):
    """Coordinator to manage package tracking and updates."""

    def __init__(self, hass: HomeAssistant, entry_id: str, client) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
        )
        self.hass = hass
        self.entry_id = entry_id
        self.client = client
        self.packages: dict[str, dict[str, Any]] = {}
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY_PACKAGES)
        self._custom_store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.custom_repos")
        self._hidden_store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.hidden_repos")
        self._add_entities_callback = None  # Will be set by sensor platform
        self._add_button_entities_callback = None  # Will be set by button platform
        self._add_update_entities_callback = None  # Will be set by update platform
        self._created_entities: set[str] = set()
        self.custom_repos: list[dict[str, str]] = []
        self.hidden_repos: list[dict[str, str]] = []
        # Don't override _listeners - parent class handles it

    async def async_load_packages(self) -> None:
        """Load tracked packages from storage."""
        _LOGGER.info("Loading tracked packages...")
        data = await self._store.async_load()

        if data:
            self.packages = data.get("packages", {})
            _LOGGER.info("Loaded %d tracked packages", len(self.packages))
        else:
            self.packages = {}
            _LOGGER.info("No tracked packages found")

        # Load custom hidden repos
        custom_data = await self._custom_store.async_load()
        self.custom_repos = custom_data.get("repos", []) if custom_data else []
        _LOGGER.info("Loaded %d custom repositories", len(self.custom_repos))

        # Load manually hidden repos
        hidden_data = await self._hidden_store.async_load()
        self.hidden_repos = hidden_data.get("repos", []) if hidden_data else []
        _LOGGER.info("Loaded %d hidden repositories", len(self.hidden_repos))

    async def async_save_packages(self) -> None:
        """Save tracked packages to storage."""
        _LOGGER.info("Saving %d tracked packages...", len(self.packages))
        await self._store.async_save({"packages": self.packages})
        _LOGGER.info("✓ Packages saved")

    async def async_add_or_update_package(
        self,
        repo_name: str,
        owner: str,
        package_type: str,
        installed_version: str,
        mode: str = None,
        asset_name: str = None,
        source: str = "gitea",
    ) -> str:
        """Add or update a tracked package."""
        package_id = f"{owner}_{repo_name}".lower().replace("-", "_")

        is_new_package = package_id not in self.packages

        _LOGGER.info("Adding/updating package: %s (new: %s)", package_id, is_new_package)

        # Get existing data to preserve some fields
        existing_data = self.packages.get(package_id, {})

        package_data = {
            "repo_name": repo_name,
            "owner": owner,
            "package_type": package_type,
            "installed_version": installed_version,
            "latest_version": installed_version,  # When installing, latest = installed
            "update_available": False,  # Just installed, so no update available
            "install_date": existing_data.get("install_date", datetime.now().isoformat()),
            "last_update": datetime.now().isoformat(),
            "last_check": existing_data.get("last_check"),  # Preserve last check time
            "mode": mode,
            "asset_name": asset_name,
            "source": source or existing_data.get("source", "gitea"),
        }

        _LOGGER.info("Package data for %s: installed=%s, latest=%s, update_available=%s",
                    package_id, installed_version, installed_version, False)

        self.packages[package_id] = package_data
        await self.async_save_packages()

        _LOGGER.info("✓ Package %s tracked", package_id)

        # If this is a new package and we have the callback, create sensors immediately
        if is_new_package and self._add_entities_callback and package_id not in self._created_entities:
            _LOGGER.info("Creating sensors for new package: %s", package_id)
            await self._create_sensors_for_package(package_id, package_data)
        else:
            # If updating existing package, notify sensors to refresh
            _LOGGER.info("Notifying sensors to update for: %s", package_id)
            self.async_update_listeners()

            # Update device registry with new version
            from homeassistant.helpers import device_registry as dr
            device_registry = dr.async_get(self.hass)
            device = device_registry.async_get_device(identifiers={(DOMAIN, package_id)})
            if device:
                device_registry.async_update_device(
                    device.id,
                    sw_version=installed_version
                )
                _LOGGER.info("✓ Updated device registry sw_version to %s", installed_version)

        return package_id

    async def _create_sensors_for_package(self, package_id: str, package_data: dict) -> None:
        """Create sensors and button for a package dynamically."""
        # Create sensors
        if self._add_entities_callback:
            try:
                # Import here to avoid circular import
                from .sensor import (
                    PackageVersionSensor,
                    PackageUpdateSensor,
                    PackageTypeSensor,
                    WaitingRestartSensor,
                )

                new_sensors = [
                    PackageVersionSensor(self, package_id, package_data, self.entry_id),
                    PackageUpdateSensor(self, package_id, package_data, self.entry_id),
                    PackageTypeSensor(self, package_id, package_data, self.entry_id),
                ]

                # Add restart sensor only for integrations
                if package_data.get('package_type') == 'integration':
                    new_sensors.append(WaitingRestartSensor(self, package_id, package_data, self.hass, self.entry_id))

                self._add_entities_callback(new_sensors)
                _LOGGER.info("✓ Created %d sensors for %s", len(new_sensors), package_data["repo_name"])

            except Exception as e:
                _LOGGER.error("Failed to create sensors for %s: %s", package_id, e, exc_info=True)
        else:
            _LOGGER.warning("Cannot create sensors - no callback registered")

        # Create button
        if self._add_button_entities_callback:
            try:
                # Import here to avoid circular import
                from .button import PackageUpdateButton, PackageCheckUpdateButton

                # Get entry_id from hass data
                entry_id = self.entry_id
                entry = None
                for config_entry_id, data in self.hass.data.get(DOMAIN, {}).items():
                    if data.get("coordinator") == self:
                        entry = self.hass.config_entries.async_get_entry(config_entry_id)
                        break

                if entry:
                    new_button = [
                        PackageUpdateButton(self, package_id, package_data, entry),
                        PackageCheckUpdateButton(self, package_id, package_data, entry)
                    ]
                    self._add_button_entities_callback(new_button)
                    self._created_entities.add(package_id)
                    _LOGGER.info("✓ Created dynamic entities for %s", package_data["repo_name"])
                else:
                    _LOGGER.warning("Could not find config entry for button creation")

            except Exception as e:
                _LOGGER.error("Failed to create button for %s: %s", package_id, e, exc_info=True)
        else:
            _LOGGER.debug("Button callback not registered yet")

        # Create update entity
        if self._add_update_entities_callback:
            try:
                from .update import PackageUpdateEntity

                entry = None
                for config_entry_id, data in self.hass.data.get(DOMAIN, {}).items():
                    if data.get("coordinator") == self:
                        entry = self.hass.config_entries.async_get_entry(config_entry_id)
                        break

                if entry:
                    new_update = [PackageUpdateEntity(self, package_id, package_data, entry)]
                    self._add_update_entities_callback(new_update)
                    _LOGGER.info("✓ Created update entity for %s", package_data["repo_name"])
            except Exception as e:
                _LOGGER.error("Failed to create update entity for %s: %s", package_id, e, exc_info=True)
        else:
            _LOGGER.debug("Update entity callback not registered yet")

    async def async_check_updates(self, now=None) -> None:
        """Check for updates for all tracked packages."""
        if not self.packages:
            _LOGGER.info("No packages tracked yet, skipping update check")
            return

        _LOGGER.info("Checking for updates for %d packages...", len(self.packages))

        for package_id, package_data in self.packages.items():
            try:
                if package_data.get("source", "gitea") == "github":
                    _LOGGER.debug("Skipping update check for GitHub repo: %s", package_id)
                    continue

                owner = package_data["owner"]
                repo = package_data["repo_name"]
                installed_version = package_data["installed_version"]

                _LOGGER.debug("Checking %s/%s (installed: %s)", owner, repo, installed_version)

                # Get latest release
                latest_release = await self.client.get_latest_release(owner, repo)

                if latest_release:
                    latest_version = latest_release.get("tag_name", "unknown")
                    _LOGGER.debug("Latest version: %s", latest_version)

                    # Check if update available
                    update_available = latest_version != installed_version

                    # Update package data
                    package_data["latest_version"] = latest_version
                    package_data["update_available"] = update_available
                    package_data["last_check"] = datetime.now().isoformat()
                    package_data["release_summary"] = latest_release.get("name")
                    package_data["release_notes"] = latest_release.get("body")

                    if update_available:
                        _LOGGER.info("✓ Update available for %s: %s → %s",
                                   repo, installed_version, latest_version)
                    else:
                        _LOGGER.debug("No update available for %s", repo)
                else:
                    # No release found
                    _LOGGER.debug("No releases found for %s/%s", owner, repo)
                    package_data["last_check"] = datetime.now().isoformat()

            except Exception as e:
                error_str = str(e)
                # Check if it's a 404 (repo doesn't exist or no access) or 401 (auth failed)
                if "404" in error_str or "not found" in error_str.lower():
                    _LOGGER.debug("Repo %s/%s not found on Gitea server. This might be a private repo that requires a token.", owner, repo)
                elif "401" in error_str or "unauthorized" in error_str.lower():
                    _LOGGER.debug("Auth failed for %s/%s. Token might be invalid or expired, or repo requires different permissions.", owner, repo)
                else:
                    _LOGGER.warning("Error checking updates for %s: %s", package_id, e)
                # Mark as checked even on error to avoid repeated errors
                package_data["last_check"] = datetime.now().isoformat()

        # Save updated data
        await self.async_save_packages()

        # Notify sensors to update
        self.async_update_listeners()

        _LOGGER.info("✓ Update check complete")

    async def async_get_package_info(self, package_id: str) -> dict[str, Any] | None:
        """Get package information by ID."""
        return self.packages.get(package_id)

    def get_package_by_repo(self, owner: str, repo_name: str) -> dict[str, Any] | None:
        """Get package information by owner and repo name."""
        package_id = f"{owner}_{repo_name}".lower().replace("-", "_")
        return self.packages.get(package_id)

    async def async_add_custom_repo(self, owner: str, repo: str, source: str = "gitea", repo_type: str | None = None, repo_url: str | None = None) -> None:
        """Add a custom repo to the visible list."""
        if not any(r.get("owner") == owner and r.get("repo") == repo for r in self.custom_repos):
            entry = {"owner": owner, "repo": repo, "source": source}
            if repo_type:
                entry["type"] = repo_type
            if repo_url:
                entry["url"] = repo_url
            self.custom_repos.append(entry)
            await self._custom_store.async_save({"repos": self.custom_repos})
            _LOGGER.info("Added custom repo: %s/%s (source=%s)", owner, repo, source)

    def is_custom_repo(self, owner: str, repo: str) -> bool:
        """Check if a repo is in the custom list."""
        return any(r.get("owner", "").lower() == owner.lower() and r.get("repo", "").lower() == repo.lower() for r in self.custom_repos)

    async def async_remove_custom_repo(self, owner: str, repo: str) -> None:
        """Remove a custom repo from the list."""
        self.custom_repos = [r for r in self.custom_repos if not (r["owner"].lower() == owner.lower() and r["repo"].lower() == repo.lower())]
        await self._custom_store.async_save({"repos": self.custom_repos})
        _LOGGER.info("Removed custom repo: %s/%s", owner, repo)

    def get_custom_repos(self) -> list[dict[str, str]]:
        """Get list of custom repos."""
        return self.custom_repos.copy()

    async def async_hide_repo(self, owner: str, repo: str) -> None:
        """Hide a repository from view."""
        if not any(r["owner"] == owner and r["repo"] == repo for r in self.hidden_repos):
            self.hidden_repos.append({"owner": owner, "repo": repo})
            await self._hidden_store.async_save({"repos": self.hidden_repos})
            _LOGGER.info("Hid repo: %s/%s", owner, repo)

    async def async_unhide_repo(self, owner: str, repo: str) -> None:
        """Unhide a repository."""
        self.hidden_repos = [r for r in self.hidden_repos if not (r["owner"] == owner and r["repo"] == repo)]
        await self._hidden_store.async_save({"repos": self.hidden_repos})
        _LOGGER.info("Unhid repo: %s/%s", owner, repo)

    def is_hidden_repo(self, owner: str, repo: str) -> bool:
        """Check if a repo is manually hidden."""
        return any(r["owner"].lower() == owner.lower() and r["repo"].lower() == repo.lower() for r in self.hidden_repos)

    async def async_remove_package(self, owner: str, repo_name: str) -> None:
        """Remove a tracked package from storage."""
        package_id = f"{owner}_{repo_name}".lower().replace("-", "_")
        if package_id in self.packages:
            _LOGGER.info("Removing tracking for package: %s", package_id)
            self.packages.pop(package_id)
            await self.async_save_packages()
            self.async_update_listeners()
