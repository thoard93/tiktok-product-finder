/* =============================================================================
   VANTAGE — Shared JS v2
   Flash messages, confirm modal, nav, counters, tooltips, loading states
   ============================================================================= */

/* --- Flash / Toast -------------------------------------------------------- */
function showFlash(message, type) {
  type = type || "info";
  var c = document.getElementById("flashContainer");
  if (!c) return;
  var el = document.createElement("div");
  el.className = "flash-msg " + type;
  el.innerHTML = "<span>" + esc(message) + "</span>" +
    '<button class="flash-close" onclick="dismissFlash(this.parentElement)">' +
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg></button>';
  c.appendChild(el);
  setTimeout(function () { dismissFlash(el); }, 4000);
}

function dismissFlash(el) {
  if (!el || el.classList.contains("out")) return;
  el.classList.add("out");
  setTimeout(function () { el.remove(); }, 300);
}

/* Auto-dismiss server-rendered flashes */
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("[data-auto-dismiss]").forEach(function (el) {
    setTimeout(function () { dismissFlash(el); }, 4000);
  });
});

/* --- Confirm Modal -------------------------------------------------------- */
var _confirmCb = null;

function confirmAction(title, desc, cb) {
  var o = document.getElementById("modalOverlay");
  if (!o) return;
  document.getElementById("modalTitle").textContent = title;
  document.getElementById("modalDesc").textContent = desc;
  _confirmCb = cb;
  o.classList.add("active");
}

document.addEventListener("DOMContentLoaded", function () {
  var o = document.getElementById("modalOverlay");
  var cancel = document.getElementById("modalCancel");
  var confirm = document.getElementById("modalConfirm");
  if (cancel) cancel.addEventListener("click", function () { o.classList.remove("active"); _confirmCb = null; });
  if (confirm) confirm.addEventListener("click", function () { o.classList.remove("active"); if (typeof _confirmCb === "function") { _confirmCb(); _confirmCb = null; } });
  if (o) o.addEventListener("click", function (e) { if (e.target === o) { o.classList.remove("active"); _confirmCb = null; } });
});

/* --- Active Nav Detection ------------------------------------------------- */
document.addEventListener("DOMContentLoaded", function () {
  var path = window.location.pathname;
  document.querySelectorAll(".sidebar-link, .mobile-tab").forEach(function (a) {
    var href = a.getAttribute("href");
    if (href && path.startsWith(href)) {
      a.classList.add("active");
    }
  });
});

/* --- Global Search -------------------------------------------------------- */
document.addEventListener("DOMContentLoaded", function () {
  var s = document.getElementById("globalSearch");
  if (!s) return;
  var timer;
  s.addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      e.preventDefault();
      var q = s.value.trim();
      if (q) window.location.href = "/app/products?search=" + encodeURIComponent(q);
    }
  });
});

/* Cmd/Ctrl+K → focus search */
document.addEventListener("keydown", function (e) {
  if ((e.metaKey || e.ctrlKey) && e.key === "k") {
    e.preventDefault();
    var s = document.getElementById("globalSearch");
    if (s) s.focus();
  }
});

/* --- KPI Counter Animation ------------------------------------------------ */
function animateCounters() {
  document.querySelectorAll("[data-count-to]").forEach(function (el) {
    var target = parseFloat(el.dataset.countTo);
    var suffix = el.dataset.countSuffix || "";
    var decimals = el.dataset.countDecimals ? parseInt(el.dataset.countDecimals) : 0;
    var duration = 800;
    var start = performance.now();

    function step(now) {
      var progress = Math.min((now - start) / duration, 1);
      var eased = 1 - Math.pow(1 - progress, 3); // ease-out cubic
      var current = target * eased;
      el.textContent = decimals > 0 ? current.toFixed(decimals) + suffix : Math.floor(current).toLocaleString() + suffix;
      if (progress < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  });
}

/* Run counters when stat cards are in viewport */
document.addEventListener("DOMContentLoaded", function () {
  if (!("IntersectionObserver" in window)) { animateCounters(); return; }
  var grid = document.querySelector(".stat-grid");
  if (!grid) return;
  var obs = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (e.isIntersecting) { animateCounters(); obs.disconnect(); }
    });
  }, { threshold: 0.3 });
  obs.observe(grid);
});

/* --- Button Loading State ------------------------------------------------- */
function setLoading(btn, loading) {
  if (loading) {
    btn.classList.add("loading");
    btn.disabled = true;
  } else {
    btn.classList.remove("loading");
    btn.disabled = false;
  }
}

/* --- Skeleton → Content Swap ---------------------------------------------- */
function revealContent(skeletonId, contentId) {
  var sk = document.getElementById(skeletonId);
  var ct = document.getElementById(contentId);
  if (sk) sk.style.display = "none";
  if (ct) ct.style.display = "";
}

/* --- Tooltip (truncated names) -------------------------------------------- */
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("[data-tooltip]").forEach(function (el) {
    el.style.position = "relative";
    el.addEventListener("mouseenter", function () {
      var tip = document.createElement("div");
      tip.className = "tooltip-pop";
      tip.textContent = el.dataset.tooltip;
      tip.style.cssText = "position:absolute;bottom:calc(100% + 6px);left:50%;transform:translateX(-50%);background:var(--bg-elevated);color:var(--text-primary);font-size:12px;padding:4px 10px;border-radius:6px;white-space:nowrap;z-index:999;box-shadow:var(--shadow-md);pointer-events:none;border:1px solid var(--border)";
      el.appendChild(tip);
    });
    el.addEventListener("mouseleave", function () {
      var t = el.querySelector(".tooltip-pop");
      if (t) t.remove();
    });
  });
});

/* --- Utility -------------------------------------------------------------- */
function esc(t) { var d = document.createElement("div"); d.textContent = t; return d.innerHTML; }
function formatNum(n) { if (n >= 1e6) return (n / 1e6).toFixed(1) + "M"; if (n >= 1e3) return (n / 1e3).toFixed(1) + "K"; return n.toString(); }
function formatCurrency(n) { return "$" + n.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ","); }
