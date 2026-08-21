from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from celery.result import AsyncResult
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.celery_app import celery_app
from app.tasks import process_video
from app.schemas import (
    SubmitJobRequest, SubmitJobResponse, JobStatusResponse,
    SignupRequest, LoginRequest, AuthResponse, UserResponse,
)
from app.database import get_db, engine, Base
from app.models import User, Subscription
from app.auth_utils import hash_password, verify_password, create_access_token
from app.dependencies import get_current_user
from app.subscriptions_router import router as subscriptions_router

# Creates the users table (and now subscriptions table) on startup if missing.
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Video-to-Script API")

# Tighten allow_origins to your actual frontend domain before going live.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(subscriptions_router, prefix="/api")

FREE_SCRIPT_LIMIT = 3  # adjust as needed, or move to an env var later


@app.post("/api/auth/signup", response_model=AuthResponse)
def signup(req: SignupRequest, db: Session = Depends(get_db)):
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    user = User(email=req.email.lower(), hashed_password=hash_password(req.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    db.refresh(user)

    token = create_access_token(str(user.id), user.email)
    return AuthResponse(access_token=token, user=UserResponse(id=str(user.id), email=user.email))


@app.post("/api/auth/login", response_model=AuthResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == req.email.lower()).first()
    if not user or not verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect email or password")

    token = create_access_token(str(user.id), user.email)
    return AuthResponse(access_token=token, user=UserResponse(id=str(user.id), email=user.email))


@app.get("/api/auth/me", response_model=UserResponse)
def get_me(user: User = Depends(get_current_user)):
    return UserResponse(id=str(user.id), email=user.email)


def _has_active_subscription(user: User, db: Session) -> bool:
    sub = db.query(Subscription).filter_by(user_id=user.id).first()
    return bool(sub and sub.status == "active")


@app.get("/api/usage")
def get_usage(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Frontend polls this to show remaining free scripts / subscribed state."""
    subscribed = _has_active_subscription(user, db)
    return {
        "subscribed": subscribed,
        "used": user.free_scripts_used,
        "limit": FREE_SCRIPT_LIMIT,
        "remaining": None if subscribed else max(0, FREE_SCRIPT_LIMIT - user.free_scripts_used),
    }


@app.post("/api/jobs", response_model=SubmitJobResponse)
def submit_job(
    req: SubmitJobRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not _has_active_subscription(user, db):
        if user.free_scripts_used >= FREE_SCRIPT_LIMIT:
            raise HTTPException(
                status_code=402,
                detail="Free script limit reached. Upgrade to Scriptly Pro to continue.",
            )
        user.free_scripts_used += 1
        db.commit()

    task = process_video.delay(str(req.url))
    return SubmitJobResponse(job_id=task.id)


@app.get("/api/jobs/{job_id}", response_model=JobStatusResponse)
def get_job_status(job_id: str):
    result = AsyncResult(job_id, app=celery_app)

    if result.state == "PENDING":
        return JobStatusResponse(job_id=job_id, status="pending")
    elif result.state == "STARTED":
        return JobStatusResponse(job_id=job_id, status="started")
    elif result.state == "SUCCESS":
        return JobStatusResponse(job_id=job_id, status="success", result=result.result)
    elif result.state == "FAILURE":
        return JobStatusResponse(job_id=job_id, status="failure", error=str(result.result))
    else:
        # e.g. RETRY — treat as still-in-progress for the client
        return JobStatusResponse(job_id=job_id, status="started")


@app.get("/api/health")
def health():
    return {"status": "ok"}