"""YidStore Apps: Home Assistant add-ons from the Apps / PrivateApps orgs.

How an app gets installed on Home Assistant OS
----------------------------------------------
Core (where this integration runs) cannot write to the folder the Supervisor
scans for local add-ons. The **YidStore Connector** add-on can, so:

1. YidStore ships the connector inside the integration and serves it from
   its own add-on store (``addon_store.py``). The "Install add-on" button
   registers that store with the Supervisor and installs + starts the
   connector. No separate repository is needed.
2. Installing an app: YidStore downloads it from the store server, the
   connector unpacks it into ``/addons/<slug>``, and YidStore then asks the
   Supervisor to reload, **build and install** it (or update it).
3. Public apps come from ``Apps``. ``PrivateApps`` is only read when the
   integration has a working token. The browser only ever sees opaque app
   ids, never organization names or the store server's address.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
import uuid
from pathlib import Path

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Public apps
APPS_ORG = "Apps"
# Private apps: only listed/installable when the integration has a token.
PRIVATE_APPS_ORG = "PrivateApps"

CONNECTOR_SLUG = "yidstore_connector"
_CONNECTOR_SRC = Path(__file__).parent / "connector_addon" / CONNECTOR_SLUG
STORE_GIT_PATH = "/api/yidstore/store.git"

# How recently the connector must have checked in to count as "online".
_CONNECTOR_ONLINE_WINDOW = 60.0
# Re-offer a job the connector claimed but never finished after this long.
_CONNECTOR_JOB_RETRY = 120.0

_CONNECTOR_STATE: dict = {"last_seen": 0.0}
_CONNECTOR_JOBS: dict[str, dict] = {}
# Background work shown in the UI: app id (or "connector") -> state.
_TASKS: dict[str, dict] = {}
# Opaque app ids handed to the browser -> (owner, repo).
_APP_IDS: dict[str, tuple[str, str]] = {}
_MANIFEST_CACHE: dict[tuple, dict] = {}
_ICON_CACHE: dict[str, tuple[float, bytes | None, str]] = {}
_ICON_TTL = 6 * 60 * 60


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _client(hass, entry_id: str):
    data = hass.data.get(DOMAIN, {})
    if entry_id in data and "client" in data[entry_id]:
        return data[entry_id]["client"]
    for value in data.values():
        if isinstance(value, dict) and "client" in value:
            return value["client"]
    return None


def _is_admin(request) -> bool:
    user = request.get("hass_user")
    return bool(user and user.is_admin)


def _addon_slug(repo_name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", repo_name.lower().replace("-", "_"))


def _app_id(owner: str, repo: str) -> str:
    app_id = hashlib.sha256(f"{owner.lower()}/{repo.lower()}".encode()).hexdigest()[:16]
    _APP_IDS[app_id] = (owner, repo)
    return app_id


def _set_task(key: str, state: str, error: str | None = None, **extra) -> None:
    _TASKS[key] = {"state": state, "error": error, "updated": time.time(), **extra}


def _task(key: str) -> dict | None:
    task = _TASKS.get(key)
    if not task:
        return None
    # Finished tasks are only reported for a while.
    if task["state"] in ("done", "error") and time.time() - task["updated"] > 600:
        _TASKS.pop(key, None)
        return None
    return task


def _version_tuple(version: str) -> tuple:
    parts = re.findall(r"\d+", str(version or ""))
    return tuple(int(p) for p in parts) if parts else (0,)


def _is_newer(latest: str | None, installed: str | None) -> bool:
    if not latest or not installed:
        return False
    return _version_tuple(latest) > _version_tuple(installed)


# ---------------------------------------------------------------------------
# Supervisor API
# ---------------------------------------------------------------------------

def supervisor_available(hass) -> bool:
    if os.environ.get("SUPERVISOR_TOKEN"):
        return True
    try:
        from homeassistant.components.hassio import is_hassio

        if is_hassio(hass):
            return True
    except Exception:
        pass
    return "hassio" in hass.config.components


async def _sup(hass, method: str, path: str, body=None, timeout: int = 60):
    """Call the Supervisor. Returns (status, json) or None."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    sess = async_get_clientsession(hass)
    try:
        async with sess.request(
            method.upper(),
            f"http://supervisor{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            timeout=timeout,
        ) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = None
            _LOGGER.debug("Supervisor %s %s -> %s", method.upper(), path, resp.status)
            return resp.status, data
    except Exception as exc:
        _LOGGER.warning("Supervisor %s %s failed: %s", method.upper(), path, exc)
        return None


def _ok(res) -> bool:
    return bool(res and res[0] in (200, 201))


def _data(res):
    return res[1].get("data") if res and isinstance(res[1], dict) else None


def _message(res) -> str:
    if res and isinstance(res[1], dict) and res[1].get("message"):
        return str(res[1]["message"])
    return f"HTTP {res[0]}" if res else "Supervisor not reachable"


def _list_of(res, key: str) -> list:
    data = _data(res)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get(key), list):
        return data[key]
    return []


async def _installed_addons(hass) -> list[dict]:
    return [a for a in _list_of(await _sup(hass, "get", "/addons"), "addons") if isinstance(a, dict)]


async def _store_addons(hass) -> list[dict]:
    return [a for a in _list_of(await _sup(hass, "get", "/store/addons"), "addons") if isinstance(a, dict)]


def _find_slug(addons: list[dict], slug: str, local: bool | None = None) -> str | None:
    """Supervisor slug for an add-on folder slug (local_<slug> or <hash>_<slug>)."""
    target = slug.lower()
    best = None
    for a in addons:
        s = str(a.get("slug", "")).lower()
        if s == f"local_{target}":
            if local is not False:
                return a["slug"]
        elif s.endswith(f"_{target}") or s == target:
            if local is not True:
                best = best or a["slug"]
    return best


async def _store_reload(hass) -> None:
    await _sup(hass, "post", "/store/reload", timeout=300)


async def _install_or_update(hass, store_slug: str, update: bool) -> tuple[bool, str | None]:
    """Ask the Supervisor to build + install (or update) an add-on."""
    action = "update" if update else "install"
    res = await _sup(hass, "post", f"/store/addons/{store_slug}/{action}", timeout=3600)
    if not _ok(res) and res and res[0] == 404:
        res = await _sup(hass, "post", f"/addons/{store_slug}/{action}", timeout=3600)
    if _ok(res):
        return True, None
    msg = _message(res)
    if not update and "already installed" in msg.lower():
        return True, None
    return False, msg


