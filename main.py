# main.py
from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime
import asyncio
import os
from dotenv import load_dotenv
from database import SessionLocal, engine, Base
import models
import schemas
import crud
import auth
from background import expiry_loop

# Load environment variables manually
def load_env_file():
    env_file = os.path.join(os.path.dirname(__file__), '.env')
    if os.path.exists(env_file):
        with open(env_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ[key] = value

load_env_file()

# create tables
Base.metadata.create_all(bind=engine)

app = FastAPI(title="FocusBubble Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Startup background task
@app.on_event("startup")
async def startup_event():
    # optionally set GOOGLE_CLIENT_ID from env
    from auth import GOOGLE_CLIENT_ID
    env_cid = os.getenv("GOOGLE_CLIENT_ID")
    if env_cid:
        auth.GOOGLE_CLIENT_ID = env_cid

    # start expiry loop
    loop = asyncio.get_event_loop()
    loop.create_task(expiry_loop(30))


# Health
@app.get("/health")
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat()}

# Test endpoint to verify Android connectivity
@app.post("/test/echo")
def test_echo(data: dict):
    return {"received": data, "message": "Backend is reachable!"}


# AUTH: verify google token and create/get user
@app.post("/auth/google", response_model=schemas.UserOut)
def google_sign_in(token_in: schemas.TokenIn, db: Session = Depends(get_db)):
    payload = auth.verify_google_token(token_in.id_token)
    # payload has keys: email, name, picture etc
    email = payload.get("email")
    name = payload.get("name")
    picture = payload.get("picture")
    if not email:
        raise HTTPException(status_code=400, detail="Google token missing email")
    user = crud.get_or_create_user(db, email=email, name=name, picture=picture)
    return user


# USERS
@app.post("/users", response_model=schemas.UserOut)
def create_user(user_in: schemas.UserCreate, db: Session = Depends(get_db)):
    user = crud.get_or_create_user(db, email=user_in.email, name=user_in.name, picture=user_in.picture)
    return user

@app.get("/users/{user_id}", response_model=schemas.UserOut)
def get_user(user_id: int, db: Session = Depends(get_db)):
    u = crud.get_user(db, user_id)
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    return u


# SCHEDULES
@app.post("/users/{user_id}/schedules", response_model=schemas.ScheduleOut)
def create_schedule_for_user(user_id:int, s_in: schemas.ScheduleCreate, db: Session = Depends(get_db)):
    user = crud.get_user(db, user_id)
    if not user: raise HTTPException(status_code=404, detail="User not found")
    s = crud.create_schedule(db, user_id, s_in)
    return {
        "id": s.id, "label": s.label, "duration_minutes": s.duration_minutes,
        "apps": s.apps_csv.split(",") if s.apps_csv else [], "is_active": s.is_active,
        "created_at": s.created_at
    }

@app.get("/users/{user_id}/schedules")
def list_schedules_for_user(user_id:int, db: Session = Depends(get_db)):
    return crud.list_schedules(db, user_id)

@app.delete("/users/{user_id}/schedules/{schedule_id}")
def delete_schedule_for_user(user_id:int, schedule_id:int, db: Session = Depends(get_db)):
    ok = crud.delete_schedule(db, user_id, schedule_id)
    if not ok: raise HTTPException(status_code=404, detail="Schedule not found")
    return {"ok": True}


# SESSIONS (start/pause/resume/stop)
@app.post("/users/{user_id}/sessions", response_model=schemas.SessionOut)
def start_session_for_user(user_id:int, body: schemas.SessionCreate, db: Session = Depends(get_db)):
    user = crud.get_user(db, user_id)
    if not user: raise HTTPException(status_code=404, detail="User not found")

    # Create session
    session = crud.start_session(db, user_id, body.schedule_id, body.duration_minutes)

    # create blocked apps entries for selected schedule if schedule provided
    if body.schedule_id:
        sched = db.query(models.Schedule).filter(models.Schedule.id == body.schedule_id).first()
        if sched and sched.apps_csv:
            pkgs = sched.apps_csv.split(",")
            crud.create_blocked_apps_for_session(db, user_id, pkgs, body.duration_minutes)

    return session

@app.post("/sessions/{session_id}/pause", response_model=schemas.SessionOut)
def pause_session(session_id:int, db: Session = Depends(get_db)):
    s = crud.pause_session(db, session_id)
    if not s: raise HTTPException(status_code=404, detail="Session not found")
    return s

@app.post("/sessions/{session_id}/resume", response_model=schemas.SessionOut)
def resume_session(session_id:int, db: Session = Depends(get_db)):
    s = crud.resume_session(db, session_id)
    if not s: raise HTTPException(status_code=404, detail="Session not found")
    return s

@app.post("/sessions/{session_id}/stop", response_model=schemas.SessionOut)
def stop_session(session_id:int, db: Session = Depends(get_db)):
    s = crud.stop_session(db, session_id)
    if not s: raise HTTPException(status_code=404, detail="Session not found")
    # Also deactivate blocked apps for that user which are active and end_time > now
    from datetime import datetime
    now = datetime.utcnow()
    blocks = db.query(models.BlockedApp).filter(models.BlockedApp.user_id == s.user_id, models.BlockedApp.is_active == True).all()
    for b in blocks:
        b.is_active = False
        b.end_time = now
    db.commit()
    return s

@app.get("/users/{user_id}/sessions/active")
def list_active_sessions_for_user(user_id:int, db: Session = Depends(get_db)):
    rows = crud.list_active_sessions(db, user_id)
    return rows


# BLOCKED APPS endpoints
@app.post("/users/{user_id}/blocks", response_model=List[schemas.BlockedAppOut])
def create_blocks(user_id:int, body: List[schemas.BlockedAppCreate], db: Session = Depends(get_db)):
    user = crud.get_user(db, user_id)
    if not user: raise HTTPException(status_code=404, detail="User not found")
    created = []
    for b in body:
        start = b.start_time or datetime.utcnow()
        end = b.end_time
        if not end:
            from datetime import timedelta
            end = start + timedelta(minutes=25)
        # create directly in DB
        row = models.BlockedApp(
            user_id=user_id,
            package_name=b.package_name,
            app_name=b.app_name,
            start_time=start,
            end_time=end,
            is_active=True
        )
        db.add(row); created.append(row)
    db.commit()
    for c in created: db.refresh(c)
    result = [{
        "id": c.id,
        "package_name": c.package_name,
        "app_name": c.app_name,
        "start_time": c.start_time,
        "end_time": c.end_time,
        "is_active": c.is_active
    } for c in created]
    return result

@app.get("/users/{user_id}/blocks", response_model=List[schemas.BlockedAppOut])
def get_active_blocks(user_id:int, db: Session = Depends(get_db)):
    rows = crud.list_active_blocked_apps(db, user_id)
    return rows

@app.post("/refresh_blocks")
def refresh_blocks(db: Session = Depends(get_db)):
    expired = crud.deactivate_expired_blocks(db)
    return {"expired": len(expired)}