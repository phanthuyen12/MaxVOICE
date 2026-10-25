import json
import os
from pathlib import Path

APP_DIR = Path.home() / ".voicetext_studio"
CONFIG_PATH = APP_DIR / "config.json"

DEFAULTS = {
    "tts_backend": "vieneu",  # "vieneu" (free, offline) or "resona" (cloud API)
    "resona_api_base": "https://resona.live",
    "resona_api_key": "",
    "resona_request_timeout_s": 120,
    "segment_seconds": 60,
    "whisper_model": "small",
    "whisper_device": "cpu",
    "whisper_compute_type": "int8",
    "ffmpeg_path": "ffmpeg",
    "ffprobe_path": "ffprobe",
    "storage_dir": str(APP_DIR / "storage"),
}


class AppConfig:
    """Thin dict-backed settings object, persisted as JSON in the user's home dir.

    Desktop-only settings replace the old backend/.env (no Redis/Mongo/Node env needed).
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
    return config


def save_config(config: AppConfig):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(config.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
