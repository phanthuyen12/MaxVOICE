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
        "-filter_complex", "[1:a]apad,volume=1.0[aout]",
        "-map", "0:v:0",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "128k",
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
    stream_refs = "".join(f"[{i}:a]" for i in range(len(audio_paths)))
    filter_arg = f"{stream_refs}concat=n={len(audio_paths)}:v=0:a=1[aout]"

    _run([
        ffmpeg, "-y",
        *input_args,
        "-filter_complex", filter_arg,
        "-map", "[aout]",
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


def _atempo_filters(speed):
    factors = []
    remaining = speed
    while remaining > 2:
        factors.append(2)
        remaining /= 2
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    factors.append(remaining)
    return ",".join(f"atempo={factor:.5f}" for factor in factors)


def create_timed_audio_track(cue_audio_paths, cues, duration_seconds, output_path, ffmpeg="ffmpeg"):
    """Places each generated subtitle voice at its SRT start time and fits it to
    that cue's duration. The resulting WAV has exactly the source segment length.
    """
    if not cue_audio_paths or len(cue_audio_paths) != len(cues):
        raise RuntimeError("Thiếu audio hoặc mốc SRT để ghép giọng nói theo timeline")
    if not (duration_seconds and duration_seconds > 0):
        raise RuntimeError("Độ dài đoạn video không hợp lệ")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    inputs = ["-f", "lavfi", "-t", str(duration_seconds), "-i", "anullsrc=r=48000:cl=stereo"]
    for audio_path in cue_audio_paths:
        inputs += ["-i", audio_path]

    filters = [f"[0:a]atrim=duration={duration_seconds},asetpts=PTS-STARTPTS[base]"]
    cue_refs = ["[base]"]
    for index, cue in enumerate(cues):
        cue_start = max(0.0, float(cue.get("start") or 0))
        cue_end = min(duration_seconds, float(cue.get("end") or 0))
        cue_duration = cue_end - cue_start
        if cue_duration <= 0:
            raise RuntimeError(f"Mốc SRT {index + 1} không hợp lệ")

        source_duration = get_duration_seconds(cue_audio_paths[index])
        if not (source_duration and source_duration > 0):
            raise RuntimeError(f"Audio của phụ đề {index + 1} không có thời lượng hợp lệ")
        speed = source_duration / cue_duration
        delay_ms = round(cue_start * 1000)
        ref = f"[cue{index}]"
        filters.append(
            f"[{index + 1}:a]aresample=48000,{_atempo_filters(speed)},apad,atrim=duration={cue_duration},"
            f"asetpts=PTS-STARTPTS,adelay={delay_ms}|{delay_ms}{ref}"
        )
        cue_refs.append(ref)
    filters.append(
        f"{''.join(cue_refs)}amix=inputs={len(cue_refs)}:duration=first:dropout_transition=0:normalize=0[aout]"
    )

    _run([
        ffmpeg, "-y",
        *inputs,
        "-filter_complex", ";".join(filters),
        "-map", "[aout]",
        "-t", str(duration_seconds),
        "-ar", "48000",
        "-ac", "2",
        "-c:a", "pcm_s16le",
        output_path,
    ])
    return output_path
