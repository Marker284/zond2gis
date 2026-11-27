"""Config flow + Options flow — логин, повторная авторизация и выбор друзей."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .coordinator import AuthFailed, CannotConnect, validate_credentials

_LOGGER = logging.getLogger(__name__)

DOMAIN = "zond2gis"
CONF_SELECTED_FRIENDS = "selected_friends"

EMAIL_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
)
PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)


def _friends_schema(friends: list[dict], default: list[str]) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_SELECTED_FRIENDS, default=default):
            SelectSelector(SelectSelectorConfig(
                options=[{"value": f["uid"], "label": f["name"]} for f in friends],
                multiple=True,
                mode=SelectSelectorMode.LIST,
            )),
    })


async def _async_login(hass, email: str, password: str, errors: dict[str, str]) -> dict | None:
    """Логинится и раскладывает ошибки по кодам формы. None — вход не удался."""
    try:
        return await hass.async_add_executor_job(validate_credentials, email, password)
    except AuthFailed as exc:
        _LOGGER.debug("2GIS отверг вход для %s: %s", email, exc)
        errors["base"] = "invalid_auth"
    except CannotConnect as exc:
        _LOGGER.debug("Нет связи с 2GIS: %s", exc)
        errors["base"] = "cannot_connect"
    except Exception:
        _LOGGER.exception("Неожиданная ошибка при входе в 2GIS")
        errors["base"] = "unknown"
    return None


class ZondConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

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
            await self.async_set_unique_id(self._email.lower())
            self._abort_if_unique_id_configured()

            token_data = await _async_login(self.hass, self._email, self._password, errors)
            if token_data is not None:
                self._token_data = token_data
                try:
                    self._all_friends = await self.hass.async_add_executor_job(
                        _fetch_friend_list, token_data["access_token"]
                    )
                except CannotConnect as exc:
                    _LOGGER.debug("Не удалось получить список друзей: %s", exc)
                    errors["base"] = "cannot_connect"
                else:
                    return await self.async_step_friends()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema({
                    vol.Required(CONF_EMAIL):    EMAIL_SELECTOR,
                    vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                }),
                {CONF_EMAIL: self._email} if self._email else {},
            ),
            errors=errors,
        )

    # ── Шаг 2: выбор друзей ───────────────────────────────────────────────

    async def async_step_friends(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None or not self._all_friends:
            # Друзей пока нет вовсе — отслеживаем всех (None), чтобы новые
            # друзья появились сами, а не отфильтровались пустым списком.
            selected = (
                user_input.get(CONF_SELECTED_FRIENDS, []) if user_input is not None else None
            )
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
            data_schema=_friends_schema(self._all_friends, all_uids),
        )

    # ── Повторная авторизация ─────────────────────────────────────────────

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        email = entry.data[CONF_EMAIL]

        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            token_data = await _async_login(self.hass, email, password, errors)
            if token_data is not None:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_PASSWORD:   password,
                        "access_token":  token_data["access_token"],
                        "refresh_token": token_data["refresh_token"],
                        "expires_at":    token_data["expires_at"],
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR}),
            description_placeholders={"email": email},
            errors=errors,
        )

    # ── Options flow ───────────────────────────────────────────────────────

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ZondOptionsFlow()


class ZondOptionsFlow(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self.config_entry
        coordinator = self.hass.data.get(DOMAIN, {}).get(entry.entry_id)

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
            # Fallback: перелогиниваемся, чтобы получить список. Без списка
            # форму не показываем — иначе сохранился бы пустой выбор.
            errors: dict[str, str] = {}
            token_data = await _async_login(
                self.hass, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD], errors
            )
            if token_data is None:
                return self.async_abort(reason=errors["base"])
            try:
                friends = await self.hass.async_add_executor_job(
                    _fetch_friend_list, token_data["access_token"]
                )
            except CannotConnect:
                return self.async_abort(reason="cannot_connect")

        current = entry.options.get(CONF_SELECTED_FRIENDS)
        if current is None:  # None = отслеживаются все
            current = [f["uid"] for f in friends]

        return self.async_show_form(
            step_id="init",
            data_schema=_friends_schema(friends, current),
        )


# ── Helpers ────────────────────────────────────────────────────────────────

def _fetch_friend_list(access_token: str) -> list[dict]:
    """Подключается к зонду и получает список друзей (синхронно через asyncio.run).

    Бросает CannotConnect, если список получить не удалось. Пустой список
    возвращается только когда сервер честно прислал initialState без друзей.
    """
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
        raise CannotConnect("initialState не пришёл")

    try:
        return asyncio.run(_get())
    except CannotConnect:
        raise
    except Exception as exc:
        raise CannotConnect(f"{type(exc).__name__}: {exc}") from exc
