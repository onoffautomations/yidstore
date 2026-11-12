from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers import config_validation as cv

from .const import (
    DOMAIN,
    SERVICE_INSTALL,
    SERVICE_INSTALL_INTEGRATION,
    SERVICE_INSTALL_LOVELACE,
    SERVICE_INSTALL_BLUEPRINTS,
    SERVICE_CHECK_UPDATES,
    SERVICE_INSTALL_APP,
    CONF_BETA_RELEASES,
    MODE_ASSET,
    MODE_ZIPBALL,
    TYPE_INTEGRATION,
    TYPE_LOVELACE,
    TYPE_BLUEPRINTS,
    TYPE_AUDIO,
)
from .gitea import GiteaClient
from .installer import download_and_install, uninstall_package
from .dashboard import async_setup_dashboard
from ._utils import (
    async_github_hacs_manifest,
    async_github_latest_tag,
    github_archive_url,
    github_release_asset_url,
)

_LOGGER = logging.getLogger(__name__)

# Set up from the UI only (no YAML).
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# Names that end up in download addresses and file paths.
_NAME = vol.All(str, vol.Match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"))


def _valid_tag(value):
    """A release tag or branch: no '..' segments, query or fragment."""
    value = str(value)
    if (
        not value or len(value) > 128 or not value[0].isalnum()
        or ".." in value.split("/") or any(c in value for c in "?#%\\ ")
    ):
        raise vol.Invalid("Invalid tag")
    return value


SERVICE_SCHEMA_GENERIC = vol.Schema(
    {
        vol.Optional("owner"): _NAME,
        vol.Required("repo"): _NAME,
        vol.Required("type"): vol.In([TYPE_INTEGRATION, TYPE_LOVELACE, TYPE_BLUEPRINTS, TYPE_AUDIO]),
        vol.Optional("mode", default=MODE_ASSET): vol.In([MODE_ASSET, MODE_ZIPBALL]),
        vol.Optional("tag"): _valid_tag,
        vol.Optional("asset_name"): str,
        vol.Optional("source"): str,
        vol.Optional("repo_url"): str,
        vol.Optional("audio_location"): vol.In(["www", "media"]),
        vol.Optional("audio_files"): [str],
        vol.Optional("audio_subfolder"): str,
    }
)


async def _require_admin(hass: HomeAssistant, call: ServiceCall) -> None:
    """Only admins may call YidStore's services. Calls without a user
    (automations, scripts, Home Assistant itself) are allowed, like Home
    Assistant's own admin services."""
    user_id = call.context.user_id if call.context else None
    if user_id is None:
        return
    user = await hass.auth.async_get_user(user_id)
    if user is None or not user.is_admin:
        from homeassistant.exceptions import Unauthorized

        raise Unauthorized(context=call.context)


def _require_app_ref(data: dict) -> dict:
    """install_app needs app_id, or both owner and repo."""
    if data.get("app_id") or (data.get("owner") and data.get("repo")):
        return data
    raise vol.Invalid("Give app_id, or both owner and repo")


SERVICE_SCHEMA_INSTALL_APP = vol.All(
    vol.Schema(
        {
            vol.Optional("app_id"): vol.All(str, vol.Match(r"^[a-f0-9]{16}$")),
            vol.Optional("owner"): _NAME,
            vol.Optional("repo"): _NAME,
            vol.Optional("update", default=False): bool,
        }
    ),
    _require_app_ref,
)

SERVICE_SCHEMA_SIMPLE = vol.Schema(
    {
        vol.Optional("owner"): str,
        vol.Required("repo"): str,
        vol.Optional("mode"): vol.In([MODE_ASSET, MODE_ZIPBALL]),
        vol.Optional("tag"): str,
        vol.Optional("asset_name"): str,
        vol.Optional("source"): str,
        vol.Optional("repo_url"): str,
        vol.Optional("audio_location"): vol.In(["www", "media"]),
        vol.Optional("audio_files"): [str],
        vol.Optional("audio_subfolder"): str,
    }
)


def _ha_version_tuple(version: str) -> tuple[int, ...]:
    """Leading integer parts of a Home Assistant version, e.g. "2026.11.1" -> (2026, 11, 1)."""
    import re
    return tuple(int(p) for p in re.findall(r"\d+", str(version or ""))[:3])


def _ha_version_at_least(version: str, target: tuple[int, ...]) -> bool:
    parts = _ha_version_tuple(version)
    return bool(parts) and parts >= target


# Home Assistant release that moved the community store to the built-in
# "Marketplace"; from here on YidStore may need re-adding as a custom repo.
MARKETPLACE_MIN_VERSION = (2026, 11, 1)


async def _maybe_notify_marketplace_migration(hass: HomeAssistant) -> None:
    """On HA 2026.11.1+, leave a persistent notification explaining that the
    integration may need to be re-added as a custom repository in the
    Marketplace (HACS), with the steps for doing so."""
    try:
        from homeassistant.const import __version__ as ha_version
    except Exception:
        return
    if not _ha_version_at_least(ha_version, MARKETPLACE_MIN_VERSION):
        return
    try:
        from homeassistant.components import persistent_notification

        persistent_notification.async_create(
            hass,
            title="YidStore: check the Marketplace after updating Home Assistant",
            message=(
                "You're on Home Assistant {ver}. After this update, YidStore "
                "sometimes needs to be re-added as a **custom repository** in "
                "the Marketplace (HACS).\n\n"
                "**If YidStore is missing, add it back:**\n"
                "1. Open **Marketplace** and tap the **add link** button next to the search bar.\n"
                "2. Paste `https://github.com/onoffautomations/yidstore` and tap **Add**.\n"
                "3. Install **YidStore**, then restart Home Assistant.\n\n"
                "Everything already working? You can safely dismiss this notice."
            ).format(ver=ha_version),
            notification_id="yidstore_marketplace_migration",
        )
    except Exception as exc:
        _LOGGER.debug("Could not create Marketplace migration notice: %s", exc)


def _domain_known_to_loader(hass: HomeAssistant, domain: str | None) -> bool:
    """Whether Home Assistant's loader already resolved this domain this run.

    If it has, the old code is already imported and a restart is the only way
    to pick up newly written files.
    """
    if not domain:
        return False
    try:
        from homeassistant.loader import async_get_loaded_integration

        async_get_loaded_integration(hass, domain)
        return True
    except Exception:
        return False


async def _async_refresh_custom_integrations(hass: HomeAssistant, domain: str | None) -> set[str]:
    """Rescan custom_components so a freshly installed integration is found
    without a restart (mirrors Home Assistant's Marketplace). Returns the set
    of custom-component domains the loader now sees, or an empty set when this
    Home Assistant version can't refresh the cache."""
    try:
        from homeassistant.loader import async_get_custom_components
    except Exception:
        return set()
    import sys

    try:
        try:
            from homeassistant.loader import (
                PACKAGE_CUSTOM_COMPONENTS,
                async_mount_config_dir,
            )

            # The loader mounts custom_components at startup; the very first
            # install creates that folder afterwards.
            if PACKAGE_CUSTOM_COMPONENTS not in sys.modules:
                async_mount_config_dir(hass)
        except Exception:
            pass
        try:
            from homeassistant.loader import async_clear_custom_components_cache

            async_clear_custom_components_cache(hass)
        except Exception:
            # Older Home Assistant: drop the cached mapping directly.
            try:
                hass.data.pop("custom_components", None)
            except Exception:
                pass
        if domain:
            try:
                from homeassistant.helpers.translation import async_invalidate_translations

                async_invalidate_translations(hass, {domain})
            except Exception:
                pass
        found = await async_get_custom_components(hass)
        return set(found or [])
    except Exception as exc:
        _LOGGER.debug("Could not refresh custom integrations: %s", exc)
        return set()


def _custom_integration_dirs(path: str) -> set[str]:
    """Folder names in custom_components (blocking; executor)."""
    try:
        return {p.name for p in Path(path).iterdir() if p.is_dir() and not p.name.startswith(".")}
    except OSError:
        return set()


async def _integration_pending_restart(
    hass: HomeAssistant, domain: str | None, was_on_disk: bool = False
) -> bool:
    """Whether an installed integration needs a restart.

    Only an update does: the integration was already installed (on disk) or
    its code is already running in this session, and Home Assistant only
    loads the new code after a restart. A new install (also a reinstall
    after YidStore removed it) is picked up right away: the loader rescans
    custom_components so it can be set up without a restart, like Home
    Assistant's Marketplace.
    """
    if not domain:
        return False
    was_loaded = _domain_known_to_loader(hass, domain)
    await _async_refresh_custom_integrations(hass, domain)
    return was_loaded or was_on_disk


def forget_loaded_integration(hass: HomeAssistant, domain: str | None) -> bool:
    """Drop a removed custom integration from Home Assistant's loader.

    Called after YidStore removed all of its config entries and its files,
    so a later install of the same integration loads its new code instead of
    the copy still in memory (which would otherwise need a restart).
    Returns True when nothing of it is left loaded.
    """
    import sys

    if not domain or hass.config_entries.async_entries(domain):
        return False
    try:
        cache = hass.data.get("integrations")
        if isinstance(cache, dict):
            cache.pop(domain, None)
        prefix = f"custom_components.{domain}"
        components = hass.data.get("components")
        for name in list(sys.modules):
            if name == prefix or name.startswith(prefix + "."):
                sys.modules.pop(name, None)
                if isinstance(components, dict):
                    components.pop(name, None)
        if domain in hass.config.components:
            hass.config.components.remove(domain)
        hass.data.pop("custom_components", None)
        return True
    except Exception as exc:  # unusual Home Assistant internals: restart instead
        _LOGGER.debug("Could not unload %s from the loader: %s", domain, exc)
        return False


def _get_datetime_timestamp() -> str:
    """
    Get current date and time as a timestamp string.
    Format: YYYYMMDDHHmmss (date and time with seconds)
    Example: 20260118143045 (January 18, 2026, 14:30:45)
    """
    import datetime
    now = datetime.datetime.now()
    return now.strftime("%Y%m%d%H%M%S")


def _with_time_update(url: str) -> str:
    """
    Add time-update query parameter to URL for cache busting.
    Format: ?time-update=20260118143045 (YYYYMMDDHHmmss)
    """
    parsed = urlparse(url)
    q = parse_qs(parsed.query)
    timestamp = _get_datetime_timestamp()
    q["time-update"] = [timestamp]
    new_query = urlencode({k: v[0] for k, v in q.items()})
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))


