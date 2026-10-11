"""
Core video-to-script pipeline logic (download -> transcribe -> translate).
This is import-only — no CLI entrypoint here. Called by the Celery task in
tasks.py. Keeping this separate from Celery/FastAPI concerns so it stays
easy to test and reuse.

Speech-to-text has two interchangeable providers, chosen with STT_PROVIDER:
  - "groq"  : hosted Whisper (default whisper-large-v3-turbo) over HTTPS.
              Fast, accurate, needs no GPU and almost no RAM on the server.
              Requires GROQ_API_KEY. Default when GROQ_API_KEY is set.
  - "local" : faster-whisper running in this process (CPU or CUDA GPU).
              Default when GROQ_API_KEY is not set.
faster-whisper is imported lazily, so a Groq-only server does not need
faster-whisper / ctranslate2 / av installed at all.

Other behaviour:
  - Cookie support for yt-dlp via a cookies.txt file (YTDLP_COOKIES_FILE)
  - Download size cap, socket timeout and retries for yt-dlp
  - Friendly, user-safe error messages (raw errors are logged, not shown)
  - Silent / no-speech videos raise a clear error instead of returning blanks
  - Chunked, parallel Hindi / English / Hinglish translation with retries
  - Per-stage timings are returned in the result under "timings"
"""

import glob
import os
import subprocess
import sys
import tempfile
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

# Load backend/.env explicitly (this file lives in backend/app/), then fall
# back to the default upward search. Already-set environment variables win.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")
load_dotenv()

import yt_dlp
import requests


# ---------- Config ----------

MAX_DURATION_SECONDS = int(os.environ.get("MAX_DURATION_SECONDS", str(15 * 60)))  # hard cap
MAX_DOWNLOAD_MB = int(os.environ.get("MAX_DOWNLOAD_MB", "150"))  # yt-dlp size cap
FFMPEG_PATH = os.environ.get("FFMPEG_PATH", "ffmpeg")

# Which speech-to-text engine to use (see module docstring)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
STT_PROVIDER = (os.environ.get("STT_PROVIDER") or ("groq" if GROQ_API_KEY else "local")).strip().lower()
if STT_PROVIDER not in ("groq", "local"):
    print(f"[warn] Unknown STT_PROVIDER '{STT_PROVIDER}', using 'local'", file=sys.stderr)
    STT_PROVIDER = "local"

# "auto", or a language code like "hi" / "en". WHISPER_LANGUAGE is the old name.
STT_LANGUAGE = (os.environ.get("STT_LANGUAGE") or os.environ.get("WHISPER_LANGUAGE") or "auto").strip().lower()

# Groq
GROQ_MODEL = os.environ.get("GROQ_MODEL", "whisper-large-v3-turbo")  # or "whisper-large-v3"
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MAX_UPLOAD_MB = int(os.environ.get("GROQ_MAX_UPLOAD_MB", "24"))  # free tier limit is 25 MB

# Local faster-whisper
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cuda").lower()  # "cuda" or "cpu"
# Default model depends on the device: turbo is great on a GPU but too heavy
# for a small CPU server. Setting WHISPER_MODEL_SIZE explicitly always wins.
WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL_SIZE") or (
    "large-v3-turbo" if WHISPER_DEVICE == "cuda" else "small"
)
WHISPER_FALLBACK_MODEL = os.environ.get("WHISPER_FALLBACK_MODEL", "small")  # used only if GPU fails
WHISPER_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE") or (
    "float16" if WHISPER_DEVICE == "cuda" else "int8"
)
WHISPER_BATCH_SIZE = int(os.environ.get("WHISPER_BATCH_SIZE", "8"))  # lower to 4 if you hit VRAM limits
WHISPER_BATCHED = os.environ.get("WHISPER_BATCHED", "auto").lower()  # auto | true | false
WHISPER_CPU_THREADS = int(os.environ.get("WHISPER_CPU_THREADS", "0"))  # 0 = let ctranslate2 decide

# Local only: below this confidence, a detected "en" is treated as likely
# Hinglish and Hindi is forced. Always verify on real Indian-accented clips.
ENGLISH_CONFIDENCE_THRESHOLD = float(os.environ.get("ENGLISH_CONFIDENCE_THRESHOLD", "0.85"))

