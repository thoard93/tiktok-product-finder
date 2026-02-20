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
from urllib.parse import unquote, urlparse, parse_qs

basedir = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(basedir, 'products.db')

# Use Render persistent disk if available, otherwise local paths
PERSISTENT_DATA = '/var/data' if os.path.isdir('/var/data') else basedir
PROFILE_DIR = os.path.join(PERSISTENT_DATA, 'ebay_browser_profile')
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
    screenshot_path = os.path.join(SCREENSHOTS_DIR, filename)
    page.screenshot(path=screenshot_path, full_page=False)
    log(f"Screenshot saved: {screenshot_path}")
    return f"/ebay/screenshots/{filename}"


def extract_listing_url(redirect_url, original_title=None):
    """Extract the actual eBay listing URL from a CAPTCHA/signin redirect chain.
    The URL is typically nested 2-3 levels deep in 'ru' parameters.
    If original_title is provided, replaces eBay's product-matched title with ours."""
    try:
        import re
        from urllib.parse import quote_plus, urlencode
        
        # Decode the full URL repeatedly to un-nest it
        decoded = redirect_url
        for _ in range(5):  # Max decode depth
            decoded = unquote(decoded)
        
        # Find the ebay.com/sl/list URL in the decoded string
        match = re.search(r'(https://www\.ebay\.com/sl/list\?[^&\s"]*(?:&[^&\s"]*)*)', decoded)
        if not match:
            # Try to find any ebay.com/sl/ URL
            match = re.search(r'(https://www\.ebay\.com/sl/[^&\s"]*(?:&[^&\s"]*)*)', decoded)
        
        if match:
            listing_url = match.group(1)
            
            if original_title:
                # Replace eBay's wrong product-matched title with our original title
                listing_url = re.sub(r'title=[^&]*', 'title=' + quote_plus(original_title), listing_url)
                
                # Remove productRefId (links to wrong catalog product)
                listing_url = re.sub(r'&productRefId=[^&]*', '', listing_url)
                
                # Remove itemId (links to wrong seller's item)
                listing_url = re.sub(r'&itemId=[^&]*', '', listing_url)
                
                # Remove aspects (pre-filled from wrong product)
                listing_url = re.sub(r'&aspects=[^&]*', '', listing_url)
                
                # Change mode from SellLikeItem to AddItem (fresh listing)
                listing_url = re.sub(r'mode=SellLikeItem', 'mode=AddItem', listing_url)
                
                log(f"Fixed listing URL with original title: {original_title[:50]}...")
            
            log(f"Extracted listing URL: {listing_url[:100]}...")
            return listing_url
            
    except Exception as e:
        log(f"URL extraction failed: {e}")
    
    return None


