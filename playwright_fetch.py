#!/usr/bin/env python3
"""
Standalone Playwright script for TikTokCopilot V2 API fetching.
Run in subprocess to avoid asyncio conflicts with gunicorn.

Uses saved session cookies from clerk_auth.py (SQLite storage).

Usage:
    python playwright_fetch.py <page_num> <timeframe> <sort_by> <limit> <region>

Outputs JSON to stdout.
"""

import os
import sys
import json
import time
import sqlite3

# Use the same DB path as the main app
basedir = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(basedir, 'products.db')
BASE_URL = "https://www.tiktokcopilot.com"


def get_saved_cookies():
    """Load session cookies from SQLite database."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT value, updated_at FROM copilot_session WHERE key = ?", ("cookies",)
        ).fetchone()
        conn.close()
        
        if not row:
            return None
        
        # Check if cookies are less than 14 days old
        if time.time() - row['updated_at'] > 14 * 24 * 60 * 60:
            return None
        
        return json.loads(row['value'])
    except Exception as e:
        print(f"Error loading cookies: {e}", file=sys.stderr)
        return None


def main():
    # Parse args
    if len(sys.argv) < 6:
        print(json.dumps({"error": "Usage: playwright_fetch.py <page> <timeframe> <sort_by> <limit> <region>"}))
        sys.exit(1)
    
    page_num = int(sys.argv[1])
    timeframe = sys.argv[2]
    sort_by = sys.argv[3]
    limit = int(sys.argv[4])
    region = sys.argv[5]
    
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(json.dumps({"error": "Playwright not installed"}))
        sys.exit(1)
    
    api_url = f"{BASE_URL}/api/trending/products?timeframe={timeframe}&sortBy={sort_by}&limit={limit}&page={page_num}&region={region}"
    
    # Check for saved cookies
    saved_cookies = get_saved_cookies()
    
    if not saved_cookies:
        print(json.dumps({"error": "No saved session. Please authenticate via /api/copilot-auth/initiate first."}))
        sys.exit(1)
    
    try:
        with sync_playwright() as p:
            # Launch browser
            try:
                browser = p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox", 
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--single-process",
                    ]
                )
            except Exception as browser_err:
                if "Executable doesn't exist" in str(browser_err):
                    import subprocess
                    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
                    browser = p.chromium.launch(
                        headless=True,
                        args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--single-process"]
                    )
                else:
                    raise
            
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080}
            )
            
            # Inject saved cookies
            print("Using saved session cookies from database...", file=sys.stderr)
            context.add_cookies(saved_cookies)
            
            page = context.new_page()
            
            # Navigate to site to establish cookie context
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)
            
            # Verify we're authenticated by checking for session cookie
            cookies = context.cookies()
            session_cookies = [c for c in cookies if "__session" in c["name"]]
            
            if not session_cookies:
                print(json.dumps({"error": "Session cookies not valid. Re-authenticate via /api/copilot-auth/initiate."}))
                browser.close()
                sys.exit(1)
            
            print(f"Session valid. Fetching API: {api_url}", file=sys.stderr)
            
            # Fetch API via JS
            result = page.evaluate(
                """async (url) => {
                    try {
                        const resp = await fetch(url, { credentials: 'include' });
                        const body = await resp.json();
                        return { status: resp.status, body, ok: resp.ok };
                    } catch (err) {
                        return { error: err.message };
                    }
                }""",
                api_url
            )
            
            browser.close()
            
            if "error" in result:
                print(json.dumps({"error": result["error"]}))
                sys.exit(1)
            
            if not result.get("ok"):
                print(json.dumps({"error": f"HTTP {result.get('status')}", "body": str(result.get('body', ''))[:500]}))
                sys.exit(1)
            
            products = result.get("body", {}).get("products", [])
            print(json.dumps({"success": True, "products": products, "count": len(products)}))
            
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
