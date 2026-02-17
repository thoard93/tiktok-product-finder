#!/usr/bin/env python3
"""
Standalone Playwright script for eBay listing auto-fill.
Run as subprocess to avoid asyncio conflicts with gunicorn.

Uses persistent browser profile for eBay session persistence.
Images are passed as temp file paths via JSON stdin.

Usage:
    echo '{"listing_id": 123}' | python ebay_playwright.py

    OR for initial login:
    python ebay_playwright.py --login

Outputs JSON to stdout.
"""

import os
import sys
import json
import time
import base64
import sqlite3
import tempfile
import traceback
from datetime import datetime

basedir = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(basedir, 'products.db')
PROFILE_DIR = os.path.join(basedir, 'ebay_browser_profile')
SCREENSHOTS_DIR = os.path.join(basedir, 'pwa', 'ebay', 'screenshots')

# Ensure dirs exist
os.makedirs(PROFILE_DIR, exist_ok=True)
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)


def log(msg):
    """Log to stderr (stdout reserved for JSON result)."""
    print(f"[eBay-PW] {msg}", file=sys.stderr, flush=True)


def load_listing_from_db(listing_id):
    """Load listing data from SQLite database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM ebay_listings WHERE id = ?", (listing_id,)
    ).fetchone()
    conn.close()

    if not row:
        return None

    data = dict(row)
    # Parse images JSON
    try:
        data['images'] = json.loads(data.get('images_json', '[]'))
    except:
        data['images'] = []
    return data


def save_images_to_temp(images_b64):
    """Write base64 images to temp files for Playwright upload.
    Returns list of file paths."""
    paths = []
    for i, img_b64 in enumerate(images_b64[:12]):  # eBay max 12
        # Strip data URI prefix
        if ',' in img_b64:
            img_b64 = img_b64.split(',', 1)[1]
        try:
            img_bytes = base64.b64decode(img_b64)
            path = os.path.join(tempfile.gettempdir(), f'ebay_img_{i}_{int(time.time())}.jpg')
            with open(path, 'wb') as f:
                f.write(img_bytes)
            paths.append(path)
            log(f"Saved image {i+1} to {path} ({len(img_bytes)} bytes)")
        except Exception as e:
            log(f"Failed to save image {i}: {e}")
    return paths


def cleanup_temp_images(paths):
    """Remove temp image files."""
    for p in paths:
        try:
            os.remove(p)
        except:
            pass


def take_screenshot(page, name):
    """Save screenshot and return relative path."""
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"{name}_{ts}.png"
    filepath = os.path.join(SCREENSHOTS_DIR, filename)
    page.screenshot(path=filepath, full_page=False)
    log(f"Screenshot saved: {filepath}")
    return f"/ebay/screenshots/{filename}"


def launch_browser(playwright, headless=True):
    """Launch persistent Chromium context with eBay profile."""
    try:
        context = playwright.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
            ],
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
            ignore_https_errors=True,
        )
        return context
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            import subprocess
            subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                check=True
            )
            return playwright.chromium.launch_persistent_context(
                PROFILE_DIR,
                headless=headless,
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-dev-shm-usage", "--disable-gpu",
                      "--disable-blink-features=AutomationControlled"],
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 900},
                locale="en-US", timezone_id="America/New_York",
                ignore_https_errors=True,
            )
        raise


def check_login(page):
    """Check if we're logged into eBay by looking for the user menu."""
    try:
        page.goto("https://www.ebay.com", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)

        # Check for sign-in indicator — if "Sign in" link visible, not logged in
        sign_in = page.locator('text=Sign in').first
        if sign_in.is_visible():
            return False
        # Check for user greeting or account icon
        return True
    except:
        return False


def do_login(context):
    """Interactive login flow — opens eBay sign-in for user.
    Used with headless=False for initial setup."""
    page = context.pages[0] if context.pages else context.new_page()
    page.goto("https://signin.ebay.com/ws/eBayISAPI.dll?SignIn", wait_until="domcontentloaded", timeout=30000)
    log("eBay login page opened. Please sign in manually...")
    log("Waiting up to 120 seconds for login to complete...")

    # Wait for redirect to eBay homepage after login
    try:
        page.wait_for_url("**/ebay.com/**", timeout=120000)
        page.wait_for_timeout(3000)

        if check_login(page):
            log("Login successful! Session saved to profile.")
            return True
        else:
            log("Login may have failed — could not verify session.")
            return False
    except Exception as e:
        log(f"Login timeout or error: {e}")
        return False


