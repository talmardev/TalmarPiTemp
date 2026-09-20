"use strict";

(function () {
  const POLL_MS = 1000;
  const RETRY_MS = 2000;
  const CHART_REFRESH_MS = 30000;
  const LIVE_PRESETS = ["1h", "6h", "24h", "7d"];
  const fmt = window.TimeChartFormat;

  const $ = (id) => document.getElementById(id);
  const text = (id, value) => { $(id).textContent = value; };

  let state = null;
  let unitSeen = null;
  let csvSeen = null;
  const dirty = { interval: false, csv: false };

  async function api(path, options) {
    const response = await fetch(path, options);
    let payload = null;
    try { payload = await response.json(); } catch (error) { payload = null; }
    if (!response.ok) {
      throw new Error(payload && payload.error ? payload.error : "Request failed (" + response.status + ")");
    }
    return payload;
  }

  function number(value, symbol) {
    return value === null || value === undefined ? "No data" : value.toFixed(1) + " " + symbol;
  }

  function uptime(seconds) {
    const d = Math.floor(seconds / 86400);
    const h = Math.floor(seconds % 86400 / 3600);
    const m = Math.floor(seconds % 3600 / 60);
    const s = seconds % 60;
    const parts = [];
    if (d) { parts.push(d + "d"); }
    if (d || h) { parts.push(h + "h"); }
    parts.push(m + "m", s + "s");
    return parts.join(" ");
  }

  function setStatus(label, kind) {
    const pill = $("status");
    pill.textContent = label;
    pill.className = "pill" + (kind ? " " + kind : "");
  }

  function render(s) {
    state = s;
    const symbol = s.unit_symbol;
    const icons = { "Running": "● Running", "Sensor error": "▲ Sensor error", "Starting": "○ Starting" };
    setStatus(icons[s.status] || s.status, s.status === "Running" ? "ok" : s.status === "Sensor error" ? "warn" : "");
    text("temperature", s.temperature === null ? "N/A" : s.temperature.toFixed(1));
    text("temperature-unit", s.temperature === null ? "" : symbol);
    document.title = s.temperature === null ? "TalmarPiTemp" : s.temperature.toFixed(1) + " " + symbol + " | TalmarPiTemp";
    const problems = [s.sensor_error && "Sensor: " + s.sensor_error, s.record_error && "Recording: " + s.record_error,
      s.history_error && "History: " + s.history_error].filter(Boolean);
    $("problems").hidden = problems.length === 0;
    text("problems", problems.join(" | "));

    const week = s.summary;
    text("stat-max", week ? number(week.maximum, symbol) : "Loading");
    text("stat-min", week ? number(week.minimum, symbol) : "Loading");
    text("stat-avg", week ? number(week.average, symbol) : "Loading");
    text("stat-count-24h", week ? week.count_24h.toLocaleString("en-US") : "Loading");
    text("stat-count-7d", week ? week.count_7d.toLocaleString("en-US") : "Loading");

    text("info-interval", s.interval + (s.interval === 1 ? " second" : " seconds"));
    text("info-csv", s.csv_path);
    text("info-last", s.last_recorded || "Nothing recorded yet");
    text("info-uptime", uptime(s.uptime_seconds));
    text("info-source", s.source);
    text("info-hostname", s.hostname);
    text("info-now", s.now);

    for (const button of document.querySelectorAll("[data-unit]")) {
      button.setAttribute("aria-pressed", String(button.dataset.unit === s.unit));
    }
    if (!dirty.interval && document.activeElement !== $("interval-input")) { $("interval-input").value = s.interval; }
    if (!dirty.csv && document.activeElement !== $("csv-input")) { $("csv-input").value = s.csv_path; }
    $("interval-input").min = s.limits.min_interval;
    $("interval-input").max = s.limits.max_interval;

    if (unitSeen !== null && (unitSeen !== s.unit || csvSeen !== s.csv_path)) { refreshCharts(); }
    unitSeen = s.unit;
    csvSeen = s.csv_path;
  }

  async function poll() {
    let delay = POLL_MS;
    try {
      render(await api("/api/state"));
    } catch (error) {
      setStatus("▲ Connection lost", "warn");
      delay = RETRY_MS;
    }
    window.setTimeout(poll, delay);
  }

  const definitions = {
    day: { chart: new TimeChart($("chart-24h")), note: $("note-24h"), title: "Temperature over the last 24 hours", seq: 0,
      query: () => "range=24h" },
    week: { chart: new TimeChart($("chart-7d")), note: $("note-7d"), title: "Temperature over the last 7 days", seq: 0,
      query: () => "range=7d" },
    history: { chart: new TimeChart($("chart-history"), { onZoom: zoomTo, onReset: resetZoom }), note: $("note-history"),
      title: "Temperature history for the selected range", seq: 0, query: () => historyQuery() },
  };

  let historyRange = { kind: "preset", value: "7d" };
  let lastPreset = "7d";

  function historyQuery() {
    if (historyRange.kind === "preset") { return "range=" + historyRange.value; }
    return "range=custom&from=" + encodeURIComponent(fmt.isoValue(historyRange.from)) +
      "&to=" + encodeURIComponent(fmt.isoValue(historyRange.to));
  }

  function describeCounts(payload) {
    const count = payload.count.toLocaleString("en-US");
    if (payload.points.length < payload.count * 0.9) {
      return count + " readings grouped into " + payload.points.length.toLocaleString("en-US") +
        " points. The line is the average and the shaded band is the minimum to maximum.";
    }
    return count + " readings. Each point is one reading.";
  }

  async function loadChart(definition) {
    definition.seq += 1;
    const ticket = definition.seq;
    definition.chart.setLoading(true);
    try {
      const payload = await api("/api/chart?" + definition.query());
      if (ticket !== definition.seq) { return; }
      const summary = definition.chart.setData(payload);
      definition.chart.canvas.setAttribute("aria-label", definition.title + ". " + summary);
      const counts = describeCounts(payload);
      const stale = definition === definitions.history && !historyIsLive() ? " This range is not refreshed automatically." : "";
      definition.note.textContent = definition === definitions.history
        ? counts + stale + " Drag across the chart to zoom in. Double click to reset." : counts;
      $("history-message").textContent = "";
    } catch (error) {
      if (ticket === definition.seq && definition === definitions.history) { $("history-message").textContent = error.message; }
    } finally {
      if (ticket === definition.seq) { definition.chart.setLoading(false); }
    }
  }

  function refreshCharts() {
    loadChart(definitions.day);
    loadChart(definitions.week);
    loadChart(definitions.history);
  }

  function markRange(name) {
    for (const button of document.querySelectorAll("[data-range]")) {
      button.setAttribute("aria-pressed", String(button.dataset.range === name));
    }
    $("custom-range").hidden = name !== "custom";
  }

  function zoomTo(from, to) {
    historyRange = { kind: "custom", from: from, to: to };
    $("range-from").value = fmt.inputValue(from);
    $("range-to").value = fmt.inputValue(to);
    markRange("custom");
    loadChart(definitions.history);
  }

  function resetZoom() {
    historyRange = { kind: "preset", value: lastPreset };
    markRange(lastPreset);
    loadChart(definitions.history);
  }

  for (const button of document.querySelectorAll("[data-range]")) {
    button.addEventListener("click", () => {
      const name = button.dataset.range;
      if (name === "custom") {
        const chart = definitions.history.chart.data;
        if (chart) {
          $("range-from").value = fmt.inputValue(chart.start);
          $("range-to").value = fmt.inputValue(chart.end);
        }
        markRange("custom");
        return;
      }
      lastPreset = name;
      historyRange = { kind: "preset", value: name };
      markRange(name);
      loadChart(definitions.history);
    });
  }

  $("custom-range").addEventListener("submit", (event) => {
    event.preventDefault();
    const from = fmt.parseInput($("range-from").value);
    const to = fmt.parseInput($("range-to").value);
    if (from === null || to === null) { $("history-message").textContent = "Enter both a start and an end time."; return; }
    if (from >= to) { $("history-message").textContent = "The start time must be before the end time."; return; }
    historyRange = { kind: "custom", from: from, to: to };
    loadChart(definitions.history);
  });

  async function postConfig(body, messageId, form) {
    const message = $(messageId);
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    message.className = "message";
    message.textContent = "Applying";
    try {
      const payload = await api("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      render(payload.state);
      message.className = "message ok";
      message.textContent = payload.warnings.length ? payload.warnings.join(" ") : "Saved";
      return true;
    } catch (error) {
      message.className = "message error";
      message.textContent = error.message;
      return false;
    } finally {
      button.disabled = false;
    }
  }

  for (const button of document.querySelectorAll("[data-unit]")) {
    button.addEventListener("click", async () => {
      try { render((await api("/api/config", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ unit: button.dataset.unit }),
      })).state); } catch (error) { setStatus("▲ " + error.message, "warn"); }
    });
  }

  $("interval-input").addEventListener("input", () => { dirty.interval = true; });
  $("csv-input").addEventListener("input", () => { dirty.csv = true; });

  $("interval-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (await postConfig({ interval: $("interval-input").value.trim() }, "interval-message", event.target)) { dirty.interval = false; }
  });

  $("csv-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = { csv_path: $("csv-input").value.trim(), copy_history: $("copy-history").checked };
    if (await postConfig(body, "csv-message", event.target)) {
      dirty.csv = false;
      $("copy-history").checked = false;
    }
  });

  function historyIsLive() {
    return historyRange.kind === "preset" && LIVE_PRESETS.indexOf(historyRange.value) >= 0;
  }

  function refreshLiveCharts(includeSlow) {
    loadChart(definitions.day);
    if (includeSlow) {
      loadChart(definitions.week);
      if (historyIsLive()) { loadChart(definitions.history); }
    }
  }

  let tick = 0;
  window.setInterval(() => {
    if (document.hidden) { return; }
    tick += 1;
    refreshLiveCharts(tick % 2 === 0);
  }, CHART_REFRESH_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { refreshLiveCharts(true); } });

  poll();
  refreshCharts();
})();
