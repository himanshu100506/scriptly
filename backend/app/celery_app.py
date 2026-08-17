import os
from celery import Celery

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "video_to_script",
    broker=REDIS_URL,
    backend=REDIS_URL,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    # Transcription can take several minutes on CPU — give tasks room before
    # Celery considers them stuck. Adjust if you move to GPU/managed STT.
    task_time_limit=30 * 60,       # hard kill after 30 min
    task_soft_time_limit=25 * 60,  # raises SoftTimeLimitExceeded first
    result_expires=60 * 60 * 24,   # keep results for 24h
)

# Ensures tasks.py gets registered with this app
celery_app.autodiscover_tasks(["app"])