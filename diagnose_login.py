"""
Diagnostic v2: Click "Log in" button and see what Clerk renders.
"""
import json
from playwright.sync_api import sync_playwright

BASE_URL = "https://www.tiktokcopilot.com"

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

        print("[1] Navigating to main page...")
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        page.screenshot(path="/tmp/diag_step1_loaded.png")
        print(f"[1] URL: {page.url}")

        # Click "Log in" using JavaScript to bypass visibility issues
        print("\n[2] Clicking 'Log in' via JS...")
        clicked = page.evaluate("""() => {
            const elements = document.querySelectorAll('a, button, div, span');
            for (const el of elements) {
                const text = el.textContent.trim();
                if (text === 'Log in' || text === 'Log In') {
                    el.click();
                    return { clicked: true, tag: el.tagName, text: text, href: el.href || '' };
                }
            }
            return { clicked: false };
        }""")
        print(f"[2] Click result: {json.dumps(clicked)}")

        # Wait for modal / navigation
        page.wait_for_timeout(5000)
        page.screenshot(path="/tmp/diag_step2_after_click.png")
        print(f"[2] URL after click: {page.url}")

        # Check frames again
        frames = page.frames
        print(f"\n[3] Frames after click: {len(frames)}")
        for i, frame in enumerate(frames):
            print(f"  Frame {i}: name='{frame.name}' url='{frame.url[:120]}'")

        # Check for iframes
        iframes = page.locator("iframe").all()
        print(f"\n[4] <iframe> elements: {len(iframes)}")
        for i, iframe in enumerate(iframes):
            src = iframe.get_attribute("src") or "(no src)"
            name = iframe.get_attribute("name") or "(no name)"
            print(f"  iframe {i}: src='{src[:120]}' name='{name}'")

        # Search ALL frames for input elements
        print("\n[5] Input elements in all frames:")
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
                        print(f"    <input type='{inp_type}' name='{inp_name}' id='{inp_id}' placeholder='{inp_placeholder}'>")
            except Exception as e:
                print(f"  Frame {i}: error - {e}")

        # Check for Clerk modal/overlay elements
        print("\n[6] Modal/overlay elements:")
        modals = page.evaluate("""() => {
            const results = [];
            const all = document.querySelectorAll('[role="dialog"], [class*="modal"], [class*="overlay"], [class*="clerk"], [id*="clerk"]');
            for (const el of all) {
                results.push({
                    tag: el.tagName.toLowerCase(),
                    id: el.id || '',
                    class: (el.className?.toString() || '').substring(0, 150),
                    role: el.getAttribute('role') || '',
                    visible: el.offsetParent !== null,
                    children: el.children.length
                });
            }
            return results;
        }""")
        for m in modals:
            print(f"  {json.dumps(m)}")
        if not modals:
            print("  (none found)")

        # Get page HTML around any potential auth elements
        print("\n[7] Auth-related HTML snippet:")
        auth_html = page.evaluate("""() => {
            const body = document.body.innerHTML;
            const idx = body.indexOf('Sign In');
            if (idx > -1) {
                return body.substring(Math.max(0, idx - 500), idx + 500);
            }
            const idx2 = body.indexOf('sign-in');
            if (idx2 > -1) {
                return body.substring(Math.max(0, idx2 - 500), idx2 + 500);
            }
            return '(no sign-in content found)';
        }""")
        print(auth_html[:2000])

        browser.close()
        print("\n[DONE]")

if __name__ == "__main__":
    diagnose()
