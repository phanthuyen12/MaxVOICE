from fastapi import status
from contextlib import asynccontextmanager
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import urllib.parse
import uuid
from typing import Dict, List, Optional

import requests
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, Query, Security, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from . import db
from .config import load_config, save_config
from .paths import job_output_dir, job_upload_dir, music_dir
from .pipeline import tasks
from .pipeline.worker import PipelineWorker
from .services import ffmpeg_service, local_tts_service, translation_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
logger = logging.getLogger("maxvoice.api")

config = load_config()
worker = PipelineWorker(
    thread_count=getattr(config, "worker_threads", 2),
    redis_url=getattr(config, "redis_url", "redis://localhost:6379/0"),
)
_cleanup_stop_event = threading.Event()
_ffmpeg_available_cache: Optional[bool] = None


def _is_ffmpeg_available() -> bool:
    global _ffmpeg_available_cache
    if _ffmpeg_available_cache is None:
        try:
            ffmpeg_service.check_binary(config.ffmpeg_path)
            _ffmpeg_available_cache = True
        except Exception:
            _ffmpeg_available_cache = False
    return _ffmpeg_available_cache


def _safe_unlink(path: str):
    try:
        if path and os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass


def cleanup_expired_files_and_jobs(retention_hours: Optional[float] = None) -> dict:
    """Automatically deletes jobs, uploaded media, output videos/voices/SRTs, and temp files
    older than `retention_hours` (defaults to `config.file_retention_hours` = 2 hours).
    """
    hours = (
        float(retention_hours)
        if retention_hours is not None
        else float(getattr(config, "file_retention_hours", 2.0))
    )
    cutoff_ts = time.time() - (hours * 3600.0)
    deleted_jobs = 0
    deleted_dirs = 0
    deleted_temp_files = 0

    # 1. Delete expired jobs from SQLite & their associated storage directories
    try:
        expired_jobs = db.list_expired_jobs(retention_hours=hours)
        for job in expired_jobs:
            job_id = job["id"]
            clip_ids = [clip["id"] for clip in db.list_clips(job_id)]
            db.delete_job(job_id)
            deleted_jobs += 1
            for owner_id in [job_id, *clip_ids]:
                for d in (
                    job_upload_dir(config, owner_id),
                    job_output_dir(config, owner_id),
                    music_dir(config, owner_id),
                ):
                    if os.path.exists(d):
                        shutil.rmtree(d, ignore_errors=True)
                        deleted_dirs += 1
    except Exception as exc:
        logger.warning("Error cleaning expired DB jobs: %s", exc)

    # 2. Sweep any orphaned upload/output/music folders older than `hours`
    for root_dir in (
        config.upload_dir,
        config.output_dir,
        os.path.join(config.storage_dir, "music"),
    ):
        if not os.path.isdir(root_dir):
            continue
        try:
            for entry in os.scandir(root_dir):
                try:
                    if entry.stat().st_mtime < cutoff_ts:
                        if entry.is_dir(follow_symlinks=False):
                            shutil.rmtree(entry.path, ignore_errors=True)
                        else:
                            os.unlink(entry.path)
                        deleted_dirs += 1
                except OSError:
                    pass
        except OSError:
            pass

    # 3. Sweep temporary TTS / preview files in system temp directory
    tmp_root = tempfile.gettempdir()
    temp_cutoff_ts = min(cutoff_ts, time.time() - 1800.0)  # max 30m for temp files
    try:
        for entry in os.scandir(tmp_root):
            if not entry.name.startswith(("kingvoice_tts_", "maxvoice_preview_", "maxvoice_mtts_", "maxvoice_clone_")):
                continue
            try:
                if entry.stat().st_mtime < temp_cutoff_ts:
                    if entry.is_dir(follow_symlinks=False):
                        shutil.rmtree(entry.path, ignore_errors=True)
                    else:
                        os.unlink(entry.path)
                    deleted_temp_files += 1
            except OSError:
                pass
    except OSError:
        pass

    if deleted_jobs or deleted_dirs or deleted_temp_files:
        logger.info(
            "Auto-cleanup (>%sh): removed %d expired jobs, %d storage folders, %d temp files",
            hours,
            deleted_jobs,
            deleted_dirs,
            deleted_temp_files,
        )
    return {
        "retention_hours": hours,
        "deleted_jobs": deleted_jobs,
        "deleted_dirs": deleted_dirs,
        "deleted_temp_files": deleted_temp_files,
    }


def _periodic_cleanup_loop():
    interval = max(60, int(getattr(config, "cleanup_interval_seconds", 300)))
    while not _cleanup_stop_event.wait(timeout=interval):
        cleanup_expired_files_and_jobs()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.connect(config.db_path)
    local_tts_service.init(config)
    cleanup_expired_files_and_jobs()
    worker.start()
    _cleanup_stop_event.clear()
    cleanup_thread = threading.Thread(
        target=_periodic_cleanup_loop, name="KingVoiceAutoCleanup", daemon=True
    )
    cleanup_thread.start()
    logger.info(
        "KingVoice Backend started (port=%s, queue=%s, retention=%sh, storage=%s, whisper=%s, threads=%s)",
        getattr(config, "port", 8686),
        worker.queue_backend,
        getattr(config, "file_retention_hours", 2.0),
        config.storage_dir,
        config.whisper_model,
        config.worker_threads,
    )
    yield
    logger.info("Stopping KingVoice worker threads...")
    _cleanup_stop_event.set()
    worker.stop()
    worker.wait(timeout_s=0.5)


app = FastAPI(
    title="KingVoice — AI Video Dubbing & Task Manager API",
    description=(
        "KingVoice containerized backend & web task manager for automatic video cutting, "
        "Faster-Whisper speech recognition, multilingual translation, neural TTS dubbing "
        "(84+ voices across Vietnamese, Russian, English, Japanese, Korean, Chinese...), and video merging."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- KingVoice API Key Security -----------------------------------------------

_api_key_header_scheme = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description="KingVoice API Key (e.g. `kv_live_...`)",
)
_bearer_scheme = HTTPBearer(
    auto_error=False,
    description="Or pass API Key via `Authorization: Bearer kv_live_...`",
)


def verify_api_key(
    x_api_key: Optional[str] = Security(_api_key_header_scheme),
    bearer: Optional[HTTPAuthorizationCredentials] = Security(_bearer_scheme),
    x_kingvoice_dashboard: Optional[str] = Header(default=None, include_in_schema=False),
    api_key: Optional[str] = Query(default=None, include_in_schema=False),
) -> str:
    """Validates `X-API-Key`, `Authorization: Bearer <key>`, or `?api_key=<key>`
    against the `api_keys` table in SQLite. Returns the client/key name.
    """
    if x_kingvoice_dashboard == "1":
        return "Web Dashboard"

    token = x_api_key or api_key
    if not token and bearer and bearer.credentials:
        token = bearer.credentials.strip()

    key_row = db.verify_and_touch_api_key(token) if token else None
    if key_row:
        return key_row.get("name") or "API Client"

    if not getattr(config, "require_api_key", True):
        return "Public / No-Key"

    raise HTTPException(
        status_code=401,
        detail="Invalid or missing KingVoice API Key. Pass header 'X-API-Key: kv_live_...'",
    )


# --- Helpers ------------------------------------------------------------------

def _save_upload_file(upload: UploadFile, dest_dir: str, prefix: str = "original") -> str:
    os.makedirs(dest_dir, exist_ok=True)
    ext = os.path.splitext(upload.filename or "")[1] or ".mp4"
    dest_path = os.path.join(dest_dir, f"{prefix}{ext}")
    with open(dest_path, "wb") as f:
        shutil.copyfileobj(upload.file, f)
    return dest_path


def _download_url_to_dir(url: str, dest_dir: str, prefix: str = "original", default_ext: str = ".mp4") -> tuple[str, str]:
    os.makedirs(dest_dir, exist_ok=True)
    parsed = urllib.parse.urlparse(url)
    base_name = os.path.basename(parsed.path) or f"{prefix}{default_ext}"
    ext = os.path.splitext(base_name)[1] or default_ext
    dest_path = os.path.join(dest_dir, f"{prefix}{ext}")
    resp = requests.get(url, stream=True, timeout=180)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 256):
            if chunk:
                f.write(chunk)
    return dest_path, base_name


