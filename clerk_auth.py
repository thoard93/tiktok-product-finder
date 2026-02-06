#!/usr/bin/env python3
"""
Clerk 2FA Authentication Flow Handler.
Manages the semi-automated 2FA auth flow for TikTokCopilot.

Usage:
    python clerk_auth.py initiate <email> <password>
    python clerk_auth.py verify <sign_in_id> <code>
    python clerk_auth.py status

Outputs JSON to stdout.
"""

import os
import sys
import json
import time

COOKIES_FILE = "/tmp/copilot_session.json"
SIGNIN_STATE_FILE = "/tmp/copilot_signin_state.json"
BASE_URL = "https://www.tiktokcopilot.com"


def save_state(sign_in_id, email):
    """Save the sign-in state for later verification."""
    with open(SIGNIN_STATE_FILE, 'w') as f:
        json.dump({
            "sign_in_id": sign_in_id,
            "email": email,
            "timestamp": time.time()
        }, f)


def load_state():
    """Load the pending sign-in state."""
    try:
        with open(SIGNIN_STATE_FILE, 'r') as f:
            return json.load(f)
    except:
        return None


def save_cookies(cookies):
    """Persist session cookies to disk."""
    with open(COOKIES_FILE, 'w') as f:
        json.dump({
            "cookies": cookies,
            "timestamp": time.time()
        }, f)


def load_cookies():
    """Load persisted session cookies."""
    try:
        with open(COOKIES_FILE, 'r') as f:
            data = json.load(f)
            # Check if cookies are less than 14 days old
            if time.time() - data.get("timestamp", 0) < 14 * 24 * 60 * 60:
                return data.get("cookies", [])
    except:
        pass
    return None


def initiate_login(email, password):
    """Start the login flow, returns sign_in_id on 2FA requirement."""
    from playwright.sync_api import sync_playwright
    
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            viewport={"width": 1920, "height": 1080},
        )
        page = context.new_page()
        
        # Load main page
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
        
        # Wait for Clerk to be ready
        try:
            page.wait_for_function("() => window.Clerk && window.Clerk.client", timeout=20000)
        except:
            browser.close()
            return {"error": "Clerk SDK not ready"}
        
        # Attempt sign-in
        result = page.evaluate("""async ([email, password]) => {
            try {
                const si = await window.Clerk.client.signIn.create({
                    identifier: email,
                    password: password,
                    strategy: 'password'
                });
                
                if (si.status === 'complete') {
                    await window.Clerk.setActive({ session: si.createdSessionId });
                    return { 
                        status: 'complete', 
                        sessionId: si.createdSessionId 
                    };
                }
                
                if (si.status === 'needs_second_factor') {
                    return { 
                        status: 'needs_2fa',
                        signInId: si.id,
                        methods: si.supportedSecondFactors?.map(f => f.strategy) || ['email_code']
                    };
                }
                
                return { status: si.status };
            } catch (err) {
                return { error: err.message || String(err) };
            }
        }""", [email, password])
        
        if result.get("status") == "complete":
            # No 2FA, save cookies immediately
            cookies = context.cookies()
            save_cookies(cookies)
            browser.close()
            return {"status": "complete", "message": "Login successful, session saved"}
        
        if result.get("status") == "needs_2fa":
            # Save the sign-in ID for later verification
            save_state(result.get("signInId"), email)
            browser.close()
            return {
                "status": "needs_2fa",
                "signInId": result.get("signInId"),
                "methods": result.get("methods"),
                "message": "Check your email for the verification code"
            }
        
        browser.close()
        return result


def verify_code(code):
    """Complete 2FA verification with the email code."""
    from playwright.sync_api import sync_playwright
    
    state = load_state()
    if not state:
        return {"error": "No pending sign-in. Please initiate login first."}
    
    sign_in_id = state.get("sign_in_id")
    
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            viewport={"width": 1920, "height": 1080},
        )
        page = context.new_page()
        
        # Load main page to get Clerk SDK
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
        
        try:
            page.wait_for_function("() => window.Clerk && window.Clerk.client", timeout=20000)
        except:
            browser.close()
            return {"error": "Clerk SDK not ready"}
        
        # Complete 2FA
        result = page.evaluate("""async ([signInId, code]) => {
            try {
                // Get the pending sign-in
                const signInAttempt = await window.Clerk.client.signIn.attemptSecondFactor({
                    strategy: 'email_code',
                    code: code
                });
                
                if (signInAttempt.status === 'complete') {
                    await window.Clerk.setActive({ session: signInAttempt.createdSessionId });
                    return { 
                        status: 'complete', 
                        sessionId: signInAttempt.createdSessionId 
                    };
                }
                
                return { 
                    status: signInAttempt.status,
                    error: 'Verification incomplete'
                };
            } catch (err) {
                return { error: err.message || String(err) };
            }
        }""", [sign_in_id, code])
        
        if result.get("status") == "complete":
            # Save session cookies
            page.wait_for_timeout(2000)  # Let cookies settle
            cookies = context.cookies()
            save_cookies(cookies)
            
            # Clean up state file
            try:
                os.remove(SIGNIN_STATE_FILE)
            except:
                pass
            
            browser.close()
            return {
                "status": "complete",
                "message": "2FA verified! Session saved.",
                "cookieCount": len([c for c in cookies if "__session" in c["name"]])
            }
        
        browser.close()
        return result


def get_status():
    """Check current auth status."""
    cookies = load_cookies()
    pending = load_state()
    
    status = {
        "hasSavedSession": cookies is not None,
        "pendingSignIn": pending is not None
    }
    
    if cookies:
        session_cookies = [c["name"] for c in cookies if "__session" in c["name"]]
        status["sessionCookies"] = session_cookies
    
    if pending:
        status["pendingEmail"] = pending.get("email")
        status["pendingSince"] = pending.get("timestamp")
    
    return status


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: clerk_auth.py <initiate|verify|status> [args]"}))
        sys.exit(1)
    
    command = sys.argv[1]
    
    if command == "initiate":
        email = os.environ.get('COPILOT_EMAIL', '').strip()
        password = os.environ.get('COPILOT_PASSWORD', '').strip()
        if not email or not password:
            print(json.dumps({"error": "COPILOT_EMAIL and COPILOT_PASSWORD env vars required"}))
            sys.exit(1)
        result = initiate_login(email, password)
        print(json.dumps(result))
    
    elif command == "verify":
        if len(sys.argv) < 3:
            print(json.dumps({"error": "Usage: clerk_auth.py verify <code>"}))
            sys.exit(1)
        code = sys.argv[2]
        result = verify_code(code)
        print(json.dumps(result))
    
    elif command == "status":
        result = get_status()
        print(json.dumps(result))
    
    else:
        print(json.dumps({"error": f"Unknown command: {command}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
