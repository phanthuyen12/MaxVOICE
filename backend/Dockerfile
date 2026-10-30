FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_DISABLE_SYMLINKS=1 \
    MAXVOICE_DATA_DIR=/data \
    MAXVOICE_STORAGE_DIR=/data/storage \
    PORT=8686

# System dependencies:
# - ffmpeg: audio/video cutting, muxing, and subtitle burn-in (libass)
# - espeak-ng: phonemizer backend required by VieNeu offline Vietnamese TTS
# - libsndfile1: required by soundfile for WAV processing
# - fonts-noto-core & fonts-noto-cjk: full Unicode/Vietnamese/Chinese/Japanese/Korean/Cyrillic fonts for hardsub burn-in
# - cmake & build-essential: required if sentencepiece/llama-cpp-python builds from source
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    espeak-ng \
    libsndfile1 \
    fonts-noto-core \
    fonts-noto-cjk \
    cmake \
    build-essential \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY run_server.py /app/run_server.py

RUN mkdir -p /data/storage/uploads /data/storage/outputs

EXPOSE 8686

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8686/health || exit 1

CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8686"]

