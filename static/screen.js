// Projector view: lobby, question, reveal, leaderboard, finale. Read-only.
import { $, el, clear, show, key, icon, makeRing, msLeft, fmtCost, fmtLatency, roomFromUrl, Live, countTo } from "./common.js";

const S = { room: roomFromUrl(), bots: [], total: 0, names: new Set(), q: -1, question: null, joinUrl: "" };
const ring = makeRing("ring ring-xl");
$("#ring-slot").appendChild(ring.node);

const BOT_HUES = ["bot-a", "bot-b", "bot-c", "bot-d", "bot-e"];
function botClass(id) {
  const i = S.bots.findIndex((b) => b.id === id);
  return BOT_HUES[(i < 0 ? 0 : i) % BOT_HUES.length];
}

function setProgress(q, total) {
  $("#progress").textContent = q >= 0 && total ? `Question ${q + 1} of ${total}` : "";
}

function onRoom(m) {
  S.room = m.room;
  S.bots = m.bots || [];
  S.total = m.total || 0;
  S.joinUrl = m.join_url || "";
  $("#doc-title").textContent = m.title || "";
  $("#room-code").textContent = m.room || "";
  $("#join-url").textContent = m.join_display || m.join_url || "";
  // Until the host opens the room to the LAN, the join URL (and so the QR) is loopback only.
  const local = !m.lan;
  $("#local-hint").classList.toggle("hidden", !local);
  $("#qr-panel").classList.toggle("local", local);
  const qr = $("#qr");
  if (m.qr) {
    qr.classList.remove("hidden");
    $("#qr-fallback").classList.add("hidden");
    qr.src = "/qr.svg?room=" + encodeURIComponent(m.room) + "&v=" + encodeURIComponent(m.join_url || "");
  } else {
    qr.classList.add("hidden");
    $("#qr-fallback").classList.remove("hidden");
  }
  const vb = clear($("#versus-bots"));
  S.bots.forEach((b) => {
    const chip = el("span", "bot-chip " + botClass(b.id));
    chip.append(el("span", "dot"), el("span", "", b.label));
    vb.appendChild(chip);
  });
  if (S.room && roomFromUrl() !== S.room) {
    history.replaceState(null, "", "/screen?room=" + encodeURIComponent(S.room));
  }
  if (m.state === "lobby") show("v-lobby");
}

function onLobby(m) {
  countTo($("#player-count"), m.count || 0, 400);
  $("#player-word").textContent = (m.count === 1 ? " player" : " players");
  const box = $("#names");
  const incoming = m.names || [];
  const keep = new Set(incoming);
  Array.from(box.children).forEach((c) => { if (!keep.has(c.textContent)) c.remove(); });
  incoming.forEach((n) => {
    if (!Array.from(box.children).some((c) => c.textContent === n)) {
      const chip = el("span", "name-chip pop", n);
      box.appendChild(chip);
    }
  });
  if (S.q < 0) show("v-lobby");
}

function choiceTile(i, text, extraCls) {
  const tile = el("div", `choice c${i} ${extraCls || ""}`);
  tile.append(key(i), el("span", "choice-text", text));
  return tile;
}

function onQuestion(m) {
  S.q = m.q;
  S.question = m;
  setProgress(m.q, m.total);
  $("#q-num").textContent = `Question ${m.q + 1}`;
  $("#q-prompt").textContent = m.prompt;
  const box = clear($("#q-choices"));
  box.classList.toggle("two", (m.choices || []).length <= 2);
  (m.choices || []).forEach((c, i) => {
    const t = choiceTile(i, c, "enter");
    t.style.setProperty("--delay", `${120 + i * 90}ms`);
    box.appendChild(t);
  });
  $("#answered-count").textContent = "0";
  $("#answered-count").dataset.value = "0";
  renderBotStatus([]);
  const total = (Number(m.time_limit_s) || 20) * 1000;
  ring.start(msLeft(m), total);
  show("v-question");
}