# ---------------------------------------------------------------------------
# Connector: jobs and state
# ---------------------------------------------------------------------------

def connector_online() -> bool:
    return (time.time() - _CONNECTOR_STATE.get("last_seen", 0.0)) < _CONNECTOR_ONLINE_WINDOW


def _enqueue_job(action: str, owner: str, repo: str, slug: str, ref: str | None) -> str:
    job_id = uuid.uuid4().hex
    _CONNECTOR_JOBS[job_id] = {
        "id": job_id, "action": action, "owner": owner, "repo": repo,
        "slug": slug, "ref": ref, "status": "pending", "error": None,
        "config_found": None, "addon_slug": None, "created": time.time(), "sent": 0.0,
    }
    return job_id


def _pending_jobs() -> list[dict]:
    now = time.time()
    out = []
    for job_id, job in list(_CONNECTOR_JOBS.items()):
        if now - job["created"] > 3600:
            _CONNECTOR_JOBS.pop(job_id, None)
            continue
        if job["status"] == "pending" or (
            job["status"] == "sent" and now - job["sent"] > _CONNECTOR_JOB_RETRY
        ):
            job["status"] = "sent"
            job["sent"] = now
            out.append({"id": job["id"], "action": job["action"], "slug": job["slug"]})
    return out


async def _wait_for_job(job_id: str, timeout: float) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = _CONNECTOR_JOBS.get(job_id)
        if not job:
            return {"status": "missing"}
        if job["status"] in ("done", "error"):
            return job
        await asyncio.sleep(1.5)
    return _CONNECTOR_JOBS.get(job_id, {"status": "timeout"}) | {"status": "timeout"}


# ---------------------------------------------------------------------------
# Built-in add-on store (serves the bundled connector)
# ---------------------------------------------------------------------------

def _store_root(hass) -> Path:
    return Path(hass.config.path(".yidstore_store"))


def _publish_store(root: Path) -> str | None:
    """Blocking: put the bundled connector (and only it) in the store and publish."""
    import shutil

    from . import addon_store

    addon_store.ensure_repository_json(root)
    src = root / "src"
    # Older versions also copied apps into this store; it now holds only the
    # connector, so nothing private is ever served without authentication.
    for child in src.iterdir():
        if child.is_dir() and child.name != CONNECTOR_SLUG:
            shutil.rmtree(child, ignore_errors=True)
    legacy_objects = root / "repo.git" / "objects"
    if legacy_objects.is_dir():
        shutil.rmtree(legacy_objects, ignore_errors=True)
    addon_store.sync_addon_dir(root, CONNECTOR_SLUG, _CONNECTOR_SRC)
    return addon_store.publish_from_dir(root)


async def _store_url(hass) -> str:
    """Address the Supervisor uses to clone the built-in store."""
    override = os.environ.get("YIDSTORE_CORE_URL")
    if override:
        return f"{override.rstrip('/')}{STORE_GIT_PATH}"
    info = _data(await _sup(hass, "get", "/core/info")) or {}
    ip = info.get("ip_address") or "homeassistant"
    port = info.get("port") or 8123
    scheme = "https" if info.get("ssl") else "http"
    return f"{scheme}://{ip}:{port}{STORE_GIT_PATH}"


async def _ensure_store_registered(hass) -> tuple[bool, str | None]:
    url = await _store_url(hass)
    res = await _sup(hass, "get", "/store/repositories")
    for repo in _list_of(res, "repositories"):
        source = repo.get("source") if isinstance(repo, dict) else str(repo)
        if source and source.rstrip("/").endswith(STORE_GIT_PATH):
            if source == url:
                return True, None
            # The address changed (e.g. new port): replace the old entry.
            slug = repo.get("slug") if isinstance(repo, dict) else None
            if slug:
                await _sup(hass, "delete", f"/store/repositories/{slug}")
    add = await _sup(hass, "post", "/store/repositories", {"repository": url}, timeout=300)
    if _ok(add):
        return True, None
    return False, _message(add)


_CONNECTOR_SETUP_LOCK: asyncio.Lock | None = None


async def setup_connector(hass) -> None:
    """Install (if needed) and start the bundled YidStore Connector.

    Raises on failure. Progress is kept in _TASKS["connector"] so the Apps
    tab shows it no matter who started the setup. Concurrent callers share
    one run: whoever comes second waits and returns once it is online.
    """
    global _CONNECTOR_SETUP_LOCK
    if _CONNECTOR_SETUP_LOCK is None:
        _CONNECTOR_SETUP_LOCK = asyncio.Lock()
    key = "connector"
    async with _CONNECTOR_SETUP_LOCK:
        if connector_online():
            task = _TASKS.get(key)
            if task and task["state"] not in ("done", "error"):
                _set_task(key, "done")
            return
        try:
            _set_task(key, "preparing")
            await hass.async_add_executor_job(_publish_store, _store_root(hass))

            installed = await _installed_addons(hass)
            slug = _find_slug(installed, CONNECTOR_SLUG)
            if not slug:
                ok, err = await _ensure_store_registered(hass)
                if not ok:
                    raise RuntimeError(f"Home Assistant could not add the YidStore add-on store: {err}")
                await _store_reload(hass)
                slug = _find_slug(await _store_addons(hass), CONNECTOR_SLUG, local=False)
                if not slug:
                    raise RuntimeError("The YidStore Connector did not show up in the add-on store.")
                _set_task(key, "installing")
                ok, err = await _install_or_update(hass, slug, update=False)
                if not ok:
                    raise RuntimeError(f"Installing the YidStore Connector failed: {err}")

            _set_task(key, "starting")
            await _sup(hass, "post", f"/addons/{slug}/options", {"boot": "auto", "watchdog": True})
            res = await _sup(hass, "post", f"/addons/{slug}/start", timeout=300)
            if not _ok(res) and "running" not in _message(res).lower():
                raise RuntimeError(f"Starting the YidStore Connector failed: {_message(res)}")

            # Wait for its first check-in so apps can be installed right away.
            for _ in range(40):
                if connector_online():
                    break
                await asyncio.sleep(1.5)
            _set_task(key, "done")
        except Exception as exc:
            _set_task(key, "error", str(exc))
            raise


