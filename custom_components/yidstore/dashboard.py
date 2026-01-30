"""Dashboard for YidStore - V4 Robust."""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from aiohttp import web

from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.components import frontend

from .const import DOMAIN, CONF_SIDE_PANEL, SERVICE_INSTALL
from .config_flow import load_store_list
from .installer import uninstall_package


def _parse_github_url(url: str) -> tuple[str, str] | None:
    try:
        cleaned = url.strip()
        if cleaned.endswith(".git"):
            cleaned = cleaned[:-4]
        cleaned = cleaned.replace("https://", "").replace("http://", "")
        parts = cleaned.split("/")
        if len(parts) < 3:
            return None
        if parts[0].lower() != "github.com":
            return None
        owner = parts[1].strip()
        repo = parts[2].strip()
        if not owner or not repo:
            return None
        return owner, repo
    except Exception:
        return None

_LOGGER = logging.getLogger(__name__)

URL_BASE = "/onoff_store_static"
BRANDS_PATCHER_URL = "/onoff_store_static/yidstore-brands.js"


async def async_setup_brand_patcher(hass: HomeAssistant) -> None:
    """Setup the frontend brand icon patcher."""
    # Create the JavaScript patcher file
    static_dir = os.path.join(os.path.dirname(__file__), "dashboard_static")
    js_path = os.path.join(static_dir, "yidstore-brands.js")

    js_content = '''/**
 * YidStore Local Brands Patcher
 * Patches Home Assistant frontend to use local brand icons for custom integrations
 */
(function() {
    'use strict';

    const BRANDS_API = '/api/onoff_store/brands';
    let localBrands = {};
    let patchApplied = false;

    // Fetch list of available local brands
    async function loadLocalBrands() {
        try {
            const response = await fetch(BRANDS_API);
            if (response.ok) {
                localBrands = await response.json();
                console.log('[YidStore] Loaded local brands:', Object.keys(localBrands).length);
            }
        } catch (e) {
            console.debug('[YidStore] Could not load local brands:', e);
        }
    }

    // Get local icon URL if available
    function getLocalIconUrl(domain) {
        if (localBrands[domain]) {
            return localBrands[domain];
        }
        return null;
    }

    // Patch the customElements to intercept integration icons
    function patchIntegrationIcons() {
        if (patchApplied) return;

        // Method 1: Patch image loading for ha-integration-card and similar
        const originalFetch = window.fetch;
        window.fetch = async function(url, options) {
            // Intercept brand icon requests
            if (typeof url === 'string' && url.includes('brands.home-assistant.io')) {
                const match = url.match(/brands\\.home-assistant\\.io\\/_\\/([^/]+)\\/icon/);
                if (match) {
                    const domain = match[1];
                    const localUrl = getLocalIconUrl(domain);
                    if (localUrl) {
                        console.debug('[YidStore] Redirecting brand icon for:', domain);
                        return originalFetch.call(this, localUrl, options);
                    }
                }
            }
            return originalFetch.call(this, url, options);
        };

        // Method 2: Override Image loading
        const originalImageSrc = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, 'src');
        Object.defineProperty(HTMLImageElement.prototype, 'src', {
            get: function() {
                return originalImageSrc.get.call(this);
            },
            set: function(value) {
                if (typeof value === 'string' && value.includes('brands.home-assistant.io')) {
                    const match = value.match(/brands\\.home-assistant\\.io\\/_\\/([^/]+)\\/icon/);
                    if (match) {
                        const domain = match[1];
                        const localUrl = getLocalIconUrl(domain);
                        if (localUrl) {
                            console.debug('[YidStore] Redirecting image src for:', domain);
                            return originalImageSrc.set.call(this, localUrl);
                        }
                    }
                }
                return originalImageSrc.set.call(this, value);
            }
        });

        // Method 3: MutationObserver to catch dynamically added images
        const observer = new MutationObserver((mutations) => {
            mutations.forEach((mutation) => {
                mutation.addedNodes.forEach((node) => {
                    if (node.nodeType === 1) {
                        // Check images
                        const images = node.tagName === 'IMG' ? [node] : node.querySelectorAll ? node.querySelectorAll('img') : [];
                        images.forEach((img) => {
                            const src = img.getAttribute('src') || '';
                            if (src.includes('brands.home-assistant.io')) {
                                const match = src.match(/brands\\.home-assistant\\.io\\/_\\/([^/]+)\\/icon/);
                                if (match) {
                                    const domain = match[1];
                                    const localUrl = getLocalIconUrl(domain);
                                    if (localUrl) {
                                        console.debug('[YidStore] Patching image for:', domain);
                                        img.src = localUrl;
                                    }
                                }
                            }
                        });
                    }
                });
            });
        });

        observer.observe(document.body, {
            childList: true,
            subtree: true
        });

        patchApplied = true;
        console.log('[YidStore] Brand icon patcher activated');
    }

    // Initialize
    async function init() {
        await loadLocalBrands();
        patchIntegrationIcons();

        // Refresh brands list periodically (in case new integrations are installed)
        setInterval(loadLocalBrands, 60000);
    }

    // Start when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
'''

    # Write the JS file
    try:
        os.makedirs(static_dir, exist_ok=True)
        with open(js_path, 'w', encoding='utf-8') as f:
            f.write(js_content)
        _LOGGER.info("Created YidStore brands patcher JS at %s", js_path)
    except Exception as e:
        _LOGGER.error("Failed to create brands patcher JS: %s", e)
        return

    # Register as a Lovelace resource
    try:
        store = Store(hass, 1, "lovelace_resources")
        data = await store.async_load()

        if data is None:
            data = {"items": [], "version": 1}

        if "items" not in data:
            data["items"] = []

        # Check if already registered
        resource_url = BRANDS_PATCHER_URL
        already_registered = any(
            item.get("url", "").split("?")[0] == resource_url
            for item in data["items"]
        )

        if not already_registered:
            import uuid
            new_resource = {
                "id": uuid.uuid4().hex,
                "type": "module",
                "url": f"{resource_url}?v={int(time.time())}"
            }
            data["items"].append(new_resource)
            await store.async_save(data)
            _LOGGER.info("Registered YidStore brands patcher as Lovelace resource")
        else:
            _LOGGER.debug("YidStore brands patcher already registered")

    except Exception as e:
        _LOGGER.warning("Could not register brands patcher as resource: %s", e)


