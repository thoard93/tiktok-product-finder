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
            
            # Step 1: Navigate to main page
            print(f"Navigating to main page: https://www.tiktokcopilot.com", file=sys.stderr)
            page.goto("https://www.tiktokcopilot.com", wait_until="domcontentloaded", timeout=60000)
            
            # Step 2: Wait for Clerk JS to initialize and click "Log in" in top-right
            page.wait_for_timeout(5000)
            
            print("Clicking 'Log in' button...", file=sys.stderr)
            page.evaluate("""() => {
                const elements = document.querySelectorAll('a, button, div, span');
                for (const el of elements) {
                    if (el.textContent.trim() === 'Log in') { el.click(); return true; }
                }
                return false;
            }""")
            page.wait_for_timeout(3000)
            
            # Step 3: Click "Sign In" inside the Clerk modal
            print("Clicking 'Sign In' inside modal...", file=sys.stderr)
            page.evaluate("""() => {
                const buttons = document.querySelectorAll('button');
                for (const btn of buttons) {
                    const text = btn.textContent.trim();
                    if (text === 'Sign In' || text === 'Sign in') {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }""")
            page.wait_for_timeout(3000)
            
            # Step 4: Fill email
            print("Filling email...", file=sys.stderr)
            email_input = page.locator('input[name="identifier"], input[type="email"], input[autocomplete="email"], input[autocomplete="username"]').first
            email_input.wait_for(state="visible", timeout=10000)
            email_input.fill(email)
            
            # Continue button (Clerk two-step)
            try:
                continue_btn = page.locator('button:has-text("Continue")').first
                if continue_btn.is_visible(timeout=3000):
                    continue_btn.click()
                    page.wait_for_timeout(3000)
            except:
                pass
            
            # Step 5: Fill password
            print("Filling password...", file=sys.stderr)
            password_input = page.locator('input[name="password"], input[type="password"]').first
            password_input.wait_for(state="visible", timeout=10000)
            password_input.fill(password)
            
            # Submit
            print("Submitting login...", file=sys.stderr)
            submit_btn = page.locator('button:has-text("Sign In"), button:has-text("Sign in"), button:has-text("Continue"), button[type="submit"]').first
            submit_btn.click()
            
            # Wait for auth completion (URL changes back or modal closes)
            page.wait_for_timeout(8000)
            print(f"URL after login: {page.url}", file=sys.stderr)
            
            # Verify session cookies exist
            cookies = context.cookies()
            session_cookies = [c for c in cookies if "__session" in c["name"]]
            
            if not session_cookies:
                # One last attempt to wait
                page.wait_for_timeout(5000)
                cookies = context.cookies()
                session_cookies = [c for c in cookies if "__session" in c["name"]]
                
                if not session_cookies:
                    print(json.dumps({"error": "Login completed but no session cookies found. Check credentials or modal state."}))
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
