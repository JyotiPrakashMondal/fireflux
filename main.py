# main.py

import json
from fastapi import FastAPI, Depends, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from database import SessionLocal, engine
from models import Base, Room, SensorReading, RiskAssessment, DangerEvent, Alert
from schemas import SensorReadingInput
from typing import Dict, List
from datetime import datetime

app = FastAPI()

# ============================================================
# CORS
# ============================================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

Base.metadata.create_all(bind=engine)


# ============================================================
# SEED ROOMS
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
        print("✅ 3 rooms inserted")
    else:
        print("Rooms already exist — skipping")
    db.close()

seed_rooms()


# ============================================================
# DB DEPENDENCY
# ============================================================
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ============================================================
# WEBSOCKET MANAGER
# ============================================================
class ConnectionManager:
    def __init__(self):
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
        if room_id in self.active:
            dead = []
            for ws in self.active[room_id]:
                try:
                    await ws.send_text(json.dumps(data))
                except:
                    dead.append(ws)
            for ws in dead:
                self.active[room_id].remove(ws)

manager = ConnectionManager()


# ============================================================
# RISK ENGINE — rule based only (no ML)
# ============================================================
def assess_risk(temperature: float, gas_value: float, motion: bool, room_id: int):
    if gas_value >= 2000 or temperature >= 78:
        reasons = []
        if gas_value >= 2000:
            reasons.append("Gas critically high")
        if temperature >= 78:
            reasons.append("Temperature critically high")
        return 0.9, "danger", ", ".join(reasons)

    elif gas_value >= 1000 or temperature >= 57:
        reasons = []
        if gas_value >= 1000:
            reasons.append("Gas elevated")
        if temperature >= 57:
            reasons.append("Temperature elevated")
        return 0.5, "warning", ", ".join(reasons)

    return 0.0, "safe", "All readings normal"


# ============================================================
# CORE FUNCTION — saves + classifies + broadcasts
# ============================================================
async def process_sensor_data(data: SensorReadingInput, db: Session):
    # 1. Save sensor reading
    reading = SensorReading(
        room_id     = data.room_id,
        temperature = data.temperature,
        gas_value   = data.gas_value,
        motion      = data.motion
    )
    db.add(reading)
    db.commit()
    db.refresh(reading)

    # 2. Classify risk
    risk_score, risk_level, reason = assess_risk(
        data.temperature, data.gas_value, data.motion, data.room_id
    )

    # 3. Save risk assessment
    assessment = RiskAssessment(
        room_id    = data.room_id,
        risk_level = risk_level,
        reason     = reason
    )
    db.add(assessment)
    db.commit()

    # 4. Handle danger events
    if risk_level == "danger":
        open_event = db.query(DangerEvent).filter(
            DangerEvent.room_id == data.room_id,
            DangerEvent.ended_at == None
        ).first()
        if not open_event:
            event = DangerEvent(room_id=data.room_id, trigger=reason)
            db.add(event)
            db.commit()
    else:
        open_event = db.query(DangerEvent).filter(
            DangerEvent.room_id == data.room_id,
            DangerEvent.ended_at == None
        ).first()
        if open_event:
            open_event.ended_at = datetime.utcnow()
            db.commit()

    # 5. Build payload
    payload = {
        "room_id"    : data.room_id,
        "temperature": data.temperature,
        "gas_value"  : data.gas_value,
        "motion"     : data.motion,
        "risk_score" : risk_score,
        "risk_level" : risk_level,
        "reason"     : reason,
        "timestamp"  : datetime.utcnow().isoformat()
    }

    # 6. Push to frontend via WebSocket
    await manager.broadcast(data.room_id, payload)

    return payload


# ============================================================
# ENDPOINTS
# ============================================================

# ESP32 sends here
@app.post("/sensor-data")
async def receive_sensor_data(data: SensorReadingInput, db: Session = Depends(get_db)):
    await process_sensor_data(data, db)
    return {"message": "Data saved successfully"}


# Frontend simulator sends here — returns risk result
@app.post("/ingest")
async def ingest(data: SensorReadingInput, db: Session = Depends(get_db)):
    payload = await process_sensor_data(data, db)
    return payload


# Latest reading for a room
@app.get("/rooms/{room_id}/latest")
def get_latest(room_id: int, db: Session = Depends(get_db)):
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
        "risk_score" : 0.0 if not assessment else 0.0,
        "risk_level" : "safe" if not assessment else assessment.risk_level,
        "reason"     : "No assessment yet" if not assessment else assessment.reason,
        "timestamp"  : reading.recorded_at.isoformat()
    }


# History for chart and timeline
@app.get("/rooms/{room_id}/history")
def get_history(room_id: int, limit: int = 10, db: Session = Depends(get_db)):
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
            "reason"     : a.reason if a else "All normal",
            "timestamp"  : r.recorded_at.isoformat()
        })
    return result


# All rooms
@app.get("/rooms")
def get_rooms(db: Session = Depends(get_db)):
    return db.query(Room).all()


# All danger events
@app.get("/danger-events")
def get_danger_events(db: Session = Depends(get_db)):
    return db.query(DangerEvent).order_by(DangerEvent.started_at.desc()).all()


# All alerts
@app.get("/alerts")
def get_alerts(db: Session = Depends(get_db)):
    return db.query(Alert).order_by(Alert.sent_at.desc()).all()


# ============================================================
# WEBSOCKET — frontend connects to /ws/{room_id}
# ============================================================
@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: int):
    await manager.connect(room_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(room_id, websocket)