#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import hashlib
import importlib.util
import io
import itertools
import json
import os
import runpy
import stat
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).with_name("autoreview")
fixture_git = runpy.run_path(str(SCRIPT_PATH.with_name("test-review-harness.py")))["fixture_git"]
LOADER = SourceFileLoader("autoreview_module", str(SCRIPT_PATH))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None
AUTOREVIEW = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(AUTOREVIEW)


FINAL_REPORT = {
    "findings": [],
    "overall_correctness": "patch is correct",
    "overall_explanation": "clean",
    "overall_confidence": 0.9,
}

DRAFT_REPORT = {
    "findings": [
        {
            "title": "Draft finding",
            "body": "draft",
            "priority": "P3",
            "confidence": 0.2,
            "category": "maintainability",
            "code_location": {"file_path": "draft.js", "line": 1},
        }
    ],
    "overall_correctness": "patch is incorrect",
    "overall_explanation": "draft",
    "overall_confidence": 0.2,
}


class AutoreviewCursorTests(unittest.TestCase):
    def test_parser_resource_errors_are_invalid_reports(self) -> None:
        args = argparse.Namespace(engine="codex", max_priority="P2")
        for raw in ("[" * 2000 + "]" * 2000, '{"findings":[],"number":' + "9" * 10000 + "}"):
            with self.subTest(length=len(raw)), mock.patch.object(
                AUTOREVIEW, "run_engine", return_value=raw,
            ), mock.patch.object(
                AUTOREVIEW, "scan_outgoing_review_pack",
            ):
                with self.assertRaises(AUTOREVIEW.ReviewerUnavailable) as caught:
                    AUTOREVIEW.run_reviewer(args, Path.cwd(), "synthetic", set(), [])
                self.assertEqual(caught.exception.reason, "invalid_report")

    def test_container_valued_report_enums_are_invalid_reports(self) -> None:
        args = argparse.Namespace(engine="codex", max_priority="P2")
        finding = copy.deepcopy(DRAFT_REPORT["findings"][0])
        finding["source_attribution"] = {
            "target": "index", "record_id": "record", "source_id": "source",
            "side": "present", "column": 1, "excerpt": "text",
        }
        for field in ("overall_correctness", "priority", "category", "target", "side"):
            for value in ([], {}, None, 42, False):
                report = copy.deepcopy(FINAL_REPORT)
                report["findings"] = [copy.deepcopy(finding)]
                owner = report if field == "overall_correctness" else report["findings"][0]
                if field in {"target", "side"}:
                    owner = owner["source_attribution"]
                owner[field] = value
                with self.subTest(field=field, value=value), mock.patch.object(
                    AUTOREVIEW, "run_engine", return_value=json.dumps({**report, "review_completion": "complete"}),
                ), mock.patch.object(
                    AUTOREVIEW, "scan_outgoing_review_pack",
                ):
                    with self.assertRaises(AUTOREVIEW.ReviewerUnavailable) as caught:
                        AUTOREVIEW.run_reviewer(args, Path.cwd(), "synthetic", {"draft.js"}, [])
                    self.assertEqual(caught.exception.reason, "invalid_report")

    def test_private_completion_is_required_validated_and_stripped(self) -> None:
        args = argparse.Namespace(engine="codex", max_priority="P2")
        for completion in ("complete", "incomplete"):
            provider = {**FINAL_REPORT, "review_completion": completion}
            with self.subTest(completion=completion), mock.patch.object(
                AUTOREVIEW, "run_engine", return_value=json.dumps(provider),
            ):
                result = AUTOREVIEW.run_reviewer(args, Path.cwd(), "synthetic", set(), [])
            self.assertEqual(result.complete, completion == "complete")
            self.assertEqual(result.report["provider_report"], FINAL_REPORT)
            self.assertNotIn("review_completion", result.report)
            self.assertEqual(
                AUTOREVIEW.review_status(result.report, complete=result.complete),
                "scoped-clean" if result.complete else "incomplete",
            )
        for provider in (
            FINAL_REPORT,
            *({**FINAL_REPORT, "review_completion": value}
              for value in ("", "deferred", [], {}, None, 42, False)),
        ):
            with self.subTest(provider=provider), mock.patch.object(
                AUTOREVIEW, "run_engine", return_value=json.dumps(provider),
            ):
                with self.assertRaises(AUTOREVIEW.ReviewerUnavailable) as caught:
                    AUTOREVIEW.run_reviewer(args, Path.cwd(), "synthetic", set(), [])
            self.assertEqual(caught.exception.reason, "invalid_report")
            self.assertIn("missing or invalid review_completion", str(caught.exception))

    def test_provider_schema_keeps_completion_out_of_public_schema(self) -> None:
        self.assertEqual(
            AUTOREVIEW.PROVIDER_SCHEMA["required"],
            [*AUTOREVIEW.SCHEMA["required"], "review_completion"],
        )
        self.assertEqual(
            AUTOREVIEW.PROVIDER_SCHEMA["properties"]["review_completion"],
            {"type": "string", "enum": ["complete", "incomplete"]},
        )
        self.assertFalse(AUTOREVIEW.PROVIDER_SCHEMA["additionalProperties"])
        self.assertNotIn("review_completion", AUTOREVIEW.SCHEMA["properties"])
        prompt = AUTOREVIEW.render_review_prompt(
            "task", "local", None, AUTOREVIEW.ReviewChunk("change"), "", "", (1, 2),
        )
        self.assertIn(json.dumps(AUTOREVIEW.PROVIDER_SCHEMA), prompt)
        self.assertIn("independent, complete assignment", prompt)
        self.assertIn("no shared conversation or future evidence batch", prompt)

    def test_extract_json_prefers_terminal_result_event(self) -> None:
        stream = "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": json.dumps(DRAFT_REPORT)}]},
                    }
                ),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "result": json.dumps(FINAL_REPORT),
                        "session_id": "session-id",
                        "request_id": "request-id",
                    }
                ),
            ]
        )
        self.assertEqual(AUTOREVIEW.extract_json(stream), FINAL_REPORT)

    def test_extract_json_can_fallback_to_assistant_message(self) -> None:
        stream = json.dumps(
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": json.dumps(FINAL_REPORT)}]},
            }
        )
        self.assertEqual(AUTOREVIEW.extract_json(stream), FINAL_REPORT)

    def test_extract_json_does_not_fallback_past_bad_terminal_result(self) -> None:
        stream = "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": json.dumps(FINAL_REPORT)}]},
                    }
                ),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "result": "not json",
                    }
                ),
            ]
        )
        with self.assertRaises(SystemExit) as exc_info:
            AUTOREVIEW.extract_json(stream)
        self.assertIn("review engine result was not structured JSON", str(exc_info.exception))


class AutoreviewImageEvidenceTests(unittest.TestCase):
    PNG = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )

    def test_supported_image_is_staged_with_exact_manifest_identity(self) -> None:
        digest = hashlib.sha256(self.PNG).hexdigest()
        image = AUTOREVIEW.ImageEvidence("assets/avatar.png", "image/png", digest, self.PNG)
        self.assertEqual(AUTOREVIEW.image_media_type(image.path, image.content), "image/png")
        manifest = AUTOREVIEW.image_manifest((image,))
        self.assertIn('path="assets/avatar.png"', manifest)
        self.assertIn(f"sha256={digest}", manifest)
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = AUTOREVIEW.stage_review_images(Path(tmpdir), (image,))
            self.assertEqual(paths[0].read_bytes(), self.PNG)

    def test_oversized_dimensions_are_refused_before_pixel_decoding(self):
        from PIL import Image
        original_open = Image.open
        def declared_large(*args, **kwargs):
            image = original_open(*args, **kwargs)
            image._size = (8192, 8192)
            image.load = mock.Mock(side_effect=AssertionError("pixels must not be decoded"))
            return image
        with mock.patch.object(Image, "open", side_effect=declared_large):
            with self.assertRaisesRegex(SystemExit, "decoder limits"):
                AUTOREVIEW.image_media_type("large.png", self.PNG)

    def test_decompression_bomb_warning_is_a_refusal(self):
        from PIL import Image
        with mock.patch.object(Image, "MAX_IMAGE_PIXELS", 0.75):
            with self.assertRaisesRegex(SystemExit, "decoder limits"):
                AUTOREVIEW.image_media_type("warning.png", self.PNG)

    def test_missing_decoder_fails_closed(self):
        with mock.patch.dict(sys.modules, {"PIL": None}):
            with self.assertRaisesRegex(SystemExit, "requires Pillow"):
                AUTOREVIEW.image_media_type("image.png", self.PNG)

    def test_native_codex_command_attaches_images_before_stdin_separator(self):
        args = argparse.Namespace(codex_bin="codex", web_search=False,
            thinking="high", stream_engine_output=False, codex_config=None,
            codex_speed=None)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            AUTOREVIEW, "resolve_command", return_value="/usr/bin/codex"
        ):
            root = Path(tmp)
            images = [root / "attachment-1.webp", root / "attachment-2.png"]
            command = AUTOREVIEW.codex_command(args, root, root, root,
                root / "schema.json", root / "output.json", "vision-model",
                auth_config=[], image_paths=images)
        self.assertEqual(command[-6:], ["--image", str(images[0]),
            "--image", str(images[1]), "--", "-"])
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("features.plugins=false", command)

    def test_tampered_image_fails_closed_before_reviewer_launch(self) -> None:
        image = AUTOREVIEW.ImageEvidence(
            "assets/avatar.png", "image/png", "0" * 64, self.PNG,
        )
        with tempfile.TemporaryDirectory() as tmpdir, self.assertRaisesRegex(
            SystemExit, "captured image bytes changed"
        ):
            AUTOREVIEW.stage_review_images(Path(tmpdir), (image,))


class AutoreviewImageGitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.repo / "text.txt").write_bytes(b"old\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args):
        return fixture_git(
            self.repo, *args, check=True, stdout=subprocess.PIPE, text=True,
        ).stdout

    def commit(self, path, content):
        file = self.repo / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)
        self.git("add", ".")
        self.git("commit", "-qm", "change")

    def test_branch_captures_commit_bytes_not_dirty_worktree(self):
        path = "assets/portrait.png"
        self.commit(path, AutoreviewImageEvidenceTests.PNG)
        (self.repo / path).write_bytes(b"dirty replacement")
        captured = AUTOREVIEW.branch_bundle(self.repo, self.base)
        self.assertEqual(captured.images[0].content, AutoreviewImageEvidenceTests.PNG)
        self.assertIn(path, captured.paths)
        self.assertIn("Binary files", captured.text)
        self.assertEqual(len(captured.images), 1)

    def test_image_capture_preserves_literal_paths_and_ignores_dirty_filters(self):
        path = "assets/[literal]\timage.png" if os.name != "nt" else "assets/[literal]image.png"
        self.commit(path, AutoreviewImageEvidenceTests.PNG)
        self.git("config", "filter.denied.clean", "exit 91")
        self.git("config", "filter.denied.required", "true")
        (self.repo / ".gitattributes").write_text("* filter=denied\n")
        (self.repo / path).write_bytes(b"uncommitted replacement")
        captured = AUTOREVIEW.branch_bundle(self.repo, self.base)
        self.assertEqual(captured.images[0].path, path)
        self.assertEqual(captured.images[0].content, AutoreviewImageEvidenceTests.PNG)
        self.assertEqual(captured.commit, self.git("rev-parse", "HEAD").strip())

    def test_encoded_image_budget_is_checked_before_blob_capture(self):
        self.commit("oversized.png", AutoreviewImageEvidenceTests.PNG)
        original = AUTOREVIEW.git_bytes
        def limited(repo, *args, **kwargs):
            if args[:2] == ("cat-file", "-s"):
                return subprocess.CompletedProcess(args, 0, b"20971521\n", b"")
            if args[:2] == ("cat-file", "blob"):
                raise AssertionError("oversized image bytes must not be captured")
            return original(repo, *args, **kwargs)
        with mock.patch.object(AUTOREVIEW, "git_bytes", side_effect=limited):
            with self.assertRaisesRegex(SystemExit, "encoded image"):
                AUTOREVIEW.branch_bundle(self.repo, self.base)

    def test_aggregate_image_budget_is_checked_before_next_blob(self):
        self.commit("first.png", AutoreviewImageEvidenceTests.PNG)
        self.commit("second.png", AutoreviewImageEvidenceTests.PNG)
        original = AUTOREVIEW.git_bytes
        reads = []
        def observe(repo, *args, **kwargs):
            if args[:2] == ("cat-file", "blob"):
                reads.append(args)
            return original(repo, *args, **kwargs)
        with mock.patch.object(AUTOREVIEW, "MAX_REVIEW_IMAGE_TOTAL_BYTES", len(AutoreviewImageEvidenceTests.PNG), create=True), \
                mock.patch.object(AUTOREVIEW, "git_bytes", side_effect=observe):
            with self.assertRaisesRegex(SystemExit, "encoded image"):
                AUTOREVIEW.branch_bundle(self.repo, self.base)
        self.assertEqual(len(reads), 1)

    def test_unsupported_binary_and_spoofed_extension_are_rejected(self):
        for name, data in [("payload.bin", b"\x00opaque"),
                           ("fake.png", b"\x89PNG\r\n\x1a\n\x00truncated")]:
            with self.subTest(name=name):
                self.git("reset", "--hard", self.base)
                self.commit(name, data)
                with self.assertRaisesRegex(SystemExit, "unsupported or malformed"):
                    AUTOREVIEW.branch_bundle(self.repo, self.base)

    def test_attributes_cannot_hide_opaque_blobs_or_image_attachments(self):
        self.commit(".gitattributes", "* diff\n".encode())
        self.commit("hidden.bin", b"opaque\0payload")
        with self.assertRaisesRegex(SystemExit, "unsupported or malformed"):
            AUTOREVIEW.branch_bundle(self.repo, self.base)
        self.git("rm", "hidden.bin")
        self.git("commit", "-qm", "remove opaque")
        self.commit("portrait.png", AutoreviewImageEvidenceTests.PNG)
        # Forced textual image patches must never be lossily decoded or
        # silently reviewed without pixels. The existing UTF-8 gate refuses.
        with self.assertRaisesRegex(SystemExit, "non-UTF-8 Git output"):
            AUTOREVIEW.branch_bundle(self.repo, self.base)

    def test_modified_images_refuse_but_deletions_keep_only_metadata(self):
        self.commit("portrait.png", AutoreviewImageEvidenceTests.PNG)
        base = self.git("rev-parse", "HEAD").strip()
        self.commit("portrait.png", AutoreviewImageEvidenceTests.PNG + b"x")
        with self.assertRaisesRegex(SystemExit, "only added images"):
            AUTOREVIEW.branch_bundle(self.repo, base)
        self.git("rm", "portrait.png")
        self.git("commit", "-qm", "delete")
        with mock.patch.object(AUTOREVIEW, "image_media_type", side_effect=AssertionError("decoded deletion")):
            captured = AUTOREVIEW.branch_bundle(self.repo, base)
        self.assertEqual(captured.images, ())
        self.assertIn("portrait.png", captured.paths)
        self.assertIn("Binary files ", captured.text)

    def test_sensitive_images_are_not_attached(self):
        self.commit(".ssh/portrait.png", AutoreviewImageEvidenceTests.PNG)
        with self.assertRaisesRegex(SystemExit, "sensitive binary"):
            AUTOREVIEW.branch_bundle(self.repo, self.base)

    def test_local_and_commit_modes_still_fail_closed(self):
        self.commit("portrait.png", AutoreviewImageEvidenceTests.PNG)
        with self.assertRaisesRegex(SystemExit, "refusing binary changes"):
            AUTOREVIEW.commit_bundle(self.repo, "HEAD")
        (self.repo / "portrait.png").write_bytes(AutoreviewImageEvidenceTests.PNG + b"x")
        with self.assertRaisesRegex(SystemExit, "refusing binary changes"):
            AUTOREVIEW.local_bundle(self.repo)

    def test_other_engine_cannot_get_clean_image_verdict(self):
        self.commit("portrait.png", AutoreviewImageEvidenceTests.PNG)
        captured = AUTOREVIEW.branch_bundle(self.repo, self.base)
        for engine in ("claude", "amp", "pi", "kimi"):
            with self.subTest(engine=engine), mock.patch.object(AUTOREVIEW, "run_engine") as run:
                with self.assertRaisesRegex(SystemExit, "only by the Codex"):
                    AUTOREVIEW.run_reviewer(argparse.Namespace(engine=engine), self.repo, "review", captured, [])
                run.assert_not_called()

    def test_manifest_and_attachments_travel_on_every_pass(self):
        self.commit("portrait.png", AutoreviewImageEvidenceTests.PNG)
        captured = AUTOREVIEW.branch_bundle(self.repo, self.base)
        with mock.patch.object(AUTOREVIEW, "build_review_prompts", return_value=["one", "two"]) as build:
            AUTOREVIEW.prepare_review_prompts(self.repo, "branch", self.base, captured, "instructions", [], 512000)
        self.assertIn(captured.images[0].sha256, build.call_args.args[4])
        args = argparse.Namespace(engine="codex", max_priority="P0")
        with mock.patch.object(AUTOREVIEW, "run_engine", return_value=json.dumps({**FINAL_REPORT, "review_completion": "complete"})) as run:
            AUTOREVIEW.run_review_passes(args, [args], self.repo, ["one", "two"], captured)
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertEqual(call.args[0].review_images, captured.images)
            self.assertIs(call.args[0].review_usage, args.review_usage)
        self.assertFalse(hasattr(args, "review_images"))

    def test_valid_webp_jpeg_and_png_decode_but_animation_does_not(self):
        from PIL import Image
        for fmt, suffix in [("WEBP", "webp"), ("JPEG", "jpg"), ("PNG", "png")]:
            out = io.BytesIO()
            Image.new("RGB", (4, 4), "red").save(out, format=fmt)
            self.assertIsNotNone(AUTOREVIEW.image_media_type("image." + suffix, out.getvalue()))
        out = io.BytesIO()
        Image.new("RGB", (4, 4), "red").save(out, format="WEBP", save_all=True,
            append_images=[Image.new("RGB", (4, 4), "blue")], duration=100)
        with self.assertRaisesRegex(SystemExit, "animated"):
            AUTOREVIEW.image_media_type("image.webp", out.getvalue())


