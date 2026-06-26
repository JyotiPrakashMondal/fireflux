# train_model.py
#
# Run this script ONCE after collecting enough sensor data.
# It reads from your PostgreSQL database and saves a trained
# Isolation Forest model for each room as a .pkl file.
#
# Usage:
#   python train_model.py
#
# Re-run anytime you want to retrain (e.g. every few weeks).
# The server must be RESTARTED after retraining to pick up the new model.

import os
import pickle
import numpy as np
from database import SessionLocal
from models import SensorReading

# ============================================================
# CONFIG
# ============================================================

# Minimum number of rows required before training is attempted.
# Too few rows = the model has no idea what "normal" looks like.
MIN_ROWS = 500

# Which rooms to train a model for.
# Room 1 = real ESP32. Rooms 2 & 3 = simulated.
# You can train on simulated data too — it will still work,
# but a model trained on real data is always better.
ROOM_IDS = [1, 2, 3]

# Where to save the .pkl files.
# Must be the same folder your main.py runs from.
OUTPUT_DIR = "."

# Isolation Forest settings:
#   n_estimators  — number of trees (more = more accurate, slower to train)
#   contamination — fraction of data you expect to be anomalous
#                   0.05 = "assume 5% of my training data was already unusual"
#                   increase to 0.1 if your building has frequent false readings
CONTAMINATION = 0.05
N_ESTIMATORS  = 100


# ============================================================
# TRAINING FUNCTION
# ============================================================

def train_room(room_id: int, db):
    print(f"\n── Room {room_id} ─────────────────────────────")

    # Pull all readings for this room from the database
    readings = db.query(SensorReading).filter(
        SensorReading.room_id == room_id
    ).order_by(SensorReading.recorded_at.asc()).all()

    total = len(readings)
    print(f"   Found {total} readings in database")

    if total < MIN_ROWS:
        print(f"   ⚠️  Need at least {MIN_ROWS} rows to train.")
        print(f"   ⏳ Leave ESP32 running and come back when you have more data.")
        print(f"   ℹ️  At 5s per reading that's ~{MIN_ROWS * 5 // 60} minutes of collection.")
        return False

    # Build feature matrix
    # Features: temperature, gas_value
    # We deliberately exclude motion — motion alone doesn't indicate fire
    X = np.array([
        [r.temperature, r.gas_value]
        for r in readings
    ])

    print(f"   Temperature — min: {X[:,0].min():.1f}°C  max: {X[:,0].max():.1f}°C  mean: {X[:,0].mean():.1f}°C")
    print(f"   Gas value   — min: {X[:,1].min():.0f}ppm  max: {X[:,1].max():.0f}ppm  mean: {X[:,1].mean():.0f}ppm")

    # Train the Isolation Forest
    print(f"   Training Isolation Forest (n_estimators={N_ESTIMATORS}, contamination={CONTAMINATION})...")

    from sklearn.ensemble import IsolationForest
    model = IsolationForest(
        n_estimators  = N_ESTIMATORS,
        contamination = CONTAMINATION,
        random_state  = 42,     # fixed seed = reproducible results
        n_jobs        = -1      # use all CPU cores
    )
    model.fit(X)

    # Quick sanity check — score a few of the training rows
    # Most should come back as "normal" (score > 0)
    sample_scores = model.decision_function(X[:20])
    flagged = (sample_scores < -0.05).sum()
    print(f"   Sanity check — {flagged}/20 sample rows flagged as anomalous (expect ~{int(20 * CONTAMINATION)})")

    # Save to disk
    output_path = os.path.join(OUTPUT_DIR, f"model_room{room_id}.pkl")
    with open(output_path, "wb") as f:
        pickle.dump(model, f)

    print(f"   ✅ Saved → {output_path}")
    return True


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    print("=" * 50)
    print("  FireFlux — Model Training Script")
    print("=" * 50)

    db = SessionLocal()

    trained = 0
    skipped = 0

    try:
        for room_id in ROOM_IDS:
            success = train_room(room_id, db)
            if success:
                trained += 1
            else:
                skipped += 1
    finally:
        db.close()

    print(f"\n{'=' * 50}")
    print(f"  Done — {trained} model(s) trained, {skipped} skipped")
    if trained > 0:
        print(f"  Restart your FastAPI server to load the new models.")
    print("=" * 50)