#!/usr/bin/env python3
"""
Clerk 2FA Authentication Flow Handler.
Manages the semi-automated 2FA auth flow for TikTokCopilot.

Uses SQLite for persistent session storage (survives deploys).

Usage:
    python clerk_auth.py initiate
    python clerk_auth.py verify <code>
    python clerk_auth.py status
    python clerk_auth.py fetch <api_url>

Outputs JSON to stdout.
"""

import os
import sys
import json
import time
import sqlite3
import threading

# Thread lock to protect Playwright operations (sync API is not thread-safe)
_playwright_lock = threading.Lock()

# Persistent Playwright session (survives between initiate and verify calls)
_playwright = None
_browser = None
_context = None
_page = None

# Use the same DB path as the main app
basedir = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(basedir, 'products.db')
BASE_URL = "https://www.tiktokcopilot.com"


def get_page(force_new=False):
    """Get or create the persistent Playwright page.
    
    Uses sync_playwright().start() to keep browser alive between calls.
    This is critical for 2FA flow where signIn state must persist from
    initiate to verify.
    """
    global _playwright, _browser, _context, _page
    
    from playwright.sync_api import sync_playwright
    
    # Force new session if requested (e.g., after successful auth)
    if force_new and _browser:
        try:
            _browser.close()
        except:
            pass
        try:
            _playwright.stop()
        except:
            pass
        _playwright = None
        _browser = None
        _context = None
        _page = None
    
    # Create new browser if needed
    if _page is None:
        _playwright = sync_playwright().start()
        _browser = _playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        _context = _browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        _page = _context.new_page()
        _page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
        # Wait for Clerk SDK to be ready
        try:
            _page.wait_for_function("() => window.Clerk && window.Clerk.client", timeout=20000)
        except:
            pass  # Will check again in the actual functions
    
    return _page


def close_page():
    """Close the persistent browser session."""
    global _playwright, _browser, _context, _page
    
    if _browser:
        try:
            _browser.close()
        except:
            pass
    if _playwright:
        try:
            _playwright.stop()
        except:
            pass
    
    _playwright = None
    _browser = None
    _context = None
    _page = None


def get_db_connection():
    """Get a SQLite connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_session_table():
    """Create the session storage table if it doesn't exist."""
    conn = get_db_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS copilot_session (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at REAL
        )
    """)
    conn.commit()
    conn.close()


def save_session_data(key, value):
    """Save a key-value pair to the session table."""
    ensure_session_table()
    conn = get_db_connection()
    conn.execute("""
        INSERT OR REPLACE INTO copilot_session (key, value, updated_at)
        VALUES (?, ?, ?)
    """, (key, json.dumps(value), time.time()))
    conn.commit()
    conn.close()


def get_session_data(key, max_age_days=14):
    """Get a value from the session table. Returns None if expired or missing."""
    ensure_session_table()
    conn = get_db_connection()
    row = conn.execute(
        "SELECT value, updated_at FROM copilot_session WHERE key = ?", (key,)
    ).fetchone()
    conn.close()
    
    if not row:
        return None
    
    # Check age
    if time.time() - row['updated_at'] > max_age_days * 24 * 60 * 60:
        return None
    
    return json.loads(row['value'])


def delete_session_data(key):
    """Delete a key from the session table."""
    ensure_session_table()
    conn = get_db_connection()
    conn.execute("DELETE FROM copilot_session WHERE key = ?", (key,))
    conn.commit()
    conn.close()


def initiate_login(email, password):
    """Start the login flow, returns sign_in_id on 2FA requirement.
    
    Uses persistent browser so signIn state survives until verify_code is called.
    """
    global _context
    
    # Acquire lock to prevent concurrent Playwright operations (not thread-safe)
    with _playwright_lock:
        # Start fresh browser for new login attempt
        page = get_page(force_new=True)
        
        # Ensure Clerk SDK is ready
        try:
            page.wait_for_function("() => window.Clerk && window.Clerk.client", timeout=10000)
        except:
            return {"error": "Clerk SDK not ready - page may not have loaded correctly"}
        
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
                    // Get available 2FA methods
                    const methods = si.supportedSecondFactors?.map(f => f.strategy) || [];
                    
                    // Try to prepare email_code (this triggers sending the email)
                    if (methods.includes('email_code')) {
                        try {
                            await si.prepareSecondFactor({ strategy: 'email_code' });
                            return { 
                                status: 'needs_2fa',
                                signInId: si.id,
                                methods: methods,
                                message: 'Email code sent! Check your inbox.'
                            };
                        } catch (prepErr) {
                            return { 
                                status: 'needs_2fa',
                                signInId: si.id,
                                methods: methods,
                                prepareError: prepErr.message || String(prepErr)
                            };
                        }
                    }
                    
                    // Fallback if email_code not available
                    return { 
                        status: 'needs_2fa',
                        signInId: si.id,
                        methods: methods,
                        message: 'Email code not available. Methods: ' + methods.join(', ')
                    };
                }
                
                return { status: si.status };
            } catch (err) {
                return { error: err.message || String(err) };
            }
        }""", [email, password])

        
        if result.get("status") == "complete":
            # No 2FA, save cookies immediately
            cookies = _context.cookies()
            save_session_data("cookies", cookies)
            close_page()  # Clean up browser
            return {"status": "complete", "message": "Login successful, session saved to database"}
        
        if result.get("status") == "needs_2fa":
            # Save the sign-in ID for later verification
            # IMPORTANT: Keep browser alive - verify_code will use same page
            save_session_data("pending_signin", {
                "sign_in_id": result.get("signInId"),
                "email": email,
                "timestamp": time.time()
            })
            # DON'T close browser - need same page for verify step
            return {
                "status": "needs_2fa",
                "signInId": result.get("signInId"),
                "methods": result.get("methods"),
                "message": "Check your email for the verification code, then use /api/copilot-auth/verify"
            }
        
        close_page()  # Clean up on error
        return result