# Translation
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")
TRANSLATE_CHUNK_WORDS = int(os.environ.get("TRANSLATE_CHUNK_WORDS", "350"))
TRANSLATE_MAX_WORKERS = int(os.environ.get("TRANSLATE_MAX_WORKERS", "6"))

# Download / cookies
# Server: set YTDLP_COOKIES_FILE to a cookies.txt (use a throwaway account).
# Local: USE_BROWSER_COOKIES=true reads cookies straight from COOKIE_BROWSER.
YTDLP_COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE", "").strip()
USE_BROWSER_COOKIES = os.environ.get("USE_BROWSER_COOKIES", "false").lower() == "true"
COOKIE_BROWSER = os.environ.get("COOKIE_BROWSER", "firefox")


# ---------- Errors (messages are safe to show to end users) ----------

class VideoTooLongError(ValueError):
    pass


class NoSpeechError(ValueError):
    pass


class DownloadFailedError(RuntimeError):
    """str(e) is a friendly message; the raw yt-dlp error is kept in .raw."""

    def __init__(self, user_message: str, raw: str = ""):
        super().__init__(user_message)
        self.user_message = user_message
        self.raw = raw


class TranscriptionFailedError(RuntimeError):
    pass


class TranslationFailedError(RuntimeError):
    pass


def _friendly_download_error(raw: str) -> str:
    low = raw.lower()
    if "unsupported url" in low:
        return "That link isn't supported. Paste a YouTube link or a public Instagram Reel link."
    if "sign in to confirm" in low or "confirm you're not a bot" in low or "confirm you’re not a bot" in low:
        return "YouTube blocked the download request. Please try again later or try a different link."
    if "empty media response" in low or "login required" in low or "rate-limit reached or login required" in low:
        return ("Instagram wouldn't let us download this Reel. It may be private, "
                "or Instagram blocked the request. Try a public Reel or a YouTube link.")
    if "private" in low:
        return "This video is private, so we can't download it."
    if "429" in low or "too many requests" in low:
        return "That site is limiting requests right now. Please try again in a few minutes."
    if ("unavailable" in low or "not available" in low or "removed" in low
            or "404" in low or "does not exist" in low or "no video" in low):
        return "This video is unavailable or has been removed."
    return "We couldn't download that video. Check that the link is public and try again."


# ---------- Stage 1 + 2: download video, extract audio ----------

def _build_ydl_opts(out_dir: str) -> dict:
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
        "socket_timeout": 30,
        "retries": 3,
        "max_filesize": MAX_DOWNLOAD_MB * 1024 * 1024,
    }

    if YTDLP_COOKIES_FILE:
        if os.path.isfile(YTDLP_COOKIES_FILE):
            ydl_opts["cookiefile"] = YTDLP_COOKIES_FILE
        else:
            print(f"[warn] YTDLP_COOKIES_FILE set but not found: {YTDLP_COOKIES_FILE}", file=sys.stderr)
    elif USE_BROWSER_COOKIES:
        ydl_opts["cookiesfrombrowser"] = (COOKIE_BROWSER,)

    return ydl_opts


def download_audio(url: str, out_dir: str) -> tuple[str, dict]:
    ydl_opts = _build_ydl_opts(out_dir)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            duration = info.get("duration") or 0
            # NOTE: Instagram Reels often report no duration (0), so this
            # early check only works when the platform provides it. The real
            # cap is enforced again on the downloaded audio in run_pipeline().
            if duration > MAX_DURATION_SECONDS:
                raise VideoTooLongError(
                    f"Video is {int(duration)}s, exceeds the {MAX_DURATION_SECONDS // 60}-minute limit."
                )
            ydl.download([url])
            wav_path = str(Path(out_dir) / f"{info['id']}.wav")
    except VideoTooLongError:
        raise
    except Exception as e:
        raw = str(e)
        print(f"[error] download failed: {raw}", file=sys.stderr)
        raise DownloadFailedError(_friendly_download_error(raw), raw=raw) from e

    # With max_filesize, yt-dlp skips oversized files instead of raising.
    if not os.path.exists(wav_path):
        raise DownloadFailedError(
            "That video is too large to process. Try a shorter video.",
            raw=f"{wav_path} missing after download (likely over {MAX_DOWNLOAD_MB} MB)",
        )

    metadata = {
        "title": info.get("title"),
        "uploader": info.get("uploader"),
        "duration": duration,
        "source_url": url,
    }
    return wav_path, metadata


