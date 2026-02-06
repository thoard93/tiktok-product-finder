"""
Diagnostic v4: Clerk SDK Authentication Bypass.
Calls window.Clerk.client.signIn.create() directly in JS.
"""
import json
import os
import sys
from playwright.sync_api import sync_playwright

BASE_URL = "https://www.tiktokcopilot.com"
EMAIL = os.environ.get('COPILOT_EMAIL', 'thoard93@gmail.com').strip()
PASSWORD = os.environ.get('COPILOT_PASSWORD', 'Mychaela7193!').strip()

def diagnose():
    print(f"Using Email: {EMAIL}")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
        )
        page = context.new_page()

        # Step 1: Load main page
        print("[1] Loading main page...")
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
        
        # Step 2: Wait for Clerk SDK to be ready
        print("[2] Waiting for Clerk SDK to be ready...")
        try:
            page.wait_for_function("() => window.Clerk && window.Clerk.isReady && window.Clerk.isReady()", timeout=20000)
            print("    Clerk SDK is ready!")
        except Exception as e:
            print(f"    Clerk SDK wait error: {e}")
            # Log what's on window
            win_keys = page.evaluate("() => Object.keys(window)")
            print(f"    Window keys: {win_keys[:20]}...")
            if "Clerk" not in win_keys:
                print("    !!! Clerk NOT found on window object !!!")
                browser.close()
                return

        # Step 3: Sign in via Clerk's JS API
        print("[3] Attempting Sign-in via Clerk JS API...")
        result = page.evaluate("""async ([email, password]) => {
            try {
                if (!window.Clerk || !window.Clerk.client) {
                    return { error: 'Clerk.client not initialized' };
                }
                
                console.log('Starting signIn.create...');
                const si = await window.Clerk.client.signIn.create({
                    identifier: email,
                    password: password,
                    strategy: 'password'
                });
                
                console.log('signIn status:', si.status);
                
                if (si.status === 'complete') {
                    console.log('Setting active session:', si.createdSessionId);
                    await window.Clerk.setActive({ session: si.createdSessionId });
                    return { success: true, status: si.status, sessionId: si.createdSessionId };
                }
                
                return { 
                    success: false, 
                    status: si.status, 
                    nextSteps: si.supportedFirstFactors 
                };
            } catch (err) {
                return { error: err.message || String(err), stack: err.stack };
            }
        }""", [EMAIL, PASSWORD])
        
        print(f"[3] Result: {json.dumps(result, indent=2)}")

        if result.get("success"):
            print("\n[4] Login SUCCESS! Checking session cookies...")
            page.wait_for_timeout(3000)
            cookies = context.cookies()
            clerk_cookies = [c["name"] for c in cookies if "__session" in c["name"] or "__client_uat" in c["name"] or "__clerk" in c["name"]]
            print(f"    Clerk cookies: {clerk_cookies}")
            
            # Step 5: Test API fetch
            api_url = "https://www.tiktokcopilot.com/api/trending/products?timeframe=7d&sortBy=revenue&limit=10&page=1&region=US"
            print(f"\n[5] Testing API fetch to {api_url}...")
            api_result = page.evaluate("""async (url) => {
                try {
                    const resp = await fetch(url, { credentials: 'include' });
                    const body = await resp.json();
                    return { status: resp.status, ok: resp.ok, productsCount: body.products ? body.products.length : 0 };
                } catch (err) {
                    return { error: err.message };
                }
            }""", api_url)
            print(f"    API Result: {json.dumps(api_result, indent=2)}")
        else:
            print("\n[4] Login FAILED or incomplete.")
            # Screenshot for debugging
            try:
                os.makedirs('static', exist_ok=True)
                page.screenshot(path="static/diag_sdk_fail.png")
                print("    Screenshot saved to static/diag_sdk_fail.png")
            except:
                pass

        browser.close()
        print("\n[DONE]")

if __name__ == "__main__":
    diagnose()
