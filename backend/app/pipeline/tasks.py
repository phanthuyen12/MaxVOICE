import hashlib
import json
import logging
import os
import re
import shutil
import requests

from .. import db
from ..paths import chunks_dir, job_output_dir, segment_dir
from ..services import ffmpeg_service, local_tts_service, merge_service, resona_service, srt_service
from ..services import text_splitter, translation_service, whisper_service

logger = logging.getLogger("maxvoice.tasks")

DEFAULT_VOICE_BY_LANG = {
    "vi": "multi:vi-VN-HoaiMy",
    "en": "multi:en-US-Ava",
    "ru": "multi:ru-RU-Svetlana",
    "ja": "multi:ja-JP-Nanami",
    "ko": "multi:ko-KR-SunHi",
    "zh": "multi:zh-CN-Xiaoxiao",
    "fr": "multi:fr-FR-Denise",
    "de": "multi:de-DE-Katja",
    "es": "multi:es-ES-Elvira",
    "th": "multi:th-TH-Premwadee",
}

DEFAULT_MALE_VOICE_BY_LANG = {
    "vi": "multi:vi-VN-NamMinh",
    "en": "multi:en-US-Andrew",
    "ru": "multi:ru-RU-Dmitry",
    "ja": "multi:ja-JP-Keita",
    "ko": "multi:ko-KR-InJoon",
    "zh": "multi:zh-CN-Yunxi",
    "fr": "multi:fr-FR-Henri",
    "de": "multi:de-DE-Conrad",
    "es": "multi:es-ES-Alvaro",
    "th": "multi:th-TH-Niwat",
}


def resolve_default_voice(voice_id, target_language="vi", gender=None):
    """Resolves `voice_id` if the caller passed None, '', 'auto', 'default',
    or a shorthand like 'male'/'female' or '<lang>-male'/'<lang>-female'.
    """
    raw = str(voice_id or "").strip()
    low = raw.lower()
    lang = str(target_language or "vi").strip().lower().split("-")[0]

    if low in ("male", "nam"):
        return DEFAULT_MALE_VOICE_BY_LANG.get(lang, "multi:vi-VN-NamMinh")
    if low in ("female", "nu", "nữ"):
        return DEFAULT_VOICE_BY_LANG.get(lang, "multi:vi-VN-HoaiMy")
    if "-" in low and low.endswith(("-male", "-nam", "-female", "-nu")):
        parts = low.split("-")
        lang_prefix = parts[0]
        is_male = parts[-1] in ("male", "nam")
        table = DEFAULT_MALE_VOICE_BY_LANG if is_male else DEFAULT_VOICE_BY_LANG
        if lang_prefix in table:
            return table[lang_prefix]

    if raw and low not in ("", "auto", "default"):
        return raw

    if str(gender or "").strip().lower() in ("male", "nam", "m"):
        return DEFAULT_MALE_VOICE_BY_LANG.get(lang, "multi:vi-VN-NamMinh")
    return DEFAULT_VOICE_BY_LANG.get(lang, "multi:vi-VN-HoaiMy")


def _notify_webhook(job_id):
    """If the job has a `webhookUrl`, POSTs the current job status JSON to it."""
    job = db.get_job(job_id)
    if not job or not job.get("webhookUrl"):
        return
    webhook_url = job["webhookUrl"]
    payload = {
        "event": "job.finished" if job.get("finalVideoStatus") == "done" else "job.failed",
        "job_id": job_id,
        "type": job.get("type"),
        "stage": job.get("stage"),
        "finalVideoStatus": job.get("finalVideoStatus"),
        "error": job.get("error") or "",
        "targetLanguage": job.get("targetLanguage"),
        "subMode": job.get("subMode"),
        "download_video_path": f"/api/v1/jobs/{job_id}/download/video" if job.get("finalVideoStatus") == "done" else None,
        "download_srt_path": f"/api/v1/jobs/{job_id}/download/srt",
        "updatedAt": job.get("updatedAt"),
    }
    try:
        requests.post(webhook_url, json=payload, timeout=15)
    except Exception as exc:
        logger.warning("Webhook delivery failed for job %s -> %s: %s", job_id, webhook_url, exc)


QUALITY_RETRIES = 6
_QUALITY_RETRY_RE = re.compile(
    r"quality[\s_-]*check[\s_-]*failed|pronunciation|none of the generated takes passed",
    re.IGNORECASE,
)


def _resolve_parent_job(job_or_clip_id):
    """Finds the parent `jobs` row whether `job_or_clip_id` is a project/audio job ID
    or a clip ID inside a project.
    """
    job = db.get_job(job_or_clip_id)
    if job:
        return job
    clip = db.get_clip(job_or_clip_id)
    if clip and clip.get("jobId"):
        return db.get_job(clip["jobId"])
    return None


def _get_job_language_settings(config, job_or_clip_id, target_language=None, sub_mode=None):
    job = _resolve_parent_job(job_or_clip_id) or {}
    resolved_target = (
        target_language
        or job.get("targetLanguage")
        or getattr(config, "target_language", "vi")
        or "vi"
    )
    resolved_mode = (
        sub_mode
        or job.get("subMode")
        or getattr(config, "sub_mode", "target")
        or "target"
    )
    return resolved_target, resolved_mode, job


