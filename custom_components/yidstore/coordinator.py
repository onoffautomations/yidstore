"""Coordinator for OnOff Integration Store package tracking."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DOMAIN,
    STORAGE_KEY_PACKAGES,
    STORAGE_VERSION,
)
from ._utils import (
    MANAGED_BY_HACS,
    MANAGED_BY_MANUAL,
    MANAGED_BY_YIDSTORE,
    SOURCE_GITHUB,
    async_github_latest_release,
    is_comparable_version,
    load_hacs_state,
    normalize_release,
    normalize_version,
    version_is_newer,
)

_LOGGER = logging.getLogger(__name__)

# Backwards-compatible aliases (kept so any external references keep working).
_norm_version = normalize_version
_is_version_comparable = is_comparable_version


def _read_manifest_version(hass: HomeAssistant, domain: str | None) -> str | None:
    """Read the on-disk custom_components/<domain>/manifest.json version.

    The manifest reflects the code actually installed right now — it's the
    source of truth for "what version is on disk", regardless of which
    installer put it there. Returns ``None`` when the folder/manifest is
    absent or unreadable.
    """
    if not domain:
        return None
    import json

    manifest = Path(hass.config.path("custom_components", domain, "manifest.json"))
    try:
        if not manifest.is_file():
            return None
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Failed to read manifest for %s: %s", domain, err)
        return None
    version = data.get("version")
    return str(version) if version else None


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

    async def _async_load_hacs_state(self) -> dict:
        """Read HACS storage off the event loop."""
        try:
            return await self.hass.async_add_executor_job(
                load_hacs_state, self.hass.config.config_dir
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not read HACS state: %s", err)
            return {"domains": set(), "repos": set(), "versions": {}}

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

        # Migrate legacy records that predate the ownership model.
        await self._async_migrate_packages()

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

    async def _async_migrate_packages(self) -> None:
        """Add ownership metadata to legacy package records in place.

        Existing installs have package records without ``managed_by`` /
        ``installed_by_yidstore``. We never blindly assume every old record is
        YidStore-managed; instead we use the strongest available evidence:

        * ``source == "hacs"`` or the domain is currently owned by HACS ->
          treat as HACS-managed (non-actionable YidStore update).
        * otherwise the record is a package YidStore was already tracking and
          updating (it was created by an install/track flow and is not owned by
          HACS) -> keep it YidStore-managed so existing users keep getting
          updates. This preserves backward compatibility without losing data.

        No package data is removed. Tokens and private URLs are never logged.
        """
        if not self.packages:
            return
        if all("managed_by" in pkg for pkg in self.packages.values()):
            return  # Already migrated.

        hacs_state = await self._async_load_hacs_state()
        hacs_domains = hacs_state.get("domains", set())

        migrated = 0
        for package_id, pkg in self.packages.items():
            if "managed_by" in pkg:
                continue
            domain = (pkg.get("domain") or "").strip().lower().replace("-", "_")
            source = pkg.get("source")
            if source == MANAGED_BY_HACS or (domain and domain in hacs_domains):
                pkg["managed_by"] = MANAGED_BY_HACS
                pkg["installed_by_yidstore"] = False
                decision = "hacs"
            else:
                pkg["managed_by"] = MANAGED_BY_YIDSTORE
                pkg["installed_by_yidstore"] = True
                pkg.setdefault("installation_id", uuid.uuid4().hex)
                decision = "yidstore"
            pkg["schema_migrated"] = True
            migrated += 1
            _LOGGER.debug(
                "Migrated package %s -> managed_by=%s (source=%s)",
                package_id, decision, source or "unknown",
            )

        if migrated:
            _LOGGER.info("Migrated %d legacy package record(s) to ownership model", migrated)
            await self.async_save_packages()

    @staticmethod
    def is_yidstore_managed(pkg: dict[str, Any] | None) -> bool:
        """Whether a package record is owned/managed by YidStore.

        Ownership is durable: it comes from stored install metadata, never from
        merely finding a matching folder in custom_components or a HACS catalog
        entry. Legacy records without the field fall back to the old behavior
        (treated as YidStore-managed) so existing installs keep working until
        migration stamps them.
        """
        if not pkg:
            return False
        if "managed_by" in pkg:
            return pkg.get("managed_by") == MANAGED_BY_YIDSTORE
        return bool(pkg.get("installed_by_yidstore", True))

    async def async_add_or_update_package(
        self,
        repo_name: str,
        owner: str,
        package_type: str,
        installed_version: str,
        mode: str = None,
        asset_name: str = None,
        source: str = "gitea",
        domain: str | None = None,
        managed_by: str | None = None,
        installed_by_yidstore: bool | None = None,
        installation_id: str | None = None,
        repo_url: str | None = None,
        release: dict | None = None,
    ) -> str:
        """Add or update a tracked package.

        ``managed_by`` / ``installed_by_yidstore`` / ``installation_id`` capture
        durable ownership. When YidStore performs the install it passes
        ``managed_by="yidstore"``; ownership already proven on an existing record
        is never silently downgraded (e.g. a later HACS catalog match cannot
        take a YidStore-owned package away).
        """
        package_id = f"{owner}_{repo_name}".lower().replace("-", "_")

        is_new_package = package_id not in self.packages

        _LOGGER.info("Adding/updating package: %s (new: %s)", package_id, is_new_package)

        # Get existing data to preserve some fields
        existing_data = self.packages.get(package_id, {})

        # Resolve durable ownership. A proven YidStore install stays
        # YidStore-managed unless the caller explicitly changes it.
        if managed_by is None:
            managed_by = existing_data.get("managed_by")
            if installed_by_yidstore:
                managed_by = MANAGED_BY_YIDSTORE
            elif managed_by is None:
                managed_by = MANAGED_BY_YIDSTORE if installed_by_yidstore is not False else MANAGED_BY_MANUAL
        if installed_by_yidstore is None:
            installed_by_yidstore = existing_data.get(
                "installed_by_yidstore", managed_by == MANAGED_BY_YIDSTORE
            )
        if managed_by == MANAGED_BY_YIDSTORE:
            installed_by_yidstore = True
            installation_id = (
                installation_id
                or existing_data.get("installation_id")
                or uuid.uuid4().hex
            )
        else:
            installation_id = installation_id or existing_data.get("installation_id")

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
            "mode": mode if mode is not None else existing_data.get("mode"),
            "asset_name": asset_name if asset_name is not None else existing_data.get("asset_name"),
            "source": source or existing_data.get("source", "gitea"),
            "domain": domain or existing_data.get("domain"),
            "managed_by": managed_by,
            "installed_by_yidstore": bool(installed_by_yidstore),
            "installation_id": installation_id,
            "repo_url": repo_url or existing_data.get("repo_url"),
        }

        # Capture release metadata at install time so the exact release notes
        # for the installed version survive a Home Assistant restart via
        # storage. Fields are shared between GitHub and Gitea.
        normalized_release = normalize_release(release) if release else None
        if normalized_release:
            package_data["release_summary"] = normalized_release["release_summary"]
            package_data["release_notes"] = normalized_release["release_notes"]
            package_data["release_url"] = normalized_release["release_url"]
            package_data["release_published_at"] = normalized_release["release_published_at"]
        else:
            # Preserve any previously stored release info.
            for key in ("release_summary", "release_notes", "release_url", "release_published_at"):
                if existing_data.get(key) is not None:
                    package_data[key] = existing_data.get(key)

        _LOGGER.info("Package data for %s: installed=%s, latest=%s, managed_by=%s",
                    package_id, installed_version, installed_version, managed_by)

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

        # Create update entity — only for YidStore-managed packages so we never
        # advertise a competing update for HACS/manual installs.
        if self._add_update_entities_callback and self.is_yidstore_managed(package_data):
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

    @staticmethod
    def _domain_candidates(pkg: dict[str, Any]) -> list[str]:
        """Possible on-disk folder names for a package's integration."""
        cands: list[str] = []
        for value in (pkg.get("domain"), pkg.get("repo_name")):
            if not value:
                continue
            slug = str(value).strip().lower()
            for cand in (slug, slug.replace("-", "_")):
                if cand and cand not in cands:
                    cands.append(cand)
        return cands

    def _reconcile_installed_version(
        self, pkg: dict[str, Any], disk_versions: dict, hacs_versions: dict
    ) -> bool:
        """Re-read the real installed version and update the record in place.

        The on-disk manifest is the code that's actually running; HACS metadata
        is authoritative for HACS-managed integrations. This keeps YidStore from
        showing a stale version (or a phantom update) after the integration was
        updated by another installer. Returns True when the version changed.

        Importantly this only synchronizes the *version*, never ownership —
        finding the folder on disk does not make YidStore the owner.
        """
        if pkg.get("package_type", "integration") != "integration":
            return False

        candidates = self._domain_candidates(pkg)
        disk_version = next((disk_versions[c] for c in candidates if c in disk_versions), None)
        hacs_version = next((hacs_versions[c] for c in candidates if c in hacs_versions), None)

        if pkg.get("managed_by") == MANAGED_BY_HACS:
            new_version = hacs_version or disk_version
        else:
            new_version = disk_version or hacs_version

        if not new_version or new_version in ("unknown", "None"):
            return False
        if normalize_version(new_version) == normalize_version(pkg.get("installed_version")):
            if new_version != pkg.get("installed_version"):
                pkg["installed_version"] = new_version  # keep exact string in sync
            return False

        _LOGGER.info(
            "Reconciled installed version for %s: %s -> %s",
            pkg.get("repo_name"), pkg.get("installed_version"), new_version,
        )
        pkg["installed_version"] = new_version
        return True

    @staticmethod
    def _apply_release(pkg: dict[str, Any], release: dict, now: str) -> None:
        """Store normalized release metadata and recompute update availability."""
        latest = release["release_tag"]
        pkg["latest_version"] = latest
        pkg["release_summary"] = release["release_summary"]
        pkg["release_notes"] = release["release_notes"]
        pkg["release_url"] = release["release_url"]
        pkg["release_published_at"] = release["release_published_at"]
        pkg["last_check"] = now
        pkg["update_available"] = version_is_newer(latest, pkg.get("installed_version"))
        if pkg["update_available"]:
            _LOGGER.info(
                "✓ Update available for %s: %s → %s",
                pkg.get("repo_name"), pkg.get("installed_version"), latest,
            )

    async def async_check_updates(self, now=None) -> None:
        """Check for updates for all tracked packages."""
        if not self.packages:
            _LOGGER.info("No packages tracked yet, skipping update check")
            return

        _LOGGER.info("Checking for updates for %d packages...", len(self.packages))

        # One filesystem/HACS read for the whole sweep (off the event loop).
        from ._utils import scan_custom_components_versions

        disk_versions = await self.hass.async_add_executor_job(
            scan_custom_components_versions, self.hass.config.config_dir
        )
        hacs_state = await self._async_load_hacs_state()
        hacs_versions = hacs_state.get("versions", {})

        for package_id, package_data in self.packages.items():
            timestamp = datetime.now().isoformat()
            try:
                # 1. Always reconcile the real installed version first so the
                #    displayed state (and any update decision) is accurate.
                self._reconcile_installed_version(package_data, disk_versions, hacs_versions)

                # 2. Only YidStore-managed packages get an actionable update.
                #    HACS/manual packages: sync version, never advertise an
                #    update (HACS/the user own their lifecycle).
                if not self.is_yidstore_managed(package_data):
                    package_data["update_available"] = False
                    package_data["latest_version"] = package_data.get("installed_version")
                    package_data["last_check"] = timestamp
                    _LOGGER.debug(
                        "Package %s is %s-managed; skipping actionable update check",
                        package_id, package_data.get("managed_by", "non-yidstore"),
                    )
                    continue

                owner = package_data["owner"]
                repo = package_data["repo_name"]
                installed_version = package_data.get("installed_version")
                source = package_data.get("source", "gitea")
                allow_prerelease = bool(package_data.get("allow_prerelease"))

                if source == SOURCE_GITHUB:
                    result = await async_github_latest_release(
                        self.hass, owner, repo, allow_prerelease=allow_prerelease
                    )
                    status = result.get("status")
                    if status == "ok" and result.get("release"):
                        self._apply_release(package_data, result["release"], timestamp)
                    elif status == "none":
                        # No stable release exists (empty repo or prerelease-only).
                        package_data["update_available"] = False
                        package_data["latest_version"] = installed_version
                        package_data["last_check"] = timestamp
                        _LOGGER.debug("No stable GitHub release for %s/%s", owner, repo)
                    else:
                        # rate_limited / error: reuse cached release info, never
                        # invent a false update.
                        package_data["last_check"] = timestamp
                        _LOGGER.debug(
                            "GitHub release lookup %s for %s/%s; keeping cached state",
                            status, owner, repo,
                        )
                    continue

                # Gitea (default): fetch the latest release via the configured
                # Gitea client and store the same shared fields as GitHub.
                _LOGGER.debug("Checking %s/%s (installed: %s)", owner, repo, installed_version)
                latest_release = await self.client.get_latest_release(owner, repo)
                normalized = normalize_release(latest_release)
                if normalized and not normalized["draft"]:
                    if normalized["prerelease"] and not allow_prerelease:
                        # Gitea's latest endpoint normally excludes prereleases;
                        # if one slips through, don't treat it as a stable update.
                        package_data["update_available"] = False
                        package_data["latest_version"] = installed_version
                        package_data["last_check"] = timestamp
                    else:
                        self._apply_release(package_data, normalized, timestamp)
                else:
                    _LOGGER.debug("No usable release found for %s/%s", owner, repo)
                    package_data["update_available"] = False
                    package_data["last_check"] = timestamp

            except Exception as e:
                error_str = str(e)
                owner = package_data.get("owner")
                repo = package_data.get("repo_name")
                if "404" in error_str or "not found" in error_str.lower():
                    _LOGGER.debug("Repo %s/%s not found on Gitea server. This might be a private repo that requires a token.", owner, repo)
                elif "401" in error_str or "unauthorized" in error_str.lower():
                    _LOGGER.debug("Auth failed for %s/%s. Token might be invalid or expired, or repo requires different permissions.", owner, repo)
                else:
                    _LOGGER.warning("Error checking updates for %s: %s", package_id, e)
                # Mark as checked even on error. Crucially we do NOT clear the
                # existing package record or flip update_available — an
                # unavailable repo must not corrupt stored data or create a
                # false update.
                package_data["last_check"] = timestamp

        # Save updated data
        await self.async_save_packages()

        # Notify sensors/update entities to refresh immediately.
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
