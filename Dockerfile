# Dockerfile for Vantage - TikTok Shop Intelligence Platform
# Uses Python slim with full Chromium dependencies for Playwright

FROM python:3.11-slim

# Install system dependencies for Chromium (Playwright needs these)
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libc6 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libexpat1 \
    libfontconfig1 \
    libgcc1 \
    libgdk-pixbuf2.0-0 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libstdc++6 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxi6 \
    libxrandr2 \
    libxrender1 \
    libxss1 \
    libxtst6 \
    lsb-release \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install Python deps first (better caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (runs every build, reliably in Docker)
RUN python -m playwright install-deps && python -m playwright install chromium

# Copy app code
COPY . .

# Render uses PORT env var
ENV PORT=10000
EXPOSE 10000

# Start command - gunicorn with your current config
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 3 --threads 2 --timeout 120
