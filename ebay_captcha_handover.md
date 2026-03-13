# Handover: eBay Playwright CAPTCHA Issue on Render

## Objective

We have a Python/Flask web app hosted on Render that helps users quickly list products on eBay. Because the user was denied access to the official eBay Inventory API, we built a Playwright-based scraper (`ebay_playwright.py`) that runs as a headless subprocess to automate the listing creation process.

The goal is to have Playwright:

1. Load a persistent browser profile (`/var/data/ebay_browser_profile`).
2. Navigate to eBay's sell page (`https://www.ebay.com/sl/sell` or `/sell/create`).
3. Fill in the title, category, and condition.
4. Click "Continue to listing".
5. Fill out the rest of the form (price, description, photos) and click "Save for later" to save it as a Draft.
6. The user then opens the native iOS eBay app, finds the Draft, and publishes it manually.

## The Problem

The flow was working perfectly earlier today on Render's native US datacenter IP.

However, after multiple rapid deployments and testing runs, eBay began throwing a CAPTCHA. Specifically, the CAPTCHA triggers immediately after Playwright clicks the **"Continue to listing"** button on the initial `/sell/create` page.

When "Continue to listing" is clicked, eBay initiates a redirect chain through `signin.ebay.com/ws/eBayISAPI.dll?SignIn&ru=...` which intercepts the bot with a CAPTCHA page, preventing it from ever reaching the main listing form to input the price and save the draft.

## What We've Ruled Out

1. **It is NOT a missing cookie issue:**
   - We built an `--inject-cookies` CLI argument into `ebay_playwright.py`.
   - The user has a UI in the app Settings to paste fresh `document.cookie` JSON from their personal authenticated Safari session.
   - The backend successfully parses these cookies and uses `context.add_cookies()` to inject them into the Playwright profile *before* navigating to eBay.
   - Even with completely fresh, valid session cookies injected seconds prior, clicking "Continue to listing" still triggers the CAPTCHA redirect.

2. **It is NOT solved by Residential Proxies:**
   - We tried routing Playwright through a SmartProxy residential proxy (`proxy.smartproxy.net`) to hide Render's datacenter IP.
   - This actually made the CAPTCHA worse, likely because SmartProxy's shared residential IPs are heavily abused by other eBay scrapers and are blacklisted by eBay's anti-fraud system.
   - When we removed the proxy and went back to Render's native IP, the CAPTCHA persisted (likely because Render's IP is now rate-limited/flagged from our testing).

## Technical Details & Constraints

* **Platform:** Render Web Service (Ubuntu/Debian environment, ephemeral filesystem but we mount `/var/data` as a persistent disk for the browser profile).
- **Browser:** Playwright Chromium (`headless=True`), launched with `launch_persistent_context`.
- **Playwright Args:** `--no-sandbox`, `--disable-setuid-sandbox`, `--disable-dev-shm-usage`, `--disable-gpu`, `--disable-blink-features=AutomationControlled`.
- **User Agent:** Standard Chrome Windows UA (`Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36`).
- **Constraint:** We **CANNOT** use the official eBay REST APIs (Inventory/Trading/Sell) because the developer account was rejected/not approved. We must use web automation.

## The Ask for Grok

Given that we cannot use the official API, and both Render's datacenter IPs and shared residential proxies are triggering eBay's CAPTCHA wall at the "Continue to listing" redirect (even with valid injected session cookies), what are the best alternative technical approaches to bypass or avoid this CAPTCHA?

Specifically looking for ideas on:

1. **URL Bypasses:** Is there a direct URL format to the main listing form that skips the `sell/create` -> `SignIn` redirect chain entirely?
2. **Playwright Stealth:** Are there specific Playwright stealth plugins or deeper fingerprint spoofing techniques (e.g., overriding `navigator.webdriver`, canvas spoofing) that work reliably against eBay's specific Akamai/PerimeterX bot detection rules in 2026?
3. **Alternative Proxy Strategies:** If shared residential proxies fail, what proxy architecture is required for eBay scraping?
4. **Cookie/Header Flaws:** Does eBay require specific headers (`Sec-Ch-Ua`, `Accept-Language`) or specific cookie syncing mechanisms beyond just injecting the `ebay` and `dp1` cookies to validate a session during that redirect?