async def _run_connector_setup(hass) -> None:
    """Background task behind the Apps tab's "Install add-on" button."""
    try:
        await setup_connector(hass)
    except Exception as exc:
        _LOGGER.error("YidStore Connector setup failed: %s", exc)


# ---------------------------------------------------------------------------
# Store server (Gitea): app list, manifests, archives, icons
# ---------------------------------------------------------------------------

async def _list_app_repos(client) -> list[tuple[str, dict]]:
    """(org, repo) for every app visible with the current settings.

    Public apps: the Apps organization, read without the token. On top:
    every Apps / PrivateApps repository the token sees (a repo key sees only
    its own repositories and can't list organizations). Installed apps the
    token could see before but not now are kept (marked access_stopped).
    """
    app_orgs = {APPS_ORG.lower(), PRIVATE_APPS_ORG.lower()}
    candidates: list[tuple[str, dict]] = []

    repos = await client.get_org_repos(APPS_ORG)
    if not repos:
        repos = await client.get_user_repos(APPS_ORG)
    for repo in repos or []:
        if isinstance(repo, dict) and not repo.get("private", False):
            candidates.append(((repo.get("owner") or {}).get("login") or APPS_ORG, repo))

    visible: dict = {}
    if client.token and await client.test_auth():
        visible = await client.refresh_token_repos(max_age=300)
    for key, repo in visible.items():
        if key.split("/", 1)[0] in app_orgs:
            candidates.append(((repo.get("owner") or {}).get("login") or key.split("/", 1)[0], repo))
    # Custom repositories with their own repo key.
    for key in client.repo_keys:
        org, _, name = key.partition("/")
        if org in app_orgs and key not in visible:
            candidates.append((org, {"name": name, "owner": {"login": org}}))
    for key in client.authorized_repos:
        org, _, name = key.partition("/")
        if org in app_orgs and key not in visible and key not in client.repo_keys:
            candidates.append((org, {"name": name, "owner": {"login": org}, "access_stopped": True}))

    out: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for owner, repo in candidates:
        name = repo.get("name", "")
        if not name or repo.get("archived", False):
            continue
        if name.startswith("x-") and not client.credential_for(owner, name):
            continue
        slug = _addon_slug(name)
        if slug in seen:
            continue  # a public app wins over a private one with the same slug
        seen.add(slug)
        out.append((owner, repo))
    return out


async def _latest_release(client, owner: str, repo: str) -> str | None:
    try:
        latest = await client.get_latest_release(owner, repo)
        return (latest or {}).get("tag_name") or None
    except Exception:
        return None


async def _default_ref(client, owner: str, repo: str, repo_info: dict | None = None) -> str:
    tag = await _latest_release(client, owner, repo)
    if tag:
        return tag
    if repo_info and repo_info.get("default_branch"):
        return repo_info["default_branch"]
    try:
        info = await client.get_repo(owner, repo)
        return (info or {}).get("default_branch") or "main"
    except Exception:
        return "main"


async def _raw_file(hass, client, owner: str, repo: str, path: str, ref: str | None) -> bytes | None:
    """A raw file; the repository's credential is used only if it needs one."""
    return await client.get_raw_file(owner, repo, path, ref)


async def _read_manifest(hass, client, owner: str, repo: str, ref: str, prefix: str) -> dict:
    """Parse config.yaml / .yml / .json under ``prefix`` ("" or "folder/")."""
    for path in ("config.yaml", "config.yml", "config.json"):
        raw = await _raw_file(hass, client, owner, repo, prefix + path, ref)
        if raw is None:
            continue
        try:
            if path.endswith(".json"):
                import json

                data = json.loads(raw)
            else:
                import yaml

                data = yaml.safe_load(raw)
        except Exception:
            data = None
        if isinstance(data, dict) and (data.get("slug") or data.get("version") or data.get("name")):
            return data
    return {}


_ORG_DISPLAY: dict[str, str] = {}


async def _org_display(client, org: str) -> str:
    """The organization's display name (e.g. "Private Apps")."""
    key = org.lower()
    if key not in _ORG_DISPLAY:
        info = None
        try:
            info = await client.get_org_info(org)
        except Exception:
            pass
        name = ((info or {}).get("full_name") or "").strip()
        if not name:
            # "PrivateApps" -> "Private Apps"
            name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", (info or {}).get("username") or org)
        _ORG_DISPLAY[key] = name
    return _ORG_DISPLAY[key]


async def _app_manifest(hass, client, owner: str, repo: dict) -> dict:
    """name / version / slug / description from the app's config file at the
    ref YidStore would install (latest release, else default branch)."""
    name = repo.get("name", "")
    ref = await _default_ref(client, owner, name, repo)
    key = (owner.lower(), name.lower(), ref, repo.get("updated_at"))
    if key in _MANIFEST_CACHE:
        return _MANIFEST_CACHE[key]
    manifest = await _read_manifest(hass, client, owner, name, ref, "")
    if not manifest:
        # Some repositories keep the add-on one folder down (next to a
        # repository.yaml); the connector installs those too.
        try:
            entries = await client.list_dir(owner, name, "", branch=ref)
        except Exception:
            entries = []
        folders = [
            e.get("name") for e in entries
            if isinstance(e, dict) and e.get("type") == "dir"
            and e.get("name") and not str(e.get("name")).startswith(".")
        ]
        for folder in folders[:15]:
            manifest = await _read_manifest(hass, client, owner, name, ref, folder + "/")
            if manifest:
                break
    if not manifest:
        _LOGGER.debug("No config.yaml found for app %s/%s at %s", owner, name, ref)
    tag = ref if re.match(r"^v?\d+(\.\d+)+", str(ref or "")) else None
    result = {
        "ref": ref,
        # Version of the latest release, used when config.yaml has none.
        "tag_version": tag.lstrip("vV") if tag else None,
        "name": str(manifest.get("name") or ""),
        "version": str(manifest.get("version") or "") or None,
        "slug": str(manifest.get("slug") or "") or None,
        "description": str(manifest.get("description") or ""),
        "has_config": bool(manifest),
    }
    _MANIFEST_CACHE[key] = result
    return result


