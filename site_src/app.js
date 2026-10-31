/* Verdict scorecard. Reads data/forecasts.json, data/scores.json and data/calendar.geojson (built from the ledger by
 * tools/build_site.py). Text goes in as text, never HTML. Times are shown through the shared TicTime module. */
(function () {
  "use strict";
  var SVGNS = "http://www.w3.org/2000/svg";
  var T = window.TicTime;

  function h(tag, attrs, kids) {
    var e = document.createElement(tag);
    for (var k in attrs || {}) { if (attrs[k] != null) e.setAttribute(k, attrs[k]); }
    (kids || []).forEach(function (c) { if (c != null) e.append(c instanceof Node ? c : document.createTextNode(String(c))); });
    return e;
  }
  function s(tag, attrs, kids) {
    var e = document.createElementNS(SVGNS, tag);
    for (var k in attrs || {}) { e.setAttribute(k, attrs[k]); }
    (kids || []).forEach(function (c) { e.append(c instanceof Node ? c : document.createTextNode(String(c))); });
    return e;
  }
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  /* A calendar date ("2026-11-16") as "16 Nov 2026": the day the event is about, never shifted by time zone. */
  function day(d) {
    if (!d) return "—";
    var p = String(d).slice(0, 10).split("-");
    return Number(p[2]) + " " + MONTHS[Number(p[1]) - 1] + " " + p[0];
  }
  function when(iso) { return iso ? T.format(iso) : "—"; }
  function pct(p) { return Math.round(p * 100) + "%"; }
  function link(url, text) { return url ? h("a", { href: url, rel: "noopener" }, [text || url]) : text; }

  var tip = h("div", { "class": "tip", role: "tooltip", hidden: "" });
  document.body.append(tip);
  function showTip(ev, lines) {
    tip.replaceChildren.apply(tip, lines);
    tip.hidden = false;
    var x = Math.min(ev.clientX + 14, window.innerWidth - tip.offsetWidth - 12);
    var y = ev.clientY + 16;
    if (y + tip.offsetHeight > window.innerHeight - 8) y = ev.clientY - tip.offsetHeight - 12;
    tip.style.left = Math.max(8, x) + "px";
    tip.style.top = Math.max(8, y) + "px";
  }
  function hideTip() { tip.hidden = true; }

  function tiles(rows, scores) {
    var open = rows.filter(function (r) { return r.status === "open"; });
    var all = (scores.by_program || {}).all;
    var next = open.map(function (r) { return r.event_date; }).filter(Boolean).sort()[0];
    var box = document.getElementById("tiles");
    box.replaceChildren(
      tile(rows.length, "forecasts registered"),
      tile(open.length, "open"),
      tile(scores.resolved || 0, "resolved"),
      tile(all ? all.brier.toFixed(3) : "—", all ? "Brier score (0 is perfect)" : "Brier score: after the first resolutions"),
      tile(next ? day(next) : "—", "next event decided"));
    function tile(n, l) { return h("div", { "class": "tile" }, [h("div", { "class": "n" }, [n]), h("div", { "class": "l" }, [l])]); }
  }

  function board(rows) {
    var box = document.getElementById("board");
    var axis = h("div", { "class": "axis", "aria-hidden": "true" }, [
      h("span", { "class": "axis-q" }, ["Question"]),
      h("span", { "class": "axis-scale" }, ["0%", "25%", "50%", "75%", "100%"].map(function (t) { return h("span", null, [t]); })),
      h("span", { "class": "axis-when" }, ["Event date"])]);
    var list = h("div", { role: "list" });
    rows.forEach(function (r) {
      var svg = s("svg", { viewBox: "0 0 100 26", preserveAspectRatio: "none", role: "img",
        "aria-label": r.title + ": " + pct(r.probability) + (r.status === "resolved" ? (r.outcome ? ", happened" : ", did not happen") : ", open") });
      [0, 25, 50, 75, 100].forEach(function (x) {
        svg.append(s("line", { x1: x, x2: x, y1: 6, y2: 20, stroke: "var(--line)", "stroke-width": x === 50 ? 0.6 : 0.4,
          "vector-effect": "non-scaling-stroke" }));
      });
      svg.append(s("line", { x1: 0, x2: 100, y1: 13, y2: 13, stroke: "var(--line)", "stroke-width": 1, "vector-effect": "non-scaling-stroke" }));
      svg.append(s("line", { x1: 0, x2: r.probability * 100, y1: 13, y2: 13, stroke: "var(--mark)", "stroke-width": 2,
        "vector-effect": "non-scaling-stroke", opacity: 0.45 }));
      var row = h("div", { "class": "board-row", role: "listitem", tabindex: "0" }, [
        h("div", { "class": "board-q" }, [r.title]),
        h("div", { "class": "track" }, [svg]),
        h("div", { "class": "board-when" }, [r.status === "resolved" ? (r.outcome ? "✓ happened" : "✗ did not") : day(r.event_date)])]);
      // the dot is drawn in an overlay so it stays round however wide the track is
      var track = row.querySelector(".track");
      var dot = s("svg", { "class": "dot", width: 14, height: 26, "aria-hidden": "true",
        style: "left:calc(" + (r.probability * 100) + "% - 7px)" });
      dot.append(s("circle", { cx: 7, cy: 13, r: 5.5, fill: r.status === "resolved" ? "var(--mark)" : "var(--panel)",
        stroke: "var(--mark)", "stroke-width": 2 }));
      track.append(dot);
      var lines = [h("b", null, [r.title]),
        h("div", null, [pct(r.probability) + ": " + r.likelihood]),
        h("div", { "class": "muted" }, ["Confidence: " + (r.confidence || "—")]),
        h("div", null, ["Settles on: " + (r.rule || "—")]),
        h("div", { "class": "muted" }, ["Registered " + when(r.published_at)])];
      row.addEventListener("mousemove", function (ev) { showTip(ev, lines); });
      row.addEventListener("mouseleave", hideTip);
      row.addEventListener("focus", function () { var b = row.getBoundingClientRect(); showTip({ clientX: b.left + 20, clientY: b.bottom }, lines); });
      row.addEventListener("blur", hideTip);
      list.append(row);
    });
    box.replaceChildren(axis, list);
  }

  function calibration(rows, scores) {
    var box = document.getElementById("calibration");
    var done = rows.filter(function (r) { return r.status === "resolved"; });
    var W = 320, P = 30, PL = 52, S = W - PL - P;
    var svg = s("svg", { viewBox: "0 0 " + W + " " + W, role: "img",
      "aria-label": done.length ? "Calibration of " + done.length + " resolved forecasts" : "Calibration chart, empty until forecasts resolve" });
    function X(v) { return PL + v * S; }
    function Y(v) { return W - P - v * S; }
    [0, 0.25, 0.5, 0.75, 1].forEach(function (v) {
      svg.append(s("line", { x1: X(v), x2: X(v), y1: Y(0), y2: Y(1), stroke: "var(--soft)" }));
      svg.append(s("line", { x1: X(0), x2: X(1), y1: Y(v), y2: Y(v), stroke: "var(--soft)" }));
      svg.append(s("text", { x: X(v), y: W - P + 16, "text-anchor": "middle", "font-size": 11, fill: "var(--muted)" }, [Math.round(v * 100) + "%"]));
      svg.append(s("text", { x: PL - 6, y: Y(v) + 4, "text-anchor": "end", "font-size": 11, fill: "var(--muted)" }, [Math.round(v * 100) + "%"]));
    });
    svg.append(s("line", { x1: X(0), y1: Y(0), x2: X(1), y2: Y(1), stroke: "var(--diag)", "stroke-dasharray": "4 4" }));
    var mid = Y(0.5);
    svg.append(s("text", { x: X(0.5), y: W - 4, "text-anchor": "middle", "font-size": 11, fill: "var(--muted)" }, ["probability we gave"]));
    svg.append(s("text", { x: 10, y: mid, "text-anchor": "middle", "font-size": 11, fill: "var(--muted)",
      transform: "rotate(-90 10 " + mid + ")" }, ["how often it happened"]));
    if (done.length) {
      var bins = {};
      done.forEach(function (r) { var b = Math.min(9, Math.floor(r.probability * 10)); (bins[b] = bins[b] || []).push(r); });
      Object.keys(bins).forEach(function (b) {
        var g = bins[b];
        var mp = g.reduce(function (a, r) { return a + r.probability; }, 0) / g.length;
        var mo = g.reduce(function (a, r) { return a + r.outcome; }, 0) / g.length;
        svg.append(s("circle", { cx: X(mp), cy: Y(mo), r: 4 + Math.sqrt(g.length) * 2, fill: "var(--mark-soft)", stroke: "var(--mark)",
          "stroke-width": 2 }, [s("title", null, [g.length + " forecasts near " + pct(mp) + "; " + pct(mo) + " happened"])]));
      });
    }
    box.replaceChildren(svg);
    if (!done.length) box.append(h("p", { "class": "empty" }, ["Fills in as forecasts resolve (the first on 6 Nov 2026)."]));
    var sc = document.getElementById("scores");
    var progs = Object.keys(scores.by_program || {});
    if (!progs.length) { sc.replaceChildren(); return; }
    var tb = h("tbody");
    progs.forEach(function (p) {
      var x = scores.by_program[p];
      tb.append(h("tr", null, [h("td", null, [p]), h("td", null, [x.n]), h("td", null, [x.brier.toFixed(3)]),
        h("td", null, [x.skill_vs_base_rate == null ? "—" : x.skill_vs_base_rate.toFixed(2)])]));
    });
    sc.replaceChildren(h("table", { "class": "scores-table" }, [
      h("thead", null, [h("tr", null, [h("th", null, ["Program"]), h("th", null, ["Resolved"]), h("th", null, ["Brier"]),
        h("th", null, ["Skill vs base rate"])])]), tb]));
  }

  function timeline(cal) {
    var box = document.getElementById("timeline");
    var evs = (cal.features || []).map(function (f) { return f.properties; });
    var ul = h("ol", { "class": "tl" });
    evs.forEach(function (e) {
      var ev = e.event || {};
      var has = (ev.forecast_ids || []).length > 0;
      ul.append(h("li", { "class": "tl-item" + (has ? " has" : "") }, [
        h("span", { "class": "tl-dot", "aria-hidden": "true" }),
        h("span", { "class": "tl-date" }, [day(ev.date)]),
        h("span", { "class": "tl-title" }, [e.title.replace(/^Scheduled [0-9-]+: /, ""),
          has ? h("span", { "class": "chip" }, [ev.forecast_ids.length === 1 ? "1 forecast" : ev.forecast_ids.length + " forecasts"]) : null,
          ev.verified === false ? h("span", { "class": "chip conf" }, ["date unconfirmed"]) : null]),
        ]));
    });
    box.replaceChildren(ul);
  }

  function list(rows) {
    var box = document.getElementById("list");
    box.replaceChildren.apply(box, rows.map(function (r) {
      var status = r.status === "resolved" ? (r.outcome ? "✓ happened" : "✗ did not happen") : r.status;
      var dl = h("dl", null, [
        h("dt", null, ["Bottom line"]), h("dd", null, [r.summary || "—"]),
        h("dt", null, ["Settles on"]), h("dd", null, [r.rule || "—"]),
        h("dt", null, ["Settling source"]), h("dd", null, [r.resolution_source || "—"]),
        h("dt", null, ["Void if"]), h("dd", null, [r.void_if || "—"]),
        h("dt", null, ["Alternatives considered"]), h("dd", null, [h("ul", null, r.alternatives.map(function (a) {
          return h("li", null, [a.hypothesis + " (tell apart by " + a.tell_apart + ")"]); }))]),
        h("dt", null, ["Sources"]), h("dd", null, [h("ul", null, r.sources.map(function (x) {
          return h("li", null, [link(x.url, x.publisher), x.descriptor ? ": " + x.descriptor : ""]); }))]),
        h("dt", null, ["To raise confidence"]), h("dd", null, [r.raise_by.join("; ") || "—"]),
        h("dt", null, ["Registered"]), h("dd", null, [when(r.published_at) + (r.approved_by ? ", number set by " + r.approved_by : "")])]);
      if (r.status === "resolved") {
        dl.append(h("dt", null, ["Resolved"]), h("dd", null, [when(r.resolved_at) + ": " + status + ", Brier " + r.brier.toFixed(3) + " ",
          link(r.evidence, "evidence")]));
      }
      return h("details", { "class": "item" }, [
        h("summary", null, [h("span", { "class": "p" }, [pct(r.probability)]), h("span", null, [r.title]),
          h("span", { "class": "chip" }, [r.likelihood]), h("span", { "class": "chip conf" }, [(r.confidence || "—") + " confidence"]),
          h("span", { "class": "chip" }, [status])]),
        h("div", { "class": "item-body" }, [dl])]);
    }));
  }

  function render(data) {
    tiles(data.f.forecasts, data.s);
    board(data.f.forecasts);
    calibration(data.f.forecasts, data.s);
    timeline(data.c);
    list(data.f.forecasts);
    var asof = document.getElementById("asof");
    asof.setAttribute("datetime", data.f.as_of);
    T.renderAll(document);
  }

  T.mountSettings(document.getElementById("time-settings"));
  Promise.all(["data/forecasts.json", "data/scores.json", "data/calendar.geojson"].map(function (u) {
    return fetch(u, { cache: "no-cache" }).then(function (r) { if (!r.ok) throw new Error(u + " " + r.status); return r.json(); });
  })).then(function (d) {
    var data = { f: d[0], s: d[1], c: d[2] };
    render(data);
    T.onChange(function () { render(data); });
  }).catch(function (e) {
    document.getElementById("board").replaceChildren(h("p", { "class": "empty" }, ["The ledger could not be loaded (" + e.message + ")."]));
  });
})();
