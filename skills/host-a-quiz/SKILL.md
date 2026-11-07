---
name: host-a-quiz
description: "Use when asked for a quiz on a document. Runs Pop Quiz."
version: 0.1.0
license: MIT
metadata:
  hermes:
    tags: [quiz, education, training, documents, multiplayer]
    related_skills: []
---

# Host a quiz (Pop Quiz)

Turns a document into a live multiplayer quiz: people join from their phones with a QR code, and AI
models from the Nous Portal answer the same questions on the same clock. You write the questions; the
`popquiz_create` tool checks every `source_quote` against the document and refuses the ones it
cannot find, so you fix them.

## When to use

- The user sends or names a document (runbook, product doc, policy, course notes) and asks for a quiz,
  a pop quiz, a test, a trivia game for the team or the class.
- The user asks to start, advance or end a running Pop Quiz, or asks for its report.
- Do not use for a quiz with no source document (the tool needs a file to anchor every question).

## 1. Read the document

- A file sent on a messaging platform: the conversation says where it is saved; use that path.
- Otherwise a file under the current folder. Read it with your file reading tool (PDF, DOCX and EPUB
  are extracted to text).
- Hidden files and files inside the Hermes home (except its document cache) are refused.

## 2. Write the questions

Write 5 to 12 multiple-choice questions (default 8) about what matters in the document.

- `prompt`: one clear question, at most 240 chars, answerable from the document alone. It must not
  contain the text of the correct choice.
- `choices`: 2 to 4 short, distinct, plausible options (at most 90 chars each). Exactly one is right.
- `answer_index`: 0-based index of the correct choice. Vary its position across questions.
- `source_quote`: 12 to 300 chars copied WORD FOR WORD from the document, proving the answer.
  Copy and paste, never paraphrase, never change a number, never join two distant sentences. Quote
  the smallest passage that contains the answer.
- `explanation`: one sentence shown on the projector after the reveal.

Call `popquiz_create` with `doc_path`, `questions`, an optional `title`, and `mode`:
`open_book` (default, the models see the document) or `closed_book` (they see only the question).

## 3. Handle refusals

`status: needs_fix` means some questions were refused. The answer carries a `draft_id`, the indexes
already `accepted` (kept by the tool), and `rejected` entries with `index`, `code`, `detail`,
`best_score` and `closest_passage` (the nearest text found in the document).

- `quote_not_found`: the quote is not in the document. Replace it with the exact passage, usually the
  `closest_passage` or the sentence around it.
- `number_mismatch`: a number in your quote is not in the document. Read the passage again; fix the
  quote and, if needed, the question and the choices.
- `quote_too_short`: quote a longer passage (12 chars or more).
- `bad_schema`, `duplicate_choice`, `prompt_leaks_answer`: fix the question itself.

Then call `popquiz_create` again with the same `draft_id` and ONLY the fixed questions, in the same
order as `rejected`. Do not resend accepted questions and do not send `doc_path` again. You have 3
attempts in total; after the last one, refused questions are dropped. `answer_not_in_quote` in
`warnings` is advice, not a refusal. `needs_more` means fewer than 3 questions survived: send more
with the same `draft_id`.

Tell the user briefly what was refused and how you fixed it.

## 4. Run the game

`status: ready` returns `room`, `urls` (`screen`, `join`, `host`), `players` (the model players) and
`estimated_cost_usd`.

1. Give the user the screen URL to open on the projector. The host URL works only on this machine.
2. Phones can join only after `popquiz_control` with `action: open_lan` (or `/popquiz open`); do it
   when the user wants people in the room to play. Until then the server listens on loopback only.
3. `popquiz_control` actions: `start`, `next` (next question), `skip` (close the current question
   now), `end`. The user can also type `/popquiz` for buttons, or use the host page.
4. `popquiz_status` gives the state, players, model spend and public URLs.

Model players are paid calls through the user's Nous Portal account, capped by the `budget_usd`
setting; say so if the user asks about cost. If `degraded` is true, only the user's main model plays
(the plugin was not granted model overrides).

## 5. After the game

When the game ends the report comes back into the conversation. If it does not, call
`popquiz_report`. Then explain in a few lines what the room misunderstood (each with the document
quote), which model understood the document best and for what cost, and propose remediation: a fix
to an unclear passage of the document, or a short follow-up quiz on the weak points.
