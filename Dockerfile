# Dockerfile for Vantage - TikTok Shop Intelligence Platform
# With Playwright + Chromium for authenticated V2 API access

FROM python:3.11-slim

WORKDIR /app

# Install build deps + Playwright system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    # Playwright/Chromium dependencies
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libatspi2.0-0 \
    libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium browser
RUN python -m playwright install chromium

# Copy app code
COPY . .

# Render uses PORT env var
ENV PORT=10000
EXPOSE 10000

# Reduced workers for Playwright memory usage (1 worker handles Playwright subprocess)
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 2 --timeout 180
