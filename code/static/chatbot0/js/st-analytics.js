/* smallTech analytics, v1.
   One Google Analytics 4 property (smallTech, G-YTYS0G2P7L) for every smallTech site.
   This file is copied unchanged into each site's static folder; change it everywhere or nowhere.

   What it does
   - Runs only on the production hostnames in SITES, so local, preview and staging stay out of GA.
   - Skips private areas (dashboards, admin, the embedded chat frame) by path.
   - Sends the automatic page_view, tagged with the site name as content_group.
     GA4 enhanced measurement adds scrolls, outbound clicks, downloads and history-based page views.
   - Sends three click events without any per-page code:
       cta_click      a button-style link or button (.ga-cta, .pub-btn) was clicked
       contact_click  a mailto: link was clicked
       <custom>       any element with data-ga-event="name"; other data-ga-* attributes become parameters
   - Exposes window.stTrack(name, params) for events fired from code (for example sign_up).
   No inline script is needed, so it works under a strict Content-Security-Policy that allows
   https://www.googletagmanager.com in script-src and the Google Analytics hosts in connect-src. */
(function () {
  "use strict";

  var GA_ID = "G-YTYS0G2P7L";
  var ADS_ID = "AW-17476588629"; // Google Ads, smalltech.in only
  var SITES = {
    "smalltech.in": "smalltech",
    "www.smalltech.in": "smalltech",
    "zer0.smalltech.in": "zer0",
    "chatbot0.smalltech.in": "chatbot0",
    "sales0.smalltech.in": "sales0",
    "blog0.smalltech.in": "blog0",
    "workshop0.smalltech.in": "workshop0"
  };
  var PRIVATE_PATH = /^\/(dashboard|embed|chat|admin|app)(\/|$)/;

  // Always defined, so callers never need to check whether analytics is on.
  window.stTrack = function () {};

  var site = SITES[location.hostname];
  if (!site || PRIVATE_PATH.test(location.pathname) || window.__stAnalytics) return;
  window.__stAnalytics = true;

  window.dataLayer = window.dataLayer || [];
  function gtag() { window.dataLayer.push(arguments); }
  window.gtag = window.gtag || gtag;

  gtag("js", new Date());
  gtag("config", GA_ID, { content_group: site });
  if (site === "smalltech") gtag("config", ADS_ID);

  var tag = document.createElement("script");
  tag.async = true;
  tag.src = "https://www.googletagmanager.com/gtag/js?id=" + GA_ID;
  document.head.appendChild(tag);

  window.stTrack = function (name, params) {
    var p = { site: site };
    for (var k in params || {}) if (Object.prototype.hasOwnProperty.call(params, k)) p[k] = params[k];
    gtag("event", name, p);
  };

  function text(el) {
    return (el.getAttribute("aria-label") || el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 80);
  }

  // Nearest named region of the page: a section id, else header / footer / page.
  function where(el) {
    var region = el.closest("section[id], header, footer, nav");
    if (!region) return "page";
    return region.id || region.tagName.toLowerCase();
  }

  document.addEventListener("click", function (e) {
    var t = e.target;
    if (!t || !t.closest) return;
    var el = t.closest("[data-ga-event], .ga-cta, .pub-btn, a[href^='mailto:']");
    if (!el) return;

    var custom = el.getAttribute("data-ga-event");
    if (custom) {
      var params = { location: where(el) };
      for (var i = 0; i < el.attributes.length; i++) {
        var a = el.attributes[i];
        if (a.name.indexOf("data-ga-") === 0 && a.name !== "data-ga-event") {
          params[a.name.slice(8).replace(/-/g, "_")] = a.value;
        }
      }
      window.stTrack(custom, params);
      return;
    }

    var href = el.getAttribute("href") || "";
    if (href.indexOf("mailto:") === 0) {
      window.stTrack("contact_click", { method: "email", location: where(el) });
      return;
    }
    window.stTrack("cta_click", { cta_text: text(el), link_url: href.split("?")[0], location: where(el) });
  }, true);
})();
