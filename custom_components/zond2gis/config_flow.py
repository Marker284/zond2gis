"""Config flow + Options flow — логин и выбор друзей."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .coordinator import validate_credentials

DOMAIN = "zond2gis"
CONF_SELECTED_FRIENDS = "selected_friends"


class ZondConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1
    icon = "mdi:map-marker-account"

    def __init__(self) -> None:
        self._email:    str = ""
        self._password: str = ""
        self._token_data: dict = {}
        self._all_friends: list[dict] = []  # [{uid, name}, ...]

    # ── Шаг 1: email + пароль ─────────────────────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._email    = user_input[CONF_EMAIL].strip()
            self._password = user_input[CONF_PASSWORD]
            try:
                self._token_data = await self.hass.async_add_executor_job(
                    validate_credentials, self._email, self._password
                )
            except Exception:
                errors["base"] = "invalid_auth"
            else:
                await self.async_set_unique_id(self._email.lower())
                self._abort_if_unique_id_configured()
                # Сохраняем список друзей для шага 2
                self._all_friends = await self.hass.async_add_executor_job(
                    _fetch_friend_list, self._token_data["access_token"]
                )
                return await self.async_step_friends()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_EMAIL):    str,
                vol.Required(CONF_PASSWORD): str,
            }),
            errors=errors,
        )

    # ── Шаг 2: выбор друзей ───────────────────────────────────────────────

    async def async_step_friends(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            selected = user_input.get(CONF_SELECTED_FRIENDS, [])
            return self.async_create_entry(
                title=self._token_data["user"].get("display_name", self._email),
                data={
                    CONF_EMAIL:             self._email,
                    CONF_PASSWORD:          self._password,
                    "access_token":         self._token_data["access_token"],
                    "refresh_token":        self._token_data["refresh_token"],
                    "expires_at":           self._token_data["expires_at"],
                },
                options={
                    CONF_SELECTED_FRIENDS: selected,
                },
            )

        all_uids = [f["uid"] for f in self._all_friends]
        return self.async_show_form(
            step_id="friends",
            data_schema=vol.Schema({
                vol.Required(CONF_SELECTED_FRIENDS, default=all_uids):
                    SelectSelector(SelectSelectorConfig(
                        options=[
                            {"value": f["uid"], "label": f["name"]}
                            for f in self._all_friends
                        ],
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )),
            }),
        )

    # ── Options flow ───────────────────────────────────────────────────────

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ZondOptionsFlow(config_entry)


class ZondOptionsFlow(OptionsFlow):
    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        coordinator = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)

        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={CONF_SELECTED_FRIENDS: user_input.get(CONF_SELECTED_FRIENDS, [])},
            )

        # Берём актуальный список из координатора (если уже подключён)
        if coordinator and coordinator.profiles:
            friends = [
                {"uid": uid, "name": p.name}
                for uid, p in coordinator.profiles.items()
            ]
        else:
            # Fallback: перелогиниваемся чтобы получить список
            try:
                token_data = await self.hass.async_add_executor_job(
                    validate_credentials,
                    self._entry.data[CONF_EMAIL],
                    self._entry.data[CONF_PASSWORD],
                )
                friends = await self.hass.async_add_executor_job(
                    _fetch_friend_list, token_data["access_token"]
                )
            except Exception:
                friends = []

        current = self._entry.options.get(CONF_SELECTED_FRIENDS, [])

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_SELECTED_FRIENDS, default=current):
                    SelectSelector(SelectSelectorConfig(
                        options=[
                            {"value": f["uid"], "label": f["name"]}
                            for f in friends
                        ],
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )),
            }),
        )


# ── Helpers ────────────────────────────────────────────────────────────────

def _fetch_friend_list(access_token: str) -> list[dict]:
    """Подключается к зонду и получает список друзей (синхронно через asyncio.run)."""
    import asyncio
    import json
    import websockets as _ws

    async def _get():
        url = (f"wss://zond.api.2gis.ru/api/1.1/user/ws"
               f"?appVersion=6.31.0&channels=markers,sharing,routes&token={access_token}")
        async with _ws.connect(url, additional_headers={"Origin": "https://2gis.kz"},
                               open_timeout=10) as ws:
            for _ in range(5):
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
                msg = json.loads(raw)
                if msg.get("type") == "initialState":
                    return [
                        {"uid": p["id"], "name": p.get("name", p["id"][:8])}
                        for p in msg["payload"].get("profiles", [])
                    ]
        return []

    try:
        return asyncio.run(_get())
    except Exception:
        return []
