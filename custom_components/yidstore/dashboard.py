"""Dashboard for YidStore - V4 Robust."""
from __future__ import annotations

import logging
import os
import re
import time
import asyncio
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


async def _github_json(hass: HomeAssistant, url: str) -> dict | list | None:
    """Fetch JSON from GitHub API (unauthenticated)."""
    sess = async_get_clientsession(hass)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "yidstore",
    }
    try:
        async with sess.get(url, headers=headers, timeout=20) as resp:
            if resp.status != 200:
                return None
            return await resp.json()
    except Exception:
        return None


async def _resolve_github_integration_domain(hass: HomeAssistant, owner: str, repo: str) -> str | None:
    """Resolve HA integration domain in a GitHub repo (HACS-style)."""
    # 1) Get default branch
    info = await _github_json(hass, f"https://api.github.com/repos/{owner}/{repo}")
    branch = None
    if isinstance(info, dict):
        branch = info.get("default_branch")
    if not branch:
        branch = "main"

    # 2) List custom_components/
    cc = await _github_json(hass, f"https://api.github.com/repos/{owner}/{repo}/contents/custom_components?ref={branch}")
    if not isinstance(cc, list):
        return None

    domains = [x.get("name") for x in cc if isinstance(x, dict) and x.get("type") == "dir" and x.get("name")]
    if not domains:
        return None

    # Prefer locally installed domain if present
    local_cc = Path("/config/custom_components")
    chosen = None
    for d in domains:
        try:
            if (local_cc / d).is_dir():
                chosen = d
                break
        except Exception:
            continue
    if not chosen:
        chosen = domains[0]

    # 3) Read manifest for chosen domain (optional)
    mf = await _github_json(hass, f"https://api.github.com/repos/{owner}/{repo}/contents/custom_components/{chosen}/manifest.json?ref={branch}")
    if isinstance(mf, dict) and mf.get("content"):
        try:
            import base64, json
            content = base64.b64decode(mf["content"]).decode("utf-8", errors="ignore")
            manifest = json.loads(content)
            dom = (manifest.get("domain") or chosen or "").strip()
            return dom or None
        except Exception:
            return chosen
    return chosen


def _github_brand_icon_url(owner: str, repo: str, domain: str | None, branch: str = "main") -> str:
    """Return preferred icon URL for GitHub integrations.

    Prefer repo brand folder first. UI then falls back to HA Brands by domain.
    """
    chosen_domain = (domain or repo).strip().lower().replace("-", "_")
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/custom_components/{chosen_domain}/brand/icon.png"

_LOGGER = logging.getLogger(__name__)


def _repo_slug(text: str) -> str:
    """Create stable entity-id-safe slug from repo/domain text."""
    return re.sub(r"[^a-z0-9_]+", "_", (text or "").strip().lower().replace("-", "_")).strip("_")


def _waiting_restart_from_sensor(hass: HomeAssistant, repo: str) -> bool:
    """Return restart-needed flag from the integration Waiting Restart sensor."""
    slug = _repo_slug(repo)

    # Try multiple entity_id patterns
    candidates = [
        f"sensor.{slug}_waiting_restart",
        f"sensor.onoff_{slug}_waiting_restart",
        f"sensor.yidstore_{slug}_waiting_restart",
        f"sensor.{slug}_{slug}_waiting_restart",  # Device name prefix
    ]

    for entity_id in candidates:
        st = hass.states.get(entity_id)
        if st is not None:
            result = str(st.state).strip().lower() == "yes"
            _LOGGER.debug("Found sensor %s with state %s -> needs_restart=%s", entity_id, st.state, result)
            return result

    # Fallback: search all sensors ending with _waiting_restart
    repo_lower = repo.lower().replace("-", "_").replace(" ", "_")
    try:
        for st in hass.states.async_all("sensor"):
            if not st.entity_id.endswith("_waiting_restart"):
                continue
            # Check if entity_id contains the repo slug
            if slug in st.entity_id or repo_lower in st.entity_id:
                result = str(st.state).strip().lower() == "yes"
                _LOGGER.debug("Found sensor by search %s with state %s -> needs_restart=%s", st.entity_id, st.state, result)
                return result
            # Also check friendly name
            friendly = str((st.attributes or {}).get("friendly_name", "")).strip().lower()
            if repo_lower in friendly or slug in friendly:
                result = str(st.state).strip().lower() == "yes"
                _LOGGER.debug("Found sensor by friendly name %s with state %s -> needs_restart=%s", st.entity_id, st.state, result)
                return result
    except Exception as e:
        _LOGGER.debug("Error searching for restart sensor: %s", e)

    _LOGGER.debug("No restart sensor found for repo %s (slug: %s)", repo, slug)
    return False

# GitHub Integrations source repo on Gitea
GITHUB_INTEGRATIONS_REPO = "OnOffPublic/Github-Integrations"
GITHUB_INTEGRATIONS_BASE_URL = "https://git.onoffapi.com"

