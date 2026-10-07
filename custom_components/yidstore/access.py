"""Credentials bookkeeping: repo keys, what the main token can see, and
"access stopped" repair issues.

Nothing here ever logs or returns a token or key. The token is only
remembered as a short one-way fingerprint so the list of repositories it
could see is dropped when a different token is entered.
"""
from __future__ import annotations

import hashlib
import logging
import re

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_KEYS_STORE = f"{DOMAIN}.repo_keys"
_ACCESS_STORE = f"{DOMAIN}.token_access"
_STORE_VERSION = 1
ISSUE_PREFIX = "access_stopped_"


def _fingerprint(token: str | None) -> str | None:
    if not token:
        return None
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _issue_id(owner: str, repo: str) -> str:
    return ISSUE_PREFIX + re.sub(r"[^a-z0-9_]", "_", f"{owner}_{repo}".lower())


def mask_key(key: str | None) -> str | None:
    """Only the last 4 characters, for display."""
    return key[-4:] if key else None


class AccessManager:
    """Owns the repo keys and the token's repository list for one client."""

    def __init__(self, hass: HomeAssistant, client) -> None:
        self.hass = hass
        self.client = client
        self._keys_store = Store(hass, _STORE_VERSION, _KEYS_STORE)
        self._access_store = Store(hass, _STORE_VERSION, _ACCESS_STORE)
        self._stopped: set[str] = set()
        client.on_access = self._on_access
        client.on_authorized_change = self._save_access

    async def async_load(self) -> None:
        keys = await self._keys_store.async_load() or {}
        self.client.repo_keys = {
            str(k).lower(): str(v) for k, v in (keys.get("keys") or {}).items() if v
        }
        access = await self._access_store.async_load() or {}
        if access.get("token_fp") and access.get("token_fp") == _fingerprint(self.client.token):
            self.client.authorized_repos = {str(r).lower() for r in access.get("authorized") or []}
        else:
            self.client.authorized_repos = set()

    # -- the main token ------------------------------------------------

    @callback
    def _save_access(self) -> None:
        self._access_store.async_delay_save(
            lambda: {
                "token_fp": _fingerprint(self.client.token),
                "authorized": sorted(self.client.authorized_repos),
            },
            5,
        )

    @callback
    def token_changed(self) -> None:
        """A different token was entered: forget what the old one could see."""
        self.client.token_repos = {}
        self.client.authorized_repos = set()
        self.client._token_repos_at = 0.0
        self._save_access()
        for key in list(self._stopped):
            owner, _, repo = key.partition("/")
            if key not in self.client.repo_keys:
                self._on_access(owner, repo, True)

    # -- repo keys -----------------------------------------------------

    def key_last4(self, owner: str, repo: str) -> str | None:
        return mask_key(self.client.repo_keys.get(self.client.repo_key(owner, repo)))

    async def async_set_key(self, owner: str, repo: str, key: str | None) -> None:
        """Store (or with None: delete) the key of one custom repository."""
        rk = self.client.repo_key(owner, repo)
        if key:
            self.client.repo_keys[rk] = key
        else:
            self.client.repo_keys.pop(rk, None)
            if rk in self._stopped and rk not in self.client.authorized_repos:
                self._on_access(owner, repo, True)
        await self._keys_store.async_save({"keys": dict(self.client.repo_keys)})

    # -- access stopped repair issues ------------------------------------

    @callback
    def _on_access(self, owner: str, repo: str, ok: bool) -> None:
        rk = f"{owner}/{repo}".lower()
        if ok:
            if rk in self._stopped:
                self._stopped.discard(rk)
                ir.async_delete_issue(self.hass, DOMAIN, _issue_id(owner, repo))
                _LOGGER.info("Access to %s/%s is back", owner, repo)
            return
        if rk in self._stopped:
            return  # already reported: stay quiet
        self._stopped.add(rk)
        _LOGGER.info("Access to %s/%s was stopped", owner, repo)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            _issue_id(owner, repo),
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="access_stopped",
            translation_placeholders={"owner": owner, "repo": repo},
        )

    def is_stopped(self, owner: str, repo: str) -> bool:
        return f"{owner}/{repo}".lower() in self._stopped
