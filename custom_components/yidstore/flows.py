"""Run an integration's setup wizard (config flow) from the YidStore panel.

Like Home Assistant's Marketplace, installing an integration flows straight
into its configuration wizard in the same dialog. The panel draws the
wizard's forms itself; these views drive Home Assistant's own config-flow
manager and return each step with the integration's translations, so the
labels, descriptions, errors and abort reasons read exactly as they do in
Settings -> Devices & Services.

Only admins can use them, and only for integrations YidStore tracks.
"""
from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Result fields passed through to the panel as they are.
_PASSTHROUGH = (
    "type", "flow_id", "handler", "step_id", "errors", "description_placeholders",
    "last_step", "reason", "menu_options", "url", "progress_action",
    "translation_domain", "title", "description", "sort", "preview",
)


def _is_admin(request) -> bool:
    user = request.get("hass_user")
    return bool(user and user.is_admin)


def _tracked_integration_domains(hass) -> set[str]:
    domains: set[str] = set()
    for data in (hass.data.get(DOMAIN) or {}).values():
        coordinator = data.get("coordinator") if isinstance(data, dict) else None
        if not coordinator:
            continue
        for pkg in coordinator.packages.values():
            if pkg.get("package_type", "integration") == "integration" and pkg.get("domain"):
                domains.add(str(pkg["domain"]))
    return domains


def _serialize_schema(schema) -> list | None:
    if schema is None:
        return None
    import voluptuous_serialize

    from homeassistant.helpers import config_validation as cv

    return voluptuous_serialize.convert(schema, custom_serializer=cv.custom_serializer)


def _result_json(result: dict) -> dict:
    """A config-flow step as JSON, like Home Assistant's own flow API."""
    out: dict[str, Any] = {k: result.get(k) for k in _PASSTHROUGH if k in result}
    rtype = str(result.get("type") or "")
    out["type"] = rtype
    if rtype == "form":
        try:
            out["data_schema"] = _serialize_schema(result.get("data_schema"))
        except Exception as exc:  # a schema the panel can't draw
            _LOGGER.debug("Could not serialize flow schema: %s", exc)
            out["data_schema"] = None
            out["unsupported"] = True
    if rtype == "create_entry":
        entry = result.get("result")
        out["entry_id"] = getattr(entry, "entry_id", None)
    # Never send objects the panel can't use (e.g. the config entry).
    out.pop("result", None)
    if isinstance(out.get("menu_options"), dict):
        out["menu_options"] = list(out["menu_options"].keys())
    return out


async def _translations(hass, language: str, domain: str) -> dict[str, str]:
    from homeassistant.helpers.translation import async_get_translations

    merged: dict[str, str] = {}
    for category in ("config", "selector"):
        try:
            merged.update(await async_get_translations(hass, language, category, [domain]))
        except Exception as exc:
            _LOGGER.debug("No %s translations for %s: %s", category, domain, exc)
    return merged


async def _integration_name(hass, domain: str) -> str:
    try:
        from homeassistant.loader import async_get_integration

        return (await async_get_integration(hass, domain)).name or domain
    except Exception:
        return domain


def _manager(hass, kind: str):
    """Setup wizard ("config") or an integration's settings ("options")."""
    return hass.config_entries.options if kind == "options" else hass.config_entries.flow


def _flow_domain(hass, kind: str, flow_id: str) -> str | None:
    """The integration a running flow belongs to."""
    for flow in _manager(hass, kind).async_progress():
        if flow.get("flow_id") != flow_id:
            continue
        handler = flow.get("handler")
        if kind != "options":
            return handler
        # Options flows are keyed by config entry id.
        entry = hass.config_entries.async_get_entry(handler)
        return entry.domain if entry else None
    return None


class FlowStartView(HomeAssistantView):
    """Start an integration's setup wizard."""
    url = "/api/yidstore/flow/start"
    name = "api:yidstore:flow:start"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        if not _is_admin(request):
            return web.json_response({"error": "unauthorized"}, status=403)
        body = await request.json()
        domain = str(body.get("domain") or "").strip()
        kind = "options" if body.get("kind") == "options" else "config"
        if not domain or domain not in _tracked_integration_domains(hass):
            return web.json_response({"error": "Unknown integration"}, status=400)
        language = str(body.get("language") or hass.config.language or "en")
        user = request.get("hass_user")
        try:
            if kind == "options":
                entry = next(
                    (e for e in hass.config_entries.async_entries(domain) if e.supports_options),
                    None,
                )
                if entry is None:
                    return web.json_response({"error": "This integration has no settings"}, status=400)
                result = await hass.config_entries.options.async_init(entry.entry_id)
            else:
                result = await hass.config_entries.flow.async_init(
                    domain,
                    context={"source": "user", "show_advanced_options": False, "user_id": user.id if user else None},
                )
        except Exception as exc:
            _LOGGER.warning("Could not start setup for %s: %s", domain, exc)
            return web.json_response({"error": str(exc) or "Could not start the setup"}, status=400)
        out = _result_json(result)
        out["kind"] = kind
        out["domain"] = domain
        out["integration_name"] = await _integration_name(hass, domain)
        out["translations"] = await _translations(hass, language, domain)
        return web.json_response(out)


