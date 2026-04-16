#!/usr/bin/env python3
"""
Vantage Website Audit Script
Crawls thoardburgersauce.com, checks every page for issues,
and generates a styled audit_report.html.
"""

import time
import json
import base64
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright

BASE_URL = "https://thoardburgersauce.com"
PASSKEY = "Batman7193!"

# Pages to audit (public + authenticated)
PUBLIC_PAGES = [
    ("/", "Landing Page"),
    ("/login", "Login Page"),
]

AUTH_PAGES = [
    ("/app/dashboard", "Dashboard"),
    ("/app/products", "Products"),
    ("/app/analytics", "Analytics"),
    ("/app/brands", "Brand Hunter"),
    ("/app/favorites", "Favorites"),
    ("/app/settings", "Settings"),
    ("/app/subscribe", "Subscribe"),
    ("/app/redeem", "Redeem Code"),
    ("/app/tap-lists", "Boosted Lists"),
    ("/app/admin", "Admin Panel"),
    ("/app/admin/campaigns", "Admin Campaigns"),
    ("/app/admin/coupons", "Admin Coupons"),
]

MOBILE_WIDTH = 375
MOBILE_HEIGHT = 812
DESKTOP_WIDTH = 1440
DESKTOP_HEIGHT = 900


def login(page):
    """Login via developer passkey."""
    print("[Auth] Logging in via passkey...")
    page.goto(f"{BASE_URL}/login", wait_until="networkidle", timeout=30000)
    time.sleep(1)

    # Use the passkey API endpoint directly
    resp = page.request.post(
        f"{BASE_URL}/auth/passkey",
        data=json.dumps({"passkey": PASSKEY}),
        headers={"Content-Type": "application/json"},
    )
    if resp.status == 200:
        data = resp.json()
        if data.get("success"):
            print("[Auth] Passkey login successful")
            # Navigate to dashboard to establish session cookies in browser context
            page.goto(f"{BASE_URL}/app/dashboard", wait_until="networkidle", timeout=30000)
            return True
    print(f"[Auth] Passkey login failed: {resp.status}")
    return False


