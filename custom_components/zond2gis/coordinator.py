"""WebSocket coordinator — держит соединение с zond.api.2gis.ru и раздаёт данные."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests
import websockets
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util.ssl import client_context

_LOGGER = logging.getLogger(__name__)

ZOND_WS_BASE  = "wss://zond.api.2gis.ru/api/1.1"
UNAVAILABLE_THRESHOLD_MS = 86_400_000  # 1 день в миллисекундах
IDLE_TIMEOUT = 300  # сек без единого сообщения от WS — считаем фид подвисшим
# Сколько подряд соединений может закончиться без initialState, прежде чем
# мы перестанем доверять токену и перелогинимся.
MAX_EMPTY_SESSIONS = 3
AUTH_CLOSE_CODES = (4001, 4003, 401, 403)
ZOND_CHANNELS = "markers,sharing,routes"
APP_VERSION   = "6.31.0"
ORIGIN        = "https://2gis.kz"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# После принудительного перелогина не повторяем его раньше этого срока:
# если сервер отвергает и свежий токен, долбить вход каждую минуту нельзя.
RELOGIN_COOLDOWN = 600
SIGN_IN_REJECTED = (400, 401, 403)

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
    lat:         float | None = None
    lon:         float | None = None
    accuracy:    float | None = None
    speed:       float | None = None
    battery_pct: int   = 0
    is_charging: bool  = False
    movement:    str   = "unknown"
    place:       str   = "unknown"

    def is_available(self) -> bool:
        if not self.last_seen:
            return False
        return (time.time() * 1000 - self.last_seen) < UNAVAILABLE_THRESHOLD_MS


class AuthFailed(Exception):
    """2GIS отверг вход: неверный email/пароль или аккаунт заблокирован."""


class CannotConnect(Exception):
    """Сеть или сервер 2GIS недоступны — имеет смысл повторить позже."""


def _do_login(email: str, password: str) -> dict:
    """Синхронный логин — вызываем через executor.

    Бросает AuthFailed, если вход отвергнут, и CannotConnect при сетевых
    ошибках и ошибках сервера.
    """
    try:
        resp = requests.post(
            "https://id.2gis.com/api/v1/sign_in",
            json={"grant_type": "password", "username": email,
                  "password": password, "locale": "ru"},
            headers={"Content-Type": "application/json",
                     "Origin": "https://id.2gis.com",
                     "User-Agent": UA},
            timeout=15,
        )
    except requests.exceptions.RequestException as exc:
        raise CannotConnect(str(exc)) from exc

    if resp.status_code in SIGN_IN_REJECTED:
        raise AuthFailed(f"HTTP {resp.status_code}")
    if not resp.ok:
        raise CannotConnect(f"HTTP {resp.status_code}")

    cookies = resp.cookies
    access  = cookies.get("id_access_token")
    refresh = cookies.get("id_refresh_token")
    if not access:
        # Вход «прошёл», но токена нет (капча, подтверждение и т.п.) —
        # автоматически это не исправится, нужен человек.
        raise AuthFailed(f"Токен не получен: {resp.text[:200]}")

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
                 selected_friends: list[str] | None = None,
                 on_token_refreshed: Callable[[str, float], None] | None = None,
                 on_auth_failed: Callable[[], None] | None = None,
                 ) -> None:
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

        self._on_token_refreshed = on_token_refreshed
        self._on_auth_failed = on_auth_failed
        self._last_login: float = 0.0

        self._task:    asyncio.Task | None = None
        self._running: bool = False
        self._got_initial: bool = False
        self._empty_sessions: int = 0

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
        self._last_login = time.time()
        result = await self.hass.async_add_executor_job(
            _do_login, self.email, self.password
        )
        self.access_token = result["access_token"]
        self.expires_at   = result["expires_at"]
        self._empty_sessions = 0
        if self._on_token_refreshed:
            self._on_token_refreshed(self.access_token, self.expires_at)

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
            self._got_initial = False
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
                    # SSL-контекст HA создаётся заранее и кэшируется: иначе
                    # websockets грузит сертификаты прямо в event loop.
                    ssl=client_context(),
                ) as ws:
                    _LOGGER.info("WebSocket подключён")
                    delay = 5
                    # WS-пинги подтверждают, что TCP-соединение живо, но не
                    # гарантируют, что бэкенд продолжает слать friendState —
                    # известны случаи "тихого" зависания фида. Поэтому ждём
                    # каждое сообщение с таймаутом и переподключаемся, если
                    # данные не приходят слишком долго.
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=IDLE_TIMEOUT)
                        await self._handle_message(raw)

            except AuthFailed as exc:
                # Повторять вход со старым паролем бессмысленно и опасно
                # (2GIS может заблокировать аккаунт) — просим пользователя
                # ввести пароль заново и останавливаемся.
                _LOGGER.error(
                    "2GIS отверг вход (%s) — нужна повторная авторизация", exc
                )
                self._running = False
                if self._on_auth_failed:
                    self._on_auth_failed()
                break

            except CannotConnect as exc:
                _LOGGER.warning("Не удалось войти в 2GIS: %s", exc)

            except TimeoutError:
                _LOGGER.warning(
                    "Нет данных от WS более %ds — считаем соединение подвисшим, переподключаемся",
                    IDLE_TIMEOUT,
                )

            except websockets.exceptions.ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd else None
                _LOGGER.warning("WS закрыт (code=%s): %s", code, exc)
                if code in AUTH_CLOSE_CODES:
                    self._force_relogin("сервер закрыл WS с кодом %s" % code)

            except websockets.exceptions.InvalidStatus as exc:
                status = exc.response.status_code
                _LOGGER.warning("WS отклонён при подключении: HTTP %s", status)
                if status in (401, 403):
                    self._force_relogin("HTTP %s при подключении" % status)

            except (OSError, websockets.exceptions.WebSocketException) as exc:
                _LOGGER.warning("WS ошибка: %s", exc)

            except asyncio.CancelledError:
                raise

            except Exception:
                _LOGGER.exception("Неожиданная ошибка в WS loop")

            if not self._running:
                break

            # Сессия без initialState — признак того, что бэкенд нас не
            # принимает (например, токен отозван без явного кода закрытия).
            if self._got_initial:
                self._empty_sessions = 0
            else:
                self._empty_sessions += 1
                if self._empty_sessions >= MAX_EMPTY_SESSIONS:
                    self._force_relogin(
                        "%d соединений подряд без initialState" % self._empty_sessions
                    )

            _LOGGER.debug("Переподключение через %ds…", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    def _force_relogin(self, reason: str) -> None:
        self._empty_sessions = 0
        since = time.time() - self._last_login
        if since < RELOGIN_COOLDOWN:
            _LOGGER.warning(
                "%s, но перелогин был %ds назад — пока не повторяем", reason, since
            )
            return
        _LOGGER.warning("Принудительный перелогин: %s", reason)
        self.expires_at = 0

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

        self._got_initial = True

        for p in profiles:
            uid = p["id"]
            self.profiles[uid] = FriendProfile(
                uid=uid,
                name=p.get("name", uid[:8]),
                logo_url=p.get("logo"),
            )
            if uid not in self.states and self.is_friend_selected(uid):
                self.states[uid] = FriendState(uid=uid)

        for s in states:
            if self.is_friend_selected(s.get("id", "")):
                self._apply_state(s)

        # Объявляем платформам всех друзей при каждом initialState, а не
        # только новых: платформы сами отбрасывают уже добавленных. Иначе
        # сигнал, отправленный до подписки платформ (гонка при старте HA),
        # терялся навсегда, и сущности висели restored/unavailable до reload.
        if self.states:
            async_dispatcher_send(
                self.hass,
                SIGNAL_NEW_FRIEND.format(self.entry_id),
                list(self.states),
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
            raw = movement["status"]
            st.movement = "no_geo" if raw == "noGeo" else raw

        place = s.get("locationPlace") or {}
        status = (place.get("status") or {}).get("id")
        if status:
            st.place = status