def _wav_duration_seconds(wav_path: str) -> float:
    """Real length of the extracted audio (works even when the platform reports no duration)."""
    try:
        with wave.open(wav_path, "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception as e:
        print(f"[warn] Could not read WAV duration: {e}", file=sys.stderr)
        return 0.0


# ---------- Stage 3a: speech-to-text via Groq (hosted Whisper) ----------

_LANG_NAME_TO_CODE = {
    "hindi": "hi", "urdu": "ur", "english": "en", "punjabi": "pa",
    "bengali": "bn", "tamil": "ta", "telugu": "te", "marathi": "mr",
    "gujarati": "gu", "kannada": "kn", "malayalam": "ml",
}


def _lang_code(value: str | None) -> str | None:
    """Groq may return 'hindi' or 'hi'; normalise to a short code."""
    if not value:
        return None
    v = value.strip().lower()
    return _LANG_NAME_TO_CODE.get(v, v)


def _run_ffmpeg(args: list[str]) -> None:
    subprocess.run(
        [FFMPEG_PATH, "-y", "-loglevel", "error", *args],
        check=True, capture_output=True, timeout=180,
    )


def _prepare_for_api(wav_path: str, work_dir: str) -> str:
    """
    Shrink the audio before upload: 16 kHz mono FLAC (lossless, much smaller
    than a typical WAV). If it is still over the upload limit, fall back to a
    low-bitrate MP3, which Whisper handles fine for speech.
    """
    limit = GROQ_MAX_UPLOAD_MB * 1024 * 1024
    flac_path = os.path.join(work_dir, "api_audio.flac")
    _run_ffmpeg(["-i", wav_path, "-ar", "16000", "-ac", "1", "-c:a", "flac", flac_path])
    if os.path.getsize(flac_path) <= limit:
        return flac_path

    mp3_path = os.path.join(work_dir, "api_audio.mp3")
    _run_ffmpeg(["-i", wav_path, "-ar", "16000", "-ac", "1", "-b:a", "48k", mp3_path])
    if os.path.getsize(mp3_path) > limit:
        raise RuntimeError(f"Audio is still over {GROQ_MAX_UPLOAD_MB} MB after compression")
    return mp3_path


def _groq_request(audio_path: str, language: str | None, retries: int = 3) -> dict:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY not set")

    form = {"model": GROQ_MODEL, "response_format": "verbose_json", "temperature": "0"}
    if language:
        form["language"] = language

    last_err = None
    for attempt in range(retries + 1):
        try:
            with open(audio_path, "rb") as f:
                resp = requests.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    files={"file": (os.path.basename(audio_path), f)},
                    data=form,
                    timeout=180,
                )
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = 2 ** attempt
                retry_after = resp.headers.get("retry-after")
                if retry_after and retry_after.replace(".", "", 1).isdigit():
                    wait = min(float(retry_after), 20)
                print(f"[warn] Groq returned {resp.status_code}, retrying in {wait:.0f}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"Groq error {resp.status_code}: {resp.text[:300]}")
            return resp.json()
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = e
            if attempt == retries:
                raise
            time.sleep(2 ** attempt)

    raise RuntimeError(f"Groq request failed after retries: {last_err}")


def _segments_from_groq(result: dict) -> list[dict]:
    out = []
    for s in result.get("segments") or []:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        # Drop obvious silence hallucinations ("Thanks for watching" over music, etc.)
        if (s.get("no_speech_prob") or 0) > 0.9:
            continue
        out.append({"start": round(s.get("start", 0.0), 2), "end": round(s.get("end", 0.0), 2), "text": text})
    if not out:
        text = (result.get("text") or "").strip()
        if text:
            out.append({"start": 0.0, "end": 0.0, "text": text})
    return out


def _transcribe_groq(wav_path: str, work_dir: str) -> tuple[list[dict], str | None]:
    try:
        api_audio = _prepare_for_api(wav_path, work_dir)
        forced = None if STT_LANGUAGE == "auto" else STT_LANGUAGE

        result = _groq_request(api_audio, forced)
        detected = _lang_code(result.get("language"))
        print(f"[info] Groq {GROQ_MODEL}: language={forced or detected}", file=sys.stderr)

        # Hinglish is often detected as Urdu, which comes back in Arabic
        # script. Re-run once as Hindi so we get Devanagari.
        if forced is None and detected == "ur":
            print("[info] Detected 'ur', re-running as 'hi'", file=sys.stderr)
            result = _groq_request(api_audio, "hi")
            detected = "hi"

        return _segments_from_groq(result), (forced or detected)
    except Exception as e:
        print(f"[error] Groq transcription failed: {type(e).__name__}: {e}", file=sys.stderr)
        raise TranscriptionFailedError(
            "The transcription service is busy or unavailable. Please try again in a moment."
        ) from e


# ---------- Stage 3b: speech-to-text locally (faster-whisper) ----------

def _add_windows_cuda_dll_dirs():
    """
    On Windows, pip-installed nvidia-cublas-cu12 / nvidia-cudnn-cu12 land in
    site-packages but their DLL folder isn't on PATH by default. Find the
    actual .dll files under site-packages/nvidia/ and register whatever
    directories contain them. No-op on non-Windows.
    """
    if sys.platform != "win32":
        return

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


if STT_PROVIDER == "local" and WHISPER_DEVICE == "cuda":
    _add_windows_cuda_dll_dirs()

_model = None
_batched = None


def _effective_compute_type(device: str, compute_type: str) -> str:
    # float16 variants are GPU-only; asking for them on CPU errors or crawls.
    if device == "cpu" and compute_type in ("float16", "int8_float16", "bfloat16"):
        print(f"[warn] compute_type '{compute_type}' is not valid on CPU, using 'int8'", file=sys.stderr)
        return "int8"
    return compute_type


def _want_batched(device: str) -> bool:
    if WHISPER_BATCHED == "true":
        return True
    if WHISPER_BATCHED == "false":
        return False
    return device == "cuda"  # auto: batching only pays off on GPU


def _load_model(model_size: str, device: str, compute_type: str) -> None:
    global _model, _batched
    # Imported here so a Groq-only server never needs faster-whisper installed.
    from faster_whisper import WhisperModel
    try:
        from faster_whisper import BatchedInferencePipeline  # needs faster-whisper >= 1.1.0
    except ImportError:
        BatchedInferencePipeline = None

    compute_type = _effective_compute_type(device, compute_type)
    kwargs = {"device": device, "compute_type": compute_type}
    if device == "cpu" and WHISPER_CPU_THREADS > 0:
        kwargs["cpu_threads"] = WHISPER_CPU_THREADS
    _model = WhisperModel(model_size, **kwargs)
    use_batched = _want_batched(device) and BatchedInferencePipeline is not None
    _batched = BatchedInferencePipeline(model=_model) if use_batched else None
    print(
        f"[info] Whisper loaded: {model_size} on {device} ({compute_type}), "
        f"batched={'yes' if _batched else 'no'}",
        file=sys.stderr,
    )


def get_model():
    global _model
    if _model is None:
        try:
            _load_model(WHISPER_MODEL_SIZE, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE)
        except Exception as e:
            if WHISPER_DEVICE == "cpu":
                raise  # nothing to fall back to
            print(
                f"[warn] GPU init failed ({e}), falling back to CPU with "
                f"'{WHISPER_FALLBACK_MODEL}' — expect slow, lower-quality results",
                file=sys.stderr,
            )
            _load_model(WHISPER_FALLBACK_MODEL, "cpu", "int8")
    return _model


def resolve_language(wav_path: str) -> str | None:
    """
    Local provider only. Hinglish is often misdetected (as English or Urdu):
      - Hindi/Urdu detected  -> force Hindi
      - English, high confidence -> English
      - English, low confidence  -> likely Hinglish, force Hindi
      - anything else -> trust the detection
    If detection itself fails we return None so Whisper auto-detects.
    """
    if STT_LANGUAGE != "auto":
        return STT_LANGUAGE

    try:
        from faster_whisper.audio import decode_audio
        audio = decode_audio(wav_path, sampling_rate=16000)[: 16000 * 30]  # first 30s is enough
        lang, prob, _ = get_model().detect_language(audio)
    except Exception as e:
        print(f"[warn] Language detection failed ({e}), letting Whisper auto-detect", file=sys.stderr)
        return None

    print(f"[info] Detected language '{lang}' (p={prob:.2f})", file=sys.stderr)
    if lang in ("hi", "ur"):
        return "hi"
    if lang == "en":
        return "en" if prob >= ENGLISH_CONFIDENCE_THRESHOLD else "hi"
    return lang


def _run_transcribe(wav_path: str, language: str | None) -> list[dict]:
    model = get_model()
    if _batched is not None:
        segments, _info = _batched.transcribe(
            wav_path,
            language=language,
            beam_size=5,
            batch_size=WHISPER_BATCH_SIZE,
        )
    else:
        segments, _info = model.transcribe(
            wav_path,
            language=language,
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,  # avoids repetition loops / hallucination drift
        )

    # `segments` is a lazy generator — GPU/CUDA errors surface while it is
    # consumed, so materialise it here, inside the caller's try/except.
    out = []
    for seg in segments:
        text = seg.text.strip()
        if text:
            out.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": text})
    return out


def _transcribe_local(wav_path: str) -> tuple[list[dict], str | None]:
    language = resolve_language(wav_path)
    try:
        segments = _run_transcribe(wav_path, language)
    except RuntimeError as e:
        msg = str(e).lower()
        if WHISPER_DEVICE == "cuda" and ("cublas" in msg or "cudnn" in msg or "cuda" in msg):
            print(f"[warn] GPU transcription failed ({e}), retrying on CPU", file=sys.stderr)
            _load_model(WHISPER_FALLBACK_MODEL, "cpu", "int8")
            segments = _run_transcribe(wav_path, language)
        else:
            raise
    return segments, language


def transcribe(wav_path: str, work_dir: str) -> tuple[str, list[dict], str | None]:
    """Returns (full_text, segments, language_used) using the configured provider."""
    if STT_PROVIDER == "groq":
        segments, language = _transcribe_groq(wav_path, work_dir)
    else:
        segments, language = _transcribe_local(wav_path)

    if not segments:
        raise NoSpeechError("No speech was detected in this video.")

    text = " ".join(s["text"] for s in segments).strip()
    return text, segments, language


# ---------- Stage 4: translation (Hindi / English / Hinglish) ----------

def _call_llm(prompt: str, retries: int = 2) -> str:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set")

    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                json={
                    "model": OPENROUTER_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "max_tokens": 4096,  # Devanagari is token-heavy; don't let output get cut short
                },
                timeout=90,
            )
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()

            data = resp.json()
            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                raise RuntimeError(f"Unexpected LLM response shape: {str(data)[:200]}")
            if not content or not content.strip():
                raise RuntimeError("LLM returned an empty response")
            return content.strip()
        except (requests.Timeout, requests.ConnectionError):
            if attempt == retries:
                raise
            time.sleep(2 ** attempt)

    raise RuntimeError("LLM call failed after retries")  # unreachable, keeps type-checkers happy


