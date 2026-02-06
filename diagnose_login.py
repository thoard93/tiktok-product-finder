#!/usr/bin/env python3
"""
Diagnostic script for TikTokCopilot login.
Checks for iframes and common Clerk elements.
"""

import os
import sys
import json
from playwright.sync_api import sync_playwright

def main():
    try:
        with sync_playwright() as p:
            # Launch browser
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            
            print(f"Navigating to login page...")
            page.goto("https://www.tiktokcopilot.com/?auth=sign-in", wait_until="domcontentloaded", timeout=60000)
            
            # Wait for any Clerk-related loading to settle
            page.wait_for_timeout(5000)
            
            print(f"Current URL: {page.url}")
            
            # 1. Check for iframes
            frames = page.frames
            print(f"Total frames found: {len(frames)}")
            for i, frame in enumerate(frames):
                print(f"Frame {i}: name='{frame.name}', url='{frame.url}'")
            
            # 2. Check for common login elements in all frames
            print("\nSearching for login elements in all frames...")
            for frame in page.frames:
                email_inputs = frame.locator('input[type="email"], input[name="identifier"]').count()
                pass_inputs = frame.locator('input[type="password"]').count()
                buttons = frame.locator('button').count()
                
                if email_inputs > 0 or pass_inputs > 0:
                    print(f"Found match in frame '{frame.name or '(main)'}':")
                    print(f"  - Email inputs: {email_inputs}")
                    print(f"  - Password inputs: {pass_inputs}")
                    print(f"  - Buttons: {buttons}")
                    
                    # Log some HTML if found
                    try:
                        html = frame.locator('form').first.inner_html() if frame.locator('form').count() > 0 else "No form tag found"
                        print(f"  - Form HTML Snippet: {html[:500]}...")
                    except:
                        pass
            
            # 3. List all input elements on the main page for debugging
            print("\nAll input elements on main page:")
            inputs = page.locator('input').all()
            for inp in inputs:
                try:
                    name = inp.get_attribute('name') or '(no name)'
                    typ = inp.get_attribute('type') or '(no type)'
                    placeholder = inp.get_attribute('placeholder') or '(no placeholder)'
                    print(f"  - name={name}, type={typ}, placeholder={placeholder}")
                except:
                    pass
            
            browser.close()
            
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