async def _fetch_github_integrations_list(hass: HomeAssistant, client) -> list[dict]:
    """Fetch GitHub integrations/cards list from the Gitea repo.

    Reads Cards.md and integrations.md from the Github-Integrations repo
    and parses GitHub URLs to add them to the store.
    """
    result = []

    files_to_fetch = [
        ("integrations.md", "integration"),
        ("Cards.md", "lovelace"),
    ]

    for filename, pkg_type in files_to_fetch:
        try:
            content = await client.get_file_content("OnOffPublic", "Github-Integrations", filename, branch="main")
            if not content:
                continue

            # Parse GitHub URLs from the markdown file
            # Supports formats like:
            # - https://github.com/owner/repo
            # - [text](https://github.com/owner/repo)
            # - github.com/owner/repo
            import re
            github_pattern = r'(?:https?://)?github\.com/([a-zA-Z0-9_-]+)/([a-zA-Z0-9_.-]+)'

            for match in re.finditer(github_pattern, content):
                owner = match.group(1).strip()
                repo = match.group(2).strip()

                # Clean up repo name (remove .git suffix, trailing slashes, etc.)
                repo = repo.rstrip('/').rstrip('.git')
                if repo.endswith('.git'):
                    repo = repo[:-4]

                if owner and repo:
                    result.append({
                        "owner": owner,
                        "repo": repo,
                        "type": pkg_type,
                        "source": "github",
                        "url": f"https://github.com/{owner}/{repo}",
                        "from_list": filename,
                    })

        except Exception as e:
            _LOGGER.debug("Failed to fetch %s from Github-Integrations: %s", filename, e)

    _LOGGER.debug("Loaded %d GitHub integrations from Github-Integrations repo", len(result))
    return result


URL_BASE = "/yidstore_static"
BRANDS_PATCHER_URL = "/yidstore_static/yidstore-brands.js"


def _normalize_slug(s: str) -> str:
    return (s or "").strip().lower().replace("-", "_")


def _collect_local_installed_sync(config_path: str) -> dict:
    """Collect what is installed on this Home Assistant (sync version for executor).

    This function does filesystem operations and should be called via async_add_executor_job.
    """
    import json as json_module

    domains: set[str] = set()
    community: set[str] = set()
    audio_paths: set[str] = set()
    hacs_domains: set[str] = set()
    hacs_repos: set[str] = set()

    # A) Installed custom integrations on disk
    try:
        cc_path = Path(config_path) / "custom_components"
        if cc_path.is_dir():
            for p in cc_path.iterdir():
                if p.is_dir() and (p / "manifest.json").is_file():
                    domains.add(_normalize_slug(p.name))
    except Exception:
        pass

    # B2) Installed audio packages in /www/audio/<owner>/<repo>/
    try:
        audio_root = Path(config_path) / "www" / "audio"
        if audio_root.is_dir():
            for owner_dir in audio_root.iterdir():
                if not owner_dir.is_dir():
                    continue
                owner_slug = _normalize_slug(owner_dir.name)
                for repo_dir in owner_dir.iterdir():
                    if not repo_dir.is_dir():
                        continue
                    repo_slug = _normalize_slug(repo_dir.name)
                    if owner_slug and repo_slug:
                        audio_paths.add(f"{owner_slug}/{repo_slug}")
    except Exception:
        pass

    # B) Installed frontend/community content (HACS typically puts cards/themes here)
    try:
        comm_path = Path(config_path) / "www" / "community"
        if comm_path.is_dir():
            for p in comm_path.iterdir():
                if p.is_dir():
                    community.add(_normalize_slug(p.name))
    except Exception:
        pass

    # C) Detect HACS-installed items by checking HACS data
    try:
        hacs_file = Path(config_path) / ".storage" / "hacs.repositories"
        if hacs_file.is_file():
            with open(hacs_file, 'r', encoding='utf-8') as f:
                hacs_data = json_module.load(f)
            repos = hacs_data.get("data", {})
            if isinstance(repos, dict):
                for repo_id, repo_info in repos.items():
                    if isinstance(repo_info, dict):
                        domain = repo_info.get("domain") or repo_info.get("name", "")
                        if domain:
                            hacs_domains.add(_normalize_slug(domain))
                        full_name = repo_info.get("full_name", "")
                        if full_name:
                            hacs_repos.add(full_name.lower())
    except Exception:
        pass

    return {
        "domains": domains,
        "community": community,
        "audio_paths": audio_paths,
        "hacs_domains": hacs_domains,
        "hacs_repos": hacs_repos,
    }


async def _collect_local_installed(hass: HomeAssistant) -> dict:
    """Collect what is installed on this Home Assistant (async version).

    We intentionally detect *files present* (custom_components / www/community) and
    *configured integrations* (config entries). This makes the "Installed" badge
    match what the user actually has on disk, even if it was installed outside
    of YidStore (e.g. HACS/manual).
    """
    # Run filesystem operations in executor
    result = await hass.async_add_executor_job(_collect_local_installed_sync, hass.config.config_dir)

    # Add configured integrations (this is async-safe)
    try:
        for entry in hass.config_entries.async_entries():
            if getattr(entry, "domain", None):
                result["domains"].add(_normalize_slug(entry.domain))
    except Exception:
        pass

    return result


