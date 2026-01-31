# Dockerfile for Vantage - TikTok Shop Intelligence Platform
# Lightweight Python image - no Playwright/Chromium needed

FROM python:3.11-slim

WORKDIR /app

# Install build deps for python-Levenshtein
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . .

# Render uses PORT env var
ENV PORT=10000
EXPOSE 10000

# Optimized for 2GB RAM - no Playwright overhead!
# 4 workers x 2 threads = handles more concurrent requests
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 4 --threads 2 --timeout 180
