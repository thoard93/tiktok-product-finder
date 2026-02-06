#!/usr/bin/env python3
"""
Standalone Playwright script for TikTokCopilot V2 API fetching.
Run in subprocess to avoid asyncio conflicts with gunicorn.

Usage:
    python playwright_fetch.py <page_num> <timeframe> <sort_by> <limit> <region>

Outputs JSON to stdout.
"""

import os
import sys
import json
import time

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
    
    email = os.environ.get('COPILOT_EMAIL', '').strip()
    password = os.environ.get('COPILOT_PASSWORD', '').strip()
    
    if not email or not password:
        print(json.dumps({"error": "COPILOT_EMAIL and COPILOT_PASSWORD env vars required"}))
        sys.exit(1)
    
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(json.dumps({"error": "Playwright not installed"}))
        sys.exit(1)
    
    api_url = f"https://www.tiktokcopilot.com/api/trending/products?timeframe={timeframe}&sortBy={sort_by}&limit={limit}&page={page_num}&region={region}"
    
    try:
        with sync_playwright() as p:
            # Check if browser is installed, install if not
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
                    # Try to install browser
                    import subprocess
                    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
                    # Retry
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
            page = context.new_page()
            
            # Navigate to login
            page.goto("https://www.tiktokcopilot.com/?auth=sign-in", wait_until="networkidle", timeout=30000)
            
            # Click "Sign in" if modal
            try:
                sign_in = page.locator("text=Sign in").first
                if sign_in.is_visible(timeout=3000):
                    sign_in.click()
                    page.wait_for_timeout(1500)
            except:
                pass
            
            # Fill email
            email_input = page.locator('input[name="identifier"], input[type="email"], input[autocomplete="email"]').first
            email_input.wait_for(state="visible", timeout=10000)
            email_input.fill(email)
            
            # Continue button (Clerk two-step)
            try:
                continue_btn = page.locator('button:has-text("Continue")')
                if continue_btn.is_visible(timeout=2000):
                    continue_btn.click()
                    page.wait_for_timeout(2000)
            except:
                pass
            
            # Fill password
            password_input = page.locator('input[name="password"], input[type="password"]').first
            password_input.wait_for(state="visible", timeout=10000)
            password_input.fill(password)
            
            # Submit
            submit_btn = page.locator('button:has-text("Sign In"), button:has-text("Sign in"), button[type="submit"]').first
            submit_btn.click()
            
            # Wait for auth
            page.wait_for_url(lambda url: "auth=sign-in" not in url, timeout=20000)
            page.wait_for_load_state("networkidle", timeout=15000)
            
            # Verify cookies
            cookies = context.cookies()
            session_cookies = [c for c in cookies if "__session" in c["name"]]
            
            if not session_cookies:
                print(json.dumps({"error": "Login completed but no session cookies found"}))
                browser.close()
                sys.exit(1)
            
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