def _parse_url_list(raw_urls: Optional[str]) -> list[str]:
    if not raw_urls or not raw_urls.strip():
        return []
    text = raw_urls.strip()
    if text.startswith("["):
        try:
            arr = json.loads(text)
            if isinstance(arr, list):
                return [str(u).strip() for u in arr if str(u).strip()]
        except ValueError:
            pass
    return [u.strip() for u in text.replace(",", "\n").splitlines() if u.strip()]


def _compute_job_progress(job: dict, segments: list[dict]) -> dict:
    """Computes human-friendly progress percentage and summary counts for external callers."""
    total = len(segments)
    if job.get("finalVideoStatus") == "done":
        pct = 100
    elif job.get("finalVideoStatus") == "processing":
        pct = 90
    elif total == 0:
        pct = 5 if job.get("stage") in ("cutting", "transcribing", "generating_voice") else 0
    else:
        score = 0.0
        for s in segments:
            st = s.get("status") or ""
            if st == "tts_done":
                score += 1.0
            elif st == "tts_processing":
                score += 0.75
            elif st in ("transcribed", "tts_queued"):
                score += 0.5
            elif st in ("extracting_audio", "transcribing"):
                score += 0.25
            elif st == "cut":
                score += 0.1
        pct = min(88, int(round((score / total) * 85)) + 5)

    return {
        "progress_percent": pct,
        "total_segments": total,
        "transcribed_segments": sum(1 for s in segments if s.get("status") in ("transcribed", "tts_queued", "tts_processing", "tts_done")),
        "tts_done_segments": sum(1 for s in segments if s.get("status") == "tts_done"),
        "failed_segments": sum(1 for s in segments if s.get("status") in ("failed", "tts_failed")),
    }


def _serialize_job_detail(job_id: str) -> dict:
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    clips = db.list_clips(job_id) if job.get("type") == "video" else []
    segments = tasks.all_segments_for_job(job)
    progress = _compute_job_progress(job, segments)

    video_ready = (
        job.get("finalVideoStatus") == "done"
        and job.get("finalVideoPath")
        and os.path.exists(job["finalVideoPath"])
    )
    has_srt = any(bool(s.get("transcriptCues") or s.get("srtPath")) for s in segments)

    return {
        **job,
        "burnSubtitles": bool(job.get("burnSubtitles")),
        "progress": progress,
        "clips": [
            {
                **clip,
                "segments": [
                    {
                        **seg,
                        "transcriptCues": json.loads(seg["transcriptCues"]) if seg.get("transcriptCues") else [],
                    }
                    for seg in db.get_all_segments(clip["id"])
                ],
            }
            for clip in clips
        ],
        "segments": [
            {
                **seg,
                "transcriptCues": json.loads(seg["transcriptCues"]) if seg.get("transcriptCues") else [],
            }
            for seg in (segments if job.get("type") != "video" else [])
        ],
        "urls": {
            "self": f"/api/v1/jobs/{job_id}",
            "download_video": f"/api/v1/jobs/{job_id}/download/video" if video_ready else None,
            "download_srt": f"/api/v1/jobs/{job_id}/download/srt" if has_srt else None,
        },
    }


# --- Pydantic Schemas ---------------------------------------------------------

class CreateJobRequest(BaseModel):
    title: Optional[str] = "Project mới"
    target_language: Optional[str] = "vi"
    sub_mode: Optional[str] = "target"
    burn_subtitles: Optional[bool] = False
    default_voice_id: Optional[str] = "multi:vi-VN-HoaiMy"
    webhook_url: Optional[str] = None


class StartJobRequest(BaseModel):
    voice_id: Optional[str] = None
    voice_gender: Optional[str] = None
    target_language: Optional[str] = None
    sub_mode: Optional[str] = None
    burn_subtitles: Optional[bool] = None
    webhook_url: Optional[str] = None


class RetranslateRequest(BaseModel):
    target_language: str = "vi"
    sub_mode: str = "target"


class UpdateSegmentRequest(BaseModel):
    edited_text: str


class SegmentTTSRequest(BaseModel):
    voice_id: str
    target_language: Optional[str] = None


class VoicePreviewRequest(BaseModel):
    voice_id: str = "multi:vi-VN-HoaiMy"
    text: Optional[str] = None
    speed: float = 1.0


class TextToSpeechRequest(BaseModel):
    text: str
    voice_id: str = "auto"
    language: str = "vi"
    voice_gender: Optional[str] = None
    translate_text: bool = False
    speed: float = 1.0
    output_format: str = "mp3"  # "mp3" or "wav"
    title: Optional[str] = "TTS Voice Task"
    webhook_url: Optional[str] = None


class CreateApiKeyRequest(BaseModel):
    name: str
    custom_key: Optional[str] = None


class UpdateApiKeyRequest(BaseModel):
    is_active: Optional[bool] = None
    name: Optional[str] = None


class AdminSettingsRequest(BaseModel):
    require_api_key: Optional[bool] = None


class CountryItem(BaseModel):
    code: str = Field(..., description="ISO 2-letter country code (e.g. VN, US, GB, CN, TW, JP, KR, RU, FR, DE, ES, TH)")
    name: str = Field(..., description="Country name in English (e.g. Vietnam, United States)")
    name_vi: str = Field(..., description="Country name in Vietnamese (e.g. Việt Nam, Hoa Kỳ)")
    languages: List[str] = Field(default_factory=list, description="Language codes available in this country")
    genders: List[str] = Field(default_factory=list, description="Available voice genders (female, male)")
    voice_count: int = Field(default=0, description="Number of voices available in this country")


class VoiceItem(BaseModel):
    id: str = Field(..., description="Unique Voice ID (e.g. 'multi:vi-VN-NamMinh', 'multi:en-US-Ava')")
    name: str = Field(..., description="Display name of the voice")
    subtitle: Optional[str] = Field(default=None, description="Short description / style of the voice")
    lang: str = Field(..., description="Language code (vi, en, ru, ja, ko, zh, fr, de, es, th)")
    country: str = Field(..., description="2-letter country code (VN, US, GB, RU, JP, KR, CN, TW, FR, DE, ES, TH)")
    country_name: str = Field(..., description="Country name (Vietnam, United States, ...)")
    locale: str = Field(..., description="Locale code (e.g. vi-VN, en-US, en-GB, zh-CN, zh-TW)")
    gender: str = Field(..., description="Voice gender: 'female', 'male', or 'unknown'")
    type: str = Field(default="multilingual", description="Voice engine type: 'multilingual', 'vieneu_offline', or 'cloned'")
    demo: Optional[str] = Field(default=None, description="Sample preview sentence for this voice")


class VoiceFilterOption(BaseModel):
    code: str
    label: str
    count: int = 0


class VoiceFiltersResponse(BaseModel):
    total_voices: int
    countries: List[CountryItem]
    languages: List[VoiceFilterOption]
    genders: List[VoiceFilterOption]
    voice_types: List[VoiceFilterOption]