FIDELITY_RULES = (
    "STRICT RULES: Preserve every piece of information from the original — "
    "do not summarize, shorten, add, drop, or reinterpret anything. Every "
    "sentence and detail in the source must be represented. Only change "
    "language/script, not content or meaning. Do not add any commentary, "
    "notes, or explanation — output ONLY the converted transcript.\n\n"
)

INSTRUCTIONS = {
    "hindi": (
        "You are transcribing/adapting spoken content for an Indian audience. "
        "Convert the following transcript into natural, conversational Hindi "
        "written in Devanagari script. Keep commonly used English words as they "
        "would naturally be spoken (e.g. 'phone', 'online') rather than forcing "
        "pure/formal Hindi translations.\n\n"
    ),
    "english": (
        "Convert the following transcript (which may be in Hindi, English, or "
        "code-mixed Hinglish) into natural, fluent spoken English.\n\n"
    ),
    "hinglish": (
        "Rewrite the following transcript in Hinglish — the way an Indian "
        "creator would actually type/speak it casually, using the Roman/Latin "
        "alphabet only (no Devanagari). This is NOT a translation into pure "
        "Hindi or pure English — naturally code-switch between Hindi and "
        "English words exactly as a bilingual Indian speaker would say it out "
        "loud, transliterating Hindi words into Roman script (e.g. 'aap', "
        "'kya', 'kar sakte hain') and keeping English words as English (e.g. "
        "'invest', 'mutual fund', 'video'). Match the speaker's actual tone "
        "and phrasing as closely as possible.\n\n"
    ),
}


