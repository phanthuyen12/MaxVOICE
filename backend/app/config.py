import json
import os
from pathlib import Path

APP_DIR = Path(os.environ.get("MAXVOICE_DATA_DIR", str(Path.home() / ".voicetext_studio")))
CONFIG_PATH = APP_DIR / "config.json"

DEFAULTS = {
    "tts_backend": os.environ.get("MAXVOICE_TTS_BACKEND", "vieneu"),  # "vieneu" or "resona"
    "resona_api_base": os.environ.get("RESONA_API_BASE", "https://resona.live"),
    "resona_api_key": os.environ.get("RESONA_API_KEY", ""),
    "resona_request_timeout_s": int(os.environ.get("RESONA_TIMEOUT_S", "120")),
    "segment_seconds": int(os.environ.get("MAXVOICE_SEGMENT_SECONDS", "60")),
    "whisper_model": os.environ.get("MAXVOICE_WHISPER_MODEL", "small"),
    "whisper_device": os.environ.get("MAXVOICE_WHISPER_DEVICE", "cpu"),
    "whisper_compute_type": os.environ.get("MAXVOICE_WHISPER_COMPUTE_TYPE", "int8"),
    "target_language": os.environ.get("MAXVOICE_TARGET_LANGUAGE", "vi"),
    "sub_mode": os.environ.get("MAXVOICE_SUB_MODE", "target"),  # "target", "bilingual", "original"
    "burn_subtitles": os.environ.get("MAXVOICE_BURN_SUBTITLES", "false").lower() in ("1", "true", "yes"),
    "ffmpeg_path": os.environ.get("FFMPEG_PATH", "ffmpeg"),
    "ffprobe_path": os.environ.get("FFPROBE_PATH", "ffprobe"),
    "storage_dir": os.environ.get("MAXVOICE_STORAGE_DIR", str(APP_DIR / "storage")),
    "worker_threads": int(os.environ.get("MAXVOICE_WORKER_THREADS", "2")),
    "redis_url": os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
    "port": int(os.environ.get("PORT", "8686")),
    "file_retention_hours": float(os.environ.get("KINGVOICE_RETENTION_HOURS", "2")),
    "cleanup_interval_seconds": int(os.environ.get("KINGVOICE_CLEANUP_INTERVAL_S", "300")),
    "require_api_key": os.environ.get("KINGVOICE_REQUIRE_API_KEY", "true").lower() in ("1", "true", "yes"),
    "admin_token": os.environ.get("KINGVOICE_ADMIN_TOKEN", ""),
}


class AppConfig:
    """Settings object for the headless container backend, supporting both
    environment variables and JSON persistence in `storage_dir`.
    """

    def __init__(self, data=None):
        object.__setattr__(self, "_data", {**DEFAULTS, **(data or {})})

    def __getattr__(self, name):
        try:
            return self._data[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self._data[name] = value

    def to_dict(self):
        return dict(self._data)

    @property
    def upload_dir(self):
        return os.path.join(self.storage_dir, "uploads")

    @property
    def output_dir(self):
        return os.path.join(self.storage_dir, "outputs")

    @property
    def db_path(self):
        return os.path.join(self.storage_dir, "voicetext.sqlite3")


def load_config():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    data = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    config = AppConfig(data)
    os.makedirs(config.storage_dir, exist_ok=True)
    os.makedirs(config.upload_dir, exist_ok=True)
    os.makedirs(config.output_dir, exist_ok=True)
    return config


def save_config(config: AppConfig):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(config.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )

