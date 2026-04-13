/* =============================================================================
   VANTAGE — Shared JavaScript
   Flash messages, confirm dialog, global search, utilities
   ============================================================================= */

// --- Flash messages -------------------------------------------------------

function showFlash(message, type) {
  type = type || "info";
  var container = document.getElementById("flashContainer");
  if (!container) return;

  var msg = document.createElement("div");
  msg.className = "flash-msg " + type;
  msg.innerHTML = '<span>' + escapeHtml(message) + '</span>' +
    '<button class="flash-close" onclick="dismissFlash(this.parentElement)">' +
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>' +
    '</button>';

  container.appendChild(msg);

  // Auto-dismiss after 4s
  setTimeout(function() { dismissFlash(msg); }, 4000);
}

function dismissFlash(el) {
  if (!el || el.classList.contains("out")) return;
  el.classList.add("out");
  setTimeout(function() { if (el.parentElement) el.remove(); }, 300);
}

// Auto-dismiss flash messages on page load
document.addEventListener("DOMContentLoaded", function() {
  var flashes = document.querySelectorAll("[data-auto-dismiss]");
  flashes.forEach(function(el) {
    setTimeout(function() { dismissFlash(el); }, 4000);
  });
});

// --- Confirm dialog -------------------------------------------------------

var _confirmCallback = null;

function confirmAction(title, desc, callback) {
  var overlay = document.getElementById("confirmOverlay");
  if (!overlay) return;

  document.getElementById("confirmTitle").textContent = title;
  document.getElementById("confirmDesc").textContent = desc;
  _confirmCallback = callback;
  overlay.classList.add("active");
}

document.addEventListener("DOMContentLoaded", function() {
  var overlay = document.getElementById("confirmOverlay");
  var cancelBtn = document.getElementById("confirmCancel");
  var actionBtn = document.getElementById("confirmAction");

  if (cancelBtn) {
    cancelBtn.addEventListener("click", function() {
      overlay.classList.remove("active");
      _confirmCallback = null;
    });
  }

  if (actionBtn) {
    actionBtn.addEventListener("click", function() {
      overlay.classList.remove("active");
      if (typeof _confirmCallback === "function") {
        _confirmCallback();
        _confirmCallback = null;
      }
    });
  }

  // Close on overlay click
  if (overlay) {
    overlay.addEventListener("click", function(e) {
      if (e.target === overlay) {
        overlay.classList.remove("active");
        _confirmCallback = null;
      }
    });
  }
});

// --- Global search --------------------------------------------------------

document.addEventListener("DOMContentLoaded", function() {
  var searchInput = document.getElementById("globalSearch");
  if (!searchInput) return;

  var debounceTimer;
  searchInput.addEventListener("input", function() {
    clearTimeout(debounceTimer);
    var query = searchInput.value.trim();
    debounceTimer = setTimeout(function() {
      if (query.length >= 2) {
        window.location.href = "/app/products?search=" + encodeURIComponent(query);
      }
    }, 500);
  });

  searchInput.addEventListener("keydown", function(e) {
    if (e.key === "Enter") {
      e.preventDefault();
      var query = searchInput.value.trim();
      if (query) {
        window.location.href = "/app/products?search=" + encodeURIComponent(query);
      }
    }
  });
});

// --- Utilities ------------------------------------------------------------

function escapeHtml(text) {
  var div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

function formatNumber(num) {
  if (num >= 1000000) return (num / 1000000).toFixed(1) + "M";
  if (num >= 1000) return (num / 1000).toFixed(1) + "K";
  return num.toString();
}

function formatCurrency(num) {
  return "$" + num.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

// --- Keyboard shortcuts ---------------------------------------------------

document.addEventListener("keydown", function(e) {
  // Cmd/Ctrl + K -> focus search
  if ((e.metaKey || e.ctrlKey) && e.key === "k") {
    e.preventDefault();
    var search = document.getElementById("globalSearch");
    if (search) search.focus();
  }
});