# --- video split -----------------------------------------------------------
#
# A "project" (jobs row, type="video") holds an ordered list of clips (its
# own `clips` table rows). Each clip gets cut into segments exactly like the
# single-video case used to work — segments are stored with
# segments.jobId = clip_id, so every segment-level function below (transcribe,
# TTS) is completely unaware that clips/projects exist; it just takes
# whatever id it's handed as "jobId".

def _cut_clip(worker, config, clip_id, file_path):
    """Pure cut step: splits the clip into segments, no auto-chaining to
    transcription. Shared by the manual per-clip flow (handle_cut_clip, which
    DOES chain into transcription) and the "Bắt đầu" project orchestrator
    (which runs every step itself, synchronously, so it must not also have
    this silently enqueue work on the same queue it's running from).
    """
    db.update_clip(clip_id, stage="cutting")
    worker.job_changed.emit(clip_id)

    try:
        duration_seconds = ffmpeg_service.get_duration_seconds(file_path, config.ffprobe_path)
        out_dir = chunks_dir(config, clip_id)
        chunk_paths = ffmpeg_service.split_video_into_segments(
            file_path, out_dir, config.segment_seconds, config.ffmpeg_path
        )

        start_sec = 0.0
        indices = []
        for i, chunk_path in enumerate(chunk_paths):
            chunk_duration = ffmpeg_service.get_duration_seconds(chunk_path, config.ffprobe_path)
            end_sec = min(duration_seconds, start_sec + chunk_duration)
            db.create_segment(
                clip_id, i,
                kind="video-chunk", sourcePath=chunk_path,
                startSec=start_sec, endSec=end_sec, status="cut",
            )
            worker.segment_changed.emit(clip_id, i)
            indices.append(i)
            start_sec = end_sec

        db.update_clip(
            clip_id, stage="ready", segmentCount=len(chunk_paths), durationSeconds=duration_seconds
        )
        worker.job_changed.emit(clip_id)
        return indices
    except Exception as exc:
        db.update_clip(clip_id, stage="failed", error=str(exc))
        worker.job_changed.emit(clip_id)
        raise


def handle_cut_clip(worker, config, clip_id, file_path):
    """Task-queue entry point for adding one clip manually: cut it, then
    auto-enqueue transcription for each resulting segment (today's
    incremental, no-"Start"-needed UX for the clip timeline).
    """
    indices = _cut_clip(worker, config, clip_id, file_path)
    for index in indices:
        worker.submit(handle_transcribe_segment, config, clip_id, index)


# --- transcription & multilingual translation ---------------------------------

