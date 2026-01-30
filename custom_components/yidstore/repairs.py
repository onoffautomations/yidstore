"""Repairs for OnOff Integration Store."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class RestartRequiredRepairFlow(RepairsFlow):
    """Handler for restart required repair flow."""

    def __init__(self, integration_name: str) -> None:
        """Initialize the repair flow."""
        super().__init__()
        self.integration_name = integration_name

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Handle the first step of the repair flow."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Handle the confirm step - restart Home Assistant."""
        if user_input is not None:
            # User clicked submit - restart Home Assistant
            _LOGGER.info("User requested restart for %s via repair flow", self.integration_name)
            await self.hass.services.async_call("homeassistant", "restart")
            return self.async_create_entry(data={})

        return self.async_show_form(
            step_id="confirm",
            description_placeholders={"integration_name": self.integration_name},
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Create flow for fixing a repair issue."""
    # Get integration name from data if available
    if data and "integration_name" in data:
        integration_name = data["integration_name"]
    else:
        # Fallback: Extract integration name from issue_id (format: onoff_restart_{repo}_{timestamp})
        parts = issue_id.split("_")
        if len(parts) >= 3:
            # Get repo name (everything between "onoff_restart_" and the timestamp)
            integration_name = "_".join(parts[2:-1]) if len(parts) > 3 else parts[2]
        else:
            integration_name = "integration"

    return RestartRequiredRepairFlow(integration_name)
