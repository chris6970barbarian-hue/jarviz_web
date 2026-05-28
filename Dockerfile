FROM python:3.11-slim

# libopus0     -> opuslib needs libopus.so.0 at runtime
# ffmpeg       -> Edge-TTS produces mp3, we decode to PCM via ffmpeg
# build deps   -> faster-whisper / numpy wheels are usually prebuilt, but
#                 keep ca-certificates so outbound HTTPS (DeepSeek, HF model
#                 download) trusts the standard CA bundle.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libopus0 \
    ffmpeg \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server  ./server
COPY prompts ./prompts

# Hugging Face Spaces runs Docker SDK containers as a non-root user (uid 1000)
# in a sandbox WITHOUT a persistent /data volume on the free tier. Pointing
# the data dir at /tmp keeps `store.py`'s devices.json writable regardless of
# whether the deployment platform mounts an external volume. On platforms
# that DO offer persistent storage (Render, Fly, your own host) override
# JARVIZ_DATA_DIR at runtime to point at it.
ENV PYTHONUNBUFFERED=1 \
    JARVIZ_DATA_DIR=/tmp/jarviz-data \
    JARVIZ_HTTP_HOST=0.0.0.0 \
    JARVIZ_HTTP_PORT=8080 \
    HF_HOME=/tmp/huggingface

EXPOSE 8080

# `python -m server` wraps uvicorn with our defaults (WS keepalive, host/port
# from .env). Falling back to plain `uvicorn` works too — see server/__main__.py.
CMD ["python", "-m", "server"]
