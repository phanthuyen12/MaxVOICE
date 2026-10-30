import os
import subprocess


def _run(args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {result.stderr.strip() or result.returncode}")
    return result.stdout


def check_binary(path):
    """Raises if `path -version` can't be run — used by the Settings dialog
    to confirm the configured ffmpeg/ffprobe paths actually work.
    """
    return _run([path, "-version"])


def get_duration_seconds(path, ffprobe="ffprobe"):
    out = _run([
        ffprobe, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path,
    ])
    try:
        return float(out.strip())
    except ValueError:
        raise RuntimeError(f"Could not read duration for {path}")


def has_audio_stream(path, ffprobe="ffprobe"):
    out = _run([
        ffprobe, "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=index", "-of", "csv=p=0", path,
    ])
    return len(out.strip()) > 0


def split_video_into_segments(input_path, output_dir, segment_seconds, ffmpeg="ffmpeg"):
    """Splits a video into fixed-length chunks without re-encoding (stream copy),
    so segment boundaries snap to the nearest keyframe rather than being exact.
    Returns the list of generated chunk file paths, in order.
    """
    os.makedirs(output_dir, exist_ok=True)
    pattern = os.path.join(output_dir, "segment-%03d.mp4")

    _run([
        ffmpeg, "-y", "-i", input_path,
        "-map", "0", "-c", "copy", "-f", "segment",
        "-segment_time", str(segment_seconds), "-reset_timestamps", "1",
        pattern,
    ])

    files = sorted(
        f for f in os.listdir(output_dir) if f.startswith("segment-") and f.endswith(".mp4")
    )
    return [os.path.join(output_dir, f) for f in files]


def extract_audio_wav(input_path, output_wav_path, ffmpeg="ffmpeg"):
    """Extracts a 16kHz mono WAV from any audio/video file — the format faster-whisper expects."""
    os.makedirs(os.path.dirname(output_wav_path), exist_ok=True)
    _run([
        ffmpeg, "-y", "-i", input_path,
        "-vn", "-ac", "1", "-ar", "16000", "-f", "wav",
        output_wav_path,
    ])
    return output_wav_path