class VoicesListResponse(BaseModel):
    total_voices: int
    applied_filters: Dict[str, Optional[str]]
    default_female_voice_by_language: Dict[str, str]
    default_male_voice_by_language: Dict[str, str]
    by_language: Dict[str, List[dict]]
    by_country: Dict[str, List[dict]]
    voices: List[VoiceItem]
    multilingual_voices: List[dict]
    vieneu_preset_voices: List[dict]
    cloned_voices: List[dict]


COUNTRY_NAMES_EN = {
    "AU": "Australia",
    "BR": "Brazil",
    "CA": "Canada",
    "CN": "China",
    "DE": "Germany",
    "ES": "Spain",
    "FR": "France",
    "GB": "United Kingdom",
    "HK": "Hong Kong",
    "IN": "India",
    "IT": "Italy",
    "JP": "Japan",
    "KR": "South Korea",
    "MX": "Mexico",
    "RU": "Russia",
    "SA": "Saudi Arabia",
    "TH": "Thailand",
    "TW": "Taiwan",
    "US": "United States",
    "VN": "Vietnam",
}

COUNTRY_NAMES_VI = {
    "AU": "Úc (Australia)",
    "BR": "Brazil",
    "CA": "Canada",
    "CN": "Trung Quốc (Đại lục)",
    "DE": "Đức (Germany)",
    "ES": "Tây Ban Nha (Spain)",
    "FR": "Pháp (France)",
    "GB": "Vương quốc Anh (UK)",
    "HK": "Hồng Kông",
    "IN": "Ấn Độ (India)",
    "IT": "Ý (Italy)",
    "JP": "Nhật Bản (Japan)",
    "KR": "Hàn Quốc (South Korea)",
    "MX": "Mexico",
    "RU": "Nga (Russia)",
    "SA": "Ả Rập Xê Út",
    "TH": "Thái Lan (Thailand)",
    "TW": "Đài Loan (Taiwan)",
    "US": "Hoa Kỳ (Mỹ)",
    "VN": "Việt Nam",
}

DEFAULT_COUNTRY_BY_LANG = {
    "vi": "VN",
    "en": "US",
    "ru": "RU",
    "ja": "JP",
    "ko": "KR",
    "zh": "CN",
    "fr": "FR",
    "de": "DE",
    "es": "ES",
    "th": "TH",
}


def _extract_voice_country_and_locale(voice_id: str, lang: str = "vi") -> tuple[str, str]:
    """Extracts `(country_code, locale)` from a voice ID like `multi:en-US-Ava` or defaults by `lang`."""
    raw_id = str(voice_id or "").removeprefix("multi:")
    parts = raw_id.split("-")
    if len(parts) >= 2 and len(parts[0]) in (2, 3) and len(parts[1]) == 2:
        country_code = parts[1].upper()
        locale = f"{parts[0].lower()}-{country_code}"
        return country_code, locale
    lang_clean = str(lang or "vi").lower().split("-")[0]
    country_code = DEFAULT_COUNTRY_BY_LANG.get(lang_clean, "VN")
    return country_code, f"{lang_clean}-{country_code}"


def _enrich_voice_dict(v: dict, default_type: str = "multilingual") -> dict:
    lang = str(v.get("lang") or "vi").lower()
    country_code, locale = _extract_voice_country_and_locale(v.get("id", ""), lang)
    raw_gender = str(v.get("gender") or "unknown").lower()
    if raw_gender in ("nam", "m"):
        raw_gender = "male"
    elif raw_gender in ("nu", "nữ", "f"):
        raw_gender = "female"
    v_type = v.get("type") or default_type
    return {
        **v,
        "lang": lang,
        "country": country_code,
        "country_name": COUNTRY_NAMES_EN.get(country_code, country_code),
        "locale": locale,
        "gender": raw_gender if raw_gender in ("male", "female") else "unknown",
        "type": v_type,
        "subtitle": v.get("subtitle") or v.get("name") or "",
        "demo": v.get("demo") or local_tts_service.demo_text_for_voice(v.get("id", "")),
    }


def _collect_all_voices_enriched(include_vieneu_presets: bool = True) -> tuple[list[dict], list[dict], list[dict]]:
    multilingual = [
        _enrich_voice_dict(v, default_type="multilingual")
        for v in local_tts_service.list_multilingual_voices()
    ]

    cloned_map = local_tts_service.load_cloned_voices(config)
    cloned_list = [
        _enrich_voice_dict(
            {
                "id": name,
                "name": f"{name} (Giọng nhân bản)",
                "subtitle": info.get("description") or "Giọng nhân bản tùy chỉnh",
                "lang": "vi",
                "gender": info.get("gender") or "unknown",
                "type": "cloned",
            },
            default_type="cloned",
        )
        for name, info in cloned_map.items()
    ]

    vieneu_presets = []
    if include_vieneu_presets:
        try:
            for desc, vid in local_tts_service.list_preset_voices():
                label = str(desc or vid)
                v_gender = "male" if "nam" in label.lower() else ("female" if "nữ" in label.lower() else "unknown")
                vieneu_presets.append(
                    _enrich_voice_dict(
                        {
                            "id": vid,
                            "name": label,
                            "subtitle": f"🇻🇳 Tiếng Việt (VieNeu Offline) • {label}",
                            "lang": "vi",
                            "gender": v_gender,
                            "type": "vieneu_offline",
                        },
                        default_type="vieneu_offline",
                    )
                )
        except Exception as exc:
            logger.warning("Could not load VieNeu presets: %s", exc)

    return multilingual, vieneu_presets, cloned_list