def _get_install_info(
    *,
    local_state: dict,
    pkg_type: str,
    domain: str | None,
    repo_name: str,
    owner: str | None = None,
) -> tuple[bool, str | None]:
    """Determine install status and source using local HA state.

    Returns (is_installed, install_source) where install_source is:
    - 'hacs' if installed by HACS
    - 'manual' if found on disk but not tracked
    - None if not installed
    """
    domains = local_state.get("domains", set()) if isinstance(local_state, dict) else set()
    community = local_state.get("community", set()) if isinstance(local_state, dict) else set()
    audio_paths = local_state.get("audio_paths", set()) if isinstance(local_state, dict) else set()
    hacs_domains = local_state.get("hacs_domains", set()) if isinstance(local_state, dict) else set()
    hacs_repos = local_state.get("hacs_repos", set()) if isinstance(local_state, dict) else set()

    rn = _normalize_slug(repo_name)
    dom = _normalize_slug(domain) if domain else ""
    full_name = f"{owner}/{repo_name}".lower() if owner else ""

    # Check if this is in HACS repos
    is_hacs = full_name in hacs_repos or (dom and dom in hacs_domains) or (rn and rn in hacs_domains)

    if pkg_type == "integration":
        # Primary: resolved domain
        if dom and dom in domains:
            return (True, "hacs" if is_hacs else "manual")
        # Fallback: repo name often equals folder name
        if rn and rn in domains:
            return (True, "hacs" if is_hacs else "manual")
        return (False, None)

    if pkg_type == "audio":
        owner_slug = _normalize_slug(owner or "")
        full_slug = f"{owner_slug}/{rn}" if owner_slug and rn else ""
        if full_slug and full_slug in audio_paths:
            return (True, "manual")
        return (False, None)

    # Lovelace / themes / blueprints: best-effort via www/community folder
    if rn and rn in community:
        return (True, "hacs" if is_hacs else "manual")

    return (False, None)


def _is_repo_installed(
    *,
    local_state: dict,
    pkg_type: str,
    domain: str | None,
    repo_name: str,
    owner: str | None = None,
    source: str | None = None,
) -> bool:
    """Determine install status using local HA state + best-effort fallbacks."""
    is_installed, _ = _get_install_info(
        local_state=local_state,
        pkg_type=pkg_type,
        domain=domain,
        repo_name=repo_name,
        owner=owner,
    )
    return is_installed


