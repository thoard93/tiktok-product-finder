# TikTokCopilot V2 API — Direct Playwright Auth

## What Changed
Dropped BrightData entirely. Instead of fighting cookie injection and proxy detection,
we run Playwright directly on Render and let Clerk handle auth natively.

## How It Works
1. First API request triggers Playwright to launch Chromium
2. Navigates to TikTokCopilot login, fills email/password via Clerk form
3. Clerk sets all cookies naturally — no injection, no domain mismatch
4. API calls run as `fetch()` inside the authenticated browser page
5. Clerk JS auto-refreshes the JWT — no 60-second expiry problem
6. Session re-authenticates every hour as a safety net

## Deploy to Render

### Environment Variables
Set these in your Render dashboard (or render.yaml):
```
COPILOT_EMAIL=thoard93@gmail.com
COPILOT_PASSWORD=Mychaela7193!
```

### Build Command
```
pip install -r requirements.txt && playwright install --with-deps chromium
```

### Start Command
```
gunicorn app:app --bind 0.0.0.0:$PORT --timeout 120 --workers 1 --threads 4
```

### Plan Requirement
**Starter plan minimum** (512MB RAM). Free tier (256MB) won't reliably run Chromium.

## Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/api/v2/products` | GET | Fetch trending products |
| `/api/v2/session` | GET | Session diagnostics (cookies, auth age) |
| `/api/v2/reauth` | POST | Force re-authentication |
| `/health` | GET | Health check |

### Query Parameters for `/api/v2/products`
- `timeframe` — `7d` (default), `24h`, `30d`
- `sortBy` — `revenue` (default), `units`, `views`
- `limit` — `50` (default)
- `page` — `0` (default)
- `region` — `US` (default)

## Removed
- BrightData Scraping Browser (SCRAPING_BROWSER_URL)
- curl_cffi
- COPILOT_COOKIE env var
- All cookie injection logic