def _strip_query(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, "", p.fragment))


def _alternate_lovelace_resource_urls(base_url_without_query: str) -> set[str]:
    """Return old/new equivalent resource paths for onoff-vendored cards."""
    p = urlparse(base_url_without_query)
    parts = [part for part in p.path.split("/") if part]
    if len(parts) < 3:
        return set()

    alternates: set[str] = set()
    equivalent_paths: list[list[str]] = []

    if parts[0] == "hacsfiles" and len(parts) >= 3:
        equivalent_paths.append(["", "local", "community", *parts[1:]])
        equivalent_paths.append(["", "local", "community", "onoff", *parts[1:]])
    elif len(parts) >= 4 and parts[0] == "local" and parts[1] == "community":
        if parts[2] == "onoff" and len(parts) >= 5:
            equivalent_paths.append(["", "local", "community", *parts[3:]])
            equivalent_paths.append(["", "hacsfiles", *parts[3:]])
        else:
            equivalent_paths.append(["", "local", "community", "onoff", *parts[2:]])
            equivalent_paths.append(["", "hacsfiles", *parts[2:]])

    for alt_parts in equivalent_paths:
        alt_path = "/".join(alt_parts)
        alternates.add(urlunparse((p.scheme, p.netloc, alt_path, p.params, "", p.fragment)))
    return alternates


