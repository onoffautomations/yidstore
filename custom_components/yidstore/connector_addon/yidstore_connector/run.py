"""YidStore Connector.

Home Assistant Core cannot write to the Supervisor's local add-ons folder on
HA OS. This add-on can (``/addons`` is mapped read-write), so the YidStore
integration hands it jobs:

* ``install``   - download the app archive from YidStore and unpack it into
                  ``/addons/<slug>``
* ``uninstall`` - remove ``/addons/<slug>``

Everything goes through the Supervisor's Core API proxy using the add-on's
own SUPERVISOR_TOKEN. The archive is fetched by YidStore itself with the
integration's settings (including its token, if one is set), so this add-on
never needs a token, a password or the address of the app server.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

CORE_API = "http://supervisor/core/api/yidstore/connector"
ADDONS_DIR = Path("/addons")
OPTIONS_FILE = Path("/data/options.json")
SLUG_RE = re.compile(r"^[a-z0-9_]+$")
CONFIG_NAMES = ("config.yaml", "config.yml", "config.json")
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024


def log(msg: str) -> None:
    print(f"[yidstore-connector] {msg}", flush=True)


def poll_seconds() -> int:
    try:
        return max(5, int(json.loads(OPTIONS_FILE.read_text()).get("poll_seconds", 15)))
    except Exception:
        return 15


def request(path: str, data: dict | None = None, timeout: int = 30) -> bytes:
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        f"{CORE_API}/{path}",
        data=body,
        method="POST" if body is not None else "GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(MAX_ARCHIVE_BYTES + 1)


def _http_error_text(exc: urllib.error.HTTPError) -> str:
    """Prefer the message YidStore put in the response over the bare status."""
    try:
        body = json.loads(exc.read() or b"{}")
        if isinstance(body, dict) and body.get("error"):
            return str(body["error"])
    except Exception:
        pass
    return f"YidStore answered HTTP {exc.code}"


def _config_slug(addon_dir: Path) -> str | None:
    """The slug Home Assistant will use for the app (from its config file)."""
    for name in CONFIG_NAMES:
        path = addon_dir / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if name.endswith(".json"):
            try:
                return str(json.loads(text).get("slug") or "") or None
            except Exception:
                return None
        m = re.search(r"^slug:\s*[\"']?([A-Za-z0-9_\-]+)", text, re.M)
        return m.group(1) if m else None
    return None


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    root = dest.resolve()
    for member in zf.infolist():
        target = (dest / member.filename).resolve()
        if target != root and root not in target.parents:
            raise RuntimeError(f"Unsafe path in archive: {member.filename}")
    zf.extractall(dest)


def _addon_root(extracted: Path) -> Path:
    """The folder holding the add-on's config file.

    Archives usually wrap everything in one top-level folder; some repos
    keep the add-on one level deeper.
    """
    for candidate in [extracted, *sorted(p for p in extracted.iterdir() if p.is_dir())]:
        if any((candidate / n).is_file() for n in CONFIG_NAMES):
            return candidate
    entries = [p for p in extracted.iterdir() if not p.name.startswith(".")]
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        for candidate in [inner, *sorted(p for p in inner.iterdir() if p.is_dir())]:
            if any((candidate / n).is_file() for n in CONFIG_NAMES):
                return candidate
        return inner
    return extracted


def install(job: dict) -> dict:
    slug = job["slug"]
    try:
        data = request(f"fetch/{job['id']}", timeout=300)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(_http_error_text(exc)) from None
    if not data.startswith(b"PK"):
        raise RuntimeError("YidStore did not send a valid app archive")
    if len(data) > MAX_ARCHIVE_BYTES:
        raise RuntimeError("Archive is too large")

    with tempfile.TemporaryDirectory(prefix="yidstore_") as td:
        tmp = Path(td)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            _safe_extract(zf, tmp)
        src = _addon_root(tmp)

        ADDONS_DIR.mkdir(parents=True, exist_ok=True)
        dest = ADDONS_DIR / slug
        staging = ADDONS_DIR / f".{slug}.new"
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(src, staging)
        if dest.exists():
            shutil.rmtree(dest)
        staging.rename(dest)

    config_found = any((dest / n).is_file() for n in CONFIG_NAMES)
    addon_slug = _config_slug(dest)
    log(f"Installed {slug} (config found: {config_found}, app slug: {addon_slug})")
    return {"ok": True, "config_found": config_found, "addon_slug": addon_slug}


def uninstall(job: dict) -> dict:
    dest = ADDONS_DIR / job["slug"]
    if dest.exists():
        shutil.rmtree(dest)
    log(f"Removed {job['slug']}")
    return {"ok": True}


def handle(job: dict) -> None:
    result: dict = {"id": job.get("id")}
    try:
        if not SLUG_RE.match(str(job.get("slug", ""))):
            raise RuntimeError("Invalid app slug")
        action = job.get("action")
        if action == "install":
            result.update(install(job))
        elif action == "uninstall":
            result.update(uninstall(job))
        else:
            raise RuntimeError(f"Unknown action: {action}")
    except Exception as exc:  # report every failure back to YidStore
        log(f"Job {job.get('id')} failed: {exc}")
        result.update({"ok": False, "error": str(exc)})
    try:
        request("result", result)
    except Exception as exc:
        log(f"Could not report result: {exc}")


def main() -> None:
    log("Started")
    waiting_logged = False
    while True:
        try:
            jobs = json.loads(request("jobs")).get("jobs", [])
            if waiting_logged:
                log("Connected to YidStore")
                waiting_logged = False
            for job in jobs:
                handle(job)
            # Stay responsive right after work, otherwise poll at the set pace.
            time.sleep(2 if jobs else poll_seconds())
        except urllib.error.HTTPError as exc:
            if not waiting_logged:
                log(f"YidStore not reachable yet (HTTP {exc.code}). Is the YidStore integration set up?")
                waiting_logged = True
            time.sleep(30)
        except Exception as exc:
            if not waiting_logged:
                log(f"Waiting for Home Assistant: {exc}")
                waiting_logged = True
            time.sleep(15)


if __name__ == "__main__":
    main()
