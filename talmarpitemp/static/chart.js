"use strict";

(function () {
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const TIME_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600,
    43200, 86400, 172800, 432000, 864000, 2592000];
  const PAD = { top: 18, right: 18, bottom: 30, left: 12 };
  const MIN_DRAG_PIXELS = 8;

  function pad2(n) { return n < 10 ? "0" + n : String(n); }

  function clock(seconds, withSeconds) {
    const d = new Date(seconds * 1000);
    const text = pad2(d.getUTCHours()) + ":" + pad2(d.getUTCMinutes());
    return withSeconds ? text + ":" + pad2(d.getUTCSeconds()) : text;
  }

  function dayLabel(seconds, withYear) {
    const d = new Date(seconds * 1000);
    const text = MONTHS[d.getUTCMonth()] + " " + d.getUTCDate();
    return withYear ? text + ", " + d.getUTCFullYear() : text;
  }

  function fullLabel(seconds) {
    return dayLabel(seconds, false) + ", " + clock(seconds, true);
  }

  function inputValue(seconds) {
    const d = new Date(seconds * 1000);
    return d.getUTCFullYear() + "-" + pad2(d.getUTCMonth() + 1) + "-" + pad2(d.getUTCDate()) +
      "T" + pad2(d.getUTCHours()) + ":" + pad2(d.getUTCMinutes());
  }

  function isoValue(seconds) {
    return inputValue(seconds) + ":" + pad2(new Date(seconds * 1000).getUTCSeconds());
  }

  function parseInput(value) {
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(value || "");
    if (!m) { return null; }
    return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]) / 1000;
  }

  function niceStep(raw) {
    const base = Math.pow(10, Math.floor(Math.log10(raw)));
    const f = raw / base;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * base;
  }

  function valueTicks(min, max, count) {
    const step = niceStep((max - min) / count);
    const ticks = [];
    let v = Math.floor(min / step + 1e-9) * step;
    for (let guard = 0; guard < 60; guard += 1) {
      ticks.push(Math.round(v * 1e6) / 1e6);
      if (v >= max - step * 1e-9) { break; }
      v += step;
    }
    return { ticks: ticks, decimals: step >= 1 ? 0 : Math.ceil(-Math.log10(step) - 1e-9) };
  }

  function timeTicks(t0, t1, maxTicks) {
    const span = t1 - t0;
    let step = TIME_STEPS[TIME_STEPS.length - 1];
    for (const candidate of TIME_STEPS) {
      if (span / candidate <= maxTicks) { step = candidate; break; }
    }
    const ticks = [];
    for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) { ticks.push(t); }
    return { ticks: ticks, step: step };
  }

  function tickLabel(t, step, withYear) {
    if (step >= 86400) { return dayLabel(t, withYear); }
    if (t % 86400 === 0) { return dayLabel(t, false); }
    return clock(t, step < 60);
  }

  function palette() {
    const dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    return dark ? {
      surface: "#161b21", line: "#3bc9db", band: "rgba(59, 201, 219, 0.14)", grid: "#27303a",
      axis: "#3a4652", ink: "#e9eef3", secondary: "#b3bfcb", muted: "#8b9aaa",
      select: "rgba(59, 201, 219, 0.18)"
    } : {
      surface: "#ffffff", line: "#0b7285", band: "rgba(11, 114, 133, 0.12)", grid: "#e8ecf0",
      axis: "#c3ccd5", ink: "#16212c", secondary: "#4d5c6b", muted: "#66768a",
      select: "rgba(11, 114, 133, 0.16)"
    };
  }

  class TimeChart {
    constructor(canvas, options) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.options = options || {};
      this.data = null;
      this.points = [];
      this.hover = -1;
      this.drag = null;
      this.width = 0;
      this.height = 0;
      this.plot = null;
      this.font = "system-ui, sans-serif";
      canvas.addEventListener("pointermove", (e) => this.onMove(e));
      canvas.addEventListener("pointerdown", (e) => this.onDown(e));
      canvas.addEventListener("pointerup", (e) => this.onUp(e));
      canvas.addEventListener("pointercancel", () => this.cancel());
      canvas.addEventListener("pointerleave", () => { if (!this.drag) { this.hover = -1; this.draw(); } });
      canvas.addEventListener("dblclick", () => { if (this.options.onReset) { this.options.onReset(); } });
      canvas.addEventListener("keydown", (e) => this.onKey(e));
      canvas.addEventListener("focus", () => { if (this.hover < 0 && this.points.length) { this.hover = this.points.length - 1; this.draw(); } });
      canvas.addEventListener("blur", () => { this.hover = -1; this.draw(); });
      if (window.ResizeObserver) { new ResizeObserver(() => this.resize()).observe(canvas); }
      window.addEventListener("resize", () => this.resize());
      if (window.matchMedia) {
        window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => this.draw());
      }
      this.resize();
    }

    setLoading(loading) { this.canvas.classList.toggle("loading", loading); }

    setData(data) {
      this.data = data;
      this.points = data.points;
      this.hover = -1;
      this.drag = null;
      const times = this.points.map((p) => p[0]);
      const gaps = [];
      for (let i = 1; i < times.length; i += 1) { gaps.push(times[i] - times[i - 1]); }
      gaps.sort((a, b) => a - b);
      const median = gaps.length ? gaps[Math.floor(gaps.length / 2)] : 0;
      this.gapLimit = Math.max(median * 4, data.bucket_seconds * 3, 2);
      this.hasBand = this.points.some((p) => p[4] > 1);
      this.draw();
      return this.describe();
    }

    describe() {
      const d = this.data;
      if (!d || !d.count) { return "No readings in this range."; }
      return d.count + " readings from " + fullLabel(d.start) + " to " + fullLabel(d.end) +
        ". Minimum " + d.minimum[1].toFixed(1) + " " + d.unit_symbol + " at " + fullLabel(d.minimum[0]) +
        ", maximum " + d.maximum[1].toFixed(1) + " " + d.unit_symbol + " at " + fullLabel(d.maximum[0]) + ".";
    }

    resize() {
      const rect = this.canvas.getBoundingClientRect();
      const ratio = window.devicePixelRatio || 1;
      if (!rect.width || !rect.height) { return; }
      this.width = rect.width;
      this.height = rect.height;
      this.canvas.width = Math.round(rect.width * ratio);
      this.canvas.height = Math.round(rect.height * ratio);
      this.ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      this.font = window.getComputedStyle(this.canvas).fontFamily || this.font;
      this.draw();
    }

    layout() {
      const ctx = this.ctx;
      const d = this.data;
      let lo = d.minimum[1];
      let hi = d.maximum[1];
      if (hi - lo < 1) { lo -= 0.5; hi += 0.5; }
      const margin = (hi - lo) * 0.08;
      const y = valueTicks(lo - margin, hi + margin, 4);
      ctx.font = "12px " + this.font;
      let widest = 0;
      for (const v of y.ticks) { widest = Math.max(widest, ctx.measureText(v.toFixed(y.decimals)).width); }
      const left = PAD.left + widest + 10;
      const plot = { left: left, top: PAD.top, right: this.width - PAD.right, bottom: this.height - PAD.bottom };
      const spanDays = (d.end - d.start) / 86400;
      const x = timeTicks(d.start, d.end, Math.max(2, Math.floor((plot.right - plot.left) / (spanDays > 300 ? 110 : 84))));
      this.plot = plot;
      this.yAxis = { ticks: y.ticks, decimals: y.decimals, min: y.ticks[0], max: y.ticks[y.ticks.length - 1] };
      this.xAxis = x;
      this.withYear = spanDays > 300;
    }

    px(t) { return this.plot.left + (t - this.data.start) / (this.data.end - this.data.start) * (this.plot.right - this.plot.left); }
    py(v) { return this.plot.bottom - (v - this.yAxis.min) / (this.yAxis.max - this.yAxis.min) * (this.plot.bottom - this.plot.top); }
    timeAt(x) { return this.data.start + (x - this.plot.left) / (this.plot.right - this.plot.left) * (this.data.end - this.data.start); }

    draw() {
      const ctx = this.ctx;
      if (!this.width) { return; }
      const c = palette();
      ctx.clearRect(0, 0, this.width, this.height);
      if (!this.data) { return; }
      if (!this.points.length) { this.drawEmpty(c); return; }
      this.layout();
      this.drawAxes(c);
      this.drawSeries(c);
      this.drawExtremes(c);
      this.drawSelection(c);
      if (this.hover >= 0 && !this.drag) { this.drawHover(c); }
    }

    drawEmpty(c) {
      const ctx = this.ctx;
      ctx.fillStyle = c.muted;
      ctx.font = "14px " + this.font;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("No readings in this range", this.width / 2, this.height / 2);
    }

    drawAxes(c) {
      const ctx = this.ctx;
      const p = this.plot;
      ctx.lineWidth = 1;
      ctx.font = "12px " + this.font;
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      for (const v of this.yAxis.ticks) {
        const y = Math.round(this.py(v)) + 0.5;
        ctx.strokeStyle = c.grid;
        ctx.beginPath(); ctx.moveTo(p.left, y); ctx.lineTo(p.right, y); ctx.stroke();
        ctx.fillStyle = c.secondary;
        ctx.fillText(v.toFixed(this.yAxis.decimals), p.left - 8, y);
      }
      ctx.strokeStyle = c.axis;
      ctx.beginPath(); ctx.moveTo(p.left, p.bottom + 0.5); ctx.lineTo(p.right, p.bottom + 0.5); ctx.stroke();
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.fillStyle = c.secondary;
      for (const t of this.xAxis.ticks) {
        const x = Math.round(this.px(t)) + 0.5;
        ctx.strokeStyle = c.axis;
        ctx.beginPath(); ctx.moveTo(x, p.bottom); ctx.lineTo(x, p.bottom + 4); ctx.stroke();
        ctx.fillText(tickLabel(t, this.xAxis.step, this.withYear), x, p.bottom + 8);
      }
    }

    segments() {
      const out = [];
      let current = [];
      for (let i = 0; i < this.points.length; i += 1) {
        if (i > 0 && this.points[i][0] - this.points[i - 1][0] > this.gapLimit) { out.push(current); current = []; }
        current.push(this.points[i]);
      }
      if (current.length) { out.push(current); }
      return out;
    }

    drawSeries(c) {
      const ctx = this.ctx;
      const p = this.plot;
      ctx.save();
      ctx.beginPath();
      ctx.rect(p.left - 4, p.top - 6, p.right - p.left + 8, p.bottom - p.top + 12);
      ctx.clip();
      const segments = this.segments();
      if (this.hasBand) {
        ctx.fillStyle = c.band;
        for (const seg of segments) {
          if (seg.length < 2) { continue; }
          ctx.beginPath();
          seg.forEach((pt, i) => { i ? ctx.lineTo(this.px(pt[0]), this.py(pt[3])) : ctx.moveTo(this.px(pt[0]), this.py(pt[3])); });
          for (let i = seg.length - 1; i >= 0; i -= 1) { ctx.lineTo(this.px(seg[i][0]), this.py(seg[i][2])); }
          ctx.closePath();
          ctx.fill();
        }
      }
      ctx.strokeStyle = c.line;
      ctx.fillStyle = c.line;
      ctx.lineWidth = 2;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      for (const seg of segments) {
        if (seg.length === 1) {
          ctx.beginPath(); ctx.arc(this.px(seg[0][0]), this.py(seg[0][1]), 2.5, 0, Math.PI * 2); ctx.fill();
          continue;
        }
        ctx.beginPath();
        seg.forEach((pt, i) => { i ? ctx.lineTo(this.px(pt[0]), this.py(pt[1])) : ctx.moveTo(this.px(pt[0]), this.py(pt[1])); });
        ctx.stroke();
      }
      ctx.restore();
      const last = this.points[this.points.length - 1];
      this.dot(this.px(last[0]), this.py(last[1]), c);
    }

    dot(x, y, c) {
      const ctx = this.ctx;
      ctx.beginPath(); ctx.arc(x, y, 6, 0, Math.PI * 2); ctx.fillStyle = c.surface; ctx.fill();
      ctx.beginPath(); ctx.arc(x, y, 4, 0, Math.PI * 2); ctx.fillStyle = c.line; ctx.fill();
    }

    label(text, x, y, c, align) {
      const ctx = this.ctx;
      ctx.font = "600 12px " + this.font;
      ctx.textAlign = align;
      ctx.lineWidth = 4;
      ctx.lineJoin = "round";
      ctx.strokeStyle = c.surface;
      ctx.strokeText(text, x, y);
      ctx.fillStyle = c.ink;
      ctx.fillText(text, x, y);
    }

    drawExtremes(c) {
      const d = this.data;
      const p = this.plot;
      const items = [["Max", d.maximum, -14], ["Min", d.minimum, 16]];
      for (const item of items) {
        const x = this.px(item[1][0]);
        const y = this.py(item[1][1]);
        this.dot(x, y, c);
        const text = item[0] + " " + item[1][1].toFixed(1) + " " + d.unit_symbol;
        this.ctx.font = "600 12px " + this.font;
        const half = this.ctx.measureText(text).width / 2;
        const cx = Math.min(Math.max(x, p.left + half), p.right - half);
        this.ctx.textBaseline = "middle";
        this.label(text, cx, Math.min(Math.max(y + item[2], p.top + 4), p.bottom - 4), c, "center");
      }
    }

    drawSelection(c) {
      if (!this.drag) { return; }
      const ctx = this.ctx;
      const a = Math.min(this.drag.start, this.drag.current);
      const b = Math.max(this.drag.start, this.drag.current);
      ctx.fillStyle = c.select;
      ctx.fillRect(a, this.plot.top, b - a, this.plot.bottom - this.plot.top);
    }

    drawHover(c) {
      const ctx = this.ctx;
      const p = this.plot;
      const pt = this.points[this.hover];
      const x = this.px(pt[0]);
      ctx.strokeStyle = c.muted;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(Math.round(x) + 0.5, p.top); ctx.lineTo(Math.round(x) + 0.5, p.bottom); ctx.stroke();
      this.dot(x, this.py(pt[1]), c);

      const unit = this.data.unit_symbol;
      const lines = [{ text: fullLabel(pt[0]), font: "12px " + this.font, color: c.secondary }];
      lines.push({ text: pt[1].toFixed(1) + " " + unit, font: "600 16px " + this.font, color: c.ink, key: true });
      if (pt[4] > 1) {
        lines.push({ text: pt[4] + " readings, from " + pt[2].toFixed(1) + " to " + pt[3].toFixed(1) + " " + unit, font: "12px " + this.font, color: c.secondary });
      }
      let width = 0;
      for (const line of lines) {
        ctx.font = line.font;
        width = Math.max(width, ctx.measureText(line.text).width + (line.key ? 20 : 0));
      }
      const boxW = width + 20;
      const boxH = lines.length * 20 + 12;
      let boxX = x + 14;
      if (boxX + boxW > this.width - 4) { boxX = x - 14 - boxW; }
      boxX = Math.max(4, boxX);
      const boxY = p.top + 4;
      ctx.fillStyle = c.surface;
      ctx.strokeStyle = c.axis;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.roundRect ? ctx.roundRect(boxX, boxY, boxW, boxH, 8) : ctx.rect(boxX, boxY, boxW, boxH);
      ctx.fill();
      ctx.stroke();
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      lines.forEach((line, i) => {
        const y = boxY + 6 + i * 20 + 10;
        let textX = boxX + 10;
        if (line.key) {
          ctx.strokeStyle = c.line; ctx.lineWidth = 2; ctx.lineCap = "round";
          ctx.beginPath(); ctx.moveTo(textX, y); ctx.lineTo(textX + 12, y); ctx.stroke();
          textX += 20;
        }
        ctx.font = line.font;
        ctx.fillStyle = line.color;
        ctx.fillText(line.text, textX, y);
      });
    }

    localX(e) { return e.clientX - this.canvas.getBoundingClientRect().left; }

    nearest(x) {
      const t = this.timeAt(x);
      let lo = 0;
      let hi = this.points.length - 1;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (this.points[mid][0] < t) { lo = mid + 1; } else { hi = mid; }
      }
      if (lo > 0 && Math.abs(this.points[lo - 1][0] - t) <= Math.abs(this.points[lo][0] - t)) { lo -= 1; }
      return lo;
    }

    onMove(e) {
      if (!this.points.length || !this.plot) { return; }
      const x = Math.min(Math.max(this.localX(e), this.plot.left), this.plot.right);
      if (this.drag) { this.drag.current = x; } else { this.hover = this.nearest(x); }
      this.draw();
    }

    onDown(e) {
      if (!this.points.length || !this.plot || e.button > 0) { return; }
      const x = Math.min(Math.max(this.localX(e), this.plot.left), this.plot.right);
      this.hover = this.nearest(x);
      if (this.options.onZoom) {
        this.drag = { start: x, current: x };
        this.canvas.setPointerCapture(e.pointerId);
      }
      this.draw();
    }

    onUp(e) {
      if (!this.drag) { return; }
      const drag = this.drag;
      this.drag = null;
      if (this.canvas.hasPointerCapture(e.pointerId)) { this.canvas.releasePointerCapture(e.pointerId); }
      if (Math.abs(drag.current - drag.start) >= MIN_DRAG_PIXELS) {
        const a = this.timeAt(Math.min(drag.start, drag.current));
        const b = this.timeAt(Math.max(drag.start, drag.current));
        this.options.onZoom(Math.floor(a), Math.ceil(b));
      }
      this.draw();
    }

    cancel() { this.drag = null; this.hover = -1; this.draw(); }

    onKey(e) {
      if (!this.points.length) { return; }
      const last = this.points.length - 1;
      const jump = e.shiftKey ? 10 : 1;
      let next = this.hover < 0 ? last : this.hover;
      if (e.key === "ArrowLeft") { next -= jump; }
      else if (e.key === "ArrowRight") { next += jump; }
      else if (e.key === "Home") { next = 0; }
      else if (e.key === "End") { next = last; }
      else if (e.key === "Escape") { this.hover = -1; this.draw(); return; }
      else { return; }
      e.preventDefault();
      this.hover = Math.min(Math.max(next, 0), last);
      this.draw();
    }
  }

  window.TimeChart = TimeChart;
  window.TimeChartFormat = { fullLabel: fullLabel, inputValue: inputValue, isoValue: isoValue, parseInput: parseInput };
})();
