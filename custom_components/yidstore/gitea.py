from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Callable
from urllib.parse import urljoin, urlsplit

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

# Hosts (scheme, host, port) a token or repo key may be sent to: only the
# configured store server. Filled in by GiteaClient; checked by every request
# that carries a credential, including downloads in installer.py.
_AUTH_ORIGINS: set[tuple[str, str, int | None]] = set()

_REDIRECTS = (301, 302, 303, 307, 308)
_MAX_REDIRECTS = 5


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    port = parts.port or (443 if scheme == "https" else 80 if scheme == "http" else None)
    return scheme, (parts.hostname or "").lower(), port


def credential_allowed(url: str) -> bool:
    """True if a token / repo key may be sent to this URL (store host only)."""
    return _origin(url) in _AUTH_ORIGINS


def strip_unsafe_auth(url: str, headers: dict | None) -> dict:
    """Copy of headers without Authorization unless the URL is the store host."""
    out = dict(headers or {})
    if not credential_allowed(url):
        for key in list(out):
            if key.lower() == "authorization":
                out.pop(key)
    return out


async def safe_get(hass, url: str, headers: dict | None = None, timeout: int = 30,
                   max_bytes: int | None = None) -> tuple[int, bytes, str]:
    """GET without automatic redirects.

    A credential only ever goes to the store host: it is stripped up front
    for any other host and again whenever a redirect leaves that host.
    Returns (status, body, final_url). Never logs headers.
    """
    sess = async_get_clientsession(hass)
    hdrs = strip_unsafe_auth(url, headers)
    for _ in range(_MAX_REDIRECTS + 1):
        async with sess.get(url, headers=hdrs, timeout=timeout, allow_redirects=False) as resp:
            location = resp.headers.get("Location")
            if resp.status in _REDIRECTS and location:
                url = urljoin(url, location)
                hdrs = strip_unsafe_auth(url, hdrs)
                continue
            if max_bytes is not None:
                body = await resp.content.read(max_bytes + 1)
            else:
                body = await resp.read()
            return resp.status, body, url
    return 599, b"", url


def _json(body: bytes):
    try:
        return json.loads(body or b"null")
    except Exception:
        return None