def _scan_custom_components_versions(hass: HomeAssistant) -> dict[str, str]:
    """Return installed custom_components domains mapped to their manifest versions."""
    cc_root = Path(hass.config.path("custom_components"))
    versions: dict[str, str] = {}
    if not cc_root.exists():
        return versions

    for domain_dir in cc_root.iterdir():
        if not domain_dir.is_dir():
            continue
        domain = domain_dir.name
        if domain.startswith("."):
            continue
        version = "unknown"
        manifest_path = domain_dir / "manifest.json"
        if manifest_path.exists():
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                version = data.get("version") or version
                manifest_domain = (data.get("domain") or "").strip()
                if manifest_domain and manifest_domain.lower() != domain.lower():
                    versions.setdefault(manifest_domain.lower(), version)
            except Exception as e:
                _LOGGER.debug("Failed to read manifest for %s: %s", domain, e)
        versions[domain.lower()] = version

    return versions


def _load_hacs_integrations(hass: HomeAssistant) -> set[str]:
    """Return integration domains installed by HACS or the HA Marketplace."""
    from ._utils import community_store_installed

    try:
        domains, _ = community_store_installed(hass.config.config_dir)
    except Exception as e:
        _LOGGER.debug("Failed to read HACS/Marketplace storage: %s", e)
        return set()
    return domains


async def _sync_preinstalled_integrations(
    hass: HomeAssistant,
    coordinator,
    entry: ConfigEntry,
) -> None:
    """Track custom_components integrations as installed when they already exist on disk."""
    installed = await hass.async_add_executor_job(_scan_custom_components_versions, hass)
    if not installed:
        return

    hacs_domains = await hass.async_add_executor_job(_load_hacs_integrations, hass)
    from .config_flow import load_store_list

    default_owner: str | None = (entry.data.get("owner") or "").strip() or None
    store_packages = await hass.async_add_executor_job(load_store_list, hass)

    to_track: list[tuple[str, str, str, str | None, str | None, str]] = []

    def _match_installed_domain(repo: str, pkg_domain: str | None = None) -> str | None:
        candidates = []
        if pkg_domain:
            candidates.append(pkg_domain)
        candidates.append(repo)
        candidates.append(repo.replace("-", "_"))
        for cand in candidates:
            key = (cand or "").strip().lower()
            if key and key in installed:
                return key
        return None

    for pkg in store_packages:
        repo = (pkg.get("repo") or "").strip()
        if not repo:
            continue
        if pkg.get("type", TYPE_INTEGRATION) != TYPE_INTEGRATION:
            continue
        match_domain = _match_installed_domain(repo, pkg.get("domain"))
        if not match_domain:
            continue
        owner = (pkg.get("owner") or default_owner or "").strip()
        if not owner:
            continue
        if coordinator.get_package_by_repo(owner, repo) is not None:
            continue
        source = "hacs" if match_domain in hacs_domains else pkg.get("source", "gitea")
        to_track.append(
            (
                owner,
                repo,
                installed.get(match_domain, "unknown"),
                pkg.get("mode"),
                pkg.get("asset_name"),
                source,
                match_domain,
            )
        )

    for cr in coordinator.get_custom_repos():
        repo = (cr.get("repo") or "").strip()
        if not repo:
            continue
        repo_type = cr.get("type") or TYPE_INTEGRATION
        if repo_type != TYPE_INTEGRATION:
            continue
        match_domain = _match_installed_domain(repo, cr.get("domain"))
        if not match_domain:
            continue
        owner = (cr.get("owner") or default_owner or "").strip()
        if not owner:
            continue
        if coordinator.get_package_by_repo(owner, repo) is not None:
            continue
        source = "hacs" if match_domain in hacs_domains else cr.get("source", "gitea")
        to_track.append(
            (
                owner,
                repo,
                installed.get(match_domain, "unknown"),
                None,
                None,
                source,
                match_domain,
            )
        )

    if not to_track:
        return

    _LOGGER.info("Tracking %d pre-installed custom_components integrations", len(to_track))
    for owner, repo, version, mode, asset_name, source, domain in to_track:
        await coordinator.async_add_or_update_package(
            repo_name=repo,
            owner=owner,
            package_type=TYPE_INTEGRATION,
            installed_version=version,
            mode=mode,
            asset_name=asset_name,
            source=source,
            domain=domain,
            managed_by_yidstore=False,
        )


