# Self-improving skills loop — plan (P-457, 2026-10-26; Phase 1 built, Phases 2-3 not built)

Goal (Mohit, 2026-10-26): every skill in CRISP notices its own problems by watching how work is actually done,
and proposes an improvement to itself. Applies to ALL skills (global `~/.claude/skills`, Codex skills, project
skills), not only the engineering loop. The idea of a skill that observes task execution and writes improvement
notes is credited to rebelytics' "Task Observer" skill (github.com/rebelytics/one-skill-to-rule-them-all,
CC BY 4.0). That skill is NOT installed; this is CRISP's own build, reusing CRISP's existing parts.

## Ground rules

1. Observe, propose, never self-edit. No skill file changes without a reviewed patch and Mohit's yes.
2. Evidence or nothing. Every proposal cites the observations behind it (session id, date, file, quote <= 15 words).
3. Reuse, don't add stores. Observations go to the vault's existing per-project tree in JSONL, like instincts.
4. Cheap by default. Capture is regex/hook-level (no model call per turn); the model runs only in the weekly batch.
5. Diagnostics and fixes stay separate (global rule 1): the batch writes proposals; applying one is its own task.
6. No secrets: the same secret filter `learnInstinctFromPrompt` uses runs on every captured line.

## What already exists (reuse)

| Part | Where | Reused for |
|---|---|---|
| Instinct candidates (0.45, `tea instincts promote/drop`) | `engine/lib/hook-runtime.js` | proposal lifecycle shape: candidate -> reviewed -> live |
| Mistake-admission detector + relevance-by-file gate | `claude/hooks/auto_handover.py` (`_scan_mistakes`, `_relevant_mistakes`) | signal source + "show the right lesson at the right time" |
| Vault staging per project | `<vault>/projects/<project>/staging.md` | landing zone for reviewed proposals |
| Response log | `claude/hooks/usage_tracker.py` | turn counts per skill (denominator) |
| MEL roster (`~/.mel/roster.jsonl`) | mohit-engineering-loop | review findings per model/task as a signal for MEL itself |
| Skill keyword map | `claude/hooks/skill_suggest.py` | knowing which skills exist / were suggested |

## Loop

1. **Attribute.** A Stop-hook step (inside `auto_handover.py`, not a new hook) reads the turn's transcript and
   records which skills were active: `Skill` tool calls, `<command-name>` blocks, skill files read.
2. **Capture signals** for that turn, each tied to the active skill(s):
   - user corrections ("no", "don't", "stop", "wrong", "I said") within 2 turns of a skill's output;
   - mistake admissions already detected for MISTAKES.md;
   - tool errors and retries of the same command (>= 2) while the skill drove the work;
   - skill steps skipped or contradicted by the user (skill says X, user overrides);
   - review findings recorded by MEL (`finding`) against work a skill guided;
   - repeated manual sequences (same 3+ tool-call pattern in >= 3 sessions) -> NEW-skill candidate.
   One JSON line per signal in `<vault>/projects/<project>/skill-observations.jsonl`
   `{ts, session, project, skill, kind, evidence, quote, files}`; skill `(project)` when none was active (old rows say `unknown`; readers treat both as `(project)`).
3. **Weekly batch** (`tea skills review`, Sunday, or by hand): cluster observations per skill. A pattern seen
   >= 2 times, or in >= 2 projects, becomes a PROPOSAL: problem, evidence list, suggested edit as a unified
   diff against the installed skill, expected effect, how recurrence will be measured. Written to
   `<vault>/skill-proposals/<skill>/<date>-<n>.md` with status `candidate`. One model call per skill with
   observations; skills with none cost nothing.
4. **Review.** The other provider reviews each proposal (Claude-built proposal -> Codex, and vice versa), as
   in MEL plan review. Verdict + findings appended to the proposal.
5. **Approve and apply.** Mohit approves per proposal (`tea skills approve <id>` / `reject <id> --reason`).
   Approved patches are applied to the skill's SOURCE repo on a branch, then installed with backups like MEL
   installs. Rejections are kept so the same proposal is not raised again.
6. **Measure.** After install, the same signal kind for that skill is counted for the next 4 weeks. Fewer
   recurrences = kept; same or more = proposal reopened with the new evidence. This is the eval.

## Build phases

- Phase 1 (capture only, 1 session): attribution + signal capture + `tea skills observations` listing. Run 2
  weeks; check false-positive rate by hand before any batch exists (the 2026-09-08 MISTAKES lesson: regexes on
  analysis prose fabricate entries).
  **BUILT 2026-10-26, not yet committed** (`_capture_skill_observations` in `claude/hooks/auto_handover.py`,
  `tea.js skills observations`, tests in `claude/hooks/tests/`). What it does and does not do:
  - **Phase 1 finding 2026-10-31 (16 real rows):** attribution works (skill-active rows named the right skill).
    The 14 `unknown` rows were turns with no skill active, mostly `retry` rows caused by harness preconditions
    (Write/Edit before Read, TaskStop on a finished task, permission blocks). Fixed: no-skill signals are now
    bucketed `(project)` = the project's own workflow (feeds CLAUDE.md / MISTAKES.md, never a skill patch), and
    failed tool results matching `HARNESS_PRECONDITION_ERRORS` are ignored by the retry signal.
  - Signals captured: `correction`, `override`, `mistake`, `retry`. Not captured yet: MEL review findings and
    repeated manual sequences (new-skill candidates); both move to Phase 2.
  - Attribution window: skills active in the current turn or the two before it. A correction or override is
    tied to skills from the two turns BEFORE the user's message; the evidence field says how many turns back.
  - One line per (signal, skill), with a `turn` field (first 8 chars of the user message uuid) added to the
    schema above; dedup is per session + turn + kind + skill.
  - Replayed over 703 real turns from 45 recent sessions before shipping: 15 retry and 3 correction signals.
    A bare leading "no"/"wait"/"stop" was dropped from the correction patterns after it fired on answers to
    questions (6 of 9 hits in the first replay).
  - Reads only the transcript tail (whole-file read cost 56 s on a 1.4 GB transcript; tail read 0.2 s).
  - Hand-check after 2 weeks: `node engine/cli/tea.js skills observations --json`, mark each row real/false,
    and only build Phase 2 if the false-positive rate is acceptable.