def fill_listing_on_ebay(listing_data, image_paths):
    """Main function: navigate to eBay, fill listing form."""
    from playwright.sync_api import sync_playwright

    result = {
        'success': False,
        'step': 'init',
        'screenshot': None,
        'error': None,
        'ebay_url': None,
    }

    with sync_playwright() as p:
        context = launch_browser(p, headless=True)
        page = context.pages[0] if context.pages else context.new_page()

        try:
            # ─── Step 1: Check login ─────────────────────────────
            result['step'] = 'check_login'
            log("Checking eBay login status...")

            if not check_login(page):
                result['error'] = 'Not logged into eBay. Go to Settings → eBay Session → Login first.'
                result['screenshot'] = take_screenshot(page, 'login_required')
                context.close()
                return result

            log("eBay login confirmed ✓")

            # ─── Step 2: Navigate to sell page ───────────────────
            result['step'] = 'navigate_sell'
            log("Navigating to eBay sell page...")
            page.goto("https://www.ebay.com/sl/sell", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            log(f"On page: {page.url}")

            # ─── Step 3: Fill title ──────────────────────────────
            result['step'] = 'fill_title'
            title = listing_data.get('title', '')
            log(f"Filling title: {title[:50]}...")

            # eBay's title input — try multiple selectors
            title_filled = False
            title_selectors = [
                'input[placeholder*="Tell buyers"]',
                'input[placeholder*="title"]',
                'input[name="title"]',
                '#s0-1-1-24-7-\\@keyword-\\@box-textbox',
                'input[type="text"][maxlength="80"]',
            ]

            for sel in title_selectors:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=3000):
                        el.click()
                        el.fill(title)
                        title_filled = True
                        log(f"Title filled via selector: {sel}")
                        break
                except:
                    continue

            if not title_filled:
                # Try role-based
                try:
                    el = page.get_by_role("textbox").first
                    if el.is_visible():
                        el.fill(title)
                        title_filled = True
                        log("Title filled via role=textbox")
                except:
                    pass

            if not title_filled:
                result['error'] = 'Could not find title input field'
                result['screenshot'] = take_screenshot(page, 'error_title')
                context.close()
                return result

            # Press Enter or click "Get started" to proceed
            page.wait_for_timeout(1500)

            # Look for a "Get started" or "Continue" or submit button
            for btn_text in ['Get started', 'Continue', 'Start listing']:
                try:
                    btn = page.get_by_role("button", name=btn_text)
                    if btn.is_visible(timeout=2000):
                        btn.click()
                        log(f"Clicked '{btn_text}' button")
                        break
                except:
                    continue
            else:
                # Try pressing Enter on the title field
                try:
                    page.keyboard.press("Enter")
                    log("Pressed Enter after title")
                except:
                    pass

            page.wait_for_timeout(4000)
            log(f"After title submit, on page: {page.url}")

            # ─── Step 4: Category selection ──────────────────────
            result['step'] = 'select_category'
            log("Waiting for category suggestions...")

            # eBay usually shows category suggestions after title
            # Try to select the first suggestion or click through
            try:
                # Look for category suggestion radio/button
                cat_suggestion = page.locator('.category-suggestion, [data-testid="category-suggestion"]').first
                if cat_suggestion.is_visible(timeout=5000):
                    cat_suggestion.click()
                    log("Selected category suggestion")
                else:
                    # Try clicking first radio button in category list
                    radio = page.locator('input[type="radio"]').first
                    if radio.is_visible(timeout=3000):
                        radio.click()
                        log("Selected first category radio")
            except Exception as e:
                log(f"Category selection: {e} — continuing anyway")

            # Click Continue after category
            page.wait_for_timeout(2000)
            for btn_text in ['Continue', 'Continue to listing']:
                try:
                    btn = page.get_by_role("button", name=btn_text)
                    if btn.is_visible(timeout=3000):
                        btn.click()
                        log(f"Clicked '{btn_text}'")
                        break
                except:
                    continue

            page.wait_for_timeout(4000)

            # ─── Step 5: Upload images ───────────────────────────
            result['step'] = 'upload_images'
            if image_paths:
                log(f"Uploading {len(image_paths)} images...")
                try:
                    # Find file input for photos
                    file_input = page.locator('input[type="file"]').first
                    if file_input.count() > 0:
                        # Upload all images at once
                        file_input.set_input_files(image_paths)
                        log(f"Uploaded {len(image_paths)} images via file input")
                        # Wait for upload processing
                        page.wait_for_timeout(3000 + (len(image_paths) * 1500))
                    else:
                        log("No file input found — trying drag and drop area")
                        # Try clicking the upload area first
                        upload_area = page.locator('[data-testid="photos-upload"], .photo-upload, text=Add photos')
                        if upload_area.first.is_visible(timeout=3000):
                            # Use file chooser
                            with page.expect_file_chooser() as fc:
                                upload_area.first.click()
                            file_chooser = fc.value
                            file_chooser.set_files(image_paths)
                            log("Uploaded via file chooser")
                            page.wait_for_timeout(3000 + (len(image_paths) * 1500))
                except Exception as e:
                    log(f"Image upload issue: {e} — continuing")

            # ─── Step 6: Fill condition ──────────────────────────
            result['step'] = 'fill_condition'
            condition = listing_data.get('condition', 'NEW')
            log(f"Setting condition: {condition}")

            try:
                # Try clicking condition dropdown and selecting
                cond_map = {
                    'NEW': 'New',
                    'NEW_OTHER': 'New (Other)',
                    'LIKE_NEW': 'Like New',
                    'USED_EXCELLENT': 'Used - Excellent',
                }
                cond_text = cond_map.get(condition, 'New')

                # Look for condition selector
                cond_select = page.locator('select').filter(has_text='New')
                if cond_select.first.is_visible(timeout=3000):
                    cond_select.first.select_option(label=cond_text)
                    log(f"Selected condition: {cond_text}")
                else:
                    # Try button/radio approach
                    cond_btn = page.get_by_text(cond_text, exact=True)
                    if cond_btn.first.is_visible(timeout=2000):
                        cond_btn.first.click()
                        log(f"Clicked condition: {cond_text}")
            except Exception as e:
                log(f"Condition selection: {e} — continuing")

            # ─── Step 7: Fill price ──────────────────────────────
            result['step'] = 'fill_price'
            price = listing_data.get('price', 0)
            log(f"Setting price: ${price}")

            try:
                price_selectors = [
                    'input[aria-label*="rice"]',
                    'input[placeholder*="rice"]',
                    'input[name="price"]',
                    '#s0-1-1-24-7-\\@price-textbox',
                ]
                for sel in price_selectors:
                    try:
                        el = page.locator(sel).first
                        if el.is_visible(timeout=2000):
                            el.click()
                            el.fill(str(price))
                            log(f"Price filled via: {sel}")
                            break
                    except:
                        continue
            except Exception as e:
                log(f"Price fill: {e}")

            # ─── Step 8: Set shipping to Free ────────────────────
            result['step'] = 'set_shipping'
            log("Setting shipping to Free...")

            try:
                # Look for Free shipping option
                free_ship_selectors = [
                    'text=Free shipping',
                    'text=free shipping',
                    'label:has-text("Free shipping")',
                    'input[value="FREE"]',
                ]
                for sel in free_ship_selectors:
                    try:
                        el = page.locator(sel).first
                        if el.is_visible(timeout=2000):
                            el.click()
                            log("Free shipping selected")
                            break
                    except:
                        continue
            except Exception as e:
                log(f"Shipping selection: {e} — continuing")

            # ─── Step 9: Fill description ────────────────────────
            result['step'] = 'fill_description'
            description = listing_data.get('description', '')
            log(f"Filling description ({len(description)} chars)...")

            try:
                desc_selectors = [
                    'textarea[aria-label*="escription"]',
                    'textarea[placeholder*="escription"]',
                    '#s0-1-1-24-7-\\@description-editor',
                    '[data-testid="description-editor"]',
                    '.ql-editor',  # Quill editor
                    'div[contenteditable="true"]',
                    'iframe[title*="escription"]',
                ]

                desc_filled = False
                for sel in desc_selectors:
                    try:
                        el = page.locator(sel).first
                        if el.is_visible(timeout=2000):
                            if el.evaluate('e => e.tagName') == 'IFRAME':
                                # Handle iframe editor
                                frame = el.content_frame()
                                body = frame.locator('body')
                                body.click()
                                body.evaluate(
                                    '(el, html) => { el.innerHTML = html; }',
                                    description
                                )
                            elif el.evaluate('e => e.isContentEditable'):
                                # Contenteditable div
                                el.click()
                                el.evaluate(
                                    '(el, html) => { el.innerHTML = html; }',
                                    description
                                )
                            else:
                                # Regular textarea
                                el.fill(description)
                            desc_filled = True
                            log(f"Description filled via: {sel}")
                            break
                    except:
                        continue

                if not desc_filled:
                    # Try to find and click "Add description" first
                    add_desc = page.get_by_text('Add description')
                    if add_desc.first.is_visible(timeout=2000):
                        add_desc.first.click()
                        page.wait_for_timeout(1500)
                        # Retry
                        for sel in desc_selectors:
                            try:
                                el = page.locator(sel).first
                                if el.is_visible(timeout=2000):
                                    el.fill(description)
                                    desc_filled = True
                                    log(f"Description filled after expand: {sel}")
                                    break
                            except:
                                continue
            except Exception as e:
                log(f"Description fill: {e}")

            # ─── Step 10: Fill UPC if available ──────────────────
            result['step'] = 'fill_specifics'
            upc = listing_data.get('upc', '')
            if upc:
                log(f"Filling UPC: {upc}")
                try:
                    upc_input = page.locator('input[aria-label*="UPC"], input[placeholder*="UPC"]').first
                    if upc_input.is_visible(timeout=2000):
                        upc_input.fill(upc)
                        log("UPC filled")
                except:
                    log("UPC field not found")

            # ─── Step 11: Set quantity ───────────────────────────
            qty = listing_data.get('quantity', 1)
            if qty > 1:
                try:
                    qty_input = page.locator('input[aria-label*="uantity"], input[name="quantity"]').first
                    if qty_input.is_visible(timeout=2000):
                        qty_input.fill(str(qty))
                        log(f"Quantity set to {qty}")
                except:
                    pass

            # ─── Step 12: Final screenshot ───────────────────────
            result['step'] = 'complete'
            page.wait_for_timeout(2000)

            # Scroll to top for clean screenshot
            page.evaluate('window.scrollTo(0, 0)')
            page.wait_for_timeout(500)

            screenshot_path = take_screenshot(page, 'filled_listing')
            result['success'] = True
            result['screenshot'] = screenshot_path
            result['ebay_url'] = page.url
            log(f"✅ Listing filled successfully! URL: {page.url}")

        except Exception as e:
            log(f"Error at step '{result['step']}': {e}")
            traceback.print_exc(file=sys.stderr)
            result['error'] = f"Failed at {result['step']}: {str(e)}"
            try:
                result['screenshot'] = take_screenshot(page, f"error_{result['step']}")
            except:
                pass

        finally:
            context.close()

    return result


