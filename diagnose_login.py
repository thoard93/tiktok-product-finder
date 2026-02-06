"""
Diagnostic v3: Full two-step Clerk login flow.
1. Click "Log in" -> opens modal
2. Click "Sign In" inside modal -> shows email/password
3. Fill credentials and submit
"""
import json
from playwright.sync_api import sync_playwright

BASE_URL = "https://www.tiktokcopilot.com"
EMAIL = "thoard93@gmail.com"
PASSWORD = "Mychaela7193!"

def diagnose():
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
        page.wait_for_timeout(5000)

        # Step 2: Click "Log in" button
        print("[2] Clicking 'Log in'...")
        page.evaluate("""() => {
            const elements = document.querySelectorAll('a, button, div, span');
            for (const el of elements) {
                if (el.textContent.trim() === 'Log in') { el.click(); return true; }
            }
            return false;
        }""")
        page.wait_for_timeout(3000)
        print(f"[2] URL: {page.url}")

        # Step 3: Click "Sign In" button inside the Clerk modal
        print("[3] Clicking 'Sign In' inside modal...")
        clicked = page.evaluate("""() => {
            const buttons = document.querySelectorAll('button');
            for (const btn of buttons) {
                const text = btn.textContent.trim();
                if (text === 'Sign In' || text === 'Sign in') {
                    btn.click();
                    return { clicked: true, text: text };
                }
            }
            return { clicked: false };
        }""")
        print(f"[3] Result: {json.dumps(clicked)}")
        page.wait_for_timeout(5000)
        print(f"[3] URL: {page.url}")

        # Step 4: Check what's on screen now
        frames = page.frames
        print(f"\n[4] Frames: {len(frames)}")
        for i, frame in enumerate(frames):
            print(f"  Frame {i}: url='{frame.url[:120]}'")

        # Check for iframes (Clerk might load auth in iframe now)
        iframes = page.locator("iframe").all()
        print(f"\n[4b] iframes: {len(iframes)}")
        for i, iframe in enumerate(iframes):
            src = iframe.get_attribute("src") or "(no src)"
            name = iframe.get_attribute("name") or "(no name)"
            print(f"  iframe {i}: src='{src[:150]}' name='{name}'")

        # Search ALL frames for input elements
        print("\n[5] Inputs in all frames:")
        for i, frame in enumerate(frames):
            try:
                inputs = frame.locator("input").all()
                if inputs:
                    print(f"  Frame {i}: {len(inputs)} inputs")
                    for inp in inputs:
                        inp_type = inp.get_attribute("type") or "?"
                        inp_name = inp.get_attribute("name") or "?"
                        inp_id = inp.get_attribute("id") or "?"
                        inp_placeholder = inp.get_attribute("placeholder") or "?"
                        inp_autocomplete = inp.get_attribute("autocomplete") or "?"
                        visible = inp.is_visible()
                        print(f"    <input type='{inp_type}' name='{inp_name}' id='{inp_id}' placeholder='{inp_placeholder}' autocomplete='{inp_autocomplete}' visible={visible}>")
            except Exception as e:
                print(f"  Frame {i}: error - {e}")

        # Screenshot
        page.screenshot(path="/tmp/diag_step3.png")
        print("\n[5b] Screenshot saved")

        # If we found email input, try filling it
        print("\n[6] Attempting to fill email...")
        for i, frame in enumerate(frames):
            try:
                email_input = frame.locator('input[name="identifier"], input[type="email"], input[autocomplete="email"], input[autocomplete="username"]').first
                if email_input.is_visible(timeout=3000):
                    print(f"  Found email input in frame {i}! Filling...")
                    email_input.fill(EMAIL)
                    page.wait_for_timeout(1000)
                    
                    # Look for Continue button (Clerk often splits email/password)
                    continue_btn = frame.locator('button:has-text("Continue")').first
                    if continue_btn.is_visible(timeout=2000):
                        print("  Clicking Continue...")
                        continue_btn.click()
                        page.wait_for_timeout(3000)
                    
                    # Look for password field
                    pw_input = frame.locator('input[type="password"], input[name="password"]').first
                    if pw_input.is_visible(timeout=5000):
                        print("  Found password input! Filling...")
                        pw_input.fill(PASSWORD)
                        page.wait_for_timeout(500)
                        
                        # Click submit
                        submit = frame.locator('button:has-text("Sign In"), button:has-text("Sign in"), button:has-text("Continue"), button[type="submit"]').first
                        if submit.is_visible(timeout=3000):
                            print("  Clicking submit...")
                            submit.click()
                            page.wait_for_timeout(8000)
                            print(f"  URL after submit: {page.url}")
                            
                            # Check cookies
                            cookies = context.cookies()
                            clerk_cookies = [c["name"] for c in cookies if "__session" in c["name"] or "__client_uat" in c["name"] or "__clerk" in c["name"]]
                            print(f"  Clerk cookies: {clerk_cookies}")
                            
                            page.screenshot(path="/tmp/diag_step4_loggedin.png")
                            print("  Screenshot saved: diag_step4_loggedin.png")
                        else:
                            print("  No submit button found")
                            page.screenshot(path="/tmp/diag_step4_nosubmit.png")
                    else:
                        print("  No password input found after email")
                        page.screenshot(path="/tmp/diag_step4_nopw.png")
                        
                        # Dump what's visible
                        html_snippet = frame.evaluate("() => document.body.innerHTML.substring(0, 3000)")
                        print(f"  HTML: {html_snippet[:1500]}")
                    break
            except Exception as e:
                print(f"  Frame {i}: {e}")

        browser.close()
        print("\n[DONE]")

if __name__ == "__main__":
    diagnose()
