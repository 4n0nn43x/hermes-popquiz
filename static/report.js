// Post-game report page (public data only, available once the game is finished).
import { $, el, clear, key, fmtCost, fmtLatency, roomFromUrl, LETTERS } from "./common.js";

const room = roomFromUrl();

function statBox(value, label) {
  const p = el("div", "panel");
  p.append(el("span", "stat-big", value), el("span", "hero-sub", label));
  return p;
}

function render(d) {
  $("#title").textContent = d.title || "Pop Quiz";
  $("#sub").textContent = `Room ${d.room} · ${d.played} of ${d.total} questions played`;
  const rows = d.room_vs_models || [];
  const roomRow = rows.find((r) => r.id === "room") || { score: 0, correct: 0 };
  const best = (d.models || []).slice().sort((a, b) => b.score - a.score)[0];
  const sum = clear($("#summary"));
  sum.append(
    statBox(String(d.players), "players"),
    statBox(`${roomRow.correct}/${d.played}`, "The Room correct"),
    statBox(best ? best.label : "-", "best model"),
    statBox(fmtCost(d.spent_usd), "spent on models"),
  );
  const tb = clear($("#models"));
  const tr0 = el("tr");
  tr0.append(el("td", "accent-room", "The Room"), el("td", "", String(roomRow.score)), el("td", "", String(roomRow.correct)), el("td", "", ""), el("td", "", ""), el("td", "", "majority vote of the humans"));
  tb.appendChild(tr0);
  (d.models || []).forEach((m) => {
    const tr = el("tr");
    tr.append(el("td", "", m.label), el("td", "", String(m.score)), el("td", "", String(m.correct)),
      el("td", "", m.median_latency_ms === null ? "" : fmtLatency(m.median_latency_ms)), el("td", "", fmtCost(m.cost_usd)),
      el("td", "", (m.errors || []).join(", ")));
    tb.appendChild(tr);
  });
  const qs = clear($("#questions"));
  (d.questions || []).filter((q) => q.played).forEach((q) => {
    const card = el("div", "panel qcard");
    const h = el("h3", "", `${q.q + 1}. ${q.prompt}`);
    const tags = el("p", "");
    if (q.correct_pct !== null) tags.appendChild(el("span", "tag " + (q.correct_pct >= 50 ? "ok" : "ko"), `${q.correct_pct}% of humans right`));
    tags.appendChild(el("span", "tag " + (q.room.correct ? "ok" : "ko"), q.room.correct ? "Room right" : "Room wrong"));
    (q.bots || []).forEach((b) => tags.appendChild(el("span", "tag " + (b.correct ? "ok" : "ko"), b.label)));
    card.append(h, tags);
    const max = Math.max(1, ...q.distribution);
    q.choices.forEach((c, i) => {
      const row = el("div", `qbar c${i} ${i === q.answer_index ? "right" : ""}`);
      const track = el("div", "track");
      const fill = el("div", "fill");
      fill.style.setProperty("--w", `${Math.round((100 * (q.distribution[i] || 0)) / max)}%`);
      track.append(fill, el("span", "lbl", c));
      row.append(key(i), track, el("span", "", String(q.distribution[i] || 0)));
      card.appendChild(row);
    });
    if (q.trap && q.trap.count) card.appendChild(el("p", "hero-sub", `Most common wrong answer: ${LETTERS[q.trap.choice]}. ${q.choices[q.trap.choice]}`));
    card.appendChild(el("p", "small-quote", q.source_quote));
    qs.appendChild(card);
  });
}

async function load() {
  try {
    const res = await fetch("/api/report?room=" + encodeURIComponent(room), { cache: "no-store" });
    if (res.status === 409) { $("#title").textContent = "The game is still running"; setTimeout(load, 4000); return; }
    if (!res.ok) { $("#title").textContent = "Report not found"; return; }
    render(await res.json());
  } catch (_) {
    $("#title").textContent = "Report unavailable";
  }
}
load();
