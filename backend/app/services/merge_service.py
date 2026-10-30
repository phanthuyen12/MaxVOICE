import os
import shutil
import subprocess

from .ffmpeg_service import get_duration_seconds


def _run(args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {result.stderr.strip() or result.returncode}")
    return result.stdout


def mux_video_with_voice(video_path, tts_audio_path, output_path, ffmpeg="ffmpeg"):
    """Replaces the source video's audio with the generated voice."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    _run([
        ffmpeg, "-y",
        "-i", video_path,
        "-i", tts_audio_path,
        "-filter_complex", "[1:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,apad,volume=1.0[aout]",
        "-map", "0:v:0",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        output_path,
    ])
    return output_path


def concat_audio_files(audio_paths, output_path, ffmpeg="ffmpeg"):
    """Concatenates several audio files (e.g. per-chunk TTS output from splitting
    a long transcript across multiple Resona requests) into one track, in
    order. Uses the `concat` filter (not the concat demuxer) so it doesn't
    care whether inputs share the same codec/container.
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    if len(audio_paths) == 1:
        shutil.copyfile(audio_paths[0], output_path)
        return output_path

    input_args = []
    for p in audio_paths:
        input_args += ["-i", p]
    stream_refs = "".join(
        f"[{i}:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a{i}];"
        for i in range(len(audio_paths))
    )
    concat_inputs = "".join(f"[a{i}]" for i in range(len(audio_paths)))
    filter_arg = f"{stream_refs}{concat_inputs}concat=n={len(audio_paths)}:v=0:a=1[aout]"

    _run([
        ffmpeg, "-y",
        *input_args,
        "-filter_complex", filter_arg,
        "-map", "[aout]",
        "-ar", "48000",
        "-ac", "2",
        "-c:a", "pcm_s16le",
        output_path,
    ])
    return output_path


def concat_videos(segment_paths, output_path, ffmpeg="ffmpeg"):
    """Concatenates dubbed segments (same codecs, produced by mux_video_with_voice)
    into one final video, in order, via ffmpeg's concat demuxer.
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    list_path = output_path + ".list.txt"
    list_content = "\n".join(
        "file '{}'".format(p.replace("'", "'\\''")) for p in segment_paths
    )
    with open(list_path, "w", encoding="utf-8") as f:
        f.write(list_content)

    try:
        _run([
            ffmpeg, "-y",
            "-f", "concat", "-safe", "0",
            "-i", list_path,
            "-c", "copy",
            output_path,
        ])
    finally:
        try:
            os.unlink(list_path)
        except OSError:
            pass
    return output_path


def add_background_music(video_path, music_path, output_path, ffmpeg="ffmpeg"):
    """Mixes uploaded music under the dubbed voice. MP4/MOV files are treated as
    audio sources; their video streams are ignored. Music loops or trims to fit.
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    _run([
        ffmpeg, "-y",
        "-i", video_path,
        "-stream_loop", "-1",
        "-i", music_path,
        "-filter_complex",
        "[1:a]volume=0.18[music];[0:a][music]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]",
        "-map", "0:v:0",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        output_path,
    ])
    return output_path


def _escape_subtitles_filter_path(path):
    """Escapes file path for ffmpeg's `subtitles='...'` filter across macOS/Linux/Windows."""
    escaped = path.replace("\\", "/")
    escaped = escaped.replace(":", "\\:")
    escaped = escaped.replace("'", "'\\\\\\''")
    escaped = escaped.replace("[", "\\[").replace("]", "\\]")
    return escaped


def burn_subtitles_into_video(video_path, srt_path, output_path, ffmpeg="ffmpeg"):
    """Burns SRT subtitles directly into the video stream with clean readable styling."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    escaped_srt = _escape_subtitles_filter_path(srt_path)
    style = "FontSize=18,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H64000000,BorderStyle=3,Outline=1,Shadow=0,MarginV=22"
    vf = f"subtitles='{escaped_srt}':force_style='{style}'"
    try:
        _run([
            ffmpeg, "-y",
            "-i", video_path,
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "20",
            "-c:a", "copy",
            output_path,
        ])
    except Exception:
        # Fallback if ffmpeg was built without libass (`subtitles` filter): attach soft subtitles instead
        _run([
            ffmpeg, "-y",
            "-i", video_path,
            "-i", srt_path,
            "-map", "0:v:0",
            "-map", "0:a:0",
            "-map", "1:0",
            "-c:v", "copy",
            "-c:a", "copy",
            "-c:s", "mov_text",
            output_path,
        ])
    return output_path


def _atempo_filters(speed):
    factors = []
    remaining = speed
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    if abs(remaining - 1.0) > 0.01:
        factors.append(remaining)
    if not factors:
        return ""
    return ",".join(f"atempo={factor:.5f}" for factor in factors)


def create_timed_audio_track(cue_audio_paths, cues, duration_seconds, output_path, ffmpeg="ffmpeg", ffprobe="ffprobe"):
    """Places each generated subtitle voice at its SRT start time and fits it
    naturally into the timeline without ever dragging/slowing down speech.

    Key fixes against buzzing / slow-motion ("rè rề") voice artifacts:
    1. NEVER slow down speech (`speed < 1.0` is clamped to `1.0`). When a synthesized
       cue is shorter than the subtitle window, it plays at natural 1.0x speed and is
       followed by clean silence until the next cue.
    2. Use available silent space up to `next_cue_start` before speeding up a cue,
       so translated sentences with slightly more syllables don't get unnecessarily
       rushed.
    3. Cap maximum speed-up (`MAX_ATEMPO_SPEED = 1.38`) so even dense cues remain
       clear and natural without phase-vocoder buzzing.
    4. Apply consistent 48kHz stereo float formatting + tiny 12ms fade-in/out per cue
       to eliminate boundary clicks/pops.
    """
    if not cue_audio_paths or len(cue_audio_paths) != len(cues):
        raise RuntimeError("Thiếu audio hoặc mốc SRT để ghép giọng nói theo timeline")
    if not (duration_seconds and duration_seconds > 0):
        raise RuntimeError("Độ dài đoạn video không hợp lệ")

    MAX_ATEMPO_SPEED = 1.52

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    inputs = ["-f", "lavfi", "-t", f"{duration_seconds:.4f}", "-i", "anullsrc=r=48000:cl=stereo"]
    for audio_path in cue_audio_paths:
        inputs += ["-i", audio_path]

    filters = [
        f"[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
        f"atrim=duration={duration_seconds:.4f},asetpts=PTS-STARTPTS[base]"
    ]
    cue_refs = ["[base]"]

    for index, cue in enumerate(cues):
        cue_start = max(0.0, float(cue.get("start") or 0.0))
        cue_end = min(duration_seconds, float(cue.get("end") or 0.0))
        if cue_end <= cue_start:
            cue_end = min(duration_seconds, cue_start + 0.25)
        cue_duration = cue_end - cue_start
        if cue_duration <= 0:
            continue

        # Allow speech to naturally flow into trailing silence before the next cue starts
        if index + 1 < len(cues):
            next_start = max(cue_end, min(duration_seconds, float(cues[index + 1].get("start") or cue_end)))
            available_end = max(cue_end, next_start - 0.03)
        else:
            available_end = duration_seconds
        available_duration = max(cue_duration, available_end - cue_start)

        source_duration = get_duration_seconds(cue_audio_paths[index], ffprobe)
        if not (source_duration and source_duration > 0):
            raise RuntimeError(f"Audio của phụ đề {index + 1} không có thời lượng hợp lệ")

        # NEVER slow down audio (speed < 1.0 causes severe robotic/dragging distortion).
        if source_duration <= available_duration * 1.02:
            speed = 1.0
            effective_duration = min(source_duration, available_duration)
        else:
            raw_speed = source_duration / available_duration
            speed = min(raw_speed, MAX_ATEMPO_SPEED)
            effective_duration = min(source_duration / speed, available_duration)

        delay_ms = max(0, round(cue_start * 1000))
        ref = f"[cue{index}]"

        chain = [
            "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo",
            "highpass=f=55",
        ]
        atempo_str = _atempo_filters(speed)
        if atempo_str:
            chain.append(atempo_str)

        fade_dur = min(0.012, effective_duration * 0.15)
        if fade_dur >= 0.004:
            fade_out_start = max(0.0, effective_duration - fade_dur)
            chain.append(f"afade=t=in:st=0:d={fade_dur:.4f}")
            chain.append(f"afade=t=out:st={fade_out_start:.4f}:d={fade_dur:.4f}")

        chain.extend([
            f"atrim=duration={available_duration:.4f}",
            "asetpts=PTS-STARTPTS",
            f"adelay={delay_ms}|{delay_ms}",
        ])
        filters.append(f"[{index + 1}:a]{','.join(chain)}{ref}")
        cue_refs.append(ref)

    filters.append(
        f"{''.join(cue_refs)}amix=inputs={len(cue_refs)}:duration=first:dropout_transition=0:normalize=0,"
        f"alimiter=limit=0.96:attack=5:release=50:level=disabled[aout]"
    )

    _run([
        ffmpeg, "-y",
        *inputs,
        "-filter_complex", ";".join(filters),
        "-map", "[aout]",
        "-t", f"{duration_seconds:.4f}",
        "-ar", "48000",
        "-ac", "2",
        "-c:a", "pcm_s16le",
        output_path,
    ])
    return output_path