async def async_setup_brand_patcher(hass: HomeAssistant) -> None:
    """Setup the frontend brand icon patcher."""
    # Create the JavaScript patcher file
    static_dir = os.path.join(os.path.dirname(__file__), "dashboard_static")
    js_path = os.path.join(static_dir, "yidstore-brands.js")

    js_content = r'''/**
 * YidStore Brands Patcher
 * - Overrides Home Assistant brand icon/logo fetches to prefer local overrides:
 *     /config/custom_components/<domain>/brand/...
 *     /config/custom_components/<domain>/...
 * - Falls back to official brands site if no local file exists.
 */
(function() {
  'use strict';

  const BRANDS_LIST_API = '/api/yidstore/brands';
  let localBrands = {}; // domain -> { filename -> local url }
  let patchApplied = false;

  async function loadLocalBrands() {
    try {
      const res = await fetch(BRANDS_LIST_API, { cache: 'no-store' });
      if (res.ok) {
        localBrands = await res.json();
        console.debug('[YidStore] Local brands loaded:', Object.keys(localBrands).length);
      }
    } catch (e) {
      console.debug('[YidStore] Failed to load local brands list:', e);
    }
  }

  function matchBrandUrl(u) {
    if (typeof u !== 'string') return null;
    if (!u.includes('brands.home-assistant.io')) return null;

    // Supports:
    //  - https://brands.home-assistant.io/<domain>/icon.png
    //  - https://brands.home-assistant.io/_/<domain>/icon.png
    //  - .../logo.png, dark_icon.png, dark_logo.png, @2x and svg
    const m = u.match(/brands\.home-assistant\.io\/(?:_\/)?([^/]+)\/([^/?#]+)$/);
    if (!m) return null;
    return { domain: decodeURIComponent(m[1] || ''), filename: (m[2] || '').split('?')[0] };
  }

  function getLocalUrl(domain, filename) {
    const d = (domain || '').toLowerCase();
    const files = localBrands[d];
    if (!files) return null;
    if (files[filename]) return files[filename];

    // Fallback order: if a specific file isn't present, fall back between dark/light, and logo->icon.
    const alt = {
      'dark_icon.png': 'icon.png',
      'icon.png': 'dark_icon.png',
      'dark_logo.png': 'logo.png',
      'logo.png': 'dark_logo.png',
      'dark_icon@2x.png': 'icon@2x.png',
      'icon@2x.png': 'dark_icon@2x.png',
      'dark_logo@2x.png': 'logo@2x.png',
      'logo@2x.png': 'dark_logo@2x.png',
    };
    const a = alt[filename];
    if (a && files[a]) return files[a];

    // If logo missing, try icon
    if (filename.startsWith('logo') && files['icon.png']) return files['icon.png'];
    if (filename.startsWith('dark_logo') && files['dark_icon.png']) return files['dark_icon.png'];

    return null;
  }

  function patch() {
    if (patchApplied) return;

    // Patch fetch
    const origFetch = window.fetch;
    window.fetch = async function(url, options) {
      const hit = matchBrandUrl(url);
      if (hit) {
        const local = getLocalUrl(hit.domain, hit.filename);
        if (local) return origFetch.call(this, local, options);
      }
      return origFetch.call(this, url, options);
    };

    // Patch <img src=...>
    const desc = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, 'src');
    Object.defineProperty(HTMLImageElement.prototype, 'src', {
      get() { return desc.get.call(this); },
      set(v) {
        const hit = matchBrandUrl(v);
        if (hit) {
          const local = getLocalUrl(hit.domain, hit.filename);
          if (local) return desc.set.call(this, local);
        }
        return desc.set.call(this, v);
      }
    });

    // Catch dynamically inserted images
    const obs = new MutationObserver((mutations) => {
      for (const m of mutations) {
        for (const node of m.addedNodes || []) {
          if (!node || node.nodeType !== 1) continue;
          const imgs = node.tagName === 'IMG' ? [node] : (node.querySelectorAll ? node.querySelectorAll('img') : []);
          imgs.forEach(img => {
            const src = img.getAttribute('src') || '';
            const hit = matchBrandUrl(src);
            if (!hit) return;
            const local = getLocalUrl(hit.domain, hit.filename);
            if (local) img.src = local;
          });
        }
      }
    });
    obs.observe(document.body, { childList: true, subtree: true });

    patchApplied = true;
    console.debug('[YidStore] Brands patcher active');
  }

  async function init() {
    await loadLocalBrands();
    patch();
    setInterval(loadLocalBrands, 60000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})(); 
'''

    # Write the JS file (in executor to avoid blocking event loop)
    def _write_js_file():
        os.makedirs(static_dir, exist_ok=True)
        with open(js_path, 'w', encoding='utf-8') as f:
            f.write(js_content)

    try:
        await hass.async_add_executor_job(_write_js_file)
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
        if "yidstore" not in hass.data.get("frontend_panels", {}):
            try:
                frontend.async_register_built_in_panel(
                    hass,
                    component_name="iframe",
                    sidebar_title="YidStore",
                    sidebar_icon="mdi:storefront",
                    frontend_url_path="yidstore",
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
    hass.http.register_view(OnOffStoreStatusView(eid))
    hass.http.register_view(LocalBrandsIconView())
    hass.http.register_view(LocalBrandsListView())
    hass.http.register_view(LocalBrandsUploadView())

    # Initialize requires_restart set in hass.data
    if "yidstore_requires_restart" not in hass.data:
        hass.data["yidstore_requires_restart"] = set()

    # Setup the frontend brand patcher
    await async_setup_brand_patcher(hass)


class OnOffStoreReposView(HomeAssistantView):
    """API to list Gitea repositories."""
    url = "/api/yidstore/repos"
    name = "api:yidstore:repos"
    requires_auth = False 

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app.get("hass")
        try:
            # Local install state (custom_components, config entries, www/community)
            local_state = await _collect_local_installed(hass)

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

            # 1. Fetch custom repos FIRST (will be processed at end if missed)
            custom_repos_to_fetch = list(coordinator.custom_repos)

            # 1b. Fetch GitHub integrations from the Github-Integrations repo
            github_integrations_list = await _fetch_github_integrations_list(hass, client)
            for gi in github_integrations_list:
                # Add to custom repos if not already there
                if not any(
                    cr.get("owner", "").lower() == gi["owner"].lower() and
                    cr.get("repo", "").lower() == gi["repo"].lower()
                    for cr in custom_repos_to_fetch
                ):
                    custom_repos_to_fetch.append(gi)

            # State for collecting unique repos
            seen_repos = set()
            tasks = []
            
            def add_repo_task(repo_obj, bypass=False, auth=False):
                full_name = repo_obj.get("full_name")
                if not full_name: return
                if full_name in seen_repos: return
                seen_repos.add(full_name)
                tasks.append(self._process_repo(repo_obj, coordinator, yaml_items, bypass, auth, local_state))
            
            for y in yaml_items:
                try:
                    # Fetch full info from Gitea for the YAML item
                    r = await client.get_repo(y["owner"], y["repo"])
                    if r: add_repo_task(r, bypass=True, auth=is_authenticated)
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
                            add_repo_task(r, auth=is_authenticated)

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
            default_users = []  # Add usernames here if needed

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
                            add_repo_task(r, auth=is_authenticated)
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
                                    add_repo_task(repo, bypass=True, auth=True)
                except Exception:
                    pass

            # E. Search for all accessible repos (catches user repos from both orgs and individuals)
            # This works for public repos even without authentication
            try:
                search_repos = await client.search_repos(limit=200)
                if isinstance(search_repos, list):
                    for repo in search_repos:
                        add_repo_task(repo, auth=is_authenticated)
            except Exception as e:
                _LOGGER.debug("Store: Search repos error: %s", e)

            # Execute all collected tasks in parallel
            processed_results = await asyncio.gather(*tasks)
            resp_data = [res for res in processed_results if res is not None]

            # F. Explicitly fetch custom repos if they weren't in organizations or users
            for cr in custom_repos_to_fetch:
                owner = (cr.get("owner") or "").strip()
                repo = (cr.get("repo") or "").strip()
                source = cr.get("source", "gitea")
                if not owner or not repo:
                    continue

                if not any(x["owner"].lower() == owner.lower() and x["repo_name"].lower() == repo.lower() for x in resp_data):
                    if source == "github":
                        repo_type = cr.get("type") or ("audio" if owner.lower() == "audio" else "integration")
                        repo_url = cr.get("url")
                        # Best-effort domain resolution (repo_name != domain for many integrations)
                        domain = None
                        if repo_type == "integration":
                            domain = await _resolve_github_integration_domain(hass, owner, repo)

                        # Prefer integration-local brand icon in the repo.
                        icon_url = _github_brand_icon_url(owner, repo, domain, "main")

                        # Determine installation status and source
                        tracked_pkg = coordinator.get_package_by_repo(owner, repo)
                        disk_installed, disk_source = _get_install_info(
                            local_state=local_state,
                            pkg_type=repo_type,
                            domain=domain,
                            repo_name=repo,
                            owner=owner,
                        )

                        if tracked_pkg is not None:
                            is_installed = True
                            install_source = "yidstore"
                        elif disk_installed:
                            is_installed = True
                            install_source = disk_source
                        else:
                            is_installed = False
                            install_source = None

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
                            "is_installed": is_installed,
                            "install_source": install_source,
                            "update_available": False,
                            "latest_version": None,
                            "release_notes": None,
                            "is_hidden": coordinator.is_hidden_repo(owner, repo),
                            "icon_url": icon_url,
                            "domain": domain,
                            "default_branch": "main",
                            "source": "github",
                            "repo_url": repo_url,
                        })
                    else:
                        try:
                            # Manually fetch missed Gitea custom repo
                            r = await client.get_repo(owner, repo)
                            if r:
                                item = await self._process_repo(r, coordinator, yaml_items, bypass=True, auth=is_authenticated, local_state=local_state)
                                if item:
                                    resp_data.append(item)
                        except Exception:
                            pass

            return web.json_response(resp_data)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _process_repo(self, r, coord, yaml_items=None, bypass=False, auth=False, local_state=None):
        name = r.get("full_name")
        # Dupe check handled by caller

        owner = r.get("owner", {}).get("login", "Unknown")
        rn = r.get("name", "Unknown")

        # Skip if archived
        if r.get("archived", False):
            return None

        # Skip Github-Integrations repo (it's just a config repo for GitHub links)
        if owner.lower() == "onoffpublic" and rn.lower() == "github-integrations":
            return None

        # Skip if MANUALLY hidden (unless we are showing hidden ones - logic will be in UI)
        is_hidden = coord.is_hidden_repo(owner, rn)

        # NEW FILTERING LOGIC:
        # 1. Hide integrations starting with 'x-' unless authenticated or custom repo
        if not bypass and not auth and rn.lower().startswith("x-") and not coord.is_custom_repo(owner, rn):
            return None

        # 2. Hide repos from "xshow" org unless authenticated
        if not bypass and not auth and owner.lower() == "xshow":
            return None
        
        # 3. Hide repos from any org starting with "private" unless authenticated
        if not bypass and not auth and owner.lower().startswith("private"):
            return None

        p = coord.get_package_by_repo(owner, rn)
        
        # Determine default mode/asset from YAML if present
        y_mode = None
        y_asset = None
        if yaml_items:
            y_pkg = next((y for y in yaml_items if y.get("owner") == owner and y.get("repo") == rn), None)
            if y_pkg:
                y_mode = y_pkg.get("mode")
                y_asset = y_pkg.get("asset_name")

        default_branch = r.get("default_branch", "main")

        # Better type detection
        pkg_type = "integration"
        desc = (r.get("description") or "").lower()
        if p:
            pkg_type = p.get("package_type", "integration")
        elif owner.lower() == "audio":
            pkg_type = "audio"
        elif "card" in rn.lower() or "lovelace" in rn.lower() or "card" in desc or "theme" in desc:
            pkg_type = "lovelace"
        elif "blueprint" in rn.lower() or "blueprint" in desc:
            pkg_type = "blueprints"
        else:
            # Repo layout-based detection for better card/integration classification.
            try:
                root_entries = await coord.client.list_dir(owner, rn, path="", branch=default_branch)
                if isinstance(root_entries, list):
                    has_custom_components = any(
                        isinstance(e, dict) and e.get("type") == "dir" and (e.get("name") or "").lower() == "custom_components"
                        for e in root_entries
                    )
                    has_blueprints = any(
                        isinstance(e, dict) and e.get("type") == "dir" and (e.get("name") or "").lower() == "blueprints"
                        for e in root_entries
                    )
                    has_root_js = any(
                        isinstance(e, dict)
                        and e.get("type") == "file"
                        and str(e.get("name") or "").lower().endswith(".js")
                        for e in root_entries
                    )
                    if has_blueprints:
                        pkg_type = "blueprints"
                    elif has_root_js and not has_custom_components:
                        pkg_type = "lovelace"
            except Exception:
                pass

        # Get display name from owner object if available
        owner_obj = r.get("owner", {})
        owner_display_name = owner_obj.get("full_name") or owner_obj.get("username") or owner

        # Generate potential icon URL with fallback to Brands
        if pkg_type in ("lovelace", "audio"):
            # Never show integration/brands icon for cards or audio packs.
            icon_url = None
        else:
            icon_url = None
            try:
                # Check Gitea for icon
                icon_url = await coord.client.get_icon_url(owner, rn, branch=default_branch)
            except Exception:
                pass


        # Resolve integration domain (HACS-style): custom_components/<domain>/manifest.json -> manifest["domain"].
        # This is critical because repo_name != domain for many integrations (e.g. ha_pura -> pura).
        domain_from_manifest = None
        if pkg_type == "integration":
            try:
                # 1) Prefer a domain that is already installed locally
                local_cc = Path("/config/custom_components")
                domains = await coord.client.get_integration_domains(owner, rn, branch=default_branch)
                chosen_domain = None
                if domains:
                    for d in domains:
                        try:
                            if (local_cc / d).is_dir():
                                chosen_domain = d
                                break
                        except Exception:
                            continue
                    if not chosen_domain:
                        chosen_domain = domains[0]

                # 2) Read manifest from the chosen domain folder (or fall back to legacy root manifest)
                manifest_paths = []
                if chosen_domain:
                    manifest_paths.append(f"custom_components/{chosen_domain}/manifest.json")
                manifest_paths.append("manifest.json")

                for mp in manifest_paths:
                    try:
                        manifest_content = await coord.client.get_file_content(owner, rn, mp, branch=default_branch)
                        if not manifest_content:
                            continue
                        import json
                        manifest = json.loads(manifest_content)
                        domain_from_manifest = (manifest.get("domain") or chosen_domain or "").strip() or None
                        if domain_from_manifest:
                            break
                    except Exception:
                        continue
            except Exception:
                pass

        # Determine installation status and source
        disk_installed, disk_source = _get_install_info(
            local_state=local_state or {},
            pkg_type=pkg_type,
            domain=domain_from_manifest,
            repo_name=rn,
            owner=owner,
        )

        # If tracked by coordinator (p is not None), it's installed by YidStore
        # Otherwise check disk detection
        if p is not None:
            installed = True
            install_source = "yidstore"
        elif disk_installed:
            installed = True
            install_source = disk_source  # 'hacs' or 'manual'
        else:
            installed = False
            install_source = None

        return {
            "name": name,
            "repo_name": rn,
            "owner": owner,
            "owner_display_name": owner_display_name,
            "type": pkg_type,
            "description": r.get("description") or "",
            "updated_at": r.get("updated_at", ""),
            "mode": p.get("mode") if p else y_mode,
            "asset_name": p.get("asset_name") if p else y_asset,
            "is_installed": installed,
            "install_source": install_source,
            "update_available": p.get("update_available", False) if p else False,
            "latest_version": p.get("latest_version") if p else None,
            "release_notes": p.get("release_notes") if p else None,
            "is_hidden": is_hidden,
            "icon_url": icon_url,
            "domain": domain_from_manifest,
            "default_branch": default_branch,
            "source": p.get("source", "gitea") if p else "gitea",
        }


