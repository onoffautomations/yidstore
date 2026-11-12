from __future__ import annotations

import io
import json
import logging
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import AUDIO_VENDOR_FOLDER, LOVELACE_VENDOR_FOLDER

_LOGGER = logging.getLogger(__name__)


# Names that end up in file paths. Owner and repository names follow
# GitHub/Gitea rules; integration domains follow Home Assistant's.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_DOMAIN_RE = re.compile(r"^[a-z0-9_]{1,64}$")
# Never overwritten or removed by a package.
PROTECTED_DOMAINS = frozenset({"yidstore", "hacs", "homeassistant"})
# Limits for downloaded archives.
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
MAX_UNPACKED_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 20000


def valid_name(value) -> bool:
    """A safe owner or repository name (no path separators, not . or ..)."""
    return isinstance(value, str) and bool(_NAME_RE.match(value))


def valid_domain(value) -> bool:
    """A Home Assistant integration domain (lowercase letters, digits, _)."""
    return isinstance(value, str) and bool(_DOMAIN_RE.match(value))


def _inside(base: Path, path: Path) -> bool:
    """True when ``path`` is strictly inside ``base`` (after resolving)."""
    try:
        b = base.resolve()
        p = path.resolve()
    except OSError:
        return False
    return p != b and b in p.parents


def _safe_rmtree(path: Path, base: Path) -> None:
    """Delete a folder, but only one that sits inside ``base``."""
    if path.is_symlink():
        raise RuntimeError(f"Refusing to delete {path}: it is a link")
    if not path.exists():
        return
    if not _inside(base, path):
        raise RuntimeError(f"Refusing to delete {path}: it is outside {base}")
    shutil.rmtree(path)


def _swap_into_place(staging: Path, target: Path, base: Path) -> None:
    """Replace ``target`` with the fully prepared ``staging`` folder, so a
    failed or interrupted install never leaves a half-copied package."""
    old = target.with_name(f".{target.name}.yidstore_old")
    _safe_rmtree(old, base)
    if target.exists():
        target.rename(old)
    try:
        staging.rename(target)
    except Exception:
        if old.exists() and not target.exists():
            old.rename(target)
        raise
    _safe_rmtree(old, base)


def _detect_single_top_folder(extract_dir: Path) -> Path:
    children = [p for p in extract_dir.iterdir()]
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return extract_dir