def _chunk_segments(segments: list[dict], max_words: int) -> list[str]:
    """Group consecutive segments into chunks of roughly max_words words, keeping order."""
    chunks, current, count = [], [], 0
    for seg in segments:
        words = len(seg["text"].split())
        if current and count + words > max_words:
            chunks.append(" ".join(current))
            current, count = [], 0
        current.append(seg["text"])
        count += words
    if current:
        chunks.append(" ".join(current))
    return chunks


def _build_prompt(kind: str, chunk: str, idx: int, total: int) -> str:
    part_note = ""
    if total > 1:
        part_note = (
            f"This is part {idx + 1} of {total} of a longer transcript. Convert ONLY "
            "this part — do not add an introduction, conclusion, or repeat any "
            "other part.\n\n"
        )
    return INSTRUCTIONS[kind] + FIDELITY_RULES + part_note + "Transcript:\n" + chunk


def make_bilingual(segments: list[dict]) -> dict:
    """
    Translate in small chunks, all chunks x all 3 languages in parallel.
    Short chunks mean the LLM is far less likely to shorten, skip, or
    truncate content, and wall-clock time stays low because everything runs
    concurrently. Chunk order is preserved when reassembling.
    """
    chunks = _chunk_segments(segments, TRANSLATE_CHUNK_WORDS)
    if not chunks:
        return {"hindi": "", "english": "", "hinglish": ""}

    kinds = ("hindi", "english", "hinglish")
    jobs = [
        (kind, _build_prompt(kind, chunk, i, len(chunks)))
        for kind in kinds
        for i, chunk in enumerate(chunks)
    ]

    try:
        with ThreadPoolExecutor(max_workers=TRANSLATE_MAX_WORKERS) as executor:
            results = list(executor.map(lambda job: _call_llm(job[1]), jobs))  # map() preserves order
    except Exception as e:
        print(f"[error] translation failed: {type(e).__name__}: {e}", file=sys.stderr)
        raise TranslationFailedError(
            "The translation step failed. Please try again in a moment."
        ) from e

    out = {}
    n = len(chunks)
    for k, kind in enumerate(kinds):
        out[kind] = "\n\n".join(results[k * n:(k + 1) * n])
    return out


