"""WebSocket coordinator — держит соединение с zond.api.2gis.ru и раздаёт данные."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import requests
import websockets
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

_LOGGER = logging.getLogger(__name__)

ZOND_WS_BASE  = "wss://zond.api.2gis.ru/api/1.1"
UNAVAILABLE_THRESHOLD_MS = 86_400_000  # 1 день в миллисекундах
ZOND_CHANNELS = "markers,sharing,routes"
APP_VERSION   = "6.31.0"
ORIGIN        = "https://2gis.kz"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

SIGNAL_NEW_FRIEND    = "zond2gis_new_friend_{}"      # .format(entry_id)
SIGNAL_UPDATE_FRIEND = "zond2gis_update_{}_{}"       # .format(entry_id, uid)


@dataclass
class FriendProfile:
    uid:      str
    name:     str
    logo_url: str | None = None


@dataclass
class FriendState:
    uid:         str
    last_seen:   int   = 0

    def is_available(self) -> bool:
        if not self.last_seen:
            return False
        import time
        return (time.time() * 1000 - self.last_seen) < UNAVAILABLE_THRESHOLD_MS
    lat:         float | None = None
    lon:         float | None = None
    accuracy:    float | None = None
    speed:       float | None = None
    battery_pct: int   = 0
    is_charging: bool  = False
    movement:    str   = "unknown"
    place:       str   = "unknown"


def _do_login(email: str, password: str) -> dict:
    """Синхронный логин — вызываем через executor."""
    resp = requests.post(
        "https://id.2gis.com/api/v1/sign_in",
        json={"grant_type": "password", "username": email,
              "password": password, "locale": "ru"},
        headers={"Content-Type": "application/json",
                 "Origin": "https://id.2gis.com",
                 "User-Agent": UA},
        timeout=15,
    )
    resp.raise_for_status()
    cookies = resp.cookies
    access  = cookies.get("id_access_token")
    refresh = cookies.get("id_refresh_token")
    if not access:
        raise ValueError(f"Токен не получен: {resp.text[:200]}")

    cookie_obj = resp.cookies._cookies.get("id.2gis.com", {}).get("/", {}).get("id_access_token")
    expires_at = cookie_obj.expires if (cookie_obj and cookie_obj.expires) else time.time() + 86400 * 365

    return {
        "access_token":  access,
        "refresh_token": refresh or "",
        "expires_at":    expires_at,
        "user":          resp.json(),
    }


def validate_credentials(email: str, password: str) -> dict:
    """Проверка логина — используется из config_flow (синхронно)."""
    return _do_login(email, password)


class ZondCoordinator:
    """Управляет авторизацией и WebSocket соединением с зондом."""

    def __init__(self, hass: HomeAssistant, entry_id: str,
                 email: str, password: str,
                 access_token: str, expires_at: float,
                 selected_friends: list[str] | None = None) -> None:
        self.hass         = hass
        self.entry_id     = entry_id
        self.email        = email
        self.password     = password
        self.access_token = access_token
        self.expires_at   = expires_at
        # None = все друзья, список = только выбранные
        self.selected_friends: set[str] | None = (
            set(selected_friends) if selected_friends is not None else None
        )

        self.profiles: dict[str, FriendProfile] = {}
        self.states:   dict[str, FriendState]   = {}

        self._task:    asyncio.Task | None = None
        self._running: bool = False

    def is_friend_selected(self, uid: str) -> bool:
        """Проверяет, выбран ли этот друг пользователем."""
        if self.selected_friends is None:
            return True
        return uid in self.selected_friends

    def update_selected_friends(self, selected: list[str]) -> None:
        self.selected_friends = set(selected)

    # ── Auth ──────────────────────────────────────────────────────────────

    async def _refresh_token(self) -> None:
        if time.time() < self.expires_at - 3600:
            return
        _LOGGER.info("Токен истекает, перелогиниваемся…")
        result = await self.hass.async_add_executor_job(
            _do_login, self.email, self.password
        )
        self.access_token = result["access_token"]
        self.expires_at   = result["expires_at"]

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def async_start(self) -> None:
        self._running = True
        self._task = self.hass.async_create_background_task(
            self._ws_loop(), f"zond2gis_{self.entry_id}"
        )

    async def async_stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ── WebSocket loop ────────────────────────────────────────────────────

    async def _ws_loop(self) -> None:
        delay = 5
        while self._running:
            try:
                await self._refresh_token()
                url = (f"{ZOND_WS_BASE}/user/ws"
                       f"?appVersion={APP_VERSION}&channels={ZOND_CHANNELS}"
                       f"&token={self.access_token}")
                async with websockets.connect(
                    url,
                    additional_headers={"Origin": ORIGIN},
                    ping_interval=30,
                    ping_timeout=10,
                ) as ws:
                    _LOGGER.info("WebSocket подключён")
                    delay = 5
                    async for raw in ws:
                        await self._handle_message(raw)

            except websockets.exceptions.ConnectionClosedError as exc:
                code = getattr(exc, "code", None)
                _LOGGER.warning("WS закрыт (code=%s): %s", code, exc)
                if code in (4001, 4003, 401):
                    self.expires_at = 0  # принудительный перелогин

            except (OSError, websockets.exceptions.WebSocketException) as exc:
                _LOGGER.warning("WS ошибка: %s", exc)

            except Exception:
                _LOGGER.exception("Неожиданная ошибка в WS loop")

            if not self._running:
                break
            _LOGGER.debug("Переподключение через %ds…", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type", "")
        payload  = msg.get("payload", {})

        if msg_type == "initialState":
            await self._handle_initial_state(payload)
        elif msg_type == "friendState":
            states = payload if isinstance(payload, list) else [payload]
            for s in states:
                await self._handle_friend_state(s)

    async def _handle_initial_state(self, payload: dict) -> None:
        profiles = payload.get("profiles", [])
        states   = payload.get("states", [])

        new_uids: list[str] = []

        for p in profiles:
            uid = p["id"]
            self.profiles[uid] = FriendProfile(
                uid=uid,
                name=p.get("name", uid[:8]),
                logo_url=p.get("logo"),
            )
            if uid not in self.states and self.is_friend_selected(uid):
                self.states[uid] = FriendState(uid=uid)
                new_uids.append(uid)

        for s in states:
            if self.is_friend_selected(s.get("id", "")):
                self._apply_state(s)

        # Сигнализируем платформам о новых друзьях
        if new_uids:
            async_dispatcher_send(
                self.hass,
                SIGNAL_NEW_FRIEND.format(self.entry_id),
                new_uids,
            )

        # Обновляем все существующие сущности
        for uid in self.states:
            async_dispatcher_send(
                self.hass,
                SIGNAL_UPDATE_FRIEND.format(self.entry_id, uid),
            )

    async def _handle_friend_state(self, s: dict) -> None:
        uid = s.get("id", "")
        if not uid or not self.is_friend_selected(uid):
            return
        is_new = uid not in self.states
        if is_new:
            self.states[uid] = FriendState(uid=uid)
        self._apply_state(s)

        if is_new:
            async_dispatcher_send(
                self.hass,
                SIGNAL_NEW_FRIEND.format(self.entry_id),
                [uid],
            )
        async_dispatcher_send(
            self.hass,
            SIGNAL_UPDATE_FRIEND.format(self.entry_id, uid),
        )

    def _apply_state(self, s: dict) -> None:
        uid = s.get("id", "")
        if uid not in self.states:
            return

        st = self.states[uid]
        st.last_seen = s.get("lastSeen", 0)

        loc = s.get("location") or {}
        if loc.get("lat") is not None:
            st.lat      = loc.get("lat")
            st.lon      = loc.get("lon")
            st.accuracy = loc.get("accuracy")
            st.speed    = loc.get("speed")

        battery = s.get("battery") or {}
        if "level" in battery:
            st.battery_pct = round(battery["level"] * 100)
            st.is_charging = bool(battery.get("isCharging"))

        movement = s.get("movement") or {}
        if "status" in movement:
            st.movement = movement["status"]

        place = s.get("locationPlace") or {}
        status = (place.get("status") or {}).get("id")
        if status:
            st.place = status