def _copytree_merge(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def _extract_zip_bytes(zip_bytes: bytes, extract_to: Path) -> None:
    extract_to.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise RuntimeError("The package archive has too many files.")
        if sum(i.file_size for i in infos) > MAX_UNPACKED_BYTES:
            raise RuntimeError("The package archive is too large when unpacked.")
        # extractall drops "..", absolute paths and drive letters, and never
        # creates links; the checks above stop zip bombs.
        zf.extractall(extract_to)


def _install_integration_from_extracted(extracted_root: Path, ha_custom_components: Path) -> list[str]:
    import logging
    _LOGGER = logging.getLogger(__name__)

    ha_custom_components.mkdir(parents=True, exist_ok=True)
    installed_domains: list[str] = []

    # Standard repository/source archive layout:
    #   custom_components/<domain>/...
    cc = extracted_root / "custom_components"
    if cc.exists() and cc.is_dir():
        domain_dirs = [p for p in cc.iterdir() if p.is_dir()]
    else:
        # HACS zip_release layout. HACS release ZIPs contain the *contents* of
        # custom_components/<domain> at the archive root (manifest.json,
        # __init__.py, etc.), not a custom_components/ wrapper.
        manifest_path = extracted_root / "manifest.json"
        if not manifest_path.is_file():
            raise RuntimeError(
                "Integration install expected either 'custom_components/<domain>/' "
                "or a HACS zip_release archive with manifest.json at its root."
            )

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as err:
            raise RuntimeError("HACS zip_release contains an invalid manifest.json.") from err

        domain = manifest.get("domain") if isinstance(manifest, dict) else None
        domain = domain.strip() if isinstance(domain, str) else None
        if not valid_domain(domain):
            raise RuntimeError("HACS zip_release manifest.json is missing a valid integration domain.")
        if domain in PROTECTED_DOMAINS:
            raise RuntimeError(f"A package can't replace the {domain} integration.")

        target = ha_custom_components / domain
        staging = ha_custom_components / f".{domain}.yidstore_new"
        _safe_rmtree(staging, ha_custom_components)
        shutil.copytree(extracted_root, staging)
        _swap_into_place(staging, target, ha_custom_components)
        installed_domains.append(domain)
        _LOGGER.info("Installed HACS zip_release integration: %s", domain)
        return installed_domains

    for domain_dir in domain_dirs:
        if not valid_domain(domain_dir.name):
            raise RuntimeError(f"Invalid integration folder name in the package: {domain_dir.name!r}")
        if domain_dir.name in PROTECTED_DOMAINS:
            raise RuntimeError(f"A package can't replace the {domain_dir.name} integration.")
        final_target = ha_custom_components / domain_dir.name
        # Build the new copy next to the old one, swap it in at the end.
        target = ha_custom_components / f".{domain_dir.name}.yidstore_new"
        _safe_rmtree(target, ha_custom_components)
        shutil.copytree(domain_dir, target)
        installed_domains.append(domain_dir.name)

        # Look for branding files in common locations and store them under
        # custom_components/<domain>/brand for Home Assistant branding pickup.
        icons_folders = [
            domain_dir / "brand",
            domain_dir / "Brand",
            extracted_root / "icons",
            extracted_root / "Icons",
            domain_dir / "icons",
            domain_dir / "Icons",
        ]

        icon_files = []
        for folder in icons_folders:
            if folder.exists() and folder.is_dir():
                icon_files.extend(
                    [
                        p for p in folder.iterdir()
                        if p.is_file() and p.suffix.lower() in {".png", ".svg", ".jpg", ".jpeg", ".webp"}
                    ]
                )

        if icon_files:
            _LOGGER.info("Found %d icon files for %s", len(icon_files), domain_dir.name)

            # Find icon files - look for common naming patterns
            main_icon = None
            icon_2x = None
            logo = None

            for icon_file in icon_files:
                name_lower = icon_file.name.lower()
                if name_lower in ['icon.png', 'icon.svg']:
                    main_icon = icon_file
                elif name_lower in ['icon@2x.png', 'icon_2x.png']:
                    icon_2x = icon_file
                elif name_lower in ['logo.png', 'logo.svg']:
                    logo = icon_file
                elif name_lower.endswith('.png') or name_lower.endswith('.svg'):
                    if main_icon is None:
                        main_icon = icon_file

            # Copy to custom_components/<domain>/brand.
            brand_target = target / "brand"
            brand_target.mkdir(parents=True, exist_ok=True)
            for icon_file in icon_files:
                dest_file = brand_target / icon_file.name
                shutil.copy2(icon_file, dest_file)

            if main_icon:
                dest_icon = brand_target / "icon.png"
                if not dest_icon.exists() or main_icon.name.lower() != 'icon.png':
                    shutil.copy2(main_icon, dest_icon)
                    _LOGGER.info("Created brand/icon.png from %s", main_icon.name)

            if icon_2x:
                dest_icon_2x = brand_target / "icon@2x.png"
                if not dest_icon_2x.exists():
                    shutil.copy2(icon_2x, dest_icon_2x)
                    _LOGGER.info("Created brand/icon@2x.png")

            if logo:
                dest_logo = brand_target / "logo.png"
                if not dest_logo.exists():
                    shutil.copy2(logo, dest_logo)
                    _LOGGER.info("Created brand/logo.png")

            _LOGGER.info("Icons installed for %s", domain_dir.name)

        _swap_into_place(target, final_target, ha_custom_components)

    return installed_domains


def _find_hacs_filename(extracted_root: Path) -> str | None:
    hacs_path = extracted_root / "hacs.json"
    if not hacs_path.is_file():
        return None

    try:
        data = json.loads(hacs_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    filename = data.get("filename") if isinstance(data, dict) else None
    if not isinstance(filename, str):
        return None
    filename = filename.strip().lstrip("/").replace("\\", "/")
    if filename.lower().endswith(".js") and ".." not in filename.split("/"):
        return filename
    return None


def _find_main_js(dest: Path, repo_name: str, preferred_name: str | None = None) -> str | None:
    if preferred_name:
        candidates = [preferred_name]
        if preferred_name.startswith("dist/"):
            candidates.append(preferred_name[5:])
        candidates.append(f"dist/{preferred_name}")
        for candidate in candidates:
            preferred_path = dest / candidate
            if preferred_path.is_file():
                return str(preferred_path.relative_to(dest)).replace("\\", "/")

    preferred = dest / f"{repo_name}.js"
    if preferred.exists():
        return preferred.name

    root_js = sorted([p for p in dest.glob("*.js") if p.is_file() and not p.name.endswith(".map")])
    if root_js:
        return root_js[0].name

    js_files = sorted([p for p in dest.rglob("*.js") if p.is_file() and not p.name.endswith(".map")])
    if not js_files:
        return None
    return str(js_files[0].relative_to(dest)).replace("\\", "/")


def _install_lovelace_from_extracted(
    extracted_root: Path,
    ha_www_community: Path,
    repo_name: str,
    *,
    use_vendor_folder: bool = True,
) -> tuple[str, str]:
    """Install a card into www/community[/onoff]/<repo>, built in a temporary
    folder and swapped into place only once it is complete."""
    if not valid_name(repo_name):
        raise RuntimeError(f"Invalid card repository name: {repo_name!r}")

    # Gitea store cards are namespaced under onoff; GitHub custom cards use
    # the normal HACS-style community/<repo> path.
    base_folder = ha_www_community / LOVELACE_VENDOR_FOLDER if use_vendor_folder else ha_www_community
    dest = base_folder / repo_name
    work = base_folder / f".{repo_name}.yidstore_new"
    hacs_filename = _find_hacs_filename(extracted_root)

    base_folder.mkdir(parents=True, exist_ok=True)
    _safe_rmtree(work, base_folder)
    work.mkdir(parents=True, exist_ok=True)

    dist = extracted_root / "dist"
    repo_folder = extracted_root / repo_name
    if dist.is_dir():
        source, what = dist, "dist/ found but no .js files were found to register."
    elif repo_folder.is_dir():
        source, what = repo_folder, "repo folder copied but no .js files were found to register."
    else:
        source, what = extracted_root, "no .js files were found to register."
    _copytree_merge(source, work)
    main_js = _find_main_js(work, repo_name, hacs_filename)
    if not main_js:
        _safe_rmtree(work, base_folder)
        raise RuntimeError(f"Lovelace install: {what}")

    _swap_into_place(work, dest, base_folder)
    _LOGGER.info("Installed card %s (%s)", repo_name, main_js)
    return main_js, str(dest.relative_to(ha_www_community)).replace("\\", "/")


def _install_blueprints_from_extracted(extracted_root: Path, ha_blueprints_root: Path) -> None:
    bp = extracted_root / "blueprints"
    if not bp.exists():
        raise RuntimeError("Blueprint install expected a top-level 'blueprints/' folder in the repo.")
    _copytree_merge(bp, ha_blueprints_root)


def _is_audio_file(path: Path) -> bool:
    audio_exts = {
        ".mp3",
        ".wav",
        ".ogg",
        ".m4a",
        ".flac",
        ".aac",
        ".opus",
        ".webm",
    }
    return path.suffix.lower() in audio_exts


def _install_audio_from_extracted(
    extracted_root: Path,
    ha_www_root: Path,
    ha_media_root: Path,
    owner: str,
    repo_name: str,
    audio_location: str = "www",
    selected_files: list | None = None,
    subfolder: str | None = None,
) -> dict:
    """Install audio files under /config/www/<folder>/... or /config/media/<folder>/...

    ``selected_files`` (relative POSIX paths) limits the install to specific
    tracks; when None, all audio files are installed. ``subfolder`` overrides
    the default ``<owner>/<repo>`` destination so users can choose where the
    tracks land.
    """
    import logging

    _LOGGER = logging.getLogger(__name__)
    owner_slug = (owner or "").strip()
    if not owner_slug:
        raise RuntimeError("Audio install requires a repository owner.")
    if not valid_name(owner_slug) or not valid_name(repo_name):
        raise RuntimeError("Invalid audio package name.")

    location = (audio_location or "www").strip().lower()
    base = ha_media_root if location == "media" else ha_www_root

    # Sanitize a user-chosen subfolder (no traversal, no leading slash).
    if subfolder:
        clean = "/".join(
            p for p in str(subfolder).replace("\\", "/").split("/")
            if p and p not in (".", "..")
        )
        rel_dest = clean or f"{owner_slug}/{repo_name}"
    else:
        rel_dest = f"{owner_slug}/{repo_name}"
    dest = base / AUDIO_VENDOR_FOLDER / rel_dest
    if not _inside(base, dest):
        raise RuntimeError("Invalid audio destination folder.")

    # Normalize the selected-file set for matching against repo-relative paths.
    selected = None
    if selected_files:
        selected = {str(s).replace("\\", "/").lstrip("/") for s in selected_files}

    # Merge into an existing folder rather than wiping it, so installing a few
    # more tracks doesn't remove previously installed ones.
    dest.mkdir(parents=True, exist_ok=True)

    copied = 0
    for src in extracted_root.rglob("*"):
        if not src.is_file() or not _is_audio_file(src):
            continue
        rel = src.relative_to(extracted_root).as_posix()
        if selected is not None and rel not in selected:
            continue
        # Flatten into the destination by filename when a subfolder was chosen;
        # otherwise preserve the repo's structure.
        target = dest / (Path(rel).name if subfolder else rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        copied += 1

    if copied == 0:
        raise RuntimeError("Audio install: no matching audio files were found.")

    _LOGGER.info("Installed %d audio files to %s", copied, dest)
    return {
        "files_copied": copied,
        "dest_path": str(dest),
        "audio_location": location,
        "subfolder": rel_dest,
    }


async def _download_zip_bytes(hass: HomeAssistant, url: str, headers: dict) -> bytes:
    from .gitea import safe_get

    async def _get(u: str) -> bytes:
        # safe_get never follows redirects with a credential to another
        # host, and only sends one to the store host at all.
        status, body, _final = await safe_get(hass, u, headers, timeout=120, max_bytes=MAX_ARCHIVE_BYTES)
        if len(body) > MAX_ARCHIVE_BYTES:
            raise RuntimeError("Download failed: the package is too large.")
        if status != 200:
            text = body[:2000].decode("utf-8", "replace")
            logging.getLogger(__name__).debug("Download failed: HTTP %s", status)
            # Preserve the marker the retry logic below looks for, without
            # exposing the URL to the user.
            hint = "unrecognized repository reference" if "unrecognized repository reference" in text else ""
            raise RuntimeError(f"Download failed: {status} {hint}".strip())
        return body

    try:
        return await _get(url)
    except RuntimeError as err:
        msg = str(err)
        if "unrecognized repository reference" in msg and "/archive/" in url and url.endswith(".zip"):
            marker = "/archive/"
            idx = url.find(marker)
            if idx != -1:
                prefix = url[: idx + len(marker)]
                rest = url[idx + len(marker) :]
                if not rest.startswith("v"):
                    retry_url = prefix + "v" + rest
                    return await _get(retry_url)
        # GitHub archive fallback: repo has no releases and its default
        # branch is master, not main.
        if "github.com/" in url and url.endswith("/archive/main.zip") and "404" in msg:
            return await _get(url.replace("/archive/main.zip", "/archive/master.zip"))
        raise


async def install_package(
    hass: HomeAssistant,
    *,
    zip_bytes: bytes,
    package_type: str,
    repo_name: str,
    owner: str | None = None,
    audio_location: str = "www",
    audio_files: list | None = None,
    audio_subfolder: str | None = None,
    source: str | None = None,
) -> dict:
    ha_custom_components = Path(hass.config.path("custom_components"))
    ha_www_community = Path(hass.config.path("www", "community"))
    ha_www_root = Path(hass.config.path("www"))
    ha_media_root = Path(hass.config.path("media"))
    ha_blueprints_root = Path(hass.config.path("blueprints"))
    # /hacsfiles/ is served only by the legacy HACS custom integration. The
    # built-in Marketplace (HA 2026.11+) uses /local/community/, which
    # always works, so prefer that whenever HACS isn't the one serving files.
    from ._utils import community_store_serves_hacsfiles

    hacs_present = community_store_serves_hacsfiles(hass)

    def _work() -> dict:
        with tempfile.TemporaryDirectory(prefix="yidstore_") as td:
            extract_dir = Path(td)
            _extract_zip_bytes(zip_bytes, extract_dir)
            root = _detect_single_top_folder(extract_dir)

            if package_type == "integration":
                installed_domains = _install_integration_from_extracted(root, ha_custom_components)
                return {"domains": installed_domains}

            if package_type == "lovelace":
                is_github = source == "github"
                main_js, relative_dest = _install_lovelace_from_extracted(
                    root,
                    ha_www_community,
                    repo_name,
                    use_vendor_folder=not is_github,
                )
                if is_github and hacs_present:
                    dest_url = f"/hacsfiles/{repo_name}/{main_js}"
                else:
                    dest_url = f"/local/community/{relative_dest}/{main_js}"
                result = {"main_js": main_js, "dest_url": dest_url}
                import logging
                _LOGGER = logging.getLogger(__name__)
                _LOGGER.info("Lovelace install complete: %s", result)
                return result

            if package_type == "blueprints":
                _install_blueprints_from_extracted(root, ha_blueprints_root)
                return {}

            if package_type == "audio":
                return _install_audio_from_extracted(
                    root,
                    ha_www_root,
                    ha_media_root,
                    owner or "",
                    repo_name,
                    audio_location=audio_location,
                    selected_files=audio_files,
                    subfolder=audio_subfolder,
                )

            raise RuntimeError("Invalid package_type. Must be integration|lovelace|blueprints|audio.")

    return await hass.async_add_executor_job(_work)


async def download_and_install(
    hass: HomeAssistant,
    *,
    url: str,
    headers: dict,
    package_type: str,
    repo_name: str,
    owner: str | None = None,
    audio_location: str = "www",
    audio_files: list | None = None,
    audio_subfolder: str | None = None,
    source: str | None = None,
) -> dict:
    zip_bytes = await _download_zip_bytes(hass, url, headers=headers)
    return await install_package(
        hass,
        zip_bytes=zip_bytes,
        package_type=package_type,
        repo_name=repo_name,
        owner=owner,
        audio_location=audio_location,
        audio_files=audio_files,
        audio_subfolder=audio_subfolder,
        source=source,
    )


def uninstall_package(
    hass: HomeAssistant,
    package_type: str,
    repo_name: str,
    owner: str | None = None,
    domain: str | None = None,
) -> None:
    """Delete an installed package's folder.

    Names are checked strictly and every delete must stay inside the
    package type's own folder, so a bad name can never remove anything else.
    Add-ons are removed through the Apps tab (the connector), not here.
    """
    if not valid_name(repo_name) or (owner is not None and owner != "" and not valid_name(owner)):
        raise RuntimeError(f"Invalid package name: {owner}/{repo_name}")

    if package_type == "lovelace":
        community = Path(hass.config.path("www", "community"))
        for base in (community / LOVELACE_VENDOR_FOLDER, community):
            dest = base / repo_name
            if dest.exists():
                _LOGGER.info("Uninstalling card: %s", dest)
                _safe_rmtree(dest, base)

    elif package_type == "audio":
        if not owner:
            _LOGGER.warning("Audio uninstall skipped: missing owner for repo %s", repo_name)
            return
        for root in ("www", "media"):
            base = Path(hass.config.path(root, AUDIO_VENDOR_FOLDER))
            dest = base / owner / repo_name
            if dest.exists():
                _LOGGER.info("Uninstalling audio package: %s", dest)
                _safe_rmtree(dest, base)

    elif package_type == "integration":
        candidates = []
        if domain:
            candidates += [domain.lower(), domain.lower().replace("-", "_")]
        candidates += [repo_name.lower(), repo_name.lower().replace("-", "_")]
        cc_root = Path(hass.config.path("custom_components"))
        seen: set[str] = set()
        for dom in candidates:
            if dom in seen or not valid_domain(dom) or dom in PROTECTED_DOMAINS:
                continue
            seen.add(dom)
            dest = cc_root / dom
            if dest.exists():
                _LOGGER.info("Uninstalling integration: %s", dest)
                _safe_rmtree(dest, cc_root)
