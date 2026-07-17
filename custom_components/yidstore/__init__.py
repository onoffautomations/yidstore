from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    SERVICE_INSTALL,
    SERVICE_INSTALL_INTEGRATION,
    SERVICE_INSTALL_LOVELACE,
    SERVICE_INSTALL_BLUEPRINTS,
    SERVICE_CHECK_UPDATES,
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
    MANAGED_BY_HACS,
    async_github_latest_release,
    async_github_latest_tag,
    github_archive_url,
    load_hacs_state,
)

_LOGGER = logging.getLogger(__name__)

SERVICE_SCHEMA_GENERIC = vol.Schema(
    {
        vol.Optional("owner"): str,
        vol.Required("repo"): str,
        vol.Required("type"): vol.In([TYPE_INTEGRATION, TYPE_LOVELACE, TYPE_BLUEPRINTS, TYPE_AUDIO]),
        vol.Optional("mode", default=MODE_ASSET): vol.In([MODE_ASSET, MODE_ZIPBALL]),
        vol.Optional("tag"): str,
        vol.Optional("asset_name"): str,
        vol.Optional("source"): str,
        vol.Optional("repo_url"): str,
        vol.Optional("audio_location"): vol.In(["www", "media"]),
        vol.Optional("audio_files"): [str],
        vol.Optional("audio_subfolder"): str,
    }
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


async def _fetch_release_metadata(hass, client, owner: str, repo: str, source: str) -> dict | None:
    """Fetch raw latest-release metadata from the package's own host.

    GitHub packages hit GitHub; everything else uses the configured Gitea
    client — a GitHub package never queries the Gitea server. Returns the raw
    release dict (GitHub/Gitea share the same shape) or ``None`` on any failure.
    """
    try:
        if source == "github":
            result = await async_github_latest_release(hass, owner, repo)
            release = result.get("release") if result.get("status") == "ok" else None
            if not release:
                return None
            # Re-shape the normalized fields back into the raw keys the
            # coordinator's normalizer expects.
            return {
                "tag_name": release.get("release_tag"),
                "name": release.get("release_summary"),
                "body": release.get("release_notes"),
                "html_url": release.get("release_url"),
                "published_at": release.get("release_published_at"),
                "prerelease": release.get("prerelease"),
                "draft": release.get("draft"),
            }
        return await client.get_latest_release(owner, repo)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Could not fetch release metadata for %s/%s: %s", owner, repo, err)
        return None


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


async def _sync_preinstalled_integrations(
    hass: HomeAssistant,
    coordinator,
    entry: ConfigEntry,
) -> None:
    """Reconcile ownership/version for integrations found on disk.

    IMPORTANT: detection is not ownership. Finding a matching folder in
    custom_components/ (or a HACS catalog entry) does NOT mean YidStore
    installed or manages it. This routine therefore never fabricates a
    YidStore-managed package from a mere disk match.

    What it does do:

    * Reconcile the stored installed version of already-tracked packages
      against the on-disk manifest / HACS metadata (so YidStore doesn't keep
      showing a stale version — e.g. after HACS updates an integration).
    * If a package YidStore already tracks is now clearly owned by HACS, mark
      it HACS-managed so YidStore stops advertising its own update.

    New disk-only integrations are left untracked; the store UI still shows
    them as "Installed with HACS" / "Manually installed" via independent disk
    detection, and the user must explicitly install/adopt through YidStore
    before YidStore owns the update lifecycle.
    """
    installed = await hass.async_add_executor_job(_scan_custom_components_versions, hass)
    hacs_state = await hass.async_add_executor_job(load_hacs_state, hass.config.config_dir)
    hacs_domains = hacs_state.get("domains", set())
    hacs_versions = hacs_state.get("versions", {})

    if not coordinator.packages:
        return

    def _candidates(pkg: dict) -> list[str]:
        cands: list[str] = []
        for value in (pkg.get("domain"), pkg.get("repo_name")):
            if not value:
                continue
            slug = str(value).strip().lower()
            for cand in (slug, slug.replace("-", "_")):
                if cand and cand not in cands:
                    cands.append(cand)
        return cands

    changed = False
    for package_id, pkg in coordinator.packages.items():
        if pkg.get("package_type", TYPE_INTEGRATION) != TYPE_INTEGRATION:
            continue
        candidates = _candidates(pkg)
        match_domain = next((c for c in candidates if c in installed), None)
        in_hacs = any(c in hacs_domains for c in candidates)

        # Reclaim ownership for packages HACS now owns, but never downgrade a
        # package that YidStore itself installed (installed_by_yidstore proves
        # it) just because HACS also recognizes the same domain.
        if in_hacs and not pkg.get("installed_by_yidstore") and pkg.get("managed_by") != MANAGED_BY_HACS:
            _LOGGER.info(
                "Package %s is now managed by HACS; YidStore will not advertise its update",
                package_id,
            )
            pkg["managed_by"] = MANAGED_BY_HACS
            changed = True

        # Reconcile the displayed installed version from the real source.
        hacs_version = next((hacs_versions[c] for c in candidates if c in hacs_versions), None)
        disk_version = installed.get(match_domain) if match_domain else None
        if pkg.get("managed_by") == MANAGED_BY_HACS:
            new_version = hacs_version or disk_version
        else:
            new_version = disk_version or hacs_version
        if new_version and new_version not in ("unknown", "None") and new_version != pkg.get("installed_version"):
            _LOGGER.info(
                "Reconciling installed version for %s: %s -> %s",
                package_id, pkg.get("installed_version"), new_version,
            )
            pkg["installed_version"] = new_version
            changed = True

    if changed:
        await coordinator.async_save_packages()
        coordinator.async_update_listeners()


async def _dump_resources_state(hass: HomeAssistant) -> None:
    """Diagnostic: Dump current state of lovelace resources."""
    _LOGGER.info("")
    _LOGGER.info("=" * 80)
    _LOGGER.info("DIAGNOSTIC: Current Lovelace Resources State")
    _LOGGER.info("=" * 80)

    # Check storage file
    storage_path = Path(hass.config.path(".storage", "lovelace_resources"))
    _LOGGER.info("Storage file: %s", storage_path)
    _LOGGER.info("  Exists: %s", storage_path.exists())

    if storage_path.exists():
        _LOGGER.info("  Size: %d bytes", storage_path.stat().st_size)
        _LOGGER.info("  Modified: %s", storage_path.stat().st_mtime)

        # Load and show contents
        try:
            store = Store(hass, 1, "lovelace_resources")
            data = await store.async_load()

            if data:
                items = data.get("items", [])
                _LOGGER.info("  Resources in storage: %d", len(items))
                _LOGGER.info("")
                _LOGGER.info("  Resources list:")
                for idx, item in enumerate(items):
                    _LOGGER.info("    [%d] ID=%s, type=%s", idx, item.get("id", "?"), item.get("type", "?"))
                    _LOGGER.info("        URL: %s", item.get("url", "?"))
            else:
                _LOGGER.info("  Storage is empty or invalid")
        except Exception as e:
            _LOGGER.error("  Error reading storage: %s", e)

    # Check lovelace services
    _LOGGER.info("")
    _LOGGER.info("Lovelace services available:")
    lovelace_services = hass.services.async_services().get("lovelace", {})
    if lovelace_services:
        for service in lovelace_services.keys():
            _LOGGER.info("  - lovelace.%s", service)
    else:
        _LOGGER.info("  (none)")

    # Check lovelace component state
    _LOGGER.info("")
    _LOGGER.info("Lovelace component data:")
    lovelace_data = hass.data.get("lovelace")
    if lovelace_data:
        _LOGGER.info("  Type: %s", type(lovelace_data))
        _LOGGER.info("  Keys: %s", list(lovelace_data.keys()) if isinstance(lovelace_data, dict) else "N/A")
    else:
        _LOGGER.info("  (not loaded)")

    _LOGGER.info("=" * 80)
    _LOGGER.info("")


async def _register_or_update_lovelace_resource(hass: HomeAssistant, base_url: str, git_tag: str) -> None:
    """
    Register or update a Lovelace resource with time-update parameter.
    Uses direct storage access + proper Home Assistant service calls.

    Args:
        hass: Home Assistant instance
        base_url: Base resource URL (e.g., /local/community/onoff/search-card/search-card.js)
        git_tag: Git release tag (e.g., v1.0.0) - logged for reference only
    """
    timestamp = _get_datetime_timestamp()

    _LOGGER.info("")
    _LOGGER.info("=" * 80)
    _LOGGER.info("LOVELACE RESOURCE REGISTRATION")
    _LOGGER.info("=" * 80)
    _LOGGER.info("Base URL: %s", base_url)
    _LOGGER.info("Git Tag: %s", git_tag)
    _LOGGER.info("Timestamp: %s", timestamp)

    desired_url = _with_time_update(base_url)
    base_url_without_query = _strip_query(base_url)

    _LOGGER.info("Final URL: %s", desired_url)
    _LOGGER.info("=" * 80)
    _LOGGER.info("")

    # DIAGNOSTIC: Show state before registration
    await _dump_resources_state(hass)

    # STEP 1: Load storage file
    _LOGGER.info("STEP 1: Loading lovelace_resources storage...")
    store = Store(hass, 1, "lovelace_resources")
    storage_path = Path(hass.config.path(".storage", "lovelace_resources"))

    _LOGGER.info("  Storage path: %s", storage_path)
    _LOGGER.info("  File exists: %s", storage_path.exists())

    if storage_path.exists():
        _LOGGER.info("  File size: %d bytes", storage_path.stat().st_size)

    data = await store.async_load()

    if data is None:
        _LOGGER.info("  No existing storage found, creating new...")
        data = {"items": [], "version": 1}
    else:
        _LOGGER.info("  Loaded storage: version=%s, items=%d",
                    data.get("version", "?"), len(data.get("items", [])))

    # Ensure proper structure
    if "items" not in data:
        data["items"] = []
    if "version" not in data:
        data["version"] = 1

    items = data["items"]
    _LOGGER.info("✓ Storage loaded successfully (%d resources)", len(items))
    _LOGGER.info("")

    # STEP 2: Find or create resource
    _LOGGER.info("STEP 2: Finding existing resource...")
    found_index = None
    resource_id = None
    alternate_urls = _alternate_lovelace_resource_urls(base_url_without_query)
    stale_indices: list[int] = []

    for idx, item in enumerate(items):
        item_url = item.get("url", "")
        stripped_item_url = _strip_query(item_url)
        if stripped_item_url == base_url_without_query:
            found_index = idx
            resource_id = item.get("id")
            _LOGGER.info("✓ Found existing resource:")
            _LOGGER.info("    Index: %d", idx)
            _LOGGER.info("    ID: %s", resource_id)
            _LOGGER.info("    Current URL: %s", item_url)
            continue
        if stripped_item_url in alternate_urls:
            stale_indices.append(idx)

    if found_index is None:
        _LOGGER.info("  No existing resource found - will create new")
    for idx in reversed(stale_indices):
        stale = items.pop(idx)
        if found_index is not None and idx < found_index:
            found_index -= 1
        _LOGGER.info("  Removed stale duplicate resource: %s", stale.get("url", ""))
    _LOGGER.info("")

    # STEP 3: Update or add resource
    _LOGGER.info("STEP 3: Updating resource data...")

    if found_index is not None:
        # Update existing
        old_url = items[found_index].get("url")
        items[found_index]["type"] = "module"
        items[found_index]["url"] = desired_url

        _LOGGER.info("✓ UPDATED existing resource:")
        _LOGGER.info("    Old URL: %s", old_url)
        _LOGGER.info("    New URL: %s", desired_url)
    else:
        # Create new
        new_id = uuid.uuid4().hex
        new_resource = {
            "id": new_id,
            "type": "module",
            "url": desired_url
        }
        items.append(new_resource)

        _LOGGER.info("✓ CREATED new resource:")
        _LOGGER.info("    ID: %s", new_id)
        _LOGGER.info("    URL: %s", desired_url)
        _LOGGER.info("    Total resources: %d", len(items))
    _LOGGER.info("")

    # STEP 4: Save to storage
    _LOGGER.info("STEP 4: Saving to storage...")
    try:
        await store.async_save(data)
        _LOGGER.info("✓ Saved successfully")

        # Verify the save
        if storage_path.exists():
            _LOGGER.info("  File size after save: %d bytes", storage_path.stat().st_size)

        # Reload to verify
        verify_data = await store.async_load()
        if verify_data and "items" in verify_data:
            verify_count = len(verify_data["items"])
            _LOGGER.info("✓ Verified: Storage contains %d resources", verify_count)

            # Find our resource in verification
            found_in_verify = any(
                _strip_query(item.get("url", "")) == base_url_without_query
                for item in verify_data["items"]
            )
            if found_in_verify:
                _LOGGER.info("✓ Verified: Our resource exists in storage!")
            else:
                _LOGGER.error("✗ CRITICAL: Resource NOT found after save!")
                raise RuntimeError("Resource was not saved properly!")
        else:
            _LOGGER.error("✗ CRITICAL: Could not verify save!")
            raise RuntimeError("Could not verify storage save!")

    except Exception as e:
        _LOGGER.error("✗ CRITICAL: Failed to save storage: %s", e, exc_info=True)
        raise

    _LOGGER.info("")

    # STEP 5: Trigger Home Assistant reload
    _LOGGER.info("STEP 5: Triggering Home Assistant reload...")
    _LOGGER.info("")

    reload_success = False

    # Method 1: Fire bus events
    try:
        hass.bus.async_fire("lovelace_updated")
        hass.bus.async_fire("lovelace_resources_updated")
        _LOGGER.info("✓ Fired bus events (lovelace_updated, lovelace_resources_updated)")
    except Exception as e:
        _LOGGER.warning("⚠ Could not fire bus events: %s", e)

    # Method 2: Call reload_resources service
    try:
        if hass.services.has_service("lovelace", "reload_resources"):
            await hass.services.async_call(
                "lovelace",
                "reload_resources",
                {},
                blocking=True
            )
            _LOGGER.info("✓ Called lovelace.reload_resources service")
            reload_success = True
    except Exception:
        pass

    # Method 3: Direct collection reload
    try:
        from homeassistant.components import lovelace
        if hasattr(hass.data.get("lovelace"), "resources"):
            await hass.data["lovelace"].resources.async_load()
            _LOGGER.info("✓ Directly reloaded lovelace resources collection")
            reload_success = True
    except Exception as e:
        _LOGGER.debug("Could not directly reload collection: %s", e)

    _LOGGER.info("")

    if not reload_success:
        _LOGGER.error("=" * 80)
        _LOGGER.error("⚠⚠⚠ WARNING ⚠⚠⚠")
        _LOGGER.error("=" * 80)
        _LOGGER.error("Could not trigger automatic reload!")
        _LOGGER.error("YOU MUST RESTART HOME ASSISTANT for changes to take effect.")
        _LOGGER.error("=" * 80)
        _LOGGER.error("")

    # DIAGNOSTIC: Show state after registration
    await _dump_resources_state(hass)

    # STEP 6: Final summary
    _LOGGER.info("=" * 80)
    _LOGGER.info("✓✓✓ REGISTRATION COMPLETE ✓✓✓")
    _LOGGER.info("=" * 80)
    _LOGGER.info("")
    _LOGGER.info("Resource URL: %s", desired_url)
    _LOGGER.info("Storage file: %s", storage_path)
    _LOGGER.info("")
    _LOGGER.info("Next steps:")
    if reload_success:
        _LOGGER.info("  1. Check Settings → Dashboards → Resources (should appear immediately)")
        _LOGGER.info("  2. Hard refresh browser (Ctrl+F5 or Cmd+Shift+R)")
        _LOGGER.info("  3. Add card to your dashboard")
    else:
        _LOGGER.info("  1. RESTART HOME ASSISTANT (required!)")
        _LOGGER.info("  2. Check Settings → Dashboards → Resources")
        _LOGGER.info("  3. Hard refresh browser (Ctrl+F5 or Cmd+Shift+R)")
        _LOGGER.info("  4. Add card to your dashboard")

    _LOGGER.info("=" * 80)
    _LOGGER.info("")


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

    if not token:
        _LOGGER.info("No token configured - only public repositories will be accessible")

    # Import coordinator
    from .coordinator import OnOffGiteaStoreCoordinator

    # Create coordinator for package tracking
    coordinator = OnOffGiteaStoreCoordinator(hass, entry.entry_id, client)
    await coordinator.async_load_packages()

    # Build headers with optional auth
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"token {token}"

    hass.data[DOMAIN][entry.entry_id] = {
        "client": client,
        "default_owner": default_owner,
        "headers": headers,
        "coordinator": coordinator,
    }

    # Track already-installed custom_components integrations as if installed by the store
    await _sync_preinstalled_integrations(hass, coordinator, entry)

    # Setup Dashboard (Store UI) - catch errors so setup doesn't fail entirely
    try:
        await async_setup_dashboard(hass, entry)
    except Exception as e:
        _LOGGER.error("Failed to setup OnOff Store Dashboard: %s", e, exc_info=True)

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

                    # Determine service name
                    if pkg_type == "integration":
                        service_name = "install_integration"
                    elif pkg_type == "lovelace":
                        service_name = "install_lovelace"
                    elif pkg_type == "blueprints":
                        service_name = "install_blueprints"
                    elif pkg_type == "audio":
                        service_name = SERVICE_INSTALL
                    else:
                        _LOGGER.error("Unknown package type: %s", pkg_type)
                        continue

                    # Build service data
                    service_data = {
                        "owner": owner,
                        "repo": repo,
                    }
                    if service_name == SERVICE_INSTALL:
                        service_data["type"] = pkg_type
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
                        _LOGGER.info("✓ Installed: %s/%s", owner, repo)
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
                        _LOGGER.info("✓ Setup complete! Restart notification created.")
                    except Exception as e:
                        _LOGGER.warning("Could not create restart notification: %s", e)
                else:
                    _LOGGER.info("✓ Setup complete! Packages installed.")

                # Clear pending installs from entry data
                new_data = dict(entry.data)
                new_data.pop("pending_installs", None)
                hass.config_entries.async_update_entry(entry, data=new_data)
                _LOGGER.info("✓ Cleared pending installations from entry data")

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

    async def _download_url_for_call(owner: str, repo: str, mode: str, tag: str | None, asset_name: str | None, source: str | None) -> tuple[str, str]:
        if source == "github":
            # Never use api.github.com for downloads — unauthenticated REST
            # API calls are capped at 60/hour per IP, which is what caused
            # "Download failed: 403 rate limit". The archive endpoint
            # (codeload) and the /releases/latest redirect are not limited.
            ref = tag
            if not ref:
                ref = await async_github_latest_tag(hass, owner, repo)
            if not ref:
                # No releases — fall back to the default branch
                # (installer retries master if main doesn't exist).
                ref = "main"
            return github_archive_url(owner, repo, ref), ref

        # Intelligent "Zipball First" logic with silent Asset recovery
        
        # 1. Always try Zipball first as requested
        try:
            ref = await _resolve_ref_for_zipball(owner, repo, tag)
            return client.archive_zip_url(owner, repo, ref), ref
        except Exception as e:
            _LOGGER.debug("Zipball method failed for %s/%s, trying Release Asset: %s", owner, repo, e)

        # 2. Try Asset mode as fallback
        try:
            resolved_tag = await _resolve_tag_for_asset(owner, repo, tag)
            release = await client.get_release_by_tag(owner, repo, resolved_tag)
            asset = client.pick_asset(release, asset_name=asset_name)
            return asset["browser_download_url"], resolved_tag
        except Exception as final_err:
            _LOGGER.error("Both installation modes (Zipball & Asset) failed for %s/%s.", owner, repo)
            raise final_err

    async def _do_install(call: ServiceCall, package_type: str, default_mode: str) -> None:
        try:
            owner = await _resolve_owner(call)
            repo = call.data["repo"].strip()
            
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
            url, version = await _download_url_for_call(owner, repo, mode, tag, asset_name, source)
            _LOGGER.info("")
            _LOGGER.info("=" * 60)
            _LOGGER.info("Installing Package")
            _LOGGER.info("=" * 60)
            _LOGGER.info("Repository: %s/%s", owner, repo)
            _LOGGER.info("Type: %s", package_type)
            _LOGGER.info("Mode: %s", mode)
            _LOGGER.info("Git Tag/Ref: %s", version)
            # Don't log full URL to avoid exposing endpoint
            _LOGGER.info("Download: Ready")
            _LOGGER.info("=" * 60)
            _LOGGER.info("")

            # Get auth token for download - use client's token (not Accept: application/json header)
            download_headers = {}
            current_token = client.token  # Get from client instance
            if source == "github":
                download_headers["Accept"] = "application/vnd.github+json"
                download_headers["User-Agent"] = "YidStore"
            elif current_token:
                download_headers["Authorization"] = f"token {current_token}"
                _LOGGER.debug("Using authenticated download for %s/%s", owner, repo)
            else:
                _LOGGER.debug("Using anonymous download for %s/%s", owner, repo)

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

            # Create repair issue for integration installs (requires restart)
            if package_type == TYPE_INTEGRATION:
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
                    _LOGGER.info("✓ Created fixable repair issue for restart")
                except Exception as e:
                    _LOGGER.debug("Could not create repair issue (non-critical): %s", e)

            if package_type == TYPE_LOVELACE:
                base_resource_url = result.get("dest_url")
                _LOGGER.info("")
                _LOGGER.info("=" * 60)
                _LOGGER.info("Lovelace Card Installation Result")
                _LOGGER.info("=" * 60)
                _LOGGER.info("Result: %s", result)
                _LOGGER.info("Resource URL: %s", base_resource_url)
                _LOGGER.info("=" * 60)

                if not base_resource_url:
                    raise RuntimeError("Lovelace install succeeded but could not determine resource URL to register.")

                _LOGGER.info("")
                _LOGGER.info("=" * 60)
                _LOGGER.info("Starting Lovelace Resource Registration")
                _LOGGER.info("=" * 60)
                _LOGGER.info("URL to register: %s", base_resource_url)
                _LOGGER.info("Git tag/ref: %s", version)
                _LOGGER.info("=" * 60)

                try:
                    await _register_or_update_lovelace_resource(hass, base_resource_url, version)
                    _LOGGER.info("")
                    _LOGGER.info("✓✓✓ RESOURCE REGISTRATION SUCCESSFUL! ✓✓✓")
                    _LOGGER.info("")
                except Exception as reg_err:
                    _LOGGER.error("")
                    _LOGGER.error("=" * 60)
                    _LOGGER.error("✗✗✗ RESOURCE REGISTRATION FAILED! ✗✗✗")
                    _LOGGER.error("=" * 60)
                    _LOGGER.error("Error: %s", reg_err, exc_info=True)
                    _LOGGER.error("=" * 60)
                    _LOGGER.error("")
                    raise RuntimeError(f"Failed to register Lovelace resource: {reg_err}") from reg_err

            # Register package with coordinator for tracking and updates
            _LOGGER.info("")
            _LOGGER.info("=" * 60)
            _LOGGER.info("Registering Package for Tracking")
            _LOGGER.info("=" * 60)

            # Capture the release metadata for the installed version so its
            # release notes/summary/URL survive a restart via storage and show
            # in the update dialog immediately. Best-effort — install must not
            # fail if the release lookup does. Route to the correct host.
            release_meta = await _fetch_release_metadata(
                hass, client, owner, repo, source or "gitea"
            )

            repo_url = call.data.get("repo_url")

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
                managed_by="yidstore",
                installed_by_yidstore=True,
                repo_url=repo_url,
                release=release_meta,
            )

            _LOGGER.info("✓ Package registered with ID: %s", package_id)
            _LOGGER.info("✓ Device and sensors created automatically")
            _LOGGER.info("✓ Update checking enabled (every 2 hours)")
            _LOGGER.info("=" * 60)
            _LOGGER.info("")
            _LOGGER.info("Check your device at:")
            _LOGGER.info("  Settings → Devices & Services → OnOff Integration Store → %s", repo)
            _LOGGER.info("")

        except Exception as err:
            _LOGGER.exception("Install failed")
            raise HomeAssistantError(str(err)) from err

    async def _handle_install_generic(call: ServiceCall) -> None:
        package_type = call.data["type"]
        # Default to zipball for everything as requested
        default_mode = MODE_ZIPBALL
        await _do_install(call, package_type, default_mode=default_mode)

    async def _handle_check_updates(call: ServiceCall) -> None:
        """Handle check_updates service call."""
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        await coordinator.async_check_updates()

    hass.services.async_register(DOMAIN, SERVICE_INSTALL, _handle_install_generic, schema=SERVICE_SCHEMA_GENERIC)
    hass.services.async_register(DOMAIN, SERVICE_CHECK_UPDATES, _handle_check_updates)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Unload sensor, button, and update platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["sensor", "button", "update"])

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok
