/* =============================================================================
   VANTAGE — Dashboard JavaScript
   Chart initializations, product card interactions, skeleton loading
   ============================================================================= */

document.addEventListener("DOMContentLoaded", function() {

  // --- Skeleton loading (show skeleton, replace when data loaded) ---------
  // If data is already server-rendered via Jinja2, skip skeleton logic.
  // This is here for future client-side data fetching.

  // --- Lazy load images with IntersectionObserver -------------------------
  if ("IntersectionObserver" in window) {
    var imgObserver = new IntersectionObserver(function(entries) {
      entries.forEach(function(entry) {
        if (entry.isIntersecting) {
          var img = entry.target;
          if (img.dataset.src) {
            img.src = img.dataset.src;
            img.removeAttribute("data-src");
          }
          imgObserver.unobserve(img);
        }
      });
    }, { rootMargin: "100px" });

    document.querySelectorAll("img[data-src]").forEach(function(img) {
      imgObserver.observe(img);
    });
  }

  // --- Scroll row drag-to-scroll -----------------------------------------
  var scrollRows = document.querySelectorAll(".scroll-row");
  scrollRows.forEach(function(row) {
    var isDown = false, startX, scrollLeft;

    row.addEventListener("mousedown", function(e) {
      isDown = true;
      row.style.cursor = "grabbing";
      startX = e.pageX - row.offsetLeft;
      scrollLeft = row.scrollLeft;
    });
    row.addEventListener("mouseleave", function() { isDown = false; row.style.cursor = ""; });
    row.addEventListener("mouseup", function() { isDown = false; row.style.cursor = ""; });
    row.addEventListener("mousemove", function(e) {
      if (!isDown) return;
      e.preventDefault();
      var x = e.pageX - row.offsetLeft;
      var walk = (x - startX) * 1.5;
      row.scrollLeft = scrollLeft - walk;
    });
  });

});