async def async_setup_dashboard(hass: HomeAssistant, entry) -> None:
    """Set up the store dashboard."""
    static_dir = os.path.join(os.path.dirname(__file__), "dashboard_static")
    if not os.path.exists(static_dir):
        os.makedirs(static_dir, exist_ok=True)

    await hass.http.async_register_static_paths([StaticPathConfig(URL_BASE, static_dir, False)])

    if entry.data.get(CONF_SIDE_PANEL, True):
        # Sidebar entry for Admin users - Cache buster added
        # Check if panel already registered to avoid error on reload
        if "onoff_store" not in hass.data.get("frontend_panels", {}):
            try:
                frontend.async_register_built_in_panel(
                    hass,
                    component_name="iframe",
                    sidebar_title="YidStore",
                    sidebar_icon="mdi:storefront",
                    frontend_url_path="onoff_store",
                    config={"url": f"{URL_BASE}/index.html?v={int(time.time())}"},
                    require_admin=True,
                )
            except ValueError as e:
                # Panel already exists, this is fine
                _LOGGER.debug("Panel already registered: %s", e)

    # Register API views
    eid = entry.entry_id
    hass.http.register_view(OnOffStoreReposView(eid))
    hass.http.register_view(OnOffStoreInstallView(eid))
    hass.http.register_view(OnOffStoreReadmeView(eid))
    hass.http.register_view(OnOffStoreReleasesView(eid))
    hass.http.register_view(OnOffStoreRefreshView(eid))
    hass.http.register_view(OnOffStoreAddCustomView(eid))
    hass.http.register_view(OnOffStoreListCustomView(eid))
    hass.http.register_view(OnOffStoreRemoveCustomView(eid))
    hass.http.register_view(OnOffStoreHideView(eid))
    hass.http.register_view(OnOffStoreUnhideView(eid))
    hass.http.register_view(OnOffStoreUninstallView(eid))
    hass.http.register_view(LocalBrandsIconView())
    hass.http.register_view(LocalBrandsListView())
    hass.http.register_view(LocalBrandsUploadView())

    # Setup the frontend brand patcher
    await async_setup_brand_patcher(hass)