def _build_cues_and_write_srt(seg_dir, raw_segments, source_language, target_language, sub_mode):
    """Translates `raw_segments` (each having `start`, `end`, and `originalText` or `text`)
    into `target_language` (unless `target_language == 'original'` or same as `source_language`),
    writes `subtitle.srt` according to `sub_mode` ('target', 'bilingual', 'original'),
    and returns `(cues, transcript_text, srt_path)`.
    """
    original_texts = [
        str(cue.get("originalText") or cue.get("text") or "").strip()
        for cue in raw_segments
    ]

    need_translation = (
        bool(original_texts)
        and target_language
        and target_language != "original"
        and not translation_service.is_same_language(source_language, target_language)
    )

    translated_texts = None
    if need_translation:
        try:
            translated_texts = translation_service.translate_texts(
                original_texts, source_language, target_language
            )
        except Exception as exc:
            lang_label = translation_service.language_label(target_language)
            raise RuntimeError(
                f"Không dịch được phụ đề từ {source_language or 'gốc'} sang {lang_label}: {exc}"
            )

    cues = []
    for i, cue in enumerate(raw_segments):
        orig = original_texts[i]
        trans = translated_texts[i].strip() if translated_texts and i < len(translated_texts) else ""
        active_text = trans if (need_translation and trans) else orig
        entry = {
            "start": cue["start"],
            "end": cue["end"],
            "text": active_text,
            "originalText": orig,
        }
        if need_translation and trans:
            entry["translatedText"] = trans
            entry["targetLanguage"] = target_language
        cues.append(entry)

    transcript_text = srt_service.segments_to_plain_text(cues, mode=sub_mode)
    srt_content = srt_service.segments_to_srt(cues, mode=sub_mode)
    os.makedirs(seg_dir, exist_ok=True)
    srt_path = os.path.join(seg_dir, "subtitle.srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(srt_content)

    return cues, transcript_text, srt_path


def handle_transcribe_segment(worker, config, job_id, segment_index, target_language=None, sub_mode=None):
    segment = db.get_segment(job_id, segment_index)
    if not segment:
        raise RuntimeError(f"Segment {segment_index} of job {job_id} not found")

    resolved_target, resolved_mode, _ = _get_job_language_settings(
        config, job_id, target_language=target_language, sub_mode=sub_mode
    )

    seg_dir = segment_dir(config, job_id, segment_index)
    os.makedirs(seg_dir, exist_ok=True)
    audio_path = os.path.join(seg_dir, "audio.wav")

    try:
        db.update_segment(job_id, segment_index, status="extracting_audio", error="")
        worker.segment_changed.emit(job_id, segment_index)
        ffmpeg_service.extract_audio_wav(segment["sourcePath"], audio_path, config.ffmpeg_path)

        db.update_segment(job_id, segment_index, status="transcribing")
        worker.segment_changed.emit(job_id, segment_index)
        result = whisper_service.transcribe(
            audio_path, config.whisper_model, config.whisper_device, config.whisper_compute_type
        )
        language = result["language"]
        segments = result["segments"]

        cues, transcript_text, srt_path = _build_cues_and_write_srt(
            seg_dir, segments, language, resolved_target, resolved_mode
        )

        db.update_segment(
            job_id, segment_index,
            audioPath=audio_path, language=language, transcriptText=transcript_text,
            editedText=None,
            transcriptCues=json.dumps(cues, ensure_ascii=False), srtPath=srt_path, status="transcribed",
            error="",
        )
        worker.segment_changed.emit(job_id, segment_index)
    except Exception as exc:
        db.update_segment(job_id, segment_index, status="failed", error=str(exc))
        worker.segment_changed.emit(job_id, segment_index)
        raise


def handle_retranslate_segment(worker, config, job_id, segment_index, target_language=None, sub_mode=None):
    """Re-translates an already-transcribed segment into `target_language` and
    updates its SRT/transcript in `sub_mode` without re-running Whisper.
    """
    segment = db.get_segment(job_id, segment_index)
    if not segment:
        return
    raw_cues = _read_transcript_cues(segment)
    if not raw_cues:
        return

    resolved_target, resolved_mode, _ = _get_job_language_settings(
        config, job_id, target_language=target_language, sub_mode=sub_mode
    )
    seg_dir = segment_dir(config, job_id, segment_index)
    source_language = segment.get("language") or "en"

    cues, transcript_text, srt_path = _build_cues_and_write_srt(
        seg_dir, raw_cues, source_language, resolved_target, resolved_mode
    )
    new_status = segment.get("status") or "transcribed"
    if new_status == "tts_done":
        # Text language changed, mark ready to re-synthesize voice if desired
        new_status = "transcribed"
    db.update_segment(
        job_id, segment_index,
        transcriptText=transcript_text,
        editedText=None,
        transcriptCues=json.dumps(cues, ensure_ascii=False),
        srtPath=srt_path,
        status=new_status,
        error="",
    )
    worker.segment_changed.emit(job_id, segment_index)


def handle_retranslate_project(worker, config, project_id, target_language, sub_mode="target"):
    """Re-translates all transcribed segments of a video project or audio job
    to `target_language` and refreshes all SRT files according to `sub_mode`.
    """
    db.update_job(project_id, targetLanguage=target_language, subMode=sub_mode, error="")
    worker.job_changed.emit(project_id)

    job = db.get_job(project_id)
    if not job:
        return

    if job.get("type") == "video":
        for clip in db.list_clips(project_id):
            for seg in db.get_all_segments(clip["id"]):
                if seg.get("transcriptCues"):
                    handle_retranslate_segment(
                        worker, config, clip["id"], seg["index"],
                        target_language=target_language, sub_mode=sub_mode,
                    )
    else:
        for seg in db.get_all_segments(project_id):
            if seg.get("transcriptCues"):
                handle_retranslate_segment(
                    worker, config, project_id, seg["index"],
                    target_language=target_language, sub_mode=sub_mode,
                )

    worker.job_changed.emit(project_id)


# --- text-to-speech -----------------------------------------------------------

def _read_transcript_cues(segment):
    try:
        raw = segment.get("transcriptCues")
        cues = json.loads(raw) if raw else []
        if not isinstance(cues, list):
            return []
        start_sec = segment.get("startSec")
        end_sec = segment.get("endSec")
        duration = (end_sec - start_sec) if (start_sec is not None and end_sec is not None) else None
        out = []
        for cue in cues:
            start = cue.get("start")
            end = cue.get("end")
            text = str(cue.get("text") or cue.get("originalText") or "").strip()
            if start is None or end is None or not text:
                continue
            if not (start >= 0 and end > start):
                continue
            if duration is not None and not (start < duration):
                continue
            new_end = min(end, duration) if duration is not None else end
            if new_end <= start:
                continue
            out.append({**cue, "end": new_end})
        return out
    except (ValueError, TypeError):
        return []


def _apply_user_edited_text_to_cues(cues, edited_text):
    """If the user edited the transcript text box in the UI, sync those edits
    back into the cues so TTS speaks the edited words.
    """
    if not edited_text or not edited_text.strip() or not cues:
        return cues
    cleaned = edited_text.strip()
    lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
    if len(lines) == len(cues):
        return [{**cue, "text": lines[i], "translatedText": lines[i]} for i, cue in enumerate(cues)]
    if len(cues) == 1:
        return [{**cues[0], "text": cleaned, "translatedText": cleaned}]
    return cues


def _prepare_cues_for_voice(cues, segment_source_lang, target_language, voice_id):
    """Ensures the text spoken by `voice_id` matches the target/voice language.
    For example, if the user selects an English voice (`multi:en-...`) or sets
    `target_language = 'en'`, ensures the cues are in English (using `originalText`
    if the video was originally English, or translating to English on the fly).
    """
    if not cues:
        return cues, target_language or "vi"

    desired_lang = target_language or "vi"
    if local_tts_service.is_multilingual_voice(voice_id):
        desired_lang = local_tts_service.voice_language(voice_id, default=desired_lang)
    elif desired_lang == "original":
        desired_lang = segment_source_lang or "vi"

    # Check if cues already have translations in `desired_lang`
    prepared = []
    need_on_the_fly_translate = False
    for cue in cues:
        orig = str(cue.get("originalText") or cue.get("text") or "").strip()
        trans = str(cue.get("translatedText") or "").strip()
        cue_target_lang = cue.get("targetLanguage") or ("vi" if trans else segment_source_lang)

        if translation_service.is_same_language(desired_lang, segment_source_lang):
            spoken = orig
        elif trans and translation_service.is_same_language(desired_lang, cue_target_lang):
            spoken = trans
        else:
            spoken = trans or orig
            if desired_lang != "original" and not translation_service.is_same_language(desired_lang, cue_target_lang):
                need_on_the_fly_translate = True

        prepared.append({**cue, "text": spoken})

    if need_on_the_fly_translate and desired_lang != "original":
        source_texts = [str(c.get("originalText") or c.get("text") or "").strip() for c in cues]
        try:
            translated_list = translation_service.translate_texts(
                source_texts, segment_source_lang or "auto", desired_lang
            )
            prepared = [
                {
                    **c,
                    "text": translated_list[i] if i < len(translated_list) else c["text"],
                    "translatedText": translated_list[i] if i < len(translated_list) else c.get("translatedText", ""),
                    "targetLanguage": desired_lang,
                }
                for i, c in enumerate(prepared)
            ]
        except Exception:
            pass

    return prepared, desired_lang


def _synthesize_chunk_once_vieneu(config, text, voice_id, dest_path):
    """Local/Multilingual synthesis via `local_tts_service` — supports both
    VieNeu offline Vietnamese voices/clones and `multi:...` English/Multilingual voices.
    """
    file_path = dest_path + ".wav"
    local_tts_service.synthesize_to_file(
        text, file_path, voice=voice_id, ffmpeg=config.ffmpeg_path
    )
    return {"filePath": file_path, "audioUrl": "", "requestId": ""}


def _synthesize_chunk_once_resona(config, text, voice_id, language, dest_path):
    submitted = resona_service.text_to_speech(
        config.resona_api_base, config.resona_api_key, text, voice_id, language,
        timeout=config.resona_request_timeout_s,
    )
    result = resona_service.poll_until_done(
        lambda: resona_service.get_text_to_speech_result(
            config.resona_api_base, config.resona_api_key, submitted["request_id"],
            timeout=config.resona_request_timeout_s,
        ),
        interval_s=3, timeout_s=600,
    )
    if result.get("status") != "completed":
        raise resona_service.ResonaError(
            f"{result.get('error_code', 'unknown')}: {result.get('error', 'generation failed')}"
        )
    audio_url = result["audio_urls"][0]
    ext = resona_service.url_extension(audio_url)
    file_path = dest_path + ext
    resona_service.download_to_file(audio_url, file_path)
    return {"filePath": file_path, "audioUrl": audio_url, "requestId": submitted["request_id"]}


def _synthesize_chunk(config, text, voice_id, language, dest_path, log=None):
    """Resona sometimes rejects every take of a chunk ("quality_check_failed");
    a fresh request usually passes. The local VieNeu / Multilingual backend has
    no such per-request quality gate, so it's called once, directly.
    """
    if config.tts_backend == "vieneu" or local_tts_service.is_multilingual_voice(voice_id):
        return _synthesize_chunk_once_vieneu(config, text, voice_id, dest_path)

    attempt = 1
    while True:
        try:
            return _synthesize_chunk_once_resona(config, text, voice_id, language, dest_path)
        except Exception as exc:
            retryable = bool(_QUALITY_RETRY_RE.search(f"{getattr(exc, 'status', '')} {exc}"))
            if not retryable or attempt >= QUALITY_RETRIES:
                raise
            if log:
                log(f"quality check failed, retry {attempt}/{QUALITY_RETRIES - 1}")
            attempt += 1


def _synthesize_cue(config, cue, cue_index, voice_id, language, cue_dir):
    text = str(cue.get("text") or cue.get("translatedText") or "").strip()
    cue_dur = max(0.5, float(cue.get("end") or 0) - float(cue.get("start") or 0))
    # Estimate natural reading pace vs cue window so fast-talking videos are synthesized
    # briskly by the neural TTS engine itself instead of relying heavily on ffmpeg atempo
    chars_per_sec = len(text) / cue_dur
    base_speed = 1.0
    if chars_per_sec > 18:
        base_speed = min(1.30, round(chars_per_sec / 15.5, 2))
    elif chars_per_sec > 14:
        base_speed = 1.10

    cue_hash = hashlib.sha1(
        f"v3\n{voice_id}\n{language}\n{base_speed:.2f}\n{text}".encode("utf-8")
    ).hexdigest()
    final_cue_path = os.path.join(cue_dir, f"cue-{cue_hash}.wav")
    if os.path.exists(final_cue_path):
        return {"filePath": final_cue_path, "audioUrls": []}

    pieces = text_splitter.split_text_for_tts(text)
    files = []
    audio_urls = []
    for piece_index, piece in enumerate(pieces):
        part_base = os.path.join(cue_dir, f"cue-{cue_index}-{cue_hash}-part-{piece_index}")
        if config.tts_backend == "vieneu" or local_tts_service.is_multilingual_voice(voice_id):
            file_path = part_base + ".wav"
            local_tts_service.synthesize_to_file(
                piece, file_path, voice=voice_id, speed=base_speed, ffmpeg=config.ffmpeg_path
            )
            generated = {"filePath": file_path, "audioUrl": "", "requestId": ""}
        else:
            generated = _synthesize_chunk(config, piece, voice_id, language, part_base)
        files.append(generated["filePath"])
        audio_urls.append(generated["audioUrl"])

    raw_path = os.path.join(cue_dir, f"cue-{cue_hash}-raw.wav")
    merge_service.concat_audio_files(files, raw_path, config.ffmpeg_path)
    os.replace(raw_path, final_cue_path)
    for file in files:
        try:
            os.unlink(file)
        except OSError:
            pass
    return {"filePath": final_cue_path, "audioUrls": audio_urls}


def handle_tts_segment(worker, config, job_id, segment_index, voice_id, language=None):
    segment = db.get_segment(job_id, segment_index)
    if not segment:
        raise RuntimeError(f"Segment {segment_index} of job {job_id} not found")

    resolved_target, _, _ = _get_job_language_settings(config, job_id, target_language=language)
    seg_source_lang = segment.get("language") or "vi"

    raw_cues = _read_transcript_cues(segment)
    cues, tts_language = _prepare_cues_for_voice(raw_cues, seg_source_lang, resolved_target, voice_id)
    if segment.get("editedText"):
        cues = _apply_user_edited_text_to_cues(cues, segment.get("editedText"))

    text = (
        " ".join(cue["text"].strip() for cue in cues) if cues
        else (segment.get("editedText") or segment.get("transcriptText") or "")
    ).strip()

    if not text:
        db.update_segment(job_id, segment_index, status="tts_failed", error="Segment has no transcript text to send to TTS")
        worker.segment_changed.emit(job_id, segment_index)
        raise RuntimeError("Segment has no transcript text to send to TTS")

    seg_dir = segment_dir(config, job_id, segment_index)

    try:
        db.update_segment(job_id, segment_index, status="tts_processing", ttsVoiceId=voice_id, error="")
        worker.segment_changed.emit(job_id, segment_index)

        if segment.get("kind") == "video-chunk" and cues:
            cue_dir = os.path.join(seg_dir, "tts-cues")
            os.makedirs(cue_dir, exist_ok=True)
            # For local VieNeu / Multilingual TTS, synthesize per cue (only merging
            # tiny <12-char fragments that have <0.22s gap) so each subtitle cue
            # aligns accurately to its timestamp without spanning across pauses!
            if config.tts_backend == "vieneu" or local_tts_service.is_multilingual_voice(voice_id):
                tts_cues = text_splitter.group_cues_for_tts(cues, min_credits=12, max_credits=900, max_gap_sec=0.22)
            else:
                tts_cues = text_splitter.group_cues_for_tts(cues, min_credits=50, max_credits=1800, max_gap_sec=0.55)

            cue_audio_paths = []
            audio_urls = []

            for i, cue in enumerate(tts_cues):
                generated = _synthesize_cue(config, cue, i, voice_id, tts_language, cue_dir)
                cue_audio_paths.append(generated["filePath"])
                audio_urls.extend(generated["audioUrls"])
                db.update_segment(job_id, segment_index, ttsProgress=f"{i + 1}/{len(tts_cues)}")
                worker.segment_changed.emit(job_id, segment_index)

            segment_duration = max(0.0, (segment.get("endSec") or 0) - (segment.get("startSec") or 0))
            final_path = os.path.join(seg_dir, "tts-audio.wav")
            merge_service.create_timed_audio_track(
                cue_audio_paths, tts_cues, segment_duration, final_path,
                config.ffmpeg_path, config.ffprobe_path,
            )
            db.update_segment(
                job_id, segment_index, status="tts_done", ttsAudioPath=final_path,
                ttsAudioUrl=" | ".join(audio_urls), ttsProgress="", error="",
            )
            worker.segment_changed.emit(job_id, segment_index)
            return

        chunks = text_splitter.split_text_for_tts(text)
        chunk_files = []
        chunk_urls = []
        last_request_id = ""

        for i, chunk in enumerate(chunks):
            generated = _synthesize_chunk(
                config, chunk, voice_id, tts_language, os.path.join(seg_dir, f"tts-chunk-{i}")
            )
            chunk_files.append(generated["filePath"])
            chunk_urls.append(generated["audioUrl"])
            last_request_id = generated["requestId"]
            db.update_segment(
                job_id, segment_index, ttsRequestId=last_request_id,
                ttsProgress=f"{i + 1}/{len(chunks)}" if len(chunks) > 1 else "",
            )
            worker.segment_changed.emit(job_id, segment_index)

        final_path = os.path.join(seg_dir, "tts-audio.wav")
        merge_service.concat_audio_files(chunk_files, final_path, config.ffmpeg_path)
        if len(chunk_files) > 1:
            for f in chunk_files:
                try:
                    os.unlink(f)
                except OSError:
                    pass

        db.update_segment(
            job_id, segment_index, status="tts_done", ttsAudioPath=final_path,
            ttsAudioUrl=" | ".join(chunk_urls), ttsProgress="", error="",
        )
        worker.segment_changed.emit(job_id, segment_index)
    except Exception as exc:
        db.update_segment(job_id, segment_index, status="tts_failed", error=str(exc))
        worker.segment_changed.emit(job_id, segment_index)
        raise


# --- merge & full-project SRT -------------------------------------------------

def all_segments_for_job(job):
    """Every segment belonging to a job, regardless of type: a "video"
    project's segments live under its clips; an "audio" (Voice to Text) job's
    single segment is stored directly under the job's own id, as before.
    """
    if job.get("type") == "video":
        return project_segments_in_order(job["id"])
    return db.get_all_segments(job["id"])


def project_segments_in_order(project_id):
    """Flattens every clip's segments, in project order (clip orderIndex,
    then segment index) — the timeline the final video follows.
    """
    flattened = []
    for clip in db.list_clips(project_id):
        for seg in db.get_all_segments(clip["id"]):
            flattened.append({**seg, "clipId": clip["id"], "clipName": clip.get("originalFilename")})
    return flattened


def build_project_srt(config, project_id, mode=None, target_language=None):
    """Builds the combined SRT string across all segments in `project_id`.
    Supports `mode` = 'target' | 'bilingual' | 'original', and optional on-the-fly
    translation if `target_language` is specified and differs from existing cues.
    """
    job = db.get_job(project_id) or {}
    resolved_mode = mode or job.get("subMode") or getattr(config, "sub_mode", "target") or "target"
    resolved_target = target_language or job.get("targetLanguage") or getattr(config, "target_language", "vi") or "vi"

    segments = project_segments_in_order(project_id) if job.get("type") == "video" else db.get_all_segments(project_id)
    parts = []
    offset_seconds = 0.0

    for segment in segments:
        cues = _read_transcript_cues(segment)
        if cues:
            seg_lang = segment.get("language") or "en"
            # If user requested a specific target language for export that isn't cached yet
            if (
                target_language
                and target_language != "original"
                and not translation_service.is_same_language(seg_lang, resolved_target)
            ):
                needs_tx = any(
                    not c.get("translatedText")
                    or not translation_service.is_same_language(c.get("targetLanguage") or "vi", resolved_target)
                    for c in cues
                )
                if needs_tx:
                    orig_texts = [str(c.get("originalText") or c.get("text") or "").strip() for c in cues]
                    tx_texts = translation_service.translate_texts(orig_texts, seg_lang, resolved_target)
                    cues = [
                        {
                            **c,
                            "text": tx_texts[i] if i < len(tx_texts) else c["text"],
                            "translatedText": tx_texts[i] if i < len(tx_texts) else c.get("translatedText", ""),
                            "targetLanguage": resolved_target,
                        }
                        for i, c in enumerate(cues)
                    ]
            srt_content = srt_service.segments_to_srt(cues, mode=resolved_mode)
            parts.append({"content": srt_content, "offsetSeconds": offset_seconds})
        elif segment.get("srtPath") and os.path.exists(segment["srtPath"]):
            with open(segment["srtPath"], "r", encoding="utf-8") as f:
                parts.append({"content": f.read(), "offsetSeconds": offset_seconds})

        if segment.get("sourcePath") and os.path.exists(segment["sourcePath"]):
            offset_seconds += ffmpeg_service.get_duration_seconds(segment["sourcePath"], config.ffprobe_path)
        else:
            offset_seconds += max(0.0, (segment.get("endSec") or 0) - (segment.get("startSec") or 0))

    return srt_service.combine_srt_files(parts)


def handle_merge_project(worker, config, project_id, music_path):
    segments = project_segments_in_order(project_id)

    if not segments:
        message = "Project chưa có đoạn nào để ghép — thêm clip trước"
        db.update_job(project_id, finalVideoStatus="failed", error=message)
        worker.job_changed.emit(project_id)
        raise RuntimeError(message)

    not_ready = next((s for s in segments if s.get("status") != "tts_done" or not s.get("ttsAudioPath")), None)
    if not_ready:
        clip_label = not_ready.get("clipName") or not_ready["clipId"]
        message = f"{clip_label} — đoạn {not_ready['index'] + 1} chưa có giọng nói ({not_ready.get('status')})"
        db.update_job(project_id, finalVideoStatus="failed", error=message)
        worker.job_changed.emit(project_id)
        raise RuntimeError(message)

    try:
        db.update_job(project_id, finalVideoStatus="processing", error="")
        worker.job_changed.emit(project_id)

        out_root = job_output_dir(config, project_id)
        dubbed_dir = os.path.join(out_root, "dubbed")
        dubbed_paths = []
        voice_paths = []
        # one segment at a time, in timeline order — ffmpeg re-encoding is
        # CPU heavy, avoid overloading the box. Index by position in the
        # flattened timeline (not seg['index'], which restarts at 0 per clip
        # and would collide across clips).
        for i, seg in enumerate(segments):
            out_path = os.path.join(dubbed_dir, f"segment-{i:03d}.mp4")
            merge_service.mux_video_with_voice(seg["sourcePath"], seg["ttsAudioPath"], out_path, config.ffmpeg_path)
            dubbed_paths.append(out_path)
            if seg.get("ttsAudioPath") and os.path.exists(seg["ttsAudioPath"]):
                voice_paths.append(seg["ttsAudioPath"])

        # Also save a standalone combined voice track (`final-voice.wav`) so callers
        # can download just the dubbed voice audio if desired
        if voice_paths:
            final_voice_wav = os.path.join(out_root, "final-voice.wav")
            try:
                merge_service.concat_audio_files(voice_paths, final_voice_wav, config.ffmpeg_path)
            except Exception:
                pass

        concatenated_path = os.path.join(out_root, "final-dubbed-base.mp4")
        merge_service.concat_videos(dubbed_paths, concatenated_path, config.ffmpeg_path)

        with_music_path = os.path.join(out_root, "final-dubbed-music.mp4")
        if music_path:
            merge_service.add_background_music(concatenated_path, music_path, with_music_path, config.ffmpeg_path)
            try:
                os.unlink(concatenated_path)
            except OSError:
                pass
            current_video_path = with_music_path
        else:
            current_video_path = concatenated_path

        job = db.get_job(project_id) or {}
        final_path = os.path.join(out_root, "final-dubbed.mp4")

        # Always save the combined full-project SRT alongside final-dubbed.mp4
        sub_mode = job.get("subMode") or getattr(config, "sub_mode", "target") or "target"
        full_srt_content = build_project_srt(config, project_id, mode=sub_mode)
        full_srt_path = os.path.join(out_root, "final-dubbed.srt")
        if full_srt_content.strip():
            with open(full_srt_path, "w", encoding="utf-8") as f:
                f.write(full_srt_content)

        if job.get("burnSubtitles") and full_srt_content.strip():
            merge_service.burn_subtitles_into_video(
                current_video_path, full_srt_path, final_path, config.ffmpeg_path
            )
            if current_video_path != final_path and os.path.exists(current_video_path):
                try:
                    os.unlink(current_video_path)
                except OSError:
                    pass
        else:
            shutil.move(current_video_path, final_path)

        db.update_job(project_id, finalVideoStatus="done", finalVideoPath=final_path)
        worker.job_changed.emit(project_id)
        _notify_webhook(project_id)
    except Exception as exc:
        db.update_job(project_id, finalVideoStatus="failed", error=str(exc))
        worker.job_changed.emit(project_id)
        _notify_webhook(project_id)
        raise


# --- "Bắt đầu" — run the whole project end to end -----------------------------------------------------

def handle_start_project(worker, config, project_id, voice_id, language=None):
    """Cut every clip, transcribe every segment, generate voice for every
    segment with `voice_id`, then merge — all in one go. Runs synchronously,
    step by step, directly on the worker thread.
    """
    resolved_target, resolved_mode, _ = _get_job_language_settings(
        config, project_id, target_language=language
    )
    voice_id = resolve_default_voice(voice_id, resolved_target)
    if local_tts_service.is_multilingual_voice(voice_id) and resolved_target == "vi":
        v_lang = local_tts_service.voice_language(voice_id, default=resolved_target)
        if v_lang != "vi":
            resolved_target = v_lang
            db.update_job(project_id, targetLanguage=resolved_target)

    db.update_job(project_id, stage="cutting", error="")
    worker.job_changed.emit(project_id)

    clips = db.list_clips(project_id)
    if not clips:
        message = "Project chưa có clip nào — thêm video rồi bấm Bắt đầu"
        db.update_job(project_id, stage="failed", error=message)
        worker.job_changed.emit(project_id)
        raise RuntimeError(message)

    had_error = False
    for clip in clips:
        if clip.get("stage") == "ready":
            continue
        try:
            _cut_clip(worker, config, clip["id"], clip["filePath"])
        except Exception:
            had_error = True

    db.update_job(project_id, stage="transcribing")
    worker.job_changed.emit(project_id)

    for clip in db.list_clips(project_id):
        for seg in db.get_all_segments(clip["id"]):
            if seg.get("status") in ("transcribed", "tts_queued", "tts_processing", "tts_done"):
                # Ensure already-transcribed segments are translated to `resolved_target`
                try:
                    handle_retranslate_segment(
                        worker, config, clip["id"], seg["index"],
                        target_language=resolved_target, sub_mode=resolved_mode,
                    )
                except Exception:
                    had_error = True
                continue
            try:
                handle_transcribe_segment(
                    worker, config, clip["id"], seg["index"],
                    target_language=resolved_target, sub_mode=resolved_mode,
                )
            except Exception:
                had_error = True

    db.update_job(project_id, stage="generating_voice")
    worker.job_changed.emit(project_id)

    for clip in db.list_clips(project_id):
        for seg in db.get_all_segments(clip["id"]):
            if seg.get("status") not in ("transcribed", "tts_failed", "tts_done"):
                continue  # couldn't transcribe this one — nothing to send to TTS
            try:
                handle_tts_segment(worker, config, clip["id"], seg["index"], voice_id, resolved_target)
            except Exception:
                had_error = True

    db.update_job(project_id, stage="failed" if had_error else "ready")
    worker.job_changed.emit(project_id)

    music_path = (db.get_job(project_id) or {}).get("backgroundMusicPath")
    handle_merge_project(worker, config, project_id, music_path)


# --- Standalone Text-to-Speech (Text -> Voice Audio) --------------------------

def synthesize_long_text_to_file(
    config,
    text: str,
    voice_id: str,
    output_path: str,
    speed: float = 1.0,
    output_format: str = "mp3",
    on_progress=None,
) -> str:
    """Splits arbitrarily long `text` into sentence chunks, synthesizes each chunk
    with `voice_id`, concatenates them into a clean track, and exports as `.mp3` or `.wav`.
    """
    import subprocess
    import tempfile

    cleaned = (text or "").strip()
    if not cleaned:
        raise RuntimeError("Text is empty")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    chunks = text_splitter.split_text_for_tts(cleaned)
    if not chunks:
        chunks = [cleaned]

    with tempfile.TemporaryDirectory(prefix="kingvoice_tts_") as tmp_dir:
        chunk_wavs = []
        for idx, chunk in enumerate(chunks):
            part_wav = os.path.join(tmp_dir, f"chunk-{idx:04d}.wav")
            local_tts_service.synthesize_to_file(
                chunk, part_wav, voice=voice_id, speed=speed, ffmpeg=config.ffmpeg_path
            )
            chunk_wavs.append(part_wav)
            if on_progress:
                on_progress(idx + 1, len(chunks))

        combined_wav = os.path.join(tmp_dir, "combined.wav")
        merge_service.concat_audio_files(chunk_wavs, combined_wav, config.ffmpeg_path)

        fmt = (output_format or "mp3").lower().strip(".")
        if fmt == "mp3":
            res = subprocess.run(
                [
                    config.ffmpeg_path, "-y",
                    "-i", combined_wav,
                    "-c:a", "libmp3lame",
                    "-b:a", "192k",
                    output_path,
                ],
                capture_output=True,
                text=True,
            )
            if res.returncode != 0:
                shutil.copyfile(combined_wav, output_path)
        else:
            shutil.copyfile(combined_wav, output_path)

    return output_path


def handle_tts_standalone_job(
    worker,
    config,
    job_id: str,
    text: str,
    voice_id: str,
    target_language: str = "vi",
    translate_text: bool = False,
    speed: float = 1.0,
    output_format: str = "mp3",
):
    """Background task for Text -> Voice jobs (`type='tts'`).
    Optionally translates `text` into `target_language` before synthesizing speech,
    tracks chunk progress in `segments`, saves final audio to `finalVideoPath`,
    and triggers webhook when done.
    """
    out_root = job_output_dir(config, job_id)
    os.makedirs(out_root, exist_ok=True)
    ext = "mp3" if str(output_format or "mp3").lower().strip(".") == "mp3" else "wav"
    final_audio_path = os.path.join(out_root, f"voice-output.{ext}")

    try:
        db.update_job(job_id, stage="generating_voice", finalVideoStatus="processing", error="")
        worker.job_changed.emit(job_id)

        spoken_text = (text or "").strip()
        if translate_text and target_language and target_language != "original":
            db.update_segment(job_id, 0, status="transcribing", error="")
            worker.segment_changed.emit(job_id, 0)
            tx_list = translation_service.translate_texts([spoken_text], "auto", target_language)
            if tx_list and tx_list[0].strip():
                spoken_text = tx_list[0].strip()

        db.update_segment(
            job_id, 0,
            status="tts_processing",
            transcriptText=spoken_text,
            ttsVoiceId=voice_id,
            error="",
        )
        worker.segment_changed.emit(job_id, 0)

        def _report(done_idx, total_cnt):
            db.update_segment(job_id, 0, ttsProgress=f"{done_idx}/{total_cnt}")
            worker.segment_changed.emit(job_id, 0)

        synthesize_long_text_to_file(
            config,
            text=spoken_text,
            voice_id=voice_id,
            output_path=final_audio_path,
            speed=speed,
            output_format=ext,
            on_progress=_report,
        )

        dur = 0.0
        try:
            dur = ffmpeg_service.get_duration_seconds(final_audio_path, config.ffprobe_path)
        except Exception:
            pass

        db.update_segment(
            job_id, 0,
            status="tts_done",
            ttsAudioPath=final_audio_path,
            endSec=dur,
            ttsProgress="",
            error="",
        )
        db.update_job(
            job_id,
            stage="ready",
            durationSeconds=dur,
            finalVideoStatus="done",
            finalVideoPath=final_audio_path,
            error="",
        )
        worker.segment_changed.emit(job_id, 0)
        worker.job_changed.emit(job_id)
        _notify_webhook(job_id)
    except Exception as exc:
        db.update_segment(job_id, 0, status="tts_failed", error=str(exc))
        db.update_job(job_id, stage="failed", finalVideoStatus="failed", error=str(exc))
        worker.segment_changed.emit(job_id, 0)
        worker.job_changed.emit(job_id)
        _notify_webhook(job_id)
        raise


def handle_audio_dub_job(
    worker,
    config,
    job_id: str,
    voice_id: str,
    target_language: str = "vi",
    sub_mode: str = "target",
):
    """Automatic Speech-to-Voice pipeline:
    1. Extracts audio from input audio/video file & recognizes speech with Faster-Whisper
    2. Translates transcript to `target_language` & writes `.srt`
    3. Synthesizes new AI voice (`voice_id`) from the recognized/translated speech
    4. Saves the generated voice audio to `finalVideoPath` so caller can download `.wav`/`.mp3` + `.srt`.
    """
    resolved_voice = resolve_default_voice(voice_id, target_language)
    try:
        db.update_job(job_id, stage="transcribing", finalVideoStatus="processing", error="")
        worker.job_changed.emit(job_id)

        handle_transcribe_segment(
            worker, config, job_id, 0, target_language=target_language, sub_mode=sub_mode
        )

        db.update_job(job_id, stage="generating_voice")
        worker.job_changed.emit(job_id)

        handle_tts_segment(
            worker, config, job_id, 0, resolved_voice, language=target_language
        )

        seg = db.get_segment(job_id, 0) or {}
        tts_audio = seg.get("ttsAudioPath")
        db.update_job(
            job_id,
            stage="ready",
            finalVideoStatus="done" if tts_audio else "failed",
            finalVideoPath=tts_audio,
            error="" if tts_audio else "Failed to generate voice audio",
        )
        worker.job_changed.emit(job_id)
        _notify_webhook(job_id)
    except Exception as exc:
        db.update_job(job_id, stage="failed", finalVideoStatus="failed", error=str(exc))
        worker.job_changed.emit(job_id)
        _notify_webhook(job_id)
        raise