class GiteaClient:
    """Store server client.

    Credentials:
    - ``token``: the main token (Reconfigure). It is only ever sent for
      repositories it can see (``token_repos`` from its search, plus
      ``authorized_repos`` it could see before) and to the user endpoints.
      Everything else - the public catalogue and every other repository -
      goes without a credential, exactly as with no token. That makes a
      restricted "repo key" (it sees nothing public) safe as a main token.
    - ``repo_keys``: optional key per custom repository, used only for it.
    """

    def __init__(self, hass: HomeAssistant, base_url: str, token: str = None):
        self.hass = hass
        self.base_url = base_url.rstrip("/")
        self.token = token or None
        self._token_valid = True  # Assume valid until a 401 says otherwise
        # "Beta releases" setting; only takes effect with a working token.
        self.include_prereleases = False
        # Repositories (owner/repo, lower-case) the main token can see now,
        # with their repo objects, and every one it has been able to see.
        self.token_repos: dict[str, dict] = {}
        self.authorized_repos: set[str] = set()
        self._token_repos_at = 0.0
        # Custom repository keys: owner/repo (lower-case) -> key.
        self.repo_keys: dict[str, str] = {}
        # Called with (owner, repo, ok) when a repository that needs a
        # credential becomes reachable / unreachable (repair issues).
        self.on_access: Callable[[str, str, bool], None] | None = None
        # Called when authorized_repos grows (so it can be saved).
        self.on_authorized_change: Callable[[], None] | None = None
        _AUTH_ORIGINS.add(_origin(self.base_url))

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------

    @staticmethod
    def repo_key(owner: str, repo: str) -> str:
        return f"{owner}/{repo}".lower()

    @property
    def beta_enabled(self) -> bool:
        """Offer pre-releases (betas)? Authenticated installs only."""
        return bool(self.include_prereleases and self.token and self._token_valid)

    def credential_for(self, owner: str, repo: str) -> str | None:
        """The key or token for this repository, or None (public access)."""
        key = self.repo_key(owner, repo)
        if key in self.repo_keys:
            return self.repo_keys[key]
        if self.token and self._token_valid and (key in self.token_repos or key in self.authorized_repos):
            return self.token
        return None

    def auth_headers(self, owner: str, repo: str) -> dict:
        """Headers for a download from this repository (empty = public)."""
        cred = self.credential_for(owner, repo)
        return {"Authorization": f"token {cred}"} if cred else {}

    async def _get(self, url: str, cred: str | None = None, timeout: int = 30) -> tuple[int, bytes]:
        headers = {"Accept": "application/json"}
        if cred:
            headers["Authorization"] = f"token {cred}"
        status, body, _ = await safe_get(self.hass, url, headers, timeout=timeout)
        return status, body

    async def _repo_get(self, owner: str, repo: str, path: str, timeout: int = 30) -> tuple[int, bytes]:
        """GET a repository API path with that repository's credential (if any)."""
        url = f"{self.base_url}/api/v1/repos/{owner}/{repo}{path}"
        return await self._get(url, self.credential_for(owner, repo), timeout=timeout)

    def _note_access(self, owner: str, repo: str, status: int, used_cred: bool) -> None:
        """Report reachability of a repository that needs a credential."""
        if not used_cred or self.on_access is None:
            return
        if status == 200:
            self.on_access(owner, repo, True)
        elif status in (401, 404):
            self.on_access(owner, repo, False)

    async def check_token(self) -> bool | None:
        """Ask the server whether the token is accepted.

        Only a 401 on /api/v1/user means invalid. 403 = accepted but without
        the read:user scope. None = couldn't be asked (timeout, 5xx). The
        token is always sent here, so one bad check never locks it out.
        """
        if not self.token:
            return False
        try:
            status, _ = await self._get(f"{self.base_url}/api/v1/user", self.token, timeout=20)
        except Exception as e:
            _LOGGER.debug("Token check failed: %s", type(e).__name__)
            return None
        if status in (200, 403):
            return True
        if status == 401:
            return False
        _LOGGER.debug("Token check got HTTP %s; keeping last state", status)
        return None

    async def test_auth(self) -> bool:
        """True when a token is set and accepted. Without a token: True (public access)."""
        if not self.token:
            return True
        result = await self.check_token()
        if result is None:
            return self._token_valid
        if self._token_valid != result:
            if result:
                _LOGGER.info("Store token accepted again")
            else:
                _LOGGER.warning("Store token was rejected (expired or removed)")
        self._token_valid = result
        return result

    async def validate_key(self, owner: str, repo: str, key: str) -> tuple[bool, str | None]:
        """Check a repo key against one repository: (ok, reason)."""
        try:
            status, _ = await self._get(f"{self.base_url}/api/v1/repos/{owner}/{repo}", key, timeout=20)
        except Exception:
            return False, "cannot_connect"
        if status == 200:
            return True, None
        if status == 401:
            return False, "key_invalid"
        if status in (403, 404):
            return False, "key_no_access"
        return False, "cannot_connect"

    # ------------------------------------------------------------------
    # What the main token can see
    # ------------------------------------------------------------------

    async def refresh_token_repos(self, max_age: float = 0) -> dict[str, dict]:
        """Repositories the main token sees: /repos/search (paged) and
        /user/repos, both with the token. Remembered in ``token_repos``."""
        import time

        if not self.token or not self._token_valid:
            self.token_repos = {}
            return {}
        if max_age and self.token_repos and time.time() - self._token_repos_at < max_age:
            return self.token_repos

        found: dict[str, dict] = {}
        for repo in await self.search_repos(limit=1000, cred=self.token):
            self._add_repo(found, repo)
        page = 1
        while page <= 20:
            try:
                status, body = await self._get(
                    f"{self.base_url}/api/v1/user/repos?limit=50&page={page}", self.token
                )
            except Exception:
                break
            data = _json(body) if status == 200 else None
            if not isinstance(data, list) or not data:
                break
            for repo in data:
                self._add_repo(found, repo)
            if len(data) < 50:
                break
            page += 1

        self.token_repos = found
        self._token_repos_at = time.time()
        new = set(found) - self.authorized_repos
        if new:
            self.authorized_repos |= new
            if self.on_authorized_change:
                self.on_authorized_change()
        return found

    @classmethod
    def _add_repo(cls, found: dict, repo) -> None:
        if not isinstance(repo, dict):
            return
        owner = (repo.get("owner") or {}).get("login") or (repo.get("owner") or {}).get("username")
        name = repo.get("name")
        if owner and name:
            found[cls.repo_key(owner, name)] = repo

    # ------------------------------------------------------------------
    # Public catalogue (never with a credential)
    # ------------------------------------------------------------------

    async def get_org_repos(self, org: str, include_token: bool = False) -> list[dict]:
        """Public repositories of an organization (read without a credential).

        With ``include_token``, also the repositories of that organization
        the main token can see (organization listings answer 403 for a
        restricted repo key, so they come from the token's own list).
        """
        data = None
        try:
            status, body = await self._get(f"{self.base_url}/api/v1/orgs/{org}/repos?limit=50")
            data = _json(body) if status == 200 else None
            if not isinstance(data, list) and status not in (403, 404):
                _LOGGER.debug("Org %s repos: HTTP %s", org, status)
        except Exception as e:
            _LOGGER.debug("Org %s repos failed: %s", org, type(e).__name__)
        repos = data if isinstance(data, list) else []
        if include_token and self.token and self._token_valid:
            visible = await self.refresh_token_repos(max_age=300)
            known = {self.repo_key(org, r.get("name", "")) for r in repos if isinstance(r, dict)}
            for key, repo in visible.items():
                if key.split("/", 1)[0] == org.lower() and key not in known:
                    repos.append(repo)
        return repos

    async def get_user_repos(self, user: str) -> list[dict]:
        """Public repositories of a user."""
        try:
            status, body = await self._get(f"{self.base_url}/api/v1/users/{user}/repos")
        except Exception:
            return []
        data = _json(body) if status == 200 else None
        return data if isinstance(data, list) else []

    async def get_org_info(self, org: str) -> dict | None:
        try:
            status, body = await self._get(f"{self.base_url}/api/v1/orgs/{org}", timeout=20)
        except Exception:
            return None
        data = _json(body) if status == 200 else None
        return data if isinstance(data, dict) else None

    async def get_user_info(self, user: str) -> dict | None:
        try:
            status, body = await self._get(f"{self.base_url}/api/v1/users/{user}", timeout=20)
        except Exception:
            return None
        data = _json(body) if status == 200 else None
        return data if isinstance(data, dict) else None

    async def search_repos(self, limit: int = 500, cred: str | None = None) -> list[dict]:
        """Search repositories (paged, in parallel). Public unless ``cred``."""
        per_page = 50
        max_pages = max(1, (limit + per_page - 1) // per_page)

        async def _fetch_page(page: int) -> list[dict]:
            url = f"{self.base_url}/api/v1/repos/search?limit={per_page}&page={page}"
            try:
                status, body = await self._get(url, cred, timeout=60)
            except Exception as e:
                _LOGGER.debug("Search page %d failed: %s", page, type(e).__name__)
                return []
            data = _json(body) if status == 200 else None
            if isinstance(data, dict) and "data" in data:
                return data["data"] or []
            return data if isinstance(data, list) else []

        page_results = await asyncio.gather(*[_fetch_page(p) for p in range(1, max_pages + 1)])
        merged: list[dict] = []
        for batch in page_results:
            if batch:
                merged.extend(batch)
        return merged[:limit] if limit else merged

    # ------------------------------------------------------------------
    # Token-only endpoints (a 403 = scope missing: skipped quietly)
    # ------------------------------------------------------------------

    async def _token_list(self, path: str) -> list[dict]:
        if not self.token or not self._token_valid:
            return []
        try:
            status, body = await self._get(f"{self.base_url}/api/v1{path}", self.token)
        except Exception:
            return []
        data = _json(body) if status == 200 else None
        return data if isinstance(data, list) else []

    async def get_user_orgs(self) -> list[dict]:
        return await self._token_list("/user/orgs")

    async def get_user_following(self) -> list[dict]:
        return await self._token_list("/user/following")

    async def get_org_members(self, org: str) -> list[dict]:
        return await self._token_list(f"/orgs/{org}/members")

    async def get_current_user(self) -> dict | None:
        if not self.token:
            return None
        try:
            status, body = await self._get(f"{self.base_url}/api/v1/user", self.token, timeout=20)
        except Exception:
            return None
        data = _json(body) if status == 200 else None
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------------
    # Per repository (credential only if this repository needs one)
    # ------------------------------------------------------------------

    async def get_repo(self, owner: str, repo: str) -> dict:
        cred = self.credential_for(owner, repo)
        status, body = await self._get(f"{self.base_url}/api/v1/repos/{owner}/{repo}", cred)
        self._note_access(owner, repo, status, bool(cred))
        if status != 200:
            # Never include the response body: it could name the store host.
            raise RuntimeError(f"Repo fetch failed ({status})")
        return _json(body) or {}

    async def check_repo_access(self, owner: str, repo: str) -> bool | None:
        """For a repository that needs a credential: True = reachable,
        False = access stopped (401/404), None = can't tell / not needed."""
        cred = self.credential_for(owner, repo)
        if not cred:
            return None
        try:
            status, _ = await self._get(f"{self.base_url}/api/v1/repos/{owner}/{repo}", cred, timeout=20)
        except Exception:
            return None
        self._note_access(owner, repo, status, True)
        if status == 200:
            return True
        if status in (401, 404):
            return False
        return None

    async def _all_releases(self, owner: str, repo: str) -> list[dict]:
        """Every release (newest first), including drafts and pre-releases."""
        try:
            status, body = await self._repo_get(owner, repo, "/releases")
        except Exception:
            return []
        data = _json(body) if status == 200 else None
        return data if isinstance(data, list) else []

    async def get_releases(self, owner: str, repo: str) -> list[dict]:
        """Releases offered for install: never drafts; pre-releases only
        when beta releases are on (authenticated installs)."""
        beta = self.beta_enabled
        return [
            r for r in await self._all_releases(owner, repo)
            if isinstance(r, dict) and not r.get("draft") and (beta or not r.get("prerelease"))
        ]

    async def get_file_content(self, owner: str, repo: str, file_path: str, branch: str = "main") -> str | None:
        try:
            status, body = await self._repo_get(owner, repo, f"/contents/{file_path}?ref={branch}", timeout=20)
        except Exception:
            return None
        data = _json(body) if status == 200 else None
        try:
            return base64.b64decode(data["content"]).decode("utf-8") if isinstance(data, dict) else None
        except Exception:
            return None

    async def get_readme(self, owner: str, repo: str) -> str | None:
        for name in ("README.md", "readme.md", "README"):
            try:
                status, body = await self._repo_get(owner, repo, f"/contents/{name}", timeout=20)
            except Exception:
                continue
            data = _json(body) if status == 200 else None
            if isinstance(data, dict) and data.get("content"):
                try:
                    return base64.b64decode(data["content"]).decode("utf-8")
                except Exception:
                    continue
        return None

    async def get_latest_release(self, owner: str, repo: str) -> dict:
        """Newest release. Gitea's /releases/latest skips pre-releases and
        drafts; with beta releases on, the newest pre-release counts too."""
        if self.beta_enabled:
            for rel in await self._all_releases(owner, repo):
                if isinstance(rel, dict) and not rel.get("draft"):
                    return rel
        status, body = await self._repo_get(owner, repo, "/releases/latest")
        if status != 200:
            raise RuntimeError(f"Latest release fetch failed ({status})")
        return _json(body) or {}

    async def get_release_by_tag(self, owner: str, repo: str, tag: str) -> dict:
        status, body = await self._repo_get(owner, repo, f"/releases/tags/{tag}")
        if status != 200:
            raise RuntimeError(f"Release-by-tag fetch failed ({status})")
        return _json(body) or {}

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
        return f"{self.base_url}/api/v1/repos/{owner}/{repo}/archive/{ref}.zip"

    async def get_icon_url(self, owner: str, repo: str, branch: str = "main", domains: list[str] | None = None) -> str | None:
        """Best-guess icon URL WITHOUT verifying existence (served to the
        browser through YidStore's repo_icon proxy)."""
        if domains is None:
            try:
                domains = await self.get_integration_domains(owner, repo, branch=branch)
            except Exception:
                domains = []

        if domains:
            return (
                f"{self.base_url}/{owner}/{repo}/raw/branch/{branch}"
                f"/custom_components/{domains[0]}/brand/icon.png"
            )
        return f"{self.base_url}/{owner}/{repo}/raw/branch/{branch}/icons/icon.png"

    def get_raw_icon_url(self, owner: str, repo: str, branch: str = "main") -> str:
        return f"{self.base_url}/{owner}/{repo}/raw/branch/{branch}/icons/icon.png"

    async def get_raw_file(self, owner: str, repo: str, path: str, ref: str | None = None) -> bytes | None:
        """A raw file from a repository (with its credential if it needs one)."""
        q = f"?ref={ref}" if ref else ""
        try:
            status, body = await self._repo_get(owner, repo, f"/raw/{path}{q}", timeout=20)
        except Exception:
            return None
        return body if status == 200 else None

    async def list_dir(self, owner: str, repo: str, path: str, branch: str = "main") -> list[dict]:
        """List a directory in a repo using the Gitea contents API."""
        p = path.strip("/")
        try:
            status, body = await self._repo_get(owner, repo, f"/contents/{p}?ref={branch}", timeout=15)
        except Exception:
            return []
        data = _json(body) if status == 200 else None
        return data if isinstance(data, list) else []

    async def get_integration_domains(self, owner: str, repo: str, branch: str = "main") -> list[str]:
        """Return possible integration domain folder(s) under custom_components/."""
        entries = await self.list_dir(owner, repo, "custom_components", branch=branch)
        domains: list[str] = []
        for e in entries:
            if (e or {}).get("type") == "dir":
                name = (e.get("name") or "").strip()
                if name and not name.startswith("."):
                    domains.append(name)
        return domains

    async def get_file_commits(self, owner: str, repo: str, file_path: str, branch: str = "main", limit: int = 1) -> list[dict]:
        p = file_path.strip("/")
        try:
            status, body = await self._repo_get(
                owner, repo, f"/commits?path={p}&sha={branch}&limit={limit}", timeout=20
            )
        except Exception:
            return []
        data = _json(body) if status == 200 else None
        return data if isinstance(data, list) else []

    async def get_file_info_with_history(self, owner: str, repo: str, file_path: str, branch: str = "main") -> dict | None:
        """Get file content and last commit info (modifier, date)."""
        content = await self.get_file_content(owner, repo, file_path, branch)
        if content is None:
            return None

        commits = await self.get_file_commits(owner, repo, file_path, branch, limit=1)

        last_modified_by = None
        last_modified_at = None
        commit_message = None

        if commits:
            commit = commits[0]
            committer = commit.get("committer") or commit.get("author") or {}
            last_modified_by = committer.get("login") or committer.get("name") or committer.get("username")
            commit_info = commit.get("commit", {})
            committer_info = commit_info.get("committer") or commit_info.get("author") or {}
            last_modified_at = committer_info.get("date") or commit.get("created")
            commit_message = commit_info.get("message", "")

        return {
            "content": content,
            "last_modified_by": last_modified_by,
            "last_modified_at": last_modified_at,
            "commit_message": commit_message,
            "file_path": file_path,
        }

    async def list_dir_recursive(self, owner: str, repo: str, path: str = "", branch: str = "main") -> list[dict]:
        """List directory contents with file info including last modified data."""
        entries = await self.list_dir(owner, repo, path, branch)
        result = []

        for entry in entries:
            if not isinstance(entry, dict):
                continue

            name = entry.get("name", "")
            entry_type = entry.get("type", "")
            entry_path = entry.get("path", "")

            if name.startswith("."):
                continue

            item = {
                "name": name,
                "path": entry_path,
                "type": entry_type,
                "size": entry.get("size", 0),
            }

            if entry_type == "file":
                if name.lower().endswith(('.md', '.html', '.htm', '.txt', '.yaml', '.yml')):
                    commits = await self.get_file_commits(owner, repo, entry_path, branch, limit=1)
                    if commits:
                        commit = commits[0]
                        committer = commit.get("committer") or commit.get("author") or {}
                        item["last_modified_by"] = committer.get("login") or committer.get("name")
                        commit_info = commit.get("commit", {})
                        committer_info = commit_info.get("committer") or commit_info.get("author") or {}
                        item["last_modified_at"] = committer_info.get("date") or commit.get("created")

            result.append(item)

        return result
