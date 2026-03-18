from __future__ import annotations

import io
import shutil
import tempfile
import zipfile
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import AUDIO_VENDOR_FOLDER, LOVELACE_VENDOR_FOLDER


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
        zf.extractall(extract_to)


def _install_integration_from_extracted(extracted_root: Path, ha_custom_components: Path) -> list[str]:
    import logging
    _LOGGER = logging.getLogger(__name__)

    cc = extracted_root / "custom_components"
    if not cc.exists():
        raise RuntimeError("Integration install expected 'custom_components/<domain>/' in the zip/zipball.")

    ha_custom_components.mkdir(parents=True, exist_ok=True)

    installed_domains: list[str] = []

    for domain_dir in cc.iterdir():
        if not domain_dir.is_dir():
            continue
        target = ha_custom_components / domain_dir.name
        if target.exists():
            shutil.rmtree(target)
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

    return installed_domains


def _find_main_js(dest: Path, repo_name: str) -> str | None:
    preferred = dest / f"{repo_name}.js"
    if preferred.exists():
        return preferred.name

    root_js = sorted([p for p in dest.glob("*.js") if p.is_file() and not p.name.endswith(".map")])
    if root_js:
        return root_js[0].name

    js_files = sorted([p for p in dest.rglob("*.js") if p.is_file() and not p.name.endswith(".map")])
    if not js_files:
        return None
    return js_files[0].name


def _install_lovelace_from_extracted(extracted_root: Path, ha_www_community: Path, repo_name: str) -> str:
    import logging
    _LOGGER = logging.getLogger(__name__)

    # Create destination: /config/www/community/onoff/<repo_name>
    # This only affects the specific repo folder, not other repos in /onoff/
    onoff_folder = ha_www_community / LOVELACE_VENDOR_FOLDER
    dest = onoff_folder / repo_name

    _LOGGER.info("Installing Lovelace card...")
    _LOGGER.info("  Vendor folder: %s", onoff_folder)
    _LOGGER.info("  Repo destination: %s", dest)

    # Ensure onoff vendor folder exists (won't delete it if it exists)
    onoff_folder.mkdir(parents=True, exist_ok=True)
    _LOGGER.info("✓ Vendor folder ready (other repos preserved)")

    # Remove only this specific repo folder if it exists (for clean reinstall)
    if dest.exists():
        _LOGGER.info("  Removing old installation: %s", dest)
        shutil.rmtree(dest)

    # Create fresh repo folder
    dest.mkdir(parents=True, exist_ok=True)
    _LOGGER.info("✓ Destination prepared: %s", dest)

    dist = extracted_root / "dist"
    if dist.exists() and dist.is_dir():
        _LOGGER.info("Found dist/ folder, copying files")
        _copytree_merge(dist, dest)
        main_js = _find_main_js(dest, repo_name)
        if not main_js:
            raise RuntimeError("Lovelace install: dist/ found but no .js files were found to register.")
        _LOGGER.info("Found main JS file: %s", main_js)
        return main_js

    repo_folder = extracted_root / repo_name
    if repo_folder.exists() and repo_folder.is_dir():
        _LOGGER.info("Found repo folder %s, copying files", repo_name)
        _copytree_merge(repo_folder, dest)
        main_js = _find_main_js(dest, repo_name)
        if not main_js:
            raise RuntimeError("Lovelace install: repo folder copied but no .js files were found to register.")
        _LOGGER.info("Found main JS file: %s", main_js)
        return main_js

    _LOGGER.info("Copying all files from root")
    _copytree_merge(extracted_root, dest)
    main_js = _find_main_js(dest, repo_name)
    if not main_js:
        raise RuntimeError("Lovelace install: no .js files were found to register.")
    _LOGGER.info("Found main JS file: %s", main_js)
    return main_js


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
) -> dict:
    """Install audio files under /config/www/audio/... or /config/media/audio/... preserving repo paths."""
    import logging

    _LOGGER = logging.getLogger(__name__)
    owner_slug = (owner or "").strip()
    if not owner_slug:
        raise RuntimeError("Audio install requires a repository owner.")

    location = (audio_location or "www").strip().lower()
    if location == "media":
        dest = ha_media_root / AUDIO_VENDOR_FOLDER / owner_slug / repo_name
    else:
        dest = ha_www_root / AUDIO_VENDOR_FOLDER / owner_slug / repo_name

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    copied = 0
    for src in extracted_root.rglob("*"):
        if not src.is_file() or not _is_audio_file(src):
            continue
        rel = src.relative_to(extracted_root)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        copied += 1

    if copied == 0:
        raise RuntimeError("Audio install: no audio files were found in the repository.")

    _LOGGER.info("Installed %d audio files to %s", copied, dest)
    return {"files_copied": copied, "dest_path": str(dest), "audio_location": location}