class OnOffStoreReposView(HomeAssistantView):
    """API to list Gitea repositories."""
    url = "/api/onoff_store/repos"
    name = "api:onoff_store:repos"
    requires_auth = False 

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app.get("hass")
        try:
            # Dynamically find the entry if possible
            eid = self.entry_id
            if DOMAIN not in hass.data:
                return web.json_response({"error": "Integration not ready"}, status=503)
            
            if eid not in hass.data[DOMAIN]:
                # Try to find any existing entry (handles reload/reinstall cases)
                eids = list(hass.data[DOMAIN].keys())
                if not eids:
                    return web.json_response({"error": "Integration not ready"}, status=503)
                eid = eids[0]

            comp = hass.data[DOMAIN][eid]
            client = comp["client"]
            coordinator = comp["coordinator"]

            # Check if we have a valid authenticated session
            is_authenticated = False
            if client.token:
                is_authenticated = await client.test_auth()
                if not is_authenticated:
                    _LOGGER.warning("Gitea token provided but authentication failed (expired or revoked). Using public access.")

            # A. Load from store_list.yaml first (Ensures these are ALWAYS visible)
            yaml_items = await hass.async_add_executor_job(load_store_list, hass)
            
            # 1. Fetch custom repos FIRST to ensure they bypass filters
            custom_repos_to_fetch = coordinator.custom_repos
            
            resp_data = []
            
            for y in yaml_items:
                try:
                    # Fetch full info from Gitea for the YAML item
                    r = await client.get_repo(y["owner"], y["repo"])
                    if r: self._fill(resp_data, r, coordinator, yaml_items=yaml_items, bypass_filter=True, is_authenticated=is_authenticated)
                except Exception:
                    pass

            # B. Fetch from Organizations
            # Default public orgs (always fetched)
            default_orgs = ["Zing", "OnOffPublic"]

            # If authenticated, also fetch from all orgs they have access to
            orgs_to_fetch = set(default_orgs)
            if is_authenticated:
                try:
                    user_orgs = await client.get_user_orgs()
                    for org in user_orgs:
                        org_name = org.get("username") or org.get("name")
                        if org_name:
                            orgs_to_fetch.add(org_name)
                    _LOGGER.debug("Fetching repos from %d organizations", len(orgs_to_fetch))
                except Exception as e:
                    _LOGGER.debug("Failed to fetch user orgs: %s", e)

            # Collect org members to fetch their repos too (when authenticated)
            users_from_orgs = set()

            for o in orgs_to_fetch:
                try:
                    repos = await client.get_org_repos(o)
                    if isinstance(repos, list):
                        for r in repos:
                            self._fill(resp_data, r, coordinator, yaml_items=yaml_items, is_authenticated=is_authenticated)

                    # When authenticated, also get org members to fetch their personal repos
                    if is_authenticated:
                        try:
                            members = await client.get_org_members(o)
                            for member in members:
                                username = member.get("username") or member.get("login")
                                if username:
                                    users_from_orgs.add(username)
                        except Exception:
                            pass
                except Exception as e:
                    _LOGGER.debug("Store: Org %s error: %s", o, e)

            # C. Fetch from Individual Users
            # Default public users (always fetched) - add any public users you want to include
            default_users = []  # Add usernames here if needed, e.g., ["publicuser1", "publicuser2"]

            users_to_fetch = set(default_users)

            # Add org members when authenticated
            if is_authenticated:
                users_to_fetch.update(users_from_orgs)

                # Also fetch from users the authenticated user is following
                try:
                    following = await client.get_user_following()
                    for user in following:
                        username = user.get("login") or user.get("username")
                        if username:
                            users_to_fetch.add(username)
                except Exception as e:
                    _LOGGER.debug("Failed to fetch following users: %s", e)

                _LOGGER.debug("Fetching repos from %d users (org members + following)", len(users_to_fetch))

            # Fetch repos from users
            for u in users_to_fetch:
                try:
                    repos = await client.get_user_repos(u)
                    if isinstance(repos, list):
                        for r in repos:
                            self._fill(resp_data, r, coordinator, yaml_items=yaml_items, is_authenticated=is_authenticated)
                except Exception as e:
                    _LOGGER.debug("Store: User %s error: %s", u, e)

            # D. If authenticated, fetch user's own repositories
            if is_authenticated:
                try:
                    # Fetch user's own repositories
                    sess = async_get_clientsession(hass)
                    async with sess.get(f"{client.base_url}/api/v1/user/repos", headers=client._headers()) as resp:
                        if resp.status == 200:
                            u_repos = await resp.json()
                            if isinstance(u_repos, list):
                                for repo in u_repos:
                                    self._fill(resp_data, repo, coordinator, yaml_items=yaml_items, bypass_filter=True, is_authenticated=True)
                except Exception:
                    pass

            # E. Search for all accessible repos (catches user repos from both orgs and individuals)
            # This works for public repos even without authentication
            try:
                search_repos = await client.search_repos(limit=200)
                if isinstance(search_repos, list):
                    for repo in search_repos:
                        self._fill(resp_data, repo, coordinator, yaml_items=yaml_items, is_authenticated=is_authenticated)
            except Exception as e:
                _LOGGER.debug("Store: Search repos error: %s", e)

            # F. Explicitly fetch custom repos if they weren't in organizations or users
            for cr in custom_repos_to_fetch:
                owner = (cr.get("owner") or "").strip()
                repo = (cr.get("repo") or "").strip()
                source = cr.get("source", "gitea")
                if not owner or not repo:
                    continue

                if not any(x["owner"].lower() == owner.lower() and x["repo_name"].lower() == repo.lower() for x in resp_data):
                    if source == "github":
                        repo_type = cr.get("type") or "integration"
                        repo_url = cr.get("url")
                        icon_url = f"https://raw.githubusercontent.com/{owner}/{repo}/main/icons/icon.png"
                        resp_data.append({
                            "name": f"{owner}/{repo}",
                            "repo_name": repo,
                            "owner": owner,
                            "owner_display_name": owner,
                            "type": repo_type,
                            "description": "",
                            "updated_at": "",
                            "mode": "zipball",
                            "asset_name": None,
                            "is_installed": coordinator.get_package_by_repo(owner, repo) is not None,
                            "update_available": False,
                            "latest_version": None,
                            "release_notes": None,
                            "is_hidden": coordinator.is_hidden_repo(owner, repo),
                            "icon_url": icon_url,
                            "default_branch": "main",
                            "source": "github",
                            "repo_url": repo_url,
                        })
                    else:
                        try:
                            r = await client.get_repo(owner, repo)
                            if r: self._fill(resp_data, r, coordinator, yaml_items=yaml_items, bypass_filter=True, is_authenticated=is_authenticated)
                        except Exception:
                            pass

            return web.json_response(resp_data)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    def _fill(self, data_list, r, coord, yaml_items=None, bypass_filter=False, is_authenticated=False):
        name = r.get("full_name")
        if not name or any(x["name"] == name for x in data_list):
            return
        
        owner = r.get("owner", {}).get("login", "Unknown")
        rn = r.get("name", "Unknown")

        # Skip if MANUALLY hidden (unless we are showing hidden ones - logic will be in UI)
        is_hidden = coord.is_hidden_repo(owner, rn)

        # NEW FILTERING LOGIC:
        # 1. Hide integrations starting with 'x-' unless authenticated or custom repo
        if not bypass_filter and not is_authenticated and rn.lower().startswith("x-") and not coord.is_custom_repo(owner, rn):
            return
        
        # 2. Hide repos from "xshow" org unless authenticated
        if not bypass_filter and not is_authenticated and owner.lower() == "xshow":
            return
        
        # 3. Hide repos from any org starting with "private" unless authenticated
        if not bypass_filter and not is_authenticated and owner.lower().startswith("private"):
            return

        p = coord.get_package_by_repo(owner, rn)
        
        # Determine default mode/asset from YAML if present
        y_mode = None
        y_asset = None
        if yaml_items:
            y_pkg = next((y for y in yaml_items if y.get("owner") == owner and y.get("repo") == rn), None)
            if y_pkg:
                y_mode = y_pkg.get("mode")
                y_asset = y_pkg.get("asset_name")

        # Better type detection
        pkg_type = "integration"
        desc = (r.get("description") or "").lower()
        if p:
            pkg_type = p.get("package_type", "integration")
        elif "card" in rn.lower() or "lovelace" in rn.lower() or "card" in desc or "theme" in desc:
            pkg_type = "lovelace"
        elif "blueprint" in rn.lower() or "blueprint" in desc:
            pkg_type = "blueprints"

        # Get display name from owner object if available
        owner_obj = r.get("owner", {})
        owner_display_name = owner_obj.get("full_name") or owner_obj.get("username") or owner

        # Generate potential icon URL (frontend will try to load it)
        default_branch = r.get("default_branch", "main")
        base_url = r.get("html_url", "").rsplit("/", 2)[0] if r.get("html_url") else ""
        icon_url = f"{base_url}/{owner}/{rn}/raw/branch/{default_branch}/icons/icon.png" if base_url else None

        data_list.append({
            "name": name,
            "repo_name": rn,
            "owner": owner,
            "owner_display_name": owner_display_name,
            "type": pkg_type,
            "description": r.get("description") or "",
            "updated_at": r.get("updated_at", ""),
            "mode": p.get("mode") if p else y_mode,
            "asset_name": p.get("asset_name") if p else y_asset,
            "is_installed": p is not None,
            "update_available": p.get("update_available", False) if p else False,
            "latest_version": p.get("latest_version") if p else None,
            "release_notes": p.get("release_notes") if p else None,
            "is_hidden": is_hidden,
            "icon_url": icon_url,
            "default_branch": default_branch,
            "source": "gitea",
        })


