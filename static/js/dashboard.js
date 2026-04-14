/* =============================================================================
   VANTAGE — Dashboard & Chart JS v2
   Chart.js init, blacklist AJAX, coupon validation, commission calc, scoring
   ============================================================================= */

var CHART_COLORS = {
  accent: "#ff5272",
  teal: "#36d9c4",
  gold: "#f5a623",
  success: "#5ee89c",
  danger: "#ff6b6b",
  purple: "#a78bfa",
  orange: "#fb923c",
  grid: "rgba(255,255,255,0.06)",
  tick: "#8d8a84"
};

var CHART_SCALE_DEFAULTS = {
  x: { grid: { color: CHART_COLORS.grid }, ticks: { color: CHART_COLORS.tick, font: { size: 10, family: "Satoshi" } }, border: { display: false } },
  y: { grid: { color: CHART_COLORS.grid }, ticks: { color: CHART_COLORS.tick, font: { size: 10, family: "Satoshi" } }, border: { display: false }, beginAtZero: true }
};

/* --- Trending Score Color ------------------------------------------------- */
function scoreClass(score) {
  if (score >= 80) return "badge-green";
  if (score >= 50) return "badge-gold";
  return "badge-red";
}

function scoreLabel(score) {
  if (score >= 80) return "trending";
  if (score >= 50) return "stable";
  return "low";
}

/* --- Blacklist AJAX ------------------------------------------------------- */
function blacklistProduct(productId, btn) {
  if (btn) setLoading(btn, true);
  fetch("/api/blacklist/add", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ product_id: productId })
  })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (d.success) {
        showFlash("Product blacklisted", "success");
        if (btn) { btn.innerHTML = '<i data-lucide="check" style="width:14px;height:14px"></i> Blacklisted'; btn.disabled = true; btn.classList.remove("loading"); if (window.lucide) lucide.createIcons(); }
      } else {
        showFlash(d.error || "Failed to blacklist", "error");
        if (btn) setLoading(btn, false);
      }
    })
    .catch(function () {
      showFlash("Network error", "error");
      if (btn) setLoading(btn, false);
    });
}

/* --- Coupon Validation ---------------------------------------------------- */
function applyCoupon() {
  var code = document.getElementById("couponInput").value.trim().toUpperCase();
  var result = document.getElementById("couponResult");
  if (!code) { result.textContent = ""; return; }

  fetch("/api/subscribe/coupon", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code: code })
  })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (d.valid) {
        result.className = "coupon-result valid";
        result.textContent = "\u2713 " + code + " applied \u2014 " + d.discount_percent + "% off!";
      } else {
        result.className = "coupon-result invalid";
        result.textContent = "Invalid coupon code";
      }
    })
    .catch(function () {
      result.className = "coupon-result invalid";
      result.textContent = "Could not validate coupon";
    });
}

/* --- Commission Calculator ------------------------------------------------ */
function initCalc(price, commRate) {
  var input = document.getElementById("calcDailySales");
  var output = document.getElementById("calcRevenue");
  if (!input || !output) return;

  function update() {
    var daily = parseInt(input.value) || 0;
    var monthly = daily * price * commRate * 30;
    output.textContent = "$" + monthly.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  input.addEventListener("input", update);
  update();
}

/* --- Create Line Chart (sales trend) -------------------------------------- */
function createLineChart(canvasId, labels, values, color) {
  var ctx = document.getElementById(canvasId);
  if (!ctx) return;
  color = color || CHART_COLORS.teal;

  return new Chart(ctx, {
    type: "line",
    data: {
      labels: labels,
      datasets: [{
        data: values,
        borderColor: color,
        backgroundColor: color.replace(")", ",0.08)").replace("rgb", "rgba"),
        fill: true,
        tension: 0.4,
        pointRadius: 0,
        pointHoverRadius: 4,
        borderWidth: 2
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: CHART_SCALE_DEFAULTS
    }
  });
}

/* --- Create Bar Chart ----------------------------------------------------- */
function createBarChart(canvasId, labels, values, colors) {
  var ctx = document.getElementById(canvasId);
  if (!ctx) return;

  return new Chart(ctx, {
    type: "bar",
    data: {
      labels: labels,
      datasets: [{
        data: values,
        backgroundColor: colors || CHART_COLORS.teal,
        borderRadius: 6,
        borderSkipped: false
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      indexAxis: (labels.length > 5 && labels[0].length > 8) ? "y" : "x",
      plugins: { legend: { display: false } },
      scales: CHART_SCALE_DEFAULTS
    }
  });
}

/* --- Create Doughnut Chart ------------------------------------------------ */
function createDoughnutChart(canvasId, labels, values, colors) {
  var ctx = document.getElementById(canvasId);
  if (!ctx) return;

  return new Chart(ctx, {
    type: "doughnut",
    data: {
      labels: labels,
      datasets: [{
        data: values,
        backgroundColor: colors,
        borderWidth: 0,
        spacing: 2
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: "65%",
      plugins: {
        legend: {
          display: true,
          position: "bottom",
          labels: { color: "#8a8885", font: { size: 11, family: "Satoshi" }, padding: 16, usePointStyle: true, pointStyleWidth: 8 }
        }
      }
    }
  });
}

/* --- Scroll Row Drag ------------------------------------------------------ */
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll(".scroll-row").forEach(function (row) {
    var isDown = false, startX, scrollLeft;
    row.addEventListener("mousedown", function (e) { isDown = true; row.style.cursor = "grabbing"; startX = e.pageX - row.offsetLeft; scrollLeft = row.scrollLeft; });
    row.addEventListener("mouseleave", function () { isDown = false; row.style.cursor = ""; });
    row.addEventListener("mouseup", function () { isDown = false; row.style.cursor = ""; });
    row.addEventListener("mousemove", function (e) { if (!isDown) return; e.preventDefault(); row.scrollLeft = scrollLeft - (e.pageX - row.offsetLeft - startX) * 1.5; });
  });
});

/* --- Table Sort (client-side) --------------------------------------------- */
var _sortDir = {};
function sortTable(tableId, colIndex) {
  var key = tableId + "-" + colIndex;
  _sortDir[key] = (_sortDir[key] || 1) * -1;
  var dir = _sortDir[key];
  var tbody = document.querySelector("#" + tableId + " tbody");
  var rows = Array.from(tbody.querySelectorAll("tr"));

  rows.sort(function (a, b) {
    var av = (a.cells[colIndex] ? a.cells[colIndex].textContent.trim() : "");
    var bv = (b.cells[colIndex] ? b.cells[colIndex].textContent.trim() : "");
    var an = parseFloat(av.replace(/[$,%]/g, ""));
    var bn = parseFloat(bv.replace(/[$,%]/g, ""));
    if (!isNaN(an) && !isNaN(bn)) return (an - bn) * dir;
    return av.localeCompare(bv) * dir;
  });

  rows.forEach(function (r) { tbody.appendChild(r); });
}

/* --- Lazy Images ---------------------------------------------------------- */
document.addEventListener("DOMContentLoaded", function () {
  if (!("IntersectionObserver" in window)) return;
  var obs = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (e.isIntersecting) {
        var img = e.target;
        if (img.dataset.src) { img.src = img.dataset.src; img.removeAttribute("data-src"); }
        obs.unobserve(img);
      }
    });
  }, { rootMargin: "100px" });
  document.querySelectorAll("img[data-src]").forEach(function (img) { obs.observe(img); });
});
