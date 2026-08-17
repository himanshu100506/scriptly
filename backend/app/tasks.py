from celery.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.pipeline_core import run_pipeline, VideoTooLongError, DownloadFailedError


@celery_app.task(bind=True, name="app.tasks.process_video")
def process_video(self, url: str) -> dict:
    """
    Runs the full pipeline for a given video URL. Result dict shape on
    success matches pipeline_core.run_pipeline()'s return value.
    On failure, raises — Celery stores the exception and the API layer
    translates it into a clean error response.
    """
    try:
        return run_pipeline(url)
    except VideoTooLongError as e:
        # Distinguish user-facing validation errors from real failures
        raise ValueError(f"VIDEO_TOO_LONG: {e}") from e
    except DownloadFailedError as e:
        raise ValueError(f"DOWNLOAD_FAILED: {e}") from e
    except SoftTimeLimitExceeded as e:
        raise ValueError("TIMEOUT: processing took too long") from e