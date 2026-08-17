"""
Core video-to-script pipeline logic (download -> transcribe -> translate).
This is import-only — no CLI entrypoint here. Called by the Celery task in
tasks.py. Keeping this separate from Celery/FastAPI concerns so it stays
easy to test and reuse.
"""

import glob
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def _add_windows_cuda_dll_dirs():
    """
    On Windows, pip-installed nvidia-cublas-cu12 / nvidia-cudnn-cu12 land in
    site-packages but their DLL folder isn't on PATH by default, causing
    'Library cublas64_12.dll is not found or cannot be loaded' even though
    the libraries ARE installed. Rather than guess the folder layout, find
    the actual .dll files under site-packages/nvidia/ and register whatever
    directories contain them. No-op on non-Windows.
    """
    if sys.platform != "win32":
        return

    # 'nvidia' is a namespace package (no __init__.py), so it has no
    # __file__ we can rely on — search site-packages directories directly.
    nvidia_roots = []
    for p in sys.path:
        candidate = os.path.join(p, "nvidia")
        if os.path.isdir(candidate):
            nvidia_roots.append(candidate)

    if not nvidia_roots:
        print("[warn] nvidia-cublas-cu12/nvidia-cudnn-cu12 not installed — GPU unavailable", file=sys.stderr)
        return

    dll_dirs = set()
    for nvidia_root in nvidia_roots:
        for dll_path in glob.glob(os.path.join(nvidia_root, "**", "*.dll"), recursive=True):
            dll_dirs.add(os.path.dirname(dll_path))

    if not dll_dirs:
        print(f"[warn] No CUDA DLLs found under {nvidia_roots} — GPU unavailable", file=sys.stderr)
        return

    for d in dll_dirs:
        try:
            os.add_dll_directory(d)
        except Exception as e:
            print(f"[warn] Could not register DLL dir {d}: {e}", file=sys.stderr)

    print(f"[info] Registered {len(dll_dirs)} CUDA DLL director{'y' if len(dll_dirs)==1 else 'ies'} for GPU support", file=sys.stderr)


_add_windows_cuda_dll_dirs()

from dotenv import load_dotenv
load_dotenv()  # reads .env file in the working directory into os.environ

import yt_dlp
from faster_whisper import WhisperModel
import requests

MAX_DURATION_SECONDS = 15 * 60  # hard cap — reject longer videos early
WHISPER_MODEL_SIZE = "medium"
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")  # "cuda" or "cpu"
WHISPER_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")  # float16 for GPU, int8 for CPU

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")

USE_BROWSER_COOKIES = os.environ.get("USE_BROWSER_COOKIES", "false").lower() == "true"
COOKIE_BROWSER = os.environ.get("COOKIE_BROWSER", "chrome")


class VideoTooLongError(ValueError):
    pass


class DownloadFailedError(RuntimeError):
    pass


# ---------- Stage 1 + 2: download video, extract audio ----------

def download_audio(url: str, out_dir: str) -> tuple[str, dict]:
    outtmpl = str(Path(out_dir) / "%(id)s.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "wav",
        }],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "fixup": "never",
    }

    if USE_BROWSER_COOKIES:
        ydl_opts["cookiesfrombrowser"] = (COOKIE_BROWSER,)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            duration = info.get("duration") or 0
            if duration > MAX_DURATION_SECONDS:
                raise VideoTooLongError(
                    f"Video is {duration}s, exceeds {MAX_DURATION_SECONDS}s cap."
                )
            ydl.download([url])
            wav_path = str(Path(out_dir) / f"{info['id']}.wav")
    except VideoTooLongError:
        raise
    except Exception as e:
        raise DownloadFailedError(str(e)) from e

    metadata = {
        "title": info.get("title"),
        "uploader": info.get("uploader"),
        "duration": duration,
        "source_url": url,
    }
    return wav_path, metadata


# ---------- Stage 3: speech-to-text ----------

_model = None

def get_model() -> WhisperModel:
    global _model
    if _model is None:
        try:
            _model = WhisperModel(
                WHISPER_MODEL_SIZE, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE
            )
        except Exception as e:
            # GPU libs missing/misconfigured — fall back to CPU rather than
            # crashing every request. Fix the underlying issue (see
            # _add_windows_cuda_dll_dirs) rather than relying on this path.
            print(f"[warn] GPU init failed ({e}), falling back to CPU", file=sys.stderr)
            _model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
    return _model


