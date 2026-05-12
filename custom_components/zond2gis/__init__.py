"""2GIS Friends on Map — Home Assistant integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant

from .coordinator import ZondCoordinator
from .config_flow import CONF_SELECTED_FRIENDS

DOMAIN = "zond2gis"
PLATFORMS = [Platform.DEVICE_TRACKER, Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    selected = entry.options.get(CONF_SELECTED_FRIENDS)  # None = все

    coordinator = ZondCoordinator(
        hass=hass,
        entry_id=entry.entry_id,
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
        access_token=entry.data["access_token"],
        expires_at=entry.data["expires_at"],
        selected_friends=selected,
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await coordinator.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Перезагружаем entry при изменении options (выбор друзей)
    entry.async_on_unload(
        entry.add_update_listener(_async_options_updated)
    )

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Вызывается когда пользователь меняет список друзей в options flow."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator: ZondCoordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_stop()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
