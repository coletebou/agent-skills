"""--snapshot reviews a private detached worktree of the frozen commit."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from .test_autoreview_hardening import (
    SCRIPT, fake_trufflehog_script, git, init_repo, load_helper, write_executable,
)


CLEAN_REPORT = {
    "findings": [], "overall_correctness": "patch is correct",
    "overall_explanation": "Synthetic snapshot review.", "overall_confidence": 0.99,
}
CLEAN_REPLY = json.dumps({**CLEAN_REPORT, "review_completion": "complete"})


class SnapshotModeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="autoreview-snapshot.")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "operator"
        self.home.mkdir()
        self.state = self.root / "state"
        env = {key: os.environ[key] for key in (
            "PATH", "PATHEXT", "SYSTEMROOT", "SystemRoot", "COMSPEC", "WINDIR",
            "TEMP", "TMP", "TMPDIR", "DEVELOPER_DIR",
        ) if key in os.environ}
        env.update({
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "AUTOREVIEW_RUN_LOG": "0",
        })
        self.env = env
        environment = mock.patch.dict(os.environ, env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.repo = init_repo(self.root)
        git(self.repo, "config", "core.autocrlf", "false")
        git(self.repo, "config", "commit.gpgsign", "false")
        (self.repo / "calc.py").write_bytes(b"def add(a, b):\n    return a + b\n")
        (self.repo / "notes.md").write_bytes(b"Review the division helper.\n")
        (self.repo / "data.json").write_bytes(b'{"cases": [1, 2]}\n')
        (self.repo / ".gitignore").write_bytes(b"ignored.md\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "base")
        git(self.repo, "branch", "-M", "main")
        self.base = git(self.repo, "rev-parse", "HEAD").strip()
        git(self.repo, "switch", "-q", "-c", "feature")
        (self.repo / "calc.py").write_bytes(
            b"def add(a, b):\n    return a + b\n\n\ndef div(a, b):\n    return a / b\n"
        )
        git(self.repo, "commit", "-qam", "add div")
        self.head = git(self.repo, "rev-parse", "HEAD").strip()
        self.helper = load_helper()
        self.observed: list[dict[str, object]] = []
        previous_cwd = Path.cwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, previous_cwd)

    # Harness ---------------------------------------------------------------

    def recording_engine(self, side_effect=None):
        def engine(_args, repo, prompt):
            repo = Path(repo)
            self.observed.append({
                "repo": repo,
                "prompt": prompt,
                "head": git(repo, "rev-parse", "HEAD").strip(),
                "branch": git(repo, "branch", "--show-current").strip(),
                "worktrees": git(self.repo, "worktree", "list", "--porcelain"),
                "mode": stat.S_IMODE(repo.stat().st_mode),
                "parent_mode": stat.S_IMODE(repo.parent.stat().st_mode),
                "parent_entries": sorted(path.name for path in repo.parent.iterdir()),
                "hooks_entries": sorted(path.name for path in (repo.parent / "no-hooks").iterdir())
                if (repo.parent / "no-hooks").is_dir() else None,
            })
            if side_effect is not None:
                side_effect(repo)
            return CLEAN_REPLY

        return engine

    def invoke(self, *argv, engine=None, patches=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        main = self.helper["main_impl"]
        replacements = {
            "run_engine": engine or self.recording_engine(),
            # The pre-send scanner is covered elsewhere and by the CLI tests below.
            "scan_outgoing_review_pack": lambda *_args: None,
            "resolve_engine_binary": lambda *_args: (True, None),
        }
        replacements.update(patches or {})
        with mock.patch.dict(main.__globals__, replacements), \
                mock.patch.object(sys, "argv", [str(SCRIPT), "--engine", "codex", *argv]), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                return main()
            finally:
                self.stdout, self.stderr = stdout.getvalue(), stderr.getvalue()

    def snapshot_args(self, *extra):
        return ("--snapshot", "--run-log-dir", str(self.state), *extra)

    def worktree_paths(self):
        listed = git(self.repo, "worktree", "list", "--porcelain")
        return [Path(line.removeprefix("worktree ")).resolve()
                for line in listed.splitlines() if line.startswith("worktree ")]

    def assert_no_snapshot_left(self):
        self.assertEqual(self.worktree_paths(), [self.repo.resolve()])
        snapshots = self.state / "snapshots"
        self.assertEqual(list(snapshots.iterdir()) if snapshots.exists() else [], [])
        self.assertEqual(self.helper["_SNAPSHOT_ORIGINS"], {})

    def snapshot_line(self):
        lines = [line for line in self.stdout.splitlines() if line.startswith("snapshot: ")]
        self.assertEqual(len(lines), 1, self.stdout)
        return Path(lines[0].removeprefix("snapshot: "))

    # Parsing ---------------------------------------------------------------

    def parse(self, *argv, env=None):
        with mock.patch.dict(os.environ, env or {}), \
                mock.patch.object(sys, "argv", [str(SCRIPT), *argv]):
            return self.helper["parse_args"]()

    def test_flag_and_environment_parsing(self):
        self.assertIs(self.parse().snapshot, False)
        self.assertIs(self.parse("--snapshot").snapshot, True)
        self.assertIs(self.parse("--no-snapshot").snapshot, False)
        for value in ("1", "true", "YES", " on "):
            with self.subTest(value=value):
                self.assertIs(self.parse(env={"AUTOREVIEW_SNAPSHOT": value}).snapshot, True)
        for value in ("0", "false", "", "Off"):
            with self.subTest(value=value):
                self.assertIs(self.parse(env={"AUTOREVIEW_SNAPSHOT": value}).snapshot, False)
        self.assertIs(self.parse("--no-snapshot", env={"AUTOREVIEW_SNAPSHOT": "1"}).snapshot, False)
        self.assertIs(self.parse("--snapshot", env={"AUTOREVIEW_SNAPSHOT": "0"}).snapshot, True)
        # An explicit choice wins over an unusable default, as with --log-bundle.
        self.assertIs(self.parse("--no-snapshot", env={"AUTOREVIEW_SNAPSHOT": "maybe"}).snapshot, False)
        with self.assertRaisesRegex(SystemExit, "invalid AUTOREVIEW_SNAPSHOT value"):
            self.parse(env={"AUTOREVIEW_SNAPSHOT": "maybe"})
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            self.parse("--snapshot", "--no-snapshot")
        self.assertEqual(caught.exception.code, 2)
        help_text = io.StringIO()
        with contextlib.redirect_stdout(help_text), self.assertRaises(SystemExit) as caught:
            self.parse("--help", env={"AUTOREVIEW_SNAPSHOT": "maybe"})
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("--snapshot", help_text.getvalue())
        self.assertIn("AUTOREVIEW_SNAPSHOT=1", help_text.getvalue())

    # Creation and cleanup --------------------------------------------------

    def test_branch_snapshot_is_private_detached_frozen_head_and_removed(self):
        self.assertEqual(self.invoke("--mode", "branch", "--base", "main", *self.snapshot_args()), 0)
        self.assertEqual(len(self.observed), 1)
        seen = self.observed[0]
        snapshot = seen["repo"]
        self.assertEqual(snapshot, self.snapshot_line().resolve())
        self.assertIn(f"snapshot_commit: {self.head}", self.stdout)
        self.assertNotEqual(snapshot, self.repo.resolve())
        self.assertFalse(snapshot.is_relative_to(self.repo.resolve()))
        self.assertTrue(snapshot.is_relative_to((self.state / "snapshots").resolve()))
        self.assertEqual(seen["head"], self.head)
        self.assertEqual(seen["branch"], "")
        self.assertIn(f"worktree {snapshot}", seen["worktrees"])
        # The checkout and the empty hooks path use fixed, distinct names.
        self.assertEqual(snapshot.name, "checkout")
        self.assertEqual(seen["parent_entries"], ["checkout", "no-hooks"])
        self.assertEqual(seen["hooks_entries"], [])
        self.assertIn("detached", seen["worktrees"])
        if os.name != "nt":
            self.assertEqual(seen["mode"], 0o700)
            self.assertEqual(seen["parent_mode"], 0o700)
        # The prompt keeps the operator branch label and the frozen branch diff.
        self.assertIn("Current branch: feature", seen["prompt"])
        self.assertIn("Review target: branch main", seen["prompt"])
        self.assertIn("+def div(a, b):", seen["prompt"])
        self.assertFalse(snapshot.exists())
        self.assertIn("snapshot: removed", self.stderr)
        self.assert_no_snapshot_left()

    def test_commit_snapshot_uses_the_commit_ref_and_pins_worktree_relative_refs(self):
        git(self.repo, "commit", "--allow-empty", "-qm", "later commit")
        for ref, label in (("HEAD~1", self.head), (self.head, self.head)):
            with self.subTest(ref=ref):
                self.observed.clear()
                self.assertEqual(self.invoke("--mode", "commit", "--commit", ref, *self.snapshot_args()), 0)
                seen = self.observed[0]
                self.assertEqual(seen["head"], self.head)
                self.assertIn(f"snapshot_commit: {self.head}", self.stdout)
                self.assertIn(f"ref: {ref}", self.stdout)
                # HEAD~1 would name a different commit inside the snapshot.
                self.assertIn(f"Review target: commit {label}", seen["prompt"])
                self.assertIn(f"commit: {self.head}", seen["prompt"])
                self.assertIn("+def div(a, b):", seen["prompt"])
                self.assert_no_snapshot_left()

    def test_worktree_relative_base_keeps_the_operator_resolution(self):
        # @{-1} is per-worktree reflog state: main here, unknown in a fresh snapshot.
        self.assertEqual(self.invoke("--mode", "branch", "--base", "@{-1}", *self.snapshot_args()), 0)
        prompt = self.observed[0]["prompt"]
        self.assertIn(f"Review target: branch {self.base}", prompt)
        self.assertIn(f"base: {self.base}", prompt)
        self.assertIn("+def div(a, b):", prompt)
        self.assertIn("ref: @{-1}", self.stdout)
        self.assert_no_snapshot_left()

    def test_relative_run_log_root_still_yields_absolute_private_snapshot(self):
        relative = os.path.relpath(self.state, self.repo)
        self.assertEqual(self.invoke("--mode", "branch", "--base", "main", "--snapshot",
                                     "--run-log-dir", relative), 0, self.stderr)
        snapshot = self.observed[0]["repo"]
        self.assertTrue(snapshot.is_absolute())
        self.assertTrue(snapshot.is_relative_to((self.state / "snapshots").resolve()))
        self.assertFalse(snapshot.is_relative_to(self.repo.resolve()))
        self.assert_no_snapshot_left()

    def test_dry_run_reaches_plan_and_engine_check_then_removes_snapshot(self):
        self.assertEqual(
            self.invoke("--mode", "branch", "--base", self.base, "--dry-run", *self.snapshot_args()), 0,
        )
        for expected in ("snapshot: ", f"snapshot_commit: {self.head}", "inputs: OK",
                         "bundle: constructible", "review plan: 1 passes;", "prompt: OK",
                         "engine check: codex"):
            self.assertIn(expected, self.stdout)
        self.assertFalse(self.snapshot_line().exists())
        self.assert_no_snapshot_left()

    def test_operator_commits_edits_and_scratch_files_do_not_abort_the_review(self):
        report = self.root / "report.json"

        def keep_working(_repo):
            (self.repo / "scratch.txt").write_text("untracked scratch\n", encoding="utf-8")
            (self.repo / "calc.py").write_text("def add(a, b):\n    return b + a\n", encoding="utf-8")
            git(self.repo, "commit", "-qam", "operator commit during review")
            (self.repo / "notes.md").write_text("edited after commit\n", encoding="utf-8")

        self.assertEqual(self.invoke(
            "--mode", "branch", "--base", "main", "--json-output", str(report),
            *self.snapshot_args(), engine=self.recording_engine(keep_working),
        ), 0, self.stderr)
        published = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(published["review_status"], "scoped-clean")
        self.assertNotIn("source changed", self.stderr)
        self.assert_no_snapshot_left()

        # Control: the same work in a direct review still aborts after the reviewer.
        git(self.repo, "reset", "-q", "--hard", self.head)
        (self.repo / "scratch.txt").unlink()
        report.unlink()
        self.assertEqual(self.invoke(
            "--mode", "branch", "--base", "main", "--json-output", str(report),
            engine=self.recording_engine(keep_working),
        ), 1)
        self.assertIn("source changed after the review bundle was created", self.stderr)
        self.assertFalse(report.exists())

    def test_cleanup_on_reviewer_failure_interrupt_and_preparation_failure(self):
        failure = self.helper["ReviewerUnavailable"]("synthetic reviewer failure", reason="engine_failed")
        interrupt = self.helper["EngineInterrupted"](130)
        for name, raised in (("reviewer", failure), ("interrupt", interrupt)):
            with self.subTest(failure=name):
                def failing_engine(_args, repo, _prompt, raised=raised):
                    self.observed.append({"repo": Path(repo)})
                    raise raised

                with self.assertRaises(type(raised)):
                    self.invoke("--mode", "branch", "--base", "main", *self.snapshot_args(),
                                engine=failing_engine)
                self.assertTrue(self.observed.pop()["repo"].is_relative_to(self.state.resolve()))
                self.assert_no_snapshot_left()
        # The bundle reports an unknown base after the snapshot exists.
        with self.assertRaisesRegex(SystemExit, "unknown base ref: missing-base"):
            self.invoke("--mode", "branch", "--base", "missing-base", *self.snapshot_args())
        self.assertIn("snapshot: ", self.stdout)
        self.assert_no_snapshot_left()

    def test_failed_snapshot_creation_leaves_nothing_behind(self):
        original = self.helper["snapshot_git"]

        def failing_add(repo, env, *args):
            if "add" in args:
                return subprocess.CompletedProcess(args, 128, "", "fatal: synthetic add failure")
            return original(repo, env, *args)

        with self.assertRaisesRegex(SystemExit, "could not create the review snapshot") as caught:
            self.invoke("--mode", "branch", "--base", "main", *self.snapshot_args(),
                        patches={"snapshot_git": failing_add})
        self.assertIn("synthetic add failure", str(caught.exception))
        self.assertEqual(self.observed, [])
        self.assert_no_snapshot_left()

    def test_incomplete_cleanup_is_reported_with_removal_command(self):
        with mock.patch.dict(self.helper["main_impl"].__globals__, {
                "remove_review_snapshot": lambda *_args: False}):
            self.assertEqual(self.invoke("--mode", "branch", "--base", "main", *self.snapshot_args()), 0)
        self.assertIn("autoreview snapshot cleanup incomplete", self.stderr)
        self.assertIn("worktree remove --force", self.stderr)
        # The real removal still works afterwards.
        snapshot = self.observed[0]["repo"]
        git(self.repo, "worktree", "remove", "--force", str(snapshot))
        for parent in (snapshot.parent,):
            for child in parent.iterdir():
                child.rmdir()
            parent.rmdir()
        self.assert_no_snapshot_left()

    # Refusals --------------------------------------------------------------

    def test_local_modes_refuse_before_git_or_snapshot_work(self):
        guards = {name: mock.Mock(side_effect=AssertionError(f"refusal reached {name}"))
                  for name in ("preflight_git", "repo_root", "create_snapshot_parent")}
        for argv, env in ((("--mode", "local", "--snapshot"), {}),
                          (("--mode", "uncommitted", "--snapshot"), {}),
                          (("--mode", "local"), {"AUTOREVIEW_SNAPSHOT": "1"})):
            with self.subTest(argv=argv, env=env), mock.patch.dict(os.environ, env):
                with self.assertRaisesRegex(SystemExit, "--snapshot reviews committed content only; "
                                                        "local mode reviews the index"):
                    self.invoke(*argv, patches=guards)
        for guard in guards.values():
            guard.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_auto_mode_on_dirty_checkout_refuses_and_no_snapshot_overrides(self):
        (self.repo / "calc.py").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "--mode auto selected local work"):
            self.invoke("--mode", "auto", *self.snapshot_args())
        self.assertFalse(self.state.exists())
        with mock.patch.dict(os.environ, {"AUTOREVIEW_SNAPSHOT": "1"}):
            self.assertEqual(self.invoke("--mode", "local", "--no-snapshot"), 0)
        self.assertEqual(self.observed[0]["repo"], self.repo)
        self.assertIn("+dirty", self.observed[0]["prompt"])

    def test_uncommitted_prompt_files_and_datasets_refuse_before_snapshot(self):
        (self.repo / "draft.md").write_text("untracked notes\n", encoding="utf-8")
        (self.repo / "ignored.md").write_text("ignored notes\n", encoding="utf-8")
        (self.repo / "staged.md").write_text("staged notes\n", encoding="utf-8")
        git(self.repo, "add", "staged.md")
        for option, path in (("--prompt-file", "draft.md"), ("--prompt-file", "ignored.md"),
                             ("--prompt-file", "staged.md"), ("--prompt-file", str(self.repo / "draft.md")),
                             ("--dataset", "draft.md")):
            with self.subTest(option=option, path=path):
                with self.assertRaisesRegex(
                        SystemExit, f"--snapshot reviews committed content only: {option} "
                                    r"\S+ is untracked or uncommitted") as caught:
                    self.invoke("--mode", "branch", "--base", "main", option, path, *self.snapshot_args())
                self.assertIn(self.head, str(caught.exception))
                self.assertNotIn("snapshot: ", self.stdout)
                self.assertEqual(self.observed, [])
                self.assert_no_snapshot_left()
        # Control: a direct branch review reads the same untracked prompt file.
        self.assertEqual(self.invoke("--mode", "branch", "--base", "main", "--prompt-file", "draft.md"), 0)
        self.assertIn("untracked notes", self.observed[0]["prompt"])

    def test_modified_prompt_file_refuses_and_committed_absolute_path_is_frozen(self):
        (self.repo / "notes.md").write_text("uncommitted edit\n", encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "--prompt-file notes.md has uncommitted or later changes"):
            self.invoke("--mode", "branch", "--base", "main", "--prompt-file", "notes.md",
                        *self.snapshot_args())
        self.assertIn("snapshot: ", self.stdout)
        self.assertEqual(self.observed, [])
        self.assert_no_snapshot_left()
        git(self.repo, "checkout", "--", "notes.md")
        self.assertEqual(self.invoke(
            "--mode", "branch", "--base", "main", "--prompt-file", str(self.repo / "notes.md"),
            "--dataset", "data.json", *self.snapshot_args(),
        ), 0, self.stderr)
        prompt = self.observed[0]["prompt"]
        self.assertIn("# Prompt file: notes.md\nReview the division helper.", prompt)
        self.assertIn("# Dataset: data.json", prompt)
        self.assert_no_snapshot_left()

    def test_outputs_inside_the_operator_checkout_stay_refused(self):
        with self.assertRaisesRegex(SystemExit, "must point outside the reviewed repository"):
            self.invoke("--mode", "branch", "--base", "main", "--json-output",
                        str(self.repo / "report.json"), *self.snapshot_args())
        self.assertFalse(self.state.exists())
        snapshot = self.root / "snapshot-checkout"
        snapshot.mkdir()
        origins = self.helper["_SNAPSHOT_ORIGINS"]
        origins[snapshot.resolve()] = self.helper["SnapshotOrigin"](self.repo.resolve(), "feature")
        self.addCleanup(origins.clear)
        for destination in (self.repo / "inside.json", snapshot / "inside.json"):
            with self.subTest(destination=destination):
                args = argparse.Namespace(json_output=str(destination), output=None,
                                          status_output=None, engine_stage_dir=None)
                with self.assertRaisesRegex(SystemExit, "must point outside the reviewed repository"):
                    self.helper["prepare_output_paths"](args, snapshot)
        args = argparse.Namespace(json_output=str(self.root / "outside.json"), output=None,
                                  status_output=None, engine_stage_dir=None)
        self.helper["prepare_output_paths"](args, snapshot)

    # Equivalence and trust boundary -----------------------------------------

    def test_snapshot_prompts_match_a_direct_review_byte_for_byte(self):
        for argv in (("--mode", "branch", "--base", "main"),
                     ("--mode", "commit"),
                     ("--mode", "branch", "--base", "main", "--prompt-file", "notes.md",
                      "--dataset", "data.json", "--source-context-file", "calc.py",
                      "--max-priority", "P2")):
            with self.subTest(argv=argv):
                self.observed.clear()
                self.assertEqual(self.invoke(*argv), 0)
                self.assertEqual(self.invoke(*argv, *self.snapshot_args()), 0)
                direct, frozen = self.observed
                self.assertEqual(direct["repo"], self.repo)
                self.assertNotEqual(frozen["repo"], self.repo)
                self.assertEqual(frozen["prompt"], direct["prompt"])
                self.assert_no_snapshot_left()

    def test_without_flag_review_uses_checkout_and_creates_no_snapshot(self):
        self.assertIs(self.helper["parse_args"].__globals__["_SNAPSHOT_ORIGINS"],
                      self.helper["_SNAPSHOT_ORIGINS"])
        self.assertEqual(self.helper["repository_roots"](self.repo), (self.repo.resolve(),))
        (self.repo / "scratch.txt").write_text("dirty work is outside branch mode\n", encoding="utf-8")
        self.assertEqual(self.invoke("--mode", "branch", "--base", "main",
                                     "--run-log-dir", str(self.state)), 0)
        self.assertEqual(self.observed[0]["repo"], self.repo)
        self.assertNotIn("snapshot: ", self.stdout)
        self.assertNotIn("snapshot_commit: ", self.stdout)
        self.assertNotIn("snapshot: ", self.stderr)
        self.assertFalse((self.state / "snapshots").exists())
        self.assertEqual(self.worktree_paths(), [self.repo.resolve()])

    def test_snapshot_keeps_operator_checkout_untrusted(self):
        marker = self.root / "repo-git-ran"
        bin_dir = self.repo / "bin"
        bin_dir.mkdir()
        write_executable(bin_dir / "git", (
            "#!/usr/bin/env python3\n"
            f"open({str(marker)!r}, 'a').write('ran')\n"
            "raise SystemExit(1)\n"
        ))
        write_executable(bin_dir / "reviewtool", "#!/bin/sh\nexit 0\n")
        checks = {}

        def inspect(snapshot):
            helper = self.helper
            checks["git"] = helper["resolve_git"](snapshot)
            checks["tool"] = helper["find_command"]("reviewtool", snapshot)
            checks["path"] = helper["safe_engine_path"](snapshot)
            checks["external"] = helper["external_env_path"](snapshot, str(self.repo / "home"))
            checks["owned"] = helper["repository_owned"](self.repo.resolve() / "x", snapshot)
            with mock.patch.dict(os.environ, {"TMPDIR": str(self.repo), "TMP": str(self.repo),
                                              "TEMP": str(self.repo)}), \
                    mock.patch.object(helper["safe_temp_root"].__globals__["tempfile"], "tempdir", None):
                with self.assertRaisesRegex(SystemExit, "temporary directory must be outside"):
                    helper["safe_temp_root"](snapshot)

        with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}):
            self.assertEqual(self.invoke("--mode", "branch", "--base", "main", *self.snapshot_args(),
                                         engine=self.recording_engine(inspect)), 0, self.stderr)
        self.assertFalse(marker.exists(), "a repository-owned git executable ran")
        self.assertFalse(Path(checks["git"]).resolve().is_relative_to(self.repo.resolve()))
        self.assertIsNone(checks["tool"])
        self.assertNotIn(str(bin_dir.resolve()), checks["path"].split(os.pathsep))
        self.assertFalse(checks["external"])
        self.assertTrue(checks["owned"])
        self.assert_no_snapshot_left()

    @unittest.skipIf(os.name == "nt", "POSIX hook and filter scripts")
    def test_snapshot_creation_runs_no_hooks_filters_or_sparse_cone(self):
        markers = self.root / "markers"
        hooks = self.repo / ".githooks"
        hooks.mkdir()
        for hook in ("post-checkout", "reference-transaction", "post-index-change"):
            write_executable(hooks / hook, f"#!/bin/sh\necho {hook} >> '{markers}'\n")
        git(self.repo, "config", "core.hooksPath", ".githooks")
        git(self.repo, "config", "filter.mark.smudge", f"sh -c 'echo smudge >> \"{markers}\"; cat'")
        git(self.repo, "config", "filter.mark.clean", "cat")
        git(self.repo, "config", "filter.mark.required", "true")
        (self.repo / ".gitattributes").write_text("*.dat filter=mark\n", encoding="utf-8")
        (self.repo / "keep").mkdir()
        (self.repo / "keep" / "raw.dat").write_text("raw bytes\n", encoding="utf-8")
        (self.repo / "drop").mkdir()
        (self.repo / "drop" / "outside.txt").write_text("outside the cone\n", encoding="utf-8")
        git(self.repo, "add", ".gitattributes", "keep", "drop")
        git(self.repo, "commit", "-qm", "attributes", "--no-verify")
        markers.unlink(missing_ok=True)
        head = git(self.repo, "rev-parse", "HEAD").strip()
        # Control: a plain worktree add in this repository runs hooks and the filter.
        control = self.root / "control"
        git(self.repo, "worktree", "add", "-q", "--detach", str(control), head)
        self.assertIn("post-checkout", markers.read_text(encoding="utf-8"))
        self.assertIn("smudge", markers.read_text(encoding="utf-8"))
        git(self.repo, "worktree", "remove", "--force", str(control))
        markers.unlink()
        git(self.repo, "sparse-checkout", "set", "keep")
        self.assertFalse((self.repo / "drop").exists())
        markers.unlink(missing_ok=True)
        seen = {}

        def inspect(snapshot):
            seen["raw"] = (snapshot / "keep" / "raw.dat").read_text(encoding="utf-8")
            seen["outside"] = (snapshot / "drop" / "outside.txt").is_file()
            seen["skip_worktree"] = [line for line in git(snapshot, "ls-files", "-t").splitlines()
                                     if not line.startswith("H ")]

        self.assertEqual(self.invoke("--mode", "branch", "--base", "main", *self.snapshot_args(),
                                     engine=self.recording_engine(inspect)), 0, self.stderr)
        self.assertFalse(markers.exists(), markers.read_text(encoding="utf-8") if markers.exists() else "")
        self.assertEqual(seen, {"raw": "raw bytes\n", "outside": True, "skip_worktree": []})
        self.assert_no_snapshot_left()

    def test_run_history_keeps_operator_identity_and_records_snapshot(self):
        with mock.patch.dict(os.environ, {"AUTOREVIEW_RUN_LOG": "1"}):
            self.assertEqual(self.invoke("--mode", "branch", "--base", "main", *self.snapshot_args()), 0)
        runs = list((self.state / "runs").iterdir())
        self.assertEqual(len(runs), 1)
        metadata = json.loads((runs[0] / "metadata.json").read_text(encoding="utf-8"))
        snapshot = self.observed[0]["repo"]
        self.assertEqual(metadata["repo"], {
            "root": str(self.repo.resolve()), "branch": "feature", "commit": self.head,
        })
        self.assertEqual(metadata["target"], {"mode": "branch", "ref": "main"})
        self.assertEqual(metadata["snapshot"], {"commit": self.head, "path": str(snapshot)})
        self.assert_no_snapshot_left()


@unittest.skipIf(os.name == "nt", "POSIX signal delivery")
class SnapshotModeProcessTests(unittest.TestCase):
    """Exercise the real CLI process: interrupts and deadlines still remove the snapshot."""

    FAKE_CODEX = r'''#!/usr/bin/env python3
import os
import sys
import time
from pathlib import Path

if "--version" in sys.argv[1:]:
    print("codex-cli 0.0.0-test")
    raise SystemExit(0)
Path(os.environ["AUTOREVIEW_FAKE_READY"]).write_text(os.getcwd())
time.sleep(60)
'''

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="autoreview-snapshot-cli.")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = init_repo(self.root)
        git(self.repo, "config", "core.autocrlf", "false")
        (self.repo / "calc.py").write_text("one\n", encoding="utf-8")
        git(self.repo, "add", "calc.py")
        git(self.repo, "commit", "-qm", "base")
        self.base = git(self.repo, "rev-parse", "HEAD").strip()
        (self.repo / "calc.py").write_text("two\n", encoding="utf-8")
        git(self.repo, "commit", "-qam", "change")
        tools = self.root / "tools"
        tools.mkdir()
        write_executable(tools / "trufflehog", fake_trufflehog_script())
        self.codex = write_executable(tools / "codex", self.FAKE_CODEX)
        self.home = self.root / "home"
        self.home.mkdir()
        self.state = self.root / "state"
        self.ready = self.root / "ready"
        self.env = {key: os.environ[key] for key in ("PATH", "TMPDIR", "DEVELOPER_DIR") if key in os.environ}
        self.env.update({
            "PATH": f"{tools}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(self.home), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "AUTOREVIEW_RUN_LOG": "0", "AUTOREVIEW_FAKE_READY": str(self.ready),
        })

    def start(self, *extra, ready=None):
        env = dict(self.env)
        if ready is not None:
            env["AUTOREVIEW_FAKE_READY"] = str(ready)
        return subprocess.Popen(
            [sys.executable, str(SCRIPT), "--engine", "codex", "--codex-bin", str(self.codex),
             "--mode", "branch", "--base", self.base, "--snapshot",
             "--run-log-dir", str(self.state), *extra],
            cwd=self.repo, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def wait_until_ready(self, proc, ready):
        # Generous: a loaded runner can take a while to reach reviewer launch.
        deadline = time.monotonic() + 180
        while not ready.exists():
            if proc.poll() is not None or time.monotonic() > deadline:
                proc.kill()
                self.fail(f"reviewer never started: {proc.communicate()}")
            time.sleep(0.05)

    def assert_no_snapshot_left(self, stdout):
        snapshot = next(Path(line.removeprefix("snapshot: ")) for line in stdout.splitlines()
                        if line.startswith("snapshot: "))
        self.assertFalse(snapshot.exists())
        self.assertEqual(list((self.state / "snapshots").iterdir()), [])
        listed = git(self.repo, "worktree", "list", "--porcelain")
        self.assertEqual([line for line in listed.splitlines() if line.startswith("worktree ")],
                         [f"worktree {self.repo.resolve()}"])

    def test_interrupt_during_review_removes_snapshot(self):
        proc = self.start()
        try:
            self.wait_until_ready(proc, self.ready)
            # The reviewer runs in an isolated workspace, never in either checkout.
            reviewer_cwd = Path(self.ready.read_text())
            self.assertFalse(reviewer_cwd.is_relative_to(self.repo))
            self.assertFalse(reviewer_cwd.is_relative_to(self.state))
            proc.send_signal(signal.SIGINT)
            stdout, stderr = proc.communicate(timeout=120)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertEqual(proc.returncode, 130, stderr)
        self.assertIn("snapshot: removed", stderr)
        self.assert_no_snapshot_left(stdout)

    def test_parallel_snapshot_reviews_are_independent_and_both_removed(self):
        # Two engines reviewing the same pinned scope at once, as the skill recommends.
        ready = [self.root / "ready-a", self.root / "ready-b"]
        procs = [self.start(ready=path) for path in ready]
        try:
            for proc, path in zip(procs, ready):
                self.wait_until_ready(proc, path)
            listed = git(self.repo, "worktree", "list", "--porcelain")
            self.assertEqual(len([line for line in listed.splitlines() if line.startswith("worktree ")]), 3)
            for proc in procs:
                proc.send_signal(signal.SIGINT)
            results = [proc.communicate(timeout=120) for proc in procs]
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
        snapshots = set()
        for proc, (stdout, stderr) in zip(procs, results):
            self.assertEqual(proc.returncode, 130, stderr)
            self.assertIn("snapshot: removed", stderr)
            snapshots.add(next(line for line in stdout.splitlines() if line.startswith("snapshot: ")))
        self.assertEqual(len(snapshots), 2)
        self.assert_no_snapshot_left(results[0][0])

    def test_reviewer_deadline_removes_snapshot(self):
        proc = self.start("--engine-timeout-seconds", "2")
        stdout, stderr = proc.communicate(timeout=240)
        self.assertEqual(proc.returncode, 1, stderr)
        self.assertTrue(self.ready.exists())
        self.assertIn("snapshot: removed", stderr)
        self.assert_no_snapshot_left(stdout)


if __name__ == "__main__":
    unittest.main()