def transcribe(wav_path: str, initial_prompt: str | None = None) -> str:
    model = get_model()
    try:
        segments, info = model.transcribe(
            wav_path,
            beam_size=5,
            vad_filter=True,
            initial_prompt=initial_prompt,
        )
        text = " ".join(seg.text.strip() for seg in segments)
        return text.strip()
    except RuntimeError as e:
        if "cublas" in str(e).lower() or "cudnn" in str(e).lower():
            # GPU library issue discovered only now (CUDA libs load lazily on
            # first actual use, not at model creation) — fall back to CPU
            # for this call so the job doesn't just fail outright.
            print(f"[warn] GPU transcription failed ({e}), retrying on CPU", file=sys.stderr)
            global _model
            _model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
            segments, info = _model.transcribe(
                wav_path, beam_size=5, vad_filter=True, initial_prompt=initial_prompt
            )
            return " ".join(seg.text.strip() for seg in segments).strip()
        raise


# ---------- Stage 4: translation (Hindi / English / Hinglish) ----------

def _call_llm(prompt: str) -> str:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set")

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
        json={
            "model": OPENROUTER_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        },
        timeout=90,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


FIDELITY_RULES = (
    "STRICT RULES: Preserve every piece of information from the original — "
    "do not summarize, shorten, add, drop, or reinterpret anything. Every "
    "sentence and detail in the source must be represented. Only change "
    "language/script, not content or meaning. Do not add any commentary, "
    "notes, or explanation — output ONLY the converted transcript.\n\n"
)


def make_bilingual(original_transcript: str) -> dict:
    hindi_prompt = (
        "You are transcribing/adapting spoken content for an Indian audience. "
        "Convert the following transcript into natural, conversational Hindi "
        "written in Devanagari script. Keep commonly used English words as they "
        "would naturally be spoken (e.g. 'phone', 'online') rather than forcing "
        "pure/formal Hindi translations.\n\n" + FIDELITY_RULES
        + "Transcript:\n" + original_transcript
    )
    english_prompt = (
        "Convert the following transcript (which may be in Hindi, English, or "
        "code-mixed Hinglish) into natural, fluent spoken English.\n\n"
        + FIDELITY_RULES + "Transcript:\n" + original_transcript
    )
    hinglish_prompt = (
        "Rewrite the following transcript in Hinglish — the way an Indian "
        "creator would actually type/speak it casually, using the Roman/Latin "
        "alphabet only (no Devanagari). This is NOT a translation into pure "
        "Hindi or pure English — naturally code-switch between Hindi and "
        "English words exactly as a bilingual Indian speaker would say it out "
        "loud, transliterating Hindi words into Roman script (e.g. 'aap', "
        "'kya', 'kar sakte hain') and keeping English words as English (e.g. "
        "'invest', 'mutual fund', 'video'). Match the speaker's actual tone "
        "and phrasing as closely as possible.\n\n" + FIDELITY_RULES
        + "Transcript:\n" + original_transcript
    )

    # Run all three translation calls in parallel instead of sequentially —
    # they're independent network calls, no reason to wait for one before
    # starting the next. Cuts this stage to roughly 1/3 the time.
    with ThreadPoolExecutor(max_workers=3) as executor:
        hindi_future = executor.submit(_call_llm, hindi_prompt)
        english_future = executor.submit(_call_llm, english_prompt)
        hinglish_future = executor.submit(_call_llm, hinglish_prompt)

        return {
            "hindi": hindi_future.result(),
            "english": english_future.result(),
            "hinglish": hinglish_future.result(),
        }


# ---------- Orchestration ----------

def run_pipeline(url: str) -> dict:
    """Runs the full pipeline synchronously. Called from inside a Celery task."""
    with tempfile.TemporaryDirectory() as tmp:
        wav_path, metadata = download_audio(url, tmp)
        original = transcribe(wav_path)
        bilingual = make_bilingual(original)

        return {
            "metadata": metadata,
            "original_transcript": original,
            "hindi": bilingual["hindi"],
            "english": bilingual["english"],
            "hinglish": bilingual["hinglish"],
        }