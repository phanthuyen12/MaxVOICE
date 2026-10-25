import datetime
import os
import sqlite3
import threading

_lock = threading.Lock()
_conn = None

JOB_COLUMNS = [
    "id", "type", "originalFilename", "filePath", "stage", "segmentCount",
    "durationSeconds", "defaultVoiceId", "defaultVoiceName",
    "finalVideoStatus", "finalVideoPath", "error",
    "backgroundMusicPath", "backgroundMusicName", "createdAt", "updatedAt",
]

# A "clip" is one video file inside a project ("jobs" row of type "video").
# Segments belonging to a clip are stored with segments.jobId = clip id (not
# the project id) — segIndex only needs to be unique per clip, and every bit
# of existing segment-level code (transcribe/TTS/UI) already keys off
# "jobId" + "segIndex", so clips slot into that unchanged by just handing it
# the clip's id wherever a job id was passed before.
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

        CREATE INDEX IF NOT EXISTS idx_jobs_createdAt ON jobs (createdAt DESC);
        """
    )
    # CREATE TABLE IF NOT EXISTS is a no-op against an existing table from an
    # older schema version — add any columns a prior version's `jobs` table
    # might be missing.
    _add_column_if_missing("jobs", "defaultVoiceId", "TEXT")
    _add_column_if_missing("jobs", "defaultVoiceName", "TEXT")
    _conn.commit()
    return _conn


def _add_column_if_missing(table, column, sql_type):
    existing = _conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not any(row["name"] == column for row in existing):
        _conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _filter_columns(fields, columns):
    return {k: v for k, v in fields.items() if k in columns}


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


def delete_job(job_id):
    with _lock:
        clip_ids = [r["id"] for r in _conn.execute("SELECT id FROM clips WHERE jobId = ?", (job_id,)).fetchall()]
        for clip_id in clip_ids:
            _conn.execute("DELETE FROM segments WHERE jobId = ?", (clip_id,))
        _conn.execute("DELETE FROM clips WHERE jobId = ?", (job_id,))
        _conn.execute("DELETE FROM segments WHERE jobId = ?", (job_id,))  # "audio" standalone jobs
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
        # Keep the parent job's retention/updatedAt timestamp fresh while
        # transcription or TTS is actively touching one of its segments.
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
