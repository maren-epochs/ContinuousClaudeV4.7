"""Tests for tools/fleet/lessons.py (lesson proposals from memories and bloks cards).

Run from the repo root:  py -3.13 -m pytest -q tools/fleet
Every test runs under a fixture HOME/USERPROFILE tree; the real ~/.claude is never
touched and bloks is never spawned (runner injected or subprocess mocked).
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.fleet import lessons, model

GENERIC_FEEDBACK = (
    "Ask for decisions with lettered options A to D and mark the recommended "
    "option first, with a one line reason for the recommendation."
)
GENERIC_REFERENCE = (
    "The pricing calculator for cloud storage tiers lives on the vendor status "
    "page under the billing section, updated every quarter."
)


def memory_text(name, mtype, body, description="", scope=None):
    lines = ["---", f"name: {name}", f'description: "{description or name}"']
    lines += ["metadata:", "  node_type: memory", f"  type: {mtype}"]
    if scope:
        lines.append(f"  scope: {scope}")
    lines += ["  originSessionId: sess-a1", "---", "", body, ""]
    return "\n".join(lines)


def no_bloks(home):
    return None


class LessonsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name).resolve()
        patcher = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "USERPROFILE": str(self.home),
                "CCV_PRIVACY_TERMS": "zebracorp",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # The clone's own .git/info/privacy-terms is real data; keep fixtures isolated.
        no_clone_terms = mock.patch.object(
            lessons.privacy_guard, "default_terms_file", return_value=None
        )
        self.cwd_terms = no_clone_terms.start()
        self.addCleanup(no_clone_terms.stop)
        no_own_repo = mock.patch.object(lessons, "HARNESS_DIR", None)
        no_own_repo.start()
        self.addCleanup(no_own_repo.stop)
        self.claude = self.home / ".claude"
        self.claude.mkdir()
        (self.claude / "CLAUDE.md").write_text(
            "# Personal preferences\n\n- Be concise and direct.\n", encoding="utf-8"
        )
        self.repo = self.home / "work" / "project-A"
        (self.repo / "tools").mkdir(parents=True)
        (self.repo / "tools" / "build_index.py").write_text("", encoding="utf-8")

    def project(self, slug="w--work-project-A", cwd=None):
        pdir = self.claude / "projects" / slug
        (pdir / "memory").mkdir(parents=True, exist_ok=True)
        if cwd is not None:
            record = {"type": "user", "cwd": str(cwd), "sessionId": "sess-a1"}
            (pdir / "sess-a1.jsonl").write_text(
                '{"type":"mode"}\n' + json.dumps(record) + "\n", encoding="utf-8"
            )
        return pdir

    def memory(self, pdir, fname, text):
        path = pdir / "memory" / fname
        path.write_text(text, encoding="utf-8")
        return path

    def run_lessons(self, **kw):
        kw.setdefault("run_bloks", no_bloks)
        return lessons.propose_lessons(self.home, **kw)

    def inbox(self):
        return model.list_proposals(self.claude / "harness-inbox")


class ProjectCwdProbe(LessonsTestCase):
    def write(self, pdir, first_line):
        record = {"type": "user", "cwd": str(self.repo), "sessionId": "sess-a1"}
        (pdir / "sess-a1.jsonl").write_text(
            first_line + "\n" + json.dumps(record) + "\n", encoding="utf-8"
        )

    def test_cwd_found_within_the_byte_budget(self):
        pdir = self.project()
        self.write(pdir, '{"type":"mode"}')
        self.assertEqual(lessons.project_cwd(pdir), str(self.repo))

    def test_reads_stop_at_the_byte_budget(self):
        pdir = self.project()
        self.write(pdir, json.dumps({"type": "summary", "pad": "x" * 500}))
        with mock.patch.object(lessons, "TRANSCRIPT_PROBE_BYTES", 300):
            self.assertIsNone(lessons.project_cwd(pdir))
        self.assertEqual(lessons.project_cwd(pdir), str(self.repo))

    def test_one_huge_line_is_not_read_whole(self):
        pdir = self.project()
        self.write(pdir, "x" * (lessons.TRANSCRIPT_PROBE_BYTES * 4))
        real_open = Path.open
        reads = []

        def counting_open(path, *args, **kwargs):
            fh = real_open(path, *args, **kwargs)
            real_readline = fh.readline

            def readline(size=-1):
                line = real_readline(size)
                reads.append(len(line))
                return line

            fh.readline = readline
            return fh

        with mock.patch.object(Path, "open", counting_open):
            self.assertIsNone(lessons.project_cwd(pdir))
        self.assertLessEqual(sum(reads), lessons.TRANSCRIPT_PROBE_BYTES)


class MemoryScan(LessonsTestCase):
    def test_feedback_and_reference_memories_become_lesson_proposals(self):
        pdir = self.project(cwd=self.repo)
        self.memory(pdir, "ask.md", memory_text("ask", "feedback", GENERIC_FEEDBACK))
        self.memory(pdir, "ref.md", memory_text("ref", "reference", GENERIC_REFERENCE))
        out = self.run_lessons()
        self.assertEqual(len(out), 2)
        stored = {p.id: p for p in self.inbox()}
        self.assertEqual(set(stored), {p.id for p in out})
        for p in out:
            self.assertEqual(p.kind, "lesson")
            self.assertEqual(p.status, "pending")
            self.assertEqual(p.change.tool, "lesson")
            self.assertTrue(model.is_safe_proposal_id(p.id))
            self.assertRegex(p.extra["content_hash"], r"^[0-9a-f]{64}$")
            self.assertEqual(
                stored[p.id].extra["content_hash"], p.extra["content_hash"]
            )
            self.assertEqual(p.source.project, "project-A")
            self.assertEqual(p.target.installed_path, "CLAUDE.md")
            self.assertTrue(p.reason)
        contents = sorted(p.change.content or "" for p in out)
        self.assertIn(GENERIC_FEEDBACK, contents[0] + contents[1])
        self.assertIn(GENERIC_REFERENCE, contents[0] + contents[1])

    def test_other_memory_types_index_and_no_frontmatter_are_skipped(self):
        pdir = self.project()
        self.memory(pdir, "u.md", memory_text("u", "user", GENERIC_FEEDBACK))
        self.memory(pdir, "p.md", memory_text("p", "project", GENERIC_FEEDBACK))
        self.memory(pdir, "MEMORY.md", "- [ask](ask.md) - " + GENERIC_FEEDBACK + "\n")
        self.memory(pdir, "plain.md", GENERIC_FEEDBACK + "\n")
        self.assertEqual(self.run_lessons(), [])
        self.assertEqual(self.inbox(), [])

    def test_top_level_type_key_is_accepted(self):
        pdir = self.project()
        text = f"---\nname: ask\ntype: feedback\n---\n\n{GENERIC_FEEDBACK}\n"
        self.memory(pdir, "ask.md", text)
        self.assertEqual(len(self.run_lessons()), 1)

    def test_missing_claude_dir_yields_nothing(self):
        empty = self.home / "empty-home"
        empty.mkdir()
        self.assertEqual(lessons.propose_lessons(empty, run_bloks=no_bloks), [])

    def test_dry_run_writes_nothing(self):
        pdir = self.project()
        self.memory(pdir, "ask.md", memory_text("ask", "feedback", GENERIC_FEEDBACK))
        out = self.run_lessons(dry_run=True)
        self.assertEqual(len(out), 1)
        self.assertFalse((self.claude / "harness-inbox").exists())


class ProjectSpecific(LessonsTestCase):
    def test_memory_naming_its_project_is_skipped(self):
        pdir = self.project(cwd=self.repo)
        body = "In project-A the release script must run before tagging a build."
        self.memory(pdir, "rel.md", memory_text("rel", "feedback", body))
        self.assertEqual(self.run_lessons(), [])

    def test_memory_referencing_a_repo_file_is_skipped(self):
        pdir = self.project(cwd=self.repo)
        body = "Regenerate the search index with `tools/build_index.py` after edits."
        self.memory(pdir, "idx.md", memory_text("idx", "feedback", body))
        self.assertEqual(self.run_lessons(), [])

    def test_scope_metadata_overrides_heuristics(self):
        pdir = self.project(cwd=self.repo)
        self.memory(
            pdir,
            "a.md",
            memory_text("a", "feedback", GENERIC_FEEDBACK, scope="project"),
        )
        named = "Keep project-A style: lettered options A to D in every question."
        self.memory(pdir, "b.md", memory_text("b", "feedback", named, scope="global"))
        out = self.run_lessons()
        self.assertEqual(len(out), 1)
        self.assertIn("lettered options", out[0].change.content or "")


class AlreadyPresent(LessonsTestCase):
    def test_lesson_already_in_global_claude_md_is_skipped(self):
        (self.claude / "CLAUDE.md").write_text(
            "# Prefs\n\n- **Ask for decisions** with lettered options A to D, and "
            "always mark the recommended\n  option first with a one-line reason for "
            "the recommendation.\n",
            encoding="utf-8",
        )
        pdir = self.project()
        self.memory(pdir, "ask.md", memory_text("ask", "feedback", GENERIC_FEEDBACK))
        self.assertEqual(self.run_lessons(), [])

    def test_lesson_already_in_installed_skill_doc_is_skipped(self):
        skill = self.claude / "skills" / "review" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# Review\n\n" + GENERIC_REFERENCE + "\n", encoding="utf-8")
        pdir = self.project()
        self.memory(pdir, "ref.md", memory_text("ref", "reference", GENERIC_REFERENCE))
        self.assertEqual(self.run_lessons(), [])

    def test_extra_docs_count_as_harness_docs(self):
        doc = self.home / "repo-CLAUDE.md"
        doc.write_text(GENERIC_FEEDBACK + "\n", encoding="utf-8")
        pdir = self.project()
        self.memory(pdir, "ask.md", memory_text("ask", "feedback", GENERIC_FEEDBACK))
        self.assertEqual(self.run_lessons(docs=[doc]), [])


class Dedupe(LessonsTestCase):
    def test_second_run_does_not_repropose(self):
        pdir = self.project()
        self.memory(pdir, "ask.md", memory_text("ask", "feedback", GENERIC_FEEDBACK))
        self.assertEqual(len(self.run_lessons()), 1)
        self.assertEqual(self.run_lessons(), [])
        self.assertEqual(len(self.inbox()), 1)

    def test_same_lesson_in_two_projects_is_proposed_once(self):
        for slug in ("w--work-project-A", "w--work-project-B"):
            pdir = self.project(slug=slug)
            text = memory_text("ask", "feedback", GENERIC_FEEDBACK)
            self.memory(pdir, "ask.md", text)
        self.assertEqual(len(self.run_lessons()), 1)

    def test_whitespace_and_case_changes_hash_the_same(self):
        pdir = self.project()
        self.memory(pdir, "ask.md", memory_text("ask", "feedback", GENERIC_FEEDBACK))
        self.assertEqual(len(self.run_lessons()), 1)
        reflowed = GENERIC_FEEDBACK.upper().replace(" ", "\n  ", 3)
        self.memory(pdir, "ask.md", memory_text("ask", "feedback", reflowed))
        self.assertEqual(self.run_lessons(), [])

    def test_rejected_proposal_is_never_reproposed(self):
        pdir = self.project()
        self.memory(pdir, "ask.md", memory_text("ask", "feedback", GENERIC_FEEDBACK))
        (first,) = self.run_lessons()
        first.status = "rejected"
        model.save_proposal(first)
        self.assertEqual(self.run_lessons(), [])

    def test_rejected_proposal_moved_to_a_subdir_still_blocks(self):
        pdir = self.project()
        self.memory(pdir, "ask.md", memory_text("ask", "feedback", GENERIC_FEEDBACK))
        (first,) = self.run_lessons()
        inbox = self.claude / "harness-inbox"
        archive = inbox / "rejected"
        archive.mkdir()
        first.status = "rejected"
        (archive / f"{first.id}.json").write_text(first.to_json(), encoding="utf-8")
        (inbox / f"{first.id}.json").unlink()
        self.assertEqual(self.run_lessons(), [])

    def test_hash_in_change_extra_also_counts(self):
        pdir = self.project()
        self.memory(pdir, "ask.md", memory_text("ask", "feedback", GENERIC_FEEDBACK))
        (first,) = self.run_lessons(dry_run=True)
        legacy = model.Proposal(
            id=model.new_proposal_id(),
            kind="lesson",
            status="rejected",
            change=model.Change(
                tool="lesson", extra={"content_hash": first.extra["content_hash"]}
            ),
        )
        model.save_proposal(legacy)
        self.assertEqual(self.run_lessons(), [])


class Masking(LessonsTestCase):
    def test_private_terms_paths_and_ids_are_masked(self):
        drive = "C:"
        user_path = f"{drive}/Users/fakeperson/notes"
        uuid = "11111111-aaaa-bbbb-cccc-222222222222"
        body = (
            f"Keep zebracorp exports out of {user_path} and never paste "
            f"session {uuid} into issues filed for the vendor."
        )
        slug = f"{drive[0]}--Users-fakeperson-work"
        pdir = self.project(slug=slug)
        text = memory_text("m", "feedback", body, description="Mask ZebraCorp data")
        self.memory(pdir, "m.md", text)
        (p,) = self.run_lessons()
        raw = (self.claude / "harness-inbox" / f"{p.id}.json").read_text("utf-8")
        for secret in ("zebracorp", "fakeperson", uuid):
            self.assertNotIn(secret, raw.lower())
        self.assertIn(lessons.MASK, p.change.content or "")
        self.assertIn(lessons.MASK, p.reason or "")
        self.assertIn("never paste", p.change.content or "")

    def test_user_level_privacy_terms_file_is_used(self):
        with mock.patch.dict(os.environ, {"CCV_PRIVACY_TERMS": ""}):
            (self.claude / "privacy-terms").write_text("quokkaworks\n", "utf-8")
            pdir = self.project()
            body = GENERIC_FEEDBACK + " Applies to quokkaworks reviews."
            self.memory(pdir, "m.md", memory_text("m", "feedback", body))
            (p,) = self.run_lessons()
        self.assertNotIn("quokkaworks", (p.change.content or "").lower())

    def git_repo(self, name, term):
        repo = self.home / "clones" / name
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=30)
        terms = repo / ".git" / "info" / "privacy-terms"
        terms.parent.mkdir(parents=True, exist_ok=True)
        terms.write_text(f"{term}\n", encoding="utf-8")
        return repo, terms

    def test_clone_terms_come_from_the_manifest_repo_not_cwd(self):
        repo, terms = self.git_repo("harness", "quokkacorp")
        model.manifest_path().write_text(
            json.dumps({"repo": str(repo)}), encoding="utf-8"
        )
        found = lessons.clone_terms_file(self.claude)
        self.assertEqual(found.resolve(), terms.resolve())
        guard = lessons.default_guard(self.home)
        self.assertEqual(lessons.mask("ship quokkacorp", guard), f"ship {lessons.MASK}")
        self.cwd_terms.assert_not_called()

    def test_clone_terms_fall_back_to_the_harness_dir(self):
        repo, terms = self.git_repo("installed-from", "quokkacorp")
        with mock.patch.object(lessons, "HARNESS_DIR", repo):
            found = lessons.clone_terms_file(self.claude)
        self.assertEqual(found.resolve(), terms.resolve())

    def test_clone_terms_none_without_manifest_or_repo(self):
        self.assertIsNone(lessons.clone_terms_file(self.claude))
        model.manifest_path().write_text(
            json.dumps({"repo": str(self.home / "nowhere")}), encoding="utf-8"
        )
        self.assertIsNone(lessons.clone_terms_file(self.claude))
        model.manifest_path().write_text("not json", encoding="utf-8")
        self.assertIsNone(lessons.clone_terms_file(self.claude))
        self.cwd_terms.assert_not_called()

    def test_mask_uses_privacy_guard(self):
        guard = lessons.privacy_guard.Guard([], [("Wombat", "t")], [])
        self.assertEqual(
            lessons.mask("a wombat b WOMBAT", guard),
            f"a {lessons.MASK} b {lessons.MASK}",
        )


BLOKS_JSON = {
    "project": "someone",
    "rules": [
        {
            "kind": "rule",
            "title": "Run bloks from Bash",
            "body": "bloks under PowerShell reads a different card store; run it "
            "from Bash so every agent sees the same cards.\n",
            "tags": ["bloks"],
        }
    ],
    "tastes": [
        {
            "kind": "taste",
            "title": "Prefer small commits",
            "body": "Prefer one logical change per commit with a conventional "
            "subject line and a short body.\n",
            "tags": [],
        }
    ],
    "project_cards": [
        {
            "kind": "rule",
            "title": "Local only",
            "body": "This repository deploys from the release branch every Friday.",
            "tags": [],
        }
    ],
}


class Bloks(LessonsTestCase):
    def test_rules_and_tastes_are_proposed_project_cards_are_not(self):
        calls = []

        def runner(home):
            calls.append(home)
            return json.dumps(BLOKS_JSON)

        out = self.run_lessons(run_bloks=runner)
        self.assertEqual(calls, [self.home])
        contents = " ".join(p.change.content or "" for p in out)
        self.assertEqual(len(out), 2)
        self.assertIn("different card store", contents)
        self.assertIn("one logical change", contents)
        self.assertNotIn("Friday", contents)
        self.assertEqual({p.source.project for p in out}, {"bloks"})
        self.assertEqual({p.extra["origin"] for p in out}, {"bloks"})

    def test_bloks_absent_or_broken_is_skipped_and_memories_still_scan(self):
        pdir = self.project()
        self.memory(pdir, "ask.md", memory_text("ask", "feedback", GENERIC_FEEDBACK))
        self.assertEqual(len(self.run_lessons(run_bloks=lambda h: "not json")), 1)
        self.assertEqual(self.run_lessons(run_bloks=lambda h: "[1, 2]"), [])

    def test_default_runner_without_bloks_on_path_returns_none(self):
        with mock.patch.object(lessons.shutil, "which", return_value=None):
            self.assertIsNone(lessons.bloks_context(self.home))

    def test_default_runner_failures_return_none(self):
        failures = [
            OSError("spawn failed"),
            subprocess.TimeoutExpired("bloks", 30),
            subprocess.CompletedProcess(["bloks"], 1, stdout="", stderr="boom"),
        ]
        with mock.patch.object(lessons.shutil, "which", return_value="bloks"):
            for failure in failures:
                kw = {"side_effect": failure}
                if isinstance(failure, subprocess.CompletedProcess):
                    kw = {"return_value": failure}
                with mock.patch.object(lessons.subprocess, "run", **kw):
                    self.assertIsNone(lessons.bloks_context(self.home))

    def test_default_runner_invokes_bloks_context_json(self):
        done = subprocess.CompletedProcess(["bloks"], 0, stdout='{"rules": []}')
        with (
            mock.patch.object(lessons.shutil, "which", return_value="bloks"),
            mock.patch.object(lessons.subprocess, "run", return_value=done) as run,
        ):
            self.assertEqual(lessons.bloks_context(self.home), '{"rules": []}')
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["bloks", "context"])
        self.assertIn("--format", argv)
        self.assertIn("json", argv)

    def test_default_runner_is_used_when_not_injected(self):
        with mock.patch.object(lessons, "bloks_context", return_value=None) as bc:
            lessons.propose_lessons(self.home)
        bc.assert_called_once_with(self.home)


if __name__ == "__main__":
    unittest.main()
