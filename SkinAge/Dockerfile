FROM python:3.11-slim

# Install system dependencies for OpenCV and MediaPipe
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
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

# Copy source code, config, and trained model weights
COPY src/ ./src/
COPY config/ ./config/
COPY outputs/models/best_model.pth ./outputs/models/best_model.pth
COPY scripts/ ./scripts/

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "src.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