async def _download_zip_bytes(hass: HomeAssistant, url: str, headers: dict) -> bytes:
    sess = async_get_clientsession(hass)

    async def _get(u: str) -> bytes:
        async with sess.get(u, headers=headers, timeout=120) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Download failed: {resp.status} {await resp.text()}")
            return await resp.read()

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
        raise


async def install_package(
    hass: HomeAssistant,
    *,
    zip_bytes: bytes,
    package_type: str,
    repo_name: str,
    owner: str | None = None,
    audio_location: str = "www",
) -> dict:
    ha_custom_components = Path(hass.config.path("custom_components"))
    ha_www_community = Path(hass.config.path("www", "community"))
    ha_www_root = Path(hass.config.path("www"))
    ha_media_root = Path(hass.config.path("media"))
    ha_blueprints_root = Path(hass.config.path("blueprints"))
    def _work() -> dict:
        with tempfile.TemporaryDirectory(prefix="yidstore_") as td:
            extract_dir = Path(td)
            _extract_zip_bytes(zip_bytes, extract_dir)
            root = _detect_single_top_folder(extract_dir)

            if package_type == "integration":
                installed_domains = _install_integration_from_extracted(root, ha_custom_components)
                return {"domains": installed_domains}

            if package_type == "lovelace":
                main_js = _install_lovelace_from_extracted(root, ha_www_community, repo_name)
                dest_url = f"/local/community/{LOVELACE_VENDOR_FOLDER}/{repo_name}/{main_js}"
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
) -> dict:
    zip_bytes = await _download_zip_bytes(hass, url, headers=headers)
    return await install_package(
        hass,
        zip_bytes=zip_bytes,
        package_type=package_type,
        repo_name=repo_name,
        owner=owner,
        audio_location=audio_location,
    )


def uninstall_package(
    hass: HomeAssistant,
    package_type: str,
    repo_name: str,
    owner: str | None = None,
    domain: str | None = None,
) -> None:
    """Best effort uninstall of a package by deleting its folder."""
    import logging
    _LOGGER = logging.getLogger(__name__)
    
    if package_type == "lovelace":
        dest = Path(hass.config.path("www", "community", LOVELACE_VENDOR_FOLDER, repo_name))
        if dest.exists():
            _LOGGER.info("Uninstalling Lovelace card: %s", dest)
            shutil.rmtree(dest)

    elif package_type == "audio":
        if not owner:
            _LOGGER.warning("Audio uninstall skipped: missing owner for repo %s", repo_name)
            return
        dest_www = Path(hass.config.path("www", AUDIO_VENDOR_FOLDER, owner, repo_name))
        if dest_www.exists():
            _LOGGER.info("Uninstalling audio package: %s", dest_www)
            shutil.rmtree(dest_www)
        dest_media = Path(hass.config.path("media", AUDIO_VENDOR_FOLDER, owner, repo_name))
        if dest_media.exists():
            _LOGGER.info("Uninstalling audio package: %s", dest_media)
            shutil.rmtree(dest_media)
            
    elif package_type == "integration":
        domains: list[str] = []
        if domain:
            domains.append(domain.lower())
            domains.append(domain.lower().replace("-", "_"))
        domains.append(repo_name.lower())
        domains.append(repo_name.lower().replace("-", "_"))
        seen: set[str] = set()
        cc_root = Path(hass.config.path("custom_components"))
        for dom in domains:
            if not dom or dom in seen:
                continue
            seen.add(dom)
            dest = cc_root / dom
            if dest.exists():
                _LOGGER.info("Uninstalling integration: %s", dest)
                shutil.rmtree(dest)