def _lovelace_resources(hass: HomeAssistant):
    """Home Assistant's dashboard-resources collection, or None."""
    data = hass.data.get("lovelace")
    resources = getattr(data, "resources", None)
    if resources is None and isinstance(data, dict):
        resources = data.get("resources")
    return resources


async def _register_or_update_lovelace_resource(hass: HomeAssistant, base_url: str, git_tag: str) -> None:
    """Add or update a card's dashboard resource through Home Assistant's
    official resources collection (the same API Settings -> Dashboards ->
    Resources uses), with a cache-busting ?time-update= parameter.

    Falls back to writing the resources storage only when the collection
    isn't available (e.g. dashboards not loaded yet during startup)."""
    desired_url = _with_time_update(base_url)
    base = _strip_query(base_url)
    alternates = _alternate_lovelace_resource_urls(base)
    resources = _lovelace_resources(hass)

    if resources is None:
        _LOGGER.info("Dashboard resources not loaded yet; writing the resource to storage")
        await _register_resource_in_storage(hass, base_url, git_tag)
        return
    if not hasattr(resources, "async_create_item"):
        # Resources are managed in YAML: Home Assistant can't add them.
        _LOGGER.warning(
            "Dashboard resources are managed in YAML. Add this resource yourself: "
            "url: %s, type: module", base,
        )
        return

    if not getattr(resources, "loaded", True):
        await resources.async_load()
        resources.loaded = True

    found = None
    stale = []
    for item in list(resources.async_items()):
        url = _strip_query(item.get("url", ""))
        if url == base and found is None:
            found = item
        elif url == base or url in alternates:
            stale.append(item)
    for item in stale:
        try:
            await resources.async_delete_item(item["id"])
            _LOGGER.info("Removed duplicate dashboard resource %s", item.get("url"))
        except Exception as err:
            _LOGGER.debug("Could not remove duplicate resource %s: %s", item.get("url"), err)

    if found:
        await resources.async_update_item(found["id"], {"res_type": "module", "url": desired_url})
        _LOGGER.info("Updated dashboard resource %s (%s)", desired_url, git_tag)
    else:
        await resources.async_create_item({"res_type": "module", "url": desired_url})
        _LOGGER.info("Added dashboard resource %s (%s)", desired_url, git_tag)