class OnOffStoreInstallView(HomeAssistantView):
    """API to install integration."""
    url = "/api/yidstore/install"
    name = "api:yidstore:install"
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

            # Track that this repo requires restart
            if "yidstore_requires_restart" not in hass.data:
                hass.data["yidstore_requires_restart"] = set()
            hass.data["yidstore_requires_restart"].add(f"{o}/{r}".lower())

            return web.json_response({"success": True, "requires_restart": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)


class OnOffStoreReadmeView(HomeAssistantView):
    """API to fetch README."""
    url = "/api/yidstore/readme/{owner}/{repo}"
    name = "api:yidstore:readme"
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

            # Check if source is passed as query parameter
            source = request.query.get("source", "")

            # If repo is a GitHub custom repo or source=github, fetch README from GitHub
            is_github = source == "github"
            if not is_github and coordinator:
                for cr in coordinator.get_custom_repos():
                    if cr.get("source") == "github" and cr.get("owner", "").lower() == owner.lower() and cr.get("repo", "").lower() == repo.lower():
                        is_github = True
                        break

            if is_github:
                sess = async_get_clientsession(hass)
                for branch in ("main", "master"):
                    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/README.md"
                    try:
                        async with sess.get(raw_url, timeout=30) as resp:
                            if resp.status == 200:
                                return web.Response(text=await resp.text(), content_type="text/markdown")
                    except Exception:
                        continue
                return web.Response(text=f"{repo} has no instructions available.", content_type="text/markdown")

            txt = await client.get_readme(owner, repo)
            return web.Response(text=txt or f"{repo} has no instructions available.", content_type="text/markdown")
        except Exception:
            return web.Response(text="Error", status=500)


class OnOffStoreReleasesView(HomeAssistantView):
    """API to fetch all releases for a repository."""
    url = "/api/yidstore/releases/{owner}/{repo}"
    name = "api:yidstore:releases"
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

            # Check if source is passed as query parameter
            source = request.query.get("source", "")

            # Check if this is a GitHub repo
            is_github = source == "github"
            if not is_github and coordinator:
                for cr in coordinator.get_custom_repos():
                    if cr.get("source") == "github" and cr.get("owner", "").lower() == owner.lower() and cr.get("repo", "").lower() == repo.lower():
                        is_github = True
                        break

            if is_github:
                # Fetch releases from GitHub API
                github_releases = await _github_json(hass, f"https://api.github.com/repos/{owner}/{repo}/releases")
                out: list[dict] = []
                seen_tags: set[str] = set()

                if isinstance(github_releases, list):
                    for rel in github_releases[:30]:
                        tag = (rel.get("tag_name") or "").strip()
                        if not tag:
                            continue
                        seen_tags.add(tag)
                        out.append(
                            {
                                "tag_name": tag,
                                "name": rel.get("name", ""),
                                "body": rel.get("body", ""),
                                "published_at": rel.get("published_at", ""),
                                "created_at": rel.get("created_at", ""),
                                "prerelease": rel.get("prerelease", False),
                            }
                        )

                # Also include tags so older versions are available even when releases are missing.
                github_tags = await _github_json(hass, f"https://api.github.com/repos/{owner}/{repo}/tags?per_page=100")
                if isinstance(github_tags, list):
                    for tag_obj in github_tags:
                        tag = (tag_obj.get("name") or "").strip()
                        if not tag or tag in seen_tags:
                            continue
                        out.append(
                            {
                                "tag_name": tag,
                                "name": tag,
                                "body": "",
                                "published_at": "",
                                "created_at": "",
                                "prerelease": False,
                            }
                        )
                        seen_tags.add(tag)
                        if len(out) >= 100:
                            break

                return web.json_response(out)

            releases = await client.get_releases(owner, repo)
            return web.json_response(releases)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)