async def _fetch_app_archive(hass, client, owner: str, repo: str, ref: str | None) -> bytes:
    """Download an app's zip, trying the token, then without it, then the default branch."""
    from .installer import _download_zip_bytes

    refs = [ref] if ref else []
    try:
        info = await client.get_repo(owner, repo)
        branch = (info or {}).get("default_branch")
        if branch and branch not in refs:
            refs.append(branch)
    except Exception:
        pass
    if not refs:
        refs.append("main")
    # The repository's credential if it needs one, else public access.
    header_sets = [client.auth_headers(owner, repo)]
    last_err: Exception | None = None
    for r in refs:
        url = client.archive_zip_url(owner, repo, r)
        for headers in header_sets:
            try:
                return await _download_zip_bytes(hass, url, headers=headers)
            except Exception as exc:
                last_err = exc
                _LOGGER.debug("App archive %s/%s@%s failed: %s", owner, repo, r, exc)
    raise RuntimeError(f"Could not download the app ({last_err})")


async def _fetch_icon(hass, client, owner: str, repo: str) -> tuple[bytes | None, str]:
    key = f"{owner.lower()}/{repo.lower()}"
    cached = _ICON_CACHE.get(key)
    if cached and time.time() - cached[0] < _ICON_TTL:
        return cached[1], cached[2]
    result: tuple[bytes | None, str] = (None, "")
    for name in ("icon.png", "logo.png"):
        data = await _raw_file(hass, client, owner, repo, name, None)
        if data and data[:8] == b"\x89PNG\r\n\x1a\n":
            result = (data, "image/png")
            break
    _ICON_CACHE[key] = (time.time(), result[0], result[1])
    return result


# ---------------------------------------------------------------------------
# Install / update / remove
# ---------------------------------------------------------------------------

async def install_app(hass, entry_id: str, owner: str, repo: str, update: bool) -> str:
    """Download an app through the connector and have the Supervisor install
    (or update) it. Returns the Supervisor slug; raises with the reason.

    Progress is kept in _TASKS[app id] so the Apps tab shows it.
    """
    app_id = _app_id(owner, repo)
    action = "update" if update else "install"
    try:
        client = _client(hass, entry_id)
        if client is None:
            raise RuntimeError("YidStore is not ready yet")
        if owner.lower() == PRIVATE_APPS_ORG.lower() and not client.credential_for(owner, repo):
            raise RuntimeError("This app is not available")
        if not connector_online():
            raise RuntimeError("The YidStore Connector add-on is not running")

        _set_task(app_id, "downloading", action=action)
        ref = await _default_ref(client, owner, repo)
        job_id = _enqueue_job("install", owner, repo, _addon_slug(repo), ref)
        job = await _wait_for_job(job_id, timeout=300)
        if job.get("status") != "done":
            raise RuntimeError(
                job.get("error")
                or "The YidStore Connector add-on didn't respond in time. Check that it is running."
            )
        if job.get("config_found") is False:
            raise RuntimeError("This app has no config.yaml, so Home Assistant can't install it.")
        addon_slug = job.get("addon_slug") or _addon_slug(repo)

        _set_task(app_id, "updating" if update else "installing", action=action)
        store_slug = None
        for _ in range(6):
            await _store_reload(hass)
            store_slug = _find_slug(await _store_addons(hass), addon_slug, local=True)
            if store_slug:
                break
            await asyncio.sleep(5)
        if not store_slug:
            raise RuntimeError(
                "Home Assistant did not pick up the app. Check its config.yaml "
                "(see Settings → System → Logs → Supervisor)."
            )
        installed = _find_slug(await _installed_addons(hass), addon_slug, local=True)
        ok, err = await _install_or_update(hass, store_slug, update=bool(installed))
        if not ok and installed and "no update" in (err or "").lower():
            # Already on the latest version (e.g. updated from Home
            # Assistant's own Updates in the meantime): nothing to do.
            current = next((a for a in await _installed_addons(hass) if a.get("slug") == installed), {})
            try:
                latest = (await _app_manifest(hass, client, owner, {"name": repo})).get("version")
            except Exception:
                latest = None
            if latest and not _is_newer(latest, current.get("version")):
                ok, err = True, None
        if not ok and installed and "no update" in (err or "").lower():
            # The Supervisor may not have read the new files yet: reload and
            # try once more before reporting what it still sees.
            await asyncio.sleep(3)
            await _store_reload(hass)
            ok, err = await _install_or_update(hass, store_slug, update=True)
            if not ok and "no update" in (err or "").lower():
                seen = next((a for a in await _installed_addons(hass) if a.get("slug") == installed), {})
                err = (
                    f"Home Assistant still sees version {seen.get('version_latest') or seen.get('version') or '?'} "
                    "of this app. Update the YidStore Connector add-on and try again; "
                    "if it keeps happening, see Settings → System → Logs → Supervisor."
                )
        if not ok:
            raise RuntimeError(err or "Home Assistant could not install the app")
        _set_task(app_id, "done", action=action)
        return store_slug
    except Exception as exc:
        _LOGGER.error("App %s %s failed: %s", repo, action, exc)
        _set_task(app_id, "error", str(exc))
        raise


async def _run_install(hass, entry_id: str, app_id: str, owner: str, repo: str, update: bool) -> None:
    """Background task behind the Apps tab's Install / Update buttons."""
    try:
        await install_app(hass, entry_id, owner, repo, update)
    except Exception:
        pass  # already logged and shown on the card


# ---------------------------------------------------------------------------
# Service: yidstore.install_app
# ---------------------------------------------------------------------------

async def _resolve_app_ref(hass, client, app_id: str | None, owner: str | None,
                           repo: str | None) -> tuple[str, str]:
    """(owner, repo) for the service call; raises if it can't be found."""
    if app_id and app_id in _APP_IDS:
        return _APP_IDS[app_id]
    if owner and repo:
        if owner.lower() not in (APPS_ORG.lower(), PRIVATE_APPS_ORG.lower()):
            raise RuntimeError(f"Unknown app {owner}/{repo}: apps live in {APPS_ORG} or {PRIVATE_APPS_ORG}")
        return owner, repo
    if app_id:
        # The mapping only fills once a list was loaded (empty after a restart).
        for org, info in await _list_app_repos(client):
            _app_id(org, info.get("name", ""))
        if app_id in _APP_IDS:
            return _APP_IDS[app_id]
    raise RuntimeError(f"Unknown app {app_id or ''}".strip())


async def _wait_until_idle(app_id: str, timeout: float = 3600) -> dict | None:
    """Wait for a running install/update of this app; returns its final task."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = _TASKS.get(app_id)
        if not task or task["state"] in ("done", "error"):
            return task
        await asyncio.sleep(2)
    raise RuntimeError("Timed out waiting for the running install of this app")


async def async_install_app_service(hass, entry_id: str, data: dict) -> dict:
    """yidstore.install_app: install (or update) an app, blocking until done.

    Raises HomeAssistantError with the real reason on failure.
    """
    from homeassistant.exceptions import HomeAssistantError

    update = bool(data.get("update", False))
    try:
        if not supervisor_available(hass):
            raise RuntimeError("Apps need Home Assistant OS / Supervised (no Supervisor here)")
        client = _client(hass, entry_id)
        if client is None:
            raise RuntimeError("YidStore is not ready yet")

        owner, repo = await _resolve_app_ref(
            hass, client, data.get("app_id"), data.get("owner"), data.get("repo")
        )
        if owner.lower() == PRIVATE_APPS_ORG.lower():
            if client.token:
                await client.test_auth()
                await client.refresh_token_repos(max_age=300)
            if not client.credential_for(owner, repo):
                raise RuntimeError("This app is not available")
        app_id = _app_id(owner, repo)

        # Something is already running for this app: wait for it first.
        running = _TASKS.get(app_id)
        if running and running["state"] not in ("done", "error"):
            running_action = running.get("action")
            final = await _wait_until_idle(app_id)
            if running_action in ("install", "update"):
                if final and final["state"] == "error":
                    raise RuntimeError(final.get("error") or "The running install failed")
                if not update:
                    slug = _find_slug(await _installed_addons(hass), _addon_slug(repo), local=True)
                    if slug:
                        return {"slug": slug, "installed": True, "updated": running_action == "update"}
            # A remove (or anything else) ran, or the app isn't actually
            # installed: carry on with the normal path below.

        # Already installed and no update asked for: nothing to do.
        try:
            info = await client.get_repo(owner, repo)
        except Exception:
            info = None
        manifest = await _app_manifest(hass, client, owner, info if isinstance(info, dict) else {"name": repo})
        addon_slug = manifest.get("slug") or _addon_slug(repo)
        installed = _find_slug(await _installed_addons(hass), addon_slug, local=True)
        if installed and not update:
            return {"slug": installed, "installed": True, "updated": False}

        if not connector_online():
            await setup_connector(hass)
        if not connector_online():
            raise RuntimeError("The YidStore Connector add-on is not running")

        slug = await install_app(hass, entry_id, owner, repo, update=bool(installed))
        # Only report "installed" when the Supervisor really has it.
        now_installed = await _installed_addons(hass)
        confirmed = _find_slug(now_installed, slug.removeprefix("local_"), local=True) \
            or _find_slug(now_installed, addon_slug, local=True)
        if not confirmed:
            raise RuntimeError("The app was not installed")
        return {"slug": confirmed, "installed": True, "updated": bool(installed)}
    except HomeAssistantError:
        raise
    except Exception as exc:
        raise HomeAssistantError(str(exc)) from exc


async def _run_uninstall(hass, app_id: str, owner: str, repo: str, addon_slug: str | None) -> None:
    try:
        _set_task(app_id, "removing", action="remove")
        slug = addon_slug or _addon_slug(repo)
        installed = _find_slug(await _installed_addons(hass), slug, local=True)
        if installed:
            res = await _sup(hass, "post", f"/addons/{installed}/uninstall", timeout=600)
            if not _ok(res):
                raise RuntimeError(f"Home Assistant could not remove the app: {_message(res)}")
        if connector_online():
            job_id = _enqueue_job("uninstall", owner, repo, _addon_slug(repo), None)
            await _wait_for_job(job_id, timeout=90)
        await _store_reload(hass)
        _set_task(app_id, "done", action="remove")
    except Exception as exc:
        _LOGGER.error("App %s remove failed: %s", repo, exc)
        _set_task(app_id, "error", str(exc))


# ---------------------------------------------------------------------------
# Updates in Home Assistant: stage new versions so the Supervisor offers them
# ---------------------------------------------------------------------------
#
# The Supervisor only knows a local add-on has an update once the newer files
# are in its folder. So when a newer release of an installed app exists,
# YidStore has the connector put the new files in place ("stage"). The
# running add-on keeps using its built image; Home Assistant then shows the
# update on the add-on page and under Settings -> Updates, and updates it
# with its own Update button (or the add-on's auto-update setting).

_STAGE_LOCK: asyncio.Lock | None = None
_STAGE_LAST: dict[str, float] = {"run": 0.0}
_STAGE_MIN_GAP = 600  # seconds between runs triggered by the Apps tab


def _bundled_connector_version() -> str | None:
    """Version of the connector shipped with this integration."""
    try:
        import yaml

        with open(_CONNECTOR_SRC / "config.yaml", encoding="utf-8") as fh:
            return str((yaml.safe_load(fh) or {}).get("version") or "") or None
    except Exception:
        return None


async def ensure_connector_current(hass) -> bool:
    """Update the connector from the built-in store when this integration
    ships a newer one. Returns True when an update was started."""
    bundled = await hass.async_add_executor_job(_bundled_connector_version)
    if not bundled:
        return False
    addon = next(
        (a for a in await _installed_addons(hass)
         if str(a.get("slug", "")).lower().endswith(f"_{CONNECTOR_SLUG}")
         and not str(a.get("slug", "")).startswith("local_")),
        None,
    )
    if not addon or not _is_newer(bundled, addon.get("version")):
        return False
    await hass.async_add_executor_job(_publish_store, _store_root(hass))
    await _store_reload(hass)
    _LOGGER.info("Updating the YidStore Connector to %s", bundled)
    ok, err = await _install_or_update(hass, addon["slug"], update=True)
    if not ok:
        _LOGGER.warning("Could not update the YidStore Connector: %s", err)
    return ok


async def stage_app_updates(hass, entry_id: str) -> list[str]:
    """Stage newer versions of installed apps; returns the repos staged."""
    global _STAGE_LOCK
    if _STAGE_LOCK is None:
        _STAGE_LOCK = asyncio.Lock()
    client = _client(hass, entry_id)
    if client is None or not supervisor_available(hass) or not connector_online():
        return []
    if _STAGE_LOCK.locked():
        return []
    async with _STAGE_LOCK:
        _STAGE_LAST["run"] = time.time()
        installed = {
            str(a.get("slug", "")).lower(): a for a in await _installed_addons(hass)
            if str(a.get("slug", "")).startswith("local_")
        }
        if not installed:
            return []
        staged: list[str] = []
        for owner, repo in await _list_app_repos(client):
            name = repo.get("name", "")
            if not name or repo.get("access_stopped"):
                continue
            try:
                manifest = await _app_manifest(hass, client, owner, repo)
            except Exception:
                continue
            latest = manifest.get("version")  # what the Supervisor will read
            if not latest or not manifest.get("has_config"):
                continue
            addon = installed.get(f"local_{(manifest.get('slug') or _addon_slug(name)).lower()}")
            if not addon or not _is_newer(latest, addon.get("version")):
                continue
            if not _is_newer(latest, addon.get("version_latest")):
                continue  # already staged: the Supervisor offers it
            app_id = _app_id(owner, name)
            busy = _task(app_id)
            if busy and busy["state"] not in ("done", "error"):
                continue
            job_id = _enqueue_job("stage", owner, name, _addon_slug(name), manifest.get("ref"))
            job = await _wait_for_job(job_id, timeout=300)
            if job.get("status") == "done":
                staged.append(f"{owner}/{name}")
            else:
                _LOGGER.debug("Staging %s/%s skipped: %s", owner, name, job.get("error") or job.get("status"))
        if staged:
            await _store_reload(hass)
            _LOGGER.info("App updates ready in Home Assistant: %s", ", ".join(staged))
        return staged


def _maybe_stage_soon(hass, entry_id: str) -> None:
    """From the Apps tab: stage at most every few minutes, in the background."""
    if time.time() - _STAGE_LAST["run"] < _STAGE_MIN_GAP:
        return
    _STAGE_LAST["run"] = time.time()
    hass.async_create_background_task(stage_app_updates(hass, entry_id), "yidstore_stage_app_updates")


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

async def autoinstall_app_entry(hass, client, owner: str, repo: str) -> dict | None:
    """An Auto-Install line that points at an app (Apps / PrivateApps org)."""
    if owner.lower() == PRIVATE_APPS_ORG.lower() and not client.credential_for(owner, repo):
        return None
    try:
        info = await client.get_repo(owner, repo)
    except Exception:
        info = {"name": repo}
    info = info if isinstance(info, dict) else {"name": repo}
    info.setdefault("name", repo)
    manifest = await _app_manifest(hass, client, owner, info)
    addon_slug = manifest.get("slug") or _addon_slug(repo)
    installed = await _installed_addons(hass) if supervisor_available(hass) else []
    sup_slug = _find_slug(installed, addon_slug, local=True)
    installed_version = None
    if sup_slug:
        installed_version = next((a.get("version") for a in installed if a.get("slug") == sup_slug), None)
    return {
        "owner": "",
        "repo_name": info.get("name") or repo,
        "name": manifest.get("name") or repo,
        "type": "app",
        "app_id": _app_id(owner, info.get("name") or repo),
        "is_installed": bool(sup_slug),
        "install_source": "yidstore" if sup_slug else None,
        "update_available": bool(sup_slug) and _is_newer(
            manifest.get("version") or manifest.get("tag_version"), installed_version
        ),
        "in_store": True,
    }


class AppsTaskView(HomeAssistantView):
    """Progress of one app's background job (or "connector")."""
    url = "/api/yidstore/apps/task/{key}"
    name = "api:yidstore:apps:task"
    requires_auth = True

    async def get(self, request: web.Request, key: str) -> web.Response:
        hass = request.app["hass"]
        if not _is_admin(request):
            return web.json_response({"error": "unauthorized"}, status=403)
        return web.json_response({
            "task": _task(key),
            "connector_online": connector_online(),
            "supervisor_available": supervisor_available(hass),
        })