class AutoreviewPriorityTests(unittest.TestCase):
    def test_default_priority_is_p0(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(sys, "argv", ["autoreview"]):
            args = AUTOREVIEW.parse_args()
        self.assertEqual(args.max_priority, "P0")

    def test_priority_environment_values_and_explicit_overrides(self) -> None:
        for priority in ("P0", "P1", "P2", "P3"):
            for env_priority, options in (
                (priority, []),
                ("P4", ["--max-priority", priority]),
                ("", ["--max-priority", priority]),
            ):
                with self.subTest(priority=priority, env=env_priority), mock.patch.dict(
                    os.environ, {"AUTOREVIEW_MAX_PRIORITY": env_priority}, clear=True,
                ), mock.patch.object(sys, "argv", ["autoreview", *options]):
                    self.assertEqual(AUTOREVIEW.parse_args().max_priority, priority)

    def test_invalid_priority_defaults_refuse_before_preparation(self) -> None:
        for priority in ("P4", "", " ", "p2"):
            for dry_run in (False, True):
                with self.subTest(priority=priority, dry_run=dry_run), tempfile.TemporaryDirectory() as tmp:
                    status = Path(tmp) / "status.json"
                    status.write_text("existing status\n")
                    activity = {
                        name: mock.Mock(side_effect=AssertionError(f"unexpected {name}"))
                        for name in (
                            "EngineStage", "persist_engine_stage", "reviewer_args", "preflight_git",
                            "prepare_output_paths", "run_engine",
                        )
                    }
                    argv = [
                        "autoreview", "--status-output", str(status), "--stream-engine-output",
                        "--engine-stage-dir", str(Path(tmp) / "stage"),
                        *(["--dry-run"] if dry_run else []),
                    ]
                    stderr = io.StringIO()
                    with mock.patch.dict(os.environ, {"AUTOREVIEW_MAX_PRIORITY": priority}, clear=True), \
                            mock.patch.object(sys, "argv", argv), \
                            mock.patch.multiple(AUTOREVIEW, **activity), contextlib.redirect_stderr(stderr):
                        with self.assertRaises(SystemExit) as caught:
                            AUTOREVIEW.main_impl()
                    self.assertEqual(caught.exception.code, 2)
                    self.assertIn("invalid --max-priority/AUTOREVIEW_MAX_PRIORITY", stderr.getvalue())
                    for call in activity.values():
                        call.assert_not_called()
                    self.assertEqual(status.read_text(), "existing status\n")
                    self.assertEqual(sorted(item.name for item in Path(tmp).iterdir()), ["status.json"])

    def test_priority_help_ignores_invalid_environment_default(self) -> None:
        with mock.patch.dict(os.environ, {"AUTOREVIEW_MAX_PRIORITY": "P4"}, clear=True), \
                mock.patch.object(sys, "argv", ["autoreview", "--help"]), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            with self.assertRaises(SystemExit) as caught:
                AUTOREVIEW.parse_args()
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("--max-priority {P0,P1,P2,P3}", stdout.getvalue())

    def test_priority_filter_preserves_lower_findings_and_provider_verdict(self) -> None:
        report = copy.deepcopy(DRAFT_REPORT)
        AUTOREVIEW.filter_findings_by_priority(report, "P0")
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["priority_filtered_findings"], DRAFT_REPORT["findings"])
        for key in ("overall_correctness", "overall_explanation", "overall_confidence"):
            self.assertEqual(report[key], DRAFT_REPORT[key])

    def test_unfinished_assessment_keeps_filtered_observations_incomplete(self) -> None:
        args = argparse.Namespace(engine="codex", max_priority="P0")
        with mock.patch.object(
            AUTOREVIEW, "run_engine",
            return_value=json.dumps({**DRAFT_REPORT, "review_completion": "incomplete"}),
        ):
            result = AUTOREVIEW.run_reviewer(args, Path.cwd(), "synthetic", {"draft.js"}, [])
        self.assertFalse(result.complete)
        self.assertEqual(result.report["provider_report"], DRAFT_REPORT)
        self.assertEqual(result.report["findings"], [])
        self.assertEqual(result.report["priority_filtered_findings"], DRAFT_REPORT["findings"])
        self.assertEqual(AUTOREVIEW.review_status(result.report, complete=result.complete), "incomplete")


class AutoreviewResultScopeTests(unittest.TestCase):
    def test_scope_rejection_preserves_provider_conclusion_and_audit(self) -> None:
        report = copy.deepcopy(DRAFT_REPORT)
        with contextlib.redirect_stderr(io.StringIO()):
            AUTOREVIEW.validate_report(report, Path.cwd(), {"changed.js"}, [])
        self.assertEqual(report["findings"], [])
        for key in ("overall_correctness", "overall_explanation", "overall_confidence"):
            self.assertEqual(report[key], DRAFT_REPORT[key])
        self.assertEqual(report["scope_rejected_findings"], DRAFT_REPORT["findings"])
        report["review_status"] = AUTOREVIEW.review_status(report, complete=True)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            AUTOREVIEW.print_report(report)
        self.assertIn("incomplete", output.getvalue())
        self.assertIn("Draft finding", output.getvalue())
        self.assertIn("draft.js:1", output.getvalue())
        self.assertNotIn("clean:", output.getvalue())

    def test_chunk_merge_keeps_rejections_explanations_and_conservative_confidence(self) -> None:
        rejected = copy.deepcopy(DRAFT_REPORT)
        with contextlib.redirect_stderr(io.StringIO()):
            AUTOREVIEW.validate_report(rejected, Path.cwd(), {"changed.js"}, [])
        reports = [("chunk 1/2", copy.deepcopy(FINAL_REPORT)), ("chunk 2/2", rejected)]
        merged = AUTOREVIEW.merge_chunk_reports(reports)
        self.assertEqual(merged["overall_correctness"], "patch is incorrect")
        self.assertEqual(merged["overall_confidence"], 0.2)
        self.assertEqual(len(merged["scope_rejected_findings"]), 1)
        self.assertEqual(merged["pass_reports"][1]["report"], rejected)
        merged["review_status"] = AUTOREVIEW.review_status(merged, complete=True)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            AUTOREVIEW.print_report(merged)
        self.assertIn("draft", output.getvalue())
        self.assertIn("incomplete", output.getvalue())
        self.assertNotIn("clean:", output.getvalue())

    def test_required_finding_must_survive_priority_filter_for_every_pass_count(self) -> None:
        args = argparse.Namespace(engine="codex", max_priority="P0", require_finding=["Draft finding"])
        for count in (1, 2):
            with self.subTest(count=count), mock.patch.object(
                AUTOREVIEW, "run_engine", return_value=json.dumps({**DRAFT_REPORT, "review_completion": "complete"}),
            ):
                results = AUTOREVIEW.run_review_passes(
                    args, [args], Path.cwd(), ["pack"] * count, {"draft.js"}
                )
            self.assertTrue(all(result.complete for _, result in results))
            reports = [(label, result.report) for label, result in results]
            report = reports[0][1] if count == 1 else AUTOREVIEW.merge_chunk_reports(reports)
            self.assertEqual(
                AUTOREVIEW.missing_required_findings(report, args.require_finding), ["Draft finding"]
            )
            self.assertEqual(report["overall_correctness"], "patch is incorrect")
            self.assertTrue(report["priority_filtered_findings"])

    def test_provider_cannot_supply_local_audit_metadata(self) -> None:
        for key in ("scope_rejected_findings", "priority_filtered_findings", "pass_reports", "review_status", "review_completion"):
            report = copy.deepcopy(FINAL_REPORT)
            report[key] = []
            with self.subTest(key=key), self.assertRaisesRegex(SystemExit, "unexpected top-level"):
                AUTOREVIEW.validate_report(report, Path.cwd(), set(), [])

    def test_required_finding_survives_merge_deduplication_and_body_prefix(self) -> None:
        first = copy.deepcopy(DRAFT_REPORT)
        second = copy.deepcopy(DRAFT_REPORT)
        second["findings"][0]["body"] = "x" * 1980 + " required tail"
        merged = AUTOREVIEW.merge_chunk_reports([("chunk 1/2", first), ("chunk 2/2", second)])
        self.assertEqual(len(merged["findings"]), 1)
        self.assertEqual(AUTOREVIEW.missing_required_findings(merged, ["required tail"]), [])


class AutoreviewTargetResultTests(unittest.TestCase):
    def setUp(self):
        source = AUTOREVIEW.SourceVersion
        self.record = AUTOREVIEW.MixedPath(
            "src/migrate.py", "synthetic-record",
            source("base-source", "100644", "original()\nkeep()\n"),
            source("index-source", "100644", "obsolete()\nkeep()\n"),
            source("working-source", "100644", "corrected()\nkeep()\nbroken()\n"),
            "synthetic staged delta", "synthetic unstaged delta",
            ((1, "original()"),), ((1, "obsolete()"),), (),
        )

    def finding(self, target="index", side="present", line=1, excerpt=None, **changes):
        source = ((self.record.base if target == "index" else self.record.index)
                  if side == "removed" else getattr(self.record, target))
        if excerpt is None:
            excerpt = source.content.splitlines()[line - 1]
        finding = {
            "title": "Synthetic defect", "body": "A concrete synthetic claim.",
            "priority": "P0", "confidence": 0.8, "category": "bug",
            "code_location": {"file_path": self.record.path, "line": line},
            "source_attribution": {"target": target, "record_id": self.record.identity,
                                   "source_id": source.identity, "side": side,
                                   "column": 1, "excerpt": excerpt},
        }
        finding.update(changes)
        return finding

    def validate(self, findings, available=True):
        report = copy.deepcopy(FINAL_REPORT)
        report["findings"] = copy.deepcopy(findings)
        AUTOREVIEW.validate_report(report, Path.cwd(), {self.record.path}, [], (self.record,),
                                   {self.record.identity} if available else set())
        return report

    def test_explicit_targets_anchors_and_pass_availability(self):
        accepted = [self.finding(), self.finding("working_tree", line=3),
                    self.finding("working_tree", line=2), self.finding(side="removed"),
                    self.finding("working_tree", side="removed")]
        self.assertEqual(self.validate(accepted)["findings"], accepted)
        cases = []
        missing = self.finding()
        missing.pop("source_attribution")
        cases.append((missing, "requires explicit"))
        null = self.finding(source_attribution=None)
        cases.append((null, "requires explicit"))
        for field, value, reason in (
            ("record_id", "wrong", "record identity"),
            ("source_id", "wrong", "source identity"),
            ("column", 1000, "excerpt"),
            ("excerpt", "invented()", "excerpt"),
        ):
            finding = self.finding()
            finding["source_attribution"][field] = value
            cases.append((finding, reason))
        cases.extend([
            (self.finding("working_tree", excerpt="obsolete()"), "excerpt"),
            (self.finding("working_tree", line=900, excerpt="broken()"), "out of range"),
            (self.finding("working_tree", side="removed", line=2), "genuinely removed"),
        ])
        for finding, reason in cases:
            with self.subTest(reason=reason, finding=finding):
                report = self.validate([finding])
                self.assertEqual(report["findings"], [])
                self.assertIn(reason, report["attribution_rejected_findings"][0]["attribution_rejection_reason"])
                self.assertEqual(AUTOREVIEW.review_status(report, complete=True), "incomplete")
                self.assertEqual(report["overall_correctness"], "patch is correct")
        report = self.validate([self.finding()], available=False)
        self.assertIn("not available", report["attribution_rejected_findings"][0]["attribution_rejection_reason"])
        original = self.record
        for path in (" src/migrate.py", "src/migrate.py ", " "):
            with self.subTest(path=path):
                self.record = original._replace(path=path)
                finding = self.finding()
                self.assertEqual(self.validate([finding])["findings"], [finding])
            self.record = original

    def test_absence_readd_and_removed_side_are_distinct(self):
        absent = AUTOREVIEW.SourceVersion("absent", None, None)
        for target in ("index", "working_tree"):
            with self.subTest(target=target):
                original = self.record
                if target == "index":
                    self.record = original._replace(index=absent, working_tree_removed=())
                else:
                    self.record = original._replace(working_tree=absent)
                present = self.finding(target, excerpt="obsolete()")
                removed = self.finding(target, side="removed")
                report = self.validate([present, removed])
                self.assertEqual(report["findings"], [removed])
                self.assertIn("absent", report["attribution_rejected_findings"][0]["attribution_rejection_reason"])
                self.record = original

        original = self.record
        for target, side, content, line in (
            ("index", "present", "", 1), ("working_tree", "present", "", 1),
            ("index", "present", "before()\n\n", 2), ("working_tree", "present", "before()\n\n", 2),
            ("index", "removed", "\n", 1), ("working_tree", "removed", "\n", 1),
        ):
            with self.subTest(target=target, side=side, content=content):
                owner = ("base" if target == "index" else "index") if side == "removed" else target
                self.record = original._replace(**{owner: getattr(original, owner)._replace(content=content)})
                if side == "removed":
                    self.record = self.record._replace(**{target + "_removed": ((line, ""),)})
                valid = self.finding(target, side=side, line=line, excerpt="")
                self.assertEqual(self.validate([valid])["findings"], [valid])
                invalid = []
                for key, value in (("record_id", "wrong"), ("source_id", "wrong"),
                                   ("column", 2), ("excerpt", "invented")):
                    bad = copy.deepcopy(valid)
                    bad["source_attribution"][key] = value
                    invalid.append(bad)
                bad = copy.deepcopy(valid)
                bad["code_location"]["line"] = 2 if not content else 900
                invalid.append(bad)
                if side == "present" and content:
                    bad = copy.deepcopy(valid)
                    bad["code_location"]["line"] = 1
                    invalid.append(bad)
                for bad in invalid:
                    report = self.validate([bad])
                    self.assertEqual(report["findings"], [])
                    self.assertEqual(AUTOREVIEW.review_status(report, complete=True), "incomplete")
                self.record = original
        for target in ("index", "working_tree"):
            for side in ("present", "removed"):
                report = self.validate([self.finding(target, side=side, excerpt="")])
                self.assertEqual(report["findings"], [])
            self.record = original._replace(**{target: absent})
            report = self.validate([self.finding(target, excerpt="")])
            self.assertIn("absent", report["attribution_rejected_findings"][0]["attribution_rejection_reason"])
            self.record = original

    def test_title_independent_groups_keep_variants_targets_and_observations(self):
        reports = []
        for index in range(8):
            finding = self.finding(title=f"Index title {index}")
            findings = [finding]
            if index == 7:
                findings += [self.finding(body="Distinct consequence requiring a different fix."),
                             self.finding("working_tree")]
            reports.append((f"pass {index}", self.validate(findings)))
        for selected in (reports, [("single", self.validate([
            finding for _, report in reports for finding in report["findings"]
        ]))]):
            with self.subTest(passes=len(selected)):
                result = AUTOREVIEW.merge_chunk_reports(selected)
                self.assertEqual(len(result["findings"]), 2)
                grouped = result["findings"][0]
                self.assertEqual(len(grouped["claim_variants"]), 2)
                self.assertEqual(len(grouped["claim_variants"][0]["observations"]), 8)
                self.assertEqual(AUTOREVIEW.missing_required_findings(result, ["Index title 7", "different fix"]), [])
                self.assertEqual(len(result["pass_reports"]), len(selected))
                result["review_status"] = AUTOREVIEW.review_status(result, complete=True)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    AUTOREVIEW.print_report(result)
                for text in ("INDEX-only", "WORKING_TREE", "Index title 7", "different fix"):
                    self.assertIn(text, output.getvalue())

    def test_all_engines_keep_raw_reports_before_normalization_and_filters(self):
        captured = AUTOREVIEW.CapturedBundle("delta", {self.record.path}, (self.record,), ())
        prompt = AUTOREVIEW.ReviewPass("synthetic pack", AUTOREVIEW.ReviewChunk("delta", sources=(self.record,)))
        valid = self.finding()
        valid["code_location"]["file_path"] = r".\src\migrate.py"
        stale = self.finding("working_tree", excerpt="obsolete()")
        outside = self.finding(code_location={"file_path": "outside.py", "line": 1})
        provider = {**FINAL_REPORT, "findings": [valid, stale, outside],
                    "overall_correctness": "patch is incorrect", "overall_confidence": 0.43}
        for engine in ("codex", "claude", "amp", "pi"):
            with self.subTest(engine=engine), mock.patch.object(AUTOREVIEW, "run_engine", return_value=json.dumps({**provider, "review_completion": "complete"})), \
                    mock.patch.object(AUTOREVIEW, "verify_mixed_sources"), \
                    mock.patch.object(AUTOREVIEW, "scan_outgoing_review_pack"), \
                    contextlib.redirect_stderr(io.StringIO()):
                result = AUTOREVIEW.run_reviewer(argparse.Namespace(engine=engine, max_priority="P0"),
                                                 Path.cwd(), prompt, captured, [])
            self.assertTrue(result.complete)
            report = result.report
            self.assertEqual(report["provider_report"], provider)
            self.assertEqual(report["overall_confidence"], 0.43)
            self.assertEqual(len(report["findings"]), 1)
            self.assertEqual(len(report["scope_rejected_findings"]), 1)
            self.assertEqual(len(report["attribution_rejected_findings"]), 1)
            self.assertEqual(AUTOREVIEW.review_status(report, complete=result.complete), "incomplete")
            self.assertEqual(report["available_source_records"], [self.record.identity])
        low = self.validate([self.finding(priority="P2")])
        AUTOREVIEW.filter_findings_by_priority(low, "P0")
        self.assertEqual(AUTOREVIEW.missing_required_findings(low, ["Synthetic defect"]), ["Synthetic defect"])
        self.assertEqual(AUTOREVIEW.review_status(low, complete=True), "filtered")
        for bad in ({}, {**self.finding()["source_attribution"], "column": True}):
            with self.assertRaisesRegex(SystemExit, "source_attribution"):
                self.validate([self.finding(source_attribution=bad)])