class FlowStepView(HomeAssistantView):
    """Submit a step (POST), poll a running step (GET) or cancel (DELETE)."""
    url = "/api/yidstore/flow/{flow_id}"
    name = "api:yidstore:flow:step"
    requires_auth = True

    @staticmethod
    def _kind(request) -> str:
        return "options" if request.query.get("kind") == "options" else "config"

    def _allowed(self, hass, kind: str, flow_id: str) -> bool:
        domain = _flow_domain(hass, kind, flow_id)
        return bool(domain and domain in _tracked_integration_domains(hass))

    async def _configure(self, hass, kind: str, flow_id: str, user_input) -> web.Response:
        try:
            result = await _manager(hass, kind).async_configure(flow_id, user_input)
        except Exception as exc:
            # Schema validation and other errors come back as a message.
            return web.json_response({"error": str(exc) or "Invalid input"}, status=400)
        return web.json_response(_result_json(result))

    async def post(self, request: web.Request, flow_id: str) -> web.Response:
        hass = request.app["hass"]
        if not _is_admin(request):
            return web.json_response({"error": "unauthorized"}, status=403)
        kind = self._kind(request)
        if not self._allowed(hass, kind, flow_id):
            return web.json_response({"error": "This setup has ended"}, status=404)
        body = await request.json()
        user_input = body if isinstance(body, dict) else {}
        return await self._configure(hass, kind, flow_id, user_input)

    async def get(self, request: web.Request, flow_id: str) -> web.Response:
        hass = request.app["hass"]
        if not _is_admin(request):
            return web.json_response({"error": "unauthorized"}, status=403)
        kind = self._kind(request)
        if not self._allowed(hass, kind, flow_id):
            return web.json_response({"error": "This setup has ended"}, status=404)
        return await self._configure(hass, kind, flow_id, None)

    async def delete(self, request: web.Request, flow_id: str) -> web.Response:
        hass = request.app["hass"]
        if not _is_admin(request):
            return web.json_response({"error": "unauthorized"}, status=403)
        kind = self._kind(request)
        if self._allowed(hass, kind, flow_id):
            try:
                _manager(hass, kind).async_abort(flow_id)
            except Exception:
                pass
        return web.json_response({"success": True})


class RestartView(HomeAssistantView):
    """Restart options, like Home Assistant's own restart dialog:
    quick reload, restart, and (with a Supervisor) reboot / shut down."""
    url = "/api/yidstore/restart"
    name = "api:yidstore:restart"
    requires_auth = True

    _ACTIONS = {
        "reload": ("homeassistant", "reload_all"),
        "restart": ("homeassistant", "restart"),
        "reboot": ("hassio", "host_reboot"),
        "shutdown": ("hassio", "host_shutdown"),
    }

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        if not _is_admin(request):
            return web.json_response({"error": "unauthorized"}, status=403)
        return web.json_response({
            "supervisor": hass.services.has_service("hassio", "host_reboot"),
        })

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        if not _is_admin(request):
            return web.json_response({"error": "unauthorized"}, status=403)
        try:
            body = await request.json()
        except Exception:
            body = {}
        action = str((body or {}).get("action") or "restart")
        if action not in self._ACTIONS:
            return web.json_response({"error": "Unknown action"}, status=400)
        domain, service = self._ACTIONS[action]
        if not hass.services.has_service(domain, service):
            return web.json_response({"error": "Not available on this system"}, status=400)
        if action == "reload":
            try:
                await hass.services.async_call(domain, service, blocking=True)
            except Exception as exc:
                return web.json_response({"error": str(exc) or "Failed to reload configuration"}, status=500)
        else:
            hass.async_create_task(hass.services.async_call(domain, service))
        return web.json_response({"success": True})


def async_register_flow_views(hass) -> None:
    for view in (FlowStartView(), FlowStepView(), RestartView()):
        hass.http.register_view(view)
