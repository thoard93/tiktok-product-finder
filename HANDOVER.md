# TikTok Product Finder — Complete Project Handover

> **Last Updated:** April 9, 2026
> **Repo:** [thoard93/tiktok-product-finder](https://github.com/thoard93/tiktok-product-finder)
> **Live Domain:** `thoardburgersauce.com`

---

## 1. Project Overview

This repository contains **three major subsystems** sharing a single Flask backend and PostgreSQL database:

| System | Purpose | Status (Apr 2026) |
|---|---|---|
| **Vantage** | TikTok Shop product intelligence platform | ⏸️ Data stale since Feb 4 (Copilot API dead) |
| **FlipTracker** | eBay sales/shipment/refund tracker | ✅ Active at `/price` |
| **Brand Hunter Bot** | Discord bot for product alerts & AI chat | ⏸️ Suspended on Render |

---

## 2. Repository Structure

```
tiktok-product-finder/
├── app.py                    # Main Flask backend (11,942 lines) - Vantage + all API routes
├── price_research.py         # FlipTracker eBay module (2,479 lines) - imports from app.py
├── discord_bot.py            # Brand Hunter Discord bot (1,882 lines) - imports from app.py
├── clerk_auth.py             # Clerk-based auth (experimental, unused)
├── playwright_fetch.py       # Browser-based scraping helper
├── Dockerfile                # Docker build (Python 3.11 + Playwright + Chromium)
├── Procfile                  # Heroku/Render process: gunicorn app:app
├── render.yaml               # Render Blueprint (gem-hunter web + copilot-sync cron)
├── requirements.txt          # Python dependencies
├── products.db               # Local SQLite fallback
├── pwa/                      # All frontend HTML files
│   ├── price/                # FlipTracker PWA (index.html + manifest.json)
│   ├── dashboard_v4.html     # Vantage main dashboard
│   ├── dashboard_mobile.html # Vantage mobile dashboard
│   ├── admin_v4.html         # Admin panel (80KB)
│   ├── brand_hunter.html     # Brand Hunter web UI
│   ├── scanner_v4.html       # Manual product scanner
│   ├── product_detail_v4.html# Product detail page
│   ├── vantage_v2.html       # Vantage v2 layout
│   ├── settings.html         # User settings
│   ├── login.html            # Discord OAuth login
│   ├── maintenance.html      # Maintenance mode page
│   ├── developer_v4.html     # Developer API portal
│   ├── css/                  # Shared stylesheets
│   └── js/                   # Shared JavaScript
├── static/                   # Static assets
├── caked/                    # "Caked Picks" feature assets
└── test_*.py                 # Various test/debug scripts
```

---

## 3. Render Infrastructure

### 3.1 Active Services (as of last check)

| Service | Runtime | What It Does | Monthly Cost |
|---|---|---|---|
| `tiktokshop-finder` | Python 3 | Main Vantage backend (`app.py`) | ~$7/mo |
| `tiktok-product-finder-1` | Docker | Docker variant (likely redundant) | ~$7/mo |
| `brand-hunter` | Python 3 | Brand Hunter web app | ~$7/mo |
| `brand-hunter-discord-bot` | Python 3 | Discord bot (`discord_bot.py`) | ~$7/mo |
| `discord-video-bot` | Node | CGI video bot (separate project) | ~$7/mo |
| `tiktok-db` | PostgreSQL 18 | Primary database (referenced in `render.yaml`) | Free tier |
| `tiktok-db-4o6b` | PostgreSQL 18 | Secondary DB (check which services use) | Free tier |
| `trading-db` | PostgreSQL 18 | DEGEN DEX trading bot (unrelated) | Free tier |

> **⚠️ Recommendation:** `tiktok-product-finder-1` is likely redundant with `tiktokshop-finder`. Check which domain `thoardburgersauce.com` points to and suspend the other.

### 3.2 Render Blueprint (`render.yaml`)

```yaml
services:
  - type: web
    name: gem-hunter           # Main web service
    runtime: python
    startCommand: gunicorn app:app --bind 0.0.0.0:$PORT --timeout 120 --workers 1 --threads 4
    envVars:
      - key: DATABASE_URL
        fromDatabase:
          name: tiktok-db      # Primary PostgreSQL

  - type: cron
    name: copilot-sync         # Auto-sync trending products every 12h
    schedule: "0 */12 * * *"
    startCommand: python -c "from app import sync_copilot_products; [sync_copilot_products(page=p) for p in range(3)]"

databases:
  - name: tiktok-db
    plan: free
```

---

## 4. Database Schema

### 4.1 TikTok Product Intelligence (in `app.py`)

#### `Product` — Core product data (40+ fields)
| Key Fields | Type | Description |
|---|---|---|
| `product_id` | String(50) PK | Format: `shop_<numeric_id>` |
| `product_name` | String(500) | |
| `seller_id` / `seller_name` | String | TikTok Shop seller |
| `price` / `original_price` | Float | Current & strikethrough price |
| `sales` / `sales_7d` / `sales_30d` | Integer | Sales metrics |
| `video_count` / `video_count_alltime` | Integer | 7D vs all-time video counts |
| `influencer_count` | Integer | Creator count |
| `commission_rate` / `shop_ads_commission` | Float | Affiliate commission rates |
| `ad_spend` / `ad_spend_total` | Float | 7D & lifetime ad spend |
| `gmv` / `gmv_30d` / `gmv_growth` | Float | Gross merchandise value |
| `product_status` | String | `active`, `removed`, `out_of_stock`, `likely_oos` |
| `scan_type` | String | `brand_hunter`, `bot_lookup`, `bot_lookup_v2`, `daily_virals` |
| `is_favorite` | Boolean | User watchlist |
| `image_url` / `cached_image_url` | Text | Product images |

#### Supporting Models
| Model | Purpose |
|---|---|
| `User` | Discord OAuth users (id, discord_id, username, is_admin) |
| `ActivityLog` | User activity tracking |
| `SystemConfig` | Key-value runtime settings |
| `SiteConfig` | Site-level configuration |
| `BlacklistedBrand` | Flagged sellers (commission scams) |
| `WatchedBrand` | Brand Hunter tracked brands |
| `CreatorList` | Per-thread creator lists (inspo-chat) |
| `ApiKey` | Developer API key management |
| `ScanJob` | Async scan job tracking |

### 4.2 FlipTracker / eBay Sales (in `price_research.py`)

#### `EbayListing` — Full sales lifecycle
| Field | Type | Description |
|---|---|---|
| `id` | Integer PK | Auto-increment |
| `product_name` | String(300) | |
| `status` | String(20) | `listed`, `sold`, `shipped`, `cancelled`, `returned`, `refunded`, `removed` |
| `list_price` / `sold_price` | Float | Prices |
| `shipping_cost` / `ebay_fees` / `profit` | Float | Cost breakdown |
| `refund_amount` | Float | Partial or full refund |
| `cancel_id` / `return_id` | String(50) | eBay cancel/return IDs |
| `order_number` / `ebay_item_id` | String(50) | eBay identifiers |
| `buyer_name` / `buyer_address` | String | Buyer info |
| `tracking_number` / `shipping_service` | String | Shipment tracking |
| `weight_oz` / `length_in` / `width_in` / `height_in` | Float | Package dimensions |
| `team` | String(20) | `thoard` or `reol` (multi-user support) |
| `listed_at` / `sold_at` | DateTime | Timestamps |

#### `PriceResearch` — Price research results
| Field | Type | Description |
|---|---|---|
| `product_name` | String(300) | Identified product |
| `prices` | Text (JSON) | Multi-source price data |
| `recommended_price` | Float | AI-calculated recommended sell price |
| `is_bundle` | Boolean | Bundle detection |
| `aggressiveness` | String | `conservative`, `balanced`, `aggressive` |

---

## 5. Environment Variables

### 5.1 Core Infrastructure
| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string (Render auto-sets) |
| `SECRET_KEY` | Flask session encryption |
| `PORT` | Web server port (Render sets to 10000) |

### 5.2 Authentication
| Variable | Purpose |
|---|---|
| `DISCORD_CLIENT_ID` | Discord OAuth app ID |
| `DISCORD_CLIENT_SECRET` | Discord OAuth secret |
| `DISCORD_REDIRECT_URI` | Default: `https://thoardburgersauce.com/auth/discord/callback` |
| `DISCORD_GUILD_ID` | Required server membership check |
| `ADMIN_DISCORD_IDS` | Comma-separated admin Discord user IDs |
| `DEV_PASSKEY` | Developer bypass login |
| `DEVELOPER_PASSWORD` | Maintenance mode resume password |

### 5.3 AI & API Keys
| Variable | Purpose |
|---|---|
| `XAI_API_KEY` | Grok 4.1 (primary AI for chat + price research vision) |
| `ANTHROPIC_API_KEY` | Claude fallback for AI chat |
| `GEMINI_API_KEY` | Google Gemini (image generation, enrichment) |

### 5.4 TikTok Data Sources
| Variable | Purpose |
|---|---|
| `COPILOT_EMAIL` / `COPILOT_PASSWORD` | TikTokCopilot.com login (data source) |
| `TIKTOK_COPILOT_COOKIE` | Direct cookie for Copilot API |
| `TIKTOK_PARTNER_COOKIE` | TikTok Partner Center cookie |
| `ECHOTIK_USERNAME` / `ECHOTIK_PASSWORD` | EchoTik product enrichment |
| `DAILYVIRALS_TOKEN` | DailyVirals.io API token |
| `SCRAPFLY_API_KEY` | Scrapfly web scraping |
| `SCRAPING_BROWSER_URL` | BrightData scraping browser |
| `ECHOTIK_PROXY_STRING` / `DAILYVIRALS_PROXY` / `COPILOT_PROXY` | Proxy configs |

### 5.5 Discord Bot
| Variable | Purpose |
|---|---|
| `DISCORD_BOT_TOKEN` | Bot authentication |
| `HOT_PRODUCTS_CHANNEL_ID` | Daily hot products posting channel |
| `BRAND_HUNTER_CHANNEL_ID` | Brand hunter daily posts |

### 5.6 Other Services
| Variable | Purpose |
|---|---|
| `KLING_ACCESS_KEY` / `KLING_SECRET_KEY` | Kling AI video generation |
| `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` | Payment processing |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram notifications |
| `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` | Gmail scanning for FlipTracker |

---

## 6. API Routes Summary

### 6.1 Vantage / Product Intelligence (~60 routes in `app.py`)

**Core Data:**
- `GET /api/products` — Paginated product listing with filters
- `GET /api/product/<product_id>` — Single product detail
- `GET /api/stats` — Dashboard statistics
- `GET /api/brands` — Brand listing
- `GET /api/brand-products/<seller_name>` — Products by brand
- `GET /api/favorites` — User's watchlist
- `POST /api/favorite/<product_id>` — Toggle favorite

**Scanning & Data Ingestion:**
- `POST /api/scan/manual` — Manual Copilot sync
- `POST /api/scan/dv-live` — DailyVirals live scan
- `POST /api/scan/partner_opportunity` — TikTok Partner scan
- `POST /api/run-viral-trends-scan` — Viral trends scan
- `POST /api/refresh-product/<product_id>` — Refresh single product
- `POST /api/refresh-all-products` — Batch refresh
- `POST /api/refresh-ads` — Refresh ad spend data
- `POST /api/refresh-images` — Refresh product images
- `POST /api/deep-refresh` — Full enrichment re-run
- `GET /api/copilot/sync` — Copilot cookie-based sync

**AI & Video:**
- `POST /api/ai/chat` — Vantage AI chatbot (Grok 4.1 / Claude)
- `POST /api/generate-video` — AI video generation (Kling)
- `POST /api/one-click-video` — One-click video creation
- `GET /api/video-status/<task_id>` — Video generation status

**Analytics:**
- `GET /api/analytics/movers-shakers` — Sales momentum
- `GET /api/analytics/creative-linker` — Video→product linking
- `GET /api/analytics/top-videos` — Best performing videos
- `GET /api/trending-products` — Trending products feed
- `GET /api/hidden-gems` — Low-video high-sales opportunities
- `GET /api/oos-products` — Out of stock products

**Admin:**
- `GET /admin` — Admin dashboard page
- `GET /api/admin/users` — User management
- `GET /api/admin/activity` — Activity logs
- `POST /api/admin/config` — Runtime config management
- `POST /api/admin/migrate` — Database migrations
- `POST /api/admin/purge-low-signal` — Cleanup low-quality products
- `POST /api/admin/products/nuke` — Delete all products
- `POST /api/admin/create-indexes` — Database index creation

**Auth:**
- `GET /auth/discord` → `GET /auth/discord/callback` — Discord OAuth flow
- `POST /auth/passkey` — Developer passkey login
- `GET /auth/logout` — Logout
- `GET /api/me` — Current user info

**External API:**
- `POST /api/extern/scan` — External scan trigger (API key required)
- `GET /api/extern/jobs/<job_id>` — Job status
- `POST /api/developer/keygen` — Generate API keys
- `POST /api/developer/checkout` — Stripe checkout for API access

### 6.2 FlipTracker / eBay (~10 routes in `price_research.py`)

- `GET /price` — FlipTracker PWA
- `POST /price/api/research` — AI-powered price research (image upload → Grok vision)
- `GET /price/api/history` — Research history
- `GET /price/api/listings` — eBay listings (filtered by team)
- `POST /price/api/listings` — Add listing manually
- `PUT /price/api/listings/<id>` — Update listing
- `DELETE /price/api/listings/<id>` — Delete listing
- `POST /price/api/listings/<id>/sell` — Mark as sold
- `POST /price/api/listings/scan-gmail` — Manual Gmail scan
- `POST /price/api/shipping-estimate` — AI shipping cost estimate

### 6.3 Page Routes

| URL | Page |
|---|---|
| `/` | Vantage dashboard (v4) |
| `/admin` | Admin panel |
| `/scanner` | Manual product scanner |
| `/settings` | User settings |
| `/brand-hunter` | Brand Hunter UI |
| `/vantage-v2` | Vantage v2 layout |
| `/product/<id>` | Product detail |
| `/developer` | Developer API portal |
| `/login` | Discord OAuth login |
| `/price` | FlipTracker eBay tracker |

---

## 7. Key Features — Deep Dive

### 7.1 Vantage Product Intelligence

**Data Pipeline:**
1. **TikTokCopilot** → Primary source. Scrapes trending products via authenticated API using Playwright browser
2. **DailyVirals** → Secondary scan for viral products
3. **EchoTik** → Product enrichment (video counts, creator counts, reviews)
4. **TikTok Partner Center** → Commission data scraping
5. All data merges into the `Product` model with dedup by `product_id`

**Product Scoring:**
- **Opportunity Gems:** High ad spend + low video count = untapped opportunity
- **Caked Picks:** $50K-$200K GMV + ≤50 influencers
- **High Volume:** 500+ 7D sales
- Products are scored and tagged automatically during sync

**AI Chat (Grok 4.1):**
- System prompt includes live product database context
- Can answer questions about best opportunities, trending niches, commission rates
- Falls back to Claude if `XAI_API_KEY` not set

### 7.2 FlipTracker (eBay Sales Tracker)

**Auto-Scanner Flow (runs every 30 min via APScheduler):**
1. Connects to Gmail via IMAP (`GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD`)
2. Searches for eBay email types:
   - **"listed on eBay"** → Creates new listing with status `listed`
   - **"sold!"** → Matches listing, sets status `sold`, records price/buyer
   - **"shipping label"** → Adds shipping cost, tracking, calculates profit
   - **"canceled an order"** → Sets status `cancelled`, zeros profit
   - **"has been refunded"** → Sets `refunded` (full) or adjusts profit (partial)
   - **"Return approved"** → Sets status `returned`, zeros profit
   - **"removed some of your listings"** → Sets status `removed`
3. Deduplicates listings (matches by product name similarity)
4. Calculates profit: `sold_price - ebay_fees (13%) - shipping_cost`

**Discount Ladder (Price Research):**
```python
DISCOUNT_LADDER = {
    'conservative': {15: 17%, 30: 20%, 60: 20%, 100: 23%, 9999: 25%},
    'balanced':     {15: 23%, 30: 27%, 60: 27%, 100: 30%, 9999: 33%},
    'aggressive':   {15: 33%, 30: 37%, 60: 35%, 100: 39%, 9999: 43%},
}
```
Undercuts TikTok Shop median prices by tier-based percentages for eBay reselling.

**Frontend (FlipTracker PWA at `/price`):**
- Rebranded from "PriceBlade" to "FlipTracker" (March 2026)
- 3 nav tabs: 📦 Listings, 📊 Dashboard, ⚙️ Settings
- Sections: Active Listings, Sold, Cancelled/Returned/Refunded, Removed by eBay
- Multi-team support: "Thoard" and "Reol" team toggle
- Manual add listing, mark sold, scan Gmail buttons

### 7.3 Discord Bot (Brand Hunter)

**Daily Tasks (12:00 PM EST):**
1. **Hot Products** — Top 15 products matching: 40-120 all-time videos, $100+ ad spend, 100+ 7D sales, commission > 0%
2. **Brand Hunter** — Top 10 opportunity products from top 50 revenue brands (50-300 all-time videos)

**Interactive Features:**
- **Product Lookup** — Drop a TikTok product link in `#product-lookup` → bot returns embed with stats
- **Blacklist Warnings** — Flags blacklisted brands on lookup
- **AI Chat** — Private AI chat channels powered by Grok 4.1
- **Inspo-Chat** — Auto-joins forum threads, manages per-thread creator lists
- **Emoji reactions** — Fuzzy-matched 50+ word emoji dictionary

**Channel IDs (hardcoded):**
```python
PRODUCT_LOOKUP_CHANNEL_ID = 1461053839800139959
BLACKLIST_CHANNEL_ID = 1440369747467174019
AI_CHAT_CHANNEL_ID = 1473031651599847631
AI_CHAT_CATEGORY_ID = 1444029219951874129
```

---

## 8. Authentication

### Discord OAuth Flow
1. User clicks "Login with Discord" → redirects to Discord OAuth
2. Discord returns `code` → exchanged for `access_token`
3. Bot verifies user is in required `DISCORD_GUILD_ID`
4. Creates/updates `User` record → sets Flask session

### Developer Passkey
- `POST /auth/passkey` with `{"passkey": "..."}` → creates admin Developer user
- Bypasses Discord OAuth entirely

### Maintenance Mode
- Admin can enable/disable via `/api/maintenance/enable|disable`
- Resume with password via `/api/maintenance/resume`
- Default password: `DEVELOPER_PASSWORD` env var

---

## 9. Deployment

### Docker (Primary — `tiktok-product-finder-1`)
```dockerfile
FROM python:3.11-slim
# Installs Playwright + Chromium for authenticated scraping
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 2 --timeout 180
```

### Native Python (`tiktokshop-finder`)
```
gunicorn app:app --workers 1 --threads 4 --timeout 120 --keep-alive 5
```

### Git Push Deploy
All Render services auto-deploy on push to `main` branch.

---

## 10. Known Issues & Technical Debt

### Critical
- **Copilot API is dead** — Last data update ~Feb 4, 2026. The entire Vantage product intelligence depends on this. No alternative data source is configured.
- **Redundant Render services** — `tiktokshop-finder` and `tiktok-product-finder-1` likely do the same thing. One should be suspended.

### Moderate
- **app.py is 11,942 lines** — All routes, models, and logic in one file. Should be split into modules (routes/, models/, services/).
- **Hardcoded Discord channel IDs** — Should be environment variables.
- **No automated tests** — Only manual test scripts (`test_*.py`) for debugging.
- **SQLite fallback** — `products.db` exists but is never used in production (Render uses PostgreSQL). Could cause confusion.
- **Duplicate `@app.route('/api/stats')` and `/api/admin/activity`** — Defined twice in app.py (lines 2945 and 3768, and 1736 and 5668).

### Minor
- **stale `temp.html`** — 141KB temp file in root.
- **`copilot_products_response.json`** and `copilot_trending.json`** — 676KB and 776KB debug/cache files committed to repo.
- **`render.yaml` references `gem-hunter`** — But actual Render service is named differently.

---

## 11. Current Operational Status (April 2026)

| Component | Status | Notes |
|---|---|---|
| **Vantage Dashboard** | ⚠️ Stale data | Copilot API not updating since Feb 4 |
| **FlipTracker** | ✅ Active | Gmail auto-scanner running, cancel/refund support |
| **Discord Bot** | ⏸️ Check Render | May need manual resume |
| **Database** | ✅ Operational | PostgreSQL on Render free tier |
| **Domain** | ✅ Live | `thoardburgersauce.com` |

### Recommended Actions
1. **Suspend** `tiktokshop-finder`, `brand-hunter`, `brand-hunter-discord-bot` to save ~$21/mo until a new data source replaces Copilot
2. **Keep** the service running FlipTracker (`tiktok-product-finder` or `tiktok-product-finder-1` — whichever has the domain)
3. **Investigate** alternative TikTok Shop data sources if Vantage needs to resume
4. **Consider** splitting `app.py` into modules if further development is planned

---

## 12. Quick Reference Commands

```bash
# Local development
pip install -r requirements.txt
python -m playwright install chromium
flask run --debug

# Deploy (auto on git push)
git add -A; git commit -m "message"; git push

# Database migration (hit once after deploy)
curl -X POST https://thoardburgersauce.com/api/admin/migrate

# Manual Gmail scan
curl -X POST https://thoardburgersauce.com/price/api/listings/scan-gmail

# Manual Copilot sync
curl -X POST https://thoardburgersauce.com/api/copilot/sync
```
