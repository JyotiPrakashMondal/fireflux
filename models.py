from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime

Base = declarative_base()


class Room(Base):
    __tablename__ = "rooms"

    id          = Column(Integer, primary_key=True, index=True)
    name        = Column(String)
    floor       = Column(Integer)
    description = Column(String)
    created_at  = Column(DateTime, default=datetime.utcnow)


class SensorReading(Base):
    __tablename__ = "sensor_readings"

    id          = Column(Integer, primary_key=True, index=True)
    room_id     = Column(Integer, ForeignKey("rooms.id"))
    temperature = Column(Float)
    gas_value   = Column(Float)
    motion      = Column(Boolean)
    recorded_at = Column(DateTime, default=datetime.utcnow)


class RiskAssessment(Base):
    __tablename__ = "risk_assessments"

    id          = Column(Integer, primary_key=True, index=True)
    room_id     = Column(Integer, ForeignKey("rooms.id"))
    risk_level  = Column(String)
    reason      = Column(String)
    assessed_at = Column(DateTime, default=datetime.utcnow)


class DangerEvent(Base):
    __tablename__ = "danger_events"

    id         = Column(Integer, primary_key=True, index=True)
    room_id    = Column(Integer, ForeignKey("rooms.id"))
    trigger    = Column(String)
    started_at = Column(DateTime, default=datetime.utcnow)
    ended_at   = Column(DateTime, nullable=True)


class Alert(Base):
    __tablename__ = "alerts"

    id       = Column(Integer, primary_key=True, index=True)
    room_id  = Column(Integer, ForeignKey("rooms.id"))
    event_id = Column(Integer, ForeignKey("danger_events.id"))
    message  = Column(Text)
    sent_at  = Column(DateTime, default=datetime.utcnow)