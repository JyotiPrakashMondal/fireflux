from pydantic import BaseModel
from typing import Optional
from datetime import datetime


# ============================================================
# SENSOR READING
# ============================================================
# Only this one is needed — it's the only POST endpoint
# Room 101  → ESP32 sends this
# Room 102/103 → Python script sends this

class SensorReadingInput(BaseModel):
    room_id     : int
    temperature : float
    gas_value   : float
    motion      : bool


# ============================================================
# EVERYTHING ELSE BELOW IS CREATED INTERNALLY — NO POST NEEDED
# ============================================================
#
# RiskAssessment → created by rule engine after sensor data arrives
# DangerEvent    → created automatically when room hits High-Risk
# DangerEventUpdate → updated automatically when danger is over
# Alert          → created automatically after Telegram message sent
#
# No Pydantic needed for any of these ✅