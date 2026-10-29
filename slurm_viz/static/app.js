// Instant filters, in-place page updates with a loading bar, refresh button,
// auto-refresh toggle (remembered per browser), "updated Xs ago", and log autoscroll.
(function () {
  document.documentElement.classList.add("js");

  function load(key, fallback) {
    try { var v = localStorage.getItem(key); return v === null ? fallback : v; } catch (e) { return fallback; }
  }
  function save(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* storage blocked */ }
  }

  // ------------------------------------------------------------ loading bar
  var bar = document.getElementById("loadbar");
  var barTimer = null;
  function loading(on) {
    clearTimeout(barTimer);
    document.body.classList.toggle("is-loading", on);
    if (!bar) return;
    if (on) {
      bar.className = "";
      void bar.offsetWidth;  // restart the animation from 0
      bar.className = "active";
    } else {
      bar.className = "active done";
      barTimer = setTimeout(function () { bar.className = ""; }, 300);
    }
  }

  // ------------------------------------------------------------ in-place navigation
  // Fetch the page and swap every [data-swap] region by id, so the filter form
  // (and the search box's focus) stays put. Falls back to a full load if anything is off.
  var inflight = null;
  function navigate(url, opts) {
    opts = opts || {};
    if (!document.getElementById("results")) {  // e.g. job detail page
      location.href = opts.fresh ? withFresh(url) : url;
      return;
    }
    if (inflight) inflight.abort();
    inflight = new AbortController();
    if (!opts.quiet) loading(true);
    fetch(opts.fresh ? withFresh(url) : url, { credentials: "same-origin", signal: inflight.signal })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.text();
      })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        var regions = doc.querySelectorAll("[data-swap][id]");
        if (!regions.length) throw new Error("unexpected page");
        regions.forEach(function (fresh) {
          var old = document.getElementById(fresh.id);
          if (old) old.replaceWith(fresh);
        });
        document.title = doc.title;
        if (opts.push) history.pushState(null, "", url);
        else if (url !== location.href) history.replaceState(null, "", url);
        startUpdatedClock();
        loading(false);
        scheduleAuto();
      })
      .catch(function (err) {
        if (err.name === "AbortError") return;
        location.href = url;  // let the browser show whatever the server says
      });
  }

  function withFresh(url) {
    var u = new URL(url, location.href);
    u.searchParams.set("fresh", "1");
    return u.toString();
  }

  window.addEventListener("popstate", function () { navigate(location.href, {}); });

  // Sorting and pagination links inside the results.
  document.addEventListener("click", function (e) {
    var a = e.target.closest("#results th a, #results .pager a");
    if (!a || e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault();
    navigate(a.href, { push: true });
  });

  // ------------------------------------------------------------ instant filters
  var form = document.getElementById("filters");
  if (form) {
    var formUrl = function () {
      // Keep the current sort from the address bar; take filters from the form; go back to page 1.
      var params = new URLSearchParams(location.search);
      ["q", "state", "partition", "from", "to", "page", "fresh"].forEach(function (k) { params.delete(k); });
      new FormData(form).forEach(function (value, key) {
        if (key === "sort" || key === "order") return;
        if (value) params.set(key, value);
      });
      var qs = params.toString();
      return form.getAttribute("action") + (qs ? "?" + qs : "");
    };
    var debounce = null;
    var apply = function (delay) {
      clearTimeout(debounce);
      debounce = setTimeout(function () {
        var url = new URL(formUrl(), location.href).toString();
        if (url !== location.href) navigate(url, { push: true });
      }, delay);
    };
    form.addEventListener("submit", function (e) { e.preventDefault(); apply(0); });
    form.addEventListener("change", function (e) { if (e.target.type !== "search") apply(0); });
    form.addEventListener("input", function (e) { if (e.target.type === "search") apply(300); });
    var reset = form.querySelector(".reset");
    if (reset) reset.addEventListener("click", function (e) {
      e.preventDefault();
      form.reset();
      form.querySelectorAll("input:not([type=hidden]), select").forEach(function (el) { el.value = ""; });
      navigate(new URL(form.getAttribute("action"), location.href).toString(), { push: true });
    });
  }

  // ------------------------------------------------------------ refresh button
  var refreshBtn = document.getElementById("refresh-btn");
  if (refreshBtn) refreshBtn.addEventListener("click", function () { navigate(location.href, { fresh: true }); });

  // The detail page reloads with ?fresh=1; tidy the address bar afterwards.
  if (new URLSearchParams(location.search).has("fresh")) {
    var clean = new URL(location.href);
    clean.searchParams.delete("fresh");
    history.replaceState(null, "", clean.toString());
  }

  // ------------------------------------------------------------ auto-refresh
  var box = document.getElementById("autorefresh");
  var autoTimer = null;
  function scheduleAuto() {
    clearTimeout(autoTimer);
    if (box && box.checked && !document.hidden) {
      autoTimer = setTimeout(function () { navigate(location.href, { quiet: true }); },
                             Number(box.dataset.seconds) * 1000);
    }
  }
  if (box) {
    box.checked = load("slurmviz.autorefresh", "1") === "1";
    box.addEventListener("change", function () { save("slurmviz.autorefresh", box.checked ? "1" : "0"); scheduleAuto(); });
    document.addEventListener("visibilitychange", scheduleAuto);  // pause while the tab is hidden
    scheduleAuto();
  }

  // ------------------------------------------------------------ "updated Xs ago"
  var clock = null;
  function startUpdatedClock() {
    clearInterval(clock);
    var updated = document.getElementById("updated");
    if (!updated) return;
    var ts = Number(updated.dataset.ts) * 1000;
    var tick = function () {
      var s = Math.max(0, Math.round((Date.now() - ts) / 1000));
      updated.textContent = "updated " + (s < 60 ? s + "s" : Math.floor(s / 60) + "m") + " ago";
    };
    tick();
    clock = setInterval(tick, 5000);
  }
  startUpdatedClock();

  document.querySelectorAll(".logtext").forEach(function (pre) { pre.scrollTop = pre.scrollHeight; });

  // ------------------------------------------------------------ live logs (job page)
  var logs = document.getElementById("logs");
  if (logs) {
    var liveBox = document.getElementById("log-live");
    var logUrl = "/job/" + encodeURIComponent(logs.dataset.job) + "/log";
    var logTimer = null;
    var busy = false;

    var updateLogs = function (showBar) {
      if (busy) return;
      busy = true;
      if (showBar) loading(true);
      fetch(logUrl, { credentials: "same-origin" })
        .then(function (resp) {
          if (!resp.ok) throw new Error("HTTP " + resp.status);
          return resp.json();
        })
        .then(function (data) {
          if (data.state !== logs.dataset.state) {  // job started or finished: reload the whole page
            location.reload();
            return;
          }
          data.logs.forEach(function (log) {
            if (log.same_as_stdout) return;
            var box = logs.parentNode.querySelector('.log[data-kind="' + log.kind + '"]');
            if (!box) return;
            box.querySelector(".log-msg").textContent = log.msg;
            box.querySelector(".log-note").textContent = log.note;
            var pre = box.querySelector(".logtext");
            if (log.text === null) { pre.hidden = true; return; }
            if (pre.hidden || pre.textContent !== log.text) {
              // follow the end only if you were already at the bottom (or it was empty)
              var atBottom = pre.hidden || pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
              pre.hidden = false;
              pre.textContent = log.text;
              if (atBottom) pre.scrollTop = pre.scrollHeight;
            }
          });
        })
        .catch(function (err) {
          // logged out or job gone: stop polling; network blips just retry next tick
          if (/HTTP 40[34]/.test(err.message) && liveBox) { liveBox.checked = false; scheduleLogs(); }
        })
        .then(function () { busy = false; if (showBar) loading(false); });
    };

    var scheduleLogs = function () {
      clearInterval(logTimer);
      if (liveBox && liveBox.checked && !document.hidden) {
        logTimer = setInterval(function () { updateLogs(false); }, Number(logs.dataset.poll) * 1000);
      }
    };
    if (liveBox) liveBox.addEventListener("change", function () { if (liveBox.checked) updateLogs(false); scheduleLogs(); });
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && liveBox && liveBox.checked) updateLogs(false);
      scheduleLogs();
    });
    logs.querySelector(".log-refresh").addEventListener("click", function () { updateLogs(true); });
    scheduleLogs();
  }
})();
