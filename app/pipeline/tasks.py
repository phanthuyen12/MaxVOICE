import hashlib
import json
import os
import re
import shutil

from .. import db
from ..paths import chunks_dir, job_output_dir, segment_dir
from ..services import ffmpeg_service, local_tts_service, merge_service, resona_service, srt_service
from ..services import text_splitter, translation_service, whisper_service

QUALITY_RETRIES = 6
_QUALITY_RETRY_RE = re.compile(
    r"quality[\s_-]*check[\s_-]*failed|pronunciation|none of the generated takes passed",
    re.IGNORECASE,
)


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


# --- transcription -----------------------------------------------------------

def handle_transcribe_segment(worker, config, job_id, segment_index):
    segment = db.get_segment(job_id, segment_index)
    if not segment:
        raise RuntimeError(f"Segment {segment_index} of job {job_id} not found")

    seg_dir = segment_dir(config, job_id, segment_index)
    os.makedirs(seg_dir, exist_ok=True)
    audio_path = os.path.join(seg_dir, "audio.wav")

    try:
        db.update_segment(job_id, segment_index, status="extracting_audio")
        worker.segment_changed.emit(job_id, segment_index)
        ffmpeg_service.extract_audio_wav(segment["sourcePath"], audio_path, config.ffmpeg_path)

        db.update_segment(job_id, segment_index, status="transcribing")
        worker.segment_changed.emit(job_id, segment_index)
        result = whisper_service.transcribe(
            audio_path, config.whisper_model, config.whisper_device, config.whisper_compute_type
        )
        language = result["language"]
        segments = result["segments"]

        # Keep the recognized source text, while using Vietnamese translations
        # for generated subtitles and dubbing whenever the source is another
        # language.
        translated_segments = segments
        if language and not re.match(r"^vi(?:[-_]|$)", language, re.IGNORECASE):
            try:
                translated_texts = translation_service.translate_texts_to_vietnamese(
                    [cue["text"] for cue in segments], language
                )
                translated_segments = [
                    {**cue, "translatedText": translated_texts[i]} for i, cue in enumerate(segments)
                ]
            except Exception as exc:
                raise RuntimeError(
                    f"Không dịch được phụ đề từ {language} sang tiếng Việt; chưa thể tạo voice: {exc}"
                )

        display_segments = [
            {**cue, "text": cue.get("translatedText") or cue["text"]} for cue in translated_segments
        ]
        transcript_text = srt_service.segments_to_plain_text(display_segments)
        srt_content = srt_service.segments_to_srt(display_segments)
        srt_path = os.path.join(seg_dir, "subtitle.srt")
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(srt_content)

        cues = []
        for cue in translated_segments:
            entry = {"start": cue["start"], "end": cue["end"], "text": cue.get("translatedText") or cue["text"]}
            if cue.get("translatedText"):
                entry["translatedText"] = cue["translatedText"]
                entry["originalText"] = cue["text"]
            cues.append(entry)

        db.update_segment(
            job_id, segment_index,
            audioPath=audio_path, language=language, transcriptText=transcript_text,
            transcriptCues=json.dumps(cues, ensure_ascii=False), srtPath=srt_path, status="transcribed",
        )
        worker.segment_changed.emit(job_id, segment_index)
    except Exception as exc:
        db.update_segment(job_id, segment_index, status="failed", error=str(exc))
        worker.segment_changed.emit(job_id, segment_index)
        raise


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
            text = str(cue.get("text") or "").strip()
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


