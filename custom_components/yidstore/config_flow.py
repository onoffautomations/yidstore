from __future__ import annotations

import logging
import os
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.util.yaml import load_yaml

from .const import DOMAIN, MODE_ASSET, MODE_ZIPBALL, TYPE_INTEGRATION, TYPE_LOVELACE, TYPE_BLUEPRINTS, CONF_SIDE_PANEL
from .gitea import GiteaClient
from ._utils import get_primary_endpoint

_LOGGER = logging.getLogger(__name__)


def load_store_list(hass: HomeAssistant) -> list[dict]:
    """Load store list from YAML file."""
    try:
        # Get the integration directory
        integration_dir = os.path.dirname(__file__)
        store_list_path = os.path.join(integration_dir, "store_list.yaml")

        if not os.path.exists(store_list_path):
            _LOGGER.warning("Store list file not found: %s", store_list_path)
            return []

        # Use Home Assistant's YAML loader
        data = load_yaml(store_list_path)

        packages = data.get('packages', []) if data else []

        # Filter out empty or None packages
        packages = [p for p in packages if p and isinstance(p, dict)]

        _LOGGER.info("Loaded %d packages from store list", len(packages))
        return packages

    except Exception as e:
        _LOGGER.error("Failed to load store list: %s", e, exc_info=True)
        return []


class OnOffGiteaStoreConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        """Initialize config flow."""
        self.config_data = {}
        self.entry_id = None

    async def async_step_user(self, user_input=None):
        """Handle initial configuration."""
        _LOGGER.debug("async_step_user called with input: %s", user_input)
        errors = {}

        # Only allow one instance
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            # Use hardcoded obfuscated endpoint
            base_url = get_primary_endpoint()

            # No token during initial setup - public access only
            try:
                _LOGGER.debug("Testing Gitea client... %s", base_url)
                _LOGGER.info("Setting up with public access - token can be added later in settings")
            except Exception as e:
                _LOGGER.error("Cannot connect to endpoint %s: %s", base_url, e, exc_info=True)
                errors["base"] = "cannot_connect"

            if not errors:
                # Store configuration without token
                self.config_data = {
                    "base_url": base_url,
                    "token": "",
                    "owner": "",
                    CONF_SIDE_PANEL: user_input.get(CONF_SIDE_PANEL, True),
                }

                # Packages are installed from the YidStore panel afterwards;
                # setup no longer asks which ones to install.
                return self.async_create_entry(
                    title="YidStore",
                    data=self.config_data,
                )

        schema = vol.Schema(
            {
                vol.Optional(CONF_SIDE_PANEL, default=True): bool,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input=None):
        """Handle reconfiguration of the integration."""
        _LOGGER.debug("async_step_reconfigure called")

        # Get the config entry being reconfigured
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if not entry:
            return self.async_abort(reason="cannot_reconfigure")

        if user_input is not None:
            use_token = user_input.get("use_token", False)
            # Only use token if use_token is enabled
            token = user_input.get("token", "").strip() if use_token else None
            base_url = entry.data.get("base_url", get_primary_endpoint())

            # Test connection if token provided
            if token:
                try:
                    client = GiteaClient(self.hass, base_url=base_url, token=token)
                    ok = await client.check_token()
                    if ok is None:
                        return self.async_show_form(
                            step_id="reconfigure",
                            data_schema=self._get_reconfigure_schema(entry),
                            errors={"base": "cannot_connect"},
                        )
                    if not ok:
                        return self.async_show_form(
                            step_id="reconfigure",
                            data_schema=self._get_reconfigure_schema(entry),
                            errors={"token": "invalid_auth"},
                        )
                except Exception as e:
                    _LOGGER.error("Auth test failed: %s", e)
                    return self.async_show_form(
                        step_id="reconfigure",
                        data_schema=self._get_reconfigure_schema(entry),
                        errors={"base": "cannot_connect"},
                    )

            # Update the entry
            new_data = {**entry.data}
            new_data["token"] = token or ""
            new_data[CONF_SIDE_PANEL] = user_input.get(CONF_SIDE_PANEL, True)

            self.hass.config_entries.async_update_entry(entry, data=new_data)

            # Update runtime data if available
            if DOMAIN in self.hass.data and entry.entry_id in self.hass.data[DOMAIN]:
                runtime = self.hass.data[DOMAIN][entry.entry_id]
                if "client" in runtime:
                    changed = (runtime["client"].token or None) != (token or None)
                    runtime["client"].token = token
                    runtime["client"]._token_valid = True if token else False
                    if changed and runtime.get("access") is not None:
                        runtime["access"].token_changed()

            _refresh_store_after_token_change(self.hass, entry.entry_id)

            return self.async_abort(reason="reconfigure_successful")

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self._get_reconfigure_schema(entry),
        )

    def _get_reconfigure_schema(self, entry):
        """Get schema for reconfigure form."""
        has_token = bool(entry.data.get("token", ""))
        return vol.Schema(
            {
                vol.Optional("use_token", default=has_token): bool,
                vol.Optional("token", default=entry.data.get("token", "")): str,
                vol.Optional(CONF_SIDE_PANEL, default=entry.data.get(CONF_SIDE_PANEL, True)): bool,
            }
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return OptionsFlowHandler()


def _refresh_store_after_token_change(hass, entry_id: str) -> None:
    """Rebuild the store list right away so private packages appear (or
    disappear) without reloading the integration."""
    try:
        from .dashboard import _ensure_repos_rebuild, _invalidate_repos_cache

        _invalidate_repos_cache(entry_id)
        _ensure_repos_rebuild(hass, entry_id)
    except Exception as e:  # dashboard not set up (yet)
        _LOGGER.debug("Could not refresh store list after token change: %s", e)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for reconfiguration and installation."""

    # No __init__ needed - self.config_entry is set automatically by Home Assistant

    async def async_step_init(self, user_input=None):
        """Manage integration settings."""
        errors = {}
        entry_data = self.config_entry.data

        if user_input is not None:
            # Update basic settings
            use_token = user_input.get("use_token", False)
            # Only use token if use_token is enabled
            token = user_input.get("token", "").strip() if use_token else ""
            side_panel = user_input.get(CONF_SIDE_PANEL, True)

            # Validate token if provided
            if token:
                try:
                    base_url = entry_data.get("base_url", get_primary_endpoint())
                    client = GiteaClient(self.hass, base_url=base_url, token=token)
                    ok = await client.check_token()
                    if ok is None:
                        errors["base"] = "cannot_connect"
                    elif not ok:
                        errors["token"] = "invalid_auth"
                except Exception as e:
                    _LOGGER.error("Auth test failed: %s", e)
                    errors["base"] = "cannot_connect"

            if not errors:
                new_data = dict(entry_data)
                new_data["token"] = token
                new_data[CONF_SIDE_PANEL] = side_panel

                # Update the entry
                self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)

                # Update runtime data if available
                if DOMAIN in self.hass.data and self.config_entry.entry_id in self.hass.data[DOMAIN]:
                    runtime = self.hass.data[DOMAIN][self.config_entry.entry_id]
                    if "client" in runtime:
                        changed = (runtime["client"].token or None) != (token or None)
                        runtime["client"].token = token or None
                        runtime["client"]._token_valid = True if token else False
                        if changed and runtime.get("access") is not None:
                            runtime["access"].token_changed()

                _refresh_store_after_token_change(self.hass, self.config_entry.entry_id)

                return self.async_create_entry(title="", data={})

        has_token = bool(entry_data.get("token", ""))
        schema = vol.Schema(
            {
                vol.Optional("use_token", default=has_token): bool,
                vol.Optional("token", default=entry_data.get("token", "")): str,
                vol.Optional(CONF_SIDE_PANEL, default=entry_data.get(CONF_SIDE_PANEL, True)): bool,
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
        )
