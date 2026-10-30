import datetime
import os
import secrets
import sqlite3
import threading
import uuid

_lock = threading.Lock()
_conn = None

JOB_COLUMNS = [
    "id", "type", "originalFilename", "filePath", "stage", "segmentCount",
    "durationSeconds", "defaultVoiceId", "defaultVoiceName", "targetLanguage",
    "subMode", "burnSubtitles", "webhookUrl", "apiKeyName",
    "finalVideoStatus", "finalVideoPath", "error",
    "backgroundMusicPath", "backgroundMusicName", "createdAt", "updatedAt",
]

CLIP_COLUMNS = [
    "id", "jobId", "orderIndex", "originalFilename", "filePath", "stage",
    "segmentCount", "durationSeconds", "error", "createdAt", "updatedAt",
]

SEGMENT_COLUMNS = [
    "jobId", "segIndex", "kind", "sourcePath", "startSec", "endSec", "status",
    "language", "transcriptText", "editedText", "transcriptCues", "srtPath",
    "audioPath", "ttsRequestId", "ttsVoiceId", "ttsAudioPath", "ttsAudioUrl",
    "ttsProgress", "error", "createdAt", "updatedAt",
]

API_KEY_COLUMNS = [
    "id", "name", "apiKey", "isActive", "requestCount", "lastUsedAt", "createdAt",
]


