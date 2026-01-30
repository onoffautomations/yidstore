from __future__ import annotations

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
)
from .gitea import GiteaClient
from .installer import download_and_install, uninstall_package
from .dashboard import async_setup_dashboard

_LOGGER = logging.getLogger(__name__)

SERVICE_SCHEMA_GENERIC = vol.Schema(
    {
        vol.Optional("owner"): str,
        vol.Required("repo"): str,
        vol.Required("type"): vol.In([TYPE_INTEGRATION, TYPE_LOVELACE, TYPE_BLUEPRINTS]),
        vol.Optional("mode", default=MODE_ASSET): vol.In([MODE_ASSET, MODE_ZIPBALL]),
        vol.Optional("tag"): str,
        vol.Optional("asset_name"): str,
    }
)

SERVICE_SCHEMA_SIMPLE = vol.Schema(
    {
        vol.Optional("owner"): str,
        vol.Required("repo"): str,
        vol.Optional("mode"): vol.In([MODE_ASSET, MODE_ZIPBALL]),
        vol.Optional("tag"): str,
        vol.Optional("asset_name"): str,
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

    for idx, item in enumerate(items):
        item_url = item.get("url", "")
        if _strip_query(item_url) == base_url_without_query:
            found_index = idx
            resource_id = item.get("id")
            _LOGGER.info("✓ Found existing resource:")
            _LOGGER.info("    Index: %d", idx)
            _LOGGER.info("    ID: %s", resource_id)
            _LOGGER.info("    Current URL: %s", item_url)
            break

    if found_index is None:
        _LOGGER.info("  No existing resource found - will create new")
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

    # Send persistent notification
    try:
        card_name = base_url.split('/')[-1]
        if reload_success:
            message = (
                f"✓ **{card_name}** installed successfully!\n\n"
                f"**Resource is now available!**\n"
                f"Go to Settings → Dashboards → Resources to verify.\n\n"
                f"**Device created** with update tracking sensors.\n\n"
                f"Hard refresh your browser (Ctrl+F5) to load the card."
            )
        else:
            message = (
                f"⚠ **{card_name}** saved to storage!\n\n"
                f"**RESTART HOME ASSISTANT REQUIRED**\n"
                f"The resource was saved but needs a restart to load.\n\n"
                f"After restart, hard refresh your browser (Ctrl+F5)."
            )

        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "YidStore - Card Installed",
                "message": message,
                "notification_id": f"onoff_gitea_store_{timestamp}"
            },
            blocking=False
        )
        _LOGGER.info("✓ Sent user notification")
    except Exception as e:
        _LOGGER.debug("Could not send notification: %s", e)


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

    # Setup Dashboard (Store UI) - catch errors so setup doesn't fail entirely
    try:
        await async_setup_dashboard(hass, entry)
    except Exception as e:
        _LOGGER.error("Failed to setup OnOff Store Dashboard: %s", e, exc_info=True)

    # Check for updates on startup
    await coordinator.async_check_updates()

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
                    else:
                        _LOGGER.error("Unknown package type: %s", pkg_type)
                        continue

                    # Build service data
                    service_data = {
                        "owner": owner,
                        "repo": repo,
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

    async def _download_url_for_call(owner: str, repo: str, mode: str, tag: str | None, asset_name: str | None) -> tuple[str, str]:
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

            url, version = await _download_url_for_call(owner, repo, mode, tag, asset_name)
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
            if current_token:
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
            )

            # Create repair issue for integration installs (requires restart)
            if package_type == TYPE_INTEGRATION:
                _LOGGER.info("Creating repair issue for integration restart requirement")
                try:
                    from homeassistant.helpers import issue_registry as ir
                    # Create a fixable repair issue with restart button
                    ir.async_create_issue(
                        hass,
                        domain=DOMAIN,
                        issue_id=f"onoff_restart_{repo}_{_get_datetime_timestamp()}",
                        is_fixable=True,
                        severity=ir.IssueSeverity.WARNING,
                        translation_key="integration_restart_required",
                        translation_placeholders={"integration_name": repo},
                        data={"integration_name": repo},
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

            coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
            package_id = await coordinator.async_add_or_update_package(
                repo_name=repo,
                owner=owner,
                package_type=package_type,
                installed_version=version,
                mode=mode,
                asset_name=asset_name,
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