class AppsReposView(HomeAssistantView):
    """The Apps tab: connector state plus every app with its live status."""
    url = "/api/yidstore/apps"
    name = "api:yidstore:apps"
    requires_auth = True

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        if not _is_admin(request):
            return web.json_response({"error": "unauthorized"}, status=403)
        if request.query.get("force") in ("1", "true", "yes"):
            # Full Reload: re-read versions, names, icons and org names.
            _MANIFEST_CACHE.clear()
            _ICON_CACHE.clear()
            _ORG_DISPLAY.clear()
        sup_ok = supervisor_available(hass)
        online = connector_online()
        out = {
            "supervisor_available": sup_ok,
            "connector_online": online,
            "connector_task": _task("connector"),
            "apps": [],
        }
        client = _client(hass, self.entry_id)
        if not (sup_ok and online and client):
            return web.json_response(out)

        try:
            app_repos = await _list_app_repos(client)
        except Exception as exc:
            _LOGGER.error("Could not load apps: %s", exc)
            out["error"] = "Could not load apps"
            return web.json_response(out)

        # Ids stay valid (Auto-Install also hands them out); private apps are
        # still refused without a token when an action runs.
        manifests = await asyncio.gather(
            *(_app_manifest(hass, client, owner, repo) for owner, repo in app_repos),
            return_exceptions=True,
        )
        installed = await _installed_addons(hass)

        rows = []
        info_tasks = []
        for (owner, repo), manifest in zip(app_repos, manifests):
            if isinstance(manifest, Exception):
                manifest = {}
            repo_name = repo.get("name", "")
            addon_slug = manifest.get("slug") or _addon_slug(repo_name)
            sup_slug = _find_slug(installed, addon_slug, local=True)
            rows.append((owner, repo, manifest, addon_slug, sup_slug))
            info_tasks.append(_sup(hass, "get", f"/addons/{sup_slug}/info") if sup_slug else asyncio.sleep(0))
        infos = await asyncio.gather(*info_tasks, return_exceptions=True)

        # Home Assistant moved add-on pages from the "hassio" panel to
        # Settings → Apps (/config/app/<slug>/…, web UI at /app/<slug>).
        legacy = "hassio" in (hass.data.get("frontend_panels") or {})

        def links(slug: str, ingress: bool) -> dict:
            if legacy:
                base = f"/hassio/addon/{slug}"
                open_path = f"/hassio/ingress/{slug}" if ingress else None
            else:
                base = f"/config/app/{slug}"
                open_path = f"/app/{slug}" if ingress else None
            return {
                "open_path": open_path,
                "info_path": f"{base}/info",
                "config_path": f"{base}/config",
                "logs_path": f"{base}/logs",
            }

        owner_names = {o.lower(): await _org_display(client, o) for o in {r[0] for r in rows}}

        for (owner, repo, manifest, addon_slug, sup_slug), info_res in zip(rows, infos):
            repo_name = repo.get("name", "")
            if not sup_slug and not manifest.get("has_config"):
                # Not an add-on repository (no config.yaml at the root).
                continue
            app_id = _app_id(owner, repo_name)
            info = _data(info_res) if sup_slug and not isinstance(info_res, Exception) else None
            info = info if isinstance(info, dict) else {}
            installed_version = info.get("version") if sup_slug else None
            latest = manifest.get("version") or manifest.get("tag_version")
            ingress = bool(info.get("ingress"))
            if repo.get("access_stopped"):
                await client.check_repo_access(owner, repo_name)
            out["apps"].append({
                "access_stopped": bool(repo.get("access_stopped")),
                "id": app_id,
                "name": manifest.get("name") or repo_name,
                "repo_name": repo_name,
                "owner_display": owner_names.get(owner.lower(), owner),
                "description": repo.get("description") or manifest.get("description") or "",
                "updated_at": repo.get("updated_at", ""),
                "latest_version": latest,
                "installed": bool(sup_slug),
                "installed_version": installed_version,
                "update_available": bool(sup_slug) and _is_newer(latest, installed_version),
                "state": info.get("state") if sup_slug else None,
                "boot": info.get("boot") if sup_slug else None,
                "watchdog": info.get("watchdog") if sup_slug else None,
                "ingress": ingress,
                "slug": sup_slug,
                **(links(sup_slug, ingress) if sup_slug else {}),
                "webui": info.get("webui") if sup_slug and not ingress else None,
                "task": _task(app_id),
            })
        if any(a.get("update_available") for a in out["apps"]):
            _maybe_stage_soon(hass, self.entry_id)
        return web.json_response(out)