def connect(db_path):
    global _conn
    if _conn is not None:
        return _conn
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    _conn = sqlite3.connect(db_path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            type TEXT,
            originalFilename TEXT,
            filePath TEXT,
            stage TEXT,
            segmentCount INTEGER,
            durationSeconds REAL,
            defaultVoiceId TEXT,
            defaultVoiceName TEXT,
            targetLanguage TEXT,
            subMode TEXT,
            burnSubtitles INTEGER,
            webhookUrl TEXT,
            apiKeyName TEXT,
            finalVideoStatus TEXT,
            finalVideoPath TEXT,
            error TEXT,
            backgroundMusicPath TEXT,
            backgroundMusicName TEXT,
            createdAt TEXT,
            updatedAt TEXT
        );

        CREATE TABLE IF NOT EXISTS clips (
            id TEXT PRIMARY KEY,
            jobId TEXT NOT NULL,
            orderIndex INTEGER NOT NULL,
            originalFilename TEXT,
            filePath TEXT,
            stage TEXT,
            segmentCount INTEGER,
            durationSeconds REAL,
            error TEXT,
            createdAt TEXT,
            updatedAt TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_clips_job ON clips (jobId, orderIndex);

        CREATE TABLE IF NOT EXISTS segments (
            jobId TEXT NOT NULL,
            segIndex INTEGER NOT NULL,
            kind TEXT,
            sourcePath TEXT,
            startSec REAL,
            endSec REAL,
            status TEXT,
            language TEXT,
            transcriptText TEXT,
            editedText TEXT,
            transcriptCues TEXT,
            srtPath TEXT,
            audioPath TEXT,
            ttsRequestId TEXT,
            ttsVoiceId TEXT,
            ttsAudioPath TEXT,
            ttsAudioUrl TEXT,
            ttsProgress TEXT,
            error TEXT,
            createdAt TEXT,
            updatedAt TEXT,
            PRIMARY KEY (jobId, segIndex)
        );

        CREATE TABLE IF NOT EXISTS api_keys (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            apiKey TEXT NOT NULL UNIQUE,
            isActive INTEGER NOT NULL DEFAULT 1,
            requestCount INTEGER NOT NULL DEFAULT 0,
            lastUsedAt TEXT,
            createdAt TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_createdAt ON jobs (createdAt DESC);
        CREATE INDEX IF NOT EXISTS idx_api_keys_key ON api_keys (apiKey);
        """
    )
    _add_column_if_missing("jobs", "defaultVoiceId", "TEXT")
    _add_column_if_missing("jobs", "defaultVoiceName", "TEXT")
    _add_column_if_missing("jobs", "targetLanguage", "TEXT")
    _add_column_if_missing("jobs", "subMode", "TEXT")
    _add_column_if_missing("jobs", "burnSubtitles", "INTEGER")
    _add_column_if_missing("jobs", "webhookUrl", "TEXT")
    _add_column_if_missing("jobs", "apiKeyName", "TEXT")
    _conn.commit()

    # Seed a default KingVoice API key if table is empty
    existing_keys = _conn.execute("SELECT COUNT(*) AS cnt FROM api_keys").fetchone()["cnt"]
    if existing_keys == 0:
        env_key = (
            os.environ.get("KINGVOICE_API_KEY")
            or os.environ.get("MAXVOICE_API_KEY")
            or f"kv_live_{secrets.token_hex(16)}"
        )
        create_api_key(name="KingVoice Default Key", custom_key=env_key)

    return _conn


def _add_column_if_missing(table, column, sql_type):
    existing = _conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not any(row["name"] == column for row in existing):
        _conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _filter_columns(fields, columns):
    return {k: v for k, v in fields.items() if k in columns}


# api_keys -------------------------------------------------------------

def _format_api_key_row(row_or_dict):
    if not row_or_dict:
        return None
    d = dict(row_or_dict)
    d["key"] = d.get("apiKey") or ""
    d["usageCount"] = d.get("requestCount") or 0
    return d


def create_api_key(name: str, custom_key: str = None):
    now = _now()
    key_id = str(uuid.uuid4())
    raw_key = (custom_key or "").strip() or f"kv_live_{secrets.token_hex(16)}"
    row = {
        "id": key_id,
        "name": (name or "KingVoice Client").strip(),
        "apiKey": raw_key,
        "isActive": 1,
        "requestCount": 0,
        "lastUsedAt": None,
        "createdAt": now,
    }
    with _lock:
        _conn.execute(
            """
            INSERT INTO api_keys (id, name, apiKey, isActive, requestCount, lastUsedAt, createdAt)
            VALUES (:id, :name, :apiKey, :isActive, :requestCount, :lastUsedAt, :createdAt)
            """,
            row,
        )
        _conn.commit()
    return _format_api_key_row(row)


def list_api_keys():
    with _lock:
        rows = _conn.execute("SELECT * FROM api_keys ORDER BY createdAt DESC").fetchall()
    return [_format_api_key_row(r) for r in rows]


def get_api_key_by_id(key_id: str):
    with _lock:
        row = _conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
    return _format_api_key_row(row) if row else None


def verify_and_touch_api_key(token: str):
    """Checks if `token` matches an active API key in `api_keys` (or env MAXVOICE_API_KEY).
    If valid, increments `requestCount` and updates `lastUsedAt`, returning the key dict.
    """
    if not token:
        return None
    token = token.strip()
    now = _now()
    with _lock:
        row = _conn.execute(
            "SELECT * FROM api_keys WHERE apiKey = ? AND isActive = 1", (token,)
        ).fetchone()
        if row:
            _conn.execute(
                "UPDATE api_keys SET requestCount = requestCount + 1, lastUsedAt = ? WHERE id = ?",
                (now, row["id"]),
            )
            _conn.commit()
            return dict(row)
    env_key = (os.environ.get("KINGVOICE_API_KEY") or os.environ.get("MAXVOICE_API_KEY") or "").strip()
    if env_key and token == env_key:
        return {"id": "env", "name": "Env Master Key", "apiKey": env_key, "isActive": 1}
    return None


def update_api_key(key_id: str, **fields):
    row = _filter_columns(fields, API_KEY_COLUMNS)
    if not row:
        return get_api_key_by_id(key_id)
    row["id"] = key_id
    sets = ", ".join(f"{c} = :{c}" for c in row if c != "id")
    with _lock:
        _conn.execute(f"UPDATE api_keys SET {sets} WHERE id = :id", row)
        _conn.commit()
    return get_api_key_by_id(key_id)


def delete_api_key(key_id: str):
    with _lock:
        _conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
        _conn.commit()


# jobs -----------------------------------------------------------------

def create_job(**fields):
    now = _now()
    fields.setdefault("createdAt", now)
    fields["updatedAt"] = now
    row = _filter_columns(fields, JOB_COLUMNS)
    cols = list(row.keys())
    placeholders = ", ".join(f":{c}" for c in cols)
    with _lock:
        _conn.execute(f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({placeholders})", row)
        _conn.commit()
    return row


def update_job(job_id, **fields):
    fields["updatedAt"] = _now()
    row = _filter_columns(fields, JOB_COLUMNS)
    row["id"] = job_id
    sets = ", ".join(f"{c} = :{c}" for c in row if c != "id")
    with _lock:
        _conn.execute(f"UPDATE jobs SET {sets} WHERE id = :id", row)
        _conn.commit()


def get_job(job_id):
    with _lock:
        row = _conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs(limit=50):
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM jobs ORDER BY createdAt DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def list_expired_jobs(retention_hours: float = 2.0):
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(hours=max(0.0, float(retention_hours)))
    ).isoformat()
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM jobs WHERE createdAt < ?", (cutoff,)
        ).fetchall()
    return [dict(r) for r in rows]


def delete_job(job_id):
    with _lock:
        clip_ids = [r["id"] for r in _conn.execute("SELECT id FROM clips WHERE jobId = ?", (job_id,)).fetchall()]
        for clip_id in clip_ids:
            _conn.execute("DELETE FROM segments WHERE jobId = ?", (clip_id,))
        _conn.execute("DELETE FROM clips WHERE jobId = ?", (job_id,))
        _conn.execute("DELETE FROM segments WHERE jobId = ?", (job_id,))
        _conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        _conn.commit()


# clips ---------------------------------------------------------------

def create_clip(**fields):
    now = _now()
    fields.setdefault("createdAt", now)
    fields["updatedAt"] = now
    row = _filter_columns(fields, CLIP_COLUMNS)
    cols = list(row.keys())
    placeholders = ", ".join(f":{c}" for c in cols)
    with _lock:
        _conn.execute(f"INSERT INTO clips ({', '.join(cols)}) VALUES ({placeholders})", row)
        _conn.commit()
    return row


def update_clip(clip_id, **fields):
    fields["updatedAt"] = _now()
    row = _filter_columns(fields, CLIP_COLUMNS)
    row["id"] = clip_id
    sets = ", ".join(f"{c} = :{c}" for c in row if c != "id")
    with _lock:
        _conn.execute(f"UPDATE clips SET {sets} WHERE id = :id", row)
        _conn.commit()


def get_clip(clip_id):
    with _lock:
        row = _conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
    return dict(row) if row else None


def list_clips(job_id):
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM clips WHERE jobId = ? ORDER BY orderIndex ASC", (job_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def delete_clip(clip_id):
    with _lock:
        _conn.execute("DELETE FROM segments WHERE jobId = ?", (clip_id,))
        _conn.execute("DELETE FROM clips WHERE id = ?", (clip_id,))
        _conn.commit()


def next_clip_order_index(job_id):
    with _lock:
        row = _conn.execute(
            "SELECT COALESCE(MAX(orderIndex), -1) + 1 AS next FROM clips WHERE jobId = ?", (job_id,)
        ).fetchone()
    return row["next"]


# segments ---------------------------------------------------------------

def create_segment(job_id, index, **fields):
    now = _now()
    fields["jobId"] = job_id
    fields["segIndex"] = int(index)
    fields.setdefault("createdAt", now)
    fields["updatedAt"] = now
    row = _filter_columns(fields, SEGMENT_COLUMNS)
    cols = list(row.keys())
    placeholders = ", ".join(f":{c}" for c in cols)
    with _lock:
        _conn.execute(f"INSERT INTO segments ({', '.join(cols)}) VALUES ({placeholders})", row)
        _conn.commit()
    return row


def update_segment(job_id, index, **fields):
    updated_at = _now()
    fields["updatedAt"] = updated_at
    row = _filter_columns(fields, SEGMENT_COLUMNS)
    row["jobId"] = job_id
    row["segIndex"] = int(index)
    sets = ", ".join(f"{c} = :{c}" for c in row if c not in ("jobId", "segIndex"))
    with _lock:
        _conn.execute(
            f"UPDATE segments SET {sets} WHERE jobId = :jobId AND segIndex = :segIndex", row
        )
        _conn.execute(
            "UPDATE jobs SET updatedAt = :u WHERE id = :id", {"u": updated_at, "id": job_id}
        )
        _conn.commit()


def _segment_row_to_dict(row):
    if row is None:
        return None
    d = dict(row)
    d["index"] = d.pop("segIndex")
    return d


def get_segment(job_id, index):
    with _lock:
        row = _conn.execute(
            "SELECT * FROM segments WHERE jobId = ? AND segIndex = ?", (job_id, int(index))
        ).fetchone()
    return _segment_row_to_dict(row)


def get_all_segments(job_id):
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM segments WHERE jobId = ? ORDER BY segIndex ASC", (job_id,)
        ).fetchall()
    return [_segment_row_to_dict(r) for r in rows]
