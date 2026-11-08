// Shared helpers for every Pop Quiz page. No inline script, no CDN, text only via textContent.

export const LETTERS = ["A", "B", "C", "D"];
const SVG_NS = "http://www.w3.org/2000/svg";

export function $(sel, root = document) {
  return root.querySelector(sel);
}

export function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

export function clear(node) {
  while (node && node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function show(viewId) {
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === viewId));
}

function svgEl(name, attrs) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, String(v));
  return node;
}

// Answer key for option i: a keycap carrying its letter. The letter, not the hue, is the meaning,
// so colour-blind players and the models' badges read the same thing ("C"), on every screen.
export function key(i, cls = "") {
  const node = el("span", `key c${i} ${cls}`.trim(), LETTERS[i] || "?");
  node.setAttribute("aria-hidden", "true");
  return node;
}

export function icon(kind, cls = "icon") {
  const svg = svgEl("svg", { viewBox: "0 0 24 24", class: cls, "aria-hidden": "true", focusable: "false" });
  if (kind === "check") svg.appendChild(svgEl("path", { d: "M4 12.5 L9.5 18 L20 6.5", fill: "none" }));
  else if (kind === "cross") svg.appendChild(svgEl("path", { d: "M6 6 L18 18 M18 6 L6 18", fill: "none" }));
  else if (kind === "clock") {
    svg.appendChild(svgEl("circle", { cx: 12, cy: 12, r: 9, fill: "none" }));
    svg.appendChild(svgEl("path", { d: "M12 7 V12 L15.5 14", fill: "none" }));
  }
  return svg;
}

// Countdown ring: returns {node, start(msLeft, totalMs), stop()}.
export function makeRing(cls = "ring") {
  const wrap = el("div", cls);
  const svg = svgEl("svg", { viewBox: "0 0 120 120", "aria-hidden": "true" });
  const r = 52;
  const len = 2 * Math.PI * r;
  svg.appendChild(svgEl("circle", { cx: 60, cy: 60, r, class: "ring-track" }));
  const bar = svgEl("circle", { cx: 60, cy: 60, r, class: "ring-bar", "stroke-dasharray": len.toFixed(2) });
  svg.appendChild(bar);
  const num = el("span", "ring-num", "");
  wrap.append(svg, num);
  let raf = 0;
  let endAt = 0;
  let total = 1;
  function tick() {
    const left = Math.max(0, endAt - performance.now());
    const frac = Math.max(0, Math.min(1, left / total));
    bar.style.strokeDashoffset = String(len * (1 - frac));
    const secs = Math.ceil(left / 1000);
    if (num.textContent !== String(secs)) num.textContent = String(secs);
    wrap.classList.toggle("urgent", left <= 5000 && left > 0);
    wrap.classList.toggle("done", left <= 0);
    if (left > 0) raf = requestAnimationFrame(tick);
  }
  return {
    node: wrap,
    start(msLeft, totalMs) {
      cancelAnimationFrame(raf);
      total = Math.max(1, totalMs || msLeft);
      endAt = performance.now() + Math.max(0, msLeft);
      tick();
    },
    stop() {
      cancelAnimationFrame(raf);
      endAt = performance.now();
      tick();
    },
  };
}

// Milliseconds left for a `question` message, using only the server's own clock difference.
export function msLeft(msg) {
  const left = Number(msg.ends_at_ms) - Number(msg.server_now_ms);
  return Number.isFinite(left) ? Math.max(0, left) : 0;
}

// localStorage with an in-memory fallback (private mode, sandboxed frames).
const memory = new Map();
export const store = {
  get(key) {
    try {
      const v = window.localStorage.getItem(key);
      if (v !== null) return v;
    } catch (_) { /* storage unavailable */ }
    return memory.has(key) ? memory.get(key) : null;
  },
  set(key, value) {
    memory.set(key, value);
    try { window.localStorage.setItem(key, value); } catch (_) { /* memory only */ }
  },
  del(key) {
    memory.delete(key);
    try { window.localStorage.removeItem(key); } catch (_) { /* memory only */ }
  },
};

// A cost booked at its estimate (call launched, real cost unknown) reads "~$x (est.)".
export function fmtCost(usd, estimated = false) {
  const v = Number(usd) || 0;
  let text = "$" + v.toFixed(2);
  if (v === 0) text = "$0";
  else if (v < 0.001) text = "<$0.001";
  else if (v < 0.1) text = "$" + v.toFixed(4);
  return estimated && v > 0 ? `~${text} (est.)` : text;
}

export function fmtLatency(ms) {
  if (ms === null || ms === undefined) return "";
  return (Number(ms) / 1000).toFixed(1) + " s";
}

export function roomFromUrl() {
  const p = new URLSearchParams(location.search);
  return (p.get("room") || "").trim().toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 8);
}

// Reconnecting WebSocket. onOpen(send) runs on every (re)connection to re-identify.
export class Live {
  constructor({ onMessage, onOpen, onStatus }) {
    this.onMessage = onMessage;
    this.onOpen = onOpen;
    this.onStatus = onStatus || (() => {});
    this.ws = null;
    this.backoff = 400;
    this.closed = false;
    this.pingTimer = 0;
    this.connect();
  }
  url() {
    return (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
  }
  connect() {
    if (this.closed) return;
    this.onStatus("connecting");
    let ws;
    try {
      ws = new WebSocket(this.url());
    } catch (_) {
      this.retry();
      return;
    }
    this.ws = ws;
    ws.addEventListener("open", () => {
      this.backoff = 400;
      this.onStatus("open");
      this.onOpen((m) => this.send(m));
      clearInterval(this.pingTimer);
      this.pingTimer = setInterval(() => this.send({ t: "ping" }), 20000);
    });
    ws.addEventListener("message", (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      if (msg && typeof msg.t === "string") this.onMessage(msg);
    });
    ws.addEventListener("close", () => {
      clearInterval(this.pingTimer);
      this.onStatus("closed");
      this.retry();
    });
    ws.addEventListener("error", () => { try { ws.close(); } catch (_) { /* ignore */ } });
  }
  retry() {
    if (this.closed) return;
    const wait = this.backoff + Math.random() * 250;
    this.backoff = Math.min(5000, this.backoff * 1.7);
    setTimeout(() => this.connect(), wait);
  }
  send(msg) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
      return true;
    }
    return false;
  }
  stop() {
    this.closed = true;
    clearInterval(this.pingTimer);
    if (this.ws) this.ws.close();
  }
}

// Animated number (count up), used by the race and the scores.
export function countTo(node, target, ms = 900) {
  const from = Number(node.dataset.value || 0);
  const to = Number(target) || 0;
  node.dataset.value = String(to);
  if (from === to) { node.textContent = to.toLocaleString("en-US"); return; }
  const t0 = performance.now();
  function step(now) {
    const k = Math.min(1, (now - t0) / ms);
    const e = 1 - Math.pow(1 - k, 3);
    node.textContent = Math.round(from + (to - from) * e).toLocaleString("en-US");
    if (k < 1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}