class AppsActionView(HomeAssistantView):
    """install / update / remove / start / stop / restart / boot / watchdog."""
    url = "/api/yidstore/apps/action"
    name = "api:yidstore:apps:action"
    requires_auth = True

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        if not _is_admin(request):
            return web.json_response({"error": "unauthorized"}, status=403)
        data = await request.json()
        app_id = str(data.get("id") or "")
        action = str(data.get("action") or "")
        resolved = _APP_IDS.get(app_id)
        if not resolved:
            return web.json_response({"error": "Unknown app, refresh the list and try again"})
        owner, repo = resolved

        busy = _task(app_id)
        if busy and busy["state"] not in ("done", "error"):
            return web.json_response({"error": "This app is busy, please wait"})

        if action in ("install", "update"):
            _set_task(app_id, "starting", action=action)
            hass.async_create_background_task(
                _run_install(hass, self.entry_id, app_id, owner, repo, action == "update"),
                f"yidstore_app_{action}_{repo}",
            )
            return web.json_response({"success": True, "started": True})

        slug = str(data.get("slug") or "")
        installed = await _installed_addons(hass)
        if not slug or not any(a.get("slug") == slug for a in installed):
            slug = _find_slug(installed, _addon_slug(repo), local=True) or ""

        if action == "remove":
            hass.async_create_background_task(
                _run_uninstall(hass, app_id, owner, repo, slug.removeprefix("local_") or None),
                f"yidstore_app_remove_{repo}",
            )
            return web.json_response({"success": True, "started": True})

        if not slug:
            return web.json_response({"error": "This app is not installed"})
        if action in ("start", "stop", "restart"):
            res = await _sup(hass, "post", f"/addons/{slug}/{action}", timeout=300)
        elif action == "boot":
            res = await _sup(hass, "post", f"/addons/{slug}/options",
                             {"boot": "auto" if data.get("value") else "manual"})
        elif action == "watchdog":
            res = await _sup(hass, "post", f"/addons/{slug}/options", {"watchdog": bool(data.get("value"))})
        else:
            return web.json_response({"error": "Unknown action"})
        if not _ok(res):
            return web.json_response({"error": _message(res)})
        return web.json_response({"success": True})


class AppsIconView(HomeAssistantView):
    """An app's icon by its opaque id (img tags can't send auth)."""
    url = "/api/yidstore/apps/icon/{app_id}"
    name = "api:yidstore:apps:icon"
    requires_auth = False

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request, app_id: str) -> web.Response:
        hass = request.app["hass"]
        resolved = _APP_IDS.get(app_id)
        client = _client(hass, self.entry_id)
        if not resolved or client is None:
            return web.Response(status=404)
        data, ctype = await _fetch_icon(hass, client, *resolved)
        if not data:
            return web.Response(status=404)
        return web.Response(body=data, content_type=ctype, headers={"Cache-Control": "max-age=21600"})


