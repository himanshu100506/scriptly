"""
Razorpay subscriptions for Scriptly Pro (flat monthly plan).

Setup checklist:
  1. pip install razorpay
  2. Add to backend/.env: RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET,
     RAZORPAY_WEBHOOK_SECRET, RAZORPAY_PLAN_ID
     (create the Plan once in the Razorpay dashboard first, then copy its ID)
  3. This file is already mounted in main.py via:
     app.include_router(subscriptions_router, prefix="/api")
  4. Once deployed, point Razorpay Dashboard > Webhooks at:
     https://your-domain.com/api/subscriptions/webhook
"""

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone

import razorpay
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models import Subscription, User

router = APIRouter(tags=["subscriptions"])

RAZORPAY_KEY_ID = os.environ["RAZORPAY_KEY_ID"]
RAZORPAY_KEY_SECRET = os.environ["RAZORPAY_KEY_SECRET"]
RAZORPAY_WEBHOOK_SECRET = os.environ["RAZORPAY_WEBHOOK_SECRET"]
RAZORPAY_PLAN_ID = os.environ["RAZORPAY_PLAN_ID"]

client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))


@router.post("/subscriptions/create")
def create_subscription(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Creates (or reuses) a Razorpay subscription for the logged-in user
    and returns what the frontend needs to open Razorpay Checkout."""

    existing = (
        db.query(Subscription)
        .filter_by(user_id=current_user.id)
        .filter(Subscription.status.in_(["created", "authenticated", "active"]))
        .first()
    )
    if existing:
        return {
            "subscription_id": existing.razorpay_subscription_id,
            "razorpay_key_id": RAZORPAY_KEY_ID,
            "user_email": current_user.email,
        }

    rzp_subscription = client.subscription.create(
        {
            "plan_id": RAZORPAY_PLAN_ID,
            "customer_notify": 1,
            "total_count": 120,  # ~10 years of monthly cycles; cancelling ends it early
            "notes": {"user_id": str(current_user.id)},
        }
    )

    sub = Subscription(
        user_id=current_user.id,
        razorpay_subscription_id=rzp_subscription["id"],
        status=rzp_subscription["status"],  # "created"
        plan_id=RAZORPAY_PLAN_ID,
    )
    db.add(sub)
    db.commit()

    return {
        "subscription_id": rzp_subscription["id"],
        "razorpay_key_id": RAZORPAY_KEY_ID,
        "user_email": current_user.email,
    }


@router.get("/subscriptions/me")
def get_my_subscription(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    sub = db.query(Subscription).filter_by(user_id=current_user.id).first()
    if not sub:
        return {"status": "none"}
    return {
        "status": sub.status,
        "current_period_end": sub.current_period_end,
    }


@router.post("/subscriptions/webhook")
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    expected = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    payload = json.loads(body)
    event = payload.get("event")
    entity = payload.get("payload", {}).get("subscription", {}).get("entity")
    if not entity:
        return {"status": "ignored"}

    rzp_sub_id = entity["id"]
    sub = db.query(Subscription).filter_by(razorpay_subscription_id=rzp_sub_id).first()
    if not sub:
        return {"status": "ignored"}

    if event == "subscription.authenticated":
        sub.status = "authenticated"
    elif event == "subscription.activated":
        sub.status = "active"
    elif event == "subscription.charged":
        sub.status = "active"
        current_end = entity.get("current_end")
        if current_end:
            sub.current_period_end = datetime.fromtimestamp(current_end, tz=timezone.utc)
    elif event in ("subscription.cancelled", "subscription.completed", "subscription.expired"):
        sub.status = "cancelled"
    elif event == "subscription.paused":
        sub.status = "paused"
    elif event == "payment.failed":
        sub.status = "past_due"

    sub.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {"status": "ok"}