# --- Web Dashboard & Admin API Key Endpoints ----------------------------------

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def serve_kingvoice_dashboard():
    dashboard_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    with open(dashboard_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/api/v1/admin/keys", tags=["Admin & API Keys"])
def admin_list_api_keys():
    return {
        "require_api_key": bool(getattr(config, "require_api_key", True)),
        "keys": db.list_api_keys(),
    }


@app.post("/api/v1/admin/keys", status_code=201, tags=["Admin & API Keys"])
def admin_create_api_key(req: CreateApiKeyRequest):
    try:
        created = db.create_api_key(name=req.name, custom_key=req.custom_key)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not create API key: {exc}") from exc
    return created


@app.patch("/api/v1/admin/keys/{key_id}", tags=["Admin & API Keys"])
def admin_update_api_key(key_id: str, req: UpdateApiKeyRequest):
    updates = {}
    if req.is_active is not None:
        updates["isActive"] = 1 if req.is_active else 0
    if req.name is not None:
        updates["name"] = req.name.strip()
    updated = db.update_api_key(key_id, **updates)
    if not updated:
        raise HTTPException(status_code=404, detail="API key not found")
    return updated


@app.delete("/api/v1/admin/keys/{key_id}", tags=["Admin & API Keys"])
def admin_delete_api_key(key_id: str):
    db.delete_api_key(key_id)
    return {"status": "deleted", "id": key_id}


@app.post("/api/v1/admin/settings", tags=["Admin & API Keys"])
def admin_update_settings(req: AdminSettingsRequest):
    if req.require_api_key is not None:
        config.require_api_key = bool(req.require_api_key)
        save_config(config)
    return {"status": "ok", "require_api_key": bool(config.require_api_key)}


@app.post("/api/v1/admin/cleanup", tags=["Admin & API Keys"], dependencies=[Depends(verify_api_key)])
def admin_trigger_cleanup(
    retention_hours: Optional[float] = Query(
        default=None,
        description="Delete files & jobs older than this many hours (default: config.file_retention_hours = 2.0)",
    ),
):
    result = cleanup_expired_files_and_jobs(retention_hours=retention_hours)
    return {"status": "ok", **result}


# --- System & Discovery Endpoints ---------------------------------------------

@app.get("/health", tags=["System"])
def health_check():
    return {
        "status": "ok",
        "ffmpeg_available": _is_ffmpeg_available(),
        "redis_connected": worker.redis_connected,
        "queue_backend": worker.queue_backend,
        "queue_size": worker.queue_size,
        "active_tasks": worker.active_tasks,
        "port": getattr(config, "port", 8686),
        "file_retention_hours": getattr(config, "file_retention_hours", 2.0),
        "whisper_model": config.whisper_model,
        "tts_backend": config.tts_backend,
        "storage_dir": config.storage_dir,
    }


@app.get("/api/v1/languages", tags=["Discovery", "Get Voice"], dependencies=[Depends(verify_api_key)])
def list_languages():
    return {
        "target_languages": [
            {"code": code, "label": label} for label, code in translation_service.TARGET_LANGUAGES
        ],
        "subtitle_modes": [
            {"code": code, "label": label} for label, code in translation_service.SUBTITLE_MODES
        ],
        "source_languages": [
            {"code": code, "label": label}
            for label, code in translation_service.COMMON_SOURCE_LANGUAGES
            if code
        ],
    }


def _build_countries_list(
    lang: Optional[str] = None,
    gender: Optional[str] = None,
    search: Optional[str] = None,
    include_vieneu_presets: bool = True,
) -> List[dict]:
    multilingual, vieneu_presets, cloned_list = _collect_all_voices_enriched(
        include_vieneu_presets=include_vieneu_presets
    )
    all_voices = multilingual + vieneu_presets + cloned_list

    lang_norm = (lang or "").strip().lower()
    gender_norm = (gender or "").strip().lower()
    if gender_norm in ("nam", "m"):
        gender_norm = "male"
    elif gender_norm in ("nu", "nữ", "f"):
        gender_norm = "female"
    q = (search or "").strip().lower()

    countries: Dict[str, dict] = {}
    for voice in all_voices:
        if lang_norm and lang_norm != "all" and voice["lang"] != lang_norm:
            continue
        if gender_norm and gender_norm in ("male", "female") and voice["gender"] != gender_norm:
            continue

        country_code = voice["country"]
        entry = countries.setdefault(
            country_code,
            {
                "code": country_code,
                "name": COUNTRY_NAMES_EN.get(country_code, country_code),
                "name_vi": COUNTRY_NAMES_VI.get(country_code, COUNTRY_NAMES_EN.get(country_code, country_code)),
                "languages": [],
                "genders": [],
                "voice_count": 0,
            },
        )
        entry["voice_count"] += 1
        if voice["lang"] and voice["lang"] not in entry["languages"]:
            entry["languages"].append(voice["lang"])
        if voice["gender"] in ("male", "female") and voice["gender"] not in entry["genders"]:
            entry["genders"].append(voice["gender"])

    result = list(countries.values())
    if q:
        result = [
            c
            for c in result
            if q in c["code"].lower()
            or q in c["name"].lower()
            or q in c["name_vi"].lower()
            or any(q in lg for lg in c["languages"])
        ]
    return sorted(result, key=lambda c: c["name"])


@app.get(
    "/api/v1/countries",
    response_model=List[CountryItem],
    status_code=200,
    tags=["Get Voice", "Voices"],
    summary="Lấy danh sách quốc gia có giọng đọc AI (Hỗ trợ bộ lọc ngôn ngữ & giới tính)",
)
@app.get("/api/v1/contry", response_model=List[CountryItem], include_in_schema=False)
def get_all_countries(
    lang: Optional[str] = Query(
        default=None,
        description="Filter countries by language code: vi, en, ru, ja, ko, zh, fr, de, es, th (or 'all')",
    ),
    gender: Optional[str] = Query(
        default=None,
        description="Filter countries by available voice gender: male or female (or 'all')",
    ),
    search: Optional[str] = Query(
        default=None,
        description="Search country by ISO code (VN, US, GB, RU...) or country name",
    ),
    include_vieneu_presets: bool = Query(
        default=True,
        description="Count 25 offline VieNeu Vietnamese voices under Vietnam (VN)",
    ),
    api_key_name: str = Depends(verify_api_key),
):
    """Trả về danh sách các quốc gia (`VN`, `US`, `GB`, `CN`, `TW`, `JP`, `KR`, `RU`, `FR`, `DE`, `ES`, `TH`...)
    kèm danh sách mã ngôn ngữ (`languages`), giới tính (`genders`) và tổng số giọng đọc (`voice_count`) của từng quốc gia.
    """
    return _build_countries_list(
        lang=lang,
        gender=gender,
        search=search,
        include_vieneu_presets=include_vieneu_presets,
    )


@app.get(
    "/api/v1/voices/filters",
    response_model=VoiceFiltersResponse,
    tags=["Get Voice", "Voices"],
    summary="Lấy toàn bộ tùy chọn Bộ lọc Giọng đọc (Quốc gia, Ngôn ngữ, Giới tính, Loại giọng)",
    dependencies=[Depends(verify_api_key)],
)
def get_voice_filter_options(
    include_vieneu_presets: bool = Query(
        default=True,
        description="Include 25 offline VieNeu Vietnamese voices in filter counts",
    ),
):
    """Endpoint phục vụ UI / Client khởi tạo các dropdown bộ lọc trong 1 lần gọi:
    trả về danh sách `countries`, `languages`, `genders`, và `voice_types` kèm số lượng giọng đọc tương ứng.
    """
    multilingual, vieneu_presets, cloned_list = _collect_all_voices_enriched(
        include_vieneu_presets=include_vieneu_presets
    )
    all_voices = multilingual + vieneu_presets + cloned_list

    lang_counts: Dict[str, int] = {}
    gender_counts: Dict[str, int] = {"female": 0, "male": 0}
    type_counts: Dict[str, int] = {
        "multilingual": len(multilingual),
        "vieneu_offline": len(vieneu_presets),
        "cloned": len(cloned_list),
    }

    for v in all_voices:
        lg = v.get("lang") or "vi"
        lang_counts[lg] = lang_counts.get(lg, 0) + 1
        gd = v.get("gender") or "unknown"
        if gd in gender_counts:
            gender_counts[gd] += 1

    lang_labels = {code: label for label, code in translation_service.TARGET_LANGUAGES}
    languages = [
        {
            "code": code,
            "label": lang_labels.get(code, code.upper()),
            "count": count,
        }
        for code, count in sorted(lang_counts.items(), key=lambda x: (-x[1], x[0]))
    ]

    genders = [
        {"code": "female", "label": "Nữ (Female)", "count": gender_counts.get("female", 0)},
        {"code": "male", "label": "Nam (Male)", "count": gender_counts.get("male", 0)},
    ]

    voice_types = [
        {"code": "multilingual", "label": "Neural Đa ngôn ngữ (Multilingual)", "count": type_counts["multilingual"]},
        {"code": "vieneu_offline", "label": "VieNeu Offline (Tiếng Việt)", "count": type_counts["vieneu_offline"]},
        {"code": "cloned", "label": "Giọng nhân bản (Cloned)", "count": type_counts["cloned"]},
    ]

    return {
        "total_voices": len(all_voices),
        "countries": _build_countries_list(include_vieneu_presets=include_vieneu_presets),
        "languages": languages,
        "genders": genders,
        "voice_types": voice_types,
    }


@app.get(
    "/api/v1/voices",
    response_model=VoicesListResponse,
    tags=["Voices", "Get Voice"],
    summary="Danh sách & Bộ lọc Giọng đọc AI (Lọc theo Quốc gia, Ngôn ngữ, Giới tính, Loại giọng, Từ khóa)",
    dependencies=[Depends(verify_api_key)],
)
def list_voices(
    country: Optional[str] = Query(
        default=None,
        description="Filter by 2-letter country code: VN, US, GB, CN, TW, JP, KR, RU, FR, DE, ES, TH (or 'all')",
    ),
    lang: Optional[str] = Query(
        default=None,
        description="Filter by language code: vi, en, ru, ja, ko, zh, fr, de, es, th (or 'all')",
    ),
    gender: Optional[str] = Query(
        default=None,
        description="Filter by gender: male, female (or 'all')",
    ),
    voice_type: Optional[str] = Query(
        default=None,
        alias="type",
        description="Filter by voice engine type: multilingual, vieneu_offline, cloned (or 'all')",
    ),
    search: Optional[str] = Query(
        default=None,
        description="Search keyword matching voice ID, name, description/subtitle, country, or locale",
    ),
    include_vieneu_presets: bool = Query(
        default=True,
        description="Include 25 offline VieNeu Vietnamese voices",
    ),
):
    """Lấy danh sách giọng đọc AI hỗ trợ bộ lọc kết hợp:
    - `country`: Mã quốc gia 2 ký tự (`VN`, `US`, `GB`, `CN`, `TW`, `JP`, `KR`, `RU`, `FR`, `DE`, `ES`, `TH`)
    - `lang`: Mã ngôn ngữ (`vi`, `en`, `ru`, `ja`, `ko`, `zh`, `fr`, `de`, `es`, `th`)
    - `gender`: Giới tính (`male` / `female`)
    - `type`: Loại giọng (`multilingual`, `vieneu_offline`, `cloned`)
    - `search`: Từ khóa tìm theo tên giọng, mã `voice_id`, mô tả phong cách đọc
    """
    multilingual, vieneu_presets, cloned_list = _collect_all_voices_enriched(
        include_vieneu_presets=include_vieneu_presets
    )

    country_norm = (country or "").strip().upper()
    lang_norm = (lang or "").strip().lower()
    gender_norm = (gender or "").strip().lower()
    if gender_norm in ("nam", "m"):
        gender_norm = "male"
    elif gender_norm in ("nu", "nữ", "f"):
        gender_norm = "female"
    type_norm = (voice_type or "").strip().lower()
    q = (search or "").strip().lower()

    def _matches(v: dict) -> bool:
        if country_norm and country_norm != "ALL" and v.get("country") != country_norm:
            return False
        if lang_norm and lang_norm != "all" and v.get("lang") != lang_norm:
            return False
        if gender_norm and gender_norm in ("male", "female") and v.get("gender") != gender_norm:
            return False
        if type_norm and type_norm != "all" and v.get("type") != type_norm:
            return False
        if q:
            haystack = " ".join(
                str(v.get(k) or "")
                for k in ("id", "name", "subtitle", "lang", "country", "country_name", "locale", "gender", "type")
            ).lower()
            if q not in haystack:
                return False
        return True

    multilingual = [v for v in multilingual if _matches(v)]
    vieneu_presets = [v for v in vieneu_presets if _matches(v)]
    cloned_list = [v for v in cloned_list if _matches(v)]
    combined_voices = multilingual + vieneu_presets + cloned_list

    by_language: Dict[str, List[dict]] = {}
    by_country: Dict[str, List[dict]] = {}
    for v in combined_voices:
        by_language.setdefault(v.get("lang", "other"), []).append(v)
        by_country.setdefault(v.get("country", "OTHER"), []).append(v)

    return {
        "total_voices": len(combined_voices),
        "applied_filters": {
            "country": country_norm or None,
            "lang": lang_norm or None,
            "gender": gender_norm or None,
            "type": type_norm or None,
            "search": search.strip() if search else None,
        },
        "default_female_voice_by_language": tasks.DEFAULT_VOICE_BY_LANG,
        "default_male_voice_by_language": tasks.DEFAULT_MALE_VOICE_BY_LANG,
        "by_language": by_language,
        "by_country": by_country,
        "voices": combined_voices,
        "multilingual_voices": multilingual,
        "vieneu_preset_voices": vieneu_presets,
        "cloned_voices": cloned_list,
    }


@app.post("/api/v1/voices/clone", tags=["Voices"], dependencies=[Depends(verify_api_key)])
def clone_custom_voice(
    name: str = Form(..., description="Unique name for the cloned voice"),
    description: str = Form(default=""),
    gender: str = Form(default=""),
    file: UploadFile = File(..., description="3-8 second clean reference audio sample (.wav/.mp3)"),
):
    with tempfile.TemporaryDirectory(prefix="maxvoice_clone_") as tmp_dir:
        sample_path = _save_upload_file(file, tmp_dir, prefix="ref_sample")
        try:
            voice_name = local_tts_service.clone_voice(
                config, name.strip(), sample_path, description=description, gender=gender
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "voice_id": voice_name, "name": voice_name}


@app.post("/api/v1/voices/preview", tags=["Voices"], dependencies=[Depends(verify_api_key)])
def preview_voice(req: VoicePreviewRequest, background_tasks: BackgroundTasks):
    demo_text = (req.text or "").strip() or local_tts_service.demo_text_for_voice(req.voice_id)
    tmp = tempfile.NamedTemporaryFile(prefix="maxvoice_preview_", suffix=".wav", delete=False)
    tmp.close()
    try:
        local_tts_service.synthesize_to_file(
            demo_text, tmp.name, voice=req.voice_id, speed=req.speed, ffmpeg=config.ffmpeg_path
        )
    except Exception as exc:
        _safe_unlink(tmp.name)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    background_tasks.add_task(_safe_unlink, tmp.name)
    return FileResponse(tmp.name, media_type="audio/wav", filename="preview.wav")


# --- Text-to-Speech (Truyền Text -> Trả về Voice MP3/WAV) ---------------------

@app.post("/api/v1/tts", tags=["Text to Speech (Voice)"], dependencies=[Depends(verify_api_key)])
def synthesize_text_to_voice_sync(req: TextToSpeechRequest, background_tasks: BackgroundTasks):
    """Truyền `text` vào -> Tạo và trả về trực tiếp file âm thanh (`.mp3` hoặc `.wav`).
    Hỗ trợ tự động chia đoạn văn bản dài, tùy chọn dịch sang ngôn ngữ đích (`translate_text=true`),
    và chọn bất kỳ giọng nào trong 84+ giọng (Việt, Nga, Anh, Nhật, Hàn, Trung...).
    """
    raw_text = (req.text or "").strip()
    if not raw_text:
        raise HTTPException(status_code=400, detail="Field `text` cannot be empty")

    resolved_voice = tasks.resolve_default_voice(req.voice_id, req.language, gender=req.voice_gender)
    spoken_text = raw_text
    if req.translate_text and req.language and req.language != "original":
        try:
            tx = translation_service.translate_texts([raw_text], "auto", req.language)
            if tx and tx[0].strip():
                spoken_text = tx[0].strip()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Translation failed: {exc}") from exc

    ext = "mp3" if str(req.output_format or "mp3").lower().strip(".") == "mp3" else "wav"
    tmp = tempfile.NamedTemporaryFile(prefix="kingvoice_tts_", suffix=f".{ext}", delete=False)
    tmp.close()
    try:
        tasks.synthesize_long_text_to_file(
            config,
            text=spoken_text,
            voice_id=resolved_voice,
            output_path=tmp.name,
            speed=req.speed,
            output_format=ext,
        )
    except Exception as exc:
        _safe_unlink(tmp.name)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    background_tasks.add_task(_safe_unlink, tmp.name)
    media_type = "audio/mpeg" if ext == "mp3" else "audio/wav"
    return FileResponse(tmp.name, media_type=media_type, filename=f"kingvoice-tts.{ext}")

@app.post("/api/v1/tts/jobs", status_code=202, tags=["Text to Speech (Voice)"])
def create_tts_task_async(
    req: TextToSpeechRequest,
    api_key_name: str = Depends(verify_api_key),
):
    """Truyền `text` vào -> Đưa vào Hàng đợi Task KingVoice (dành cho kịch bản/văn bản dài,
    có lưu lịch sử trên Dashboard, theo dõi tiến độ % và bắn Webhook khi tạo xong file Voice).
    """
    raw_text = (req.text or "").strip()
    if not raw_text:
        raise HTTPException(status_code=400, detail="Field `text` cannot be empty")

    resolved_voice = tasks.resolve_default_voice(req.voice_id, req.language, gender=req.voice_gender)
    ext = "mp3" if str(req.output_format or "mp3").lower().strip(".") == "mp3" else "wav"

    job_id = str(uuid.uuid4())
    db.create_job(
        id=job_id,
        type="tts",
        originalFilename=req.title or f"TTS: {raw_text[:36]}...",
        stage="queued",
        segmentCount=1,
        defaultVoiceId=resolved_voice,
        defaultVoiceName=resolved_voice,
        targetLanguage=req.language or "vi",
        subMode="target",
        webhookUrl=req.webhook_url,
        apiKeyName=api_key_name,
    )
    db.create_segment(
        job_id, 0,
        kind="tts-standalone",
        status="tts_queued",
        transcriptText=raw_text,
        ttsVoiceId=resolved_voice,
    )

    worker.submit(
        tasks.handle_tts_standalone_job,
        config,
        job_id,
        raw_text,
        resolved_voice,
        req.language or "vi",
        req.translate_text,
        req.speed,
        ext,
    )

    return {
        "status": "queued",
        "job_id": job_id,
        "type": "tts",
        "voice_id": resolved_voice,
        "output_format": ext,
        "urls": {
            "status": f"/api/v1/jobs/{job_id}",
            "download_audio": f"/api/v1/jobs/{job_id}/download/audio",
        },
    }


# --- One-Shot Video Dubbing & Subtitling Endpoint -----------------------------

@app.post("/api/v1/videos/generate", status_code=202, tags=["Video Pipeline"], dependencies=[Depends(verify_api_key)])
def generate_video_oneshot(
    files: Optional[List[UploadFile]] = File(default=None, description="1 or more video files to dub/merge"),
    video_urls: Optional[str] = Form(default=None, description="Comma/newline separated list or JSON array of video URLs"),
    music_file: Optional[UploadFile] = File(default=None, description="Optional background music file"),
    music_url: Optional[str] = Form(default=None, description="Optional background music URL"),
    title: str = Form(default="API Video Project"),
    voice_id: str = Form(default="auto", description="Voice ID (e.g. 'multi:ru-RU-Dmitry', 'multi:en-US-Ava', 'multi:ja-JP-Nanami', 'multi:ko-KR-SunHi', 'auto')"),
    voice_gender: Optional[str] = Form(default=None, description="Optional 'female' or 'male' when voice_id='auto'"),
    target_language: str = Form(default="vi", description="Target language code ('vi', 'en', 'ru', 'ja', 'ko', 'zh', 'original'...)"),
    sub_mode: str = Form(default="target", description="Subtitle mode: 'target', 'bilingual', or 'original'"),
    burn_subtitles: bool = Form(default=False, description="Burn hardsub directly into output MP4"),
    webhook_url: Optional[str] = Form(default=None, description="Optional URL to receive HTTP POST when job completes"),
    api_key_name: str = Depends(verify_api_key),
):
    """One-shot endpoint for external systems:
    Uploads or downloads video clip(s), creates a project, and immediately queues
    the full end-to-end pipeline: Cut -> Transcribe -> Translate -> Neural TTS -> Merge (+ Burn Subtitles).
    """
    urls = _parse_url_list(video_urls)
    valid_files = [f for f in (files or []) if f and f.filename]
    if not valid_files and not urls:
        raise HTTPException(
            status_code=400,
            detail="Must provide at least one video file in `files` or URL in `video_urls`.",
        )

    resolved_voice = tasks.resolve_default_voice(voice_id, target_language, gender=voice_gender)
    if local_tts_service.is_multilingual_voice(resolved_voice) and target_language == "vi":
        v_lang = local_tts_service.voice_language(resolved_voice, default=target_language)
        if v_lang != "vi":
            target_language = v_lang

    project_id = str(uuid.uuid4())
    db.create_job(
        id=project_id,
        type="video",
        originalFilename=title or "API Video Project",
        stage="queued",
        defaultVoiceId=resolved_voice,
        defaultVoiceName=resolved_voice,
        targetLanguage=target_language,
        subMode=sub_mode,
        burnSubtitles=1 if burn_subtitles else 0,
        webhookUrl=webhook_url,
        apiKeyName=api_key_name,
    )

    # 1. Save uploaded video files as clips
    for upload in valid_files:
        clip_id = str(uuid.uuid4())
        dest_dir = job_upload_dir(config, clip_id)
        dest_path = _save_upload_file(upload, dest_dir, prefix="original")
        order_index = db.next_clip_order_index(project_id)
        db.create_clip(
            id=clip_id,
            jobId=project_id,
            orderIndex=order_index,
            originalFilename=upload.filename or f"clip-{order_index + 1}.mp4",
            filePath=dest_path,
            stage="queued",
        )

    # 2. Download any remote video URLs as clips
    for url in urls:
        clip_id = str(uuid.uuid4())
        dest_dir = job_upload_dir(config, clip_id)
        try:
            dest_path, base_name = _download_url_to_dir(url, dest_dir, prefix="original", default_ext=".mp4")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to download video URL {url}: {exc}") from exc
        order_index = db.next_clip_order_index(project_id)
        db.create_clip(
            id=clip_id,
            jobId=project_id,
            orderIndex=order_index,
            originalFilename=base_name,
            filePath=dest_path,
            stage="queued",
        )

    # 3. Optional background music
    if music_file and music_file.filename:
        m_dir = music_dir(config, project_id)
        m_path = _save_upload_file(music_file, m_dir, prefix="background_music")
        db.update_job(project_id, backgroundMusicPath=m_path, backgroundMusicName=music_file.filename)
    elif music_url and music_url.strip():
        m_dir = music_dir(config, project_id)
        try:
            m_path, m_name = _download_url_to_dir(music_url.strip(), m_dir, prefix="background_music", default_ext=".mp3")
            db.update_job(project_id, backgroundMusicPath=m_path, backgroundMusicName=m_name)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to download music_url: {exc}") from exc

    # 4. Submit full pipeline to worker queue
    worker.submit(tasks.handle_start_project, config, project_id, resolved_voice, target_language)

    return {
        "status": "queued",
        "job_id": project_id,
        "voice_id": resolved_voice,
        "target_language": target_language,
        "sub_mode": sub_mode,
        "burn_subtitles": burn_subtitles,
        "urls": {
            "status": f"/api/v1/jobs/{project_id}",
            "download_video": f"/api/v1/jobs/{project_id}/download/video",
            "download_srt": f"/api/v1/jobs/{project_id}/download/srt",
        },
    }


# --- One-Shot Audio/Video Transcription & Speech-to-Voice Dubbing ------------

@app.post("/api/v1/audio/dub", status_code=202, tags=["Voice to Voice (Speech -> New Voice)"])
def dub_audio_to_voice_oneshot(
    file: Optional[UploadFile] = File(default=None, description="Audio or video file to recognize speech from"),
    media_url: Optional[str] = Form(default=None, description="Or URL to audio/video file"),
    voice_id: str = Form(default="auto", description="New AI Voice ID (e.g. 'multi:vi-VN-NamMinh', 'multi:ru-RU-Dmitry', 'auto')"),
    voice_gender: Optional[str] = Form(default=None, description="'female' or 'male' when voice_id='auto'"),
    target_language: str = Form(default="vi", description="Target language ('vi', 'ru', 'en', 'ja', 'ko', 'zh', 'original'...)"),
    sub_mode: str = Form(default="target", description="'target', 'bilingual', or 'original'"),
    webhook_url: Optional[str] = Form(default=None, description="Optional Webhook URL"),
    api_key_name: str = Depends(verify_api_key),
):
    print("===== DUB AUDIO REQUEST =====", flush=True)

    if file:
        print("File name:", file.filename, flush=True)
        print("Content type:", file.content_type, flush=True)
        print("File size:", file.size, flush=True)
    else:
        print("File: None", flush=True)

    print("Media URL:", media_url, flush=True)
    print("Voice ID:", voice_id, flush=True)
    print("Voice gender:", voice_gender, flush=True)
    print("Target language:", target_language, flush=True)
    print("Subtitle mode:", sub_mode, flush=True)
    print("Webhook URL:", webhook_url, flush=True)
    """Tự động nhận diện giọng nói (Faster-Whisper) từ file Audio/Video đầu vào ->
    Dịch sang ngôn ngữ đích -> Tạo ra file Giọng nói AI mới (`voice_id`) + file Phụ đề `.srt`.
    """
    if (not file or not file.filename) and not (media_url and media_url.strip()):
        raise HTTPException(status_code=400, detail="Provide `file` or `media_url`")

    resolved_voice = tasks.resolve_default_voice(voice_id, target_language, gender=voice_gender)
    job_id = str(uuid.uuid4())
    dest_dir = job_upload_dir(config, job_id)
    if file and file.filename:
        dest_path = _save_upload_file(file, dest_dir, prefix="original")
        orig_name = file.filename
    else:
        try:
            dest_path, orig_name = _download_url_to_dir(media_url.strip(), dest_dir, prefix="original")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to download media_url: {exc}") from exc

    db.create_job(
        id=job_id,
        type="audio",
        originalFilename=orig_name,
        filePath=dest_path,
        stage="queued",
        segmentCount=1,
        defaultVoiceId=resolved_voice,
        defaultVoiceName=resolved_voice,
        targetLanguage=target_language,
        subMode=sub_mode,
        webhookUrl=webhook_url,
        apiKeyName=api_key_name,
    )
    db.create_segment(job_id, 0, kind="audio-standalone", sourcePath=dest_path, status="cut")
    worker.submit(
        tasks.handle_audio_dub_job, config, job_id, resolved_voice, target_language, sub_mode
    )

    return {
        "status": "queued",
        "job_id": job_id,
        "voice_id": resolved_voice,
        "target_language": target_language,
        "urls": {
            "status": f"/api/v1/jobs/{job_id}",
            "download_voice": f"/api/v1/jobs/{job_id}/download/voice",
            "download_srt": f"/api/v1/jobs/{job_id}/download/srt",
        },
    }


@app.post("/api/v1/audio/transcribe", status_code=202, tags=["Voice to Text"], dependencies=[Depends(verify_api_key)])
def transcribe_media_oneshot(
    file: Optional[UploadFile] = File(default=None, description="Audio or video file to transcribe"),
    media_url: Optional[str] = Form(default=None, description="Or URL to audio/video file"),
    target_language: str = Form(default="vi", description="Target language ('vi', 'en', 'original'...)"),
    sub_mode: str = Form(default="target", description="'target', 'bilingual', or 'original'"),
):
    if (not file or not file.filename) and not (media_url and media_url.strip()):
        raise HTTPException(status_code=400, detail="Provide `file` or `media_url`")

    job_id = str(uuid.uuid4())
    dest_dir = job_upload_dir(config, job_id)
    if file and file.filename:
        dest_path = _save_upload_file(file, dest_dir, prefix="original")
        orig_name = file.filename
    else:
        try:
            dest_path, orig_name = _download_url_to_dir(media_url.strip(), dest_dir, prefix="original")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to download media_url: {exc}") from exc

    db.create_job(
        id=job_id,
        type="audio",
        originalFilename=orig_name,
        filePath=dest_path,
        stage="ready",
        segmentCount=1,
        targetLanguage=target_language,
        subMode=sub_mode,
    )
    db.create_segment(job_id, 0, kind="audio-standalone", sourcePath=dest_path, status="cut")
    worker.submit(tasks.handle_transcribe_segment, config, job_id, 0, target_language, sub_mode)

    return {
        "status": "queued",
        "job_id": job_id,
        "urls": {
            "status": f"/api/v1/jobs/{job_id}",
            "download_srt": f"/api/v1/jobs/{job_id}/download/srt",
        },
    }


# --- Step-by-Step Job & Clip Management Endpoints -----------------------------

@app.get("/api/v1/jobs", tags=["Jobs"], dependencies=[Depends(verify_api_key)])
def list_all_jobs(limit: int = Query(default=50, ge=1, le=200)):
    jobs = db.list_jobs(limit=limit)
    out = []
    for job in jobs:
        segments = tasks.all_segments_for_job(job)
        out.append({
            **job,
            "burnSubtitles": bool(job.get("burnSubtitles")),
            "progress": _compute_job_progress(job, segments),
        })
    return {"jobs": out}


@app.post("/api/v1/jobs", status_code=201, tags=["Jobs"], dependencies=[Depends(verify_api_key)])
def create_project(req: CreateJobRequest):
    project_id = str(uuid.uuid4())
    db.create_job(
        id=project_id,
        type="video",
        originalFilename=req.title or "Project mới",
        stage="queued",
        defaultVoiceId=req.default_voice_id,
        defaultVoiceName=req.default_voice_id,
        targetLanguage=req.target_language or "vi",
        subMode=req.sub_mode or "target",
        burnSubtitles=1 if req.burn_subtitles else 0,
        webhookUrl=req.webhook_url,
    )
    return _serialize_job_detail(project_id)


@app.get("/api/v1/jobs/{job_id}", tags=["Jobs"], dependencies=[Depends(verify_api_key)])
def get_job_status(job_id: str):
    return _serialize_job_detail(job_id)


@app.delete("/api/v1/jobs/{job_id}", tags=["Jobs"], dependencies=[Depends(verify_api_key)])
def delete_job_endpoint(job_id: str):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    clip_ids = [clip["id"] for clip in db.list_clips(job_id)]
    db.delete_job(job_id)
    for owner_id in [job_id, *clip_ids]:
        for root in (job_upload_dir(config, owner_id), job_output_dir(config, owner_id)):
            shutil.rmtree(root, ignore_errors=True)
    return {"status": "deleted", "job_id": job_id}


@app.post("/api/v1/jobs/{job_id}/clips", tags=["Jobs"], dependencies=[Depends(verify_api_key)])
def add_clips_to_job(
    job_id: str,
    files: List[UploadFile] = File(...),
    auto_cut_transcribe: bool = Form(default=True),
):
    job = db.get_job(job_id)
    if not job or job.get("type") != "video":
        raise HTTPException(status_code=404, detail="Video project not found")

    created_clips = []
    for upload in files:
        clip_id = str(uuid.uuid4())
        dest_dir = job_upload_dir(config, clip_id)
        dest_path = _save_upload_file(upload, dest_dir, prefix="original")
        order_index = db.next_clip_order_index(job_id)
        clip = db.create_clip(
            id=clip_id,
            jobId=job_id,
            orderIndex=order_index,
            originalFilename=upload.filename or f"clip-{order_index + 1}.mp4",
            filePath=dest_path,
            stage="queued",
        )
        created_clips.append(clip)
        if auto_cut_transcribe:
            worker.submit(tasks.handle_cut_clip, config, clip_id, dest_path)

    return {"status": "ok", "added_clips": created_clips}


@app.post("/api/v1/jobs/{job_id}/music", tags=["Jobs"], dependencies=[Depends(verify_api_key)])
def set_job_music(job_id: str, file: UploadFile = File(...)):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    m_dir = music_dir(config, job_id)
    m_path = _save_upload_file(file, m_dir, prefix="background_music")
    db.update_job(job_id, backgroundMusicPath=m_path, backgroundMusicName=file.filename or "music")
    return {"status": "ok", "backgroundMusicName": file.filename}


@app.post("/api/v1/jobs/{job_id}/start", status_code=202, tags=["Jobs"], dependencies=[Depends(verify_api_key)])
def start_job_pipeline(job_id: str, req: StartJobRequest):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not db.list_clips(job_id):
        raise HTTPException(status_code=400, detail="Project has no video clips yet")

    target_lang = req.target_language or job.get("targetLanguage") or "vi"
    sub_mode = req.sub_mode or job.get("subMode") or "target"
    voice_id = tasks.resolve_default_voice(
        req.voice_id or job.get("defaultVoiceId"), target_lang, gender=req.voice_gender
    )

    updates = {
        "targetLanguage": target_lang,
        "subMode": sub_mode,
        "defaultVoiceId": voice_id,
    }
    if req.burn_subtitles is not None:
        updates["burnSubtitles"] = 1 if req.burn_subtitles else 0
    if req.webhook_url is not None:
        updates["webhookUrl"] = req.webhook_url
    db.update_job(job_id, **updates)

    worker.submit(tasks.handle_start_project, config, job_id, voice_id, target_lang)
    return {"status": "queued", "job_id": job_id, "voice_id": voice_id, "target_language": target_lang}


@app.post("/api/v1/jobs/{job_id}/retranslate", status_code=202, tags=["Jobs"], dependencies=[Depends(verify_api_key)])
def retranslate_job(job_id: str, req: RetranslateRequest):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    worker.submit(tasks.handle_retranslate_project, config, job_id, req.target_language, req.sub_mode)
    return {"status": "queued", "job_id": job_id, "target_language": req.target_language, "sub_mode": req.sub_mode}


@app.post("/api/v1/jobs/{job_id}/merge", status_code=202, tags=["Jobs"], dependencies=[Depends(verify_api_key)])
def merge_job_video(job_id: str, burn_subtitles: Optional[bool] = Query(default=None)):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if burn_subtitles is not None:
        db.update_job(job_id, burnSubtitles=1 if burn_subtitles else 0)
    music_path = job.get("backgroundMusicPath")
    worker.submit(tasks.handle_merge_project, config, job_id, music_path)
    return {"status": "queued", "job_id": job_id}


@app.put("/api/v1/clips/{clip_id}/segments/{index}", tags=["Segments"], dependencies=[Depends(verify_api_key)])
def update_segment_transcript(clip_id: str, index: int, req: UpdateSegmentRequest):
    seg = db.get_segment(clip_id, index)
    if not seg:
        raise HTTPException(status_code=404, detail="Segment not found")
    db.update_segment(clip_id, index, editedText=req.edited_text, transcriptText=req.edited_text)
    return db.get_segment(clip_id, index)


@app.post("/api/v1/clips/{clip_id}/segments/{index}/tts", status_code=202, tags=["Segments"], dependencies=[Depends(verify_api_key)])
def synthesize_segment_voice(clip_id: str, index: int, req: SegmentTTSRequest):
    seg = db.get_segment(clip_id, index)
    if not seg:
        raise HTTPException(status_code=404, detail="Segment not found")
    db.update_segment(clip_id, index, status="tts_queued", ttsVoiceId=req.voice_id, error="")
    worker.submit(tasks.handle_tts_segment, config, clip_id, index, req.voice_id, req.target_language)
    return {"status": "queued", "clip_id": clip_id, "index": index, "voice_id": req.voice_id}


# --- Download Endpoints (Final MP4 Video & SRT Subtitles) ---------------------

@app.get("/api/v1/jobs/{job_id}/download/video", tags=["Downloads"], dependencies=[Depends(verify_api_key)])
@app.get("/api/v1/jobs/{job_id}/download/audio", tags=["Downloads"], dependencies=[Depends(verify_api_key)])
def download_final_video_or_audio(job_id: str):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    final_path = job.get("finalVideoPath")
    if job.get("finalVideoStatus") != "done" or not final_path or not os.path.exists(final_path):
        raise HTTPException(
            status_code=409,
            detail=f"Output file is not ready yet (stage={job.get('stage')}, finalVideoStatus={job.get('finalVideoStatus')})",
        )
    safe_name = os.path.splitext(job.get("originalFilename") or job_id)[0]
    ext = os.path.splitext(final_path)[1].lower()
    if ext == ".mp3":
        return FileResponse(final_path, media_type="audio/mpeg", filename=f"{safe_name}.mp3")
    if ext == ".wav":
        return FileResponse(final_path, media_type="audio/wav", filename=f"{safe_name}.wav")
    return FileResponse(
        final_path,
        media_type="video/mp4",
        filename=f"{safe_name}-dubbed.mp4",
    )


@app.get("/api/v1/jobs/{job_id}/download/voice", tags=["Downloads"], dependencies=[Depends(verify_api_key)])
def download_voice_only_track(job_id: str):
    """Tải riêng file âm thanh giọng đọc AI (`.wav` / `.mp3`) đã tạo của một Job (kể cả Job Video hoặc Audio)."""
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    safe_name = os.path.splitext(job.get("originalFilename") or job_id)[0]

    # 1. Check if a combined `final-voice.wav` exists in output dir (for video projects)
    combined_voice = os.path.join(job_output_dir(config, job_id), "final-voice.wav")
    if os.path.exists(combined_voice):
        return FileResponse(combined_voice, media_type="audio/wav", filename=f"{safe_name}-voice.wav")

    # 2. Check job finalVideoPath if it's already an audio file (for tts / audio dub jobs)
    final_path = job.get("finalVideoPath")
    if final_path and os.path.exists(final_path) and final_path.lower().endswith((".wav", ".mp3")):
        ext = os.path.splitext(final_path)[1].lower()
        media = "audio/mpeg" if ext == ".mp3" else "audio/wav"
        return FileResponse(final_path, media_type=media, filename=f"{safe_name}-voice{ext}")

    # 3. Check segment 0 ttsAudioPath
    segs = tasks.all_segments_for_job(job)
    if segs and segs[0].get("ttsAudioPath") and os.path.exists(segs[0]["ttsAudioPath"]):
        return FileResponse(segs[0]["ttsAudioPath"], media_type="audio/wav", filename=f"{safe_name}-voice.wav")

    raise HTTPException(status_code=409, detail="Voice audio file is not ready yet for this job")


@app.get("/api/v1/jobs/{job_id}/download/srt", tags=["Downloads"], dependencies=[Depends(verify_api_key)])
def download_project_srt(
    job_id: str,
    mode: Optional[str] = Query(default=None, description="'target', 'bilingual', or 'original'"),
    target_language: Optional[str] = Query(default=None, description="Optional language code to translate SRT on the fly"),
):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    srt_content = tasks.build_project_srt(
        config, job_id, mode=mode, target_language=target_language
    )
    if not srt_content.strip():
        raise HTTPException(status_code=409, detail="Subtitles are not ready yet for this job")
    safe_name = os.path.splitext(job.get("originalFilename") or job_id)[0]
    resolved_mode = mode or job.get("subMode") or "target"
    return PlainTextResponse(
        content=srt_content,
        media_type="application/x-subrip; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}-{resolved_mode}.srt"'
        },
    )