function renderBotStatus(done) {
  const box = clear($("#bot-status"));
  S.bots.forEach((b) => {
    const isDone = done.includes(b.id);
    const chip = el("div", `bot-live ${botClass(b.id)} ${isDone ? "done" : "thinking"}`);
    const st = el("span", "bot-live-state");
    if (isDone) { st.appendChild(icon("check", "icon")); st.append(el("span", "", "locked in")); }
    else { st.append(el("span", "dots"), el("span", "", "thinking")); }
    chip.append(el("span", "bot-live-name", b.label), st);
    box.appendChild(chip);
  });
}

function onAnswered(m) {
  if (m.q !== S.q) return;
  countTo($("#answered-count"), m.humans || 0, 250);
  $("#answered-of").textContent = m.players ? `of ${m.players} answered` : "answered";
  renderBotStatus(m.bots_done || []);
}

function errorLabel(err) {
  if (err === "fallback") return "fallback model";
  if (err === "timeout") return "too slow";
  if (err === "budget") return "budget reached";
  if (err === "invalid_json") return "no valid answer";
  if (err === "trust_denied") return "not allowed";
  if (err === "refused") return "refused";
  if (err) return "error";
  return "";
}

function card({ cls, label, choice, correct, latency, cost, flag, sub }) {
  const c = el("div", `card ${cls} ${correct ? "ok" : "ko"}`);
  const head = el("div", "card-head");
  head.append(el("span", "card-label", label));
  const mark = el("span", "card-mark");
  mark.appendChild(icon(correct ? "check" : "cross", "icon"));
  head.appendChild(mark);
  const body = el("div", "card-body");
  if (choice !== null && choice !== undefined) {
    body.appendChild(key(choice));
  } else {
    body.appendChild(el("span", "card-pick none", "no answer"));
  }
  const stats = el("div", "card-stats");
  if (latency) { const s = el("span", "stat"); s.appendChild(icon("clock", "icon icon-sm")); s.append(el("span", "", latency)); stats.appendChild(s); }
  if (cost) stats.appendChild(el("span", "stat cost", cost));
  if (sub) stats.appendChild(el("span", "stat", sub));
  body.appendChild(stats);
  c.append(head, body);
  if (flag) c.appendChild(el("span", "card-flag", flag));
  return c;
}

function onReveal(m) {
  S.q = m.q;
  ring.stop();
  setProgress(m.q, m.total || S.total);
  $("#r-num").textContent = `Question ${m.q + 1}`;
  const choices = m.choices || (S.question && S.question.choices) || [];
  $("#r-prompt").textContent = m.prompt || (S.question && S.question.prompt) || "";
  const dist = m.distribution || [];
  const totalVotes = dist.reduce((a, b) => a + b, 0);
  const max = Math.max(1, ...dist);
  const box = clear($("#r-choices"));
  choices.forEach((c, i) => {
    const right = i === m.answer_index;
    const t = choiceTile(i, c, right ? "right" : "wrong");
    const bar = el("div", "dist");
    const fill = el("span", "dist-fill");
    fill.style.setProperty("--w", `${Math.round((100 * (dist[i] || 0)) / max)}%`);
    bar.appendChild(fill);
    const count = el("span", "dist-count", String(dist[i] || 0));
    t.append(bar, count);
    if (right) { const ok = el("span", "right-mark"); ok.appendChild(icon("check", "icon")); t.appendChild(ok); }
    box.appendChild(t);
  });
  $("#r-quote").textContent = m.source_quote || "";
  $("#r-expl").textContent = m.explanation || "";
  const cards = clear($("#r-cards"));
  const room = m.room || {};
  cards.appendChild(card({
    cls: "card-room", label: "The Room", choice: room.choice, correct: !!room.correct,
    sub: totalVotes ? `${totalVotes} vote${totalVotes === 1 ? "" : "s"}` : "no votes",
  }));
  (m.bots || []).forEach((b, i) => {
    const answered = b.choice !== null && b.choice !== undefined;
    const c = card({
      cls: "card-bot " + botClass(b.id), label: b.label, choice: b.choice, correct: !!b.correct,
      latency: answered ? fmtLatency(b.latency_ms) : (b.error === "timeout" ? "> limit" : ""),
      // an answered call always shows its cost, so a free model reads "$0"
      cost: b.cost_usd || answered ? fmtCost(b.cost_usd, b.cost_estimated) : "", flag: errorLabel(b.error) || (b.late ? "late" : ""),
    });
    c.style.setProperty("--delay", `${200 + i * 140}ms`);
    cards.appendChild(c);
  });
  show("v-reveal");
}

