# Dockerfile for Vantage - TikTok Shop Intelligence Platform
# Uses Playwright's official image with Chromium pre-installed

FROM mcr.microsoft.com/playwright/python:v1.57.0-noble

WORKDIR /app

# Copy requirements and install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . .

# Render uses PORT env var
ENV PORT=10000
EXPOSE 10000

# Start command - reduced workers for 512MB memory limit (leaves room for Chromium)
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 1 --timeout 180