class AutoreviewSingleEngineRoutingTests(unittest.TestCase):
    def test_explicit_aws_routes_keep_verified_model_defaults(self) -> None:
        for auth, model, effort in (
            ("bedrock", "global.anthropic.claude-fable-5-1[1m]", "high"),
            ("mantle", "anthropic.claude-opus-5-5[1m]", "xhigh"),
        ):
            with self.subTest(auth=auth), mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                sys, "argv", ["autoreview", "--engine", "claude", "--claude-auth", auth,
                              "--claude-bedrock-region", "us-east-1"]
            ):
                reviewer = AUTOREVIEW.reviewer_args(AUTOREVIEW.parse_args())[0]
            self.assertEqual((reviewer.model, reviewer.thinking), (model, effort))

    @staticmethod
    def fable_args() -> argparse.Namespace:
        return argparse.Namespace(
            claude_auth="bedrock",
            claude_bedrock_region="us-east-1",
            claude_bin="claude",
            engine_timeout_seconds=None,
            fallback_model=None,
            model="global.anthropic.claude-fable-5-1[1m]",
            stream_engine_output=False,
            thinking="high",
            tools=False,
            web_search=False,
        )

    def test_chatgpt_auth_replaces_competing_forced_login_method(self) -> None:
        with tempfile.TemporaryDirectory(prefix="autoreview-codex-auth-test.") as tmpdir:
            codex_home = Path(tmpdir)
            (codex_home / "config.toml").write_text(
                'forced_login_method = "api"\n',
                encoding="utf-8",
            )
            with mock.patch.object(
                AUTOREVIEW,
                "codex_source_home",
                return_value=codex_home,
            ):
                flags = AUTOREVIEW.codex_auth_config_flags(
                    Path.cwd(),
                    auth_mode="chatgpt",
                )

        overrides = [
            flags[index + 1]
            for index, value in enumerate(flags)
            if value == "-c"
        ]
        forced = [
            value for value in overrides if value.startswith("forced_login_method=")
        ]
        self.assertEqual(forced, ['forced_login_method="chatgpt"'])

    def test_explicit_auth_routes_remove_competing_provider_env(self) -> None:
        repo = Path("/tmp/autoreview-auth-test")
        base_env = {
            "OPENAI_API_KEY": "provider-key",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDE_CODE_USE_MANTLE": "1",
            "AWS_BEARER_TOKEN_BEDROCK": "bearer",
        }
        with mock.patch.object(
            AUTOREVIEW,
            "safe_engine_env",
            side_effect=lambda *_args, **_kwargs: dict(base_env),
        ):
            codex_env = AUTOREVIEW.codex_engine_env(
                argparse.Namespace(codex_auth="chatgpt", codex_profile=None),
                repo,
            )
            bedrock_env = AUTOREVIEW.claude_engine_env(
                argparse.Namespace(
                    claude_auth="bedrock",
                    claude_bedrock_region="us-east-1",
                ),
                repo,
            )

        self.assertNotIn("OPENAI_API_KEY", codex_env)
        self.assertNotIn("CLAUDE_CODE_USE_MANTLE", bedrock_env)
        self.assertEqual(bedrock_env["CLAUDE_CODE_USE_BEDROCK"], "1")
        self.assertEqual(bedrock_env["AWS_REGION"], "us-east-1")
        self.assertEqual(bedrock_env["AWS_DEFAULT_REGION"], "us-east-1")
        self.assertEqual(bedrock_env["AWS_BEARER_TOKEN_BEDROCK"], "bearer")

    def test_bedrock_region_is_required_before_review(self) -> None:
        args = argparse.Namespace(
            claude_auth="bedrock",
            claude_bedrock_region=None,
        )
        with mock.patch.dict(
            os.environ,
            {
                "AUTOREVIEW_CLAUDE_BEDROCK_REGION": "",
                "AWS_REGION": "",
                "AWS_DEFAULT_REGION": "",
            },
            clear=False,
        ), self.assertRaisesRegex(SystemExit, "Claude Bedrock auth requires"):
            AUTOREVIEW.claude_bedrock_region(args)

    def test_explicit_claude_auth_requires_empty_setting_sources_version(self) -> None:
        args = self.fable_args()
        version_result = subprocess.CompletedProcess(
            ["claude", "--version"],
            0,
            "2.1.236 (Claude Code)",
            "",
        )
        help_result = subprocess.CompletedProcess(
            ["claude", "--help"],
            0,
            "--safe-mode --setting-sources --strict-mcp-config "
            "--disallowedTools --tools",
            "",
        )

        def probe(command: list[str], *_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return version_result if "--version" in command else help_result

        with tempfile.TemporaryDirectory(
            prefix="autoreview-claude-version-test."
        ) as tmpdir:
            with mock.patch.object(
                AUTOREVIEW,
                "resolve_command",
                return_value="/usr/bin/claude",
            ), mock.patch.object(
                AUTOREVIEW,
                "claude_engine_env",
                return_value={},
            ), mock.patch.object(
                AUTOREVIEW,
                "safe_temp_root",
                return_value=Path(tmpdir),
            ), mock.patch.object(
                AUTOREVIEW,
                "run",
                side_effect=probe,
            ), self.assertRaisesRegex(SystemExit, "2.1.237"):
                AUTOREVIEW.ensure_claude_isolation_supported(args, Path(tmpdir))

    def test_explicit_claude_auth_uses_no_ambient_setting_sources(self) -> None:
        for auth in ("subscription", "bedrock", "mantle"):
            with self.subTest(auth=auth):
                flags = AUTOREVIEW.claude_review_isolation_flags(
                    argparse.Namespace(claude_auth=auth)
                )
                index = flags.index("--setting-sources")
                self.assertEqual(flags[index + 1], "")

    def test_fable_refusal_gets_one_fresh_same_model_retry(self) -> None:
        refusal = subprocess.CompletedProcess(
            ["claude"],
            0,
            json.dumps({"subtype": "model_refusal_no_fallback"}),
            "",
        )
        success = subprocess.CompletedProcess(["claude"], 0, json.dumps(FINAL_REPORT), "")
        args = self.fable_args()
        with mock.patch.object(
            AUTOREVIEW,
            "ensure_claude_isolation_supported",
        ), mock.patch.object(
            AUTOREVIEW,
            "run_claude_once",
            side_effect=[refusal, success],
        ) as run_once, contextlib.redirect_stderr(io.StringIO()):
            output = AUTOREVIEW.run_claude(args, Path.cwd(), "frozen prompt")

        self.assertEqual(output, success.stdout)
        self.assertEqual(run_once.call_count, 2)
        self.assertIs(run_once.call_args_list[0].args[0], args)
        self.assertIs(run_once.call_args_list[1].args[0], args)

    def test_second_fable_refusal_uses_explicit_isolated_fallback(self) -> None:
        refusal = subprocess.CompletedProcess(
            ["claude"],
            0,
            json.dumps({"terminal_reason": "model_refusal"}),
            "",
        )
        success = subprocess.CompletedProcess(["claude"], 0, json.dumps(FINAL_REPORT), "")
        args = self.fable_args()
        observed: list[argparse.Namespace] = []

        def run_once(
            selected: argparse.Namespace,
            _repo: Path,
            _prompt: str,
        ) -> subprocess.CompletedProcess[str]:
            observed.append(copy.copy(selected))
            return [refusal, refusal, success][len(observed) - 1]

        with mock.patch.object(
            AUTOREVIEW,
            "ensure_claude_isolation_supported",
        ), mock.patch.object(
            AUTOREVIEW,
            "run_claude_once",
            side_effect=run_once,
        ), contextlib.redirect_stderr(io.StringIO()):
            output = AUTOREVIEW.run_claude(args, Path.cwd(), "frozen prompt")

        self.assertEqual(output, success.stdout)
        self.assertEqual(len(observed), 3)
        self.assertEqual(
            [(attempt.model, attempt.thinking, attempt.claude_auth) for attempt in observed],
            [
                ("global.anthropic.claude-fable-5-1[1m]", "high", "bedrock"),
                ("global.anthropic.claude-fable-5-1[1m]", "high", "bedrock"),
                ("anthropic.claude-opus-5-5[1m]", "max", "mantle"),
            ],
        )
        self.assertEqual(args.actual_model, "anthropic.claude-opus-5-5[1m]")
        self.assertEqual(args.actual_thinking, "max")
        self.assertEqual(args.actual_claude_auth, "mantle")

    @unittest.skipIf(os.name == "nt", "POSIX mode assertions")
    def test_explicit_codex_profile_is_projected_without_capabilities(self) -> None:
        with tempfile.TemporaryDirectory(prefix="autoreview-codex-profile-test.") as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            codex_home = root / "codex-home"
            runtime_home = root / "runtime-codex-home"
            repo.mkdir()
            codex_home.mkdir()
            (codex_home / "autoreview-bedrock.config.toml").write_text(
                '\n'.join(
                    (
                        'model = "openai.gpt-6-astra"',
                        'model_provider = "amazon-bedrock"',
                        'model_reasoning_effort = "max"',
                        'service_tier = "default"',
                        'developer_instructions = "must not be projected"',
                        '',
                        '[model_providers.amazon-bedrock.aws]',
                        'region = "us-west-2"',
                        '',
                    )
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"CODEX_HOME": str(codex_home)},
                clear=False,
            ):
                staged = AUTOREVIEW.prepare_codex_runtime_profile(
                    argparse.Namespace(codex_profile="autoreview-bedrock"),
                    repo,
                    runtime_home,
                )

            self.assertTrue(staged)
            projected = (
                runtime_home / "autoreview-bedrock.config.toml"
            ).read_text(encoding="utf-8")
            self.assertIn('model = "openai.gpt-6-astra"', projected)
            self.assertIn('model_provider = "amazon-bedrock"', projected)
            self.assertIn('model_reasoning_effort = "max"', projected)
            self.assertIn('region = "us-west-2"', projected)
            self.assertNotIn("developer_instructions", projected)
            self.assertEqual(
                stat.S_IMODE(
                    (runtime_home / "autoreview-bedrock.config.toml").stat().st_mode
                ),
                0o600,
            )

    def test_codex_profile_preflight_uses_isolated_runtime_and_aws_route(self) -> None:
        with tempfile.TemporaryDirectory(prefix="autoreview-codex-preflight-test.") as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            codex_home = root / "codex-home"
            repo.mkdir()
            codex_home.mkdir()
            (codex_home / "autoreview-bedrock.config.toml").write_text(
                '\n'.join(
                    (
                        'model = "openai.gpt-6-astra"',
                        'model_provider = "amazon-bedrock"',
                        'model_reasoning_effort = "max"',
                        '',
                        '[model_providers.amazon-bedrock.aws]',
                        'region = "us-west-2"',
                        '',
                    )
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                codex_auth="default",
                codex_bin="codex",
                codex_profile="autoreview-bedrock",
            )
            observed: dict[str, object] = {}

            def probe(
                command: list[str],
                cwd: Path,
                **kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                observed["command"] = command
                observed["cwd"] = cwd
                observed["env"] = kwargs["env"]
                runtime_home = Path(str(kwargs["env"]["CODEX_HOME"]))
                observed["profile"] = (
                    runtime_home / "autoreview-bedrock.config.toml"
                ).read_text(encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "codex-cli test", "")

            with mock.patch.dict(
                os.environ,
                {
                    "AWS_BEARER_TOKEN_BEDROCK": "test-bearer",
                    "AWS_REGION": "us-west-2",
                    "CODEX_HOME": str(codex_home),
                },
                clear=False,
            ), mock.patch.object(
                AUTOREVIEW,
                "resolve_command",
                return_value="/usr/bin/codex",
            ), mock.patch.object(
                AUTOREVIEW,
                "safe_temp_root",
                return_value=root,
            ), mock.patch.object(AUTOREVIEW, "run", side_effect=probe):
                resolved = AUTOREVIEW.ensure_codex_isolation_supported(args, repo)

            self.assertEqual(resolved, "/usr/bin/codex")
            self.assertEqual(observed["command"], ["/usr/bin/codex", "--version"])
            env = observed["env"]
            self.assertIsInstance(env, dict)
            assert isinstance(env, dict)
            self.assertEqual(env["AWS_BEARER_TOKEN_BEDROCK"], "test-bearer")
            self.assertEqual(env["AWS_REGION"], "us-west-2")
            runtime_home = Path(str(env["CODEX_HOME"]))
            self.assertNotEqual(runtime_home, codex_home)
            self.assertIn('model = "openai.gpt-6-astra"', observed["profile"])


class AutoreviewRunHistoryTests(unittest.TestCase):
    @staticmethod
    def args(**overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "claude_auth": "default",
            "codex_auth": "chatgpt",
            "codex_config": None,
            "codex_profile": None,
            "codex_speed": None,
            "engine": "codex",
            "fallback_model": None,
            "log_bundle": False,
            "model": "gpt-6-astra",
            "no_run_log": False,
            "run_log_dir": None,
            "stream_engine_output": False,
            "thinking": "max",
            "tools": True,
            "web_search": False,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    @unittest.skipIf(os.name == "nt", "POSIX mode assertions")
    def test_run_evidence_is_private_and_history_compatible(self) -> None:
        with tempfile.TemporaryDirectory(prefix="autoreview-history-test.") as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            history = root / "history"
            repo.mkdir()
            args = self.args()
            with mock.patch.object(
                AUTOREVIEW,
                "current_branch",
                return_value="main",
            ), mock.patch.object(AUTOREVIEW, "git", return_value="abc123"):
                evidence = AUTOREVIEW.RunEvidence(
                    args,
                    repo,
                    "local",
                    None,
                    [args],
                    root=history,
                )
                reviewer_index = evidence.start_reviewer(args, 1)
                evidence.finish_reviewer(
                    reviewer_index,
                    status="completed",
                    findings=2,
                )
                evidence.record_bundle(
                    "bundle",
                    ["prompt"],
                    {"source.py"},
                    truncated=False,
                )
                report = copy.deepcopy(DRAFT_REPORT)
                report["findings"].append(copy.deepcopy(DRAFT_REPORT["findings"][0]))
                evidence.record_result(report, "findings")
                evidence.finish(1)

            metadata = json.loads(evidence.metadata_path.read_text(encoding="utf-8"))
            reviewer_run = metadata["reviewer_runs"][0]
            self.assertEqual(stat.S_IMODE(evidence.run_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(evidence.metadata_path.stat().st_mode), 0o600)
            self.assertEqual(metadata["schema_version"], 1)
            self.assertEqual(metadata["target"], {"mode": "local", "ref": None})
            self.assertEqual(metadata["reviewers"][0]["auth"], "chatgpt")
            self.assertEqual(metadata["attempts"][0]["reason"], "primary")
            self.assertEqual(metadata["attempts"][0]["status"], "completed")
            self.assertEqual(
                reviewer_run["finding_disposition"],
                {
                    "confirmed": 0,
                    "false_positive": 0,
                    "not_actionable": 0,
                    "pending": 2,
                },
            )

    @unittest.skipIf(os.name == "nt", "POSIX mode assertions")
    def test_bundle_history_is_explicit_and_saves_exact_scanned_inputs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="autoreview-history-bundle-test.") as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            history = root / "history"
            repo.mkdir()
            args = self.args(log_bundle=True)
            with mock.patch.object(
                AUTOREVIEW,
                "current_branch",
                return_value="main",
            ), mock.patch.object(AUTOREVIEW, "git", return_value="abc123"):
                evidence = AUTOREVIEW.RunEvidence(
                    args,
                    repo,
                    "commit",
                    "abc123",
                    [args],
                    root=history,
                )
                evidence.record_bundle(
                    "exact frozen bundle\n",
                    ["exact scanned prompt\n"],
                    {"source.py"},
                    truncated=False,
                )
                evidence.record_result(FINAL_REPORT, "clean")
                evidence.finish(0)

            self.assertEqual(
                (evidence.run_dir / "bundle.txt").read_text(encoding="utf-8"),
                "exact frozen bundle\n",
            )
            self.assertEqual(
                json.loads((evidence.run_dir / "prompts.json").read_text(encoding="utf-8")),
                ["exact scanned prompt\n"],
            )
            self.assertEqual(
                json.loads((evidence.run_dir / "report.json").read_text(encoding="utf-8")),
                FINAL_REPORT,
            )
            for name in ("bundle.txt", "prompts.json", "report.json"):
                self.assertEqual(
                    stat.S_IMODE((evidence.run_dir / name).stat().st_mode),
                    0o600,
                )

    def test_default_history_overlap_uses_private_fallback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="autoreview-history-overlap-test.") as tmpdir:
            repo = Path(tmpdir)
            args = self.args()
            with mock.patch.dict(
                os.environ,
                {"AUTOREVIEW_RUN_LOG_DIR": ""},
                clear=False,
            ), mock.patch.object(
                AUTOREVIEW,
                "default_run_log_root",
                return_value=repo / ".state" / "autoreview",
            ), mock.patch.object(
                AUTOREVIEW,
                "current_branch",
                return_value="main",
            ), mock.patch.object(AUTOREVIEW, "git", return_value="abc123"):
                evidence = AUTOREVIEW.create_run_evidence(
                    args,
                    repo,
                    "local",
                    None,
                    [args],
                )

            self.assertIsNotNone(evidence)
            assert evidence is not None
            self.assertFalse(AUTOREVIEW.path_overlaps_repo(evidence.root, repo))

    def test_invalid_bundle_logging_config_is_rejected(self) -> None:
        args = self.args(log_bundle=None)
        with mock.patch.dict(
            os.environ,
            {"AUTOREVIEW_RUN_LOG_BUNDLE": "maybe"},
            clear=False,
        ), self.assertRaisesRegex(SystemExit, "invalid AUTOREVIEW_RUN_LOG_BUNDLE"):
            AUTOREVIEW.run_log_bundle_enabled(args)


COMPLETE_REPORT = json.dumps({**FINAL_REPORT, "review_completion": "complete"})


def route_reviewer(argv: list[str], env: dict[str, str] | None = None) -> argparse.Namespace:
    with mock.patch.dict(os.environ, env or {}, clear=True), mock.patch.object(
        sys, "argv", ["autoreview", *argv],
    ):
        return AUTOREVIEW.reviewer_args(AUTOREVIEW.parse_args())[0]


def reviewer_failure(message: str, *, reason: str = "engine_failed", returncode: int = 1,
                     timed_out: bool = False) -> AUTOREVIEW.ReviewerUnavailable:
    result_type = AUTOREVIEW.TimedOutEngineProcess if timed_out else subprocess.CompletedProcess
    return AUTOREVIEW.ReviewerUnavailable(
        message, reason=reason, result=result_type(["engine"], returncode, "", ""),
    )


MANTLE_ENV = {
    "AUTOREVIEW_CLAUDE_AUTH": "mantle",
    "AUTOREVIEW_CLAUDE_BEDROCK_REGION": "us-east-1",
    "AUTOREVIEW_CLAUDE_FALLBACK_AUTH": "subscription",
}
CLAUDE_MANTLE_ARGV = ["--engine", "claude", "--mode", "local"]
CODEX_PROFILE_ARGV = [
    "--engine", "codex", "--codex-profile", "autoreview-bedrock",
    "--model", "openai.gpt-6-sol", "--thinking", "max",
]


class AutoreviewRouteFallbackTests(unittest.TestCase):
    def route_engine(self, *outcomes: object) -> tuple[mock.Mock, list[tuple[object, ...]]]:
        observed: list[tuple[object, ...]] = []
        pending = list(outcomes)

        def fake_run_engine(reviewer: argparse.Namespace, _repo: Path, _prompt: str) -> str:
            observed.append((
                AUTOREVIEW.reviewer_route(reviewer),
                reviewer.model,
                reviewer.thinking,
            ))
            outcome = pending.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return str(outcome)

        return mock.Mock(side_effect=fake_run_engine), observed

    def test_default_mantle_route_is_opus_5_5_xhigh(self) -> None:
        reviewer = route_reviewer(CLAUDE_MANTLE_ARGV, MANTLE_ENV)
        self.assertEqual(
            (reviewer.claude_auth, reviewer.model, reviewer.thinking),
            ("mantle", "anthropic.claude-opus-5-5[1m]", "xhigh"),
        )
        bare = route_reviewer(
            [*CLAUDE_MANTLE_ARGV, "--model", "claude-opus-5-5"], MANTLE_ENV,
        )
        self.assertEqual(bare.model, "anthropic.claude-opus-5-5")

    def test_claude_mantle_failure_retries_subscription_with_derived_model(self) -> None:
        reviewer = route_reviewer(CLAUDE_MANTLE_ARGV, MANTLE_ENV)
        engine, observed = self.route_engine(
            reviewer_failure("claude engine failed (1)\nsynthetic-provider-log token=abc"),
            COMPLETE_REPORT,
        )
        stderr = io.StringIO()
        with mock.patch.object(AUTOREVIEW, "run_engine", engine), mock.patch.object(
            AUTOREVIEW, "scan_outgoing_review_pack",
        ), contextlib.redirect_stderr(stderr):
            result = AUTOREVIEW.run_reviewer(reviewer, Path.cwd(), "frozen", set(), [])

        self.assertTrue(result.complete)
        self.assertEqual(observed, [
            ("mantle", "anthropic.claude-opus-5-5[1m]", "xhigh"),
            ("subscription", "claude-opus-5-5", "xhigh"),
        ])
        self.assertIn(
            "claude route mantle failed (engine_failed: claude engine failed (1)); "
            "retrying on subscription with claude-opus-5-5",
            stderr.getvalue(),
        )
        self.assertNotIn("synthetic-provider-log", stderr.getvalue())
        fallback = reviewer.route_fallback
        self.assertIsNone(fallback.fallback_model)
        self.assertIsNone(fallback.route_fallback)
        self.assertEqual(reviewer.claude_auth, "mantle")

    def test_claude_route_model_derivation(self) -> None:
        cases = (
            ("anthropic.claude-opus-5-5[1m]", "subscription", "claude-opus-5-5"),
            ("global.anthropic.claude-fable-5-1[1m]", "subscription", "claude-fable-5-1"),
            ("us.anthropic.claude-opus-5", "default", "claude-opus-5"),
            ("us-gov.anthropic.claude-opus-5-5", "subscription", "claude-opus-5-5"),
            ("claude-opus-5-5", "mantle", "anthropic.claude-opus-5-5"),
            ("global.anthropic.claude-opus-5-5[1m]", "mantle", "anthropic.claude-opus-5-5[1m]"),
            ("anthropic.claude-fable-5-1[1m]", "bedrock", "global.anthropic.claude-fable-5-1[1m]"),
            ("custom-model", "mantle", "custom-model"),
        )
        for model, auth, expected in cases:
            with self.subTest(model=model, auth=auth):
                self.assertEqual(AUTOREVIEW.claude_route_model(model, auth), expected)

    def test_codex_profile_fallback_drops_profile_and_provider_route(self) -> None:
        reviewer = route_reviewer(
            [*CODEX_PROFILE_ARGV, "--codex-fallback-auth", "chatgpt"],
            {"AUTOREVIEW_CODEX_CONFIG": "model_verbosity=\"low\""},
        )
        fallback = reviewer.route_fallback
        self.assertEqual(
            (fallback.codex_auth, fallback.codex_profile, fallback.model, fallback.thinking),
            ("chatgpt", None, "gpt-6-sol", "max"),
        )
        # The ChatGPT route keeps explicit GPT-6 Sol's access-only Luna retry.
        self.assertEqual(fallback.fallback_model, "gpt-6-luna")
        route = AUTOREVIEW.RunEvidence.reviewer_metadata(fallback)
        self.assertEqual((route["auth"], route["profile"], route["output_schema"]),
                         ("chatgpt", None, True))
        with mock.patch.dict(
            os.environ,
            {"AUTOREVIEW_CODEX_CONFIG": 'model_provider="review_api"'},
            clear=True,
        ):
            self.assertEqual(AUTOREVIEW.codex_config_overrides(fallback), ['model_verbosity="low"'])

        provider_primary = route_reviewer(
            ["--engine", "codex", "--codex-config", 'model_provider="review_api"',
             "--codex-config", 'model_verbosity="low"', "--codex-fallback-auth", "chatgpt"],
        )
        provider_fallback = provider_primary.route_fallback
        self.assertEqual(provider_fallback.model, AUTOREVIEW.DEFAULT_MODEL_BY_ENGINE["codex"])
        self.assertEqual(provider_fallback.fallback_model, AUTOREVIEW.DEFAULT_CODEX_ACCESS_FALLBACK_MODEL)
        self.assertNotIn("model_provider", AUTOREVIEW.codex_config_keys(provider_fallback))

        env = {"OPENAI_API_KEY": "synthetic", "AWS_BEARER_TOKEN_BEDROCK": "synthetic"}
        with tempfile.TemporaryDirectory(prefix="autoreview-route-fallback.") as tmpdir, \
                mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(AUTOREVIEW, "resolve_command", return_value="/usr/bin/codex"), \
                mock.patch.object(AUTOREVIEW, "safe_engine_env",
                                  side_effect=lambda *_a, **_k: dict(env)):
            root = Path(tmpdir)
            command = AUTOREVIEW.codex_command(
                fallback, root, root, root, root / "schema.json", root / "out.json",
                fallback.model, auth_config=[],
            )
            engine_env = AUTOREVIEW.codex_engine_env(fallback, root)
        self.assertNotIn("--profile", command)
        self.assertIn("--output-schema", command)
        self.assertEqual(command[command.index("--model") + 1], "gpt-6-sol")
        self.assertFalse(any(part.startswith("model_provider=") for part in command))
        self.assertNotIn("OPENAI_API_KEY", engine_env)

    @unittest.skipIf(os.name == "nt", "POSIX profile staging")
    def test_codex_profile_failure_runs_chatgpt_route_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory(prefix="autoreview-route-codex.") as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            codex_home = root / "codex-home"
            repo.mkdir()
            codex_home.mkdir()
            (codex_home / "autoreview-bedrock.config.toml").write_text(
                'model = "openai.gpt-6-sol"\nmodel_provider = "amazon-bedrock"\n',
                encoding="utf-8",
            )
            reviewer = route_reviewer(
                CODEX_PROFILE_ARGV, {"AUTOREVIEW_CODEX_FALLBACK_AUTH": "chatgpt"},
            )
            reviewer.stream_engine_output = False
            events: list[tuple[bool, str, bool]] = []

            def fake_run(command, _cwd, **kwargs):
                profile = "--profile" in command
                events.append((
                    profile,
                    command[command.index("--model") + 1] if "--model" in command else "(profile)",
                    "AWS_BEARER_TOKEN_BEDROCK" in kwargs["env"],
                ))
                if profile:
                    return subprocess.CompletedProcess(
                        command, 1, "", "HTTP 403 Forbidden synthetic-provider-log",
                    )
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(COMPLETE_REPORT, encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            stderr = io.StringIO()
            with mock.patch.dict(os.environ, {
                "AWS_BEARER_TOKEN_BEDROCK": "synthetic-bearer",
                "CODEX_HOME": str(codex_home),
                "HOME": str(root),
                "PATH": os.environ.get("PATH", ""),
            }, clear=True), mock.patch.object(
                AUTOREVIEW, "resolve_command", return_value="/usr/bin/codex",
            ), mock.patch.object(
                AUTOREVIEW, "ensure_codex_isolation_supported", return_value="/usr/bin/codex",
            ), mock.patch.object(
                AUTOREVIEW, "codex_auth_config_flags", return_value=[],
            ), mock.patch.object(
                AUTOREVIEW, "prepare_codex_runtime_auth", return_value=None,
            ), mock.patch.object(
                AUTOREVIEW, "safe_temp_root", return_value=root,
            ), mock.patch.object(
                AUTOREVIEW, "scan_outgoing_review_pack",
            ), mock.patch.object(
                AUTOREVIEW, "run_with_heartbeat", side_effect=fake_run,
            ), contextlib.redirect_stderr(stderr):
                result = AUTOREVIEW.run_reviewer(reviewer, repo, "frozen", set(), [])

        self.assertTrue(result.complete)
        self.assertEqual(events, [
            (True, "openai.gpt-6-sol", True),
            (False, "gpt-6-sol", False),
        ])
        self.assertIn(
            "codex route profile=autoreview-bedrock failed (engine_failed: codex engine failed: "
            "provider-access; provider diagnostics suppressed); retrying on chatgpt with gpt-6-sol",
            stderr.getvalue(),
        )
        self.assertNotIn("synthetic-provider-log", stderr.getvalue())

    def test_codex_runtime_profile_fallback_keeps_bedrock_route(self) -> None:
        reviewer = route_reviewer(
            [*CODEX_PROFILE_ARGV, "--codex-fallback-profile", "autoreview-bedrock-runtime"],
            {"AUTOREVIEW_CODEX_CONFIG": 'model_verbosity="low"'},
        )
        fallback = reviewer.route_fallback
        self.assertEqual(
            (fallback.codex_auth, fallback.codex_profile, fallback.model, fallback.thinking),
            ("default", "autoreview-bedrock-runtime", None, "max"),
        )
        self.assertIsNone(fallback.fallback_model)
        self.assertIsNone(fallback.route_fallback)
        self.assertEqual(AUTOREVIEW.reviewer_route(fallback), "profile=autoreview-bedrock-runtime")
        self.assertEqual(reviewer.codex_profile, "autoreview-bedrock")

        env = {"OPENAI_API_KEY": "synthetic", "AWS_BEARER_TOKEN_BEDROCK": "synthetic"}
        with tempfile.TemporaryDirectory(prefix="autoreview-route-runtime.") as tmpdir, \
                mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(AUTOREVIEW, "resolve_command", return_value="/usr/bin/codex"), \
                mock.patch.object(AUTOREVIEW, "safe_engine_env",
                                  side_effect=lambda *_a, **_k: dict(env)):
            root = Path(tmpdir)
            command = AUTOREVIEW.codex_command(
                fallback, root, root, root, root / "schema.json", root / "out.json",
                fallback.model, auth_config=[],
            )
            engine_env = AUTOREVIEW.codex_engine_env(fallback, root)
        self.assertEqual(command[command.index("--profile") + 1], "autoreview-bedrock-runtime")
        # The fallback profile owns its model id; the primary's Mantle id is not carried over.
        self.assertNotIn("--model", command)
        self.assertFalse(any(part.startswith("model_provider=") for part in command))
        self.assertIn("model_verbosity=\"low\"", command)
        self.assertEqual(engine_env.get("AWS_BEARER_TOKEN_BEDROCK"), "synthetic")

        env_reviewer = route_reviewer(CODEX_PROFILE_ARGV, {
            "AUTOREVIEW_CODEX_FALLBACK_PROFILE": "autoreview-bedrock-runtime",
            "AUTOREVIEW_CODEX_FALLBACK_AUTH_MODEL": "us.openai.gpt-6-sol",
        })
        self.assertEqual(
            (env_reviewer.route_fallback.codex_profile, env_reviewer.route_fallback.model),
            ("autoreview-bedrock-runtime", "us.openai.gpt-6-sol"),
        )

    def test_codex_profile_fallback_model_is_the_profile_or_explicit_id(self) -> None:
        # Mantle -> Mantle (another region) keeps no rewritten id: the profile decides.
        mantle = route_reviewer(CODEX_PROFILE_ARGV, {
            "AUTOREVIEW_CODEX_FALLBACK_PROFILE": "autoreview-bedrock-west",
        })
        self.assertEqual(
            (mantle.route_fallback.codex_profile, mantle.route_fallback.model),
            ("autoreview-bedrock-west", None),
        )
        # An explicit id is passed through untouched, whatever its geo prefix.
        for explicit in ("eu.openai.gpt-6-sol", "global.openai.gpt-6-sol", "openai.gpt-6-sol"):
            with self.subTest(explicit=explicit):
                reviewer = route_reviewer(CODEX_PROFILE_ARGV, {
                    "AUTOREVIEW_CODEX_FALLBACK_PROFILE": "autoreview-bedrock-runtime",
                    "AUTOREVIEW_CODEX_FALLBACK_AUTH_MODEL": explicit,
                })
                self.assertEqual(reviewer.route_fallback.model, explicit)

    @unittest.skipIf(os.name == "nt", "POSIX profile staging")
    def test_codex_mantle_failure_runs_runtime_profile_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory(prefix="autoreview-route-runtime-e2e.") as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            codex_home = root / "codex-home"
            repo.mkdir()
            codex_home.mkdir()
            (codex_home / "autoreview-bedrock.config.toml").write_text(
                'model = "openai.gpt-6-sol"\nmodel_provider = "amazon-bedrock"\n'
                '\n[model_providers.amazon-bedrock.aws]\nregion = "us-east-1"\n',
                encoding="utf-8",
            )
            (codex_home / "autoreview-bedrock-runtime.config.toml").write_text(
                'model = "global.openai.gpt-6-sol"\nmodel_provider = "amazon-bedrock-runtime"\n'
                'model_reasoning_effort = "max"\nservice_tier = "default"\n'
                'approvals_reviewer = "user"\n'
                '\n[model_providers.amazon-bedrock-runtime.aws]\nregion = "us-east-1"\n',
                encoding="utf-8",
            )
            reviewer = route_reviewer(
                CODEX_PROFILE_ARGV,
                {"AUTOREVIEW_CODEX_FALLBACK_PROFILE": "autoreview-bedrock-runtime"},
            )
            reviewer.stream_engine_output = False
            events: list[tuple[str, str, bool, str]] = []

            def fake_run(command, _cwd, **kwargs):
                profile = command[command.index("--profile") + 1]
                staged = Path(kwargs["env"]["CODEX_HOME"]) / f"{profile}.config.toml"
                events.append((
                    profile,
                    command[command.index("--model") + 1] if "--model" in command else "(profile)",
                    kwargs["env"].get("AWS_BEARER_TOKEN_BEDROCK") == "synthetic-bearer",
                    staged.read_text(encoding="utf-8"),
                ))
                if profile == "autoreview-bedrock":
                    return subprocess.CompletedProcess(
                        command, 1, "", "HTTP 529 Overloaded synthetic-provider-log",
                    )
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(COMPLETE_REPORT, encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            stderr = io.StringIO()
            with mock.patch.dict(os.environ, {
                "AWS_BEARER_TOKEN_BEDROCK": "synthetic-bearer",
                "CODEX_HOME": str(codex_home),
                "HOME": str(root),
                "PATH": os.environ.get("PATH", ""),
            }, clear=True), mock.patch.object(
                AUTOREVIEW, "resolve_command", return_value="/usr/bin/codex",
            ), mock.patch.object(
                AUTOREVIEW, "ensure_codex_isolation_supported", return_value="/usr/bin/codex",
            ), mock.patch.object(
                AUTOREVIEW, "codex_auth_config_flags", return_value=[],
            ), mock.patch.object(
                AUTOREVIEW, "prepare_codex_runtime_auth", return_value=None,
            ), mock.patch.object(
                AUTOREVIEW, "safe_temp_root", return_value=root,
            ), mock.patch.object(
                AUTOREVIEW, "scan_outgoing_review_pack",
            ), mock.patch.object(
                AUTOREVIEW, "run_with_heartbeat", side_effect=fake_run,
            ), contextlib.redirect_stderr(stderr):
                result = AUTOREVIEW.run_reviewer(reviewer, repo, "frozen", set(), [])

        self.assertTrue(result.complete)
        self.assertEqual([event[:3] for event in events], [
            ("autoreview-bedrock", "openai.gpt-6-sol", True),
            ("autoreview-bedrock-runtime", "(profile)", True),
        ])
        staged_runtime = events[1][3]
        self.assertIn('model = "global.openai.gpt-6-sol"', staged_runtime)
        self.assertIn('model_provider = "amazon-bedrock-runtime"', staged_runtime)
        self.assertIn("[model_providers.amazon-bedrock-runtime.aws]", staged_runtime)
        self.assertIn('region = "us-east-1"', staged_runtime)
        self.assertNotIn("approvals_reviewer", staged_runtime)
        self.assertIn(
            "retrying on profile=autoreview-bedrock-runtime with the profile model",
            stderr.getvalue(),
        )
        self.assertNotIn("synthetic-provider-log", stderr.getvalue())

    def test_codex_fallback_profile_configuration(self) -> None:
        same = route_reviewer(CODEX_PROFILE_ARGV, {
            "AUTOREVIEW_CODEX_FALLBACK_PROFILE": "autoreview-bedrock",
        })
        self.assertIsNone(same.route_fallback)
        for argv, env, message in (
            ([*CODEX_PROFILE_ARGV, "--codex-fallback-profile", "autoreview-bedrock-runtime",
              "--codex-fallback-auth", "chatgpt"], {},
             "--codex-fallback-auth and --codex-fallback-profile are exclusive"),
            ([*CLAUDE_MANTLE_ARGV, "--codex-fallback-profile", "autoreview-bedrock-runtime"],
             MANTLE_ENV, "--codex-fallback-profile is only supported for codex"),
            ([*CODEX_PROFILE_ARGV, "--codex-fallback-profile", "../escape"], {},
             "invalid Codex profile"),
            ([*CODEX_PROFILE_ARGV[:-2], "--thinking", "minimal",
              "--codex-fallback-auth-model", "gpt-6-astra"],
             {"AUTOREVIEW_CODEX_FALLBACK_PROFILE": "autoreview-bedrock-runtime"},
             "invalid thinking level for codex model gpt-6-astra"),
        ):
            with self.subTest(argv=argv), self.assertRaisesRegex(SystemExit, message):
                route_reviewer(argv, env)

    def test_route_fallback_runs_after_same_route_refusal_policy(self) -> None:
        refusal = subprocess.CompletedProcess(
            ["claude"], 0, json.dumps({"terminal_reason": "model_refusal"}), "",
        )
        failure = subprocess.CompletedProcess(["claude"], 1, "", "synthetic-provider-log")
        success = subprocess.CompletedProcess(["claude"], 0, COMPLETE_REPORT, "")
        for primary_model, outcomes, expected in (
            (
                "anthropic.claude-fable-5-1[1m]",
                [refusal, refusal, failure, success],
                [
                    ("mantle", "anthropic.claude-fable-5-1[1m]", "xhigh"),
                    ("mantle", "anthropic.claude-fable-5-1[1m]", "xhigh"),
                    ("mantle", "anthropic.claude-opus-5-5[1m]", "max"),
                    ("subscription", "claude-fable-5-1", "xhigh"),
                ],
            ),
            (
                "anthropic.claude-opus-5-5[1m]",
                [refusal, success],
                [
                    ("mantle", "anthropic.claude-opus-5-5[1m]", "xhigh"),
                    ("subscription", "claude-opus-5-5", "xhigh"),
                ],
            ),
        ):
            with self.subTest(model=primary_model):
                reviewer = route_reviewer(
                    [*CLAUDE_MANTLE_ARGV, "--model", primary_model], MANTLE_ENV,
                )
                observed: list[tuple[str, str, str]] = []
                pending = list(outcomes)

                def run_once(selected, _repo, _prompt):
                    observed.append((selected.claude_auth, selected.model, selected.thinking))
                    return pending.pop(0)

                with mock.patch.object(
                    AUTOREVIEW, "ensure_claude_isolation_supported",
                ), mock.patch.object(
                    AUTOREVIEW, "run_claude_once", side_effect=run_once,
                ), mock.patch.object(
                    AUTOREVIEW, "scan_outgoing_review_pack",
                ), contextlib.redirect_stderr(io.StringIO()):
                    result = AUTOREVIEW.run_reviewer(reviewer, Path.cwd(), "frozen", set(), [])
                self.assertTrue(result.complete)
                self.assertEqual(observed, expected)

    def test_no_route_fallback_when_unset_or_same_route(self) -> None:
        for argv, env in (
            (CLAUDE_MANTLE_ARGV, {**MANTLE_ENV, "AUTOREVIEW_CLAUDE_FALLBACK_AUTH": ""}),
            ([*CLAUDE_MANTLE_ARGV, "--claude-auth", "subscription"], MANTLE_ENV),
            (CODEX_PROFILE_ARGV, {}),
            (["--engine", "codex", "--codex-auth", "chatgpt"],
             {"AUTOREVIEW_CODEX_FALLBACK_AUTH": "chatgpt"}),
            (["--engine", "pi"], {"AUTOREVIEW_CLAUDE_FALLBACK_AUTH": "subscription",
                                  "AUTOREVIEW_CODEX_FALLBACK_AUTH": "chatgpt"}),
        ):
            with self.subTest(argv=argv):
                reviewer = route_reviewer(argv, env)
                self.assertIsNone(reviewer.route_fallback)
                engine, observed = self.route_engine(reviewer_failure("engine failed (1)"))
                with mock.patch.object(AUTOREVIEW, "run_engine", engine), mock.patch.object(
                    AUTOREVIEW, "scan_outgoing_review_pack",
                ), self.assertRaises(AUTOREVIEW.ReviewerUnavailable):
                    AUTOREVIEW.run_reviewer(reviewer, Path.cwd(), "frozen", set(), [])
                self.assertEqual(len(observed), 1)

    def test_no_route_fallback_for_interrupt_scan_mutation_setup_or_verdict(self) -> None:
        reviewer = route_reviewer(CLAUDE_MANTLE_ARGV, MANTLE_ENV)
        self.assertIsNotNone(reviewer.route_fallback)
        incomplete = json.dumps({**FINAL_REPORT, "review_completion": "incomplete"})
        for label, outcome, expected in (
            ("interrupt", AUTOREVIEW.EngineInterrupted(130), AUTOREVIEW.EngineInterrupted),
            ("isolation", SystemExit("claude isolation probe failed"), SystemExit),
        ):
            with self.subTest(label=label):
                engine, observed = self.route_engine(outcome)
                with mock.patch.object(AUTOREVIEW, "run_engine", engine), mock.patch.object(
                    AUTOREVIEW, "scan_outgoing_review_pack",
                ), self.assertRaises(expected) as caught:
                    AUTOREVIEW.run_reviewer(reviewer, Path.cwd(), "frozen", set(), [])
                self.assertNotIsInstance(caught.exception, AUTOREVIEW.ReviewerUnavailable)
                self.assertEqual(len(observed), 1)

        engine, observed = self.route_engine(incomplete)
        with mock.patch.object(AUTOREVIEW, "run_engine", engine), mock.patch.object(
            AUTOREVIEW, "scan_outgoing_review_pack",
        ):
            result = AUTOREVIEW.run_reviewer(reviewer, Path.cwd(), "frozen", set(), [])
        self.assertFalse(result.complete)
        self.assertEqual(len(observed), 1)

        engine, observed = self.route_engine(COMPLETE_REPORT)
        with mock.patch.object(AUTOREVIEW, "run_engine", engine), mock.patch.object(
            AUTOREVIEW, "scan_outgoing_review_pack",
            side_effect=SystemExit("refusing to send review pack: config.ts"),
        ), self.assertRaisesRegex(SystemExit, "config.ts"):
            AUTOREVIEW.run_reviewer(reviewer, Path.cwd(), "frozen", set(), [])
        self.assertEqual(observed, [])

        engine, observed = self.route_engine(
            reviewer_failure("claude engine failed (1)"), COMPLETE_REPORT,
        )
        with mock.patch.object(AUTOREVIEW, "run_engine", engine), mock.patch.object(
            AUTOREVIEW, "scan_outgoing_review_pack",
        ), mock.patch.object(
            AUTOREVIEW, "verify_evidence",
            side_effect=[None, SystemExit("evidence changed after capture: notes.md")],
        ), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            AUTOREVIEW.run_reviewer(reviewer, Path.cwd(), "frozen", set(), [])
        self.assertNotIsInstance(caught.exception, AUTOREVIEW.ReviewerUnavailable)
        self.assertIn("evidence changed", str(caught.exception))
        self.assertEqual(len(observed), 1)

    def test_invalid_primary_report_and_timeout_trigger_route_fallback(self) -> None:
        reviewer = route_reviewer(CLAUDE_MANTLE_ARGV, MANTLE_ENV)
        for label, primary in (
            ("empty", ""),
            ("timeout", reviewer_failure("claude engine failed (124)", returncode=124, timed_out=True)),
        ):
            with self.subTest(label=label):
                engine, observed = self.route_engine(primary, COMPLETE_REPORT)
                stderr = io.StringIO()
                with mock.patch.object(AUTOREVIEW, "run_engine", engine), mock.patch.object(
                    AUTOREVIEW, "scan_outgoing_review_pack",
                ), contextlib.redirect_stderr(stderr):
                    result = AUTOREVIEW.run_reviewer(reviewer, Path.cwd(), "frozen", set(), [])
                self.assertTrue(result.complete)
                self.assertEqual(len(observed), 2)
                expected = (
                    "invalid_report: review engine returned empty output"
                    if label == "empty"
                    else "engine_failed: claude engine failed (124) (timed out)"
                )
                self.assertIn(f"claude route mantle failed ({expected})", stderr.getvalue())

    def test_fallback_failure_surfaces_both_summaries_in_stable_envelope(self) -> None:
        reviewer = route_reviewer(CLAUDE_MANTLE_ARGV, MANTLE_ENV)
        engine, _observed = self.route_engine(
            reviewer_failure("claude engine failed (1)\nsynthetic-primary-log"),
            reviewer_failure(
                "claude engine refused the requested model 'claude-opus-5-5'",
                reason="model_refusal", returncode=0, timed_out=True,
            ),
        )
        with mock.patch.object(AUTOREVIEW, "run_engine", engine), mock.patch.object(
            AUTOREVIEW, "scan_outgoing_review_pack",
        ), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(
            AUTOREVIEW.ReviewerUnavailable,
        ) as caught:
            AUTOREVIEW.run_reviewer(reviewer, Path.cwd(), "frozen", set(), [])

        failure = caught.exception
        message = str(failure)
        self.assertIn("refused the requested model 'claude-opus-5-5'", message)
        self.assertIn(
            "claude primary route mantle failed first: engine_failed: claude engine failed (1)",
            message,
        )
        self.assertNotIn("synthetic-primary-log", message)
        self.assertEqual((failure.reason, failure.returncode, failure.timed_out),
                         ("model_refusal", 0, True))
        with tempfile.TemporaryDirectory(prefix="autoreview-route-status.") as tmpdir:
            status_path = Path(tmpdir) / "status.json"
            status_args = argparse.Namespace(status_output=str(status_path), engine="claude")
            AUTOREVIEW.write_review_status(status_args, "reviewer_unavailable", 1, failure)
            envelope = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(envelope, {
            "schema_version": 1,
            "status": "reviewer_unavailable",
            "exit_code": 1,
            "engine": "claude",
            "report_produced": False,
            "reason": "model_refusal",
            "reviewer_exit_code": 0,
            "timed_out": True,
        })

    def test_run_evidence_records_route_fallback_attempt(self) -> None:
        reviewer = route_reviewer(CLAUDE_MANTLE_ARGV, MANTLE_ENV)
        engine, _observed = self.route_engine(
            reviewer_failure("claude engine failed (7)", returncode=7), COMPLETE_REPORT,
        )
        with tempfile.TemporaryDirectory(prefix="autoreview-route-history.") as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                AUTOREVIEW, "current_branch", return_value="main",
            ), mock.patch.object(AUTOREVIEW, "git", return_value="abc123"):
                evidence = AUTOREVIEW.RunEvidence(
                    reviewer, repo, "local", None, [reviewer], root=root / "history",
                )
            reviewer.run_evidence = evidence
            with mock.patch.object(AUTOREVIEW, "run_engine", engine), mock.patch.object(
                AUTOREVIEW, "scan_outgoing_review_pack",
            ), contextlib.redirect_stderr(io.StringIO()):
                AUTOREVIEW.run_reviewer(reviewer, repo, "frozen", set(), [])
            metadata = json.loads(evidence.metadata_path.read_text(encoding="utf-8"))

        self.assertEqual(metadata["reviewers"][0]["route_fallback"], {
            "auth": "subscription", "profile": None,
            "model": "claude-opus-5-5", "thinking": "xhigh",
        })
        attempts = [
            (attempt["reason"], attempt["status"], attempt["auth"], attempt["model"],
             attempt["thinking"], attempt["returncode"])
            for attempt in metadata["attempts"]
        ]
        self.assertEqual(attempts, [
            ("primary", "failed", "mantle", "anthropic.claude-opus-5-5[1m]", "xhigh", 7),
            ("route_fallback", "completed", "subscription", "claude-opus-5-5", "xhigh", 0),
        ])
        self.assertNotIn("region", metadata["attempts"][1])
        self.assertFalse(metadata["attempts"][0]["refusal"])
        run = metadata["reviewer_runs"][0]
        self.assertEqual(
            (run["status"], run["auth"], run["model"], run["thinking"]),
            ("completed", "subscription", "claude-opus-5-5", "xhigh"),
        )
        self.assertNotIn("region", run)
        self.assertEqual(run["route_fallback_from"], {
            "route": "mantle", "model": "anthropic.claude-opus-5-5[1m]", "thinking": "xhigh",
        })

    def test_run_evidence_marks_primary_refusal_before_route_fallback(self) -> None:
        reviewer = route_reviewer(CLAUDE_MANTLE_ARGV, MANTLE_ENV)
        engine, _observed = self.route_engine(
            reviewer_failure("claude engine refused the requested model", reason="model_refusal",
                             returncode=0),
            COMPLETE_REPORT,
        )
        with tempfile.TemporaryDirectory(prefix="autoreview-route-refusal.") as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                AUTOREVIEW, "current_branch", return_value="main",
            ), mock.patch.object(AUTOREVIEW, "git", return_value="abc123"):
                evidence = AUTOREVIEW.RunEvidence(
                    reviewer, repo, "local", None, [reviewer], root=root / "history",
                )
            reviewer.run_evidence = evidence
            with mock.patch.object(AUTOREVIEW, "run_engine", engine), mock.patch.object(
                AUTOREVIEW, "scan_outgoing_review_pack",
            ), contextlib.redirect_stderr(io.StringIO()):
                AUTOREVIEW.run_reviewer(reviewer, repo, "frozen", set(), [])
            metadata = json.loads(evidence.metadata_path.read_text(encoding="utf-8"))
        first = metadata["attempts"][0]
        self.assertEqual((first["status"], first["refusal"], first["returncode"]),
                         ("refused", True, 0))
        self.assertEqual(metadata["attempts"][1]["reason"], "route_fallback")

    def test_later_passes_stay_on_exhausted_fallback_route(self) -> None:
        reviewer = route_reviewer(CLAUDE_MANTLE_ARGV, MANTLE_ENV)
        engine, observed = self.route_engine(
            reviewer_failure("claude engine failed (1)"), COMPLETE_REPORT, COMPLETE_REPORT,
        )
        with mock.patch.object(AUTOREVIEW, "run_engine", engine), contextlib.redirect_stderr(
            io.StringIO(),
        ), contextlib.redirect_stdout(io.StringIO()):
            results = AUTOREVIEW.run_review_passes(
                reviewer, [reviewer], Path.cwd(), ["pass one", "pass two"], set(),
            )
        self.assertEqual(len(results), 2)
        self.assertEqual([route for route, _model, _thinking in observed],
                         ["mantle", "subscription", "subscription"])

    def test_env_and_explicit_fallback_models_override_derivation(self) -> None:
        env_claude = route_reviewer(CLAUDE_MANTLE_ARGV, {
            **MANTLE_ENV, "AUTOREVIEW_CLAUDE_FALLBACK_AUTH_MODEL": "claude-fable-5-1",
        })
        self.assertEqual(env_claude.route_fallback.model, "claude-fable-5-1")
        flag_claude = route_reviewer(
            [*CLAUDE_MANTLE_ARGV, "--claude-fallback-auth", "bedrock",
             "--claude-fallback-auth-model", "global.anthropic.claude-opus-5-5"],  # gitleaks:allow (model id)
            MANTLE_ENV,
        )
        self.assertEqual(
            (flag_claude.route_fallback.claude_auth, flag_claude.route_fallback.model),
            ("bedrock", "global.anthropic.claude-opus-5-5"),
        )
        env_codex = route_reviewer(CODEX_PROFILE_ARGV, {
            "AUTOREVIEW_CODEX_FALLBACK_AUTH": "chatgpt",
            "AUTOREVIEW_CODEX_FALLBACK_AUTH_MODEL": "gpt-6-astra",
        })
        self.assertEqual(
            (env_codex.route_fallback.model, env_codex.route_fallback.thinking),
            ("gpt-6-astra", "max"),
        )
        flag_codex = route_reviewer(
            [*CODEX_PROFILE_ARGV, "--codex-fallback-auth=chatgpt",
             "--codex-fallback-auth-model=gpt-5.6-sol"],
        )
        self.assertEqual(flag_codex.route_fallback.model, "gpt-5.6-sol")
        self.assertEqual(flag_codex.route_fallback.fallback_model, "gpt-5.6-terra")

    def test_route_fallback_configuration_errors_fail_before_review(self) -> None:
        for argv, env, message in (
            (["--engine", "codex", "--claude-fallback-auth", "subscription"], {},
             "--claude-fallback-auth is only supported for claude"),
            ([*CLAUDE_MANTLE_ARGV, "--codex-fallback-auth", "chatgpt"], MANTLE_ENV,
             "--codex-fallback-auth is only supported for codex"),
            ([*CLAUDE_MANTLE_ARGV, "--claude-fallback-auth-model", "claude-opus-5-5"],
             {**MANTLE_ENV, "AUTOREVIEW_CLAUDE_FALLBACK_AUTH": ""},
             "requires --claude-fallback-auth"),
            (["--engine", "claude", "--claude-fallback-auth", "mantle"], {},
             "Claude Mantle auth requires"),
            (["--engine", "codex", "--codex-profile", "autoreview-bedrock",
              "--codex-fallback-auth", "chatgpt"], {},
             "needs a model when the Codex profile selects it"),
            ([*CODEX_PROFILE_ARGV[:-2], "--thinking", "minimal",
              "--codex-fallback-auth-model", "gpt-6-astra"],
             {"AUTOREVIEW_CODEX_FALLBACK_AUTH": "chatgpt"},
             "invalid thinking level for codex model gpt-6-astra"),
            (["--engine", "claude"], {"AUTOREVIEW_CLAUDE_FALLBACK_AUTH": "api"},
             "invalid Claude fallback auth mode"),
        ):
            with self.subTest(argv=argv), self.assertRaisesRegex(SystemExit, message):
                route_reviewer(argv, env)

    def test_dry_run_checks_route_fallback_startup(self) -> None:
        reviewer = route_reviewer(CLAUDE_MANTLE_ARGV, MANTLE_ENV)

        def resolve(selected, _repo):
            if selected.claude_auth == "subscription":
                return False, "claude subscription login missing"
            return True, None

        stdout = io.StringIO()
        with mock.patch.object(
            AUTOREVIEW, "capture_evidence_inputs", side_effect=SystemExit("synthetic"),
        ), mock.patch.object(
            AUTOREVIEW, "build_bundle", side_effect=SystemExit("synthetic"),
        ), mock.patch.object(
            AUTOREVIEW, "resolve_engine_binary", side_effect=resolve,
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            status = AUTOREVIEW.dry_run_preflight(reviewer, [reviewer], Path.cwd(), "local", None)
        self.assertEqual(status, 1)
        self.assertIn("engine check: claude model=anthropic.claude-opus-5-5[1m] thinking=xhigh OK",
                      stdout.getvalue())
        self.assertIn(
            "route fallback check: claude auth=subscription model=claude-opus-5-5 thinking=xhigh "
            "UNAVAILABLE (claude subscription login missing)",
            stdout.getvalue(),
        )


class AutoreviewSecretScannerTests(unittest.TestCase):
    def test_findings_map_to_prompt_dataset_untracked_and_diff_paths(self) -> None:
        prompt = "\n".join(
            (
                "# Prompt file: review-notes.md",
                "prompt body",
                "# Dataset: evidence.json",
                "dataset body",
                "# Untracked File",
                'path: "new/config.ts"',
                "source-line 1: redacted example",
                "diff --git a/old.ts b/new.ts",
                "--- a/old.ts",
                "+++ b/new.ts",
                "@@ -1 +1 @@",
                "+redacted example",
            )
        )
        output = "\n".join(
            json.dumps(
                {"SourceMetadata": {"Data": {"Filesystem": {"line": line}}}}
            )
            for line in (2, 4, 7, 12)
        )

        self.assertEqual(
            AUTOREVIEW.trufflehog_review_pack_paths(prompt, output),
            ["evidence.json", "new.ts", "new/config.ts", "review-notes.md"],
        )

    def test_deleted_diff_finding_maps_to_original_path(self) -> None:
        prompt = "\n".join(
            (
                "# Change Bundle",
                "diff --git a/config.ts b/config.ts",
                "deleted file mode 100644",
                "--- a/config.ts",
                "+++ /dev/null",
                "@@ -1 +0,0 @@",
                "-redacted example",
            )
        )
        output = json.dumps(
            {"SourceMetadata": {"Data": {"Filesystem": {"Line": 7}}}}
        )

        self.assertEqual(
            AUTOREVIEW.trufflehog_review_pack_paths(prompt, output),
            ["config.ts"],
        )

    def test_unusable_scanner_output_falls_back_without_echoing_it(self) -> None:
        output = "not-json\n" + json.dumps(
            {
                "SourceMetadata": {"Data": {"Filesystem": {"line": "invalid"}}},
                "Raw": "must-not-be-returned",
            }
        )

        self.assertEqual(
            AUTOREVIEW.trufflehog_review_pack_paths("prompt", output),
            ["review pack"],
        )

    def test_scanner_command_requests_verified_and_unknown_results(self) -> None:
        prompt = "review pack with redacted examples only"
        with tempfile.TemporaryDirectory() as tempdir:
            repo = Path(tempdir)

            def run_scanner(
                command: list[str],
                cwd: Path,
                **kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                self.assertEqual(cwd, Path(command[2]).parent)
                self.assertEqual(Path(command[2]).read_text(encoding="utf-8"), prompt)
                self.assertEqual(
                    command[3:],
                    [
                        "--json",
                        "--no-color",
                        "--results=verified,unknown",
                        "--fail",
                        "--fail-on-scan-errors",
                    ],
                )
                self.assertEqual(kwargs["check"], False)
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch.object(
                AUTOREVIEW,
                "find_command",
                return_value="/trusted/trufflehog",
            ), mock.patch.object(AUTOREVIEW, "run", side_effect=run_scanner):
                AUTOREVIEW.scan_outgoing_review_pack(repo, prompt)


def amp_test_stream(
    cwd: Path,
    *,
    tools: list[object] | None = None,
    mcp_servers: list[object] | None = None,
    trigger: str = "Run the isolated autoreview adapter.",
    tool_name: str = "autoreview_generate",
    tool_input: object = None,
    tool_result_id: str = "amp-tool-use",
    tool_error: bool = False,
    tool_result_content: str | None = None,
    final_text: str = "Completed.",
    ensure_ascii: bool = True,
) -> str:
    if tool_input is None:
        tool_input = {}
    if tool_result_content is None:
        tool_result_content = (
            "Autoreview generation failed." if tool_error else "Adapter completed."
        )
    return "\n".join(
        [
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "cwd": str(cwd),
                    "session_id": "amp-test-session",
                    "tools": ["autoreview_generate"] if tools is None else tools,
                    "mcp_servers": [] if mcp_servers is None else mcp_servers,
                    "agent_mode": "medium",
                },
                ensure_ascii=ensure_ascii,
            ),
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": trigger}],
                    },
                    "parent_tool_use_id": None,
                    "session_id": "amp-test-session",
                },
                ensure_ascii=ensure_ascii,
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "amp-tool-use",
                                "name": tool_name,
                                "input": tool_input,
                            }
                        ],
                    },
                    "parent_tool_use_id": None,
                    "session_id": "amp-test-session",
                },
                ensure_ascii=ensure_ascii,
            ),
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_result_id,
                                "content": tool_result_content,
                                "is_error": tool_error,
                            }
                        ],
                    },
                    "parent_tool_use_id": None,
                    "session_id": "amp-test-session",
                },
                ensure_ascii=ensure_ascii,
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": final_text}],
                    },
                    "parent_tool_use_id": None,
                    "session_id": "amp-test-session",
                },
                ensure_ascii=ensure_ascii,
            ),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": final_text,
                    "session_id": "amp-test-session",
                },
                ensure_ascii=ensure_ascii,
            ),
        ]
    ) + "\n"


