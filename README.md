# FireFlux

**Real-time IoT fire detection and response system.**  
ESP32 sensors → FastAPI backend → PostgreSQL → WebSocket broadcast → browser dashboard.

[![Python](https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square)](https://python.org)
[![FastAPI](https://img.shields.io/badge/fastapi-0.110%2B-009688?style=flat-square)](https://fastapi.tiangolo.com)
[![PostgreSQL](https://img.shields.io/badge/postgresql-15%2B-336791?style=flat-square)](https://postgresql.org)
[![ESP32](https://img.shields.io/badge/esp32-arduino-red?style=flat-square)](https://espressif.com)
[![License](https://img.shields.io/badge/license-MIT-yellow?style=flat-square)](#license)

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Repository Structure](#repository-structure)
- [Database Schema](#database-schema)
- [Risk Engine](#risk-engine)
- [API Reference](#api-reference)
- [WebSocket Protocol](#websocket-protocol)
- [Hardware Setup](#hardware-setup)
- [Installation](#installation)
- [Frontend](#frontend)
- [Data Flow](#data-flow)
- [Configuration Reference](#configuration-reference)
- [Testing](#testing)
- [Roadmap](#roadmap)
- [License](#license)

---

## Overview

FireFlux monitors temperature, gas concentration, and motion across multiple rooms in real time. Each sensor reading is ingested by a FastAPI server, classified by a risk engine, persisted to PostgreSQL, and immediately broadcast over WebSocket to all connected browser clients.

**Room assignment:**

| Room ID | Name    | Data Source              |
|---------|---------|--------------------------|
| 1       | Lab 101 | Physical ESP32 hardware  |
| 2       | Lab 102 | Python simulation script |
| 3       | Lab 103 | Python simulation script |

---

## Architecture

```
                    ┌─────────────────────────────────────────────┐
                    │                 FireFlux System              │
                    │                                             │
  ┌──────────────┐  │  ┌─────────────────────────────────────┐   │
  │    ESP32     │  │  │           FastAPI  :8000             │   │
  │              │  │  │                                     │   │
  │  DHT11 ──┐  │  │  │  POST /sensor-data                  │   │
  │  MQ2  ──┼──┼──┼──►│  POST /ingest          ┌──────────┐ │   │
  │  PIR  ──┘  │  │  │         │               │PostgreSQL│ │   │
  └──────────────┘  │  │         ▼               │          │ │   │
                    │  │  assess_risk()  ────────►│ 5 tables │ │   │
  ┌──────────────┐  │  │         │               └──────────┘ │   │
  │  Python Sim  │  │  │         ▼                            │   │
  │  (rooms 2,3) │──┼──►  WebSocket broadcast                 │   │
  └──────────────┘  │  │         │                            │   │
                    │  └─────────┼───────────────────────────┘   │
                    │            │                                 │
                    │            ▼  ws://<host>:8000/ws/{room_id} │
                    │  ┌─────────────────────────────────────┐   │
                    │  │           Browser Clients            │   │
                    │  │  index.html  buildingA.html          │   │
                    │  │  room101     room102     room103      │   │
                    │  └─────────────────────────────────────┘   │
                    └─────────────────────────────────────────────┘
```

---

## Repository Structure

```
fireflux/
├── backend/
│   ├── database.py          # SQLAlchemy engine + SessionLocal
│   ├── models.py            # ORM table definitions
│   ├── schemas.py           # Pydantic input validation
│   └── main.py              # App entrypoint — routes, WS manager, risk engine
│
├── firmware/
│   └── esp32_sensor.ino     # Arduino sketch for ESP32
│
├── frontend/
│   ├── index.html           # Command center (Leaflet map + simulator)
│   ├── buildingA.html       # Floor plan with live room tiles
│   ├── room101.html         # Room dashboard — room_id = 1
│   ├── room102.html         # Room dashboard — room_id = 2
│   └── room103.html         # Room dashboard — room_id = 3
│
└── README.md
```

---

## Database Schema

All tables are created automatically via `Base.metadata.create_all(bind=engine)` on startup.  
Room rows are inserted by `seed_rooms()` if the `rooms` table is empty.

```
rooms
├── id           INTEGER   PRIMARY KEY
├── name         VARCHAR
├── floor        INTEGER
├── description  VARCHAR
└── created_at   TIMESTAMP DEFAULT utcnow

sensor_readings
├── id           INTEGER   PRIMARY KEY
├── room_id      INTEGER   FK → rooms.id
├── temperature  FLOAT
├── gas_value    FLOAT
├── motion       BOOLEAN
└── recorded_at  TIMESTAMP DEFAULT utcnow

risk_assessments
├── id           INTEGER   PRIMARY KEY
├── room_id      INTEGER   FK → rooms.id
├── risk_level   VARCHAR   -- 'safe' | 'warning' | 'danger'
├── reason       VARCHAR
└── assessed_at  TIMESTAMP DEFAULT utcnow

danger_events
├── id           INTEGER   PRIMARY KEY
├── room_id      INTEGER   FK → rooms.id
├── trigger      VARCHAR
├── started_at   TIMESTAMP DEFAULT utcnow
└── ended_at     TIMESTAMP DEFAULT NULL   -- NULL = event still active

alerts
├── id           INTEGER   PRIMARY KEY
├── room_id      INTEGER   FK → rooms.id
├── event_id     INTEGER   FK → danger_events.id
├── message      TEXT
└── sent_at      TIMESTAMP DEFAULT utcnow
```

### Event lifecycle

A `DangerEvent` row is opened when a `danger` reading arrives and no open event exists for that room. It is closed (i.e., `ended_at` is written) when a subsequent reading returns to `safe`. This ensures danger incidents are bounded and queryable by duration.

```python
# Open
if risk_level == "danger":
    open_event = db.query(DangerEvent).filter(
        DangerEvent.room_id == room_id,
        DangerEvent.ended_at == None
    ).first()
    if not open_event:
        db.add(DangerEvent(room_id=room_id, trigger=reason))

# Close
else:
    open_event = db.query(DangerEvent).filter(...).first()
    if open_event:
        open_event.ended_at = datetime.utcnow()
```

---

## Risk Engine

`assess_risk(temperature, gas_value, motion, room_id)` in `main.py`:

| Level     | Condition                              | risk_score |
|-----------|----------------------------------------|------------|
| `safe`    | temp < 57 °C **and** gas < 1000 ppm   | `0.0`      |
| `warning` | temp ≥ 57 °C **or** gas ≥ 1000 ppm    | `0.5`      |
| `danger`  | temp ≥ 78 °C **or** gas ≥ 2000 ppm    | `0.9`      |

Conditions are evaluated in order from most severe to least — the first match wins. Both temperature and gas can independently trigger `warning` or `danger`; all triggered reasons are concatenated into the `reason` field.

---

## API Reference

### Ingest endpoints

| Method | Path           | Body                                             | Description                             |
|--------|----------------|--------------------------------------------------|-----------------------------------------|
| `POST` | `/sensor-data` | `SensorReadingInput`                             | ESP32 endpoint — saves to DB, no return |
| `POST` | `/ingest`      | `SensorReadingInput`                             | Saves + returns full risk payload       |

**`SensorReadingInput` schema:**
```json
{
  "room_id":     1,
  "temperature": 28.5,
  "gas_value":   400.0,
  "motion":      false
}
```

**`/ingest` response:**
```json
{
  "room_id":     1,
  "temperature": 28.5,
  "gas_value":   400.0,
  "motion":      false,
  "risk_score":  0.0,
  "risk_level":  "safe",
  "reason":      "All readings normal",
  "timestamp":   "2026-03-10T20:42:15.123456"
}
```

### Query endpoints

| Method | Path                               | Query params  | Description                          |
|--------|------------------------------------|---------------|--------------------------------------|
| `GET`  | `/rooms`                           | —             | List all rooms                       |
| `GET`  | `/rooms/{room_id}/latest`          | —             | Latest reading + risk for one room   |
| `GET`  | `/rooms/{room_id}/history`         | `limit` (int) | Last N readings with risk assessment |
| `GET`  | `/danger-events`                   | —             | All events, ordered by started_at    |
| `GET`  | `/alerts`                          | —             | All alerts, ordered by sent_at       |

Interactive docs available at `http://<host>:8000/docs` (Swagger UI).

---

## WebSocket Protocol

**Endpoint:** `ws://<host>:8000/ws/{room_id}`

One connection per room. The server broadcasts a JSON message to all active connections for a room every time a new reading is processed for it. The client must only call `receive_text()` to keep the connection alive — no heartbeat messages are required.

**Broadcast payload** (identical to `/ingest` response):
```json
{
  "room_id":     1,
  "temperature": 28.5,
  "gas_value":   400.0,
  "motion":      false,
  "risk_score":  0.0,
  "risk_level":  "safe",
  "reason":      "All readings normal",
  "timestamp":   "2026-03-10T20:42:15.123456"
}
```

**Client reconnection** (implemented in all frontend pages):
```javascript
function openWS(roomId) {
  const ws = new WebSocket(`${WS}/ws/${roomId}`);
  ws.onmessage = e => handleUpdate(JSON.parse(e.data));
  ws.onclose   = () => setTimeout(() => openWS(roomId), 3000); // 3s back-off
}
```

**Server-side connection manager** uses a `Dict[int, List[WebSocket]]` to track active connections per room. Dead sockets are silently removed from the list on next broadcast.

---

## Hardware Setup

### Components

| Component | Model  | Role               |
|-----------|--------|--------------------|
| MCU       | ESP32 DevKit V1 | WiFi + GPIO control |
| Temp sensor | DHT11 | Temperature (±2 °C accuracy) |
| Gas sensor  | MQ2   | LPG / smoke / CO analogue output |
| Motion sensor | HC-SR501 PIR | Passive infrared motion detection |

### Pin mapping

| GPIO | Sensor      | Type    | Notes                              |
|------|-------------|---------|------------------------------------|
| 4    | DHT11 DATA  | Digital | 10 kΩ pull-up resistor to 3.3 V required |
| 34   | MQ2 AOUT    | Analog  | ADC1 — input-only pin, 0–4095 range |
| 27   | PIR OUT     | Digital | HIGH on motion detected            |

> GPIO 34–39 on ESP32 are input-only. Never connect them to output signals.  
> MQ2 requires approximately 60 seconds warm-up time after power-on for stable readings.

### Wiring

```
ESP32 3.3V ──────── DHT11 VCC
ESP32 GND  ──────── DHT11 GND
ESP32 GPIO4 ─┬───── DHT11 DATA
             └── 10kΩ ── 3.3V

ESP32 5V   ──────── MQ2 VCC
ESP32 GND  ──────── MQ2 GND
ESP32 GPIO34 ─────── MQ2 AOUT

ESP32 5V   ──────── PIR VCC
ESP32 GND  ──────── PIR GND
ESP32 GPIO27 ─────── PIR OUT
```

### Firmware configuration

Edit the top of `firmware/esp32_sensor.ino` before flashing:

```cpp
const char* ssid      = "YOUR_WIFI_SSID";
const char* password  = "YOUR_WIFI_PASSWORD";
const char* serverUrl = "http://10.63.176.231:8000/sensor-data";

#define DHT_PIN   4
#define MQ2_PIN   34
#define PIR_PIN   27
#define ROOM_ID   1
#define INTERVAL  5000   // ms between readings
```

The sketch sends a `POST /sensor-data` every 5 seconds with a JSON body. It does not use HTTPS or authentication.

---

## Installation

### Requirements

- Python 3.10+
- PostgreSQL 15+ running locally (pgAdmin or native service)
- Arduino IDE 2.x with ESP32 board support

### 1 — Clone

```bash
git clone https://github.com/yourusername/fireflux.git
cd fireflux
```

### 2 — Python dependencies

```bash
pip install fastapi uvicorn sqlalchemy psycopg2-binary pydantic
```

### 3 — Database

Create the database in pgAdmin or psql:

```sql
CREATE DATABASE fireflux;
```

Set the connection string in `backend/database.py`:

```python
DATABASE_URL = "postgresql://postgres:YOUR_PASSWORD@localhost:5432/fireflux"
```

### 4 — Run the backend

```bash
cd backend
uvicorn main:app --host 0.0.0.0 --port 8000
```

On first boot, SQLAlchemy creates all five tables and `seed_rooms()` inserts the three room rows. No migration tooling is needed.

### 5 — Flash firmware

- Open `firmware/esp32_sensor.ino` in Arduino IDE
- Install `ArduinoJson`, `DHT sensor library`, `Adafruit Unified Sensor` via Library Manager
- Set board to **ESP32 Dev Module**
- Edit WiFi credentials and server IP, then upload

### 6 — Open the frontend

Update the IP in every HTML file:

```javascript
const API = "http://10.63.176.231:8000";   // ← change to your PC's IP
const WS  = "ws://10.63.176.231:8000";
```

Open `frontend/index.html` directly in a browser — no web server required. Leaflet.js is loaded dynamically from CDN; if the CDN is unreachable the map panel degrades gracefully to an offline message without blocking the rest of the UI.

### Firewall (Windows)

The ESP32 reaches the API over your LAN. Port 8000 must be open inbound:

```powershell
# Run as Administrator
New-NetFirewallRule `
  -DisplayName "FireFlux API" `
  -Direction   Inbound `
  -Protocol    TCP `
  -LocalPort   8000 `
  -Action      Allow
```

---

## Frontend

All five pages are self-contained single-file HTML — no bundler, no framework, no separate CSS or JS files.

| File             | Description |
|------------------|-------------|
| `index.html`     | Leaflet.js map centred on Sector V, Kolkata. Sidebar with building status cards (safe/warning/danger colour-coded). Lab 101 simulator panel — sliders for temperature and gas, toggle for motion, sends to `POST /ingest`. WebSocket connections to all three rooms to keep the global banner updated. |
| `buildingA.html` | Floor plan grid. Each room tile polls `GET /rooms/{id}/latest` on load then subscribes to its WebSocket. Exit signs change to red when any room enters danger. |
| `room101.html`   | Full room dashboard. Animated sensor bar for temperature (colour shifts at thresholds), animated bar for gas, pulsing motion indicator dot, risk score ring, rule-based recommendation text, Chart.js dual-axis line chart (last 50 readings, updated live), scrollable timeline table (last 10 rows, prepended on each WS message), offline warning bar. |
| `room102.html`   | Identical to `room101.html`, `ROOM_ID = 2`. |
| `room103.html`   | Identical to `room101.html`, `ROOM_ID = 3`. |

---

## Data Flow

```
1.  ESP32 collects DHT11 + MQ2 + PIR readings
2.  ESP32 → POST /sensor-data  {room_id, temperature, gas_value, motion}
3.  FastAPI validates input via Pydantic
4.  INSERT INTO sensor_readings
5.  assess_risk() → (risk_score, risk_level, reason)
6.  INSERT INTO risk_assessments
7.  danger_event logic:
      risk_level == 'danger' AND no open event → INSERT danger_events
      risk_level == 'safe'   AND open event    → UPDATE ended_at = NOW()
8.  ConnectionManager.broadcast(room_id, payload)
9.  All /ws/{room_id} clients receive JSON payload
10. Browser repaints sensor bars, banner, chart, timeline
```

---

## Configuration Reference

| Variable | File | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | `backend/database.py` | `postgresql://postgres:password@localhost:5432/fireflux` | SQLAlchemy connection string |
| `ssid` | `firmware/esp32_sensor.ino` | — | WiFi SSID |
| `password` | `firmware/esp32_sensor.ino` | — | WiFi password |
| `serverUrl` | `firmware/esp32_sensor.ino` | `http://10.63.176.231:8000/sensor-data` | Backend POST endpoint |
| `INTERVAL` | `firmware/esp32_sensor.ino` | `5000` | Reading interval in ms |
| `DHT_PIN` | `firmware/esp32_sensor.ino` | `4` | GPIO for DHT11 |
| `MQ2_PIN` | `firmware/esp32_sensor.ino` | `34` | GPIO for MQ2 analog |
| `PIR_PIN` | `firmware/esp32_sensor.ino` | `27` | GPIO for PIR |
| `API` | all `frontend/*.html` | `http://10.63.176.231:8000` | REST base URL |
| `WS` | all `frontend/*.html` | `ws://10.63.176.231:8000` | WebSocket base URL |

---

## Testing

### Manually send readings with curl

```bash
# Safe reading — room 2
curl -s -X POST http://localhost:8000/ingest \
  -H "Content-Type: application/json" \
  -d '{"room_id":2,"temperature":28.0,"gas_value":400,"motion":false}' | python -m json.tool

# Warning — elevated gas
curl -s -X POST http://localhost:8000/ingest \
  -H "Content-Type: application/json" \
  -d '{"room_id":2,"temperature":30.0,"gas_value":1200,"motion":false}' | python -m json.tool

# Danger — high temp + high gas + motion
curl -s -X POST http://localhost:8000/ingest \
  -H "Content-Type: application/json" \
  -d '{"room_id":2,"temperature":90.0,"gas_value":2800,"motion":true}' | python -m json.tool

# Verify danger event was opened
curl -s http://localhost:8000/danger-events | python -m json.tool

# Send safe reading to close the event
curl -s -X POST http://localhost:8000/ingest \
  -H "Content-Type: application/json" \
  -d '{"room_id":2,"temperature":28.0,"gas_value":400,"motion":false}' | python -m json.tool

# Verify event ended_at is now set
curl -s http://localhost:8000/danger-events | python -m json.tool
```

### Query endpoints

```bash
curl http://localhost:8000/rooms
curl http://localhost:8000/rooms/1/latest
curl "http://localhost:8000/rooms/1/history?limit=20"
curl http://localhost:8000/danger-events
curl http://localhost:8000/alerts
```

### Swagger UI

Navigate to `http://localhost:8000/docs` to explore and test all endpoints interactively.

---

## Roadmap

- [ ] Replace threshold engine with `IsolationForest` anomaly detection (scikit-learn `model.pkl`)
- [ ] Telegram Bot API push alerts on `danger` events via `alerts` table
- [ ] Python simulation scripts with realistic Gaussian random-walk data for Rooms 102 & 103
- [ ] Docker Compose for single-command local deployment
- [ ] Nginx reverse proxy + WSS (WebSocket over TLS)
- [ ] JWT authentication on API routes and dashboard
- [ ] Mobile-responsive frontend
- [ ] Multi-building live support (Buildings B–F)

---

## License

MIT License — use freely, keep attribution.

```
Copyright (c) 2026 FireFlux Project — IEM Campus, Kolkata

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```
