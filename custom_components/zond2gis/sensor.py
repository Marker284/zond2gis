"""Сенсоры: батарея, движение, место, скорость, расстояние от дома."""
from __future__ import annotations

import math

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfLength, UnitOfSpeed
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DOMAIN
from .coordinator import FriendState, ZondCoordinator, SIGNAL_NEW_FRIEND, SIGNAL_UPDATE_FRIEND


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние между двумя точками в километрах (формула Хаверсина)."""
    R = 6371.0
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(dλ / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ZondCoordinator = hass.data[DOMAIN][entry.entry_id]
    added: set[str] = set()

    @callback
    def _on_new_friends(uids: list[str]) -> None:
        new = []
        for uid in uids:
            if uid not in added:
                added.add(uid)
                new += [
                    ZondBatterySensor(coordinator, uid),
                    ZondMovementSensor(coordinator, uid),
                    ZondPlaceSensor(coordinator, uid),
                    ZondSpeedSensor(coordinator, uid),
                    ZondDistanceSensor(coordinator, uid),
                ]
        if new:
            async_add_entities(new)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, SIGNAL_NEW_FRIEND.format(entry.entry_id), _on_new_friends
        )
    )


class _ZondBaseSensor(SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: ZondCoordinator, uid: str) -> None:
        self._coordinator = coordinator
        self._uid = uid

    @property
    def device_info(self):
        from homeassistant.helpers.device_registry import DeviceInfo
        profile = self._coordinator.profiles.get(self._uid)
        return DeviceInfo(
            identifiers={(DOMAIN, self._uid)},
            name=profile.name if profile else self._uid[:8],
            manufacturer="2GIS",
            model="Friends on Map",
        )

    @property
    def _state(self) -> FriendState | None:
        return self._coordinator.states.get(self._uid)

    @property
    def available(self) -> bool:
        s = self._state
        return s.is_available() if s else False

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_UPDATE_FRIEND.format(self._coordinator.entry_id, self._uid),
                self._handle_update,
            )
        )


class ZondBatterySensor(_ZondBaseSensor):
    _attr_translation_key = "battery"
    _attr_device_class    = SensorDeviceClass.BATTERY
    _attr_state_class     = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    @property
    def unique_id(self) -> str:
        return f"zond_{self._uid}_battery"

    @property
    def native_value(self) -> int | None:
        s = self._state
        return s.battery_pct if s else None


class ZondMovementSensor(_ZondBaseSensor):
    _attr_translation_key = "movement"
    _attr_icon = "mdi:walk"

    @property
    def unique_id(self) -> str:
        return f"zond_{self._uid}_movement"

    @property
    def native_value(self) -> str | None:
        s = self._state
        return s.movement if s else None


class ZondPlaceSensor(_ZondBaseSensor):
    _attr_translation_key = "place"
    _attr_icon = "mdi:map-marker"

    @property
    def unique_id(self) -> str:
        return f"zond_{self._uid}_place"

    @property
    def native_value(self) -> str | None:
        s = self._state
        return s.place if s else None


class ZondSpeedSensor(_ZondBaseSensor):
    _attr_translation_key = "speed"
    _attr_icon  = "mdi:speedometer"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfSpeed.KILOMETERS_PER_HOUR
    _attr_device_class = SensorDeviceClass.SPEED

    @property
    def unique_id(self) -> str:
        return f"zond_{self._uid}_speed"

    @property
    def native_value(self) -> float | None:
        s = self._state
        if not s or s.speed is None:
            return None
        return round(s.speed * 3.6, 1)


class ZondDistanceSensor(_ZondBaseSensor):
    _attr_translation_key = "distance"
    _attr_icon  = "mdi:map-marker-distance"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfLength.KILOMETERS

    @property
    def unique_id(self) -> str:
        return f"zond_{self._uid}_distance"

    @property
    def native_value(self) -> float | None:
        s = self._state
        if not s or s.lat is None or s.lon is None:
            return None
        home_lat = self.hass.config.latitude
        home_lon = self.hass.config.longitude
        if not home_lat or not home_lon:
            return None
        return round(_haversine_km(home_lat, home_lon, s.lat, s.lon), 2)
