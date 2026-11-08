// Phone player: join or resume with the stored token, big answer buttons, feedback, rank.
import { $, el, clear, show, key, icon, makeRing, msLeft, roomFromUrl, store, Live, LETTERS } from "./common.js";

const params = new URLSearchParams(location.search);
const room = roomFromUrl();
let pendingNick = (params.get("nick") || "").trim();
if (params.has("nick")) history.replaceState(null, "", "/play?room=" + encodeURIComponent(room));
const tokenKey = "popquiz.token." + room;
const S = { q: -1, choices: [], locked: false, myChoice: null, nickname: "", total: 0, fatal: false };
const ring = makeRing("ring");
$("#pq-ring").appendChild(ring.node);

if (!room) location.replace("/");

function fatal(title, text) {
  S.fatal = true;
  $("#err-title").textContent = title;
  $("#err-text").textContent = text;
  $("#err-link").href = "/?room=" + encodeURIComponent(room);
  show("v-error");
  live.stop();
}

function onQuestion(m) {
  S.q = m.q;
  S.choices = m.choices || [];
  S.locked = false;
  S.myChoice = null;
  $("#pq-num").textContent = `Question ${m.q + 1}${m.total ? " / " + m.total : ""}`;
  $("#pq-prompt").textContent = m.prompt || "";
  const pad = clear($("#pad"));
  pad.classList.remove("locked");
  S.choices.forEach((text, i) => {
    const b = el("button", `pad-btn c${i}`);
    b.type = "button";
    b.setAttribute("aria-label", `${LETTERS[i]}: ${text}`);
    b.append(key(i), el("span", "pad-text", text));
    b.addEventListener("click", () => answer(i));
    pad.appendChild(b);
  });
  $("#lock-note").textContent = "";
  ring.start(msLeft(m), (Number(m.time_limit_s) || 20) * 1000);
  show("v-question");
}

function answer(i) {
  if (S.locked || S.q < 0) return;
  if (!live.send({ t: "answer", q: S.q, choice: i })) {
    $("#lock-note").textContent = "Not connected, try again";
    return;
  }
  lock(i, "Sending...");
}

function lock(i, note) {
  S.locked = true;
  S.myChoice = i;
  const pad = $("#pad");
  pad.classList.add("locked");
  Array.from(pad.children).forEach((b, k) => { b.disabled = true; b.classList.toggle("chosen", k === i); });
  $("#lock-note").textContent = note;
  if (navigator.vibrate) { try { navigator.vibrate(30); } catch (_) { /* optional */ } }
}

function onReveal(m) {
  ring.stop();
  const you = m.you || {};
  const box = $("#result");
  const answered = you.choice !== null && you.choice !== undefined;
  box.className = "result " + (answered ? (you.correct ? "ok" : "ko") : "none");
  const ic = clear($("#res-icon"));
  if (answered) ic.appendChild(icon(you.correct ? "check" : "cross", "icon"));
  $("#res-verdict").textContent = !answered ? "No answer" : you.correct ? "Correct!" : "Not quite";
  $("#res-points").textContent = you.correct ? `+${you.points || 0}` : "";
  const right = (m.choices || S.choices)[m.answer_index];
  const ans = clear($("#res-answer"));
  if (right) ans.append(key(m.answer_index), el("span", "", right));
  const room = m.room || {};
  const lines = [];
  if (answered && !you.correct) lines.push(`You picked ${LETTERS[you.choice] || "?"}.`);
  if (room.choice !== null && room.choice !== undefined) lines.push(room.correct ? "The Room got it right." : "The Room got it wrong.");
  $("#res-detail").textContent = lines.join(" ");
  show("v-result");
}

function onLeaderboard(m) {
  const you = m.you;
  if (!you) return;
  $("#rank-title").textContent = m.final ? "Final rank" : "Your rank";
  $("#rank").textContent = you.rank ? `#${you.rank}` : "-";
  $("#rank-of").textContent = you.of ? `of ${you.of} players` : "";
  $("#rank-score").textContent = `${(you.score || 0).toLocaleString("en-US")} points`;
  $("#rank-note").textContent = m.final ? "Thanks for playing!" : "Next question soon";
  show("v-rank");
}

const live = new Live({
  onOpen(send) {
    const token = store.get(tokenKey);
    if (token) send({ t: "resume", room, token });
    else if (pendingNick) send({ t: "join", room, nickname: pendingNick });
    else location.replace("/?room=" + encodeURIComponent(room));
  },
  onStatus(st) { if (!S.fatal) $("#banner").classList.toggle("hidden", st === "open"); },
  onMessage(m) {
    switch (m.t) {
      case "welcome":
        store.set(tokenKey, m.token);
        pendingNick = "";
        S.nickname = m.nickname;
        $("#me").textContent = m.nickname;
        $("#lobby-nick").textContent = m.nickname;
        show("v-lobby");
        break;
      case "room": S.total = m.total || 0; break;
      case "lobby": $("#lobby-count").textContent = `${m.count} player${m.count === 1 ? "" : "s"} in the room`; break;
      case "question": onQuestion(m); break;
      case "locked": if (m.q === S.q) lock(m.choice, "Answer locked. Eyes on the big screen!"); break;
      case "reveal": onReveal(m); break;
      case "leaderboard": onLeaderboard(m); break;
      case "finished": $("#rank-note").textContent = "Thanks for playing!"; break;
      case "error": onError(m.code); break;
      default: break;
    }
  },
});

function onError(code) {
  if (code === "bad_token") {
    store.del(tokenKey);
    const nick = store.get("popquiz.nick");
    if (nick) { pendingNick = nick; live.send({ t: "join", room, nickname: nick }); }
    else fatal("Session expired", "Join again with your nickname.");
  } else if (code === "bad_room" || code === "no_game") fatal("Room not found", "Check the code on the big screen.");
  else if (code === "game_finished" || code === "game_over") fatal("Game over", "This quiz has ended. Thanks for playing!");
  else if (code === "game_full") fatal("Room full", "This quiz already has the maximum number of players.");
  else if (code === "server_full") fatal("Room is full", "Try again in a moment.");
  else if (code === "bad_nickname") fatal("Nickname refused", "Use 1 to 24 visible characters.");
  else if (code === "too_late") $("#lock-note").textContent = "Too late for this one!";
  else if (code === "already_answered") $("#lock-note").textContent = "Answer locked.";
  else if (code === "rate_limited") $("#lock-note").textContent = "Slow down a little";
}
