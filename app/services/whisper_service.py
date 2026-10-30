import re
import threading
import wave

import numpy as np

from .cpu_lock import heavy_inference_lock

# Loads the model once per (size, device, compute_type) combination and keeps
# it in memory — the original Node backend shelled out to a fresh Python
# process per segment; here we're already in Python, so load once and reuse.
_model_cache = {}
_model_lock = threading.Lock()

_PUNCT_END_RE = re.compile(r'[.!?…。！？]["\'\)\]]*$')
_CLAUSE_END_RE = re.compile(r'[,;:—\-]["\'\)\]]*$')


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


def _regroup_words_into_natural_cues(raw_segments, max_duration=7.5, max_chars=115, pause_threshold=0.38):
    """Regroups Whisper word-level timestamps into natural sentence/clause subtitle
    cues so sentences are never chopped mid-phrase (e.g. 'full | time day trader'
    or 'a hundred | dollar account').
    """
    words = []
    for seg in raw_segments:
        seg_words = getattr(seg, "words", None)
        if seg_words:
            for w in seg_words:
                w_text = (getattr(w, "word", "") or "").strip()
                if w_text:
                    words.append({
                        "start": float(w.start),
                        "end": float(w.end),
                        "word": w_text,
                    })
        else:
            s_text = (seg.text or "").strip()
            if s_text:
                words.append({
                    "start": float(seg.start),
                    "end": float(seg.end),
                    "word": s_text,
                })

    if not words:
        return [
            {"start": round(s.start, 3), "end": round(s.end, 3), "text": s.text.strip()}
            for s in raw_segments if (s.text or "").strip()
        ]

    cues = []
    curr_words = []

    def flush():
        if not curr_words:
            return
        text = " ".join(w["word"] for w in curr_words)
        text = re.sub(r"\s+([,.;:!?])", r"\1", text).strip()
        start = round(curr_words[0]["start"], 3)
        end = round(max(curr_words[-1]["end"], start + 0.25), 3)
        if text:
            cues.append({"start": start, "end": end, "text": text})
        curr_words.clear()

    for idx, w in enumerate(words):
        curr_words.append(w)
        curr_dur = w["end"] - curr_words[0]["start"]
        curr_text = " ".join(x["word"] for x in curr_words)
        next_gap = (words[idx + 1]["start"] - w["end"]) if (idx + 1 < len(words)) else 99.0

        is_sent_end = bool(_PUNCT_END_RE.search(w["word"]))
        is_clause_end = bool(_CLAUSE_END_RE.search(w["word"]))

        # Break on sentence end (. ! ?) as soon as cue has at least 1.2s or 3 words
        if is_sent_end and (curr_dur >= 1.2 or len(curr_words) >= 3 or next_gap >= 0.2):
            flush()
            continue

        # Break on natural speech pause (>= 0.38s) if cue is already meaningful
        if next_gap >= pause_threshold and (curr_dur >= 1.5 or len(curr_words) >= 4):
            flush()
            continue

        # Break on clause boundary (, ; :) if cue is getting long (>= 4.2s or >= 65 chars)
        if is_clause_end and (curr_dur >= 4.2 or len(curr_text) >= 65):
            flush()
            continue

        # Hard cap so a single subtitle cue doesn't stay on screen too long
        if curr_dur >= max_duration or len(curr_text) >= max_chars:
            flush()

    flush()
    return cues


def transcribe(audio_path, model_size="small", device="cpu", compute_type="int8"):
    """Transcribes a 16kHz mono WAV file with faster-whisper using word-level
    timestamps to produce clean, clause-aligned subtitle cues.

    Returns {"language": str, "language_probability": float, "segments": [{start, end, text}]}.
    """
    model = _get_model(model_size, device, compute_type)
    audio = _load_wav_as_float32(audio_path)

    with heavy_inference_lock:
        segments_iter, info = model.transcribe(
            audio,
            vad_filter=True,
            word_timestamps=True,
            condition_on_previous_text=False,
        )
        raw_segments = list(segments_iter)
        segments = _regroup_words_into_natural_cues(raw_segments)

    return {
        "language": info.language,
        "language_probability": round(info.language_probability, 3),
        "segments": segments,
    }
