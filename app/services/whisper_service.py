import threading
import wave

import numpy as np

from .cpu_lock import heavy_inference_lock

# Loads the model once per (size, device, compute_type) combination and keeps
# it in memory — the original Node backend shelled out to a fresh Python
# process per segment; here we're already in Python, so load once and reuse.
_model_cache = {}
_model_lock = threading.Lock()


def _load_wav_as_float32(path):
    """Reads a 16-bit PCM WAV file into a float32 numpy array in [-1, 1].

    Bypasses faster-whisper's PyAV-based decode_audio() entirely — we always
    feed it a WAV produced by ffmpeg (16kHz mono), so decoding it ourselves
    avoids depending on a PyAV version matching what this faster-whisper
    release expects.
    """
    with wave.open(path, "rb") as wf:
        if wf.getsampwidth() != 2:
            raise ValueError(f"Expected 16-bit PCM WAV, got sampwidth={wf.getsampwidth()}")
        raw = wf.readframes(wf.getnframes())
        channels = wf.getnchannels()

    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio


def _get_model(model_size, device, compute_type):
    key = (model_size, device, compute_type)
    model = _model_cache.get(key)
    if model is None:
        with _model_lock:
            model = _model_cache.get(key)  # re-check: another thread may have loaded it first
            if model is None:
                from faster_whisper import WhisperModel

                model = WhisperModel(model_size, device=device, compute_type=compute_type)
                _model_cache[key] = model
    return model


def transcribe(audio_path, model_size="small", device="cpu", compute_type="int8"):
    """Transcribes a 16kHz mono WAV file with faster-whisper.

    Returns {"language": str, "language_probability": float, "segments": [{start, end, text}]}.
    """
    model = _get_model(model_size, device, compute_type)
    audio = _load_wav_as_float32(audio_path)

    with heavy_inference_lock:
        segments_iter, info = model.transcribe(audio, vad_filter=True)
        segments = [
            {"start": round(s.start, 3), "end": round(s.end, 3), "text": s.text.strip()}
            for s in segments_iter
        ]

    return {
        "language": info.language,
        "language_probability": round(info.language_probability, 3),
        "segments": segments,
    }
