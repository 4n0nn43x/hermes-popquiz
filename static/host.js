// Host console: token from the URL fragment (never sent in a URL), loopback only (enforced by the server).
import { $, el, clear, fmtCost, Live } from "./common.js";

const token = decodeURIComponent((location.hash || "").replace(/^#/, ""));
if (token) history.replaceState(null, "", "/host");
let authed = false;
let lastRoom = "";

function toast(text) { $("#toast").textContent = text || ""; }

const buttons = { open_lan: $("#b-lan"), start: $("#b-start"), next: $("#b-next"), end: $("#b-end") };
Object.entries(buttons).forEach(([cmd, b]) => b.addEventListener("click", () => {
  if (cmd === "end" && !window.confirm("End the quiz now?")) return;
  toast("");
  live.send({ t: "host", cmd });
}));

function setLink(node, url) {
  node.textContent = url || "";
  if (node.tagName === "A") node.href = url || "#";
}

function render(s) {
  const state = s.state || "idle";
  $("#state").textContent = state;
  $("#room").textContent = s.room || "------";
  $("#title").textContent = s.title || "";
  $("#players").textContent = String(s.players || 0);
  $("#question").textContent = s.total ? `${s.question || 0}/${s.total}` : "-";
  $("#answered").textContent = state === "question" ? String(s.answered || 0) : "-";
  const urls = s.urls || {};
  setLink($("#join"), urls.join);
  setLink($("#screen"), urls.screen);
  setLink($("#report"), state === "finished" ? urls.report : "");
  $("#est").textContent = fmtCost(s.estimated_cost_usd);
  $("#spent").textContent = fmtCost(s.spent_usd);
  $("#budget").textContent = s.budget_usd !== undefined && s.budget_usd !== null ? fmtCost(s.budget_usd) : "";
  buttons.open_lan.disabled = !!s.lan_open || state === "finished";
  buttons.open_lan.textContent = s.lan_open ? "Open to phones: on" : "Open to phones";
  buttons.start.disabled = state !== "lobby";
  buttons.next.disabled = !(state === "reveal" || state === "leaderboard" || state === "question");
  buttons.next.textContent = state === "question" ? "Close question" : "Next";
  buttons.end.disabled = state === "finished" || state === "idle";
  const box = clear($("#bots"));
  (s.bots || []).forEach((b, i) => {
    const row = el("div", "bot-row " + ["bot-a", "bot-b", "bot-c", "bot-d"][i % 4]);
    const status = b.stopped ? "stopped (budget)" : b.error ? b.error : b.answered ? "answered" : "";
    row.append(el("span", "", b.label), el("span", "muted", `${b.score || 0} pts ${status ? "· " + status : ""}`));
    box.appendChild(row);
  });
  if (s.room && s.room !== lastRoom) {
    lastRoom = s.room;
    const qr = $("#qr");
    qr.src = "/qr.svg?room=" + encodeURIComponent(s.room) + "&v=" + Date.now();
    qr.classList.remove("hidden");
    qr.addEventListener("error", () => qr.classList.add("hidden"), { once: true });
  }
  if (s.lan_open && urls.join) {
    $("#qr").src = "/qr.svg?room=" + encodeURIComponent(s.room) + "&v=" + encodeURIComponent(urls.join);
  }
}

const live = new Live({
  onOpen(send) {
    if (!token) { toast("Missing host token: open the host link printed by Hermes."); return; }
    send({ t: "host_auth", token });
  },
  onStatus(st) { if (st !== "open") $("#state").textContent = "reconnecting"; },
  onMessage(m) {
    if (m.t === "host_state") { authed = true; render(m); }
    else if (m.t === "error") {
      if (m.code === "forbidden" && !authed) toast("Access refused: wrong or expired host link.");
      else toast(m.code === "no_lan" ? "No local network address found" : m.code === "wrong_state" ? "Not possible right now" : "Error: " + m.code);
    }
  },
});