# ---------- Orchestration ----------

def run_pipeline(url: str) -> dict:
    """Runs the full pipeline synchronously. Called from inside a Celery task."""
    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        wav_path, metadata = download_audio(url, tmp)

        # Enforce the duration cap on the actual audio — Instagram Reels
        # report duration 0, so the pre-download check can't be trusted.
        audio_seconds = _wav_duration_seconds(wav_path)
        if audio_seconds > MAX_DURATION_SECONDS:
            raise VideoTooLongError(
                f"Video is {int(audio_seconds)}s, exceeds the {MAX_DURATION_SECONDS // 60}-minute limit."
            )
        if not metadata.get("duration"):
            metadata["duration"] = round(audio_seconds)

        t1 = time.time()
        print(f"[timing] download: {t1 - t0:.1f}s", file=sys.stderr)

        original, segments, language = transcribe(wav_path, tmp)
        t2 = time.time()
        print(f"[timing] transcription ({STT_PROVIDER}): {t2 - t1:.1f}s", file=sys.stderr)

        bilingual = make_bilingual(segments)
        t3 = time.time()
        print(f"[timing] translation: {t3 - t2:.1f}s", file=sys.stderr)
        print(f"[timing] TOTAL: {t3 - t0:.1f}s", file=sys.stderr)

        return {
            "metadata": metadata,
            "original_transcript": original,
            "hindi": bilingual["hindi"],
            "english": bilingual["english"],
            "hinglish": bilingual["hinglish"],
            # Optional keys — existing consumers can ignore them.
            "segments": segments,   # [{"start": 0.0, "end": 3.2, "text": "..."}]
            "language": language,
            "stt_provider": STT_PROVIDER,
            "timings": {
                "download": round(t1 - t0, 1),
                "transcription": round(t2 - t1, 1),
                "translation": round(t3 - t2, 1),
                "total": round(t3 - t0, 1),
            },
        }