def _synthesize_chunk_once_vieneu(config, text, voice_id, dest_path):
    """Local, offline synthesis via the `vieneu` package — no network, no
    per-character cost. `voice_id` is a preset voice name (e.g. "Hải Đăng")
    or the name of a voice previously cloned via local_tts_service.clone_voice.
    """
    audio = local_tts_service.synthesize(text, voice=voice_id)
    file_path = dest_path + ".wav"
    local_tts_service.save_wav(audio, file_path)
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
    a fresh request usually passes. The local VieNeu backend has no such
    per-request quality gate, so it's called once, directly.
    """
    if config.tts_backend == "vieneu":
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
    text = str(cue.get("translatedText") or cue["text"]).strip()
    cue_hash = hashlib.sha1(f"{voice_id}\n{language}\n{text}".encode("utf-8")).hexdigest()
    final_cue_path = os.path.join(cue_dir, f"cue-{cue_hash}.wav")
    if os.path.exists(final_cue_path):
        return {"filePath": final_cue_path, "audioUrls": []}

    pieces = text_splitter.split_text_for_tts(text)
    files = []
    audio_urls = []
    for piece_index, piece in enumerate(pieces):
        generated = _synthesize_chunk(
            config, piece, voice_id, language,
            os.path.join(cue_dir, f"cue-{cue_index}-{cue_hash}-part-{piece_index}"),
        )
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


def handle_tts_segment(worker, config, job_id, segment_index, voice_id, language):
    segment = db.get_segment(job_id, segment_index)
    if not segment:
        raise RuntimeError(f"Segment {segment_index} of job {job_id} not found")

    raw_cues = _read_transcript_cues(segment)
    cues = [{**cue, "text": cue.get("translatedText") or cue["text"]} for cue in raw_cues]
    tts_language = "vi" if any(cue.get("translatedText") for cue in cues) else language
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
            # Resona rejects any single request under 50 voice credits, and a
            # lot of individual subtitle cues (a few words, a few seconds)
            # fall under that on their own — group adjacent cues so every
            # request we send clears it.
            tts_cues = text_splitter.group_cues_for_tts(cues)
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
                cue_audio_paths, tts_cues, segment_duration, final_path, config.ffmpeg_path
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


# --- merge -----------------------------------------------------------

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

        dubbed_dir = os.path.join(job_output_dir(config, project_id), "dubbed")
        dubbed_paths = []
        # one segment at a time, in timeline order — ffmpeg re-encoding is
        # CPU heavy, avoid overloading the box. Index by position in the
        # flattened timeline (not seg['index'], which restarts at 0 per clip
        # and would collide across clips).
        for i, seg in enumerate(segments):
            out_path = os.path.join(dubbed_dir, f"segment-{i:03d}.mp4")
            merge_service.mux_video_with_voice(seg["sourcePath"], seg["ttsAudioPath"], out_path, config.ffmpeg_path)
            dubbed_paths.append(out_path)

        concatenated_path = os.path.join(job_output_dir(config, project_id), "final-dubbed-base.mp4")
        merge_service.concat_videos(dubbed_paths, concatenated_path, config.ffmpeg_path)

        final_path = os.path.join(job_output_dir(config, project_id), "final-dubbed.mp4")
        if music_path:
            merge_service.add_background_music(concatenated_path, music_path, final_path, config.ffmpeg_path)
            try:
                os.unlink(concatenated_path)
            except OSError:
                pass
        else:
            shutil.move(concatenated_path, final_path)

        db.update_job(project_id, finalVideoStatus="done", finalVideoPath=final_path)
        worker.job_changed.emit(project_id)
    except Exception as exc:
        db.update_job(project_id, finalVideoStatus="failed", error=str(exc))
        worker.job_changed.emit(project_id)
        raise


# --- "Bắt đầu" — run the whole project end to end -----------------------------------------------------

def handle_start_project(worker, config, project_id, voice_id, language="vi"):
    """Cut every clip, transcribe every segment, generate voice for every
    segment with `voice_id`, then merge — all in one go. Runs synchronously,
    step by step, directly on the worker thread: this task IS the single
    worker thread's current job, so it cannot `worker.submit(...)` further
    steps and then wait for them (nothing else would ever run to complete
    them — the queue only has the one thread this task is occupying).
    """
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
                continue
            try:
                handle_transcribe_segment(worker, config, clip["id"], seg["index"])
            except Exception:
                had_error = True

    db.update_job(project_id, stage="generating_voice")
    worker.job_changed.emit(project_id)

    for clip in db.list_clips(project_id):
        for seg in db.get_all_segments(clip["id"]):
            if seg.get("status") == "tts_done":
                continue
            if seg.get("status") not in ("transcribed", "tts_failed"):
                continue  # couldn't transcribe this one — nothing to send to TTS
            try:
                handle_tts_segment(worker, config, clip["id"], seg["index"], voice_id, language)
            except Exception:
                had_error = True

    db.update_job(project_id, stage="failed" if had_error else "ready")
    worker.job_changed.emit(project_id)

    music_path = (db.get_job(project_id) or {}).get("backgroundMusicPath")
    handle_merge_project(worker, config, project_id, music_path)
