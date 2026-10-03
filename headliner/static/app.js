// headliner web viewer: optional enhancements. Every page works without this.
//
// - Filters apply as soon as a box or menu changes (the Apply button stays for
//   search boxes and for browsers without JavaScript).
// - The tags & sources panel starts open on wide screens.
// - "New since your last visit": items first fetched after your previous visit
//   are marked, and the Latest tab shows how many there are. The visit time is
//   kept in this browser only (localStorage); nothing is sent anywhere except
//   the count request to this server.
// - Tables marked "sortable" sort by a clicked column header.
// - Keyboard shortcuts (press ? for the list).
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

  // Click a column header to sort; again to reverse. The server order is the
  // default, and the totals row (tfoot) stays put.
  function sortValue(cell) {
    if (!cell) return "";
    var raw = cell.getAttribute("data-sort");
    var text = raw !== null ? raw : cell.textContent.trim();
    var number = parseFloat(text.replace(/,/g, ""));
    return isNaN(number) ? text.toLowerCase() : number;
  }
  function sortable() {
    document.querySelectorAll("table.sortable").forEach(function (table) {
      var body = table.tBodies[0];
      if (!body || !table.tHead) return;
      var headers = table.tHead.rows[0].cells;
      Array.prototype.forEach.call(headers, function (th, column) {
        th.classList.add("sorter");
        th.tabIndex = 0;
        function sort() {
          var ascending = th.getAttribute("aria-sort") !== "ascending";
          Array.prototype.forEach.call(headers, function (other) {
            other.removeAttribute("aria-sort");
          });
          th.setAttribute("aria-sort", ascending ? "ascending" : "descending");
          var rows = Array.prototype.slice.call(body.rows);
          rows.sort(function (a, b) {
            var x = sortValue(a.cells[column]);
            var y = sortValue(b.cells[column]);
            if (x === "" && y !== "") return 1;
            if (y === "" && x !== "") return -1;
            var order = x < y ? -1 : x > y ? 1 : 0;
            return ascending ? order : -order;
          });
          rows.forEach(function (row) {
            body.appendChild(row);
          });
        }
        th.addEventListener("click", sort);
        th.addEventListener("keydown", function (event) {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            sort();
          }
        });
      });
    });
  }

  // Keyboard shortcuts: j/k next/previous item, o or Enter opens it, / search,
  // g then a letter for a page, ? for help. Ignored while typing.
  var PAGES = { b: "/", l: "/latest", s: "/stories", t: "/trends", r: "/rewrites", o: "/sources" };
  function keys() {
    var current = -1;
    var pendingG = false;
    function items() {
      return document.querySelectorAll("li.item, li.story, ol.framing > li");
    }
    function move(step) {
      var list = items();
      if (!list.length) return;
      if (current >= 0 && list[current]) list[current].classList.remove("kbd-focus");
      current = Math.max(0, Math.min(list.length - 1, current + step));
      var item = list[current];
      item.classList.add("kbd-focus");
      item.scrollIntoView({ block: "nearest" });
    }
    function help() {
      var dialog = document.querySelector("dialog.keys");
      if (!dialog) {
        dialog = document.createElement("dialog");
        dialog.className = "keys";
        var rows = [
          ["j / k", "next / previous item"],
          ["o or Enter", "open the item"],
          ["/", "search"],
          ["g b, g l, g s", "Briefing, Latest, Stories"],
          ["g t, g r, g o", "Trends, Rewrites, Sources"],
          ["?", "this help (Esc closes)"],
        ];
        var title = document.createElement("strong");
        title.textContent = "Keyboard shortcuts";
        var list = document.createElement("dl");
        rows.forEach(function (row) {
          var dt = document.createElement("dt");
          var kbd = document.createElement("kbd");
          kbd.textContent = row[0];
          dt.appendChild(kbd);
          var dd = document.createElement("dd");
          dd.textContent = row[1];
          list.appendChild(dt);
          list.appendChild(dd);
        });
        dialog.appendChild(title);
        dialog.appendChild(list);
        dialog.addEventListener("click", function () {
          dialog.close();
        });
        document.body.appendChild(dialog);
      }
      if (dialog.open) dialog.close();
      else if (dialog.showModal) dialog.showModal();
    }
    document.addEventListener("keydown", function (event) {
      var target = event.target;
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      if (target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)))
        return;
      if (pendingG) {
        pendingG = false;
        if (PAGES[event.key]) window.location.href = PAGES[event.key];
        return;
      }
      if (event.key === "j") move(1);
      else if (event.key === "k") move(-1);
      else if (event.key === "g") pendingG = true;
      else if (event.key === "?") help();
      else if (event.key === "/") {
        var box = document.querySelector('input[type="search"]');
        event.preventDefault();
        if (box) box.focus();
        else window.location.href = "/search";
      } else if ((event.key === "o" || event.key === "Enter") && current >= 0) {
        if (target && target.tagName === "A") return;
        var item = items()[current];
        var link = item && item.querySelector("h3 a, a.title, .diff a, a");
        if (link) link.click();
      }
    });
  }

  function start() {
    filters();
    sortable();
    keys();
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
