FROM python:3.11-slim

# Install system dependencies for OpenCV and MediaPipe.
#
# MediaPipe's Tasks API (mediapipe.tasks.python.vision) loads a native shared
# library that links against OpenGL ES / EGL at import time, so libglesv2 and
# libegl1 are required even though we only run CPU inference — without them
# FaceLandmarker.create_from_options() fails with:
#   OSError: libGLESv2.so.2: cannot open shared object file
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libgles2 \
    libegl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download MediaPipe models at build time.
#
# outputs/models is bind-mounted read-only in production, so the runtime
# auto-download in face_alignment.py cannot create this directory. Baking the
# models into the image means we never depend on that fallback. Note these land
# under /app/outputs/models/mediapipe, which the read-only mount will shadow at
# runtime — so the host directory must also contain them (see deploy guide).
RUN mkdir -p /opt/mediapipe-models \
    && curl -fsSL -o /opt/mediapipe-models/face_landmarker.task \
        https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task \
    && curl -fsSL -o /opt/mediapipe-models/blaze_face_short_range.tflite \
        https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/latest/blaze_face_short_range.tflite

# Copy source code, config, and trained model weights
COPY src/ ./src/
COPY config/ ./config/
COPY outputs/models/best_model.pth ./outputs/models/best_model.pth
COPY scripts/ ./scripts/

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "src.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