function renderRace(box, rows) {
  const max = Math.max(1, ...rows.map((r) => r.score || 0));
  const leader = rows.reduce((best, r) => (r.score > (best ? best.score : -1) ? r : best), null);
  const existing = new Map(Array.from(box.children).map((c) => [c.dataset.id, c]));
  rows.forEach((r) => {
    let row = existing.get(r.id);
    if (!row) {
      row = el("div", "race-row " + (r.id === "room" ? "race-room" : botClass(r.id)));
      row.dataset.id = r.id;
      const name = el("div", "race-name");
      name.append(el("span", "race-label", r.label), el("span", "race-correct", ""));
      const track = el("div", "race-track");
      const bar = el("div", "race-bar");
      track.appendChild(bar);
      const score = el("span", "race-score", "0");
      row.append(name, track, score);
      box.appendChild(row);
    }
    row.classList.toggle("leader", !!leader && leader.id === r.id && r.score > 0);
    row.querySelector(".race-correct").textContent = `${r.correct || 0} correct`;
    const bar = row.querySelector(".race-bar");
    requestAnimationFrame(() => bar.style.setProperty("--w", `${Math.max(2, Math.round((100 * (r.score || 0)) / max))}%`));
    countTo(row.querySelector(".race-score"), r.score || 0, 1100);
  });
}

function renderTop(box, top, limit) {
  clear(box);
  (top || []).slice(0, limit).forEach((p, i) => {
    const li = el("li", "top-row");
    li.style.setProperty("--delay", `${i * 70}ms`);
    li.append(el("span", "top-rank", String(i + 1)), el("span", "top-name", p.nickname), el("span", "top-score", (p.score || 0).toLocaleString("en-US")));
    box.appendChild(li);
  });
  if (!top || !top.length) box.appendChild(el("li", "top-empty", "No players yet: the models are playing alone"));
}

function onLeaderboard(m) {
  setProgress(m.q, m.total || S.total);
  if (m.final) {
    renderTop($("#f-top"), m.top, 5);
    renderRace($("#f-race"), m.room_vs_models || []);
    const rows = m.room_vs_models || [];
    const room = rows.find((r) => r.id === "room");
    const bestBot = rows.filter((r) => r.id !== "room").sort((a, b) => b.score - a.score)[0];
    let line = "";
    if (room && bestBot) line = room.score >= bestBot.score ? "The Room beat the models!" : `${bestBot.label} beat the Room`;
    $("#f-headline").textContent = line || "Game over";
    show("v-finished");
    return;
  }
  renderTop($("#lb-top"), m.top, 8);
  renderRace($("#race"), m.room_vs_models || []);
  $("#lb-hint").textContent = m.last ? "Last question done: final results next" : "Next question coming up";
  show("v-leaderboard");
}

function onFinished(m) {
  $("#f-report").textContent = m.report_url ? "The full report is ready for the host" : "";
  show("v-finished");
}

const live = new Live({
  onOpen(send) { send({ t: "screen", room: S.room }); },
  onStatus(st) { $("#net-status").classList.toggle("hidden", st === "open"); },
  onMessage(m) {
    switch (m.t) {
      case "room": onRoom(m); break;
      case "lobby": onLobby(m); break;
      case "question": onQuestion(m); break;
      case "answered": onAnswered(m); break;
      case "reveal": onReveal(m); break;
      case "leaderboard": onLeaderboard(m); break;
      case "finished": onFinished(m); break;
      case "error":
        if (m.code === "bad_room") { S.room = ""; live.send({ t: "screen", room: "" }); }
        break;
      default: break;
    }
  },
});