def main():
    # Check for --login flag (initial setup)
    if '--login' in sys.argv:
        log("Starting eBay login flow (non-headless)...")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print(json.dumps({"error": "Playwright not installed"}))
            sys.exit(1)

        with sync_playwright() as p:
            context = launch_browser(p, headless=False)
            success = do_login(context)
            context.close()

        print(json.dumps({
            "success": success,
            "message": "Login saved!" if success else "Login failed or timed out",
        }))
        sys.exit(0 if success else 1)

    # Check for --check-session flag
    if '--check-session' in sys.argv:
        log("Checking eBay session...")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print(json.dumps({"error": "Playwright not installed"}))
            sys.exit(1)

        with sync_playwright() as p:
            context = launch_browser(p, headless=True)
            page = context.pages[0] if context.pages else context.new_page()
            logged_in = check_login(page)
            if logged_in:
                screenshot_path = take_screenshot(page, 'session_check')
            else:
                screenshot_path = take_screenshot(page, 'session_expired')
            context.close()

        print(json.dumps({
            "success": True,
            "logged_in": logged_in,
            "screenshot": screenshot_path,
        }))
        sys.exit(0)

    # Normal mode: read listing_id from stdin JSON or args
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        listing_id = int(sys.argv[1])
    else:
        try:
            input_data = json.loads(sys.stdin.read())
            listing_id = input_data.get('listing_id')
        except:
            print(json.dumps({"error": "Usage: python ebay_playwright.py <listing_id> OR --login OR --check-session"}))
            sys.exit(1)

    if not listing_id:
        print(json.dumps({"error": "No listing_id provided"}))
        sys.exit(1)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(json.dumps({"error": "Playwright not installed. Run: pip install playwright && playwright install chromium"}))
        sys.exit(1)

    # Load listing from DB
    log(f"Loading listing #{listing_id} from database...")
    listing = load_listing_from_db(listing_id)
    if not listing:
        print(json.dumps({"error": f"Listing #{listing_id} not found"}))
        sys.exit(1)

    log(f"Listing loaded: {listing.get('title', '')[:50]}")

    # Save images to temp files
    image_paths = save_images_to_temp(listing.get('images', []))
    log(f"Prepared {len(image_paths)} images for upload")

    try:
        # Fill listing on eBay
        result = fill_listing_on_ebay(listing, image_paths)
        print(json.dumps(result))
    finally:
        # Cleanup temp images
        cleanup_temp_images(image_paths)

    sys.exit(0 if result.get('success') else 1)


if __name__ == "__main__":
    main()