def amp_test_plugin_list(plugin_path: Path) -> str:
    return "\n".join(
        [
            f"✓ {plugin_path} active",
            "  tool: autoreview_generate",
            "  agent: autoreview-adapter",
            "  agent mode: autoreview",
        ]
    ) + "\n"


def amp_test_mcp_denial_result(
    command: list[str],
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    skills_root = Path(env["HOME"]) / ".config" / "agents" / "skills"
    probe_roots = list(skills_root.glob("autoreview-mcp-deny-*"))
    if len(probe_roots) != 1:
        raise AssertionError(f"expected one MCP denial probe, found {probe_roots}")
    mcp_config = json.loads((probe_roots[0] / "mcp.json").read_text(encoding="utf-8"))
    probe_name = next(iter(mcp_config))
    return subprocess.CompletedProcess(
        command,
        0,
        "12 tools available\n",
        f"error connecting to {probe_name}: MCP server is not allowed by MCP permissions\n",
    )


class AutoreviewAmpTests(unittest.TestCase):
    def test_amp_dry_run_and_runtime_reject_the_same_model_grammar(self) -> None:
        for model, diagnostic in (
            (None, "amp engine requires a model"),
            ("", "amp engine requires a model"),
            ("synthetic-model", "amp engine model must use a supported provider/model format"),
            ("unsupported/synthetic-model", "amp engine model must use a supported provider/model format"),
        ):
            args = argparse.Namespace(engine="amp", amp_bin="amp", model=model, thinking="high")
            with self.subTest(model=model), mock.patch.object(
                AUTOREVIEW, "find_command", return_value="/usr/bin/amp",
            ), mock.patch.dict(AUTOREVIEW.ENGINE_ISOLATION_PROBES, {
                "amp": lambda *_args: "/usr/bin/amp",
            }), mock.patch.object(
                AUTOREVIEW, "ensure_amp_isolation_supported", return_value="/usr/bin/amp",
            ), mock.patch.object(AUTOREVIEW, "safe_temp_root") as staging:
                self.assertEqual(AUTOREVIEW.resolve_engine_binary(args, Path.cwd()), (False, diagnostic))
                with self.assertRaises(SystemExit) as caught:
                    AUTOREVIEW.run_amp(args, Path.cwd(), "synthetic prompt")
                self.assertEqual(str(caught.exception.code), diagnostic)
                staging.assert_not_called()

    def test_amp_dry_run_validates_the_resolved_model_without_changing_precedence(self) -> None:
        valid, invalid = "openai/synthetic-model", "synthetic-model"
        cases = (
            ({}, [], "openai/gpt-5.6-sol", True),
            ({}, ["--model", valid], valid, True),
            ({}, ["--model", invalid], invalid, False),
            ({"AUTOREVIEW_MODEL": invalid}, [], invalid, False),
            ({"AUTOREVIEW_AMP_MODEL": invalid}, [], invalid, False),
            ({"AUTOREVIEW_MODEL": invalid, "AUTOREVIEW_AMP_MODEL": valid}, [], valid, True),
            ({"AUTOREVIEW_AMP_MODEL": invalid}, ["--model", valid], valid, True),
            ({"AUTOREVIEW_AMP_MODEL": valid}, ["--model", invalid], invalid, False),
            ({}, ["--model", invalid, "--model", "amp=" + valid], valid, True),
        )
        for env, options, expected_model, available in cases:
            with self.subTest(env=env, options=options), mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(sys, "argv", ["autoreview", "--engine", "amp", "--dry-run", *options]), \
                    mock.patch.object(AUTOREVIEW, "find_command", return_value="/usr/bin/amp"), \
                    mock.patch.dict(AUTOREVIEW.ENGINE_ISOLATION_PROBES, {"amp": lambda *_args: "/usr/bin/amp"}):
                reviewer = AUTOREVIEW.reviewer_args(AUTOREVIEW.parse_args())[0]
                self.assertEqual(reviewer.model, expected_model)
                expected_error = None if available else "amp engine model must use a supported provider/model format"
                self.assertEqual(AUTOREVIEW.resolve_engine_binary(reviewer, Path.cwd()), (available, expected_error))

    def test_amp_bin_cli_option_and_defaults(self) -> None:
        with mock.patch.object(
            sys,
            "argv",
            ["autoreview", "--engine", "amp", "--amp-bin", "/tmp/trusted-amp"],
        ):
            args = AUTOREVIEW.parse_args()
        reviewer = AUTOREVIEW.reviewer_args(args)[0]
        self.assertEqual(reviewer.amp_bin, "/tmp/trusted-amp")
        self.assertEqual(reviewer.model, "openai/gpt-5.6-sol")
        self.assertEqual(reviewer.thinking, "high")
        self.assertFalse(reviewer.tools)

    @unittest.skipIf(os.name == "nt", "Amp runtime is unsupported on native Windows")
    def test_amp_isolation_probe_requires_api_key_and_flags(self) -> None:
        args = argparse.Namespace(amp_bin="amp")
        required_flags = " ".join(
            [
                "--execute",
                "--stream-json",
                "--stream-json-input",
                "--plugin-ready-timeout",
                "--settings-file",
                "--no-ide",
            ]
        )
        with tempfile.TemporaryDirectory(prefix="autoreview-amp-probe-test.") as tmpdir, mock.patch.dict(
            os.environ,
            {"AMP_API_KEY": "test-key"},
            clear=False,
        ), mock.patch.object(
            AUTOREVIEW,
            "resolve_command",
            return_value="/usr/bin/amp",
        ), mock.patch.object(
            AUTOREVIEW,
            "safe_engine_env",
            return_value={},
        ), mock.patch.object(
            AUTOREVIEW,
            "safe_temp_root",
            return_value=Path(tmpdir),
        ), mock.patch.object(
            AUTOREVIEW,
            "run",
            return_value=subprocess.CompletedProcess(["amp", "--help"], 0, required_flags, ""),
        ):
            self.assertEqual(
                AUTOREVIEW.ensure_amp_isolation_supported(args, Path(tmpdir)),
                "/usr/bin/amp",
            )

        with mock.patch.dict(os.environ, {"AMP_API_KEY": ""}, clear=False), mock.patch.object(
            AUTOREVIEW,
            "resolve_command",
            return_value="/usr/bin/amp",
        ):
            with self.assertRaisesRegex(SystemExit, "requires AMP_API_KEY"):
                AUTOREVIEW.ensure_amp_isolation_supported(args, Path("/tmp/repo"))

    def test_amp_isolation_probe_rejects_native_windows(self) -> None:
        args = argparse.Namespace(amp_bin="amp")
        repo = Path("/tmp/repo")
        context = (
            contextlib.nullcontext()
            if os.name == "nt"
            else mock.patch.object(AUTOREVIEW.os, "name", "nt")
        )
        with context:
            with self.assertRaisesRegex(SystemExit, "native Windows"):
                AUTOREVIEW.ensure_amp_isolation_supported(args, repo)

    @unittest.skipIf(os.name == "nt", "Amp runtime is unsupported on native Windows")
    def test_amp_run_keeps_review_prompt_out_of_outer_agent(self) -> None:
        args = argparse.Namespace(
            amp_bin="amp",
            max_output_chars=2_000_000,
            model="openai/gpt-5.6-sol",
            stream_engine_output=False,
            thinking="high",
        )
        secret_prompt = "review diff PRIVATE_REVIEW_MARKER_8f3c"
        observed: dict[str, object] = {}

        def fake_preflight(
            command: list[str],
            cwd: Path,
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            env = kwargs["env"]
            assert isinstance(env, dict)
            runtime_root = Path(str(env["XDG_CONFIG_HOME"])).parent
            plugin_root = Path(str(env["XDG_CONFIG_HOME"])) / "amp" / "plugins"
            plugin_path = next(plugin_root.glob("autoreview-*.ts"))
            if command[-2:] == ["tools", "list"]:
                observed["mcp_preflight_prompt_exists"] = (
                    runtime_root / "review-prompt.txt"
                ).exists()
                return amp_test_mcp_denial_result(command, env)
            observed["preflight_command"] = command
            observed["preflight_prompt_exists"] = (
                runtime_root / "review-prompt.txt"
            ).exists()
            return subprocess.CompletedProcess(
                command,
                0,
                amp_test_plugin_list(plugin_path),
                "",
            )

        def fake_execute(
            command: list[str],
            cwd: Path,
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            observed["command"] = command
            observed["cwd"] = cwd
            observed["input"] = kwargs["input_text"]
            observed["env"] = kwargs["env"]
            env = kwargs["env"]
            assert isinstance(env, dict)
            runtime_root = Path(str(env["XDG_CONFIG_HOME"])).parent
            prompt_path = runtime_root / "review-prompt.txt"
            result_path = runtime_root / "review-result.json"
            settings_path = runtime_root / "settings.json"
            plugin_root = Path(str(env["XDG_CONFIG_HOME"])) / "amp" / "plugins"
            plugin_path = next(plugin_root.glob("autoreview-*.ts"))
            observed["prompt"] = prompt_path.read_text(encoding="utf-8")
            observed["settings"] = json.loads(settings_path.read_text(encoding="utf-8"))
            observed["plugin"] = plugin_path.read_text(encoding="utf-8")
            observed["plugin_path"] = plugin_path
            observed["workspace"] = list(cwd.iterdir())
            result_path.write_text(json.dumps(FINAL_REPORT), encoding="utf-8")
            result_path.chmod(0o600)
            return subprocess.CompletedProcess(command, 0, amp_test_stream(cwd), "")

        inherited = {
            "AMP_API_KEY": "test-key",
            "AMP_URL": "https://attacker.invalid",
            "NODE_OPTIONS": "--require=/tmp/attack.js",
            "PYTHONPATH": "/tmp/attack",
            "PLUGINS": "inherited-plugins",
        }
        with tempfile.TemporaryDirectory(prefix="autoreview-amp-run-test.") as tmpdir:
            repo = Path(tmpdir) / "repo"
            repo.mkdir()
            with mock.patch.dict(os.environ, inherited, clear=False), mock.patch.object(
                AUTOREVIEW,
                "ensure_amp_isolation_supported",
                return_value="/usr/bin/amp",
            ), mock.patch.object(
                AUTOREVIEW,
                "run",
                side_effect=fake_preflight,
            ), mock.patch.object(
                AUTOREVIEW,
                "run_with_heartbeat",
                side_effect=fake_execute,
            ):
                output = AUTOREVIEW.run_amp(args, repo, secret_prompt)

        self.assertEqual(json.loads(output), FINAL_REPORT)
        self.assertFalse(observed["mcp_preflight_prompt_exists"])
        self.assertFalse(observed["preflight_prompt_exists"])
        preflight_command = observed["preflight_command"]
        self.assertIsInstance(preflight_command, list)
        assert isinstance(preflight_command, list)
        self.assertEqual(preflight_command[-2:], ["plugins", "list"])
        command = observed["command"]
        self.assertIsInstance(command, list)
        assert isinstance(command, list)
        self.assertIn("--execute", command)
        self.assertIn("--stream-json-input", command)
        self.assertIn("--settings-file", command)
        self.assertNotIn("--orb-execute", command)
        self.assertEqual(command[command.index("--mode") + 1], "autoreview")
        self.assertNotIn(secret_prompt, " ".join(command))
        self.assertNotIn(secret_prompt, str(observed["input"]))
        self.assertEqual(observed["prompt"], secret_prompt)
        self.assertEqual(observed["workspace"], [])
        settings = observed["settings"]
        self.assertIsInstance(settings, dict)
        assert isinstance(settings, dict)
        self.assertNotIn("amp.tools.disable", settings)
        self.assertNotIn("amp.tools.enable", settings)
        self.assertEqual(settings["amp.updates.mode"], "disabled")
        self.assertEqual(
            settings["amp.mcpPermissions"],
            [
                {"matches": {"command": "*"}, "action": "reject"},
                {"matches": {"url": "*"}, "action": "reject"},
            ],
        )
        plugin = observed["plugin"]
        self.assertIsInstance(plugin, str)
        assert isinstance(plugin, str)
        self.assertIn("amp.ai.generate", plugin)
        self.assertIn("amp.registerTool", plugin)
        self.assertIn("amp.createAgent", plugin)
        schema, _ = json.JSONDecoder().raw_decode(plugin.split("schema: ", 1)[1])
        self.assertEqual(schema, {
            "name": "autoreview_report",
            "description": "A security-focused code-review report for the supplied patch.",
            "fields": AUTOREVIEW.PROVIDER_SCHEMA["properties"],
        })
        self.assertIn('tools: ["autoreview_generate"]', plugin)
        self.assertIn("readFileSync", plugin)
        self.assertNotIn(secret_prompt, plugin)
        env = observed["env"]
        self.assertIsInstance(env, dict)
        assert isinstance(env, dict)
        self.assertEqual(env["AMP_API_KEY"], "test-key")
        self.assertNotIn("AMP_URL", env)
        self.assertNotIn("NODE_OPTIONS", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertEqual(env["PLUGINS"], "all")
        plugin_path = observed["plugin_path"]
        self.assertIsInstance(plugin_path, Path)
        assert isinstance(plugin_path, Path)
        self.assertRegex(plugin_path.stem, r"^autoreview-[0-9a-f]{32}$")
        cwd = observed["cwd"]
        self.assertIsInstance(cwd, Path)
        assert isinstance(cwd, Path)
        self.assertNotEqual(cwd.resolve(), repo.resolve())
        self.assertEqual(Path(env["HOME"]).parent, cwd.parent)

    def test_amp_stream_attestation_rejects_bad_events(self) -> None:
        cwd = Path("/tmp/amp-review-empty")
        misplaced_events = [
            json.loads(line) for line in amp_test_stream(cwd).splitlines()
        ]
        tool_use = misplaced_events[2]["message"]["content"].pop()
        misplaced_events[4]["message"]["content"].append(tool_use)
        misplaced = "\n".join(json.dumps(event) for event in misplaced_events) + "\n"
        extra_result_events = [
            json.loads(line) for line in amp_test_stream(cwd).splitlines()
        ]
        extra_result_events[3]["message"]["content"].append(
            {"type": "text", "text": "unexpected"}
        )
        extra_result = (
            "\n".join(json.dumps(event) for event in extra_result_events) + "\n"
        )
        cases = {
            "malformed": "not-json\n",
            "tools": amp_test_stream(cwd, tools=["autoreview_generate", "shell_command"]),
            "mcp": amp_test_stream(cwd, mcp_servers=[{"name": "server"}]),
            "trigger": amp_test_stream(cwd, trigger="untrusted diff"),
            "wrong tool": amp_test_stream(cwd, tool_name="shell_command"),
            "tool input": amp_test_stream(cwd, tool_input={"command": "id"}),
            "tool result": amp_test_stream(cwd, tool_result_id="wrong-id"),
            "unsanitized error": amp_test_stream(
                cwd,
                tool_error=True,
                tool_result_content="provider echoed PRIVATE_REVIEW_MARKER_8f3c",
            ),
            "multiple init": amp_test_stream(cwd).splitlines()[0] + "\n" + amp_test_stream(cwd),
            "multiple result": amp_test_stream(cwd) + amp_test_stream(cwd).splitlines()[-1] + "\n",
            "misplaced tool use": misplaced,
            "extra tool result content": extra_result,
        }
        for label, stream in cases.items():
            with self.subTest(label=label), self.assertRaisesRegex(
                SystemExit,
                "amp isolation attestation failed",
            ):
                AUTOREVIEW.attest_amp_stream(stream, cwd)

        self.assertTrue(AUTOREVIEW.attest_amp_stream(amp_test_stream(cwd), cwd))
        self.assertFalse(
            AUTOREVIEW.attest_amp_stream(amp_test_stream(cwd, tool_error=True), cwd)
        )

    def test_amp_stream_attestation_preserves_unicode_json_strings(self) -> None:
        cwd = Path("/tmp/amp-review-empty")
        for separator in ("\u0085", "\u2028", "\u2029"):
            final_text = f"Completed.{separator}Synthetic response."
            escaped = amp_test_stream(cwd, final_text=final_text)
            literal = amp_test_stream(cwd, final_text=final_text, ensure_ascii=False)
            self.assertNotIn(separator, escaped)
            self.assertIn(separator, literal)
            escaped_events = [json.loads(line) for line in escaped.split("\n") if line]
            literal_events = [json.loads(line) for line in literal.split("\n") if line]
            self.assertEqual(len(escaped_events), 6)
            self.assertEqual(escaped_events, literal_events)
            for encoding, stream in (("escaped", escaped), ("literal", literal)):
                with self.subTest(separator=f"U+{ord(separator):04X}", encoding=encoding):
                    self.assertTrue(AUTOREVIEW.attest_amp_stream(stream, cwd))

    def test_amp_stream_attestation_keeps_line_framing_and_noise_guards(self) -> None:
        cwd = Path("/tmp/amp-review-empty")
        records = amp_test_stream(cwd).split("\n")[:-1]
        for line_ending in ("\n", "\r\n"):
            for blank_line in ("", " \t"):
                framed = (line_ending + blank_line + line_ending).join(records)
                framed = blank_line + line_ending + framed + line_ending + blank_line
                with self.subTest(line_ending=repr(line_ending), blank_line=blank_line):
                    self.assertTrue(AUTOREVIEW.attest_amp_stream(framed, cwd))
                noisy = line_ending.join([blank_line, *records[:2], "not-json", *records[2:]])
                with self.subTest(line_ending=repr(line_ending), noise=True), self.assertRaisesRegex(
                    SystemExit, "amp isolation attestation failed: malformed stream JSON",
                ):
                    AUTOREVIEW.attest_amp_stream(noisy, cwd)

    @unittest.skipIf(os.name == "nt", "Amp runtime is unsupported on native Windows")
    def test_amp_review_result_preserves_unicode_stream_and_private_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="autoreview-amp-unicode-test.") as tmpdir:
            root = Path(tmpdir)
            result_path = root / "result.json"
            for separator in ("\u0085", "\u2028", "\u2029"):
                explanation = f"Completed.{separator}Synthetic response."
                report = {
                    **FINAL_REPORT,
                    "overall_explanation": explanation,
                    "review_completion": "complete",
                }
                raw_report = json.dumps(report, ensure_ascii=False)
                result_path.write_text(raw_report, encoding="utf-8")
                result_path.chmod(0o600)
                for ensure_ascii in (True, False):
                    stream = amp_test_stream(
                        root, final_text=explanation, ensure_ascii=ensure_ascii,
                    )
                    process = subprocess.CompletedProcess([], 0, stream, "")
                    with self.subTest(separator=f"U+{ord(separator):04X}", ensure_ascii=ensure_ascii):
                        output = AUTOREVIEW.amp_review_result(
                            process, root, root / "error", result_path,
                        )
                        self.assertEqual(output, raw_report)
                        self.assertEqual(json.loads(output), report)

    @unittest.skipIf(os.name == "nt", "Amp runtime is unsupported on native Windows")
    def test_amp_run_reports_timeout_before_stream_attestation(self) -> None:
        args = argparse.Namespace(
            amp_bin="amp",
            engine_timeout_seconds=0.01,
            max_output_chars=2_000_000,
            model="openai/gpt-5.6-sol",
            stream_engine_output=False,
            thinking="high",
        )

        def fake_preflight(
            command: list[str],
            cwd: Path,
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            env = kwargs["env"]
            assert isinstance(env, dict)
            if command[-2:] == ["tools", "list"]:
                return amp_test_mcp_denial_result(command, env)
            plugin_root = Path(str(env["XDG_CONFIG_HOME"])) / "amp" / "plugins"
            plugin_path = next(plugin_root.glob("autoreview-*.ts"))
            return subprocess.CompletedProcess(
                command,
                0,
                amp_test_plugin_list(plugin_path),
                "",
            )

        def fake_execute(
            command: list[str],
            cwd: Path,
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            self.assertEqual(kwargs["max_runtime_seconds"], 0.01)
            return subprocess.CompletedProcess(
                command,
                124,
                '{"type":"system","subtype":"init"}\n',
                "amp engine timed out after 0.01s",
            )

        with tempfile.TemporaryDirectory(prefix="autoreview-amp-timeout-test.") as tmpdir:
            repo = Path(tmpdir) / "repo"
            repo.mkdir()
            with mock.patch.object(
                AUTOREVIEW,
                "ensure_amp_isolation_supported",
                return_value="/usr/bin/amp",
            ), mock.patch.object(
                AUTOREVIEW,
                "run",
                side_effect=fake_preflight,
            ), mock.patch.object(
                AUTOREVIEW,
                "run_with_heartbeat",
                side_effect=fake_execute,
            ), mock.patch.object(
                AUTOREVIEW,
                "attest_amp_stream",
                side_effect=AssertionError("timeout stream must not be attested"),
            ) as attest:
                with self.assertRaises(SystemExit) as exc_info:
                    AUTOREVIEW.run_amp(args, repo, "review")

        message = str(exc_info.exception)
        self.assertIn("amp engine failed (124)", message)
        self.assertIn("amp engine timed out after 0.01s", message)
        attest.assert_not_called()

    def test_amp_failed_process_and_invalid_artifact_keep_runtime_guards(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cases = (
                (subprocess.CompletedProcess([], 7, "", "provider failed"), "expected exactly one leading"),
                (subprocess.CompletedProcess([], 7, '{"type":"system","subtype":"init"}\n', ""), "unexpected adapter event sequence"),
                (subprocess.CompletedProcess([], 0, amp_test_stream(root, tools=["shell_command"]), ""), "exposed tools"),
                (subprocess.CompletedProcess([], 0, amp_test_stream(root), ""), "produced no result file"),
                (subprocess.CompletedProcess([], 0, '{"type":[]}\n', ""), "unexpected stream event type"),
            )
            for result, diagnostic in cases:
                with self.subTest(diagnostic=diagnostic), self.assertRaises(AUTOREVIEW.ReviewerUnavailable) as caught:
                    AUTOREVIEW.amp_review_result(result, root, root / "error", root / "result")
                self.assertEqual(caught.exception.reason, "runtime_validation_failed")
                self.assertIn(diagnostic, str(caught.exception))
                self.assertEqual(caught.exception.returncode, result.returncode)
            for raw in ("[" * 2000 + "]" * 2000, '{"number":' + "9" * 10000 + "}"):
                with self.subTest(length=len(raw)), self.assertRaises(AUTOREVIEW.ReviewerUnavailable) as caught:
                    AUTOREVIEW.amp_review_result(subprocess.CompletedProcess([], 0, raw, ""), root, root / "error", root / "result")
                self.assertEqual(caught.exception.reason, "runtime_validation_failed")

    def test_amp_plugin_inventory_attestation_fails_closed(self) -> None:
        cwd = Path("/tmp/amp-review-empty")
        plugin_path = cwd.parent / "config" / "amp" / "plugins" / "autoreview-token.ts"
        valid = amp_test_plugin_list(plugin_path)
        AUTOREVIEW.attest_amp_plugin_inventory(valid, plugin_path, cwd)

        cases = {
            "missing": "",
            "inactive": valid.replace("✓", "✗", 1).replace(" active", " error", 1),
            "other plugin": valid
            + amp_test_plugin_list(plugin_path.with_name("unexpected.ts")),
            "event handler": valid + "  events: agent.start\n",
            "other tool": valid.replace(
                "  agent: autoreview-adapter",
                "  tool: shell_command\n  agent: autoreview-adapter",
            ),
        }
        for label, output in cases.items():
            with self.subTest(label=label), self.assertRaisesRegex(
                SystemExit,
                "amp plugin isolation preflight failed",
            ):
                AUTOREVIEW.attest_amp_plugin_inventory(output, plugin_path, cwd)

    @unittest.skipIf(os.name == "nt", "Amp runtime is unsupported on native Windows")
    def test_amp_run_surfaces_direct_generation_failure(self) -> None:
        args = argparse.Namespace(
            amp_bin="amp",
            max_output_chars=2_000_000,
            model="openai/gpt-5.6-sol",
            stream_engine_output=False,
            thinking="high",
        )

        def fake_preflight(
            command: list[str],
            cwd: Path,
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            env = kwargs["env"]
            assert isinstance(env, dict)
            if command[-2:] == ["tools", "list"]:
                return amp_test_mcp_denial_result(command, env)
            plugin_root = Path(str(env["XDG_CONFIG_HOME"])) / "amp" / "plugins"
            plugin_path = next(plugin_root.glob("autoreview-*.ts"))
            return subprocess.CompletedProcess(
                command,
                0,
                amp_test_plugin_list(plugin_path),
                "",
            )

        def fake_execute(
            command: list[str],
            cwd: Path,
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            env = kwargs["env"]
            assert isinstance(env, dict)
            error_path = Path(str(env["XDG_CONFIG_HOME"])).parent / "review-error.txt"
            error_path.write_text("provider rejected model", encoding="utf-8")
            error_path.chmod(0o600)
            return subprocess.CompletedProcess(
                command,
                0,
                amp_test_stream(cwd, tool_error=True),
                "",
            )

        with tempfile.TemporaryDirectory(prefix="autoreview-amp-error-test.") as tmpdir:
            repo = Path(tmpdir) / "repo"
            repo.mkdir()
            with mock.patch.object(
                AUTOREVIEW,
                "ensure_amp_isolation_supported",
                return_value="/usr/bin/amp",
            ), mock.patch.object(
                AUTOREVIEW,
                "run",
                side_effect=fake_preflight,
            ), mock.patch.object(
                AUTOREVIEW,
                "run_with_heartbeat",
                side_effect=fake_execute,
            ):
                with self.assertRaisesRegex(SystemExit, "provider rejected model"):
                    AUTOREVIEW.run_amp(args, repo, "review")


class AutoreviewInputTests(unittest.TestCase):
    def test_every_provider_scans_each_pack_before_review(self) -> None:
        for engine in ("codex", "claude", "amp", "pi"):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tempdir:
                args = argparse.Namespace(engine=engine, max_priority="P0")
                prompts = [f"complete pack {index}: unicode π\r\n-context\n+change\n" for index in range(2)]
                events: list[tuple[str, str]] = []

                def scan(_repo: Path, prompt: str) -> None:
                    events.append(("scan", prompt))

                def review(_args: argparse.Namespace, _repo: Path, prompt: str) -> str:
                    events.append(("review", prompt))
                    return json.dumps({**FINAL_REPORT, "review_completion": "complete"})

                with mock.patch.object(AUTOREVIEW, "scan_outgoing_review_pack", side_effect=scan), \
                        mock.patch.object(AUTOREVIEW, f"run_{engine}", return_value=json.dumps({**FINAL_REPORT, "review_completion": "complete"})) as provider:
                    provider.side_effect = review
                    for prompt in prompts:
                        result = AUTOREVIEW.run_reviewer(args, Path(tempdir), prompt, set(), [])
                        self.assertTrue(result.complete)
                        self.assertEqual(result.report["findings"], [])
                self.assertEqual([call.args[2] for call in provider.call_args_list], prompts)
                self.assertEqual(
                    events,
                    [
                        ("scan", prompts[0]),
                        ("review", prompts[0]),
                        ("scan", prompts[1]),
                        ("review", prompts[1]),
                    ],
                )

    def test_scan_refusal_prevents_provider_call(self) -> None:
        args = argparse.Namespace(engine="codex", max_priority="P0")
        provider = mock.Mock(return_value=json.dumps(FINAL_REPORT))
        with mock.patch.object(
            AUTOREVIEW,
            "scan_outgoing_review_pack",
            side_effect=SystemExit("refusing to send review pack: config.ts"),
        ), mock.patch.object(AUTOREVIEW, "run_codex", provider):
            with self.assertRaisesRegex(SystemExit, "config.ts"):
                AUTOREVIEW.run_reviewer(args, Path.cwd(), "prompt", set(), [])
        provider.assert_not_called()

    def test_binary_stdin_preserves_utf8_and_crlf_bytes(self) -> None:
        payload = "unicode \u03c0\r\nnext\n".encode("utf-8")
        with tempfile.TemporaryDirectory() as tempdir, tempfile.TemporaryFile() as source:
            source.write(payload)
            source.seek(0)
            result = AUTOREVIEW.run(
                [sys.executable, "-c", "import sys; print(sys.stdin.buffer.read().hex())"],
                Path(tempdir), stdin=source,
            )
        self.assertEqual(result.stdout.strip(), payload.hex())


class AutoreviewEfficiencyTests(unittest.TestCase):
    @staticmethod
    def usage_event(multiplier=1):
        return json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 100 * multiplier, "cached_input_tokens": 20 * multiplier,
            "output_tokens": 30 * multiplier, "reasoning_output_tokens": 10 * multiplier,
        }})

    def test_usage_keeps_last_cumulative_snapshot_and_sums_fresh_attempts(self):
        args = argparse.Namespace()
        AUTOREVIEW.record_codex_usage(args, self.usage_event() + "\n" + self.usage_event(2))
        AUTOREVIEW.record_codex_usage(args, self.usage_event(3))
        self.assertEqual(AUTOREVIEW.review_usage_summary(args), {
            "attempts": 2, "reported_attempts": 2, "unknown_attempts": 0,
            "partial_attempts": 0, "complete": True,
            "tokens": {"input_tokens": 500, "cached_input_tokens": 100,
                       "output_tokens": 150, "reasoning_output_tokens": 50},
        })

    def test_usage_missing_or_invalid_terminal_snapshot_is_unknown(self):
        for invalid in ("", "malformed", '{"type":"turn.failed","error":{}}',
                        '{"type":"turn.completed","usage":{}}',
                        self.usage_event().replace('100', 'true'),
                        self.usage_event().replace('100', '-1')):
            with self.subTest(invalid=invalid):
                args = argparse.Namespace()
                AUTOREVIEW.record_codex_usage(args, invalid)
                self.assertEqual(AUTOREVIEW.review_usage_summary(args), {
                    "attempts": 1, "reported_attempts": 0, "unknown_attempts": 1,
                    "partial_attempts": 0, "complete": False, "tokens": None,
                })
                AUTOREVIEW.record_codex_usage(args, self.usage_event())
                summary = AUTOREVIEW.review_usage_summary(args)
                self.assertEqual(summary["unknown_attempts"], 1)
                self.assertFalse(summary["complete"])
                self.assertEqual(summary["tokens"]["input_tokens"], 100)
        args = argparse.Namespace()
        AUTOREVIEW.record_codex_usage(args, self.usage_event() + '\n{"type":"turn.completed"}')
        summary = AUTOREVIEW.review_usage_summary(args)
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["partial_attempts"], 1)
        self.assertEqual(summary["tokens"]["input_tokens"], 100)

    def test_failed_or_timed_out_attempt_keeps_observed_lower_bound(self):
        for suffix in ('\n{"type":"turn.failed"}', '\n{"type":"turn.started"}', ""):
            with self.subTest(suffix=suffix):
                args = argparse.Namespace()
                AUTOREVIEW.record_codex_usage(args, self.usage_event() + suffix, completed=False)
                summary = AUTOREVIEW.review_usage_summary(args)
                self.assertFalse(summary["complete"])
                self.assertEqual(summary["partial_attempts"], 1)
                self.assertEqual(summary["tokens"]["input_tokens"], 100)

    def test_codex_zero_default_without_usage_sample_is_unknown(self):
        for earlier in ("", self.usage_event() + "\n"):
            with self.subTest(earlier=bool(earlier)):
                args = argparse.Namespace()
                AUTOREVIEW.record_codex_usage(args, earlier + self.usage_event(0))
                summary = AUTOREVIEW.review_usage_summary(args)
                self.assertFalse(summary["complete"])
                self.assertEqual(summary["unknown_attempts"], 0 if earlier else 1)
                self.assertEqual(summary["partial_attempts"], 1 if earlier else 0)
                self.assertEqual(summary["tokens"]["input_tokens"] if earlier else summary["tokens"],
                                 100 if earlier else None)

    def test_malformed_usage_cannot_break_report_acceptance(self):
        for event in ('{"type":[]}', '[' * 2000 + ']' * 2000,
                      '{"n":' + '9' * 10000 + '}', 'not json'):
            with self.subTest(event=event[:20]):
                args = argparse.Namespace()
                AUTOREVIEW.record_codex_usage(args, event)
                self.assertIsNone(AUTOREVIEW.review_usage_summary(args)["tokens"])
        args = argparse.Namespace()
        event = json.loads(self.usage_event())
        event["usage"]["cache_write_input_tokens"] = 80
        AUTOREVIEW.record_codex_usage(args, json.dumps(event))
        self.assertTrue(AUTOREVIEW.review_usage_summary(args)["complete"])

    def test_interruption_retains_observed_usage_with_and_without_live_display(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming), tempfile.TemporaryDirectory() as tempdir:
                def interrupt(*_args):
                    raise AUTOREVIEW.EngineInterrupted(130)

                script = f"import time; print({self.usage_event()!r}, flush=True); time.sleep(5)"
                with mock.patch.object(AUTOREVIEW, "emit_heartbeat", side_effect=interrupt), \
                        self.assertRaises(AUTOREVIEW.EngineInterrupted) as caught:
                    AUTOREVIEW.run_with_heartbeat(
                        [sys.executable, "-c", script], Path(tempdir), label="usage-fixture",
                        heartbeat_seconds=0.2, stream_output=streaming, stream_display=interrupt,
                    )
                args = argparse.Namespace()
                AUTOREVIEW.record_codex_usage(args, caught.exception.stdout, completed=False)
                summary = AUTOREVIEW.review_usage_summary(args)
                self.assertEqual(summary["tokens"]["input_tokens"], 100)
                self.assertEqual(summary["partial_attempts"], 1)
                self.assertFalse(summary["complete"])

    def test_planner_reduces_repeated_context_without_more_passes(self):
        bundle = "# Commit Diff\n" + "+change\n" * 75_000
        datasets = [AUTOREVIEW.ReviewDataset("evidence.txt", "evidence π\r\n" * (800_000 // 13))]
        with mock.patch.object(AUTOREVIEW, "current_branch", return_value="topic"):
            with mock.patch.object(AUTOREVIEW, "optimize_evidence_plan", side_effect=lambda plan, *args: plan):
                legacy = AUTOREVIEW.build_review_prompts(Path("."), "commit", "HEAD", bundle, "", datasets)
            planned = AUTOREVIEW.build_review_prompts(Path("."), "commit", "HEAD", bundle, "", datasets)
        self.assertLessEqual(len(planned), len(legacy))
        self.assertLess(AUTOREVIEW.review_plan_bytes(planned), AUTOREVIEW.review_plan_bytes(legacy))
        # Full cross-product and byte-offset reconstruction are exercised by the
        # hardening suite; this fixture measures the formerly repeated context.
        self.assertTrue(all(AUTOREVIEW.utf8_size(prompt) <= AUTOREVIEW.MAX_REVIEW_PROMPT_BYTES
                            for prompt in planned))

    def test_planner_retains_legacy_when_bytes_or_passes_would_regress(self):
        baseline = ["a" * 100, "b" * 100]
        for candidate in (["x", "y", "z"], ["x" * 201], ["x" * 300, "y"]):
            with self.subTest(candidate=candidate):
                planned = AUTOREVIEW.optimize_evidence_plan(
                    baseline, 200, 100, [AUTOREVIEW.ReviewDataset("evidence", "z" * 100)],
                    lambda _limit, _max_passes: candidate,
                )
                self.assertEqual(planned, baseline)

    def test_explicit_pass_budget_requires_a_positive_integer(self):
        for value in ("0", "-1", "1.5"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch.object(sys, "argv", ["autoreview", "--max-review-passes", value]), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    AUTOREVIEW.parse_args()


class AutoreviewKimiRefusalTests(unittest.TestCase):
    @contextlib.contextmanager
    def no_engine_activity(self):
        names = (
            "find_command", "resolve_command", "run", "run_with_heartbeat", "run_with_stream",
            "safe_temp_root", "run_codex", "run_claude", "run_amp", "run_pi",
        )
        guards = {name: mock.Mock(side_effect=AssertionError(f"refused engine reached {name}"))
                  for name in names}
        with contextlib.ExitStack() as stack:
            for name, guard in guards.items():
                stack.enter_context(mock.patch.object(AUTOREVIEW, name, guard))
            for name in ("open", "read_text", "read_bytes"):
                guard = mock.Mock(side_effect=AssertionError("refused engine read configuration/auth"))
                guards[f"Path.{name}"] = guard
                stack.enter_context(mock.patch.object(Path, name, guard))
            guard = mock.Mock(side_effect=AssertionError("refused engine staged a runtime"))
            guards["TemporaryDirectory"] = guard
            stack.enter_context(mock.patch.object(AUTOREVIEW.tempfile, "TemporaryDirectory", guard))
            yield guards

    def assert_private_input_diagnostic(self, message):
        self.assertIn("kimi review is unavailable", str(message).lower())
        self.assertIn("private input channel", str(message).lower())

    def test_kimi_explicit_and_environment_selection_refuse_private_input(self):
        for environment in (False, True):
            for dry_run in (False, True):
                with self.subTest(environment=environment, dry_run=dry_run):
                    env = {"AUTOREVIEW_ENGINE": "kimi"} if environment else {}
                    argv = ["autoreview", "--kimi-bin", "synthetic-kimi"]
                    if not environment:
                        argv += ["--engine", "kimi"]
                    if dry_run:
                        argv += ["--dry-run"]
                    with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "argv", argv):
                        args = AUTOREVIEW.parse_args()
                        self.assertEqual((args.engine, args.kimi_bin), ("kimi", "synthetic-kimi"))
                        with self.no_engine_activity() as guards:
                            with self.assertRaises(SystemExit) as caught:
                                AUTOREVIEW.reviewer_args(args)
                    self.assert_private_input_diagnostic(caught.exception.code)
                    for guard in guards.values():
                        guard.assert_not_called()

    def test_kimi_direct_dispatch_refuses_before_engine_activity(self):
        args = argparse.Namespace(engine="kimi", kimi_bin="synthetic-kimi", model=None,
                                  thinking=None, stream_engine_output=False)
        with self.no_engine_activity() as guards:
            with self.assertRaises(SystemExit) as caught:
                AUTOREVIEW.run_engine(args, Path.cwd(), "synthetic private review text")
        self.assert_private_input_diagnostic(caught.exception.code)
        for guard in guards.values():
            guard.assert_not_called()

    def test_kimi_preflight_refuses_before_engine_activity(self):
        args = argparse.Namespace(engine="kimi", kimi_bin="synthetic-kimi")
        with self.no_engine_activity() as guards:
            available, reason = AUTOREVIEW.resolve_engine_binary(args, Path.cwd())
        self.assertFalse(available)
        self.assert_private_input_diagnostic(reason)
        for guard in guards.values():
            guard.assert_not_called()


class AutoreviewCompatibilityTests(unittest.TestCase):
    def test_default_reviewer_uses_sol_61_high_with_sol_6_access_retry(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(sys, "argv", ["autoreview"]):
            reviewer = AUTOREVIEW.reviewer_args(AUTOREVIEW.parse_args())[0]
        self.assertEqual(reviewer.engine, "codex")
        self.assertEqual(reviewer.model, "gpt-6.1-sol")
        self.assertEqual(reviewer.thinking, "high")
        self.assertEqual(reviewer.fallback_model, "gpt-6-sol")

    def test_sol_61_and_astra_reject_unsupported_effort_before_preparation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="autoreview-invalid-effort.") as tempdir:
            for model, effort, source in itertools.product(
                (None, "gpt-6.1-sol", "gpt-6-astra"),
                ("none", "minimal", "ultra"),
                ("cli", "keyed-cli", "environment", "global-environment"),
            ):
                with self.subTest(model=model, effort=effort, source=source):
                    argv = [sys.executable, str(SCRIPT_PATH), "--engine", "codex",
                            "--codex-bin", str(Path(tempdir) / "missing-codex")]
                    env = {key: value for key, value in os.environ.items()
                           if not key.startswith("AUTOREVIEW_")}
                    if source in {"cli", "keyed-cli"}:
                        prefix = "codex=" if source == "keyed-cli" else ""
                        argv += ["--thinking", prefix + effort]
                        if model:
                            argv += ["--model", prefix + model]
                    else:
                        prefix = "AUTOREVIEW_CODEX_" if source == "environment" else "AUTOREVIEW_"
                        env[prefix + "THINKING"] = effort
                        if model:
                            env[prefix + "MODEL"] = model
                    # No Git repository or engine exists: rejection must precede preparation.
                    result = subprocess.run(argv, cwd=tempdir, env=env, text=True,
                                            capture_output=True, timeout=30)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(result.stderr.strip(),
                                     f"invalid thinking level for codex model {model or 'gpt-6.1-sol'}: {effort} "
                                     "(valid: high, low, max, medium, xhigh)")

    def test_model_validation_uses_effective_cli_overrides(self) -> None:
        cases = (
            ({}, ["--thinking", "minimal", "--thinking", "codex=high"], "gpt-6.1-sol", "high"),
            ({"AUTOREVIEW_THINKING": "none", "AUTOREVIEW_CODEX_THINKING": "high"},
             [], "gpt-6.1-sol", "high"),
            ({"AUTOREVIEW_CODEX_THINKING": "minimal"},
             ["--thinking", "high"], "gpt-6.1-sol", "high"),
            ({"AUTOREVIEW_CODEX_MODEL": "gpt-6.1-sol", "AUTOREVIEW_CODEX_THINKING": "none"},
             ["--thinking", "high"], "gpt-6.1-sol", "high"),
            ({"AUTOREVIEW_CODEX_MODEL": "gpt-6.1-sol", "AUTOREVIEW_CODEX_THINKING": "none"},
             ["--model", "gpt-6-sol"], "gpt-6-sol", "none"),
            ({"AUTOREVIEW_CODEX_MODEL": "gpt-6-astra", "AUTOREVIEW_CODEX_THINKING": "none"},
             ["--thinking", "high"], "gpt-6-astra", "high"),
            ({"AUTOREVIEW_CODEX_MODEL": "gpt-6-astra", "AUTOREVIEW_CODEX_THINKING": "minimal"},
             ["--model", "gpt-5.6-sol"], "gpt-5.6-sol", "minimal"),
        )
        for env, overrides, model, effort in cases:
            with self.subTest(overrides=overrides), mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(sys, "argv", ["autoreview", "--engine", "codex", *overrides]):
                reviewer = AUTOREVIEW.reviewer_args(AUTOREVIEW.parse_args())[0]
                self.assertEqual(reviewer.model, model)
                self.assertEqual(reviewer.thinking, effort)

    def test_effort_only_cli_and_environment_preserve_supported_models(self) -> None:
        for effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
            selections = (
                (["--thinking", effort], {}),
                (["--thinking", "codex=" + effort], {}),
                ([], {"AUTOREVIEW_THINKING": effort}),
                ([], {"AUTOREVIEW_CODEX_THINKING": effort}),
            )
            for thinking_args, env in selections:
                with self.subTest(effort=effort, thinking_args=thinking_args, env=env):
                    with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
                        sys, "argv", ["autoreview", *thinking_args],
                    ):
                        if effort in {"none", "minimal"}:
                            with self.assertRaisesRegex(SystemExit, "invalid thinking level for codex model gpt-6.1-sol"):
                                AUTOREVIEW.reviewer_args(AUTOREVIEW.parse_args())
                            continue
                        reviewer = AUTOREVIEW.reviewer_args(AUTOREVIEW.parse_args())[0]
                    self.assertEqual(reviewer.model, "gpt-6.1-sol")
                    self.assertEqual(reviewer.thinking, effort)
                    self.assertEqual(reviewer.fallback_model, "gpt-6-sol")

    def test_sol_and_luna_validate_effort_and_explicit_model_selections(self) -> None:
        for model, fallback in (("gpt-6.1-sol", "gpt-6-sol"), ("gpt-6-sol", "gpt-6-luna"), ("gpt-6-luna", None)):
            selections = (
                (["--model", model], {}),
                (["--model", "codex=" + model], {}),
                ([], {"AUTOREVIEW_MODEL": model}),
                ([], {"AUTOREVIEW_CODEX_MODEL": model}),
            )
            for model_args, env in selections:
                for effort in (None, "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
                    with self.subTest(model=model, model_args=model_args, env=env, effort=effort):
                        argv = ["autoreview", *model_args]
                        if effort:
                            argv += ["--thinking", effort]
                        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "argv", argv):
                            if effort in {"minimal", "ultra"} or (model == "gpt-6.1-sol" and effort == "none"):
                                with self.assertRaisesRegex(SystemExit, f"invalid thinking level for codex model {model}"):
                                    AUTOREVIEW.reviewer_args(AUTOREVIEW.parse_args())
                                continue
                            reviewer = AUTOREVIEW.reviewer_args(AUTOREVIEW.parse_args())[0]
                        self.assertEqual(reviewer.model, model)
                        self.assertEqual(reviewer.thinking, effort or "high")
                        self.assertEqual(reviewer.fallback_model, fallback)

    def test_astra_preserves_supported_effort_and_explicit_model(self) -> None:
        selections = (
            (["--model", "gpt-6-astra"], {}),
            (["--model", "codex=gpt-6-astra"], {}),
            ([], {"AUTOREVIEW_MODEL": "gpt-6-astra"}),
            ([], {"AUTOREVIEW_CODEX_MODEL": "gpt-6-astra"}),
        )
        for model_args, env in selections:
            for effort in (None, "low", "medium", "high", "xhigh", "max"):
                with self.subTest(model_args=model_args, env=env, effort=effort):
                    argv = ["autoreview", "--engine", "codex", *model_args]
                    if effort:
                        argv += ["--thinking", effort]
                    with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "argv", argv):
                        reviewer = AUTOREVIEW.reviewer_args(AUTOREVIEW.parse_args())[0]
                    self.assertEqual(reviewer.model, "gpt-6-astra")
                    self.assertEqual(reviewer.thinking, effort or "high")
                    self.assertIsNone(reviewer.fallback_model)

    def test_astra_effort_restrictions_do_not_change_other_codex_models(self) -> None:
        for effort in ("none", "minimal"):
            with self.subTest(effort=effort), mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                sys, "argv", ["autoreview", "--engine", "codex", "--model", "gpt-5.6-sol", "--thinking", effort],
            ):
                reviewer = AUTOREVIEW.reviewer_args(AUTOREVIEW.parse_args())[0]
                self.assertEqual(reviewer.model, "gpt-5.6-sol")
                self.assertEqual(reviewer.thinking, effort)
                self.assertEqual(reviewer.fallback_model, "gpt-5.6-terra")

    @classmethod
    def setUpClass(cls) -> None:
        cls.home_dir = tempfile.TemporaryDirectory(prefix="autoreview-test-home.")
        cls.home_patch = mock.patch.object(Path, "home", return_value=Path(cls.home_dir.name))
        cls.home_patch.start()
        cls.home_keys = ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH")
        cls.old_home_env = {key: os.environ.get(key) for key in cls.home_keys}
        os.environ["HOME"] = cls.home_dir.name
        os.environ["USERPROFILE"] = cls.home_dir.name
        os.environ.pop("HOMEDRIVE", None)
        os.environ.pop("HOMEPATH", None)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.home_patch.stop()
        for key, value in cls.old_home_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls.home_dir.cleanup()

    def test_kimi_bin_cli_option(self) -> None:
        with mock.patch.object(
            sys,
            "argv",
            ["autoreview", "--kimi-bin", "/tmp/trusted-kimi"],
        ):
            args = AUTOREVIEW.parse_args()
        self.assertEqual(args.kimi_bin, "/tmp/trusted-kimi")

    def test_codex_config_status_exposes_keys_only(self) -> None:
        args = argparse.Namespace(codex_config=['model_verbosity="low"'])
        self.assertEqual(AUTOREVIEW.codex_config_keys(args), ["model_verbosity"])

    def test_codex_retries_sol_6_after_default_sol_61_access_failure(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            sys, "argv", ["autoreview", "--no-web-search"],
        ):
            args = AUTOREVIEW.reviewer_args(AUTOREVIEW.parse_args())[0]
        prompt = "complete retry pack: unicode \u03c0\r\n-deleted line\n unchanged context\n"
        with tempfile.TemporaryDirectory(prefix="autoreview-codex-fallback.") as tmpdir:
            events = []

            def fake_run(command, _cwd, **kwargs):
                self.assertEqual(kwargs["input_text"], prompt)
                model = command[command.index("--model") + 1]
                events.append(model)
                self.assertIn('model_reasoning_effort="high"', command)
                if model == "gpt-6.1-sol":
                    return subprocess.CompletedProcess(
                        command, 1, AutoreviewEfficiencyTests.usage_event(),
                        "The model `gpt-6.1-sol` does not exist or you do not have access to it.",
                    )
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(json.dumps({**FINAL_REPORT, "review_completion": "complete"}))
                return subprocess.CompletedProcess(command, 0, AutoreviewEfficiencyTests.usage_event(2), "")

            with mock.patch.object(AUTOREVIEW, "resolve_command", return_value="/usr/bin/codex"), \
                    mock.patch.object(AUTOREVIEW, "ensure_codex_isolation_supported", return_value="/usr/bin/codex"), \
                    mock.patch.object(AUTOREVIEW, "codex_auth_config_flags", return_value=[]), \
                    mock.patch.object(AUTOREVIEW, "prepare_codex_runtime_auth", return_value=None), \
                    mock.patch.object(AUTOREVIEW, "scan_outgoing_review_pack") as scanner, \
                    mock.patch.object(AUTOREVIEW, "run_with_heartbeat", side_effect=fake_run):
                result = AUTOREVIEW.run_reviewer(args, Path(tmpdir), prompt, set(), [])
                self.assertTrue(result.complete)
                self.assertEqual(result.report["findings"], [])
                scanner.assert_called_once_with(Path(tmpdir), prompt)
            self.assertEqual(events, ["gpt-6.1-sol", "gpt-6-sol"])
            usage = AUTOREVIEW.review_usage_summary(args)
            self.assertEqual(usage["attempts"], 2)
            self.assertEqual(usage["tokens"]["input_tokens"], 300)
            self.assertEqual(usage["partial_attempts"], 1)
            self.assertFalse(usage["complete"])

    def test_default_sol_61_retries_only_access_failure_without_chaining(self) -> None:
        failures = (
            ("network timeout", False),
            ("rate limit exceeded for {model}", False),
            ("model_not_available: {model} is temporarily unavailable due to capacity", False),
            ("Unsupported value: 'none' is not supported with the '{model}' model", False),
            ("The '{model}' model is not supported when using Codex with a ChatGPT account.", True),
        )
        for message, retries in failures:
            with self.subTest(message=message), mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                sys, "argv", ["autoreview", "--no-web-search"],
            ):
                args = AUTOREVIEW.reviewer_args(AUTOREVIEW.parse_args())[0]
                models = []

                def fake_run(command, *_args, **_kwargs):
                    model = command[command.index("--model") + 1]
                    models.append(model)
                    event = {"type": "error", "message": message.format(model=model)}
                    return subprocess.CompletedProcess(command, 1, json.dumps(event), "")

                with tempfile.TemporaryDirectory(prefix="autoreview-codex-fallback.") as tmpdir, \
                        mock.patch.object(AUTOREVIEW, "resolve_command", return_value="/usr/bin/codex"), \
                        mock.patch.object(AUTOREVIEW, "ensure_codex_isolation_supported", return_value="/usr/bin/codex"), \
                        mock.patch.object(AUTOREVIEW, "codex_auth_config_flags", return_value=[]), \
                        mock.patch.object(AUTOREVIEW, "prepare_codex_runtime_auth", return_value=None), \
                        mock.patch.object(AUTOREVIEW, "run_with_heartbeat", side_effect=fake_run):
                    with self.assertRaises(AUTOREVIEW.ReviewerUnavailable):
                        AUTOREVIEW.run_codex(args, Path(tmpdir), "review")
                self.assertEqual(models, ["gpt-6.1-sol", "gpt-6-sol"] if retries else ["gpt-6.1-sol"])

    def test_codex_runs_outside_repo_with_bundle_only_workspace(self) -> None:
        args = argparse.Namespace(
            codex_bin="codex",
            codex_config=None,
            codex_speed=None,
            fallback_model=None,
            model="gpt-5.6-sol",
            stream_engine_output=False,
            thinking="high",
            tools=True,
            web_search=False,
        )
        observed: dict[str, object] = {}

        def fake_run(
            command: list[str],
            cwd: Path,
            *_args: object,
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            observed["cwd"] = cwd
            observed["command"] = command
            observed["command_cwd"] = Path(command[command.index("-C") + 1])
            observed["workspace_entries"] = list(cwd.iterdir())
            observed["env"] = kwargs["env"]
            observed["schema"] = json.loads(Path(command[command.index("--output-schema") + 1]).read_text())
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text(json.dumps(FINAL_REPORT))
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory(prefix="autoreview-codex-workspace-test.") as tmpdir:
            repo = Path(tmpdir)
            (repo / ".env").write_text("ignored environment fixture\n")
            with mock.patch.dict(
                os.environ,
                {"CODEX_HOME": ""},
                clear=False,
            ), mock.patch.object(
                AUTOREVIEW,
                "resolve_command",
                return_value="/usr/bin/codex",
            ), mock.patch.object(
                AUTOREVIEW,
                "ensure_codex_isolation_supported",
                return_value="/usr/bin/codex",
            ), mock.patch.object(
                AUTOREVIEW,
                "codex_auth_config_flags",
                return_value=[],
            ), mock.patch.object(
                AUTOREVIEW,
                "prepare_codex_runtime_auth",
                return_value=None,
            ), mock.patch.object(
                AUTOREVIEW,
                "codex_source_home",
                return_value=None,
            ), mock.patch.object(
                AUTOREVIEW,
                "run_with_heartbeat",
                side_effect=fake_run,
            ):
                output = AUTOREVIEW.run_codex(args, repo, "review")

            self.assertEqual(json.loads(output), FINAL_REPORT)
            self.assertEqual(observed["schema"], AUTOREVIEW.PROVIDER_SCHEMA)
            observed_cwd = observed["cwd"]
            command_cwd = observed["command_cwd"]
            self.assertIsInstance(observed_cwd, Path)
            self.assertIsInstance(command_cwd, Path)
            assert isinstance(observed_cwd, Path)
            assert isinstance(command_cwd, Path)
            self.assertNotEqual(observed_cwd.resolve(), repo.resolve())
            self.assertEqual(observed_cwd, command_cwd)
            self.assertEqual(observed["workspace_entries"], [])
            env = observed["env"]
            self.assertIsInstance(env, dict)
            assert isinstance(env, dict)
            self.assertNotEqual(env["HOME"], os.environ.get("HOME"))
            self.assertEqual(env["USERPROFILE"], env["HOME"])
            self.assertNotEqual(env.get("CODEX_HOME"), str(repo.resolve()))
            self.assertEqual(Path(env["CODEX_HOME"]).name, "codex-home")
            self.assertNotEqual(env["CODEX_HOME"], str((Path.home() / ".codex").resolve()))
            self.assertIn("features.shell_snapshot=false", observed["command"])
            self.assertIn("features.hooks=false", observed["command"])
            self.assertIn("features.plugins=false", observed["command"])
            self.assertIn("skills.include_instructions=false", observed["command"])

    def test_codex_does_not_fallback_after_unrelated_failure(self) -> None:
        args = argparse.Namespace(
            codex_bin="codex",
            codex_config=None,
            codex_speed=None,
            fallback_model="gpt-6-luna",
            model="gpt-6-sol",
            stream_engine_output=False,
            thinking="high",
            tools=True,
            web_search=False,
        )
        models: list[str] = []

        def fake_run(command: list[str], *_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            models.append(command[command.index("--model") + 1])
            return subprocess.CompletedProcess(command, 1, "", "network timeout")

        with tempfile.TemporaryDirectory(prefix="autoreview-codex-fallback.") as tmpdir, mock.patch.object(
            AUTOREVIEW,
            "resolve_command",
            return_value="/usr/bin/codex",
        ), mock.patch.object(
            AUTOREVIEW,
            "ensure_codex_isolation_supported",
            return_value="/usr/bin/codex",
        ), mock.patch.object(AUTOREVIEW, "codex_auth_config_flags", return_value=[]), mock.patch.object(
            AUTOREVIEW,
            "prepare_codex_runtime_auth",
            return_value=None,
        ), mock.patch.object(
            AUTOREVIEW,
            "run_with_heartbeat",
            side_effect=fake_run,
        ):
            with self.assertRaisesRegex(SystemExit, "network timeout"):
                AUTOREVIEW.run_codex(args, Path(tmpdir), "review")

        self.assertEqual(models, ["gpt-6-sol"])

    def test_codex_does_not_fallback_after_model_capacity_failure(self) -> None:
        args = argparse.Namespace(
            codex_bin="codex",
            codex_config=None,
            codex_speed=None,
            fallback_model="gpt-6-luna",
            model="gpt-6-sol",
            stream_engine_output=False,
            thinking="high",
            tools=True,
            web_search=False,
        )
        models: list[str] = []

        def fake_run(command: list[str], *_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            models.append(command[command.index("--model") + 1])
            return subprocess.CompletedProcess(
                command,
                1,
                "",
                "model_not_available: gpt-6-sol is temporarily unavailable due to capacity",
            )

        with tempfile.TemporaryDirectory(prefix="autoreview-codex-fallback.") as tmpdir, mock.patch.object(
            AUTOREVIEW,
            "resolve_command",
            return_value="/usr/bin/codex",
        ), mock.patch.object(
            AUTOREVIEW,
            "ensure_codex_isolation_supported",
            return_value="/usr/bin/codex",
        ), mock.patch.object(AUTOREVIEW, "codex_auth_config_flags", return_value=[]), mock.patch.object(
            AUTOREVIEW,
            "prepare_codex_runtime_auth",
            return_value=None,
        ), mock.patch.object(
            AUTOREVIEW,
            "run_with_heartbeat",
            side_effect=fake_run,
        ):
            with self.assertRaisesRegex(SystemExit, "temporarily unavailable"):
                AUTOREVIEW.run_codex(args, Path(tmpdir), "review")

        self.assertEqual(models, ["gpt-6-sol"])

    def test_codex_access_fallback_ignores_structured_output_text(self) -> None:
        result = subprocess.CompletedProcess(
            ["codex"],
            1,
            '{"type":"agent_message","text":"gpt-5.6-sol does not exist or you do not have access"}',
            '{"type":"agent_message","message":"gpt-5.6-sol does not exist or you do not have access"}',
        )

        self.assertFalse(
            AUTOREVIEW.codex_model_access_failure(result, "gpt-5.6-sol")
        )

    def test_codex_access_fallback_accepts_terminal_error_event(self) -> None:
        result = subprocess.CompletedProcess(
            ["codex"],
            1,
            '{"type":"error","message":"gpt-5.6-sol does not exist or you do not have access"}',
            "",
        )

        self.assertTrue(
            AUTOREVIEW.codex_model_access_failure(result, "gpt-5.6-sol")
        )

    def test_codex_access_fallback_accepts_account_model_list_error(self) -> None:
        result = subprocess.CompletedProcess(
            ["codex"],
            1,
            "",
            (
                "The model gpt-5.6-sol does not appear in the list of models "
                "available to your account"
            ),
        )

        self.assertTrue(
            AUTOREVIEW.codex_model_access_failure(result, "gpt-5.6-sol")
        )

    def test_codex_access_fallback_ignores_plain_stdout(self) -> None:
        message = "gpt-5.6-sol does not exist or you do not have access"
        stdout_result = subprocess.CompletedProcess(["codex"], 1, message, "")
        stderr_result = subprocess.CompletedProcess(["codex"], 1, "", message)

        self.assertFalse(
            AUTOREVIEW.codex_model_access_failure(stdout_result, "gpt-5.6-sol")
        )
        self.assertTrue(
            AUTOREVIEW.codex_model_access_failure(stderr_result, "gpt-5.6-sol")
        )

    def test_extract_json_accepts_dict_result_payload(self) -> None:
        payload = {
            "type": "result",
            "subtype": "success",
            "result": FINAL_REPORT,
            "session_id": "session-id",
            "request_id": "request-id",
        }
        self.assertEqual(AUTOREVIEW.extract_json(json.dumps(payload)), FINAL_REPORT)

    def test_extract_json_rejects_result_string_with_preamble(self) -> None:
        payload = {
            "type": "result",
            "subtype": "success",
            "result": "Inspecting the diff first.\n" + json.dumps(FINAL_REPORT),
        }
        with self.assertRaisesRegex(SystemExit, "result was not structured JSON"):
            AUTOREVIEW.extract_json(json.dumps(payload))

if __name__ == "__main__":
    unittest.main()