def launch_browser(playwright, headless=True):
    """Launch persistent Chromium context with eBay profile and Smartproxy."""
    # Smartproxy Residential Proxy Settings
    proxy_config = {
        "server": "http://proxy.smartproxy.net:3120",
        "username": "smart-yx842akr4euy",
        "password": "pEMWTNMYDV2cMYsp"
    }

    try:
        context = playwright.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=headless,
            proxy=proxy_config,
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
                proxy=proxy_config,
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
    """Check if we're logged into eBay using multiple methods."""
    try:
        page.goto("https://www.ebay.com", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        # Method 1: Check cookies for login indicators (most reliable)
        try:
            cookies = page.context.cookies()
            for c in cookies:
                # ebay cookie with sin=in means signed in
                if c.get('name') == 'ebay' and 'sin%3Din' in c.get('value', ''):
                    log("Login confirmed via ebay cookie (sin=in)")
                    return True
                # dp1 cookie with u1f/ contains username = logged in
                if c.get('name') == 'dp1' and 'u1f/' in c.get('value', ''):
                    log("Login confirmed via dp1 cookie (user data present)")
                    return True
        except Exception as e:
            log(f"Cookie check error: {e}")

        # Method 2: Look for "Hi <name>" greeting on page
        try:
            hi_el = page.locator('[id*="gh-ug"]').first  # eBay greeting element
            if hi_el.is_visible(timeout=3000):
                log(f"Login confirmed via greeting element")
                return True
        except:
            pass

        try:
            # Also try text matching for "Hi " greeting
            greeting = page.locator('text=/^Hi /').first
            if greeting.is_visible(timeout=2000):
                log("Login confirmed via 'Hi' greeting text")
                return True
        except:
            pass

        # Method 3: Check if sign-in link is prominent (least reliable)
        try:
            sign_in = page.locator('a[href*="signin"]').first
            if sign_in.is_visible(timeout=2000):
                text = sign_in.text_content()
                if text and 'sign in' in text.lower():
                    log("Not logged in — sign-in link visible")
                    return False
        except:
            pass

        # If we got here and didn't find sign-in, assume logged in
        log("Login status uncertain — assuming logged in (no sign-in link found)")
        return True
    except Exception as e:
        log(f"Login check error: {e}")
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

            # Try direct listing creation URLs first
            listing_urls = [
                "https://www.ebay.com/sell/create",
                "https://www.ebay.com/sl/sell",
            ]

            for url in listing_urls:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                log(f"On page: {page.url}")

                # Check if we're on the hub page — need to click through
                for btn_text in ['List an item', 'Sell an item', 'Create listing',
                                 'Start listing', 'List it', 'Sell it']:
                    try:
                        btn = page.get_by_role("link", name=btn_text)
                        if btn.first.is_visible(timeout=2000):
                            btn.first.click()
                            log(f"Clicked '{btn_text}' link on hub page")
                            page.wait_for_timeout(4000)
                            break
                    except:
                        continue
                else:
                    # Also try buttons (not just links)
                    for btn_text in ['List an item', 'Sell an item', 'Start listing']:
                        try:
                            btn = page.get_by_role("button", name=btn_text)
                            if btn.first.is_visible(timeout=2000):
                                btn.first.click()
                                log(f"Clicked '{btn_text}' button on hub page")
                                page.wait_for_timeout(4000)
                                break
                        except:
                            continue

                # Check if we now have a title input
                try:
                    any_input = page.locator('input[type="text"]').first
                    if any_input.is_visible(timeout=3000):
                        log(f"Found text input on page: {page.url}")
                        break
                except:
                    pass
            
            log(f"After navigation, on page: {page.url}")
            take_screenshot(page, 'sell_page')

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

            # ─── Step 4: Handle eBay "Confirm details" prelist page ──
            # This page has TWO stages:
            #   Stage A: Product catalog matching (select a product or skip)
            #   Stage B: Condition selection (New, Used, etc.)
            # Both must be completed before clicking "Continue to listing"
            result['step'] = 'confirm_details'
            log("Handling prelist page...")
            
            # Wait for page to stabilize
            try:
                page.wait_for_load_state('networkidle', timeout=15000)
            except:
                pass
            page.wait_for_timeout(3000)
            
            # Save page HTML for debugging
            try:
                html = page.content()
                html_path = os.path.join(SCREENSHOTS_DIR, 'prelist_page.html')
                with open(html_path, 'w', encoding='utf-8') as f:
                    f.write(html)
                log(f"Page HTML saved ({len(html)} bytes)")
            except Exception as e:
                log(f"HTML save error: {e}")
            
            take_screenshot(page, 'prelist_before')
            current_url = page.url
            log(f"Prelist URL: {current_url}")
            
            # ─── Stage A: Product Catalog Matching ────────────────
            # eBay shows product matches with class "product-button"
            # We need to either click the first matching product OR skip
            log("Stage A: Product catalog matching...")
            
            product_selected = False
            
            # Check if product buttons exist on the page
            try:
                product_btns = page.locator('button[class*="product-button"]')
                btn_count = product_btns.count()
                log(f"Found {btn_count} product-button elements")
                
                if btn_count > 0:
                    # Click the FIRST product match
                    product_btns.first.click()
                    product_selected = True
                    log("Clicked first product match")
                    page.wait_for_timeout(3000)
                    take_screenshot(page, 'after_product_select')
            except Exception as e:
                log(f"Product button click error: {e}")
            
            # Fallback: try "Continue without match" / "I can't find it" / skip buttons
            if not product_selected:
                skip_texts = [
                    "Continue without match", "continue without match",
                    "Continue without product match",
                    "I can't find", "Can't find",
                    "List it yourself", "Skip",
                    "Continue without selecting",
                ]
                for skip_text in skip_texts:
                    try:
                        skip_btn = page.get_by_text(skip_text)
                        if skip_btn.count() > 0 and skip_btn.first.is_visible(timeout=1500):
                            skip_btn.first.click()
                            product_selected = True
                            log(f"Clicked skip: '{skip_text}'")
                            page.wait_for_timeout(3000)
                            break
                    except:
                        pass
            
            # If product buttons weren't found, the page might already be on condition step
            if not product_selected:
                log("No product buttons found — may already be on condition step")
            
            # ─── Stage B: Condition Selection ─────────────────────
            # After product match, eBay shows condition options (New, Used, etc.)
            # Wait for condition step to appear
            log("Stage B: Condition selection...")
            
            page.wait_for_timeout(2000)
            take_screenshot(page, 'condition_step')
            
            # Log what we see now
            try:
                page_text = page.evaluate('() => document.body.innerText.substring(0, 1500)')
                log(f"PAGE TEXT (first 400): {page_text[:400]}")
            except:
                pass
            
            condition = listing_data.get('condition', 'NEW')
            cond_map = {
                'NEW': ['New', 'New with tags', 'New with box', 'Brand New'],
                'NEW_OTHER': ['New (Other)', 'New without tags', 'New other'],
                'LIKE_NEW': ['Like New', 'Excellent'],
                'USED_EXCELLENT': ['Used - Excellent', 'Very Good', 'Pre-owned'],
                'USED_GOOD': ['Used - Good', 'Good'],
                'USED': ['Used', 'Pre-owned'],
            }
            cond_texts = cond_map.get(condition, ['New'])
            condition_selected = False
            
            # Wait for "condition" text to appear (indicates condition step loaded)
            try:
                page.get_by_text("condition", exact=False).first.wait_for(state='visible', timeout=8000)
                log("Condition text visible")
            except:
                log("Condition text not found — checking if already past this step")
                # If URL is no longer prelist, we might have skipped past it
                if 'prelist' not in page.url:
                    log(f"Already past prelist! URL: {page.url}")
                    condition_selected = True  # Skip condition selection
            
            if not condition_selected:
                # Dump interactive elements for debugging
                try:
                    elems = page.evaluate('''() => {
                        const results = [];
                        const all = document.querySelectorAll('button, [role="button"], [role="radio"], [role="option"], input[type="radio"], label, [aria-checked], [class*="condition"], [class*="Condition"]');
                        for (const el of all) {
                            const text = (el.textContent || "").trim().substring(0, 80);
                            if (!text) continue;
                            results.push({
                                tag: el.tagName,
                                text: text,
                                role: el.getAttribute("role") || "",
                                cls: (el.className?.toString() || "").substring(0, 60),
                                checked: el.getAttribute("aria-checked") || ""
                            });
                        }
                        return results.slice(0, 20);
                    }''')
                    for e in elems[:10]:
                        log(f"  COND_ELEM: <{e['tag']}> role={e['role']} checked={e['checked']} text=\"{e['text'][:40]}\" cls=\"{e['cls'][:40]}\"")
                except:
                    pass
                
                # Method 1: get_by_role("radio") 
                for cond_text in cond_texts:
                    if condition_selected:
                        break
                    try:
                        radio = page.get_by_role("radio", name=cond_text)
                        if radio.count() > 0:
                            radio.first.click(force=True)
                            condition_selected = True
                            log(f"M1: Clicked radio role '{cond_text}'")
                    except:
                        pass
                
                # Method 2: ARIA selectors
                if not condition_selected:
                    for sel in ['[aria-checked="false"]', '[role="radio"]', '[role="option"]']:
                        try:
                            el = page.locator(sel).first
                            if el.is_visible(timeout=1500):
                                el.click()
                                condition_selected = True
                                log(f"M2: Clicked {sel}")
                                break
                        except:
                            pass
                
                # Method 3: get_by_label
                if not condition_selected:
                    for cond_text in cond_texts:
                        try:
                            el = page.get_by_label(cond_text, exact=True)
                            if el.count() > 0:
                                el.first.click(force=True)
                                condition_selected = True
                                log(f"M3: Clicked label '{cond_text}'")
                                break
                        except:
                            pass
                
                # Method 4: input[type=radio]
                if not condition_selected:
                    try:
                        radios = page.locator('input[type="radio"]')
                        count = radios.count()
                        log(f"M4: Found {count} radio inputs")
                        if count > 0:
                            radios.first.click(force=True)
                            condition_selected = True
                            log("M4: Clicked first radio")
                    except:
                        pass
                
                # Method 5: Click visible text matching condition
                if not condition_selected:
                    for cond_text in cond_texts:
                        try:
                            el = page.get_by_text(cond_text, exact=True)
                            if el.count() > 0:
                                el.first.click()
                                condition_selected = True
                                log(f"M5: Clicked text '{cond_text}'")
                                break
                        except:
                            pass
                
                # Method 6: Coordinate-based click on text node
                if not condition_selected:
                    try:
                        coords = page.evaluate('''(targets) => {
                            const walker = document.createTreeWalker(
                                document.body, NodeFilter.SHOW_TEXT, null, false
                            );
                            while (walker.nextNode()) {
                                const text = walker.currentNode.textContent.trim();
                                for (const target of targets) {
                                    if (text === target) {
                                        const el = walker.currentNode.parentElement;
                                        const rect = el.getBoundingClientRect();
                                        if (rect.width > 0 && rect.height > 0) {
                                            return {x: rect.x + rect.width/2, y: rect.y + rect.height/2, tag: el.tagName, text: text};
                                        }
                                    }
                                }
                            }
                            return null;
                        }''', cond_texts)
                        if coords:
                            page.mouse.click(coords['x'], coords['y'])
                            condition_selected = True
                            log(f"M6: Mouse-clicked '{coords['text']}' in <{coords['tag']}> at ({coords['x']:.0f}, {coords['y']:.0f})")
                    except Exception as e:
                        log(f"M6 error: {e}")
                
                if not condition_selected:
                    log("WARNING: All condition methods failed")
            
            page.wait_for_timeout(2000)
            take_screenshot(page, 'after_condition')
            
            # ─── Stage C: Click "Continue to listing" ─────────────
            result['step'] = 'continue_to_listing'
            log("Clicking 'Continue to listing'...")
            
            continue_clicked = False
            
            # Method 1: Role-based
            for btn_text in ['Continue to listing', 'Continue', 'Next', 'Start listing']:
                try:
                    btn = page.get_by_role("button", name=btn_text)
                    if btn.first.is_visible(timeout=2000):
                        btn.first.click()
                        continue_clicked = True
                        log(f"Clicked '{btn_text}' via role")
                        break
                except:
                    continue

            # Method 2: Text locator
            if not continue_clicked:
                try:
                    btn = page.locator('button:has-text("Continue"), button:has-text("continue")').first
                    if btn.is_visible(timeout=2000):
                        btn.click()
                        continue_clicked = True
                        log("Clicked Continue via text locator")
                except:
                    pass

            # Method 3: JS click any button with "continue"
            if not continue_clicked:
                try:
                    clicked = page.evaluate('''() => {
                        const btns = document.querySelectorAll('button, [role="button"]');
                        for (const btn of btns) {
                            const txt = (btn.textContent || "").toLowerCase().trim();
                            if (txt.includes("continue") || txt.includes("next")) {
                                btn.click();
                                return txt.substring(0, 50);
                            }
                        }
                        return false;
                    }''')
                    if clicked:
                        continue_clicked = True
                        log(f"Clicked via JS: '{clicked}'")
                except Exception as e:
                    log(f"JS continue error: {e}")

            # Method 4: Submit / primary button
            if not continue_clicked:
                try:
                    btn = page.locator('button[type="submit"], .btn--primary, .btn-primary').first
                    if btn.is_visible(timeout=2000):
                        btn.click()
                        continue_clicked = True
                        log("Clicked submit/primary button")
                except:
                    pass

            if continue_clicked:
                page.wait_for_timeout(6000)
                log(f"After continue, URL: {page.url}")
                take_screenshot(page, 'listing_form')
            else:
                log("WARNING: Could not click Continue")
                take_screenshot(page, 'stuck_on_confirm')
            
            # ─── CAPTCHA Detection ────────────────────────────
            current_url = page.url
            is_captcha = False
            
            # Check URL for captcha signals
            if 'captcha' in current_url.lower():
                is_captcha = True
                log("CAPTCHA detected via URL")
            
            # Check page text for captcha signals
            if not is_captcha:
                try:
                    page_text = page.evaluate('() => document.body.innerText.substring(0, 500).toLowerCase()')
                    captcha_signals = ['verify yourself', 'i am human', 'captcha', 'hcaptcha', 'recaptcha']
                    for signal in captcha_signals:
                        if signal in page_text:
                            is_captcha = True
                            log(f"CAPTCHA detected via page text: '{signal}'")
                            break
                except:
                    pass
            
            # Check for hCaptcha/reCAPTCHA iframe
            if not is_captcha:
                try:
                    captcha_frames = page.locator('iframe[src*="captcha"], iframe[src*="hcaptcha"], iframe[src*="recaptcha"], #captcha, .h-captcha, .g-recaptcha')
                    if captcha_frames.count() > 0:
                        is_captcha = True
                        log("CAPTCHA detected via iframe/element")
                except:
                    pass
            
            if is_captcha:
                log("⚠️ CAPTCHA page detected — extracting listing URL")
                result['captcha_detected'] = True
                result['error'] = 'CAPTCHA detected'
                result['screenshot'] = take_screenshot(page, 'captcha_page')
                result['ebay_url'] = current_url
                # Extract the actual listing URL from the redirect chain
                listing_url = extract_listing_url(current_url, listing_data.get('title'))
                if listing_url:
                    result['listing_url'] = listing_url
                    log(f"Listing URL for user: {listing_url[:80]}...")
                context.close()
                return result
            
            # ─── Login Page Detection ────────────────────────────
            current_url = page.url
            is_login = False
            
            if 'signin.ebay.com' in current_url or 'SignIn' in current_url:
                is_login = True
                log("Login page detected via URL")
            
            if not is_login:
                try:
                    page_text = page.evaluate('() => document.body.innerText.substring(0, 500).toLowerCase()')
                    login_signals = ['welcome back', 'sign in', 'sign in to your account', 'continue with google']
                    for signal in login_signals:
                        if signal in page_text:
                            is_login = True
                            log(f"Login page detected via text: '{signal}'")
                            break
                except:
                    pass
            
            if is_login:
                log("⚠️ Login page detected — extracting listing URL")
                result['login_required'] = True
                result['error'] = 'eBay session expired'
                result['screenshot'] = take_screenshot(page, 'login_page')
                result['ebay_url'] = current_url
                # Extract the actual listing URL from the redirect chain
                listing_url = extract_listing_url(current_url, listing_data.get('title'))
                if listing_url:
                    result['listing_url'] = listing_url
                    log(f"Listing URL for user: {listing_url[:80]}...")
                context.close()
                return result
            
            # Check if we're still stuck on prelist
            if 'prelist' in page.url:
                result['error'] = 'Stuck on prelist page — product or condition selection failed'
                result['screenshot'] = take_screenshot(page, 'stuck_condition')
                context.close()
                return result

            # ─── Step 5: Upload images ───────────────────────────
            result['step'] = 'upload_images'
            if image_paths:
                log(f"Uploading {len(image_paths)} images...")
                upload_success = False
                try:
                    # Method 1: Direct file input (most reliable)
                    file_inputs = page.locator('input[type="file"]')
                    if file_inputs.count() > 0:
                        file_inputs.first.set_input_files(image_paths)
                        log(f"Uploaded {len(image_paths)} images via file input")
                        upload_success = True
                        page.wait_for_timeout(3000 + (len(image_paths) * 2000))
                except Exception as e:
                    log(f"File input upload failed: {e}")

                if not upload_success:
                    # Method 2: Try clicking upload areas one at a time
                    upload_selectors = [
                        '[data-testid="photos-upload"]',
                        '.photo-upload',
                        'button[aria-label*="photo"]',
                        'button[aria-label*="Photo"]',
                        'button[aria-label*="image"]',
                        '[data-testid="add-photos"]',
                    ]
                    for sel in upload_selectors:
                        try:
                            el = page.locator(sel).first
                            if el.is_visible(timeout=2000):
                                with page.expect_file_chooser(timeout=5000) as fc:
                                    el.click()
                                file_chooser = fc.value
                                file_chooser.set_files(image_paths)
                                log(f"Uploaded via file chooser ({sel})")
                                upload_success = True
                                page.wait_for_timeout(3000 + (len(image_paths) * 2000))
                                break
                        except:
                            continue

                if not upload_success:
                    # Method 3: Try text-based buttons
                    try:
                        add_photos_btn = page.get_by_text('Add photos', exact=False)
                        if add_photos_btn.first.is_visible(timeout=2000):
                            with page.expect_file_chooser(timeout=5000) as fc:
                                add_photos_btn.first.click()
                            file_chooser = fc.value
                            file_chooser.set_files(image_paths)
                            log("Uploaded via 'Add photos' text button")
                            upload_success = True
                            page.wait_for_timeout(3000 + (len(image_paths) * 2000))
                    except Exception as e:
                        log(f"Text-based upload failed: {e}")

                if not upload_success:
                    log("WARNING: Could not upload images — no upload method worked")
                    take_screenshot(page, 'image_upload_failed')

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
            shipping_set = False

            try:
                # eBay's Modern Listing Tool uses a dropdown/radio for "Who pays?"
                # Method A: Click "I'll pay" (Free Shipping for buyer)
                try:
                    ill_pay_radio = page.get_by_text("I'll pay").locator("xpath=ancestor::label")
                    if ill_pay_radio.count() > 0 and ill_pay_radio.first.is_visible(timeout=2000):
                        ill_pay_radio.first.click()
                        shipping_set = True
                        log("Shipping: Clicked \"I'll pay\" (Free Shipping)")
                except:
                    pass

                # Method B: Click any label containing "Free"
                if not shipping_set:
                    try:
                        free_label = page.get_by_text("Free shipping", exact=False).locator("xpath=ancestor::label | ancestor::button")
                        if free_label.count() > 0 and free_label.first.is_visible(timeout=2000):
                            free_label.first.click()
                            shipping_set = True
                            log("Shipping: Clicked \"Free shipping\" label/button")
                    except:
                        pass

                # Method C: Find shipping cost input and set to 0
                if not shipping_set:
                    try:
                        cost_inputs = page.locator('input[aria-label*="cost"], input[name*="shipping"]')
                        if cost_inputs.count() > 0 and cost_inputs.first.is_visible(timeout=2000):
                            cost_inputs.first.fill('0')
                            shipping_set = True
                            log("Shipping: Manually set cost to 0")
                    except:
                        pass

                if not shipping_set:
                    log("WARNING: Could not set free shipping — none of the methods worked")
                    take_screenshot(page, 'shipping_failed')

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

    # Check for --inject-cookies flag (headless session setup via cookie paste)
    if '--inject-cookies' in sys.argv:
        log("Injecting eBay cookies into browser profile...")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print(json.dumps({"error": "Playwright not installed"}))
            sys.exit(1)

        # Read cookie JSON from stdin
        try:
            raw = sys.stdin.read().strip()
            if not raw:
                print(json.dumps({"error": "No cookie data provided on stdin"}))
                sys.exit(1)
            cookies_input = json.loads(raw)
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"Invalid JSON: {e}"}))
            sys.exit(1)

        # Normalize cookies — support multiple formats
        cookies = []
        if isinstance(cookies_input, list):
            for c in cookies_input:
                cookie = {
                    "name": c.get("name", ""),
                    "value": c.get("value", ""),
                    "domain": c.get("domain", ".ebay.com"),
                    "path": c.get("path", "/"),
                }
                # Only include non-empty cookies
                if cookie["name"] and cookie["value"]:
                    cookies.append(cookie)
        elif isinstance(cookies_input, dict):
            # Simple {name: value} format
            for name, value in cookies_input.items():
                if name and value:
                    cookies.append({
                        "name": name,
                        "value": str(value),
                        "domain": ".ebay.com",
                        "path": "/",
                    })

        if not cookies:
            print(json.dumps({"error": "No valid cookies found in input"}))
            sys.exit(1)

        log(f"Parsed {len(cookies)} cookies to inject")

        with sync_playwright() as p:
            context = launch_browser(p, headless=True)
            page = context.pages[0] if context.pages else context.new_page()

            # Navigate to eBay first (required for cookie domain)
            page.goto("https://www.ebay.com", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1000)

            # Inject cookies
            context.add_cookies(cookies)
            log(f"Injected {len(cookies)} cookies")

            # Reload to apply cookies
            page.reload(wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)

            # Verify login
            logged_in = check_login(page)
            screenshot_path = take_screenshot(page, 'cookie_inject')
            context.close()

        print(json.dumps({
            "success": logged_in,
            "logged_in": logged_in,
            "screenshot": screenshot_path,
            "cookies_injected": len(cookies),
            "message": "eBay session established!" if logged_in else "Cookies injected but login not confirmed. Try fresh cookies.",
        }))
        sys.exit(0 if logged_in else 1)

    # Fill mode: read listing data from stdin JSON (passed by backend)
    if '--fill' in sys.argv:
        try:
            raw = sys.stdin.read().strip()
            if not raw:
                print(json.dumps({"error": "No listing data provided on stdin"}))
                sys.exit(1)
            listing = json.loads(raw)
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"Invalid listing JSON: {e}"}))
            sys.exit(1)

        log(f"Listing loaded from stdin: {listing.get('title', '')[:50]}")

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print(json.dumps({"error": "Playwright not installed. Run: pip install playwright && playwright install chromium"}))
            sys.exit(1)

        # Save images to temp files
        image_paths = save_images_to_temp(listing.get('images', []))
        log(f"Prepared {len(image_paths)} images for upload")

        try:
            result = fill_listing_on_ebay(listing, image_paths)
            print(json.dumps(result))
        finally:
            cleanup_temp_images(image_paths)

        sys.exit(0 if result.get('success') else 1)

    # Legacy mode: read listing_id from args (for manual CLI testing)
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        listing_id = int(sys.argv[1])
    else:
        print(json.dumps({"error": "Usage: python ebay_playwright.py --fill (with JSON stdin) | --login | --check-session | --inject-cookies"}))
        sys.exit(1)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(json.dumps({"error": "Playwright not installed"}))
        sys.exit(1)

    log(f"Loading listing #{listing_id} from database...")
    listing = load_listing_from_db(listing_id)
    if not listing:
        print(json.dumps({"error": f"Listing #{listing_id} not found"}))
        sys.exit(1)

    log(f"Listing loaded: {listing.get('title', '')[:50]}")

    image_paths = save_images_to_temp(listing.get('images', []))
    log(f"Prepared {len(image_paths)} images for upload")

    try:
        result = fill_listing_on_ebay(listing, image_paths)
        print(json.dumps(result))
    finally:
        cleanup_temp_images(image_paths)

    sys.exit(0 if result.get('success') else 1)


if __name__ == "__main__":
    main()
