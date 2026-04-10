/**
 * PRISM Shared Navigation Module
 * Renders sidebar, mobile nav, theme toggle, user session, and sync button.
 *
 * Usage:
 *   <div id="prism-nav"></div>
 *   <script src="/pwa/js/prism-nav.js"></script>
 *
 * Pages can hook into sub-nav by defining window.prismSubNav = [
 *   { label: 'Tab Name', icon: 'fas fa-icon', key: 'tabKey' }
 * ] and window.prismSubNavCallback = function(key) { ... }
 * before loading this script.
 */
(function () {
  'use strict';

  // ── Nav items ──
  const NAV_ITEMS = [
    { section: 'Main' },
    { label: 'Dashboard',    icon: 'fas fa-chart-line',      href: '/' },
    { label: 'Scanner',      icon: 'fas fa-satellite-dish',  href: '/scanner',      adminOnly: true },
    { label: 'Brand Hunter', icon: 'fas fa-crosshairs',      href: '/brand-hunter' },
    { label: 'Analytics',    icon: 'fas fa-chart-bar',        href: '/vantage-v2' },
    { label: 'FlipTracker',  icon: 'fas fa-tags',            href: '/price/index.html', adminOnly: true },
    { section: 'System' },
    { label: 'Settings',     icon: 'fas fa-sliders-h',       href: '/settings' },
    { label: 'Admin',        icon: 'fas fa-terminal',         href: '/admin',        adminOnly: true },
  ];

  // ── Detect active page ──
  function isActive(href) {
    const path = window.location.pathname.replace(/\/+$/, '') || '/';
    const h = href.replace(/\/+$/, '') || '/';
    if (h === '/') return path === '/' || path === '/index.html' || path === '/dashboard' || path === '/dashboard_v4.html';
    if (h === '/price/index.html') return path === '/price/index.html' || path === '/price' || path === '/price/';
    if (h === '/scanner') return path === '/scanner' || path === '/scanner_v4.html';
    if (h === '/admin') return path === '/admin' || path === '/admin_v4.html';
    if (h === '/brand-hunter') return path === '/brand-hunter' || path === '/brand_hunter.html';
    if (h === '/settings') return path === '/settings' || path === '/settings.html';
    if (h === '/vantage-v2') return path === '/vantage-v2';
    return path === h;
  }

  // ── SVG Logo ──
  const LOGO_SVG = `<svg class="prism-nav-logo" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
    <defs><linearGradient id="prismNavGrad" x1="4" y1="4" x2="28" y2="28" gradientUnits="userSpaceOnUse">
      <stop stop-color="#7c3aed"/><stop offset="1" stop-color="#06b6d4"/>
    </linearGradient></defs>
    <path d="M16 2L28 26H4L16 2Z" fill="url(#prismNavGrad)" opacity="0.9"/>
    <path d="M16 8L22 22H10L16 8Z" fill="#0a0a0f" opacity="0.5"/>
    <path d="M16 2L28 26H16V2Z" fill="white" opacity="0.12"/>
  </svg>`;

  // ── Theme ──
  function getTheme() {
    return localStorage.getItem('prism-theme') || 'dark';
  }
  function setTheme(t) {
    localStorage.setItem('prism-theme', t);
    document.documentElement.setAttribute('data-theme', t);
  }
  // Apply stored theme immediately
  setTheme(getTheme());

  // ── Build sidebar HTML ──
  function buildSidebar() {
    let navHtml = '';
    const subNav = window.prismSubNav || [];
    const hasSubNav = subNav.length > 0;

    for (const item of NAV_ITEMS) {
      if (item.section) {
        navHtml += `<div class="prism-nav-section">${item.section}</div>`;
        continue;
      }
      const active = isActive(item.href) && !hasSubNav ? ' active' : '';
      const admin = item.adminOnly ? ' data-admin-only style="display:none"' : '';
      navHtml += `<a href="${item.href}" class="prism-nav-link${active}"${admin}>
        <i class="${item.icon}"></i><span>${item.label}</span>
      </a>`;
    }

    // Page-specific sub-nav (FlipTracker tabs, Admin sections, etc.)
    if (hasSubNav) {
      // Find which main nav item is active and mark it
      for (const item of NAV_ITEMS) {
        if (!item.section && isActive(item.href)) {
          // We'll mark it active via JS after render
        }
      }

      const pageLabel = document.title.split('|')[0].split('—')[0].trim();
      navHtml += `<div class="prism-nav-section">${pageLabel}</div>`;
      subNav.forEach((s, i) => {
        const active = i === 0 ? ' active' : '';
        navHtml += `<a class="prism-nav-link prism-subnav-link${active}" data-subnav="${s.key}">
          <i class="${s.icon}"></i><span>${s.label}</span>
        </a>`;
      });
    }

    return `
    <div class="prism-nav-overlay" id="prismNavOverlay"></div>
    <aside class="prism-nav-sidebar" id="prismNavSidebar">
      <div class="prism-nav-brand">
        ${LOGO_SVG}
        <div>
          <div class="prism-nav-title">PRISM</div>
          <div class="prism-nav-subtitle">Intelligence Platform</div>
        </div>
      </div>
      <nav class="prism-nav-links">${navHtml}</nav>
      <div class="prism-nav-footer">
        <div class="prism-nav-user" id="prismNavUser">
          <div class="prism-nav-avatar" id="prismNavAvatar">
            <img src="https://cdn.discordapp.com/embed/avatars/0.png" alt=""
                 onerror="this.src='https://cdn.discordapp.com/embed/avatars/0.png'" id="prismNavAvatarImg">
          </div>
          <div class="prism-nav-user-info">
            <div class="prism-nav-username" id="prismNavUsername">Loading...</div>
            <div class="prism-nav-role" id="prismNavRole">Operator</div>
          </div>
          <button class="prism-nav-theme-btn" id="prismNavThemeBtn" title="Toggle theme">
            <i class="fas fa-moon" id="prismNavThemeIcon"></i>
          </button>
        </div>
      </div>
    </aside>`;
  }

  // ── Inject CSS ──
  function injectStyles() {
    if (document.getElementById('prism-nav-styles')) return;
    const style = document.createElement('style');
    style.id = 'prism-nav-styles';
    style.textContent = `
      /* ── PRISM Nav Sidebar ── */
      .prism-nav-sidebar {
        position: fixed; top: 0; left: 0; bottom: 0; width: 240px;
        background: var(--prism-surface); border-right: 1px solid var(--prism-border);
        display: flex; flex-direction: column; z-index: 40;
        transition: transform 0.25s ease;
      }
      .prism-nav-sidebar::before {
        content: ''; position: absolute; top: 0; left: 0; width: 2px; height: 100%;
        background: linear-gradient(180deg, var(--prism-accent) 0%, var(--prism-cyan) 40%, var(--prism-accent) 80%, var(--prism-cyan) 100%);
        background-size: 100% 200%; animation: prismNavGlow 6s ease-in-out infinite; opacity: .7;
      }
      @keyframes prismNavGlow { 0%,100%{background-position:0% 0%} 50%{background-position:0% 100%} }

      .prism-nav-brand {
        padding: 20px 20px 16px; display: flex; align-items: center; gap: 12px;
        border-bottom: 1px solid var(--prism-border);
      }
      .prism-nav-logo { width: 32px; height: 32px; flex-shrink: 0; }
      .prism-nav-title {
        font-family: 'Cabinet Grotesk', sans-serif; font-weight: 800; font-size: 1.1rem;
        letter-spacing: 0.06em; color: var(--prism-text);
      }
      .prism-nav-subtitle {
        font-size: 9px; color: var(--prism-text-faint); font-weight: 600;
        letter-spacing: 0.1em; text-transform: uppercase;
      }

      .prism-nav-links {
        flex: 1; padding: 12px; overflow-y: auto; display: flex; flex-direction: column; gap: 2px;
      }
      .prism-nav-links::-webkit-scrollbar { width: 4px; }
      .prism-nav-links::-webkit-scrollbar-thumb { background: var(--prism-border); border-radius: 99px; }

      .prism-nav-section {
        padding: 16px 12px 4px; font-size: 10px; font-weight: 600;
        color: var(--prism-text-faint); text-transform: uppercase; letter-spacing: 0.08em;
      }
      .prism-nav-link {
        display: flex; align-items: center; gap: 12px; padding: 8px 12px;
        border-radius: var(--radius-md, 8px); font-size: 13px; font-weight: 500;
        color: var(--prism-text-muted); text-decoration: none;
        transition: all 120ms ease; position: relative; cursor: pointer;
      }
      .prism-nav-link:hover { background: var(--prism-surface-2); color: var(--prism-text); }
      .prism-nav-link.active {
        background: var(--prism-accent-glow); color: var(--prism-accent-text);
      }
      .prism-nav-link.active::before {
        content: ''; position: absolute; left: 0; top: 50%; transform: translateY(-50%);
        width: 3px; height: 16px; border-radius: 0 9999px 9999px 0; background: var(--prism-accent);
      }
      .prism-nav-link i { width: 20px; text-align: center; font-size: 13px; flex-shrink: 0; }

      /* Footer */
      .prism-nav-footer {
        padding: 12px; border-top: 1px solid var(--prism-border); margin-top: auto;
      }
      .prism-nav-user {
        background: var(--prism-surface-2); border-radius: var(--radius-lg, 12px);
        padding: 10px 12px; display: flex; align-items: center; gap: 10px;
        border: 1px solid var(--prism-border);
      }
      .prism-nav-avatar {
        width: 34px; height: 34px; border-radius: var(--radius-md, 8px);
        background: var(--prism-accent-glow); overflow: hidden; flex-shrink: 0;
      }
      .prism-nav-avatar img { width: 100%; height: 100%; object-fit: cover; }
      .prism-nav-user-info { flex: 1; min-width: 0; }
      .prism-nav-username {
        font-size: 13px; font-weight: 600; color: var(--prism-text);
        overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
      }
      .prism-nav-role {
        font-size: 9px; color: var(--prism-accent-text); font-weight: 600;
        text-transform: uppercase; letter-spacing: 0.08em;
      }
      .prism-nav-theme-btn {
        background: none; border: 1px solid var(--prism-border);
        color: var(--prism-text-muted); width: 30px; height: 30px;
        border-radius: var(--radius-md, 8px); cursor: pointer;
        display: flex; align-items: center; justify-content: center;
        font-size: 12px; transition: all 120ms; flex-shrink: 0;
      }
      .prism-nav-theme-btn:hover {
        border-color: var(--prism-accent); color: var(--prism-accent-text);
        background: var(--prism-accent-glow);
      }

      /* Overlay (mobile) */
      .prism-nav-overlay {
        display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.6);
        backdrop-filter: blur(4px); z-index: 39;
      }
      .prism-nav-overlay.open { display: block; }

      /* Main content push */
      .prism-nav-main { margin-left: 240px; flex: 1; display: flex; flex-direction: column; min-height: 100vh; }

      /* Hamburger (injected into top-bar) */
      .prism-nav-hamburger {
        display: none; background: none; border: 1px solid var(--prism-border);
        color: var(--prism-text-muted); width: 36px; height: 36px;
        border-radius: var(--radius-md, 8px); cursor: pointer; font-size: 16px;
        align-items: center; justify-content: center; flex-shrink: 0;
      }

      /* Sync button */
      .prism-nav-sync-btn {
        display: inline-flex; align-items: center; gap: 6px;
        padding: 6px 14px; border-radius: var(--radius-md, 8px);
        border: 1px solid var(--prism-border); background: transparent;
        color: var(--prism-text-muted); font-size: 12px; font-weight: 600;
        cursor: pointer; transition: all 120ms;
      }
      .prism-nav-sync-btn:hover {
        border-color: var(--prism-accent); color: var(--prism-accent-text);
        background: var(--prism-accent-glow);
      }
      .prism-nav-sync-btn:disabled { opacity: 0.5; cursor: default; }
      .prism-nav-sync-btn .fa-spin { animation: prismNavSpin 0.6s linear infinite; }
      @keyframes prismNavSpin { to { transform: rotate(360deg); } }

      /* ── Mobile ── */
      @media (max-width: 767px) {
        .prism-nav-sidebar { transform: translateX(-100%); }
        .prism-nav-sidebar.open { transform: translateX(0); }
        .prism-nav-main { margin-left: 0; }
        .prism-nav-hamburger { display: flex; }
      }
    `;
    document.head.appendChild(style);
  }

  // ── Mount ──
  function mount() {
    injectStyles();

    const container = document.getElementById('prism-nav');
    if (!container) {
      console.warn('[prism-nav] #prism-nav container not found');
      return;
    }

    container.innerHTML = buildSidebar();

    // Mark the correct main nav link as active (even when sub-nav exists)
    const subNav = window.prismSubNav || [];
    if (subNav.length > 0) {
      document.querySelectorAll('.prism-nav-link:not(.prism-subnav-link)').forEach(link => {
        if (link.href && isActive(new URL(link.href, location.origin).pathname)) {
          link.classList.add('active');
        }
      });
    }

    // Ensure body flex layout
    document.body.style.display = 'flex';
    document.body.style.minHeight = '100vh';

    // Add .prism-nav-main to the main content div if not already
    const main = container.nextElementSibling;
    if (main && !main.classList.contains('prism-nav-main')) {
      main.classList.add('prism-nav-main');
    }

    // ── Mobile overlay toggle ──
    const overlay = document.getElementById('prismNavOverlay');
    const sidebar = document.getElementById('prismNavSidebar');

    overlay.addEventListener('click', closeSidebar);

    // ── Sub-nav click handling ──
    if (subNav.length > 0 && typeof window.prismSubNavCallback === 'function') {
      document.querySelectorAll('.prism-subnav-link').forEach(link => {
        link.addEventListener('click', (e) => {
          e.preventDefault();
          document.querySelectorAll('.prism-subnav-link').forEach(l => l.classList.remove('active'));
          link.classList.add('active');
          window.prismSubNavCallback(link.dataset.subnav);
          closeSidebar();
        });
      });
    }

    // ── Theme toggle ──
    const themeBtn = document.getElementById('prismNavThemeBtn');
    const themeIcon = document.getElementById('prismNavThemeIcon');
    updateThemeIcon();
    themeBtn.addEventListener('click', () => {
      const next = getTheme() === 'dark' ? 'light' : 'dark';
      setTheme(next);
      updateThemeIcon();
    });

    function updateThemeIcon() {
      const isDark = getTheme() === 'dark';
      themeIcon.className = isDark ? 'fas fa-moon' : 'fas fa-sun';
    }

    // ── Load user session ──
    loadUser();
  }

  // ── Sidebar open/close (exported globally) ──
  function openSidebar() {
    const sidebar = document.getElementById('prismNavSidebar');
    const overlay = document.getElementById('prismNavOverlay');
    if (sidebar) sidebar.classList.add('open');
    if (overlay) overlay.classList.add('open');
  }

  function closeSidebar() {
    const sidebar = document.getElementById('prismNavSidebar');
    const overlay = document.getElementById('prismNavOverlay');
    if (sidebar) sidebar.classList.remove('open');
    if (overlay) overlay.classList.remove('open');
  }

  function toggleSidebar() {
    const sidebar = document.getElementById('prismNavSidebar');
    if (sidebar && sidebar.classList.contains('open')) closeSidebar();
    else openSidebar();
  }

  // ── User session ──
  async function loadUser() {
    try {
      const res = await fetch('/api/me');
      if (res.status === 401) {
        window.location.href = '/login';
        return;
      }
      if (!res.ok) return;
      const data = await res.json();
      const user = data.user || data;

      const nameEl = document.getElementById('prismNavUsername');
      const roleEl = document.getElementById('prismNavRole');
      const avatarEl = document.getElementById('prismNavAvatarImg');

      if (nameEl) nameEl.textContent = user.discord_username || 'Operator';
      if (roleEl) roleEl.textContent = user.is_admin ? 'Admin' : 'Pro Operator';

      if (user.discord_id && user.discord_avatar && avatarEl) {
        avatarEl.src = `https://cdn.discordapp.com/avatars/${user.discord_id}/${user.discord_avatar}.png`;
      }

      // Show admin-only nav items
      if (user.is_admin) {
        document.querySelectorAll('[data-admin-only]').forEach(el => {
          el.style.display = '';
        });
      }

      // Expose user globally for page scripts
      window.prismUser = user;
      // Fire event so pages can react
      window.dispatchEvent(new CustomEvent('prism-user-loaded', { detail: user }));

    } catch (e) {
      console.warn('[prism-nav] Auth check failed:', e);
    }
  }

  // ── Sync Now ──
  async function triggerSync() {
    const btn = document.querySelector('.prism-nav-sync-btn');
    if (!btn || btn.disabled) return;
    btn.disabled = true;
    const origHtml = btn.innerHTML;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Syncing...';
    try {
      const res = await fetch('/api/scan/partner_opportunity', { method: 'POST' });
      if (!res.ok) throw new Error('Sync failed');
      btn.innerHTML = '<i class="fas fa-check"></i> Triggered';
      setTimeout(() => { btn.innerHTML = origHtml; btn.disabled = false; }, 3000);
    } catch (e) {
      btn.innerHTML = '<i class="fas fa-times"></i> Failed';
      setTimeout(() => { btn.innerHTML = origHtml; btn.disabled = false; }, 3000);
    }
  }

  // ── Create hamburger helper ──
  function createHamburger() {
    const btn = document.createElement('button');
    btn.className = 'prism-nav-hamburger';
    btn.innerHTML = '<i class="fas fa-bars"></i>';
    btn.addEventListener('click', toggleSidebar);
    return btn;
  }

  // ── Create sync button helper ──
  function createSyncButton() {
    const btn = document.createElement('button');
    btn.className = 'prism-nav-sync-btn';
    btn.innerHTML = '<i class="fas fa-sync-alt"></i> Sync Now';
    btn.addEventListener('click', triggerSync);
    return btn;
  }

  // ── Export globally ──
  window.prismNav = {
    openSidebar,
    closeSidebar,
    toggleSidebar,
    triggerSync,
    createHamburger,
    createSyncButton,
    getTheme,
    setTheme,
  };

  // Also export flat for backward compat
  window.openSidebar = openSidebar;
  window.closeSidebar = closeSidebar;
  window.toggleSidebar = toggleSidebar;
  window.triggerSync = triggerSync;

  // ── Auto-mount on DOM ready ──
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }
})();