def verify_code(code):
    """Complete 2FA verification with the email code.
    
    Uses the SAME persistent browser/page from initiate_login so
    Clerk's signIn state is preserved.
    """
    global _context, _page
    
    state = get_session_data("pending_signin", max_age_days=1)  # Sign-in attempts expire in 1 day
    if not state:
        return {"error": "No pending sign-in. Please call /api/copilot-auth/initiate first."}
    
    # Acquire lock to prevent concurrent Playwright operations (not thread-safe)
    with _playwright_lock:
        # Get the SAME page that was used for initiate (with signIn state)
        if _page is None:
            return {"error": "Browser session expired. Please restart login."}
        
        page = _page
        
        # Verify 2FA code using the existing signIn state
        result = page.evaluate("""async (code) => {
            try {
                // Check if we have an active signIn object in Clerk
                if (!window.Clerk || !window.Clerk.client || !window.Clerk.client.signIn) {
                    return { error: 'No active sign-in state. Please restart login.' };
                }
                
                const si = window.Clerk.client.signIn;
                
                // Attempt 2FA verification with the code
                const verifyResult = await si.attemptSecondFactor({
                    strategy: 'email_code',
                    code: code
                });
                
                if (verifyResult.status === 'complete') {
                    await window.Clerk.setActive({ session: verifyResult.createdSessionId });
                    return { 
                        status: 'complete', 
                        sessionId: verifyResult.createdSessionId 
                    };
                }
                
                return { 
                    status: verifyResult.status,
                    error: 'Verification incomplete: ' + verifyResult.status
                };
            } catch (err) {
                return { error: err.message || String(err) };
            }
        }""", code)

        
        if result.get("status") == "complete":
            # Save session cookies to database
            page.wait_for_timeout(2000)  # Let cookies settle
            cookies = _context.cookies()
            save_session_data("cookies", cookies)
            
            # Clean up pending sign-in
            delete_session_data("pending_signin")
            
            session_cookies = [c["name"] for c in cookies if "__session" in c["name"]]
            
            # Close browser now that we have saved cookies
            close_page()
            
            return {
                "status": "complete",
                "message": "2FA verified! Session saved to database. You can now use the API.",
                "sessionCookies": session_cookies
            }
        
        # Don't close on error - let user retry with same browser
        return result


def get_status():
    """Check current auth status."""
    cookies = get_session_data("cookies")
    pending = get_session_data("pending_signin", max_age_days=1)
    
    status = {
        "hasSavedSession": cookies is not None,
        "sessionValid": False,
        "pendingSignIn": pending is not None
    }
    
    if cookies:
        session_cookies = [c["name"] for c in cookies if "__session" in c["name"]]
        status["sessionCookies"] = session_cookies
        status["sessionValid"] = len(session_cookies) > 0
    
    if pending:
        status["pendingEmail"] = pending.get("email")
    
    return status


def fetch_api(api_url):
    """Fetch from the TikTokCopilot API using saved session cookies."""
    from playwright.sync_api import sync_playwright
    
    cookies = get_session_data("cookies")
    if not cookies:
        return {"error": "No saved session. Please authenticate first via /api/copilot-auth/initiate"}
    
    # Acquire lock to prevent concurrent Playwright operations (not thread-safe)
    with _playwright_lock:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--single-process"],
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080}
            )
            
            # Inject saved cookies
            context.add_cookies(cookies)
            
            page = context.new_page()
            
            # Navigate to establish cookie context
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)
            
            # Verify session
            current_cookies = context.cookies()
            session_cookies = [c for c in current_cookies if "__session" in c["name"]]
            
            if not session_cookies:
                browser.close()
                return {"error": "Session cookies expired. Please re-authenticate."}
            
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
                return {"error": result["error"]}
            
            if not result.get("ok"):
                return {"error": f"HTTP {result.get('status')}", "body": str(result.get('body', ''))[:500]}
            
            return {"success": True, "data": result.get("body", {})}


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: clerk_auth.py <initiate|verify|status|fetch> [args]"}))
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
    
    elif command == "fetch":
        if len(sys.argv) < 3:
            print(json.dumps({"error": "Usage: clerk_auth.py fetch <api_url>"}))
            sys.exit(1)
        api_url = sys.argv[2]
        result = fetch_api(api_url)
        print(json.dumps(result))
    
    else:
        print(json.dumps({"error": f"Unknown command: {command}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
