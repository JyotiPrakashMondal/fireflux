import os
import pickle
import json
import numpy as np

# LOGIN EMAIL START
import smtplib
from email.message import EmailMessage
from pydantic import BaseModel
# LOGIN EMAIL END

from fastapi                  import FastAPI, Depends, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors  import CORSMiddleware
from sqlalchemy.orm           import Session
from database                 import SessionLocal, engine
from models                   import Base, Room, SensorReading, RiskAssessment, DangerEvent, Alert
from schemas                  import SensorReadingInput
from typing                   import Dict, List
from datetime                 import datetime


# ============================================================
# LOAD ML MODELS — one per room
# ============================================================
# Models are saved by train_model.py as model_room1.pkl etc.
# If a model file doesn't exist yet, that room falls back to
# pure IS 2189 rule-based classification — no crash, no problem.

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def load_models() -> Dict[int, object]:
    loaded = {}

    for room_id in [1, 2, 3]:
        path = os.path.join(BASE_DIR, f"model_room{room_id}.pkl")
        if os.path.exists(path):
            with open(path, "rb") as f:
                loaded[room_id] = pickle.load(f)
            print(f"✅ ML model loaded — Room {room_id}  ({path})")
        else:
            loaded[room_id] = None
            print(f"⚠️  No model for Room {room_id} — using IS 2189 rules only")
    return loaded

models = load_models()


# LOGIN EMAIL START
# Temporary email list. It becomes empty when FastAPI server restarts.
registered_emails = []

SENDER_EMAIL    = "jyoti111333999@gmail.com"
SENDER_PASSWORD = "akjznfvneqecwetx"

class EmailRegister(BaseModel):
    email: str


def send_danger_email(room_id: int, reason: str):
    if len(registered_emails) == 0:
        print("No registered emails for danger alert")
        return

    for user_email in registered_emails:
        try:
            msg = EmailMessage()
            msg["Subject"] = "FireFlux Danger Alert"
            msg["From"] = SENDER_EMAIL
            msg["To"] = user_email
            msg.set_content(
                f"DANGER detected in Room {room_id}\n\n"
                f"Reason: {reason}\n\n"
                f"Please check the FireFlux dashboard immediately."
            )

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
                smtp.login(SENDER_EMAIL, SENDER_PASSWORD)
                smtp.send_message(msg)

            print(f"Danger alert email sent to {user_email}")

        except Exception as e:
            print(f"Email failed for {user_email}: {e}")
# LOGIN EMAIL END


# ============================================================
# APP SETUP
# ============================================================

app = FastAPI(title="FireFlux API", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins  = ["*"],
    allow_methods  = ["*"],
    allow_headers  = ["*"],
)

# Create all database tables on startup if they don't exist
Base.metadata.create_all(bind=engine)


# ============================================================
# SEED ROOMS — inserts 3 rooms if table is empty
# ============================================================

def seed_rooms():
    db = SessionLocal()
    if db.query(Room).count() == 0:
        rooms = [
            Room(id=1, name="Lab 101", floor=1, description="Live ESP32 sensors"),
            Room(id=2, name="Lab 102", floor=2, description="Simulated data"),
            Room(id=3, name="Lab 103", floor=3, description="Simulated data"),
        ]
        db.add_all(rooms)
        db.commit()
        print("✅ 3 rooms seeded into database")
    else:
        print("ℹ️  Rooms already exist — skipping seed")
    db.close()

seed_rooms()


# ============================================================
# DB SESSION DEPENDENCY
# ============================================================

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# LOGIN EMAIL START
@app.post("/register-email")
def register_email(data: EmailRegister):
    email = data.email.strip().lower()

    if "@" not in email or "." not in email:
        return {"success": False, "message": "Invalid email"}

    if email not in registered_emails:
        registered_emails.append(email)

    return {
        "success": True,
        "message": "Email registered for danger alerts",
        "total_emails": len(registered_emails),
    }


@app.get("/registered-emails")
def get_registered_emails():
    return {"total_emails": len(registered_emails), "emails": registered_emails}
# LOGIN EMAIL END


# ============================================================
# WEBSOCKET CONNECTION MANAGER
# ============================================================

class ConnectionManager:
    def __init__(self):
        # Maps room_id → list of active WebSocket connections
        self.active: Dict[int, List[WebSocket]] = {}

    async def connect(self, room_id: int, websocket: WebSocket):
        await websocket.accept()
        if room_id not in self.active:
            self.active[room_id] = []
        self.active[room_id].append(websocket)

    def disconnect(self, room_id: int, websocket: WebSocket):
        if room_id in self.active:
            self.active[room_id].remove(websocket)

    async def broadcast(self, room_id: int, data: dict):
        if room_id not in self.active:
            return
        dead = []
        for ws in self.active[room_id]:
            try:
                await ws.send_text(json.dumps(data))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active[room_id].remove(ws)

