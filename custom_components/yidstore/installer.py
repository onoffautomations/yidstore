from __future__ import annotations

import io
import shutil
import tempfile
import zipfile
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import LOVELACE_VENDOR_FOLDER


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


def _install_integration_from_extracted(extracted_root: Path, ha_custom_components: Path, ha_brands_path: Path = None) -> None:
    import logging
    _LOGGER = logging.getLogger(__name__)

    cc = extracted_root / "custom_components"
    if not cc.exists():
        raise RuntimeError("Integration install expected 'custom_components/<domain>/' in the zip/zipball.")

    ha_custom_components.mkdir(parents=True, exist_ok=True)

    for domain_dir in cc.iterdir():
        if not domain_dir.is_dir():
            continue
        target = ha_custom_components / domain_dir.name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(domain_dir, target)

        # Look for icons in common locations and sync to brands + integration folder
        icons_folders = [
            extracted_root / "icons",
            extracted_root / "Icons",
            domain_dir / "icons",
            domain_dir / "Icons",
            domain_dir,
        ]

        icon_files = []
        for folder in icons_folders:
            if folder.exists() and folder.is_dir():
                icon_files.extend([p for p in folder.iterdir() if p.is_file()])

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

            # Copy to brands directory for HA brands system
            if ha_brands_path:
                brand_target = ha_brands_path / domain_dir.name
                brand_target.mkdir(parents=True, exist_ok=True)

                for icon_file in icon_files:
                    dest_file = brand_target / icon_file.name
                    shutil.copy2(icon_file, dest_file)
                    _LOGGER.info("Copied to brands: %s", icon_file.name)

                if main_icon and main_icon.name.lower() != 'icon.png':
                    dest_icon = brand_target / "icon.png"
                    shutil.copy2(main_icon, dest_icon)
                    _LOGGER.info("Created icon.png in brands from %s", main_icon.name)

            # Copy to the integration folder for HA 2023.2+ local icons
            for icon_file in icon_files:
                dest_file = target / icon_file.name
                shutil.copy2(icon_file, dest_file)

            if main_icon:
                dest_icon = target / "icon.png"
                if not dest_icon.exists() or main_icon.name.lower() != 'icon.png':
                    shutil.copy2(main_icon, dest_icon)
                    _LOGGER.info("Created icon.png in integration from %s", main_icon.name)

            if icon_2x:
                dest_icon_2x = target / "icon@2x.png"
                if not dest_icon_2x.exists():
                    shutil.copy2(icon_2x, dest_icon_2x)
                    _LOGGER.info("Created icon@2x.png in integration")

            if logo:
                dest_logo = target / "logo.png"
                if not dest_logo.exists():
                    shutil.copy2(logo, dest_logo)
                    _LOGGER.info("Created logo.png in integration")

            _LOGGER.info("Icons installed for %s", domain_dir.name)


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
) -> dict:
    ha_custom_components = Path(hass.config.path("custom_components"))
    ha_www_community = Path(hass.config.path("www", "community"))
    ha_blueprints_root = Path(hass.config.path("blueprints"))
    # Brands directory for custom integration icons
    ha_brands = Path(hass.config.path("www", "brands"))

    def _work() -> dict:
        with tempfile.TemporaryDirectory(prefix="onoff_gitea_store_") as td:
            extract_dir = Path(td)
            _extract_zip_bytes(zip_bytes, extract_dir)
            root = _detect_single_top_folder(extract_dir)

            if package_type == "integration":
                _install_integration_from_extracted(root, ha_custom_components, ha_brands)
                return {}

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

            raise RuntimeError("Invalid package_type. Must be integration|lovelace|blueprints.")

    return await hass.async_add_executor_job(_work)


async def download_and_install(
    hass: HomeAssistant,
    *,
    url: str,
    headers: dict,
    package_type: str,
    repo_name: str,
) -> dict:
    zip_bytes = await _download_zip_bytes(hass, url, headers=headers)
    return await install_package(hass, zip_bytes=zip_bytes, package_type=package_type, repo_name=repo_name)


def uninstall_package(hass: HomeAssistant, package_type: str, repo_name: str) -> None:
    """Best effort uninstall of a package by deleting its folder."""
    import logging
    _LOGGER = logging.getLogger(__name__)
    
    if package_type == "lovelace":
        dest = Path(hass.config.path("www", "community", LOVELACE_VENDOR_FOLDER, repo_name))
        if dest.exists():
            _LOGGER.info("Uninstalling Lovelace card: %s", dest)
            shutil.rmtree(dest)
            
    elif package_type == "integration":
        # Best effort: domain name is often repo name or underscore version
        domains = [repo_name.lower(), repo_name.lower().replace("-", "_")]
        cc_root = Path(hass.config.path("custom_components"))
        for dom in domains:
            dest = cc_root / dom
            if dest.exists():
                _LOGGER.info("Uninstalling integration: %s", dest)
                shutil.rmtree(dest)
                break
