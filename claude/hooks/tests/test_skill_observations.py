"""Tests for the skill-observation capture in auto_handover.py (P-457 phase 1).

Run:  python -m unittest discover -s claude/hooks/tests -v   (from the crisp repo root)

Stdlib only. Every transcript here is synthetic, shaped like the real Claude Code
JSONL seen on this machine 2026-10-26: skill text arrives as a role=user isMeta entry
("Base directory for this skill: ..."), Stop-hook feedback is isMeta too, and a
tool_result is a role=user entry with no text block.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HOOKS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS))
import auto_handover as ah  # noqa: E402

_n = [0]


def _uid():
    _n[0] += 1
    return f"u{_n[0]:07d}-0000"


def user(text, **extra):
    return {"type": "user", "uuid": _uid(), "message": {"role": "user", "content": text}, **extra}


def meta(text):
    return {"type": "user", "uuid": _uid(), "isMeta": True,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def say(text):
    return {"type": "assistant", "uuid": _uid(),
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def tool(name, inp, tid):
    return {"type": "assistant", "uuid": _uid(),
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def result(tid, text, error=False):
    block = {"type": "tool_result", "tool_use_id": tid, "content": text}
    if error:
        block["is_error"] = True
    return {"type": "user", "uuid": _uid(), "message": {"role": "user", "content": [block]}}


def skill_call(name, tid="s1"):
    return [tool("Skill", {"skill": name}, tid), result(tid, f"Launching skill: {name}"),
            meta(f"Base directory for this skill: C:\\Users\\x\\.claude\\skills\\{name}\n\n# {name}\nDon't skip steps.")]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.vault = root / "vault"
        self.repo = root / "My Repo"
        (self.repo / ".git").mkdir(parents=True)
        self.transcript = root / "t.jsonl"
        self.env = mock.patch.dict(os.environ, {"TEA_MEMORY_DIR": str(self.vault)})
        self.env.start()
        self.errlog = mock.patch.object(ah, "ERROR_LOG", root / "hook-errors.log")
        self.errlog.start()

    def tearDown(self):
        self.errlog.stop()
        self.env.stop()
        self.tmp.cleanup()

    def write(self, entries):
        flat = []
        for e in entries:
            flat.extend(e if isinstance(e, list) else [e])
        self.transcript.write_text("\n".join(json.dumps(e) for e in flat) + "\n", encoding="utf-8")

    def capture(self, mistakes=(), session="sess-1"):
        n = ah._capture_skill_observations(session, str(self.repo), str(self.transcript), list(mistakes))
        return n, self.rows()

    def rows(self):
        p = self.vault / "projects" / "my-repo" / "skill-observations.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


class Attribution(Base):
    def turns(self, entries):
        self.write(entries)
        return ah._parse_recent_turns(str(self.transcript))

    def test_skill_tool_and_meta_base_directory(self):
        t = self.turns([user("build it"), *skill_call("codex-build")])
        self.assertEqual(t[-1]["skills"], {"codex-build"})

    def test_slash_command_counts_but_builtin_does_not(self):
        t = self.turns([user("<command-message>watch</command-message>\n<command-name>/watch</command-name>\n<command-args>url</command-args>")])
        self.assertEqual(t[-1]["skills"], {"watch"})
        t = self.turns([user("<command-name>/model</command-name>\n<command-args>opus</command-args>")])
        self.assertEqual(t[-1]["skills"], set())

    def test_reading_a_skill_file_counts(self):
        t = self.turns([user("check the loop"), tool("Read", {"file_path": "C:/x/skills/claudex-loop/SKILL.md"}, "r1"),
                        result("r1", "...")])
        self.assertEqual(t[-1]["skills"], {"claudex-loop"})

    def test_quoted_command_name_in_tool_input_is_not_attribution(self):
        # Positive control found the real failure: grepping raw lines matched my own Bash
        # command text. Only real user text may carry a <command-name> block.
        t = self.turns([user("look"), tool("Bash", {"command": "grep '<command-name>/graphify</command-name>' x"}, "b1"),
                        result("b1", "<command-name>/graphify</command-name>")])
        self.assertEqual(t[-1]["skills"], set())

    def test_tail_read_grows_past_large_turns(self):
        # Three turns spread over ~3 MB: the 1 MB first window holds only one of them.
        pad = "x" * (1 << 20)
        t = self.turns([say(pad), user("first"), *skill_call("alpha"), say(pad), user("second"), say(pad),
                        user("third")])
        self.assertEqual([x["text"] for x in t], ["first", "second", "third"])
        self.assertEqual(t[0]["skills"], {"alpha"})

    def test_meta_and_tool_results_are_not_turn_boundaries(self):
        t = self.turns([user("one"), say("a"), meta("Stop hook feedback:\nthat's wrong"), result("x", "ok"),
                        user("two")])
        self.assertEqual([x["text"] for x in t], ["one", "two"])


class Signals(Base):
    def test_correction_after_skill_output(self):
        self.write([user("plan it"), *skill_call("codex-review"), say("Here is the plan."),
                    user("no i meant the other file")])
        n, rows = self.capture()
        self.assertEqual(n, 1)
        r = rows[0]
        self.assertEqual((r["skill"], r["kind"], r["project"]), ("codex-review", "correction", "my-repo"))
        self.assertIn("1 turn(s) earlier", r["evidence"])
        self.assertEqual(r["quote"], "no i meant the other file")
        self.assertEqual(set(r), {"ts", "session", "project", "turn", "skill", "kind", "evidence", "quote", "files"})

    def test_correction_three_turns_later_is_project(self):
        self.write([user("go"), *skill_call("codex-review"), say("a"), user("ok"), say("b"), user("next"), say("c"), user("more"), say("d"),
                    user("that's wrong, redo it")])
        _, rows = self.capture()
        self.assertEqual([(r["skill"], r["kind"]) for r in rows], [("(project)", "correction")])

    def test_answer_to_a_question_is_not_a_correction(self):
        # Seen in the 702-turn replay before the pattern was narrowed.
        for reply in ("no whatever u r doing based on hand off prompt is fine", "wait for this.", "no need, thanks",
                      "stop the checkin for weekend"):
            self.write([user("go"), *skill_call("x"), say("Shall I also do Y?"), user(reply)])
            n, _ = self.capture(session=reply)
            self.assertEqual(n, 0, reply)

    def test_override_needs_an_active_skill(self):
        self.write([user("go"), *skill_call("mohit-engineering-loop"), say("Step 3: run the review."),
                    user("skip the review step and commit")])
        _, rows = self.capture()
        self.assertEqual([(r["skill"], r["kind"]) for r in rows], [("mohit-engineering-loop", "override")])
        self.write([user("hello"), say("hi"), user("skip the review step and commit")])
        n, _ = self.capture(session="no-skill")
        self.assertEqual(n, 0)

    def test_retry_of_same_failing_command(self):
        self.write([user("run tests"), *skill_call("dev-review-pipeline", "s9"),
                    tool("Bash", {"command": "pytest  -q tests"}, "b1"), result("b1", "Exit code 1", error=True),
                    tool("Bash", {"command": "pytest -q tests"}, "b2"), result("b2", "Exit code 1", error=True)])
        _, rows = self.capture()
        self.assertEqual([(r["skill"], r["kind"]) for r in rows], [("dev-review-pipeline", "retry")])
        self.assertIn("failed 2 times", rows[0]["evidence"])
        self.assertEqual(rows[0]["quote"], "pytest -q tests")

    def test_single_failure_or_differing_commands_is_not_a_retry(self):
        self.write([user("run"), tool("Bash", {"command": "a"}, "b1"), result("b1", "x", error=True),
                    tool("Bash", {"command": "b"}, "b2"), result("b2", "x", error=True)])
        n, _ = self.capture()
        self.assertEqual(n, 0)

    def test_no_skill_turn_is_project_bucket_with_evidence(self):
        self.write([user("hello"), say("hi"), user("you missed the second file")])
        _, rows = self.capture()
        self.assertEqual(rows[0]["skill"], "(project)")
        self.assertIn("no skill active - project workflow", rows[0]["evidence"])

    def test_precondition_only_double_failure_is_not_a_retry(self):
        cases = [("Write", {"file_path": "C:/r/a.py", "content": "x"},
                  "File has not been read yet. Read it first before writing to it."),
                 ("Edit", {"file_path": "C:/r/a.py", "old_string": "a", "new_string": "b"},
                  "File has not been read yet. Read it first before writing to it."),
                 ("Edit", {"file_path": "C:/r/a.py", "old_string": "a", "new_string": "b"},
                  "<tool_use_error>File has been modified since read, either by the user or by a linter. "
                  "Read it again before attempting to write it.</tool_use_error>"),
                 ("TaskStop", {"task_id": "7"}, "No task found with ID: 7")]
        for i, (name, inp, msg) in enumerate(cases):
            self.write([user("go"), tool(name, inp, "w1"), result("w1", msg, error=True),
                        tool(name, inp, "w2"), result("w2", [{"type": "text", "text": msg}], error=True)])
            n, _ = self.capture(session=f"pre{i}")
            self.assertEqual(n, 0, name)

    def test_lookalike_real_errors_are_still_retries(self):
        cases = [("Bash", "Docker daemon is not running"),
                 ("Bash", "migration was blocked by a database lock"),
                 ("Bash", "must read at least one record before committing"),
                 ("Bash", "File has not been read yet. Read it first before writing to it."),  # wrong tool
                 ("Bash", "File has been modified since read, either by the user or by a linter."),  # wrong tool
                 ("Write", "migration was blocked by a database lock")]            # right tool, wrong text
        for i, (name, msg) in enumerate(cases):
            inp = {"command": "run it"} if name == "Bash" else {"file_path": "C:/r/a.py", "content": "x"}
            self.write([user("go"), tool(name, inp, "w1"), result("w1", msg, error=True),
                        tool(name, inp, "w2"), result("w2", msg, error=True)])
            n, rows = self.capture(session=f"real{i}")
            self.assertEqual((n, rows[-1]["kind"]), (1, "retry"), msg)

    def test_real_double_failure_still_recorded(self):
        self.write([user("run"), tool("Bash", {"command": "make"}, "b1"), result("b1", "Exit code 2: boom", error=True),
                    tool("Bash", {"command": "make"}, "b2"), result("b2", "Exit code 2: boom", error=True)])
        _, rows = self.capture()
        self.assertEqual([(r["skill"], r["kind"]) for r in rows], [("(project)", "retry")])

    def test_retry_never_quotes_raw_tool_input(self):
        body = "line\n" * 50
        self.write([user("save"), tool("Write", {"file_path": "C:/r/app.py", "content": body}, "w1"),
                    result("w1", "denied", error=True),
                    tool("Write", {"file_path": "C:/r/app.py", "content": body}, "w2"), result("w2", "denied", error=True)])
        _, rows = self.capture()
        self.assertEqual(rows[0]["quote"], "Write app.py")

    def test_mistake_admission_is_attributed(self):
        self.write([user("fix"), *skill_call("codex-build"), say("I made a mistake in the merge.")])
        _, rows = self.capture(mistakes=["I made a mistake in the merge."])
        self.assertEqual([(r["skill"], r["kind"]) for r in rows], [("codex-build", "mistake")])
        self.assertIn("used this turn", rows[0]["evidence"])


class MistakeScan(unittest.TestCase):
    """P-457 A3: rows 36/38/42 of the 2026-11-11 hand-check are negatives, 0/18/19/28/29/45 positives."""

    def scan(self, text, role="assistant"):
        return ah._scan_mistakes([{"role": role, "text": text}])

    def test_real_admissions_still_found(self):
        for text in ("I was wrong about the restart: the site is still running the old server.",
                     "That was my mistake and I've corrected the pending file.",
                     "I was wrong to report it live; I passed on the coordinator's word without checking.",
                     "- **Correction:** I said earlier that X was old. That was wrong: I'd read an old copy."):
            self.assertEqual(len(self.scan(text)), 1, text)

    def test_row_42_abi_broke_is_not_i_broke(self):
        self.assertEqual(self.scan("Churn: Windows support is about three weeks old and the ABI broke recently."), [])
        self.assertEqual(len(self.scan("I broke the import in that commit.")), 1)

    def test_rows_36_38_pasted_relay_in_user_role(self):
        relay = "CLAUDE CAP CORRECTION" + chr(10) + "- I told you x. That was wrong: core/llm_budget.py has a cap."
        self.assertEqual(self.scan(relay, role="user"), [])
        self.assertEqual(self.scan("my bad, redo it", role="user"), [])

    def test_saved_excerpt_comes_from_original_text(self):
        text = 'That was my mistake: I wrote "keep the cap at 20" but it is 40.'
        found = self.scan(text)
        self.assertEqual(len(found), 1)
        self.assertIn('"keep the cap at 20"', found[0])

    def test_quoted_ledger_text_and_tables_are_not_admissions(self):
        quoted = 'The auto-draft "That was wrong: core/llm_budget." is false and should be removed.'
        self.assertEqual(self.scan(quoted), [])
        self.assertEqual(self.scan("> That was wrong: x" + chr(10) + "| a | I was wrong |"), [])


class MelAttribution(Base):
    """P-457 A5: an open .mel task credits mohit-engineering-loop even though no Skill call happened."""

    def mel_task(self, root, status, task="MEL-001"):
        d = Path(root) / ".mel" / task
        d.mkdir(parents=True)
        (d / "state.json").write_text(json.dumps({"status": status}), encoding="utf-8")

    def retry_turn(self, path="x.py"):
        inp = {"file_path": str(path), "content": "x"}
        return [user("run"), tool("Write", inp, "w1"), result("w1", "disk full", error=True),
                tool("Write", inp, "w2"), result("w2", "disk full", error=True)]

    def test_open_task_in_cwd_credits_mel(self):
        self.mel_task(self.repo, "implementing")
        self.write(self.retry_turn())
        _, rows = self.capture()
        self.assertEqual([(r["skill"], r["kind"]) for r in rows], [(ah.MEL_SKILL, "retry")])
        self.assertIn("inferred from an open .mel task", rows[0]["evidence"])

    def test_merged_task_does_not(self):
        self.mel_task(self.repo, "merged")
        self.write(self.retry_turn())
        _, rows = self.capture()
        self.assertEqual([r["skill"] for r in rows], ["(project)"])

    def test_no_mel_folder_does_not(self):
        self.write(self.retry_turn())
        self.assertEqual([r["skill"] for r in self.capture()[1]], ["(project)"])

    def test_path_touched_this_turn_in_another_checkout(self):
        other = Path(self.tmp.name) / "other-checkout"
        (other / "src").mkdir(parents=True)
        self.mel_task(other, "review_pending")
        self.write(self.retry_turn(other / "src" / "a.py"))
        self.assertEqual([r["skill"] for r in self.capture()[1]], [ah.MEL_SKILL])

    def test_stale_open_task_is_not_credited_fresh_is_(self):
        import time
        self.mel_task(self.repo, "review_pending")
        state = self.repo / ".mel" / "MEL-001" / "state.json"
        old = time.time() - 20 * 86400
        os.utime(state, (old, old))
        self.write(self.retry_turn())
        self.assertEqual([r["skill"] for r in self.capture()[1]], ["(project)"])
        now = time.time() - 2 * 86400
        os.utime(state, (now, now))
        self.write(self.retry_turn())
        self.assertEqual([r["skill"] for r in self.capture(session="s2")[1]][-1:], [ah.MEL_SKILL])

    def test_corrupt_state_file_is_ignored(self):
        d = self.repo / ".mel" / "MEL-002"
        d.mkdir(parents=True)
        (d / "state.json").write_text("{not json", encoding="utf-8")
        self.write(self.retry_turn())
        self.assertEqual([r["skill"] for r in self.capture()[1]], ["(project)"])


class CorrectionFamily(Base):
    """P-457 A1/H2: the 'it did not work' family, and what must NOT count as one."""

    def fires(self, reply, first_turn=False):
        self.tmp_n = getattr(self, "tmp_n", 0) + 1
        entries = [user(reply)] if first_turn else [user("go"), say("Done, fixed it."), user(reply)]
        self.write(entries)
        return self.capture(session=f"s{self.tmp_n}")[0]

    def test_real_complaints_are_corrections(self):
        for reply in ("it is not working", "whatever fix you have implemented is not working",
                      "it still shows the old one", "the page is still broken", "that didn't work",
                      "this doesn't fix it", "why bulk fetch didnt work?", "we are still showing filing freshness as Q2"):
            self.assertEqual(self.fires(reply), 1, reply)

    def test_replay_false_positive_shapes(self):
        # Hits from the 2026-11-11 replay that were not verdicts on a reply.
        for reply in ("dont work rt now...the app has to stay frozen until the demo", "behaviour doesn't change; I'll restart",
                      "i still wonder why scan was not working? have u try to dig that out",
                      "having multiple RS in scan formula doesnt help? can u test with this",
                      "Suggested task Fix ARGUS tests. Do not change ARGUS behaviour (bug row 678)."):
            self.assertEqual(self.fires(reply), 0, reply)

    def test_corrections_that_carry_a_follow_up_ask_or_a_when_clause(self):
        # Review MED1 (2026-11-11): these were dropped by the over-broad ask/when/once/row filters.
        for reply in ("no thats wrong, please fix it", "wrong file, could you revert",
                      "no i meant the other file, can you redo it", "thats not what i asked please redo",
                      "when i open the page it still shows the old one", "once i reload it is not working",
                      "row 1479 fix is still not working", "what you built is not working"):
            self.assertEqual(self.fires(reply), 1, reply)

    def test_first_message_has_no_reply_to_correct(self):
        self.assertEqual(self.fires("it is not working", first_turn=True), 0)

    def test_conditionals_are_instructions(self):
        for reply in ("if this doesn't fix it, revert", "If it still shows fewer than ten, tell me",
                      "if the key doesn't work use the other one"):
            self.assertEqual(self.fires(reply), 0, reply)

    def test_new_bug_reports_and_task_asks(self):
        for reply in ("can u check why autohandover is not working", "check why the scan is not working",
                      "please look into why it doesn't work", "highlight what is not working"):
            self.assertEqual(self.fires(reply), 0, reply)

    def test_pasted_relays_briefs_and_tables(self):
        nl = chr(10)
        relay = nl.join(["coordinator says:", "key probe: NOT working yet", "OTEX EPS still wrong live",
                         "next", "more", "end"])
        table = nl.join(["| name | state |", "| a | not working |"])
        for reply in (relay, table):
            self.assertEqual(self.fires(reply), 0, reply)

    def test_approvals_starting_with_no_are_not_corrections(self):
        for reply in ("no its fine", "no that's right", "no it's ok, go on"):
            self.assertEqual(self.fires(reply), 0, reply)
        self.assertEqual(self.fires("no that's wrong"), 1)


class NegativeControls(Base):
    def test_analysis_prose_and_quoted_errors_are_not_signals(self):
        # The 2026-09-08 failure family: a detector reading analysis prose as an event.
        entries = [user("why did the scan fail?"), *skill_call("debug"),
                   say("Root cause was a stale cache. This broke because the lock was held. That's incorrect "
                       "in the old docs, and the fix is in."),
                   tool("Bash", {"command": "cat err.log"}, "b1"),
                   result("b1", "ERROR: that's wrong. I said no. I made a mistake. stop it")]
        self.write(entries)
        msgs = ah._read_transcript(str(self.transcript))[2]
        mistakes = ah._scan_mistakes(msgs)   # exactly what main() would pass in
        self.assertEqual(mistakes, [])
        n, rows = self.capture(mistakes=mistakes)
        self.assertEqual((n, rows), (0, []))

    def test_skill_text_and_stop_feedback_are_not_user_corrections(self):
        self.write([user("go"), *skill_call("x"),
                    meta("Stop hook feedback:\nBefore finishing, that's wrong, I said no")])
        n, _ = self.capture()
        self.assertEqual(n, 0)
        # Skill text alone (no hook phrase, so only the isMeta guard stops it).
        self.write([user("go"), *skill_call("x"), say("ok"),
                    meta("Base directory for this skill: C:/s/y\n\nNo, that's wrong: you missed the step.")])
        n, _ = self.capture(session="s2")
        self.assertEqual(n, 0)

    def test_pasted_and_injected_text_is_ignored(self):
        self.write([user("go"), *skill_call("x"), say("done"),
                    user("<pasted_content id=1>no i meant that's wrong</pasted_content>\nplease continue"),
                    ])
        n, _ = self.capture()
        self.assertEqual(n, 0)
        self.write([user("go"), *skill_call("x"), say("done"),
                    user("<system-reminder>that's wrong</system-reminder>please continue")])
        n, _ = self.capture(session="s2")
        self.assertEqual(n, 0)

    def test_skill_less_turn_without_signal_writes_nothing(self):
        self.write([user("hello"), say("hi"), user("thanks, carry on")])
        n, rows = self.capture()
        self.assertEqual((n, rows), (0, []))
        self.assertFalse((self.vault / "projects" / "my-repo" / "skill-observations.jsonl").exists())

    def test_skill_less_turn_with_real_signal_records_project(self):
        self.write([user("hello"), say("hi"), user("you missed the second file")])
        _, rows = self.capture()
        self.assertEqual([(r["skill"], r["kind"]) for r in rows], [("(project)", "correction")])


class Safety(Base):
    def test_secret_like_quotes_are_dropped_but_signal_kept(self):
        for i, reply in enumerate(("no i meant use api_key=abc123 instead", "that's wrong, token: hunter2",
                                   "that's wrong, use ghp_aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1vW3xY")):
            self.write([user("go"), *skill_call("x"), say("done"), user(reply)])
            _, rows = self.capture(session=f"s{i}")
            self.assertEqual(rows[-1]["quote"], "", reply)
        self.assertNotIn("hunter2", json.dumps(self.rows()))

    def test_quote_is_at_most_15_words(self):
        long = "that's wrong " + " ".join(f"w{i}" for i in range(40))
        self.write([user("go"), *skill_call("x"), say("done"), user(long)])
        _, rows = self.capture()
        self.assertLessEqual(len(rows[0]["quote"].split()), 15)

    def test_dedup_per_session_turn_kind_skill(self):
        self.write([user("go"), *skill_call("x"), say("done"), user("that's wrong")])
        self.capture()
        n, rows = self.capture()          # second Stop on the same turn
        self.assertEqual((n, len(rows)), (0, 1))
        n, rows = self.capture(session="other")
        self.assertEqual((n, len(rows)), (1, 2))

    def test_failure_is_logged_never_raised(self):
        n = ah._capture_skill_observations("s", str(self.repo), str(Path(self.tmp.name) / "missing.jsonl"), [])
        self.assertEqual(n, 0)
        self.assertIn("_capture_skill_observations", ah.ERROR_LOG.read_text(encoding="utf-8"))


class HookEndToEnd(Base):
    def test_stop_hook_writes_observation_and_exits_zero(self):
        self.write([user("go"), *skill_call("codex-build"), say("done"), user("that's wrong, redo it")])
        payload = json.dumps({"session_id": "e2e-session", "cwd": str(self.repo),
                              "transcript_path": str(self.transcript)})
        env = {**os.environ, "TEA_MEMORY_DIR": str(self.vault)}
        proc = subprocess.run([sys.executable, str(HOOKS / "auto_handover.py")], input=payload,
                              capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([(r["skill"], r["kind"]) for r in self.rows()], [("codex-build", "correction")])


if __name__ == "__main__":
    unittest.main()
