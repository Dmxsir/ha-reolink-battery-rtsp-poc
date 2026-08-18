"""Config and options flows for the isolated Reolink Battery RTSP PoC."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_GITHUB_DIAGNOSTICS_ENABLED,
    CONF_GITHUB_TOKEN,
    CONF_SOURCE_ENTRY_ID,
    DOMAIN,
    SOURCE_DOMAIN,
)


class ReolinkBatteryRtspPocConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Select an existing Reolink Battery config entry as the PoC source."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        source_entries = self.hass.config_entries.async_entries(SOURCE_DOMAIN)
        if not source_entries:
            return self.async_abort(reason="no_source_entries")

        choices = {entry.entry_id: entry.title for entry in source_entries}
        if user_input is not None:
            source_entry_id = user_input[CONF_SOURCE_ENTRY_ID]
            source = next(
                entry
                for entry in source_entries
                if entry.entry_id == source_entry_id
            )
            await self.async_set_unique_id(source_entry_id)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"RTSP PoC - {source.title}",
                data={CONF_SOURCE_ENTRY_ID: source_entry_id},
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_SOURCE_ENTRY_ID): vol.In(choices)}
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the PoC options flow."""
        return ReolinkBatteryRtspPocOptionsFlow()


class ReolinkBatteryRtspPocOptionsFlow(config_entries.OptionsFlow):
    """Configure optional secret-safe GitHub diagnostics export."""

    async def async_step_init(self, user_input=None):
        errors: dict[str, str] = {}
        existing_token = str(self.config_entry.options.get(CONF_GITHUB_TOKEN, ""))

        if user_input is not None:
            enabled = bool(user_input.get(CONF_GITHUB_DIAGNOSTICS_ENABLED, False))
            supplied_token = str(user_input.get(CONF_GITHUB_TOKEN, "")).strip()
            token = supplied_token or existing_token
            if enabled and not token:
                errors[CONF_GITHUB_TOKEN] = "github_token_required"
            else:
                options = dict(self.config_entry.options)
                options[CONF_GITHUB_DIAGNOSTICS_ENABLED] = enabled
                if supplied_token:
                    options[CONF_GITHUB_TOKEN] = supplied_token
                return self.async_create_entry(title="", data=options)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_GITHUB_DIAGNOSTICS_ENABLED,
                        default=bool(
                            self.config_entry.options.get(
                                CONF_GITHUB_DIAGNOSTICS_ENABLED, False
                            )
                        ),
                    ): bool,
                    vol.Optional(CONF_GITHUB_TOKEN): TextSelector(
                        TextSelectorConfig(
                            type=TextSelectorType.PASSWORD,
                            autocomplete="off",
                        )
                    ),
                }
            ),
            errors=errors,
        )
