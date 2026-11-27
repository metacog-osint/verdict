/* Weekly call pages. Probabilities are never typed into a call: they are read from the ledger's build by id, and an
 * id that is not there shows "not yet registered". Text goes in as text, never HTML. */
(function () {
  "use strict";
  var T = window.TicTime;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function pct(p) { return Math.round(p * 100) + "%"; }

  function showTimes() {
    document.querySelectorAll("time[data-utc]").forEach(function (t) {
      t.dateTime = t.dataset.utc;
      t.textContent = T.format(t.dataset.utc);
    });
  }

  function row(id, f) {
    var box = el("article", "call-q");
    if (!f) {
      box.appendChild(el("p", "call-q-title", id));
      box.appendChild(el("p", "unsigned", "Not yet registered."));
      return box;
    }
    box.appendChild(el("p", "call-q-title", f.title));
    var line = pct(f.probability) + ", " + f.likelihood + ". Confidence: " + f.confidence + ".";
    if (f.reference && typeof f.reference.probability === "number") line += " Reference: " + pct(f.reference.probability) + ".";
    box.appendChild(el("p", "call-q-num", line));
    var state = f.status === "open" ? "Open. Knowable from " + T.format(f.known_at) + "." : "Resolved: " + f.outcome + ".";
    box.appendChild(el("p", "muted small", state));
    var rule = el("details");
    rule.appendChild(el("summary", "", "The rule that settles it"));
    rule.appendChild(el("p", "small", f.rule));
    box.appendChild(rule);
    return box;
  }

  function showQuestions() {
    var host = document.getElementById("questions");
    if (!host) return;
    var ids = host.dataset.ids.split(/\s+/);
    fetch("../data/forecasts.json", { cache: "no-cache" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (d) {
        var byId = {};
        d.forecasts.forEach(function (f) { byId[f.id] = f; });
        ids.forEach(function (id) { host.appendChild(row(id, byId[id])); });
      })
      .catch(function () {
        host.appendChild(el("p", "unsigned", "The ledger could not be read, so no numbers are shown."));
      });
  }

  T.mountSettings(document.getElementById("time-settings"));
  showTimes();
  showQuestions();
  T.onChange(function () {
    showTimes();
    var host = document.getElementById("questions");
    if (host) { host.textContent = ""; showQuestions(); }
  });
})();