class OnOffStoreInstallView(HomeAssistantView):
    """API to install integration."""
    url = "/api/onoff_store/install"
    name = "api:onoff_store:install"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            eid = self.entry_id
            if DOMAIN not in hass.data: return web.json_response({"error": "Integration not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if not eids: return web.json_response({"error": "Integration not ready"}, status=503)
                eid = eids[0]

            body = await request.json()
            o, r, t = body.get("owner"), body.get("repo"), body.get("type", "integration")
            source = body.get("source")
            repo_url = body.get("repo_url")
            mode = body.get("mode")
            asset_name = body.get("asset_name")
            
            if not o or not r:
                return web.json_response({"error": "Missing params"}, status=400)
            
            # Using the unified 'install' service
            svc_data = {
                "owner": o, 
                "repo": r,
                "type": t
            }
            if source:
                svc_data["source"] = source
            if repo_url:
                svc_data["repo_url"] = repo_url
            if mode: svc_data["mode"] = mode
            if asset_name: svc_data["asset_name"] = asset_name
            
            # Support for installing specific versions (passed as tag to service)
            version = body.get("version")
            if version: svc_data["tag"] = version

            await hass.services.async_call(DOMAIN, SERVICE_INSTALL, svc_data)
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)


class OnOffStoreReadmeView(HomeAssistantView):
    """API to fetch README."""
    url = "/api/onoff_store/readme/{owner}/{repo}"
    name = "api:onoff_store:readme"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request, owner: str, repo: str) -> web.Response:
        hass = request.app["hass"]
        try:
            eid = self.entry_id
            if DOMAIN not in hass.data: return web.Response(text="Not ready", status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if eids: eid = eids[0]
                else: return web.Response(text="Not ready", status=503)

            client = hass.data[DOMAIN][eid].get("client")
            coordinator = hass.data[DOMAIN][eid].get("coordinator")

            # If repo is a GitHub custom repo, fetch README from GitHub
            if coordinator:
                for cr in coordinator.get_custom_repos():
                    if cr.get("source") == "github" and cr.get("owner", "").lower() == owner.lower() and cr.get("repo", "").lower() == repo.lower():
                        sess = async_get_clientsession(hass)
                        for branch in ("main", "master"):
                            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/README.md"
                            async with sess.get(raw_url, timeout=30) as resp:
                                if resp.status == 200:
                                    return web.Response(text=await resp.text(), content_type="text/markdown")
                        break

            txt = await client.get_readme(owner, repo)
            return web.Response(text=txt or f"{repo} has no instructions available.", content_type="text/markdown")
        except Exception:
            return web.Response(text="Error", status=500)


class OnOffStoreReleasesView(HomeAssistantView):
    """API to fetch all releases for a repository."""
    url = "/api/onoff_store/releases/{owner}/{repo}"
    name = "api:onoff_store:releases"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request, owner: str, repo: str) -> web.Response:
        hass = request.app["hass"]
        try:
            eid = self.entry_id
            if DOMAIN not in hass.data: return web.json_response({"error": "Not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if eids: eid = eids[0]
                else: return web.json_response({"error": "Not ready"}, status=503)

            client = hass.data[DOMAIN][eid].get("client")
            coordinator = hass.data[DOMAIN][eid].get("coordinator")

            if coordinator:
                for cr in coordinator.get_custom_repos():
                    if cr.get("source") == "github" and cr.get("owner", "").lower() == owner.lower() and cr.get("repo", "").lower() == repo.lower():
                        return web.json_response([])

            releases = await client.get_releases(owner, repo)
            return web.json_response(releases)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)



class OnOffStoreRefreshView(HomeAssistantView):
    """API to trigger update check."""
    url = "/api/onoff_store/refresh"
    name = "api:onoff_store:refresh"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            eid = self.entry_id
            if DOMAIN not in hass.data: return web.json_response({"error": "Integration not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if not eids: return web.json_response({"error": "Integration not ready"}, status=503)
                eid = eids[0]

            coordinator = hass.data[DOMAIN][eid].get("coordinator")
            if coordinator:
                await coordinator.async_check_updates()
                return web.json_response({"success": True})
            return web.json_response({"error": "Coordinator missing"}, status=503)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)


class OnOffStoreAddCustomView(HomeAssistantView):
    """API to add a custom repository."""
    url = "/api/onoff_store/custom/add"
    name = "api:onoff_store:custom:add"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            body = await request.json()
            source = body.get("source", "gitea")
            repo_type = body.get("type") or "integration"
            repo_url = body.get("url")
            o, r = body.get("owner"), body.get("repo")

            if source == "github":
                if not repo_url:
                    return web.json_response({"error": "Missing GitHub URL"}, status=400)
                parsed = _parse_github_url(repo_url)
                if not parsed:
                    return web.json_response({"error": "Invalid GitHub URL"}, status=400)
                o, r = parsed
            elif not o or not r:
                return web.json_response({"error": "Missing params"}, status=400)
            
            eid = self.entry_id
            if DOMAIN not in hass.data: return web.json_response({"error": "Not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if eids: eid = eids[0]
                else: return web.json_response({"error": "Not ready"}, status=503)

            coordinator = hass.data[DOMAIN][eid].get("coordinator")
            if coordinator:
                await coordinator.async_add_custom_repo(o, r, source=source, repo_type=repo_type, repo_url=repo_url)
                return web.json_response({"success": True})
            return web.json_response({"error": "Coordinator missing"}, status=503)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)


class OnOffStoreListCustomView(HomeAssistantView):
    """API to list custom repositories."""
    url = "/api/onoff_store/custom/list"
    name = "api:onoff_store:custom:list"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            eid = self.entry_id
            if DOMAIN not in hass.data: return web.json_response({"error": "Not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if eids: eid = eids[0]
                else: return web.json_response({"error": "Not ready"}, status=503)

            coordinator = hass.data[DOMAIN][eid].get("coordinator")
            if coordinator:
                return web.json_response(coordinator.get_custom_repos())
            return web.json_response({"error": "Coordinator missing"}, status=503)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)


class OnOffStoreRemoveCustomView(HomeAssistantView):
    """API to remove a custom repository."""
    url = "/api/onoff_store/custom/remove"
    name = "api:onoff_store:custom:remove"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            body = await request.json()
            o, r = body.get("owner"), body.get("repo")
            if not o or not r:
                return web.json_response({"error": "Missing params"}, status=400)

            eid = self.entry_id
            if DOMAIN not in hass.data: return web.json_response({"error": "Not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if eids: eid = eids[0]
                else: return web.json_response({"error": "Not ready"}, status=503)

            coordinator = hass.data[DOMAIN][eid].get("coordinator")
            if coordinator:
                await coordinator.async_remove_custom_repo(o, r)
                return web.json_response({"success": True})
            return web.json_response({"error": "Coordinator missing"}, status=503)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)


class OnOffStoreHideView(HomeAssistantView):
    """API to hide a repository."""
    url = "/api/onoff_store/hide"
    name = "api:onoff_store:hide"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            body = await request.json()
            o, r = body.get("owner"), body.get("repo")
            eid = self.entry_id
            if DOMAIN not in hass.data: return web.json_response({"error": "Not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if eids: eid = eids[0]
                else: return web.json_response({"error": "Not ready"}, status=503)

            coordinator = hass.data[DOMAIN][eid].get("coordinator")
            if coordinator:
                await coordinator.async_hide_repo(o, r)
                return web.json_response({"success": True})
            return web.json_response({"error": "Coordinator missing"}, status=503)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)


class OnOffStoreUnhideView(HomeAssistantView):
    """API to unhide a repository."""
    url = "/api/onoff_store/unhide"
    name = "api:onoff_store:unhide"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            body = await request.json()
            o, r = body.get("owner"), body.get("repo")
            
            eid = self.entry_id
            if DOMAIN not in hass.data: return web.json_response({"error": "Not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if eids: eid = eids[0]
                else: return web.json_response({"error": "Not ready"}, status=503)

            coordinator = hass.data[DOMAIN][eid].get("coordinator")
            if coordinator:
                await coordinator.async_unhide_repo(o, r)
                return web.json_response({"success": True})
            return web.json_response({"error": "Coordinator missing"}, status=503)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)


class OnOffStoreUninstallView(HomeAssistantView):
    """API to uninstall a repository."""
    url = "/api/onoff_store/uninstall"
    name = "api:onoff_store:uninstall"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            body = await request.json()
            o, r, t = body.get("owner"), body.get("repo"), body.get("type", "integration")
            if not o or not r:
                return web.json_response({"error": "Missing params"}, status=400)

            eid = self.entry_id
            if DOMAIN not in hass.data: return web.json_response({"error": "Not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if eids: eid = eids[0]
                else: return web.json_response({"error": "Not ready"}, status=503)

            coordinator = hass.data[DOMAIN][eid].get("coordinator")
            if coordinator:
                # 1. Delete folder
                await hass.async_add_executor_job(uninstall_package, hass, t, r)
                # 2. Remove tracking
                await coordinator.async_remove_package(o, r)
                return web.json_response({"success": True})
            return web.json_response({"error": "Coordinator missing"}, status=503)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)


class LocalBrandsIconView(HomeAssistantView):
    """Serve local brand icons for custom integrations."""
    url = "/api/onoff_store/brands/{domain}/{filename}"
    name = "api:onoff_store:brands"
    requires_auth = False

    async def get(self, request: web.Request, domain: str, filename: str) -> web.Response:
        """Serve icon file from local brands or custom_components folder."""
        hass = request.app["hass"]

        # Security: only allow specific filenames
        allowed_files = ['icon.png', 'icon@2x.png', 'logo.png', 'icon.svg', 'logo.svg']
        if filename not in allowed_files:
            return web.Response(status=404)

        # Security: sanitize domain name
        domain = domain.replace('..', '').replace('/', '').replace('\\', '')

        # Try multiple locations in order of preference
        locations = [
            os.path.join(hass.config.path("www", "brands", domain), filename),
            os.path.join(hass.config.path("custom_components", domain), filename),
        ]

        for path in locations:
            if os.path.exists(path) and os.path.isfile(path):
                try:
                    with open(path, 'rb') as f:
                        content = f.read()

                    content_type = 'image/png' if filename.endswith('.png') else 'image/svg+xml'
                    return web.Response(
                        body=content,
                        content_type=content_type,
                        headers={
                            'Cache-Control': 'public, max-age=86400',
                            'Access-Control-Allow-Origin': '*',
                        }
                    )
                except Exception as e:
                    _LOGGER.debug("Error reading icon %s: %s", path, e)
                    continue

        return web.Response(status=404)


class LocalBrandsListView(HomeAssistantView):
    """List all available local brand icons."""
    url = "/api/onoff_store/brands"
    name = "api:onoff_store:brands_list"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Return list of domains with local icons."""
        hass = request.app["hass"]

        domains_with_icons = {}

        # Check www/brands folder
        brands_path = hass.config.path("www", "brands")
        if os.path.exists(brands_path):
            for domain in os.listdir(brands_path):
                domain_path = os.path.join(brands_path, domain)
                if os.path.isdir(domain_path):
                    icon_png = os.path.join(domain_path, "icon.png")
                    icon_svg = os.path.join(domain_path, "icon.svg")
                    if os.path.exists(icon_png):
                        domains_with_icons[domain] = f"/api/onoff_store/brands/{domain}/icon.png"
                    elif os.path.exists(icon_svg):
                        domains_with_icons[domain] = f"/api/onoff_store/brands/{domain}/icon.svg"

        # Check custom_components folders
        cc_path = hass.config.path("custom_components")
        if os.path.exists(cc_path):
            for domain in os.listdir(cc_path):
                if domain in domains_with_icons:
                    continue
                domain_path = os.path.join(cc_path, domain)
                if os.path.isdir(domain_path):
                    icon_png = os.path.join(domain_path, "icon.png")
                    icon_svg = os.path.join(domain_path, "icon.svg")
                    if os.path.exists(icon_png):
                        domains_with_icons[domain] = f"/api/onoff_store/brands/{domain}/icon.png"
                    elif os.path.exists(icon_svg):
                        domains_with_icons[domain] = f"/api/onoff_store/brands/{domain}/icon.svg"

        return web.json_response(domains_with_icons)


class LocalBrandsUploadView(HomeAssistantView):
    """Upload local brand icons for custom integrations."""
    url = "/api/onoff_store/brands/upload"
    name = "api:onoff_store:brands_upload"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]

        reader = await request.multipart()
        domain = None
        files: dict[str, bytes] = {}

        allowed_part_names = {"icon", "icon2x", "logo"}
        allowed_ext = {".png", ".svg"}

        while True:
            part = await reader.next()
            if not part:
                break

            if part.name == "domain":
                domain = (await part.text()).strip().lower()
                continue

            if part.name not in allowed_part_names or not part.filename:
                continue

            ext = os.path.splitext(part.filename)[1].lower()
            if ext not in allowed_ext:
                return web.json_response({"error": "Invalid file type. Use .png or .svg."}, status=400)

            if part.name == "icon2x" and ext != ".png":
                return web.json_response({"error": "icon@2x must be a .png file."}, status=400)

            data = await part.read()
            if not data:
                continue

            if part.name == "icon":
                out_name = f"icon{ext}"
            elif part.name == "logo":
                out_name = f"logo{ext}"
            else:
                out_name = "icon@2x.png"

            files[out_name] = data

        if not domain:
            return web.json_response({"error": "Missing domain."}, status=400)

        if not re.match(r"^[a-z0-9_]+$", domain):
            return web.json_response({"error": "Invalid domain. Use only a-z, 0-9, and _."}, status=400)

        if not files:
            return web.json_response({"error": "No files uploaded."}, status=400)

        brands_dir = Path(hass.config.path("www", "brands", domain))
        try:
            brands_dir.mkdir(parents=True, exist_ok=True)
            for filename, data in files.items():
                dest = brands_dir / filename
                with open(dest, "wb") as f:
                    f.write(data)
        except Exception as e:
            _LOGGER.error("Failed to save brand files for %s: %s", domain, e)
            return web.json_response({"error": "Failed to save branding."}, status=500)

        return web.json_response({"success": True, "domain": domain, "files": list(files.keys())})