def audit_page(page, path, label, screenshots, is_mobile=False):
    """Audit a single page and return findings dict."""
    url = f"{BASE_URL}{path}"
    result = {
        "path": path,
        "label": label,
        "url": url,
        "status": None,
        "load_time_ms": None,
        "title": None,
        "meta_description": None,
        "console_errors": [],
        "images_no_alt": [],
        "broken_links": [],
        "missing_labels": [],
        "issues": [],
        "screenshot_desktop": None,
        "screenshot_mobile": None,
    }

    # Collect console errors
    console_errors = []
    page.on("console", lambda msg: console_errors.append(f"[{msg.type}] {msg.text}") if msg.type in ("error", "warning") else None)

    try:
        # Navigate and measure load time
        start = time.time()
        response = page.goto(url, wait_until="networkidle", timeout=30000)
        load_time = (time.time() - start) * 1000
        result["load_time_ms"] = round(load_time)
        result["status"] = response.status if response else None

        if response and response.status >= 400:
            result["issues"].append(f"HTTP {response.status} error")

        # Wait for page to settle
        time.sleep(0.5)

        # Title tag
        title = page.title()
        result["title"] = title
        if not title or title.strip() == "":
            result["issues"].append("Missing <title> tag")
        elif len(title) > 70:
            result["issues"].append(f"Title too long ({len(title)} chars, max 70)")

        # Meta description
        meta_desc = page.evaluate("""
            () => {
                const el = document.querySelector('meta[name="description"]');
                return el ? el.getAttribute('content') : null;
            }
        """)
        result["meta_description"] = meta_desc
        if not meta_desc:
            result["issues"].append("Missing meta description")

        # Open Graph tags
        og_title = page.evaluate("() => document.querySelector('meta[property=\"og:title\"]')?.content")
        og_image = page.evaluate("() => document.querySelector('meta[property=\"og:image\"]')?.content")
        if not og_title:
            result["issues"].append("Missing og:title meta tag")
        if not og_image:
            result["issues"].append("Missing og:image meta tag")

        # Images without alt text
        images_no_alt = page.evaluate("""
            () => {
                const imgs = document.querySelectorAll('img');
                const missing = [];
                imgs.forEach(img => {
                    if (!img.alt || img.alt.trim() === '') {
                        missing.push({src: img.src?.substring(0, 100), width: img.width, height: img.height});
                    }
                });
                return missing;
            }
        """)
        result["images_no_alt"] = images_no_alt or []
        if images_no_alt and len(images_no_alt) > 0:
            result["issues"].append(f"{len(images_no_alt)} images missing alt text")

        # Form inputs without labels
        missing_labels = page.evaluate("""
            () => {
                const inputs = document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="checkbox"]):not([type="radio"]), select, textarea');
                const missing = [];
                inputs.forEach(input => {
                    const id = input.id;
                    const hasLabel = id && document.querySelector(`label[for="${id}"]`);
                    const hasAriaLabel = input.getAttribute('aria-label');
                    const hasPlaceholder = input.getAttribute('placeholder');
                    const parentLabel = input.closest('label');
                    if (!hasLabel && !hasAriaLabel && !parentLabel) {
                        missing.push({
                            tag: input.tagName.toLowerCase(),
                            name: input.name || input.id || '(unnamed)',
                            placeholder: hasPlaceholder || ''
                        });
                    }
                });
                return missing;
            }
        """)
        result["missing_labels"] = missing_labels or []
        if missing_labels and len(missing_labels) > 0:
            result["issues"].append(f"{len(missing_labels)} form inputs missing labels")

        # Buttons without accessible names
        no_aria_buttons = page.evaluate("""
            () => {
                const btns = document.querySelectorAll('button, [role="button"]');
                let count = 0;
                btns.forEach(btn => {
                    const text = btn.textContent?.trim();
                    const ariaLabel = btn.getAttribute('aria-label');
                    const title = btn.getAttribute('title');
                    if (!text && !ariaLabel && !title) count++;
                });
                return count;
            }
        """)
        if no_aria_buttons > 0:
            result["issues"].append(f"{no_aria_buttons} buttons without accessible names")

        # Console errors
        result["console_errors"] = [e for e in console_errors if "error" in e.lower()]
        if result["console_errors"]:
            result["issues"].append(f"{len(result['console_errors'])} console errors")

        # Check load time
        if load_time > 5000:
            result["issues"].append(f"Slow load: {round(load_time)}ms (>5s)")
        elif load_time > 3000:
            result["issues"].append(f"Moderate load: {round(load_time)}ms (>3s)")

        # Desktop screenshot
        page.set_viewport_size({"width": DESKTOP_WIDTH, "height": DESKTOP_HEIGHT})
        time.sleep(0.3)
        desktop_bytes = page.screenshot(full_page=True, type="jpeg", quality=60)
        result["screenshot_desktop"] = base64.b64encode(desktop_bytes).decode()

        # Mobile screenshot
        page.set_viewport_size({"width": MOBILE_WIDTH, "height": MOBILE_HEIGHT})
        time.sleep(0.5)
        mobile_bytes = page.screenshot(full_page=True, type="jpeg", quality=60)
        result["screenshot_mobile"] = base64.b64encode(mobile_bytes).decode()

        # Check for mobile overflow (horizontal scroll)
        has_overflow = page.evaluate("""
            () => document.documentElement.scrollWidth > window.innerWidth
        """)
        if has_overflow:
            result["issues"].append("Horizontal overflow on mobile viewport (375px)")

        # Reset viewport
        page.set_viewport_size({"width": DESKTOP_WIDTH, "height": DESKTOP_HEIGHT})

    except Exception as e:
        result["issues"].append(f"Page error: {str(e)[:200]}")
        result["status"] = "ERROR"

    return result


def check_broken_links(page, pages_audited):
    """Check all unique links found across pages for 404s."""
    print("[Links] Checking for broken links...")
    all_links = set()

    for result in pages_audited:
        page.goto(result["url"], wait_until="networkidle", timeout=30000)
        links = page.evaluate("""
            () => {
                const anchors = document.querySelectorAll('a[href]');
                return Array.from(anchors)
                    .map(a => a.href)
                    .filter(h => h.startsWith('http'));
            }
        """)
        for link in (links or []):
            parsed = urlparse(link)
            if parsed.netloc and "thoardburgersauce.com" in parsed.netloc:
                all_links.add(link)

    broken = []
    for link in sorted(all_links):
        try:
            resp = page.request.get(link, timeout=10000)
            if resp.status >= 400:
                broken.append({"url": link, "status": resp.status})
        except Exception:
            broken.append({"url": link, "status": "TIMEOUT"})

    return broken


