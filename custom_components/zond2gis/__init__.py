"""2GIS Friends on Map — Home Assistant integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, callback

from .coordinator import ZondCoordinator
from .config_flow import CONF_SELECTED_FRIENDS

DOMAIN = "zond2gis"
PLATFORMS = [Platform.DEVICE_TRACKER, Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    selected = entry.options.get(CONF_SELECTED_FRIENDS)  # None = все

    @callback
    def _save_token(access_token: str, expires_at: float) -> None:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, "access_token": access_token, "expires_at": expires_at},
        )

    coordinator = ZondCoordinator(
        hass=hass,
        entry_id=entry.entry_id,
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
        access_token=entry.data["access_token"],
        expires_at=entry.data["expires_at"],
        selected_friends=selected,
        on_token_refreshed=_save_token,
        on_auth_failed=lambda: entry.async_start_reauth(hass),
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Сначала платформы подписываются на сигналы, потом стартует WS —
    # иначе initialState может прийти раньше, чем появятся слушатели.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await coordinator.async_start()

    # Update listener срабатывает на любое изменение entry, в том числе на
    # сохранение токена в entry.data. Перезагружаемся только если поменялись
    # options (выбор друзей), иначе каждый перелогин рвал бы WebSocket.
    setup_options = dict(entry.options)

    async def _async_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
        if dict(entry.options) != setup_options:
            await hass.config_entries.async_reload(entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator: ZondCoordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_stop()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
