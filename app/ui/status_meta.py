# Mirrors frontend/src/utils/statusMeta.js so the desktop app describes the
# same statuses the same way as the web app did.

SEGMENT_STATUS_META = {
    "cut": {"label": "Đã cắt đoạn", "color": "#6c757d"},
    "extracting_audio": {"label": "Đang trích âm thanh", "color": "#d6a419"},
    "transcribing": {"label": "Đang nhận diện giọng nói", "color": "#d6a419"},
    "transcribed": {"label": "Đã có transcript", "color": "#198754"},
    "tts_queued": {"label": "Đang xếp hàng tạo giọng", "color": "#6c757d"},
    "tts_processing": {"label": "Đang tạo giọng nói", "color": "#d6a419"},
    "tts_done": {"label": "Đã tạo giọng nói", "color": "#17a673"},
    "failed": {"label": "Lỗi xử lý", "color": "#e5484d"},
    "tts_failed": {"label": "Lỗi tạo giọng nói", "color": "#e5484d"},
}

JOB_STATUS_META = {
    "queued": {"label": "Đang chờ xử lý", "color": "#6c757d"},
    "cutting": {"label": "Đang cắt video", "color": "#d6a419"},
    "ready": {"label": "Đã cắt xong", "color": "#198754"},
    "processing": {"label": "Đang xử lý", "color": "#d6a419"},
    "transcribed": {"label": "Đã có transcript", "color": "#198754"},
    "completed": {"label": "Hoàn tất", "color": "#17a673"},
    "partial_failure": {"label": "Có lỗi một phần", "color": "#e5484d"},
}

JOB_TYPE_LABEL = {
    "video": "Project (video → cắt & đổi giọng)",
    "audio": "Voice to Text",
}

# The project's own pipeline "stage" (set by handle_start_project) is a
# separate axis from derive_overall_status(segments) below — it reflects
# which step the "Bắt đầu" run is currently on, shown in the project header
# while an automated run is in progress.
PROJECT_STAGE_LABEL = {
    "queued": "Đang chờ xử lý",
    "cutting": "Đang cắt video...",
    "transcribing": "Đang nhận diện giọng nói...",
    "generating_voice": "Đang tạo giọng nói...",
    "ready": "Đã xử lý xong, đang ghép video",
    "failed": "Có lỗi trong quá trình xử lý",
}


def status_meta(status, kind="segment"):
    table = JOB_STATUS_META if kind == "job" else SEGMENT_STATUS_META
    return table.get(status, {"label": status or "—", "color": "#6c757d"})


def derive_overall_status(segments):
    if not segments:
        return "processing"
    if any(s.get("status") in ("failed", "tts_failed") for s in segments):
        return "partial_failure"
    if all(s.get("status") == "tts_done" for s in segments):
        return "completed"
    if all(
        s.get("status") in ("transcribed", "tts_queued", "tts_processing", "tts_done")
        for s in segments
    ):
        return "transcribed"
    return "processing"