class AddonStoreGitView(HomeAssistantView):
    """The built-in add-on store (bundled connector), over git smart HTTP."""
    url = STORE_GIT_PATH + "/{tail:.*}"
    name = "api:yidstore:store_git"
    requires_auth = False

    async def get(self, request: web.Request, tail: str = "") -> web.Response:
        from . import addon_store

        hass = request.app["hass"]
        if tail != "info/refs" or request.query.get("service") != "git-upload-pack":
            return web.Response(status=404)
        body = await hass.async_add_executor_job(
            addon_store.advertise_refs, _store_root(hass) / "repo.git"
        )
        if body is None:
            return web.Response(status=404)
        return web.Response(
            body=body,
            content_type="application/x-git-upload-pack-advertisement",
            headers={"Cache-Control": "no-cache"},
        )

    async def post(self, request: web.Request, tail: str = "") -> web.Response:
        from . import addon_store

        hass = request.app["hass"]
        if tail != "git-upload-pack":
            return web.Response(status=404)
        payload = await request.read()
        body = await hass.async_add_executor_job(
            addon_store.upload_pack,
            _store_root(hass) / "repo.git",
            payload,
            request.headers.get("Content-Encoding", ""),
        )
        return web.Response(
            body=body,
            content_type="application/x-git-upload-pack-result",
            headers={"Cache-Control": "no-cache"},
        )


class ConnectorSetupView(HomeAssistantView):
    """Install + start the bundled YidStore Connector (runs in the background)."""
    url = "/api/yidstore/connector/setup"
    name = "api:yidstore:connector:setup"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        if not _is_admin(request):
            return web.json_response({"error": "unauthorized"}, status=403)
        if not supervisor_available(hass):
            return web.json_response({"error": "Apps need Home Assistant OS or a Supervised installation"})
        task = _task("connector")
        if not task or task["state"] in ("done", "error"):
            _set_task("connector", "preparing")
            hass.async_create_background_task(_run_connector_setup(hass), "yidstore_connector_setup")
        return web.json_response({"success": True, "started": True})


class ConnectorJobsView(HomeAssistantView):
    """The connector polls this for pending jobs (and checks in)."""
    url = "/api/yidstore/connector/jobs"
    name = "api:yidstore:connector:jobs"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        # The connector reaches Core through the Supervisor's API proxy,
        # which authenticates as the Supervisor's admin system user.
        if not _is_admin(request):
            return web.json_response({"error": "unauthorized"}, status=403)
        _CONNECTOR_STATE["last_seen"] = time.time()
        return web.json_response({"jobs": _pending_jobs()})


class ConnectorFetchView(HomeAssistantView):
    """Hand the connector an app archive (downloaded here, server side)."""
    url = "/api/yidstore/connector/fetch/{job_id}"
    name = "api:yidstore:connector:fetch"
    requires_auth = True

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def get(self, request: web.Request, job_id: str) -> web.Response:
        hass = request.app["hass"]
        if not _is_admin(request):
            return web.json_response({"error": "unauthorized"}, status=403)
        _CONNECTOR_STATE["last_seen"] = time.time()
        job = _CONNECTOR_JOBS.get(job_id)
        if not job or job["action"] not in ("install", "stage"):
            return web.json_response({"error": "Unknown job"}, status=404)
        client = _client(hass, self.entry_id)
        if client is None:
            return web.json_response({"error": "YidStore is not ready yet"}, status=503)
        try:
            zip_bytes = await _fetch_app_archive(hass, client, job["owner"], job["repo"], job.get("ref"))
        except Exception as exc:
            _LOGGER.error("App download for %s failed: %s", job["repo"], exc)
            return web.json_response(
                {"error": "Could not download the app from the store. See the Home Assistant log."},
                status=502,
            )
        return web.Response(body=zip_bytes, content_type="application/zip")


class ConnectorResultView(HomeAssistantView):
    """The connector reports the outcome of a job here."""
    url = "/api/yidstore/connector/result"
    name = "api:yidstore:connector:result"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        if not _is_admin(request):
            return web.json_response({"error": "unauthorized"}, status=403)
        _CONNECTOR_STATE["last_seen"] = time.time()
        data = await request.json()
        job = _CONNECTOR_JOBS.get(str(data.get("id", "")))
        if job:
            job["status"] = "done" if data.get("ok") else "error"
            job["error"] = data.get("error")
            job["config_found"] = data.get("config_found")
            job["addon_slug"] = data.get("addon_slug")
        return web.json_response({"success": True})


class AppsDiagView(HomeAssistantView):
    """Diagnostics for the Apps feature (admins only)."""
    url = "/api/yidstore/apps/diag"
    name = "api:yidstore:apps:diag"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        if not _is_admin(request):
            return web.json_response({"error": "unauthorized"}, status=403)
        repos = await _sup(hass, "get", "/store/repositories")
        return web.json_response({
            "supervisor_available": supervisor_available(hass),
            "connector_online": connector_online(),
            "connector_task": _task("connector"),
            "store_url": await _store_url(hass),
            "store_repositories": [
                r.get("source") if isinstance(r, dict) else r for r in _list_of(repos, "repositories")
            ],
            "tasks": {k: v for k, v in _TASKS.items() if k == "connector"},
        })


async def async_setup_apps(hass: HomeAssistant, entry_id: str) -> list:
    """Register the Apps views, refresh the built-in store and schedule app
    update staging. Returns unsubscribe callbacks for the config entry."""
    for view in (
        AppsReposView(entry_id),
        AppsActionView(entry_id),
        AppsTaskView(),
        AppsIconView(entry_id),
        AppsDiagView(),
        AddonStoreGitView(),
        ConnectorSetupView(),
        ConnectorJobsView(),
        ConnectorFetchView(entry_id),
        ConnectorResultView(),
    ):
        hass.http.register_view(view)

    async def _refresh_store() -> None:
        # Keeps the served connector in step with this integration version,
        # so connector updates show up in Home Assistant's updates.
        try:
            await hass.async_add_executor_job(_publish_store, _store_root(hass))
        except Exception as exc:
            _LOGGER.debug("Could not publish the YidStore add-on store: %s", exc)

    hass.async_create_background_task(_refresh_store(), "yidstore_publish_store")

    from datetime import timedelta

    from homeassistant.helpers.event import async_call_later, async_track_time_interval

    async def _stage(_now=None) -> None:
        try:
            if supervisor_available(hass) and await ensure_connector_current(hass):
                return  # connector restarting; staging runs next time
            await stage_app_updates(hass, entry_id)
        except Exception as exc:
            _LOGGER.debug("App update staging failed: %s", exc)

    return [
        async_call_later(hass, 120, _stage),
        async_track_time_interval(hass, _stage, timedelta(hours=6)),
    ]
