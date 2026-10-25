import json
import os
import threading

from .cpu_lock import heavy_inference_lock

# Must be set before the `vieneu` package (and the huggingface_hub it uses to
# download model weights) is imported: newer onnxruntime releases reject the
# cross-directory symlinks huggingface_hub's cache normally creates between
# `blobs/` and `snapshots/` ("External data path escapes model directory").
# Forcing real file copies instead of symlinks avoids that entirely.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")

_tts = None
_config = None
_model_lock = threading.Lock()


def init(config):
    """Remembers the app config so the model-loading singleton can re-register
    cloned voices the moment it's created. Cheap — doesn't load the model.
    """
    global _config
    _config = config


def _get_tts():
    global _tts
    if _tts is None:
        with _model_lock:
            if _tts is None:  # re-check: another thread may have loaded it first
                from vieneu import Vieneu

                _tts = Vieneu()
                if _config is not None:
                    registry = load_cloned_voices(_config)
                    for name, ref_audio_path in registry.items():
                        if os.path.exists(ref_audio_path):
                            _tts.add_voice(name, ref_audio_path)
    return _tts


def list_preset_voices():
    """Returns [(display_label, voice_name), ...] for the 25 built-in voices."""
    return _get_tts().list_preset_voices()


def synthesize(text, voice=None, ref_audio=None, speed=1.0, seed=None):
    """Synthesizes `text` with either a preset `voice` name or a cloned
    `ref_audio` sample. Returns a float32 numpy array at `sample_rate()`.

    Serialized via heavy_inference_lock: onnxruntime already parallelizes a
    single call across every CPU core, so two concurrent synth calls would
    just fight each other for cores rather than finish faster (measured with
    faster-whisper, same story — see cpu_lock.py).
    """
    tts = _get_tts()
    kwargs = {}
    if seed is not None:
        kwargs["seed"] = seed
    if speed and speed != 1.0:
        kwargs["speed"] = speed
    with heavy_inference_lock:
        return tts.infer(text=text, voice=voice, ref_audio=ref_audio, **kwargs)


def save_wav(audio, output_path):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    _get_tts().save(audio, output_path)
    return output_path


def sample_rate():
    return _get_tts().sample_rate


# --- cloned voices registry -------------------------------------------------
# vieneu's own add_voice()/save_voices() persist into the installed package's
# assets dir, which isn't writable once this app is packaged (PyInstaller) and
# mixes user data with the install. Instead we keep our own small registry —
# {name: ref_audio_path} — under the app's storage dir, and re-register each
# cloned voice with add_voice() at the start of every session.

def _registry_path(config):
    return os.path.join(config.storage_dir, "vieneu_voices.json")


def load_cloned_voices(config):
    path = _registry_path(config)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def clone_voice(config, name, ref_audio_path, description="", gender=""):
    """Registers a new voice from a 3-8s reference audio sample and saves it
    to the local registry so it survives app restarts.
    """
    import shutil
    import uuid

    tts = _get_tts()
    voices_dir = os.path.join(config.storage_dir, "voices")
    os.makedirs(voices_dir, exist_ok=True)
    ext = os.path.splitext(ref_audio_path)[1] or ".wav"
    stored_path = os.path.join(voices_dir, f"{uuid.uuid4()}{ext}")
    shutil.copyfile(ref_audio_path, stored_path)

    tts.add_voice(name, stored_path, description=description, gender=gender)

    registry = load_cloned_voices(config)
    registry[name] = stored_path
    with open(_registry_path(config), "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
    return name


def remove_cloned_voice(config, name):
    registry = load_cloned_voices(config)
    stored_path = registry.pop(name, None)
    with open(_registry_path(config), "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
    if stored_path and os.path.exists(stored_path):
        try:
            os.unlink(stored_path)
        except OSError:
            pass
    try:
        _get_tts().remove_voice(name)
    except Exception:
        pass