class OnOffStoreRefreshView(HomeAssistantView):
    """API to trigger update check."""
    url = "/api/yidstore/refresh"
    name = "api:yidstore:refresh"
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
    url = "/api/yidstore/custom/add"
    name = "api:yidstore:custom:add"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            body = await request.json()
            source = body.get("source", "gitea")
            repo_type = body.get("type") or ("audio" if str(body.get("owner", "")).strip().lower() == "audio" else "integration")
            repo_url = body.get("url")
            o, r = body.get("owner"), body.get("repo")

            if source == "github":
                if not repo_url:
                    return web.json_response({"error": "Missing GitHub URL"}, status=400)
                parsed = _parse_github_url(repo_url)
                if not parsed:
                    return web.json_response({"error": "Invalid GitHub URL"}, status=400)
                o, r = parsed
                if not body.get("type") and o.lower() == "audio":
                    repo_type = "audio"
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
    url = "/api/yidstore/custom/list"
    name = "api:yidstore:custom:list"
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
    url = "/api/yidstore/custom/remove"
    name = "api:yidstore:custom:remove"
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
    url = "/api/yidstore/hide"
    name = "api:yidstore:hide"
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
    url = "/api/yidstore/unhide"
    name = "api:yidstore:unhide"
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
    url = "/api/yidstore/uninstall"
    name = "api:yidstore:uninstall"
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
                await hass.async_add_executor_job(uninstall_package, hass, t, r, o)
                # 2. Remove tracking
                await coordinator.async_remove_package(o, r)

                # Remove from requires_restart if present
                if "yidstore_requires_restart" in hass.data:
                    hass.data["yidstore_requires_restart"].discard(f"{o}/{r}".lower())

                return web.json_response({"success": True})
            return web.json_response({"error": "Coordinator missing"}, status=503)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)


