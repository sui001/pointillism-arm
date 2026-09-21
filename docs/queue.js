/* The print queue panel, shared by the pad and the portrait page.
 *
 * There is one queue. A drawing somebody made with a finger and a portrait the
 * arm took of them are the same kind of job by the time they reach it: same
 * 30 x 42 grid, same two copies, same runner painting them oldest first. Which
 * is the point of showing the identical panel on both pages, rather than each
 * one implying it has a queue of its own.
 *
 * This used to be inline in pad.html. It moved out when the portrait flow got
 * its own page, because two copies of queue rendering would drift, and this
 * repo already has a section in CLAUDE.md about the copies that drifted.
 *
 * Give it markup with #queue and #queue-count, then:
 *
 *     PadQueue.start();                 // renders now, then every 4 seconds
 *     PadQueue.demoJobs = function () { ... };   // what to show with no server
 *     PadQueue.onDemo = function () { ... };     // called once when it gives up
 */
window.PadQueue = (function () {
  "use strict";

  // 4.0 s per dab per copy is measured off the arm at 150 mm/s with dabs
  // batched 15 to a sheet, not a guess: 363 dabs comes out at 48 minutes.
  var SECONDS_PER_DAB = 4.0, COPIES = 2, SHOW = 5, QUEUE_CAP = 50;
  var THUMB = "data:image/png;base64,";
  var demo = false;

  function jobSeconds(job) {
    return (job.dab_count || 0) * COPIES * SECONDS_PER_DAB;
  }

  function fmtTime(secs) {
    secs = Math.round(secs);
    if (secs >= 3600) {
      return Math.floor(secs / 3600) + "h " + Math.round((secs % 3600) / 60) + "m";
    }
    return Math.floor(secs / 60) + ":" + String(secs % 60).padStart(2, "0");
  }

  function render(data, run) {
    var box = document.getElementById("queue");
    if (!box) return;
    var jobs = data.jobs || [];
    var total = typeof data.total === "number" ? data.total : jobs.length;
    var queueSecs = jobs.reduce(function (sum, j) { return sum + jobSeconds(j); }, 0);
    document.getElementById("queue-count").textContent =
      total + " of " + QUEUE_CAP + " waiting · " + fmtTime(queueSecs) + " to paint";
    box.textContent = "";
    if (!total) {
      var p = document.createElement("p");
      p.className = "note";
      p.textContent = box.dataset.empty || "Nothing waiting. Yours would paint first.";
      box.appendChild(p);
      return;
    }
    jobs.slice(0, SHOW).forEach(function (j, i) {
      var fig = document.createElement("figure");
      fig.className = "qthumb" + (i === 0 ? " next" : "");
      var img;
      if (typeof j.thumb === "string" && j.thumb.indexOf(THUMB) === 0) {
        img = document.createElement("img");
        img.src = j.thumb;
        img.alt = "Queued drawing " + j.id;
      } else {
        img = document.createElement("div");
        img.className = "blank";
        img.setAttribute("role", "img");
        img.setAttribute("aria-label", "Queued drawing " + j.id + ", no preview");
      }
      var cap = document.createElement("figcaption");
      cap.textContent = (i === 0 ? "next #" : "#") + j.id;
      var dur = document.createElement("div");
      dur.className = "qtime";
      fig.appendChild(img);
      fig.appendChild(cap);
      fig.appendChild(dur);

      // The arm reports which job it is on, so only mark that one as painting.
      // Any other job is still waiting, however far along the arm happens to be.
      var painting = run && run.state === "running" && run.job === j.id
        && typeof run.total === "number" && run.total > 0;
      if (painting) {
        var frac = Math.max(0, Math.min(1, (run.index || 0) / run.total));
        cap.textContent = "painting #" + j.id;
        dur.textContent = fmtTime(jobSeconds(j) * (1 - frac)) + " left";
        var bar = document.createElement("div");
        bar.className = "qbar";
        bar.setAttribute("role", "progressbar");
        bar.setAttribute("aria-valuemin", "0");
        bar.setAttribute("aria-valuemax", "100");
        bar.setAttribute("aria-valuenow", String(Math.round(frac * 100)));
        bar.setAttribute("aria-label", "Painting progress for drawing " + j.id);
        var fill = document.createElement("span");
        fill.style.width = (frac * 100).toFixed(1) + "%";
        bar.appendChild(fill);
        var pct = document.createElement("div");
        pct.className = "qpct";
        pct.textContent = Math.round(frac * 100) + "% · " + (run.index || 0) + " of " + run.total;
        fig.appendChild(bar);
        fig.appendChild(pct);
      } else {
        dur.textContent = fmtTime(jobSeconds(j));
      }
      box.appendChild(fig);
    });
    if (total > SHOW) {
      var more = document.createElement("div");
      more.className = "qmore";
      more.title = (total - SHOW) + " more waiting";
      more.textContent = (total - SHOW) + "+";
      box.appendChild(more);
    }
  }

  function load() {
    if (demo) { render(api.demoJobs()); return; }
    // The run state is a separate endpoint and a nice-to-have, so a failure to
    // read it must not cost us the queue itself.
    Promise.all([
      fetch("/api/jobs", { cache: "no-store" })
        .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); }),
      fetch("/api/run", { cache: "no-store" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .catch(function () { return null; })
    ])
      .then(function (both) { render(both[0], both[1]); })
      .catch(function () { api.enterDemo(); render(api.demoJobs()); });
  }

  var api = {
    jobSeconds: jobSeconds,
    fmtTime: fmtTime,
    render: render,
    load: load,
    secondsPerDab: SECONDS_PER_DAB,
    copies: COPIES,
    isDemo: function () { return demo; },
    // Overridden by the page. With no pad_server behind it, which is what the
    // GitHub Pages demo is, the queue is whatever that page is keeping.
    demoJobs: function () { return { jobs: [], total: 0 }; },
    onDemo: function () {},
    enterDemo: function () {
      if (demo) return;
      demo = true;
      api.onDemo();
    },
    start: function (everyMs) {
      load();
      setInterval(function () { if (!document.hidden) load(); }, everyMs || 4000);
    }
  };
  return api;
})();
