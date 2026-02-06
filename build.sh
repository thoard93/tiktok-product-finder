#!/usr/bin/env bash
# Build script for Render - installs dependencies and Playwright browsers

set -e

echo "Installing Python dependencies..."
pip install -r requirements.txt

echo "Installing Playwright system dependencies..."
python -m playwright install-deps

echo "Installing Playwright Chromium browser..."
python -m playwright install chromium

echo "Build complete!"