async def _register_resource_in_storage(hass: HomeAssistant, base_url: str, git_tag: str) -> None:
    """Fallback when the resources collection is not available: add or
    update the card's resource in the lovelace_resources store directly."""
    desired_url = _with_time_update(base_url)
    base_url_without_query = _strip_query(base_url)
    alternate_urls = _alternate_lovelace_resource_urls(base_url_without_query)

    store = Store(hass, 1, "lovelace_resources")
    data = await store.async_load() or {}
    items = data.setdefault("items", [])
    data.setdefault("version", 1)

    found = None
    kept = []
    for item in items:
        stripped = _strip_query(item.get("url", ""))
        if stripped == base_url_without_query and found is None:
            found = item
        elif stripped in alternate_urls or stripped == base_url_without_query:
            _LOGGER.debug("Removed duplicate card resource %s", item.get("url", ""))
            continue
        kept.append(item)
    data["items"] = kept

    if found is not None:
        found["type"] = "module"
        found["url"] = desired_url
    else:
        kept.append({"id": uuid.uuid4().hex, "type": "module", "url": desired_url})

    await store.async_save(data)
    _LOGGER.debug("Card resource %s saved (release %s)", desired_url, git_tag)

    reloaded = False
    try:
        hass.bus.async_fire("lovelace_updated")
        hass.bus.async_fire("lovelace_resources_updated")
    except Exception as exc:
        _LOGGER.debug("Could not fire lovelace events: %s", exc)
    try:
        if hass.services.has_service("lovelace", "reload_resources"):
            await hass.services.async_call("lovelace", "reload_resources", {}, blocking=True)
            reloaded = True
    except Exception as exc:
        _LOGGER.debug("lovelace.reload_resources failed: %s", exc)
    try:
        resources = getattr(hass.data.get("lovelace"), "resources", None)
        if resources is not None and hasattr(resources, "async_load"):
            await resources.async_load()
            reloaded = True
    except Exception as exc:
        _LOGGER.debug("Could not reload the resources collection: %s", exc)

    if not reloaded:
        _LOGGER.warning(
            "Added the card resource %s, but Home Assistant could not reload its "
            "resources. Restart Home Assistant to load the card.",
            desired_url,
        )


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    base_url: str = entry.data["base_url"].rstrip("/")
    token: str = (entry.data.get("token") or "").strip() or None
    default_owner: str = (entry.data.get("owner") or "").strip() or None

    # Store HA start time (for tracking restart requirements)
    if 'homeassistant_start_time' not in hass.data:
        from datetime import datetime
        hass.data['homeassistant_start_time'] = datetime.now()
        _LOGGER.info("Recorded HA start time: %s", hass.data['homeassistant_start_time'])

    client = GiteaClient(hass, base_url=base_url, token=token)
    client.include_prereleases = bool(entry.data.get(CONF_BETA_RELEASES, False))

    # Repo keys, the token's repository list and "access stopped" repairs.
    from .access import AccessManager

    access = AccessManager(hass, client)
    await access.async_load()

    if not token:
        _LOGGER.info("No token configured - only public repositories will be accessible")

    # Import coordinator
    from .coordinator import OnOffGiteaStoreCoordinator

    # Create coordinator for package tracking
    coordinator = OnOffGiteaStoreCoordinator(hass, entry.entry_id, client)
    await coordinator.async_load_packages()

    hass.data[DOMAIN][entry.entry_id] = {
        "client": client,
        "default_owner": default_owner,
        "coordinator": coordinator,
        "access": access,
    }

    # Track already-installed custom_components integrations as if installed by the store
    await _sync_preinstalled_integrations(hass, coordinator, entry)

    # Setup Dashboard (Store UI) - catch errors so setup doesn't fail entirely
    try:
        await async_setup_dashboard(hass, entry)
    except Exception as e:
        _LOGGER.error("Failed to setup OnOff Store Dashboard: %s", e, exc_info=True)

    # On HA 2026.11.1+, remind the user they may need to re-add YidStore as a
    # custom repository in the Marketplace (HACS).
    await _maybe_notify_marketplace_migration(hass)

    # Check for updates on startup — run in the background so the network
    # round-trips don't block integration setup / HA boot.
    entry.async_create_background_task(
        hass, coordinator.async_check_updates(), "yidstore_startup_update_check"
    )

    # Load sensor, button, and update platforms
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor", "button", "update"])

    # Handle pending installations from initial setup
    pending_installs = entry.data.get("pending_installs", [])
    if pending_installs:
        _LOGGER.info("Found %d pending packages to install from setup", len(pending_installs))

        # Schedule installation as a background task
        async def _install_pending_packages():
            """Install packages that were selected during setup."""
            try:
                # Import here to avoid circular dependency
                from .config_flow import load_store_list

                packages = await hass.async_add_executor_job(load_store_list, hass)
                installed_integration = False

                # The install service is registered later in this setup.
                for _ in range(30):
                    if hass.services.has_service(DOMAIN, SERVICE_INSTALL):
                        break
                    await asyncio.sleep(1)

                for key in pending_installs:
                    # Find package by key
                    pkg = None
                    for p in packages:
                        pkg_key = f"{p.get('owner', '')}_{p.get('repo', '')}"
                        if pkg_key == key:
                            pkg = p
                            break

                    if not pkg:
                        _LOGGER.error("Package not found for key: %s", key)
                        continue

                    repo = pkg.get("repo")
                    owner = pkg.get("owner", default_owner)
                    pkg_type = pkg.get("type", "integration")
                    mode = pkg.get("mode")
                    asset_name = pkg.get("asset_name")

                    if not repo or not owner:
                        _LOGGER.error("Invalid package data: %s", pkg)
                        continue

                    # Track if integration was installed
                    if pkg_type == "integration":
                        installed_integration = True

                    _LOGGER.info("Installing package from setup: %s/%s (type: %s)", owner, repo, pkg_type)

                    if pkg_type not in ("integration", "lovelace", "blueprints", "audio"):
                        _LOGGER.error("Unknown package type: %s", pkg_type)
                        continue

                    # Only the generic "install" service is registered.
                    service_name = SERVICE_INSTALL
                    service_data = {
                        "owner": owner,
                        "repo": repo,
                        "type": pkg_type,
                    }
                    if mode:
                        service_data["mode"] = mode
                    if asset_name:
                        service_data["asset_name"] = asset_name

                    # Call installation service
                    try:
                        await hass.services.async_call(
                            DOMAIN,
                            service_name,
                            service_data,
                            blocking=True
                        )
                        _LOGGER.info("Installed: %s/%s", owner, repo)
                    except Exception as e:
                        _LOGGER.error("Failed to install %s/%s: %s", owner, repo, e, exc_info=True)

                # Show restart notification if integration was installed
                if installed_integration:
                    try:
                        from homeassistant.helpers import issue_registry as ir
                        issue_registry = ir.async_get(hass)
                        issue_registry.async_create_issue(
                            domain=DOMAIN,
                            issue_id="setup_restart_required",
                            is_fixable=False,
                            severity=ir.IssueSeverity.WARNING,
                            translation_key="setup_restart_required",
                        )
                        _LOGGER.info("Setup complete! Restart notification created.")
                    except Exception as e:
                        _LOGGER.warning("Could not create restart notification: %s", e)
                else:
                    _LOGGER.info("Setup complete! Packages installed.")

                # Clear pending installs from entry data
                new_data = dict(entry.data)
                new_data.pop("pending_installs", None)
                hass.config_entries.async_update_entry(entry, data=new_data)
                _LOGGER.info("Cleared pending installations from entry data")

            except Exception as e:
                _LOGGER.error("Failed to install pending packages: %s", e, exc_info=True)

        # Schedule the installation task
        hass.async_create_task(_install_pending_packages())

    async def _resolve_owner(call: ServiceCall) -> str:
        owner = (call.data.get("owner") or "").strip()
        if owner:
            return owner
        if call.data.get("source") == "github":
            repo_url = (call.data.get("repo_url") or "").strip()
            if repo_url:
                try:
                    cleaned = repo_url.replace("https://", "").replace("http://", "")
                    if cleaned.endswith(".git"):
                        cleaned = cleaned[:-4]
                    parts = cleaned.split("/")
                    if len(parts) >= 3 and parts[0].lower() == "github.com":
                        return parts[1]
                except Exception:
                    pass
        if default_owner:
            return default_owner
        raise ValueError("Missing owner. Set it in integration config or pass owner in service call.")

    async def _resolve_ref_for_zipball(owner: str, repo: str, tag: str | None) -> str:
        if tag:
            _LOGGER.info("Using provided tag: %s", tag)
            return tag

        latest = await client.get_latest_release(owner, repo)
        if latest:
            ref = latest.get("tag_name") or latest.get("name")
            if ref:
                _LOGGER.info("Using latest release tag: %s", ref)
                return ref

        repo_info = await client.get_repo(owner, repo)
        branch = repo_info.get("default_branch") or "main"
        _LOGGER.warning("No release tag found, falling back to branch: %s", branch)
        return branch

    async def _resolve_tag_for_asset(owner: str, repo: str, tag: str | None) -> str:
        if tag:
            _LOGGER.info("Using provided tag: %s", tag)
            return tag
        latest = await client.get_latest_release(owner, repo)
        if not latest:
            raise RuntimeError("No releases found. For mode=asset you must create a Release with a ZIP asset.")
        resolved = latest.get("tag_name") or latest.get("name")
        if not resolved:
            raise RuntimeError("Could not determine latest release tag_name from Gitea.")
        _LOGGER.info("Using latest release tag: %s", resolved)
        return resolved

    async def _download_url_for_call(
        owner: str,
        repo: str,
        mode: str,
        tag: str | None,
        asset_name: str | None,
        source: str | None,
        package_type: str,
    ) -> tuple[str, str, str, str | None]:
        if source == "github":
            # Never use api.github.com for downloads — unauthenticated REST
            # API calls are capped at 60/hour per IP. Resolve the latest tag via
            # github.com redirect, then read hacs.json from raw.githubusercontent.
            ref = tag
            if not ref:
                ref = await async_github_latest_tag(
                    hass, owner, repo, include_prereleases=client.beta_enabled
                )

            # HACS zip_release is supported only for integrations. If hacs.json
            # opts in, download the named GitHub Release asset exactly like HACS
            # instead of GitHub's auto-generated source archive.
            if package_type == TYPE_INTEGRATION and ref:
                hacs_manifest = await async_github_hacs_manifest(hass, owner, repo, ref)
                if isinstance(hacs_manifest, dict) and hacs_manifest.get("zip_release") is True:
                    filename = hacs_manifest.get("filename")
                    if not isinstance(filename, str) or not filename.strip():
                        raise RuntimeError(
                            "hacs.json has zip_release=true but does not define a release asset filename."
                        )
                    filename = filename.strip()
                    if (
                        "/" in filename
                        or "\\" in filename
                        or filename in {".", ".."}
                        or not filename.lower().endswith(".zip")
                    ):
                        raise RuntimeError(
                            "hacs.json zip_release filename must be a .zip release asset filename."
                        )

                    _LOGGER.info(
                        "GitHub HACS zip_release detected for %s/%s: %s",
                        owner,
                        repo,
                        filename,
                    )
                    return (
                        github_release_asset_url(owner, repo, ref, filename),
                        ref,
                        MODE_ASSET,
                        filename,
                    )

            if not ref:
                # No releases — fall back to the default branch
                # (installer retries master if main doesn't exist).
                ref = "main"
            return github_archive_url(owner, repo, ref), ref, MODE_ZIPBALL, None

        # Intelligent "Zipball First" logic with silent Asset recovery
        
        # 1. Always try Zipball first as requested
        try:
            ref = await _resolve_ref_for_zipball(owner, repo, tag)
            return client.archive_zip_url(owner, repo, ref), ref, MODE_ZIPBALL, None
        except Exception as e:
            _LOGGER.debug("Zipball method failed for %s/%s, trying Release Asset: %s", owner, repo, e)

        # 2. Try Asset mode as fallback
        try:
            resolved_tag = await _resolve_tag_for_asset(owner, repo, tag)
            release = await client.get_release_by_tag(owner, repo, resolved_tag)
            asset = client.pick_asset(release, asset_name=asset_name)
            return asset["browser_download_url"], resolved_tag, MODE_ASSET, asset.get("name") or asset_name
        except Exception as final_err:
            _LOGGER.error("Both installation modes (Zipball & Asset) failed for %s/%s.", owner, repo)
            raise final_err

    async def _do_install(call: ServiceCall, package_type: str, default_mode: str) -> None:
        try:
            owner = await _resolve_owner(call)
            repo = call.data["repo"].strip()
            from .installer import valid_name

            if not valid_name(owner) or not valid_name(repo):
                raise ValueError(f"Invalid repository name: {owner}/{repo}")
            
            # Check coordinator for existing package data to ensure update consistency
            coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
            existing_pkg = coordinator.get_package_by_repo(owner, repo)
            
            
            # DEEP FIX: If not tracked, also check store_list.yaml for pre-configured mode
            yaml_pkg = None
            if not existing_pkg:
                from .config_flow import load_store_list
                yaml_items = await hass.async_add_executor_job(load_store_list, hass)
                yaml_pkg = next((y for y in yaml_items if y.get("owner") == owner and y.get("repo") == repo), None)

            # Priority: 1. Service Call Args, 2. Existing Package Data, 3. YAML config, 4. Defaults
            mode = call.data.get("mode")
            if not mode:
                if existing_pkg:
                    mode = existing_pkg.get("mode")
                elif yaml_pkg:
                    mode = yaml_pkg.get("mode")
            if not mode:
                mode = default_mode
                
            asset_name = call.data.get("asset_name")
            if not asset_name:
                if existing_pkg:
                    asset_name = existing_pkg.get("asset_name")
                elif yaml_pkg:
                    asset_name = yaml_pkg.get("asset_name")
            
            tag = call.data.get("tag")

            source = call.data.get("source")
            audio_location = (call.data.get("audio_location") or "www").strip().lower()
            audio_files = call.data.get("audio_files")
            audio_subfolder = call.data.get("audio_subfolder")
            url, version, effective_mode, effective_asset_name = await _download_url_for_call(
                owner, repo, mode, tag, asset_name, source, package_type
            )
            mode = effective_mode
            asset_name = effective_asset_name
            _LOGGER.info("Installing %s/%s (%s, %s) at %s", owner, repo, package_type, mode, version)

            # A credential only for a store repository that needs one (its
            # repo key, or the main token if it can see the repository).
            # Never for GitHub; installer.py also strips it for other hosts.
            download_headers = {}
            if source == "github":
                download_headers["Accept"] = "application/octet-stream"
                download_headers["User-Agent"] = "YidStore"
            else:
                download_headers.update(client.auth_headers(owner, repo))

            # Integrations already on disk before this install: installing
            # over one of them is an update, and an update needs a restart.
            existing_integrations: set[str] = set()
            if package_type == TYPE_INTEGRATION:
                existing_integrations = await hass.async_add_executor_job(
                    _custom_integration_dirs, hass.config.path("custom_components")
                )

            result = await download_and_install(
                hass,
                url=url,
                headers=download_headers,
                package_type=package_type,
                repo_name=repo,
                owner=owner,
                audio_location=audio_location,
                audio_files=audio_files,
                audio_subfolder=audio_subfolder,
                source=source,
            )

            installed_domain = None
            if package_type == TYPE_INTEGRATION:
                domains = result.get("domains") or []
                if domains:
                    installed_domain = domains[0]

            # Like Home Assistant's Marketplace: a brand-new config-flow
            # integration is loaded without a restart. A restart is only
            # needed when the integration was already running this session
            # (an update/reinstall, where the old code stays loaded) or when
            # it is YAML-only.
            pending_restart = True
            if package_type == TYPE_INTEGRATION:
                domain_for_restart = installed_domain or repo.lower().replace("-", "_")
                try:
                    pending_restart = await _integration_pending_restart(
                        hass, domain_for_restart, domain_for_restart in existing_integrations
                    )
                except Exception as e:
                    _LOGGER.debug("Restart-need check failed, assuming restart needed: %s", e)
                    pending_restart = True

                if pending_restart:
                    _LOGGER.info("Creating repair issue for integration restart requirement")
                    try:
                        from homeassistant.helpers import issue_registry as ir
                        # Create a fixable repair issue with restart button.
                        # issue_domain makes HA show the installed integration's
                        # own brand icon on the repair instead of YidStore's.
                        ir.async_create_issue(
                            hass,
                            domain=DOMAIN,
                            issue_id=f"onoff_restart_{repo}_{_get_datetime_timestamp()}",
                            is_fixable=True,
                            severity=ir.IssueSeverity.WARNING,
                            translation_key="integration_restart_required",
                            translation_placeholders={"integration_name": repo},
                            data={"integration_name": repo},
                            issue_domain=installed_domain or repo.lower().replace("-", "_"),
                        )
                        _LOGGER.info("Created fixable repair issue for restart")
                    except Exception as e:
                        _LOGGER.debug("Could not create repair issue (non-critical): %s", e)
                else:
                    _LOGGER.info("%s loaded without needing a restart", repo)

            if package_type == TYPE_LOVELACE:
                base_resource_url = result.get("dest_url")
                _LOGGER.debug("Card installed, resource URL: %s", base_resource_url)

                if not base_resource_url:
                    raise RuntimeError("Lovelace install succeeded but could not determine resource URL to register.")

                try:
                    await _register_or_update_lovelace_resource(hass, base_resource_url, version)
                except Exception as reg_err:
                    _LOGGER.error("Could not add the card resource %s: %s", base_resource_url, reg_err, exc_info=True)
                    raise RuntimeError(f"Failed to register Lovelace resource: {reg_err}") from reg_err

            # Register package with coordinator for tracking and updates

            coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
            package_id = await coordinator.async_add_or_update_package(
                repo_name=repo,
                owner=owner,
                package_type=package_type,
                installed_version=version,
                mode=mode,
                asset_name=asset_name,
                source=source or "gitea",
                domain=installed_domain,
                # Only integrations can need a restart, and only when the new
                # code could not be loaded on the fly (see above).
                requires_restart=(pending_restart if package_type == TYPE_INTEGRATION else False),
            )

            _LOGGER.info("Package registered with ID: %s", package_id)
            _LOGGER.info("Device and sensors created automatically")
            _LOGGER.info("Update checking enabled (every 2 hours)")
            _LOGGER.info("Check your device at:")
            _LOGGER.info("  Settings → Devices & Services → OnOff Integration Store → %s", repo)

        except Exception as err:
            _LOGGER.exception("Install failed")
            raise HomeAssistantError(str(err)) from err

    async def _handle_install_generic(call: ServiceCall) -> None:
        await _require_admin(hass, call)
        package_type = call.data["type"]
        # Default to zipball for everything as requested
        default_mode = MODE_ZIPBALL
        await _do_install(call, package_type, default_mode=default_mode)

    async def _handle_check_updates(call: ServiceCall) -> None:
        await _require_admin(hass, call)
        """Handle check_updates service call."""
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        await coordinator.async_check_updates()

    hass.services.async_register(DOMAIN, SERVICE_INSTALL, _handle_install_generic, schema=SERVICE_SCHEMA_GENERIC)
    hass.services.async_register(DOMAIN, SERVICE_CHECK_UPDATES, _handle_check_updates)

    async def _handle_install_app(call: ServiceCall):
        await _require_admin(hass, call)
        """Install or update an app (add-on); returns when it's done."""
        from .apps import async_install_app_service

        result = await async_install_app_service(hass, entry.entry_id, dict(call.data))
        return result if call.return_response else None

    hass.services.async_register(
        DOMAIN,
        SERVICE_INSTALL_APP,
        _handle_install_app,
        schema=SERVICE_SCHEMA_INSTALL_APP,
        supports_response=SupportsResponse.OPTIONAL,
    )

    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry
) -> bool:
    """Uninstall a YidStore-managed package when its HA device is deleted.

    Home Assistant calls this hook from the device page's Delete action.
    Packages merely discovered as pre-existing/HACS installs are protected and
    cannot be uninstalled by deleting their YidStore tracking device.
    """
    package_id = next(
        (value for domain, value in device_entry.identifiers if domain == DOMAIN),
        None,
    )
    if not package_id:
        return False

    entry_data = hass.data.get(DOMAIN, {}).get(config_entry.entry_id, {})
    coordinator = entry_data.get("coordinator")
    if coordinator is None:
        return False

    package = coordinator.packages.get(package_id)
    if package is None:
        # Nothing remains to uninstall; allow HA to clean up the stale device.
        return True

    if not package.get("managed_by_yidstore", True):
        _LOGGER.warning(
            "Refusing device-delete uninstall for unmanaged package %s", package_id
        )
        return False

    owner = package.get("owner")
    repo = package.get("repo_name")
    package_type = package.get("package_type", TYPE_INTEGRATION)
    domain = package.get("domain")
    if not owner or not repo:
        return False

    try:
        await hass.async_add_executor_job(
            uninstall_package, hass, package_type, repo, owner, domain
        )
        # HA itself removes the device after this hook returns True, so don't
        # race it by deleting the device registry entry here.
        await coordinator.async_remove_package(owner, repo, remove_device=False)

        if "yidstore_requires_restart" in hass.data:
            hass.data["yidstore_requires_restart"].discard(f"{owner}/{repo}".lower())

        try:
            from .dashboard import _invalidate_local_state_cache, _patch_repos_cache
            _patch_repos_cache(
                owner, repo,
                is_installed=False,
                install_source=None,
                update_available=False,
            )
            _invalidate_local_state_cache()
        except Exception:
            pass

        _LOGGER.info("Uninstalled %s/%s from device delete", owner, repo)
        return True
    except Exception as err:
        _LOGGER.error(
            "Failed to uninstall %s/%s from device delete: %s",
            owner, repo, err, exc_info=True,
        )
        return False


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Unload sensor, button, and update platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["sensor", "button", "update"])

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok
