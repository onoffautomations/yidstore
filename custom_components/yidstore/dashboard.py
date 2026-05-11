"""Dashboard for YidStore - V4 Robust."""
from __future__ import annotations

import logging
import os
import re
import time
import asyncio
from datetime import datetime
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


def _waiting_restart_from_package_data(hass: HomeAssistant, package_data: dict | None) -> bool:
    """Return restart-needed flag from tracked package data."""
    if not isinstance(package_data, dict):
        return False

    last_update_str = package_data.get("last_update")
    if not last_update_str:
        return False

    try:
        last_update = datetime.fromisoformat(last_update_str)
    except Exception as e:
        _LOGGER.debug("Invalid last_update for %s: %s", package_data.get("repo_name"), e)
        return False

    ha_start_time = hass.data.get("homeassistant_start_time")
    if not ha_start_time:
        return False

    return last_update > ha_start_time


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
    media_audio_paths: set[str] = set()
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

    # B3) Installed audio packages in /media/audio/<owner>/<repo>/
    try:
        media_audio_root = Path(config_path) / "media" / "audio"
        if media_audio_root.is_dir():
            for owner_dir in media_audio_root.iterdir():
                if not owner_dir.is_dir():
                    continue
                owner_slug = _normalize_slug(owner_dir.name)
                for repo_dir in owner_dir.iterdir():
                    if not repo_dir.is_dir():
                        continue
                    repo_slug = _normalize_slug(repo_dir.name)
                    if owner_slug and repo_slug:
                        media_audio_paths.add(f"{owner_slug}/{repo_slug}")
    except Exception:
        pass

    # B) Installed frontend/community content (HACS typically puts cards/themes here)
    try:
        comm_path = Path(config_path) / "www" / "community"
        if comm_path.is_dir():
            for p in comm_path.iterdir():
                if p.is_dir():
                    community.add(_normalize_slug(p.name))
                    # YidStore installs cards under /www/community/<vendor>/<repo>/.
                    # Include one nested level so those repos are detected as installed.
                    for child in p.iterdir():
                        if child.is_dir():
                            community.add(_normalize_slug(child.name))
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
        "media_audio_paths": media_audio_paths,
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
    media_audio_paths = local_state.get("media_audio_paths", set()) if isinstance(local_state, dict) else set()
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
        if full_slug and (full_slug in audio_paths or full_slug in media_audio_paths):
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
    hass.http.register_view(DocumentationReposView(eid))
    hass.http.register_view(DocumentationFilesView(eid))
    hass.http.register_view(DocumentationContentView(eid))
    hass.http.register_view(AutomationsReposView(eid))
    hass.http.register_view(AutomationsFilesView(eid))
    hass.http.register_view(AutomationsContentView(eid))
    hass.http.register_view(DashboardsReposView(eid))
    hass.http.register_view(DashboardsFilesView(eid))
    hass.http.register_view(DashboardsContentView(eid))
    hass.http.register_view(HelpersReposView(eid))
    hass.http.register_view(HelpersFilesView(eid))
    hass.http.register_view(HelpersContentView(eid))
    hass.http.register_view(AudioReposView(eid))
    hass.http.register_view(AudioFilesView(eid))
    hass.http.register_view(AudioContentView(eid))
    hass.http.register_view(BlueprintsReposView(eid))
    hass.http.register_view(BlueprintsFilesView(eid))
    hass.http.register_view(BlueprintsContentView(eid))
    hass.http.register_view(AddAutomationView())
    hass.http.register_view(AddDashboardView())
    hass.http.register_view(AddHelperView())
    hass.http.register_view(ListHelpersView())

    # Initialize requires_restart set in hass.data
    if "yidstore_requires_restart" not in hass.data:
        hass.data["yidstore_requires_restart"] = set()

    # Setup the frontend brand patcher
    await async_setup_brand_patcher(hass)


# Module-level response cache for /api/yidstore/repos.
# Side-panel opens, multi-tab, and quick close-and-reopen all hit this and
# get an instant response instead of redoing the full collection pass.
# Mutating operations (install/uninstall/refresh/custom add/remove/hide)
# call _invalidate_repos_cache() to bust it.
_REPOS_CACHE: dict[str, tuple[float, list]] = {}
_REPOS_CACHE_TTL = 45.0  # seconds
_REPOS_CACHE_LOCK = asyncio.Lock()


def _invalidate_repos_cache(eid: str | None = None) -> None:
    """Drop cached /api/yidstore/repos response so the next call recomputes."""
    if eid is None:
        _REPOS_CACHE.clear()
    else:
        _REPOS_CACHE.pop(eid, None)


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

            # Serve from cache if fresh — coalesces concurrent requests.
            force = request.query.get("force") in ("1", "true", "yes")
            if not force:
                cached = _REPOS_CACHE.get(eid)
                if cached:
                    age = time.time() - cached[0]
                    if age < _REPOS_CACHE_TTL:
                        return web.json_response(cached[1])

            # Lock to coalesce simultaneous misses — first one computes,
            # rest re-check the cache before doing the work themselves.
            async with _REPOS_CACHE_LOCK:
                if not force:
                    cached = _REPOS_CACHE.get(eid)
                    if cached and (time.time() - cached[0]) < _REPOS_CACHE_TTL:
                        return web.json_response(cached[1])

                resp_data = await self._build_repos(hass, eid)
                _REPOS_CACHE[eid] = (time.time(), resp_data)
                return web.json_response(resp_data)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _build_repos(self, hass, eid):
        # Raises on failure so we don't cache a broken empty list.
        try:
            comp = hass.data[DOMAIN][eid]
            client = comp["client"]
            coordinator = comp["coordinator"]

            local_state_task = asyncio.create_task(_collect_local_installed(hass))
            yaml_items_task = hass.async_add_executor_job(load_store_list, hass)
            github_integrations_task = asyncio.create_task(_fetch_github_integrations_list(hass, client))
            auth_task = asyncio.create_task(client.test_auth()) if client.token else None

            # Check if we have a valid authenticated session
            is_authenticated = False
            if auth_task:
                is_authenticated = await auth_task
                if not is_authenticated:
                    _LOGGER.warning("Gitea token provided but authentication failed (expired or revoked). Using public access.")

            # A. Load from store_list.yaml first (Ensures these are ALWAYS visible)
            yaml_items, local_state, github_integrations_list = await asyncio.gather(
                yaml_items_task,
                local_state_task,
                github_integrations_task,
            )

            # 1. Fetch custom repos FIRST (will be processed at end if missed)
            custom_repos_to_fetch = list(coordinator.custom_repos)

            # 1b. Fetch GitHub integrations from the Github-Integrations repo
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
                # Skip repos from orgs that have their own tabs (Documentation, Automations, Dashboards, Helpers)
                owner = repo_obj.get("owner", {}).get("login", "")
                if owner in HIDDEN_STORE_ORGS:
                    return
                seen_repos.add(full_name)
                tasks.append(self._process_repo(repo_obj, coordinator, yaml_items, bypass, auth, local_state))
            
            # === PARALLEL COLLECTION PHASE ===
            # Previously every fetch below was sequential (one YAML repo, then
            # one org, then one user, etc.). With even a handful of orgs/users
            # that turned into dozens of round-trips in series. Now we fan
            # everything out and only await the dependencies we truly need.

            async def _safe_get_repo(owner_, repo_):
                try:
                    return await client.get_repo(owner_, repo_)
                except Exception as e:
                    _LOGGER.debug("Store: YAML repo %s/%s fetch failed: %s", owner_, repo_, e)
                    return None

            yaml_fetch_task = asyncio.gather(*[_safe_get_repo(y["owner"], y["repo"]) for y in yaml_items])

            # Kick off search immediately — it's the slowest single call and now paginates.
            search_task = asyncio.create_task(client.search_repos(limit=500))

            # Authenticated metadata (orgs membership + following) can run in parallel too.
            user_orgs_task = asyncio.create_task(client.get_user_orgs()) if is_authenticated else None
            following_task = asyncio.create_task(client.get_user_following()) if is_authenticated else None

            # Authenticated user's own repos
            async def _fetch_own_repos():
                try:
                    sess = async_get_clientsession(hass)
                    async with sess.get(f"{client.base_url}/api/v1/user/repos", headers=client._headers()) as resp:
                        if resp.status == 200:
                            return await resp.json()
                except Exception as e:
                    _LOGGER.debug("Failed to fetch own repos: %s", e)
                return None
            own_repos_task = asyncio.create_task(_fetch_own_repos()) if is_authenticated else None

            # B. Determine which orgs to fetch from
            default_orgs = ["Zing", "OnOffPublic"]
            orgs_to_fetch = set(default_orgs)
            if user_orgs_task is not None:
                try:
                    user_orgs = await user_orgs_task
                    for org in user_orgs:
                        org_name = org.get("username") or org.get("name")
                        if org_name and org_name not in HIDDEN_STORE_ORGS:
                            orgs_to_fetch.add(org_name)
                    _LOGGER.debug("Fetching repos from %d organizations", len(orgs_to_fetch))
                except Exception as e:
                    _LOGGER.debug("Failed to fetch user orgs: %s", e)

            orgs_to_fetch = orgs_to_fetch - HIDDEN_STORE_ORGS

            # Fan out org repos + org members concurrently across ALL orgs.
            org_repos_tasks = {o: asyncio.create_task(client.get_org_repos(o)) for o in orgs_to_fetch}
            org_members_tasks = (
                {o: asyncio.create_task(client.get_org_members(o)) for o in orgs_to_fetch}
                if is_authenticated else {}
            )

            # Collect YAML repos as soon as they're all back.
            yaml_results = await yaml_fetch_task
            for r in yaml_results:
                if r:
                    add_repo_task(r, bypass=True, auth=is_authenticated)

            # Collect org repos
            users_from_orgs = set()
            for o, task in org_repos_tasks.items():
                try:
                    repos = await task
                    if isinstance(repos, list):
                        for r in repos:
                            add_repo_task(r, auth=is_authenticated)
                except Exception as e:
                    _LOGGER.debug("Store: Org %s repos error: %s", o, e)

            for o, task in org_members_tasks.items():
                try:
                    members = await task
                    if isinstance(members, list):
                        for member in members:
                            username = member.get("username") or member.get("login")
                            if username:
                                users_from_orgs.add(username)
                except Exception as e:
                    _LOGGER.debug("Store: Org %s members error: %s", o, e)

            # C. Determine which users to fetch from
            users_to_fetch = set()
            if is_authenticated:
                users_to_fetch.update(users_from_orgs)
                if following_task is not None:
                    try:
                        following = await following_task
                        for user in following:
                            username = user.get("login") or user.get("username")
                            if username:
                                users_to_fetch.add(username)
                    except Exception as e:
                        _LOGGER.debug("Failed to fetch following users: %s", e)
                _LOGGER.debug("Fetching repos from %d users (org members + following)", len(users_to_fetch))

            # Fan out user repos
            user_repos_tasks = {u: asyncio.create_task(client.get_user_repos(u)) for u in users_to_fetch}
            for u, task in user_repos_tasks.items():
                try:
                    repos = await task
                    if isinstance(repos, list):
                        for r in repos:
                            add_repo_task(r, auth=is_authenticated)
                except Exception as e:
                    _LOGGER.debug("Store: User %s error: %s", u, e)

            # D. Authenticated user's own repos
            if own_repos_task is not None:
                try:
                    u_repos = await own_repos_task
                    if isinstance(u_repos, list):
                        for repo in u_repos:
                            add_repo_task(repo, bypass=True, auth=True)
                except Exception as e:
                    _LOGGER.debug("Store: Own repos error: %s", e)

            # E. Global search (now paginated — previously silently capped at one page)
            try:
                search_repos = await search_task
                if isinstance(search_repos, list):
                    _LOGGER.debug("Store: search returned %d repos", len(search_repos))
                    for repo in search_repos:
                        add_repo_task(repo, auth=is_authenticated)
            except Exception as e:
                _LOGGER.debug("Store: Search repos error: %s", e)

            # Execute all collected tasks in parallel
            processed_results = await asyncio.gather(*tasks)
            resp_data = [res for res in processed_results if res is not None]

            # F. Explicitly fetch custom repos if they weren't in organizations or users.
            # Process all the misses in parallel — GitHub custom repos do a slow
            # domain resolution that used to block the whole response.
            existing_keys = {(x["owner"].lower(), x["repo_name"].lower()) for x in resp_data}
            missing_custom = []
            for cr in custom_repos_to_fetch:
                owner = (cr.get("owner") or "").strip()
                repo = (cr.get("repo") or "").strip()
                if not owner or not repo:
                    continue
                if (owner.lower(), repo.lower()) in existing_keys:
                    continue
                missing_custom.append((cr, owner, repo))

            async def _process_missing_custom(cr, owner, repo):
                source = cr.get("source", "gitea")
                if source == "github":
                    repo_type = cr.get("type") or ("audio" if owner.lower() == "audio" else "integration")
                    repo_url = cr.get("url")
                    domain = None
                    if repo_type == "integration":
                        domain = await _resolve_github_integration_domain(hass, owner, repo)

                    icon_url = _github_brand_icon_url(owner, repo, domain, "main")

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

                    return {
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
                    }

                # Gitea custom repo
                try:
                    r = await client.get_repo(owner, repo)
                    if r:
                        item = await self._process_repo(r, coordinator, yaml_items, bypass=True, auth=is_authenticated, local_state=local_state)
                        if item:
                            return item
                except Exception as e:
                    _LOGGER.debug("Custom repo %s/%s metadata fetch failed, adding fallback row: %s", owner, repo, e)

                repo_type = cr.get("type") or ("audio" if owner.lower() == "audio" else "integration")
                tracked_pkg = coordinator.get_package_by_repo(owner, repo)
                domain = cr.get("domain")
                disk_installed, disk_source = _get_install_info(
                    local_state=local_state,
                    pkg_type=repo_type,
                    domain=domain,
                    repo_name=repo,
                    owner=owner,
                )
                return {
                    "name": f"{owner}/{repo}",
                    "repo_name": repo,
                    "owner": owner,
                    "owner_display_name": owner,
                    "type": repo_type,
                    "description": "",
                    "updated_at": "",
                    "mode": tracked_pkg.get("mode") if tracked_pkg else cr.get("mode"),
                    "asset_name": tracked_pkg.get("asset_name") if tracked_pkg else cr.get("asset_name"),
                    "is_installed": tracked_pkg is not None or disk_installed,
                    "install_source": "yidstore" if tracked_pkg is not None else disk_source,
                    "update_available": tracked_pkg.get("update_available", False) if tracked_pkg else False,
                    "latest_version": tracked_pkg.get("latest_version") if tracked_pkg else None,
                    "release_notes": tracked_pkg.get("release_notes") if tracked_pkg else None,
                    "is_hidden": coordinator.is_hidden_repo(owner, repo),
                    "icon_url": None,
                    "domain": domain,
                    "default_branch": cr.get("default_branch", "main"),
                    "source": "gitea",
                }

            if missing_custom:
                custom_results = await asyncio.gather(
                    *[_process_missing_custom(cr, owner, repo) for cr, owner, repo in missing_custom],
                    return_exceptions=True,
                )
                for item in custom_results:
                    if isinstance(item, Exception):
                        _LOGGER.debug("Custom repo processing raised: %s", item)
                        continue
                    if item is not None:
                        resp_data.append(item)

            return resp_data
        except Exception:
            # Let the caller decide what to do — never cache a partial result.
            raise

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

        # Type detection — layout is the source of truth.
        #
        # Name/description hints alone are unreliable: a repo named
        # "discord_card_integration" or one whose description mentions
        # "theme" can be a real integration. So whenever there is no
        # definitive answer from the coordinator (installed package
        # metadata) or the special "audio" owner, we always check the
        # actual repo layout. If layout is inconclusive, only then do we
        # fall back to name/desc hints.
        pkg_type = None
        desc = (r.get("description") or "").lower()
        if p:
            pkg_type = p.get("package_type", "integration")
        elif owner.lower() == "audio":
            pkg_type = "audio"

        if pkg_type is None:
            # Compute the name/desc hint up-front but only use it if the
            # layout check can't give a definitive answer.
            name_hint = None
            if "blueprint" in rn.lower() or "blueprint" in desc:
                name_hint = "blueprints"
            elif "card" in rn.lower() or "lovelace" in rn.lower() or "card" in desc or "theme" in desc:
                name_hint = "lovelace"

            # Fire both directory listings in parallel — root layout (for
            # type detection) and custom_components/ (for integration
            # domains). For ~most repos in this store these are both needed;
            # for non-integrations the second call is a cheap miss but we
            # save a round-trip on the critical path.
            root_task = asyncio.create_task(
                coord.client.list_dir(owner, rn, path="", branch=default_branch)
            )
            cc_task = asyncio.create_task(
                coord.client.get_integration_domains(owner, rn, branch=default_branch)
            )

            try:
                root_entries = await root_task
            except Exception:
                root_entries = None
            try:
                speculative_domains = await cc_task
            except Exception:
                speculative_domains = []
            if not isinstance(speculative_domains, list):
                speculative_domains = []

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

                # Layout takes precedence over name/desc — custom_components/
                # presence definitively means integration even if the repo
                # name or description mentions "card" or "theme".
                if has_custom_components:
                    pkg_type = "integration"
                elif has_blueprints:
                    pkg_type = "blueprints"
                elif has_root_js:
                    pkg_type = "lovelace"
                else:
                    pkg_type = name_hint or "integration"
            else:
                # Layout fetch failed — fall back to hint, default to integration
                pkg_type = name_hint or "integration"
        else:
            speculative_domains = None  # Not fetched on this path

        # Get display name from owner object if available
        owner_obj = r.get("owner", {})
        owner_display_name = owner_obj.get("full_name") or owner_obj.get("username") or owner

        # Network-heavy work is now ONLY done for integration repos. For
        # cards/audio/blueprints, none of this matters and used to be wasted
        # round-trips that blocked the whole response.
        icon_url = None
        domain_from_manifest = None

        if pkg_type == "integration":
            # Reuse the speculative custom_components/ listing if we already
            # fetched it in parallel above; otherwise fetch now.
            if speculative_domains is not None:
                domains = speculative_domains
            else:
                try:
                    domains = await coord.client.get_integration_domains(owner, rn, branch=default_branch)
                except Exception:
                    domains = []
                if not isinstance(domains, list):
                    domains = []

            # Prefer a domain that is already installed locally
            chosen_domain = None
            if domains:
                local_cc = Path("/config/custom_components")
                for d in domains:
                    try:
                        if (local_cc / d).is_dir():
                            chosen_domain = d
                            break
                    except Exception:
                        continue
                if not chosen_domain:
                    chosen_domain = domains[0]

            # Use dir name as domain. This matches what's on disk (what
            # install detection checks) and avoids an extra manifest fetch.
            # In the rare case where manifest["domain"] differs from the
            # folder name, install detection still works because the folder
            # name is canonical for "is it installed?".
            domain_from_manifest = chosen_domain

            # Best-guess icon URL — frontend handles fallback if it 404s.
            try:
                icon_url = await coord.client.get_icon_url(
                    owner, rn, branch=default_branch, domains=domains
                )
            except Exception:
                icon_url = None

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
            audio_location = (body.get("audio_location") or "www").strip().lower()
            
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
            if t == "audio":
                if audio_location not in {"www", "media"}:
                    return web.json_response({"error": "Invalid audio_location. Use 'www' or 'media'."}, status=400)
                svc_data["audio_location"] = audio_location
            
            # Support for installing specific versions (passed as tag to service)
            version = body.get("version")
            if version: svc_data["tag"] = version

            await hass.services.async_call(DOMAIN, SERVICE_INSTALL, svc_data, blocking=True)

            # Track that this repo requires restart
            if "yidstore_requires_restart" not in hass.data:
                hass.data["yidstore_requires_restart"] = set()
            hass.data["yidstore_requires_restart"].add(f"{o}/{r}".lower())

            _invalidate_repos_cache()
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
                _invalidate_repos_cache()
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
                _invalidate_repos_cache()
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
                _invalidate_repos_cache()
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
                _invalidate_repos_cache()
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
                _invalidate_repos_cache()
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
                tracked_pkg = coordinator.get_package_by_repo(o, r) or {}
                domain = tracked_pkg.get("domain")
                # 1. Delete folder
                await hass.async_add_executor_job(uninstall_package, hass, t, r, o, domain)
                # 2. Remove tracking
                await coordinator.async_remove_package(o, r)

                # Remove from requires_restart if present
                if "yidstore_requires_restart" in hass.data:
                    hass.data["yidstore_requires_restart"].discard(f"{o}/{r}".lower())

                _invalidate_repos_cache()
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
                    # Prefer direct computation from tracked package data so status
                    # updates immediately after install/update, even before the
                    # entity state machine catches up.
                    needs_restart = _waiting_restart_from_package_data(hass, pkg_data)
                    if not needs_restart:
                        needs_restart = _waiting_restart_from_sensor(hass, repo)
                    _LOGGER.debug("Status API: package %s needs_restart=%s", pkg_id, needs_restart)
                if needs_restart:
                    restart_list.append(key)
                    _LOGGER.info("Status API: adding %s to restart_list", key)

                status[key] = {
                    "is_installed": disk_installed,
                    "install_source": "yidstore" if disk_installed else None,
                    "requires_restart": needs_restart,
                    "update_available": pkg_data.get("update_available", False) if disk_installed else False,
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


# Documentation Organization name
DOCUMENTATION_ORG = "Documentation"


class DocumentationReposView(HomeAssistantView):
    """API to list documentation repositories from the Documentation organization."""
    url = "/api/yidstore/docs"
    name = "api:yidstore:docs"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            eid = self.entry_id
            if DOMAIN not in hass.data:
                return web.json_response({"error": "Integration not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if not eids:
                    return web.json_response({"error": "Integration not ready"}, status=503)
                eid = eids[0]

            client = hass.data[DOMAIN][eid]["client"]

            # Fetch repos from Documentation organization
            repos = await client.get_org_repos(DOCUMENTATION_ORG)

            result = []
            for repo in repos:
                if not isinstance(repo, dict):
                    continue

                # Skip archived repos
                if repo.get("archived", False):
                    continue

                owner = repo.get("owner", {}).get("login", DOCUMENTATION_ORG)
                repo_name = repo.get("name", "")
                description = repo.get("description", "")
                updated_at = repo.get("updated_at", "")
                default_branch = repo.get("default_branch", "main")

                result.append({
                    "name": f"{owner}/{repo_name}",
                    "owner": owner,
                    "repo_name": repo_name,
                    "description": description,
                    "updated_at": updated_at,
                    "default_branch": default_branch,
                })

            return web.json_response(result)
        except Exception as e:
            _LOGGER.error("Error fetching documentation repos: %s", e)
            return web.json_response({"error": str(e)}, status=500)


class DocumentationFilesView(HomeAssistantView):
    """API to list files in a documentation repository."""
    url = "/api/yidstore/docs/{owner}/{repo}/files"
    name = "api:yidstore:docs:files"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request, owner: str, repo: str) -> web.Response:
        hass = request.app["hass"]
        try:
            eid = self.entry_id
            if DOMAIN not in hass.data:
                return web.json_response({"error": "Integration not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if not eids:
                    return web.json_response({"error": "Integration not ready"}, status=503)
                eid = eids[0]

            client = hass.data[DOMAIN][eid]["client"]

            # Get path parameter (for subdirectories)
            path = request.query.get("path", "")
            branch = request.query.get("branch", "main")

            # List directory with file info
            entries = await client.list_dir_recursive(owner, repo, path, branch)

            # Filter to only show documentation files and directories
            result = []
            for entry in entries:
                entry_type = entry.get("type", "")
                name = entry.get("name", "")

                if entry_type == "dir":
                    result.append(entry)
                elif entry_type == "file":
                    # Only include documentation files
                    lower_name = name.lower()
                    if lower_name.endswith(('.md', '.html', '.htm', '.txt')):
                        result.append(entry)

            return web.json_response(result)
        except Exception as e:
            _LOGGER.error("Error fetching documentation files for %s/%s: %s", owner, repo, e)
            return web.json_response({"error": str(e)}, status=500)


class DocumentationContentView(HomeAssistantView):
    """API to get content of a documentation file."""
    url = "/api/yidstore/docs/{owner}/{repo}/content"
    name = "api:yidstore:docs:content"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request, owner: str, repo: str) -> web.Response:
        hass = request.app["hass"]
        try:
            eid = self.entry_id
            if DOMAIN not in hass.data:
                return web.json_response({"error": "Integration not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if not eids:
                    return web.json_response({"error": "Integration not ready"}, status=503)
                eid = eids[0]

            client = hass.data[DOMAIN][eid]["client"]

            # Get file path from query parameter
            file_path = request.query.get("path", "")
            branch = request.query.get("branch", "main")

            if not file_path:
                return web.json_response({"error": "Missing path parameter"}, status=400)

            # Get file content with history
            file_info = await client.get_file_info_with_history(owner, repo, file_path, branch)

            if file_info is None:
                return web.json_response({"error": "File not found"}, status=404)

            return web.json_response({
                "content": file_info.get("content", ""),
                "file_path": file_info.get("file_path", ""),
                "last_modified_by": file_info.get("last_modified_by"),
                "last_modified_at": file_info.get("last_modified_at"),
                "commit_message": file_info.get("commit_message"),
            })
        except Exception as e:
            _LOGGER.error("Error fetching documentation content for %s/%s: %s", owner, repo, e)
            return web.json_response({"error": str(e)}, status=500)


# Automations Organization name
AUTOMATIONS_ORG = "Automations"

# Dashboards Organization name
DASHBOARDS_ORG = "Dashboards"

# Helpers Organization name
HELPERS_ORG = "Helpers"

# Audio Organization name
AUDIO_ORG = "Audio"

# Blueprints Organization name
BLUEPRINTS_ORG = "Blueprints"

# Organizations to hide from the Store view (they have their own tabs)
HIDDEN_STORE_ORGS = {"Documentation", "Automations", "Dashboards", "Helpers", "Audio", "Blueprints"}


class AutomationsReposView(HomeAssistantView):
    """API to list automation repositories from the Automations organization."""
    url = "/api/yidstore/automations"
    name = "api:yidstore:automations"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            eid = self.entry_id
            if DOMAIN not in hass.data:
                return web.json_response({"error": "Integration not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if not eids:
                    return web.json_response({"error": "Integration not ready"}, status=503)
                eid = eids[0]

            client = hass.data[DOMAIN][eid]["client"]

            # Fetch repos from Automations organization
            repos = await client.get_org_repos(AUTOMATIONS_ORG)

            result = []
            for repo in repos:
                if not isinstance(repo, dict):
                    continue

                if repo.get("archived", False):
                    continue

                owner = repo.get("owner", {}).get("login", AUTOMATIONS_ORG)
                repo_name = repo.get("name", "")
                description = repo.get("description", "")
                updated_at = repo.get("updated_at", "")
                default_branch = repo.get("default_branch", "main")

                result.append({
                    "name": f"{owner}/{repo_name}",
                    "owner": owner,
                    "repo_name": repo_name,
                    "description": description,
                    "updated_at": updated_at,
                    "default_branch": default_branch,
                })

            return web.json_response(result)
        except Exception as e:
            _LOGGER.error("Error fetching automation repos: %s", e)
            return web.json_response({"error": str(e)}, status=500)


class AutomationsFilesView(HomeAssistantView):
    """API to list YAML files in an automations repository."""
    url = "/api/yidstore/automations/{owner}/{repo}/files"
    name = "api:yidstore:automations:files"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request, owner: str, repo: str) -> web.Response:
        hass = request.app["hass"]
        try:
            eid = self.entry_id
            if DOMAIN not in hass.data:
                return web.json_response({"error": "Integration not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if not eids:
                    return web.json_response({"error": "Integration not ready"}, status=503)
                eid = eids[0]

            client = hass.data[DOMAIN][eid]["client"]

            path = request.query.get("path", "")
            branch = request.query.get("branch", "main")

            entries = await client.list_dir_recursive(owner, repo, path, branch)

            result = []
            for entry in entries:
                entry_type = entry.get("type", "")
                name = entry.get("name", "")

                if entry_type == "dir":
                    result.append(entry)
                elif entry_type == "file":
                    lower_name = name.lower()
                    # Include YAML files and their potential instruction files
                    if lower_name.endswith(('.yaml', '.yml', '.md', '.html', '.htm')):
                        result.append(entry)

            return web.json_response(result)
        except Exception as e:
            _LOGGER.error("Error fetching automation files for %s/%s: %s", owner, repo, e)
            return web.json_response({"error": str(e)}, status=500)


class AutomationsContentView(HomeAssistantView):
    """API to get content of an automation YAML file with linked instructions."""
    url = "/api/yidstore/automations/{owner}/{repo}/content"
    name = "api:yidstore:automations:content"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request, owner: str, repo: str) -> web.Response:
        hass = request.app["hass"]
        try:
            eid = self.entry_id
            if DOMAIN not in hass.data:
                return web.json_response({"error": "Integration not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if not eids:
                    return web.json_response({"error": "Integration not ready"}, status=503)
                eid = eids[0]

            client = hass.data[DOMAIN][eid]["client"]

            file_path = request.query.get("path", "")
            branch = request.query.get("branch", "main")

            if not file_path:
                return web.json_response({"error": "Missing path parameter"}, status=400)

            # Get YAML file content
            file_info = await client.get_file_info_with_history(owner, repo, file_path, branch)

            if file_info is None:
                return web.json_response({"error": "File not found"}, status=404)

            # Try to find linked instructions file (same name but .md or .html)
            base_path = file_path.rsplit('.', 1)[0] if '.' in file_path else file_path
            base_name = base_path.rsplit('/', 1)[-1] if '/' in base_path else base_path
            dir_path = base_path.rsplit('/', 1)[0] if '/' in base_path else ""
            instructions_content = None
            instructions_path = None

            # First try exact match
            for ext in ['.md', '.html', '.htm']:
                try_path = base_path + ext
                instr_info = await client.get_file_info_with_history(owner, repo, try_path, branch)
                if instr_info and instr_info.get("content"):
                    instructions_content = instr_info.get("content")
                    instructions_path = try_path
                    break

            # If no exact match, try case-insensitive search in the directory
            if not instructions_content:
                dir_entries = await client.list_dir(owner, repo, dir_path, branch)
                base_name_lower = base_name.lower()
                for entry in dir_entries:
                    if entry.get("type") != "file":
                        continue
                    entry_name = entry.get("name", "")
                    entry_lower = entry_name.lower()
                    # Check if this file starts with the base name (case-insensitive) and has instruction extension
                    if entry_lower.endswith(('.md', '.html', '.htm')):
                        entry_base = entry_lower.rsplit('.', 1)[0]
                        # Match if base names are similar (exact match or starts with)
                        if entry_base == base_name_lower or entry_base.startswith(base_name_lower) or base_name_lower.startswith(entry_base):
                            try_path = f"{dir_path}/{entry_name}" if dir_path else entry_name
                            instr_info = await client.get_file_info_with_history(owner, repo, try_path, branch)
                            if instr_info and instr_info.get("content"):
                                instructions_content = instr_info.get("content")
                                instructions_path = try_path
                                break

            return web.json_response({
                "content": file_info.get("content", ""),
                "file_path": file_info.get("file_path", ""),
                "last_modified_by": file_info.get("last_modified_by"),
                "last_modified_at": file_info.get("last_modified_at"),
                "instructions": instructions_content,
                "instructions_path": instructions_path,
            })
        except Exception as e:
            _LOGGER.error("Error fetching automation content for %s/%s: %s", owner, repo, e)
            return web.json_response({"error": str(e)}, status=500)


class DashboardsReposView(HomeAssistantView):
    """API to list dashboard repositories from the Dashboards organization or user."""
    url = "/api/yidstore/dashboards"
    name = "api:yidstore:dashboards"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            eid = self.entry_id
            if DOMAIN not in hass.data:
                return web.json_response({"error": "Integration not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if not eids:
                    return web.json_response({"error": "Integration not ready"}, status=503)
                eid = eids[0]

            client = hass.data[DOMAIN][eid]["client"]

            # Try org first, fall back to user repos if org doesn't exist
            repos = await client.get_org_repos(DASHBOARDS_ORG)
            if not repos:
                repos = await client.get_user_repos(DASHBOARDS_ORG)

            result = []
            for repo in repos:
                if not isinstance(repo, dict):
                    continue

                if repo.get("archived", False):
                    continue

                owner = repo.get("owner", {}).get("login", DASHBOARDS_ORG)
                repo_name = repo.get("name", "")
                description = repo.get("description", "")
                updated_at = repo.get("updated_at", "")
                default_branch = repo.get("default_branch", "main")

                result.append({
                    "name": f"{owner}/{repo_name}",
                    "owner": owner,
                    "repo_name": repo_name,
                    "description": description,
                    "updated_at": updated_at,
                    "default_branch": default_branch,
                })

            return web.json_response(result)
        except Exception as e:
            _LOGGER.error("Error fetching dashboard repos: %s", e)
            return web.json_response({"error": str(e)}, status=500)


class DashboardsFilesView(HomeAssistantView):
    """API to list YAML files in a dashboards repository."""
    url = "/api/yidstore/dashboards/{owner}/{repo}/files"
    name = "api:yidstore:dashboards:files"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request, owner: str, repo: str) -> web.Response:
        hass = request.app["hass"]
        try:
            eid = self.entry_id
            if DOMAIN not in hass.data:
                return web.json_response({"error": "Integration not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if not eids:
                    return web.json_response({"error": "Integration not ready"}, status=503)
                eid = eids[0]

            client = hass.data[DOMAIN][eid]["client"]

            path = request.query.get("path", "")
            branch = request.query.get("branch", "main")

            entries = await client.list_dir_recursive(owner, repo, path, branch)

            result = []
            for entry in entries:
                entry_type = entry.get("type", "")
                name = entry.get("name", "")

                if entry_type == "dir":
                    result.append(entry)
                elif entry_type == "file":
                    lower_name = name.lower()
                    if lower_name.endswith(('.yaml', '.yml', '.md', '.html', '.htm')):
                        result.append(entry)

            return web.json_response(result)
        except Exception as e:
            _LOGGER.error("Error fetching dashboard files for %s/%s: %s", owner, repo, e)
            return web.json_response({"error": str(e)}, status=500)


class DashboardsContentView(HomeAssistantView):
    """API to get content of a dashboard YAML file with linked instructions."""
    url = "/api/yidstore/dashboards/{owner}/{repo}/content"
    name = "api:yidstore:dashboards:content"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request, owner: str, repo: str) -> web.Response:
        hass = request.app["hass"]
        try:
            eid = self.entry_id
            if DOMAIN not in hass.data:
                return web.json_response({"error": "Integration not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if not eids:
                    return web.json_response({"error": "Integration not ready"}, status=503)
                eid = eids[0]

            client = hass.data[DOMAIN][eid]["client"]

            file_path = request.query.get("path", "")
            branch = request.query.get("branch", "main")

            if not file_path:
                return web.json_response({"error": "Missing path parameter"}, status=400)

            file_info = await client.get_file_info_with_history(owner, repo, file_path, branch)

            if file_info is None:
                return web.json_response({"error": "File not found"}, status=404)

            # Try to find linked instructions file
            base_path = file_path.rsplit('.', 1)[0] if '.' in file_path else file_path
            base_name = base_path.rsplit('/', 1)[-1] if '/' in base_path else base_path
            dir_path = base_path.rsplit('/', 1)[0] if '/' in base_path else ""
            instructions_content = None
            instructions_path = None

            # First try exact match
            for ext in ['.md', '.html', '.htm']:
                try_path = base_path + ext
                instr_info = await client.get_file_info_with_history(owner, repo, try_path, branch)
                if instr_info and instr_info.get("content"):
                    instructions_content = instr_info.get("content")
                    instructions_path = try_path
                    break

            # If no exact match, try case-insensitive search in the directory
            if not instructions_content:
                dir_entries = await client.list_dir(owner, repo, dir_path, branch)
                base_name_lower = base_name.lower()
                for entry in dir_entries:
                    if entry.get("type") != "file":
                        continue
                    entry_name = entry.get("name", "")
                    entry_lower = entry_name.lower()
                    # Check if this file starts with the base name (case-insensitive) and has instruction extension
                    if entry_lower.endswith(('.md', '.html', '.htm')):
                        entry_base = entry_lower.rsplit('.', 1)[0]
                        # Match if base names are similar (exact match or starts with)
                        if entry_base == base_name_lower or entry_base.startswith(base_name_lower) or base_name_lower.startswith(entry_base):
                            try_path = f"{dir_path}/{entry_name}" if dir_path else entry_name
                            instr_info = await client.get_file_info_with_history(owner, repo, try_path, branch)
                            if instr_info and instr_info.get("content"):
                                instructions_content = instr_info.get("content")
                                instructions_path = try_path
                                break

            return web.json_response({
                "content": file_info.get("content", ""),
                "file_path": file_info.get("file_path", ""),
                "last_modified_by": file_info.get("last_modified_by"),
                "last_modified_at": file_info.get("last_modified_at"),
                "instructions": instructions_content,
                "instructions_path": instructions_path,
            })
        except Exception as e:
            _LOGGER.error("Error fetching dashboard content for %s/%s: %s", owner, repo, e)
            return web.json_response({"error": str(e)}, status=500)


class AddAutomationView(HomeAssistantView):
    """API to add an automation from YAML."""
    url = "/api/yidstore/add_automation"
    name = "api:yidstore:add_automation"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            data = await request.json()
            yaml_content = data.get("yaml", "")
            name = data.get("name", "New Automation")

            if not yaml_content:
                return web.json_response({"error": "No YAML content provided"}, status=400)

            import yaml
            import uuid

            # Parse YAML
            try:
                automation_config = yaml.safe_load(yaml_content)
            except yaml.YAMLError as e:
                return web.json_response({"error": f"Invalid YAML: {e}"}, status=400)

            # Generate unique ID if not present
            if "id" not in automation_config:
                automation_config["id"] = str(uuid.uuid4()).replace("-", "")[:12]

            # Set alias if not present
            if "alias" not in automation_config:
                automation_config["alias"] = name

            # Call HA service to create automation
            await hass.services.async_call(
                "automation",
                "reload",
                {},
                blocking=True
            )

            # Write to automations.yaml
            automations_path = Path(hass.config.path("automations.yaml"))
            existing = []
            if automations_path.exists():
                content = automations_path.read_text()
                if content.strip():
                    try:
                        existing = yaml.safe_load(content) or []
                        if not isinstance(existing, list):
                            existing = [existing]
                    except:
                        existing = []

            existing.append(automation_config)

            with open(automations_path, "w") as f:
                yaml.dump(existing, f, default_flow_style=False, allow_unicode=True)

            # Reload automations
            await hass.services.async_call(
                "automation",
                "reload",
                {},
                blocking=True
            )

            return web.json_response({"success": True, "id": automation_config["id"]})
        except Exception as e:
            _LOGGER.error("Error adding automation: %s", e)
            return web.json_response({"error": str(e)}, status=500)


class AddDashboardView(HomeAssistantView):
    """API to add a dashboard from YAML."""
    url = "/api/yidstore/add_dashboard"
    name = "api:yidstore:add_dashboard"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            data = await request.json()
            yaml_content = data.get("yaml", "")
            name = data.get("name", "New Dashboard")
            slug = data.get("slug", "custom-dashboard")

            if not yaml_content:
                return web.json_response({"error": "No YAML content provided"}, status=400)

            import yaml

            # Parse YAML
            try:
                dashboard_config = yaml.safe_load(yaml_content)
            except yaml.YAMLError as e:
                return web.json_response({"error": f"Invalid YAML: {e}"}, status=400)

            # Create lovelace dashboard storage file
            storage_path = Path(hass.config.path(f".storage/lovelace.lovelace_{slug}"))

            # Create storage entry
            storage_data = {
                "version": 1,
                "minor_version": 1,
                "key": f"lovelace.lovelace_{slug}",
                "data": {"config": dashboard_config}
            }

            import json
            with open(storage_path, "w") as f:
                json.dump(storage_data, f, indent=2)

            # Register dashboard in lovelace_dashboards storage
            dashboards_storage_path = Path(hass.config.path(".storage/lovelace_dashboards"))
            dashboards_data = {"version": 1, "minor_version": 1, "key": "lovelace_dashboards", "data": {"items": []}}

            if dashboards_storage_path.exists():
                with open(dashboards_storage_path, "r") as f:
                    dashboards_data = json.load(f)

            # Check if dashboard already exists
            items = dashboards_data.get("data", {}).get("items", [])
            existing = next((i for i in items if i.get("url_path") == slug), None)

            if not existing:
                items.append({
                    "id": slug,
                    "url_path": slug,
                    "title": name,
                    "icon": "mdi:view-dashboard",
                    "show_in_sidebar": True,
                    "require_admin": False,
                    "mode": "storage"
                })
                dashboards_data["data"]["items"] = items

                with open(dashboards_storage_path, "w") as f:
                    json.dump(dashboards_data, f, indent=2)

            return web.json_response({"success": True, "slug": slug, "message": "Dashboard created. Please restart Home Assistant to see it in the sidebar."})
        except Exception as e:
            _LOGGER.error("Error adding dashboard: %s", e)
            return web.json_response({"error": str(e)}, status=500)


class HelpersReposView(HomeAssistantView):
    """API to list helper repositories from the Helpers organization."""
    url = "/api/yidstore/helpers"
    name = "api:yidstore:helpers"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            eid = self.entry_id
            if DOMAIN not in hass.data:
                return web.json_response({"error": "Integration not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if not eids:
                    return web.json_response({"error": "Integration not ready"}, status=503)
                eid = eids[0]

            client = hass.data[DOMAIN][eid]["client"]

            # Try org first, fall back to user repos if org doesn't exist
            repos = await client.get_org_repos(HELPERS_ORG)
            if not repos:
                repos = await client.get_user_repos(HELPERS_ORG)

            result = []
            for repo in repos:
                if not isinstance(repo, dict):
                    continue

                if repo.get("archived", False):
                    continue

                owner = repo.get("owner", {}).get("login", HELPERS_ORG)
                repo_name = repo.get("name", "")
                description = repo.get("description", "")
                updated_at = repo.get("updated_at", "")
                default_branch = repo.get("default_branch", "main")

                result.append({
                    "name": f"{owner}/{repo_name}",
                    "owner": owner,
                    "repo_name": repo_name,
                    "description": description,
                    "updated_at": updated_at,
                    "default_branch": default_branch,
                })

            return web.json_response(result)
        except Exception as e:
            _LOGGER.error("Error fetching helper repos: %s", e)
            return web.json_response({"error": str(e)}, status=500)


class HelpersFilesView(HomeAssistantView):
    """API to list YAML files in a helpers repository."""
    url = "/api/yidstore/helpers/{owner}/{repo}/files"
    name = "api:yidstore:helpers:files"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request, owner: str, repo: str) -> web.Response:
        hass = request.app["hass"]
        try:
            eid = self.entry_id
            if DOMAIN not in hass.data:
                return web.json_response({"error": "Integration not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if not eids:
                    return web.json_response({"error": "Integration not ready"}, status=503)
                eid = eids[0]

            client = hass.data[DOMAIN][eid]["client"]

            path = request.query.get("path", "")
            branch = request.query.get("branch", "main")

            entries = await client.list_dir_recursive(owner, repo, path, branch)

            result = []
            for entry in entries:
                entry_type = entry.get("type", "")
                name = entry.get("name", "")

                if entry_type == "dir":
                    result.append(entry)
                elif entry_type == "file":
                    lower_name = name.lower()
                    if lower_name.endswith(('.yaml', '.yml', '.md', '.html', '.htm')):
                        result.append(entry)

            return web.json_response(result)
        except Exception as e:
            _LOGGER.error("Error fetching helper files for %s/%s: %s", owner, repo, e)
            return web.json_response({"error": str(e)}, status=500)


class HelpersContentView(HomeAssistantView):
    """API to get content of a helper YAML file with linked instructions."""
    url = "/api/yidstore/helpers/{owner}/{repo}/content"
    name = "api:yidstore:helpers:content"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request, owner: str, repo: str) -> web.Response:
        hass = request.app["hass"]
        try:
            eid = self.entry_id
            if DOMAIN not in hass.data:
                return web.json_response({"error": "Integration not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if not eids:
                    return web.json_response({"error": "Integration not ready"}, status=503)
                eid = eids[0]

            client = hass.data[DOMAIN][eid]["client"]

            file_path = request.query.get("path", "")
            branch = request.query.get("branch", "main")

            if not file_path:
                return web.json_response({"error": "Missing path parameter"}, status=400)

            file_info = await client.get_file_info_with_history(owner, repo, file_path, branch)

            if file_info is None:
                return web.json_response({"error": "File not found"}, status=404)

            # Try to find linked instructions file
            base_path = file_path.rsplit('.', 1)[0] if '.' in file_path else file_path
            base_name = base_path.rsplit('/', 1)[-1] if '/' in base_path else base_path
            dir_path = base_path.rsplit('/', 1)[0] if '/' in base_path else ""
            instructions_content = None
            instructions_path = None

            # First try exact match
            for ext in ['.md', '.html', '.htm']:
                try_path = base_path + ext
                instr_info = await client.get_file_info_with_history(owner, repo, try_path, branch)
                if instr_info and instr_info.get("content"):
                    instructions_content = instr_info.get("content")
                    instructions_path = try_path
                    break

            # If no exact match, try case-insensitive search
            if not instructions_content:
                dir_entries = await client.list_dir(owner, repo, dir_path, branch)
                base_name_lower = base_name.lower()
                for entry in dir_entries:
                    if entry.get("type") != "file":
                        continue
                    entry_name = entry.get("name", "")
                    entry_lower = entry_name.lower()
                    if entry_lower.endswith(('.md', '.html', '.htm')):
                        entry_base = entry_lower.rsplit('.', 1)[0]
                        if entry_base == base_name_lower or entry_base.startswith(base_name_lower) or base_name_lower.startswith(entry_base):
                            try_path = f"{dir_path}/{entry_name}" if dir_path else entry_name
                            instr_info = await client.get_file_info_with_history(owner, repo, try_path, branch)
                            if instr_info and instr_info.get("content"):
                                instructions_content = instr_info.get("content")
                                instructions_path = try_path
                                break

            return web.json_response({
                "content": file_info.get("content", ""),
                "file_path": file_info.get("file_path", ""),
                "last_modified_by": file_info.get("last_modified_by"),
                "last_modified_at": file_info.get("last_modified_at"),
                "instructions": instructions_content,
                "instructions_path": instructions_path,
            })
        except Exception as e:
            _LOGGER.error("Error fetching helper content for %s/%s: %s", owner, repo, e)
            return web.json_response({"error": str(e)}, status=500)


class AddHelperView(HomeAssistantView):
    """API to add a helper from YAML."""
    url = "/api/yidstore/add_helper"
    name = "api:yidstore:add_helper"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            data = await request.json()
            yaml_content = data.get("yaml", "")
            helper_type = data.get("type", "")  # input_boolean, input_number, input_text, etc.
            name = data.get("name", "New Helper")

            if not yaml_content:
                return web.json_response({"error": "No YAML content provided"}, status=400)

            import yaml

            # Parse YAML
            try:
                helper_config = yaml.safe_load(yaml_content)
            except yaml.YAMLError as e:
                return web.json_response({"error": f"Invalid YAML: {e}"}, status=400)

            # Detect helper type from YAML if not specified
            if not helper_type:
                if isinstance(helper_config, dict):
                    for key in ["input_boolean", "input_number", "input_text", "input_select", "input_datetime", "input_button", "counter", "timer", "schedule"]:
                        if key in helper_config:
                            helper_type = key
                            helper_config = helper_config[key]
                            break

            if not helper_type:
                return web.json_response({"error": "Could not determine helper type"}, status=400)

            # Create helper via HA service
            if isinstance(helper_config, dict):
                for helper_id, config in helper_config.items():
                    if not isinstance(config, dict):
                        config = {}

                    config["name"] = config.get("name", helper_id.replace("_", " ").title())

                    try:
                        await hass.services.async_call(
                            helper_type,
                            "create",
                            config,
                            blocking=True
                        )
                    except Exception as e:
                        _LOGGER.warning("Could not create helper via service, trying storage: %s", e)
                        # Fall back to writing to storage
                        return await self._create_via_storage(hass, helper_type, helper_id, config)

            return web.json_response({"success": True, "type": helper_type})
        except Exception as e:
            _LOGGER.error("Error adding helper: %s", e)
            return web.json_response({"error": str(e)}, status=500)

    async def _create_via_storage(self, hass, helper_type: str, helper_id: str, config: dict) -> web.Response:
        """Create helper via storage file when service is not available."""
        import json
        from pathlib import Path

        storage_key = f"core.config_entries" if helper_type in ["counter", "timer"] else f"input_{helper_type.replace('input_', '')}"
        storage_path = Path(hass.config.path(f".storage/{storage_key}"))

        try:
            storage_data = {"version": 1, "data": {}}
            if storage_path.exists():
                with open(storage_path, "r") as f:
                    storage_data = json.load(f)

            # Add new helper
            items = storage_data.get("data", {}).get("items", [])
            import uuid
            new_item = {
                "id": str(uuid.uuid4()),
                "name": config.get("name", helper_id),
                **config
            }
            items.append(new_item)
            storage_data["data"]["items"] = items

            with open(storage_path, "w") as f:
                json.dump(storage_data, f, indent=2)

            return web.json_response({"success": True, "type": helper_type, "message": "Helper created. Please restart Home Assistant."})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)


class ListHelpersView(HomeAssistantView):
    """API to list existing helpers in Home Assistant."""
    url = "/api/yidstore/helpers/list"
    name = "api:yidstore:helpers:list"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            helpers = []

            # Get all helper domains
            helper_domains = ["input_boolean", "input_number", "input_text", "input_select", "input_datetime", "input_button", "counter", "timer", "schedule"]

            for domain in helper_domains:
                entities = hass.states.async_entity_ids(domain)
                for entity_id in entities:
                    state = hass.states.get(entity_id)
                    if state:
                        helpers.append({
                            "entity_id": entity_id,
                            "type": domain,
                            "name": state.attributes.get("friendly_name", entity_id),
                            "state": state.state,
                        })

            return web.json_response(helpers)
        except Exception as e:
            _LOGGER.error("Error listing helpers: %s", e)
            return web.json_response({"error": str(e)}, status=500)


class AudioReposView(HomeAssistantView):
    """API to list audio repositories from the Audio organization."""
    url = "/api/yidstore/audio"
    name = "api:yidstore:audio"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            eid = self.entry_id
            if DOMAIN not in hass.data:
                return web.json_response({"error": "Integration not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if not eids:
                    return web.json_response({"error": "Integration not ready"}, status=503)
                eid = eids[0]

            client = hass.data[DOMAIN][eid]["client"]

            # Try org first, fall back to user repos
            repos = await client.get_org_repos(AUDIO_ORG)
            if not repos:
                repos = await client.get_user_repos(AUDIO_ORG)

            result = []
            for repo in repos:
                if not isinstance(repo, dict):
                    continue
                if repo.get("archived", False):
                    continue

                owner = repo.get("owner", {}).get("login", AUDIO_ORG)
                repo_name = repo.get("name", "")
                description = repo.get("description", "")
                updated_at = repo.get("updated_at", "")
                default_branch = repo.get("default_branch", "main")

                result.append({
                    "name": f"{owner}/{repo_name}",
                    "owner": owner,
                    "repo_name": repo_name,
                    "description": description,
                    "updated_at": updated_at,
                    "default_branch": default_branch,
                })

            return web.json_response(result)
        except Exception as e:
            _LOGGER.error("Error fetching audio repos: %s", e)
            return web.json_response({"error": str(e)}, status=500)


class AudioFilesView(HomeAssistantView):
    """API to list files in an audio repository."""
    url = "/api/yidstore/audio/{owner}/{repo}/files"
    name = "api:yidstore:audio:files"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request, owner: str, repo: str) -> web.Response:
        hass = request.app["hass"]
        try:
            eid = self.entry_id
            if DOMAIN not in hass.data:
                return web.json_response({"error": "Integration not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if not eids:
                    return web.json_response({"error": "Integration not ready"}, status=503)
                eid = eids[0]

            client = hass.data[DOMAIN][eid]["client"]

            path = request.query.get("path", "")
            branch = request.query.get("branch", "main")

            entries = await client.list_dir_recursive(owner, repo, path, branch)

            result = []
            for entry in entries:
                entry_type = entry.get("type", "")
                name = entry.get("name", "")

                if entry_type == "dir":
                    result.append(entry)
                elif entry_type == "file":
                    lower_name = name.lower()
                    # Include audio files and documentation
                    if lower_name.endswith(('.mp3', '.wav', '.ogg', '.m4a', '.flac', '.aac', '.md', '.html', '.htm', '.yaml', '.yml')):
                        result.append(entry)

            return web.json_response(result)
        except Exception as e:
            _LOGGER.error("Error fetching audio files for %s/%s: %s", owner, repo, e)
            return web.json_response({"error": str(e)}, status=500)


class AudioContentView(HomeAssistantView):
    """API to get content/info of an audio repo for installation."""
    url = "/api/yidstore/audio/{owner}/{repo}/content"
    name = "api:yidstore:audio:content"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request, owner: str, repo: str) -> web.Response:
        hass = request.app["hass"]
        try:
            eid = self.entry_id
            if DOMAIN not in hass.data:
                return web.json_response({"error": "Integration not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if not eids:
                    return web.json_response({"error": "Integration not ready"}, status=503)
                eid = eids[0]

            client = hass.data[DOMAIN][eid]["client"]

            branch = request.query.get("branch", "main")

            # Get repo info
            try:
                repo_info = await client.get_repo(owner, repo)
            except:
                repo_info = {}

            # List all audio files in the repo
            audio_files = []
            entries = await client.list_dir_recursive(owner, repo, "", branch)
            for entry in entries:
                if entry.get("type") == "file":
                    name = entry.get("name", "").lower()
                    if name.endswith(('.mp3', '.wav', '.ogg', '.m4a', '.flac', '.aac')):
                        audio_files.append(entry.get("path", entry.get("name", "")))

            # Check if already installed
            www_path = Path(hass.config.path("www", "audio", owner, repo))
            media_path = Path(hass.config.path("media", "audio", owner, repo))
            is_installed = www_path.exists() or media_path.exists()
            install_location = "www" if www_path.exists() else ("media" if media_path.exists() else None)

            # Get local audio files if installed
            local_files = []
            if www_path.exists():
                for f in www_path.rglob("*"):
                    if f.is_file() and f.suffix.lower() in ('.mp3', '.wav', '.ogg', '.m4a', '.flac', '.aac'):
                        rel = f.relative_to(www_path)
                        # Use forward slashes for URLs (Windows uses backslashes)
                        rel_url = str(rel).replace("\\", "/")
                        local_files.append({
                            "name": f.name,
                            "path": rel_url,
                            "url": f"/local/audio/{owner}/{repo}/{rel_url}"
                        })
            elif media_path.exists():
                for f in media_path.rglob("*"):
                    if f.is_file() and f.suffix.lower() in ('.mp3', '.wav', '.ogg', '.m4a', '.flac', '.aac'):
                        rel = f.relative_to(media_path)
                        rel_url = str(rel).replace("\\", "/")
                        local_files.append({
                            "name": f.name,
                            "path": rel_url,
                            "url": f"/media/audio/{owner}/{repo}/{rel_url}"
                        })

            # Get README if available
            readme = await client.get_readme(owner, repo)

            return web.json_response({
                "owner": owner,
                "repo": repo,
                "description": repo_info.get("description", ""),
                "default_branch": repo_info.get("default_branch", branch),
                "audio_files": audio_files,
                "is_installed": is_installed,
                "install_location": install_location,
                "local_files": local_files,
                "readme": readme,
            })
        except Exception as e:
            _LOGGER.error("Error fetching audio content for %s/%s: %s", owner, repo, e)
            return web.json_response({"error": str(e)}, status=500)


class BlueprintsReposView(HomeAssistantView):
    """API to list blueprint repositories from the Blueprints organization."""
    url = "/api/yidstore/blueprints"
    name = "api:yidstore:blueprints"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            eid = self.entry_id
            if DOMAIN not in hass.data:
                return web.json_response({"error": "Integration not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if not eids:
                    return web.json_response({"error": "Integration not ready"}, status=503)
                eid = eids[0]

            client = hass.data[DOMAIN][eid]["client"]

            # Try org first, fall back to user repos
            repos = await client.get_org_repos(BLUEPRINTS_ORG)
            if not repos:
                repos = await client.get_user_repos(BLUEPRINTS_ORG)

            result = []
            for repo in repos:
                if not isinstance(repo, dict):
                    continue
                if repo.get("archived", False):
                    continue

                owner = repo.get("owner", {}).get("login", BLUEPRINTS_ORG)
                repo_name = repo.get("name", "")
                description = repo.get("description", "")
                updated_at = repo.get("updated_at", "")
                default_branch = repo.get("default_branch", "main")

                result.append({
                    "name": f"{owner}/{repo_name}",
                    "owner": owner,
                    "repo_name": repo_name,
                    "description": description,
                    "updated_at": updated_at,
                    "default_branch": default_branch,
                })

            return web.json_response(result)
        except Exception as e:
            _LOGGER.error("Error fetching blueprint repos: %s", e)
            return web.json_response({"error": str(e)}, status=500)


class BlueprintsFilesView(HomeAssistantView):
    """API to list YAML files in a blueprints repository."""
    url = "/api/yidstore/blueprints/{owner}/{repo}/files"
    name = "api:yidstore:blueprints:files"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request, owner: str, repo: str) -> web.Response:
        hass = request.app["hass"]
        try:
            eid = self.entry_id
            if DOMAIN not in hass.data:
                return web.json_response({"error": "Integration not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if not eids:
                    return web.json_response({"error": "Integration not ready"}, status=503)
                eid = eids[0]

            client = hass.data[DOMAIN][eid]["client"]

            path = request.query.get("path", "")
            branch = request.query.get("branch", "main")

            entries = await client.list_dir_recursive(owner, repo, path, branch)

            result = []
            for entry in entries:
                entry_type = entry.get("type", "")
                name = entry.get("name", "")

                if entry_type == "dir":
                    result.append(entry)
                elif entry_type == "file":
                    lower_name = name.lower()
                    if lower_name.endswith(('.yaml', '.yml', '.md', '.html', '.htm')):
                        result.append(entry)

            return web.json_response(result)
        except Exception as e:
            _LOGGER.error("Error fetching blueprint files for %s/%s: %s", owner, repo, e)
            return web.json_response({"error": str(e)}, status=500)


class BlueprintsContentView(HomeAssistantView):
    """API to get content of a blueprint YAML file with linked instructions."""
    url = "/api/yidstore/blueprints/{owner}/{repo}/content"
    name = "api:yidstore:blueprints:content"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request, owner: str, repo: str) -> web.Response:
        hass = request.app["hass"]
        try:
            eid = self.entry_id
            if DOMAIN not in hass.data:
                return web.json_response({"error": "Integration not ready"}, status=503)
            if eid not in hass.data[DOMAIN]:
                eids = list(hass.data[DOMAIN].keys())
                if not eids:
                    return web.json_response({"error": "Integration not ready"}, status=503)
                eid = eids[0]

            client = hass.data[DOMAIN][eid]["client"]

            file_path = request.query.get("path", "")
            branch = request.query.get("branch", "main")

            if not file_path:
                return web.json_response({"error": "Missing path parameter"}, status=400)

            file_info = await client.get_file_info_with_history(owner, repo, file_path, branch)

            if file_info is None:
                return web.json_response({"error": "File not found"}, status=404)

            # Try to find linked instructions file
            base_path = file_path.rsplit('.', 1)[0] if '.' in file_path else file_path
            base_name = base_path.rsplit('/', 1)[-1] if '/' in base_path else base_path
            dir_path = base_path.rsplit('/', 1)[0] if '/' in base_path else ""
            instructions_content = None
            instructions_path = None

            for ext in ['.md', '.html', '.htm']:
                try_path = base_path + ext
                instr_info = await client.get_file_info_with_history(owner, repo, try_path, branch)
                if instr_info and instr_info.get("content"):
                    instructions_content = instr_info.get("content")
                    instructions_path = try_path
                    break

            if not instructions_content:
                dir_entries = await client.list_dir(owner, repo, dir_path, branch)
                base_name_lower = base_name.lower()
                for entry in dir_entries:
                    if entry.get("type") != "file":
                        continue
                    entry_name = entry.get("name", "")
                    entry_lower = entry_name.lower()
                    if entry_lower.endswith(('.md', '.html', '.htm')):
                        entry_base = entry_lower.rsplit('.', 1)[0]
                        if entry_base == base_name_lower or entry_base.startswith(base_name_lower) or base_name_lower.startswith(entry_base):
                            try_path = f"{dir_path}/{entry_name}" if dir_path else entry_name
                            instr_info = await client.get_file_info_with_history(owner, repo, try_path, branch)
                            if instr_info and instr_info.get("content"):
                                instructions_content = instr_info.get("content")
                                instructions_path = try_path
                                break

            return web.json_response({
                "content": file_info.get("content", ""),
                "file_path": file_info.get("file_path", ""),
                "last_modified_by": file_info.get("last_modified_by"),
                "last_modified_at": file_info.get("last_modified_at"),
                "instructions": instructions_content,
                "instructions_path": instructions_path,
            })
        except Exception as e:
            _LOGGER.error("Error fetching blueprint content for %s/%s: %s", owner, repo, e)
            return web.json_response({"error": str(e)}, status=500)
