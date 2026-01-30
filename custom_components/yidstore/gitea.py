from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)


class GiteaClient:
    def __init__(self, hass: HomeAssistant, base_url: str, token: str = None):
        self.hass = hass
        self.base_url = base_url.rstrip("/")
        self.token = token or None
        self._token_valid = True # Assume valid until proven otherwise

    def _headers(self, use_auth: bool = True) -> dict:
        """Get headers - with or without auth token."""
        headers = {"Accept": "application/json"}
        if use_auth and self.token and self._token_valid:
            headers["Authorization"] = f"token {self.token}"
        return headers

    async def test_auth(self) -> bool:
        """Test authentication - returns True if no token (public access)."""
        if not self.token:
            _LOGGER.info("No token - assuming public access")
            return True

        try:
            sess = async_get_clientsession(self.hass)
            url = f"{self.base_url}/api/v1/user"
            async with sess.get(url, headers=self._headers(), timeout=20) as resp:
                self._token_valid = (resp.status == 200)
                if not self._token_valid:
                    _LOGGER.warning("Gitea authentication failed - token may be expired or revoked")
                return self._token_valid
        except Exception as e:
            _LOGGER.debug("Auth test failed: %s", e)
            self._token_valid = False
            return False

    async def get_repo(self, owner: str, repo: str) -> dict:
        sess = async_get_clientsession(self.hass)
        url = f"{self.base_url}/api/v1/repos/{owner}/{repo}"
        async with sess.get(url, headers=self._headers(), timeout=30) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Repo fetch failed: {resp.status} {await resp.text()}")
            return await resp.json()

    async def get_org_repos(self, org: str) -> list[dict]:
        """Fetch all repositories for an organization."""
        sess = async_get_clientsession(self.hass)
        url = f"{self.base_url}/api/v1/orgs/{org}/repos"
        async with sess.get(url, headers=self._headers(), timeout=30) as resp:
            if resp.status != 200:
                _LOGGER.error("Failed to fetch repos for org %s: %s", org, resp.status)
                return []
            return await resp.json()

    async def get_user_repos(self, user: str) -> list[dict]:
        """Fetch all repositories for a user."""
        sess = async_get_clientsession(self.hass)
        url = f"{self.base_url}/api/v1/users/{user}/repos"
        async with sess.get(url, headers=self._headers(), timeout=30) as resp:
            if resp.status != 200:
                _LOGGER.error("Failed to fetch repos for user %s: %s", user, resp.status)
                return []
            return await resp.json()

    async def get_user_orgs(self) -> list[dict]:
        """Fetch all organizations the authenticated user belongs to."""
        if not self.token:
            return []
        sess = async_get_clientsession(self.hass)
        url = f"{self.base_url}/api/v1/user/orgs"
        async with sess.get(url, headers=self._headers(), timeout=30) as resp:
            if resp.status != 200:
                _LOGGER.debug("Failed to fetch user orgs: %s", resp.status)
                return []
            return await resp.json()

    async def get_current_user(self) -> dict | None:
        """Fetch the authenticated user's info."""
        if not self.token:
            return None
        sess = async_get_clientsession(self.hass)
        url = f"{self.base_url}/api/v1/user"
        try:
            async with sess.get(url, headers=self._headers(), timeout=20) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            _LOGGER.debug("Failed to fetch current user: %s", e)
        return None

    async def get_user_following(self) -> list[dict]:
        """Fetch users that the authenticated user is following."""
        if not self.token:
            return []
        sess = async_get_clientsession(self.hass)
        url = f"{self.base_url}/api/v1/user/following"
        try:
            async with sess.get(url, headers=self._headers(), timeout=30) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            _LOGGER.debug("Failed to fetch following: %s", e)
        return []

    async def get_org_info(self, org: str) -> dict | None:
        """Fetch organization information to get display name."""
        sess = async_get_clientsession(self.hass)
        url = f"{self.base_url}/api/v1/orgs/{org}"
        try:
            async with sess.get(url, headers=self._headers(), timeout=20) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            _LOGGER.debug("Failed to fetch org info for %s: %s", org, e)
        return None

    async def get_org_members(self, org: str) -> list[dict]:
        """Fetch all members of an organization."""
        sess = async_get_clientsession(self.hass)
        url = f"{self.base_url}/api/v1/orgs/{org}/members"
        try:
            async with sess.get(url, headers=self._headers(), timeout=30) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            _LOGGER.debug("Failed to fetch org members for %s: %s", org, e)
        return []

    async def get_user_info(self, user: str) -> dict | None:
        """Fetch user information to get display name."""
        sess = async_get_clientsession(self.hass)
        url = f"{self.base_url}/api/v1/users/{user}"
        try:
            async with sess.get(url, headers=self._headers(), timeout=20) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            _LOGGER.debug("Failed to fetch user info for %s: %s", user, e)
        return None

    async def get_releases(self, owner: str, repo: str) -> list[dict]:
        """Fetch all releases for a repository."""
        sess = async_get_clientsession(self.hass)
        url = f"{self.base_url}/api/v1/repos/{owner}/{repo}/releases"
        try:
            async with sess.get(url, headers=self._headers(), timeout=30) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            _LOGGER.debug("Failed to fetch releases for %s/%s: %s", owner, repo, e)
        return []

    async def get_readme(self, owner: str, repo: str) -> str | None:
        """Fetch the README content for a repository."""
        sess = async_get_clientsession(self.hass)
        # Try README.md, then readme.md, then README
        for name in ["README.md", "readme.md", "README"]:
            url = f"{self.base_url}/api/v1/repos/{owner}/{repo}/contents/{name}"
            try:
                async with sess.get(url, headers=self._headers(), timeout=20) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        import base64
                        content = base64.b64decode(data["content"]).decode("utf-8")
                        return content
            except Exception:
                continue
        return None

    async def get_latest_release(self, owner: str, repo: str) -> dict:
        sess = async_get_clientsession(self.hass)
        url = f"{self.base_url}/api/v1/repos/{owner}/{repo}/releases/latest"
        async with sess.get(url, headers=self._headers(), timeout=30) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Latest release fetch failed: {resp.status} {await resp.text()}")
            return await resp.json()

    async def get_release_by_tag(self, owner: str, repo: str, tag: str) -> dict:
        sess = async_get_clientsession(self.hass)
        url = f"{self.base_url}/api/v1/repos/{owner}/{repo}/releases/tags/{tag}"
        async with sess.get(url, headers=self._headers(), timeout=30) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Release-by-tag fetch failed: {resp.status} {await resp.text()}")
            return await resp.json()

    def pick_asset(self, release: dict, asset_name: str | None = None) -> dict:
        assets = release.get("assets") or []
        if not assets:
            raise RuntimeError("Release has no assets. Attach a ZIP asset to the release, or use mode=zipball.")

        if asset_name:
            for a in assets:
                if a.get("name") == asset_name:
                    return a
            raise RuntimeError(f"Asset '{asset_name}' not found in release assets.")

        # Prefer a single .zip
        zips = [a for a in assets if (a.get("name") or "").lower().endswith(".zip")]
        if len(zips) == 1:
            return zips[0]

        if len(assets) == 1:
            return assets[0]

        raise RuntimeError("Multiple assets found. Specify asset_name.")

    def archive_zip_url(self, owner: str, repo: str, ref: str) -> str:
        # Gitea archive endpoint (zip of repo at ref)
        # Example: /api/v1/repos/:owner/:repo/archive/:ref.zip
        return f"{self.base_url}/api/v1/repos/{owner}/{repo}/archive/{ref}.zip"

    async def search_repos(self, limit: int = 100) -> list[dict]:
        """Search for all accessible repositories."""
        sess = async_get_clientsession(self.hass)
        url = f"{self.base_url}/api/v1/repos/search?limit={limit}"
        try:
            async with sess.get(url, headers=self._headers(), timeout=60) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # The search API returns {"ok": true, "data": [...repos...]}
                    if isinstance(data, dict) and "data" in data:
                        return data["data"]
                    elif isinstance(data, list):
                        return data
        except Exception as e:
            _LOGGER.debug("Failed to search repos: %s", e)
        return []

    async def get_icon_url(self, owner: str, repo: str) -> str | None:
        """Get the URL for the repo's icon if it exists."""
        sess = async_get_clientsession(self.hass)
        # Try different icon paths
        icon_paths = [
            "icons/icon.png",
            "icons/icon@2x.png",
            "Icons/icon.png",
            "Icons/icon@2x.png",
            "icon.png",
        ]

        for path in icon_paths:
            url = f"{self.base_url}/api/v1/repos/{owner}/{repo}/contents/{path}"
            try:
                async with sess.get(url, headers=self._headers(), timeout=10) as resp:
                    if resp.status == 200:
                        # Return the raw download URL
                        return f"{self.base_url}/{owner}/{repo}/raw/branch/main/{path}"
            except Exception:
                continue

        return None

    def get_raw_icon_url(self, owner: str, repo: str, branch: str = "main") -> str:
        """Get the raw URL for potential icon files."""
        return f"{self.base_url}/{owner}/{repo}/raw/branch/{branch}/icons/icon.png"