manager = ConnectionManager()


# ============================================================
# RISK CLASSIFICATION ENGINE
# ============================================================
#
# Decision flow:
#
#  Step 1 — IS 2189 hard limits (always checked first, every room)
#            gas >= 2000 ppm  OR  temp >= 78°C  →  DANGER
#
#  Step 2 — ML anomaly check (only if model exists for this room)
#            score > -0.05            →  pattern looks normal  →  SAFE
#            score <= -0.05           →  anomaly detected
#              └─ values also elevated  →  WARNING
#              └─ values physically low →  SAFE  (sensor noise, not real)
#
#  Step 3 — IS 2189 warning thresholds (fallback if no ML model)
#            gas >= 1000 ppm  OR  temp >= 57°C  →  WARNING
#
#  Step 4 — Everything else  →  SAFE
#
# ============================================================

def assess_risk(temperature: float, gas_value: float, motion: bool, room_id: int):

    # Step 1: Danger rule for all rooms
    if gas_value >= 2000 or temperature >= 78:
        reasons = []
        if gas_value >= 2000:
            reasons.append("Gas critically high")
        if temperature >= 78:
            reasons.append("Temperature critically high")
        return 0.9, "danger", ", ".join(reasons)

    # Step 2: If ML model exists, use ML for safe/warning
    model = models.get(room_id)

    if model is not None:
        X = np.array([[temperature, gas_value]])
        score = model.decision_function(X)[0]

        # ML says normal
        if score > -0.05:
            # But physical value is elevated, so warning
            if gas_value >= 1000 or temperature >= 57:
                reasons = []
                if gas_value >= 1000:
                    reasons.append("Gas elevated")
                if temperature >= 57:
                    reasons.append("Temperature elevated")
                return 0.5, "warning", ", ".join(reasons)

            return 0.0, "safe", "All readings normal"

        # ML says unusual
        if temperature <= 45 and gas_value <= 1000:
            return 0.0, "safe", "All readings normal"

        reasons = []
        if gas_value >= 1000:
            reasons.append("Gas elevated")
        if temperature >= 57:
            reasons.append("Temperature elevated")

        reason = ", ".join(reasons) if reasons else "Unusual pattern detected by ML"
        return 0.5, "warning", reason

    # Step 3: If no ML model, use rule-based warning/safe
    if gas_value >= 1000 or temperature >= 57:
        reasons = []
        if gas_value >= 1000:
            reasons.append("Gas elevated")
        if temperature >= 57:
            reasons.append("Temperature elevated")
        return 0.5, "warning", ", ".join(reasons)

    return 0.0, "safe", "All readings normal"
    


# ============================================================
# CORE PROCESSING FUNCTION
# Shared by both /sensor-data and /ingest endpoints
# ============================================================

async def process_sensor_data(data: SensorReadingInput, db: Session) -> dict:

    # 1. Persist raw reading
    reading = SensorReading(
        room_id     = data.room_id,
        temperature = data.temperature,
        gas_value   = data.gas_value,
        motion      = data.motion,
    )
    db.add(reading)
    db.commit()
    db.refresh(reading)

    # 2. Classify risk
    risk_score, risk_level, reason = assess_risk(
        data.temperature,
        data.gas_value,
        data.motion,
        data.room_id,
    )

    # 3. Persist risk assessment
    assessment = RiskAssessment(
        room_id    = data.room_id,
        risk_level = risk_level,
        reason     = reason,
    )
    db.add(assessment)
    db.commit()

    # 4. Danger event lifecycle
    if risk_level == "danger":
        # Open a new event only if no event is already open for this room
        open_event = db.query(DangerEvent).filter(
            DangerEvent.room_id  == data.room_id,
            DangerEvent.ended_at == None,
        ).first()
        if not open_event:
            db.add(DangerEvent(room_id=data.room_id, trigger=reason))
            db.commit()

            # LOGIN EMAIL START
           # send_danger_email(data.room_id, reason)
            # LOGIN EMAIL END
    else:
        # Close any open event if situation has resolved
        open_event = db.query(DangerEvent).filter(
            DangerEvent.room_id  == data.room_id,
            DangerEvent.ended_at == None,
        ).first()
        if open_event:
            open_event.ended_at = datetime.utcnow()
            db.commit()

    # 5. Build broadcast payload
    payload = {
        "room_id"    : data.room_id,
        "temperature": data.temperature,
        "gas_value"  : data.gas_value,
        "motion"     : data.motion,
        "risk_score" : risk_score,
        "risk_level" : risk_level,
        "reason"     : reason,
        "ml_active"  : models.get(data.room_id) is not None,  # tells frontend if ML is on
        "timestamp"  : datetime.utcnow().isoformat(),
    }

    # 6. Push to all WebSocket clients subscribed to this room
    await manager.broadcast(data.room_id, payload)

    return payload


