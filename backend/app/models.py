import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, DateTime, Integer, ForeignKey
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    free_scripts_used = Column(Integer, default=0, nullable=False)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), unique=True, nullable=False)
    razorpay_subscription_id = Column(String, unique=True, nullable=False)
    status = Column(String, nullable=False)  # created, authenticated, active, paused, cancelled, past_due
    plan_id = Column(String, nullable=False)
    current_period_end = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )