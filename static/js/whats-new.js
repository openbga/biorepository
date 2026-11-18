// What's new (core/changelog.py): the top bar's unread count, and on the What's new page the
// changes this browser had not seen yet. What an account has seen is kept in localStorage under
// "vr-whats-new:<user>" as {date, ids} (the newest date seen and the ids in the window before it),
// like the help panel's preferences. A browser without a mark for the account has nothing new: the
// first page stores the current mark. The entries ship with the page; nothing is sent anywhere.
(function () {
  "use strict";

  var WINDOW_DAYS = 60; // core.changelog.SEEN_WINDOW
  var ID = /^\d{4}-\d{2}-\d{2}-[a-z0-9-]{1,80}$/;

  function readJson(id) {
    var element = document.getElementById(id);
    if (!element) { return null; }
    try { return JSON.parse(element.textContent); } catch (error) { return null; }
  }

  function day(iso) {
    var parts = /^(\d{4})-(\d{2})-(\d{2})$/.exec(typeof iso === "string" ? iso : "");
    return parts ? Date.UTC(+parts[1], +parts[2] - 1, +parts[3]) / 86400000 : NaN;
  }

  function validMarker(marker) {
    return Boolean(marker) && !isNaN(day(marker.date)) && Array.isArray(marker.ids) &&
      marker.ids.every(function (id) { return typeof id === "string" && ID.test(id); });
  }

  // The rule of core.changelog.unread: newer than the newest date seen, or in the window before
  // it and not seen (an entry that merged late with an earlier date).
  function unread(recent, marker) {
    var newest = day(marker.date);
    var seen = {};
    marker.ids.forEach(function (id) { seen[id] = true; });
    return recent.filter(function (pair) {
      var date = day(pair[1]);
      return date > newest || (date > newest - WINDOW_DAYS && !seen[pair[0]]);
    }).map(function (pair) { return pair[0]; });
  }

  function showCount(link, count) {
    var badge = link.querySelector("[data-vr-whats-new-count]");
    var name = count ? "What's new: " + count + " new change" + (count === 1 ? "" : "s") : "What's new";
    link.setAttribute("aria-label", name);
    link.setAttribute("title", name);
    if (badge) {
      badge.textContent = count > 99 ? "99+" : String(count);
      badge.hidden = !count;
    }
    link.toggleAttribute("data-vr-unread", Boolean(count));
  }

  function markNew(ids) {
    ids.forEach(function (id) {
      var item = document.querySelector('[data-vr-entry="' + id + '"]');
      var mark = item && item.querySelector("[data-vr-whats-new-mark]");
      if (mark) {
        mark.hidden = false;
        item.setAttribute("data-vr-new", ""); // the tile's ice edge (app.css, What's new)
      }
    });
    var notice = document.querySelector("[data-vr-whats-new-notice]");
    var text = notice && notice.querySelector("[data-vr-whats-new-notice-text]");
    if (notice && text && ids.length) {
      text.textContent = ids.length === 1
        ? "1 change is new since you last opened this page in this browser. It is marked New."
        : ids.length + " changes are new since you last opened this page in this browser. They are marked New.";
      notice.hidden = false;
    }
  }

  function remember(key, seen) {
    if (!validMarker(seen)) { return; }
    try { window.localStorage.setItem(key, JSON.stringify(seen)); } catch (error) { /* Storage unavailable: no count is shown. */ }
  }

  function init() {
    var link = document.querySelector("[data-vr-whats-new]");
    var data = readJson("vr-whats-new-data");
    if (!link || !data || !Array.isArray(data.recent)) { return; }
    var key = "vr-whats-new:" + (document.body.dataset.vrUser || "anonymous");
    var marker = null;
    try { marker = JSON.parse(window.localStorage.getItem(key)); } catch (error) { marker = null; }
    if (!validMarker(marker)) { marker = null; }

    if (document.querySelector("[data-vr-whats-new-notice]")) {
      // The What's new page: show what was new on arrival, then remember everything as seen.
      if (marker) { markNew(unread(data.recent, marker)); }
      remember(key, data.seen);
      showCount(link, 0);
      return;
    }
    if (!marker) {
      // First visit in this browser (or a new account): nothing is new; start counting from here.
      remember(key, data.seen);
      showCount(link, 0);
      return;
    }
    showCount(link, unread(data.recent, marker).length);
  }

  init();
})();
