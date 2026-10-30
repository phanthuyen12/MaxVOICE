import threading

# faster-whisper (ctranslate2) and VieNeu (onnxruntime) each already split a
# single inference call across every CPU core internally. Measured on this
# machine: running two faster-whisper transcriptions concurrently on two
# Python threads took 23.1s total vs 15.8s run one-after-another for the
# same work — real slowdown from CPU oversubscription, not a speedup.
# So these two call sites share one lock: only one heavy local-inference
# call runs at a time, regardless of how many worker threads exist. Cutting
# (ffmpeg stream copy), audio extraction, and network calls (Resona) are NOT
# gated by this — those genuinely benefit from overlapping with whatever
# else is running.
heavy_inference_lock = threading.Lock()
