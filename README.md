# 2GIS Friends on Map — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2024.1%2B-blue.svg)](https://www.home-assistant.io/)

Track your friends' locations from the **2GIS "Friends on Map"** feature directly in Home Assistant.

---

## Features

- Live location tracking via WebSocket (cloud push, no polling)
- One HA **device** per friend
- Per-device entities:
  | Entity | Type | Description |
  |--------|------|-------------|
  | Location | Device Tracker | GPS coordinates + map card |
  | Battery | Sensor | Battery percentage |
  | Charging | Binary Sensor | Is device charging |
  | Movement | Sensor | stopped / moving / no GPS |
  | Place | Sensor | home / work / shop |
  | Speed | Sensor | km/h |
  | Distance from Home | Sensor | km from HA home coordinates |
- Select which friends to track (in setup and in Options)
- Auto-reconnect with exponential backoff
- Token auto-refresh (valid for ~1 year)
- Russian and English UI

---

## Requirements

- Home Assistant 2024.1+
- A 2GIS account with the **Friends on Map** feature enabled and at least one friend added

---

## Installation via HACS

1. Open HACS → Integrations → ⋮ → **Custom repositories**
2. Add `https://github.com/Marker284/zond2gis` with category **Integration**
3. Install **2GIS Friends on Map**
4. Restart Home Assistant
5. Go to **Settings → Devices & Services → Add Integration** → search for `2GIS`

---

## Manual Installation

```bash
cp -r custom_components/zond2gis \
  /config/custom_components/zond2gis
```

Restart Home Assistant, then add the integration via UI.

---

## Configuration

1. Enter your 2GIS email and password
2. Select which friends to track
3. Done — entities appear automatically when the WebSocket connects

To change tracked friends later: **Settings → Devices & Services → 2GIS → Configure**

---

## How It Works

The integration authenticates against `id.2gis.com` (same credentials as the 2GIS app) and opens a persistent WebSocket to `zond.api.2gis.ru`. Location updates are pushed in real time — no polling.

Friends are marked **unavailable** if no data has been received for more than 24 hours.

---

## Disclaimer

This integration uses an **unofficial, undocumented** internal API of 2GIS. It is not affiliated with or endorsed by 2GIS. Use at your own risk. The API may change or become unavailable at any time.

---

---

# 2GIS Друзья на карте — интеграция для Home Assistant

Отслеживайте местоположение друзей из функции **«Друзья на карте» в 2GIS** прямо в Home Assistant.

## Возможности

- Обновления в реальном времени через WebSocket
- Одно устройство HA на каждого друга
- Сущности на каждое устройство: трекер, батарея, зарядка, движение, место, скорость, расстояние от дома
- Выбор отслеживаемых друзей при настройке и в параметрах
- Автопереподключение при обрыве
- Авторефреш токена (~1 год)
- Русский и английский интерфейс

## Установка через HACS

1. HACS → Интеграции → ⋮ → **Пользовательские репозитории**
2. Добавить `https://github.com/Marker284/zond2gis`, категория **Интеграция**
3. Установить **2GIS Friends on Map**
4. Перезапустить Home Assistant
5. **Настройки → Устройства и службы → Добавить интеграцию** → найти `2GIS`

## Отказ от ответственности

Интеграция использует **неофициальный внутренний API** 2GIS. Не является официальным продуктом 2GIS.

---

## License

MIT. See [`LICENSE`](LICENSE).