def generate_report(results, broken_links, output_path="audit_report.html"):
    """Generate styled HTML audit report."""
    now = datetime.now().strftime("%B %d, %Y at %I:%M %p")
    total_issues = sum(len(r["issues"]) for r in results)
    pages_clean = sum(1 for r in results if len(r["issues"]) == 0)

    def badge(text, color):
        return f'<span style="display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:700;background:{color};color:#fff">{text}</span>'

    def status_badge(status):
        if status == 200:
            return badge("200 OK", "#0d9488")
        elif status and status < 400:
            return badge(f"{status}", "#d97706")
        else:
            return badge(f"{status}", "#dc2626")

    rows = ""
    detail_sections = ""
    for i, r in enumerate(results):
        issue_count = len(r["issues"])
        color = "#0d9488" if issue_count == 0 else ("#d97706" if issue_count <= 3 else "#dc2626")
        load_badge = ""
        if r["load_time_ms"]:
            lc = "#0d9488" if r["load_time_ms"] < 3000 else ("#d97706" if r["load_time_ms"] < 5000 else "#dc2626")
            load_badge = f'<span style="color:{lc};font-weight:600">{r["load_time_ms"]}ms</span>'

        rows += f"""
        <tr>
            <td style="font-weight:700">{r['label']}</td>
            <td><code style="font-size:12px">{r['path']}</code></td>
            <td>{status_badge(r['status'])}</td>
            <td>{load_badge}</td>
            <td>{badge(f'{issue_count} issues', color)}</td>
            <td>{len(r['images_no_alt'])}</td>
            <td>{len(r['missing_labels'])}</td>
            <td><a href="#detail-{i}" style="color:#2563eb">Details</a></td>
        </tr>"""

        # Detail section with screenshots
        issues_html = ""
        if r["issues"]:
            issues_html = "<ul>" + "".join(f"<li>{iss}</li>" for iss in r["issues"]) + "</ul>"
        else:
            issues_html = '<p style="color:#0d9488;font-weight:600">No issues found</p>'

        console_html = ""
        if r["console_errors"]:
            console_html = '<h4>Console Errors</h4><pre style="background:#1e1e1e;color:#f44;padding:12px;border-radius:8px;overflow-x:auto;font-size:11px;max-height:200px">' + "\n".join(r["console_errors"][:20]) + '</pre>'

        imgs_html = ""
        if r["images_no_alt"]:
            imgs_html = '<h4>Images Missing Alt Text</h4><ul style="font-size:12px">' + "".join(f'<li><code>{img.get("src","?")[:80]}</code> ({img.get("width",0)}x{img.get("height",0)})</li>' for img in r["images_no_alt"][:10]) + '</ul>'

        labels_html = ""
        if r["missing_labels"]:
            labels_html = '<h4>Form Inputs Missing Labels</h4><ul style="font-size:12px">' + "".join(f'<li>&lt;{inp["tag"]}&gt; name="{inp["name"]}" placeholder="{inp["placeholder"]}"</li>' for inp in r["missing_labels"][:10]) + '</ul>'

        desktop_img = f'<img src="data:image/jpeg;base64,{r["screenshot_desktop"]}" style="width:100%;max-width:700px;border:1px solid #ddd;border-radius:8px">' if r.get("screenshot_desktop") else ""
        mobile_img = f'<img src="data:image/jpeg;base64,{r["screenshot_mobile"]}" style="width:200px;border:1px solid #ddd;border-radius:8px">' if r.get("screenshot_mobile") else ""

        detail_sections += f"""
        <div id="detail-{i}" style="margin-bottom:48px;padding-top:24px;border-top:1px solid #e5e7eb">
            <h3 style="margin-bottom:4px">{r['label']} <code style="font-size:13px;color:#6b7280">{r['path']}</code></h3>
            <div style="display:flex;gap:12px;margin-bottom:12px;flex-wrap:wrap">
                {status_badge(r['status'])}
                {load_badge}
                {badge(f'{len(r["issues"])} issues', color)}
                <span style="font-size:12px;color:#9ca3af">Title: {(r['title'] or 'MISSING')[:60]}</span>
            </div>
            <h4>Issues</h4>
            {issues_html}
            {console_html}
            {imgs_html}
            {labels_html}
            <h4 style="margin-top:16px">Screenshots</h4>
            <div style="display:flex;gap:24px;flex-wrap:wrap;align-items:flex-start">
                <div><div style="font-size:11px;font-weight:700;color:#6b7280;margin-bottom:4px">Desktop (1440px)</div>{desktop_img}</div>
                <div><div style="font-size:11px;font-weight:700;color:#6b7280;margin-bottom:4px">Mobile (375px)</div>{mobile_img}</div>
            </div>
        </div>"""

    # Broken links section
    broken_html = ""
    if broken_links:
        broken_rows = "".join(f'<tr><td><code style="font-size:11px">{bl["url"][:80]}</code></td><td>{badge(str(bl["status"]), "#dc2626")}</td></tr>' for bl in broken_links)
        broken_html = f"""
        <h2 style="margin-top:48px">Broken Links ({len(broken_links)})</h2>
        <table style="width:100%;border-collapse:collapse">
            <thead><tr><th style="text-align:left;padding:8px;border-bottom:2px solid #e5e7eb">URL</th><th style="padding:8px;border-bottom:2px solid #e5e7eb">Status</th></tr></thead>
            <tbody>{broken_rows}</tbody>
        </table>"""
    else:
        broken_html = '<h2 style="margin-top:48px">Broken Links</h2><p style="color:#0d9488;font-weight:600">No broken links found</p>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Vantage Audit Report — {now}</title>
    <style>
        * {{ margin:0; padding:0; box-sizing:border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color:#1f2937; background:#f9fafb; padding:32px; line-height:1.6; }}
        h1 {{ font-size:28px; margin-bottom:4px; }}
        h2 {{ font-size:20px; margin-bottom:16px; margin-top:32px; }}
        h3 {{ font-size:16px; }}
        h4 {{ font-size:13px; color:#6b7280; margin:12px 0 6px; text-transform:uppercase; letter-spacing:.05em; }}
        table {{ width:100%; border-collapse:collapse; margin-bottom:24px; }}
        th {{ text-align:left; padding:10px 12px; border-bottom:2px solid #e5e7eb; font-size:12px; color:#6b7280; text-transform:uppercase; letter-spacing:.04em; }}
        td {{ padding:10px 12px; border-bottom:1px solid #f3f4f6; font-size:13px; }}
        tr:hover {{ background:#f9fafb; }}
        code {{ background:#f3f4f6; padding:2px 6px; border-radius:4px; font-size:12px; }}
        ul {{ padding-left:20px; }}
        li {{ margin-bottom:4px; font-size:13px; }}
        a {{ color:#2563eb; }}
        .summary-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:16px; margin-bottom:32px; }}
        .summary-card {{ background:#fff; border:1px solid #e5e7eb; border-radius:12px; padding:20px; text-align:center; }}
        .summary-card .value {{ font-size:32px; font-weight:800; }}
        .summary-card .label {{ font-size:12px; color:#6b7280; margin-top:4px; }}
    </style>
</head>
<body>
    <h1>Vantage Website Audit Report</h1>
    <p style="color:#6b7280;margin-bottom:24px">Generated {now} | {BASE_URL}</p>

    <div class="summary-grid">
        <div class="summary-card">
            <div class="value">{len(results)}</div>
            <div class="label">Pages Audited</div>
        </div>
        <div class="summary-card">
            <div class="value" style="color:{'#0d9488' if total_issues == 0 else '#dc2626'}">{total_issues}</div>
            <div class="label">Total Issues</div>
        </div>
        <div class="summary-card">
            <div class="value" style="color:#0d9488">{pages_clean}</div>
            <div class="label">Clean Pages</div>
        </div>
        <div class="summary-card">
            <div class="value" style="color:{'#0d9488' if not broken_links else '#dc2626'}">{len(broken_links)}</div>
            <div class="label">Broken Links</div>
        </div>
    </div>

    <h2>Page Summary</h2>
    <table>
        <thead>
            <tr>
                <th>Page</th>
                <th>Path</th>
                <th>Status</th>
                <th>Load Time</th>
                <th>Issues</th>
                <th>Imgs No Alt</th>
                <th>No Labels</th>
                <th></th>
            </tr>
        </thead>
        <tbody>{rows}</tbody>
    </table>

    {broken_html}

    <h2 style="margin-top:48px">Page Details</h2>
    {detail_sections}

    <footer style="margin-top:64px;padding-top:24px;border-top:1px solid #e5e7eb;font-size:12px;color:#9ca3af">
        Vantage Audit Report | Playwright + Chromium Headless | {now}
    </footer>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[Report] Saved to {output_path}")


def main():
    print("=" * 60)
    print("  VANTAGE WEBSITE AUDIT")
    print(f"  Target: {BASE_URL}")
    print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": DESKTOP_WIDTH, "height": DESKTOP_HEIGHT},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            ignore_https_errors=True,
        )
        page = context.new_page()

        results = []

        # Audit public pages first
        print("\n[Phase 1] Auditing public pages...")
        for path, label in PUBLIC_PAGES:
            print(f"  Auditing: {label} ({path})")
            result = audit_page(page, path, label, {})
            results.append(result)

        # Login
        print("\n[Phase 2] Authenticating...")
        logged_in = login(page)
        if not logged_in:
            print("[Auth] FAILED - will try pages anyway")

        # Audit authenticated pages
        print("\n[Phase 3] Auditing app pages...")
        for path, label in AUTH_PAGES:
            print(f"  Auditing: {label} ({path})")
            result = audit_page(page, path, label, {})
            results.append(result)

        # Check broken links
        print("\n[Phase 4] Checking broken links...")
        broken_links = check_broken_links(page, results[:5])  # Check links from first 5 pages

        # Generate report
        print("\n[Phase 5] Generating report...")
        generate_report(results, broken_links)

        browser.close()

    # Summary
    total_issues = sum(len(r["issues"]) for r in results)
    print("\n" + "=" * 60)
    print(f"  AUDIT COMPLETE")
    print(f"  Pages: {len(results)}")
    print(f"  Total issues: {total_issues}")
    print(f"  Report: audit_report.html")
    print("=" * 60)


if __name__ == "__main__":
    main()
