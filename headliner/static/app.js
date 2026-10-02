// headliner web viewer: optional enhancements. Every page works without this.
//
// - Filters apply as soon as a box or menu changes (the Apply button stays for
//   search boxes and for browsers without JavaScript).
// - The tags & sources panel starts open on wide screens.
// - "New since your last visit": items first fetched after your previous visit
//   are marked, and the Latest tab shows how many there are. The visit time is
//   kept in this browser only (localStorage); nothing is sent anywhere except
//   the count request to this server.
(function () {
  "use strict";
  var root = document.documentElement;
  root.classList.add("js");

  function store(kind) {
    try {
      return kind === "local" ? window.localStorage : window.sessionStorage;
    } catch (e) {
      return null;
    }
  }
  function read(kind, key) {
    var s = store(kind);
    try {
      return s ? s.getItem(key) : null;
    } catch (e) {
      return null;
    }
  }
  function write(kind, key, value) {
    var s = store(kind);
    try {
      if (s) s.setItem(key, value);
    } catch (e) {
      /* storage blocked: markers just don't appear */
    }
  }

  function filters() {
    var wide = window.matchMedia && window.matchMedia("(min-width: 40rem)").matches;
    document.querySelectorAll("form.filters").forEach(function (form) {
      var panel = form.querySelector("details.panel");
      if (panel && wide) panel.open = true;
      form.addEventListener("change", function (event) {
        var target = event.target;
        if (!target || target.type === "search" || target.type === "text") return;
        if (form.requestSubmit) form.requestSubmit();
        else form.submit();
      });
    });
  }

  // The previous visit: fixed for this tab's session so markers survive
  // moving between pages, then moved on to now for the next visit.
  function baseline() {
    var key = "headliner.lastVisit";
    var fixed = read("session", key);
    if (fixed === null) {
      fixed = read("local", key) || "";
      write("session", key, fixed);
    }
    write("local", key, new Date().toISOString());
    var when = fixed ? new Date(fixed) : null;
    return when && !isNaN(when.getTime()) ? when : null;
  }

  function label(when) {
    return when.toLocaleString(undefined, {
      weekday: "short",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function markNew(since) {
    var items = document.querySelectorAll("[data-seen]");
    var sawNew = false;
    var marked = false;
    items.forEach(function (item) {
      var seen = new Date(item.getAttribute("data-seen"));
      if (isNaN(seen.getTime())) return;
      if (seen > since) {
        item.classList.add("new");
        sawNew = true;
      } else if (sawNew && !marked && item.matches("li.item")) {
        var divider = document.createElement("li");
        divider.className = "visit-divider";
        divider.textContent = "Earlier than your last visit (" + label(since) + ")";
        item.parentNode.insertBefore(divider, item);
        marked = true;
      }
    });
  }

  function countNew(since) {
    if (!window.fetch) return;
    fetch("/api/new?since=" + encodeURIComponent(since.toISOString()), {
      credentials: "same-origin",
    })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        if (!data || !data.latest) return;
        var tab = document.querySelector('nav a[data-nav="latest"]');
        if (tab && !tab.querySelector(".newcount")) {
          var badge = document.createElement("span");
          badge.className = "newcount";
          badge.textContent = data.latest > 999 ? "999+" : String(data.latest);
          badge.title = data.latest + " new since your last visit";
          tab.appendChild(badge);
        }
        var note = document.querySelector(".since-visit");
        if (note) {
          note.textContent =
            data.latest + " since your last visit (" + label(since) + ").";
          note.hidden = false;
        }
      })
      .catch(function () {
        /* the count is a nicety */
      });
  }

  function start() {
    filters();
    var since = baseline();
    if (since) {
      markNew(since);
      countNew(since);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