# ============================================================
# ROUTES — INGEST
# ============================================================

@app.post("/sensor-data")
async def receive_sensor_data(data: SensorReadingInput, db: Session = Depends(get_db)):
    """
    Used by the ESP32 firmware.
    Saves data, runs risk engine, broadcasts via WebSocket.
    Returns a simple success message (ESP32 doesn't need the full payload).
    """
    await process_sensor_data(data, db)
    return {"message": "Data saved successfully"}


@app.post("/ingest")
async def ingest(data: SensorReadingInput, db: Session = Depends(get_db)):
    """
    Used by the frontend simulator panel.
    Same as /sensor-data but returns the full risk payload so
    the simulator can display the result immediately.
    """
    return await process_sensor_data(data, db)


# ============================================================
# ROUTES — QUERY
# ============================================================

@app.api_route("/rooms", methods=["GET", "HEAD"])
def get_rooms(db: Session = Depends(get_db)):
    """List all rooms."""
    return db.query(Room).all()


@app.get("/rooms/{room_id}/latest")
def get_latest(room_id: int, db: Session = Depends(get_db)):
    """
    Latest reading + risk assessment for a room.
    Called by room pages on initial load before WebSocket connects.
    """
    reading = db.query(SensorReading).filter(
        SensorReading.room_id == room_id
    ).order_by(SensorReading.recorded_at.desc()).first()

    assessment = db.query(RiskAssessment).filter(
        RiskAssessment.room_id == room_id
    ).order_by(RiskAssessment.assessed_at.desc()).first()

    if not reading:
        return {"error": "No data yet"}

    return {
        "room_id"    : room_id,
        "temperature": reading.temperature,
        "gas_value"  : reading.gas_value,
        "motion"     : reading.motion,
        "risk_score" : 0.0,
        "risk_level" : assessment.risk_level if assessment else "safe",
        "reason"     : assessment.reason     if assessment else "No assessment yet",
        "ml_active"  : models.get(room_id) is not None,
        "timestamp"  : reading.recorded_at.isoformat(),
    }


@app.get("/rooms/{room_id}/history")
def get_history(room_id: int, limit: int = 10, db: Session = Depends(get_db)):
    """
    Last N readings with risk assessments.
    Used by the Chart.js history graph and the timeline table.
    """
    readings = db.query(SensorReading).filter(
        SensorReading.room_id == room_id
    ).order_by(SensorReading.recorded_at.desc()).limit(limit).all()

    assessments = db.query(RiskAssessment).filter(
        RiskAssessment.room_id == room_id
    ).order_by(RiskAssessment.assessed_at.desc()).limit(limit).all()

    result = []
    for i, r in enumerate(readings):
        a = assessments[i] if i < len(assessments) else None
        result.append({
            "temperature": r.temperature,
            "gas_value"  : r.gas_value,
            "motion"     : r.motion,
            "risk_level" : a.risk_level if a else "safe",
            "reason"     : a.reason     if a else "All normal",
            "timestamp"  : r.recorded_at.isoformat(),
        })
    return result


@app.get("/danger-events")
def get_danger_events(db: Session = Depends(get_db)):
    """All danger events ordered by most recent first."""
    return db.query(DangerEvent).order_by(DangerEvent.started_at.desc()).all()


@app.get("/alerts")
def get_alerts(db: Session = Depends(get_db)):
    """All alerts ordered by most recent first."""
    return db.query(Alert).order_by(Alert.sent_at.desc()).all()


@app.get("/model-status")
def get_model_status():
    """
    Shows which rooms have a trained ML model loaded.
    Useful for debugging — visit /model-status in the browser.
    """
    return {
        f"room_{room_id}": {
            "ml_loaded" : models.get(room_id) is not None,
            "model_file": f"model_room{room_id}.pkl",
            "file_exists": os.path.exists(os.path.join(BASE_DIR,f"model_room{room_id}.pkl")),
        }
        for room_id in [1, 2, 3]
    }


# ============================================================
# WEBSOCKET — /ws/{room_id}
# ============================================================

@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: int):
    """
    One persistent connection per room per browser tab.
    Server pushes JSON payload on every new sensor reading.
    Client only needs to keep the connection open — no messages to send.
    """
    await manager.connect(room_id, websocket)
    try:
        while True:
            await websocket.receive_text()   # keeps connection alive
    except WebSocketDisconnect:
        manager.disconnect(room_id, websocket)