- Phase 2: weekly batch + proposal files + cross-provider review.
- Phase 3: approve/apply/measure + new-skill candidates.

## Decisions (Mohit, 2026-10-26)

1. **Cadence:** every weekly audit is followed by the cross-provider review of its proposals, then the
   patches are applied. Approval stays one yes per weekly batch (Mohit sees the batch list, approves or strikes
   items), so patching is not silent (ground rule 1).
2. **Writer/reviewer (left to Claude):** Codex writes the proposals and patches (more weekly headroom);
   Claude reviews them. If Codex is out of capacity, Claude writes and the review waits for Codex: a writer
   never reviews its own proposal.
   **Amended 2026-11-11 (Mohit: "this is a two model engineering skill but if codex is unavailable or claude is
   unavailable... one model need to plan review and implementation"):** two companies stay the default. When one
   is unavailable, ask Mohit whether to wait or continue with the available company alone; alone, that company
   writes and reviews on DIFFERENT models (e.g. Sonnet writes, Opus or Fable reviews in a fresh session), as in
   the engineering skill's single-provider mode, and the proposal is labelled single-provider. A writer model
   never reviews its own proposal.
3. **Third-party skills are patched too, as owned copies:** the skill is copied into
   `~/tools/skill-forks/<skill>/` (source repo/version and date recorded in `UPSTREAM.md`), the patch is applied
   to the copy, and the copy is installed in place of the original (backup kept). Each weekly audit also
   checks whether the upstream skill changed since the copy; if so, merging the upstream change into the copy
   is listed as a batch item. The original author's licence and credit stay in the copy.

## Phase 1 two-week hand-check (2026-11-11, P-457 Batch A)

Snapshot of `skill-observations.jsonl` taken 2026-11-11 (first row 2026-10-27): 46 rows in 29 sessions, 40 of them
a private project. By kind: 37 `retry`, 9 `mistake`, 0 `correction`, 0 `override`. Skill-attributed: 2 of 46
(`watch` retry, `claude-api` retry); the other 44 are `(project)`.

- **Mistake false positives: 3 of 9 (33%).** Rows 36, 38 and 42 are false: row 36 and row 38 quote the ledger's own
  earlier false draft ("That was wrong: `core/llm_budget."), row 42 is the word "ABI" inside a library note
  ("ABI broke recently" matched `I broke`). Rows 0, 18, 19, 28, 29, 45 are real admissions.
- **Retry:** 37 rows, not hand-classified one by one here; the harness-precondition filter (8849d73) was written for
  the earlier batch of them.
- **Correction detector has a recall hole:** 0 rows in two weeks although Mohit wrote "it is not working" and
  "it still shows the old one" in that period. The patterns only knew "that's wrong / not what I asked".
  Fixed in Batch A step 2 (new "not working / still broken" family, guarded against conditionals, pasted relays,
  briefs and new bug reports).
- Skill attribution is rare because few skills were invoked (12 `Skill` calls in two weeks), not because attribution fails.

### Phase 1 capture limits and Batch A changes (2026-11-11)

- **Codex has no capture (A7).** The only producer of `skill-observations.jsonl` is the Claude Stop hook
  (`claude/hooks/auto_handover.py`); `crisp/codex/` holds only AGENTS.md. A Codex-only session therefore produces zero
  observations. Not built now. Codex sessions are stored under `~/.codex/sessions/YYYY/MM/DD/*.jsonl`; a later replay
  job could read those files and apply the same detectors. Until then, a weekly batch must say "Claude sessions only".
- **Corrections (A1/H2):** new "not working / still broken / still shows" family; no correction on a turn with no earlier
  assistant reply; conditionals, task asks, pasted relays and tables are excluded. Replay over 1253 transcripts
  (6680 real user turns, mtime >= 2026-10-01): old patterns 13 hits, new 20; hand precision 16/20 (80%), counting four
  borderline hits as false.
- **Mistakes (A3):** the shared `_scan_mistakes` layer now reads assistant messages only, ignores table/blockquote lines
  and double-quoted spans, and requires a word boundary before "I broke" etc. (row 42 was "ABI broke"). Bullets are kept
  because a real admission can be a bullet (row 45).
- **Harness errors (A4):** "File has been modified since read ..." joins the precondition list for Write/Edit/NotebookEdit.
- **MEL attribution (A5):** a turn counts as `mohit-engineering-loop` when its cwd or a touched path is inside a checkout
  with a non-terminal `.mel/MEL-nnn/state.json` (status other than merged/closed). Evidence says "inferred", not "invoked".