class OnOffStoreStatusView(HomeAssistantView):
    """Lightweight API to get install status for repos (for auto-refresh)."""
    url = "/api/yidstore/status"
    name = "api:yidstore:status"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request) -> web.Response:
        """Return lightweight status for all tracked repos."""
        hass = request.app["hass"]
        try:
            # Get current install state from disk
            local_state = await _collect_local_installed(hass)

            eid = self.entry_id
            if DOMAIN not in hass.data:
                return web.json_response({"error": "Not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if not eids:
                    return web.json_response({"error": "Not ready"}, status=503)
                eid = eids[0]

            coordinator = hass.data[DOMAIN][eid].get("coordinator")
            if not coordinator:
                return web.json_response({"error": "Coordinator missing"}, status=503)

            # Build status map for all known repos
            status = {}
            restart_list: list[str] = []
            _LOGGER.debug("Status API: checking %d packages", len(coordinator.packages))
            for pkg_id, pkg_data in coordinator.packages.items():
                owner = pkg_data.get("owner", "")
                repo = pkg_data.get("repo_name", "")
                pkg_type = pkg_data.get("package_type", "integration")
                domain = pkg_data.get("domain")
                _LOGGER.debug("Status API: checking package %s (owner=%s, repo=%s, type=%s)", pkg_id, owner, repo, pkg_type)

                disk_installed, disk_source = _get_install_info(
                    local_state=local_state,
                    pkg_type=pkg_type,
                    domain=domain,
                    repo_name=repo,
                    owner=owner,
                )

                key = f"{owner}/{repo}".lower()
                needs_restart = False
                if pkg_type == "integration":
                    # Source of truth: per-package Waiting Restart sensor state.
                    needs_restart = _waiting_restart_from_sensor(hass, repo)
                    _LOGGER.debug("Status API: package %s needs_restart=%s", pkg_id, needs_restart)
                if needs_restart:
                    restart_list.append(key)
                    _LOGGER.info("Status API: adding %s to restart_list", key)

                status[key] = {
                    "is_installed": True,  # Tracked by coordinator
                    "install_source": "yidstore",
                    "requires_restart": needs_restart,
                    "update_available": pkg_data.get("update_available", False),
                }

            return web.json_response({
                "status": status,
                "requires_restart_list": restart_list,
            })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)


class LocalBrandsIconView(HomeAssistantView):
    """Serve local brand icons for custom integrations."""
    url = "/api/yidstore/brands/{domain}/{filename}"
    name = "api:yidstore:brands"
    requires_auth = False

    async def get(self, request: web.Request, domain: str, filename: str) -> web.Response:
        """Serve icon file from local brands or custom_components folder."""
        hass = request.app["hass"]

        # Security: only allow specific filenames
        allowed_files = [
            'icon.png', 'icon@2x.png', 'dark_icon.png', 'dark_icon@2x.png',
            'logo.png', 'logo@2x.png', 'dark_logo.png', 'dark_logo@2x.png',
            'icon.svg', 'logo.svg'
        ]
        if filename not in allowed_files:
            return web.Response(status=404)

        # Security: sanitize domain name
        domain = domain.replace('..', '').replace('/', '').replace('\\', '')

        # Try multiple locations in order of preference
        locations = [
            os.path.join(hass.config.path("custom_components", domain, "brand"), filename),
            os.path.join(hass.config.path("custom_components", domain), filename),
            # Legacy fallback (read-only) for older installs.
            os.path.join(hass.config.path("www", "brands", domain), filename),
        ]

        def _read_file(file_path: str) -> bytes | None:
            """Read file in executor."""
            if os.path.exists(file_path) and os.path.isfile(file_path):
                with open(file_path, 'rb') as f:
                    return f.read()
            return None

        for path in locations:
            try:
                content = await hass.async_add_executor_job(_read_file, path)
                if content:
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


        # If not found locally, proxy from official Home Assistant Brands (same-origin for iframe/CSP).
        # Try HA brands GitHub repo custom_integrations path first, then brands CDN URL styles.
        remote_urls = [
            f"https://raw.githubusercontent.com/home-assistant/brands/master/custom_integrations/{domain}/{filename}",
            f"https://brands.home-assistant.io/_/{domain}/{filename}",
            f"https://brands.home-assistant.io/{domain}/{filename}",
        ]
        sess = async_get_clientsession(hass)
        for remote_url in remote_urls:
            try:
                async with sess.get(remote_url, timeout=15) as resp:
                    if resp.status != 200:
                        continue
                    content = await resp.read()
                    content_type = resp.headers.get("Content-Type") or (
                        "image/png" if filename.endswith(".png") else "image/svg+xml"
                    )
                    return web.Response(
                        body=content,
                        content_type=content_type,
                        headers={
                            "Cache-Control": "public, max-age=86400",
                            "Access-Control-Allow-Origin": "*",
                        },
                    )
            except Exception as e:
                _LOGGER.debug("Remote brands fetch failed for %s: %s", remote_url, e)

        return web.Response(status=404)


class LocalBrandsListView(HomeAssistantView):
    """List all available local brand icons."""
    url = "/api/yidstore/brands"
    name = "api:yidstore:brands_list"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Return list of domains with local icons."""
        hass = request.app["hass"]

        filenames = [
            "icon.png", "icon@2x.png", "dark_icon.png", "dark_icon@2x.png",
            "logo.png", "logo@2x.png", "dark_logo.png", "dark_logo@2x.png",
            "icon.svg", "logo.svg",
        ]

        def _scan_brand_dirs() -> dict[str, dict[str, str]]:
            """Scan brand directories in executor to avoid blocking event loop."""
            domains_with_icons: dict[str, dict[str, str]] = {}

            def _add(domain: str, filename: str) -> None:
                domains_with_icons.setdefault(domain, {})[filename] = f"/api/yidstore/brands/{domain}/{filename}"

            # Check custom_components folders
            cc_path = hass.config.path("custom_components")
            if os.path.exists(cc_path):
                for domain in os.listdir(cc_path):
                    domain_path = os.path.join(cc_path, domain)
                    if not os.path.isdir(domain_path):
                        continue
                    for fn in filenames:
                        # Prefer <domain>/brand/<file>, then fallback to <domain>/<file>.
                        if os.path.exists(os.path.join(domain_path, "brand", fn)):
                            _add(domain, fn)
                            continue
                        if os.path.exists(os.path.join(domain_path, fn)):
                            _add(domain, fn)

            # Legacy fallback (read-only): check www/brands.
            brands_path = hass.config.path("www", "brands")
            if os.path.exists(brands_path):
                for domain in os.listdir(brands_path):
                    domain_path = os.path.join(brands_path, domain)
                    if not os.path.isdir(domain_path):
                        continue
                    for fn in filenames:
                        if fn in domains_with_icons.get(domain, {}):
                            continue
                        if os.path.exists(os.path.join(domain_path, fn)):
                            _add(domain, fn)

            return domains_with_icons

        domains_with_icons = await hass.async_add_executor_job(_scan_brand_dirs)
        return web.json_response(domains_with_icons)



class LocalBrandsUploadView(HomeAssistantView):
    """Upload local brand icons for custom integrations."""
    url = "/api/yidstore/brands/upload"
    name = "api:yidstore:brands_upload"
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

        brands_dir = Path(hass.config.path("custom_components", domain, "brand"))

        def _save_brand_files():
            """Save brand files in executor to avoid blocking event loop."""
            brands_dir.mkdir(parents=True, exist_ok=True)
            for filename, data in files.items():
                dest = brands_dir / filename
                with open(dest, "wb") as f:
                    f.write(data)

        try:
            await hass.async_add_executor_job(_save_brand_files)
        except Exception as e:
            _LOGGER.error("Failed to save brand files for %s: %s", domain, e)
            return web.json_response({"error": "Failed to save branding."}, status=500)

        return web.json_response({"success": True, "domain": domain, "files": list(files.keys())})
